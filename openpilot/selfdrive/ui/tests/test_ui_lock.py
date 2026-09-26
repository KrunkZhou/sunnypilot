"""Exercise the lock worker, real PIN widget, and both navigation gates in isolation."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("scenario,theme", [("worker", "1"), ("store_integration", "1"), ("pin", "1"), ("pin_big", "1"),
                                             ("mici", "0"), ("mici", "1"), ("big", "0"), ("big", "1")])
def test_ui_lock(scenario, theme):
  # Native ui_state owns messaging/Params singletons; never replace them in the
  # pytest process where unrelated UI tests may already have imported them.
  env = os.environ | {"SCALE": "1", "SUNNYPILOT_UI": theme, "BIG": "1" if scenario in ("big", "pin_big") else "0"}
  repo_root = Path(__file__).resolve().parents[4]
  import_paths = [str(repo_root), str(repo_root / "opendbc_repo"), str(repo_root / "msgq_repo")]
  if env.get("PYTHONPATH"):
    import_paths.append(env["PYTHONPATH"])
  env["PYTHONPATH"] = os.pathsep.join(import_paths)
  result = subprocess.run([sys.executable, str(Path(__file__).resolve()), scenario], env=env, capture_output=True, text=True, timeout=30)
  assert result.returncode == 0, result.stdout + result.stderr


def _exercise(scenario):
  import importlib
  from enum import IntEnum
  from types import ModuleType, SimpleNamespace
  import threading
  import time
  from unittest.mock import Mock, patch

  import pyray as rl

  state = ModuleType("openpilot.selfdrive.ui.ui_state")
  state.ui_state = SimpleNamespace(started=False, is_body=False, ignition=False, sm={"carState": SimpleNamespace(standstill=True)})
  state.device = SimpleNamespace(awake=True)
  sys.modules[state.__name__] = state
  from openpilot.selfdrive.ui.sunnypilot import ui_lock
  from openpilot.system.ui.lib.application import gui_app, MousePos
  from openpilot.system.ui.widgets import Widget
  assert not any(thread.name == "ui-lock" for thread in threading.enumerate())  # Import is safe before manager forks.

  def snapshot(locked=True, revision="1", **kwargs):
    return {"version": 1, "sequence": "1", "revision": revision, "locked": locked, "message": "Call the owner", "storage_error": False} | kwargs

  def until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
      assert time.monotonic() < deadline, "worker did not respond"
      time.sleep(0.005)

  if scenario == "store_integration":
    import hashlib
    from tempfile import TemporaryDirectory
    from openpilot.sunnypilot.system.ui_lock import UiLockStore, PIN_ITERATIONS
    with TemporaryDirectory() as root:
      store = UiLockStore(root)
      controller = ui_lock.UiLockController(store)
      try:
        until(lambda: not controller.snapshot["locked"])
        salt = bytes.fromhex("01" * 16)
        verifier = hashlib.pbkdf2_hmac("sha256", b"0012", salt, PIN_ITERATIONS).hex()
        store.apply("1", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "lock", "Call the owner", salt.hex(), verifier)
        until(lambda: controller.snapshot["locked"])
        assert controller.submit("0012", controller.snapshot["revision"]) == 1
        until(lambda: not controller.pending)
        assert controller.take_result()[1]["status"] == "unlocked"
        assert not controller.snapshot["locked"] and store.snapshot()["sequence"] == "1"
        store.apply("2", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "lock", "New lock", salt.hex(), verifier)
        until(lambda: controller.snapshot["sequence"] == "2")
        assert controller.snapshot["locked"] and controller.snapshot["message"] == "New lock"
      finally:
        controller.close()
    return

  if scenario == "worker":
    class Store:
      def __init__(self):
        self.read = threading.Event()
        self.verify = threading.Event()
        self.state = snapshot(False)
        self.fail = False
        self.thread_ids = []

      def snapshot(self):
        self.thread_ids.append(threading.get_ident())
        assert self.read.wait(3)
        if self.fail:
          raise OSError("unreadable")
        return dict(self.state)

      def verify_pin(self, pin, revision):
        self.thread_ids.append(threading.get_ident())
        assert pin == "0012" and revision == "2"
        assert self.verify.wait(3)
        return {"status": "incorrect", "snapshot": dict(self.state), "retry_after": 0.0}

    store = Store()
    controller = ui_lock.UiLockController(store)
    try:
      assert controller.snapshot["locked"]  # No unlocked flash before the first read.
      assert controller.submit("0012", "1") is None
      store.read.set()
      until(lambda: not controller.snapshot["locked"])
      store.state = snapshot(revision="2")
      until(lambda: controller.snapshot["revision"] == "2")
      assert controller.submit("１２３４", "2") is None
      assert controller.submit("0012", "2") == 1
      assert controller.pending
      assert controller.submit("0012", "2") is None
      assert controller.snapshot["locked"]  # Accessible while verification is blocked.
      store.verify.set()
      until(lambda: not controller.pending)
      assert controller.take_result()[1]["status"] == "incorrect"
      assert controller.retry_after > 0 and controller.submit("0012", "2") is None
      store.fail = True
      until(lambda: controller.snapshot["storage_error"])
      assert controller.snapshot["locked"]
      assert set(store.thread_ids) == {controller._worker.ident}
      assert threading.get_ident() not in store.thread_ids
    finally:
      store.read.set()
      store.verify.set()
      controller.close()
    return

  class FakeController:
    pending = False
    retry_after = 0

    def __init__(self):
      self.snapshot = snapshot()
      self.requests = []
      self.results = []

    def submit(self, pin, revision):
      self.requests.append((pin, revision))
      self.pending = True
      return len(self.requests)

    def take_result(self):
      return self.results.pop(0) if self.results else None

  class Base(Widget):
    def _render(self, rect):
      pass

  with (patch.object(gui_app, "font", return_value=rl.Font()), patch.object(rl, "get_time", return_value=100.0),
        patch.object(rl, "draw_rectangle_rec"), patch.object(ui_lock, "gui_label") as labels,
        patch.object(ui_lock.Button, "render") as button_render):
    controller = FakeController()
    lock = ui_lock.UiLock(controller)
    lock.tick()
    base = Base()
    gui_app.push_widget(base)
    if scenario.startswith("pin"):
      lock.request_pin()
      keyboard = gui_app.get_active_widget()
      for digit in "0012345":
        keyboard._add_digit(digit)
      assert keyboard._pin == "001234"
      width, height = (2160, 1080) if scenario == "pin_big" else (536, 240)
      keyboard._render(rl.Rectangle(0, 0, width, height))
      rendered_texts = [call.args[1] for call in labels.call_args_list]
      assert "••••••" in rendered_texts and "001234" not in rendered_texts
      for call in button_render.call_args_list:
        rect = call.args[0]
        assert rect.width > 0 and rect.height > 0
        assert 0 <= rect.x < rect.x + rect.width <= width
        assert 0 <= rect.y < rect.y + rect.height <= height + 0.001
      keyboard._verify()
      assert controller.requests == [("001234", "1")] and keyboard._pin == ""
      controller.pending = False
      controller.results.append((1, {"status": "incorrect"}))
      lock.tick()
      assert keyboard._message == "Incorrect passcode"
      controller.retry_after = 1
      keyboard._pin = "1234"
      keyboard._render(rl.Rectangle(0, 0, width, height))
      assert not keyboard._submit.enabled
      lock.dismiss_pin()
      assert keyboard._pin == "" and gui_app.get_active_widget() is base

      lock.request_pin()
      keyboard = gui_app.get_active_widget()
      keyboard._pin = "1234"
      gui_app.pop_widgets_to(base, instant=True)
      lock.tick()
      assert not lock.pin_open and keyboard._pin == ""
      lock.request_pin()
      assert lock.pin_open and gui_app.get_active_widget() is not keyboard
      lock.dismiss_pin()

      lock.request_pin()
      keyboard = gui_app.get_active_widget()
      keyboard._pin = "1234"
      screensaver = Base()
      gui_app.push_widget(screensaver)
      controller.snapshot = snapshot(False, "2")
      lock.tick()
      assert not gui_app.widget_in_stack(keyboard) and gui_app.get_active_widget() is screensaver
      assert keyboard._pin == "" and not lock.locked
      gui_app.pop_widget()

      controller.snapshot = snapshot(revision="3")
      lock.tick()
      lock.request_pin()
      controller.snapshot = snapshot(revision="4")
      controller.results.append((1, {"status": "unlocked"}))
      lock.tick()
      assert gui_app.get_active_widget() is base and lock.locked  # A stale result cannot clear a replacement lock.
      lock.request_pin()
      assert lock.preempt_for_driving(True, True, None)
      lock.request_pin()
      assert lock.preempt_for_driving(True, False, None)
      lock.request_pin()
      assert lock.preempt_for_driving(True, False, object())
      assert gui_app.get_active_widget() is base
      return

    class HomeState(IntEnum):
      HOME = 0
      UPDATE = 1
      ALERTS = 2

    class Home(Base):
      def __init__(self):
        super().__init__()
        self.current_state = HomeState.HOME
        self.last_refresh = time.monotonic()
        self.alert_count = 1
        self.update_available = True
        self.alert_notif_rect = rl.Rectangle(240, 40, 220, 60)
        self.update_notif_rect = rl.Rectangle(40, 40, 200, 60)
        self.content_rect = rl.Rectangle(40, 145, 2000, 855)
        self._mouse_down_t = 1
        self._did_long_press = True
        self._is_pressed_prev = True
        self.home_rendered = self.home_updated = self.home_clicked = 0
        self._render_alerts_view = Mock()
        self._render_update_view = Mock()
        self._refresh = Mock()

      def _set_state(self, value):
        self.current_state = value

      def _render(self, rect):
        self.home_rendered += 1

      def _update_state(self):
        self.home_updated += 1

      def _handle_mouse_release(self, pos):
        self.home_clicked += 1

    class PanelType(IntEnum):
      DEVICE = 0
      TOGGLES = 1
      FIREHOSE = 2

    def module(name, **attributes):
      result = ModuleType(name)
      result.__dict__.update(attributes)
      sys.modules[name] = result

    module("openpilot.cereal.messaging", PubMaster=Mock())
    module("openpilot.selfdrive.ui.layouts.home", HomeLayout=Home, HomeLayoutState=HomeState, REFRESH_INTERVAL=10.0)
    module("openpilot.selfdrive.ui.mici.layouts.home", MiciHomeLayout=Home)
    module("openpilot.selfdrive.ui.sunnypilot.layouts.home", HomeLayoutSP=Home)
    module("openpilot.selfdrive.ui.sunnypilot.mici.layouts.home", MiciHomeLayoutSP=Home)
    module("openpilot.selfdrive.ui.layouts.sidebar", Sidebar=Base, SIDEBAR_WIDTH=300)
    module("openpilot.selfdrive.ui.layouts.settings.settings", SettingsLayout=Base, PanelType=PanelType)
    module("openpilot.selfdrive.ui.sunnypilot.layouts.settings.settings", SettingsLayoutSP=Base)
    module("openpilot.selfdrive.ui.mici.layouts.settings.settings", SettingsLayout=Base)
    module("openpilot.selfdrive.ui.sunnypilot.mici.layouts.settings", SettingsLayoutSP=Base)
    for prefix in ("openpilot.selfdrive.ui", "openpilot.selfdrive.ui.mici"):
      module(prefix + ".onroad.augmented_road_view", AugmentedRoadView=Base)
      module(prefix + ".layouts.onboarding", OnboardingWindow=Base)
    module("openpilot.selfdrive.ui.body.layouts.onroad", BodyLayout=Base)
    module("openpilot.selfdrive.ui.mici.layouts.offroad_alerts", MiciOffroadAlerts=Base)
    name = "openpilot.selfdrive.ui.mici.layouts.main" if scenario == "mici" else "openpilot.selfdrive.ui.layouts.main"
    main_module = importlib.import_module(name)
    main_class = main_module.MiciMainLayout if scenario == "mici" else main_module.MainLayout
    main = main_class.__new__(main_class)
    Widget.__init__(main)
    gui_app._nav_stack = []
    gui_app.push_widget(main)
    main._ui_lock = lock
    main._onboarding_window = Base()
    main._home_layout = main_module.LockableHomeLayout()
    main._home_layout.ui_lock = lock
    home = main._home_layout
    lock.render = Mock()
    main._settings_layout = Base()
    renderer = SimpleNamespace(get_alert=Mock(return_value=None))
    road = SimpleNamespace(_alert_renderer=renderer, alert_renderer=renderer)
    main._car_onroad_layout = road
    main._body_onroad_layout = road
    main._scroll_to = Mock()
    if scenario == "mici":
      home._update_state()
      home._render(rl.Rectangle(0, 0, 536, 240))
      assert home.home_updated == home.home_rendered == 0
      assert home._mouse_down_t is None and not home._did_long_press
      home._handle_mouse_release(MousePos(10, 10))
      assert isinstance(gui_app.get_active_widget(), ui_lock.PinKeyboard) and home.home_clicked == 0
      lock.dismiss_pin()
      main._open_settings()
      assert isinstance(gui_app.get_active_widget(), ui_lock.PinKeyboard)
      lock.dismiss_pin()
      gui_app.push_widget(main._settings_layout)
      gui_app.push_widget(Base())
      main._handle_ui_lock()
      assert gui_app.get_active_widget() is main and main._scroll_to.call_args.args[0] is home
      main._scroll_to.reset_mock()
      main._handle_ui_lock()
      main._scroll_to.assert_not_called()  # An alert/drive page on the root stays selected.
      state.ui_state.started = True
      main._handle_ui_lock()
      lock.request_pin()
      renderer.get_alert.return_value = object()
      main._handle_ui_lock()
      assert gui_app.get_active_widget() is main and main._scroll_to.call_args.args[0] is road
      controller.snapshot = snapshot(False, "2")
      lock.tick()
      home._update_state()
      home._render(rl.Rectangle(0, 0, 536, 240))
      home._handle_mouse_release(MousePos(10, 10))
      assert (home.home_updated, home.home_rendered, home.home_clicked) == (1, 1, 1)
    else:
      main._current_mode = main_module.MainState.HOME
      main._layouts = {main_module.MainState.HOME: home, main_module.MainState.SETTINGS: main._settings_layout,
                       main_module.MainState.ONROAD: road}
      main._sidebar = SimpleNamespace(render=Mock(), is_visible=True, set_visible=Mock())
      main._rect = rl.Rectangle(0, 0, 2160, 1080)
      main._sidebar_rect = rl.Rectangle(0, 0, 300, 1080)
      main._content_rect = rl.Rectangle(300, 0, 1860, 1080)
      main._render_main_content()
      main._sidebar.render.assert_not_called()
      assert home.rect.width == 2160 and home.home_rendered == 0
      home._set_state(HomeState.ALERTS)
      home._render(main._rect)
      home._render_alerts_view.assert_called_once()
      home._set_state(HomeState.UPDATE)
      home._render(main._rect)
      home._render_update_view.assert_called_once()
      home._set_state(HomeState.HOME)
      home._handle_mouse_release(MousePos(500, 800))
      assert not lock.pin_open  # The same release that closes an alert does not also open PIN.
      home._render(main._rect)
      home._handle_mouse_release(MousePos(1800, 50))
      assert lock.pin_open  # Blank home/header space also prompts, not just the content rectangle.
      lock.dismiss_pin()
      for panel in PanelType:
        main.open_settings(panel)
        assert isinstance(gui_app.get_active_widget(), ui_lock.PinKeyboard)
        assert main._current_mode == main_module.MainState.HOME
        lock.dismiss_pin()
      main._current_mode = main_module.MainState.SETTINGS
      main._settings_layout.hide_event = Mock()
      gui_app.push_widget(Base())
      main._handle_ui_lock()
      assert gui_app.get_active_widget() is main and main._current_mode == main_module.MainState.HOME
      state.ui_state.started = True
      main._handle_ui_lock()
      lock.request_pin()
      renderer.get_alert.return_value = object()
      road.show_event = Mock()
      main._handle_ui_lock()
      assert gui_app.get_active_widget() is main and main._current_mode == main_module.MainState.ONROAD


if __name__ == "__main__":
  _exercise(sys.argv[1])
