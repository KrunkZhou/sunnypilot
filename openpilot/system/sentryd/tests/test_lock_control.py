import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace

import pytest

from openpilot.system.sentryd.config import SentryConfigError
from openpilot.system.sentryd.door import DoorSample
from openpilot.system.sentryd.flash import LightSample
from openpilot.system.sentryd.sentryd import ARM_DELAY_SECONDS, SentryMode, UNLOCK_PAUSE_SECONDS
from openpilot.system.sentryd.store import SentryStore
from openpilot.system.sentryd.tests.test_door_events import candidate, event_rows, finish_door_pairs, finish_requested_pair, observe
from openpilot.system.sentryd.tests.test_driver_exit import DoorSource
from openpilot.system.sentryd.tests.test_repeated_capture import RepeatCapture, finish_pair, send_sample
from openpilot.system.sentryd.tests.test_repeated_capture import mode as mode
from openpilot.system.sentryd.tests.test_revision_cap import capture_through, queue_next_pair
from openpilot.system.sentryd.tests.test_sentryd import Params, SubMaster


class LightDoorSource(DoorSource):
  def __init__(self):
    super().__init__()
    self.pending_lights = []
    self.light_samples = []
    self.light_error = None
    self.light_generation = 0

  def poll(self, now, *, after):
    self.light_samples, self.pending_lights = self.pending_lights, []
    return super().poll(now, after=after)


@pytest.fixture
def lock_mode(mode):
  mode.config = replace(mode.config, infer_lock_from_flashes=True)
  mode.config_store.config = mode.config
  mode.door_source = LightDoorSource()
  mode.arm_started_at = 0.0
  mode.door_source.samples = [DoorSample(False, 100.0)]
  mode.door_source.pending_lights = [LightSample(False, False, False, True, 100.0)]
  send_sample(mode, 100.0, 0.0)
  assert mode.state == "armed"
  return mode


def light_batch(mode, start, end, pulses=(), *, doors=(), hazards=False):
  samples = []
  for index in range(round((end - start) / 0.02) + 1):
    at = round(start + index * 0.02, 9)
    offset = round(at - start, 8)
    on = any(begin <= offset < finish for begin, finish in pulses)
    samples.append(LightSample(on, on, hazards, True, at))
  mode.door_source.pending_lights = samples
  mode.door_source.samples = list(doors)
  send_sample(mode, end, 0.0)


def flashes(mode, count, *, doors_open=False):
  # An observed quiet lead also recovers the detector if CAN slept before this
  # sequence. Deliver actual ordered 50-Hz samples through the daemon poll path.
  start = round(mode.clock() + 0.02, 9)
  pulses = [(2.3, 2.7)] + ([(2.9, 3.3)] if count == 2 else [])
  end = round(start + pulses[-1][1] + 2.0, 9)
  doors = [DoorSample(doors_open, end)]
  light_batch(mode, start, end, pulses, doors=doors)
  assert mode.last_lock_inference == ("unlocked" if count == 2 else "locked")
  return end


@contextmanager
def restart(mode, at):
  mode.stop_event.set()
  if mode.active_capture is not None:
    mode.active_capture.thread.join(1)
    assert not mode.active_capture.thread.is_alive()
  mode.test_now[0] = at
  store = SentryStore(mode.store.path)
  restarted = SentryMode(config_store=mode.config_store, store=store, params=Params(), volatile_params=Params(),
                         sm=SubMaster(), capture=RepeatCapture(), door_source=LightDoorSource(), clock=mode.clock)
  restarted.test_now = mode.test_now
  try:
    yield restarted
  finally:
    restarted.stop_event.set()
    if restarted.active_capture is not None:
      restarted.active_capture.thread.join(1)
      assert not restarted.active_capture.thread.is_alive()
    store.close()


