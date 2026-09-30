from dataclasses import replace
import unittest

from openpilot.sunnypilot.external_navigation.policy import Sample, TurnPolicy


class PolicyTests(unittest.TestCase):
  def sample(self, distance=30, observation=1, **changes):
    sample = Sample((b'x' * 16, 1, 2, 3, 42), 1, 1, distance, observation, 0)
    return replace(sample, **changes)

  def step(self, policy, sample, **changes):
    args = {'mode': 1, 'lateral_active': True, 'speed': 4., 'speed_limit': 8.}
    args.update(changes)
    return policy.update(sample, **args)

  def test_crossing_once(self):
    policy = TurnPolicy()
    self.assertEqual(self.step(policy, self.sample()).desire, 0)
    self.assertEqual(self.step(policy, self.sample(14, 2)).desire, 1)
    self.assertEqual(self.step(policy, self.sample(12, 3)).desire, 0)
    self.step(policy, None)
    self.step(policy, self.sample(30, 4))
    self.assertEqual(self.step(policy, self.sample(14, 5)).desire, 0)

  def test_right_and_consecutive_turns(self):
    policy = TurnPolicy()
    for identity in (1, 2):
      self.step(policy, self.sample(identity=(b'x' * 16, 1, 2, 3, identity), maneuver_type=2))
      result = self.step(policy, self.sample(10, 2, identity=(b'x' * 16, 1, 2, 3, identity), maneuver_type=2))
      self.assertEqual(result.desire, 2)

  def test_late_attach_never_triggers(self):
    policy = TurnPolicy()
    for observation, distance in enumerate((15, 14, 10, 0), 1):
      self.assertEqual(self.step(policy, self.sample(distance, observation)).desire, 0)

  def test_repeated_observation_cannot_trigger(self):
    policy = TurnPolicy()
    self.step(policy, self.sample())
    self.assertEqual(self.step(policy, self.sample(10, 1)).desire, 0)
    self.assertEqual(self.step(policy, self.sample(10, 2)).desire, 1)

  def test_manual_stale_reroute_reset_cancels_pending(self):
    for changes in ({'manual': True}, {'lateral_active': False}, {'speed': 10.}, {'mode': 0}):
      policy = TurnPolicy()
      self.step(policy, self.sample())
      self.assertEqual(self.step(policy, self.sample(20, 2), **changes).desire, 0)
      self.assertEqual(self.step(policy, self.sample(10, 3)).desire, 0)
    for sample in (None, self.sample(20, 2, route_state=5), self.sample(20, 2, distance_age_ms=1001),
                   self.sample(20, 2, connected=False), self.sample(20, 2, supports_hints=False)):
      policy = TurnPolicy()
      self.step(policy, self.sample())
      self.step(policy, sample)
      self.assertEqual(self.step(policy, self.sample(10, 3)).desire, 0)

  def test_assisted_mode_emits_turn_without_approval(self):
    policy = TurnPolicy()
    self.step(policy, self.sample())
    result = self.step(policy, self.sample(10, 2))
    self.assertEqual(result.proposal, 1)
    self.assertEqual(result.desire, 1)
    self.assertTrue(result.assisted)
    self.assertEqual(result.reason, 'assisted_turn_proposed')

  def test_no_highway_lanechange_or_other_desires(self):
    for maneuver in range(256):
      policy = TurnPolicy()
      self.step(policy, self.sample(maneuver_type=maneuver))
      result = self.step(policy, self.sample(10, 2, maneuver_type=maneuver))
      self.assertIn(result.desire, (0, 1, 2))
      self.assertEqual(bool(result.desire), maneuver in (1, 2, 20, 21))
      if maneuver in (14, 23, 53):
        self.assertEqual(result.reason, 'branch_right_advisory_only')

  def test_speed_boundary_and_nan(self):
    for speed in (8., float('nan'), float('inf'), -1.):
      policy = TurnPolicy()
      self.step(policy, self.sample(), speed=speed)
      self.assertEqual(self.step(policy, self.sample(10, 2), speed=speed).desire, 0)

  def test_increasing_distance_requires_new_approach(self):
    policy = TurnPolicy()
    self.step(policy, self.sample(20, 1))
    self.step(policy, self.sample(30, 2))
    self.assertEqual(self.step(policy, self.sample(10, 3)).desire, 0)


if __name__ == '__main__':
  unittest.main()
