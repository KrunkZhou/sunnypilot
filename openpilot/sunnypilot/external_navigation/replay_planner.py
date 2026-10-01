"""Development-PC-only recorded longitudinal planner replay; never registered with manager."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path

from openpilot.sunnypilot.external_navigation.speed_adapter import ExternalNavigationSpeed

REPLAY_ENABLED_ENV = 'EXTERNAL_NAV_SPEED_REPLAY_ENABLED'


class RecordedNavigationSpeed(ExternalNavigationSpeed):
  """Use the recording's boot clock while leaving receipt timestamps intact."""
  def __init__(self, *args, replay_enabled=True, **kwargs):
    super().__init__(*args, **kwargs)
    self.replay_enabled = replay_enabled

  def update(self, sm, *, baseline_target: float, now_ns: int | None = None):
    recorded_ns = sm.logMonoTime['modelV2']
    if type(recorded_ns) is not int or recorded_ns < 0:
      raise ValueError('Planner replay requires a nonnegative model monotonic timestamp')
    # Research settings are local to this object; never read/write the vehicle's configuration.
    self.enabled, self.mode = self.replay_enabled, 1
    self.next_settings_ns = recorded_ns + 1_000_000_000
    return super().update(sm, baseline_target=baseline_target, now_ns=recorded_ns)


def configure_replay(config, services, *, disabled=False):
  """Copy the ordinary planner config, retaining its initialization and scheduling."""
  if 'modelV2' not in services or (not disabled and 'externalNavigationSP' not in services):
    raise ValueError('Planner replay requires modelV2 and, when enabled, externalNavigationSP messages')
  config = copy.deepcopy(config)
  optional = ('externalNavigationSP', 'carStateSP', 'selfdriveStateSP', 'liveMapDataSP', 'gpsLocation', 'gpsLocationExternal')
  config.pubs += [service for service in optional if service in services and service not in config.pubs]
  if 'longitudinalPlanSP' not in config.subs:
    config.subs.append('longitudinalPlanSP')
  return config


def require_pc():
  from openpilot.common.hardware import PC
  if not PC:
    raise RuntimeError('Recorded planner replay runs on a development PC, never an installed vehicle')


def main():
  if os.environ.get('REPLAY') != '1' or os.environ.get('SIMULATION') != '1':
    raise RuntimeError('Recorded planner launcher requires the isolated process replay harness')
  require_pc()
  enabled_setting = os.environ.get(REPLAY_ENABLED_ENV, '1')
  if enabled_setting not in ('0', '1'):
    raise ValueError('Invalid task-local planner replay setting')
  from openpilot.selfdrive.controls import plannerd
  original_planner = plannerd.LongitudinalPlanner

  class RecordedPlanner(original_planner):
    def __init__(self, *args, **kwargs):
      super().__init__(*args, **kwargs)
      self.navigation_speed = RecordedNavigationSpeed(self.CP, cruise_unset=self.navigation_speed.cruise_unset,
                                                      replay_enabled=enabled_setting == '1')

  plannerd.LongitudinalPlanner = RecordedPlanner
  try:
    plannerd.main()
  finally:
    plannerd.LongitudinalPlanner = original_planner


NAVIGATION_FIELDS = (
  'enabled', 'eligible', 'state', 'reason', 'releaseReason', 'inputMonoTime', 'routeState', 'hasManeuver', 'maneuverType',
  'cacheEpoch', 'streamId', 'generation', 'maneuverId', 'hasDistance', 'distance', 'hasAcceptedDistance', 'acceptedDistance',
  'distanceObservation', 'maneuverObservation', 'distanceAgeMs', 'maneuverAgeMs', 'deliveryAgeMs', 'nominalTurnSpeed',
  'rawCap', 'appliedCap', 'baselineTarget', 'capAvailable', 'speedSelected', 'cruiseCandidateSelected',
  'receiveMonoTime', 'sourceAgeMs', 'transportRttMs', 'snapshotSequence',
)


def output_record(message):
  """Explicit whitelist: no destination/road text, transport token or session bytes."""
  service = message.which()
  if service not in ('longitudinalPlan', 'longitudinalPlanSP'):
    return None
  plan = getattr(message, service)
  record = {'service': service, 'logMonoTime': int(message.logMonoTime), 'valid': bool(message.valid),
            'aTarget': float(plan.aTarget), 'source': str(plan.longitudinalPlanSource)}
  if service == 'longitudinalPlan':
    record.update(modelMonoTime=int(plan.modelMonoTime), shouldStop=bool(plan.shouldStop), fcw=bool(plan.fcw),
                  allowBrake=bool(plan.allowBrake), allowThrottle=bool(plan.allowThrottle))
  else:
    record['vTarget'] = float(plan.vTarget)
    diagnostic = plan.navigationSpeedControl
    record['navigation'] = {field: getattr(diagnostic, field) for field in NAVIGATION_FIELDS}
    record['navigation']['baselineSource'] = str(diagnostic.baselineSource)
  # Preserve invalid diagnostic measurements as explicit nulls, never nonstandard JSON NaN/Infinity.
  for values in (record, record.get('navigation', {})):
    for field, value in values.items():
      if isinstance(value, float) and not math.isfinite(value):
        values[field] = None
  return record


