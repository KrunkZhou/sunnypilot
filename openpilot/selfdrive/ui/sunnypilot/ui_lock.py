"""Home/menu privacy lock. Driving and alert widgets remain outside this gate."""
from functools import partial
import queue
import threading
import time

import pyray as rl

from openpilot.common.realtime import drop_realtime
from openpilot.system.ui.lib.application import gui_app, FontWeight, TextAlignment, TextAlignmentVertical
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.label import UnifiedLabel, gui_label


class UiLockController:
  """Keep disk access and PIN derivation off the UI thread; start only with the UI."""
  POLL_INTERVAL = 0.25
  ATTEMPT_INTERVAL = 2.0

  def __init__(self, store=None):
    self._store = store
    self._mutex = threading.Lock()
    self._snapshot = {"version": 1, "sequence": "0", "revision": "", "locked": True, "message": "", "storage_error": False}
    self._ready = False
    self._pending = False
    self._next_attempt = 0.0
    self._request_id = 0
    self._requests: queue.Queue = queue.Queue()
    self._results: queue.Queue = queue.Queue()
    self._stop = threading.Event()
    self._worker = threading.Thread(target=self._run, name="ui-lock", daemon=True)
    self._worker.start()

  @property
  def snapshot(self):
    with self._mutex:
      return dict(self._snapshot)

  @property
  def pending(self):
    with self._mutex:
      return self._pending

  @property
  def retry_after(self):
    with self._mutex:
      return max(0.0, self._next_attempt - time.monotonic())

  def submit(self, pin: str, revision: str) -> int | None:
    if not (4 <= len(pin) <= 6 and pin.isascii() and pin.isdigit()):
      return None
    with self._mutex:
      if not self._ready or self._pending or time.monotonic() < self._next_attempt or self._snapshot["storage_error"]:
        return None
      self._pending = True
      self._next_attempt = time.monotonic() + self.ATTEMPT_INTERVAL
      self._request_id += 1
      request_id = self._request_id
    self._requests.put((request_id, pin, revision))
    return request_id

  def take_result(self):
    try:
      return self._results.get_nowait()
    except queue.Empty:
      return None

  def _refresh(self):
    try:
      if self._store is None:
        from openpilot.sunnypilot.system.ui_lock import UiLockStore
        self._store = UiLockStore()
      snapshot = self._store.snapshot()
    except Exception:
      # A disk failure must never expose a previously unlocked home.
      snapshot = self.snapshot | {"locked": True, "storage_error": True}
    with self._mutex:
      self._snapshot = snapshot
      self._ready = True

  def _run(self):
    drop_realtime()
    self._refresh()
    while not self._stop.is_set():
      try:
        request_id, pin, revision = self._requests.get(timeout=self.POLL_INTERVAL)
      except queue.Empty:
        self._refresh()
        continue
      try:
        result = self._store.verify_pin(pin, revision)
      except Exception:
        result = {"status": "failed"}
      finally:
        pin = ""
      self._refresh()
      with self._mutex:
        self._pending = False
        self._next_attempt = max(self._next_attempt, time.monotonic() + result.get("retry_after", 0.0))
      self._results.put((request_id, result))

  def close(self):
    self._stop.set()
    self._worker.join(timeout=1.0)


