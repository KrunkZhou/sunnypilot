from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence


WARNING_TRIGGER_COUNT = 10
ALARM_TRIGGER_COUNT = 25
ALARM_TIME_SECONDS = 30.0
RESET_TIME_SECONDS = 60.0


class MotionDetector:
  """StarPilot magnitude-change counting, with an immediate provisional edge."""

  def __init__(self, *, threshold_mps2: float = 0.04, sample_stale_seconds: float = 1.0,
               clock: Callable[[], float] = time.monotonic):
    self.threshold_mps2 = threshold_mps2
    self.sample_stale_seconds = sample_stale_seconds
    self.clock = clock
    self.reset()

  def reset(self) -> None:
    self.previous_acceleration: tuple[float, float, float] | None = None
    self.previous_sample_at: float | None = None
    self.trigger_count = 0
    self.first_motion_at: float | None = None
    self.last_motion_at: float | None = None
    self.episode_active = False
    self.warning_triggered = False
    self.alarm_triggered = False

  def update(self, acceleration: Sequence[float], now: float | None = None) -> str | None:
    now = self.clock() if now is None else now
    expired = self.tick(now)
    if expired is not None:
      return expired
    if len(acceleration) < 3:
      raise ValueError("accelerometer samples must contain x, y, and z")
    # Cap'n Proto lists support integer indexing but not Python slicing.
    current = tuple(float(acceleration[index]) for index in range(3))
    if not all(math.isfinite(component) for component in current):
      raise ValueError("accelerometer samples must be finite")
    previous = self.previous_acceleration
    previous_at = self.previous_sample_at
    self.previous_acceleration = current
    self.previous_sample_at = now
    if previous is None or previous_at is None or now <= previous_at:
      return None
    current_magnitude, previous_magnitude = math.hypot(*current), math.hypot(*previous)
    if not math.isfinite(current_magnitude) or not math.isfinite(previous_magnitude):
      raise ValueError("accelerometer magnitude must be finite")
    delta = abs(current_magnitude - previous_magnitude)
    if delta <= self.threshold_mps2:
      return None
    self.trigger_count += 1
    self.last_motion_at = now
    if not self.episode_active:
      self.first_motion_at = now
      self.episode_active = True
      return "motion"
    if not self.warning_triggered and self.trigger_count >= WARNING_TRIGGER_COUNT:
      self.warning_triggered = True
      return "warning"
    if (not self.alarm_triggered and self.trigger_count > ALARM_TRIGGER_COUNT
        and self.first_motion_at is not None and now - self.first_motion_at >= ALARM_TIME_SECONDS):
      self.alarm_triggered = True
      return "alarm"
    return None

  def tick(self, now: float | None = None) -> str | None:
    now = self.clock() if now is None else now
    # Expiry wins over a tenth hit arriving exactly at the deadline.
    if self.first_motion_at is not None and now - self.first_motion_at >= RESET_TIME_SECONDS:
      self.reset()
      return "closed"
    if self.previous_sample_at is not None and now - self.previous_sample_at >= self.sample_stale_seconds:
      provisional = self.episode_active and not self.warning_triggered
      self.invalidate_samples()
      if provisional:
        return "discarded"
    return None

  def invalidate_samples(self) -> None:
    if not self.warning_triggered:
      self.reset()
    else:
      # Confirmed events survive gaps; the next sample only establishes a baseline.
      self.previous_acceleration = None
      self.previous_sample_at = None
