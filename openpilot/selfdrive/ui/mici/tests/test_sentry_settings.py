"""Exercise real Sentry widgets and navigation without a window or device services."""

import errno
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest


@pytest.mark.parametrize("scenario", [
  "consent_cancel", "consent_confirm", "sensitivity", "fixed_confirmation", "save_failure", "legacy_high", "existing_high",
  "driver_exit", "driver_exit_failure", "waiting_for_door_open", "waiting_for_door_close", "door_signal_unavailable",
  "stale_door_status", "runtime_error",
  "queue_retry", "retry_all", "retry_failure", "retry_onroad",
  "confirmation_progress", "door_pause_open", "door_pause_countdown", "door_pause_error", "door_pause_unknown", "invalid_progress",
  "stale_confirmation", "consent_details", "legacy_warning",
  "flash_toggle", "flash_failure", "flash_postreplace_failure", "flash_onroad", "flash_help", "flash_config_error",
  "flash_idle", "flash_classifying", "flash_unlock_paused", "flash_waiting_for_doors", "flash_arming", "flash_armed",
  "flash_armed_unlocked", "flash_armed_unknown",
  "flash_unavailable", "flash_critical", "flash_stale", "flash_invalid_progress", "flash_invalid_countdown",
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

    runtime_writes = []
    volatile_params = SimpleNamespace(
      get=lambda _key: None,
      put=lambda key, value, block=False: runtime_writes.append((key, value, block)),
    )
    with (patch.object(gui_app, "texture", side_effect=texture),
          patch.object(gui_app, "font", return_value=rl.Font()),
          patch.object(rl, "get_time", return_value=100.0),
          patch.object(sentry, "runtime_params", return_value=volatile_params)):
      base = NavScroller()
      gui_app.push_widget(base)
      if scenario in ("legacy_high", "existing_high"):
        old_store = SentryConfigStore()
        old_store.initialize()
        (old_store.config_dir / "motion_threshold_mps2").write_text("0.01\n" if scenario == "legacy_high" else "0.02\n")
      elif scenario == "legacy_warning":
        old_store = SentryConfigStore()
        old_store.initialize()
        (old_store.config_dir / "schema_version").write_text("2\n")
        (old_store.config_dir / "warning_persistence_seconds").write_text("5\n")
      panel = sentry.SentryLayoutMici()
      gui_app.push_widget(panel)
      enabled, sensitivity, confirmation_info = panel._scroller.items[:3]
      driver_exit = panel._scroller.items[3]
      flash_toggle = panel._infer_lock_from_flashes
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
      assert store.load().schema_version == 4
      assert (store.config_dir / "warning_persistence_seconds").read_text() == "1\n"
      assert confirmation_info.value == "10 motion samples"
      assert not isinstance(confirmation_info, sentry.BigMultiToggle)
      assert not flash_toggle._checked and not store.load().infer_lock_from_flashes
      assert flash_toggle.text == "Lock/unlock flash detection"
      assert (store.config_dir / "infer_lock_from_flashes").read_text() == "0\n"

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

      elif scenario == "sensitivity":
        click(sensitivity)
        assert (store.config_dir / "motion_threshold_mps2").read_text() == "0.08\n"
        # Settings panels are reused. Leaving and reopening must still preserve
        # the toggle object and reload the authoritative folder configuration.
        gui_app.pop_widget()
        gui_app.push_widget(panel)
        click(sensitivity)
        assert (store.config_dir / "motion_threshold_mps2").read_text() == "0.02\n"

      elif scenario in ("fixed_confirmation", "legacy_warning"):
        config_before = {path.name: path.read_bytes() for path in store.config_dir.iterdir()}
        click(confirmation_info)
        dialog = gui_app._nav_stack[-1]
        assert isinstance(dialog, sentry.BigDialog)
        assert "10 qualifying motion samples within 60 seconds" in dialog._card.value
        assert "without a webhook alert" in dialog._card.value
        assert "closed for 5 minutes" in dialog._card.value
        gui_app.pop_widget()
        gui_app.pop_widget()
        gui_app.push_widget(panel)
        assert confirmation_info.value == "10 motion samples"
        assert {path.name: path.read_bytes() for path in store.config_dir.iterdir()} == config_before
        assert runtime_writes == []

      elif scenario == "consent_details":
        click(enabled)
        consent = gui_app._nav_stack[-1]
        cards = {item.text: item.value for item in consent._scroller.items if isinstance(item, sentry.GreyBigButton)}
        assert "10 motion samples within 60 seconds" in cards["motion confirmation"]
        assert "unless a door opens" in cards["unconfirmed photos"]
        assert "pending first pair" in cards["silent door photos"]
        assert "Both upload without an alert" in cards["silent door photos"]
        assert "5 minutes" in cards["door pause"]
        assert not store.load().effective_enabled

      elif scenario in ("confirmation_progress", "door_pause_open", "door_pause_countdown", "door_pause_error", "door_pause_unknown", "invalid_progress",
                        "stale_confirmation"):
        config_before = {path.name: path.read_bytes() for path in store.config_dir.iterdir()}
        state = "door_paused" if scenario.startswith("door_pause") else "confirming"
        updated = datetime.now(UTC) - timedelta(seconds=30 if scenario == "stale_confirmation" else 0)
        status = {"state": state, "updated_at": updated.isoformat(),
                  "confirmation": {"hits": 4, "required_hits": 10, "seconds_remaining": 32.2},
                  "door_pause": {"active": True, "waiting_for_close": scenario == "door_pause_open",
                                 "seconds_remaining": None if scenario == "door_pause_open" else 299.2}}
        if scenario == "invalid_progress":
          status["confirmation"] = {"hits": "bad", "required_hits": 10, "seconds_remaining": float("nan")}
        if scenario == "door_pause_error":
          status["error"] = "Door receiver disconnected. Check the vehicle connection."
        if scenario == "door_pause_unknown":
          status["door_pause"] = {"active": True, "waiting_for_close": True, "waiting_for_status": True, "seconds_remaining": None}
        with patch.object(sentry, "get_status", return_value=status):
          panel._refresh_status()
          click(panel._status)
        dialog = gui_app._nav_stack[-1]
        assert isinstance(dialog, sentry.BigDialog)
        if scenario == "stale_confirmation":
          assert panel._status.value == "daemon unavailable"
          assert confirmation_info.value == "10 motion samples"
          assert "status is stale" in dialog._card.value
        elif scenario == "invalid_progress":
          assert confirmation_info.value == "10 motion samples"
        elif scenario.startswith("door_pause"):
          expected = "waiting for doors closed" if scenario == "door_pause_open" else "door pause · 300 sec left"
          if scenario == "door_pause_unknown":
            expected = "waiting for door status"
          assert panel._status.value == expected
          assert "without a webhook alert" in dialog._card.value
          assert "Earlier confirmed events" in dialog._card.value
          if scenario == "door_pause_error":
            assert status["error"] in dialog._card.value
            assert "Restart the device" not in dialog._card.value
        else:
          assert panel._status.value == "confirming motion"
          assert confirmation_info.value == "4/10 · 33 sec left"
          assert "4/10 · 33 sec left" in dialog._card.value
        assert {path.name: path.read_bytes() for path in store.config_dir.iterdir()} == config_before
        assert runtime_writes == []

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

      elif scenario == "flash_toggle":
        preserved = {path.name: path.read_bytes() for path in store.config_dir.iterdir() if path.name != "infer_lock_from_flashes"}
        click(flash_toggle)
        assert flash_toggle._checked and store.load().infer_lock_from_flashes
        assert (store.config_dir / "infer_lock_from_flashes").read_text() == "1\n"
        gui_app.pop_widget()
        store.set_infer_lock_from_flashes(False)
        gui_app.push_widget(panel)
        assert not flash_toggle._checked
        click(flash_toggle)
        click(flash_toggle)
        assert not store.load().infer_lock_from_flashes
        assert {name: (store.config_dir / name).read_bytes() for name in preserved} == preserved
        assert runtime_writes == []

      elif scenario in ("flash_failure", "flash_postreplace_failure"):
        if scenario == "flash_failure":
          with patch("openpilot.system.sentryd.config.os.fsync", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            click(flash_toggle)
          expected = False
        else:
          with patch.object(panel.store, "_fsync_directory", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            click(flash_toggle)
          expected = True  # replace already committed: show the authoritative file, not a guessed rollback.
        error_dialog = gui_app._nav_stack[-1]
        assert isinstance(error_dialog, sentry.BigDialog)
        assert error_dialog._card.text == "Sentry setting was not saved"
        assert "No space left on device" in error_dialog._card.value
        assert flash_toggle._checked is expected and store.load().infer_lock_from_flashes is expected
        assert runtime_writes == []
        gui_app.pop_widget()
        assert panel.enabled
        click(flash_toggle)
        assert store.load().infer_lock_from_flashes is not expected

      elif scenario == "flash_onroad":
        before = {path.name: path.read_bytes() for path in store.config_dir.iterdir()}
        ui_state_module.ui_state.is_offroad = lambda: False
        assert not flash_toggle.enabled
        click(flash_toggle)  # Deliberately invoke touch dispatch after ignition changed.
        assert not flash_toggle._checked and not store.load().infer_lock_from_flashes
        assert {path.name: path.read_bytes() for path in store.config_dir.iterdir()} == before
        assert runtime_writes == [] and gui_app._nav_stack[-1] is panel
        ui_state_module.ui_state.is_offroad = lambda: True
        assert flash_toggle.enabled
        click(flash_toggle)
        assert store.load().infer_lock_from_flashes

      elif scenario == "flash_config_error":
        field = store.config_dir / "infer_lock_from_flashes"
        field.write_text("invalid\n")
        status = {"state": "unlock_paused", "updated_at": datetime.now(UTC).isoformat(),
                  "lock_detection": {"enabled": True, "state": "unlock_paused", "pause_seconds_remaining": 3000}}
        with patch.object(sentry, "get_status", return_value=status):
          panel._refresh_controls()
          assert panel._status.value == "configuration error" and not flash_toggle.enabled
          click(panel._lock_status)
        dialog = gui_app._nav_stack[-1]
        assert "infer_lock_from_flashes must be 0 or 1" in dialog._card.value
        assert "quarantine invalid configuration" in dialog._card.value
        assert "heuristic" not in dialog._card.value
        assert field.read_text() == "invalid\n" and runtime_writes == []

      elif scenario.startswith("flash_"):
        if scenario != "flash_help":
          store.set_infer_lock_from_flashes(True)
          panel._refresh_controls()
        before = {path.name: path.read_bytes() for path in store.config_dir.iterdir()}
        lock_state = {
          "flash_help": "disabled", "flash_idle": "idle", "flash_classifying": "classifying",
          "flash_unlock_paused": "unlock_paused", "flash_waiting_for_doors": "waiting_for_doors",
          "flash_arming": "arming", "flash_armed": "armed", "flash_unavailable": "unavailable",
          "flash_armed_unlocked": "armed", "flash_armed_unknown": "armed",
          "flash_critical": "unlock_paused", "flash_stale": "classifying", "flash_invalid_progress": "classifying",
          "flash_invalid_countdown": "unlock_paused",
        }[scenario]
        state = {"classifying": "flash_classifying", "unlock_paused": "unlock_paused",
                 "waiting_for_doors": "lock_waiting_for_doors"}.get(lock_state, "arming")
        status = {"state": state, "updated_at": datetime.now(UTC).isoformat(),
                  "lock_detection": {"enabled": scenario != "flash_help", "state": lock_state,
                                     "inferred_state": "unlocked" if lock_state == "waiting_for_doors" else
                                                       "locked" if lock_state in ("arming", "armed") else None,
                                     "pulse_count": 1, "pause_seconds_remaining": 89.2 if lock_state == "arming" else 3599.2,
                                     "error": None}}
        if scenario == "flash_unavailable":
          status["lock_detection"]["error"] = "Validated Audi MQB indicator data unavailable. Check the saved vehicle profile."
        elif scenario in ("flash_armed_unlocked", "flash_armed_unknown"):
          status["lock_detection"]["inferred_state"] = "unlocked" if scenario == "flash_armed_unlocked" else None
        elif scenario == "flash_critical":
          status["error"] = "Outbox disk failure"
        elif scenario == "flash_stale":
          status["updated_at"] = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        elif scenario == "flash_invalid_progress":
          status["lock_detection"]["pulse_count"] = "invalid"
        elif scenario == "flash_invalid_countdown":
          status["lock_detection"]["pause_seconds_remaining"] = float("nan")
        with patch.object(sentry, "get_status", return_value=status):
          panel._refresh_status()
          click(panel._lock_status)
          dialog = gui_app._nav_stack[-1]
          assert isinstance(dialog, sentry.BigDialog)
          if scenario in ("flash_critical", "flash_stale"):
            assert panel._status.value == "daemon unavailable" and panel._lock_status.value == "status unavailable"
            assert ("Outbox disk failure" if scenario == "flash_critical" else "status is stale") in dialog._card.value
            assert "heuristic" not in dialog._card.value
          else:
            expected = {
              "flash_help": "disabled", "flash_idle": "waiting for flashes", "flash_classifying": "classifying · 1 pulse",
              "flash_unlock_paused": "unlock pause · 60 min left", "flash_waiting_for_doors": "lock · waiting for doors",
              "flash_arming": "lock arming · 90 sec left", "flash_armed": "inferred locked · armed",
              "flash_armed_unlocked": "inferred unlocked · armed", "flash_armed_unknown": "armed · waiting for flashes",
              "flash_unavailable": "flash signal unavailable", "flash_invalid_progress": "classifying flashes",
              "flash_invalid_countdown": "unlock pause",
            }[scenario]
            assert panel._lock_status.value == expected
            if state in sentry.LOCK_DETECTION_STATES:
              assert panel._status.value == expected
            assert "heuristic, not a verified door-lock signal" in dialog._card.value
            assert "validated Audi MQB CAN profile" in dialog._card.value
            assert "starts 90 seconds of arming immediately, without checking door closure" in dialog._card.value
            assert "bypasses the driver-exit and five-minute door waits" in dialog._card.value
            assert "Open doors or loss of the door receiver do not reset this lock timer" in dialog._card.value
            assert "all automatic motion and door captures for 60 minutes" in dialog._card.value
            assert "After that hour, all doors and the trunk must be observed closed before a new 90-second arming timer" in dialog._card.value
            assert "reopening resets that timer" in dialog._card.value
            assert "Hazard flashes are ignored" in dialog._card.value
            assert "restarts the full 60 minutes" in dialog._card.value
            assert "Restarting during lock-triggered arming starts a fresh 90 seconds without checking doors" in dialog._card.value
            assert "Manual tests and queued uploads continue" in dialog._card.value
            if scenario == "flash_unavailable":
              assert status["lock_detection"]["error"] in dialog._card.value
              assert panel._status.value == "arming"  # Heuristic errors do not mask other detector state.
          gui_app.pop_widget()
          click(panel._status)
          if scenario not in ("flash_help", "flash_critical", "flash_stale"):
            assert "heuristic, not a verified door-lock signal" in gui_app._nav_stack[-1]._card.value
        assert {path.name: path.read_bytes() for path in store.config_dir.iterdir()} == before
        assert runtime_writes == []

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

      elif scenario in ("queue_retry", "retry_all", "retry_failure", "retry_onroad"):
        config_before = {path.name: path.read_bytes() for path in store.config_dir.iterdir()}
        assert panel._queue.enabled
        assert panel._retry.enabled
        assert panel._retry.text == "retry all uploads"

        if scenario == "retry_onroad":
          ui_state_module.ui_state.is_offroad = lambda: False
          assert not panel._queue.enabled
          assert not panel._retry.enabled
          # Invoke the real callbacks even though normal touch dispatch is
          # disabled, exercising ignition changes during an existing touch.
          click(panel._queue)
          click(panel._retry)
          assert runtime_writes == []
          assert gui_app._nav_stack[-1] is panel
          ui_state_module.ui_state.is_offroad = lambda: True
          click(panel._queue)
        elif scenario == "retry_failure":
          with patch.object(volatile_params, "put", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            click(panel._queue)
          assert runtime_writes == []
          error_dialog = gui_app._nav_stack[-1]
          assert isinstance(error_dialog, sentry.BigDialog)
          assert error_dialog._card.text == "Sentry command failed"
          assert "No space left on device" in error_dialog._card.value
          gui_app.pop_widget()
          assert panel.enabled is True
          click(panel._retry)
        else:
          click(panel._queue if scenario == "queue_retry" else panel._retry)

        assert len(runtime_writes) == 1
        key, command, block = runtime_writes[0]
        assert key == "SentryRuntimeCommand"
        assert command["command"] == "retry_uploads"
        assert UUID(command["request_id"]).version == 4
        assert set(command) == {"command", "request_id"}
        assert block is True
        assert gui_app._nav_stack[-1] is panel
        assert {path.name: path.read_bytes() for path in store.config_dir.iterdir()} == config_before
        assert not store.load().effective_enabled

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
