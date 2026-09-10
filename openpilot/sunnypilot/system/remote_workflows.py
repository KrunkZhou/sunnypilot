"""Athena-side receipts for native Params requests; never dispatches or replays work."""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path


class WorkflowBusy(RuntimeError):
  pass


def utc_now() -> str:
  return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class RequestState:
  """Serialize remote submissions and remember the latest receipt in this process."""
  def __init__(self):
    self._lock = threading.RLock()
    self._state: dict = {}

  @contextmanager
  def locked(self, timeout: float = 0.25):
    if not self._lock.acquire(timeout=timeout):
      raise WorkflowBusy("Another remote request is being submitted.")
    try:
      yield self
    finally:
      self._lock.release()

  def read(self) -> dict:
    return deepcopy(self._state)

  def write(self, state: dict) -> None:
    self._state = deepcopy(state)


class BackgroundSizeCache:
  """Metadata scans run in bounded batches; RPC callers read the cached total."""
  def __init__(self, path: Path):
    self.path = Path(path)
    self.value: int | None = None
    self._last = -float("inf")
    self._running = False
    self._lock = threading.Lock()

  def get(self) -> int | None:
    with self._lock:
      if not self._running and time.monotonic() - self._last >= 10:
        self._running = True
        threading.Thread(target=self._scan, daemon=True).start()
      return self.value

  def _scan(self):
    value = 0
    visited = 0
    deadline = time.monotonic() + 0.05

    def yield_if_needed():
      nonlocal visited, deadline
      visited += 1
      if visited >= 1_000 or time.monotonic() >= deadline:
        # Large or cold map trees may take many batches. Keep our position and
        # give other work time to run instead of discarding an unfinished scan.
        time.sleep(0.05)
        visited = 0
        deadline = time.monotonic() + 0.05

    try:
      pending = [self.path] if self.path.exists() else []
      while pending:
        yield_if_needed()
        with os.scandir(pending.pop()) as entries:
          for entry in entries:
            yield_if_needed()
            if entry.is_dir(follow_symlinks=False):
              pending.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
              value += entry.stat(follow_symlinks=False).st_size
    except OSError:
      value = None
    with self._lock:
      self.value = value
      self._last = time.monotonic()
      self._running = False
