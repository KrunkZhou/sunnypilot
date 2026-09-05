import importlib.util
import sys
import types
from pathlib import Path

import pytest

from openpilot import cereal
from openpilot.system.sentryd.detector import MotionDetector
from openpilot.system.sentryd.sentryd import SentryMode


@pytest.fixture
def actual_messaging(monkeypatch):
  """Exercise production SubMaster bookkeeping without requiring native IPC."""
  native_stub = types.ModuleType("msgq")
  for name in (
      "fake_event_handle", "drain_sock_raw", "MultiplePublishersError", "IpcError",
      "Context", "Poller", "SubSocket", "PubSocket", "SocketEventHandle", "toggle_fake_events",
      "set_fake_prefix", "get_fake_prefix", "delete_fake_prefix", "wait_for_one_event",
  ):
    setattr(native_stub, name, type(name, (), {}))
  source = Path(cereal.__file__).parent / "messaging" / "__init__.py"
  spec = importlib.util.spec_from_file_location("_sentry_sensor_health_messaging", source)
  assert spec is not None and spec.loader is not None
  messaging = importlib.util.module_from_spec(spec)
  with monkeypatch.context() as context:
    context.setitem(sys.modules, "msgq", native_stub)
    spec.loader.exec_module(messaging)
  messaging.sub_sock = lambda *args, **kwargs: object()
  return messaging


def make_mode(messaging):
  mode = SentryMode.__new__(SentryMode)
  mode.sm = messaging.SubMaster(["accelerometer"], frequency=10.0)
  mode.detector = MotionDetector()
  mode.accelerometer_status = {}
  return mode


def receive_sample(messaging, mode, received_at, *, sampled_at=None, valid=True):
  sampled_at = received_at if sampled_at is None else sampled_at
  msg = messaging.new_message("accelerometer", valid=valid, logMonoTime=int(sampled_at * 1e9))
  msg.accelerometer.acceleration.v = [0.0, 0.0, 9.81]
  mode.sm.update_msgs(received_at, [msg.as_reader()])


def test_fresh_samples_below_consumer_frequency_floor_are_usable(actual_messaging):
  mode = make_mode(actual_messaging)
  for index in range(130):
    now = 100.0 + index * 0.13
    receive_sample(actual_messaging, mode, now)

  # A 100 ms sleep plus 30 ms processing fails the old 8 Hz frequency floor,
  # despite a fresh, valid accelerometer message being received every update.
  assert mode.sm.seen["accelerometer"]
  assert mode.sm.alive["accelerometer"]
  assert mode.sm.valid["accelerometer"]
  assert not mode.sm.freq_ok["accelerometer"]
  assert not mode.sm.all_checks(["accelerometer"])
  assert mode._accelerometer_error(now) is None
  assert mode.accelerometer_status["frequency_ok"] is False
  assert mode.accelerometer_status["receive_age_seconds"] == pytest.approx(0.0)
  assert mode.accelerometer_status["sample_age_seconds"] == pytest.approx(0.0, abs=1e-8)


def test_one_missed_conflated_poll_does_not_make_samples_stale(actual_messaging):
  mode = make_mode(actual_messaging)
  for index in range(130):
    now = 100.0 + index * 0.1
    receive_sample(actual_messaging, mode, now)
  now += 0.1
  mode.sm.update_msgs(now, [])

  # SubMaster still derives alive from the 104 Hz publisher's 96 ms deadline,
  # even when its consumer is deliberately limited to 10 Hz.
  assert not mode.sm.updated["accelerometer"]
  assert not mode.sm.alive["accelerometer"]
  assert mode.sm.freq_ok["accelerometer"]
  assert not mode.sm.all_checks(["accelerometer"])
  assert mode._accelerometer_error(now) is None
  assert mode.accelerometer_status["alive"] is False
  assert mode.accelerometer_status["receive_age_seconds"] == pytest.approx(0.1)
  assert mode.accelerometer_status["sample_age_seconds"] == pytest.approx(0.1)


def test_no_accelerometer_samples_fail_closed(actual_messaging):
  mode = make_mode(actual_messaging)
  mode.sm.update_msgs(100.0, [])
  assert mode._accelerometer_error(100.0)
  assert mode.accelerometer_status["seen"] is False


def test_invalid_accelerometer_message_fails_closed(actual_messaging):
  mode = make_mode(actual_messaging)
  receive_sample(actual_messaging, mode, 100.0)
  receive_sample(actual_messaging, mode, 100.1, valid=False)
  assert mode._accelerometer_error(100.1)
  assert mode.accelerometer_status["seen"] is True
  assert mode.accelerometer_status["valid"] is False


@pytest.mark.parametrize("age,usable", [(0.999, True), (1.0, False), (1.001, False)])
def test_receive_freshness_boundary(actual_messaging, age, usable):
  mode = make_mode(actual_messaging)
  receive_sample(actual_messaging, mode, 100.0)
  now = 100.0 + age
  mode.sm.update_msgs(now, [])
  assert (mode._accelerometer_error(now) is None) is usable
  assert mode.accelerometer_status["receive_age_seconds"] == pytest.approx(age)


@pytest.mark.parametrize("age,usable", [(0.999, True), (1.0, False), (1.001, False)])
def test_newly_received_stale_source_sample_fails_closed(actual_messaging, age, usable):
  mode = make_mode(actual_messaging)
  receive_sample(actual_messaging, mode, 100.0, sampled_at=100.0 - age)
  assert (mode._accelerometer_error(100.0) is None) is usable
  assert mode.accelerometer_status["receive_age_seconds"] == pytest.approx(0.0)
  assert mode.accelerometer_status["sample_age_seconds"] == pytest.approx(age)


@pytest.mark.parametrize("future_timestamp", ["receive", "source"])
def test_future_timestamps_fail_closed(actual_messaging, future_timestamp):
  mode = make_mode(actual_messaging)
  receive_sample(actual_messaging, mode, 100.0, sampled_at=100.1 if future_timestamp == "source" else 100.0)
  if future_timestamp == "receive":
    mode.sm.recv_time["accelerometer"] = 100.1
  assert mode._accelerometer_error(100.0)


@pytest.mark.parametrize("field,value", [
  ("recv_time", float("nan")),
  ("recv_time", float("inf")),
  ("recv_time", "invalid"),
  ("logMonoTime", 0),
  ("logMonoTime", -1),
  ("logMonoTime", float("nan")),
  ("logMonoTime", "invalid"),
])
def test_malformed_sample_timestamps_fail_closed(actual_messaging, field, value):
  mode = make_mode(actual_messaging)
  receive_sample(actual_messaging, mode, 100.0)
  getattr(mode.sm, field)["accelerometer"] = value
  assert mode._accelerometer_error(100.0)
