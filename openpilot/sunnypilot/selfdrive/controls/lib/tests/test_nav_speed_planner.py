"""Native planner integration tests; require the checkout's built control dependencies."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

NATIVE_IMPORT_ERROR = None
try:
  import numpy as np
  from openpilot.cereal import custom, messaging
  from opendbc.car import structs
  from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
  from openpilot.common.test import OpenpilotTestCase
  from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
  from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner, LongitudinalPlanSource, A_CRUISE_MAX_BP, J_CRUISE_VALS
  from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlannerSP
except (ImportError, OSError) as exc:
  NATIVE_IMPORT_ERROR = str(exc)
  OpenpilotTestCase = unittest.TestCase


class NavigationCandidate:
  """Isolate planner arbitration from navigation validity, tested in its own suite."""
  def __init__(self, cap=None):
    self.cap = cap
    self.decision = SimpleNamespace(speed_cap=cap)

  def update(self, sm, *, baseline_target, now_ns=None):
    self.baseline_target = baseline_target
    self.decision = SimpleNamespace(speed_cap=self.cap)
    return self.decision


class TestNavigationSpeedArbitration(unittest.TestCase):
  """Exercise the real arbitration method with isolated upstream candidates.

  This covers integration logic on hosts without native control binaries. The
  native tests below additionally run the lead MPC and complete planner.
  """
  def setUp(self):
    source = Path(__file__).resolve().parents[1] / 'longitudinal_planner.py'
    planner_class = next(node for node in ast.parse(source.read_text()).body
                         if isinstance(node, ast.ClassDef) and node.name == 'LongitudinalPlannerSP')
    update = next(node for node in planner_class.body if isinstance(node, ast.FunctionDef) and node.name == 'update_targets')
    self.sources = SimpleNamespace(cruise=0, sccVision=1, sccMap=2, speedLimitAssist=3, externalNavigation=4)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), update],
                        type_ignores=[])
    namespace = {'LongitudinalPlanSource': self.sources, 'CV': SimpleNamespace(KPH_TO_MS=1 / 3.6), 'V_CRUISE_MAX': 145}
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
    self.update = namespace['update_targets']

  def subject(self, cap):
    return SimpleNamespace(
      events_sp=object(), navigation_speed=NavigationCandidate(cap),
      scc=SimpleNamespace(update=Mock(), vision=SimpleNamespace(output_v_target=12., output_a_target=-.8),
                          map=SimpleNamespace(output_v_target=18., output_a_target=-.2)),
      resolver=SimpleNamespace(update=Mock(), speed_limit_valid=False, speed_limit_last_valid=False,
                               speed_limit=0., speed_limit_final_last=0., distance=0.),
      sla=SimpleNamespace(update=Mock(), output_v_target=16., output_a_target=-.4),
    )

  def call(self, subject):
    sm = {'carState': SimpleNamespace(vCruiseCluster=90.),
          'carControl': SimpleNamespace(enabled=True, cruiseControl=SimpleNamespace(override=False))}
    return self.update(subject, sm, 20., .1, 25.)

  def test_optional_cap_preserves_curve_seed(self):
    subject = self.subject(8.)
    self.assertEqual(self.call(subject), (8., -.8))
    self.assertEqual(subject.source, self.sources.externalNavigation)
    self.assertEqual(subject.navigation_speed.baseline_target, 12.)

  def test_no_cap_preserves_existing_arbitration(self):
    subject = self.subject(None)
    self.assertEqual(self.call(subject), (12., -.8))
    self.assertEqual(subject.source, self.sources.sccVision)

  def test_less_restrictive_cap_cannot_raise_existing_target(self):
    subject = self.subject(20.)
    self.assertEqual(self.call(subject), (12., -.8))
    self.assertEqual(subject.source, self.sources.sccVision)

  def test_each_existing_speed_source_can_remain_selected(self):
    for name, target in (('cruise', 25.), ('sccVision', 12.), ('sccMap', 18.), ('speedLimitAssist', 16.)):
      with self.subTest(source=name):
        subject = self.subject(30.)
        if name != 'sccVision':
          subject.scc.vision.output_v_target = 50.
        if name != 'sccMap':
          subject.scc.map.output_v_target = 50.
        if name != 'speedLimitAssist':
          subject.sla.output_v_target = 50.
        self.assertEqual(self.call(subject)[0], target)
        self.assertEqual(subject.source, getattr(self.sources, name))


class PlannerInputs(dict):
  def __init__(self, services):
    super().__init__(services)
    self.valid = dict.fromkeys(services, True)
    self.alive = dict.fromkeys(services, True)
    self.seen = dict.fromkeys(services, True)
    self.updated = dict.fromkeys(services, True)
    self.logMonoTime = dict.fromkeys(services, 0)
    self.recv_frame = dict.fromkeys(services, 1)

  def all_checks(self, service_list=None):
    return True


def planner_inputs(*, speed=20., cruise=100., experimental=False, e2e_accel=1.):
  services = {}
  for service in ('radarState', 'controlsState', 'vehicleParameters', 'carStateSP', 'liveMapDataSP',
                  'gpsLocation', 'gpsLocationExternal', 'carState', 'selfdriveState', 'carControl', 'modelV2'):
    services[service] = getattr(messaging.new_message(service), service)
  cs = services['carState']
  cs.vEgo, cs.vCruise, cs.vCruiseCluster = speed, cruise, cruise
  services['controlsState'].longControlState = LongCtrlState.pid
  services['selfdriveState'].enabled = True
  services['selfdriveState'].experimentalMode = experimental
  services['carControl'].enabled = True
  services['carControl'].longActive = True
  model = services['modelV2']
  model.orientationRate.z = [.001] * 33
  model.velocity.x = [speed] * 33
  model.position.x = [float(i) for i in range(33)]
  model.action.desiredAcceleration = e2e_accel
  model.meta.disengagePredictions.gasPressProbs = [1.] * 6
  return PlannerInputs(services)


def planner(*, cap=None, dec_active=False, mode='acc'):
  cp = structs.CarParams()
  cp.openpilotLongitudinalControl = True
  cp.steerRatio, cp.wheelbase, cp.longitudinalActuatorDelay = 15., 2.7, .2
  result = LongitudinalPlanner(cp, custom.CarParamsSP.new_message().as_reader(), init_v=20.)
  result.dec = SimpleNamespace(update=lambda sm: None, active=lambda: dec_active, mode=lambda: mode)
  result.navigation_speed = NavigationCandidate(cap)
  return result


@unittest.skipIf(NATIVE_IMPORT_ERROR is not None, f'native planner dependencies unavailable: {NATIVE_IMPORT_ERROR}')
class TestNavigationSpeedPlanner(OpenpilotTestCase):
  def test_disabled_candidate_matches_baseline(self):
    from unittest.mock import patch
    from openpilot.sunnypilot.external_navigation.speed_adapter import ExternalNavigationSpeed
    baseline, disabled = planner(), planner()
    disabled.navigation_speed = ExternalNavigationSpeed(disabled.CP)
    self.enterContext(patch('openpilot.sunnypilot.external_navigation.speed_adapter.read_turn_speed_control', return_value=False))
    self.enterContext(patch('openpilot.sunnypilot.external_navigation.speed_adapter.read_mode', return_value=1))
    for _ in range(20):
      baseline.update(planner_inputs())
      disabled.update(planner_inputs())
      self.assertEqual(disabled.output_v_target, baseline.output_v_target)
      self.assertEqual(disabled.output_a_target, baseline.output_a_target)
      self.assertEqual(disabled.output_should_stop, baseline.output_should_stop)
      self.assertEqual(disabled.fcw, baseline.fcw)
      self.assertEqual(disabled.source, baseline.source)
      np.testing.assert_array_equal(disabled.v_desired_trajectory, baseline.v_desired_trajectory)

  def test_navigation_preserves_existing_curve_acceleration_seed(self):
    subject = planner(cap=8.)
    subject.scc.update = Mock()
    subject.sla.update = Mock()
    subject.scc.vision.output_v_target, subject.scc.vision.output_a_target = 12., -.8
    subject.scc.map.output_v_target = 30.
    subject.sla.output_v_target = 30.
    speed, acceleration = LongitudinalPlannerSP.update_targets(subject, planner_inputs(), 20., .1, 25.)
    self.assertEqual(speed, 8.)
    self.assertEqual(acceleration, -.8)
    self.assertEqual(subject.source, custom.LongitudinalPlanSP.LongitudinalPlanSource.externalNavigation)

  def test_lower_existing_constraint_wins(self):
    subject = planner(cap=8.)
    subject.scc.update = Mock()
    subject.sla.update = Mock()
    subject.scc.vision.output_v_target, subject.scc.vision.output_a_target = 6., -.8
    subject.scc.map.output_v_target = 30.
    subject.sla.output_v_target = 30.
    speed, acceleration = LongitudinalPlannerSP.update_targets(subject, planner_inputs(), 20., .1, 25.)
    self.assertEqual((speed, acceleration), (6., -.8))
    self.assertEqual(subject.source, custom.LongitudinalPlanSP.LongitudinalPlanSource.sccVision)

  def test_navigation_operates_above_lateral_gate_in_acc_and_blended(self):
    for experimental, dec_active, mode in ((False, False, 'acc'), (True, False, 'acc'),
                                            (True, True, 'acc'), (True, True, 'blended')):
      with self.subTest(experimental=experimental, dec_active=dec_active, mode=mode):
        subject = planner(cap=8., dec_active=dec_active, mode=mode)
        for _ in range(10):
          subject.update(planner_inputs(experimental=experimental))
        self.assertEqual(subject.output_v_target, 8.)
        self.assertLess(subject.a_cruise, 0.)
        self.assertEqual(subject.mpc.source, LongitudinalPlanSource.cruise)

  def test_e2e_more_restrictive_acceleration_wins(self):
    subject = planner(cap=8., dec_active=True, mode='blended')
    subject.update(planner_inputs(experimental=True, e2e_accel=-3.))
    self.assertEqual(subject.mpc.source, LongitudinalPlanSource.e2e)
    self.assertEqual(subject.output_a_target, -3.)

  def test_lead_remains_more_restrictive(self):
    baseline, subject = planner(), planner(cap=8.)
    inputs = planner_inputs()
    lead = inputs['radarState'].leadOne
    lead.present, lead.dRel, lead.vLead, lead.modelProb = True, 10., 0., 1.
    baseline.update(inputs)
    subject.update(inputs)
    self.assertLessEqual(subject.output_a_target, baseline.output_a_target + 1e-9)
    self.assertEqual(subject.fcw, baseline.fcw)

  def test_late_cap_and_release_preserve_cruise_slew_and_accel_limits(self):
    subject = planner()
    for cap in (None, 6.25856, None):
      subject.navigation_speed.cap = cap
      for _ in range(20):
        previous = subject.a_cruise
        subject.update(planner_inputs())
        jerk = float(np.interp(20., A_CRUISE_MAX_BP, J_CRUISE_VALS))
        self.assertLessEqual(abs(subject.a_cruise - previous), jerk * subject.dt + 1e-9)
        self.assertGreaterEqual(subject.output_a_target, ACCEL_MIN)
        self.assertLessEqual(subject.output_a_target, ACCEL_MAX)


if __name__ == '__main__':
  unittest.main()
