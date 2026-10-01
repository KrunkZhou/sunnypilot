from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpilot.sunnypilot.external_navigation.speed_adapter import ExternalNavigationSpeed


class Messages(dict):
  def __init__(self, **messages):
    super().__init__(messages)
    self.valid = dict.fromkeys(messages, True)
    self.alive = dict.fromkeys(messages, True)
    self.seen = dict.fromkeys(messages, True)


class NavigationSpeedAdapterTests(unittest.TestCase):
  def setUp(self):
    self.context = ExitStack()
    self.addCleanup(self.context.close)
    self.read_mode = self.context.enter_context(patch('openpilot.sunnypilot.external_navigation.speed_adapter.read_mode', return_value=1))
    self.read_enabled = self.context.enter_context(patch('openpilot.sunnypilot.external_navigation.speed_adapter.read_turn_speed_control', return_value=True))

  def setup_adapter(self, capable=True):
    nav = SimpleNamespace(receiveMonoTime=10_000_000_000, receiverSession=b'r' * 16, publisherSession=b'p' * 16,
                          cacheEpoch=1, streamId=2, generation=3, maneuverId=42, token=1,
                          hasManeuverId=True, routeState=1, hasManeuver=True, maneuverType=1,
                          hasNextDistance=True, nextDistance=100, distanceObservation=1, maneuverObservation=1,
                          distanceAgeMs=0, maneuverAgeMs=0, sourceFresh=True, sourceAgeMs=0,
                          transportRttMs=25, snapshotSequence=10,
                          connected=True, available=True)
    cs = SimpleNamespace(vEgo=80 / 3.6, vCruise=80., gasPressed=False, brakePressed=False,
                         regenBraking=False, steeringPressed=False, leftBlinker=False, rightBlinker=False)
    cc = SimpleNamespace(enabled=True, longActive=True, latActive=False, cruiseControl=SimpleNamespace(override=False))
    controls = SimpleNamespace(longControlState='pid')
    sd = SimpleNamespace(enabled=True, experimentalMode=False)
    sm = Messages(externalNavigationSP=nav, carState=cs, carControl=cc, controlsState=controls, selfdriveState=sd)
    adapter = ExternalNavigationSpeed(SimpleNamespace(openpilotLongitudinalControl=capable), clock=lambda: 10_000_000_000)
    return adapter, sm

  def step(self, adapter, sm, now_ns=None, baseline_target=80 / 3.6):
    return adapter.update(sm, baseline_target=baseline_target, now_ns=now_ns)

  def test_capability_and_high_speed_independent_of_lateral_activity(self):
    adapter, sm = self.setup_adapter()
    self.assertFalse(sm['carControl'].latActive)
    self.assertIsNotNone(self.step(adapter, sm).speed_cap)
    adapter, sm = self.setup_adapter(capable=False)
    result = self.step(adapter, sm)
    self.assertIsNone(result.speed_cap)
    self.assertEqual(result.reason, 'longitudinal_unsupported')

  def test_assisted_turns_prerequisite_and_default_disabled_path(self):
    for mode, enabled, reason in ((0, True, 'assisted_turns_required'), (1, False, 'off'), (0, False, 'off')):
      with self.subTest(mode=mode, enabled=enabled):
        self.read_mode.return_value, self.read_enabled.return_value = mode, enabled
        adapter, sm = self.setup_adapter()
        result = self.step(adapter, sm)
        self.assertIsNone(result.speed_cap)
        self.assertEqual(result.reason, reason)

  def test_settings_refresh_removes_cap(self):
    adapter, sm = self.setup_adapter()
    self.assertIsNotNone(self.step(adapter, sm).speed_cap)
    self.read_enabled.return_value = False
    result = self.step(adapter, sm, now_ns=11_000_000_000)
    self.assertIsNone(result.speed_cap)
    self.assertEqual(result.reason, 'off')

  def test_longitudinal_engagement_gates(self):
    for name, field, value in (('carControl', 'enabled', False), ('carControl', 'longActive', False),
                               ('selfdriveState', 'enabled', False), ('controlsState', 'longControlState', 'off'),
                               ('controlsState', 'longControlState', SimpleNamespace(raw=0)),
                               ('carState', 'vCruise', 255.), ('carState', 'vCruise', float('nan'))):
      with self.subTest(name=name, field=field, value=value):
        adapter, sm = self.setup_adapter()
        setattr(sm[name], field, value)
        self.assertIsNone(self.step(adapter, sm).speed_cap)

  def test_acc_and_blended_modes_both_accept_cap(self):
    for experimental in (False, True):
      for state in ('pid', 'stopping', 'starting', SimpleNamespace(raw=1)):
        with self.subTest(experimental=experimental, state=state):
          adapter, sm = self.setup_adapter()
          sm['selfdriveState'].experimentalMode = experimental
          sm['controlsState'].longControlState = state
          self.assertIsNotNone(self.step(adapter, sm).speed_cap)

  def test_control_health_gates_but_navigation_health_is_optional(self):
    for name in ('carState', 'carControl', 'controlsState', 'selfdriveState'):
      for check in ('valid', 'alive'):
        with self.subTest(name=name, check=check):
          adapter, sm = self.setup_adapter()
          getattr(sm, check)[name] = False
          self.assertIsNone(self.step(adapter, sm).speed_cap)
    for check in ('valid', 'seen'):
      adapter, sm = self.setup_adapter()
      getattr(sm, check)['externalNavigationSP'] = False
      self.assertIsNone(self.step(adapter, sm).speed_cap)

  def test_driver_gas_brake_regen_and_cruise_override(self):
    for field in ('gasPressed', 'brakePressed', 'regenBraking', 'override'):
      with self.subTest(field=field):
        adapter, sm = self.setup_adapter()
        target = sm['carControl'].cruiseControl if field == 'override' else sm['carState']
        setattr(target, field, True)
        self.assertIsNone(self.step(adapter, sm).speed_cap)

  def test_lateral_manual_inputs_do_not_become_longitudinal_gates(self):
    for field in ('steeringPressed', 'leftBlinker', 'rightBlinker'):
      with self.subTest(field=field):
        adapter, sm = self.setup_adapter()
        setattr(sm['carState'], field, True)
        self.assertIsNotNone(self.step(adapter, sm).speed_cap)

  def test_delivery_age_included_once_in_observation_age(self):
    adapter, sm = self.setup_adapter()
    sm['externalNavigationSP'].distanceAgeMs = 800
    sm['externalNavigationSP'].maneuverAgeMs = 20_000
    result = self.step(adapter, sm, now_ns=10_200_000_000)
    self.assertEqual(result.sample.distance_age_ms, 1000)
    self.assertEqual(result.sample.delivery_age_ms, 200)
    self.assertEqual(result.sample.maneuver_age_ms, 20_200)
    self.assertIsNotNone(result.speed_cap)
    result = self.step(adapter, sm, now_ns=10_201_000_000)
    self.assertEqual(result.sample.distance_age_ms, 1001)
    self.assertIsNone(result.speed_cap)

  def test_delivery_age_rounds_up_and_future_timestamp_rejects(self):
    for timestamp in (9_999_999_999, 11_000_000_001):
      with self.subTest(timestamp=timestamp):
        adapter, sm = self.setup_adapter()
        result = self.step(adapter, sm, now_ns=timestamp)
        self.assertIsNone(result.speed_cap)

  def test_source_fresh_boolean_does_not_replace_effective_age(self):
    adapter, sm = self.setup_adapter()
    nav = sm['externalNavigationSP']
    nav.sourceFresh = False
    nav.sourceAgeMs = 100_000
    self.assertIsNotNone(self.step(adapter, sm).speed_cap)
    nav.sourceFresh = True
    nav.distanceAgeMs = 1001
    self.assertIsNone(self.step(adapter, sm).speed_cap)

  def test_presence_distinguishes_missing_distance_from_zero(self):
    adapter, sm = self.setup_adapter()
    nav = sm['externalNavigationSP']
    nav.nextDistance, nav.hasNextDistance = 0, False
    self.assertIsNone(self.step(adapter, sm).speed_cap)
    nav.hasNextDistance = True
    self.assertAlmostEqual(self.step(adapter, sm).speed_cap, 14 * .44704)
    for field in ('hasManeuver', 'hasManeuverId'):
      adapter, sm = self.setup_adapter()
      setattr(sm['externalNavigationSP'], field, False)
      self.assertIsNone(self.step(adapter, sm).speed_cap)

  def test_new_receiver_receipt_cannot_refresh_repeated_observation(self):
    adapter, sm = self.setup_adapter()
    self.assertIsNotNone(self.step(adapter, sm).speed_cap)
    sm['externalNavigationSP'].receiveMonoTime = 11_001_000_000
    self.assertIsNone(self.step(adapter, sm, now_ns=11_001_000_000).speed_cap)

  def test_diagnostics_distinguish_available_selected_and_planner_source(self):
    adapter, sm = self.setup_adapter()
    self.step(adapter, sm, baseline_target=5.)
    message = SimpleNamespace()
    adapter.fill(message, baseline_source=3, baseline_target=5.)
    self.assertTrue(message.capAvailable)
    self.assertFalse(message.speedSelected)
    self.assertFalse(message.cruiseCandidateSelected)
    self.assertEqual(message.baselineTarget, 5.)
    self.step(adapter, sm)
    adapter.fill(message, baseline_source=0, baseline_target=80 / 3.6, cruise_selected=False)
    self.assertTrue(message.speedSelected)
    self.assertFalse(message.cruiseCandidateSelected)
    adapter.fill(message, baseline_source=0, baseline_target=80 / 3.6, cruise_selected=True)
    self.assertTrue(message.cruiseCandidateSelected)
    self.assertEqual(message.maneuverId, 42)
    self.assertEqual(message.distanceObservation, 1)
    self.assertEqual(message.inputMonoTime, 10_000_000_000)
    self.assertFalse(any(field in vars(message) for field in ('instruction', 'road', 'destination', 'key')))


if __name__ == '__main__':
  unittest.main()
