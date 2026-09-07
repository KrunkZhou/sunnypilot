from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from openpilot.system.sentryd.flash import FAST_SAMPLE_MAX_GAP_SECONDS, IDLE_SAMPLE_MAX_GAP_SECONDS, LightSample

if TYPE_CHECKING:
  from openpilot.common.params import Params


MAX_CAR_PARAMS_BYTES = 256 * 1024
MAX_CAN_EVENT_BYTES = 256 * 1024
MAX_EVENTS_PER_POLL = 512
PROFILE_REFRESH_SECONDS = 1.0
SAMPLE_MAX_AGE_SECONDS = 1.0
GATEWAY_ADDRESS = 0x3DB
GATEWAY_BYTES = 8
LIGHT_ADDRESS = 0x366
LIGHT_BYTES = 8
DRIVER_DOOR_BYTE = 3
DRIVER_DOOR_MASK = 0x04
DISABLE_HINT = "Turn off Wait for Driver Exit for USB use or an unsupported vehicle."


@dataclass(frozen=True)
class DoorSample:
  open: bool
  monotonic_time: float
  passenger_open: bool = False
  rear_left_open: bool = False
  rear_right_open: bool = False
  trunk_open: bool = False

  @property
  def open_doors(self) -> frozenset[str]:
    return frozenset(name for name, opened in (
      ("driver", self.open), ("passenger", self.passenger_open),
      ("rear_left", self.rear_left_open), ("rear_right", self.rear_right_open), ("trunk", self.trunk_open),
    ) if opened)


def _open_can_socket():
  import openpilot.cereal.messaging as messaging
  # Preserve an open/close pair even if both arrive between daemon iterations.
  return messaging.sub_sock("can", conflate=False)


