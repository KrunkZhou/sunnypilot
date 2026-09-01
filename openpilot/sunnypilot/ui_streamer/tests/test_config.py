import os
import tempfile
import unittest

from openpilot.sunnypilot.ui_streamer import RestartBackoff, UIStreamerConfig, build_stream_url


class TestRestartBackoff(unittest.TestCase):
  def test_immediate_failures_back_off_and_stable_run_resets_delay(self):
    backoff = RestartBackoff(initial_delay=1.0, maximum_delay=4.0, stable_after=10.0)
    self.assertTrue(backoff.ready(0.0))

    backoff.note_started(0.0)
    backoff.note_exit(0.1)
    self.assertFalse(backoff.ready(1.09))
    self.assertTrue(backoff.ready(1.1))

    backoff.note_started(1.1)
    backoff.note_exit(1.2)
    self.assertFalse(backoff.ready(3.19))
    self.assertTrue(backoff.ready(3.2))

    backoff.note_started(3.2)
    backoff.note_exit(13.3)
    self.assertFalse(backoff.ready(14.29))
    self.assertTrue(backoff.ready(14.3))


class TestUIStreamerConfig(unittest.TestCase):
  def setUp(self):
    self.temporary_directory = tempfile.TemporaryDirectory()
    self.enabled_path = os.path.join(self.temporary_directory.name, "config", "enabled")
    self.token_path = os.path.join(self.temporary_directory.name, "session.token")
    self.config = UIStreamerConfig(self.enabled_path, self.token_path)

  def tearDown(self):
    self.temporary_directory.cleanup()

  def test_default_disabled_and_invalid_state(self):
    self.assertFalse(self.config.enabled())
    os.makedirs(os.path.dirname(self.enabled_path))
    with open(self.enabled_path, "w") as file:
      file.write("true\n")
    self.assertFalse(self.config.enabled())

  def test_enable_persists_and_disable_removes_state(self):
    self.config.set_enabled(True)
    self.assertTrue(self.config.enabled())
    self.assertTrue(UIStreamerConfig(self.enabled_path, self.token_path).enabled())
    self.assertEqual(os.stat(self.enabled_path).st_mode & 0o777, 0o600)

    self.config.set_enabled(False)
    self.assertFalse(self.config.enabled())
    self.assertFalse(os.path.exists(self.enabled_path))

  def test_session_token_lifecycle(self):
    first = self.config.create_session_token()
    second = self.config.create_session_token()
    self.assertGreaterEqual(len(first), 32)
    self.assertNotEqual(first, second)
    self.assertEqual(self.config.session_token(), second)
    self.assertEqual(os.stat(self.token_path).st_mode & 0o777, 0o600)

    self.config.clear_session_token()
    self.assertEqual(self.config.session_token(), "")

  def test_url_keeps_token_out_of_http_request(self):
    url = build_stream_url("192.168.43.1", "secret-token")
    request, fragment = url.split("#", 1)
    self.assertEqual(request, "http://192.168.43.1:8082/")
    self.assertEqual(fragment, "token=secret-token")


if __name__ == "__main__":
  unittest.main()
