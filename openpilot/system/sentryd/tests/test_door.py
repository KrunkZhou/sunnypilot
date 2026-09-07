from collections import deque

import pytest

from openpilot.cereal import log
from opendbc.car.structs import car
from openpilot.system.sentryd import door
from openpilot.system.sentryd.door import DoorSample, DriverDoorSource, LightSample


def profile(*, fingerprint="AUDI_A3_MK3", safety_configs=1, not_car=False):
  cp = car.CarParams.new_message(carFingerprint=fingerprint, notCar=not_car)
  cp.init("safetyConfigs", safety_configs)
  return cp.to_bytes()


class Params:
  def __init__(self, raw=None):
    self.raw = raw
    self.reads = []

  def get(self, key):
    self.reads.append(key)
    return self.raw


class Socket:
  def __init__(self):
    self.events = deque()
    self.reads = 0

  def receive(self, *, non_blocking):
    assert non_blocking is True
    self.reads += 1
    return self.events.popleft() if self.events else None


def frame(is_open=True, *, bus=0, address=door.GATEWAY_ADDRESS, data=None):
  if data is None:
    data = bytes([0, 0, 0, door.DRIVER_DOOR_MASK if is_open else 0, 0, 0, 0, 0])
  return {"src": bus, "address": address, "dat": data}


def event(at=100.0, *, frames=None, valid=True):
  message = log.Event.new_message(logMonoTime=int(at * 1e9), valid=valid)
  message.can = [frame()] if frames is None else frames
  return message.to_bytes()


@pytest.fixture
def source(monkeypatch):
  params = Params(profile())
  socket = Socket()
  opened = []

  def open_socket():
    opened.append(True)
    return socket

  monkeypatch.setattr(door, "_open_can_socket", open_socket)
  return DriverDoorSource(params), params, socket, opened


def test_no_native_socket_until_supported_profile(source):
  reader, params, socket, opened = source
  assert opened == []
  params.raw = None
  assert reader.poll(100, after=90) == []
  assert opened == []
  assert "No saved vehicle" in reader.error
  assert params.reads == ["CarParamsPersistent"]
  params.raw = profile()
  socket.events.append(event(at=101))
  assert reader.poll(101, after=90) == [DoorSample(True, 101)]
  assert opened == [True]
  assert reader.error is None


def test_socket_is_subscribe_only_without_conflation(monkeypatch):
  import sys
  from types import ModuleType
  from openpilot import cereal

  module = ModuleType("openpilot.cereal.messaging")
  calls = []
  socket = Socket()

  def sub_sock(name, *, conflate):
    calls.append((name, conflate))
    return socket

  module.sub_sock = sub_sock
  monkeypatch.setitem(sys.modules, "openpilot.cereal.messaging", module)
  monkeypatch.setattr(cereal, "messaging", module, raising=False)
  assert door._open_can_socket() is socket
  assert calls == [("can", False)]


def test_socket_creation_failure_is_reported_without_escaping(source, monkeypatch):
  reader, _, _, _ = source

  def fail():
    raise RuntimeError("native receiver unavailable")

  monkeypatch.setattr(door, "_open_can_socket", fail)
  assert reader.poll(100, after=90) == []
  assert "receiver is unavailable" in reader.error


def test_other_classic_mqb_vehicle_uses_existing_dbc_support(source):
  reader, params, socket, _ = source
  params.raw = profile(fingerprint="VOLKSWAGEN_GOLF_MK7")
  socket.events.append(event())
  assert reader.poll(100, after=90) == [DoorSample(True, 100)]


@pytest.mark.parametrize("raw", [
  b"", b"malformed capnp", "not bytes", b"x" * (door.MAX_CAR_PARAMS_BYTES + 1),
  profile(fingerprint="TOYOTA_PRIUS"), profile(fingerprint="UNKNOWN"),
  profile(not_car=True), profile(safety_configs=0), profile(safety_configs=5),
])
def test_bad_or_unsupported_profiles_fail_closed_without_socket(source, raw):
  reader, params, _, opened = source
  params.raw = raw
  assert reader.poll(100, after=90) == []
  assert opened == []
  assert reader.error is not None
  assert "Turn off Wait for Driver Exit" in reader.error


