import hashlib
import hmac
import json
import math
import os
import queue
import secrets
import select
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from openpilot.sunnypilot.ui_streamer import (
  BACKGROUND_CORES,
  FRAME_HEADER,
  FRAME_MAGIC,
  IPC_IDLE_TIMEOUT,
  MAX_STREAM_CLIENTS,
  PC,
  STREAM_FPS,
  STREAM_MAX_WIDTH,
  STREAM_PORT,
  STREAM_SOCKET_PATH,
  UIStreamerConfig,
)


MAX_FRAME_BYTES = STREAM_MAX_WIDTH * STREAM_MAX_WIDTH * 4
FRAME_MAX_AGE = 0.5
FRAME_RECEIVE_TIMEOUT = 2.0
ENCODER_INPUT_ACTIVE_TIMEOUT = 2.0
ENCODER_OUTPUT_STALL_TIMEOUT = 3.0
SESSION_TTL = 3600.0
MASTER_TOKEN_TTL = 300.0
STREAM_IDLE_TIMEOUT = 15.0
RECEIVER_STALL_TIMEOUT = 10.0
HTTP_SOCKET_TIMEOUT = 5.0
HTTP_HEADER_DEADLINE = 5.0
MAX_HTTP_CONNECTIONS = MAX_STREAM_CLIENTS * 3 + 4
MAX_SNAPSHOT_WAITERS = 2
MAX_SESSIONS = 32
JPEG_BUFFER_LIMIT = 4 * 1024 * 1024
TELEMETRY_INTERVAL = 0.2


@dataclass(frozen=True)
class NetworkBinding:
  address: str
  identity: str | None


def _cloudlog():
  from openpilot.common.swaglog import cloudlog

  return cloudlog


def _set_background_affinity() -> None:
  if not PC:
    try:
      os.sched_setaffinity(0, BACKGROUND_CORES)
    except OSError:
      _cloudlog().exception("UI streamer could not set background CPU affinity")


def ffmpeg_executable() -> str:
  import ffmpeg

  return os.path.join(ffmpeg.BIN_DIR, "ffmpeg")


def interface_ipv4_address(interface: str = "wlan0") -> str:
  import fcntl
  import struct

  request = struct.pack("256s", interface[:15].encode())
  with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    response = fcntl.ioctl(sock.fileno(), 0x8915, request)  # SIOCGIFADDR
  return socket.inet_ntoa(response[20:24])


def wlan_network_identity(interface: str = "wlan0") -> str | None:
  """Return the associated network identity without shelling out or logging the SSID."""
  if PC:
    return None
  try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
      sock.settimeout(0.2)
      sock.bind(f"\0ui-stream-wpa-{os.getpid()}-{time.monotonic_ns()}")
      sock.connect(f"/run/wpa_supplicant/{interface}")
      sock.send(b"STATUS")
      while True:
        response = sock.recv(8192).decode("utf-8", "replace")
        if response.startswith("<"):
          continue
        if response.startswith("FAIL"):
          return None
        status = dict(line.split("=", 1) for line in response.splitlines() if "=" in line)
        identity_fields = [status.get(key, "") for key in ("id", "ssid", "bssid")]
        if not any(identity_fields):
          return None
        return hashlib.sha256("\0".join(identity_fields).encode()).hexdigest()
  except OSError:
    return None


def network_binding_changed(original: NetworkBinding, current: NetworkBinding) -> bool:
  return current.address != original.address or current.identity != original.identity


def wait_for_listen_address(config: UIStreamerConfig) -> str | None:
  if PC:
    return "0.0.0.0"
  while config.enabled():
    try:
      return interface_ipv4_address()
    except OSError:
      time.sleep(1.0)
  return None


def resolve_network_binding(config: UIStreamerConfig) -> NetworkBinding | None:
  address = wait_for_listen_address(config)
  return NetworkBinding(address, wlan_network_identity()) if address is not None else None


