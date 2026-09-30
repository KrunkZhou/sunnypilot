"""Bounded private JSONL capture for prebuilt loggerd without the new service."""
import json
from pathlib import Path
import queue
import re
import threading
import time

from openpilot.sunnypilot.external_navigation.config import navigation_root, ensure_private_directory


class DiagnosticsWriter:
  def __init__(self, directory: Path | None = None, max_bytes: int = 2 * 1024 * 1024, files: int = 20):
    if max_bytes < 1 or files < 1:
      raise ValueError('diagnostic limits must be positive')
    self.directory, self.max_bytes, self.files = directory or navigation_root() / 'logs', max_bytes, files
    self.queue = queue.Queue(maxsize=4)
    self.dropped = 0
    self._stop = threading.Event()
    self._thread = threading.Thread(target=self._run, name='external-nav-diagnostics', daemon=True)
    self._thread.start()

  def submit(self, record: dict) -> None:
    try:
      self.queue.put_nowait(record)
    except queue.Full:
      try:
        self.queue.get_nowait()
        self.dropped += 1
      except queue.Empty:
        pass
      try:
        self.queue.put_nowait(record)
      except queue.Full:
        self.dropped += 1

  def close(self) -> None:
    self._stop.set()
    self._thread.join(timeout=1)

  def _rotate(self):
    ensure_private_directory(self.directory, create=True)
    # Names are a persistent ordering sequence, not necessarily wall-clock time.
    # A boot before clock sync must not make the newest capture the next victim.
    paths = sorted(path for path in self.directory.glob('navigation-*.jsonl')
                   if re.fullmatch(r'navigation-\d{20}\.jsonl', path.name))
    previous = int(paths[-1].stem.split('-')[1]) if paths else 0
    ordinal = max(time.time_ns(), previous + 1)
    path = self.directory / f'navigation-{ordinal:020d}.jsonl'
    output = path.open('x', encoding='utf-8')
    try:
      path.chmod(0o600)
      # Only prune after creating the new file successfully, and never prune it.
      for old in paths[:max(0, len(paths) - self.files + 1)]:
        old.unlink()
      return output
    except BaseException:
      output.close()
      raise

  def _run(self):
    output, size = None, 0
    try:
      while not self._stop.is_set() or not self.queue.empty():
        try:
          record = self.queue.get(timeout=.1)
        except queue.Empty:
          continue
        try:
          line = json.dumps({**record, 'capture_wall_time_ns': time.time_ns(), 'capture_dropped': self.dropped},
                            ensure_ascii=False, separators=(',', ':')) + '\n'
          length = len(line.encode('utf-8'))
          if length > 8192:
            self.dropped += 1
            continue
          if output is None or size + length > self.max_bytes:
            if output is not None:
              output.close()
            output, size = self._rotate(), 0
          output.write(line)
          output.flush()
          size += length
        except (OSError, ValueError, TypeError):
          self.dropped += 1
          if output is not None:
            try:
              output.close()
            except OSError:
              pass
          output = None
    finally:
      if output is not None:
        output.close()


def receiver_record(receiver, now: int, status: str, rejection: str) -> dict:
  record = {'monotonic_ms': now, 'status': status, 'rejection': rejection}
  if receiver is not None:
    record.update(received_ms=receiver.received, rtt_ms=receiver.rtt_ms, ble_state=receiver.state)
    if (nav := receiver.navigation) is not None:
      record['snapshot'] = {'publisher_session': nav.session.hex(), 'cache_epoch': nav.epoch, 'stream': nav.stream,
                            'generation': nav.generation, 'token': nav.token, 'sequence': nav.sequence,
                            'available': nav.available, 'source_age_ms': nav.source_age, 'fields': nav.fields}
  return record
