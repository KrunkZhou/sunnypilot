from collections import deque

import pytest

from openpilot.cereal import log
from opendbc.car.structs import car
from openpilot.system.sentryd import door
from openpilot.system.sentryd.door import DoorSample, DriverDoorSource


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
  reader.poll(100.1, after=90)
  assert len(params.reads) == 1
  params.raw = profile(fingerprint="UNKNOWN")
  socket.events.append(event(at=101))
  assert reader.poll(101, after=90) == []
  assert reader._socket is None
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
