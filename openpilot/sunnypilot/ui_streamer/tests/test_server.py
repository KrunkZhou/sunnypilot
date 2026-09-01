import http.client
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

from openpilot.sunnypilot.ui_streamer import screenstreamd
from openpilot.sunnypilot.ui_streamer import FRAME_HEADER, FRAME_MAGIC, UIStreamerConfig
from openpilot.sunnypilot.ui_streamer.screenstreamd import (
  FrameHub,
  FrameReceiver,
  MAX_HTTP_CONNECTIONS,
  MasterTokenStore,
  MjpegEncoder,
  NetworkBinding,
  ScreenStreamHTTPServer,
  SessionStore,
  ffmpeg_executable,
  network_binding_changed,
  wait_for_listen_address,
)


def ffmpeg_available() -> bool:
  try:
    executable = ffmpeg_executable()
  except (AttributeError, ImportError):
    return False
  return subprocess.run([executable, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


class TestFrameHub(unittest.TestCase):
  def test_demand_and_client_limit(self):
    hub = FrameHub(max_stream_clients=1)
    demand = []
    hub.set_demand_callback(demand.append)

    self.assertTrue(hub.register_stream_client())
    self.assertTrue(hub.has_demand)
    self.assertFalse(hub.register_stream_client())
    hub.unregister_stream_client()
    self.assertFalse(hub.has_demand)

    with hub.snapshot_request() as accepted:
      self.assertTrue(accepted)
      self.assertTrue(hub.has_demand)
    self.assertFalse(hub.has_demand)
    self.assertEqual(demand, [True, False, True, False])

  def test_latest_frame_sequence(self):
    hub = FrameHub()
    hub.publish_frame(b"first")
    hub.publish_frame(b"latest")
    sequence, frame, closed = hub.wait_for_frame(0, 0.1)
    self.assertEqual(sequence, 2)
    self.assertEqual(frame, b"latest")
    self.assertFalse(closed)

    sequence, frame, closed = hub.wait_for_frame(sequence, 0.01)
    self.assertEqual(sequence, 2)
    self.assertIsNone(frame)
    self.assertFalse(closed)

  def test_snapshot_limit(self):
    hub = FrameHub(max_snapshot_waiters=1)
    with hub.snapshot_request() as first:
      self.assertTrue(first)
      with hub.snapshot_request() as second:
        self.assertFalse(second)


class TestFrameReceiver(unittest.TestCase):
  def test_versioned_frame_and_demand_protocol(self):
    class RecordingEncoder:
      def __init__(self):
        self.frame = None
        self.received = threading.Event()

      def submit(self, rgba: bytes, width: int, height: int) -> None:
        self.frame = (rgba, width, height)
        self.received.set()

    with tempfile.TemporaryDirectory() as directory:
      path = os.path.join(directory, "stream.sock")
      hub = FrameHub()
      encoder = RecordingEncoder()
      receiver = FrameReceiver(hub, encoder, path)
      receiver.start()
      client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
      try:
        client.settimeout(1.0)
        self.assertTrue(hub.register_stream_client())
        client.connect(path)
        self.assertEqual(client.recv(1), b"1")

        client.sendall(FRAME_HEADER.pack(FRAME_MAGIC, 0, 0, 0))
        rgba = bytes([255, 0, 0, 255] * 8)
        client.sendall(FRAME_HEADER.pack(FRAME_MAGIC, len(rgba), 4, 2) + rgba)
        self.assertTrue(encoder.received.wait(1.0))
        self.assertEqual(encoder.frame, (rgba, 4, 2))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

        client.close()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        client.connect(path)
        self.assertEqual(client.recv(1), b"1")

        hub.unregister_stream_client()
        self.assertEqual(client.recv(1), b"0")
      finally:
        client.close()
        receiver.close()
        hub.close()

  def test_health_allows_demand_startup_grace_then_detects_stall(self):
    hub = FrameHub()
    receiver = FrameReceiver(hub, mock.Mock())
    with mock.patch.object(receiver, "is_alive", return_value=True):
      with mock.patch.object(screenstreamd.time, "monotonic", return_value=100.0):
        self.assertTrue(hub.register_stream_client())
        self.assertTrue(receiver.healthy())

      with mock.patch.object(screenstreamd.time, "monotonic", return_value=100.0 + screenstreamd.RECEIVER_STALL_TIMEOUT + 0.1):
        self.assertFalse(receiver.healthy())
        receiver._record_frame(100.0 + screenstreamd.RECEIVER_STALL_TIMEOUT + 0.1)
        self.assertTrue(receiver.healthy())

    hub.unregister_stream_client()
    receiver.close()
    hub.close()


class TestNetworkBinding(unittest.TestCase):
  def test_waits_quietly_for_wlan_address(self):
    config = mock.Mock()
    config.enabled.return_value = True
    with (
      mock.patch.object(screenstreamd, "PC", False),
      mock.patch.object(screenstreamd, "interface_ipv4_address", side_effect=[OSError, "192.168.43.1"]) as address,
      mock.patch.object(screenstreamd.time, "sleep") as sleep,
    ):
      self.assertEqual(wait_for_listen_address(config), "192.168.43.1")
    self.assertEqual(address.call_count, 2)
    sleep.assert_called_once_with(1.0)

  def test_same_address_on_different_access_point_is_a_new_binding(self):
    original = NetworkBinding("192.168.1.8", "home|aa")
    self.assertTrue(network_binding_changed(original, NetworkBinding("192.168.1.8", "work|bb")))
    self.assertTrue(network_binding_changed(original, NetworkBinding("192.168.2.8", "home|aa")))
    self.assertTrue(network_binding_changed(original, NetworkBinding("192.168.1.8", None)))
    self.assertTrue(network_binding_changed(NetworkBinding("192.168.1.8", None), original))


class TestServiceLifecycle(unittest.TestCase):
  def test_running_service_stops_when_known_identity_becomes_unknown(self):
    config = mock.Mock()
    config.enabled.return_value = True
    binding = NetworkBinding("192.168.1.8", "home")
    tokens = mock.Mock()
    sessions = mock.Mock()
    hub = mock.Mock()
    encoder = mock.Mock()
    encoder.healthy.return_value = True
    receiver = mock.Mock()
    receiver.healthy.return_value = True
    server = mock.Mock()
    server_thread = mock.Mock()
    server_thread.is_alive.return_value = True
    submaster = mock.Mock()
    fake_cereal = types.ModuleType("openpilot.cereal")
    fake_cereal.messaging = types.SimpleNamespace(SubMaster=mock.Mock(return_value=submaster))

    with (
      mock.patch.dict(sys.modules, {"openpilot.cereal": fake_cereal}),
      mock.patch.object(screenstreamd, "PC", False),
      mock.patch.object(screenstreamd, "_set_background_affinity"),
      mock.patch.object(screenstreamd, "interface_ipv4_address", return_value=binding.address),
      mock.patch.object(screenstreamd, "wlan_network_identity", side_effect=[binding.identity, None]) as identity,
      mock.patch.object(screenstreamd, "FrameHub", return_value=hub),
      mock.patch.object(screenstreamd, "MjpegEncoder", return_value=encoder),
      mock.patch.object(screenstreamd, "FrameReceiver", return_value=receiver),
      mock.patch.object(screenstreamd, "ScreenStreamHTTPServer", return_value=server),
      mock.patch.object(screenstreamd.threading, "Thread", return_value=server_thread),
      mock.patch.object(screenstreamd, "build_telemetry", return_value={}),
      mock.patch.object(screenstreamd, "_cloudlog"),
      mock.patch.object(screenstreamd.time, "monotonic", side_effect=[0.0, 3.0]),
    ):
      screenstreamd._run_service(config, binding, tokens, sessions)

    self.assertEqual(identity.call_count, 2)
    server.close_active_connections.assert_called_once_with()
    server.shutdown.assert_called_once_with()
    receiver.close.assert_called_once_with()
    encoder.close.assert_called_once_with()

  def test_network_boundary_rotates_access_state(self):
    config = mock.Mock()
    config.enabled.side_effect = [True, True]
    bindings = [NetworkBinding("192.168.1.8", "home"), NetworkBinding("192.168.1.8", "work")]
    token_states = [mock.Mock(), mock.Mock()]
    session_states = [mock.Mock(), mock.Mock()]
    with (
      mock.patch.object(screenstreamd, "UIStreamerConfig", return_value=config),
      mock.patch.object(screenstreamd, "MasterTokenStore", side_effect=token_states) as token_factory,
      mock.patch.object(screenstreamd, "SessionStore", side_effect=session_states) as session_factory,
      mock.patch.object(screenstreamd, "resolve_network_binding", side_effect=bindings),
      mock.patch.object(screenstreamd, "_run_service", side_effect=[None, KeyboardInterrupt]) as run_service,
    ):
      screenstreamd.main()

    self.assertEqual(run_service.call_args_list, [
      mock.call(config, bindings[0], token_states[0], session_states[0]),
      mock.call(config, bindings[1], token_states[1], session_states[1]),
    ])
    self.assertEqual(token_factory.call_count, 2)
    self.assertEqual(session_factory.call_count, 2)
    config.clear_session_token.assert_called_once_with()

  def test_unknown_identity_does_not_rebind_old_access(self):
    config = mock.Mock()
    config.enabled.side_effect = [True, True, True]
    binding = NetworkBinding("192.168.1.8", "home")
    unknown = NetworkBinding(binding.address, None)
    token_state = mock.Mock()
    session_state = mock.Mock()
    with (
      mock.patch.object(screenstreamd, "UIStreamerConfig", return_value=config),
      mock.patch.object(screenstreamd, "MasterTokenStore", return_value=token_state),
      mock.patch.object(screenstreamd, "SessionStore", return_value=session_state),
      mock.patch.object(screenstreamd, "resolve_network_binding", side_effect=[binding, unknown, binding]),
      mock.patch.object(screenstreamd, "_run_service", side_effect=[None, KeyboardInterrupt]) as run_service,
      mock.patch.object(screenstreamd.time, "sleep") as sleep,
    ):
      screenstreamd.main()

    self.assertEqual(run_service.call_count, 2)
    sleep.assert_called_once_with(0.5)
    config.clear_session_token.assert_called_once_with()

  def test_access_rotation_failure_is_fail_closed(self):
    config = mock.Mock()
    config.enabled.side_effect = [True, True]
    bindings = [NetworkBinding("192.168.1.8", "home"), NetworkBinding("192.168.1.8", "work")]
    token_state = mock.Mock()
    session_state = mock.Mock()
    with (
      mock.patch.object(screenstreamd, "UIStreamerConfig", return_value=config),
      mock.patch.object(screenstreamd, "MasterTokenStore", side_effect=[token_state, OSError]),
      mock.patch.object(screenstreamd, "SessionStore", return_value=session_state),
      mock.patch.object(screenstreamd, "resolve_network_binding", side_effect=bindings),
      mock.patch.object(screenstreamd, "_run_service") as run_service,
      self.assertRaises(OSError),
    ):
      screenstreamd.main()

    run_service.assert_called_once_with(config, bindings[0], token_state, session_state)
    config.clear_session_token.assert_called_once_with()

  def test_worker_restart_preserves_access_state(self):
    config = mock.Mock()
    config.enabled.side_effect = [True, True]
    binding = NetworkBinding("192.168.1.8", "home")
    token_state = mock.Mock()
    session_state = mock.Mock()
    with (
      mock.patch.object(screenstreamd, "UIStreamerConfig", return_value=config),
      mock.patch.object(screenstreamd, "MasterTokenStore", return_value=token_state) as token_factory,
      mock.patch.object(screenstreamd, "SessionStore", return_value=session_state) as session_factory,
      mock.patch.object(screenstreamd, "resolve_network_binding", return_value=binding),
      mock.patch.object(screenstreamd, "_run_service", side_effect=[RuntimeError, KeyboardInterrupt]) as run_service,
      mock.patch.object(screenstreamd, "_cloudlog") as cloudlog,
      mock.patch.object(screenstreamd.time, "sleep"),
    ):
      screenstreamd.main()

    self.assertEqual(run_service.call_args_list, [
      mock.call(config, binding, token_state, session_state),
      mock.call(config, binding, token_state, session_state),
    ])
    token_factory.assert_called_once_with(config)
    session_factory.assert_called_once_with()
    cloudlog.return_value.exception.assert_called_once()
    config.clear_session_token.assert_called_once_with()


class TestScreenStreamHTTPServer(unittest.TestCase):
  def setUp(self):
    self.temporary_directory = tempfile.TemporaryDirectory()
    self.config = UIStreamerConfig(
      os.path.join(self.temporary_directory.name, "enabled"),
      os.path.join(self.temporary_directory.name, "master.token"),
    )
    self.tokens = MasterTokenStore(self.config)
    self.master_token = self.config.session_token()
    self.hub = FrameHub()
    self.hub.publish_telemetry({"state": "offroad"})
    self.server = ScreenStreamHTTPServer(("127.0.0.1", 0), self.hub, self.tokens)
    self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
    self.thread.start()
    self.base_url = f"http://127.0.0.1:{self.server.server_port}"

  def tearDown(self):
    self.hub.close()
    self.server.close_active_connections()
    self.server.shutdown()
    self.server.server_close()
    self.thread.join(timeout=1.0)
    self.temporary_directory.cleanup()

  def test_authentication_session_and_snapshot(self):
    with urllib.request.urlopen(f"{self.base_url}/", timeout=2.0) as response:
      self.assertEqual(response.status, 200)

    malformed_cookie = urllib.request.Request(f"{self.base_url}/telemetry", headers={"Cookie": "x=abc; $bad=1"})
    with self.assertRaises(urllib.error.HTTPError) as context:
      urllib.request.urlopen(malformed_cookie, timeout=2.0)
    self.assertEqual(context.exception.code, 401)

    with self.assertRaises(urllib.error.HTTPError) as context:
      urllib.request.urlopen(f"{self.base_url}/telemetry?token={self.master_token}", timeout=2.0)
    self.assertEqual(context.exception.code, 401)

    request = urllib.request.Request(
      f"{self.base_url}/session",
      data=b"",
      method="POST",
      headers={"Authorization": f"Bearer {self.master_token}"},
    )
    with urllib.request.urlopen(request, timeout=2.0) as response:
      cookie = response.headers["Set-Cookie"].split(";", 1)[0]
      self.assertEqual(response.status, 204)

    self.assertNotEqual(self.config.session_token(), self.master_token)
    replay = urllib.request.Request(
      f"{self.base_url}/session",
      data=b"",
      method="POST",
      headers={"Authorization": f"Bearer {self.master_token}"},
    )
    with self.assertRaises(urllib.error.HTTPError) as context:
      urllib.request.urlopen(replay, timeout=2.0)
    self.assertEqual(context.exception.code, 401)

    request = urllib.request.Request(f"{self.base_url}/telemetry", headers={"Cookie": cookie})
    with urllib.request.urlopen(request, timeout=2.0) as response:
      self.assertEqual(response.read(), b'{"state":"offroad"}')

    result = {}

    def get_snapshot():
      request = urllib.request.Request(f"{self.base_url}/snapshot.jpg", headers={"Cookie": cookie})
      with urllib.request.urlopen(request, timeout=3.0) as response:
        result["frame"] = response.read()

    snapshot_thread = threading.Thread(target=get_snapshot)
    snapshot_thread.start()
    deadline = time.monotonic() + 2.0
    while not self.hub.has_demand and time.monotonic() < deadline:
      time.sleep(0.01)
    self.assertTrue(self.hub.has_demand)
    self.hub.publish_frame(b"\xff\xd8frame\xff\xd9")
    snapshot_thread.join(timeout=3.0)
    self.assertEqual(result["frame"], b"\xff\xd8frame\xff\xd9")
    self.assertFalse(self.hub.has_demand)

    connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2.0)
    try:
      connection.request("GET", "/telemetry", headers={"Cookie": cookie})
      response = connection.getresponse()
      self.assertEqual(response.status, 200)
      response.read()

      self.server.close_active_connections()
      try:
        connection.request("GET", "/telemetry", headers={"Cookie": cookie})
        response = connection.getresponse()
      except (OSError, http.client.HTTPException):
        pass
      else:
        self.assertEqual(response.status, 503)
        response.read()
    finally:
      connection.close()

  def test_partial_headers_have_an_absolute_deadline(self):
    connections = []
    try:
      with mock.patch.object(screenstreamd, "HTTP_HEADER_DEADLINE", 0.25):
        for _ in range(MAX_HTTP_CONNECTIONS):
          connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=1.0)
          connection.sendall(b"G")
          connections.append(connection)

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
          time.sleep(0.05)
          for connection in connections:
            try:
              connection.sendall(b"E")
            except OSError:
              pass

      deadline = time.monotonic() + 1.0
      while time.monotonic() < deadline:
        with self.server._connections_lock:
          if not self.server._connections:
            break
        time.sleep(0.01)
      with self.server._connections_lock:
        self.assertFalse(self.server._connections)
      with urllib.request.urlopen(f"{self.base_url}/", timeout=2.0) as response:
        self.assertEqual(response.status, 200)
    finally:
      for connection in connections:
        connection.close()

  def test_malformed_request_target_is_rejected(self):
    with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2.0) as connection:
      connection.sendall(b"GET http://[x/ HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
      response = connection.recv(4096)
    self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

  def test_non_ascii_bearer_is_rejected(self):
    with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2.0) as connection:
      connection.sendall(
        b"POST /session HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer \xff\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
      )
      response = connection.recv(4096)
    self.assertIn(b" 401 ", response.split(b"\r\n", 1)[0])


