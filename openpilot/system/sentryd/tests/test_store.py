import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from openpilot.system.sentryd.store import CaptureDiscarded, MAX_REVISION, MediaData, SentryStore, read_outbox_stats
from openpilot.system.sentryd import store as store_module


JPEG = b"\xff\xd8test-image\xff\xd9"
NOW = datetime(2026, 9, 4, tzinfo=UTC).isoformat()


def begin(store, event_id=None, revision=1, kind="warning", source="motion", schema_version=1, provisional=False) -> str:
  event_id = event_id or str(uuid4())
  store.begin_revision(
    event_id=event_id, revision=revision, kind=kind, source=source,
    episode_started_at=NOW, detected_at=NOW, message="Movement detected while parked.",
    schema_version=schema_version, provisional=provisional,
  )
  return event_id


def finish_complete(store, event_id, revision=1) -> None:
  store.finish_capture(event_id, revision, {
    "wide": MediaData(JPEG + b"w\xff\xd9", 1928, 1208),
    "cabin": MediaData(JPEG + b"c\xff\xd9", 1344, 760),
  }, {})


@pytest.mark.parametrize("capture_first", (False, True))
def test_confirmation_releases_only_provisional_first_capture(tmp_path, capture_first) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store, schema_version=2, provisional=True)
  assert store.revision_state(event_id, 1) == "provisional_capturing"
  if capture_first:
    finish_complete(store, event_id)
  assert store.next_pending(1e20) is None
  assert store.claim_pending(1e20) is None
  assert not store.upload_work_due(1e20)
  assert store.retry_all() == store.retry_terminal() == store.retry_pending() == 0
  assert not store.acknowledge(event_id, 1, {}, deleted=True)
  with pytest.raises(ValueError, match="unconfirmed"):
    begin(store, event_id, revision=2, kind="follow_up", schema_version=2)
  assert store.confirm_event(event_id)
  assert not store.confirm_event(event_id)
  assert not store.discard_event(event_id)
  if not capture_first:
    finish_complete(store, event_id)
  assert store.next_pending(0).event_id == event_id
  begin(store, event_id, revision=2, kind="follow_up", schema_version=2)
  store.close()


@pytest.mark.parametrize("capture_first", (False, True))
def test_provisional_discard_prevents_confirmation_and_removes_media(tmp_path, capture_first) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store, provisional=True)
  if capture_first:
    finish_complete(store, event_id)
  assert store.discard_event(event_id)
  assert not store.confirm_event(event_id)
  assert not store.discard_event(event_id)
  assert store.retry_all() == store.retry_pending() == 0
  with pytest.raises(CaptureDiscarded):
    finish_complete(store, event_id)
  store.cleanup_discarded_events()
  assert store.revision_state(event_id, 1) is None
  assert list(store.media_root.iterdir()) == []
  assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
  with pytest.raises(CaptureDiscarded):
    finish_complete(store, event_id)


@pytest.mark.parametrize("state", ("provisional_capturing", "provisional_ready", "discarding"))
def test_startup_discards_unconfirmed_work_but_worker_open_leaves_it_held(tmp_path, state) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store, provisional=True)
  confirmed = begin(store)
  finish_complete(store, confirmed)
  if state != "provisional_capturing":
    finish_complete(store, event_id)
  if state == "discarding":
    store.discard_event(event_id)
  if state == "provisional_capturing":
    store._write_media(event_id, 1, "wide", MediaData(JPEG, 10, 10))  # process died before manifest commit
  store.close()
  worker = SentryStore(path, run_maintenance=False)
  assert worker.revision_state(event_id, 1) == state
  worker.close()
  store = SentryStore(path)
  assert store.revision_state(event_id, 1) is None
  assert not (store.media_root / event_id).exists()
  assert store.next_pending(0).event_id == confirmed


@pytest.mark.parametrize("fail_at", ("unlink", "revision_sync", "event_sync", "root_sync"))
def test_discard_cleanup_retains_marker_until_filesystem_cleanup_is_durable(tmp_path, monkeypatch, fail_at) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store, provisional=True)
  finish_complete(store, event_id)
  store.discard_event(event_id)
  original_sync, original_unlink = store._fsync_directory, Path.unlink

  def fail_sync(directory):
    failed = {"revision_sync": "1", "event_sync": event_id, "root_sync": "media"}
    if directory.name == failed.get(fail_at):
      raise OSError("simulated discard fsync failure")
    original_sync(directory)

  def fail_unlink(target, *args, **kwargs):
    if fail_at == "unlink" and target.name == "cabin.jpg":
      raise OSError("simulated discard unlink failure")
    return original_unlink(target, *args, **kwargs)

  with monkeypatch.context() as patch:
    patch.setattr(store, "_fsync_directory", fail_sync)
    patch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(OSError, match="simulated discard"):
      store.cleanup_discarded_events()
  assert store.revision_state(event_id, 1) == "discarding"
  store.close()
  reopened = SentryStore(path)
  assert reopened.revision_state(event_id, 1) is None
  assert list(reopened.media_root.iterdir()) == []


def test_discard_cleanup_does_not_follow_replaced_directory_symlink(tmp_path) -> None:
  store = SentryStore(tmp_path / "sentry" / "outbox.sqlite3")
  event_id = begin(store, provisional=True)
  finish_complete(store, event_id)
  store.discard_event(event_id)
  event_directory = store.media_root / event_id
  moved = tmp_path / "preserved"
  event_directory.rename(moved)
  event_directory.symlink_to(moved, target_is_directory=True)
  with pytest.raises(OSError, match="unsafe Sentry discarded"):
    store.cleanup_discarded_events()
  assert store.revision_state(event_id, 1) == "discarding"
  assert len(list(moved.rglob("*.jpg"))) == 2


@pytest.mark.parametrize("transition", ("confirm", "discard", "door"))
def test_capture_finalization_rechecks_identity_after_media_writes(tmp_path, monkeypatch, transition) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store, schema_version=2, provisional=True)
  other = SentryStore(path, run_maintenance=False)
  original_write = store._write_media
  transitioned = False

  def write_then_transition(*args):
    nonlocal transitioned
    written = original_write(*args)
    if not transitioned:
      transitioned = True
      if transition == "door":
        assert other.start_door_event(
          event_id=str(uuid4()), provisional_event_id=event_id, detected_at=NOW, message="Door opened.",
        ) == (event_id, True)
      else:
        assert getattr(other, f"{transition}_event")(event_id)
    return written

  monkeypatch.setattr(store, "_write_media", write_then_transition)
  if transition == "discard":
    with pytest.raises(CaptureDiscarded):
      finish_complete(store, event_id)
    assert list(store.media_root.rglob("*.jpg")) == []
    assert store.next_pending(0) is None
  else:
    finish_complete(store, event_id)
    queued = store.next_pending(0)
    assert queued.metadata["source"] == ("door_open" if transition == "door" else "motion")
    assert queued.metadata["schema_version"] == (3 if transition == "door" else 2)
    assert queued.metadata["kind"] == ("door_open" if transition == "door" else "warning")
    assert len(queued.media) == 2
  other.close()


@pytest.mark.parametrize("capture_first", (False, True))
def test_door_conversion_retains_first_pair_and_reserves_second_before_upload(tmp_path, capture_first) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store, schema_version=2, provisional=True)
  before = None
  if capture_first:
    finish_complete(store, event_id)
    before = [dict(row) for row in store.connection.execute("SELECT * FROM media WHERE event_id=? ORDER BY role", (event_id,))]
  triggered_at = "2026-09-04T00:00:02+00:00"
  result = store.start_door_event(event_id=str(uuid4()), provisional_event_id=event_id, detected_at=triggered_at, message="Door opened.")
  assert result == (event_id, True)
  assert store.get_door_pause()
  assert store.revision_state(event_id, 2) == "capturing"
  if before is not None:
    assert [dict(row) for row in store.connection.execute("SELECT * FROM media WHERE event_id=? ORDER BY role", (event_id,))] == before
  else:
    finish_complete(store, event_id)
  assert not store.confirm_event(event_id)
  assert not store.discard_event(event_id)
  first = store.claim_pending(0)
  assert (first.metadata["schema_version"], first.metadata["source"], first.metadata["kind"]) == (3, "door_open", "door_open")
  assert first.metadata["detected_at"] == first.metadata["episode_started_at"] == NOW
  assert store.acknowledge(event_id, 1, {item.role: item.sha256 for item in first.media})
  assert store.revision_state(event_id, 1) == "acknowledged"
  finish_complete(store, event_id, 2)
  second = store.claim_pending(0)
  assert (second.revision, second.metadata["kind"], second.metadata["source"]) == (2, "follow_up", "door_open")
  assert second.metadata["detected_at"] == triggered_at
  assert store.acknowledge(event_id, 2, {item.role: item.sha256 for item in second.media})
  assert store.revision_state(event_id, 1) is None
  # Pause persists independently of acknowledged history cleanup and reboot.
  store.close()
  store = SentryStore(path)
  assert store.get_door_pause()
  assert store.start_door_event(event_id=str(uuid4()), detected_at=NOW, message="Door reopened.") == result
  store.clear_door_pause()
  store.close()
  assert not SentryStore(path).get_door_pause()


@pytest.mark.parametrize("state", ("capturing", "ready", "uploading", "terminal", "acknowledged", "discarding"))
def test_door_does_not_rewrite_confirmed_claimed_or_discarded_motion_events(tmp_path, state) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  motion = begin(store, provisional=state == "discarding")
  if state not in ("capturing", "discarding"):
    finish_complete(store, motion)
  if state == "discarding":
    store.discard_event(motion)
  elif state != "capturing":
    with store.connection:
      store.connection.execute("UPDATE revisions SET state=? WHERE event_id=?", (state, motion))
  snapshot = {table: [dict(row) for row in store.connection.execute(f"SELECT * FROM {table} WHERE event_id=?", (motion,))]
              for table in ("events", "revisions", "media")}
  door = str(uuid4())
  assert store.start_door_event(event_id=door, provisional_event_id=motion, detected_at=NOW, message="Door opened.") == (door, False)
  assert store.revision_state(door, 1) == "capturing"
  assert store.revision_state(door, 2) is None
  for table, expected in snapshot.items():
    assert [dict(row) for row in store.connection.execute(f"SELECT * FROM {table} WHERE event_id=?", (motion,))] == expected


@pytest.mark.parametrize("schema,revision,kind,source", [
  (1, 1, "door_open", "door_open"), (2, 1, "door_open", "door_open"), (3, 1, "warning", "motion"),
  (3, 1, "door_open", "manual_test"), (3, 2, "alarm", "door_open"), (3, 3, "follow_up", "door_open"),
])
def test_door_wire_contract_rejects_invalid_combinations(tmp_path, schema, revision, kind, source) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  with pytest.raises(ValueError):
    begin(store, schema_version=schema, revision=revision, kind=kind, source=source)


@pytest.mark.parametrize("provisional", (1, "yes", None))
def test_provisional_flag_has_strict_boolean_type(tmp_path, provisional) -> None:
  with pytest.raises(ValueError, match="provisional"):
    begin(SentryStore(tmp_path / "outbox.sqlite3"), provisional=provisional)


