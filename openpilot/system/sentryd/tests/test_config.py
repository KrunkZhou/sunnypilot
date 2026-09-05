import fcntl
import os
import threading
import time
from contextlib import contextmanager

import pytest

from openpilot.system.sentryd.config import CURRENT_CONSENT_VERSION, SCHEMA_VERSION, SentryConfig, SentryConfigError, SentryConfigStore


def make_store(tmp_path):
  return SentryConfigStore(tmp_path / "sentry", tmp_path / "legacy")


def test_initializes_individual_private_files_and_requires_consent(tmp_path) -> None:
  store = make_store(tmp_path)
  config = store.initialize()
  assert not config.effective_enabled
  assert config.wait_for_driver_exit
  assert (store.config_dir / "wait_for_driver_exit").read_text() == "1\n"
  assert sorted(path.name for path in store.config_dir.iterdir()) == [
    "capture_upload_consent_version", "enabled", "motion_threshold_mps2", "schema_version", "wait_for_driver_exit", "warning_persistence_seconds",
  ]
  assert all((path.stat().st_mode & 0o777) == 0o600 for path in store.config_dir.iterdir())
  assert (store.root.stat().st_mode & 0o777) == 0o700
  assert (store.config_dir.stat().st_mode & 0o777) == 0o700
  assert (store.lock_path.stat().st_mode & 0o777) == 0o600
  with pytest.raises(SentryConfigError, match="consent"):
    store.set_enabled(True)
  assert store.enable_with_consent().effective_enabled
  assert store.load().capture_upload_consent_version == CURRENT_CONSENT_VERSION


def test_existing_sentry_root_without_config_fails_closed_until_explicit_reset(tmp_path) -> None:
  root = tmp_path / "sentry"
  root.mkdir(mode=0o700)
  (root / "outbox.sqlite3").write_bytes(b"existing queue")
  store = SentryConfigStore(root, tmp_path / "legacy")
  with pytest.raises(SentryConfigError, match="configuration directory is missing"):
    store.initialize()
  assert not store.config_dir.exists()
  assert (root / "outbox.sqlite3").read_bytes() == b"existing queue"

  config, quarantine = store.reset()
  assert config == SentryConfig() and quarantine is None
  assert (root / "outbox.sqlite3").read_bytes() == b"existing queue"


def test_consent_is_committed_before_enablement(tmp_path, monkeypatch) -> None:
  store = make_store(tmp_path)
  store.initialize()
  original = store._write_field_locked

  def fail_enable(name, value):
    if name == "enabled":
      raise SentryConfigError("simulated disk failure")
    original(name, value)

  monkeypatch.setattr(store, "_write_field_locked", fail_enable)
  with pytest.raises(SentryConfigError, match="disk failure"):
    store.enable_with_consent()
  config = store.load()
  assert not config.enabled
  assert config.capture_upload_consent_version == CURRENT_CONSENT_VERSION


@pytest.mark.parametrize(("setter_name", "attribute", "value"), [
  ("set_motion_threshold", "motion_threshold_mps2", 0.02),
  ("set_motion_threshold", "motion_threshold_mps2", 0.04),
  ("set_motion_threshold", "motion_threshold_mps2", 0.08),
  ("set_warning_persistence", "warning_persistence_seconds", 0.5),
  ("set_warning_persistence", "warning_persistence_seconds", 1.0),
  ("set_warning_persistence", "warning_persistence_seconds", 2.0),
  ("set_warning_persistence", "warning_persistence_seconds", 5.0),
  ("set_wait_for_driver_exit", "wait_for_driver_exit", False),
  ("set_wait_for_driver_exit", "wait_for_driver_exit", True),
])
def test_every_allowed_tuning_value_round_trips(tmp_path, setter_name, attribute, value) -> None:
  store = make_store(tmp_path)
  store.initialize()
  updated = getattr(store, setter_name)(value)
  assert getattr(updated, attribute) == value
  assert getattr(store.load(), attribute) == value


def test_pre_replace_file_sync_failure_retains_old_value_and_cleans_temporary_file(tmp_path, monkeypatch) -> None:
  store = make_store(tmp_path)
  store.initialize()
  original_fsync = os.fsync
  failed = False

  def fail_first_sync(fd):
    nonlocal failed
    if not failed:
      failed = True
      raise OSError(28, "No space left on device")
    original_fsync(fd)

  monkeypatch.setattr(os, "fsync", fail_first_sync)
  with pytest.raises(SentryConfigError, match="No space left on device"):
    store.set_motion_threshold(0.08)
  monkeypatch.setattr(os, "fsync", original_fsync)
  assert store.load().motion_threshold_mps2 == 0.04
  assert not any(path.name.startswith(".motion_threshold_mps2.") for path in store.config_dir.iterdir())


