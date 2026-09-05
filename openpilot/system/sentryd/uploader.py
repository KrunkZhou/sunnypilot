from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import math
import os
import random
import stat
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol

import requests

from openpilot.common.api.base import BaseApi
from openpilot.common.api.comma_connect import API_HOST
from openpilot.common.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.common.version import get_version
from openpilot.system.sentryd.store import MAX_MEDIA_BYTES, QueuedMedia, QueuedRevision, SentryStore


INITIAL_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 60.0 * 60.0
MAX_ACK_BYTES = 64 * 1024
MAX_ES256_PUBLIC_KEY_BYTES = 4096
TRANSIENT_STATUSES = frozenset((408, 425, 429))


def _wall_timestamp() -> float:
  return datetime.now(UTC).timestamp()


class SentryTransport(Protocol):
  def post_event(self, dongle_id: str, revision: QueuedRevision, files: dict[str, BinaryIO]): ...


class ES256DeviceApi(BaseApi):
  def __init__(self, dongle_id: str, api_host: str = API_HOST):
    super().__init__(dongle_id, api_host)

  @staticmethod
  def get_key_pair() -> tuple[str, str, str] | tuple[None, None, None]:
    private_path = Paths.persist_root() + "/comma/id_ecdsa"
    public_path = private_path + ".pub"
    try:
      with open(private_path) as private, open(public_path) as public:
        return "ES256", private.read(), public.read()
    except OSError:
      return None, None, None


