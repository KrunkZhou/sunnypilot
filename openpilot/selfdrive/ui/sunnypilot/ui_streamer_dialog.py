import time

import numpy as np
import pyray as rl

from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.sunnypilot.ui_streamer import STREAM_PORT, UIStreamerConfig, build_stream_url
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.wrap_text import wrap_text
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.button import IconButton
from openpilot.system.ui.widgets.label import UnifiedLabel
from openpilot.system.ui.widgets.nav_widget import NavWidget


QR_REFRESH_INTERVAL = 0.5


try:
  from openpilot.common.qrcode import make_texture
except ModuleNotFoundError as error:
  if error.name != "openpilot.common.qrcode":
    raise
  # release-mici predates the shared QR helper but includes qrcode and Pillow.
  import qrcode

  def make_texture(data: str, inverted: bool = False) -> rl.Texture:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=0 if inverted else 4)
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(
      fill_color="white" if inverted else "black",
      back_color="black" if inverted else "white",
    ).convert("RGBA")
    pixels = np.asarray(image, dtype=np.uint8)
    rl_image = rl.Image()
    rl_image.data = rl.ffi.cast("void *", pixels.ctypes.data)
    rl_image.width = image.width
    rl_image.height = image.height
    rl_image.mipmaps = 1
    rl_image.format = rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_R8G8B8A8
    return rl.load_texture_from_image(rl_image)


class StreamQR:
  def __init__(self, wifi_manager):
    self._wifi_manager = wifi_manager
    self._config = UIStreamerConfig()
    self.texture: rl.Texture | None = None
    self.url = ""
    self.status = tr("starting stream...")
    self._last_refresh = float("-inf")
    self._visible = True

  def show(self) -> None:
    self._visible = True
    self._last_refresh = float("-inf")

  def update(self) -> None:
    if not self._visible:
      return
    now = time.monotonic()
    if now - self._last_refresh < QR_REFRESH_INTERVAL:
      return
    self._last_refresh = now

    address = self._wifi_manager.ipv4_address
    token = self._config.session_token()
    url = build_stream_url(address, token) if address and token else ""
    self.status = f"http://{address}:{STREAM_PORT}" if address and token else tr("connect to wi-fi") if not address else tr("starting stream...")
    if url == self.url and (not url or self.texture is not None):
      return
    if self.texture is not None and self.texture.id != 0:
      rl.unload_texture(self.texture)
    self.texture = None
    self.url = ""
    if url:
      try:
        texture = make_texture(url, inverted=not gui_app.big_ui())
      except Exception:
        cloudlog.exception("UI stream QR generation failed")
        return
      if texture is None or texture.id == 0 or texture.width <= 0 or texture.height <= 0:
        if texture is not None and texture.id != 0:
          rl.unload_texture(texture)
        cloudlog.warning("UI stream QR texture upload failed")
        return
      self.texture = texture
    self.url = url

  def close(self) -> None:
    self._visible = False
    if self.texture is not None and self.texture.id != 0:
      rl.unload_texture(self.texture)
    self.texture = None


class UIStreamerDialogMici(NavWidget):
  def __init__(self, wifi_manager):
    super().__init__()
    self._qr = StreamQR(wifi_manager)
    self._title = UnifiedLabel(tr("local UI stream"), font_size=44, font_weight=FontWeight.BOLD, line_height=0.9)
    self._status = UnifiedLabel("", font_size=28, font_weight=FontWeight.ROMAN, text_color=rl.Color(255, 255, 255, 170), line_height=0.9)

  def _update_state(self):
    super()._update_state()
    if not ui_state.is_offroad() and not self.is_dismissing:
      self.dismiss()

  def _render(self, rect: rl.Rectangle):
    self._qr.update()
    qr_size = 200
    if self._qr.texture is not None:
      scale = qr_size / self._qr.texture.height
      rl.draw_texture_ex(self._qr.texture, rl.Vector2(rect.x + 12, rect.y + 20), 0.0, scale, rl.WHITE)
    else:
      rl.draw_rectangle_rounded(rl.Rectangle(rect.x + 12, rect.y + 20, qr_size, qr_size), 0.08, 8, rl.Color(35, 35, 35, 255))

    text_x = rect.x + qr_size + 34
    self._title.render(rl.Rectangle(text_x, rect.y + 46, rect.width - text_x - 12, 70))
    self._status.set_text(f"{self._qr.status}\n{tr('trusted network - unencrypted')}")
    self._status.render(rl.Rectangle(text_x, rect.y + 120, rect.width - text_x - 12, 80))

  def hide_event(self):
    self._qr.close()
    super().hide_event()

  def show_event(self):
    self._qr.show()
    super().show_event()

  def __del__(self):
    self._qr.close()


class UIStreamerDialog(Widget):
  def __init__(self, wifi_manager):
    super().__init__()
    self._qr = StreamQR(wifi_manager)
    self._close_btn = IconButton(gui_app.texture("icons/close.png", 80, 80))
    self._close_btn.set_click_callback(gui_app.pop_widget)

  def _update_state(self):
    super()._update_state()
    if not ui_state.is_offroad():
      gui_app.pop_widget()

  def _render(self, rect: rl.Rectangle) -> int:
    rl.clear_background(rl.Color(20, 20, 20, 255))
    self._qr.update()
    margin = 70
    content = rl.Rectangle(rect.x + margin, rect.y + margin, rect.width - margin * 2, rect.height - margin * 2)
    self._close_btn.render(rl.Rectangle(content.x - 20, content.y - 20, 120, 120))

    font = gui_app.font(FontWeight.NORMAL)
    title = tr("Local UI stream")
    rl.draw_text_ex(font, title, rl.Vector2(content.x, content.y + 130), 75, 0.0, rl.WHITE)

    instructions = tr("Scan from a device on the same trusted Wi-Fi or comma hotspot. Local HTTP traffic is not encrypted.")
    wrapped = wrap_text(font, instructions, 45, int(content.width * 0.45))
    rl.draw_text_ex(font, "\n".join(wrapped), rl.Vector2(content.x, content.y + 260), 45, 0.0, rl.Color(210, 210, 210, 255))
    rl.draw_text_ex(font, self._qr.status, rl.Vector2(content.x, content.y + 500), 40, 0.0, rl.Color(150, 150, 150, 255))

    qr_size = min(int(content.width * 0.45), int(content.height)) - 30
    qr_rect = rl.Rectangle(content.x + content.width - qr_size, content.y, qr_size, qr_size)
    if self._qr.texture is not None:
      source = rl.Rectangle(0, 0, self._qr.texture.width, self._qr.texture.height)
      rl.draw_texture_pro(self._qr.texture, source, qr_rect, rl.Vector2(0, 0), 0.0, rl.WHITE)
    else:
      rl.draw_rectangle_rounded(qr_rect, 0.08, 8, rl.Color(35, 35, 35, 255))
    return -1

  def hide_event(self):
    self._qr.close()
    super().hide_event()

  def show_event(self):
    self._qr.show()
    super().show_event()

  def __del__(self):
    self._qr.close()
