"""Restart policy scoped to the optional navigation receiver."""
import time

from openpilot.common.swaglog import cloudlog
from openpilot.system.manager.process import PythonProcess


class ExternalNavigationProcess(PythonProcess):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.restart_at = 0.
    self.restart_delay = 1.
    self.started_at = None

  def _retry(self, now):
    self.restart_at = now + self.restart_delay
    self.restart_delay = min(30., self.restart_delay * 2)

  def start(self):
    now = time.monotonic()
    if self.shutting_down:
      self.stop()  # Re-enable must finish the previous intentional stop first.
    if self.proc is not None:
      if self.proc.exitcode is None:
        return
      if self.started_at is not None and now - self.started_at >= 60:
        self.restart_delay = 1.
      self.proc.join()  # Reap the exited child before releasing its handle.
      super().stop()
      self._retry(now)
    if now < self.restart_at:
      return
    try:
      super().start()
    except (OSError, RuntimeError):
      self.proc = None
      self._retry(now)
      cloudlog.exception('external navigation receiver launch failed')
    else:
      self.started_at = now

  def stop(self, *args, **kwargs):
    self.restart_at = 0.
    self.restart_delay = 1.
    self.started_at = None
    return super().stop(*args, **kwargs)