def test_profile_read_errors_do_not_stop_source(source, monkeypatch):
  reader, params, _, opened = source

  def fail(_key):
    raise OSError("read failure")

  monkeypatch.setattr(params, "get", fail)
  assert reader.poll(100, after=90) == []
  assert opened == []
  assert "could not be read" in reader.error


def test_profile_reads_are_rate_limited_and_changes_fail_closed(source):
  reader, params, socket, opened = source
  socket.events.append(event())
  assert reader.poll(100, after=90)
  generation = reader.generation
  reader.poll(100.1, after=90)
  assert len(params.reads) == 1
  params.raw = profile(fingerprint="UNKNOWN")
  socket.events.append(event(at=101))
  assert reader.poll(101, after=90) == []
  assert reader._socket is None
  assert reader.generation > generation  # Profile loss invalidates any paused closure baseline.
  assert reader.error is not None
  assert len(opened) == 1


def test_open_close_and_reopen_preserved_in_one_batch(source):
  reader, _, socket, _ = source
  socket.events.append(event(frames=[frame(), frame(False), frame()]))
  assert reader.poll(100.1, after=90) == [DoorSample(True, 100), DoorSample(False, 100), DoorSample(True, 100)]
  assert reader.error is None


@pytest.mark.parametrize("bad_frame", [
  frame(bus=1), frame(bus=2), frame(bus=128), frame(address=0x3DC),
  frame(data=b""), frame(data=b"\x00\x00\x00"), frame(data=b"\x00" * 7), frame(data=b"\x00" * 9),
])
def test_wrong_bus_address_or_length_never_implies_closed(source, bad_frame):
  reader, _, socket, _ = source
  socket.events.append(event(frames=[bad_frame]))
  assert reader.poll(100.1, after=90) == []
  assert reader.error is not None


def test_bus_offset_from_validated_profile(source):
  reader, params, socket, _ = source
  params.raw = profile(safety_configs=2)
  socket.events.append(event(frames=[frame(bus=0), frame(False, bus=4)]))
  assert reader.poll(100, after=90) == [DoorSample(False, 100)]


@pytest.mark.parametrize("at,after", [(98.999, 90), (99, 90), (100.001, 90), (99.75, 99.9)])
def test_stale_future_or_pre_offroad_frames_are_not_observations(source, at, after):
  reader, _, socket, _ = source
  socket.events.append(event(at=at))
  assert reader.poll(100, after=after) == []
  assert reader.error is not None


def test_duplicate_and_out_of_order_events_are_rejected(source):
  reader, _, socket, _ = source
  socket.events.extend([event(at=100), event(at=100, frames=[frame(False)]), event(at=99.9, frames=[frame(False)])])
  assert reader.poll(100.1, after=90) == [DoorSample(True, 100)]
  socket.events.append(event(at=100.05, frames=[frame(False)]))
  assert reader.poll(100.1, after=90) == [DoorSample(False, 100.05)]


def test_future_timestamp_does_not_poison_later_valid_events(source):
  reader, _, socket, _ = source
  socket.events.extend([event(at=1000), event(at=100)])
  assert reader.poll(100.1, after=90) == [DoorSample(True, 100)]


def test_old_boundary_does_not_supply_fresh_status_for_new_cycle(source):
  reader, _, socket, _ = source
  socket.events.append(event())
  assert reader.poll(100, after=90)
  assert reader.poll(100.1, after=100.1) == []
  assert "No fresh" in reader.error


def test_no_default_closed_and_freshness_expires_without_new_samples(source):
  reader, _, socket, _ = source
  assert reader.poll(100, after=90) == []
  assert "No fresh" in reader.error
  socket.events.append(event())
  assert reader.poll(100.1, after=90) == [DoorSample(True, 100)]
  assert reader.poll(100.9, after=90) == []
  assert reader.error is None
  assert reader.poll(101, after=90) == []
  assert "No fresh" in reader.error