def test_provisional_quota_discards_pair_without_rewriting_confirmed_work(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  confirmed = begin(store)
  finish_complete(store, confirmed)
  original = store.next_pending(0)
  store.media_quota_bytes = store.stats().media_bytes
  provisional = begin(store, provisional=True)
  with pytest.raises(CaptureDiscarded, match="quota"):
    finish_complete(store, provisional)
  assert store.revision_state(provisional, 1) is None
  assert store.next_pending(0) == original
  assert not store.confirm_event(provisional)
  assert not (store.media_root / provisional).exists()


@pytest.mark.parametrize("fail_statement", (
  "UPDATE events SET source=", "UPDATE revisions SET kind=", "INSERT INTO revisions", "UPDATE runtime_state SET door_pause_active=1",
))
def test_door_conversion_and_pause_transaction_rolls_back_completely(tmp_path, monkeypatch, fail_statement) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store, provisional=True)
  finish_complete(store, event_id)
  snapshot = {table: [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
              for table in ("events", "revisions", "media", "runtime_state")}
  files = {row["path"]: Path(row["path"]).read_bytes() for row in snapshot["media"]}
  store.close()
  original_connect = sqlite3.connect

  class FailingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
      result = super().execute(sql, parameters)
      if sql.strip().startswith(fail_statement):
        raise sqlite3.OperationalError("simulated door transaction failure")
      return result

  with monkeypatch.context() as patch:
    patch.setattr(sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, factory=FailingConnection, **kwargs))
    store = SentryStore(path, run_maintenance=False)
    with pytest.raises(sqlite3.OperationalError, match="door transaction"):
      store.start_door_event(event_id=str(uuid4()), provisional_event_id=event_id, detected_at=NOW, message="Door opened.")
  for table, expected in snapshot.items():
    assert [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")] == expected
  assert {name: Path(name).read_bytes() for name in files} == files
  assert store.next_pending(0) is None
  assert not store.get_door_pause()
  store.close()


def test_restart_after_door_conversion_preserves_first_pair_and_recovers_second_as_missing(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store, provisional=True)
  finish_complete(store, event_id)
  store.start_door_event(event_id=str(uuid4()), provisional_event_id=event_id, detected_at=NOW, message="Door opened.")
  first = store.next_pending(0)
  store.close()
  store = SentryStore(path)
  assert store.get_door_pause()
  assert store.next_pending(0) == first
  assert store.revision_state(event_id, 2) == "ready"
  metadata = json.loads(store.connection.execute("SELECT metadata_json FROM revisions WHERE event_id=? AND revision=2", (event_id,)).fetchone()[0])
  assert (metadata["schema_version"], metadata["source"], metadata["kind"]) == (3, "door_open", "follow_up")
  assert {omission["reason"] for omission in metadata["omitted_media"]} == {"stale_capture"}


def test_complete_revision_has_exact_wire_metadata(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store)
  finish_complete(store, event_id)
  queued = store.next_pending(0)
  assert queued is not None
  assert queued.metadata["schema_version"] == 1
  assert queued.metadata["event_id"] == event_id
  assert queued.metadata["revision"] == 1
  assert queued.metadata["capture_status"] == "complete"
  assert {item["role"] for item in queued.metadata["media"]} == {"wide", "cabin"}
  assert queued.metadata["omitted_media"] == []
  assert store.stats().media_bytes == sum(item.size for item in queued.media)


def test_schema_two_revisions_upload_in_order_across_retry_and_restart(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store, schema_version=2)
  finish_complete(store, event_id)
  begin(store, event_id, revision=2, kind="follow_up", schema_version=2)
  finish_complete(store, event_id, revision=2)
  begin(store, event_id, revision=3, kind="alarm", schema_version=2)
  finish_complete(store, event_id, revision=3)
  store.schedule_retry(event_id, 1, next_attempt_at=100, error="offline")
  assert not store.upload_work_due(0)
  assert store.next_pending(0) is None
  assert store.claim_pending(0) is None
  warning = store.claim_pending(100)
  assert warning.revision == 1
  assert store.acknowledge(event_id, 1, {item.role: item.sha256 for item in warning.media})
  follow_up = store.claim_pending(100)
  assert follow_up.revision == 2 and follow_up.metadata["schema_version"] == 2
  store.schedule_retry(event_id, 2, next_attempt_at=500, error="offline again")
  assert not store.upload_work_due(101)
  assert store.next_pending(101) is None
  assert store.claim_pending(101) is None
  store.close()

  reopened = SentryStore(path)
  assert reopened.reconcile_media_tree() == 0  # revision 3 is a valid directory
  assert not reopened.upload_work_due(499)
  follow_up = reopened.claim_pending(500)
  assert follow_up.revision == 2
  assert reopened.acknowledge(event_id, 2, {item.role: item.sha256 for item in follow_up.media})
  assert [row[0] for row in reopened.connection.execute(
    "SELECT revision FROM revisions WHERE event_id=? AND state='acknowledged' ORDER BY revision", (event_id,)
  )] == [1, 2]
  alarm = reopened.claim_pending(500)
  assert alarm.revision == 3 and alarm.metadata["kind"] == "alarm"
  reopened.close_event(event_id)
  assert reopened.acknowledge(event_id, 3, {item.role: item.sha256 for item in alarm.media})
  assert reopened.connection.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone() is None


def test_follow_up_terminal_failure_blocks_alarm_until_manual_retry_ack(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store, schema_version=2)
  finish_complete(store, event_id)
  warning = store.claim_pending(0)
  assert store.acknowledge(event_id, 1, {item.role: item.sha256 for item in warning.media})
  begin(store, event_id, revision=2, kind="follow_up", schema_version=2)
  finish_complete(store, event_id, revision=2)
  begin(store, event_id, revision=3, kind="alarm", schema_version=2)
  finish_complete(store, event_id, revision=3)
  store.mark_terminal(event_id, 2, error="terminal", http_status=422)
  assert not store.upload_work_due(0)
  assert store.next_pending(0) is None and store.claim_pending(0) is None
  assert store.retry_terminal() == 1
  follow_up = store.claim_pending(0)
  assert follow_up.revision == 2
  assert store.acknowledge(event_id, 2, {item.role: item.sha256 for item in follow_up.media})
  assert store.claim_pending(0).revision == 3


@pytest.mark.parametrize("method", ("retry_pending", "retry_all"))
@pytest.mark.parametrize("state,retryable", (
  ("ready", 1), ("ready", 0), ("terminal", 1), ("terminal", 0),
  ("uploading", 1), ("acknowledged", 1), ("capturing", 1),
))
def test_bulk_retry_preserves_payloads_attempts_and_active_claims(tmp_path, method, state, retryable) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store)
  if state != "capturing":
    finish_complete(store, event_id)
  with store.connection:
    store.connection.execute(
      """
      UPDATE revisions SET state=?, retryable=?, attempts=7, next_attempt_at=1000,
        last_attempt_at=?, last_error='previous failure', last_http_status=503 WHERE event_id=?
      """, (state, retryable, NOW, event_id),
    )
  before = dict(store.connection.execute("SELECT * FROM revisions WHERE event_id=?", (event_id,)).fetchone())
  media_before = [dict(row) for row in store.connection.execute("SELECT * FROM media WHERE event_id=?", (event_id,))]
  files_before = {row["path"]: Path(row["path"]).read_bytes() for row in media_before if row["path"]}

  eligible = bool(retryable and (state == "ready" or (method == "retry_all" and state == "terminal")))
  assert getattr(store, method)() == int(eligible)
  expected = before.copy()
  if eligible:
    expected["next_attempt_at"] = 0
    if method == "retry_all":
      expected.update(state="ready", last_error=None, last_http_status=None)
  assert dict(store.connection.execute("SELECT * FROM revisions WHERE event_id=?", (event_id,)).fetchone()) == expected
  assert [dict(row) for row in store.connection.execute("SELECT * FROM media WHERE event_id=?", (event_id,))] == media_before
  assert {path: Path(path).read_bytes() for path in files_before} == files_before


@pytest.mark.parametrize("method", ("retry_pending", "retry_all"))
def test_bulk_retry_keeps_revisions_ordered(tmp_path, method) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store, schema_version=2)
  finish_complete(store, event_id)
  for revision in (2, 3):
    begin(store, event_id, revision=revision, kind="follow_up", schema_version=2)
    finish_complete(store, event_id, revision=revision)
  for revision in (1, 2, 3):
    store.schedule_retry(event_id, revision, next_attempt_at=1000, error="offline")

  assert getattr(store, method)() == 3
  for revision in (1, 2, 3):
    claimed = store.claim_pending(0)
    assert claimed.revision == revision
    assert store.claim_pending(0) is None  # successors remain blocked while this claim is active
    assert store.acknowledge(event_id, revision, {item.role: item.sha256 for item in claimed.media})


@pytest.mark.parametrize("last_attempt_at,eligible", (
  (None, True), ("2026-09-03T23:59:59+00:00", True),
  (NOW, False), ("2026-09-04T00:00:01+00:00", False),
))
def test_retry_pending_honors_drain_cutoff_and_server_floor(tmp_path, last_attempt_at, eligible) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store)
  finish_complete(store, event_id)
  store.schedule_retry(event_id, 1, next_attempt_at=1000, retry_after_at=500, error="retry later", http_status=503)
  with store.connection:
    store.connection.execute("UPDATE revisions SET last_attempt_at=? WHERE event_id=?", (last_attempt_at, event_id))

  assert store.retry_pending(attempted_before=NOW) == int(eligible)
  assert tuple(store.connection.execute(
    "SELECT next_attempt_at, retry_after_at, last_error, last_http_status FROM revisions WHERE event_id=?", (event_id,),
  ).fetchone()) == (500 if eligible else 1000, 500, "retry later", 503)


def test_retry_schedule_enforces_server_floor_and_manual_retry_explicitly_clears_it(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store)
  finish_complete(store, event_id)
  store.schedule_retry(event_id, 1, next_attempt_at=100, retry_after_at=500, error="rate limited", http_status=429)
  assert store.next_pending(499) is None
  assert store.retry_pending() == 1
  assert store.next_pending(499) is None
  assert store.retry_all() == 1
  assert tuple(store.connection.execute(
    "SELECT next_attempt_at, retry_after_at, last_error FROM revisions WHERE event_id=?", (event_id,),
  ).fetchone()) == (0, 0, None)
  assert store.claim_pending(0).event_id == event_id
  store.schedule_retry(event_id, 1, next_attempt_at=600, retry_after_at=900, error="rate limited again", http_status=429)
  store.schedule_retry(event_id, 1, next_attempt_at=700, error="network failed")
  assert tuple(store.connection.execute(
    "SELECT next_attempt_at, retry_after_at FROM revisions WHERE event_id=?", (event_id,),
  ).fetchone()) == (700, 0)


@pytest.mark.parametrize("schema_version", [1, 2])
def test_cannot_change_event_schema_between_revisions(tmp_path, schema_version) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store, schema_version=schema_version)
  other_schema = 3 - schema_version
  with pytest.raises(ValueError, match="identity"):
    begin(store, event_id, revision=2, kind="alarm" if other_schema == 1 else "follow_up", schema_version=other_schema)
  assert store.connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 1


@pytest.mark.parametrize("schema_version,revision,kind,source", [
  (0, 1, "warning", "motion"), (3, 1, "warning", "motion"), (True, 1, "warning", "motion"),
  (2.0, 1, "warning", "motion"), (2, True, "warning", "motion"),
  (1, 2, "follow_up", "motion"), (1, 3, "alarm", "motion"), (1, 2, "alarm", "manual_test"),
  (2, 0, "warning", "motion"), (2, MAX_REVISION + 1, "follow_up", "motion"),
  (2, 2, "warning", "motion"), (2, 3, "alarm", "manual_test"), (2, 2, "follow_up", "manual_test"),
])
def test_rejects_revision_schema_kind_and_source_mismatches(tmp_path, schema_version, revision, kind, source) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  with pytest.raises(ValueError):
    begin(store, revision=revision, kind=kind, source=source, schema_version=schema_version)
  assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_schema_two_manual_test_is_single_warning(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store, source="manual_test", schema_version=2)
  with pytest.raises(ValueError, match="source"):
    begin(store, event_id, revision=2, kind="follow_up", source="manual_test", schema_version=2)
  finish_complete(store, event_id)
  warning = store.claim_pending(0)
  assert warning.metadata["source"] == "manual_test"
  assert store.acknowledge(event_id, 1, {item.role: item.sha256 for item in warning.media})


