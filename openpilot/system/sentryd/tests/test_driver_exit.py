from dataclasses import replace

import pytest

from openpilot.system.sentryd.door import DoorSample
from openpilot.system.sentryd.sentryd import ARM_DELAY_SECONDS, CaptureJob
from openpilot.system.sentryd.tests.test_repeated_capture import finish_pair, motion_at, send_sample
from openpilot.system.sentryd.tests.test_repeated_capture import mode as mode


class DoorSource:
  def __init__(self):
    self.samples = []
    self.error = None
    self.calls = []

  def poll(self, now, *, after):
    self.calls.append((now, after))
    samples, self.samples = self.samples, []
    return [sample for sample in samples if after <= sample.monotonic_time <= now]


@pytest.fixture
def exit_mode(mode):
  mode.config_store.config = replace(mode.config, wait_for_driver_exit=True)
  mode.door_source = DoorSource()
  send_sample(mode, 100.0, 0.0)
  assert mode.state == "waiting_for_door_open"
  return mode


def door_update(mode, now, *opened):
  now = round(now * 1e9) / 1e9
  mode.door_source.samples = [DoorSample(value, now) for value in opened]
  send_sample(mode, now, now % 2)


def complete_exit(mode):
  door_update(mode, mode.clock() + 0.1, True)
  assert mode.state == "waiting_for_door_close"
  door_update(mode, mode.clock() + 0.1, False)
  assert mode.driver_exit_completed and mode.state == "arming"
  return mode.arm_started_at


def test_closed_door_and_motion_alone_cannot_start_timer_or_stop_upload_polling(exit_mode, monkeypatch):
  mode = exit_mode
  uploads = []
  monkeypatch.setattr(mode, "_start_upload_if_needed", lambda: uploads.append(mode.clock()))
  for now in (100.1, 150.0, 200.0, 300.0):
    door_update(mode, now, False)
    assert mode.state == "waiting_for_door_open"
    assert mode.arm_started_at is None and not mode.driver_exit_completed
    assert mode.active_event_id is None and not mode.capture_queue
  assert len(uploads) == 4


def test_driver_open_close_then_full_ninety_seconds_and_fresh_motion(exit_mode):
  mode = exit_mode
  door_update(mode, 100.1, True)
  door_update(mode, 250.0, True)
  assert mode.state == "waiting_for_door_close" and mode.arm_started_at is None
  door_update(mode, 251.0, False)
  assert mode.arm_started_at == 251.0 and mode.state == "arming"
  send_sample(mode, 251.0 + ARM_DELAY_SECONDS - 0.001, 0.1)
  assert mode.state == "arming" and not mode.capture_queue
  send_sample(mode, 251.0 + ARM_DELAY_SECONDS, 0.2)
  assert mode.state == "armed" and not mode.capture_queue
  send_sample(mode, 251.0 + ARM_DELAY_SECONDS + 0.1, 0.3)
  assert mode.capture_queue == [CaptureJob(mode.active_event_id, 1)]


@pytest.mark.parametrize("sequence, ready", [((True, False), True), ((True, False, True), False), ((False,), False)])
def test_all_ordered_door_observations_are_processed(exit_mode, sequence, ready):
  door_update(exit_mode, 100.1, *sequence)
  assert exit_mode.driver_exit_completed is ready
  assert (exit_mode.arm_started_at is not None) is ready


@pytest.mark.parametrize("open_seen", [False, True])
def test_missing_door_signal_never_means_closed(exit_mode, open_seen):
  mode = exit_mode
  if open_seen:
    door_update(mode, 100.1, True)
  mode.door_source.error = "No fresh driver-door CAN samples. Turn off Wait for Driver Exit for USB use."
  send_sample(mode, 250.0, 0.1)
  assert mode.state == "door_signal_unavailable"
  assert "USB" in mode.state_error
  assert mode.driver_door_open_seen is open_seen
  assert mode.arm_started_at is None and not mode.capture_queue


def test_ignition_resets_exit_and_ignores_door_open_seen_onroad(exit_mode):
  mode = exit_mode
  complete_exit(mode)
  mode.sm.panda_states[0].ignitionCan = True
  mode.door_source.samples = [DoorSample(True, 110.0)]
  send_sample(mode, 110.0, 0.1)
  assert mode.state == "disabled" and mode.arm_started_at is None
  assert not mode.driver_exit_completed and not mode.driver_door_open_seen
  mode.sm.panda_states[0].ignitionCan = False
  send_sample(mode, 120.0, 0.2)
  assert mode.state == "waiting_for_door_open" and not mode.driver_door_open_seen
  door_update(mode, 120.1, False)
  assert mode.arm_started_at is None
  assert complete_exit(mode) == pytest.approx(120.3)


