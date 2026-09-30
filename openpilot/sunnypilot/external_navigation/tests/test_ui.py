"""Production UI/NetworkManager methods with a fake display and D-Bus boundary.

These tests exercise actual rendering/callback code. They do not claim a physical
Comma display, NetworkManager service or touchscreen has been validated.
"""
import ast
from contextlib import ExitStack
from dataclasses import dataclass
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

from openpilot.sunnypilot.external_navigation import settings as navigation_settings

try:
  from PIL import ImageFont
except ImportError:
  ImageFont = None

OPENPILOT = Path(__file__).resolve().parents[3]
UI_PATH = OPENPILOT / 'selfdrive/ui/sunnypilot/external_navigation.py'
UI_NAME = 'openpilot.selfdrive.ui.sunnypilot.external_navigation'
FONT_PATH = OPENPILOT / 'selfdrive/assets/fonts/Inter-Medium.ttf'


@dataclass
class Rectangle:
  x: float
  y: float
  width: float
  height: float


class Button:
  def __init__(self, text, value='', *args, select_callback=None, **kwargs):
    self.text, self.value, self.options = text, value, value
    self.select_callback, self.enabled, self.callback = select_callback, True, None

  def set_click_callback(self, callback):
    self.callback = callback

  def set_value(self, value):
    self.value = value

  def set_enabled(self, enabled):
    self.enabled = enabled


class Scroller:
  def __init__(self):
    self.widgets = []
    self._scroller = SimpleNamespace(add_widgets=self.widgets.extend)

  def _update_state(self):
    pass


def module(name, **attributes):
  result = ModuleType(name)
  result.__dict__.update(attributes)
  return result


def load(name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  result = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(result)
  return result


def method(path, name, namespace):
  """Execute the production method without loading unrelated platform modules."""
  tree = ast.parse(path.read_text())
  function = next(item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef) and item.name == name)
  exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
  return namespace[name]


