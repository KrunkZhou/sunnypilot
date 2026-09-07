import hashlib
import json
import sqlite3
import threading
import time

import pytest

from openpilot.system.sentryd.capture import CaptureResult
from openpilot.system.sentryd.door import DoorSample
from openpilot.system.sentryd.sentryd import ARM_DELAY_SECONDS, DOOR_PAUSE_SECONDS, SentryMode
from openpilot.system.sentryd.store import SentryStore
from openpilot.system.sentryd.tests.test_driver_exit import DoorSource
from openpilot.system.sentryd.tests.test_repeated_capture import RepeatCapture, finish_pair, send_sample
from openpilot.system.sentryd.tests.test_repeated_capture import mode as mode
from openpilot.system.sentryd.tests.test_revision_cap import capture_through, queue_next_pair
from openpilot.system.sentryd.tests.test_sentryd import Params, SubMaster


@pytest.fixture
def door_mode(mode):
  mode.door_source = DoorSource()
  mode.arm_started_at = 0.0
  mode.door_source.samples = [DoorSample(False, 100.0)]
  send_sample(mode, 100.0, 0.0)
  assert mode.state == "armed" and mode.door_previous == frozenset()
  return mode


def candidate(mode, hit_count=1):
  for index in range(1, hit_count + 1):
    send_sample(mode, 100.0 + index / 10, index / 10)
  assert mode.active_event_id is not None
  assert mode.active_confirmed is (hit_count >= 10)
  return mode.active_event_id


def finish_requested_pair(mode):
  mode._start_capture_if_needed()
  if mode.active_capture is not None and mode.active_capture.job is not None:
    finish_pair(mode)


def finish_door_pairs(mode):
  finish_requested_pair(mode)
  deadline = time.monotonic() + 2
  while mode.capture_queue:
    assert time.monotonic() < deadline, "door capture did not complete"
    mode.test_now[0] = max(mode.clock(), mode.capture_queue[0].not_before)
    if mode.active_capture is not None and mode.active_capture.job is None and mode.active_capture.idle_stop.is_set():
      mode.active_capture.thread.join(0.1)
      mode._finish_capture_if_ready()
    finish_requested_pair(mode)


def observe(mode, timestamp, *samples):
  mode.door_source.samples = list(samples)
  send_sample(mode, timestamp, (timestamp - 100.0))


def event_rows(mode, event_id):
  return mode.store.connection.execute(
    "SELECT revision, state, metadata_json FROM revisions WHERE event_id=? ORDER BY revision", (event_id,),
  ).fetchall()


def media_by_role(metadata):
  return {item["role"]: item for item in metadata["media"]}


class BlockingDoorCapture(RepeatCapture):
  """Keep a real daemon worker in flight until completion or its abort callback."""

  def __init__(self):
    super().__init__()
    self.started = threading.Event()
    self.release = threading.Event()
    self.aborted = threading.Event()

  def capture(self, abort_callback):
    self.sessions += 1
    self.started.set()
    while not self.release.wait(0.001):
      if abort_callback():
        self.aborted.set()
        return CaptureResult({}, {"wide": "capture_aborted", "cabin": "capture_aborted"})
    assert not abort_callback()
    return self._pair()


@pytest.mark.parametrize("door_name", ["driver", "passenger", "rear_left", "rear_right", "trunk"])
def test_each_door_without_candidate_creates_one_silent_pair_and_pause(door_mode, door_name):
  mode = door_mode
  fields = {f"{door_name}_open": True} if door_name != "driver" else {}
  observe(mode, 100.1, DoorSample(door_name == "driver", 100.1, **fields))
  assert mode.door_paused and mode.store.get_door_pause()
  assert mode.active_event_id is None and mode.state == "door_paused"
  finish_door_pairs(mode)
  rows = mode.store.connection.execute("SELECT source, schema_version, closed FROM events").fetchall()
  assert [tuple(row) for row in rows] == [("door_open", 3, 1)]
  pending = mode.store.next_pending(0)
  assert pending is not None
  assert pending.metadata["source"] == "door_open" and pending.metadata["kind"] == "door_open"
  assert pending.metadata["schema_version"] == 3
  assert set(media_by_role(pending.metadata)) == {"wide", "cabin"}
  assert mode.capture.pairs == 1


