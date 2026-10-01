"""Portable navigation/cruise-policy replay and ideal kinematic plant.

This deliberately does not simulate the lead MPC, actuator delay, tyres or lateral
control. It loads the actual cruise policy from source, so a desktop without the
device's native binaries can still measure envelope timing and cruise slew.
Run: python -m openpilot.sunnypilot.external_navigation.tests.speed_replay
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from openpilot.sunnypilot.external_navigation.speed_control import NavigationSpeedController, NavigationSpeedSample

ROOT = Path(__file__).resolve().parents[3]
DT = .05
TURN_SPEED = 14 * .44704
APPROACH_DECEL = .45
DISTANCE_BUFFER = 8.


def load_cruise_policy():
  """Compile only the unchanged pure functions, without native planner imports."""
  source = ROOT / 'selfdrive/controls/lib/longitudinal_planner.py'
  constants = {'A_CRUISE_MAX_VALS', 'A_CRUISE_MAX_BP', 'J_CRUISE_VALS', 'A_CRUISE_MIN',
               'MIN_ALLOW_THROTTLE_SPEED', '_A_TOTAL_MAX_V', '_A_TOTAL_MAX_BP'}
  functions = {'get_max_accel', 'get_cruise_accel'}
  statements = [node for node in ast.parse(source.read_text()).body
                if (isinstance(node, ast.FunctionDef) and node.name in functions)
                or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants
                                                         for target in node.targets))]
  limits_source = ROOT.parent / 'opendbc_repo/opendbc/car/interfaces.py'
  limits = {target.id: ast.literal_eval(node.value)
            for node in ast.parse(limits_source.read_text()).body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name) and target.id in ('ACCEL_MIN', 'ACCEL_MAX')}
  namespace = {'np': np, 'math': math, 'CV': SimpleNamespace(DEG_TO_RAD=math.pi / 180.), **limits}
  exec(compile(ast.Module(body=statements, type_ignores=[]), str(source), 'exec'), namespace)
  if not (constants | functions).issubset(namespace):
    raise RuntimeError('cruise policy source shape changed; update the replay extractor')
  return namespace


@dataclass(frozen=True)
class ReplayRow:
  time: float
  distance: float
  speed: float
  acceleration: float
  cap: float | None
  raw_cap: float | None
  reason: str


def sample(distance, observation, age_ms=0, *, maneuver=1):
  return NavigationSpeedSample(
    identity=(b'p' * 16, 1, 1, 1, maneuver), transport=(b'r' * 16, 1),
    route_state=1, maneuver_type=1, distance=distance,
    distance_observation=observation, maneuver_observation=observation,
    distance_age_ms=age_ms, maneuver_age_ms=age_ms, delivery_age_ms=0,
  )


def replay(samples, *, speed=50 / 3.6, baseline=50 / 3.6):
  """Replay sanitized (seconds, sample-or-None) inputs with deterministic clocks."""
  controller = NavigationSpeedController()
  return [controller.update(nav, now_ns=int((time + 1) * 1e9), enabled=True,
                            longitudinal_active=True, driver_override=False,
                            v_ego=speed, baseline_target=baseline)
          for time, nav in samples]


def simulate_approach(initial_kph, *, period=.25, late_distance=None, experimental=False):
  """An ideal point mass tracking the actual cruise acceleration policy at 20 Hz."""
  cruise = speed = initial_kph / 3.6
  activation_distance = DISTANCE_BUFFER + (cruise ** 2 - TURN_SPEED ** 2) / (2 * APPROACH_DECEL)
  distance = late_distance if late_distance is not None else activation_distance + 40
  controller = NavigationSpeedController()
  policy = load_cruise_policy()
  cp = SimpleNamespace(steerRatio=15., wheelbase=2.7)
  acceleration = 0.
  observed_distance, observed_at, observation = distance, 0., 0
  rows = []
  for frame in range(int(120 / DT)):
    now = frame * DT
    if observation == 0 or now >= observed_at + period - 1e-9:
      observed_distance = round(max(distance, 0))
      observed_at, observation = now, observation + 1
    nav = sample(observed_distance, observation, round((now - observed_at) * 1000))
    decision = controller.update(nav, now_ns=int((now + 1) * 1e9), enabled=True,
                                 longitudinal_active=True, driver_override=False,
                                 v_ego=speed, baseline_target=cruise)
    target = cruise if decision.speed_cap is None else min(cruise, decision.speed_cap)
    acceleration = policy['get_cruise_accel'](experimental, target, speed, acceleration,
                                              0., cp, DT, policy['ACCEL_MAX'], True)
    rows.append(ReplayRow(now, distance, speed, acceleration, decision.speed_cap, decision.raw_cap, decision.reason))
    if distance <= 0:
      break
    next_speed = max(0., speed + acceleration * DT)
    distance -= (speed + next_speed) * DT / 2
    speed = next_speed
  return rows


def summary(initial_kph, rows):
  selected = next((row for row in rows if row.cap is not None), None)
  return {
    'initial_kph': initial_kph,
    'cap_onset_metres': round(selected.distance, 2) if selected else None,
    'speed_at_turn_kph': round(rows[-1].speed * 3.6, 2),
    'nominal_turn_target_kph': round(TURN_SPEED * 3.6, 2),
    'minimum_accel_mps2': round(min(row.acceleration for row in rows), 3),
    'elapsed_seconds': rows[-1].time,
    'model': 'ideal point mass and existing cruise policy; no native MPC or vehicle dynamics',
  }


if __name__ == '__main__':
  print(json.dumps([summary(speed, simulate_approach(speed)) for speed in (30, 50, 80)], indent=2))
