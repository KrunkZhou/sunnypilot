"""Optional navigation activity indicator and silent model-pulse notices."""
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


def navigation_turn_notice(sm):
  # The model latches this event across subsequent publications, so a UI frame
  # can miss the original 20 Hz pulse without losing the notice. Its timestamp
  # never renews just because a newer model message repeats it.
  if mode() == 0 or not sm.alive['modelDataV2SP'] or not sm.valid['modelDataV2SP']:
    return None
  hint = sm['modelDataV2SP']
  timestamp = getattr(hint, 'navigationTurnEventMonoTime', 0)
  direction = getattr(hint, 'navigationTurnEvent', 0)
  if timestamp <= 0 or not 0 <= time.monotonic_ns() - timestamp < 2_000_000_000:
    return None
  return {1: 'Turn left', 2: 'Turn right'}.get(direction)


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
    if mode() == 0 or not sm.alive["externalNavigationSP"] or not sm.valid["externalNavigationSP"]:
      return
    # Alerts own the entire HUD priority, including informational prompts.
    if sm["selfdriveState"].alertSize != 0 or navigation_turn_notice(sm):
      return
    nav = sm["externalNavigationSP"]
    age_ms = receive_age_ms(nav)
    if not nav.connected or not nav.available or nav.routeState != 1 or not 0 <= age_ms <= 1000:
      return
    scale = 1 if rect.width < 800 else 2
    width = min(180 * scale, rect.width - 190 * scale)
    if width < 100:
      return
    right_margin = 12 if scale == 1 else 246  # Keep the existing 192px engagement button uncovered.
    box = rl.Rectangle(rect.x + rect.width - width - right_margin, rect.y + 8 * scale, width, 34 * scale)
    rl.draw_rectangle_rec(box, rl.Color(0, 0, 0, 185))
    title, size = 'Navigation running', 18 * scale
    while size > 10 and measure_text_cached(self.font, title, size).x > width - 16 * scale:
      size -= 1
    rl.draw_text_ex(self.font, title, rl.Vector2(box.x + 8 * scale, box.y + 7 * scale), size, 0, rl.WHITE)