@pytest.mark.parametrize("raw", [event(valid=False), b"bad capnp", b"", b"x" * (door.MAX_CAN_EVENT_BYTES + 1)])
def test_invalid_can_events_fail_closed(source, raw):
  reader, _, socket, _ = source
  socket.events.append(raw)
  assert reader.poll(100.1, after=90) == []
  assert "invalid" in reader.error


def test_wrong_capnp_union_is_ignored(source):
  reader, _, socket, _ = source
  message = log.Event.new_message(logMonoTime=100_000_000_000, valid=True)
  message.init("deviceState")
  socket.events.append(message.to_bytes())
  assert reader.poll(100.1, after=90) == []
  assert "invalid" in reader.error


def test_poll_bounded_to_512_nonblocking_events(source):
  reader, _, socket, _ = source
  socket.events.extend(event(at=100 + i / 10000) for i in range(door.MAX_EVENTS_PER_POLL + 1))
  assert len(reader.poll(100.1, after=90)) == door.MAX_EVENTS_PER_POLL
  assert socket.reads == door.MAX_EVENTS_PER_POLL
  assert len(socket.events) == 1
  assert not reader.queue_drained
  assert len(reader.poll(100.1, after=90)) == 1
  assert reader.queue_drained and not socket.events


def test_socket_failure_is_reported_and_retried(source, monkeypatch):
  reader, _, socket, opened = source

  def fail(*, non_blocking):
    raise RuntimeError("receive failed")

  original_receive = socket.receive
  monkeypatch.setattr(socket, "receive", fail)
  assert reader.poll(100, after=90) == []
  assert "receiver failed" in reader.error
  assert reader._socket is None
  monkeypatch.setattr(socket, "receive", original_receive)
  socket.events.append(event(at=100.1))
  assert reader.poll(100.1, after=90) == [DoorSample(True, 100.1)]
  assert len(opened) == 2


@pytest.mark.parametrize("now,after", [(float("nan"), 90), (100, float("inf")), (100, 101)])
def test_invalid_clock_does_not_open_socket(source, now, after):
  reader, _, _, opened = source
  assert reader.poll(now, after=after) == []
  assert "time is invalid" in reader.error
  assert opened == []


def test_driver_door_bit_mapping_matches_existing_mqb_dbc():
  from opendbc.can import CANPacker, CANParser

  packer = CANPacker("vw_mqb")
  parser = CANParser("vw_mqb", [("Gateway_72", 0)], 0)
  for signal in ("ZV_FT_offen", "ZV_BT_offen", "ZV_HFS_offen", "ZV_HBFS_offen", "ZV_HD_offen"):
    address, data, bus = packer.make_can_msg("Gateway_72", 0, {signal: 1})
    assert address == door.GATEWAY_ADDRESS
    assert len(data) == door.GATEWAY_BYTES
    assert bool(data[door.DRIVER_DOOR_BYTE] & door.DRIVER_DOOR_MASK) == (signal == "ZV_FT_offen")
    parser.update([(100, [(address, data, bus)])])
    assert bool(parser.vl["Gateway_72"]["ZV_FT_offen"]) == (signal == "ZV_FT_offen")


@pytest.mark.parametrize("signal, name", [
  ("ZV_FT_offen", "driver"), ("ZV_BT_offen", "passenger"), ("ZV_HFS_offen", "rear_left"),
  ("ZV_HBFS_offen", "rear_right"), ("ZV_HD_offen", "trunk"),
])
def test_real_dbc_decoder_reports_each_door_and_trunk_independently(source, signal, name):
  from opendbc.can import CANPacker
  reader, _, socket, _ = source
  address, data, bus = CANPacker("vw_mqb").make_can_msg("Gateway_72", 0, {signal: 1})
  socket.events.append(event(frames=[frame(bus=bus, address=address, data=data)]))
  samples = reader.poll(100.1, after=90)
  assert len(samples) == 1
  assert samples[0].open_doors == frozenset({name})
  assert samples[0].open is (name == "driver")
  assert reader.queue_drained