@pytest.mark.parametrize("first_state", ["queued", "capturing", "ready"])
def test_door_retains_initial_candidate_and_adds_one_new_pair(door_mode, first_state):
  mode = door_mode
  event_id = candidate(mode)
  if first_state in ("capturing", "ready"):
    mode._start_capture_if_needed()
  if first_state == "ready":
    finish_pair(mode)
  assert mode.store.next_pending(0) is None
  observe(mode, 100.2, DoorSample(False, 100.2, trunk_open=True))
  assert mode.door_paused and mode.active_event_id is None
  finish_door_pairs(mode)
  rows = event_rows(mode, event_id)
  assert [(row[0], row[1]) for row in rows] == [(1, "ready"), (2, "ready")]
  metadata = [json.loads(row[2]) for row in rows]
  assert [item["source"] for item in metadata] == ["door_open", "door_open"]
  assert [item["kind"] for item in metadata] == ["door_open", "follow_up"]
  assert all(item["schema_version"] == 3 for item in metadata)
  assert all(set(media_by_role(item)) == {"wide", "cabin"} for item in metadata)
  assert media_by_role(metadata[0])["wide"]["sha256"] != media_by_role(metadata[1])["wide"]["sha256"]
  assert mode.capture.pairs == 2  # Retain first pair; do not take it again.
  assert mode.store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_door_open_takes_priority_over_tenth_hit_and_upload_eligibility(door_mode, monkeypatch):
  mode = door_mode
  event_id = candidate(mode, 9)
  finish_requested_pair(mode)
  first = json.loads(event_rows(mode, event_id)[0][2])
  seen_upload_sources = []

  def inspect_uploads():
    pending = mode.store.next_pending(0)
    seen_upload_sources.append(pending.metadata["source"] if pending is not None else None)

  def unexpected_confirmation(_event_id):
    raise AssertionError("door-open candidate must never be confirmed as motion")

  monkeypatch.setattr(mode, "_start_upload_if_needed", inspect_uploads)
  monkeypatch.setattr(mode.store, "confirm_event", unexpected_confirmation)
  observe(mode, 101.0, DoorSample(False, 101.0, passenger_open=True))
  finish_door_pairs(mode)
  assert mode.door_paused and mode.active_event_id is None
  assert seen_upload_sources == ["door_open"]
  retained = json.loads(event_rows(mode, event_id)[0][2])
  assert retained["media"] == first["media"]
  assert retained["source"] == "door_open" and retained["schema_version"] == 3


def test_queued_open_at_event_513_wins_over_tenth_hit(door_mode, monkeypatch):
  from openpilot.system.sentryd import door
  from openpilot.system.sentryd.tests.test_door import Socket, event, frame, profile

  mode = door_mode
  event_id = candidate(mode, 9)
  finish_requested_pair(mode)
  socket = Socket()
  mode.params.values["CarParamsPersistent"] = profile()
  monkeypatch.setattr(door, "_open_can_socket", lambda: socket)
  mode.door_source = door.DriverDoorSource(mode.params)
  socket.events.extend(event(at=100.9001 + i / 10000, frames=[frame(False)]) for i in range(512))
  socket.events.append(event(at=100.96, frames=[frame(True)]))
  send_sample(mode, 101.0, 1.0)
  assert mode.door_paused and not mode.active_confirmed
  assert not socket.events and mode.door_source.queue_drained
  finish_door_pairs(mode)
  assert all(json.loads(row[2])["source"] == "door_open" for row in event_rows(mode, event_id))


def test_open_close_open_batch_preserves_pause_and_does_not_create_extra_pairs(door_mode):
  mode = door_mode
  observe(mode, 100.1, DoorSample(True, 100.1), DoorSample(False, 100.1), DoorSample(True, 100.1))
  assert mode.door_paused and mode.door_closed_at is None
  finish_door_pairs(mode)
  assert mode.capture.pairs == 1
  observe(mode, 100.2, DoorSample(True, 100.2, trunk_open=True))
  assert mode.door_closed_at is None and mode.capture.pairs == 1
  assert mode.store.connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1


