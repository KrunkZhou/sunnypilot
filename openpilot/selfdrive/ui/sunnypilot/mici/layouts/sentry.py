from __future__ import annotations

from datetime import UTC, datetime
import math
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


STATUS_STALE_SECONDS = 15.0
CONFIRMATION_HELP = (
  "The first movement captures a wide-road and cabin photo pair locally. " +
  "10 qualifying motion samples within 60 seconds confirm the event for upload and an alert. " +
  "Unconfirmed photos are otherwise discarded when that window expires."
)
DOOR_PAUSE_HELP = (
  "Opening any door or the trunk keeps an unconfirmed first photo pair and takes a new pair. " +
  "If there is no pending first pair, only the new pair is taken. These door photos upload without a webhook alert. " +
  "New motion capture pauses until all doors and the trunk have stayed closed for 5 minutes. " +
  "Earlier confirmed events keep their normal delivery and alerts."
)
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
      GreyBigButton("motion confirmation", "The first wide + cabin pair waits locally for 10 motion samples within 60 seconds before upload and an alert."),
      GreyBigButton("unconfirmed photos", "Without confirmation, the first pair is discarded after 60 seconds, unless a door opens."),
      GreyBigButton("silent door photos", "A door or trunk opening keeps any pending first pair and captures a new pair. Both upload without an alert."),
      GreyBigButton("door pause", "New motion capture resumes after all doors and the trunk stay closed for 5 minutes."),
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
    self._confirmation = BigButton("motion confirmation", "10 motion samples")
    self._confirmation.set_click_callback(self._show_confirmation)
    self._wait_for_driver_exit = BigToggle("wait for driver exit", "door open + close before arming",
                                         self.config.wait_for_driver_exit, self._on_wait_for_driver_exit)
    self._status = BigButton("Sentry status", "starting", self.icon)
    self._status.set_click_callback(self._show_status)
    self._queue = BigButton("upload queue", "0 pending")
    self._queue.set_click_callback(self._retry_uploads)
    self._queue.set_enabled(lambda: ui_state.is_offroad())
    self._manual_test = BigButton("test Sentry alert", "capture + upload", self.icon)
    self._manual_test.set_click_callback(lambda: self._send_command("manual_test"))
    self._manual_test.set_enabled(lambda: ui_state.is_offroad() and self.config.effective_enabled and self.config_error is None)
    self._retry = BigButton("retry all uploads", "pending + failed")
    self._retry.set_click_callback(self._retry_uploads)
    self._retry.set_enabled(lambda: ui_state.is_offroad())
    self._reset = BigButton("reset Sentry settings", "keeps queued captures")
    self._reset.set_click_callback(self._confirm_reset)

    self._scroller.add_widgets([
      self._enable_toggle,
      self._sensitivity,
      self._confirmation,
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
    elif state_key == "confirming":
      state = "confirming motion"
    elif state_key == "door_paused":
      state = self._door_pause_text(status)
    self._status.set_value(state)
    self._confirmation.set_value(self._confirmation_text(status) if state == "confirming motion" else "10 motion samples")
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

  def _retry_uploads(self) -> None:
    # Recheck at dispatch in case ignition changed after the touch began.
    if ui_state.is_offroad():
      self._send_command("retry_uploads")

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
    elif status.get("state") == "door_paused":
      description = f"{self._door_pause_text(status)}.\n\n{DOOR_PAUSE_HELP}"
      if status.get("error"):
        description += f"\n\n{status['error']}"
    elif status.get("state") == "confirming":
      description = f"{self._confirmation_text(status)}.\n\n{CONFIRMATION_HELP}\n\n{DOOR_PAUSE_HELP}"
    elif status.get("state") in DRIVER_EXIT_STATUS:
      description = DRIVER_EXIT_STATUS[status["state"]][1]
      if status.get("error"):
        description += f"\n\n{status['error']}"
    else:
      description = f"{CONFIRMATION_HELP}\n\n{DOOR_PAUSE_HELP}"
      if self.config.wait_for_driver_exit:
        description += " After ignition switches off, open and close the driver's door to start the 90-second arming timer."
    gui_app.push_widget(BigDialog("Sentry status", description))

  def _show_confirmation(self) -> None:
    gui_app.push_widget(BigDialog("motion confirmation", f"{CONFIRMATION_HELP}\n\n{DOOR_PAUSE_HELP}"))

  @staticmethod
  def _confirmation_text(status: dict) -> str:
    progress = status.get("confirmation")
    if not isinstance(progress, dict):
      return "10 motion samples"
    hits = progress.get("hits")
    seconds = progress.get("seconds_remaining")
    if (type(hits) is not int or progress.get("required_hits") != 10 or
        type(seconds) not in (int, float) or not math.isfinite(seconds)):
      return "10 motion samples"
    return f"{max(0, min(hits, 10))}/10 · {math.ceil(max(0, min(seconds, 60)))} sec left"

  @staticmethod
  def _door_pause_text(status: dict) -> str:
    pause = status.get("door_pause")
    if not isinstance(pause, dict) or not pause.get("active"):
      return "door pause"
    if pause.get("waiting_for_status"):
      return "waiting for door status"
    if pause.get("waiting_for_close"):
      return "waiting for doors closed"
    seconds = pause.get("seconds_remaining")
    if type(seconds) not in (int, float) or not math.isfinite(seconds):
      return "waiting for door status"
    return f"door pause · {math.ceil(max(0, min(seconds, 300)))} sec left"

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
    if status.get("error") and status.get("state") not in (*DRIVER_EXIT_STATUS, "door_paused"):
      return str(status["error"])
    return None

  def _confirm_reset(self) -> None:
    def reset() -> None:
      self._write_config(lambda: self.store.reset()[0])

    gui_app.push_widget(BigConfirmationDialog("slide to reset sentry settings", self.icon, reset, red=True))