class TestSessionStore(unittest.TestCase):
  def test_session_count_is_bounded(self):
    sessions = SessionStore(max_sessions=2)
    first = sessions.create()
    second = sessions.create()
    third = sessions.create()
    self.assertFalse(sessions.valid(first))
    self.assertTrue(sessions.valid(second))
    self.assertTrue(sessions.valid(third))


@unittest.skipUnless(ffmpeg_available(), "ffmpeg is not runnable")
class TestMjpegEncoder(unittest.TestCase):
  def test_encodes_rgba_to_latest_jpeg(self):
    hub = FrameHub()
    encoder = MjpegEncoder(hub)
    encoder.start()
    try:
      rgba = bytes([255, 0, 0, 255] * 8)
      sequence = 0
      for _ in range(3):
        encoder.submit(rgba, 4, 2)
        sequence, frame, closed = hub.wait_for_frame(sequence, 5.0)
        self.assertFalse(closed)
        self.assertIsNotNone(frame)
        self.assertTrue(frame.startswith(b"\xff\xd8"))
        self.assertTrue(frame.endswith(b"\xff\xd9"))
      self.assertGreaterEqual(sequence, 3)
    finally:
      encoder.close()
      hub.close()


class TestMjpegEncoderShutdown(unittest.TestCase):
  def test_close_interrupts_blocked_child(self):
    with tempfile.TemporaryDirectory() as directory:
      executable = os.path.join(directory, "blocked_encoder")
      with open(executable, "w") as file:
        file.write("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
      os.chmod(executable, 0o700)

      hub = FrameHub()
      encoder = MjpegEncoder(hub, executable=executable)
      encoder.start()
      encoder.submit(bytes(512 * 512 * 4), 512, 512)
      deadline = time.monotonic() + 1.0
      while encoder._process is None and time.monotonic() < deadline:
        time.sleep(0.01)

      started = time.monotonic()
      encoder.close()
      self.assertLess(time.monotonic() - started, 3.0)
      self.assertFalse(encoder.is_alive())
      hub.close()


if __name__ == "__main__":
  unittest.main()
