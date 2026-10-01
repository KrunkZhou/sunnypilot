"""Optional planner input adapter; navigation cannot determine planner health."""
from __future__ import annotations

import math
import time

from openpilot.sunnypilot.external_navigation.protocol import UNKNOWN_AGE, age_sum
from openpilot.sunnypilot.external_navigation.settings import read_mode, read_turn_speed_control
from openpilot.sunnypilot.external_navigation.speed_control import NavigationSpeedController, NavigationSpeedSample, TURN_SPEED


class ExternalNavigationSpeed:
  def __init__(self, CP, *, clock=time.monotonic_ns, cruise_unset=255):
    self.capable = bool(CP.openpilotLongitudinalControl)
    self.cruise_unset = cruise_unset
    self.clock = clock
    self.controller = NavigationSpeedController()
    self.decision = self.controller.decision
    self.enabled = False
    self.mode = 0
    self.next_settings_ns = 0

  def update(self, sm, *, baseline_target: float, now_ns: int | None = None):
    now_ns = self.clock() if now_ns is None else now_ns
    if type(now_ns) is not int or not 0 <= now_ns <= 0xffffffffffffffff:
      self.decision = self.controller.update(None, now_ns=now_ns, v_ego=0., baseline_target=baseline_target)
      return self.decision
    if now_ns >= self.next_settings_ns:
      self.enabled, self.mode = read_turn_speed_control(), read_mode()
      self.next_settings_ns = now_ns + 1_000_000_000
    sample = None
    nav_seen = getattr(sm, 'seen', {}).get('externalNavigationSP', False)
    if nav_seen and getattr(sm, 'valid', {}).get('externalNavigationSP', False):
      nav = sm['externalNavigationSP']
      delivery_ns = now_ns - nav.receiveMonoTime
      # Ceil milliseconds: a timestamp just beyond the TTL must not become fresh by rounding.
      delivery_age = min(UNKNOWN_AGE, (delivery_ns + 999_999) // 1_000_000) if delivery_ns >= 0 else -1
      identity = ((bytes(nav.publisherSession), nav.cacheEpoch, nav.streamId, nav.generation, nav.maneuverId)
                  if nav.hasManeuverId else None)
      sample = NavigationSpeedSample(
        identity, (bytes(nav.receiverSession), nav.token), nav.routeState,
        nav.maneuverType if nav.hasManeuver else None, nav.nextDistance if nav.hasNextDistance else None,
        nav.distanceObservation, nav.maneuverObservation,
        age_sum(nav.distanceAgeMs, max(0, delivery_age)), age_sum(nav.maneuverAgeMs, max(0, delivery_age)), delivery_age,
        nav.connected, nav.available,
        receive_mono_time=nav.receiveMonoTime, source_age_ms=nav.sourceAgeMs,
        transport_rtt_ms=nav.transportRttMs, snapshot_sequence=nav.snapshotSequence,
      )
    cs, cc, controls, sd = (sm[name] for name in ('carState', 'carControl', 'controlsState', 'selfdriveState'))
    healthy = all(getattr(sm, check, {}).get(name, False) for check in ('valid', 'alive')
                  for name in ('carState', 'carControl', 'controlsState', 'selfdriveState'))
    long_state = getattr(controls, 'longControlState', 'off')
    long_off = getattr(long_state, 'raw', long_state) in (0, 'off')
    cruise = getattr(cs, 'vCruise', self.cruise_unset)
    initialized = isinstance(cruise, (float, int)) and math.isfinite(cruise) and 0 <= cruise < self.cruise_unset
    active = self.capable and healthy and cc.enabled and cc.longActive and sd.enabled and not long_off and initialized
    override = bool(cs.gasPressed or cs.brakePressed or getattr(cs, 'regenBraking', False) or cc.cruiseControl.override)
    reason = ''
    if not self.enabled:
      reason = 'off'
    elif self.mode != 1:
      reason = 'assisted_turns_required'
    elif not self.capable:
      reason = 'longitudinal_unsupported'
    elif not healthy:
      reason = 'control_input_invalid'
    elif not initialized:
      reason = 'cruise_uninitialized'
    self.decision = self.controller.update(sample, now_ns=now_ns, enabled=self.enabled and self.mode == 1,
                                           longitudinal_active=active, driver_override=override,
                                           v_ego=cs.vEgo, baseline_target=baseline_target, inhibit_reason=reason)
    return self.decision

  def fill(self, message, *, baseline_source, baseline_target: float, cruise_selected: bool = False) -> None:
    d = self.decision
    message.enabled = self.enabled
    message.eligible = d.eligible
    message.state, message.reason, message.releaseReason = d.state, d.reason, d.release_reason
    message.inputMonoTime = d.input_mono_time
    message.nominalTurnSpeed = TURN_SPEED
    message.capAvailable, message.speedSelected = d.raw_cap is not None, d.speed_cap is not None
    message.cruiseCandidateSelected = d.speed_cap is not None and cruise_selected
    message.rawCap, message.appliedCap = d.raw_cap or 0., d.speed_cap or 0.
    message.baselineSource, message.baselineTarget = baseline_source, float(baseline_target)
    message.distanceAgeMs = message.maneuverAgeMs = message.deliveryAgeMs = UNKNOWN_AGE
    message.sourceAgeMs, message.routeState = UNKNOWN_AGE, 255
    if (s := d.sample) is not None:
      if s.identity is not None:
        message.publisherSession, message.cacheEpoch, message.streamId, message.generation, message.maneuverId = s.identity
      if s.transport is not None:
        message.receiverSession, message.transportToken = s.transport
      message.routeState = s.route_state
      message.hasManeuver = s.maneuver_type is not None
      message.maneuverType = s.maneuver_type or 0
      message.hasDistance = s.distance is not None
      message.distance = float(s.distance or 0.)
      message.distanceObservation, message.maneuverObservation = s.distance_observation, s.maneuver_observation
      message.distanceAgeMs, message.maneuverAgeMs = s.distance_age_ms, s.maneuver_age_ms
      message.deliveryAgeMs = min(UNKNOWN_AGE, s.delivery_age_ms) if s.delivery_age_ms >= 0 else UNKNOWN_AGE
      message.receiveMonoTime, message.sourceAgeMs = s.receive_mono_time, s.source_age_ms
      message.transportRttMs, message.snapshotSequence = s.transport_rtt_ms, s.snapshot_sequence
    message.hasAcceptedDistance = d.accepted_distance is not None
    message.acceptedDistance = d.accepted_distance or 0.