class PinKeyboard(Widget):
  """A small numeric keyboard shared by the 536x240 and 2160x1080 UIs."""
  def __init__(self, lock):
    super().__init__()
    self._lock = lock
    self._pin = ""
    self._request_id = None
    self._message = "Enter passcode"
    self.revision = lock.snapshot["revision"]
    size = 30 if not gui_app.big_ui() else 60
    self._keys = [Button(str(n), partial(self._add_digit, str(n)), font_size=size, button_style=ButtonStyle.KEYBOARD)
                  for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 0)]
    self._cancel = Button("Cancel", lock.dismiss_pin, font_size=size)
    self._delete = Button("Delete", self._backspace, font_size=size)
    self._submit = Button("Unlock", self._verify, font_size=size, button_style=ButtonStyle.PRIMARY)

  def _add_digit(self, digit):
    if len(self._pin) < 6 and not self._lock.controller.pending:
      self._pin += digit

  def _backspace(self):
    self._pin = self._pin[:-1]

  def _verify(self):
    self._request_id = self._lock.controller.submit(self._pin, self.revision)
    self._pin = ""
    self._message = "Checking…" if self._request_id is not None else "Please wait before trying again"

  def handle_result(self, request_id, result):
    if request_id != self._request_id:
      return
    self._request_id = None
    self._message = {"incorrect": "Incorrect passcode", "throttled": "Please wait before trying again",
                     "changed": "Lock changed. Try again", "failed": "Unable to unlock"}.get(result["status"], "Enter passcode")

  def hide_event(self):
    self._pin = ""
    self._request_id = None
    super().hide_event()

  def _render(self, rect):
    rl.draw_rectangle_rec(rect, rl.BLACK)
    width, height = min(rect.width - 20, 1000), min(rect.height - 16, 680)
    x, y = rect.x + (rect.width - width) / 2, rect.y + (rect.height - height) / 2
    font = 24 if not gui_app.big_ui() else 52
    gui_label(rl.Rectangle(x, y, width, height * 0.14), self._message, font, alignment=TextAlignment.CENTER)
    # Never render any entered digit, including a last-character preview.
    gui_label(rl.Rectangle(x, y + height * 0.14, width, height * 0.16), "•" * len(self._pin), font,
              alignment=TextAlignment.CENTER)
    gap = 5 if not gui_app.big_ui() else 16
    key_width, key_height = (width - gap * 4) / 5, (height * 0.7 - gap * 2) / 3
    pending = self._lock.controller.pending
    for i, key in enumerate(self._keys):
      key.set_enabled(self.enabled and not pending and len(self._pin) < 6)
      key.render(rl.Rectangle(x + (i % 5) * (key_width + gap), y + height * 0.3 + (i // 5) * (key_height + gap),
                              key_width, key_height))
    self._cancel.set_enabled(self.enabled)
    self._delete.set_enabled(self.enabled and bool(self._pin) and not pending)
    self._submit.set_enabled(self.enabled and 4 <= len(self._pin) <= 6 and not pending and self._lock.controller.retry_after == 0)
    action_width = (width - gap * 2) / 3
    for i, button in enumerate((self._cancel, self._delete, self._submit)):
      button.render(rl.Rectangle(x + i * (action_width + gap), y + height * 0.3 + 2 * (key_height + gap), action_width, key_height))


class UiLock:
  def __init__(self, controller=None):
    self.controller = controller or UiLockController()
    self.snapshot = self.controller.snapshot
    self._identity = None
    self._pin_keyboard = None
    self._previous_started = False
    self._previous_standstill = False
    self._message_layout = None
    self._message = UnifiedLabel("", font_size=28 if not gui_app.big_ui() else 64, wrap_text=True,
                                 alignment=TextAlignment.CENTER, alignment_vertical=TextAlignmentVertical.MIDDLE)

  @property
  def locked(self):
    return self.snapshot["locked"]

  @property
  def pin_open(self):
    return self._pin_keyboard is not None

  def tick(self):
    if self._pin_keyboard is not None and not gui_app.widget_in_stack(self._pin_keyboard):
      self.dismiss_pin()
    self.snapshot = self.controller.snapshot
    identity = (self.snapshot["sequence"], self.snapshot["revision"], self.locked, self.snapshot["storage_error"])
    changed = identity != self._identity
    if changed:
      self.dismiss_pin()
      self._identity = identity
    while (result := self.controller.take_result()) is not None:
      if self._pin_keyboard is not None:
        self._pin_keyboard.handle_result(*result)
    return changed and self.locked

  def request_pin(self):
    if self.locked and not self.snapshot["storage_error"] and self._pin_keyboard is None:
      self._pin_keyboard = PinKeyboard(self)
      gui_app.push_widget(self._pin_keyboard)

  def dismiss_pin(self):
    if self._pin_keyboard is not None:
      if gui_app.widget_in_stack(self._pin_keyboard):
        # Remove only our dialog, including when a screensaver was pushed over it.
        gui_app.pop_widget(gui_app._nav_stack.index(self._pin_keyboard))
      self._pin_keyboard.hide_event()
      self._pin_keyboard = None

  def preempt_for_driving(self, started, standstill, alert):
    transition = started and (not self._previous_started or (self._previous_standstill and not standstill))
    self._previous_started, self._previous_standstill = started, standstill
    if self._pin_keyboard is not None and (transition or (started and alert is not None)):
      self.dismiss_pin()
      return True
    return False

  def render(self, rect):
    rl.draw_rectangle_rec(rect, rl.BLACK)
    title_size = 56 if not gui_app.big_ui() else 130
    gui_label(rl.Rectangle(rect.x, rect.y + rect.height * 0.14, rect.width, rect.height * 0.35), "Locked", title_size,
              font_weight=FontWeight.MEDIUM, alignment=TextAlignment.CENTER)
    self._message.set_text(self.snapshot["message"])
    message_rect = rl.Rectangle(rect.x + 16, rect.y + rect.height * 0.5, rect.width - 32, rect.height * 0.48)
    layout = (self.snapshot["message"], message_rect.width, message_rect.height)
    if layout != self._message_layout:
      self._message.set_font_size(28 if not gui_app.big_ui() else 64)
      while self._message.font_size > 16 and self._message.get_content_height(int(message_rect.width)) > message_rect.height:
        self._message.set_font_size(self._message.font_size - 2)
      self._message_layout = layout
    self._message.render(message_rect)


class MiciUiLockHomeMixin:
  ui_lock = None

  def _update_state(self):
    if self.ui_lock is not None and self.ui_lock.locked:
      # The ordinary home update handles the ExperimentalMode long press.
      self._mouse_down_t = None
      self._did_long_press = False
      self._is_pressed_prev = False
      return
    super()._update_state()

  def _render(self, rect):
    if self.ui_lock is not None and self.ui_lock.locked:
      self.ui_lock.render(rect)
    else:
      super()._render(rect)

  def _handle_mouse_release(self, mouse_pos):
    if self.ui_lock is not None and self.ui_lock.locked:
      self.ui_lock.request_pin()
    else:
      super()._handle_mouse_release(mouse_pos)


class BigUiLockHomeMixin:
  ui_lock = None

  def __init__(self):
    super().__init__()
    from openpilot.selfdrive.ui.layouts.home import HomeLayoutState
    self._lock_home_was_drawn = False
    self._lock_alerts = Button("Alerts", lambda: self._set_state(HomeLayoutState.ALERTS), font_size=40)
    self._lock_update = Button("Update", lambda: self._set_state(HomeLayoutState.UPDATE), font_size=40)

  def _render(self, rect):
    if self.ui_lock is None or not self.ui_lock.locked:
      return super()._render(rect)
    from openpilot.selfdrive.ui.layouts.home import HomeLayoutState, REFRESH_INTERVAL
    if time.monotonic() - self.last_refresh >= REFRESH_INTERVAL:
      self._refresh()
      self.last_refresh = time.monotonic()
    self._lock_home_was_drawn = self.current_state == HomeLayoutState.HOME
    rl.draw_rectangle_rec(rect, rl.BLACK)
    # Keep alert access and acknowledgements; omit all ordinary home/status data.
    for button, visible, button_rect in ((self._lock_alerts, self.alert_count > 0, self.alert_notif_rect),
                                         (self._lock_update, self.update_available, self.update_notif_rect)):
      if visible:
        button.set_enabled(self.enabled)
        button.render(button_rect)
    if self.current_state == HomeLayoutState.HOME:
      self.ui_lock.render(self.content_rect)
    elif self.current_state == HomeLayoutState.ALERTS:
      self._render_alerts_view()
    elif self.current_state == HomeLayoutState.UPDATE:
      self._render_update_view()

  def _handle_mouse_release(self, mouse_pos):
    if self.ui_lock is None or not self.ui_lock.locked:
      return super()._handle_mouse_release(mouse_pos)
    from openpilot.selfdrive.ui.layouts.home import HomeLayoutState
    # Alert Close can change state during this same release; do not also open PIN.
    if self._lock_home_was_drawn and self.current_state == HomeLayoutState.HOME:
      self.ui_lock.request_pin()
