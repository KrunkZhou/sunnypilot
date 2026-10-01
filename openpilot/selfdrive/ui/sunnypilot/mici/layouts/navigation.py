from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigMultiToggle, GreyBigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog
from openpilot.selfdrive.ui.sunnypilot.external_navigation import MODE_LABELS, mode, status_text
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets.scroller import NavScroller
from openpilot.sunnypilot.external_navigation.settings import read_turn_speed_control, write_mode, write_turn_speed_control


class NavigationLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    self._mode = BigMultiToggle("navigation", list(MODE_LABELS), select_callback=self._select_mode)
    self._status = BigButton("relay status", "Waiting for Waveshare")
    self._status.set_click_callback(self._show_status)
    self._speed_control = BigButton("turn speed control", "Off")
    self._speed_control.set_click_callback(self._toggle_speed_control)
    self._speed_status = GreyBigButton("speed status", "Assisted turns and sunnypilot longitudinal control")
    self._scroller.add_widgets([
      self._mode, self._status, self._speed_control, self._speed_status,
      GreyBigButton("driver duties", "Traffic lights, stop signs and right of way"),
      GreyBigButton("Waveshare relay", "CarPlay → BLE → Wi-Fi"),
      GreyBigButton("hotspot", "Automatic on-road; manual changes respected"),
      GreyBigButton("assisted turns", "Fresh low-speed turns while lateral control is active"),
      GreyBigButton("highway exits", "Guidance only · no automatic lane changes"),
    ])

  def _select_mode(self, value):
    if ui_state.is_offroad():
      write_mode(MODE_LABELS.index(value))

  def _show_status(self):
    gui_app.push_widget(BigDialog("External navigation", status_text()))

  def _speed_control_available(self):
    return mode() == 1 and ui_state.CP is not None and ui_state.CP.openpilotLongitudinalControl

  def _toggle_speed_control(self):
    enabled = read_turn_speed_control()
    if ui_state.is_offroad() and (enabled or self._speed_control_available()):
      write_turn_speed_control(not enabled)

  def _update_state(self):
    super()._update_state()
    self._mode.set_value(MODE_LABELS[mode()])
    self._mode.set_enabled(ui_state.is_offroad())
    self._status.set_value(status_text())
    speed_enabled = read_turn_speed_control()
    speed_available = self._speed_control_available()
    self._speed_control.set_value(("On" if speed_available else "On · inactive") if speed_enabled else "Off")
    self._speed_control.set_enabled(ui_state.is_offroad() and (speed_enabled or speed_available))
    if mode() != 1:
      self._speed_status.set_value("Select Assisted turns")
    elif ui_state.CP is None or not ui_state.CP.openpilotLongitudinalControl:
      self._speed_status.set_value("Requires sunnypilot longitudinal control")
    else:
      self._speed_status.set_value("Reduce the speed target before supported turns")
