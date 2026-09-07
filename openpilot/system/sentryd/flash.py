from __future__ import annotations

import math
from dataclasses import dataclass


PULSE_MIN_SECONDS = 0.10
PULSE_MAX_SECONDS = 1.20
GAP_MIN_SECONDS = 0.10
GAP_MAX_SECONDS = 1.50
QUIET_SECONDS = 2.0
SEQUENCE_MAX_SECONDS = 6.0
FAST_SAMPLE_MAX_GAP_SECONDS = 0.25
IDLE_SAMPLE_MAX_GAP_SECONDS = 1.50
TIME_EPSILON = 1e-9


@dataclass(frozen=True)
class LightSample:
  left: bool
  right: bool
  hazards: bool
  fast: bool
  monotonic_time: float


class FlashDetector:
  """Conservative single/double exterior-flash inference, never actual lock state."""

  def __init__(self):
    self.reset()

  def reset(self, *, require_quiet: bool = False) -> None:
    self.error: str | None = None
    self.pulse_count = 0
    self._phase = "idle"
    self._baseline = False
    self._quarantined = require_quiet
    self._quiet_started_at: float | None = None
    self._sequence_started_at: float | None = None
    self._pulse_started_at: float | None = None
    self._off_started_at: float | None = None
    self._previous: LightSample | None = None

  @property
  def pending(self) -> bool:
    return self._phase in ("on", "gap")

  def _invalidate(self, error: str) -> None:
    # A later pulse must not become a fresh single-flash suffix of a rejected
    # hazard/three-flash/incomplete sequence. Require an observed quiet gap.
    self.reset(require_quiet=True)
    self.error = error

  def tick(self, now: float) -> None:
    """Invalidate missing/overlong data; silence can never complete a flash."""
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
      self._invalidate("Indicator observation time is invalid")
      return
    previous = self._previous
    if previous is not None:
      maximum_gap = FAST_SAMPLE_MAX_GAP_SECONDS if previous.fast else IDLE_SAMPLE_MAX_GAP_SECONDS
      if now < previous.monotonic_time or now - previous.monotonic_time > maximum_gap + TIME_EPSILON:
        self._invalidate("Indicator samples are stale or reordered")
        return
    if self._sequence_started_at is not None and now - self._sequence_started_at > SEQUENCE_MAX_SECONDS + TIME_EPSILON:
      self._invalidate("Indicator flash sequence exceeded six seconds")

  def update(self, sample: LightSample) -> str | None:
    if (not isinstance(sample, LightSample) or
        any(type(value) is not bool for value in (sample.left, sample.right, sample.hazards, sample.fast)) or
        type(sample.monotonic_time) not in (int, float) or not math.isfinite(sample.monotonic_time) or sample.monotonic_time < 0):
      self._invalidate("Indicator sample is invalid")
      return None
    now = sample.monotonic_time
    if self._previous is not None and now <= self._previous.monotonic_time:
      self._invalidate("Indicator samples are stale or reordered")
      return None
    # Use the PREVIOUS rate flag: a newly received slow/OFF frame cannot prove
    # that missing 50-Hz frames contained no extra pulse or hazard activation.
    self.tick(now)
    self._previous = sample
    if sample.hazards:
      self._invalidate("Hazard flashers are active; lock inference ignored")
      self._previous = sample
      return None
    if sample.left != sample.right:
      self._invalidate("Asymmetric indicator sequence ignored")
      self._previous = sample
      return None

    if self._quarantined:
      if sample.left:
        self._quiet_started_at = None
      elif self._quiet_started_at is None:
        self._quiet_started_at = now
      elif now - self._quiet_started_at >= QUIET_SECONDS - TIME_EPSILON:
        self._quarantined = False
        self._baseline = True
        self.error = None
      return None
    if not self._baseline:
      if sample.left:
        self._invalidate("Initial ON sample is incomplete; waiting for quiet indicators")
        self._previous = sample
      else:
        self._baseline = True
      return None
    if self._phase == "idle":
      if sample.left:
        self._phase = "on"
        self._sequence_started_at = self._pulse_started_at = now
        self.pulse_count = 0
      return None
    if self._phase == "on":
      duration = now - self._pulse_started_at
      if duration > PULSE_MAX_SECONDS + TIME_EPSILON or (not sample.left and duration < PULSE_MIN_SECONDS - TIME_EPSILON):
        self._invalidate("Indicator pulse duration is outside the accepted range")
        self._previous = sample
      elif not sample.left:
        self.pulse_count += 1
        self._phase = "gap"
        self._off_started_at = now
      return None

    gap = now - self._off_started_at
    if sample.left:
      if self.pulse_count >= 2 or not GAP_MIN_SECONDS - TIME_EPSILON <= gap <= GAP_MAX_SECONDS + TIME_EPSILON:
        self._invalidate("Additional or mistimed indicator pulse ignored")
        self._previous = sample
      else:
        self._phase = "on"
        self._pulse_started_at = now
      return None
    if gap >= QUIET_SECONDS - TIME_EPSILON:
      result = "locked" if self.pulse_count == 1 else "unlocked"
      self.reset()
      self._previous = sample
      self._baseline = True
      return result
    return None