def test_pause_requires_all_closed_then_full_five_minutes_and_reopen_resets(door_mode):
  mode = door_mode
  observe(mode, 100.1, DoorSample(True, 100.1, trunk_open=True))
  finish_door_pairs(mode)
  observe(mode, 100.2, DoorSample(False, 100.2, trunk_open=True))
  assert mode.door_closed_at is None
  observe(mode, 100.3, DoorSample(False, 100.3))
  assert mode.door_closed_at == 100.3
  send_sample(mode, 399.9, 0.2)
  assert mode.door_paused and mode.capture.pairs == 1
  observe(mode, 400.0, DoorSample(False, 400.0, passenger_open=True))
  assert mode.door_closed_at is None
  observe(mode, 400.1, DoorSample(False, 400.1))
  send_sample(mode, 400.1 + DOOR_PAUSE_SECONDS - 0.001, 0.3)
  assert mode.door_paused and mode.active_event_id is None
  send_sample(mode, 400.1 + DOOR_PAUSE_SECONDS, 0.4)
  assert not mode.door_paused and not mode.store.get_door_pause()
  assert mode.state == "armed" and mode.active_event_id is None
  send_sample(mode, 400.2 + DOOR_PAUSE_SECONDS, 0.5)
  assert mode.state == "confirming" and mode.active_event_id is not None
  assert mode.capture.pairs == 1


def test_restart_preserves_door_photos_and_requires_new_closed_observation(door_mode):
  mode = door_mode
  observe(mode, 100.1, DoorSample(True, 100.1))
  finish_door_pairs(mode)
  observe(mode, 100.2, DoorSample(False, 100.2))
  mode.stop_event.set()
  if mode.active_capture is not None:
    mode.active_capture.thread.join(1)
  mode.test_now[0] = 200.0
  restarted_store = SentryStore(mode.store.path)
  restarted = SentryMode(config_store=mode.config_store, store=restarted_store, params=Params(), volatile_params=Params(),
                         sm=SubMaster(), capture=RepeatCapture(), door_source=DoorSource(), clock=mode.clock)
  restarted.test_now = mode.test_now
  try:
    assert restarted.door_paused and restarted.door_closed_at is None
    send_sample(restarted, 500.0, 0.1)
    assert restarted.door_paused and restarted.door_closed_at is None
    observe(restarted, 500.1, DoorSample(False, 500.1))
    assert restarted.door_closed_at == 500.1
    send_sample(restarted, 800.099, 0.2)
    assert restarted.door_paused
    send_sample(restarted, 800.1, 0.3)
    assert not restarted.door_paused
    assert restarted_store.next_pending(0).metadata["source"] == "door_open"
    assert restarted.capture.pairs == 0
  finally:
    restarted.stop_event.set()
    if restarted.active_capture is not None:
      restarted.active_capture.thread.join(1)
    restarted_store.close()


def test_door_keeps_earlier_confirmed_motion_event_unchanged(door_mode):
  mode = door_mode
  event_id = candidate(mode, 10)
  finish_requested_pair(mode)
  original = json.loads(event_rows(mode, event_id)[0][2])
  assert original["source"] == "motion"
  observe(mode, 101.1, DoorSample(True, 101.1))
  finish_door_pairs(mode)
  assert json.loads(event_rows(mode, event_id)[0][2]) == original
  assert mode.store.revision_state(event_id, 1) == "ready"
  events = mode.store.connection.execute("SELECT source, schema_version FROM events ORDER BY rowid").fetchall()
  assert [tuple(row) for row in events] == [("motion", 2), ("door_open", 3)]
  assert mode.capture.pairs == 2


