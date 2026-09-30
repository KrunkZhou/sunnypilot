import json
from pathlib import Path
import tempfile
import time
import unittest

from openpilot.sunnypilot.external_navigation.diagnostics import DiagnosticsWriter, receiver_record
from openpilot.sunnypilot.external_navigation.protocol import Receiver


class DiagnosticsTests(unittest.TestCase):
  def test_ring_bound_and_private_files(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      writer = DiagnosticsWriter(root, max_bytes=100, files=3)
      for index in range(15):
        writer.submit({'index': index, 'data': 'x' * 50})
        time.sleep(.01)
      writer.close()
      paths = list(root.glob('navigation-*.jsonl'))
      self.assertLessEqual(len(paths), 3)
      self.assertTrue(paths)
      for path in paths:
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for line in path.read_text().splitlines():
          self.assertIn('index', json.loads(line))

  def test_diagnostics_never_include_key_or_packet(self):
    receiver = Receiver(b'\x01' * 16, b'\x02' * 32)
    record = receiver_record(receiver, 10, 'waiting', '')
    self.assertNotIn(receiver.key.hex(), json.dumps(record))
    self.assertNotIn('key', record)
    self.assertNotIn('packet', record)


if __name__ == '__main__':
  unittest.main()
