"""Exercise Athena's actual retry functions without compiled device dependencies."""
import ast
from collections.abc import Callable
from pathlib import Path
import random
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


ATHENA = Path(__file__).resolve().parents[1] / "athenad.py"


class FakeClock:
  def __init__(self):
    self.mono = 100.0
    self.wall = 1000.0

  def monotonic(self):
    return self.mono

  def time(self):
    return self.wall

  def advance(self, seconds):
    self.mono += seconds
    self.wall += seconds

  @property
  def offset(self):
    return self.wall - self.mono


class FakeEvent:
  def __init__(self, clock, on_wait=None):
    self.clock = clock
    self.on_wait = on_wait
    self.waits = []
    self.stopped = False

  def is_set(self):
    return self.stopped

  def set(self):
    self.stopped = True

  def wait(self, seconds):
    if not self.stopped:
      self.waits.append(seconds)
      self.clock.advance(seconds)
      if self.on_wait is not None:
        self.on_wait(self)
    return self.stopped


class WebSocketFailure(Exception):
  status_code = 401


class FakeSubMaster:
  def __init__(self):
    self.updated = {"deviceState": False}
    self.valid = {"deviceState": False}
    self.pending = None
    self.state = None

  def publish(self, network_type, *, valid=True, strength=0, metered=False):
    self.pending = (SimpleNamespace(networkType=SimpleNamespace(raw=network_type), networkStrength=strength, networkMetered=metered), valid)

  def update(self, timeout):
    assert timeout == 0
    self.updated["deviceState"] = self.pending is not None
    if self.pending is not None:
      self.state, self.valid["deviceState"] = self.pending
      self.pending = None

  def __getitem__(self, service):
    assert service == "deviceState"
    return self.state


@pytest.fixture
def retry_code():
  tree = ast.parse(ATHENA.read_text())
  names = {"backoff", "RetryNetworkMonitor", "wait_for_retry", "main"}
  functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
  clock = FakeClock()
  namespace = {"time": clock, "threading": threading, "random": random, "Callable": Callable,
               "NetworkType": SimpleNamespace(none=0), "messaging": SimpleNamespace(SubMaster=Mock(return_value=FakeSubMaster()))}
  exec(compile(ast.Module(body=functions, type_ignores=[]), str(ATHENA), "exec"), namespace)
  return namespace, clock


@pytest.mark.parametrize("wall_shift", [0.0, 4.0, -4.0])
def test_startup_preserves_delay_without_material_clock_step(retry_code, wall_shift):
  namespace, clock = retry_code
  offset = clock.offset
  clock.wall += wall_shift
  event = FakeEvent(clock)
  namespace["wait_for_retry"](6.25, event, offset)
  assert clock.mono == 106.25
  assert event.waits == [1.0] * 6 + [0.25]


@pytest.mark.parametrize("wall_shift", [5.0, -5.0, 5_210_405.0, -5_210_405.0])
def test_clock_step_during_connection_skips_remaining_startup_backoff(retry_code, wall_shift):
  namespace, clock = retry_code
  offset = clock.offset
  clock.advance(2.0)
  clock.wall += wall_shift
  event = FakeEvent(clock)
  namespace["wait_for_retry"](63.0, event, offset)
  assert event.waits == []
  assert clock.mono == 102.0


@pytest.mark.parametrize("wall_shift", [10.0, -10.0])
def test_clock_step_during_wait_wakes_at_next_poll(retry_code, wall_shift):
  namespace, clock = retry_code

  def correct_clock(event):
    if len(event.waits) == 3:
      clock.wall += wall_shift

  event = FakeEvent(clock, correct_clock)
  namespace["wait_for_retry"](63.0, event, clock.offset)
  assert event.waits == [1.0, 1.0, 1.0]
  assert clock.mono == 103.0


def test_shutdown_interrupts_startup_wait(retry_code):
  namespace, clock = retry_code
  event = FakeEvent(clock, lambda event: event.set())
  namespace["wait_for_retry"](63.0, event, clock.offset)
  assert event.waits == [1.0]
  namespace["wait_for_retry"](63.0, event, clock.offset)
  assert event.waits == [1.0]


def test_post_connect_wait_ignores_clock_changes_and_stays_interruptible(retry_code):
  namespace, clock = retry_code
  event = FakeEvent(clock, lambda event: setattr(clock, "wall", clock.wall + 100_000.0))
  namespace["wait_for_retry"](63.0, event)
  assert event.waits == [63.0]
  assert clock.mono == 163.0
  event.set()
  namespace["wait_for_retry"](63.0, event)
  assert event.waits == [63.0]


