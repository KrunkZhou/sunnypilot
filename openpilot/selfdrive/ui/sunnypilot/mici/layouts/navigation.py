from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigMultiToggle, GreyBigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog
from openpilot.selfdrive.ui.sunnypilot.external_navigation import MODE_LABELS, mode, status_text
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets.scroller import NavScroller
from openpilot.sunnypilot.external_navigation.settings import write_mode


class NavigationLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    self._mode = BigMultiToggle("navigation", list(MODE_LABELS), select_callback=self._select_mode)
    self._status = BigButton("relay status", "Waiting for Waveshare")
    self._status.set_click_callback(self._show_status)
    self._scroller.add_widgets([
      self._mode, self._status,
      GreyBigButton("Waveshare relay", "CarPlay → BLE → Wi-Fi"),
      GreyBigButton("hotspot", "Automatic on-road; manual changes respected"),
      GreyBigButton("assisted turns", "Requires a validated vehicle and model"),
      GreyBigButton("highway exits", "Guidance only · no automatic lane changes"),
    ])

  def _select_mode(self, value):
    if ui_state.is_offroad():
      write_mode(MODE_LABELS.index(value))

  def _show_status(self):
    gui_app.push_widget(BigDialog("External navigation", status_text()))

  def _update_state(self):
    super()._update_state()
    self._mode.set_value(MODE_LABELS[mode()])
    self._mode.set_enabled(ui_state.is_offroad())
    self._status.set_value(status_text())