def test_decoder_preserves_all_simultaneously_open_doors_and_closed_transition(source):
  from opendbc.can import CANPacker
  reader, _, socket, _ = source
  signals = dict.fromkeys(("ZV_FT_offen", "ZV_BT_offen", "ZV_HFS_offen", "ZV_HBFS_offen", "ZV_HD_offen"), 1)
  address, data, bus = CANPacker("vw_mqb").make_can_msg("Gateway_72", 0, signals)
  socket.events.append(event(frames=[frame(bus=bus, address=address, data=data), frame(False)]))
  samples = reader.poll(100.1, after=90)
  assert [sample.open_doors for sample in samples] == [frozenset({"driver", "passenger", "rear_left", "rear_right", "trunk"}), frozenset()]


def test_queued_open_after_budget_cannot_be_reported_as_drained(source):
  reader, _, socket, _ = source
  socket.events.extend(event(at=100 + i / 10000, frames=[frame(False)]) for i in range(door.MAX_EVENTS_PER_POLL))
  socket.events.append(event(at=100.06, frames=[frame(True)]))
  first = reader.poll(100.1, after=90)
  assert all(not sample.open_doors for sample in first)
  assert not reader.queue_drained
  assert reader.poll(100.1, after=90) == [DoorSample(True, 100.06)]
  assert reader.queue_drained


def light_frame(*, bus=0, left=False, right=False, hazards=False, fast=False):
  data = bytes([0, 0, 0x10 if hazards else 0, (0x02 if left else 0) | (0x04 if right else 0), 0x20 if fast else 0, 0, 0, 0])
  return frame(bus=bus, address=door.LIGHT_ADDRESS, data=data)


@pytest.mark.parametrize("signal,expected", [
  ("Left_Turn_Exterior_Bulb_1", (True, False, False, False)),
  ("Left_Turn_Exterior_Bulb_2", (True, False, False, False)),
  ("Right_Turn_Exterior_Bulb_1", (False, True, False, False)),
  ("Right_Turn_Exterior_Bulb_2", (False, True, False, False)),
  ("Hazard_Switch", (False, False, True, False)),
  ("Fast_Send_Rate_Active", (False, False, False, True)),
  ("Comfort_Signal_Left", (False, False, False, False)),
  ("Comfort_Signal_Right", (False, False, False, False)),
])
def test_real_dbc_light_decoder_reads_exterior_bulbs_not_comfort(source, signal, expected):
  from opendbc.can import CANPacker
  reader, _, socket, _ = source
  address, data, bus = CANPacker("vw_mqb").make_can_msg("Blinkmodi_02", 0, {signal: 1})
  assert address == door.LIGHT_ADDRESS and len(data) == door.LIGHT_BYTES
  socket.events.append(event(frames=[frame(bus=bus, address=address, data=data), frame(False)]))
  assert reader.poll(100.1, after=90) == [DoorSample(False, 100)]
  assert reader.light_samples == [LightSample(*expected, 100)]
  assert reader.light_error is None


def test_lights_share_nonconflated_socket_and_profile_bus_without_polluting_doors(source):
  reader, params, socket, opened = source
  params.raw = profile(safety_configs=2)
  socket.events.extend([
    event(at=100, frames=[light_frame(bus=0, left=True), light_frame(bus=4, left=True, right=True)]),
    event(at=100.02, frames=[light_frame(bus=4), frame(False, bus=4)]),
  ])
  assert reader.poll(100.1, after=90) == [DoorSample(False, 100.02)]
  assert reader.light_samples == [LightSample(True, True, False, False, 100), LightSample(False, False, False, False, 100.02)]
  assert opened == [True]
  assert reader.poll(100.2, after=90) == []
  assert reader.light_samples == []


