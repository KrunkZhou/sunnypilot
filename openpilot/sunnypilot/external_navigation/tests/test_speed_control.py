from dataclasses import replace
import math
import unittest

from openpilot.sunnypilot.external_navigation.speed_control import NavigationSpeedController, NavigationSpeedSample


TURN_SPEED = 14 * 0.44704
UNKNOWN_AGE = 0xffffffff


class NavigationSpeedTests(unittest.TestCase):
  def sample(self, distance=100., observation=1, **changes):
    sample = NavigationSpeedSample(identity=(b'p' * 16, 1, 2, 3, 42), transport=(b'r' * 16, 1),
                                   route_state=1, maneuver_type=1, distance=distance,
                                   distance_observation=observation, maneuver_observation=1,
                                   distance_age_ms=0, maneuver_age_ms=0, delivery_age_ms=0,
                                   connected=True, available=True)
    return replace(sample, **changes)

  def step(self, controller, sample, now=10., **changes):
    args = {'v_ego': 80 / 3.6, 'baseline_target': 80 / 3.6}
    args.update(changes)
    return controller.update(sample, now_ns=round(now * 1e9), **args)

  def test_formula_units_and_zero_distance(self):
    for distance in (0., 8., 30., 100., 300.):
      with self.subTest(distance=distance):
        result = self.step(NavigationSpeedController(), self.sample(distance))
        expected = math.sqrt(TURN_SPEED ** 2 + .9 * max(distance - 8., 0.))
        self.assertAlmostEqual(result.raw_cap, expected, places=6)
        self.assertAlmostEqual(result.speed_cap, expected, places=6)
        self.assertGreater(result.speed_cap, 0.)

  def test_higher_speed_starts_earlier_without_lateral_gate(self):
    # At 100 m the envelope is below 50/80 km/h, but above 30 km/h.
    for kph, limits in ((30, False), (50, True), (80, True)):
      with self.subTest(kph=kph):
        result = self.step(NavigationSpeedController(), self.sample(), v_ego=kph / 3.6, baseline_target=kph / 3.6)
        self.assertEqual(result.speed_cap is not None, limits)
        if limits:
          self.assertLess(result.speed_cap, kph / 3.6)

  def test_cap_never_replaces_a_lower_target(self):
    result = self.step(NavigationSpeedController(), self.sample(), baseline_target=5., v_ego=4.)
    self.assertIsNone(result.speed_cap)
    self.assertGreater(result.raw_cap, 5.)

  def test_activation_and_release_hysteresis(self):
    controller = NavigationSpeedController()
    cap = math.sqrt(TURN_SPEED ** 2 + .9 * 92.)
    self.assertIsNone(self.step(controller, self.sample(), baseline_target=cap + .20).speed_cap)
    self.assertIsNotNone(self.step(controller, self.sample(100., 2), now=10.1, baseline_target=cap + .30).speed_cap)
    self.assertIsNotNone(self.step(controller, self.sample(100., 3), now=10.2, baseline_target=cap + .15).speed_cap)
    self.assertIsNone(self.step(controller, self.sample(100., 4), now=10.3, baseline_target=cap + .05).speed_cap)

  def test_disabled_inactive_and_driver_priority_remove_cap(self):
    for changes in ({'enabled': False}, {'longitudinal_active': False}, {'driver_override': True},
                    {'longitudinal_active': False, 'inhibit_reason': 'unsupported_longitudinal'}):
      with self.subTest(changes=changes):
        controller = NavigationSpeedController()
        self.assertIsNotNone(self.step(controller, self.sample()).speed_cap)
        result = self.step(controller, self.sample(90., 2), now=10.1, **changes)
        self.assertIsNone(result.speed_cap)

  def test_invalid_navigation_restores_optional_no_cap(self):
    samples = [None, self.sample(distance=None), self.sample(-1.), self.sample(float('nan')),
               self.sample(float('inf')), self.sample(1e12), self.sample(identity=None),
               self.sample(transport=None), self.sample(maneuver_type=None),
               self.sample(distance_observation=0), self.sample(maneuver_observation=0),
               self.sample(distance_age_ms=UNKNOWN_AGE), self.sample(maneuver_age_ms=UNKNOWN_AGE),
               self.sample(distance_age_ms=-1), self.sample(maneuver_age_ms=-1),
               self.sample(delivery_age_ms=-1), self.sample(delivery_age_ms=1001),
               self.sample(distance_age_ms=1001), self.sample(connected=False), self.sample(available=False)]
    for sample in samples:
      with self.subTest(sample=sample):
        result = self.step(NavigationSpeedController(), sample)
        self.assertIsNone(result.speed_cap)
        self.assertTrue(result.reason)
    for route in (0, 2, 3, 4, 5, 6, 255):
      self.assertIsNone(self.step(NavigationSpeedController(), self.sample(route_state=route)).speed_cap)

  def test_invalid_vehicle_values_and_clock_remove_cap(self):
    for field in ('v_ego', 'baseline_target'):
      for value in (-1., float('nan'), float('inf')):
        with self.subTest(field=field, value=value):
          self.assertIsNone(self.step(NavigationSpeedController(), self.sample(), **{field: value}).speed_cap)
    controller = NavigationSpeedController()
    self.step(controller, self.sample())
    self.assertIsNone(self.step(controller, self.sample(90., 2), now=9.).speed_cap)

  def test_only_verified_ordinary_turn_types(self):
    for maneuver in range(256):
      with self.subTest(maneuver=maneuver):
        result = self.step(NavigationSpeedController(), self.sample(maneuver_type=maneuver))
        self.assertEqual(result.speed_cap is not None, maneuver in (1, 2, 20, 21))

  def test_old_selection_is_valid_with_fresh_bound_distance(self):
    result = self.step(NavigationSpeedController(), self.sample(maneuver_age_ms=120_000))
    self.assertIsNotNone(result.speed_cap)

  def test_repeated_snapshots_cannot_rejuvenate_observation(self):
    controller = NavigationSpeedController()
    sample = self.sample(distance_age_ms=200)
    self.assertIsNotNone(self.step(controller, sample).speed_cap)
    self.assertIsNotNone(self.step(controller, replace(sample, distance_age_ms=0), now=10.5).speed_cap)
    self.assertIsNone(self.step(controller, replace(sample, distance_age_ms=0), now=10.801).speed_cap)
    self.assertIsNotNone(self.step(controller, self.sample(100., 2), now=10.9).speed_cap)

  def test_clock_regression_does_not_reset_age_floor(self):
    controller = NavigationSpeedController()
    self.step(controller, self.sample(distance_age_ms=200))
    self.step(controller, self.sample(), now=10.5)
    self.assertEqual(self.step(controller, self.sample(), now=10.1).reason, 'clock_invalid')
    expired = self.step(controller, self.sample(), now=10.801)
    self.assertIsNone(expired.speed_cap)
    self.assertGreater(expired.sample.distance_age_ms, 1000)
    self.assertIsNotNone(self.step(controller, self.sample(95., 2), now=10.9).speed_cap)

  def test_actual_repeated_numeric_observation_renews_distance(self):
    controller = NavigationSpeedController()
    self.step(controller, self.sample())
    self.assertIsNotNone(self.step(controller, self.sample(100., 2), now=10.9).speed_cap)
    self.assertIsNotNone(self.step(controller, self.sample(100., 3), now=11.8).speed_cap)

  def test_distance_jitter_does_not_raise_cap(self):
    controller = NavigationSpeedController()
    previous = self.step(controller, self.sample()).speed_cap
    for observation, distance in enumerate((103., 99., 102., 98.), 2):
      result = self.step(controller, self.sample(distance, observation), now=10. + observation * .1)
      self.assertIsNotNone(result.speed_cap)
      self.assertLessEqual(result.speed_cap, previous)
      previous = result.speed_cap

  def test_distance_discontinuity_requires_another_observation(self):
    for before, jump, after in ((100., 250., 245.), (300., 100., 95.)):
      with self.subTest(jump=jump):
        controller = NavigationSpeedController()
        self.assertIsNotNone(self.step(controller, self.sample(before)).speed_cap)
        self.assertIsNone(self.step(controller, self.sample(jump, 2), now=10.1).speed_cap)
        self.assertIsNone(self.step(controller, self.sample(jump, 2), now=10.2).speed_cap)
        self.assertIsNotNone(self.step(controller, self.sample(after, 3), now=10.3).speed_cap)

  def test_interruption_does_not_erase_discontinuity_confirmation(self):
    controller = NavigationSpeedController()
    self.step(controller, self.sample(500.))
    self.assertIsNone(self.step(controller, self.sample(20., 2), now=10.1).speed_cap)
    self.assertIsNone(self.step(controller, None, now=10.2).speed_cap)
    self.assertIsNone(self.step(controller, self.sample(10., 3), now=10.3).speed_cap)
    self.assertIsNotNone(self.step(controller, self.sample(9., 4), now=10.4).speed_cap)

  def test_counter_conflict_and_regression_retire_identity(self):
    for conflicting in (self.sample(80., 2, maneuver_observation=2), self.sample(90., 1, maneuver_observation=2),
                        self.sample(90., 3, maneuver_observation=1), self.sample(90., 3, maneuver_observation=2, maneuver_type=2),
                        self.sample(90., 3, maneuver_observation=2, maneuver_type=23)):
      with self.subTest(conflicting=conflicting):
        controller = NavigationSpeedController()
        self.step(controller, self.sample(100., 2, maneuver_observation=2))
        self.assertIsNone(self.step(controller, conflicting, now=10.1).speed_cap)
        self.assertIsNone(self.step(controller, self.sample(75., 4, maneuver_observation=3), now=10.2).speed_cap)

  def test_repeated_selection_observation_age_cannot_get_younger(self):
    controller = NavigationSpeedController()
    self.step(controller, self.sample(maneuver_age_ms=4000))
    result = self.step(controller, self.sample(95., 2, maneuver_age_ms=0), now=10.2)
    self.assertIsNotNone(result.speed_cap)
    self.assertEqual(result.sample.maneuver_age_ms, 4200)
    self.assertEqual(result.sample.distance_age_ms, 0)

  def test_route_identity_changes_never_reuse_distance(self):
    for index, replacement in ((0, b'q' * 16), (1, 9), (2, 9), (3, 9), (4, 43)):
      with self.subTest(index=index):
        controller = NavigationSpeedController()
        original = self.sample(8.)
        self.step(controller, original)
        identity = list(original.identity)
        identity[index] = replacement
        missing = self.sample(distance=None, identity=tuple(identity))
        self.assertIsNone(self.step(controller, missing, now=10.1).speed_cap)
        next_turn = self.sample(50., 2, identity=tuple(identity))
        result = self.step(controller, next_turn, now=10.2)
        self.assertAlmostEqual(result.raw_cap, math.sqrt(TURN_SPEED ** 2 + .9 * 42.), places=6)

  def test_transport_restart_requires_advancing_observation(self):
    for transport in ((b's' * 16, 1), (b'r' * 16, 2)):
      with self.subTest(transport=transport):
        controller = NavigationSpeedController()
        self.step(controller, self.sample())
        changed = self.sample(90., 2, transport=transport)
        self.assertIsNone(self.step(controller, changed, now=10.1).speed_cap)
        self.assertIsNone(self.step(controller, changed, now=10.2).speed_cap)
        self.assertIsNotNone(self.step(controller, replace(changed, distance=85., distance_observation=3), now=10.3).speed_cap)

  def test_override_and_disengagement_require_fresh_post_resume_observation(self):
    for blocked in ({'driver_override': True}, {'longitudinal_active': False}):
      with self.subTest(blocked=blocked):
        controller = NavigationSpeedController()
        self.step(controller, self.sample())
        self.step(controller, self.sample(90., 2), now=10.1, **blocked)
        self.step(controller, self.sample(80., 3), now=10.2, **blocked)
        self.assertIsNone(self.step(controller, self.sample(80., 3), now=10.3).speed_cap)
        self.assertIsNotNone(self.step(controller, self.sample(75., 4), now=10.4).speed_cap)

  def test_late_guidance_and_standstill_never_generate_stop_target(self):
    for distance in (0., 3., 8.):
      for speed in (0., 80 / 3.6):
        result = self.step(NavigationSpeedController(), self.sample(distance), v_ego=speed)
        self.assertAlmostEqual(result.speed_cap, TURN_SPEED, places=6)

  def test_near_turn_timeout_is_bounded_and_zero_is_not_completion(self):
    controller = NavigationSpeedController()
    self.assertIsNotNone(self.step(controller, self.sample(0.)).speed_cap)
    self.assertIsNotNone(self.step(controller, self.sample(0., 2), now=19.9).speed_cap)
    self.assertIsNone(self.step(controller, self.sample(0., 3), now=20.001).speed_cap)
    self.assertIsNone(self.step(controller, self.sample(0., 4), now=21.).speed_cap)

  def test_approach_timeout_survives_temporary_inhibition(self):
    controller = NavigationSpeedController()
    self.step(controller, self.sample())
    self.step(controller, self.sample(90., 2), now=50., driver_override=True)
    self.assertIsNone(self.step(controller, self.sample(80., 3), now=130.001).speed_cap)

  def test_near_deadline_survives_transport_reset_and_staleness(self):
    controller = NavigationSpeedController()
    self.step(controller, self.sample(5.))
    changed = self.sample(5., 2, transport=(b'r' * 16, 2))
    self.assertIsNone(self.step(controller, changed, now=14.).speed_cap)
    self.assertIsNotNone(self.step(controller, replace(changed, distance_observation=3), now=14.1).speed_cap)
    self.assertIsNone(self.step(controller, replace(changed, distance_observation=3), now=16.).speed_cap)
    self.assertIsNone(self.step(controller, replace(changed, distance_observation=4), now=20.001).speed_cap)

  def test_retired_identity_budget_fails_closed(self):
    controller = NavigationSpeedController()
    for maneuver_id in range(512):
      sample = self.sample(0., identity=(b'p' * 16, 1, 2, 3, maneuver_id))
      self.assertIsNotNone(self.step(controller, sample, now=10. + 11 * maneuver_id).speed_cap)
    result = self.step(controller, self.sample(100., identity=(b'p' * 16, 1, 2, 3, 512)), now=10. + 11 * 512)
    self.assertIsNone(result.speed_cap)
    self.assertEqual(result.reason, 'maneuver_budget_exhausted')

  def test_back_to_back_maneuver_has_its_own_envelope(self):
    controller = NavigationSpeedController()
    first = self.sample(0.)
    self.step(controller, first)
    second = self.sample(30., 2, identity=(*first.identity[:-1], 43), maneuver_type=2)
    result = self.step(controller, second, now=10.1)
    self.assertAlmostEqual(result.raw_cap, math.sqrt(TURN_SPEED ** 2 + .9 * 22.), places=6)
    self.assertGreater(result.speed_cap, TURN_SPEED)


if __name__ == '__main__':
  unittest.main()
