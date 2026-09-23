"""UTC anchors for boot-relative measurements; a plausible date is not synchronization."""
import subprocess
import time
from datetime import UTC
from pathlib import Path

from openpilot.common.time_helpers import MAX_DATE, min_date

NANO = 1_000_000_000


def boot_ns():
  return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def boot_id():
  return str(Path("/proc/sys/kernel/random/boot_id").read_text().strip())


def python_to_boot_ns(message_ns, now_boot, now_monotonic):
  # messaging.new_message uses CLOCK_MONOTONIC, native CAN uses CLOCK_BOOTTIME.
  return message_ns + now_boot - now_monotonic


class MessageClock:
  def __init__(self):
    self.offset = None
    self.floor = 0

  def normalize(self, message_ns, before, now_boot, after):
    if not 0 <= after - before <= 50_000_000:
      return None
    offset = now_boot - (before + after) // 2
    if self.offset is not None and abs(offset - self.offset) > 50_000_000:
      # A queued Python message from before suspend cannot be mapped using
      # the new offset. Drop the backlog until a post-resume message arrives.
      self.floor = after
    self.offset = offset
    return message_ns + offset if message_ns > self.floor else None


def ntp_synchronized():
  try:
    result = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                            capture_output=True, timeout=1, check=False)
    return result.returncode == 0 and result.stdout.strip() == b"yes"
  except (OSError, subprocess.TimeoutExpired):
    return False


class TrustedClock:
  def __init__(self):
    self.anchor = None
    self.candidates = {}
    self.last_wall = None

  def check_wall(self, elapsed, wall):
    stepped = self.last_wall is not None and abs((wall - self.last_wall[1]) - (elapsed - self.last_wall[0])) > 2 * NANO
    self.last_wall = elapsed, wall
    if stepped:
      self.anchor = None
      self.candidates.clear()
    return stepped

  def observe(self, source, measured_boot, utc_ns, now_boot):
    if source not in ("gnss", "ntp") or not 0 <= now_boot - measured_boot <= 2 * NANO:
      return False
    # Broad sanity bound is additional validation, never proof of clock trust.
    if not min_date().replace(tzinfo=UTC).timestamp() * NANO < utc_ns < MAX_DATE.replace(tzinfo=UTC).timestamp() * NANO:
      return False
    candidate = (source, measured_boot, utc_ns - measured_boot)
    previous = self.candidates.get(source)
    if previous is not None and measured_boot > previous[1]:
      if utc_ns <= previous[1] + previous[2]:
        return False
      if abs(candidate[2] - previous[2]) <= NANO:
        if measured_boot - previous[1] < (NANO if source == "gnss" else 5 * NANO):
          return False
        self.anchor = candidate
        self.candidates[source] = candidate
        return True
      if abs(candidate[2] - previous[2]) > NANO:
        self.anchor = None
    self.candidates[source] = candidate
    return False

  def utc(self, measured_boot):
    if self.anchor is None:
      raise ValueError("UTC is not synchronized")
    return (measured_boot + self.anchor[2]) / NANO