@pytest.mark.parametrize("fast,age,accepted", [(True, 0.25, True), (True, 0.2501, False), (False, 1.5, True), (False, 1.5001, False)])
def test_light_freshness_uses_declared_send_rate_without_extending_door_freshness(source, fast, age, accepted):
  reader, _, socket, _ = source
  socket.events.append(event(at=100, frames=[light_frame(fast=fast), frame(False)]))
  doors = reader.poll(100 + age, after=90)
  assert bool(reader.light_samples) is accepted
  assert bool(doors) is (age < door.SAMPLE_MAX_AGE_SECONDS)
  assert (reader.light_error is None) is accepted


@pytest.mark.parametrize("bad", [
  event(at=99.9, frames=[light_frame()]), event(at=101, frames=[light_frame()]),
  event(at=100.02, frames=[frame(address=door.LIGHT_ADDRESS, data=b"\0" * 7)]),
  event(at=100.02, frames=[frame(address=door.LIGHT_ADDRESS, data=b"\0" * 9)]),
  event(at=100.02, valid=False), b"bad capnp", b"", b"x" * (door.MAX_CAN_EVENT_BYTES + 1),
])
def test_invalid_light_stream_invalidates_generation_and_clears_earlier_batch(source, bad):
  reader, _, socket, _ = source
  socket.events.append(event(at=100, frames=[light_frame()]))
  reader.poll(100.1, after=90)
  generation = reader.light_generation
  socket.events.extend([event(at=100.01, frames=[light_frame(left=True, right=True)]), bad])
  reader.poll(100.1, after=90)
  assert reader.light_generation > generation
  assert reader.light_samples == []
  assert reader.light_error is not None


def test_duplicate_light_event_invalidates_but_unrelated_wrong_bus_does_not(source):
  reader, _, socket, _ = source
  socket.events.append(event(at=100, frames=[light_frame()]))
  reader.poll(100.1, after=90)
  generation = reader.light_generation
  socket.events.append(event(at=100, frames=[light_frame(bus=1, hazards=True)]))
  reader.poll(100.1, after=90)
  assert reader.light_generation == generation
  socket.events.append(event(at=100, frames=[light_frame(left=True, right=True)]))
  reader.poll(100.1, after=90)
  assert reader.light_generation > generation
  assert reader.light_samples == []


def test_sleep_changes_light_error_without_continuously_changing_generation(source):
  reader, _, socket, _ = source
  socket.events.append(event(at=100, frames=[light_frame()]))
  reader.poll(100.1, after=90)
  generation = reader.light_generation
  reader.poll(101, after=90)
  assert reader.light_error is None
  reader.poll(102, after=90)
  assert "stale" in reader.light_error
  reader.poll(103, after=90)
  assert reader.light_generation == generation


def test_profile_boundary_and_receiver_loss_invalidate_light_generation(source, monkeypatch):
  reader, params, socket, _ = source
  socket.events.append(event(at=100, frames=[light_frame()]))
  reader.poll(100, after=90)
  generation = reader.light_generation
  reader.poll(100.1, after=100.1)
  assert reader.light_generation > generation
  generation = reader.light_generation
  params.raw = profile(fingerprint="UNKNOWN")
  reader.poll(101.1, after=100.1)
  assert reader.light_generation > generation and reader.light_error is not None
  params.raw = profile()
  reader.poll(102.1, after=100.1)
  generation = reader.light_generation

  def fail(*, non_blocking):
    raise RuntimeError("receiver failure")

  monkeypatch.setattr(socket, "receive", fail)
  reader.poll(102.2, after=100.1)
  assert reader.light_generation > generation and "receiver failed" in reader.light_error


def test_queued_hazard_after_poll_budget_remains_available_on_next_poll(source):
  reader, _, socket, _ = source
  socket.events.extend(event(at=100 + index / 10000, frames=[light_frame(fast=True)]) for index in range(door.MAX_EVENTS_PER_POLL))
  socket.events.append(event(at=100.06, frames=[light_frame(hazards=True, fast=True)]))
  assert reader.poll(100.1, after=90) == []
  assert len(reader.light_samples) == door.MAX_EVENTS_PER_POLL
  assert not reader.queue_drained
  assert reader.poll(100.1, after=90) == []
  assert reader.queue_drained
  assert reader.light_samples == [LightSample(False, False, True, True, 100.06)]
