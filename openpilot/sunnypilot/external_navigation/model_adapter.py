"""Optional model input arbitration. Disabled/missing navigation cannot fail model health checks."""
from __future__ import annotations

import time

from openpilot.sunnypilot.external_navigation.settings import read_mode
from openpilot.sunnypilot.external_navigation.policy import Sample, TurnPolicy


class ExternalNavigationHints:
  def __init__(self):
    from openpilot.cereal.messaging import SubMaster
    self.sm = SubMaster(['externalNavigationSP'])  # Optional, never included in model/all_checks.
    self.policy = TurnPolicy()
    self.mode = 0
    self.next_parameter_read = 0.
    self.decision = self.policy.status
    self.last_model = None
    self.last_transport = None
    self.turn_event = self.turn_event_time = 0
    self.input_time = self.distance_observation = self.transport_token = 0
    self.distance_age = self.receive_age = 0xffffffff

  def update(self, sm, desire_helper, model):
    now = time.monotonic()
    self.sm.update(0)
    if now >= self.next_parameter_read:
      self.mode = read_mode()
      self.next_parameter_read = now + 1
    if self.last_model is not model:
      self.policy._reset()  # Model switch must observe a new approach, never use an already armed turn.
      self.last_model = model
    sample = None
    self.input_time = time.monotonic_ns()
    self.distance_age = self.receive_age = 0xffffffff
    self.distance_observation = self.transport_token = 0
    nav = self.sm['externalNavigationSP']
    if self.sm.valid['externalNavigationSP'] and self.sm.seen['externalNavigationSP']:
      transport = bytes(getattr(nav, 'receiverSession', b'')), bytes(nav.publisherSession), nav.token
      if transport != self.last_transport:
        self.policy._reset()  # A missed transient reset publication cannot carry an armed turn across BLE resync.
        self.last_transport = transport
      receive_age = (self.input_time - nav.receiveMonoTime) // 1000000
      self.receive_age = min(0xffffffff, receive_age) if receive_age >= 0 else 0xffffffff
      self.distance_age = min(0xffffffff, nav.distanceAgeMs + max(0, receive_age))
      self.distance_observation, self.transport_token = nav.distanceObservation, nav.token
      sample = Sample(
        (bytes(nav.publisherSession), nav.cacheEpoch, nav.streamId, nav.generation, nav.maneuverId),
        nav.routeState, nav.maneuverType, nav.nextDistance if nav.hasNextDistance else None,
        nav.distanceObservation, min(0xffffffff, nav.distanceAgeMs + max(0, receive_age)),
        nav.connected and 0 <= receive_age <= 1000, nav.available,
        nav.hasManeuverId and nav.distanceObservation > 0 and nav.maneuverObservation > 0,
      )
    cs = sm['carState']
    manual = bool(cs.steeringPressed or cs.brakePressed or cs.leftBlinker or cs.rightBlinker
                  or cs.leftBlindspot or cs.rightBlindspot or int(desire_helper.lane_change_state) != 0
                  or int(desire_helper.desire) != 0)
    self.decision = self.policy.update(
      sample, mode=self.mode,
      lateral_active=sm['carControl'].latActive and sm.valid['carState'] and sm.valid['carControl']
                     and sm.alive['carState'] and sm.alive['carControl'],
      speed=cs.vEgo, speed_limit=desire_helper.lane_turn_controller.lane_turn_value, manual=manual)
    # Existing desires always win. The policy returns only none/turnLeft/turnRight.
    return desire_helper.desire if int(desire_helper.desire) != 0 else self.decision.desire

  def model_completed(self, desire: int, effective_pulse) -> None:
    """Record only a navigation pulse that survived edge filtering and inference.

    Called once after a successful model.run, before publishing that frame. A
    proposal, an existing driver desire, or an inference failure is not an event.
    """
    if (self.decision.assisted and self.decision.proposal == self.decision.desire == desire
        and desire in (1, 2) and len(effective_pulse) > desire and float(effective_pulse[desire]) > .99):
      self.turn_event, self.turn_event_time = desire, time.monotonic_ns()

  def fill(self, message) -> None:
    message.navigationHint = self.decision.proposal
    message.navigationHintStatus = self.decision.reason
    message.navigationAssisted = self.decision.assisted
    message.navigationTurnEvent = self.turn_event
    message.navigationTurnEventMonoTime = self.turn_event_time
    message.navigationInputDistanceAgeMs = self.distance_age
    message.navigationInputReceiveAgeMs = self.receive_age
    message.navigationInputMonoTime = self.input_time
    message.navigationDistanceObservation = self.distance_observation
    message.navigationTransportToken = self.transport_token
