"""Exercise real Sentry widgets and navigation without a window or device services."""

import errno
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.mark.parametrize("scenario", [
  "consent_cancel", "consent_confirm", "sensitivity", "warning", "save_failure", "legacy_high", "existing_high",
  "driver_exit", "driver_exit_failure", "waiting_for_door_open", "waiting_for_door_close", "door_signal_unavailable",
  "stale_door_status", "runtime_error",
])
def test_sentry_settings_navigation_and_persistence(tmp_path, scenario):
  # ui_state owns native messaging/Params singletons. Isolate its replacement in
  # a child interpreter instead of leaking fake modules into the UI test suite.
  result = subprocess.run(
    [sys.executable, str(Path(__file__).resolve()), scenario, str(tmp_path)],
    capture_output=True, text=True, timeout=30,
  )
  assert result.returncode == 0, result.stdout + result.stderr


def _exercise_scenario(scenario: str, temporary_root: Path) -> None:
  os.environ["SCALE"] = "1"  # Avoid creating a native window to inspect monitor scaling.
  os.environ["SENTRY_ROOT"] = str(temporary_root / "sentry")
  os.environ["PARAMS_ROOT"] = str(temporary_root / "legacy_params")

  import pyray as rl

  ui_state_module = ModuleType("openpilot.selfdrive.ui.ui_state")
  ui_state_module.ui_state = SimpleNamespace(is_offroad=lambda: True)
  ui_state_module.device = SimpleNamespace(awake=True)

  with patch.dict(sys.modules, {ui_state_module.__name__: ui_state_module}):
    from openpilot.selfdrive.ui.sunnypilot.mici.layouts import sentry
    from openpilot.system.sentryd.config import SentryConfigStore
    from openpilot.system.ui.lib.application import gui_app, MousePos
    from openpilot.system.ui.widgets.nav_widget import NavWidget
    from openpilot.system.ui.widgets.scroller import NavScroller

    def texture(_path, width, height, *args, **kwargs):
      return SimpleNamespace(id=0, width=width, height=height)

    with (patch.object(gui_app, "texture", side_effect=texture),
          patch.object(gui_app, "font", return_value=rl.Font()),
          patch.object(rl, "get_time", return_value=100.0),
          patch.object(sentry, "runtime_params", return_value=SimpleNamespace(get=lambda _key: None))):
      base = NavScroller()
      gui_app.push_widget(base)
      if scenario in ("legacy_high", "existing_high"):
        old_store = SentryConfigStore()
        old_store.initialize()
        (old_store.config_dir / "motion_threshold_mps2").write_text("0.01\n" if scenario == "legacy_high" else "0.02\n")
      panel = sentry.SentryLayoutMici()
      gui_app.push_widget(panel)
      enabled, sensitivity, warning = panel._scroller.items[:3]
      driver_exit = panel._scroller.items[3]
      store = SentryConfigStore()

      def click(widget):
        widget._handle_mouse_release(MousePos(10, 10))

      def finish_dismiss(widget):
        # Run the real navigation animation and its deferred callback without
        # rendering textures or advancing unrelated runtime status polls.
        for _ in range(300):
          NavWidget._update_state(widget)
          if widget not in gui_app._nav_stack:
            return
        raise AssertionError("navigation did not finish dismissing")

      assert panel.enabled is True
      assert not store.load().effective_enabled
      assert (store.config_dir / "enabled").read_text() == "0\n"
      assert driver_exit._checked
      assert store.load().wait_for_driver_exit
      assert (store.config_dir / "wait_for_driver_exit").read_text() == "1\n"

      if scenario in ("consent_cancel", "consent_confirm"):
        click(enabled)
        consent = gui_app._nav_stack[-1]
        assert isinstance(consent, sentry.SentryConsentLayoutMici)
        assert panel.enabled is False
        assert not enabled._checked
        assert not store.load().effective_enabled
        assert (store.config_dir / "capture_upload_consent_version").read_text() == "0\n"

        if scenario == "consent_cancel":
          gui_app.pop_widget()
          assert panel.enabled is True
          assert not store.load().effective_enabled
          # Opening again after pop exercises the framework's enabled writes.
          click(enabled)
          assert isinstance(gui_app._nav_stack[-1], sentry.SentryConsentLayoutMici)
          assert not store.load().effective_enabled
        else:
          click(consent._scroller.items[-1])
          confirmation = gui_app._nav_stack[-1]
          assert isinstance(confirmation, sentry.BigConfirmationDialog)
          assert not store.load().effective_enabled
          confirmation._slider._on_confirm()
          finish_dismiss(confirmation)
          assert gui_app._nav_stack[-1] is consent
          assert not store.load().effective_enabled
          finish_dismiss(consent)
          assert gui_app._nav_stack[-1] is panel
          assert panel.enabled is True
          assert store.load().effective_enabled
          assert enabled._checked
          assert (store.config_dir / "capture_upload_consent_version").read_text() == "1\n"
          assert (store.config_dir / "enabled").read_text() == "1\n"

          click(enabled)
          assert not store.load().effective_enabled
          click(enabled)
          assert gui_app._nav_stack[-1] is panel  # Existing consent needs no dialog.
          assert store.load().effective_enabled

      elif scenario in ("sensitivity", "warning"):
        toggle, field, expected = ((sensitivity, "motion_threshold_mps2", "0.08\n") if scenario == "sensitivity" else
                                   (warning, "warning_persistence_seconds", "2\n"))
        click(toggle)
        assert (store.config_dir / field).read_text() == expected
        # Settings panels are reused. Leaving and reopening must still preserve
        # the toggle object and reload the authoritative folder configuration.
        gui_app.pop_widget()
        gui_app.push_widget(panel)
        click(toggle)
        assert (store.config_dir / field).read_text() == ("0.02\n" if scenario == "sensitivity" else "5\n")

      elif scenario in ("legacy_high", "existing_high"):
        assert sensitivity.value == "high"
        assert panel.config_error is None
        assert (store.config_dir / "motion_threshold_mps2").read_text() == "0.02\n"
        click(sensitivity)
        assert sensitivity.value == "standard"
        assert (store.config_dir / "motion_threshold_mps2").read_text() == "0.04\n"

      elif scenario == "driver_exit":
        click(driver_exit)
        assert not driver_exit._checked
        assert not store.load().wait_for_driver_exit
        assert (store.config_dir / "wait_for_driver_exit").read_text() == "0\n"
        gui_app.pop_widget()
        # A reused panel reloads authoritative files, including changes made by
        # another local configuration writer while the settings page is hidden.
        store.set_wait_for_driver_exit(True)
        gui_app.push_widget(panel)
        assert driver_exit._checked
        click(driver_exit)
        assert not store.load().wait_for_driver_exit
        click(driver_exit)
        assert driver_exit._checked
        assert (store.config_dir / "wait_for_driver_exit").read_text() == "1\n"

      elif scenario == "driver_exit_failure":
        with patch("openpilot.system.sentryd.config.os.fsync", side_effect=OSError(errno.ENOSPC, "No space left on device")):
          click(driver_exit)
        error_dialog = gui_app._nav_stack[-1]
        assert isinstance(error_dialog, sentry.BigDialog)
        assert error_dialog._card.text == "Sentry setting was not saved"
        assert "No space left on device" in error_dialog._card.value
        assert driver_exit._checked
        assert store.load().wait_for_driver_exit
        assert (store.config_dir / "wait_for_driver_exit").read_text() == "1\n"
        gui_app.pop_widget()
        click(driver_exit)
        assert not driver_exit._checked
        assert not store.load().wait_for_driver_exit

      elif scenario in sentry.DRIVER_EXIT_STATUS or scenario in ("stale_door_status", "runtime_error"):
        state = scenario if scenario in sentry.DRIVER_EXIT_STATUS else "door_signal_unavailable" if scenario == "stale_door_status" else "error"
        updated = datetime.now(UTC) - timedelta(seconds=30 if scenario == "stale_door_status" else 0)
        status = {"state": state, "updated_at": updated.isoformat(),
                  "error": "No driver door CAN frames" if state in sentry.DRIVER_EXIT_STATUS else "Disk error"}
        with patch.object(sentry, "get_status", return_value=status):
          panel._refresh_status()
          click(panel._status)
        status_dialog = gui_app._nav_stack[-1]
        assert isinstance(status_dialog, sentry.BigDialog)
        if scenario == "stale_door_status":
          assert panel._status.value == "daemon unavailable"
          assert "status is stale" in status_dialog._card.value
        elif scenario == "runtime_error":
          assert panel._status.value == "daemon unavailable"
          assert "Disk error" in status_dialog._card.value
        else:
          assert panel._status.value == scenario.replace("_", " ")
          assert "90-second" in status_dialog._card.value
          assert "Restart the device" not in status_dialog._card.value
          if scenario == "door_signal_unavailable":
            assert "USB power" in status_dialog._card.value
            assert "Turn off wait for driver exit" in status_dialog._card.value

      elif scenario == "save_failure":
        with patch("openpilot.system.sentryd.config.os.fsync", side_effect=OSError(errno.ENOSPC, "No space left on device")):
          click(sensitivity)
        error_dialog = gui_app._nav_stack[-1]
        assert isinstance(error_dialog, sentry.BigDialog)
        assert error_dialog._card.text == "Sentry setting was not saved"
        assert "No space left on device" in error_dialog._card.value
        assert panel.enabled is False
        assert sensitivity.value == "standard"
        assert store.load().motion_threshold_mps2 == 0.04
        assert (store.config_dir / "motion_threshold_mps2").read_text() == "0.04\n"

        gui_app.pop_widget()
        assert panel.enabled is True
        click(sensitivity)
        assert gui_app._nav_stack[-1] is panel
        assert sensitivity.value == "low"
        assert store.load().motion_threshold_mps2 == 0.08
      else:
        raise AssertionError(f"unknown scenario: {scenario}")


if __name__ == "__main__":
  _exercise_scenario(sys.argv[1], Path(sys.argv[2]))