def test_double_flash_discards_candidate_and_suppresses_all_automatic_sources(lock_mode):
  mode = lock_mode
  event_id = candidate(mode, 7)
  finish_requested_pair(mode)
  flashes(mode, 2, doors_open=True)
  assert mode.lock_control == "unlock_pause" and mode.state == "unlock_paused"
  assert mode.store.get_lock_control() == ("unlock_pause", "unlocked")
  assert mode.active_event_id is None and not mode.door_paused
  assert mode.store.revision_state(event_id, 1) in (None, "discarding")
  assert mode.store.next_pending(10 ** 12) is None
  before = mode.capture.pairs
  for index in range(10):
    at = mode.clock() + 0.1
    observe(mode, at, DoorSample(index % 2 == 0, at))
    mode._process_detection("motion", at)
    mode._process_detection("warning", at)
    mode._process_detection("alarm", at)
    mode._schedule_motion_capture(at)
  assert mode.capture.pairs == before and not mode.capture_queue
  assert mode.active_event_id is None and not mode.store.get_door_pause()
  assert mode.store.connection.execute("SELECT COUNT(*) FROM events WHERE source='door_open'").fetchone()[0] == 0


@pytest.mark.parametrize("follow_up_state", ("queued", "in_flight"))
def test_unlock_keeps_confirmed_upload_claim_and_requested_capture(lock_mode, follow_up_state):
  mode = lock_mode
  event_id = candidate(mode, 10)
  finish_requested_pair(mode)
  first = event_rows(mode, event_id)[0][2]
  claimed = mode.store.claim_pending(0)
  assert claimed.event_id == event_id
  queue_next_pair(mode, event_id, 2)
  if follow_up_state == "queued":
    # The request is already durable even if capture dispatch has not occurred.
    finish_pair(mode)
    mode.test_now[0] = mode.next_capture_at
    mode.detector.previous_sample_at = mode.detector.last_motion_at = mode.clock()
    mode._schedule_motion_capture(mode.clock())
    assert mode.capture_queue
  flashes(mode, 2)
  finish_door_pairs(mode)
  rows = event_rows(mode, event_id)
  assert rows[0][1] == "uploading" and rows[0][2] == first
  assert all(row[1] == "ready" for row in rows[1:])
  assert all(json.loads(row[2])["source"] == "motion" for row in rows)
  assert mode.active_event_id is None and mode.lock_control == "unlock_pause"
  assert len(rows) == (3 if follow_up_state == "queued" else 2)
  assert mode.store.connection.execute("SELECT attempts FROM revisions WHERE event_id=? AND revision=1", (event_id,)).fetchone()[0] == 1


def test_manual_capture_and_retry_command_remain_available_during_unlock_pause(lock_mode, monkeypatch):
  mode = lock_mode
  flashes(mode, 2)
  uploads = []
  monkeypatch.setattr(mode, "_start_upload_if_needed", lambda: uploads.append(mode.clock()))
  mode.volatile_params.values["SentryRuntimeCommand"] = {"command": "manual_test"}
  send_sample(mode, mode.clock() + 0.1, 0.0)
  finish_door_pairs(mode)
  pending = mode.store.next_pending(0)
  assert pending is not None and pending.metadata["source"] == "manual_test"
  mode.store.mark_terminal(pending.event_id, 1, error="rejected", http_status=422)
  mode.volatile_params.values["SentryRuntimeCommand"] = {"command": "retry_uploads"}
  send_sample(mode, mode.clock() + 0.1, 0.0)
  assert mode.store.revision_state(pending.event_id, 1) == "ready"
  assert mode.store.get_lock_control() == ("unlock_pause", "unlocked")
  assert len(uploads) == 2


def test_unlock_expiry_requires_all_closed_then_full_ninety_seconds(lock_mode):
  mode = lock_mode
  recognized = flashes(mode, 2, doors_open=True)
  assert UNLOCK_PAUSE_SECONDS == 60 * 60
  expiry = recognized + UNLOCK_PAUSE_SECONDS
  send_sample(mode, expiry - 0.001, 0.1)
  assert mode.state == "unlock_paused" and mode.lock_arm_started_at is None
  send_sample(mode, expiry, 0.2)
  assert mode.lock_control == "rearm" and mode.state == "lock_waiting_for_doors"
  assert mode.store.get_lock_control() == ("rearm", "unlocked")
  observe(mode, expiry + 0.1, DoorSample(False, expiry + 0.1, trunk_open=True))
  assert mode.lock_arm_started_at is None
  closed = expiry + 0.2
  observe(mode, closed, DoorSample(False, closed))
  assert mode.lock_arm_started_at == closed and mode.state == "arming"
  send_sample(mode, closed + ARM_DELAY_SECONDS - 0.001, 0.3)
  assert mode.lock_control == "rearm"
  send_sample(mode, closed + ARM_DELAY_SECONDS, 0.4)
  assert mode.lock_control == "none" and mode.state == "armed"
  assert mode.store.get_lock_control() == ("none", None)
  send_sample(mode, closed + ARM_DELAY_SECONDS + 0.1, 0.5)
  assert mode.active_event_id is not None and mode.state == "confirming"