def test_post_replace_directory_sync_failure_surfaces_but_new_value_is_authoritative(tmp_path, monkeypatch) -> None:
  store = make_store(tmp_path)
  store.initialize()
  original_sync = store._fsync_directory

  def fail_config_directory_sync(path):
    if path == store.config_dir:
      raise SentryConfigError("simulated configuration directory sync failure")
    original_sync(path)

  monkeypatch.setattr(store, "_fsync_directory", fail_config_directory_sync)
  with pytest.raises(SentryConfigError, match="directory sync failure"):
    store.set_warning_persistence(5.0)
  monkeypatch.setattr(store, "_fsync_directory", original_sync)
  assert store.load().warning_persistence_seconds == 5.0


@pytest.mark.parametrize(("field", "value"), [
  ("enabled", "yes\n"),
  ("schema_version", "3\n"),
  ("schema_version", "01\n"),
  ("capture_upload_consent_version", "+1\n"),
  ("motion_threshold_mps2", "nan\n"),
  ("motion_threshold_mps2", "0.001\n"),
  ("motion_threshold_mps2", "0.010\n"),
  ("motion_threshold_mps2", "0.040\n"),
  ("warning_persistence_seconds", "3\n"),
  ("warning_persistence_seconds", "1.0\n"),
  ("warning_persistence_seconds", " 1\n"),
  ("warning_persistence_seconds", "1\n\n"),
  ("wait_for_driver_exit", "true\n"),
  ("wait_for_driver_exit", "2\n"),
  ("wait_for_driver_exit", "01\n"),
])
def test_invalid_configuration_fails_closed_without_rewrite(tmp_path, field, value) -> None:
  store = make_store(tmp_path)
  store.initialize()
  path = store.config_dir / field
  path.write_text(value)
  os.chmod(path, 0o600)
  with pytest.raises(SentryConfigError):
    store.load()
  assert path.read_text() == value


def test_missing_and_symlink_fields_are_rejected(tmp_path) -> None:
  store = make_store(tmp_path)
  store.initialize()
  (store.config_dir / "enabled").unlink()
  with pytest.raises(SentryConfigError, match="missing"):
    store.load()
  (store.config_dir / "enabled").symlink_to(store.config_dir / "schema_version")
  with pytest.raises(SentryConfigError):
    store.load()


def test_missing_fixed_lock_fails_closed_until_explicit_reset(tmp_path) -> None:
  store = make_store(tmp_path)
  store.initialize()
  store.lock_path.unlink()
  with pytest.raises(SentryConfigError, match="cannot open.*lock"):
    store.load()
  with pytest.raises(SentryConfigError, match="cannot open.*lock"):
    store.initialize()
  config, quarantine = store.reset()
  assert config == SentryConfig() and quarantine is not None
  assert store.load() == SentryConfig()


def test_symlinked_fixed_lock_is_rejected_without_touching_target(tmp_path) -> None:
  store = make_store(tmp_path)
  store.initialize()
  store.lock_path.unlink()
  outside = tmp_path / "outside.lock"
  outside.write_bytes(b"outside")
  store.lock_path.symlink_to(outside)
  with pytest.raises(SentryConfigError, match="cannot open.*lock"):
    store.load()
  assert outside.read_bytes() == b"outside"


def test_oversized_configuration_field_is_rejected(tmp_path) -> None:
  store = make_store(tmp_path)
  store.initialize()
  field = store.config_dir / "enabled"
  field.write_bytes(b"1" * 129)
  os.chmod(field, 0o600)
  with pytest.raises(SentryConfigError, match="too large"):
    store.load()


def test_reset_quarantines_corrupt_config_and_keeps_other_data(tmp_path) -> None:
  store = make_store(tmp_path)
  store.initialize()
  (store.root / "outbox.sqlite3").write_bytes(b"queue")
  (store.config_dir / "enabled").write_text("broken")
  os.chmod(store.config_dir / "enabled", 0o600)
  config, quarantine = store.reset()
  assert quarantine is not None and (quarantine / "enabled").read_text() == "broken"
  assert not config.effective_enabled
  assert (store.root / "outbox.sqlite3").read_bytes() == b"queue"


