import unittest

from openpilot.sunnypilot.external_navigation.hotspot import HotspotPolicy, NetworkState


def state(connected="home", connecting=None, revision="", ready=True):
  return NetworkState(ready, connected, connecting, "weedle-test", revision)


class TestHotspotPolicy(unittest.TestCase):
  def test_offroad_never_changes_network(self):
    policy = HotspotPolicy()
    self.assertIsNone(policy.update(False, state(), 0))

  def test_owned_hotspot_restores_previous_network(self):
    policy = HotspotPolicy()
    self.assertEqual(policy.update(True, state(), 0), ("activate", "weedle-test"))
    self.assertIsNone(policy.update(True, state("weedle-test"), 1))
    self.assertEqual(policy.update(False, state("weedle-test"), 2), ("activate", "home"))
    self.assertIsNone(policy.update(False, state("home"), 3))

  def test_preexisting_hotspot_is_never_owned(self):
    policy = HotspotPolicy()
    self.assertIsNone(policy.update(True, state("weedle-test"), 0))
    self.assertIsNone(policy.update(False, state("weedle-test"), 1))

  def test_manual_change_cancels_retry_and_restore(self):
    policy = HotspotPolicy()
    policy.update(True, state(), 0)
    self.assertIsNone(policy.update(True, state(None, revision="manual"), 1))
    self.assertIsNone(policy.update(True, state("other", revision="manual"), 40))
    self.assertIsNone(policy.update(False, state("other", revision="manual"), 41))

  def test_external_network_switch_cancels_ownership(self):
    policy = HotspotPolicy()
    policy.update(True, state(), 0)
    policy.update(True, state("weedle-test"), 1)
    self.assertIsNone(policy.update(True, state("other"), 2))
    self.assertIsNone(policy.update(False, state("other"), 3))

  def test_failed_start_does_not_toggle_radio_forever(self):
    policy = HotspotPolicy()
    policy.update(True, state(), 0)
    self.assertIsNone(policy.update(True, state(None), 16))
    self.assertIsNone(policy.update(True, state(None), 60))
    self.assertIsNone(policy.update(False, state(None), 61))
    self.assertEqual(policy.update(True, state(None), 62), ("activate", "weedle-test"))

  def test_no_prior_network_deactivates_owned_hotspot(self):
    policy = HotspotPolicy()
    policy.update(True, state(None), 0)
    self.assertEqual(policy.update(False, state("weedle-test"), 1), ("deactivate", "weedle-test"))

  def test_stop_during_activation_cancels_owned_hotspot(self):
    policy = HotspotPolicy()
    policy.update(True, state(None), 0)
    self.assertEqual(policy.update(False, state(None, "weedle-test"), 1), ("deactivate", "weedle-test"))

  def test_wait_for_network_readiness_and_manual_connection(self):
    policy = HotspotPolicy()
    self.assertIsNone(policy.update(True, state(ready=False), 0))
    self.assertIsNone(policy.update(True, state(None, "home"), 1))
    self.assertEqual(policy.update(True, state("home"), 2), ("activate", "weedle-test"))


if __name__ == "__main__":
  unittest.main()