VIEWER_HTML = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>sunnypilot UI stream</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #080808; }
    main { width: min(100vw, 1200px); padding: 12px; }
    img { display: block; width: 100%; aspect-ratio: 2.233 / 1; object-fit: contain; background: #000; border-radius: 8px; }
    #telemetry { display: flex; flex-wrap: wrap; gap: 8px 18px; padding: 10px 2px 0; color: #ccc; font-variant-numeric: tabular-nums; }
    strong { color: #fff; }
    #state { margin-left: auto; }
  </style>
</head>
<body>
<main>
  <img id="stream" alt="sunnypilot UI stream">
  <div id="telemetry">
    <span><strong id="speed">--</strong> km/h</span>
    <span>lead <strong id="lead">--</strong> m</span>
    <span>CPU <strong id="cpu">--</strong>%</span>
    <span id="state">connecting</span>
  </div>
</main>
<script>
  let authorized = true;
  async function authenticate() {
    const token = new URLSearchParams(location.hash.slice(1)).get('token');
    if (token) {
      const response = await fetch('/session', {method: 'POST', headers: {Authorization: `Bearer ${token}`}});
      history.replaceState(null, '', location.pathname);
      if (!response.ok) throw new Error('authentication failed');
    }
  }
  async function refresh() {
    try {
      const response = await fetch('/telemetry', {cache: 'no-store'});
      if (response.status === 401) {
        authorized = false;
        document.querySelector('#stream').removeAttribute('src');
        document.querySelector('#state').textContent = 'rescan required';
        return;
      }
      if (!response.ok) throw new Error(response.status);
      const telemetry = await response.json();
      document.querySelector('#speed').textContent = Math.round((telemetry.vehicle?.speedMps || 0) * 3.6);
      document.querySelector('#lead').textContent = telemetry.lead?.present ? telemetry.lead.distanceM.toFixed(1) : '--';
      document.querySelector('#cpu').textContent = telemetry.device?.cpuUsagePercent?.toFixed(0) ?? '--';
      document.querySelector('#state').textContent = telemetry.state || 'unknown';
    } catch (_) {
      document.querySelector('#state').textContent = 'disconnected';
    }
  }
  function connectStream() {
    if (!authorized) return;
    const stream = document.querySelector('#stream');
    stream.onerror = () => {
      stream.removeAttribute('src');
      setTimeout(connectStream, 1000);
    };
    stream.src = `/stream?v=${Date.now()}`;
  }
  authenticate().then(() => {
    connectStream();
    refresh();
    setInterval(refresh, 1000);
  }).catch(() => { document.querySelector('#state').textContent = 'unauthorized'; });
</script>
</body>
</html>
"""


def _finite(value: float) -> float | None:
  value = float(value)
  return value if math.isfinite(value) else None


def build_telemetry(sm: Any) -> dict[str, Any]:
  car_state = sm["carState"]
  selfdrive_state = sm["selfdriveState"]
  selfdrive_state_sp = sm["selfdriveStateSP"]
  lead = sm["radarState"].leadOne
  device_state = sm["deviceState"]
  cpu_usage = list(device_state.cpuUsagePercent)
  cpu_temp = list(device_state.cpuTempC)
  started = bool(device_state.started)
  engaged = started and bool(selfdrive_state.enabled or selfdrive_state_sp.mads.enabled)
  state = "engaged" if engaged else "started" if started else "offroad"

  lead_present = bool(getattr(lead, "present", getattr(lead, "status", False)))
  return {
    "version": 1,
    "monotonicSeconds": time.monotonic(),
    "started": started,
    "engaged": engaged,
    "state": state,
    "vehicle": {
      "valid": bool(sm.alive["carState"] and sm.valid["carState"]),
      "speedMps": _finite(car_state.vEgo),
      "accelerationMps2": _finite(car_state.aEgo),
      "steeringAngleDeg": _finite(car_state.steeringAngleDeg),
      "gasPressed": bool(car_state.gasPressed),
      "brakePressed": bool(car_state.brakePressed),
      "standstill": bool(car_state.standstill),
      "cruiseEnabled": bool(car_state.cruiseState.enabled),
      "cruiseSpeedMps": _finite(car_state.cruiseState.speed),
    },
    "selfdrive": {
      "valid": bool(sm.alive["selfdriveState"] and sm.valid["selfdriveState"]),
      "active": bool(selfdrive_state.active),
      "engageable": bool(selfdrive_state.engageable),
    },
    "lead": {
      "valid": bool(sm.alive["radarState"] and sm.valid["radarState"]),
      "present": lead_present,
      "distanceM": _finite(lead.dRel),
      "relativeSpeedMps": _finite(lead.vRel),
    },
    "device": {
      "valid": bool(sm.alive["deviceState"] and sm.valid["deviceState"]),
      "cpuUsagePercent": sum(cpu_usage) / len(cpu_usage) if cpu_usage else None,
      "maxCpuTempC": max(cpu_temp) if cpu_temp else None,
      "memoryUsagePercent": int(device_state.memoryUsagePercent),
      "freeSpacePercent": _finite(device_state.freeSpacePercent),
    },
  }


class FrameHub:
  def __init__(self, max_stream_clients: int = MAX_STREAM_CLIENTS, max_snapshot_waiters: int = MAX_SNAPSHOT_WAITERS):
    self._condition = threading.Condition()
    self._demand_lock = threading.Lock()
    self._frame: bytes | None = None
    self._sequence = 0
    self._stream_clients = 0
    self._snapshot_waiters = 0
    self._max_stream_clients = max_stream_clients
    self._max_snapshot_waiters = max_snapshot_waiters
    self._telemetry = b"{}"
    self._closed = False
    self._demand_callback = None

  def set_demand_callback(self, callback) -> None:
    with self._demand_lock:
      with self._condition:
        self._demand_callback = callback

  @property
  def has_demand(self) -> bool:
    with self._condition:
      return not self._closed and (self._stream_clients > 0 or self._snapshot_waiters > 0)

  def _notify_demand(self, active: bool) -> None:
    callback = self._demand_callback
    if callback is not None:
      callback(active)

  def publish_frame(self, frame: bytes) -> None:
    with self._condition:
      if self._closed:
        return
      self._frame = frame
      self._sequence += 1
      self._condition.notify_all()

  def publish_telemetry(self, telemetry: dict[str, Any]) -> None:
    try:
      payload = json.dumps(telemetry, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError):
      _cloudlog().exception("UI streamer telemetry serialization failed")
      return
    with self._condition:
      self._telemetry = payload

  def telemetry(self) -> bytes:
    with self._condition:
      return self._telemetry

  def register_stream_client(self) -> bool:
    with self._demand_lock:
      with self._condition:
        if self._closed or self._stream_clients >= self._max_stream_clients:
          return False
        self._stream_clients += 1
        active = self._stream_clients > 0 or self._snapshot_waiters > 0
      self._notify_demand(active)
    return True

  def unregister_stream_client(self) -> None:
    with self._demand_lock:
      with self._condition:
        self._stream_clients = max(0, self._stream_clients - 1)
        active = not self._closed and (self._stream_clients > 0 or self._snapshot_waiters > 0)
      self._notify_demand(active)

  @contextmanager
  def snapshot_request(self):
    with self._demand_lock:
      with self._condition:
        accepted = not self._closed and self._snapshot_waiters < self._max_snapshot_waiters
        if accepted:
          self._snapshot_waiters += 1
        active = not self._closed and (self._stream_clients > 0 or self._snapshot_waiters > 0)
      if accepted:
        self._notify_demand(active)
    if not accepted:
      yield False
      return
    try:
      yield True
    finally:
      with self._demand_lock:
        with self._condition:
          self._snapshot_waiters = max(0, self._snapshot_waiters - 1)
          active = not self._closed and (self._stream_clients > 0 or self._snapshot_waiters > 0)
        self._notify_demand(active)

  def latest_sequence(self) -> int:
    with self._condition:
      return self._sequence

  def wait_for_frame(self, after_sequence: int, timeout: float) -> tuple[int, bytes | None, bool]:
    deadline = time.monotonic() + timeout
    with self._condition:
      while not self._closed and self._sequence <= after_sequence:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          break
        self._condition.wait(remaining)
      frame = self._frame if self._sequence > after_sequence and not self._closed else None
      return self._sequence, frame, self._closed

  def close(self) -> None:
    with self._demand_lock:
      with self._condition:
        self._closed = True
        self._condition.notify_all()
      self._notify_demand(False)


@dataclass(frozen=True)
class RawFrame:
  rgba: bytes
  width: int
  height: int
  created_at: float


class MjpegEncoder:
  def __init__(self, hub: FrameHub, executable: str | None = None):
    self._hub = hub
    self._executable = executable or ffmpeg_executable()
    if not os.path.isfile(self._executable) or not os.access(self._executable, os.X_OK):
      raise FileNotFoundError(f"FFmpeg executable is unavailable: {self._executable}")
    self._queue: queue.Queue[RawFrame | None] = queue.Queue(maxsize=1)
    self._stop_event = threading.Event()
    self._process: subprocess.Popen | None = None
    self._reader_thread: threading.Thread | None = None
    self._process_lock = threading.Lock()
    self._health_lock = threading.Lock()
    self._last_submit_at = 0.0
    self._pending_output_since: float | None = None
    self._thread = threading.Thread(target=self._run, name="ui_stream_encoder", daemon=True)

  def start(self) -> None:
    self._thread.start()

  def is_alive(self) -> bool:
    return self._thread.is_alive()

  def healthy(self) -> bool:
    if not self.is_alive():
      return False
    now = time.monotonic()
    with self._health_lock:
      receiving_input = now - self._last_submit_at <= ENCODER_INPUT_ACTIVE_TIMEOUT
      return not receiving_input or self._pending_output_since is None or now - self._pending_output_since < ENCODER_OUTPUT_STALL_TIMEOUT

  def submit(self, rgba: bytes, width: int, height: int) -> None:
    now = time.monotonic()
    frame = RawFrame(rgba, width, height, now)
    with self._health_lock:
      if now - self._last_submit_at > ENCODER_INPUT_ACTIVE_TIMEOUT:
        self._pending_output_since = now
      elif self._pending_output_since is None:
        self._pending_output_since = now
      self._last_submit_at = now
    try:
      self._queue.put_nowait(frame)
    except queue.Full:
      try:
        self._queue.get_nowait()
      except queue.Empty:
        pass
      try:
        self._queue.put_nowait(frame)
      except queue.Full:
        pass

  def _start_process(self, width: int, height: int) -> subprocess.Popen:
    command = [
      self._executable,
      "-hide_banner",
      "-loglevel",
      "error",
      "-nostdin",
      "-f",
      "rawvideo",
      "-pixel_format",
      "rgba",
      "-video_size",
      f"{width}x{height}",
      "-framerate",
      str(STREAM_FPS),
      "-i",
      "pipe:0",
      "-vf",
      "vflip",
      "-an",
      "-c:v",
      "mjpeg",
      "-q:v",
      "10",
      "-threads",
      "1",
      # comma-deps-ffmpeg intentionally omits image2pipe; update=1 keeps image2 writing each JPEG to this pipe.
      "-f",
      "image2",
      "-update",
      "1",
      "pipe:1",
    ]
    with self._process_lock:
      if self._stop_event.is_set():
        raise InterruptedError
      process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
      reader_thread = threading.Thread(target=self._read_jpegs, args=(process,), name="ui_stream_jpeg_reader", daemon=True)
      self._process = process
      self._reader_thread = reader_thread
      reader_thread.start()
      return process

  def _read_jpegs(self, process: subprocess.Popen) -> None:
    assert process.stdout is not None
    buffer = bytearray()
    while not self._stop_event.is_set():
      chunk = process.stdout.read(64 * 1024)
      if not chunk:
        return
      buffer.extend(chunk)
      while True:
        start = buffer.find(b"\xff\xd8")
        if start < 0:
          trailing_marker = buffer[-1:] == b"\xff"
          buffer.clear()
          if trailing_marker:
            buffer.extend(b"\xff")
          break
        end = buffer.find(b"\xff\xd9", start + 2)
        if end < 0:
          if start > 0:
            del buffer[:start]
          break
        self._hub.publish_frame(bytes(buffer[start : end + 2]))
        with self._health_lock:
          self._pending_output_since = None
        del buffer[: end + 2]
      if len(buffer) > JPEG_BUFFER_LIMIT:
        buffer.clear()

  def _stop_process(self) -> None:
    with self._process_lock:
      process, self._process = self._process, None
      reader_thread, self._reader_thread = self._reader_thread, None
    if process is None:
      return
    if process.stdin is not None:
      try:
        process.stdin.close()
      except OSError:
        pass
    if process.poll() is None:
      process.terminate()
      try:
        process.wait(timeout=1.0)
      except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    if reader_thread is not None and reader_thread.is_alive():
      reader_thread.join(timeout=1.0)
    if process.stdout is not None:
      process.stdout.close()

  def _run(self) -> None:
    _set_background_affinity()
    dimensions = (0, 0)
    while not self._stop_event.is_set():
      try:
        frame = self._queue.get(timeout=0.5)
      except queue.Empty:
        continue
      if frame is None:
        break
      if time.monotonic() - frame.created_at > FRAME_MAX_AGE:
        continue
      try:
        if self._process is None or self._process.poll() is not None or dimensions != (frame.width, frame.height):
          self._stop_process()
          self._start_process(frame.width, frame.height)
          dimensions = (frame.width, frame.height)
        process = self._process
        assert process is not None and process.stdin is not None
        remaining = memoryview(frame.rgba)
        while remaining and not self._stop_event.is_set():
          written = process.stdin.write(remaining)
          if written is None or written <= 0:
            raise BrokenPipeError
          remaining = remaining[written:]
      except (BrokenPipeError, OSError):
        self._stop_process()
        if not self._stop_event.is_set():
          _cloudlog().exception("UI stream encoder failed")
          time.sleep(0.5)
    self._stop_process()

  def close(self) -> None:
    self._stop_event.set()
    try:
      self._queue.put_nowait(None)
    except queue.Full:
      try:
        self._queue.get_nowait()
      except queue.Empty:
        pass
      self._queue.put_nowait(None)
    self._stop_process()
    if self._thread.is_alive():
      self._thread.join(timeout=2.0)
    if self._thread.is_alive():
      _cloudlog().warning("UI stream encoder did not stop cleanly")
    self._stop_process()


def _recv_exact(sock: socket.socket, size: int, timeout: float | None = None) -> bytes | None:
  data = bytearray()
  deadline = time.monotonic() + timeout if timeout is not None else None
  while len(data) < size:
    if deadline is not None:
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        raise TimeoutError
      sock.settimeout(remaining)
    chunk = sock.recv(size - len(data))
    if not chunk:
      return None
    data.extend(chunk)
  return bytes(data)


class FrameReceiver:
  def __init__(self, hub: FrameHub, encoder: MjpegEncoder, path: str = STREAM_SOCKET_PATH):
    self._hub = hub
    self._encoder = encoder
    self._path = path
    self._stop_event = threading.Event()
    self._connection_lock = threading.Lock()
    self._connection: socket.socket | None = None
    self._server: socket.socket | None = None
    self._thread = threading.Thread(target=self._run, name="ui_stream_receiver", daemon=True)
    self._demand_lock = threading.Lock()
    self._demand_state = False
    self._demand_generation = 0
    self._demand_started_at = 0.0
    self._last_frame_at = 0.0
    self._hub.set_demand_callback(self._set_demand)

  def start(self) -> None:
    try:
      os.unlink(self._path)
    except FileNotFoundError:
      pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
      server.bind(self._path)
      os.chmod(self._path, 0o600)
      server.listen(1)
      server.settimeout(0.5)
    except Exception:
      server.close()
      try:
        os.unlink(self._path)
      except FileNotFoundError:
        pass
      raise
    self._server = server
    self._thread.start()

  def is_alive(self) -> bool:
    return self._thread.is_alive()

  def healthy(self) -> bool:
    with self._demand_lock:
      active = self._demand_state
      last_activity_at = max(self._demand_started_at, self._last_frame_at)
    return self.is_alive() and (not active or time.monotonic() - last_activity_at < RECEIVER_STALL_TIMEOUT)

  def _set_demand(self, active: bool) -> None:
    with self._demand_lock:
      if active != self._demand_state:
        self._demand_state = active
        self._demand_generation += 1
        if active:
          self._demand_started_at = time.monotonic()

  def _record_frame(self, received_at: float) -> None:
    with self._demand_lock:
      self._last_frame_at = received_at

  def _send_demand_if_changed(self, connection: socket.socket, sent_generation: int) -> int:
    with self._demand_lock:
      active = self._demand_state
      generation = self._demand_generation
    if generation != sent_generation:
      connection.sendall(b"1" if active else b"0")
    return generation

  def _read_connection(self, connection: socket.socket) -> None:
    sent_generation = -1
    last_message_at = time.monotonic()
    while not self._stop_event.is_set():
      sent_generation = self._send_demand_if_changed(connection, sent_generation)
      readable, _, _ = select.select([connection], [], [], 0.25)
      if not readable:
        if time.monotonic() - last_message_at >= IPC_IDLE_TIMEOUT:
          return
        continue
      header = _recv_exact(connection, FRAME_HEADER.size, FRAME_RECEIVE_TIMEOUT)
      if header is None:
        return
      magic, payload_size, width, height = FRAME_HEADER.unpack(header)
      last_message_at = time.monotonic()
      if magic == FRAME_MAGIC and payload_size == 0 and width == 0 and height == 0:
        continue
      if (
        magic != FRAME_MAGIC
        or width <= 0
        or height <= 0
        or width > STREAM_MAX_WIDTH
        or height > STREAM_MAX_WIDTH
        or payload_size != width * height * 4
        or payload_size > MAX_FRAME_BYTES
      ):
        raise ValueError("invalid UI stream frame")
      payload = _recv_exact(connection, payload_size, FRAME_RECEIVE_TIMEOUT)
      if payload is None:
        return
      last_message_at = time.monotonic()
      self._record_frame(last_message_at)
      self._encoder.submit(payload, width, height)

  def _run(self) -> None:
    _set_background_affinity()
    server = self._server
    assert server is not None
    while not self._stop_event.is_set():
      try:
        connection, _ = server.accept()
      except TimeoutError:
        continue
      except OSError:
        if self._stop_event.is_set():
          return
        raise
      with self._connection_lock:
        self._connection = connection
      try:
        connection.settimeout(FRAME_RECEIVE_TIMEOUT)
        self._read_connection(connection)
      except (OSError, ValueError):
        pass
      finally:
        with self._connection_lock:
          self._connection = None
        connection.close()

  def close(self) -> None:
    self._stop_event.set()
    with self._connection_lock:
      if self._connection is not None:
        try:
          self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
          pass
    if self._server is not None:
      self._server.close()
    if self._thread.is_alive():
      self._thread.join(timeout=1.0)
    try:
      os.unlink(self._path)
    except FileNotFoundError:
      pass


class SessionStore:
  def __init__(self, max_sessions: int = MAX_SESSIONS):
    self._lock = threading.Lock()
    self._sessions: dict[bytes, float] = {}
    self._max_sessions = max_sessions

  @staticmethod
  def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()

  def create(self) -> str:
    token = secrets.token_urlsafe(24)
    with self._lock:
      now = time.monotonic()
      self._sessions = {digest: expiry for digest, expiry in self._sessions.items() if expiry > now}
      if len(self._sessions) >= self._max_sessions:
        del self._sessions[min(self._sessions, key=self._sessions.get)]
      self._sessions[self._digest(token)] = now + SESSION_TTL
    return token

  def valid(self, token: str) -> bool:
    if not token:
      return False
    with self._lock:
      return self._sessions.get(self._digest(token), 0.0) > time.monotonic()


class MasterTokenStore:
  def __init__(self, config: UIStreamerConfig, ttl: float = MASTER_TOKEN_TTL):
    self._config = config
    self._ttl = ttl
    self._lock = threading.Lock()
    self._token = ""
    self._expires_at = 0.0
    with self._lock:
      self._rotate_locked(time.monotonic())

  def _rotate_locked(self, now: float) -> None:
    self._token = self._config.create_session_token()
    self._expires_at = now + self._ttl

  def rotate_if_expired(self) -> None:
    with self._lock:
      now = time.monotonic()
      if now >= self._expires_at:
        self._rotate_locked(now)

  def consume(self, token: str) -> bool:
    if not token:
      return False
    try:
      candidate = token.encode("ascii")
    except UnicodeEncodeError:
      return False
    with self._lock:
      now = time.monotonic()
      if now >= self._expires_at:
        self._rotate_locked(now)
        return False
      if not hmac.compare_digest(candidate, self._token.encode("ascii")):
        return False
      self._rotate_locked(now)
      return True


class ScreenStreamHTTPServer(ThreadingHTTPServer):
  daemon_threads = True
  allow_reuse_address = True

  def __init__(self, address: tuple[str, int], hub: FrameHub, tokens: MasterTokenStore, health_callback: Callable[[], bool] = lambda: True,
               sessions: SessionStore | None = None):
    self.hub = hub
    self.tokens = tokens
    self.sessions = sessions or SessionStore()
    self.health_callback = health_callback
    self._connection_slots = threading.BoundedSemaphore(MAX_HTTP_CONNECTIONS)
    self._connections_lock = threading.Lock()
    self._connections: set[socket.socket] = set()
    self._header_deadlines: dict[socket.socket, float] = {}
    self.stopping = threading.Event()
    super().__init__(address, ScreenStreamRequestHandler, bind_and_activate=False)
    self.server_bind()
    self.server_activate()

  def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
    if not self._connection_slots.acquire(blocking=False):
      self.shutdown_request(request)
      return
    with self._connections_lock:
      self._connections.add(request)
    try:
      super().process_request(request, client_address)
    except Exception:
      with self._connections_lock:
        self._connections.discard(request)
        self._header_deadlines.pop(request, None)
      self._connection_slots.release()
      raise

  def process_request_thread(self, request: socket.socket, client_address: tuple[str, int]) -> None:
    try:
      super().process_request_thread(request, client_address)
    finally:
      with self._connections_lock:
        self._connections.discard(request)
        self._header_deadlines.pop(request, None)
      self._connection_slots.release()

  def begin_header(self, connection: socket.socket) -> None:
    with self._connections_lock:
      if connection in self._connections:
        self._header_deadlines[connection] = time.monotonic() + HTTP_HEADER_DEADLINE

  def finish_header(self, connection: socket.socket) -> None:
    with self._connections_lock:
      self._header_deadlines.pop(connection, None)

  def service_actions(self) -> None:
    now = time.monotonic()
    with self._connections_lock:
      expired = [connection for connection, deadline in self._header_deadlines.items() if now >= deadline]
      for connection in expired:
        self._header_deadlines.pop(connection, None)
    for connection in expired:
      try:
        connection.shutdown(socket.SHUT_RDWR)
      except OSError:
        pass

  def close_active_connections(self) -> None:
    self.stopping.set()
    with self._connections_lock:
      connections = list(self._connections)
    for connection in connections:
      try:
        connection.shutdown(socket.SHUT_RDWR)
      except OSError:
        pass


class ScreenStreamRequestHandler(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"

  @property
  def stream_server(self) -> ScreenStreamHTTPServer:
    assert isinstance(self.server, ScreenStreamHTTPServer)
    return self.server

  def setup(self) -> None:
    super().setup()
    self.connection.settimeout(HTTP_SOCKET_TIMEOUT)

  def handle_one_request(self) -> None:
    self.stream_server.begin_header(self.connection)
    try:
      super().handle_one_request()
    except OSError:
      self.close_connection = True
    finally:
      self.stream_server.finish_header(self.connection)

  def parse_request(self) -> bool:
    try:
      return super().parse_request()
    finally:
      self.stream_server.finish_header(self.connection)

  def log_message(self, _format: str, *args: object) -> None:
    pass

  def _bearer_token(self) -> str:
    authorization = self.headers.get("Authorization", "")
    return authorization[7:] if authorization.startswith("Bearer ") else ""

  def _session_token(self) -> str:
    try:
      cookie = SimpleCookie(self.headers.get("Cookie", ""))
    except (CookieError, ValueError):
      return ""
    return cookie["ui_stream_session"].value if "ui_stream_session" in cookie else ""

  def _request_path(self) -> str | None:
    try:
      return urlsplit(self.path).path
    except ValueError:
      return None

  def _authorized(self) -> bool:
    return self.stream_server.sessions.valid(self._session_token())

  def _headers(self, status: HTTPStatus, content_type: str, length: int | None = None, cookie: str | None = None) -> None:
    self.send_response(status)
    self.send_header("Content-Type", content_type)
    self.send_header("Cache-Control", "no-store")
    self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
    self.send_header("Referrer-Policy", "no-referrer")
    self.send_header("X-Content-Type-Options", "nosniff")
    if self.close_connection:
      self.send_header("Connection", "close")
    if cookie is not None:
      self.send_header("Set-Cookie", cookie)
    if length is not None:
      self.send_header("Content-Length", str(length))
    self.end_headers()

  def _error(self, status: HTTPStatus) -> None:
    payload = f"{status.value} {status.phrase}\n".encode()
    self._headers(status, "text/plain; charset=utf-8", len(payload))
    self.wfile.write(payload)

  def do_POST(self) -> None:
    if self.stream_server.stopping.is_set():
      self.close_connection = True
      self._error(HTTPStatus.SERVICE_UNAVAILABLE)
      return
    path = self._request_path()
    if path is None:
      self.close_connection = True
      self._error(HTTPStatus.BAD_REQUEST)
      return
    if path != "/session" or not self.stream_server.tokens.consume(self._bearer_token()):
      self._error(HTTPStatus.UNAUTHORIZED)
      return
    session = self.stream_server.sessions.create()
    cookie = f"ui_stream_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age={int(SESSION_TTL)}"
    self._headers(HTTPStatus.NO_CONTENT, "text/plain", 0, cookie)

  def do_GET(self) -> None:
    if self.stream_server.stopping.is_set():
      self.close_connection = True
      self._error(HTTPStatus.SERVICE_UNAVAILABLE)
      return
    path = self._request_path()
    if path is None:
      self.close_connection = True
      self._error(HTTPStatus.BAD_REQUEST)
      return
    if path == "/":
      self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(VIEWER_HTML))
      self.wfile.write(VIEWER_HTML)
      return
    if not self._authorized():
      self._error(HTTPStatus.UNAUTHORIZED)
      return

    if path in ("/stream", "/stream.mjpg"):
      self._stream()
    elif path in ("/snapshot", "/snapshot.jpg"):
      self._snapshot()
    elif path == "/telemetry":
      payload = self.stream_server.hub.telemetry()
      self._headers(HTTPStatus.OK, "application/json", len(payload))
      self.wfile.write(payload)
    elif path == "/health":
      healthy = self.stream_server.health_callback()
      payload = b'{"status":"ok"}' if healthy else b'{"status":"degraded"}'
      self._headers(HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE, "application/json", len(payload))
      self.wfile.write(payload)
    else:
      self._error(HTTPStatus.NOT_FOUND)

  def _stream(self) -> None:
    hub = self.stream_server.hub
    session = self._session_token()
    if not hub.register_stream_client():
      self._error(HTTPStatus.SERVICE_UNAVAILABLE)
      return
    try:
      self.close_connection = True
      self._headers(HTTPStatus.OK, "multipart/x-mixed-replace; boundary=frame")
      sequence = hub.latest_sequence()
      last_frame_at = time.monotonic()
      while True:
        if not self.stream_server.sessions.valid(session):
          return
        sequence, frame, closed = hub.wait_for_frame(sequence, timeout=5.0)
        if closed:
          return
        if frame is None:
          if time.monotonic() - last_frame_at >= STREAM_IDLE_TIMEOUT:
            return
          continue
        last_frame_at = time.monotonic()
        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
      pass
    finally:
      hub.unregister_stream_client()

  def _snapshot(self) -> None:
    hub = self.stream_server.hub
    with hub.snapshot_request() as accepted:
      if not accepted:
        self._error(HTTPStatus.SERVICE_UNAVAILABLE)
        return
      sequence = hub.latest_sequence()
      _, frame, _ = hub.wait_for_frame(sequence, timeout=2.0)
    if frame is None:
      self._error(HTTPStatus.SERVICE_UNAVAILABLE)
      return
    self._headers(HTTPStatus.OK, "image/jpeg", len(frame))
    self.wfile.write(frame)


def _run_service(config: UIStreamerConfig, binding: NetworkBinding, tokens: MasterTokenStore, sessions: SessionStore) -> None:
  try:
    from openpilot.cereal import messaging
  except ModuleNotFoundError as error:
    if error.name != "openpilot.cereal":
      raise
    from cereal import messaging

  _set_background_affinity()
  listen_address = binding.address
  network_identity = binding.identity
  if not PC:
    try:
      current_binding = NetworkBinding(interface_ipv4_address(), wlan_network_identity())
    except OSError:
      return
    if network_binding_changed(binding, current_binding):
      return
    if current_binding.identity is not None:
      network_identity = current_binding.identity
  hub = FrameHub()
  encoder: MjpegEncoder | None = None
  receiver: FrameReceiver | None = None
  server: ScreenStreamHTTPServer | None = None
  server_thread: threading.Thread | None = None
  try:
    encoder = MjpegEncoder(hub)
    receiver = FrameReceiver(hub, encoder)
    server = ScreenStreamHTTPServer(
      (listen_address, STREAM_PORT),
      hub,
      tokens,
      health_callback=lambda: encoder.healthy() and receiver.healthy(),
      sessions=sessions,
    )
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="ui_stream_http", daemon=True)
    sm = messaging.SubMaster(["carState", "selfdriveState", "selfdriveStateSP", "radarState", "deviceState"], poll="deviceState")

    encoder.start()
    receiver.start()
    server_thread.start()
    _cloudlog().info(f"UI streamer started on WLAN port {STREAM_PORT}")
    next_network_check = time.monotonic() + 2.0
    while config.enabled():
      sm.update(round(TELEMETRY_INTERVAL * 1000))
      hub.publish_telemetry(build_telemetry(sm))
      tokens.rotate_if_expired()
      if not encoder.healthy() or not receiver.healthy() or not server_thread.is_alive():
        raise RuntimeError("UI streamer worker stopped unexpectedly")
      if not PC and time.monotonic() >= next_network_check:
        try:
          current_address = interface_ipv4_address()
        except OSError:
          return
        current_identity = wlan_network_identity()
        current_binding = NetworkBinding(current_address, current_identity)
        active_binding = NetworkBinding(listen_address, network_identity)
        if network_binding_changed(active_binding, current_binding):
          return
        if current_identity is not None:
          network_identity = current_identity
        next_network_check = time.monotonic() + 2.0
  finally:
    hub.close()
    if server is not None:
      server.close_active_connections()
      if server_thread is not None and server_thread.is_alive():
        server.shutdown()
      server.server_close()
    if receiver is not None:
      receiver.close()
    if encoder is not None:
      encoder.close()
    if server_thread is not None and server_thread.is_alive():
      server_thread.join(timeout=1.0)


def main() -> None:
  config = UIStreamerConfig()
  retry_delay = 1.0
  access_binding: NetworkBinding | None = None
  tokens: MasterTokenStore | None = None
  sessions: SessionStore | None = None
  try:
    while config.enabled():
      binding = resolve_network_binding(config)
      if binding is None:
        break
      if access_binding is not None and access_binding.identity is not None and binding.identity is None:
        # Never expose an old network's sessions while the current association cannot be verified.
        time.sleep(0.5)
        continue
      if access_binding is None or network_binding_changed(access_binding, binding):
        tokens = MasterTokenStore(config)
        sessions = SessionStore()
      access_binding = binding
      assert tokens is not None and sessions is not None

      service_started_at = time.monotonic()
      try:
        _run_service(config, binding, tokens, sessions)
        retry_delay = 1.0
      except KeyboardInterrupt:
        break
      except Exception:
        _cloudlog().exception("UI streamer crashed; restarting")
        if time.monotonic() - service_started_at >= 30.0:
          retry_delay = 1.0
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2.0, 30.0)
  finally:
    config.clear_session_token()


if __name__ == "__main__":
  main()
