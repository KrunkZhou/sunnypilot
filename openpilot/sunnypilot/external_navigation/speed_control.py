"""Optional navigation speed constraint. No acceleration, actuation or lateral outputs."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from openpilot.sunnypilot.external_navigation.policy import TURN_TYPES
from openpilot.sunnypilot.external_navigation.protocol import UNKNOWN_AGE

# Development calibration; this feature defaults off and requires vehicle validation.
TURN_SPEED = 14 * .44704
APPROACH_DECEL = .45
DISTANCE_BUFFER = 8.
MAX_DISTANCE = 5000.
MAX_AGE_MS = 1000
ENTER_DELTA = .25
EXIT_DELTA = .10
APPROACH_TIMEOUT_NS = 120_000_000_000
NEAR_TIMEOUT_NS = 10_000_000_000
MAX_RETIRED = 512


def approach_speed(distance: float) -> float:
  """Metres in, m/s out. Validate navigation before calling this envelope."""
  return math.sqrt(TURN_SPEED ** 2 + 2 * APPROACH_DECEL * max(distance - DISTANCE_BUFFER, 0.))


@dataclass(frozen=True)
class NavigationSpeedSample:
  identity: tuple | None
  transport: tuple | None
  route_state: int
  maneuver_type: int | None
  distance: float | None
  distance_observation: int
  maneuver_observation: int
  distance_age_ms: int
  maneuver_age_ms: int
  delivery_age_ms: int
  connected: bool = True
  available: bool = True
  receive_mono_time: int = 0
  source_age_ms: int = UNKNOWN_AGE
  transport_rtt_ms: int = 0
  snapshot_sequence: int = 0


@dataclass(frozen=True)
class NavigationSpeedDecision:
  speed_cap: float | None = None
  raw_cap: float | None = None
  state: str = 'inactive'
  reason: str = 'off'
  release_reason: str = ''
  eligible: bool = False
  accepted_distance: float | None = None
  sample: NavigationSpeedSample | None = None
  input_mono_time: int = 0


@dataclass
class _Approach:
  identity: tuple
  maneuver_type: int
  transport: tuple
  distance_observation: int = 0
  maneuver_observation: int = 0
  observed_distance: float | None = None
  distance_time_ns: int = 0
  maneuver_time_ns: int = 0
  accepted_distance: float | None = None
  recovery_distance: float | None = None
  needs_confirmation: bool = False
  minimum_observation: int = 0
  selected_at_ns: int | None = None
  near_at_ns: int | None = None


def _uint(value, maximum: int, *, nonzero: bool = False) -> bool:
  return type(value) is int and int(nonzero) <= value <= maximum


def _identity_valid(identity, transport) -> bool:
  return (isinstance(identity, tuple) and len(identity) == 5 and isinstance(identity[0], bytes) and len(identity[0]) == 16
          and all(_uint(x, 0xffffffffffffffff) for x in identity[1:])
          and isinstance(transport, tuple) and len(transport) == 2 and isinstance(transport[0], bytes) and len(transport[0]) == 16
          and _uint(transport[1], 0xffffffff, nonzero=True))


def _discontinuous(previous: float, current: float) -> bool:
  # Consistency thresholds accommodate rounded distance, not a kinematic model.
  return current - previous > 10 or previous - current > max(100., .5 * previous)


class NavigationSpeedController:
  def __init__(self):
    self.approach: _Approach | None = None
    self.retired: set[tuple] = set()
    self.selected = False
    self.release_reason = ''
    self.last_now_ns: int | None = None
    self.decision = NavigationSpeedDecision()

  def _retire(self, reason: str) -> None:
    if self.approach is not None:
      self.retired.add(self.approach.identity)
    self.approach = None
    self.selected = False
    self.release_reason = reason

  def _inhibit(self, reason: str, sample, now_ns: int, *, require_new: bool = True, state: str = 'inactive'):
    if self.selected:
      self.release_reason = reason
    self.selected = False
    if (a := self.approach) is not None:
      a.accepted_distance = a.recovery_distance = None
      if require_new:
        a.minimum_observation = max(a.minimum_observation, a.distance_observation)
        if sample is not None and sample.identity == a.identity and _uint(sample.distance_observation, 0xffffffff):
          a.minimum_observation = max(a.minimum_observation, sample.distance_observation)
    self.decision = NavigationSpeedDecision(state=state, reason=reason, release_reason=self.release_reason,
                                            sample=sample, input_mono_time=now_ns)
    return self.decision

  def update(self, sample: NavigationSpeedSample | None, *, now_ns: int, v_ego: float, baseline_target: float,
             enabled: bool = True, longitudinal_active: bool = True, driver_override: bool = False,
             inhibit_reason: str = '') -> NavigationSpeedDecision:
    if not _uint(now_ns, 0xffffffffffffffff) or (self.last_now_ns is not None and now_ns < self.last_now_ns):
      return self._inhibit('clock_invalid', sample, self.last_now_ns or 0)
    self.last_now_ns = now_ns
    if (a := self.approach) is not None:
      if a.near_at_ns is not None and now_ns - a.near_at_ns >= NEAR_TIMEOUT_NS:
        self._retire('expired_near_turn')
      elif a.selected_at_ns is not None and now_ns - a.selected_at_ns >= APPROACH_TIMEOUT_NS:
        self._retire('expired_approach')
    if not enabled:
      return self._inhibit(inhibit_reason or 'off', sample, now_ns)
    if driver_override:
      return self._inhibit('driver_override', sample, now_ns)
    if not longitudinal_active:
      return self._inhibit(inhibit_reason or 'longitudinal_inactive', sample, now_ns)
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0
               for x in (v_ego, baseline_target)):
      return self._inhibit('vehicle_input_invalid', sample, now_ns)
    if sample is None:
      return self._inhibit(inhibit_reason or 'navigation_missing', sample, now_ns)
    if not sample.connected:
      return self._inhibit('relay_stale', sample, now_ns)
    if not sample.available or type(sample.route_state) is not int or sample.route_state != 1:
      self._retire('route_inactive')
      return self._inhibit('route_inactive', sample, now_ns)
    if not _identity_valid(sample.identity, sample.transport):
      return self._inhibit('identity_missing_or_invalid', sample, now_ns)
    if self.approach is not None and self.approach.identity != sample.identity:
      self._retire('maneuver_changed')
    if sample.identity in self.retired:
      return self._inhibit('identity_retired', sample, now_ns, state='retired')
    if len(self.retired) >= MAX_RETIRED:
      return self._inhibit('maneuver_budget_exhausted', sample, now_ns)
    if self.approach is not None and sample.maneuver_type is not None and sample.maneuver_type != self.approach.maneuver_type:
      self._retire('observation_conflict')
      return self._inhibit('observation_conflict', sample, now_ns, state='retired')
    if type(sample.maneuver_type) is not int or sample.maneuver_type not in TURN_TYPES:
      return self._inhibit('unsupported_maneuver', sample, now_ns)
    if sample.distance is None:
      return self._inhibit('distance_missing', sample, now_ns)
    if not isinstance(sample.distance, (int, float)) or isinstance(sample.distance, bool) or not math.isfinite(sample.distance) or sample.distance < 0:
      return self._inhibit('distance_invalid', sample, now_ns)
    if not all(_uint(x, 0xffffffff, nonzero=True) for x in (sample.distance_observation, sample.maneuver_observation)):
      return self._inhibit('observation_missing_or_invalid', sample, now_ns)
    if not all(_uint(x, UNKNOWN_AGE - 1) for x in (sample.distance_age_ms, sample.maneuver_age_ms, sample.delivery_age_ms)):
      return self._inhibit('observation_age_unknown_or_invalid', sample, now_ns)
    if self.approach is None:
      self.approach = _Approach(sample.identity, sample.maneuver_type, sample.transport)
    a = self.approach
    if (sample.maneuver_type != a.maneuver_type or sample.distance_observation < a.distance_observation
        or sample.maneuver_observation < a.maneuver_observation
        or (sample.distance_observation == a.distance_observation and sample.distance != a.observed_distance)):
      self._retire('observation_conflict')
      return self._inhibit('observation_conflict', sample, now_ns, state='retired')
    new_distance = sample.distance_observation > a.distance_observation
    distance_time = now_ns - sample.distance_age_ms * 1_000_000
    maneuver_time = now_ns - sample.maneuver_age_ms * 1_000_000
    a.distance_time_ns = distance_time if new_distance else min(a.distance_time_ns, distance_time)
    a.maneuver_time_ns = maneuver_time if sample.maneuver_observation > a.maneuver_observation else min(a.maneuver_time_ns, maneuver_time)
    a.distance_observation, a.maneuver_observation = sample.distance_observation, sample.maneuver_observation
    a.observed_distance = sample.distance
    sample = replace(sample, distance_age_ms=min(UNKNOWN_AGE, (now_ns - a.distance_time_ns + 999_999) // 1_000_000),
                     maneuver_age_ms=min(UNKNOWN_AGE, (now_ns - a.maneuver_time_ns + 999_999) // 1_000_000))
    if sample.transport != a.transport:
      a.transport = sample.transport
      return self._inhibit('transport_changed', sample, now_ns)
    if sample.delivery_age_ms > MAX_AGE_MS or sample.distance_age_ms > MAX_AGE_MS:
      return self._inhibit('distance_stale', sample, now_ns)
    if sample.distance > MAX_DISTANCE:
      return self._inhibit('outside_approach_range', sample, now_ns)
    if sample.distance_observation <= a.minimum_observation:
      return self._inhibit('waiting_for_fresh_observation', sample, now_ns, require_new=False)
    if a.needs_confirmation:
      if a.recovery_distance is None:
        a.recovery_distance = sample.distance
        return self._decision(sample, now_ns, 'reacquiring', 'waiting_for_confirmation')
      if not new_distance:
        return self._decision(sample, now_ns, 'reacquiring', 'waiting_for_confirmation')
      if _discontinuous(a.recovery_distance, sample.distance):
        a.recovery_distance = sample.distance
        return self._decision(sample, now_ns, 'reacquiring', 'distance_discontinuity')
      a.accepted_distance, a.recovery_distance = min(a.recovery_distance, sample.distance), None
      a.needs_confirmation = False
    elif a.accepted_distance is not None and new_distance:
      if _discontinuous(a.accepted_distance, sample.distance):
        if self.selected:
          self.release_reason = 'distance_discontinuity'
        self.selected = False
        a.accepted_distance, a.recovery_distance = None, sample.distance
        a.needs_confirmation = True
        return self._decision(sample, now_ns, 'reacquiring', 'distance_discontinuity')
      a.accepted_distance = min(a.accepted_distance, sample.distance)
    elif a.accepted_distance is None:
      a.accepted_distance = sample.distance
    if a.accepted_distance <= DISTANCE_BUFFER and a.near_at_ns is None:
      a.near_at_ns = now_ns
    raw_cap = approach_speed(a.accepted_distance)
    delta = baseline_target - raw_cap
    was_selected = self.selected
    self.selected = delta > EXIT_DELTA if self.selected else delta >= ENTER_DELTA
    if was_selected and not self.selected:
      self.release_reason = 'baseline_more_restrictive'
    if self.selected and a.selected_at_ns is None:
      a.selected_at_ns = now_ns
    return self._decision(sample, now_ns, 'limiting' if self.selected else 'tracking',
                          'navigation_selected' if self.selected else 'baseline_more_restrictive', raw_cap)

  def _decision(self, sample, now_ns, state: str, reason: str, raw_cap: float | None = None):
    self.decision = NavigationSpeedDecision(speed_cap=raw_cap if self.selected else None, raw_cap=raw_cap,
                                            state=state, reason=reason, release_reason=self.release_reason,
                                            eligible=raw_cap is not None,
                                            accepted_distance=self.approach.accepted_distance if self.approach else None,
                                            sample=sample, input_mono_time=now_ns)
    return self.decision