@pytest.mark.parametrize("legacy_high", ["0.01", "0.02"])
def test_imports_only_valid_legacy_tuning_and_requires_fresh_consent(tmp_path, legacy_high) -> None:
  legacy = tmp_path / "legacy"
  legacy.mkdir()
  (legacy / "SentryModeSensitivity").write_text(legacy_high)
  (legacy / "SentryModeWarningTime").write_text("2")
  (legacy / "SentryModeEnabled").write_text("1")
  (legacy / "SentryModeConsentVersion").write_text("1")
  store = SentryConfigStore(tmp_path / "sentry", legacy)
  config = store.initialize()
  assert config.motion_threshold_mps2 == 0.02
  assert config.warning_persistence_seconds == 2.0
  assert not config.enabled and config.capture_upload_consent_version == 0
  assert not (legacy / "SentryModeSensitivity").exists()
  assert not (legacy / "SentryModeWarningTime").exists()
  assert not (legacy / "SentryModeEnabled").exists()
  assert not (legacy / "SentryModeConsentVersion").exists()


def test_invalid_regular_legacy_settings_are_removed_only_after_safe_defaults_commit(tmp_path) -> None:
  legacy = tmp_path / "legacy"
  legacy.mkdir()
  for name in ("SentryModeSensitivity", "SentryModeWarningTime", "SentryModeEnabled", "SentryModeConsentVersion"):
    (legacy / name).write_text("invalid")
  store = SentryConfigStore(tmp_path / "sentry", legacy)
  config = store.initialize()
  assert config == SentryConfig()
  assert not any((legacy / name).exists() for name in (
    "SentryModeSensitivity", "SentryModeWarningTime", "SentryModeEnabled", "SentryModeConsentVersion"))


def test_legacy_symlink_is_not_imported_or_removed(tmp_path) -> None:
  legacy = tmp_path / "legacy"
  legacy.mkdir()
  target = tmp_path / "outside"
  target.write_text("0.02")
  (legacy / "SentryModeSensitivity").symlink_to(target)
  store = SentryConfigStore(tmp_path / "sentry", legacy)
  assert store.initialize().motion_threshold_mps2 == 0.04
  assert (legacy / "SentryModeSensitivity").is_symlink()


def test_legacy_is_not_removed_until_root_directory_is_synced(tmp_path, monkeypatch) -> None:
  legacy = tmp_path / "legacy"
  legacy.mkdir()
  (legacy / "SentryModeSensitivity").write_text("0.02")
  store = SentryConfigStore(tmp_path / "sentry", legacy)
  operations = []
  original_sync = store._fsync_directory
  original_remove = store._remove_legacy

  def record_sync(path):
    operations.append(("sync", path))
    original_sync(path)

  def record_remove(paths):
    operations.append(("remove", None))
    original_remove(paths)

  monkeypatch.setattr(store, "_fsync_directory", record_sync)
  monkeypatch.setattr(store, "_remove_legacy", record_remove)
  store.initialize()
  parent_sync = operations.index(("sync", store.root.parent))
  root_sync = operations.index(("sync", store.root))
  removal = operations.index(("remove", None))
  assert parent_sync < removal and root_sync < removal


@pytest.mark.parametrize(("target", "mode"), [
  ("field", 0o700),
  ("config_dir", 0o1700),
  ("root", 0o1700),
  ("lock", 0o640),
])
def test_existing_configuration_requires_exact_private_modes(tmp_path, target, mode) -> None:
  store = make_store(tmp_path)
  store.initialize()
  path = {
    "field": store.config_dir / "enabled",
    "config_dir": store.config_dir,
    "root": store.root,
    "lock": store.lock_path,
  }[target]
  os.chmod(path, mode)
  with pytest.raises(SentryConfigError, match="permissions"):
    store.load()


def test_concurrent_updates_never_expose_partial_files(tmp_path) -> None:
  store = make_store(tmp_path)
  store.initialize()
  errors = []

  def update(value):
    try:
      for _ in range(20):
        store.set_motion_threshold(value)
        store.load()
    except Exception as exc:  # pragma: no cover - assertion reports the concrete race
      errors.append(exc)

  threads = [threading.Thread(target=update, args=(value,)) for value in (0.02, 0.04, 0.08)]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join()
  assert errors == []
  assert store.load().motion_threshold_mps2 in (0.02, 0.04, 0.08)