@pytest.mark.parametrize("previous_gate", ("unlock_pause", "door_pause", "driver_exit"))
def test_single_flash_overrides_other_parking_waits_with_closed_door_arming(lock_mode, previous_gate):
  mode = lock_mode
  if previous_gate == "unlock_pause":
    flashes(mode, 2)
  elif previous_gate == "door_pause":
    observe(mode, 100.1, DoorSample(True, 100.1))
    finish_door_pairs(mode)
    assert mode.door_paused
  else:
    mode.config_store.config = replace(mode.config, wait_for_driver_exit=True)
    mode.last_config_refresh = float("-inf")
    send_sample(mode, 100.1, 0.0)
    assert not mode.driver_exit_completed
  recognized = flashes(mode, 1)
  assert mode.lock_control == "rearm" and mode.lock_arm_started_at == recognized
  assert mode.driver_exit_completed and not mode.door_paused
  assert mode.store.get_lock_control() == ("rearm", "locked")
  assert not mode.store.get_door_pause()
  # The sensor fixture quantizes the clock to nanoseconds; cross any smaller
  # floating-point addition residue without changing production arming rules.
  send_sample(mode, recognized + ARM_DELAY_SECONDS + 1e-9, 0.0)
  assert mode.state == "armed" and mode.lock_control == "none"


def test_door_reopening_resets_lock_arming_but_normal_can_sleep_does_not(lock_mode):
  mode = lock_mode
  recognized = flashes(mode, 1)
  opened = recognized + 45.0
  observe(mode, opened, DoorSample(False, opened, passenger_open=True))
  assert mode.lock_arm_started_at is None and mode.state == "lock_waiting_for_doors"
  closed = opened + 0.1
  observe(mode, closed, DoorSample(False, closed))
  mode.door_source.error = "No fresh door CAN samples"
  mode.door_source.light_error = "No fresh indicator CAN samples"
  send_sample(mode, closed + ARM_DELAY_SECONDS - 0.001, 0.1)
  assert mode.lock_arm_started_at == closed and mode.lock_control == "rearm"
  send_sample(mode, closed + ARM_DELAY_SECONDS, 0.2)
  assert mode.state == "armed" and mode.lock_control == "none"


def test_new_door_subscription_requires_new_closed_observation(lock_mode):
  mode = lock_mode
  recognized = flashes(mode, 1)
  mode.door_source.generation += 1
  send_sample(mode, recognized + 45, 0.0)
  assert mode.lock_arm_started_at is None and mode.door_previous is None
  send_sample(mode, recognized + ARM_DELAY_SECONDS + 100, 0.0)
  assert mode.state == "lock_waiting_for_doors" and mode.lock_control == "rearm"


@pytest.mark.parametrize("control", ("unlock_pause", "rearm"))
def test_restart_restores_full_unlock_pause_or_fresh_closed_door_rearm(lock_mode, control):
  mode = lock_mode
  flashes(mode, 2 if control == "unlock_pause" else 1)
  restart_at = mode.clock() + 30
  with restart(mode, restart_at) as restarted:
    assert restarted.lock_control == control and restarted.door_previous is None
    if control == "unlock_pause":
      assert restarted.unlock_pause_until == restart_at + UNLOCK_PAUSE_SECONDS
      send_sample(restarted, restarted.unlock_pause_until - 0.001, 0.0)
      assert restarted.lock_control == "unlock_pause"
      send_sample(restarted, restarted.unlock_pause_until, 0.0)
    else:
      send_sample(restarted, restart_at + ARM_DELAY_SECONDS + 1, 0.0)
    assert restarted.state == "lock_waiting_for_doors" and restarted.lock_arm_started_at is None
    at = restarted.clock() + 0.1
    observe(restarted, at, DoorSample(False, at))
    assert restarted.lock_arm_started_at == at
    assert restarted.store.get_lock_control()[0] == "rearm"


