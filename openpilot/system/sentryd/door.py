from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from openpilot.common.params import Params


MAX_CAR_PARAMS_BYTES = 256 * 1024
MAX_CAN_EVENT_BYTES = 256 * 1024
MAX_EVENTS_PER_POLL = 512
PROFILE_REFRESH_SECONDS = 1.0
SAMPLE_MAX_AGE_SECONDS = 1.0
GATEWAY_ADDRESS = 0x3DB
GATEWAY_BYTES = 8
DRIVER_DOOR_BYTE = 3
DRIVER_DOOR_MASK = 0x04
DISABLE_HINT = "Turn off Wait for Driver Exit for USB use or an unsupported vehicle."


@dataclass(frozen=True)
class DoorSample:
  open: bool
  monotonic_time: float


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
      except Exception as exc:
        # Params, capnp and the native socket can raise different exception
        # types. A bad saved profile must fail closed, not stop the daemon.
        self._bus = None
        self._profile = None
        self._socket = None
        self._last_sample_at = None
        detail = str(exc) if isinstance(exc, ValueError) else "Saved vehicle data could not be read"
        self.error = f"{detail}. {DISABLE_HINT}"
        return False
    if self._bus is None:
      return False
    if self._socket is None:
      try:
        self._socket = _open_can_socket()
      except Exception:
        self.error = f"Driver-door CAN receiver is unavailable. {DISABLE_HINT}"
        return False
    return True

  def poll(self, now: float, *, after: float) -> list[DoorSample]:
    if not math.isfinite(now) or not math.isfinite(after) or after > now:
      self.error = "Driver-door observation time is invalid"
      return []
    if after != self._after:
      self._after = after
      self._last_sample_at = None
    if not self._configure(now):
      return []

    samples: list[DoorSample] = []
    invalid_event = False
    for _ in range(MAX_EVENTS_PER_POLL):
      try:
        raw = self._socket.receive(non_blocking=True)
      except Exception:
        self._socket = None
        self.error = f"Driver-door CAN receiver failed. {DISABLE_HINT}"
        return samples
      if raw is None:
        break
      try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CAN_EVENT_BYTES:
          raise ValueError("invalid CAN event size")
        event = _decode_can_event(raw)
        if event.which() != "can" or not event.valid:
          invalid_event = True
          continue
        timestamp_ns = event.logMonoTime
        timestamp = timestamp_ns / 1e9
        if timestamp_ns <= self._last_event_ns or timestamp < after or not 0 <= now - timestamp < SAMPLE_MAX_AGE_SECONDS:
          continue
        self._last_event_ns = timestamp_ns
        for frame in event.can:
          if frame.src != self._bus or frame.address != GATEWAY_ADDRESS:
            continue
          data = frame.dat
          if len(data) != GATEWAY_BYTES:
            invalid_event = True
            continue
          # vw_mqb.dbc: Gateway_72 / ZV_FT_offen is little-endian bit 26.
          # Exact-size checking matters: missing bytes must never mean closed.
          samples.append(DoorSample(bool(data[DRIVER_DOOR_BYTE] & DRIVER_DOOR_MASK), timestamp))
          self._last_sample_at = timestamp
      except Exception:
        invalid_event = True

    if self._last_sample_at is not None and 0 <= now - self._last_sample_at < SAMPLE_MAX_AGE_SECONDS:
      self.error = None
    elif invalid_event:
      self.error = f"Driver-door CAN data is invalid. {DISABLE_HINT}"
    else:
      self.error = f"No fresh driver-door CAN samples. {DISABLE_HINT}"
    return samples