class TestNavigationUI(unittest.TestCase):
  def setUp(self):
    self.context = ExitStack()
    self.addCleanup(self.context.close)
    self.now = 10_000_000_000
    self.drawn, self.rectangles, self.dialogs = [], [], []
    self.settings_root = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
    self.context.enter_context(patch.dict('os.environ', {'RTZS_NAVIGATION_ROOT': str(self.settings_root)}))
    self.context.enter_context(patch.object(navigation_settings, 'revision_path', return_value=self.settings_root / 'network-revision'))
    self.assertTrue(navigation_settings.write_mode(1))
    self.nav = SimpleNamespace(connected=True, available=True, sourceFresh=True, distanceAgeMs=100,
                               receiveMonoTime=self.now, status='current', instruction='Turn right',
                               road='Rue Saint-Paul', nextDistance=35, hasNextDistance=True, branchRight=False)
    self.hint = SimpleNamespace(navigationAssisted=True, navigationHintStatus='assisted_turn_proposed')
    self.alert = SimpleNamespace(alertSize=0)
    class Messages(dict):
      pass
    self.messages = Messages(externalNavigationSP=self.nav, modelDataV2SP=self.hint, selfdriveState=self.alert)
    self.messages.alive = {'externalNavigationSP': True, 'modelDataV2SP': True}
    self.messages.valid = dict(self.messages.alive)
    self.offroad = True
    self.state = SimpleNamespace(sm=self.messages, is_offroad=lambda: self.offroad)
    self.fonts = {}
    def measure(font, text, size):
      if ImageFont is not None:
        key = int(size)
        if key not in self.fonts:
          self.fonts[key] = ImageFont.truetype(str(FONT_PATH), key)
        width = self.fonts[key].getlength(text)
      else:
        width = len(text) * size * .7
      return SimpleNamespace(x=width, y=size)
    self.measure = measure
    self.rl = module('pyray', Rectangle=Rectangle, Color=lambda *value: value,
                     Vector2=lambda x, y: SimpleNamespace(x=x, y=y), WHITE=(255, 255, 255, 255),
                     draw_rectangle_rec=lambda rect, color: self.rectangles.append(rect),
                     draw_text_ex=lambda font, text, pos, size, spacing, color: self.drawn.append((text, pos, size)))
    self.gui = SimpleNamespace(font=lambda weight: str(FONT_PATH), push_widget=self.dialogs.append)
    dependencies = {
      'pyray': self.rl,
      'openpilot.selfdrive.ui.ui_state': module('ui_state', ui_state=self.state),
      'openpilot.system.ui.lib.application': module('application', gui_app=self.gui, FontWeight=SimpleNamespace(MEDIUM='medium')),
      'openpilot.system.ui.lib.text_measure': module('text_measure', measure_text_cached=measure),
      'openpilot.selfdrive.ui.mici.widgets.button': module('button', BigButton=Button, BigMultiToggle=Button, GreyBigButton=Button),
      'openpilot.selfdrive.ui.mici.widgets.dialog': module('dialog', BigDialog=lambda title, description: (title, description)),
      'openpilot.system.ui.widgets.scroller': module('scroller', NavScroller=Scroller),
    }
    self.context.enter_context(patch.dict(sys.modules, dependencies))
    self.ui = load(UI_NAME, UI_PATH)
    self.context.enter_context(patch.dict(sys.modules, {UI_NAME: self.ui}))
    self.context.enter_context(patch.object(self.ui.time, 'monotonic_ns', lambda: self.now))

  def render(self, width=536, height=240):
    self.drawn.clear()
    self.rectangles.clear()
    rect = Rectangle(17, 23, width, height)
    self.ui.NavigationHud().render(rect)
    return rect

  def test_only_off_and_assisted_modes_are_present(self):
    self.assertEqual(self.ui.MODE_LABELS, ('Off', 'Assisted turns'))
    for value in (None, 0, 2, -1, 999):
      (self.settings_root / 'config/mode').write_text(str(value))
      self.assertEqual(self.ui.mode(), 0)
    self.assertTrue(navigation_settings.write_mode(1))
    self.assertEqual(self.ui.mode(), 1)
    self.assertNotIn('shadow', ' '.join(self.ui.MODE_LABELS).lower())

  def test_missing_stale_and_future_messages_hide_hud(self):
    for age_ns in (1_001_000_000, -1_000_000):
      self.nav.receiveMonoTime = self.now - age_ns
      self.render()
      self.assertEqual(self.drawn, [])
      self.assertNotIn('validated', self.ui.status_text())
      self.assertNotEqual(self.ui.status_text(), 'current')
    self.nav.receiveMonoTime = self.now
    self.messages.alive['externalNavigationSP'] = False
    self.render()
    self.assertEqual(self.drawn, [])
    self.assertEqual(self.ui.status_text(), 'Waiting for navigation receiver')

  def test_field_age_continues_aging_without_new_publications(self):
    self.nav.distanceAgeMs = 900
    self.nav.receiveMonoTime = self.now - 200_000_000
    self.render()
    self.assertTrue(self.drawn)
    self.assertNotIn('35 m', self.drawn[0][0])
    self.assertEqual(self.ui.status_text(), 'Guidance only · awaiting fresh distance')
    self.nav.distanceAgeMs = 800
    self.render()
    self.assertIn('35 m', self.drawn[0][0])
    self.assertIn('validated', self.ui.status_text())

  def test_alerts_off_and_unavailable_navigation_have_priority(self):
    self.alert.alertSize = 1
    self.render()
    self.assertFalse(self.drawn)
    self.alert.alertSize = 0
    self.assertTrue(navigation_settings.write_mode(0))
    self.render()
    self.assertFalse(self.drawn)
    self.assertTrue(navigation_settings.write_mode(1))
    self.nav.available = False
    self.render()
    self.assertFalse(self.drawn)

  def test_native_display_geometry_and_long_text_are_bounded(self):
    self.assertTrue(FONT_PATH.is_file())
    self.assertTrue((OPENPILOT / 'selfdrive/assets/icons_mici/settings/device/lkas.png').is_file())
    for width, height in ((536, 240), (2160, 1080)):
      for text in ('Turn right', 'École Avenue ' * 30, '高速公路出口 ' * 20, 'Drive\nright ' * 60):
        self.nav.instruction = text
        rect = self.render(width, height)
        self.assertEqual(len(self.rectangles), 1)
        box = self.rectangles[0]
        self.assertGreaterEqual(box.x, rect.x)
        self.assertGreaterEqual(box.y, rect.y)
        self.assertLessEqual(box.x + box.width, rect.x + rect.width)
        self.assertLessEqual(box.y + box.height, rect.y + rect.height)
        self.assertEqual(len(self.drawn), 2)
        for line, pos, size in self.drawn:
          self.assertNotIn('\n', line)
          self.assertGreaterEqual(pos.x, box.x)
          self.assertGreaterEqual(pos.y, box.y)
          self.assertLessEqual(pos.x + self.measure(None, line, size).x, box.x + box.width)
          self.assertLessEqual(pos.y + size, box.y + box.height)
      self.nav.branchRight = True
      self.render(width, height)
      self.assertIn('Right branch', self.drawn[1][0])
      self.nav.branchRight = False

  def test_hud_preserves_existing_control_regions(self):
    for width, height in ((536, 240), (2160, 1080)):
      rect = self.render(width, height)
      box = self.rectangles[0]
      if width == 536:
        reserved = (Rectangle(rect.x, rect.y, 162, 162),
                    Rectangle(rect.x + rect.width - 128, rect.y + 100, 108, 128))
      else:
        reserved = (Rectangle(rect.x + rect.width - 222, rect.y + 30, 192, 192),)
      for control in reserved:
        intersects = (box.x < control.x + control.width and box.x + box.width > control.x
                      and box.y < control.y + control.height and box.y + box.height > control.y)
        self.assertFalse(intersects, 'Navigation must not obscure existing speed/blindspot/engagement controls')

  def test_mici_mode_callback_and_status_dialog(self):
    layout_module = load('navigation_layout_test', OPENPILOT / 'selfdrive/ui/sunnypilot/mici/layouts/navigation.py')
    panel = layout_module.NavigationLayoutMici()
    self.assertEqual(panel._mode.options, ['Off', 'Assisted turns'])
    panel._select_mode('Off')
    self.assertEqual((self.settings_root / 'config/mode').read_bytes(), b'0\n')
    panel._select_mode('Assisted turns')
    self.assertEqual(navigation_settings.read_mode(), 1)
    self.offroad = False
    panel._select_mode('Off')
    self.assertEqual(navigation_settings.read_mode(), 1)
    panel._update_state()
    self.assertFalse(panel._mode.enabled)
    self.assertEqual(panel._mode.value, 'Assisted turns')
    panel._status.callback()
    self.assertEqual(self.dialogs[-1], ('External navigation', self.ui.status_text()))

  def test_standard_mode_callback_cannot_change_onroad(self):
    cycle = method(OPENPILOT / 'selfdrive/ui/sunnypilot/layouts/settings/models.py', '_cycle_navigation_mode',
                   {'ui_state': self.state, 'navigation_mode': self.ui.mode, 'MODE_LABELS': self.ui.MODE_LABELS,
                    'write_mode': navigation_settings.write_mode})
    cycle(None)
    self.assertEqual(navigation_settings.read_mode(), 0)
    cycle(None)
    self.assertEqual(navigation_settings.read_mode(), 1)
    self.offroad = False
    cycle(None)
    self.assertEqual(navigation_settings.read_mode(), 1)


