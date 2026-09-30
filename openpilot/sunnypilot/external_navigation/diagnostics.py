"""Bounded private JSONL capture for prebuilt loggerd without the new service."""
import json
from pathlib import Path
import queue
import threading
import time

from openpilot.sunnypilot.external_navigation.config import navigation_root, ensure_private_directory


class DiagnosticsWriter:
  def __init__(self, directory: Path | None = None, max_bytes: int = 2 * 1024 * 1024, files: int = 20):
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
    paths = sorted(self.directory.glob('navigation-*.jsonl'))
    for path in paths[:max(0, len(paths) - self.files + 1)]:
      path.unlink()
    path = self.directory / f'navigation-{time.time_ns():020d}.jsonl'
    output = path.open('x', encoding='utf-8')
    path.chmod(0o600)
    return output

  def _run(self):
    output, size = None, 0
    try:
      while not self._stop.is_set() or not self.queue.empty():
        try:
          record = self.queue.get(timeout=.1)
        except queue.Empty:
          continue
        try:
          line = json.dumps({**record, 'capture_dropped': self.dropped}, ensure_ascii=False, separators=(',', ':')) + '\n'
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
