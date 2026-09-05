from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pyray as rl

from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigMultiToggle, BigToggle, GreyBigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigConfirmationCircleButton, BigConfirmationDialog, BigDialog
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.sentryd.config import (
  CURRENT_CONSENT_VERSION,
  SENSITIVITY_TO_THRESHOLD,
  SentryConfig,
  SentryConfigError,
  SentryConfigStore,
)
from openpilot.system.sentryd.runtime import get_status, runtime_params
from openpilot.system.sentryd.store import read_outbox_stats
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets.scroller import NavScroller


WARNING_TO_SECONDS = {"0.5 sec": 0.5, "1 sec": 1.0, "2 sec": 2.0, "5 sec": 5.0}
STATUS_STALE_SECONDS = 15.0
DRIVER_EXIT_STATUS = {
  "waiting_for_door_open": (
    "waiting for door open",
    "Ignition is off. Open the driver's door, then close it after exiting. The 90-second arming timer starts when that door closes.",
  ),
  "waiting_for_door_close": (
    "waiting for door close",
    "The driver's door was opened after ignition switched off. Close it to start the 90-second arming timer.",
  ),
  "door_signal_unavailable": (
    "door signal unavailable",
    "A fresh driver's-door signal is unavailable. This can happen on USB power or with an unsupported vehicle. " +
    "Sentry will wait and will not arm automatically. Turn off wait for driver exit to use the normal 90-second offroad arming timer.",
  ),
}


class SentryConsentLayoutMici(NavScroller):
  def __init__(self, icon: rl.Texture, confirm_callback):
    super().__init__()
    accept = BigConfirmationCircleButton(
      "slide to enable sentry mode", icon, lambda: self.dismiss(confirm_callback), red=True,
    )
    self._scroller.add_widgets([
      GreyBigButton("Sentry Mode", "review before enabling", icon),
      GreyBigButton("wait for driver exit", "On by default: after ignition turns off, open and close the driver's door to start the 90-second arming timer."),
      GreyBigButton("parked camera capture", "Continuing motion captures wide-road and cabin photos with a one-second minimum interval."),
      GreyBigButton("capture limit", "First capture plus up to 20 more revisions per episode, then 90 seconds to rearm."),
      GreyBigButton("alert frequency", "Only the first capture in each motion episode sends a webhook alert."),
      GreyBigButton("upload to your RTZ server", "Captures wait securely on this device while offline and upload when connectivity returns."),
      GreyBigButton("privacy", "Anyone with owner or administrator access to your RTZ server can view retained Sentry captures."),
      GreyBigButton("your choice", "Disable Sentry Mode at any time. Disabling does not erase already queued events."),
      accept,
    ])


class SentryLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    self.store = SentryConfigStore()
    self.volatile_params = runtime_params()
    self.config = SentryConfig()
    self.config_error: str | None = None
    try:
      self.config = self.store.initialize()
    except SentryConfigError as exc:
      self.config_error = str(exc)

    self.icon = gui_app.texture("icons_mici/settings/device/cameras.png", 64, 64)
    self._enable_toggle = BigToggle("parked Sentry Mode", "wide + cabin", self.config.effective_enabled, self._on_enabled)
    self._sensitivity = BigMultiToggle("motion sensitivity", list(SENSITIVITY_TO_THRESHOLD), select_callback=self._on_sensitivity)
    self._warning = BigMultiToggle("warning status delay", list(WARNING_TO_SECONDS), select_callback=self._on_warning)
    self._wait_for_driver_exit = BigToggle("wait for driver exit", "door open + close before arming",
                                         self.config.wait_for_driver_exit, self._on_wait_for_driver_exit)
    self._status = BigButton("Sentry status", "starting", self.icon)
    self._status.set_click_callback(self._show_status)
    self._queue = BigButton("upload queue", "0 pending")
    self._manual_test = BigButton("test Sentry alert", "capture + upload", self.icon)
    self._manual_test.set_click_callback(lambda: self._send_command("manual_test"))
    self._manual_test.set_enabled(lambda: ui_state.is_offroad() and self.config.effective_enabled and self.config_error is None)
    self._retry = BigButton("retry failed uploads", "manual retry")
    self._retry.set_click_callback(lambda: self._send_command("retry_uploads"))
    self._reset = BigButton("reset Sentry settings", "keeps queued captures")
    self._reset.set_click_callback(self._confirm_reset)

    self._scroller.add_widgets([
      self._enable_toggle,
      self._sensitivity,
      self._warning,
      self._wait_for_driver_exit,
      self._status,
      self._queue,
      self._manual_test,
      self._retry,
      self._reset,
    ])
    self._last_refresh = float("-inf")
    self._refresh_controls()

  def show_event(self):
    super().show_event()
    self._refresh_controls()

  def _update_state(self):
    super()._update_state()
    if rl.get_time() - self._last_refresh >= 1.0:
      self._last_refresh = rl.get_time()
      self._refresh_status()

  def _load(self) -> SentryConfig:
    try:
      self.config = self.store.load()
      self.config_error = None
    except SentryConfigError as exc:
      self.config = SentryConfig()
      self.config_error = str(exc)
    return self.config

  def _refresh_controls(self) -> None:
    config = self._load()
    self._enable_toggle.set_checked(config.effective_enabled)
    self._sensitivity.set_value(next(
      name for name, threshold in SENSITIVITY_TO_THRESHOLD.items() if threshold == config.motion_threshold_mps2))
    self._warning.set_value(next(
      name for name, seconds in WARNING_TO_SECONDS.items() if seconds == config.warning_persistence_seconds))
    self._wait_for_driver_exit.set_checked(config.wait_for_driver_exit)
    self._refresh_status()

  def _refresh_status(self) -> None:
    status = get_status(self.volatile_params) or {}
    state_key = str(status.get("state", "unavailable"))
    state = DRIVER_EXIT_STATUS[state_key][0] if state_key in DRIVER_EXIT_STATUS else state_key.replace("_", " ")
    if not ui_state.is_offroad():
      state = "disabled"
    elif self.config_error:
      state = "configuration error"
    elif self._runtime_status_error(status):
      state = "daemon unavailable"
    self._status.set_value(state)
    try:
      stats = read_outbox_stats()
      queued = f"{stats.pending} pending / {stats.media_bytes / (1024 * 1024):.1f} MB"
      if stats.terminal:
        queued += f" / {stats.terminal} failed"
      self._queue.set_value(queued)
    except Exception:
      self._queue.set_value("queue unavailable")

  def _on_enabled(self, enabled: bool) -> None:
    if not enabled:
      self._write_config(lambda: self.store.set_enabled(False))
      return
    config = self._load()
    if config.capture_upload_consent_version == CURRENT_CONSENT_VERSION:
      self._write_config(lambda: self.store.set_enabled(True))
      return
    self._enable_toggle.set_checked(False)

    def consented() -> None:
      self._write_config(self.store.enable_with_consent)

    gui_app.push_widget(SentryConsentLayoutMici(self.icon, consented))

  def _on_sensitivity(self, value: str) -> None:
    self._write_config(lambda: self.store.set_motion_threshold(SENSITIVITY_TO_THRESHOLD[value]))

  def _on_warning(self, value: str) -> None:
    self._write_config(lambda: self.store.set_warning_persistence(WARNING_TO_SECONDS[value]))

  def _on_wait_for_driver_exit(self, enabled: bool) -> None:
    self._write_config(lambda: self.store.set_wait_for_driver_exit(enabled))

  def _write_config(self, operation) -> None:
    try:
      self.config = operation()
      self.config_error = None
    except (OSError, SentryConfigError, ValueError) as exc:
      error = str(exc)
      self.config_error = error
      self._refresh_controls()
      gui_app.push_widget(BigDialog("Sentry setting was not saved", error))
      return
    self._refresh_controls()

  def _send_command(self, command: str) -> None:
    try:
      self.volatile_params.put("SentryRuntimeCommand", {
        "command": command,
        "request_id": str(uuid4()),
      }, block=True)
    except (OSError, RuntimeError) as exc:
      gui_app.push_widget(BigDialog("Sentry command failed", str(exc)))

  def _show_status(self) -> None:
    status = get_status(self.volatile_params) or {}
    runtime_error = self._runtime_status_error(status)
    if not ui_state.is_offroad():
      description = "Sentry detection is disabled while ignition is on. Pending delivery resumes after parking."
    elif self.config_error:
      reset_guidance = "Use reset Sentry settings to quarantine invalid configuration and restore safe defaults."
      description = f"{self.config_error}\n\n{reset_guidance}"
    elif runtime_error:
      description = f"{runtime_error}\n\nRestart the device or inspect sentryd logs if the problem continues."
    elif status.get("state") in DRIVER_EXIT_STATUS:
      description = DRIVER_EXIT_STATUS[status["state"]][1]
      if status.get("error"):
        description += f"\n\n{status['error']}"
    else:
      description = ("The first qualifying movement starts capture without waiting for warning. " +
                     "Warning status delay changes the status only. Captures remain queued until RTZ acknowledges them.")
      if self.config.wait_for_driver_exit:
        description += " After ignition switches off, open and close the driver's door to start the 90-second arming timer."
    gui_app.push_widget(BigDialog("Sentry status", description))

  @staticmethod
  def _runtime_status_error(status: dict) -> str | None:
    if not status:
      return "Sentry daemon status is unavailable. It may not be running or may have failed before startup completed."
    updated_at = status.get("updated_at")
    if not isinstance(updated_at, str):
      return "Sentry daemon status has no valid update timestamp."
    try:
      updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
      age = (datetime.now(UTC) - updated).total_seconds()
    except (TypeError, ValueError):
      return "Sentry daemon status has an invalid update timestamp."
    if age > STATUS_STALE_SECONDS or age < -60:
      return "Sentry daemon status is stale. Restart the device or inspect sentryd logs."
    if status.get("error") and status.get("state") not in DRIVER_EXIT_STATUS:
      return str(status["error"])
    return None

  def _confirm_reset(self) -> None:
    def reset() -> None:
      self._write_config(lambda: self.store.reset()[0])

    gui_app.push_widget(BigConfirmationDialog("slide to reset sentry settings", self.icon, reset, red=True))