class TestAutomaticWifiOperations(unittest.TestCase):
  def setUp(self):
    context = ExitStack()
    self.addCleanup(context.close)
    root = context.enter_context(tempfile.TemporaryDirectory())
    context.enter_context(patch.dict('os.environ', {'RTZS_NAVIGATION_ROOT': root}))
    context.enter_context(patch.object(navigation_settings, 'revision_path', return_value=Path(root) / 'network-revision'))
    self.before = navigation_settings.read_network_revision()
    self.assertIsNotNone(self.before)
    self.pending, self.calls = [], []
    source = OPENPILOT / 'system/ui/lib/wifi_manager.py'
    def thread(*, target, daemon):
      return SimpleNamespace(start=lambda: self.pending.append(target))
    namespace = {
      'read_network_revision': navigation_settings.read_network_revision,
      'record_network_action': navigation_settings.record_network_action,
      'threading': SimpleNamespace(Thread=thread),
      'cloudlog': SimpleNamespace(warning=lambda *args: None),
      'new_method_call': lambda *args: args,
      'MessageType': SimpleNamespace(error='error'),
      'time': SimpleNamespace(sleep=lambda seconds: None),
      'subprocess': SimpleNamespace(run=lambda *args, **kwargs: None),
    }
    activate = method(source, 'activate_connection', namespace)
    tether = method(source, 'set_tethering_active', namespace)
    self.manager = type('ActualNetworkOperations', (), {'activate_connection': activate, 'set_tethering_active': tether})()
    self.manager._connections, self.manager._wifi_device = {'weedle-test': '/saved/ap'}, '/device/wlan0'
    self.manager._tethering_ssid, self.manager._nm, self.manager._ipv4_forward = 'weedle-test', '/nm', True
    self.manager._set_connecting = lambda ssid: self.calls.append(('connecting', ssid))
    self.manager._deactivate_connection = lambda ssid: self.calls.append(('deactivate', ssid))
    self.manager._init_wifi_state = lambda: None
    self.manager._record_manual_network_action = navigation_settings.record_network_action
    def reply(call):
      self.calls.append(('dbus', call))
      return SimpleNamespace(header=SimpleNamespace(message_type='reply'))
    self.manager._router_main = SimpleNamespace(send_and_get_reply=reply)

  def test_network_ready_requires_completed_initial_state_query(self):
    source = OPENPILOT / 'system/ui/lib/wifi_manager.py'
    descriptor = method(source, 'navigation_network_ready', {})
    manager = SimpleNamespace(_initial_state_ready=False, _wifi_device='/wlan0',
                              _tethering_ssid='weedle-test', _connections={'weedle-test': '/profile'})
    self.assertFalse(descriptor.fget(manager))
    manager._initial_state_ready = True
    self.assertTrue(descriptor.fget(manager))
    manager._wifi_device = None
    self.assertFalse(descriptor.fget(manager))
    manager._wifi_device = '/wlan0'
    manager._connections.clear()
    self.assertFalse(descriptor.fget(manager))

  def test_queued_auto_connection_cannot_steal_manual_target(self):
    self.manager.activate_connection('weedle-test', manual=False, expected_revision=self.before)
    self.assertEqual(self.calls, [])
    navigation_settings.record_network_action()
    self.pending.pop()()
    self.assertEqual(self.calls, [])

  def test_current_owned_activation_uses_saved_profile(self):
    self.manager.activate_connection('weedle-test', manual=False, block=True, expected_revision=self.before)
    self.assertEqual(self.calls[0], ('connecting', 'weedle-test'))
    self.assertEqual(self.calls[1][1][-1], ('/saved/ap', '/device/wlan0', '/'))
    self.assertFalse(self.pending)
    self.assertEqual(navigation_settings.read_network_revision(), self.before)

  def test_close_deactivation_finishes_before_manager_teardown(self):
    self.manager.set_tethering_active(False, manual=False, block=True, expected_revision=self.before)
    self.assertEqual(self.calls, [('deactivate', 'weedle-test')])
    self.assertFalse(self.pending)
    navigation_settings.record_network_action()
    self.calls.clear()
    self.manager.set_tethering_active(False, manual=False, block=True, expected_revision=self.before)
    self.assertFalse(self.calls)

  def test_worker_restores_owned_ap_before_stopping_manager(self):
    from openpilot.sunnypilot.external_navigation import hotspot
    HotspotWorker = hotspot.HotspotWorker
    worker = HotspotWorker.__new__(HotspotWorker)
    worker.params = SimpleNamespace()
    worker._wanted, worker._enabled, worker._closed, worker._ap_ready = False, False, False, False
    calls = []
    class Network:
      navigation_network_ready = True
      connected_ssid = connecting_to_ssid = None
      tethering_ssid = 'weedle-test'
      def set_active(self, value):
        calls.append(('scan', value))
      def activate_connection(self, ssid, **kwargs):
        calls.append(('activate', kwargs))
        self.connected_ssid = ssid
      def set_tethering_active(self, value, **kwargs):
        calls.append(('deactivate', kwargs))
        self.connected_ssid = None
      def stop(self):
        calls.append(('stop',))
    states = iter(((True, True, False), (False, True, True)))
    class Condition:
      def __enter__(self):
        return self
      def __exit__(self, *args):
        return False
      def wait(self, timeout):
        worker._wanted, worker._enabled, worker._closed = next(states)
    worker._condition = Condition()
    fake = module('wifi_manager', WifiManager=Network)
    with patch.dict(sys.modules, {'openpilot.system.ui.lib.wifi_manager': fake}):
      worker._run()
    self.assertEqual([item[0] for item in calls], ['scan', 'activate', 'deactivate', 'stop'])
    for item in calls[1:3]:
      self.assertTrue(item[1]['block'])
      self.assertFalse(item[1]['manual'])
      self.assertEqual(item[1]['expected_revision'], self.before)
    self.assertFalse(worker._ap_ready)

  def test_manual_default_remains_async_and_revokes_automatic_ownership(self):
    self.manager.set_tethering_active(False)
    self.assertFalse(self.calls)
    self.assertNotEqual(navigation_settings.read_network_revision(), self.before)
    self.pending.pop()()
    self.assertEqual(self.calls, [('deactivate', 'weedle-test')])


if __name__ == '__main__':
  unittest.main()
