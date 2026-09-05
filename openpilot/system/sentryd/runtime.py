from __future__ import annotations

import fcntl
import math
import os
import stat
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
  from openpilot.common.params import Params


RUNTIME_KEYS = (
  "SentryRuntimeEnabled",
  "SentryCaptureLease",
  "SentryRuntimeStatus",
  "SentryRuntimeCommand",
)
MAX_CAPTURE_LEASE_SECONDS = 20.0


def runtime_root() -> str:
  shm_root = "/tmp" if sys.platform == "darwin" else "/dev/shm"
  return os.environ.get("SENTRY_RUNTIME_ROOT", str(Path(shm_root) / "sentry_params"))


def runtime_params() -> Params:
  from openpilot.common.params import ParamKeyType, Params, UnknownKeyName, ensure_bytes

  key_types = {
    "SentryRuntimeEnabled": ParamKeyType.BOOL,
    "SentryCaptureLease": ParamKeyType.JSON,
    "SentryRuntimeStatus": ParamKeyType.JSON,
    "SentryRuntimeCommand": ParamKeyType.JSON,
  }

  class SentryRuntimeParams(Params):
    """Params-compatible volatile store that also works with prebuilt libparams."""

    def check_key(self, key):
      encoded = ensure_bytes(key)
      try:
        name = encoded.decode("ascii")
      except (UnicodeDecodeError, AttributeError) as exc:
        raise UnknownKeyName(encoded) from exc
      if b"\0" in encoded or name not in key_types:
        raise UnknownKeyName(encoded)
      return encoded

    def get_type(self, key):
      encoded = self.check_key(key)
      return key_types[encoded.decode("ascii")]

    def _default(self, key):
      self.check_key(key)
      return None

  # Params' C write/read operations do not require keys to be in its compiled
  # map; only the Python type/check layer does. The local override is necessary
  # on prebuilt releases whose libparams predates these volatile key names.
  return SentryRuntimeParams(runtime_root())


def clear_runtime(params: Params | None = None) -> None:
  params = params or runtime_params()
  for key in RUNTIME_KEYS:
    try:
      params.remove(key)
    except (OSError, RuntimeError):
      pass


def set_runtime_enabled(enabled: bool, params: Params | None = None) -> None:
  params = params or runtime_params()
  params.put_bool("SentryRuntimeEnabled", enabled, block=True)
  # Some prebuilt Params bindings ignore the C write return code. Verify the
  # manager-visible value before the daemon treats the demand as published.
  if params.get_bool("SentryRuntimeEnabled") != enabled:
    raise RuntimeError("Could not update Sentry sensor demand")


def runtime_enabled(params: Params | None = None) -> bool:
  try:
    return (params or runtime_params()).get_bool("SentryRuntimeEnabled")
  except (OSError, RuntimeError):
    return False


def set_status(status: dict[str, Any], params: Params | None = None) -> None:
  (params or runtime_params()).put("SentryRuntimeStatus", status, block=True)


def get_status(params: Params | None = None) -> dict[str, Any] | None:
  try:
    value = (params or runtime_params()).get("SentryRuntimeStatus")
    return value if isinstance(value, dict) else None
  except (OSError, RuntimeError, ValueError):
    return None


def set_capture_lease(request_id: str, expires_at: float, params: Params | None = None) -> None:
  if not request_id or not math.isfinite(expires_at):
    raise ValueError("invalid Sentry capture lease")
  (params or runtime_params()).put("SentryCaptureLease", {
    "request_id": request_id,
    "expires_at_monotonic": expires_at,
  }, block=True)


def clear_capture_lease(params: Params | None = None) -> None:
  try:
    (params or runtime_params()).remove("SentryCaptureLease")
  except (OSError, RuntimeError):
    pass


def capture_lease_active(params: Params | None = None, now: float | None = None) -> bool:
  try:
    value = (params or runtime_params()).get("SentryCaptureLease")
  except (OSError, RuntimeError, ValueError):
    return False
  if not isinstance(value, dict) or not isinstance(value.get("request_id"), str) or not value["request_id"]:
    return False
  expires_at = value.get("expires_at_monotonic")
  if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool) or not math.isfinite(expires_at):
    return False
  now = time.monotonic() if now is None else now
  # Reject implausibly long leases as well as expired ones.
  return now < expires_at <= now + MAX_CAPTURE_LEASE_SECONDS + 1.0


def take_command(params: Params | None = None) -> dict[str, Any] | None:
  params = params or runtime_params()
  try:
    value = params.get("SentryRuntimeCommand")
    if value is not None:
      params.remove("SentryRuntimeCommand")
    return value if isinstance(value, dict) else None
  except (OSError, RuntimeError, ValueError):
    return None


class CameraOperationLock(AbstractContextManager["CameraOperationLock"]):
  """Cross-process guard for starting an offroad stream or Sentry capture."""

  def __init__(self, *, blocking: bool = False, path: str | Path | None = None):
    self.blocking = blocking
    shm_root = "/tmp" if sys.platform == "darwin" else "/dev/shm"
    self.path = Path(path) if path is not None else Path(os.environ.get(
      "SENTRY_CAMERA_LOCK", str(Path(shm_root) / "camera_operation.lock")))
    self.fd = -1

  def __enter__(self) -> CameraOperationLock:
    self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
      flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
      flags |= os.O_NOFOLLOW
    self.fd = os.open(self.path, flags, 0o600)
    info = os.fstat(self.fd)
    if not stat.S_ISREG(info.st_mode):
      os.close(self.fd)
      self.fd = -1
      raise RuntimeError("camera operation lock is not a regular file")
    os.fchmod(self.fd, 0o600)
    operation = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
    try:
      fcntl.flock(self.fd, operation)
    except BlockingIOError:
      os.close(self.fd)
      self.fd = -1
      raise
    return self

  def __exit__(self, exc_type, exc_value, traceback) -> None:
    if self.fd >= 0:
      os.close(self.fd)
      self.fd = -1
