import os
import platform
import secrets
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path

STREAM_PORT = 8082
STREAM_FPS = 10
BIG_UI_STREAM_FPS = 5
STREAM_MAX_WIDTH = 960
STREAM_TOKEN_BYTES = 24
MAX_STREAM_CLIENTS = 4
BACKGROUND_CORES = [0, 1, 2, 3]
IPC_HEARTBEAT_INTERVAL = 1.0
IPC_IDLE_TIMEOUT = 3.0
FRAME_MAGIC = b"UIS1"
FRAME_HEADER = struct.Struct("!4sIII")  # magic, payload size, width, height

PC = not os.path.isfile("/AGNOS")
COMMA_HOME = os.path.join(str(Path.home()), ".comma" + os.environ.get("OPENPILOT_PREFIX", ""))
SHM_PATH = "/tmp" if platform.system() == "Darwin" else "/dev/shm"
_RUNTIME_SUFFIX = os.environ.get("OPENPILOT_PREFIX", "")
STREAM_SOCKET_PATH = os.path.join(SHM_PATH, f"ui_streamer{_RUNTIME_SUFFIX}.sock")
STREAM_TOKEN_PATH = os.path.join(SHM_PATH, f"ui_streamer{_RUNTIME_SUFFIX}.token")
STREAM_ENABLED_PATH = os.path.join(COMMA_HOME, "ui_streamer", "enabled") if PC else "/data/ui_streamer/enabled"


class RestartBackoff:
  """Small monotonic crash backoff shared with the manager integration."""

  def __init__(self, initial_delay: float = 1.0, maximum_delay: float = 30.0, stable_after: float = 30.0):
    self.initial_delay = initial_delay
    self.maximum_delay = maximum_delay
    self.stable_after = stable_after
    self.reset()

  def reset(self) -> None:
    self._started_at: float | None = None
    self._retry_at = 0.0
    self._next_delay = self.initial_delay

  def ready(self, now: float) -> bool:
    return now >= self._retry_at

  def note_started(self, now: float) -> None:
    self._started_at = now

  def note_exit(self, now: float) -> None:
    if self._started_at is not None and now - self._started_at >= self.stable_after:
      self._next_delay = self.initial_delay
    self._retry_at = now + self._next_delay
    self._next_delay = min(self._next_delay * 2.0, self.maximum_delay)
    self._started_at = None


@contextmanager
def _atomic_write(path: str):
  temporary = tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), delete=False)
  try:
    with temporary:
      yield temporary
    os.replace(temporary.name, path)
  finally:
    try:
      os.unlink(temporary.name)
    except FileNotFoundError:
      pass


class UIStreamerConfig:
  def __init__(self, enabled_path: str = STREAM_ENABLED_PATH, token_path: str = STREAM_TOKEN_PATH):
    self.enabled_path = enabled_path
    self.token_path = token_path

  def enabled(self) -> bool:
    try:
      with open(self.enabled_path) as file:
        return file.read().strip() == "1"
    except OSError:
      return False

  def set_enabled(self, enabled: bool) -> None:
    if not enabled:
      try:
        os.unlink(self.enabled_path)
      except FileNotFoundError:
        pass
      return

    os.makedirs(os.path.dirname(self.enabled_path), mode=0o700, exist_ok=True)
    with _atomic_write(self.enabled_path) as file:
      file.write("1\n")
    os.chmod(self.enabled_path, 0o600)

  def create_session_token(self) -> str:
    token = secrets.token_urlsafe(STREAM_TOKEN_BYTES)
    with _atomic_write(self.token_path) as file:
      file.write(token)
    os.chmod(self.token_path, 0o600)
    return token

  def session_token(self) -> str:
    try:
      with open(self.token_path) as file:
        token = file.read().strip()
      return token if len(token) >= 32 else ""
    except OSError:
      return ""

  def clear_session_token(self) -> None:
    try:
      os.unlink(self.token_path)
    except FileNotFoundError:
      pass


def build_stream_url(address: str, token: str, port: int = STREAM_PORT) -> str:
  from urllib.parse import urlencode

  host = address.strip() or "device-ip"
  return f"http://{host}:{port}/#{urlencode({'token': token})}"