@pytest.mark.parametrize("control", ("unlock_pause", "rearm"))
def test_unknown_ignition_keeps_marker_but_positive_ignition_clears_it(lock_mode, control):
  mode = lock_mode
  flashes(mode, 2 if control == "unlock_pause" else 1)
  saved = mode.store.get_lock_control()
  mode.sm.panda_checks = False
  send_sample(mode, mode.clock() + 0.1, 0.1)
  assert mode.store.get_lock_control() == saved and mode.lock_control == control
  assert mode.state == "disabled" and mode.ignition_error is not None
  mode.sm.panda_checks = True
  mode.sm.panda_states[0].ignitionCan = True
  send_sample(mode, mode.clock() + 0.1, 0.2)
  assert mode.lock_control == "none" and mode.store.get_lock_control() == ("none", None)


def test_turning_off_inference_clears_only_lock_gate_and_restores_driver_exit(lock_mode):
  mode = lock_mode
  flashes(mode, 2)
  mode.config_store.config = replace(mode.config, infer_lock_from_flashes=False, wait_for_driver_exit=True)
  mode.last_config_refresh = float("-inf")
  at = mode.clock() + 0.1
  observe(mode, at, DoorSample(False, at))
  assert mode.store.get_lock_control() == ("none", None) and mode.lock_control == "none"
  assert mode.state == "waiting_for_door_open" and not mode.driver_exit_completed
  assert mode.arm_started_at is None


@pytest.mark.parametrize("control", ("unlock_pause", "rearm"))
def test_config_read_failure_preserves_persisted_gate_until_authoritative_reload(lock_mode, monkeypatch, control):
  mode = lock_mode
  flashes(mode, 2 if control == "unlock_pause" else 1)
  saved = mode.store.get_lock_control()
  expiry = mode.unlock_pause_until
  original = mode.config_store.load

  def broken_config():
    raise SentryConfigError("Sentry configuration is corrupt or unreadable")

  monkeypatch.setattr(mode.config_store, "load", broken_config)
  mode.last_config_refresh = float("-inf")
  send_sample(mode, mode.clock() + 0.1, 0.1)
  assert mode.state == "configuration_error"
  assert mode.lock_control == control and mode.store.get_lock_control() == saved
  assert mode.unlock_pause_until == expiry
  monkeypatch.setattr(mode.config_store, "load", original)
  mode.last_config_refresh = float("-inf")
  at = mode.clock() + 0.1
  observe(mode, at, DoorSample(False, at))
  assert mode.config_error is None and mode.store.get_lock_control() == saved
  assert mode.unlock_pause_until == expiry


def test_unfinished_flash_burst_defers_door_capture_and_confirmation_then_unlock_discards_both(lock_mode):
  mode = lock_mode
  for index in range(1, 10):
    at = 100 + index / 10
    mode.door_source.pending_lights = [LightSample(False, False, False, True, at)]
    send_sample(mode, at, index / 10)
  event_id = mode.active_event_id
  assert event_id is not None and mode.detector.trigger_count == 9
  finish_requested_pair(mode)
  start = round(mode.clock() + 0.02, 9)
  end = round(start + 0.4, 9)
  light_batch(mode, start, end, [(0.02, 0.22)], doors=[DoorSample(True, end)])
  assert mode.flash_detector.pending and mode.deferred_door_event is not None
  assert mode.active_event_id == event_id and not mode.active_confirmed
  mode._process_detection("warning", mode.clock())
  assert not mode.active_confirmed and mode.store.next_pending(0) is None
  assert not mode.door_paused and mode.pending_door_event is None
  start = round(mode.clock() + 0.02, 9)
  light_batch(mode, start, round(start + 2.42, 9), [(0.02, 0.42)])
  assert mode.lock_control == "unlock_pause"
  assert mode.active_event_id is None and mode.deferred_door_event is None
  assert mode.store.next_pending(0) is None
  assert mode.store.connection.execute("SELECT COUNT(*) FROM events WHERE source='door_open'").fetchone()[0] == 0


