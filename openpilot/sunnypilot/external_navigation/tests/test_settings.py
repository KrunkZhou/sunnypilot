from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpilot.sunnypilot.external_navigation.settings import (
  read_mode, write_mode, read_network_revision, record_network_action, read_turn_speed_control, write_turn_speed_control,
)


class SettingsTests(unittest.TestCase):
  def test_turn_speed_preference_is_optional_private_and_independent(self):
    with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'RTZS_NAVIGATION_ROOT': directory}):
      root = Path(directory)
      self.assertFalse(read_turn_speed_control())
      self.assertTrue(write_turn_speed_control(True))
      self.assertTrue(read_turn_speed_control())
      self.assertEqual(read_mode(), 0)
      self.assertEqual((root / 'config/schema_version').read_bytes(), b'1\n')
      self.assertEqual((root / 'config/turn_speed_control').stat().st_mode & 0o777, 0o600)
      self.assertEqual((root / 'config/turn_speed_control').read_bytes(), b'1\n')
      self.assertTrue(write_mode(1))
      self.assertTrue(read_turn_speed_control())
      self.assertTrue(write_mode(0))
      self.assertTrue(read_turn_speed_control())  # Preference is retained while inactive.
      for invalid in (0, 1, -1, '1', 1.0, None):
        self.assertFalse(write_turn_speed_control(invalid))
        self.assertTrue(read_turn_speed_control())
      self.assertTrue(write_turn_speed_control(False))
      self.assertFalse(read_turn_speed_control())
      (root / 'config/turn_speed_control').unlink()
      self.assertFalse(read_turn_speed_control())  # Existing schema-1 installations need no migration.

  def test_turn_speed_preference_corruption_and_insecure_paths_fail_closed(self):
    with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'RTZS_NAVIGATION_ROOT': directory}):
      root = Path(directory)
      self.assertTrue(write_mode(1))
      path = root / 'config/turn_speed_control'
      for value in (b'', b'true', b'2\n', b'1\n\n', b'1' * 17):
        self.assertTrue(write_turn_speed_control(True))
        path.write_bytes(value)
        self.assertFalse(read_turn_speed_control())
        self.assertEqual(read_mode(), 1)
      self.assertTrue(write_turn_speed_control(True))
      path.chmod(0o644)
      self.assertFalse(read_turn_speed_control())
      path.unlink()
      path.symlink_to(root / 'config/mode')
      self.assertFalse(read_turn_speed_control())
      self.assertFalse(write_turn_speed_control(True))
      path.unlink()
      self.assertTrue(write_turn_speed_control(True))
      (root / 'config/schema_version').write_text('2\n')
      self.assertFalse(read_turn_speed_control())
      self.assertFalse(write_turn_speed_control(True))

  def test_per_file_mode_atomic_persistence_and_invalid_input(self):
    with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'RTZS_NAVIGATION_ROOT': directory}):
      root = Path(directory)
      self.assertEqual(read_mode(), 0)
      self.assertTrue(write_mode(1))
      self.assertEqual(read_mode(), 1)
      self.assertEqual((root / 'config/schema_version').read_bytes(), b'1\n')
      self.assertEqual((root / 'config/mode').read_bytes(), b'1\n')
      for relative in ('config/schema_version', 'config/mode', 'config.lock'):
        self.assertEqual((root / relative).stat().st_mode & 0o777, 0o600)
      self.assertEqual((root / 'config').stat().st_mode & 0o777, 0o700)
      for invalid in (2, -1, True, '1', 1.0):
        self.assertFalse(write_mode(invalid))
        self.assertEqual(read_mode(), 1)
      (root / 'config/mode').write_text('bad')
      self.assertEqual(read_mode(), 0)
      self.assertTrue(write_mode(0))
      self.assertEqual(read_mode(), 0)
      (root / 'config/schema_version').write_text('2\n')
      self.assertEqual(read_mode(), 0)
      self.assertFalse(write_mode(1))

  def test_corruption_permissions_and_symlinks_fail_closed(self):
    with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'RTZS_NAVIGATION_ROOT': directory}):
      root = Path(directory)
      self.assertTrue(write_mode(1))
      (root / 'config/mode').chmod(0o644)
      self.assertEqual(read_mode(), 0)
      (root / 'config/mode').unlink()
      (root / 'config/mode').symlink_to(root / 'config/schema_version')
      self.assertEqual(read_mode(), 0)
      self.assertFalse(write_mode(1))
      (root / 'config/mode').unlink()
      (root / 'config').chmod(0o755)
      self.assertEqual(read_mode(), 0)
      self.assertFalse(write_mode(1))

  def test_driving_model_reader_never_waits_for_writer_fsync(self):
    import fcntl
    import os
    with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'RTZS_NAVIGATION_ROOT': directory}):
      self.assertTrue(write_mode(1))
      self.assertTrue(write_turn_speed_control(True))
      fd = os.open(Path(directory) / 'config.lock', os.O_RDWR)
      try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(read_mode(), 0)
        self.assertFalse(read_turn_speed_control())
      finally:
        os.close(fd)
      self.assertEqual(read_mode(), 1)
      self.assertTrue(read_turn_speed_control())

  def test_network_revision_and_tracking_failure(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / 'revision'
      with patch('openpilot.sunnypilot.external_navigation.settings.revision_path', return_value=path):
        before = read_network_revision()
        self.assertIsNotNone(before)
        self.assertTrue(record_network_action())
        self.assertNotEqual(read_network_revision(), before)
        path.chmod(0o644)
        self.assertIsNone(read_network_revision())
        self.assertFalse(record_network_action())
        path.chmod(0o600)
        path.write_text('invalid')
        self.assertIsNone(read_network_revision())


if __name__ == '__main__':
  unittest.main()
