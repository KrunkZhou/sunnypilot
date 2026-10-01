"""Deterministic replay and ideal-plant checks, separate from native control tests."""
from dataclasses import replace
import math
import unittest

try:
  import numpy as np
  from openpilot.sunnypilot.external_navigation.tests.speed_replay import (
    DT, TURN_SPEED, load_cruise_policy, replay, sample, simulate_approach,
  )
except ModuleNotFoundError as exc:
  if exc.name != 'numpy':
    raise
  np = None


@unittest.skipIf(np is None, 'NumPy is required for the ideal cruise-policy plant')
class TestNavigationSpeedReplay(unittest.TestCase):
  def test_ordinary_approaches_start_earlier_at_higher_speed(self):
    starts = []
    for speed in (30, 50, 80):
      rows = simulate_approach(speed)
      selected = next(row for row in rows if row.cap is not None)
      starts.append(selected.distance)
      self.assertLessEqual(selected.cap, speed / 3.6)
      self.assertLess(rows[-1].speed, speed / 3.6)
      # This is only the response of this ideal plant, not a vehicle acceptance bound.
      self.assertLess(abs(rows[-1].speed - TURN_SPEED), 1.)
      self.assertTrue(all(math.isfinite(row.speed) and math.isfinite(row.acceleration) for row in rows))
    self.assertGreater(starts[1], starts[0])
    self.assertGreater(starts[2], starts[1])
    self.assertGreater(starts[2], 400.)

  def test_late_guidance_obeys_existing_cruise_slew(self):
    policy = load_cruise_policy()
    for experimental in (False, True):
      rows = simulate_approach(80, late_distance=5., experimental=experimental)
      previous = 0.
      for row in rows:
        jerk = float(np.interp(row.speed, policy['A_CRUISE_MAX_BP'], policy['J_CRUISE_VALS']))
        self.assertLessEqual(abs(row.acceleration - previous), jerk * DT + 1e-8)
        self.assertGreaterEqual(row.acceleration, policy['A_CRUISE_MIN'])
        previous = row.acceleration
      self.assertGreater(rows[-1].speed, TURN_SPEED + 5.)  # Late receipt cannot produce instant braking.

  def test_repeated_snapshots_expire_and_fresh_observation_recovers(self):
    initial = sample(30., 1)
    decisions = replay([(0., initial), (.5, initial), (1.01, initial), (1.1, sample(15., 2))])
    self.assertIsNotNone(decisions[0].speed_cap)
    self.assertIsNotNone(decisions[1].speed_cap)
    self.assertIsNone(decisions[2].speed_cap)
    self.assertIsNotNone(decisions[3].speed_cap)

  def test_delivery_delay_counts_against_observation_ttl(self):
    # The adapter supplies effective ages (source 600 ms + delivery 500 ms).
    delayed = replace(sample(30., 1, 600 + 500), delivery_age_ms=500)
    self.assertIsNone(replay([(0., delayed)])[0].speed_cap)

  def test_stale_route_restores_ordinary_target_and_cruise_slew(self):
    decisions = replay([(0., sample(30., 1)), (.1, None)])
    self.assertIsNotNone(decisions[0].speed_cap)
    self.assertIsNone(decisions[1].speed_cap)
    policy = load_cruise_policy()
    from types import SimpleNamespace
    previous = -.8
    cp = SimpleNamespace(steerRatio=15., wheelbase=2.7)
    acceleration = policy['get_cruise_accel'](False, 50 / 3.6, 10., previous, 0., cp, DT, 2., True)
    jerk = float(np.interp(10., policy['A_CRUISE_MAX_BP'], policy['J_CRUISE_VALS']))
    self.assertLessEqual(acceleration - previous, jerk * DT + 1e-8)
    self.assertLess(acceleration, 0.)

  def test_back_to_back_turn_identity_replaces_old_approach(self):
    decisions = replay([(0., sample(10., 1)), (.2, sample(200., 1, maneuver=2))])
    self.assertIsNotNone(decisions[0].speed_cap)
    self.assertIsNone(decisions[1].speed_cap)
    self.assertGreater(decisions[1].raw_cap, decisions[0].raw_cap)


if __name__ == '__main__':
  unittest.main()