def test_hazard_rejection_releases_deferred_door_before_tenth_hit_confirmation(lock_mode, monkeypatch):
  mode = lock_mode
  for index in range(1, 10):
    at = 100 + index / 10
    mode.door_source.pending_lights = [LightSample(False, False, False, True, at)]
    send_sample(mode, at, index / 10)
  event_id = mode.active_event_id
  finish_requested_pair(mode)
  start = round(mode.clock() + 0.02, 9)
  end = round(start + 0.4, 9)
  light_batch(mode, start, end, [(0.02, 0.22)], doors=[DoorSample(True, end)])
  assert mode.deferred_door_event is not None and not mode.door_paused

  def unexpected_confirmation(_event_id):
    raise AssertionError("deferred door must take priority over motion confirmation")

  monkeypatch.setattr(mode.store, "confirm_event", unexpected_confirmation)
  at = round(mode.clock() + 0.1, 9)
  mode.door_source.pending_lights = [LightSample(False, False, True, True, at)]
  send_sample(mode, at, 1.0)
  finish_door_pairs(mode)
  assert mode.last_lock_inference is None and mode.store.get_lock_control() == ("none", None)
  assert mode.door_paused and mode.deferred_door_event is None
  assert all(json.loads(row[2])["source"] == "door_open" for row in event_rows(mode, event_id))


@pytest.mark.parametrize("acceleration_status", ("fresh", "stale", "invalid"))
def test_pending_flash_classification_keeps_valid_candidate_but_never_skips_acceleration_validation(lock_mode, acceleration_status):
  mode = lock_mode
  for index in range(1, 4):
    at = 100 + index / 10
    mode.door_source.pending_lights = [LightSample(False, False, False, True, at)]
    send_sample(mode, at, index / 10)
  event_id = mode.active_event_id
  finish_requested_pair(mode)
  start = round(mode.clock() + 0.02, 9)
  end = round(start + 0.4, 9)
  light_batch(mode, start, end, [(0.02, 0.22)])
  assert mode.flash_detector.pending and mode.active_event_id == event_id
  now = round(mode.clock() + (1.02 if acceleration_status == "stale" else 0.1), 9)
  mode.door_source.pending_lights = [
    LightSample(False, False, False, True, round(mode.clock() + index * 0.02, 9))
    for index in range(1, round((now - mode.clock()) / 0.02) + 1)
  ]
  if acceleration_status == "stale":
    mode.test_now[0] = now
    mode.sm.updated["accelerometer"] = False
    mode.update()
  else:
    mode.sm.valid["accelerometer"] = acceleration_status != "invalid"
    send_sample(mode, now, 0.0)
  assert mode.flash_detector.pending  # These are not classifier-expiry tests.
  assert mode.store.next_pending(0) is None
  if acceleration_status == "fresh":
    assert mode.active_event_id == event_id and not mode.discard_pending
    assert mode.store.revision_state(event_id, 1) == "provisional_ready"
  else:
    assert mode.active_event_id is None and mode.state == "sensor_unavailable"
    assert mode.store.revision_state(event_id, 1) in (None, "discarding")