def test_door_during_final_revision_keeps_capture_and_starts_cap_rearm_only_after_durability(door_mode, monkeypatch):
  mode = door_mode
  event_id = capture_through(mode, 20)
  original_first = json.loads(event_rows(mode, event_id)[0][2])
  queue_next_pair(mode, event_id, 21)
  final_worker = mode.active_capture
  original_finish = mode.store.finish_capture

  def delayed_finalization(target_id, revision, media, omissions):
    if target_id == event_id and revision == 21:
      raise sqlite3.OperationalError("disk full")
    return original_finish(target_id, revision, media, omissions)

  monkeypatch.setattr(mode.store, "finish_capture", delayed_finalization)
  finish_pair(mode)
  original_result = final_worker.result[0]
  assert set(original_result.media) == {"wide", "cabin"}
  opened_at = mode.clock() + 0.1
  observe(mode, opened_at, DoorSample(True, opened_at))
  door_event_id = mode.capture_queue[0].event_id
  assert door_event_id != event_id and mode.door_paused
  assert mode.cap_pending_event_id == event_id and final_worker.result[0] is original_result
  assert not final_worker.idle_stop.is_set()
  assert json.loads(event_rows(mode, event_id)[0][2]) == original_first
  closed_at = mode.clock() + 0.1
  observe(mode, closed_at, DoorSample(False, closed_at))
  send_sample(mode, closed_at + DOOR_PAUSE_SECONDS, 0.1)
  assert not mode.door_paused  # The cap cannot spend its 90 seconds during this pause.
  assert mode.cap_pending_event_id == event_id and mode.active_event_id is None
  assert mode.store.revision_state(event_id, 21) == "capturing"
  assert mode.capture.pairs == 21 and final_worker.result[0] is original_result
  assert mode.store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 2

  monkeypatch.setattr(mode.store, "finish_capture", original_finish)
  mode.test_now[0] = final_worker.next_finalize_at
  finalized_at = mode.clock()
  mode._finish_capture_if_ready()
  final_metadata = json.loads(event_rows(mode, event_id)[-1][2])
  assert mode.store.revision_state(event_id, 21) == "ready"
  assert final_metadata["source"] == "motion" and set(media_by_role(final_metadata)) == {"wide", "cabin"}
  assert {role: item["sha256"] for role, item in media_by_role(final_metadata).items()} == {
    role: hashlib.sha256(media.data).hexdigest() for role, media in original_result.media.items()
  }
  assert mode.cap_pending_event_id is None and mode.cap_rearm_until == finalized_at + ARM_DELAY_SECONDS
  assert mode.arm_started_at == finalized_at and final_worker.idle_stop.is_set()
  assert mode.capture.pairs == 21  # Storage retries retain the original bytes, never recapture.
  final_worker.thread.join(1)
  assert not final_worker.thread.is_alive()
  mode._finish_capture_if_ready()
  finish_door_pairs(mode)
  assert mode.capture.pairs == 22 and mode.store.revision_state(door_event_id, 1) == "ready"
  assert json.loads(event_rows(mode, door_event_id)[0][2])["source"] == "door_open"
  send_sample(mode, finalized_at + ARM_DELAY_SECONDS - 0.001, 0.2)
  assert mode.state == "arming" and mode.active_event_id is None
  send_sample(mode, finalized_at + ARM_DELAY_SECONDS, 0.3)
  assert mode.state == "armed" and mode.active_event_id is None
  send_sample(mode, finalized_at + ARM_DELAY_SECONDS + 0.1, 0.4)
  assert mode.state == "confirming" and mode.active_event_id not in (None, event_id, door_event_id)


def test_normal_can_sleep_after_all_closed_retains_five_minute_countdown(door_mode):
  mode = door_mode
  observe(mode, 100.1, DoorSample(True, 100.1))
  finish_door_pairs(mode)
  observe(mode, 100.2, DoorSample(False, 100.2))
  mode.door_source.error = "No fresh driver-door CAN samples. Check vehicle CAN support."
  for timestamp in (102.0, 200.0, 400.199):
    send_sample(mode, timestamp, 0.0)
    assert mode.door_paused and mode.door_closed_at == 100.2
    assert mode.door_error == mode.door_source.error
  send_sample(mode, 400.2, 0.0)
  assert not mode.door_paused and not mode.store.get_door_pause()
  assert mode.state == "armed" and mode.capture.pairs == 1


