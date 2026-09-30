"""Small optional navigation HUD and settings status shared by both displays."""
import time
import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.sunnypilot.external_navigation.settings import read_mode

MODE_LABELS = ("Off", "Assisted turns")


def mode():
  value = read_mode()
  return value if value in (0, 1) else 0


def receive_age_ms(nav):
  return (time.monotonic_ns() - nav.receiveMonoTime) // 1_000_000


def fresh_distance(nav, age_ms):
  return nav.sourceFresh and 0 <= age_ms <= 1000 and nav.distanceAgeMs + age_ms <= 1000


def status_text():
  if mode() == 0:
    return "Off"
  sm = ui_state.sm
  if not sm.alive["externalNavigationSP"]:
    return "Waiting for navigation receiver"
  nav = sm["externalNavigationSP"]
  age_ms = receive_age_ms(nav)
  if not nav.connected:
    return {"not_provisioned": "Relay not provisioned", "hotspot_unavailable": "Hotspot unavailable",
            "socket_unavailable": "Receiver unavailable", "off": "Off"}.get(nav.status, "Waiting for Waveshare")
  if not 0 <= age_ms <= 1000:
    return "Waiting for Waveshare"
  if not nav.available:
    return nav.status or "No active route"
  if not fresh_distance(nav, age_ms):
    return "Guidance only · awaiting fresh distance"
  if sm.alive["modelDataV2SP"]:
    hint = sm["modelDataV2SP"]
    if hint.navigationAssisted:
      return "Assisted turns · available"
    if hint.navigationHintStatus:
      return hint.navigationHintStatus.replace("_", " ")
  return "Guidance only · assistance unavailable"


class NavigationHud:
  def __init__(self):
    self.font = gui_app.font(FontWeight.MEDIUM)

  def render(self, rect):
    sm = ui_state.sm
    if mode() == 0 or not sm.alive["externalNavigationSP"]:
      return
    # Alerts own the entire HUD priority, including informational prompts.
    if sm["selfdriveState"].alertSize != 0:
      return
    nav = sm["externalNavigationSP"]
    age_ms = receive_age_ms(nav)
    if not nav.connected or not nav.available or not 0 <= age_ms <= 1000:
      return
    scale = 1 if rect.width < 800 else 2
    width = min(300 * scale, rect.width - 190 * scale)
    if width < 100:
      return
    right_margin = 12 if scale == 1 else 246  # Keep the existing 192px engagement button uncovered.
    box = rl.Rectangle(rect.x + rect.width - width - right_margin, rect.y + 8 * scale, width, 62 * scale)
    text = nav.instruction or nav.road or "Navigation"
    distance = ""
    if nav.hasNextDistance and fresh_distance(nav, age_ms):
      distance = f"{nav.nextDistance} m" if nav.nextDistance < 1000 else f"{nav.nextDistance / 1000:.1f} km"
    title = f"{distance}  {text}".strip()
    detail = "Right branch · driver selects lane" if nav.branchRight else status_text()
    rl.draw_rectangle_rec(box, rl.Color(0, 0, 0, 185))
    for line, size, offset, color in ((title, 21, 6, rl.WHITE), (detail, 14, 36, rl.Color(180, 180, 180, 255))):
      line = line.replace("\n", " ")
      while line and measure_text_cached(self.font, line, size * scale).x > width - 16 * scale:
        line = line[:-2].rstrip("…") + "…" if len(line) > 2 else ""
      rl.draw_text_ex(self.font, line, rl.Vector2(box.x + 8 * scale, box.y + offset * scale), size * scale, 0, color)