@pytest.mark.parametrize("tail", ("hazards", "third_pulse"))
def test_real_can_event_513_invalidates_flash_before_inference_or_motion_confirmation(lock_mode, monkeypatch, tail):
  from openpilot.system.sentryd import door
  from openpilot.system.sentryd.tests.test_door import Socket, event, frame, light_frame, profile

  mode = lock_mode
  socket = Socket()
  mode.params.values["CarParamsPersistent"] = profile()
  monkeypatch.setattr(door, "_open_can_socket", lambda: socket)
  mode.door_source = door.DriverDoorSource(mode.params)

  def pump(at, *, on=False, acceleration=0.0):
    socket.events.append(event(at=at, frames=[light_frame(left=on, right=on, fast=True), frame(False)]))
    send_sample(mode, at, acceleration)

  # Source/profile changes require observed quiet before classifying. Use the
  # real decoder and fresh 50-Hz CAN/accelerometer publications throughout.
  for index in range(1, 112):
    pump(round(100 + index * 0.02, 9))
  base = mode.clock()
  for index in range(1, 10):
    pump(round(base + index * 0.1, 9), acceleration=index / 10)
  event_id = mode.active_event_id
  assert event_id is not None and mode.detector.trigger_count == 9
  finish_requested_pair(mode)
  start = mode.clock()
  second_off = round(start + 1.1, 9)
  deadline = round(second_off + (2.0 if tail == "hazards" else 1.0), 9)
  for index in range(1, round((deadline - 0.06 - start) / 0.02) + 1):
    offset = round(index * 0.02, 8)
    on = 0.1 <= offset < 0.5 or 0.7 <= offset < 1.1
    pump(round(start + offset, 9), on=on, acceleration=0.9)
  assert mode.flash_detector.pending and mode.flash_detector.pulse_count == 2
  first = round(deadline - 0.05, 9)
  for index in range(door.MAX_EVENTS_PER_POLL):
    at = round(first + index / 10000, 9)
    socket.events.append(event(at=at, frames=[light_frame(fast=True), frame(index == 0)]))
  tail_at = round(deadline + 0.01, 9)
  socket.events.append(event(at=tail_at, frames=[light_frame(
    left=tail == "third_pulse", right=tail == "third_pulse", hazards=tail == "hazards", fast=True), frame(True)]))

  def unexpected_confirmation(_event_id):
    raise AssertionError("queued invalidation and door must be drained before confirmation")

  monkeypatch.setattr(mode.store, "confirm_event", unexpected_confirmation)
  send_sample(mode, round(deadline + 0.02, 9), 1.0)
  assert not socket.events and mode.door_source.queue_drained
  assert mode.last_lock_inference is None and mode.store.get_lock_control() == ("none", None)
  assert mode.door_paused and mode.active_event_id is None
  finish_door_pairs(mode)
  assert all(json.loads(row[2])["source"] == "door_open" for row in event_rows(mode, event_id))


def test_unlock_marker_is_committed_before_candidate_discard(lock_mode, monkeypatch):
  mode = lock_mode
  event_id = candidate(mode)
  finish_requested_pair(mode)
  original = mode.store.discard_event

  def check_marker(target_id):
    assert target_id == event_id
    assert mode.store.get_lock_control() == ("unlock_pause", "unlocked")
    return original(target_id)

  monkeypatch.setattr(mode.store, "discard_event", check_marker)
  flashes(mode, 2)
  assert mode.active_event_id is None


def test_failed_unlock_write_holds_automatic_work_and_retries_without_restarting_timer(lock_mode, monkeypatch):
  mode = lock_mode
  event_id = candidate(mode)
  finish_requested_pair(mode)
  original = mode.store.set_lock_control

  def disk_full(*_args, **_kwargs):
    raise sqlite3.OperationalError("disk full")

  monkeypatch.setattr(mode.store, "set_lock_control", disk_full)
  recognized = flashes(mode, 2, doors_open=True)
  assert mode.pending_lock_control is not None and mode.lock_control == "unlock_pause"
  assert mode.store.get_lock_control() == ("none", None)
  assert mode.store.next_pending(10 ** 12) is None
  send_sample(mode, recognized + 10, 1.0)
  assert mode.unlock_pause_until == recognized + UNLOCK_PAUSE_SECONDS
  assert mode.pending_lock_control is not None and not mode.door_paused
  monkeypatch.setattr(mode.store, "set_lock_control", original)
  send_sample(mode, recognized + 11, 2.0)
  assert mode.pending_lock_control is None
  assert mode.unlock_pause_until == recognized + UNLOCK_PAUSE_SECONDS
  assert mode.store.get_lock_control() == ("unlock_pause", "unlocked")
  assert mode.store.revision_state(event_id, 1) in (None, "discarding")
  assert mode.active_event_id is None


