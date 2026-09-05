from __future__ import annotations

import fcntl
import math
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


SCHEMA_VERSION = 2
CURRENT_CONSENT_VERSION = 1
DEFAULT_ROOT = "/data/sentry"
MAX_CONFIG_FILE_SIZE = 128
SENSITIVITY_TO_THRESHOLD = {"high": 0.02, "standard": 0.04, "low": 0.08}
ALLOWED_MOTION_THRESHOLDS = tuple(SENSITIVITY_TO_THRESHOLD.values())
LEGACY_HIGH_MOTION_THRESHOLD = 0.01
READABLE_MOTION_THRESHOLDS = (*ALLOWED_MOTION_THRESHOLDS, LEGACY_HIGH_MOTION_THRESHOLD)
ALLOWED_WARNING_PERSISTENCE = (0.5, 1.0, 2.0, 5.0)

CONFIG_FIELDS = (
  "schema_version",
  "enabled",
  "capture_upload_consent_version",
  "motion_threshold_mps2",
  "warning_persistence_seconds",
  "wait_for_driver_exit",
)

LEGACY_FIELDS = (
  "SentryModeEnabled",
  "SentryModeConsentVersion",
  "SentryModeSensitivity",
  "SentryModeWarningTime",
)


class SentryConfigError(RuntimeError):
  pass


@dataclass(frozen=True)
class SentryConfig:
  schema_version: int = SCHEMA_VERSION
  enabled: bool = False
  capture_upload_consent_version: int = 0
  motion_threshold_mps2: float = 0.04
  warning_persistence_seconds: float = 1.0
  wait_for_driver_exit: bool = True

  @property
  def effective_enabled(self) -> bool:
    return self.enabled and self.capture_upload_consent_version == CURRENT_CONSENT_VERSION


def default_sentry_root() -> Path:
  return Path(os.environ.get("SENTRY_ROOT", DEFAULT_ROOT))


def _parse_exact_float(value: str, allowed: tuple[float, ...], field: str) -> float:
  try:
    parsed = float(value)
  except ValueError as exc:
    raise SentryConfigError(f"{field} must be a number") from exc
  if not math.isfinite(parsed) or parsed not in allowed:
    choices = ", ".join(format(choice, "g") for choice in allowed)
    raise SentryConfigError(f"{field} must be one of: {choices}")
  return parsed


def _parse_canonical_integer(value: str, field: str) -> int:
  if not value or not all("0" <= character <= "9" for character in value) or (len(value) > 1 and value[0] == "0"):
    raise SentryConfigError(f"{field} must be a canonical non-negative integer")
  return int(value)


def _parse_canonical_choice(value: str, allowed: tuple[float, ...], field: str) -> float:
  serialized = {format(choice, "g"): choice for choice in allowed}
  try:
    return serialized[value]
  except KeyError as exc:
    choices = ", ".join(serialized)
    raise SentryConfigError(f"{field} must be one of: {choices}") from exc


def _serialize_field(name: str, value: object) -> str:
  if name == "schema_version":
    if type(value) is not int or value != SCHEMA_VERSION:
      raise SentryConfigError(f"unsupported schema version: {value!r}")
    return str(value)
  if name in ("enabled", "wait_for_driver_exit"):
    if type(value) is not bool:
      raise SentryConfigError(f"{name} must be a boolean")
    return "1" if value else "0"
  if name == "capture_upload_consent_version":
    if type(value) is not int or value < 0 or value > CURRENT_CONSENT_VERSION:
      raise SentryConfigError("invalid capture/upload consent version")
    return str(value)
  if name == "motion_threshold_mps2":
    return format(_parse_exact_float(str(value), ALLOWED_MOTION_THRESHOLDS, name), "g")
  if name == "warning_persistence_seconds":
    return format(_parse_exact_float(str(value), ALLOWED_WARNING_PERSISTENCE, name), "g")
  raise SentryConfigError(f"unknown Sentry configuration field: {name}")


