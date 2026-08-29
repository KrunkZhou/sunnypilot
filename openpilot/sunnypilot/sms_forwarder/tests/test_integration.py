from pathlib import Path

from openpilot.sunnypilot.sms_forwarder import is_supported_device


def test_process_is_on_by_default_for_mici_only() -> None:
  assert is_supported_device("mici")
  assert not is_supported_device("tizi")
  assert not is_supported_device("pc")


def test_single_release_integration_hook_and_no_toggle_or_param() -> None:
  root = Path(__file__).parents[4]
  params = (root / "openpilot/common/params_keys.h").read_text()
  network = (root / "openpilot/selfdrive/ui/mici/layouts/settings/network/network_layout.py").read_text()
  processes = (root / "openpilot/system/manager/process_config.py").read_text()
  assert "ForwardSIMMessages" not in params
  assert "ForwardSIMMessages" not in network
  assert "ForwardSIMMessages" not in processes
  assert 'PythonProcess("sms_forwarder", "openpilot.sunnypilot.sms_forwarder.__main__"' in processes
