import sqlite3

import pytest

from openpilot.system.sentryd.capture import CaptureResult
from openpilot.system.sentryd.sentryd import ARM_DELAY_SECONDS, CaptureJob
from openpilot.system.sentryd.tests.test_repeated_capture import begin_motion, finish_pair, motion_at, send_sample
from openpilot.system.sentryd.tests.test_repeated_capture import mode as mode


def capture_through(mode, last_revision):
  begin_motion(mode)
  event_id = mode.active_event_id
  for revision in range(2, last_revision + 1):
    queue_next_pair(mode, event_id, revision)
    finish_pair(mode)
  return event_id


def queue_next_pair(mode, event_id, revision):
  motion_at(mode, mode.next_capture_at)
  mode._schedule_motion_capture(mode.clock())
  assert mode.capture_queue == [CaptureJob(event_id, revision)]
  mode._start_capture_if_needed()


@pytest.mark.parametrize("last_result", ["complete", "alarm", "failed"])
def test_first_capture_plus_twenty_revisions_closes_episode_and_stops_warm_camera(mode, monkeypatch, last_result):
  event_id = capture_through(mode, 20)
  if last_result == "alarm":
    mode._process_detection("alarm", mode.clock())
  queue_next_pair(mode, event_id, 21)
  final_worker = mode.active_capture
  if last_result == "failed":
    # Wait for the worker result, but replace only its camera outcome before
    # finalization. A failed pair still creates a durable capture revision.
    original = mode.store.finish_capture

    def failed_pair(event_id, revision, _media, _omissions):
      failure = CaptureResult({}, {"wide": "camera_unavailable", "cabin": "capture_timeout"})
      original(event_id, revision, failure.media, failure.omissions)

    monkeypatch.setattr(mode.store, "finish_capture", failed_pair)
  finish_pair(mode)
  rows = mode.store.connection.execute(
    "SELECT revision, kind, state, capture_status FROM revisions WHERE event_id=? ORDER BY revision", (event_id,),
  ).fetchall()
  assert [row[0] for row in rows] == list(range(1, 22))
  assert all(row[2] == "ready" for row in rows)
  assert rows[-1][1] == ("alarm" if last_result == "alarm" else "follow_up")
  assert rows[-1][3] == ("failed" if last_result == "failed" else "complete")
  assert mode.store.connection.execute("SELECT closed FROM events WHERE event_id=?", (event_id,)).fetchone()[0] == 1
  assert mode.active_event_id is None and mode.active_episode_started_at is None
  assert mode.state == "arming" and mode.arm_started_at == mode.clock()
  assert not mode.detector.episode_active and mode.detector.previous_sample_at is None
  assert not mode.pending_alarm and not mode.capture_queue
  assert final_worker.idle_stop.is_set()
  final_worker.thread.join(1)
  assert not final_worker.thread.is_alive()
  mode._finish_capture_if_ready()
  assert mode.active_capture is None
  motion_at(mode, mode.clock() + 1)
  mode._schedule_motion_capture(mode.clock())
  assert not mode.capture_queue and mode.capture.pairs == 21


def test_cap_rearms_for_ninety_seconds_while_uploads_continue_then_starts_a_new_event(mode, monkeypatch):
  event_id = capture_through(mode, 21)
  rearm_at = mode.arm_started_at
  upload_checks = []
  monkeypatch.setattr(mode, "_start_upload_if_needed", lambda: upload_checks.append(mode.clock()))
  for index, elapsed in enumerate((0.1, 1.0, 45.0, ARM_DELAY_SECONDS - 0.001)):
    send_sample(mode, rearm_at + elapsed, index / 10)
    assert mode.state == "arming" and mode.active_event_id is None
    assert not mode.capture_queue and mode.capture.pairs == 21
  assert len(upload_checks) == 4
  assert mode.store.stats().pending == 21
  send_sample(mode, rearm_at + ARM_DELAY_SECONDS, 1.0)
  assert mode.state == "armed" and mode.active_event_id is None  # A fresh baseline is not motion.
  send_sample(mode, rearm_at + ARM_DELAY_SECONDS + 0.1, 1.1)
  assert mode.active_event_id is not None and mode.active_event_id != event_id
  assert mode.capture_queue == [CaptureJob(mode.active_event_id, 1)]
  assert mode.store.revision_state(mode.active_event_id, 1) == "capturing"


