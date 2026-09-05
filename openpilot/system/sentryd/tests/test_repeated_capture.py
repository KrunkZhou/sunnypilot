import json
import queue
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openpilot.cereal import log
from openpilot.common.swaglog import cloudlog
from openpilot.system.sentryd.capture import CaptureResult
from openpilot.system.sentryd.sentryd import ActiveCapture, CaptureJob, SentryMode
from openpilot.system.sentryd.store import MediaData, SentryStore
from openpilot.system.sentryd.tests.test_sentryd import ConfigStore, Params, SubMaster


class RepeatCapture:
  def __init__(self):
    self.sessions = 0
    self.pairs = 0

  def capture_repeated(self, completed, *, next_capture, abort_callback):
    self.sessions += 1
    while not abort_callback():
      self.pairs += 1
      completed(CaptureResult({role: MediaData(
        b"\xff\xd8" + str(self.pairs).encode() + b"\xff\xd9", 4, 2) for role in ("wide", "cabin")}, {}))
      while not abort_callback():
        if next_capture():
          break
        time.sleep(0.001)
      else:
        return


@pytest.fixture
def mode(tmp_path):
  now = [100.0]
  capture = RepeatCapture()
  store = SentryStore(tmp_path / "outbox.sqlite3")
  mode = SentryMode(config_store=ConfigStore(), store=store, params=Params(), volatile_params=Params(),
                    sm=SubMaster(), capture=capture, clock=lambda: now[0])
  mode.test_now = now
  yield mode
  mode.stop_event.set()
  if mode.active_capture is not None:
    mode.active_capture.thread.join(1)
    assert not mode.active_capture.thread.is_alive()
  store.close()


def finish_pair(mode):
  deadline = time.monotonic() + 1
  while not mode.active_capture.result and time.monotonic() < deadline:
    time.sleep(0.001)
  assert mode.active_capture.result
  mode._finish_capture_if_ready()


def motion_at(mode, now):
  mode.test_now[0] = now
  mode.detector.previous_sample_at = now
  mode.detector.last_motion_at = now


def begin_motion(mode):
  motion_at(mode, 100.0)
  mode._process_detection("motion", 100.0)
  mode._start_capture_if_needed()
  finish_pair(mode)


def test_diagnostic_context_tracks_each_repeated_revision(mode, monkeypatch):
  contexts = []

  def record(event, **_fields):
    if event == "sentry_capture_requested":
      contexts.append(cloudlog.get_ctx())

  original_context = cloudlog.get_ctx()
  monkeypatch.setattr(cloudlog, "event", record)
  begin_motion(mode)
  for timestamp in (101.1, 102.2):
    motion_at(mode, timestamp)
    mode._schedule_motion_capture(timestamp)
    mode._start_capture_if_needed()
    finish_pair(mode)
  assert [(context["sentry_event_id"], context["sentry_revision"]) for context in contexts] == [
    (mode.active_event_id, revision) for revision in (1, 2, 3)]
  assert cloudlog.get_ctx() == original_context  # Worker-local context never leaks into the main loop.


def send_sample(mode, timestamp, x):
  # The fake clock and encoded publication must share the same nanosecond
  # precision. Repeated float additions can otherwise make a fresh test sample
  # a fraction of a nanosecond newer than the clock and correctly fail closed.
  timestamp = round(timestamp * 1e9) / 1e9
  mode.test_now[0] = timestamp
  mode.sm.updated["accelerometer"] = True
  mode.sm.recv_time["accelerometer"] = timestamp
  mode.sm.logMonoTime["accelerometer"] = round(timestamp * 1e9)
  mode.sm.accelerometer = log.SensorEventData.new_message(acceleration={"v": [x, 0.0, 9.81]}).as_reader()
  mode.update()


@pytest.mark.parametrize("warning_seconds", [0.5, 1.0, 2.0, 5.0])
def test_first_pair_is_ready_to_upload_before_warning_for_every_setting(mode, warning_seconds):
  mode.config_store.config = replace(mode.config, warning_persistence_seconds=warning_seconds)
  mode._refresh_config(100.0)
  mode.arm_started_at = 0.0
  send_sample(mode, 100.0, 0.0)
  assert mode.active_event_id is None  # a baseline alone is not motion
  send_sample(mode, 100.1, 0.1)
  assert mode.state == "motion" and not mode.detector.warning_triggered
  assert mode.capture_queue == [CaptureJob(mode.active_event_id, 1)]
  send_sample(mode, 100.2, 0.1)  # the worker starts on the next 10 Hz iteration
  finish_pair(mode)
  queued = mode.store.next_pending(0)
  assert queued is not None and queued.metadata["capture_status"] == "complete"
  assert queued.metadata["kind"] == "warning"  # unchanged RTZ revision-1 contract
  assert mode.state == "motion" and not mode.detector.warning_triggered
  assert mode.capture.pairs == 1