class SentryConfigStore:
  def __init__(self, root: str | Path | None = None, legacy_params_dir: str | Path | None = None,
               *, lock_timeout_seconds: float | None = None):
    self.root = Path(root) if root is not None else default_sentry_root()
    self.config_dir = self.root / "config"
    self.lock_path = self.root / "config.lock"
    if legacy_params_dir is None:
      params_root = Path(os.environ.get("PARAMS_ROOT", "/data/params"))
      legacy_params_dir = params_root / os.environ.get("OPENPILOT_PREFIX", "d")
    self.legacy_params_dir = Path(legacy_params_dir)
    self.lock_timeout_seconds = lock_timeout_seconds

  def initialize(self) -> SentryConfig:
    root_created = self._ensure_directory(self.root)
    if root_created:
      self._fsync_directory(self.root.parent)
    elif not (self.config_dir.exists() or self.config_dir.is_symlink()):
      raise SentryConfigError(
        "Sentry configuration directory is missing from an existing Sentry data folder; reset Sentry settings to recover")
    with self._lock(exclusive=True, create=root_created):
      if self.config_dir.exists() or self.config_dir.is_symlink():
        return self._migrate_locked(self._load_locked())

      migrated, imported_paths = self._legacy_values()
      self._ensure_directory(self.config_dir)
      self._write_complete_locked(migrated)
      # Persist the config directory entry itself before deleting legacy data.
      self._fsync_directory(self.root)
      self._remove_legacy(imported_paths)
      return migrated

  def load(self) -> SentryConfig:
    self._validate_directory(self.root, "Sentry root")
    with self._lock(exclusive=False, create=False):
      current = self._load_locked()
    if current.schema_version == SCHEMA_VERSION and current.motion_threshold_mps2 != LEGACY_HIGH_MOTION_THRESHOLD:
      return current
    # Never upgrade a shared flock in place: concurrent readers could deadlock.
    # Revalidate the entire configuration after acquiring the writer lock so a
    # newer sensitivity selection (or corruption) cannot be overwritten.
    with self._prepared_exclusive_lock():
      return self._migrate_locked(self._load_locked())

  def _migrate_locked(self, current: SentryConfig) -> SentryConfig:
    if current.motion_threshold_mps2 == LEGACY_HIGH_MOTION_THRESHOLD:
      threshold = SENSITIVITY_TO_THRESHOLD["high"]
      self._write_field_locked("motion_threshold_mps2", threshold)
      current = replace(current, motion_threshold_mps2=threshold)
    if current.schema_version == 1:
      # A previous attempt may have committed the new field but not the schema
      # marker. Preserve a valid staged choice, and sync it again before marking
      # the migration complete; never repair an invalid or unsafe staged file.
      staged = self._read_field_locked("wait_for_driver_exit", missing_value="", sync=True)
      if staged == "":
        self._write_field_locked("wait_for_driver_exit", current.wait_for_driver_exit)
      elif staged not in ("0", "1"):
        raise SentryConfigError("wait_for_driver_exit must be 0 or 1")
      else:
        current = replace(current, wait_for_driver_exit=staged == "1")
        self._fsync_directory(self.config_dir)
      self._write_field_locked("schema_version", SCHEMA_VERSION)
      current = replace(current, schema_version=SCHEMA_VERSION)
    return current

  def set_enabled(self, enabled: bool) -> SentryConfig:
    with self._prepared_exclusive_lock():
      current = self._migrate_locked(self._load_locked())
      if enabled and current.capture_upload_consent_version != CURRENT_CONSENT_VERSION:
        raise SentryConfigError("capture and upload consent is required before enabling Sentry Mode")
      updated = replace(current, enabled=enabled)
      self._write_field_locked("enabled", enabled)
      return updated

  def enable_with_consent(self) -> SentryConfig:
    with self._prepared_exclusive_lock():
      current = self._migrate_locked(self._load_locked())
      # Consent is committed first. A crash before the second write remains disabled.
      self._write_field_locked("capture_upload_consent_version", CURRENT_CONSENT_VERSION)
      self._write_field_locked("enabled", True)
      return replace(current, enabled=True, capture_upload_consent_version=CURRENT_CONSENT_VERSION)

  def set_motion_threshold(self, value: float) -> SentryConfig:
    value = _parse_exact_float(str(value), ALLOWED_MOTION_THRESHOLDS, "motion_threshold_mps2")
    with self._prepared_exclusive_lock():
      current = self._migrate_locked(self._load_locked())
      self._write_field_locked("motion_threshold_mps2", value)
      return replace(current, motion_threshold_mps2=value)

  def set_warning_persistence(self, value: float) -> SentryConfig:
    value = _parse_exact_float(str(value), ALLOWED_WARNING_PERSISTENCE, "warning_persistence_seconds")
    with self._prepared_exclusive_lock():
      current = self._migrate_locked(self._load_locked())
      self._write_field_locked("warning_persistence_seconds", value)
      return replace(current, warning_persistence_seconds=value)

  def set_wait_for_driver_exit(self, value: bool) -> SentryConfig:
    _serialize_field("wait_for_driver_exit", value)
    with self._prepared_exclusive_lock():
      current = self._migrate_locked(self._load_locked())
      self._write_field_locked("wait_for_driver_exit", value)
      return replace(current, wait_for_driver_exit=value)

  def reset(self) -> tuple[SentryConfig, Path | None]:
    if self._ensure_directory(self.root):
      self._fsync_directory(self.root.parent)
    quarantined = None
    with self._lock(exclusive=True, create=True):
      if self.config_dir.exists() or self.config_dir.is_symlink():
        suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        quarantined = self.root / f"config.quarantine.{suffix}.{uuid4().hex[:8]}"
        os.rename(self.config_dir, quarantined)
        self._fsync_directory(self.root)
      self._ensure_directory(self.config_dir)
      config = SentryConfig()
      self._write_complete_locked(config)
      self._fsync_directory(self.root)
    return config, quarantined

  @contextmanager
  def _prepared_exclusive_lock(self) -> Iterator[None]:
    self._validate_directory(self.root, "Sentry root")
    with self._lock(exclusive=True, create=False):
      yield

  @contextmanager
  def _lock(self, *, exclusive: bool, create: bool = False) -> Iterator[None]:
    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
      flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
      flags |= os.O_NOFOLLOW
    created = False
    try:
      if create:
        try:
          fd = os.open(self.lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
          created = True
        except FileExistsError:
          fd = os.open(self.lock_path, flags)
      else:
        fd = os.open(self.lock_path, flags)
    except OSError as exc:
      raise SentryConfigError(f"cannot open Sentry configuration lock: {exc}") from exc
    try:
      info = os.fstat(fd)
      if not stat.S_ISREG(info.st_mode):
        raise SentryConfigError("Sentry configuration lock is not a regular file")
      if created:
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        self._fsync_directory(self.root)
      elif stat.S_IMODE(info.st_mode) != 0o600:
        raise SentryConfigError("Sentry configuration lock has unsafe permissions")
      path_info = self.lock_path.lstat()
      if (stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode) or
          (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino)):
        raise SentryConfigError("Sentry configuration lock changed while opening")
      operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
      if self.lock_timeout_seconds is None:
        fcntl.flock(fd, operation)
      else:
        deadline = time.monotonic() + self.lock_timeout_seconds
        while True:
          try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            break
          except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
              raise SentryConfigError("timed out waiting for the Sentry configuration lock") from exc
            time.sleep(min(0.01, remaining))
      locked_path_info = self.lock_path.lstat()
      if (stat.S_ISLNK(locked_path_info.st_mode) or not stat.S_ISREG(locked_path_info.st_mode) or
          (locked_path_info.st_dev, locked_path_info.st_ino) != (info.st_dev, info.st_ino)):
        raise SentryConfigError("Sentry configuration lock changed while waiting")
      yield
    except OSError as exc:
      raise SentryConfigError(f"cannot lock Sentry configuration: {exc}") from exc
    finally:
      os.close(fd)

  def _load_locked(self) -> SentryConfig:
    self._validate_directory(self.config_dir, "Sentry configuration directory")
    schema_version = _parse_canonical_integer(self._read_field_locked("schema_version"), "schema_version")
    if schema_version not in (1, SCHEMA_VERSION):
      raise SentryConfigError(f"unsupported Sentry configuration schema version: {schema_version}")
    values = {name: self._read_field_locked(name) for name in CONFIG_FIELDS
              if name not in ("schema_version", "wait_for_driver_exit")}
    values["wait_for_driver_exit"] = self._read_field_locked(
      "wait_for_driver_exit", missing_value="1" if schema_version == 1 else None)
    if values["enabled"] not in ("0", "1"):
      raise SentryConfigError("enabled must be 0 or 1")
    if values["wait_for_driver_exit"] not in ("0", "1"):
      raise SentryConfigError("wait_for_driver_exit must be 0 or 1")
    consent = _parse_canonical_integer(values["capture_upload_consent_version"],
                                       "capture_upload_consent_version")
    if consent < 0 or consent > CURRENT_CONSENT_VERSION:
      raise SentryConfigError("invalid capture/upload consent version")
    threshold = _parse_canonical_choice(values["motion_threshold_mps2"], READABLE_MOTION_THRESHOLDS,
                                        "motion_threshold_mps2")
    warning = _parse_canonical_choice(values["warning_persistence_seconds"], ALLOWED_WARNING_PERSISTENCE,
                                      "warning_persistence_seconds")
    return SentryConfig(
      schema_version=schema_version,
      enabled=values["enabled"] == "1",
      capture_upload_consent_version=consent,
      motion_threshold_mps2=threshold,
      warning_persistence_seconds=warning,
      wait_for_driver_exit=values["wait_for_driver_exit"] == "1",
    )

  def _read_field_locked(self, name: str, *, missing_value: str | None = None, sync: bool = False) -> str:
    path = self.config_dir / name
    # Nonblocking open lets fstat reject a substituted FIFO instead of hanging
    # the settings reader or a migration indefinitely.
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
      flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
      flags |= os.O_NOFOLLOW
    try:
      fd = os.open(path, flags)
    except FileNotFoundError as exc:
      if missing_value is not None and not path.is_symlink():
        return missing_value
      raise SentryConfigError(f"missing Sentry configuration field: {name}") from exc
    except OSError as exc:
      raise SentryConfigError(f"cannot read Sentry configuration field {name}: {exc}") from exc
    try:
      info = os.fstat(fd)
      if not stat.S_ISREG(info.st_mode):
        raise SentryConfigError(f"Sentry configuration field {name} is not a regular file")
      if info.st_size > MAX_CONFIG_FILE_SIZE:
        raise SentryConfigError(f"Sentry configuration field {name} is too large")
      if stat.S_IMODE(info.st_mode) != 0o600:
        raise SentryConfigError(f"Sentry configuration field {name} has unsafe permissions")
      value = os.read(fd, MAX_CONFIG_FILE_SIZE + 1)
      if sync:
        os.fsync(fd)
    finally:
      os.close(fd)
    try:
      decoded = value.decode("ascii")
    except UnicodeDecodeError as exc:
      raise SentryConfigError(f"Sentry configuration field {name} is not ASCII") from exc
    if decoded.endswith("\n"):
      decoded = decoded[:-1]
    if not decoded or any(character.isspace() or character == "\x00" for character in decoded):
      raise SentryConfigError(f"Sentry configuration field {name} has invalid content")
    return decoded

  def _write_complete_locked(self, config: SentryConfig) -> None:
    # Write enablement last so a partial initialization can never arm Sentry Mode.
    fields: tuple[tuple[str, object], ...] = (
      ("schema_version", config.schema_version),
      ("capture_upload_consent_version", config.capture_upload_consent_version),
      ("motion_threshold_mps2", config.motion_threshold_mps2),
      ("warning_persistence_seconds", config.warning_persistence_seconds),
      ("wait_for_driver_exit", config.wait_for_driver_exit),
      ("enabled", config.enabled),
    )
    for name, value in fields:
      self._write_field_locked(name, value)

  def _write_field_locked(self, name: str, value: object) -> None:
    self._validate_directory(self.config_dir, "Sentry configuration directory")
    target = self.config_dir / name
    if target.exists() or target.is_symlink():
      try:
        info = target.lstat()
      except OSError as exc:
        raise SentryConfigError(f"cannot inspect Sentry configuration field {name}: {exc}") from exc
      if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SentryConfigError(f"Sentry configuration field {name} is not a regular file")

    data = (_serialize_field(name, value) + "\n").encode("ascii")
    fd = -1
    temporary = ""
    try:
      fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=self.config_dir)
      os.fchmod(fd, 0o600)
      written = os.write(fd, data)
      if written != len(data):
        raise OSError("short configuration write")
      os.fsync(fd)
      os.close(fd)
      fd = -1
      os.replace(temporary, target)
      temporary = ""
      self._fsync_directory(self.config_dir)
    except OSError as exc:
      raise SentryConfigError(f"cannot write Sentry configuration field {name}: {exc}") from exc
    finally:
      if fd >= 0:
        os.close(fd)
      if temporary:
        try:
          os.unlink(temporary)
        except FileNotFoundError:
          pass

  def _legacy_values(self) -> tuple[SentryConfig, tuple[Path, ...]]:
    config = SentryConfig()
    removable = tuple(
      self.legacy_params_dir / name for name in LEGACY_FIELDS
      if self._legacy_path_is_safe(self.legacy_params_dir / name)
    )
    sensitivity = self._read_legacy("SentryModeSensitivity")
    if sensitivity is not None:
      try:
        threshold = _parse_exact_float(sensitivity, READABLE_MOTION_THRESHOLDS, "motion_threshold_mps2")
        if threshold == LEGACY_HIGH_MOTION_THRESHOLD:
          threshold = SENSITIVITY_TO_THRESHOLD["high"]
        config = replace(config, motion_threshold_mps2=threshold)
      except SentryConfigError:
        pass
    warning = self._read_legacy("SentryModeWarningTime")
    if warning is not None:
      try:
        config = replace(config, warning_persistence_seconds=_parse_exact_float(
          warning, ALLOWED_WARNING_PERSISTENCE, "warning_persistence_seconds"))
      except SentryConfigError:
        pass
    # Never migrate enablement or consent. Both require a new explicit consent flow.
    return config, removable

  @staticmethod
  def _legacy_path_is_safe(path: Path) -> bool:
    try:
      info = path.lstat()
      return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    except OSError:
      return False

  def _read_legacy(self, name: str) -> str | None:
    path = self.legacy_params_dir / name
    try:
      info = path.lstat()
      if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_FILE_SIZE:
        return None
      flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
      fd = os.open(path, flags)
      try:
        return os.read(fd, MAX_CONFIG_FILE_SIZE + 1).decode("ascii").strip()
      finally:
        os.close(fd)
    except (OSError, UnicodeError):
      return None

  def _remove_legacy(self, imported: tuple[Path, ...]) -> None:
    for path in imported:
      try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
          path.unlink()
      except OSError:
        # Migration is complete and safe even if cleanup cannot be performed.
        pass
    if imported:
      try:
        self._fsync_directory(self.legacy_params_dir)
      except SentryConfigError:
        pass

  @staticmethod
  def _ensure_directory(path: Path) -> bool:
    try:
      created = False
      try:
        path.mkdir(mode=0o700, parents=True)
        created = True
      except FileExistsError:
        pass
      info = path.lstat()
      if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SentryConfigError(f"{path} is not a safe directory")
      if created:
        os.chmod(path, 0o700)
      elif stat.S_IMODE(info.st_mode) != 0o700:
        raise SentryConfigError(f"{path} has unsafe permissions")
      return created
    except OSError as exc:
      raise SentryConfigError(f"cannot prepare Sentry directory {path}: {exc}") from exc

  @staticmethod
  def _validate_directory(path: Path, label: str) -> None:
    try:
      info = path.lstat()
    except FileNotFoundError as exc:
      raise SentryConfigError(f"{label} is missing") from exc
    except OSError as exc:
      raise SentryConfigError(f"cannot inspect {label.lower()}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
      raise SentryConfigError(f"{label} is not a safe directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
      raise SentryConfigError(f"{label} has unsafe permissions")

  @staticmethod
  def _fsync_directory(path: Path) -> None:
    try:
      fd = os.open(path, os.O_RDONLY)
      try:
        os.fsync(fd)
      finally:
        os.close(fd)
    except OSError as exc:
      raise SentryConfigError(f"cannot sync Sentry directory {path}: {exc}") from exc
