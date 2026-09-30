import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from openpilot.sunnypilot.external_navigation.diagnostics import DiagnosticsWriter, receiver_record
from openpilot.sunnypilot.external_navigation.protocol import Receiver


class DiagnosticsTests(unittest.TestCase):
  def test_clock_rollback_and_repeated_timestamps_keep_newest_files(self):
    with tempfile.TemporaryDirectory() as directory:
      # Exercise the production rotator directly, without scheduling sleeps.
      writer = DiagnosticsWriter.__new__(DiagnosticsWriter)
      writer.directory, writer.files = Path(directory), 3
      saved_names = []
      for index, clock in enumerate((900, 901, 20, 20, 10, 5000)):
        with patch('time.time_ns', return_value=clock), writer._rotate() as output:
          output.write(json.dumps({'index': index}) + '\n')
          saved_names.append(Path(output.name).name)
        surviving = sorted(writer.directory.glob('navigation-*.jsonl'))
        self.assertEqual([json.loads(path.read_text())['index'] for path in surviving],
                         list(range(max(0, index - 2), index + 1)))
      self.assertEqual(saved_names, sorted(set(saved_names)))
      # A newly constructed writer must also retain the persisted order.
      restarted = DiagnosticsWriter.__new__(DiagnosticsWriter)
      restarted.directory, restarted.files = writer.directory, 3
      with patch('time.time_ns', return_value=1), restarted._rotate() as output:
        output.write('{"index":6}\n')
      self.assertEqual([json.loads(path.read_text())['index'] for path in sorted(writer.directory.glob('navigation-*.jsonl'))], [4, 5, 6])

  def test_failed_new_file_does_not_prune_existing_capture(self):
    with tempfile.TemporaryDirectory() as directory:
      writer = DiagnosticsWriter.__new__(DiagnosticsWriter)
      writer.directory, writer.files = Path(directory), 1
      with writer._rotate() as output:
        output.write('kept')
      paths = list(writer.directory.iterdir())
      with patch.object(Path, 'open', side_effect=OSError('full')):
        with self.assertRaises(OSError):
          writer._rotate()
      self.assertEqual(list(writer.directory.iterdir()), paths)
      self.assertEqual(paths[0].read_text(), 'kept')

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
          self.assertGreater(json.loads(line)['capture_wall_time_ns'], 0)

  def test_diagnostics_never_include_key_or_packet(self):
    receiver = Receiver(b'\x01' * 16, b'\x02' * 32)
    record = receiver_record(receiver, 10, 'waiting', '')
    self.assertNotIn(receiver.key.hex(), json.dumps(record))
    self.assertNotIn('key', record)
    self.assertNotIn('packet', record)


if __name__ == '__main__':
  unittest.main()
