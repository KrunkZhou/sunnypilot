"""Deterministic turn proposals. No direct steering, speed or lane-change outputs."""
from __future__ import annotations

from dataclasses import dataclass
import math

PROFILE = 'external-turn-v1-40m-15m-1000ms'
LEFT, RIGHT = 1, 2  # cereal.Desire turnLeft / turnRight (never laneChange/keepRight)
TURN_TYPES = {1: LEFT, 2: RIGHT, 20: LEFT, 21: RIGHT}
RIGHT_BRANCH_TYPES = (14, 23, 53)
MAX_SPEED = 20 * 0.44704


@dataclass(frozen=True)
class Sample:
  identity: tuple
  route_state: int
  maneuver_type: int
  distance: int | None
  observation: int
  distance_age_ms: int
  connected: bool = True
  available: bool = True
  supports_hints: bool = True


@dataclass(frozen=True)
class Decision:
  proposal: int = 0
  desire: int = 0
  reason: str = 'off'
  assisted: bool = False


class TurnPolicy:
  def __init__(self):
    self.identity = None
    self.observation = None
    self.distance = None
    self.armed = False
    self.consumed: set[tuple] = set()
    self.status = Decision()

  def _reset(self) -> None:
    self.identity = None
    self.observation = self.distance = None
    self.armed = False

  def update(self, sample: Sample | None, *, mode: int, verified: bool, lateral_active: bool,
             speed: float, speed_limit: float, manual: bool = False) -> Decision:
    reason = None
    if mode != 1:
      reason = 'off'
    elif sample is None or not sample.connected:
      reason = 'relay_stale'
    elif not sample.available or sample.route_state != 1:
      reason = 'route_inactive'
    elif sample.maneuver_type in RIGHT_BRANCH_TYPES:
      reason = 'branch_right_advisory_only'
    elif sample.maneuver_type not in TURN_TYPES:
      reason = 'unsupported_maneuver'
    elif not sample.supports_hints or sample.distance is None or sample.distance_age_ms > 1000:
      reason = 'distance_stale_or_missing'
    elif not lateral_active:
      reason = 'lateral_inactive'
    elif manual:
      reason = 'driver_or_maneuver_priority'
    elif not all(math.isfinite(x) for x in (speed, speed_limit)) or not 0 <= speed < min(speed_limit, MAX_SPEED):
      reason = 'speed_above_turn_threshold'
    if reason is not None:
      self._reset()
      self.status = Decision(reason=reason)
      return self.status

    assert sample is not None
    assisted = mode == 1 and verified
    prefix = 'assisted' if assisted else 'guidance_only_unvalidated_model'
    if sample.identity in self.consumed:
      self.status = Decision(reason=f'{prefix}_already_consumed', assisted=assisted)
      return self.status
    if len(self.consumed) >= 512:
      self.status = Decision(reason='maneuver_budget_exhausted')
      return self.status
    if self.identity != sample.identity:
      self._reset()
      self.identity = sample.identity
    if self.observation is not None and sample.observation <= self.observation:
      self.status = Decision(reason=f'{prefix}_waiting_for_observation', assisted=assisted)
      return self.status
    previous = self.distance
    self.observation, self.distance = sample.observation, sample.distance
    if sample.distance > 40:
      self.armed = False
    elif sample.distance > 15:
      # The crossing itself can be the second distinct decreasing observation.
      self.armed = previous is None or sample.distance < previous
    elif self.armed and previous is not None and previous > 15 and sample.distance < previous:
      self.consumed.add(sample.identity)
      self.armed = False
      proposal = TURN_TYPES[sample.maneuver_type]
      self.status = Decision(proposal, proposal if assisted else 0, f'{prefix}_turn_proposed', assisted)
      return self.status
    else:
      self.armed = False
    self.status = Decision(reason=f'{prefix}_armed' if self.armed else f'{prefix}_waiting_for_approach', assisted=assisted)
    return self.status
