"""Actual worker shutdown with delayed networking; no live radios or state files."""
from contextlib import ExitStack
import sys
import threading
import time
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from openpilot.sunnypilot.external_navigation import hotspot


class HotspotCleanupTests(unittest.TestCase):
  def setUp(self):
    self.context = ExitStack()
    self.addCleanup(self.context.close)
    self.calls = []
    self.revision = 'original'
    self.created = threading.Event()
    self.activation_started = threading.Event()
    self.activated = threading.Event()
    self.restoration_started = threading.Event()
    self.restore_gate = threading.Event()
    self.restore_gate.set()
    self.activation_gate = threading.Event()
    self.activation_gate.set()
    self.preexisting = False
    test = self

    class Network:
      navigation_network_ready = True
      connecting_to_ssid = None
      tethering_ssid = 'weedle-test'
      def __init__(self):
        self.connected_ssid = self.tethering_ssid if test.preexisting else 'previous-network'
      def set_active(self, value):
        test.calls.append(('scan', value))
        test.created.set()
      def activate_connection(self, ssid, **kwargs):
        test.calls.append(('activate', ssid))
        test.assertTrue(kwargs['block'])
        test.assertFalse(kwargs['manual'])
        test.assertEqual(kwargs['expected_revision'], 'original')
        if ssid == self.tethering_ssid:
          self.connecting_to_ssid = ssid
          test.activation_started.set()
          if not test.activation_gate.wait(8):
            raise RuntimeError('test activation not released')
          self.connected_ssid, self.connecting_to_ssid = ssid, None
          test.activated.set()
        else:
          test.restoration_started.set()
          if not test.restore_gate.wait(8):
            raise RuntimeError('test restoration not released')
          self.connected_ssid = ssid
          test.calls.append(('restored', ssid))
      def set_tethering_active(self, *args, **kwargs):
        raise AssertionError('saved previous network must be restored')
      def stop(self):
        test.calls.append(('stop',))

    fake_wifi = ModuleType('wifi_manager')
    fake_wifi.WifiManager = Network
    fake_logging = ModuleType('swaglog')
    self.log = fake_logging.cloudlog = Mock()
    self.context.enter_context(patch.dict(sys.modules, {
      'openpilot.system.ui.lib.wifi_manager': fake_wifi,
      'openpilot.common.swaglog': fake_logging,
    }))
    self.context.enter_context(patch.object(hotspot, 'read_network_revision', lambda: self.revision))
    self.worker = None
    self.addCleanup(self.release_worker)

  def release_worker(self):
    self.activation_gate.set()
    self.restore_gate.set()
    if self.worker is not None:
      self.worker.close()

  def start(self, wait_activation=True):
    self.worker = hotspot.HotspotWorker(None)
    self.worker.update(True, True)
    self.assertTrue(self.created.wait(2))
    if wait_activation and not self.preexisting:
      self.assertTrue(self.activated.wait(2))

  def release_after(self, gate, delay):
    timer = threading.Timer(delay, gate.set)
    timer.start()
    self.addCleanup(timer.join)

  def test_delayed_restore_finishes_before_close_returns(self):
    self.start()
    self.restore_gate.clear()
    self.release_after(self.restore_gate, 1.15)  # Longer than the previous 1s shutdown wait.
    self.assertTrue(self.worker.close())
    self.assertTrue(self.restoration_started.is_set())
    self.assertEqual(self.calls[-2:], [('restored', 'previous-network'), ('stop',)])
    self.assertFalse(self.worker._thread.is_alive())
    self.log.warning.assert_not_called()

  def test_stalled_cleanup_is_bounded_and_reports_fixed_warning(self):
    self.start()
    self.restore_gate.clear()
    started = time.monotonic()
    self.assertFalse(self.worker.close())
    elapsed = time.monotonic() - started
    self.assertGreaterEqual(elapsed, 2.9)
    self.assertLess(elapsed, 4.)  # Reserve diagnostic drain time inside manager's 5s grace.
    self.assertTrue(self.worker._thread.is_alive())
    self.assertNotIn(('stop',), self.calls)
    self.log.warning.assert_called_once_with(
      'External navigation hotspot cleanup timed out; previous Wi-Fi may not be restored')
    self.restore_gate.set()
    self.assertTrue(self.worker.close())
    self.assertEqual(self.calls[-2:], [('restored', 'previous-network'), ('stop',)])

  def test_close_during_activation_waits_then_restores(self):
    self.activation_gate.clear()
    self.start(wait_activation=False)
    self.assertTrue(self.activation_started.wait(2))
    self.release_after(self.activation_gate, 1.15)
    self.assertTrue(self.worker.close())
    self.assertEqual(self.calls[-3:], [('activate', 'previous-network'), ('restored', 'previous-network'), ('stop',)])
    self.log.warning.assert_not_called()

  def test_restarted_worker_never_adopts_preexisting_hotspot(self):
    self.preexisting = True
    self.start()
    self.assertTrue(self.worker.close())
    self.assertEqual(self.calls, [('scan', False), ('stop',)])
    self.log.warning.assert_not_called()

  def test_manual_revision_change_prevents_shutdown_restore(self):
    self.start()
    self.revision = 'manual'
    self.assertTrue(self.worker.close())
    self.assertEqual(self.calls, [('scan', False), ('activate', 'weedle-test'), ('stop',)])
    self.log.warning.assert_not_called()


if __name__ == '__main__':
  unittest.main()
