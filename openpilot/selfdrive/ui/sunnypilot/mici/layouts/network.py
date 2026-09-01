from openpilot.selfdrive.ui.mici.layouts.settings.network.network_layout import NetworkLayoutMici
from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigToggle
from openpilot.selfdrive.ui.sunnypilot.ui_streamer_dialog import UIStreamerDialogMici
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.sunnypilot.ui_streamer import UIStreamerConfig
from openpilot.system.ui.lib.application import gui_app


class NetworkLayoutMiciSP(NetworkLayoutMici):
  def __init__(self):
    super().__init__()
    self._stream_config = UIStreamerConfig()
    self._stream_enabled = self._stream_config.enabled()

    self._stream_toggle = BigToggle("local UI streaming", "trusted network only", self._stream_enabled, self._set_stream_enabled)
    self._stream_toggle.set_enabled(lambda: ui_state.is_offroad())

    icon = gui_app.texture("icons_mici/settings/network/wifi_strength_full.png", 64, 47)
    self._stream_access = BigButton("view UI stream", "scan QR code", icon)
    self._stream_access.set_click_callback(lambda: gui_app.push_widget(UIStreamerDialogMici(self._wifi_manager)))
    self._stream_access.set_enabled(lambda: ui_state.is_offroad())
    self._stream_access.set_visible(lambda: self._stream_enabled)
    self._scroller.add_widgets([self._stream_toggle, self._stream_access])

  def _set_stream_enabled(self, enabled: bool):
    self._stream_config.set_enabled(enabled)
    self._stream_enabled = enabled

  def show_event(self):
    self._stream_enabled = self._stream_config.enabled()
    self._stream_toggle.set_checked(self._stream_enabled)
    super().show_event()