def test_toggle_off_restores_usb_timer_and_toggle_on_requires_a_new_exit(exit_mode):
  mode = exit_mode
  mode.door_source.error = "No CAN on USB"
  mode.config_store.config = replace(mode.config, wait_for_driver_exit=False)
  send_sample(mode, 101.0, 0.1)
  assert mode.state == "arming" and mode.arm_started_at == 101.0
  send_sample(mode, 191.0, 0.2)
  assert mode.state == "armed"
  send_sample(mode, 191.1, 0.3)
  event_id = mode.active_event_id
  assert mode.capture_queue == [CaptureJob(event_id, 1)]
  mode.config_store.config = replace(mode.config, wait_for_driver_exit=True)
  mode.last_config_refresh = float("-inf")
  send_sample(mode, 191.2, 0.4)
  assert mode.state == "door_signal_unavailable" and mode.active_event_id is None
  assert not mode.capture_queue and mode.store.revision_state(event_id, 1) == "ready"
  assert mode.arm_started_at is None and not mode.driver_exit_completed


def test_manual_test_still_queues_while_waiting_for_exit(exit_mode, monkeypatch):
  import openpilot.system.sentryd.sentryd as daemon
  monkeypatch.setattr(daemon, "take_command", lambda _params: {"command": "manual_test"})
  exit_mode._handle_command()
  assert len(exit_mode.capture_queue) == 1
  assert exit_mode.active_event_id is None and not exit_mode.driver_exit_completed
  row = exit_mode.store.connection.execute("SELECT source, closed FROM events").fetchone()
  assert tuple(row) == ("manual_test", 1)


def test_capture_limit_rearm_does_not_require_a_second_driver_exit(exit_mode):
  mode = exit_mode
  armed_at = complete_exit(mode) + ARM_DELAY_SECONDS + 0.001
  send_sample(mode, armed_at, 0.0)
  send_sample(mode, armed_at + 0.1, 0.1)
  event_id = mode.active_event_id
  mode._start_capture_if_needed()
  finish_pair(mode)
  for revision in range(2, 22):
    motion_at(mode, mode.next_capture_at)
    mode._schedule_motion_capture(mode.clock())
    assert mode.capture_queue == [CaptureJob(event_id, revision)]
    mode._start_capture_if_needed()
    finish_pair(mode)
  assert mode.driver_exit_completed and mode.state == "arming"
  source_calls = len(mode.door_source.calls)
  mode.door_source.error = "Parked CAN bus has gone to sleep"
  rearmed_at = mode.arm_started_at + ARM_DELAY_SECONDS
  send_sample(mode, rearmed_at - 0.001, 0.2)
  assert mode.state == "arming"
  send_sample(mode, rearmed_at + 0.001, 0.3)
  assert mode.state == "armed"
  send_sample(mode, rearmed_at + 0.1, 0.4)
  assert len(mode.door_source.calls) == source_calls
  assert mode.active_event_id is not None and mode.active_event_id != event_id
  assert mode.capture_queue == [CaptureJob(mode.active_event_id, 1)]


def test_real_can_decoder_to_daemon_waits_for_driver_not_passenger_exit(exit_mode, monkeypatch):
  from openpilot.system.sentryd import door
  from openpilot.system.sentryd.tests.test_door import Socket, event, frame, profile
  from opendbc.can import CANPacker

  mode = exit_mode
  socket = Socket()
  monkeypatch.setattr(door, "_open_can_socket", lambda: socket)
  mode.params.values["CarParamsPersistent"] = profile()
  mode.door_source = door.DriverDoorSource(mode.params)
  packer = CANPacker("vw_mqb")

  def packed_frame(signal, opened):
    address, data, bus = packer.make_can_msg("Gateway_72", 0, {signal: int(opened)})
    return frame(bus=bus, address=address, data=data)

  # Neither an old driver-open sample nor a fresh passenger-door cycle counts.
  socket.events.extend([
    event(at=99.9, frames=[packed_frame("ZV_FT_offen", True)]),
    event(at=100.1, frames=[packed_frame("ZV_BT_offen", True), packed_frame("ZV_BT_offen", False)]),
  ])
  send_sample(mode, 100.1, 0.1)
  assert mode.state == "waiting_for_door_open" and mode.arm_started_at is None
  socket.events.append(event(at=100.2, frames=[packed_frame("ZV_FT_offen", True)]))
  send_sample(mode, 100.2, 0.2)
  assert mode.state == "waiting_for_door_close"
  socket.events.append(event(at=100.3, frames=[frame(data=b"\x00" * 3)]))
  send_sample(mode, 100.3, 0.3)
  assert mode.arm_started_at is None and not mode.driver_exit_completed
  socket.events.append(event(at=100.4, frames=[packed_frame("ZV_FT_offen", False)]))
  send_sample(mode, 100.4, 0.4)
  assert mode.state == "arming" and mode.arm_started_at == 100.4
  assert mode.driver_exit_completed
  send_sample(mode, 190.5, 0.5)
  assert mode.state == "armed" and not mode.capture_queue
  send_sample(mode, 190.6, 0.6)
  assert mode.capture_queue == [CaptureJob(mode.active_event_id, 1)]