@pytest.mark.parametrize("alarm_revision", [2, 3, 7])
def test_continuous_follow_ups_allow_one_alarm_anywhere(tmp_path, alarm_revision) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store, schema_version=2)
  for revision in range(2, 10):
    begin(store, event_id, revision=revision, kind="alarm" if revision == alarm_revision else "follow_up", schema_version=2)
    finish_complete(store, event_id, revision=revision)
  with pytest.raises(ValueError, match="one alarm"):
    begin(store, event_id, revision=10, kind="alarm", schema_version=2)
  begin(store, event_id, revision=10, kind="follow_up", schema_version=2)
  assert store.reconcile_media_tree() == 0


@pytest.mark.parametrize("name,valid", [
  ("1", True), ("9", True), ("10", True), (str(MAX_REVISION), True),
  ("0", False), ("01", False), ("-1", False), ("1.0", False), ("１２", False),
  (str(MAX_REVISION + 1), False), ("9" * 100, False),
])
def test_revision_directory_accepts_only_canonical_bounded_revision(name, valid) -> None:
  assert SentryStore._valid_revision_directory_name(name) == valid


def test_schema_two_requires_preceding_revisions_and_clamps_each_timestamp(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store, schema_version=2)
  with pytest.raises(ValueError, match="preceding"):
    begin(store, event_id, revision=3, kind="alarm", schema_version=2)
  follow_up_at = "2026-09-04T00:00:10+00:00"
  for revision, kind, detected_at in ((2, "follow_up", follow_up_at), (3, "alarm", NOW)):
    store.begin_revision(
      event_id=event_id, revision=revision, kind=kind, source="motion", schema_version=2,
      episode_started_at=NOW, detected_at=detected_at, message="motion",
    )
  assert store.connection.execute(
    "SELECT detected_at FROM revisions WHERE event_id=? AND revision=3", (event_id,)
  ).fetchone()[0] == follow_up_at


def test_schema_two_recovery_and_quota_rewrite_keep_schema(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store, schema_version=2)
  finish_complete(store, event_id)
  begin(store, event_id, revision=2, kind="follow_up", schema_version=2)
  store.close()
  recovered = SentryStore(path, media_quota_bytes=1)
  rows = recovered.connection.execute("SELECT metadata_json FROM revisions ORDER BY revision").fetchall()
  warning, follow_up = (json.loads(row[0]) for row in rows)
  assert warning["schema_version"] == follow_up["schema_version"] == 2
  assert warning["capture_status"] == "omitted"
  assert follow_up["kind"] == "follow_up"
  assert {item["reason"] for item in follow_up["omitted_media"]} == {"stale_capture"}


# The deployed schema had no version column/PRAGMA and rejected follow-up kinds
# and revisions above 2. Build it independently of the production migration.
LEGACY_SCHEMA = """
CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  source TEXT NOT NULL CHECK(source IN ('motion', 'manual_test')),
  episode_started_at TEXT NOT NULL,
  closed INTEGER NOT NULL DEFAULT 0 CHECK(closed IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE revisions (
  event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK(revision IN (1, 2)),
  kind TEXT NOT NULL CHECK(kind IN ('warning', 'alarm')),
  detected_at TEXT NOT NULL, message TEXT NOT NULL,
  capture_status TEXT CHECK(capture_status IN ('complete', 'partial', 'failed', 'omitted')),
  metadata_json TEXT,
  state TEXT NOT NULL CHECK(state IN (
    'capturing', 'ready', 'uploading', 'terminal', 'acknowledged',
    'evicting_ready', 'evicting_terminal', 'evicting_uncertain'
  )),
  retryable INTEGER NOT NULL DEFAULT 1 CHECK(retryable IN (0, 1)),
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
  last_attempt_at TEXT, last_http_status INTEGER, last_error TEXT, acknowledged_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(event_id, revision)
);
CREATE INDEX sentry_revisions_pending ON revisions(state, next_attempt_at, created_at);
CREATE TABLE media (
  event_id TEXT NOT NULL, revision INTEGER NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('wide', 'cabin')),
  path TEXT, sha256 TEXT, size INTEGER, width INTEGER, height INTEGER, omission_reason TEXT,
  local_omission_reason TEXT CHECK(local_omission_reason IN ('queue_quota')),
  PRIMARY KEY(event_id, revision, role),
  FOREIGN KEY(event_id, revision) REFERENCES revisions(event_id, revision) ON DELETE CASCADE,
  CHECK((path IS NOT NULL AND sha256 IS NOT NULL AND size IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL
         AND omission_reason IS NULL AND local_omission_reason IS NULL)
     OR (path IS NULL AND sha256 IS NULL AND size IS NULL AND width IS NULL AND height IS NULL
         AND omission_reason IS NOT NULL AND local_omission_reason IS NULL)
     OR (path IS NULL AND sha256 IS NOT NULL AND size IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL
         AND omission_reason IS NULL AND local_omission_reason='queue_quota'))
);
"""


LOCK_CONTROLS = (("none", None), ("unlock_pause", "unlocked"), ("rearm", "locked"), ("rearm", "unlocked"))


@pytest.mark.parametrize("control", LOCK_CONTROLS)
@pytest.mark.parametrize("run_maintenance", (False, True))
def test_lock_control_survives_worker_open_and_daemon_restart(tmp_path, control, run_maintenance) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  assert store.get_lock_control() == ("none", None)
  event_id = begin(store)
  finish_complete(store, event_id)
  door_id = str(uuid4())
  store.start_door_event(event_id=door_id, detected_at=NOW, message="Door opened.")
  finish_complete(store, door_id)
  snapshot = {table: [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
              for table in ("events", "revisions", "media")}
  store.set_lock_control(*control)
  store.close()
  reopened = SentryStore(path, run_maintenance=run_maintenance)
  assert reopened.get_lock_control() == control
  assert reopened.get_door_pause()
  for table, expected in snapshot.items():
    assert [dict(row) for row in reopened.connection.execute(f"SELECT * FROM {table}")] == expected
  reopened.clear_door_pause()
  assert reopened.get_lock_control() == control
  reopened.close()


@pytest.mark.parametrize("state,inferred", (
  ("none", "locked"), ("none", "unlocked"), ("unlock_pause", None), ("unlock_pause", "locked"), ("rearm", None),
  ("unknown", None), ("locked", "locked"), ("none", False), (False, None), ([], None), ("rearm", []),
))
def test_lock_control_rejects_invalid_pairs_without_mutation(tmp_path, state, inferred) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  store.set_lock_control("rearm", "locked")
  with pytest.raises(ValueError, match="lock control"):
    store.set_lock_control(state, inferred, clear_door_pause=True)
  assert store.get_lock_control() == ("rearm", "locked")
  store.close()


@pytest.mark.parametrize("control", LOCK_CONTROLS[1:])
def test_lock_control_survives_provisional_startup_cleanup_but_worker_does_not_discard(tmp_path, control) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store, provisional=True)
  finish_complete(store, event_id)
  store.set_lock_control(*control)
  store.close()
  worker = SentryStore(path, run_maintenance=False)
  assert worker.get_lock_control() == control
  assert worker.revision_state(event_id, 1) == "provisional_ready"
  assert worker.claim_pending(10 ** 12) is None
  worker.close()
  restarted = SentryStore(path)
  assert restarted.get_lock_control() == control
  assert restarted.revision_state(event_id, 1) is None
  assert list(restarted.media_root.iterdir()) == []
  restarted.close()


@pytest.mark.parametrize("clear", (None, 0, 1, "true"))
def test_lock_control_requires_boolean_clear_option(tmp_path, clear) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  with pytest.raises(ValueError, match="boolean"):
    store.set_lock_control("unlock_pause", "unlocked", clear_door_pause=clear)
  assert store.get_lock_control() == ("none", None)
  store.close()


@pytest.mark.parametrize("state,inferred", (
  ("none", "locked"), ("unlock_pause", None), ("unlock_pause", "locked"), ("rearm", None), ("rearm", "unknown"), ("unknown", None),
))
def test_lock_control_database_constraint_rejects_invalid_pairs(tmp_path, state, inferred) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  with pytest.raises(sqlite3.IntegrityError), store.connection:
    store.connection.execute("UPDATE runtime_state SET lock_control=?, inferred_lock_state=?", (state, inferred))
  assert store.get_lock_control() == ("none", None)
  # Reads also reject invalid stored values instead of reporting an unpaused state.
  store.connection.execute("PRAGMA ignore_check_constraints=ON")
  with store.connection:
    store.connection.execute("UPDATE runtime_state SET lock_control=?, inferred_lock_state=?", (state, inferred))
  with pytest.raises(ValueError, match="lock control"):
    store.get_lock_control()
  store.close()


def test_lock_control_and_door_pause_clear_are_atomic_on_failure(tmp_path, monkeypatch) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  store.start_door_event(event_id=str(uuid4()), detected_at=NOW, message="Door opened.")
  store.set_lock_control("rearm", "locked")
  before = dict(store.connection.execute("SELECT * FROM runtime_state").fetchone())
  store.close()
  original_connect = sqlite3.connect

  class FailingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
      result = super().execute(sql, parameters)
      if sql.startswith("UPDATE runtime_state SET lock_control="):
        raise sqlite3.OperationalError("simulated gate write failure")
      return result

  with monkeypatch.context() as patch:
    patch.setattr(sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, factory=FailingConnection, **kwargs))
    store = SentryStore(path, run_maintenance=False)
    with pytest.raises(sqlite3.OperationalError, match="gate write"):
      store.set_lock_control("unlock_pause", "unlocked", clear_door_pause=True)
    assert dict(store.connection.execute("SELECT * FROM runtime_state").fetchone()) == before
    store.close()
  store = SentryStore(path, run_maintenance=False)
  store.set_lock_control("unlock_pause", "unlocked", clear_door_pause=True)
  assert store.get_lock_control() == ("unlock_pause", "unlocked")
  assert not store.get_door_pause()
  assert tuple(store.connection.execute("SELECT door_event_id, door_converted FROM runtime_state").fetchone()) == (None, 0)
  store.close()