def test_sensor_loss_while_paused_does_not_abort_door_capture(door_mode):
  mode = door_mode
  capture = mode.capture = BlockingDoorCapture()
  try:
    observe(mode, 100.1, DoorSample(True, 100.1))
    assert capture.started.wait(1)
    event_id = mode.active_capture.job.event_id
    mode.sm.valid["accelerometer"] = False
    send_sample(mode, 101.2, 0.0)
    assert mode.door_paused and mode.state == "door_paused"
    assert mode.active_capture.thread.is_alive() and not capture.aborted.is_set()
    assert not mode.capture_abort_event.is_set()
    capture.release.set()
    finish_pair(mode)
    assert mode.store.revision_state(event_id, 1) == "ready"
    metadata = json.loads(event_rows(mode, event_id)[0][2])
    assert metadata["source"] == "door_open" and set(media_by_role(metadata)) == {"wide", "cabin"}
    assert capture.pairs == 1
  finally:
    capture.release.set()


def test_true_ignition_aborts_door_capture_but_preserves_durable_door_event(door_mode):
  mode = door_mode
  capture = mode.capture = BlockingDoorCapture()
  try:
    observe(mode, 100.1, DoorSample(True, 100.1))
    assert capture.started.wait(1)
    worker = mode.active_capture
    event_id = worker.job.event_id
    mode.sm.panda_states[0].ignitionCan = True
    send_sample(mode, 100.2, 0.0)
    assert capture.aborted.wait(1)
    worker.thread.join(1)
    assert not worker.thread.is_alive()
    mode._finish_capture_if_ready()
    assert mode.state == "disabled" and not mode.door_paused and not mode.store.get_door_pause()
    rows = event_rows(mode, event_id)
    assert [(row[0], row[1]) for row in rows] == [(1, "ready")]
    metadata = json.loads(rows[0][2])
    assert metadata["source"] == "door_open" and metadata["kind"] == "door_open"
    assert metadata["capture_status"] == "failed" and metadata["media"] == []
    assert {item["role"]: item["reason"] for item in metadata["omitted_media"]} == {"wide": "ignition_on", "cabin": "ignition_on"}
    assert capture.pairs == 0 and not mode.capture_queue
  finally:
    capture.release.set()


def test_first_observation_already_open_does_not_fabricate_an_opening(mode):
  mode.door_source = DoorSource()
  mode.arm_started_at = 0.0
  for timestamp in (100.0, 100.1):
    mode.door_source.samples = [DoorSample(True, timestamp)]
    send_sample(mode, timestamp, 0.0)
    assert not mode.door_paused and mode.active_event_id is None
    assert mode.store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
  mode.door_source.samples = [DoorSample(False, 100.2)]
  send_sample(mode, 100.2, 0.0)
  observe(mode, 100.3, DoorSample(True, 100.3))
  finish_door_pairs(mode)
  assert mode.door_paused and mode.capture.pairs == 1


@pytest.mark.parametrize("opening_at", [99.9, 100.15])
def test_pre_observation_or_stale_can_opening_does_not_fabricate_an_event(door_mode, monkeypatch, opening_at):
  from openpilot.system.sentryd import door
  from openpilot.system.sentryd.tests.test_door import Socket, event, frame, profile

  mode = door_mode
  socket = Socket()
  mode.params.values["CarParamsPersistent"] = profile()
  monkeypatch.setattr(door, "_open_can_socket", lambda: socket)
  mode.door_source = door.DriverDoorSource(mode.params)
  socket.events.append(event(at=100.1, frames=[frame(False)]))
  send_sample(mode, 100.1, 0.0)
  assert mode.door_previous == frozenset()
  socket.events.append(event(at=opening_at, frames=[frame(True)]))
  send_sample(mode, 101.2, 0.0)
  assert not mode.door_paused and mode.active_event_id is None and not mode.capture_queue
  assert mode.door_previous == frozenset() and mode.capture.pairs == 0
  assert mode.store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
  socket.events.append(event(at=101.3, frames=[frame(True)]))
  send_sample(mode, 101.3, 0.0)
  finish_door_pairs(mode)
  assert mode.door_paused and mode.capture.pairs == 1