def summarize(records, *, enabled=True):
  plans = [record for record in records if record['service'] == 'longitudinalPlan']
  diagnostics = [record['navigation'] for record in records if record['service'] == 'longitudinalPlanSP']
  reasons = {}
  for diagnostic in diagnostics:
    reasons[diagnostic['reason']] = reasons.get(diagnostic['reason'], 0) + 1
  acceleration = [record['aTarget'] for record in plans if record['aTarget'] is not None]
  caps = [diagnostic['appliedCap'] for diagnostic in diagnostics
          if diagnostic['speedSelected'] and diagnostic['appliedCap'] is not None]
  first_selection = next((d for d in diagnostics if d['speedSelected']), None)
  longest_gap, gap_start = 0., None
  release_reasons = {}
  previous_release = ''
  for diagnostic in diagnostics:
    timestamp = diagnostic['inputMonoTime'] / 1e9
    if not diagnostic['eligible'] and gap_start is None:
      gap_start = timestamp
    if gap_start is not None:
      longest_gap = max(longest_gap, timestamp - gap_start)
    if diagnostic['eligible']:
      gap_start = None
    reason = diagnostic['releaseReason']
    if reason and reason != previous_release:
      release_reasons[reason] = release_reasons.get(reason, 0) + 1
    previous_release = reason

  def changes(values):
    return sum(before != after for before, after in zip(values, values[1:], strict=False))

  speed_plans = [record for record in records if record['service'] == 'longitudinalPlanSP']
  return {
    'navigation_speed_control_enabled': enabled,
    'longitudinal_plan_frames': len(plans), 'navigation_diagnostic_frames': len(diagnostics),
    'eligible_frame_fraction': sum(bool(d['eligible']) for d in diagnostics) / len(diagnostics) if diagnostics else None,
    'longest_observed_ineligible_interval_seconds': longest_gap,
    'first_selection_distance_metres': (first_selection['acceptedDistance'] if first_selection['hasAcceptedDistance']
                                        else first_selection['distance'] if first_selection['hasDistance'] else None) if first_selection else None,
    'navigation_speed_selected_frames': sum(bool(d['speedSelected']) for d in diagnostics),
    'navigation_cruise_selected_frames': sum(bool(d['cruiseCandidateSelected']) for d in diagnostics),
    'speed_target_changes': changes([record['vTarget'] for record in speed_plans]),
    'speed_source_changes': changes([record['source'] for record in speed_plans]),
    'acceleration_source_changes': changes([record['source'] for record in plans]),
    'release_reason_changes': release_reasons,
    'minimum_applied_cap_mps': min(caps, default=None),
    'minimum_acceleration_mps2': min(acceleration, default=None),
    'maximum_acceleration_mps2': max(acceleration, default=None), 'reasons': reasons,
    'validation': 'Recorded planner outputs only; no vehicle dynamics or driving-safety validation',
    'activation_approved': False,
  }


def run_planner_replay(args):
  require_pc()
  from openpilot.selfdrive.test.process_replay.process_replay import get_process_config, replay_process
  from openpilot.system.manager.process import PythonProcess
  from openpilot.system.manager.process_config import managed_processes
  from openpilot.tools.lib.logreader import LogReader

  logs = list(LogReader(str(args.log)))
  model_times = [int(message.logMonoTime) for message in logs if message.which() == 'modelV2']
  if any(after < before for before, after in zip(model_times, model_times[1:], strict=False)):
    raise ValueError('Planner replay must contain one ordered boot clock; split logs spanning restarts')
  enabled = not getattr(args, 'disabled', False)
  config = configure_replay(get_process_config('plannerd'), {message.which() for message in logs}, disabled=not enabled)
  original_process = managed_processes['plannerd']
  original_setting = os.environ.get(REPLAY_ENABLED_ENV)
  os.environ[REPLAY_ENABLED_ENV] = '1' if enabled else '0'
  try:
    managed_processes['plannerd'] = PythonProcess('plannerd', 'openpilot.sunnypilot.external_navigation.replay_planner', lambda *a: True)
    outputs = replay_process(config, logs)
  finally:
    managed_processes['plannerd'] = original_process
    if original_setting is None:
      os.environ.pop(REPLAY_ENABLED_ENV, None)
    else:
      os.environ[REPLAY_ENABLED_ENV] = original_setting
  records = [record for message in outputs if (record := output_record(message)) is not None]
  report = summarize(records, enabled=enabled)
  if not report['longitudinal_plan_frames'] or not report['navigation_diagnostic_frames']:
    raise RuntimeError('Planner replay did not produce both longitudinalPlan and longitudinalPlanSP outputs')
  output = Path(args.output)
  output.mkdir(parents=True, exist_ok=True)
  (output / 'longitudinal-plans.jsonl').write_text(''.join(json.dumps(record, allow_nan=False) + '\n' for record in records))
  (output / 'summary.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
  return report


if __name__ == '__main__':
  main()