def test_concurrent_lock_control_updates_never_tear_pairs_or_erase_door_pause(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  initial = SentryStore(path)
  initial.start_door_event(event_id=str(uuid4()), detected_at=NOW, message="Door opened.")
  barrier = threading.Barrier(4)

  def update_or_read(index):
    store = SentryStore(path, run_maintenance=False)
    try:
      barrier.wait(timeout=5)
      for iteration in range(40):
        if index < 2:
          store.set_lock_control(*LOCK_CONTROLS[(iteration + index) % len(LOCK_CONTROLS)])
        assert store.get_lock_control() in LOCK_CONTROLS
        assert store.get_door_pause()
    finally:
      store.close()

  with ThreadPoolExecutor(max_workers=4) as pool:
    futures = [pool.submit(update_or_read, index) for index in range(4)]
    for future in futures:
      future.result(timeout=15)
  assert initial.get_lock_control() in LOCK_CONTROLS
  assert initial.get_door_pause()
  initial.close()


def create_schema_four_outbox(path, *, door_pause=True):
  create_schema_three_outbox(path)
  store = SentryStore(path, run_maintenance=False)
  for state in ("provisional_capturing", "provisional_ready", "discarding"):
    event_id = begin(store, schema_version=2, provisional=True)
    if state != "provisional_capturing":
      finish_complete(store, event_id)
    if state == "discarding":
      assert store.discard_event(event_id)
  manual_id = begin(store, source="manual_test", schema_version=2)
  finish_complete(store, manual_id)
  provisional_id = begin(store, schema_version=2, provisional=True)
  finish_complete(store, provisional_id)
  store.start_door_event(event_id=str(uuid4()), provisional_event_id=provisional_id, detected_at=NOW, message="Door opened.")
  finish_complete(store, provisional_id, 2)
  if not door_pause:
    store.clear_door_pause()
  pause = tuple(store.connection.execute("SELECT singleton, door_pause_active, door_event_id, door_converted FROM runtime_state").fetchone())
  # Recreate the deployed v4 runtime table independently: no lock-control fields
  # existed. Queue payloads deliberately use noncanonical JSON to detect rewrites.
  with store.connection:
    for row in store.connection.execute("SELECT event_id, revision, metadata_json FROM revisions WHERE metadata_json IS NOT NULL").fetchall():
      store.connection.execute("UPDATE revisions SET metadata_json=? WHERE event_id=? AND revision=?",
                               (json.dumps(json.loads(row["metadata_json"]), indent=1) + "\n", row["event_id"], row["revision"]))
    store.connection.execute("DROP TABLE runtime_state")
    store.connection.execute("""
      CREATE TABLE runtime_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        door_pause_active INTEGER NOT NULL DEFAULT 0 CHECK(door_pause_active IN (0, 1)),
        door_event_id TEXT,
        door_converted INTEGER NOT NULL DEFAULT 0 CHECK(door_converted IN (0, 1)),
        CHECK((door_pause_active=0 AND door_event_id IS NULL AND door_converted=0)
          OR (door_pause_active=1 AND door_event_id IS NOT NULL))
      )
    """)
    store.connection.execute("INSERT INTO runtime_state VALUES (?, ?, ?, ?)", pause)
    store.connection.execute("PRAGMA user_version=4")
  snapshot = {table: [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
              for table in ("events", "revisions", "media", "runtime_state")}
  files = {row["path"]: Path(row["path"]).read_bytes() for row in snapshot["media"] if row["path"]}
  store.close()
  return snapshot, files


def assert_migrated_schema_four(store, snapshot, files):
  for table, expected in snapshot.items():
    actual = [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
    if table == "runtime_state":
      assert actual[0].pop("lock_control") == "none"
      assert actual[0].pop("inferred_lock_state") is None
    assert actual == expected
  assert {name: Path(name).read_bytes() for name in files} == files
  assert store.get_lock_control() == ("none", None)
  assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 5
  assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
  assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("door_pause", (False, True))
def test_schema_four_migration_preserves_all_payload_bytes_claims_and_door_pause(tmp_path, door_pause) -> None:
  path = tmp_path / "outbox.sqlite3"
  snapshot, files = create_schema_four_outbox(path, door_pause=door_pause)
  with sqlite3.connect(path) as connection:
    schema = connection.execute("SELECT name, rootpage, sql FROM sqlite_master WHERE name IN ('events', 'revisions', 'media')").fetchall()
  connection.close()
  for _ in range(2):
    store = SentryStore(path, run_maintenance=False)
    assert_migrated_schema_four(store, snapshot, files)
    assert [tuple(row) for row in store.connection.execute(
      "SELECT name, rootpage, sql FROM sqlite_master WHERE name IN ('events', 'revisions', 'media')"
    )] == schema  # v4 upgrade must not rebuild any queued table, even identically.
    store.close()


@pytest.mark.parametrize("fail_statement", (
  "ALTER TABLE runtime_state ADD COLUMN lock_control", "ALTER TABLE runtime_state ADD COLUMN inferred_lock_state", "PRAGMA user_version=5",
))
def test_schema_four_migration_failure_rolls_back_runtime_columns_and_version(tmp_path, monkeypatch, fail_statement) -> None:
  path = tmp_path / "outbox.sqlite3"
  snapshot, files = create_schema_four_outbox(path)
  original_connect = sqlite3.connect

  class FailingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
      result = super().execute(sql, parameters)
      if sql.strip().startswith(fail_statement):
        raise sqlite3.OperationalError("simulated schema-five migration failure")
      return result

  with monkeypatch.context() as patch:
    patch.setattr(sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, factory=FailingConnection, **kwargs))
    with pytest.raises(sqlite3.OperationalError, match="schema-five"):
      SentryStore(path, run_maintenance=False)
  with sqlite3.connect(path) as verifier:
    verifier.row_factory = sqlite3.Row
    assert verifier.execute("PRAGMA user_version").fetchone()[0] == 4
    for table, expected in snapshot.items():
      assert [dict(row) for row in verifier.execute(f"SELECT * FROM {table}")] == expected
  verifier.close()
  store = SentryStore(path, run_maintenance=False)
  assert_migrated_schema_four(store, snapshot, files)
  store.close()


@pytest.mark.parametrize("crash_statement", (
  "ALTER TABLE runtime_state ADD COLUMN lock_control", "ALTER TABLE runtime_state ADD COLUMN inferred_lock_state", "PRAGMA user_version=5",
))
def test_process_death_during_schema_four_migration_keeps_previous_runtime_and_queue(tmp_path, crash_statement) -> None:
  path = tmp_path / "outbox.sqlite3"
  snapshot, files = create_schema_four_outbox(path)
  program = """
import os
import sqlite3
import sys
from openpilot.system.sentryd.store import SentryStore
original_connect = sqlite3.connect
class CrashConnection(sqlite3.Connection):
  def execute(self, sql, parameters=()):
    result = super().execute(sql, parameters)
    if sql.strip().startswith(sys.argv[2]):
      os._exit(77)
    return result
sqlite3.connect = lambda *args, **kwargs: original_connect(*args, factory=CrashConnection, **kwargs)
SentryStore(sys.argv[1], run_maintenance=False)
"""
  result = subprocess.run([sys.executable, "-c", program, str(path), crash_statement], timeout=10, check=False)
  assert result.returncode == 77
  with sqlite3.connect(path) as verifier:
    verifier.row_factory = sqlite3.Row
    assert verifier.execute("PRAGMA user_version").fetchone()[0] == 4
    for table, expected in snapshot.items():
      assert [dict(row) for row in verifier.execute(f"SELECT * FROM {table}")] == expected
  verifier.close()
  store = SentryStore(path, run_maintenance=False)
  assert_migrated_schema_four(store, snapshot, files)
  store.close()


def test_concurrent_schema_four_initializers_preserve_runtime_and_queue(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  snapshot, files = create_schema_four_outbox(path)
  barrier = threading.Barrier(4)
  opened = threading.Barrier(4)

  def initialize():
    barrier.wait(timeout=5)
    store = None
    try:
      store = SentryStore(path, run_maintenance=False)
      assert_migrated_schema_four(store, snapshot, files)
      opened.wait(timeout=5)
    except Exception:
      opened.abort()
      raise
    finally:
      if store is not None:
        store.close()

  with ThreadPoolExecutor(max_workers=4) as pool:
    futures = [pool.submit(initialize) for _ in range(4)]
    for future in futures:
      future.result(timeout=15)


def create_schema_three_outbox(path):
  store = SentryStore(path)
  future = datetime.now(UTC).timestamp() + 3600
  for index, state in enumerate(("ready", "capturing", "uploading", "terminal", "acknowledged",
                                  "evicting_ready", "evicting_terminal", "evicting_uncertain")):
    event_id = begin(store, schema_version=1 + index % 2)
    if state != "capturing":
      finish_complete(store, event_id)
    with store.connection:
      store.connection.execute(
        "UPDATE revisions SET state=?, attempts=?, next_attempt_at=?, retry_after_at=?, last_attempt_at=?, last_http_status=? WHERE event_id=?",
        (state, index, future, 123 + index, NOW, 503, event_id),
      )
  snapshot = {table: [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
              for table in ("events", "revisions", "media")}
  files = {row["path"]: Path(row["path"]).read_bytes() for row in snapshot["media"] if row["path"]}
  # Independently recreate deployed v3 CHECK constraints. No provisional states,
  # door identities or runtime pause table existed before this migration.
  schema = LEGACY_SCHEMA.replace("revision IN (1, 2)", f"revision>=1 AND revision<={MAX_REVISION}")
  schema = schema.replace("kind IN ('warning', 'alarm')", "kind IN ('warning', 'follow_up', 'alarm')")
  schema = schema.replace("next_attempt_at REAL NOT NULL DEFAULT 0,", "next_attempt_at REAL NOT NULL DEFAULT 0, retry_after_at REAL NOT NULL DEFAULT 0,")
  store.connection.execute("PRAGMA foreign_keys=OFF")
  store.connection.executescript("DROP TABLE media; DROP TABLE revisions; DROP TABLE events; DROP TABLE runtime_state;" + schema)
  with store.connection:
    store.connection.execute("ALTER TABLE events ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version IN (1, 2))")
    for table, rows in snapshot.items():
      for row in rows:
        columns, placeholders = ",".join(row), ",".join("?" for _ in row)
        store.connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values()))
    store.connection.execute("PRAGMA user_version=3")
  store.close()
  return snapshot, files


def assert_migrated_schema_three(store, snapshot, files):
  for table, expected in snapshot.items():
    assert [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")] == expected
  assert {name: Path(name).read_bytes() for name in files} == files
  assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 5
  assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
  assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
  assert not store.get_door_pause()


def test_schema_three_migration_preserves_claims_wire_bytes_and_exact_retry_floors(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  snapshot, files = create_schema_three_outbox(path)
  for _ in range(2):
    store = SentryStore(path, run_maintenance=False)
    assert_migrated_schema_three(store, snapshot, files)
    store.close()
  store = SentryStore(path, run_maintenance=False)
  candidate = begin(store, provisional=True)
  assert store.start_door_event(event_id=str(uuid4()), provisional_event_id=candidate, detected_at=NOW, message="Door opened.") == (candidate, True)


@pytest.mark.parametrize("fail_statement", (
  "INSERT INTO sentry_revisions_v2", "DROP TABLE revisions", "ALTER TABLE sentry_revisions_v2",
  "INSERT INTO sentry_events_v4", "DROP TABLE events", "ALTER TABLE sentry_events_v4", "CREATE TABLE IF NOT EXISTS runtime_state",
  "PRAGMA user_version=5",
))
def test_schema_three_migration_failure_rolls_back_every_table_and_floor(tmp_path, monkeypatch, fail_statement) -> None:
  path = tmp_path / "outbox.sqlite3"
  snapshot, files = create_schema_three_outbox(path)
  original_connect = sqlite3.connect

  class FailingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
      result = super().execute(sql, parameters)
      if sql.strip().startswith(fail_statement):
        raise sqlite3.OperationalError("simulated schema-four migration failure")
      return result

  with monkeypatch.context() as patch:
    patch.setattr(sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, factory=FailingConnection, **kwargs))
    with pytest.raises(sqlite3.OperationalError, match="schema-four"):
      SentryStore(path, run_maintenance=False)
  with sqlite3.connect(path) as verifier:
    verifier.row_factory = sqlite3.Row
    assert verifier.execute("PRAGMA user_version").fetchone()[0] == 3
    assert verifier.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_state'").fetchone() is None
    for table, expected in snapshot.items():
      assert [dict(row) for row in verifier.execute(f"SELECT * FROM {table}")] == expected
  verifier.close()
  store = SentryStore(path, run_maintenance=False)
  assert_migrated_schema_three(store, snapshot, files)


@pytest.mark.parametrize("crash_statement", ("DROP TABLE events", "PRAGMA user_version=5"))
def test_process_death_during_schema_three_migration_retains_old_queue(tmp_path, crash_statement) -> None:
  path = tmp_path / "outbox.sqlite3"
  snapshot, files = create_schema_three_outbox(path)
  program = """
import os
import sqlite3
import sys
from openpilot.system.sentryd.store import SentryStore
original_connect = sqlite3.connect
class CrashConnection(sqlite3.Connection):
  def execute(self, sql, parameters=()):
    result = super().execute(sql, parameters)
    if sql.strip().startswith(sys.argv[2]):
      os._exit(77)
    return result
sqlite3.connect = lambda *args, **kwargs: original_connect(*args, factory=CrashConnection, **kwargs)
SentryStore(sys.argv[1], run_maintenance=False)
"""
  result = subprocess.run([sys.executable, "-c", program, str(path), crash_statement], timeout=10, check=False)
  assert result.returncode == 77
  with sqlite3.connect(path) as verifier:
    assert verifier.execute("PRAGMA user_version").fetchone()[0] == 3
  verifier.close()
  store = SentryStore(path, run_maintenance=False)
  assert_migrated_schema_three(store, snapshot, files)


def create_legacy_outbox(path):
  # Use today's media writer to make realistic bytes, then independently build
  # yesterday's schema from the exact rows with the event version stripped.
  store = SentryStore(path)
  pending = begin(store)
  finish_complete(store, pending)
  store.record_attempt(pending, 1)
  acknowledged = begin(store)
  finish_complete(store, acknowledged)
  with store.connection:
    store.connection.execute("UPDATE revisions SET state='acknowledged' WHERE event_id=?", (acknowledged,))
  begin(store, acknowledged, revision=2, kind="alarm")  # interrupted capture after warning ack
  snapshot = {table: [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
              for table in ("events", "revisions", "media")}
  for row in snapshot["events"]:
    del row["schema_version"]
  for row in snapshot["revisions"]:
    del row["retry_after_at"]
  files = {item["path"]: type(path)(item["path"]).read_bytes() for item in snapshot["media"] if item["path"]}
  store.connection.execute("PRAGMA foreign_keys=OFF")
  store.connection.executescript("DROP TABLE media; DROP TABLE revisions; DROP TABLE events;" + LEGACY_SCHEMA)
  with store.connection:
    for table, rows in snapshot.items():
      for row in rows:
        columns, placeholders = ",".join(row), ",".join("?" for _ in row)
        store.connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values()))
    store.connection.execute("PRAGMA user_version=0")
  store.close()
  return pending, acknowledged, snapshot, files


def assert_migrated_legacy(store, snapshot):
  for table in ("events", "revisions", "media"):
    actual = [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
    if table == "events":
      assert all(row.pop("schema_version") == 1 for row in actual)
    elif table == "revisions":
      assert all(row.pop("retry_after_at") == 0 for row in actual)
    assert actual == snapshot[table]
  assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
  assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
  assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 5
  assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
  assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2


@pytest.mark.parametrize("legacy_version", (0, 1))
def test_legacy_migration_preserves_every_row_and_immutable_wire_payload(tmp_path, legacy_version) -> None:
  path = tmp_path / "outbox.sqlite3"
  pending, acknowledged, snapshot, files = create_legacy_outbox(path)
  with sqlite3.connect(path) as version_connection:
    version_connection.execute(f"PRAGMA user_version={legacy_version}")
  version_connection.close()
  for _ in range(3):
    store = SentryStore(path, run_maintenance=False)
    assert_migrated_legacy(store, snapshot)
    assert {name: type(path)(name).read_bytes() for name in files} == files
    assert store.next_pending(0).metadata["schema_version"] == 1
    store.close()
  recovered = SentryStore(path)
  # Recovery of legacy capturing rows must also retain the legacy wire contract.
  assert recovered.revision_state(acknowledged, 1) == "acknowledged"
  metadata = json.loads(recovered.connection.execute(
    "SELECT metadata_json FROM revisions WHERE event_id=? AND revision=2", (acknowledged,)
  ).fetchone()[0])
  assert (metadata["schema_version"], metadata["revision"], metadata["kind"]) == (1, 2, "alarm")
  assert {item["reason"] for item in metadata["omitted_media"]} == {"stale_capture"}
  assert recovered.connection.execute(
    "SELECT metadata_json FROM revisions WHERE event_id=? AND revision=1", (pending,)
  ).fetchone()[0] == next(row["metadata_json"] for row in snapshot["revisions"] if row["event_id"] == pending)
  fresh_event = begin(recovered, schema_version=2)
  begin(recovered, fresh_event, revision=2, kind="follow_up", schema_version=2)


def create_schema_two_outbox(path):
  store = SentryStore(path)
  future = datetime.now(UTC).timestamp() + 3600
  event_ids = {}
  for name, state, status, deadline, wire_schema in (
    ("http", "ready", 503, future, 1), ("network", "ready", None, future, 2),
    ("expired_http", "ready", 429, 1, 2), ("active", "uploading", 503, future, 2),
    ("terminal", "terminal", 422, future, 1), ("capturing", "capturing", None, 0, 2),
    ("acknowledged", "acknowledged", 200, 0, 1),
  ):
    event_id = event_ids[name] = begin(store, schema_version=wire_schema)
    if state != "capturing":
      finish_complete(store, event_id)
    with store.connection:
      store.connection.execute(
        "UPDATE revisions SET state=?, attempts=?, next_attempt_at=?, last_attempt_at=?, last_http_status=? WHERE event_id=?",
        (state, 0 if state == "capturing" else 7, deadline, NOW, status, event_id),
      )
  begin(store, event_ids["network"], revision=2, kind="follow_up", schema_version=2)
  finish_complete(store, event_ids["network"], revision=2)
  store.schedule_retry(event_ids["network"], 2, next_attempt_at=future, error="HTTP retry", http_status=503)
  # Build the former v2 schema using SQLite's own DDL, only in this temporary
  # fixture; production migration only adds the new column and never drops it.
  store.connection.execute("BEGIN IMMEDIATE")
  with store.connection:
    store.connection.execute("ALTER TABLE revisions DROP COLUMN retry_after_at")
    store.connection.execute("PRAGMA user_version=2")
  snapshot = {table: [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
              for table in ("events", "revisions", "media")}
  files = {row["path"]: Path(row["path"]).read_bytes() for row in snapshot["media"] if row["path"]}
  store.close()
  return event_ids, snapshot, files


def assert_migrated_schema_two(store, snapshot, files):
  for table in ("events", "revisions", "media"):
    actual = [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
    if table == "revisions":
      for row in actual:
        expected_floor = row["next_attempt_at"] if row["state"] == "ready" and row["last_http_status"] is not None and row["next_attempt_at"] > 1 else 0
        assert row.pop("retry_after_at") == expected_floor
    assert actual == snapshot[table]
  assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 5
  assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
  assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
  assert {name: Path(name).read_bytes() for name in files} == files


def test_schema_two_migration_preserves_both_wire_schemas_claims_and_server_deadlines(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  event_ids, snapshot, files = create_schema_two_outbox(path)
  for _ in range(3):
    store = SentryStore(path, run_maintenance=False)
    assert_migrated_schema_two(store, snapshot, files)
    store.close()
  store = SentryStore(path, run_maintenance=False)
  store.retry_pending()
  deadlines = {row["event_id"]: row["next_attempt_at"] for row in store.connection.execute("SELECT * FROM revisions WHERE revision=1")}
  assert deadlines[event_ids["http"]] > datetime.now(UTC).timestamp()
  assert deadlines[event_ids["active"]] == deadlines[event_ids["http"]]
  assert deadlines[event_ids["network"]] == deadlines[event_ids["expired_http"]] == 0
  assert store.revision_state(event_ids["active"], 1) == "uploading"


@pytest.mark.parametrize("fail_statement", (
  "ALTER TABLE revisions ADD COLUMN retry_after_at", "UPDATE revisions SET retry_after_at=next_attempt_at", "PRAGMA user_version=5",
))
def test_schema_two_migration_failure_rolls_back_column_version_and_every_row(tmp_path, monkeypatch, fail_statement) -> None:
  path = tmp_path / "outbox.sqlite3"
  _, snapshot, files = create_schema_two_outbox(path)
  original_connect = sqlite3.connect

  class FailingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
      result = super().execute(sql, parameters)
      if sql.strip().startswith(fail_statement):
        raise sqlite3.OperationalError("simulated schema-three migration failure")
      return result

  with monkeypatch.context() as patch:
    patch.setattr(sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, factory=FailingConnection, **kwargs))
    with pytest.raises(sqlite3.OperationalError, match="migration failure"):
      SentryStore(path, run_maintenance=False)
  verifier = original_connect(path)
  verifier.row_factory = sqlite3.Row
  try:
    assert verifier.execute("PRAGMA user_version").fetchone()[0] == 2
    assert "retry_after_at" not in {row["name"] for row in verifier.execute("PRAGMA table_info(revisions)")}
    for table, rows in snapshot.items():
      assert [dict(row) for row in verifier.execute(f"SELECT * FROM {table}")] == rows
  finally:
    verifier.close()
  migrated = SentryStore(path, run_maintenance=False)
  assert_migrated_schema_two(migrated, snapshot, files)


@pytest.mark.parametrize("crash_statement", ("ALTER TABLE revisions ADD COLUMN retry_after_at", "PRAGMA user_version=5"))
def test_process_death_during_schema_two_migration_preserves_queue(tmp_path, crash_statement) -> None:
  path = tmp_path / "outbox.sqlite3"
  _, snapshot, files = create_schema_two_outbox(path)
  program = """
import os
import sqlite3
import sys
from openpilot.system.sentryd.store import SentryStore
original_connect = sqlite3.connect
class CrashConnection(sqlite3.Connection):
  def execute(self, sql, parameters=()):
    result = super().execute(sql, parameters)
    if sql.strip().startswith(sys.argv[2]):
      os._exit(77)
    return result
sqlite3.connect = lambda *args, **kwargs: original_connect(*args, factory=CrashConnection, **kwargs)
SentryStore(sys.argv[1], run_maintenance=False)
"""
  result = subprocess.run([sys.executable, "-c", program, str(path), crash_statement], timeout=10, check=False)
  assert result.returncode == 77
  verifier = sqlite3.connect(path)
  try:
    assert verifier.execute("PRAGMA user_version").fetchone()[0] == 2
    assert "retry_after_at" not in {row[1] for row in verifier.execute("PRAGMA table_info(revisions)")}
  finally:
    verifier.close()
  migrated = SentryStore(path, run_maintenance=False)
  assert_migrated_schema_two(migrated, snapshot, files)


def test_concurrent_legacy_initializers_migrate_once_without_losing_rows(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  _, _, snapshot, _ = create_legacy_outbox(path)
  barrier = threading.Barrier(4)
  opened = threading.Barrier(4)

  def initialize(_index):
    barrier.wait(timeout=5)
    store = None
    try:
      store = SentryStore(path, run_maintenance=False)
      assert_migrated_legacy(store, snapshot)
      opened.wait(timeout=5)  # keep shared sidecars present until every opener verifies
    except Exception:
      opened.abort()
      raise
    finally:
      if store is not None:
        store.close()

  with ThreadPoolExecutor(max_workers=4) as pool:
    futures = [pool.submit(initialize, index) for index in range(4)]
    failures = [future.exception(timeout=15) for future in futures if future.exception(timeout=15) is not None]
  assert not failures, [repr(error) for error in failures]


def test_concurrent_first_time_initializers_create_private_database(tmp_path) -> None:
  path = tmp_path / "sentry" / "outbox.sqlite3"
  barrier = threading.Barrier(4)
  opened = threading.Barrier(4)

  def initialize(_index):
    barrier.wait(timeout=5)
    store = None
    try:
      store = SentryStore(path, run_maintenance=False)
      assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 5
      assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
      opened.wait(timeout=5)
    except Exception:
      opened.abort()
      raise
    finally:
      if store is not None:
        store.close()

  with ThreadPoolExecutor(max_workers=4) as pool:
    futures = [pool.submit(initialize, index) for index in range(4)]
    failures = [future.exception(timeout=15) for future in futures if future.exception(timeout=15) is not None]
  assert not failures, [repr(error) for error in failures]
  assert (path.stat().st_mode & 0o777) == 0o600


def test_current_schema_open_does_not_request_a_migration_write_lock(tmp_path, monkeypatch) -> None:
  path = tmp_path / "outbox.sqlite3"
  initial = SentryStore(path)
  original_connect = sqlite3.connect
  statements = []

  def traced_connect(*args, **kwargs):
    connection = original_connect(*args, **kwargs)
    connection.set_trace_callback(statements.append)
    return connection

  monkeypatch.setattr(sqlite3, "connect", traced_connect)
  reopened = SentryStore(path, run_maintenance=False)
  assert "BEGIN IMMEDIATE" not in statements
  assert "PRAGMA foreign_key_check" not in statements
  assert "PRAGMA user_version=5" not in statements
  reopened.close()
  initial.close()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY | (2 << 8)])
def test_wal_initialization_retries_only_lock_contention(tmp_path, monkeypatch, code) -> None:
  original_connect = sqlite3.connect
  attempts = []

  class BusyConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
      if sql == "PRAGMA journal_mode=WAL":
        attempts.append(sql)
        if len(attempts) < 3:
          error = sqlite3.OperationalError("simulated WAL contention")
          error.sqlite_errorcode = code
          raise error
      return super().execute(sql, parameters)

  monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, factory=BusyConnection, **kwargs))
  monkeypatch.setattr(store_module.time, "sleep", lambda _duration: None)
  store = SentryStore(tmp_path / "outbox.sqlite3")
  assert len(attempts) == 3
  assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_IOERR])