def test_failed_candidate_discard_cannot_undo_durable_unlock_or_allow_confirmation(lock_mode, monkeypatch):
  mode = lock_mode
  event_id = candidate(mode, 9)
  finish_requested_pair(mode)
  original = mode.store.discard_event

  def disk_full(_event_id):
    raise sqlite3.OperationalError("disk full")

  monkeypatch.setattr(mode.store, "discard_event", disk_full)
  flashes(mode, 2)
  assert mode.store.get_lock_control() == ("unlock_pause", "unlocked")
  assert mode.discard_pending and mode.active_event_id == event_id
  mode._process_detection("warning", mode.clock())
  send_sample(mode, mode.clock() + 0.1, 1.0)
  assert not mode.active_confirmed and mode.store.next_pending(0) is None
  monkeypatch.setattr(mode.store, "discard_event", original)
  send_sample(mode, mode.clock() + 0.1, 1.1)
  assert mode.active_event_id is None and not mode.discard_pending
  assert mode.store.get_lock_control() == ("unlock_pause", "unlocked")


def test_rearm_completion_write_failure_keeps_persisted_hold_and_retries(lock_mode, monkeypatch):
  mode = lock_mode
  recognized = flashes(mode, 1)
  original = mode.store.set_lock_control

  def disk_full(state, *args, **kwargs):
    if state == "none":
      raise sqlite3.OperationalError("disk full")
    return original(state, *args, **kwargs)

  monkeypatch.setattr(mode.store, "set_lock_control", disk_full)
  send_sample(mode, recognized + ARM_DELAY_SECONDS + 0.001, 1.0)
  assert mode.lock_control == "rearm" and mode.store.get_lock_control() == ("rearm", "locked")
  assert mode.active_event_id is None and not mode.capture_queue
  monkeypatch.setattr(mode.store, "set_lock_control", original)
  send_sample(mode, mode.clock() + 0.1, 1.1)
  assert mode.lock_control == "none" and mode.state == "armed"


@pytest.mark.parametrize("unknown_ignition", (False, True))
def test_final_revision_durability_and_full_cap_delay_take_precedence_over_lock_arming(lock_mode, monkeypatch, unknown_ignition):
  mode = lock_mode
  event_id = capture_through(mode, 20)
  queue_next_pair(mode, event_id, 21)
  original = mode.store.finish_capture

  def disk_full(target_id, revision, media, omissions):
    if target_id == event_id and revision == 21:
      raise sqlite3.OperationalError("disk full")
    return original(target_id, revision, media, omissions)

  monkeypatch.setattr(mode.store, "finish_capture", disk_full)
  finish_pair(mode)
  final_worker = mode.active_capture
  final_result = final_worker.result[0]
  recognized = flashes(mode, 1)
  assert mode.cap_pending_event_id == event_id
  if unknown_ignition:
    mode.sm.panda_checks = False
    send_sample(mode, recognized + 1, 0.0)
    assert mode.ignition_error is not None and mode.cap_pending_event_id == event_id
    assert mode.store.get_lock_control() == ("rearm", "locked")
    mode.sm.panda_checks = True
    observe(mode, recognized + 2, DoorSample(False, recognized + 2))
    assert mode.cap_pending_event_id == event_id
  send_sample(mode, recognized + ARM_DELAY_SECONDS + 3, 0.0)
  assert mode.lock_control == "rearm" and mode.cap_pending_event_id == event_id
  assert final_worker.result[0] is final_result and mode.capture.pairs == 21
  monkeypatch.setattr(mode.store, "finish_capture", original)
  mode.test_now[0] = final_worker.next_finalize_at
  mode._finish_capture_if_ready()
  finalized = mode.clock()
  assert mode.cap_rearm_until == finalized + ARM_DELAY_SECONDS
  send_sample(mode, finalized + ARM_DELAY_SECONDS - 0.001, 0.0)
  assert mode.lock_control == "rearm"
  send_sample(mode, finalized + ARM_DELAY_SECONDS, 0.0)
  assert mode.lock_control == "none" and mode.state == "armed"
  assert len(event_rows(mode, event_id)) == 21 and mode.capture.pairs == 21
