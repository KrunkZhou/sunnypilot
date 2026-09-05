from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence


class MotionDetector:
  """Start an episode on motion, then promote its status using accumulated evidence."""

  def __init__(self, *, threshold_mps2: float = 0.04, warning_persistence_seconds: float = 1.0,
               alarm_persistence_seconds: float = 30.0, quiet_close_seconds: float = 60.0,
               nominal_sample_seconds: float = 0.1, sample_stale_seconds: float = 1.0,
               clock: Callable[[], float] = time.monotonic):
    self.threshold_mps2 = threshold_mps2
    self.warning_persistence_seconds = warning_persistence_seconds
    self.alarm_persistence_seconds = alarm_persistence_seconds
    self.quiet_close_seconds = quiet_close_seconds
    self.nominal_sample_seconds = nominal_sample_seconds
    self.sample_stale_seconds = sample_stale_seconds
    self.clock = clock
    self.reset()

  def reset(self) -> None:
    self.previous_acceleration: tuple[float, float, float] | None = None
    self.previous_sample_at: float | None = None
    self.motion_evidence_seconds = 0.0
    self.first_motion_at: float | None = None
    self.last_motion_at: float | None = None
    self.episode_active = False
    self.warning_triggered = False
    self.alarm_triggered = False

  def update(self, acceleration: Sequence[float], now: float | None = None) -> str | None:
    now = self.clock() if now is None else now
    if len(acceleration) < 3:
      raise ValueError("accelerometer samples must contain x, y, and z")
    # Cap'n Proto sensor vectors support integer indexing, but not slicing.
    current = tuple(float(acceleration[index]) for index in range(3))
    if not all(math.isfinite(component) for component in current):
      raise ValueError("accelerometer samples must be finite")

    if self.previous_acceleration is None:
      self.previous_acceleration = current
      self.previous_sample_at = now
      return None

    assert self.previous_sample_at is not None
    elapsed = min(max(now - self.previous_sample_at, 0.0), self.nominal_sample_seconds * 2.0)
    if elapsed == 0.0:
      elapsed = self.nominal_sample_seconds
    delta = math.sqrt(sum((current[i] - self.previous_acceleration[i]) ** 2 for i in range(3)))
    self.previous_acceleration = current
    self.previous_sample_at = now

    if delta >= self.threshold_mps2:
      self.motion_evidence_seconds += elapsed
      self.first_motion_at = now if self.first_motion_at is None else self.first_motion_at
      self.last_motion_at = now
      if not self.episode_active:
        self.episode_active = True
        # Capture starts immediately; warning persistence only promotes the
        # episode's status. A sample can satisfy both without two events.
        self.warning_triggered = self.motion_evidence_seconds + 1e-9 >= self.warning_persistence_seconds
        return "motion"
    else:
      self.motion_evidence_seconds = max(0.0, self.motion_evidence_seconds - elapsed)

    if self.episode_active and not self.warning_triggered and self.motion_evidence_seconds + 1e-9 >= self.warning_persistence_seconds:
      self.warning_triggered = True
      return "warning"

    if self.episode_active and not self.alarm_triggered and self.motion_evidence_seconds + 1e-9 >= self.alarm_persistence_seconds:
      self.alarm_triggered = True
      return "alarm"

    if self.episode_active and self.last_motion_at is not None and now - self.last_motion_at >= self.quiet_close_seconds:
      self.reset()
      return "closed"

    return None

  def tick(self, now: float | None = None) -> str | None:
    """Advance episode quiet-time even when no accelerometer sample arrives."""
    now = self.clock() if now is None else now
    if self.previous_sample_at is not None and now - self.previous_sample_at >= self.sample_stale_seconds:
      self.invalidate_samples()
    if self.episode_active and self.last_motion_at is not None and now - self.last_motion_at >= self.quiet_close_seconds:
      self.reset()
      return "closed"
    return None

  def invalidate_samples(self) -> None:
    """Discard evidence across an unknown sensor gap while retaining episode quiet time."""
    self.previous_acceleration = None
    self.previous_sample_at = None
    self.motion_evidence_seconds = 0.0
    if not self.episode_active:
      self.first_motion_at = None
      self.last_motion_at = None