@pytest.mark.parametrize("entrypoint", ["initialize", "load"])
@pytest.mark.parametrize("enabled", [False, True])
def test_saved_high_migrates_only_threshold_and_preserves_consent_and_data(tmp_path, entrypoint, enabled):
  store = make_store(tmp_path)
  store.initialize()
  if enabled:
    store.enable_with_consent()
  store.set_warning_persistence(5.0)
  threshold = store.config_dir / "motion_threshold_mps2"
  threshold.write_text("0.01\n")
  original = {path.name: path.read_bytes() for path in store.config_dir.iterdir() if path != threshold}
  lock_inode = store.lock_path.stat().st_ino
  (store.root / "outbox.sqlite3").write_bytes(b"pending queue")
  media = store.root / "media"
  media.mkdir(mode=0o700)
  (media / "capture.jpg").write_bytes(b"queued media")

  config = getattr(store, entrypoint)()
  assert config.motion_threshold_mps2 == 0.02 and config.effective_enabled == enabled
  assert threshold.read_text() == "0.02\n" and threshold.stat().st_mode & 0o777 == 0o600
  assert {path.name: path.read_bytes() for path in store.config_dir.iterdir() if path != threshold} == original
  assert store.lock_path.stat().st_ino == lock_inode
  assert (store.root / "outbox.sqlite3").read_bytes() == b"pending queue"
  assert (media / "capture.jpg").read_bytes() == b"queued media"
  inode = threshold.stat().st_ino
  assert store.initialize() == store.load() == config
  assert threshold.stat().st_ino == inode  # migration is not repeated


@pytest.mark.parametrize("entrypoint", ["initialize", "load"])
@pytest.mark.parametrize("corruption", ["missing", "future", "invalid", "symlink"])
def test_high_migration_never_overwrites_incomplete_or_corrupt_configuration(tmp_path, entrypoint, corruption):
  store = make_store(tmp_path)
  store.initialize()
  threshold = store.config_dir / "motion_threshold_mps2"
  threshold.write_text("0.01\n")
  if corruption == "missing":
    (store.config_dir / "enabled").unlink()
  elif corruption == "future":
    (store.config_dir / "schema_version").write_text("3\n")
  elif corruption == "invalid":
    (store.config_dir / "warning_persistence_seconds").write_text("bad\n")
  else:
    threshold.unlink()
    outside = tmp_path / "outside"
    outside.write_text("0.01\n")
    threshold.symlink_to(outside)
  with pytest.raises(SentryConfigError):
    getattr(store, entrypoint)()
  assert threshold.read_text() == "0.01\n"


def test_high_migration_sync_failure_surfaces_and_can_retry(tmp_path, monkeypatch):
  store = make_store(tmp_path)
  store.initialize()
  threshold = store.config_dir / "motion_threshold_mps2"
  threshold.write_text("0.01\n")
  original_fsync = os.fsync

  def disk_full(_fd):
    raise OSError(28, "No space left on device")

  monkeypatch.setattr(os, "fsync", disk_full)
  with pytest.raises(SentryConfigError, match="motion_threshold_mps2.*No space left"):
    store.load()
  assert threshold.read_text() == "0.01\n"
  assert not any(path.name.startswith(".motion_threshold_mps2.") for path in store.config_dir.iterdir())
  monkeypatch.setattr(os, "fsync", original_fsync)
  assert store.load().motion_threshold_mps2 == 0.02


def test_high_migration_revalidates_after_shared_reader_releases_lock(tmp_path, monkeypatch):
  store = make_store(tmp_path)
  store.initialize()
  (store.config_dir / "motion_threshold_mps2").write_text("0.01\n")
  original_lock = store._prepared_exclusive_lock
  writer = make_store(tmp_path)

  @contextmanager
  def intervening_selection():
    writer.set_motion_threshold(0.08)
    with original_lock():
      yield

  monkeypatch.setattr(store, "_prepared_exclusive_lock", intervening_selection)
  assert store.load().motion_threshold_mps2 == 0.08
  assert (store.config_dir / "motion_threshold_mps2").read_text() == "0.08\n"


