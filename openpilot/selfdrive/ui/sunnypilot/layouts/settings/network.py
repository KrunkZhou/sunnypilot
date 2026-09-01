"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import threading
import time
import pyray as rl

from openpilot.system.ui.lib.multilang import tr
from openpilot.selfdrive.ui.sunnypilot.ui_streamer_dialog import UIStreamerDialog
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.sunnypilot.ui_streamer import UIStreamerConfig
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.sunnypilot.widgets.list_view import button_item_sp, toggle_item_sp
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.network import NetworkUI, PanelType


class NetworkUISP(NetworkUI):
  def __init__(self, wifi_manager):
    super().__init__(wifi_manager)

    self._stream_config = UIStreamerConfig()
    self._stream_enabled = self._stream_config.enabled()
    self._stream_toggle = toggle_item_sp(
      lambda: tr("Local UI Streaming"),
      lambda: tr("Streams this device's UI to authenticated browsers on the same trusted Wi-Fi or comma hotspot."),
      initial_state=self._stream_enabled,
      callback=self._set_stream_enabled,
      enabled=ui_state.is_offroad,
    )
    self._stream_access = button_item_sp(
      lambda: tr("View Local UI Stream"), lambda: tr("SHOW QR"),
      lambda: tr("The access token rotates automatically after use or expiry."),
      callback=lambda: gui_app.push_widget(UIStreamerDialog(self._wifi_manager)),
      enabled=ui_state.is_offroad,
    )
    self._stream_access.set_visible(lambda: self._stream_enabled)
    self.add_advanced_widget(self._stream_toggle)
    self.add_advanced_widget(self._stream_access)

    self.scan_button = Button(tr("Scan"), self._scan_clicked, button_style=ButtonStyle.NORMAL, font_size=60, border_radius=30)
    self.scan_button.set_rect(rl.Rectangle(0, 0, 400, 100))

    self._scanning = False
    self._wifi_manager.add_callbacks(networks_updated=self._on_networks_updated)

  def _set_stream_enabled(self, enabled: bool):
    self._stream_config.set_enabled(enabled)
    self._stream_enabled = enabled

  def show_event(self):
    self._stream_enabled = self._stream_config.enabled()
    self._stream_toggle.action_item.set_state(self._stream_enabled)
    super().show_event()

  def _scan_clicked(self):
    self._scanning = True
    self.scan_button.set_text(tr("Scanning..."))
    self.scan_button.set_enabled(False)

    threading.Thread(target=self._wifi_manager._update_networks, daemon=True).start()
    self._wifi_manager._request_scan()
    self._wifi_manager._last_network_update = time.monotonic()

  def _on_networks_updated(self, networks):
    if self._scanning:
      self._scanning = False
      self.scan_button.set_text(tr("Scan"))
      self.scan_button.set_enabled(True)

  def _render(self, _):
    super()._render(_)

    if self._current_panel == PanelType.WIFI:
      self.scan_button.set_position(self._rect.x, self._rect.y + 20)
      self.scan_button.render()