def test_last_revision_storage_failure_retains_episode_until_durable_then_starts_full_rearm(mode, monkeypatch):
  mode.arm_started_at = 0.0
  event_id = capture_through(mode, 20)
  mode.detector.episode_active = True
  queue_next_pair(mode, event_id, 21)
  final_capture_at = mode.clock()
  original = mode.store.finish_capture

  def disk_full(*_args):
    raise sqlite3.OperationalError("disk full")

  monkeypatch.setattr(mode.store, "finish_capture", disk_full)
  finish_pair(mode)
  assert mode.active_capture.job == CaptureJob(event_id, 21)
  assert mode.active_event_id == event_id and mode.arm_started_at == 0.0
  # Waiting longer than episode quiet-close cannot bypass the revision cap or
  # spend the next arming delay while the last result is not yet durable.
  send_sample(mode, final_capture_at + 61.0, 0.1)
  assert mode.active_event_id == event_id and mode.arm_started_at == 0.0
  assert mode.store.revision_state(event_id, 21) == "capturing"
  assert mode.store.connection.execute("SELECT closed FROM events WHERE event_id=?", (event_id,)).fetchone()[0] == 0
  assert not mode.capture_queue and mode.capture.pairs == 21
  assert mode.store.connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 21
  monkeypatch.setattr(mode.store, "finish_capture", original)
  mode.test_now[0] = mode.active_capture.next_finalize_at
  mode._finish_capture_if_ready()
  assert mode.store.revision_state(event_id, 21) == "ready"
  assert mode.active_event_id is None and mode.state == "arming"
  assert mode.arm_started_at == mode.clock() and mode.arm_started_at > final_capture_at + 61.0
  assert mode.capture.pairs == 21  # Persistence retries never recapture.


def test_cap_does_not_allow_an_alarm_to_create_revision_twenty_two(mode):
  event_id = capture_through(mode, 20)
  queue_next_pair(mode, event_id, 21)
  mode._process_detection("alarm", mode.clock())
  finish_pair(mode)
  assert not mode.pending_alarm
  mode._process_detection("alarm", mode.clock())
  motion_at(mode, mode.clock() + 1)
  mode._schedule_motion_capture(mode.clock())
  assert mode.active_event_id is None and not mode.capture_queue
  assert mode.store.connection.execute("SELECT max(revision) FROM revisions").fetchone()[0] == 21


def test_ignition_during_rearming_resets_the_full_offroad_delay(mode):
  capture_through(mode, 21)
  original_rearm_at = mode.arm_started_at
  mode.sm.panda_states[0].ignitionCan = True
  send_sample(mode, original_rearm_at + 10.0, 0.1)
  assert mode.state == "disabled" and mode.arm_started_at is None
  assert mode.active_event_id is None and not mode.capture_queue
  mode.sm.panda_states[0].ignitionCan = False
  parked_at = original_rearm_at + 20.0
  send_sample(mode, parked_at, 0.2)
  assert mode.state == "arming" and mode.arm_started_at == parked_at
  send_sample(mode, parked_at + ARM_DELAY_SECONDS - 0.001, 0.3)
  assert mode.state == "arming" and mode.active_event_id is None
  assert mode.capture.pairs == 21
  send_sample(mode, parked_at + ARM_DELAY_SECONDS, 0.4)
  assert mode.state == "armed" and mode.active_event_id is None
  send_sample(mode, parked_at + ARM_DELAY_SECONDS + 0.1, 0.5)
  assert mode.active_event_id is not None
  assert mode.capture_queue == [CaptureJob(mode.active_event_id, 1)]


def test_old_final_revision_cannot_close_or_rearm_a_newer_episode(mode):
  old_event_id = capture_through(mode, 20)
  queue_next_pair(mode, old_event_id, 21)
  old_worker = mode.active_capture
  mode._close_active_episode()
  mode.detector.reset()
  mode.detector.episode_active = True
  mode.detector.first_motion_at = mode.clock()
  mode.detector.last_motion_at = mode.clock()
  mode.arm_started_at = 10.0
  mode._process_detection("motion", mode.clock())
  new_event_id = mode.active_event_id
  assert new_event_id != old_event_id
  finish_pair(mode)
  assert mode.store.revision_state(old_event_id, 21) == "ready"
  assert old_worker.idle_stop.is_set()
  assert mode.active_event_id == new_event_id and mode.arm_started_at == 10.0
  assert mode.detector.episode_active and mode.state == "motion"
  assert mode.capture_queue == [CaptureJob(new_event_id, 1)]
  assert mode.store.connection.execute("SELECT closed FROM events WHERE event_id=?", (new_event_id,)).fetchone()[0] == 0