def test_concurrent_high_readers_commit_only_one_migration(tmp_path, monkeypatch):
  store = make_store(tmp_path)
  store.initialize()
  (store.config_dir / "motion_threshold_mps2").write_text("0.01\n")
  original_write = store._write_field_locked
  writes, errors, results = [], [], []
  barrier = threading.Barrier(4)

  def write(name, value):
    writes.append((name, value))
    original_write(name, value)

  def load():
    try:
      barrier.wait(timeout=2)
      results.append(store.load().motion_threshold_mps2)
    except Exception as exc:
      errors.append(exc)

  monkeypatch.setattr(store, "_write_field_locked", write)
  threads = [threading.Thread(target=load) for _ in range(4)]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join(timeout=2)
  assert not any(thread.is_alive() for thread in threads)
  assert errors == [] and results == [0.02] * 4
  assert writes == [("motion_threshold_mps2", 0.02)]


def test_bounded_reader_fails_closed_on_contended_config_lock(tmp_path) -> None:
  writer = make_store(tmp_path)
  writer.initialize()
  reader = SentryConfigStore(writer.root, writer.legacy_params_dir, lock_timeout_seconds=0.05)
  locked = threading.Event()
  release = threading.Event()

  def hold_lock():
    with writer._lock(exclusive=True):
      locked.set()
      release.wait(1)

  thread = threading.Thread(target=hold_lock)
  thread.start()
  assert locked.wait(1)
  started = time.monotonic()
  try:
    with pytest.raises(SentryConfigError, match="timed out"):
      reader.load()
    assert time.monotonic() - started < 0.5
  finally:
    release.set()
    thread.join(1)


def test_existing_configuration_does_not_recreate_missing_lock(tmp_path) -> None:
  store = make_store(tmp_path)
  store.initialize()
  store.lock_path.unlink()

  with pytest.raises(SentryConfigError, match="cannot open Sentry configuration lock"):
    store.load()
  with pytest.raises(SentryConfigError, match="cannot open Sentry configuration lock"):
    store.set_enabled(False)
  assert not store.lock_path.exists()

  # Explicit reset is the sole recovery path allowed to recreate the fixed
  # lock inode and writes a complete disabled configuration.
  reset, quarantined = store.reset()
  assert reset == SentryConfig()
  assert quarantined is not None
  assert store.lock_path.stat().st_mode & 0o777 == 0o600
  assert store.load() == SentryConfig()


def test_unlinked_held_lock_cannot_be_replaced_by_an_ordinary_reader(tmp_path) -> None:
  store = make_store(tmp_path)
  store.initialize()
  fd = os.open(store.lock_path, os.O_RDWR)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    store.lock_path.unlink()
    reader = SentryConfigStore(store.root, store.legacy_params_dir, lock_timeout_seconds=0.05)
    with pytest.raises(SentryConfigError, match="cannot open Sentry configuration lock"):
      reader.load()
    assert not store.lock_path.exists()
  finally:
    os.close(fd)


def downgrade_to_schema_one(store):
  (store.config_dir / "wait_for_driver_exit").unlink()
  (store.config_dir / "schema_version").write_text("1\n")


@pytest.mark.parametrize("entrypoint", ["initialize", "load"])
@pytest.mark.parametrize("legacy_high", [False, True])
def test_schema_one_migration_preserves_config_and_queue(tmp_path, entrypoint, legacy_high):
  store = make_store(tmp_path)
  store.initialize()
  store.enable_with_consent()
  store.set_motion_threshold(0.08)
  store.set_warning_persistence(5.0)
  downgrade_to_schema_one(store)
  if legacy_high:
    (store.config_dir / "motion_threshold_mps2").write_text("0.01\n")
  preserved = {name: (store.config_dir / name).read_bytes() for name in (
    "enabled", "capture_upload_consent_version", "warning_persistence_seconds")}
  (store.root / "outbox.sqlite3").write_bytes(b"pending queue")
  media = store.root / "media"
  media.mkdir(mode=0o700)
  (media / "capture.jpg").write_bytes(b"pending image")
  lock_inode = store.lock_path.stat().st_ino

  result = getattr(store, entrypoint)()
  assert result.schema_version == SCHEMA_VERSION == 2
  assert result.wait_for_driver_exit and result.effective_enabled
  assert result.motion_threshold_mps2 == (0.02 if legacy_high else 0.08)
  assert {name: (store.config_dir / name).read_bytes() for name in preserved} == preserved
  assert (store.config_dir / "wait_for_driver_exit").read_text() == "1\n"
  assert (store.config_dir / "wait_for_driver_exit").stat().st_mode & 0o777 == 0o600
  assert store.lock_path.stat().st_ino == lock_inode
  assert (store.root / "outbox.sqlite3").read_bytes() == b"pending queue"
  assert (media / "capture.jpg").read_bytes() == b"pending image"