def test_wal_failure_is_bounded_and_closes_connection(tmp_path, monkeypatch, code) -> None:
  original_connect = sqlite3.connect
  closed = []
  attempts = []
  now = iter((0.0, 10.0))

  class FailedConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
      if sql == "PRAGMA journal_mode=WAL":
        attempts.append(sql)
        error = sqlite3.OperationalError("simulated WAL failure")
        error.sqlite_errorcode = code
        raise error
      return super().execute(sql, parameters)

    def close(self):
      closed.append(True)
      super().close()

  monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, factory=FailedConnection, **kwargs))
  monkeypatch.setattr(store_module.time, "monotonic", lambda: next(now))
  with pytest.raises(sqlite3.OperationalError, match="WAL failure"):
    SentryStore(tmp_path / "outbox.sqlite3")
  assert len(attempts) == len(closed) == 1


@pytest.mark.parametrize("fail_statement", [
  "INSERT INTO sentry_revisions_v2", "DROP TABLE revisions", "ALTER TABLE sentry_revisions_v2", "PRAGMA user_version=5",
])
def test_legacy_migration_failure_rolls_back_schema_and_rows(tmp_path, monkeypatch, fail_statement) -> None:
  path = tmp_path / "outbox.sqlite3"
  _, _, snapshot, files = create_legacy_outbox(path)
  original_connect = sqlite3.connect

  class FailingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
      result = super().execute(sql, parameters)
      if sql.startswith(fail_statement):
        raise sqlite3.OperationalError("simulated migration disk failure")
      return result

  with monkeypatch.context() as patch:
    patch.setattr(sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, factory=FailingConnection, **kwargs))
    with pytest.raises(sqlite3.OperationalError, match="migration disk failure"):
      SentryStore(path, run_maintenance=False)
  verifier = original_connect(path)
  verifier.row_factory = sqlite3.Row
  try:
    assert verifier.execute("PRAGMA user_version").fetchone()[0] == 0
    assert "schema_version" not in {row["name"] for row in verifier.execute("PRAGMA table_info(events)")}
    assert verifier.execute("SELECT 1 FROM sqlite_master WHERE name='sentry_revisions_v2'").fetchone() is None
    for table, rows in snapshot.items():
      assert [dict(row) for row in verifier.execute(f"SELECT * FROM {table}")] == rows
    assert {name: type(path)(name).read_bytes() for name in files} == files
  finally:
    verifier.close()
  restarted = SentryStore(path, run_maintenance=False)
  assert_migrated_legacy(restarted, snapshot)