def test_zero_backoff_does_not_wait(retry_code):
  namespace, clock = retry_code
  event = FakeEvent(clock)
  namespace["wait_for_retry"](0, event, clock.offset)
  assert event.waits == []


@pytest.mark.parametrize("first_type, expected_change", [(0, False), (1, True)])
def test_network_first_valid_report_and_consumed_updates(retry_code, first_type, expected_change):
  namespace, _ = retry_code
  monitor = namespace["RetryNetworkMonitor"]()
  namespace["messaging"].SubMaster.assert_called_once_with(["deviceState"])
  assert not monitor.changed()
  monitor.sm.publish(first_type, valid=False)
  assert not monitor.changed()
  assert monitor.network_type is None
  monitor.sm.publish(first_type)
  assert monitor.changed() is expected_change
  assert not monitor.changed()
  monitor.sm.publish(first_type)
  assert not monitor.changed()


@pytest.mark.parametrize("before, after", [(0, 1), (1, 4), (4, 6), (6, 0)])
def test_network_type_changes_wake_retry_wait(retry_code, before, after):
  namespace, clock = retry_code
  monitor = namespace["RetryNetworkMonitor"]()
  monitor.sm.publish(before)
  monitor.changed()

  def change_network(event):
    if len(event.waits) == 3:
      monitor.sm.publish(after)

  event = FakeEvent(clock, change_network)
  namespace["wait_for_retry"](63, event, network_changed=monitor.changed)
  assert event.waits == [1.0] * 3
  assert clock.mono == 103.0
  assert not monitor.changed()


def test_same_network_and_invalid_reports_preserve_entire_backoff(retry_code):
  namespace, clock = retry_code
  monitor = namespace["RetryNetworkMonitor"]()
  monitor.sm.publish(1)
  monitor.changed()

  def publish_status(event):
    if len(event.waits) == 2:
      monitor.sm.publish(4, valid=False)
    else:
      monitor.sm.publish(1, strength=len(event.waits) % 5, metered=bool(len(event.waits) % 2))

  event = FakeEvent(clock, publish_status)
  namespace["wait_for_retry"](6.25, event, network_changed=monitor.changed)
  assert event.waits == [1.0] * 6 + [0.25]
  assert clock.mono == 106.25
  assert monitor.network_type == 1


def test_shutdown_interrupts_network_retry_wait(retry_code):
  namespace, clock = retry_code
  monitor = namespace["RetryNetworkMonitor"]()
  event = FakeEvent(clock, lambda event: event.set())
  namespace["wait_for_retry"](63, event, network_changed=monitor.changed)
  assert event.waits == [1.0]
  namespace["wait_for_retry"](63, event, network_changed=monitor.changed)
  assert event.waits == [1.0]


def main_environment(namespace, clock, connect, event, handle=None):
  api = SimpleNamespace(get_token=Mock(side_effect=lambda: str(clock.wall)))
  params = Mock()
  params.get.return_value = "test-dongle"
  namespace.update(register_ui_lock_methods=Mock(), set_core_affinity=Mock(), Params=Mock(return_value=params),
                   UploadQueueCache=Mock(), upload_queue=Mock(), ATHENA_HOST="wss://test.invalid", Api=Mock(return_value=api),
                   cloudlog=Mock(), create_connection=connect, WebSocketException=WebSocketFailure,
                   cur_upload_items=Mock(), handle_long_poll=handle or (lambda ws, exit_event: event.set()))
  return api, params


def run_main(namespace, event):
  modules = {
    "openpilot.system.vehicle_telemetry.collector": SimpleNamespace(start_workers=Mock()),
    "openpilot.system.vehicle_telemetry.uploader": SimpleNamespace(connection_recovered=Mock()),
  }
  with patch.dict(sys.modules, modules):
    namespace["main"](event)


