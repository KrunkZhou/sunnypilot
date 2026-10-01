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


@unittest.skipIf(np is None, 'NumPy is required for the cruise policy')
class TestCruisePolicy(unittest.TestCase):
  def setUp(self):
    self.policy = load_cruise_policy()
    self.accel = self.policy['get_cruise_accel']

  def test_custom_maximum_knots_and_interpolation(self):
    for speed, maximum in ((0., 2.), (5., 2.), (7.5, 1.975), (10., 1.95), (15., 1.6),
                           (20., 1.3), (25., 1.05), (30., .95), (40., .75), (50., .75)):
      with self.subTest(speed=speed):
        self.assertAlmostEqual(self.policy['get_max_accel'](speed), maximum)
        self.assertAlmostEqual(self.accel(speed + 10., speed, 0., 10.), maximum)

  def test_custom_jerk_knots_limit_both_directions(self):
    for speed, jerk in ((0., 2.), (5., 2.), (10., 1.95), (15., 1.6), (20., 1.3), (25., 1.05), (40., .75)):
      for direction in (-1., 1.):
        with self.subTest(speed=speed, direction=direction):
          self.assertAlmostEqual(self.accel(speed + direction * 10., speed, 0., DT), direction * jerk * DT)

  def test_jerk_uses_its_own_breakpoints(self):
    self.policy['J_CRUISE_BP'] = [0., 20., 40.]
    self.policy['J_CRUISE_VALS'] = [3., 2., 1.]
    self.assertAlmostEqual(self.accel(30., 20., 0., DT), .1)
    self.assertAlmostEqual(self.accel(30., 20., 0., 10.), 1.3)

  def test_sustained_targets_reach_custom_maximum_and_upstream_minimum(self):
    self.assertEqual(self.policy['A_CRUISE_MIN'], -.5)
    for target, expected in ((30., 1.3), (10., -.5)):
      with self.subTest(target=target):
        previous = 0.
        for _ in range(50):
          acceleration = self.accel(target, 20., previous, DT)
          self.assertLessEqual(abs(acceleration - previous), .065 + 1e-8)
          previous = acceleration
        self.assertAlmostEqual(previous, expected)

  def test_reset_history_outside_target_limits_recovers_without_a_jump(self):
    for previous, expected in ((2., 1.935), (-1.2, -1.135)):
      with self.subTest(previous=previous):
        self.assertAlmostEqual(self.accel(30., 20., previous, DT), expected)

  def test_global_acceleration_limit_remains_authoritative(self):
    self.policy['ACCEL_MAX'] = 1.
    self.assertAlmostEqual(self.accel(30., 20., 0., 10.), 1.)


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
    rows = simulate_approach(80, late_distance=5.)
    previous = 0.
    for row in rows:
      jerk = float(np.interp(row.speed, policy['J_CRUISE_BP'], policy['J_CRUISE_VALS']))
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
    previous = -.8
    acceleration = policy['get_cruise_accel'](50 / 3.6, 10., previous, DT)
    jerk = float(np.interp(10., policy['J_CRUISE_BP'], policy['J_CRUISE_VALS']))
    self.assertLessEqual(acceleration - previous, jerk * DT + 1e-8)
    self.assertLess(acceleration, 0.)

  def test_back_to_back_turn_identity_replaces_old_approach(self):
    decisions = replay([(0., sample(10., 1)), (.2, sample(200., 1, maneuver=2))])
    self.assertIsNotNone(decisions[0].speed_cap)
    self.assertIsNone(decisions[1].speed_cap)
    self.assertGreater(decisions[1].raw_cap, decisions[0].raw_cap)


if __name__ == '__main__':
  unittest.main()