class RequestsSentryTransport:
  def __init__(self, dongle_id: str, *, api: BaseApi | None = None, es256_api: BaseApi | None = None,
               es256_public_key_path: str | Path | None = None, session: requests.Session | None = None):
    # The ordinary BaseApi preserves whichever device key RTZ registered
    # (historically RSA is preferred). A separate ES256 proof lets RTZ safely
    # bind the P-256 key needed by Sentry without breaking existing devices.
    self.api = api or BaseApi(dongle_id, API_HOST)
    self.es256_api = es256_api or ES256DeviceApi(dongle_id, self.api.api_host)
    self.es256_public_key_path = Path(es256_public_key_path) if es256_public_key_path is not None else (
      Path(Paths.persist_root()) / "comma" / "id_ecdsa.pub")
    self.session = session or requests.Session()

  def post_event(self, dongle_id: str, revision: QueuedRevision, files: dict[str, BinaryIO]):
    multipart: dict[str, object] = {
      "event": (None, json.dumps(revision.metadata, allow_nan=False, separators=(",", ":"), sort_keys=True), "application/json"),
    }
    for role, handle in files.items():
      multipart[role] = (f"{role}.jpg", handle, "image/jpeg")
    es256_public_key = self._read_es256_public_key(self.es256_public_key_path)
    try:
      primary_token = self.api.get_token()
      es256_token = self.es256_api.get_token({"sentry": True})
      if not isinstance(primary_token, str) or not primary_token or not isinstance(es256_token, str) or not es256_token:
        raise ValueError("device API returned an invalid token")
    except Exception as exc:
      # Normalize PyJWT/key-backend failures into the uploader's normal durable
      # retry path without exposing key material in the recorded error.
      raise RuntimeError("Sentry device authentication token could not be generated") from exc
    return self.session.post(
      f"{self.api.api_host}/v1/devices/{dongle_id}/sentry-events",
      headers={
        "Authorization": "JWT " + primary_token,
        "X-RTZ-Sentry-ES256-Token": es256_token,
        "X-RTZ-Sentry-ES256-Public-Key": base64.b64encode(es256_public_key).decode("ascii"),
        "User-Agent": self.api.user_agent + self.api.remove_non_ascii_chars(get_version()),
      },
      files=multipart,
      timeout=(10, 30),
      allow_redirects=False,
      stream=True,
    )

  @staticmethod
  def _read_es256_public_key(path: Path) -> bytes:
    try:
      before = path.lstat()
    except OSError as exc:
      raise OSError(f"Sentry ES256 public key is unavailable: {path}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
      raise OSError(f"Sentry ES256 public key is not a regular file: {path}")
    if not 0 < before.st_size <= MAX_ES256_PUBLIC_KEY_BYTES:
      raise OSError(f"Sentry ES256 public key has an invalid size: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
      flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
      flags |= os.O_NOFOLLOW
    try:
      fd = os.open(path, flags)
    except OSError as exc:
      raise OSError(f"Sentry ES256 public key could not be opened: {path}") from exc
    try:
      opened = os.fstat(fd)
      if (not stat.S_ISREG(opened.st_mode) or
          (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or
          opened.st_size != before.st_size):
        raise OSError(f"Sentry ES256 public key changed while opening: {path}")
      chunks = []
      remaining = MAX_ES256_PUBLIC_KEY_BYTES + 1
      while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
          break
        chunks.append(chunk)
        remaining -= len(chunk)
      value = b"".join(chunks)
      after = path.lstat()
      if (stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode) or
          (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino) or
          after.st_size != opened.st_size):
        raise OSError(f"Sentry ES256 public key changed while reading: {path}")
      if len(value) != opened.st_size or len(value) > MAX_ES256_PUBLIC_KEY_BYTES:
        raise OSError(f"Sentry ES256 public key has an invalid size: {path}")
      return value
    finally:
      os.close(fd)


class SentryUploader:
  def __init__(self, dongle_id: str, store: SentryStore, transport: SentryTransport | None = None,
               *, random_uniform=random.uniform, wall_clock=_wall_timestamp):
    self.dongle_id = dongle_id
    self.store = store
    self.transport = transport or RequestsSentryTransport(dongle_id)
    self.random_uniform = random_uniform
    self.wall_clock = wall_clock

  def upload_once(self, now: float | None = None) -> bool | None:
    fixed_now = now is not None
    claim_now = self.wall_clock() if now is None else now
    revision = self.store.claim_pending(claim_now)
    if revision is None:
      return None

    try:
      with ExitStack() as stack:
        files = {item.role: stack.enter_context(self._open_media(item, self.store.media_root)) for item in revision.media}
        response = self.transport.post_event(self.dongle_id, revision, files)
    except (AssertionError, OSError, requests.RequestException, RuntimeError, ValueError) as exc:
      cloudlog.warning("Sentry event upload failed: %s", exc)
      retry_now = claim_now if fixed_now else self.wall_clock()
      self._schedule_retry(revision, retry_now, str(exc), None, None)
      return False

    try:
      if response.status_code in (200, 201):
        try:
          acknowledgement = self._parse_ack(response, revision)
        except ValueError as exc:
          retry_now = claim_now if fixed_now else self.wall_clock()
          self._schedule_retry(
            revision, retry_now, str(exc), response.status_code, getattr(response, "headers", {}).get("Retry-After"),
          )
          return False
        if self.store.acknowledge(
            revision.event_id, revision.revision, acknowledgement["media"], deleted=acknowledgement["deleted"]):
          return True
        retry_now = claim_now if fixed_now else self.wall_clock()
        self._schedule_retry(
          revision, retry_now, "acknowledgement no longer matches queued revision", response.status_code, None,
        )
        return False

      if response.status_code in TRANSIENT_STATUSES or 500 <= response.status_code <= 599:
        retry_now = claim_now if fixed_now else self.wall_clock()
        self._schedule_retry(
          revision, retry_now, f"RTZ returned HTTP {response.status_code}", response.status_code,
          getattr(response, "headers", {}).get("Retry-After"),
        )
        return False

      self.store.mark_terminal(
        revision.event_id, revision.revision,
        error=f"RTZ returned terminal HTTP {response.status_code}", http_status=response.status_code,
      )
      return False
    finally:
      try:
        response.close()
      except Exception:
        cloudlog.warning("Could not close Sentry upload response")

  def _schedule_retry(self, revision: QueuedRevision, now: float, error: str, http_status: int | None,
                      retry_after: str | None) -> None:
    exponent = min(revision.attempts, 10)
    base = min(INITIAL_RETRY_SECONDS * (2 ** exponent), MAX_RETRY_SECONDS)
    delay = min(base + self.random_uniform(0.0, base * 0.25), MAX_RETRY_SECONDS)
    parsed_retry_after = self._retry_after_seconds(retry_after, now)
    if parsed_retry_after is not None:
      delay = max(delay, parsed_retry_after)
    self.store.schedule_retry(
      revision.event_id, revision.revision,
      next_attempt_at=now + min(delay, MAX_RETRY_SECONDS), error=error, http_status=http_status,
    )

  @staticmethod
  def _parse_ack(response, revision: QueuedRevision) -> dict[str, object]:
    try:
      try:
        content = response.raw.read(MAX_ACK_BYTES + 1, decode_content=True)
      except TypeError:
        content = response.raw.read(MAX_ACK_BYTES + 1)
    except (AttributeError, OSError, requests.RequestException) as exc:
      raise ValueError("RTZ acknowledgement could not be read") from exc
    if not isinstance(content, bytes):
      raise ValueError("RTZ acknowledgement body is invalid")
    if len(content) > MAX_ACK_BYTES:
      raise ValueError("RTZ acknowledgement is too large")
    try:
      payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
      raise ValueError("RTZ returned invalid acknowledgement JSON") from exc
    if not isinstance(payload, dict):
      raise ValueError("RTZ acknowledgement must be an object")
    if payload.get("event_id") != revision.event_id or payload.get("revision") != revision.revision:
      raise ValueError("RTZ acknowledgement identifies a different revision")
    deleted = payload.get("deleted")
    if type(deleted) is not bool:
      raise ValueError("RTZ acknowledgement has invalid deletion state")
    acknowledged_media = payload.get("media")
    if not isinstance(acknowledged_media, list):
      raise ValueError("RTZ acknowledgement has invalid media")
    media: dict[str, str] = {}
    for item in acknowledged_media:
      if not isinstance(item, dict) or set(item) != {"role", "sha256"}:
        raise ValueError("RTZ acknowledgement has invalid media entry")
      role, digest = item["role"], item["sha256"]
      if role not in ("wide", "cabin") or role in media or not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("RTZ acknowledgement has invalid media digest")
      media[role] = digest
    if deleted:
      if media:
        raise ValueError("deleted RTZ acknowledgement cannot include media")
    elif media != {item.role: item.sha256 for item in revision.media}:
      raise ValueError("RTZ acknowledgement media does not match queued revision")
    return {"deleted": deleted, "media": media}

  @staticmethod
  def _retry_after_seconds(value: str | None, now: float) -> float | None:
    if not value:
      return None
    value = value.strip()
    try:
      seconds = float(value)
      if math.isfinite(seconds) and seconds >= 0:
        return min(seconds, MAX_RETRY_SECONDS)
    except ValueError:
      pass
    try:
      retry_at = email.utils.parsedate_to_datetime(value)
      if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
      return min(max(0.0, retry_at.timestamp() - now), MAX_RETRY_SECONDS)
    except (TypeError, ValueError, OverflowError):
      return None

  @staticmethod
  def _open_media(item: QueuedMedia, media_root: Path) -> BinaryIO:
    media_root = media_root.absolute()
    path = Path(item.path).absolute()
    try:
      relative = path.relative_to(media_root)
    except ValueError as exc:
      raise ValueError(f"queued {item.role} media is outside the Sentry media directory") from exc
    if len(relative.parts) != 3 or relative.parts[-1] != f"{item.role}.jpg":
      raise ValueError(f"queued {item.role} media has an invalid path")
    for directory in (media_root, path.parent.parent, path.parent):
      directory_info = directory.lstat()
      if (stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode) or
          stat.S_IMODE(directory_info.st_mode) != 0o700):
        raise ValueError(f"queued {item.role} media has an unsafe parent directory")
    path_info = path.lstat()
    if (stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode) or
        stat.S_IMODE(path_info.st_mode) != 0o600):
      raise ValueError(f"queued {item.role} media has unsafe permissions or type")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
      flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
      flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
      info = os.fstat(fd)
      if ((info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino) or
          not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
          info.st_size != item.size or info.st_size > MAX_MEDIA_BYTES):
        raise ValueError(f"queued {item.role} media changed on disk")
      digest = hashlib.sha256()
      while chunk := os.read(fd, 128 * 1024):
        digest.update(chunk)
      if digest.hexdigest() != item.sha256:
        raise ValueError(f"queued {item.role} media hash changed on disk")
      os.lseek(fd, 0, os.SEEK_SET)
      return os.fdopen(fd, "rb")
    except Exception:
      os.close(fd)
      raise