def test_follow_ups_and_warning_status_do_not_recreate_first_event(mode):
  mode.config_store.config = replace(mode.config, warning_persistence_seconds=5.0)
  mode._refresh_config(100.0)
  mode.arm_started_at = 0.0
  send_sample(mode, 100.0, 0.0)
  send_sample(mode, 100.1, 0.1)
  send_sample(mode, 100.2, 0.2)
  finish_pair(mode)
  event_id = mode.active_event_id
  for index in range(3, 14):
    send_sample(mode, 100 + index / 10, index / 10)
  assert mode.detector.motion_evidence_seconds < 5.0
  assert mode.state == "motion"
  finish_pair(mode)
  assert mode.capture.pairs == 2  # repeat capture also does not wait for warning
  before = list(mode.store.connection.execute("SELECT event_id, revision FROM revisions ORDER BY revision"))
  mode._process_detection("warning", mode.clock())
  assert mode.state == "warning" and mode.active_event_id == event_id
  assert not mode.capture_queue and mode.capture.pairs == 2
  assert list(mode.store.connection.execute("SELECT event_id, revision FROM revisions ORDER BY revision")) == before
  mode._process_detection("motion", mode.clock())  # defensive duplicate signal
  assert mode.active_event_id == event_id and not mode.capture_queue


@pytest.mark.parametrize("gate", ["arming", "ignition", "disabled", "no_consent", "invalid", "stale", "subthreshold"])
def test_immediate_capture_keeps_existing_safety_and_freshness_gates(mode, gate):
  mode.arm_started_at = 0.0
  send_sample(mode, 100.0, 0.0)
  if gate == "arming":
    mode.arm_started_at = 100.0
  elif gate == "ignition":
    mode.sm.panda_states[0].ignitionCan = True
  elif gate in ("disabled", "no_consent"):
    mode.config_store.config = replace(mode.config, **(
      {"enabled": False} if gate == "disabled" else {"capture_upload_consent_version": 0}))
    mode.last_config_refresh = float("-inf")
  elif gate == "invalid":
    mode.sm.valid["accelerometer"] = False
  elif gate == "stale":
    mode.sm.updated["accelerometer"] = False
    mode.test_now[0] = 101.1
    mode.update()
    assert mode.active_event_id is None and not mode.capture_queue
    # After a gap, even a changed fresh value is only a new baseline.
  send_sample(mode, 101.2 if gate == "stale" else 100.1, 0.001 if gate == "subthreshold" else 0.1)
  assert mode.active_event_id is None and not mode.capture_queue
  assert mode.capture.pairs == 0


def test_repeats_beyond_second_capture_with_one_second_cooldown_and_warm_session(mode):
  begin_motion(mode)
  event_id = mode.active_event_id
  assert mode.store.next_pending(0).metadata["schema_version"] == 2
  for revision in range(2, 6):
    eligible = mode.next_capture_at
    motion_at(mode, eligible - 0.001)
    mode._schedule_motion_capture(mode.clock())
    assert not mode.capture_queue
    motion_at(mode, eligible)
    mode._schedule_motion_capture(mode.clock())
    assert mode.capture_queue == [CaptureJob(event_id, revision)]
    assert mode.store.revision_state(event_id, revision) == "capturing"
    mode._start_capture_if_needed()
    finish_pair(mode)
  rows = mode.store.connection.execute("SELECT revision, kind, metadata_json FROM revisions ORDER BY revision").fetchall()
  assert [(row[0], row[1]) for row in rows] == [(1, "warning")] + [(i, "follow_up") for i in range(2, 6)]
  assert {json.loads(row[2])["event_id"] for row in rows} == {event_id}
  assert mode.capture.sessions == 1 and mode.capture.pairs == 5
  assert mode.active_capture.job is None and mode.active_capture.thread.is_alive()