def test_main_refreshes_token_after_clock_step_preserving_retry_count_and_duration(retry_code):
  namespace, clock = retry_code
  event = FakeEvent(clock)
  attempts = []

  def connect(*args, **kwargs):
    attempts.append((clock.mono, kwargs["cookie"]))
    if len(attempts) == 1:
      clock.advance(2.0)
      clock.wall += 5_210_405.0
      raise WebSocketFailure("secret response body and cookie")
    return Mock()

  api, params = main_environment(namespace, clock, connect, event)
  namespace["backoff"] = Mock(return_value=63)
  run_main(namespace, event)

  assert attempts == [(100.0, "jwt=1000.0"), (102.0, "jwt=5211407.0")]
  assert api.get_token.call_count == 2
  assert event.waits == []
  assert [call.args[0] for call in namespace["backoff"].call_args_list] == [1, 0]
  params.remove.assert_called_once_with("LastAthenaPingTime")
  namespace["cloudlog"].event.assert_any_call("athenad.main.connected_ws", ws_uri="wss://test.invalid/ws/v2/test-dongle",
                                             retries=1, duration=2.0)
  namespace["cloudlog"].event.assert_any_call("athenad.main.connection_failed", error_type="WebSocketFailure", retries=1, http_status=401)
  assert "secret" not in str(namespace["cloudlog"].mock_calls)


def test_main_keeps_normal_backoff_after_first_connection(retry_code):
  namespace, clock = retry_code
  event = FakeEvent(clock)
  attempts = []

  def connect(*args, **kwargs):
    attempts.append(clock.mono)
    if len(attempts) == 2:
      clock.wall += 100_000.0
      raise WebSocketFailure("not logged")
    return Mock()

  def handle(ws, exit_event):
    if len(attempts) == 3:
      event.set()

  main_environment(namespace, clock, connect, event, handle)
  namespace["backoff"] = Mock(side_effect=lambda retries: 31 if retries else 0)
  run_main(namespace, event)
  assert attempts == [100.0, 100.0, 131.0]
  assert event.waits == [1.0] * 31
  assert [call.args[0] for call in namespace["backoff"].call_args_list] == [0, 1, 0]


@pytest.mark.parametrize("reconnect", [False, True], ids=["startup", "reconnect"])
@pytest.mark.parametrize("during_connect", [False, True], ids=["during_wait", "during_connect"])
def test_main_network_change_retries_once_then_resumes_backoff(retry_code, reconnect, during_connect):
  namespace, clock = retry_code
  sm = namespace["messaging"].SubMaster.return_value
  sm.publish(1 if reconnect else 0)
  attempts = []
  failed_attempt = 2 if reconnect else 1

  def change_network():
    sm.publish(4 if reconnect else 1)

  def on_wait(event):
    if not during_connect and len(event.waits) == 3:
      change_network()

  event = FakeEvent(clock, on_wait)

  def connect(*args, **kwargs):
    attempts.append(clock.mono)
    if len(attempts) == failed_attempt:
      if during_connect:
        clock.advance(2.0)
        change_network()
      raise WebSocketFailure("network unavailable")
    if len(attempts) == failed_attempt + 1:
      raise WebSocketFailure("same network still unavailable")
    return Mock()

  def handle(ws, exit_event):
    if len(attempts) > failed_attempt:
      event.set()

  main_environment(namespace, clock, connect, event, handle)
  namespace["backoff"] = Mock(side_effect=lambda retries: 31 if retries else 0)
  run_main(namespace, event)

  next_attempt = 102.0 if during_connect else 103.0
  expected = [100.0, next_attempt, next_attempt + 31.0]
  assert attempts == ([100.0] + expected if reconnect else expected)
  assert event.waits == [1.0] * (31 if during_connect else 34)
  retry_counts = [call.args[0] for call in namespace["backoff"].call_args_list]
  assert retry_counts == ([0, 1, 2, 0] if reconnect else [1, 2, 0])


def test_network_changes_do_not_interrupt_healthy_connection(retry_code):
  namespace, clock = retry_code
  sm = namespace["messaging"].SubMaster.return_value
  sm.publish(1)
  sm.update = Mock(wraps=sm.update)
  event = FakeEvent(clock)
  connect = Mock(return_value=Mock())

  def handle(ws, exit_event):
    sm.publish(4)
    clock.advance(63)
    assert sm.update.call_count == 1
    connect.assert_called_once()
    event.set()

  main_environment(namespace, clock, connect, event, handle)
  run_main(namespace, event)
  assert event.waits == []
  assert sm.update.call_count == 1


def test_failure_metadata_omits_noninteger_status(retry_code):
  namespace, clock = retry_code
  event = FakeEvent(clock)
  failure = WebSocketFailure("private body")
  failure.status_code = "private body"

  def connect(*args, **kwargs):
    event.set()
    raise failure

  main_environment(namespace, clock, connect, event)
  run_main(namespace, event)
  namespace["cloudlog"].event.assert_any_call("athenad.main.connection_failed", error_type="WebSocketFailure", retries=1, http_status=None)
  assert "private body" not in str(namespace["cloudlog"].mock_calls)
