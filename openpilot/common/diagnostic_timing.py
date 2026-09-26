"""Timing provenance for diagnostics; every monotonic value uses CLOCK_MONOTONIC.

Kernel boot identity is required for reconstruction. A process UUID, route ID, or
wall-clock-derived fallback cannot identify the same boot across producers.
"""
import datetime
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from openpilot.common.time_helpers import MAX_DATE, min_date


@lru_cache(maxsize=1)
def get_boot_id() -> str | None:
  try:
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    parsed = uuid.UUID(value)
    if parsed.int != 0 and str(parsed) == value:
      return value
  except (OSError, UnicodeError, ValueError):
    pass
  return None


@dataclass(frozen=True)
class DiagnosticTiming:
  wall_time_ns: int
  monotonic_ns: int
  boot_id: str | None

  def log_fields(self) -> dict[str, str]:
    fields = {"wall_time_ns": str(self.wall_time_ns), "monotonic_ns": str(self.monotonic_ns)}
    if self.boot_id is not None:
      fields["boot_id"] = self.boot_id
    return fields

  def stats_fields(self) -> str:
    # Influx fields, never tags: boot identity must not create a new series.
    fields = f"rtzs_monotonic_ns={self.monotonic_ns}i"
    if self.boot_id is not None:
      fields += f',rtzs_boot_id="{self.boot_id}"'
    return fields


def capture_timing() -> DiagnosticTiming:
  wall_time_ns = time.time_ns()
  monotonic_ns = time.monotonic_ns()
  return DiagnosticTiming(wall_time_ns, monotonic_ns, get_boot_id())


def gps_clock_anchor(gps, sample_monotonic_ns: int) -> dict | None:
  """Use the GPS sample's time, not the later timed/logging receipt time.

  Both qcomgpsd and ubloxd publish with Python messaging.new_message, whose
  logMonoTime is CLOCK_MONOTONIC. C++ cereal's CLOCK_BOOTTIME is not interchangeable.
  """
  boot_id = get_boot_id()
  unix_ms = gps.unixTimestampMillis
  if boot_id is None or not gps.hasFix or type(unix_ms) is not int or type(sample_monotonic_ns) is not int:
    return None
  age_ns = time.monotonic_ns() - sample_monotonic_ns
  if sample_monotonic_ns <= 0 or not 0 <= age_ns <= 2_000_000_000:
    return None
  try:
    utc = datetime.datetime.fromtimestamp(unix_ms / 1000, datetime.UTC).replace(tzinfo=None)
  except (OverflowError, OSError, ValueError):
    return None
  if not min_date() <= utc <= MAX_DATE:
    return None
  return {
    "version": 1, "source": "gps", "clock": "monotonic", "boot_id": boot_id,
    "monotonic_ns": str(sample_monotonic_ns), "wall_time_ns": str(unix_ms * 1_000_000),
  }


def clock_jump(old_offset_ns: int, new_offset_ns: int, monotonic_ns: int) -> dict | None:
  boot_id = get_boot_id()
  if boot_id is None:
    return None
  return {
    "version": 1, "clock": "monotonic", "boot_id": boot_id,
    "old_offset_ns": str(old_offset_ns), "new_offset_ns": str(new_offset_ns), "monotonic_ns": str(monotonic_ns),
  }