def _decode_can_event(raw: bytes):
  from openpilot.cereal import log
  with log.Event.from_bytes(raw, traversal_limit_in_words=MAX_CAN_EVENT_BYTES // 8, nesting_limit=16) as event:
    return event


def _profile_bus(raw: bytes) -> int:
  from opendbc.car import Bus
  from opendbc.car.structs import car
  from opendbc.car.volkswagen.values import CanBus, DBC

  with car.CarParams.from_bytes(raw, traversal_limit_in_words=MAX_CAR_PARAMS_BYTES // 8, nesting_limit=32) as cp:
    # Gateway_72's driver-door mapping is shared by the classic MQB family,
    # including Audi A3 Mk3. Do not guess a mapping for another platform.
    if cp.notCar or DBC.get(cp.carFingerprint, {}).get(Bus.pt) != "vw_mqb":
      raise ValueError("Driver door signal is unsupported for this vehicle")
    if not 1 <= len(cp.safetyConfigs) <= 4:
      raise ValueError("Saved vehicle CAN bus configuration is invalid")
    bus = CanBus(cp).pt
    if not 0 <= bus <= 12 or bus % 4:
      raise ValueError("Saved vehicle CAN bus configuration is invalid")
    return bus


class DriverDoorSource:
  """Passive parked driver-door observations; never sends CAN or starts card."""

  def __init__(self, params: Params):
    self.params = params
    self.error: str | None = None
    self._socket = None
    self._bus: int | None = None
    self._profile: bytes | None = None
    self._last_profile_check = float("-inf")
    self._last_event_ns = -1
    self._last_sample_at: float | None = None
    self._after: float | None = None
    self.queue_drained = True
    self.generation = 0
    self.light_samples: list[LightSample] = []
    self.light_error: str | None = None
    self.light_generation = 0
    self._last_light_sample: LightSample | None = None

  def _invalidate_lights(self, error: str) -> None:
    self.light_generation += 1
    self.light_error = error
    self.light_samples = []
    self._last_light_sample = None

  def _configure(self, now: float) -> bool:
    if now - self._last_profile_check >= PROFILE_REFRESH_SECONDS:
      self._last_profile_check = now
      try:
        raw = self.params.get("CarParamsPersistent")
        if raw is None or raw == b"":
          raise ValueError("No saved vehicle is available for driver-door detection")
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CAR_PARAMS_BYTES:
          raise ValueError("Saved vehicle data is invalid or oversized")
        if raw != self._profile:
          bus = _profile_bus(raw)
          self._bus = bus
          self._profile = raw
          self._socket = None
          self._last_sample_at = None
          self._last_event_ns = -1
          self.generation += 1
          self._invalidate_lights("Vehicle CAN profile changed; waiting for indicator samples")
      except Exception as exc:
        # Params, capnp and the native socket can raise different exception
        # types. A bad saved profile must fail closed, not stop the daemon.
        if self._bus is not None or self._profile is not None or self._socket is not None:
          self.generation += 1
        self._bus = None
        self._profile = None
        self._socket = None
        self._last_sample_at = None
        detail = str(exc) if isinstance(exc, ValueError) else "Saved vehicle data could not be read"
        self.error = f"{detail}. {DISABLE_HINT}"
        self._invalidate_lights("Vehicle profile is unavailable for parked indicator detection")
        return False
    if self._bus is None:
      return False
    if self._socket is None:
      try:
        self._socket = _open_can_socket()
      except Exception:
        self.error = f"Driver-door CAN receiver is unavailable. {DISABLE_HINT}"
        self._invalidate_lights("Parked indicator CAN receiver is unavailable")
        return False
    return True

  def poll(self, now: float, *, after: float) -> list[DoorSample]:
    self.queue_drained = True
    self.light_samples = []
    if not math.isfinite(now) or not math.isfinite(after) or after > now:
      self.error = "Driver-door observation time is invalid"
      self._invalidate_lights("Indicator observation time is invalid")
      return []
    if after != self._after:
      self._after = after
      self._last_sample_at = None
      self._invalidate_lights("Parked observation boundary changed; waiting for indicator samples")
    if not self._configure(now):
      return []

    samples: list[DoorSample] = []
    invalid_event = False
    for _ in range(MAX_EVENTS_PER_POLL):
      try:
        raw = self._socket.receive(non_blocking=True)
      except Exception:
        self._socket = None
        self.generation += 1
        self.error = f"Driver-door CAN receiver failed. {DISABLE_HINT}"
        self._invalidate_lights("Parked indicator CAN receiver failed")
        return samples
      if raw is None:
        break
      try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CAN_EVENT_BYTES:
          raise ValueError("invalid CAN event size")
        event = _decode_can_event(raw)
        if event.which() != "can" or not event.valid:
          invalid_event = True
          self._invalidate_lights("Indicator CAN event is invalid")
          continue
        timestamp_ns = event.logMonoTime
        timestamp = timestamp_ns / 1e9
        if timestamp_ns <= self._last_event_ns or timestamp < after or not 0 <= now - timestamp <= IDLE_SAMPLE_MAX_GAP_SECONDS:
          if any(frame.src == self._bus and frame.address == LIGHT_ADDRESS for frame in event.can):
            self._invalidate_lights("Indicator CAN timestamp is stale, reordered or outside the parked interval")
          continue
        self._last_event_ns = timestamp_ns
        for frame in event.can:
          if frame.src != self._bus:
            continue
          data = frame.dat
          if frame.address == LIGHT_ADDRESS:
            if len(data) != LIGHT_BYTES:
              self._invalidate_lights("Indicator CAN frame length is invalid")
              continue
            # Blinkmodi_02 exterior bulb bits, NOT the comfort/stalk signals.
            light = LightSample(
              left=bool(data[3] & (0x02 | 0x08)), right=bool(data[3] & (0x04 | 0x10)),
              hazards=bool(data[2] & 0x10), fast=bool(data[4] & 0x20), monotonic_time=timestamp,
            )
            maximum_age = FAST_SAMPLE_MAX_GAP_SECONDS if light.fast else IDLE_SAMPLE_MAX_GAP_SECONDS
            if now - timestamp > maximum_age:
              self._invalidate_lights("Indicator CAN sample is stale")
              continue
            if self._last_light_sample is not None and timestamp <= self._last_light_sample.monotonic_time:
              self._invalidate_lights("Indicator CAN samples are reordered or duplicated")
              continue
            self.light_samples.append(light)
            self._last_light_sample = light
            self.light_error = None
            continue
          if frame.address != GATEWAY_ADDRESS or now - timestamp >= SAMPLE_MAX_AGE_SECONDS:
            continue
          if len(data) != GATEWAY_BYTES:
            invalid_event = True
            continue
          # vw_mqb.dbc: Gateway_72 / ZV_FT_offen is little-endian bit 26.
          # Exact-size checking matters: missing bytes must never mean closed.
          samples.append(DoorSample(
            bool(data[DRIVER_DOOR_BYTE] & DRIVER_DOOR_MASK), timestamp,
            passenger_open=bool(data[3] & 0x01), rear_left_open=bool(data[2] & 0x10),
            rear_right_open=bool(data[2] & 0x20), trunk_open=bool(data[3] & 0x40),
          ))
          self._last_sample_at = timestamp
      except Exception:
        invalid_event = True
        self._invalidate_lights("Indicator CAN event is malformed or oversized")
    else:
      # A queued open at event 513 must not lose to the tenth motion hit.
      self.queue_drained = False

    if self._last_sample_at is not None and 0 <= now - self._last_sample_at < SAMPLE_MAX_AGE_SECONDS:
      self.error = None
    elif invalid_event:
      self.error = f"Driver-door CAN data is invalid. {DISABLE_HINT}"
    else:
      self.error = f"No fresh driver-door CAN samples. {DISABLE_HINT}"
    light = self._last_light_sample
    if light is None:
      self.light_error = self.light_error or "No fresh parked indicator CAN samples"
    else:
      maximum_age = FAST_SAMPLE_MAX_GAP_SECONDS if light.fast else IDLE_SAMPLE_MAX_GAP_SECONDS
      if now - light.monotonic_time > maximum_age:
        # Ordinary bus sleep is absence, not a new invalidation generation.
        # The classifier independently discards incomplete sequences on gaps.
        self.light_error = "Parked indicator CAN samples are stale"
    return samples
