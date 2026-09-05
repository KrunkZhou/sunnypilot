import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time

import pytest

from openpilot.system.sentryd.store import SentryStore
from openpilot.system.sentryd.tests.test_store import begin, finish_complete


pytestmark = pytest.mark.skipif(os.name != "posix", reason="SQLite UNIX WAL lock regression requires POSIX file locks")


LOCK_PROBE = """
import errno
import fcntl
import os
import sys

fd = os.open(sys.argv[1], os.O_RDWR)
try:
  try:
    # SQLite's UNIX WAL deadman-switch byte. Only probe the lock: never map,
    # resize, or access SQLite through this independent descriptor.
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 128, os.SEEK_SET)
  except OSError as exc:
    if exc.errno not in (errno.EACCES, errno.EAGAIN):
      raise
    print("blocked")
  else:
    print("acquired")
finally:
  os.close(fd)
"""


UI_READER = """
from pathlib import Path
import sys
import time

from openpilot.system.sentryd.store import read_outbox_stats

database, ready_path, stop_path = map(Path, sys.argv[1:])
ready_path.touch()
reads = 0
deadline = time.monotonic() + 15
while not stop_path.exists() and time.monotonic() < deadline:
  stats = read_outbox_stats(database)
  assert stats.pending >= 1
  reads += 1
  time.sleep(0.002)
print(reads)
"""


def assert_deadman_lock_held(path: Path) -> None:
  # Use exec, not a forked Python target that inherits a live SQLite connection.
  result = subprocess.run([sys.executable, "-c", LOCK_PROBE, str(path) + "-shm"],
                          capture_output=True, text=True, timeout=10, check=True)
  assert result.stdout.strip() == "blocked", "SQLite's live WAL-SHM deadman lock was released outside SQLite"


@pytest.mark.parametrize("operation", ["sidecar_validation", "uploader_connection", "capture_finalization"])
def test_sidecar_operations_preserve_live_sqlite_deadman_lock(tmp_path, monkeypatch, operation) -> None:
  path = tmp_path / "outbox.sqlite3"
  # Preserve SQLite's initial lock even when running this test against the old
  # buggy helper. The real operation below must not release that initial lock.
  with monkeypatch.context() as patch:
    patch.setattr(SentryStore, "_secure_sidecars", lambda self: None)
    store = SentryStore(path, run_maintenance=False)
  try:
    assert_deadman_lock_held(path)
    if operation == "sidecar_validation":
      store._secure_sidecars()
    elif operation == "uploader_connection":
      worker_store = SentryStore(path, run_maintenance=False)
      worker_store.close()
    else:
      event_id = begin(store)
      finish_complete(store, event_id)
    assert_deadman_lock_held(path)
  finally:
    store.close()


def test_ui_reader_writer_and_uploader_preserve_pending_media(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  reader = None
  worker = None
  worker_stop = threading.Event()
  worker_started = threading.Event()
  worker_errors = []
  claimed_events = []
  expected_metadata = {}
  ready_path, stop_path = tmp_path / "reader-ready", tmp_path / "reader-stop"

  def record_capture() -> None:
    event_id = begin(store)
    finish_complete(store, event_id)
    expected_metadata[event_id] = store.connection.execute(
      "SELECT metadata_json FROM revisions WHERE event_id=? AND revision=1", (event_id,),
    ).fetchone()[0]

  def upload_worker() -> None:
    try:
      while not worker_stop.is_set():
        uploader_store = SentryStore(path, run_maintenance=False)
        try:
          claimed = uploader_store.claim_pending(1)
          if claimed is not None:
            claimed_events.append(claimed.event_id)
            uploader_store.schedule_retry(claimed.event_id, claimed.revision, next_attempt_at=0, error="test network unavailable")
        finally:
          uploader_store.close()
        worker_started.set()
        worker_stop.wait(0.002)
    except Exception as exc:
      worker_errors.append(exc)
      worker_started.set()

  try:
    record_capture()
    # Fail safely on old code before exposing its dropped lock to a real second
    # SQLite process, which could otherwise SIGBUS the pytest process itself.
    assert_deadman_lock_held(path)
    reader = subprocess.Popen([sys.executable, "-c", UI_READER, str(path), str(ready_path), str(stop_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready_deadline = time.monotonic() + 5
    while not ready_path.exists() and reader.poll() is None and time.monotonic() < ready_deadline:
      time.sleep(0.005)
    assert ready_path.exists(), "UI-like outbox reader did not start"

    worker = threading.Thread(target=upload_worker, name="sentry-lock-test-uploader", daemon=True)
    worker.start()
    assert worker_started.wait(5), "Uploader-like connection did not start"
    assert not worker_errors
    for _ in range(24):
      record_capture()
      time.sleep(0.005)
      assert reader.poll() is None, "UI-like outbox reader exited during concurrent capture writes"

    worker_stop.set()
    worker.join(timeout=15)
    assert not worker.is_alive(), "Uploader-like worker did not stop"
    assert not worker_errors
    assert claimed_events, "Concurrent uploader never claimed queued work"

    stop_path.touch()
    stdout, stderr = reader.communicate(timeout=10)
    assert reader.returncode == 0, stderr
    assert int(stdout.strip()) > 1
    assert_deadman_lock_held(path)
    assert [row[0] for row in store.connection.execute("PRAGMA integrity_check")] == ["ok"]
    rows = store.connection.execute("SELECT event_id, metadata_json, state FROM revisions").fetchall()
    assert {row["event_id"]: row["metadata_json"] for row in rows} == expected_metadata
    assert all(row["state"] == "ready" for row in rows)
    media_rows = store.connection.execute("SELECT event_id, role, path, sha256, size FROM media").fetchall()
    assert len(media_rows) == len(expected_metadata) * 2
    for row in media_rows:
      media_path = Path(row["path"])
      content = media_path.read_bytes()
      assert len(content) == row["size"]
      assert hashlib.sha256(content).hexdigest() == row["sha256"]
      manifest = json.loads(expected_metadata[row["event_id"]])["media"]
      assert any(item["role"] == row["role"] and item["sha256"] == row["sha256"] for item in manifest)
      assert stat.S_IMODE(media_path.stat().st_mode) == 0o600
    assert store.stats().pending == len(expected_metadata)
  finally:
    worker_stop.set()
    if worker is not None:
      worker.join(timeout=15)
    stop_path.touch()
    if reader is not None and reader.poll() is None:
      reader.terminate()
      reader.communicate(timeout=5)
    store.close()