@pytest.mark.parametrize("crash_statement", ["DROP TABLE revisions", "ALTER TABLE sentry_revisions_v2", "PRAGMA user_version=5"])
def test_process_death_during_migration_recovers_the_complete_legacy_outbox(tmp_path, crash_statement) -> None:
  path = tmp_path / "outbox.sqlite3"
  _, _, snapshot, files = create_legacy_outbox(path)
  program = """
import os
import sqlite3
import sys
from openpilot.system.sentryd.store import SentryStore
original_connect = sqlite3.connect
class CrashConnection(sqlite3.Connection):
  def execute(self, sql, parameters=()):
    result = super().execute(sql, parameters)
    if sql.startswith(sys.argv[2]):
      os._exit(77)
    return result
sqlite3.connect = lambda *args, **kwargs: original_connect(*args, factory=CrashConnection, **kwargs)
SentryStore(sys.argv[1], run_maintenance=False)
"""
  result = subprocess.run([sys.executable, "-c", program, str(path), crash_statement], timeout=10, check=False)
  assert result.returncode == 77
  with sqlite3.connect(path) as verifier:
    assert verifier.execute("PRAGMA user_version").fetchone()[0] == 0
    assert "schema_version" not in {row[1] for row in verifier.execute("PRAGMA table_info(events)")}
  recovered = SentryStore(path, run_maintenance=False)
  assert_migrated_legacy(recovered, snapshot)
  assert {name: type(path)(name).read_bytes() for name in files} == files


