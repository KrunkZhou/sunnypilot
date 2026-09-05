import errno
import os
from unittest.mock import patch

import pytest

from openpilot.common import gpio


@pytest.mark.parametrize("label", ["sensord", "sensor" * 8])
def test_gpiochip_event_fd_preserves_request_and_caller_ownership(label):
  chip_fd, event_fd = 20, 21

  def ioctl(fd, operation, request):
    assert fd == chip_fd
    assert operation == 0xc030b404
    assert request.lineoffset == 84
    assert request.handleflags == 0x1
    assert request.eventflags == 0x3
    assert request.label == label.encode("utf-8")[:31]
    request.fd = event_fd

  with patch.object(gpio.os, "open", return_value=chip_fd) as open_mock, \
       patch.object(gpio.fcntl, "ioctl", side_effect=ioctl) as ioctl_mock, \
       patch.object(gpio.os, "close") as close_mock:
    assert gpio.gpiochip_get_ro_value_fd(label, 3, 84) == event_fd
    open_mock.assert_called_once_with("/dev/gpiochip3", os.O_RDONLY)
    ioctl_mock.assert_called_once()
    close_mock.assert_called_once_with(chip_fd)


@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "denied"), OSError(errno.EBUSY, "busy")])
def test_gpiochip_event_fd_closes_chip_on_ioctl_failure(error):
  with patch.object(gpio.os, "open", return_value=20), \
       patch.object(gpio.fcntl, "ioctl", side_effect=error), \
       patch.object(gpio.os, "close") as close_mock:
    with pytest.raises(type(error)) as raised:
      gpio.gpiochip_get_ro_value_fd("sensord", 0, 84)
    assert raised.value is error
    close_mock.assert_called_once_with(20)


def test_gpiochip_event_fd_open_failure_has_no_descriptor_to_close():
  error = PermissionError(errno.EACCES, "denied")
  with patch.object(gpio.os, "open", side_effect=error), \
       patch.object(gpio.fcntl, "ioctl") as ioctl_mock, \
       patch.object(gpio.os, "close") as close_mock:
    with pytest.raises(PermissionError) as raised:
      gpio.gpiochip_get_ro_value_fd("sensord", 0, 84)
    assert raised.value is error
    ioctl_mock.assert_not_called()
    close_mock.assert_not_called()