def test_slow_capture_or_storage_never_accumulates_follow_up_jobs(mode, monkeypatch):
  mode._process_detection("motion", 100.0)
  motion_at(mode, 102.0)
  mode._schedule_motion_capture(mode.clock())
  assert len(mode.capture_queue) == 1
  mode._start_capture_if_needed()
  finish = mode.store.finish_capture

  def disk_full(*_args):
    raise sqlite3.OperationalError("disk full")

  monkeypatch.setattr(mode.store, "finish_capture", disk_full)
  finish_pair(mode)
  assert mode.active_capture.job.revision == 1
  for timestamp in (103.0, 104.0, 105.0):
    motion_at(mode, timestamp)
    mode._schedule_motion_capture(timestamp)
    assert not mode.capture_queue
  monkeypatch.setattr(mode.store, "finish_capture", finish)
  mode._finish_capture_if_ready()
  assert mode.next_capture_at == 106.0
  assert mode.capture.pairs == 1  # retry persistence, never recapture the bytes
  motion_at(mode, 106.0)
  mode._schedule_motion_capture(106.0)
  assert mode.capture_queue[0].revision == 2


def test_alarm_uses_next_revision_then_follow_ups_continue(mode):
  begin_motion(mode)
  for revision in range(2, 6):
    motion_at(mode, mode.next_capture_at)
    if revision == 4:
      mode._process_detection("alarm", mode.clock())
    mode._schedule_motion_capture(mode.clock())
    mode._start_capture_if_needed()
    finish_pair(mode)
  assert [(row[0], row[1]) for row in mode.store.connection.execute(
    "SELECT revision, kind FROM revisions ORDER BY revision")] == [
      (1, "warning"), (2, "follow_up"), (3, "follow_up"), (4, "alarm"), (5, "follow_up")]


@pytest.mark.parametrize("stale", [False, True])
def test_quiet_or_stale_samples_stop_recaptures_and_release_idle_session(mode, stale):
  begin_motion(mode)
  event_id = mode.active_event_id
  mode.test_now[0] = mode.next_capture_at
  if stale:
    mode.detector.invalidate_samples()
  else:
    mode.detector.previous_sample_at = mode.clock()
  mode._schedule_motion_capture(mode.clock())
  assert not mode.capture_queue
  assert mode.active_capture.idle_stop.is_set()
  mode.active_capture.thread.join(1)
  mode._finish_capture_if_ready()
  assert mode.active_capture is None
  # New motion in the same episode resumes capture without a new event/warning.
  motion_at(mode, mode.clock() + 2)
  mode._schedule_motion_capture(mode.clock())
  assert mode.capture_queue == [CaptureJob(event_id, 2)]
  mode._start_capture_if_needed()
  finish_pair(mode)
  assert mode.capture.sessions == 2


def test_ignition_after_first_commit_does_not_erase_first_capture(mode):
  begin_motion(mode)
  first = mode.store.next_pending(0)
  mode._abort_capture_work("ignition_on")
  mode.active_capture.thread.join(1)
  mode._finish_capture_if_ready()
  assert mode.active_capture is None
  assert mode.store.next_pending(0).metadata == first.metadata
  assert first.metadata["capture_status"] == "complete"


def test_session_expiring_while_request_is_queued_does_not_strand_revision(mode):
  begin_motion(mode)
  mode.active_capture.idle_stop.set()
  mode.active_capture.thread.join(1)
  mode._finish_capture_if_ready()
  motion_at(mode, mode.next_capture_at)
  mode._schedule_motion_capture(mode.clock())
  job = mode.capture_queue[0]
  live = [True]
  thread = SimpleNamespace(is_alive=lambda: live[0], join=lambda _timeout: None)
  mode.active_capture = ActiveCapture(None, thread, [], requests=queue.SimpleQueue())
  mode._start_capture_if_needed()
  assert mode.active_capture.job == job and not mode.capture_queue
  live[0] = False  # worker exits at its lease deadline before consuming request
  mode._finish_capture_if_ready()
  assert mode.active_capture is None
  assert mode.store.revision_state(job.event_id, job.revision) == "capturing"
  assert mode.capture_queue == [job]
  mode._start_capture_if_needed()
  finish_pair(mode)
  assert mode.store.revision_state(job.event_id, job.revision) == "ready"
  assert mode.capture.pairs == 2


def test_manual_test_remains_one_first_revision(mode, monkeypatch):
  import openpilot.system.sentryd.sentryd as daemon
  monkeypatch.setattr(daemon, "take_command", lambda _params: {"command": "manual_test"})
  mode._handle_command()
  assert mode.active_event_id is None
  assert len(mode.capture_queue) == 1 and mode.capture_queue[0].revision == 1
  row = mode.store.connection.execute("SELECT source, closed, schema_version FROM events").fetchone()
  assert tuple(row) == ("manual_test", 1, 2)
  motion_at(mode, 1000)
  mode._schedule_motion_capture(1000)
  assert len(mode.capture_queue) == 1