@pytest.mark.parametrize(("method", "args", "attribute", "value"), [
  ("set_enabled", (False,), "enabled", False),
  ("enable_with_consent", (), "enabled", True),
  ("set_motion_threshold", (0.08,), "motion_threshold_mps2", 0.08),
  ("set_warning_persistence", (5.0,), "warning_persistence_seconds", 5.0),
  ("set_wait_for_driver_exit", (False,), "wait_for_driver_exit", False),
])
def test_setters_complete_pending_schema_migration(tmp_path, method, args, attribute, value):
  store = make_store(tmp_path)
  store.initialize()
  downgrade_to_schema_one(store)
  updated = getattr(store, method)(*args)
  assert updated.schema_version == SCHEMA_VERSION
  assert getattr(updated, attribute) == value
  assert store.load() == updated


@pytest.mark.parametrize("value", [0, 1, "0", "1", None, [], 0.0])
def test_driver_exit_setter_rejects_non_boolean_types(tmp_path, value):
  store = make_store(tmp_path)
  store.initialize()
  with pytest.raises(SentryConfigError, match="must be a boolean"):
    store.set_wait_for_driver_exit(value)
  assert store.load().wait_for_driver_exit


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize("corruption", ["malformed", "oversized", "symlink", "directory", "fifo", "permissions"])
def test_driver_exit_configuration_fails_closed_without_repair(tmp_path, schema, corruption):
  store = make_store(tmp_path)
  store.initialize()
  (store.config_dir / "schema_version").write_text(f"{schema}\n")
  field = store.config_dir / "wait_for_driver_exit"
  if corruption == "malformed":
    field.write_text("true\n")
  elif corruption == "oversized":
    field.write_bytes(b"1" * 129)
  elif corruption == "symlink":
    field.unlink()
    field.symlink_to(tmp_path / "missing-target")
  elif corruption == "directory":
    field.unlink()
    field.mkdir(mode=0o700)
  elif corruption == "fifo":
    field.unlink()
    os.mkfifo(field, mode=0o600)
  else:
    field.chmod(0o640)
  original = field.lstat()
  with pytest.raises(SentryConfigError):
    store.load()
  assert (field.lstat().st_ino, field.lstat().st_mode) == (original.st_ino, original.st_mode)
  assert (store.config_dir / "schema_version").read_text() == f"{schema}\n"


def test_schema_two_missing_driver_exit_field_is_not_migrated_or_repaired(tmp_path):
  store = make_store(tmp_path)
  store.initialize()
  field = store.config_dir / "wait_for_driver_exit"
  field.unlink()
  for action in (store.load, store.initialize, lambda: store.set_wait_for_driver_exit(False)):
    with pytest.raises(SentryConfigError, match="missing.*wait_for_driver_exit"):
      action()
  assert not field.exists()


@pytest.mark.parametrize("corruption", ["missing", "invalid", "symlink"])
def test_schema_one_validates_all_existing_fields_before_staging_new_field(tmp_path, corruption):
  store = make_store(tmp_path)
  store.initialize()
  downgrade_to_schema_one(store)
  field = store.config_dir / "enabled"
  if corruption == "invalid":
    field.write_text("invalid\n")
  else:
    field.unlink()
    if corruption == "symlink":
      field.symlink_to(store.config_dir / "schema_version")
  with pytest.raises(SentryConfigError):
    store.load()
  assert not (store.config_dir / "wait_for_driver_exit").exists()
  assert (store.config_dir / "schema_version").read_text() == "1\n"


