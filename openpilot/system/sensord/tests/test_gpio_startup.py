import errno
import importlib.util
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from openpilot import cereal


@pytest.fixture
def sensord(monkeypatch):
  """Load the production worker with host substitutes for IPC and scheduling."""
  messaging = types.ModuleType("openpilot.cereal.messaging")
  messaging.PubMaster = Mock()
  messaging.new_message = Mock(side_effect=lambda service, **kwargs: types.SimpleNamespace(**kwargs))
  realtime = types.ModuleType("openpilot.common.realtime")
  realtime.config_realtime_process = Mock()
  realtime.Ratekeeper = Mock()
  swaglog = types.ModuleType("openpilot.common.swaglog")
  swaglog.cloudlog = Mock()

  source = Path(__file__).parents[1] / "sensord.py"
  spec = importlib.util.spec_from_file_location("_sensord_gpio_startup", source)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  with monkeypatch.context() as context:
    for dependency in (messaging, realtime, swaglog):
      context.setitem(sys.modules, dependency.__name__, dependency)
    context.setattr(cereal, "messaging", messaging, raising=False)
    spec.loader.exec_module(module)

  # Do not monkeypatch global os/time functions used by pytest or other tests.
  module.os = types.SimpleNamespace(path=types.SimpleNamespace(exists=lambda _: False), read=Mock(), close=Mock())
  module.time = types.SimpleNamespace(time_ns=lambda: 2_000_000_000, monotonic_ns=lambda: 1_000_000_000)
  module.select = types.SimpleNamespace(poll=Mock(), POLLIN=1, POLLPRI=2)
  module.sudo_write = Mock()
  module.gpiochip_get_ro_value_fd = Mock(return_value=41)
  return module


@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "GPIO not ready"), FileNotFoundError(errno.ENOENT, "GPIO missing")])
def test_gpio_startup_recovers_after_permissions_or_device_become_ready(sensord, error):
  event = Mock()
  event.is_set.return_value = False
  event.wait.return_value = False
  sensord.gpiochip_get_ro_value_fd.side_effect = [error, 41]

  assert sensord.wait_for_gpio(event) == 41
  assert sensord.gpiochip_get_ro_value_fd.call_count == 2
  sensord.gpiochip_get_ro_value_fd.assert_called_with("sensord", 0, 84)
  event.wait.assert_called_once_with(0.1)


def test_gpio_retry_backoff_is_capped(sensord):
  event = Mock()
  event.is_set.return_value = False
  event.wait.return_value = False
  sensord.gpiochip_get_ro_value_fd.side_effect = [PermissionError("not ready")] * 10 + [41]

  assert sensord.wait_for_gpio(event) == 41
  delays = [call.args[0] for call in event.wait.call_args_list]
  assert delays == pytest.approx([0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0, 5.0, 5.0])


def test_gpio_is_not_opened_after_shutdown(sensord):
  event = threading.Event()
  event.set()
  sensord.interrupt_loop([], event)
  sensord.gpiochip_get_ro_value_fd.assert_not_called()
  sensord.select.poll.assert_not_called()
  sensord.os.close.assert_not_called()


def test_shutdown_interrupts_gpio_retry(sensord):
  event = Mock()
  event.is_set.return_value = False
  event.wait.return_value = True
  sensord.gpiochip_get_ro_value_fd.side_effect = PermissionError("not ready")

  assert sensord.wait_for_gpio(event) is None
  sensord.gpiochip_get_ro_value_fd.assert_called_once()
  event.wait.assert_called_once_with(0.1)


def test_shutdown_between_attempts_prevents_another_open(sensord):
  event = Mock()
  event.is_set.side_effect = [False, True]
  event.wait.return_value = False
  sensord.gpiochip_get_ro_value_fd.side_effect = PermissionError("not ready")

  assert sensord.wait_for_gpio(event) is None
  sensord.gpiochip_get_ro_value_fd.assert_called_once()


def test_unrelated_gpio_errors_are_not_retried(sensord):
  event = Mock()
  event.is_set.return_value = False
  sensord.gpiochip_get_ro_value_fd.side_effect = OSError(errno.EBUSY, "line already owned")

  with pytest.raises(OSError, match="line already owned"):
    sensord.wait_for_gpio(event)
  event.wait.assert_not_called()


def test_interrupt_worker_publishes_after_gpio_permission_recovery(sensord):
  event = threading.Event()
  event.wait = Mock(return_value=False)
  sensord.gpiochip_get_ro_value_fd.side_effect = [PermissionError("not ready"), 41]
  sensor = Mock()
  sensor.is_data_valid.return_value = True
  services = ["accelerometer", "gyroscope"]
  sensord.os.read.return_value = bytes(sensord.gpioevent_data(2_000_000_001, 1))

  def poll(timeout):
    assert timeout == 100
    event.set()
    return [(41, sensord.select.POLLIN)]

  sensord.select.poll.return_value.poll.side_effect = poll
  sensord.interrupt_loop([(sensor, service, True) for service in services], event)

  sensord.messaging.PubMaster.assert_called_once_with(services)
  sent = sensord.messaging.PubMaster.return_value.send.call_args_list
  assert [call.args[0] for call in sent] == services
  for call in sent:
    service, message = call.args
    assert message.valid
    assert getattr(message, service) is sensor.get_event.return_value
  sensor.get_event.assert_called_with(1_000_000_001)
  sensord.os.close.assert_called_once_with(41)


@pytest.mark.parametrize("failure_at", ["affinity", "register", "poll", "read"])
def test_interrupt_descriptor_closes_if_setup_or_read_fails(sensord, failure_at):
  event = threading.Event()
  sensord.select.poll.return_value.poll.return_value = [(41, sensord.select.POLLIN)]
  error = OSError("interrupt failure")
  if failure_at == "affinity":
    sensord.os.path.exists = lambda _: True
    sensord.sudo_write.side_effect = error
  elif failure_at == "register":
    sensord.select.poll.return_value.register.side_effect = error
  elif failure_at == "poll":
    sensord.select.poll.return_value.poll.side_effect = error
  else:
    sensord.os.read.side_effect = error

  with pytest.raises(OSError, match="interrupt failure"):
    sensord.interrupt_loop([], event)
  sensord.os.close.assert_called_once_with(41)
