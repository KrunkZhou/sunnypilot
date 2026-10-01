import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpilot.sunnypilot.external_navigation import replay_planner
from openpilot.sunnypilot.external_navigation.tests import test_speed_adapter as adapter_fixtures


class RecordedPlannerReplayTests(unittest.TestCase):
  def test_recorded_clock_preserves_delivery_age_without_reading_settings(self):
    _, sm = adapter_fixtures.NavigationSpeedAdapterTests().setup_adapter()
    sm.logMonoTime = {'modelV2': 10_200_000_000}
    sm['externalNavigationSP'].distanceAgeMs = 800
    adapter = replay_planner.RecordedNavigationSpeed(SimpleNamespace(openpilotLongitudinalControl=True),
                                                     clock=lambda: self.fail('live clock used'))
    with patch('openpilot.sunnypilot.external_navigation.speed_adapter.read_mode', side_effect=AssertionError('settings read')), \
         patch('openpilot.sunnypilot.external_navigation.speed_adapter.read_turn_speed_control', side_effect=AssertionError('settings read')):
      result = adapter.update(sm, baseline_target=80 / 3.6)
      self.assertEqual(result.input_mono_time, 10_200_000_000)
      self.assertEqual(result.sample.delivery_age_ms, 200)
      self.assertEqual(result.sample.distance_age_ms, 1000)
      self.assertIsNotNone(result.speed_cap)
      sm.logMonoTime['modelV2'] += 1_000_000
      self.assertIsNone(adapter.update(sm, baseline_target=80 / 3.6).speed_cap)

  def test_configuration_is_copied_and_adds_recorded_optional_messages(self):
    config = SimpleNamespace(pubs=['modelV2', 'carState'], subs=['longitudinalPlan'], init_callback='original', main_pub='modelV2')
    services = {'modelV2', 'externalNavigationSP', 'selfdriveStateSP', 'gpsLocationExternal'}
    modified = replay_planner.configure_replay(config, services)
    self.assertEqual(config.pubs, ['modelV2', 'carState'])
    self.assertEqual(config.subs, ['longitudinalPlan'])
    self.assertEqual(modified.pubs, ['modelV2', 'carState', 'externalNavigationSP', 'selfdriveStateSP', 'gpsLocationExternal'])
    self.assertEqual(modified.subs, ['longitudinalPlan', 'longitudinalPlanSP'])
    self.assertEqual((modified.init_callback, modified.main_pub), ('original', 'modelV2'))
    with self.assertRaises(ValueError):
      replay_planner.configure_replay(config, {'modelV2'})
    disabled = replay_planner.configure_replay(config, {'modelV2'}, disabled=True)
    self.assertNotIn('externalNavigationSP', disabled.pubs)
    self.assertIn('longitudinalPlanSP', disabled.subs)

  def test_disabled_replay_needs_neither_navigation_nor_settings_files(self):
    _, sm = adapter_fixtures.NavigationSpeedAdapterTests().setup_adapter()
    sm.logMonoTime = {'modelV2': 10_200_000_000}
    del sm['externalNavigationSP']
    sm.seen['externalNavigationSP'] = False
    adapter = replay_planner.RecordedNavigationSpeed(SimpleNamespace(openpilotLongitudinalControl=True), replay_enabled=False)
    with patch('openpilot.sunnypilot.external_navigation.speed_adapter.read_mode', side_effect=AssertionError('settings read')), \
         patch('openpilot.sunnypilot.external_navigation.speed_adapter.read_turn_speed_control', side_effect=AssertionError('settings read')):
      decision = adapter.update(sm, baseline_target=80 / 3.6)
    self.assertIsNone(decision.speed_cap)
    self.assertEqual(decision.reason, 'off')
    self.assertFalse(adapter.enabled)
    self.assertFalse(replay_planner.summarize([], enabled=False)['navigation_speed_control_enabled'])

  def test_launcher_requires_isolated_replay_environment(self):
    for environment in ({}, {'REPLAY': '1'}, {'SIMULATION': '1'}):
      with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
        with self.assertRaisesRegex(RuntimeError, 'isolated process replay'):
          replay_planner.main()

  def test_launcher_wraps_actual_plannerd_import_and_restores_on_exit(self):
    class Planner:
      def __init__(self):
        self.CP = SimpleNamespace(openpilotLongitudinalControl=True)
        self.navigation_speed = SimpleNamespace(cruise_unset=255)
    plannerd = SimpleNamespace(LongitudinalPlanner=Planner)

    def run():
      subject = plannerd.LongitudinalPlanner()
      self.assertIsInstance(subject, Planner)
      self.assertIsInstance(subject.navigation_speed, replay_planner.RecordedNavigationSpeed)
      self.assertFalse(subject.navigation_speed.replay_enabled)
      raise ValueError('end fake process')

    plannerd.main = run
    with patch.dict(os.environ, {'REPLAY': '1', 'SIMULATION': '1', replay_planner.REPLAY_ENABLED_ENV: '0'}), \
         patch.object(replay_planner, 'require_pc'), \
         patch.dict('sys.modules', {'openpilot.selfdrive.controls.plannerd': plannerd}):
      with self.assertRaisesRegex(ValueError, 'end fake process'):
        replay_planner.main()
    self.assertIs(plannerd.LongitudinalPlanner, Planner)

  def test_runner_restores_managed_process_when_native_replay_fails(self):
    original_process = object()
    processes = {'plannerd': original_process}
    config = SimpleNamespace(pubs=['modelV2'], subs=['longitudinalPlan'])
    logs = [SimpleNamespace(which=lambda: 'modelV2', logMonoTime=10),
            SimpleNamespace(which=lambda: 'externalNavigationSP', logMonoTime=10)]

    def replay(config, recording):
      self.assertEqual('externalNavigationSP' in config.pubs, not disabled)
      self.assertIn('longitudinalPlanSP', config.subs)
      self.assertIsNot(processes['plannerd'], original_process)
      self.assertEqual(os.environ[replay_planner.REPLAY_ENABLED_ENV], '0' if disabled else '1')
      raise RuntimeError('native dependency failure')

    modules = {
      'openpilot.selfdrive.test.process_replay.process_replay': SimpleNamespace(get_process_config=lambda name: config, replay_process=replay),
      'openpilot.system.manager.process': SimpleNamespace(PythonProcess=lambda *args: args),
      'openpilot.system.manager.process_config': SimpleNamespace(managed_processes=processes),
      'openpilot.tools.lib.logreader': SimpleNamespace(LogReader=lambda path: logs[:1] if disabled else logs),
    }
    with patch.object(replay_planner, 'require_pc'), patch.dict('sys.modules', modules), \
         patch.dict(os.environ, {replay_planner.REPLAY_ENABLED_ENV: 'previous'}):
      for disabled in (False, True):
        with self.subTest(disabled=disabled), self.assertRaisesRegex(RuntimeError, 'native dependency failure'):
          replay_planner.run_planner_replay(SimpleNamespace(log='test-input', output='unused-on-failure', disabled=disabled))
        self.assertEqual(os.environ[replay_planner.REPLAY_ENABLED_ENV], 'previous')
    self.assertIs(processes['plannerd'], original_process)

  def test_diagnostics_whitelist_excludes_route_text_credentials_and_session_bytes(self):
    diagnostic = SimpleNamespace(**dict.fromkeys(replay_planner.NAVIGATION_FIELDS, 0))
    diagnostic.reason = 'navigation_selected'
    diagnostic.state = 'limiting'
    diagnostic.baselineSource = 'cruise'
    diagnostic.appliedCap = 8.
    diagnostic.speedSelected = True
    diagnostic.cruiseCandidateSelected = False
    diagnostic.destination = 'private destination'
    diagnostic.publisherSession = b'private session'
    diagnostic.transportToken = 42
    diagnostic.rawCap = float('nan')
    plan = SimpleNamespace(aTarget=-.4, vTarget=8., longitudinalPlanSource='externalNavigation', navigationSpeedControl=diagnostic)
    message = SimpleNamespace(which=lambda: 'longitudinalPlanSP', logMonoTime=100, valid=True, longitudinalPlanSP=plan)
    record = replay_planner.output_record(message)
    self.assertIsNone(record['navigation']['rawCap'])
    for field in ('destination', 'publisherSession', 'transportToken'):
      self.assertNotIn(field, record['navigation'])
    report = replay_planner.summarize([record])
    self.assertEqual(report['navigation_speed_selected_frames'], 1)
    self.assertEqual(report['navigation_cruise_selected_frames'], 0)
    self.assertEqual(report['minimum_applied_cap_mps'], 8.)
    self.assertFalse(report['activation_approved'])

  def test_summary_measures_eligibility_intervals_and_selection_distance(self):
    records = []
    for time_ns, eligible, selected, distance, release in ((0, False, False, 100., ''),
                                                         (2_000_000_000, True, True, 80., ''),
                                                         (3_000_000_000, False, False, 70., 'distance_stale')):
      records.append({'service': 'longitudinalPlanSP', 'vTarget': 8. if selected else 20.,
                      'source': 'externalNavigation' if selected else 'cruise',
                      'navigation': {'inputMonoTime': time_ns, 'eligible': eligible, 'speedSelected': selected,
                                     'hasAcceptedDistance': True, 'acceptedDistance': distance, 'hasDistance': True,
                                     'distance': distance, 'cruiseCandidateSelected': selected, 'appliedCap': 8.,
                                     'reason': 'navigation_selected' if selected else 'distance_stale', 'releaseReason': release}})
    report = replay_planner.summarize(records)
    self.assertAlmostEqual(report['eligible_frame_fraction'], 1 / 3)
    self.assertEqual(report['longest_observed_ineligible_interval_seconds'], 2.)
    self.assertEqual(report['first_selection_distance_metres'], 80.)
    self.assertEqual(report['speed_source_changes'], 2)
    self.assertEqual(report['release_reason_changes'], {'distance_stale': 1})


if __name__ == '__main__':
  unittest.main()