@pytest.mark.parametrize("staged_value", ["0", "1"])
def test_schema_one_recovers_staged_field_by_syncing_before_schema_marker(tmp_path, monkeypatch, staged_value):
  store = make_store(tmp_path)
  store.initialize()
  downgrade_to_schema_one(store)
  field = store.config_dir / "wait_for_driver_exit"
  field.write_text(f"{staged_value}\n")
  field.chmod(0o600)
  inode = field.stat().st_ino
  operations = []
  original_fsync, original_write = os.fsync, store._write_field_locked

  def record_fsync(fd):
    operations.append(("fsync", os.fstat(fd).st_ino))
    original_fsync(fd)

  def record_write(name, value):
    operations.append(("write", name))
    original_write(name, value)

  monkeypatch.setattr(os, "fsync", record_fsync)
  monkeypatch.setattr(store, "_write_field_locked", record_write)
  result = store.load()
  assert result.wait_for_driver_exit is (staged_value == "1")
  assert result.schema_version == SCHEMA_VERSION
  assert field.stat().st_ino == inode
  assert operations.index(("fsync", inode)) < operations.index(("fsync", store.config_dir.stat().st_ino))
  assert operations.index(("fsync", store.config_dir.stat().st_ino)) < operations.index(("write", "schema_version"))
  assert ("write", "wait_for_driver_exit") not in operations


@pytest.mark.parametrize("failure_stage", ["new_file", "new_directory", "marker", "staged_file", "staged_directory"])
def test_schema_migration_sync_failures_leave_retryable_old_marker(tmp_path, monkeypatch, failure_stage):
  store = make_store(tmp_path)
  store.initialize()
  downgrade_to_schema_one(store)
  field = store.config_dir / "wait_for_driver_exit"
  if failure_stage.startswith("staged"):
    field.write_text("0\n")
    field.chmod(0o600)
  original_fsync, original_sync, original_write = os.fsync, store._fsync_directory, store._write_field_locked

  def fail_sync(_fd):
    raise OSError(28, "simulated fsync failure")

  def fail_directory(path):
    if path == store.config_dir:
      raise SentryConfigError("simulated directory fsync failure")
    original_sync(path)

  def fail_marker(name, value):
    if name == "schema_version":
      raise SentryConfigError("simulated crash before schema marker")
    original_write(name, value)

  if failure_stage.endswith("file"):
    monkeypatch.setattr(os, "fsync", fail_sync)
  elif failure_stage.endswith("directory"):
    monkeypatch.setattr(store, "_fsync_directory", fail_directory)
  else:
    monkeypatch.setattr(store, "_write_field_locked", fail_marker)
  with pytest.raises(SentryConfigError, match="simulated"):
    store.load()
  assert (store.config_dir / "schema_version").read_text() == "1\n"
  monkeypatch.setattr(os, "fsync", original_fsync)
  monkeypatch.setattr(store, "_fsync_directory", original_sync)
  monkeypatch.setattr(store, "_write_field_locked", original_write)
  result = store.load()
  assert result.schema_version == SCHEMA_VERSION
  assert result.wait_for_driver_exit is (not failure_stage.startswith("staged"))


def test_concurrent_schema_one_migration_commits_once(tmp_path, monkeypatch):
  store = make_store(tmp_path)
  store.initialize()
  downgrade_to_schema_one(store)
  original_write = store._write_field_locked
  writes, errors, results = [], [], []
  barrier = threading.Barrier(4)

  def record_write(name, value):
    writes.append((name, value))
    original_write(name, value)

  def load():
    try:
      barrier.wait(timeout=2)
      results.append(store.load())
    except Exception as exc:
      errors.append(exc)

  monkeypatch.setattr(store, "_write_field_locked", record_write)
  threads = [threading.Thread(target=load) for _ in range(4)]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join(timeout=2)
  assert not any(thread.is_alive() for thread in threads)
  assert errors == [] and results == [SentryConfig()] * 4
  assert writes == [("wait_for_driver_exit", True), ("schema_version", SCHEMA_VERSION)]


def test_schema_marker_directory_sync_failure_surfaces_without_erasing_complete_configuration(tmp_path, monkeypatch):
  store = make_store(tmp_path)
  store.initialize()
  downgrade_to_schema_one(store)
  original_sync = store._fsync_directory

  def fail_marker_sync(path):
    if path == store.config_dir and (path / "schema_version").read_text() == "2\n":
      raise SentryConfigError("simulated schema marker directory fsync failure")
    original_sync(path)

  monkeypatch.setattr(store, "_fsync_directory", fail_marker_sync)
  with pytest.raises(SentryConfigError, match="schema marker directory fsync failure"):
    store.load()
  monkeypatch.setattr(store, "_fsync_directory", original_sync)
  assert store.load() == SentryConfig()
  assert (store.config_dir / "wait_for_driver_exit").read_text() == "1\n"