def test_legacy_quota_rewrite_does_not_upgrade_wire_schema(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  pending, _, snapshot, _ = create_legacy_outbox(path)
  store = SentryStore(path, run_maintenance=False, media_quota_bytes=1)
  # A known terminal response permits a quota omission rewrite; an uncertain
  # attempted payload must instead stay byte-for-byte immutable.
  store.mark_terminal(pending, 1, error="rejected", http_status=422)
  store.enforce_media_quota()
  metadata = json.loads(store.connection.execute(
    "SELECT metadata_json FROM revisions WHERE event_id=? AND revision=1", (pending,)
  ).fetchone()[0])
  assert metadata["schema_version"] == 1
  assert metadata["capture_status"] == "omitted"
  original = next(row["metadata_json"] for row in snapshot["revisions"] if row["event_id"] == pending)
  assert json.loads(original)["capture_status"] == "complete"


def test_future_database_schema_is_not_downgraded(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  store.connection.execute("PRAGMA user_version=6")
  store.close()
  with pytest.raises(ValueError, match="future"):
    SentryStore(path)
  with sqlite3.connect(path) as connection:
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


def test_revision_timestamps_are_clamped_to_server_ordering_invariants(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = str(uuid4())
  episode = "2026-09-04T12:00:00+00:00"
  store.begin_revision(
    event_id=event_id, revision=1, kind="warning", source="motion",
    episode_started_at=episode, detected_at="2026-09-04T11:59:00+00:00", message="warning",
  )
  store.finish_capture(event_id, 1, {}, {"wide": "capture_failed", "cabin": "capture_failed"})
  assert store.next_pending(0).metadata["detected_at"] == episode
  store.begin_revision(
    event_id=event_id, revision=2, kind="alarm", source="motion",
    episode_started_at=episode, detected_at="2026-09-04T11:58:00+00:00", message="alarm",
  )
  store.finish_capture(event_id, 2, {}, {"wide": "capture_failed", "cabin": "capture_failed"})
  alarm_metadata = json.loads(store.connection.execute(
    "SELECT metadata_json FROM revisions WHERE event_id=? AND revision=2", (event_id,)
  ).fetchone()[0])
  assert alarm_metadata["detected_at"] == episode


def test_partial_capture_and_reason_allowlist(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store)
  store.finish_capture(event_id, 1, {"wide": MediaData(JPEG, 10, 10)}, {"cabin": "camera_unavailable"})
  assert store.next_pending(0).metadata["capture_status"] == "partial"
  other = begin(store)
  with pytest.raises(ValueError, match="omission"):
    store.finish_capture(other, 1, {}, {"wide": "invented", "cabin": "capture_failed"})


def test_ack_retains_open_warning_for_alarm_promotion(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store)
  finish_complete(store, event_id)
  queued = store.next_pending(0)
  expected = {item.role: item.sha256 for item in queued.media}
  assert store.acknowledge(event_id, 1, expected)
  assert store.stats().media_bytes == 0
  begin(store, event_id, revision=2, kind="alarm")
  store.finish_capture(event_id, 2, {}, {"wide": "capture_failed", "cabin": "capture_failed"})
  store.close_event(event_id)
  assert store.connection.execute(
    "SELECT state FROM revisions WHERE event_id=? AND revision=1", (event_id,)).fetchone()[0] == "acknowledged"
  assert store.connection.execute(
    "SELECT 1 FROM revisions WHERE event_id=? AND revision=2", (event_id,)).fetchone() is not None


def test_alarm_upload_waits_for_warning_ack_across_backoff_and_restart(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store)
  finish_complete(store, event_id)
  store.schedule_retry(event_id, 1, next_attempt_at=1000, error="offline")
  begin(store, event_id, revision=2, kind="alarm")
  finish_complete(store, event_id, revision=2)

  assert not store.upload_work_due(0)
  assert store.claim_pending(0) is None
  with store.connection:
    store.connection.execute(
      "UPDATE revisions SET next_attempt_at=0 WHERE event_id=? AND revision=1", (event_id,)
    )
  warning = store.claim_pending(0)
  assert warning is not None and warning.revision == 1
  assert store.acknowledge(event_id, 1, {item.role: item.sha256 for item in warning.media})
  store.close_event(event_id)
  assert store.connection.execute(
    "SELECT state FROM revisions WHERE event_id=? AND revision=1", (event_id,)
  ).fetchone()[0] == "acknowledged"
  store.close()

  reopened = SentryStore(path)
  assert reopened.upload_work_due(0)
  alarm = reopened.claim_pending(0)
  assert alarm is not None and alarm.revision == 2
  assert reopened.acknowledge(event_id, 2, {item.role: item.sha256 for item in alarm.media})
  assert reopened.connection.execute(
    "SELECT 1 FROM events WHERE event_id=?", (event_id,)
  ).fetchone() is None


def test_deleted_tombstone_ack_discards_local_media(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store)
  finish_complete(store, event_id)
  assert store.acknowledge(event_id, 1, {}, deleted=True)
  assert store.stats().media_bytes == 0


def test_interrupted_capture_recovers_as_stale_metadata(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store)
  store.close()
  recovered = SentryStore(path)
  queued = recovered.next_pending(0)
  assert queued.event_id == event_id
  assert queued.metadata["capture_status"] == "failed"
  assert {item["reason"] for item in queued.metadata["omitted_media"]} == {"stale_capture"}


def test_unattempted_quota_eviction_rewrites_payload_before_send(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3", media_quota_bytes=1)
  event_id = begin(store)
  finish_complete(store, event_id)
  queued = store.next_pending(0)
  assert queued.media == ()
  assert queued.metadata["capture_status"] == "omitted"
  assert {item["reason"] for item in queued.metadata["omitted_media"]} == {"queue_quota"}
  assert store.stats().media_bytes == 0


def test_attempted_payload_is_immutable_but_bytes_are_hard_capped(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3", media_quota_bytes=1024)
  event_id = begin(store)
  finish_complete(store, event_id)
  before = json.dumps(store.next_pending(0).metadata, sort_keys=True)
  store.record_attempt(event_id, 1)
  store.media_quota_bytes = 1
  assert store.enforce_media_quota() == 1
  row = store.connection.execute(
    "SELECT state, retryable, metadata_json FROM revisions WHERE event_id=? AND revision=1", (event_id,)
  ).fetchone()
  assert row["state"] == "terminal" and row["retryable"] == 0
  assert json.dumps(json.loads(row["metadata_json"]), sort_keys=True) == before
  retained = store.connection.execute(
    "SELECT path, sha256, size, local_omission_reason FROM media WHERE event_id=? AND revision=1",
    (event_id,),
  ).fetchall()
  assert retained and all(item["path"] is None and item["sha256"] and item["size"] > 0 for item in retained)
  assert {item["local_omission_reason"] for item in retained} == {"queue_quota"}
  assert store.stats().media_bytes == 0
  assert store.retry_terminal() == 0
  assert store.retry_pending() == 0
  assert store.retry_all() == 0


def test_known_terminal_payload_can_be_rewritten_as_queue_quota_omission(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3", media_quota_bytes=1024)
  event_id = begin(store)
  finish_complete(store, event_id)
  store.record_attempt(event_id, 1)
  store.mark_terminal(event_id, 1, error="RTZ returned terminal HTTP 422", http_status=422)
  store.media_quota_bytes = 1
  assert store.enforce_media_quota() == 1
  row = store.connection.execute(
    "SELECT state, retryable, metadata_json FROM revisions WHERE event_id=? AND revision=1", (event_id,)
  ).fetchone()
  assert row["state"] == "terminal" and row["retryable"] == 1
  metadata = json.loads(row["metadata_json"])
  assert metadata["capture_status"] == "omitted"
  assert {item["reason"] for item in metadata["omitted_media"]} == {"queue_quota"}
  assert store.stats().media_bytes == 0
  assert store.retry_terminal() == 1


def test_atomic_upload_claim_freezes_payload_before_quota_eviction(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path, media_quota_bytes=1024)
  event_id = begin(store)
  finish_complete(store, event_id)
  before = json.dumps(store.next_pending(0).metadata, sort_keys=True)
  claimed = store.claim_pending(0)
  assert claimed is not None and claimed.attempts == 0
  assert store.next_pending(0) is None

  store.media_quota_bytes = 1
  assert store.enforce_media_quota() == 0
  row = store.connection.execute(
    "SELECT state, retryable, attempts, metadata_json FROM revisions WHERE event_id=? AND revision=1", (event_id,)
  ).fetchone()
  assert (row["state"], row["retryable"], row["attempts"]) == ("uploading", 1, 1)
  assert json.dumps(json.loads(row["metadata_json"]), sort_keys=True) == before
  assert store.stats().media_bytes > 1
  store.schedule_retry(event_id, 1, next_attempt_at=5, error="offline")
  assert store.enforce_media_quota() == 1
  row = store.connection.execute(
    "SELECT state, retryable, metadata_json FROM revisions WHERE event_id=? AND revision=1", (event_id,)
  ).fetchone()
  assert (row["state"], row["retryable"]) == ("terminal", 0)
  assert json.dumps(json.loads(row["metadata_json"]), sort_keys=True) == before


def test_quota_rewrite_and_upload_claim_are_serialized_across_connections(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  producer = SentryStore(path)
  event_id = begin(producer)
  finish_complete(producer, event_id)
  producer.close()
  reserved = threading.Event()
  release_eviction = threading.Event()
  claim_finished = threading.Event()
  result = []

  class BlockingQuotaStore(SentryStore):
    def _finish_reserved_eviction(self, event_id, revision, reserved_state):
      reserved.set()
      assert release_eviction.wait(1)
      return super()._finish_reserved_eviction(event_id, revision, reserved_state)

  def enforce_quota():
    store = BlockingQuotaStore(path, media_quota_bytes=1, run_maintenance=False)
    try:
      assert store.enforce_media_quota() == 1
    finally:
      store.close()

  def claim_upload():
    store = SentryStore(path, run_maintenance=False)
    try:
      result.append(store.claim_pending(0))
    finally:
      store.close()
      claim_finished.set()

  quota_thread = threading.Thread(target=enforce_quota)
  quota_thread.start()
  assert reserved.wait(1)
  claim_thread = threading.Thread(target=claim_upload)
  claim_thread.start()
  assert claim_finished.wait(1)
  assert result == [None]
  release_eviction.set()
  quota_thread.join(1)
  claim_thread.join(1)
  assert not quota_thread.is_alive() and not claim_thread.is_alive()
  verifier = SentryStore(path, run_maintenance=False)
  rewritten = verifier.claim_pending(0)
  assert rewritten.event_id == event_id and rewritten.media == ()
  assert {item["reason"] for item in rewritten.metadata["omitted_media"]} == {"queue_quota"}


def test_partial_quota_delete_stays_reserved_and_recovers_on_restart(tmp_path, monkeypatch) -> None:
  path = tmp_path / "sentry" / "outbox.sqlite3"
  store = SentryStore(path, media_quota_bytes=1024)
  event_id = begin(store)
  finish_complete(store, event_id)
  media_paths = [type(path)(item.path) for item in store.next_pending(0).media]

  def fail_after_first_unlink(paths):
    type(path)(paths[0]).unlink()
    raise OSError("simulated partial media deletion")

  monkeypatch.setattr(store, "_delete_paths", fail_after_first_unlink)
  store.media_quota_bytes = 1
  with pytest.raises(OSError, match="partial media deletion"):
    store.enforce_media_quota()
  assert store.revision_state(event_id, 1) == "evicting_ready"
  assert store.claim_pending(0) is None
  assert any(item.exists() for item in media_paths)
  store.close()

  recovered = SentryStore(path, media_quota_bytes=1)
  assert recovered.revision_state(event_id, 1) == "ready"
  queued = recovered.claim_pending(0)
  assert queued is not None and queued.media == ()
  assert {item["reason"] for item in queued.metadata["omitted_media"]} == {"queue_quota"}
  assert all(not item.exists() for item in media_paths)


def test_quota_delete_sync_failure_is_restart_safe_and_recovery_resyncs_missing_paths(tmp_path, monkeypatch) -> None:
  path = tmp_path / "sentry" / "outbox.sqlite3"
  store = SentryStore(path, media_quota_bytes=1024)
  event_id = begin(store)
  finish_complete(store, event_id)
  queued = store.next_pending(0)
  media_paths = [type(path)(item.path) for item in queued.media]
  revision_directory = media_paths[0].parent
  original_sync = store._fsync_directory

  def fail_delete_sync(directory):
    if directory == revision_directory:
      raise OSError("simulated delete directory sync failure")
    original_sync(directory)

  monkeypatch.setattr(store, "_fsync_directory", fail_delete_sync)
  store.media_quota_bytes = 1
  with pytest.raises(OSError, match="delete directory sync failure"):
    store.enforce_media_quota()
  assert store.revision_state(event_id, 1) == "evicting_ready"
  assert store.claim_pending(0) is None
  assert all(not item.exists() for item in media_paths)
  store.close()

  restarted = SentryStore(path, media_quota_bytes=1, run_maintenance=False)
  assert restarted.claim_pending(0) is None
  synced = []
  restarted_sync = restarted._fsync_directory

  def record_sync(directory):
    synced.append(directory)
    restarted_sync(directory)

  monkeypatch.setattr(restarted, "_fsync_directory", record_sync)
  assert restarted.recover_interrupted_evictions() == 1
  assert revision_directory in synced
  rewritten = restarted.claim_pending(0)
  assert rewritten is not None and rewritten.media == ()
  assert {item["reason"] for item in rewritten.metadata["omitted_media"]} == {"queue_quota"}


def test_upload_claim_lease_prevents_duplicate_concurrent_claim(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  first = SentryStore(path)
  event_id = begin(first)
  finish_complete(first, event_id)
  second = SentryStore(path, run_maintenance=False)
  assert first.claim_pending(100) is not None
  assert second.claim_pending(100) is None
  assert second.claim_pending(221).event_id == event_id


def test_expired_upload_claim_is_recovered_without_another_restart(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store)
  finish_complete(store, event_id)
  now = datetime.now(UTC).timestamp()
  assert store.claim_pending(now).event_id == event_id
  store.close()

  restarted = SentryStore(path)
  assert restarted.next_pending(now) is None
  assert restarted.claim_pending(now + 121).event_id == event_id


def test_database_and_sidecars_are_private(tmp_path) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  begin(store)
  assert store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
  assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
  assert (store.path.stat().st_mode & 0o777) == 0o600
  for suffix in ("-wal", "-shm"):
    sidecar = type(store.path)(str(store.path) + suffix)
    if sidecar.exists():
      assert (sidecar.stat().st_mode & 0o777) == 0o600


def test_sidecar_validation_never_opens_or_chmods_live_sqlite_files(tmp_path, monkeypatch) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = begin(store)
  finish_complete(store, event_id)
  before = store.next_pending(0)
  inodes = dict(store._sidecar_inodes)

  def forbidden(*_args, **_kwargs):
    pytest.fail("validating a live SQLite sidecar must not open, close or chmod its descriptor")

  try:
    with monkeypatch.context() as guard:
      for name in ("open", "close", "chmod", "fchmod"):
        guard.setattr(store_module.os, name, forbidden)
      store._secure_sidecars()
    assert store._sidecar_inodes == inodes
    assert store.next_pending(0) == before
    assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
  finally:
    store.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("unsafe", ["permissions", "symlink", "directory", "inode"])
def test_live_sidecar_validation_fails_closed_without_repair(tmp_path, monkeypatch, suffix, known, unsafe) -> None:
  store = SentryStore(tmp_path / "outbox.sqlite3")
  sidecar = Path(str(store.path) + suffix)
  # Fake only the metadata. Never truncate, replace, chmod or unlink a real
  # live WAL/SHM file in a permission regression test.
  metadata = SimpleNamespace(st_dev=123, st_ino=456, st_mode=stat.S_IFREG | 0o600)
  if known:
    store._sidecar_inodes[suffix] = (metadata.st_dev, metadata.st_ino)
  else:
    store._sidecar_inodes.pop(suffix, None)
  if unsafe == "permissions":
    metadata.st_mode = stat.S_IFREG | 0o640
  elif unsafe == "symlink":
    metadata.st_mode = stat.S_IFLNK | 0o600
  elif unsafe == "directory":
    metadata.st_mode = stat.S_IFDIR | 0o600
  elif known:
    metadata.st_ino += 1
  original_lstat = Path.lstat

  def lstat(path, *args, **kwargs):
    return metadata if path == sidecar else original_lstat(path, *args, **kwargs)

  def forbidden(*_args, **_kwargs):
    pytest.fail("unsafe SQLite sidecars must not be opened or repaired")

  try:
    with monkeypatch.context() as guard:
      guard.setattr(Path, "lstat", lstat)
      for name in ("open", "close", "chmod", "fchmod"):
        guard.setattr(store_module.os, name, forbidden)
      if unsafe == "inode" and not known:
        store._secure_sidecars()  # First-seen private regular inode is remembered, never reopened.
        assert store._sidecar_inodes[suffix] == (metadata.st_dev, metadata.st_ino)
      else:
        with pytest.raises(OSError, match="sidecar"):
          store._secure_sidecars()
  finally:
    store.close()


def test_rejects_symlink_database_without_touching_target(tmp_path) -> None:
  root = tmp_path / "sentry"
  root.mkdir(mode=0o700)
  target = tmp_path / "outside.sqlite3"
  target.write_bytes(b"outside")
  os.chmod(target, 0o600)
  (root / "outbox.sqlite3").symlink_to(target)
  with pytest.raises(OSError, match="database"):
    SentryStore(root / "outbox.sqlite3")
  assert target.read_bytes() == b"outside"


@pytest.mark.parametrize("mode", [0o700, 0o640])
def test_existing_database_requires_exact_regular_private_mode(tmp_path, mode) -> None:
  path = tmp_path / "outbox.sqlite3"
  path.write_bytes(b"")
  os.chmod(path, mode)
  with pytest.raises(OSError, match="database"):
    SentryStore(path)


@pytest.mark.parametrize("unsafe", ["directory", "permissions", "symlink"])
def test_rejects_unsafe_preexisting_database_sidecars(tmp_path, unsafe) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  store.close()
  sidecar = type(path)(str(path) + "-wal")
  outside = tmp_path / "outside"
  if unsafe == "directory":
    sidecar.mkdir(mode=0o700)
  elif unsafe == "permissions":
    sidecar.write_bytes(b"")
    os.chmod(sidecar, 0o640)
  else:
    outside.write_bytes(b"outside")
    sidecar.symlink_to(outside)
  with pytest.raises(OSError, match="sidecar"):
    SentryStore(path)


def test_read_outbox_stats_rejects_unsafe_sidecar(tmp_path) -> None:
  path = tmp_path / "outbox.sqlite3"
  store = SentryStore(path)
  store.close()
  outside = tmp_path / "outside"
  outside.write_bytes(b"outside")
  Path = type(path)
  Path(str(path) + "-wal").symlink_to(outside)
  with pytest.raises(OSError, match="sidecar"):
    read_outbox_stats(path)


def test_intermediate_media_symlink_is_rejected_and_partial_paths_stay_private(tmp_path) -> None:
  store = SentryStore(tmp_path / "sentry" / "outbox.sqlite3")
  event_id = begin(store)
  outside = tmp_path / "outside"
  outside.mkdir()
  event_directory = store.media_root / event_id
  event_directory.symlink_to(outside, target_is_directory=True)
  finish_complete(store, event_id)
  queued = store.next_pending(0)
  assert queued is not None and queued.media == ()
  assert {entry["reason"] for entry in queued.metadata["omitted_media"]} == {"capture_failed"}
  assert list(outside.iterdir()) == []


def test_media_directory_and_file_modes_are_exact(tmp_path) -> None:
  store = SentryStore(tmp_path / "sentry" / "outbox.sqlite3")
  event_id = begin(store)
  finish_complete(store, event_id)
  event_directory = store.media_root / event_id
  revision_directory = event_directory / "1"
  assert (store.root.stat().st_mode & 0o7777) == 0o700
  assert (store.media_root.stat().st_mode & 0o7777) == 0o700
  assert (event_directory.stat().st_mode & 0o7777) == 0o700
  assert (revision_directory.stat().st_mode & 0o7777) == 0o700
  assert all((path.stat().st_mode & 0o7777) == 0o600 for path in revision_directory.iterdir())


def test_nested_directory_creation_and_media_deletion_are_directory_synced(tmp_path, monkeypatch) -> None:
  store = SentryStore(tmp_path / "sentry" / "outbox.sqlite3")
  event_id = begin(store)
  synced = []
  original_sync = store._fsync_directory

  def record_sync(path):
    synced.append(path)
    original_sync(path)

  monkeypatch.setattr(store, "_fsync_directory", record_sync)
  finish_complete(store, event_id)
  event_directory = store.media_root / event_id
  revision_directory = event_directory / "1"
  assert store.media_root in synced
  assert event_directory in synced
  assert revision_directory in synced
  queued = store.next_pending(0)
  synced.clear()
  assert store.acknowledge(event_id, 1, {item.role: item.sha256 for item in queued.media})
  assert revision_directory in synced


def test_database_failure_after_media_write_cleans_orphaned_files(tmp_path, monkeypatch) -> None:
  store = SentryStore(tmp_path / "sentry" / "outbox.sqlite3")
  event_id = begin(store)

  def fail_metadata(_metadata):
    raise ValueError("simulated transaction failure")

  monkeypatch.setattr(store, "_dump_metadata", fail_metadata)
  with pytest.raises(ValueError, match="transaction failure"):
    finish_complete(store, event_id)
  assert list(store.media_root.rglob("*.jpg")) == []
  assert store.connection.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
  assert store.connection.execute(
    "SELECT state FROM revisions WHERE event_id=? AND revision=1", (event_id,)
  ).fetchone()[0] == "capturing"


def test_post_replace_sync_failure_does_not_leave_untracked_media(tmp_path, monkeypatch) -> None:
  store = SentryStore(tmp_path / "sentry" / "outbox.sqlite3")
  event_id = begin(store)
  original_sync = store._fsync_directory

  def fail_revision_sync(path):
    if path.name == "1":
      raise OSError("simulated directory sync failure")
    original_sync(path)

  monkeypatch.setattr(store, "_fsync_directory", fail_revision_sync)
  finish_complete(store, event_id)
  assert list(store.media_root.rglob("*.jpg")) == []
  queued = store.next_pending(0)
  assert queued is not None and queued.media == ()
  assert {item["reason"] for item in queued.metadata["omitted_media"]} == {"capture_failed"}


def test_late_duplicate_finish_cannot_overwrite_ready_media(tmp_path) -> None:
  store = SentryStore(tmp_path / "sentry" / "outbox.sqlite3")
  event_id = begin(store)
  finish_complete(store, event_id)
  queued = store.next_pending(0)
  before = {item.role: type(store.path)(item.path).read_bytes() for item in queued.media}
  with pytest.raises(ValueError, match="not awaiting"):
    store.finish_capture(event_id, 1, {
      "wide": MediaData(b"\xff\xd8replacement\xff\xd9", 4, 2),
      "cabin": MediaData(b"\xff\xd8replacement\xff\xd9", 4, 2),
    }, {})
  assert {item.role: type(store.path)(item.path).read_bytes() for item in queued.media} == before


def test_startup_enforces_media_quota(tmp_path) -> None:
  path = tmp_path / "sentry" / "outbox.sqlite3"
  store = SentryStore(path, media_quota_bytes=1024)
  event_id = begin(store)
  finish_complete(store, event_id)
  assert store.stats().media_bytes > 1
  store.close()
  reopened = SentryStore(path, media_quota_bytes=1)
  assert reopened.stats().media_bytes == 0
  assert reopened.next_pending(0).metadata["capture_status"] == "omitted"


def test_startup_sweeps_safe_orphan_media_without_touching_manifested_files(tmp_path) -> None:
  path = tmp_path / "sentry" / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store)
  finish_complete(store, event_id)
  queued = store.next_pending(0)
  manifested = [type(path)(item.path) for item in queued.media]
  orphan = manifested[0].parent / ".wide.interrupted"
  orphan.write_bytes(JPEG)
  os.chmod(orphan, 0o600)
  store._fsync_directory(orphan.parent)
  store.close()

  reopened = SentryStore(path)
  assert not orphan.exists()
  assert all(item.exists() for item in manifested)
  assert reopened.stats().media_bytes == sum(item.stat().st_size for item in manifested)


def test_startup_rejects_unsafe_or_missing_manifested_media(tmp_path) -> None:
  path = tmp_path / "sentry" / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store)
  finish_complete(store, event_id)
  queued = store.next_pending(0)
  media_paths = [type(path)(item.path) for item in queued.media]
  store.close()

  media_paths[0].unlink()
  with pytest.raises(OSError, match="media file is missing"):
    SentryStore(path)


def test_startup_rejects_symlinked_orphan_media_without_following_it(tmp_path) -> None:
  path = tmp_path / "sentry" / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store)
  finish_complete(store, event_id)
  revision_directory = type(path)(store.next_pending(0).media[0].path).parent
  store.close()
  outside = tmp_path / "outside.jpg"
  outside.write_bytes(b"outside")
  (revision_directory / ".orphan").symlink_to(outside)

  with pytest.raises(OSError, match="unsafe Sentry media file"):
    SentryStore(path)
  assert outside.read_bytes() == b"outside"


def test_restart_finishes_acknowledged_media_cleanup(tmp_path, monkeypatch) -> None:
  path = tmp_path / "sentry" / "outbox.sqlite3"
  store = SentryStore(path)
  event_id = begin(store)
  finish_complete(store, event_id)
  queued = store.next_pending(0)
  expected = {item.role: item.sha256 for item in queued.media}
  media_paths = [type(path)(item.path) for item in queued.media]

  def simulate_crash(_paths):
    raise OSError("simulated power loss")

  monkeypatch.setattr(store, "_delete_paths", simulate_crash)
  with pytest.raises(OSError, match="power loss"):
    store.acknowledge(event_id, 1, expected)
  assert all(item.exists() for item in media_paths)
  assert store.connection.execute(
    "SELECT state FROM revisions WHERE event_id=? AND revision=1", (event_id,)
  ).fetchone()[0] == "acknowledged"
  store.close()

  recovered = SentryStore(path)
  assert all(not item.exists() for item in media_paths)
  assert recovered.connection.execute(
    "SELECT COUNT(*) FROM media WHERE event_id=? AND revision=1", (event_id,)
  ).fetchone()[0] == 0


def test_quota_retries_ack_cleanup_before_evicting_pending_media(tmp_path, monkeypatch) -> None:
  store = SentryStore(tmp_path / "sentry" / "outbox.sqlite3", media_quota_bytes=1024)
  acknowledged_event = begin(store)
  finish_complete(store, acknowledged_event)
  acknowledged = store.next_pending(0)
  expected = {item.role: item.sha256 for item in acknowledged.media}
  acknowledged_paths = [type(store.path)(item.path) for item in acknowledged.media]
  original_delete = store._delete_paths

  def fail_once(_paths):
    raise OSError("simulated transient cleanup failure")

  monkeypatch.setattr(store, "_delete_paths", fail_once)
  with pytest.raises(OSError, match="transient cleanup failure"):
    store.acknowledge(acknowledged_event, 1, expected)
  assert all(path.exists() for path in acknowledged_paths)

  monkeypatch.setattr(store, "_delete_paths", original_delete)
  pending_event = begin(store)
  finish_complete(store, pending_event)
  pending_bytes = store.connection.execute(
    "SELECT SUM(size) FROM media WHERE event_id=? AND revision=1", (pending_event,)
  ).fetchone()[0]
  store.media_quota_bytes = pending_bytes

  assert store.enforce_media_quota() == 0
  assert all(not path.exists() for path in acknowledged_paths)
  assert store.revision_state(pending_event, 1) == "ready"
  assert store.stats().media_bytes == pending_bytes
