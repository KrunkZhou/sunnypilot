import queue
import select
import socket
import threading
import time

from openpilot.common.realtime import drop_realtime, set_core_affinity
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.ui_streamer import (
  BACKGROUND_CORES,
  BIG_UI_STREAM_FPS,
  FRAME_HEADER,
  FRAME_MAGIC,
  IPC_HEARTBEAT_INTERVAL,
  STREAM_FPS,
  STREAM_MAX_WIDTH,
  STREAM_SOCKET_PATH,
  UIStreamerConfig,
)


class UIStreamBridge:
  """Moves latest-only raw UI frames from the realtime renderer to screenstreamd."""

  def __init__(self, fps: int = STREAM_FPS, config: UIStreamerConfig | None = None):
    if fps <= 0:
      raise ValueError("stream FPS must be positive")
    self._config = config or UIStreamerConfig()
    self._frame_interval = 1.0 / fps
    self._queue: queue.Queue[tuple[bytes, int, int]] = queue.Queue(maxsize=1)
    self._demand = threading.Event()
    self._demand_lock = threading.Lock()
    self._demand_generation = 0
    self._reserved_generation: int | None = None
    self._stop_event = threading.Event()
    self._next_capture = 0.0
    self._thread = threading.Thread(target=self._run, name="ui_stream_bridge", daemon=True)

  def start(self) -> None:
    self._thread.start()

  def frame_due(self) -> bool:
    with self._demand_lock:
      due = self._demand.is_set() and not self._queue.full() and time.monotonic() >= self._next_capture
      self._reserved_generation = self._demand_generation if due else None
      return due

  def active(self) -> bool:
    return self._demand.is_set()

  def submit_frame(self, rgba: bytes, width: int, height: int) -> None:
    with self._demand_lock:
      generation = self._reserved_generation
      self._reserved_generation = None
      if not self._demand.is_set() or generation != self._demand_generation:
        return
      try:
        self._queue.put_nowait((rgba, width, height))
        self._next_capture = time.monotonic() + self._frame_interval
      except queue.Full:
        pass

  def _set_demand(self, active: bool) -> None:
    with self._demand_lock:
      if active == self._demand.is_set():
        return
      self._demand_generation += 1
      self._reserved_generation = None
      if active:
        self._demand.set()
      else:
        self._demand.clear()
        self._clear_queue()

  def _connect(self) -> socket.socket | None:
    if not self._config.enabled():
      return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
      sock.connect(STREAM_SOCKET_PATH)
      return sock
    except OSError:
      sock.close()
      return None

  def _run_connected(self, sock: socket.socket) -> None:
    next_heartbeat = 0.0
    while not self._stop_event.is_set() and self._config.enabled():
      frame_ready = self._demand.is_set() and not self._queue.empty()
      write_requested = frame_ready or time.monotonic() >= next_heartbeat
      readable, writable, _ = select.select([sock], [sock] if write_requested else [], [], 0.25)
      if readable:
        state = sock.recv(64)
        if not state:
          return
        if state[-1:] == b"1":
          self._set_demand(True)
        else:
          self._set_demand(False)

      if writable:
        rgba = b""
        if self._demand.is_set():
          try:
            rgba, width, height = self._queue.get_nowait()
          except queue.Empty:
            pass
          if rgba:
            sock.sendall(FRAME_HEADER.pack(FRAME_MAGIC, len(rgba), width, height))
            sock.sendall(rgba)
        if not self._demand.is_set() or not rgba:
          sock.sendall(FRAME_HEADER.pack(FRAME_MAGIC, 0, 0, 0))
        next_heartbeat = time.monotonic() + IPC_HEARTBEAT_INTERVAL

  def _clear_queue(self) -> None:
    try:
      while True:
        self._queue.get_nowait()
    except queue.Empty:
      pass

  def _run(self) -> None:
    try:
      drop_realtime()
      set_core_affinity(BACKGROUND_CORES)
    except OSError:
      cloudlog.exception("UI stream bridge could not set background scheduling")
    while not self._stop_event.is_set():
      sock = self._connect()
      if sock is None:
        self._set_demand(False)
        self._stop_event.wait(0.5)
        continue
      try:
        self._run_connected(sock)
      except (OSError, ValueError):
        pass
      finally:
        self._set_demand(False)
        sock.close()

  def close(self) -> None:
    self._stop_event.set()
    if self._thread.is_alive():
      self._thread.join(timeout=1.0)
    if self._thread.is_alive():
      cloudlog.warning("UI stream bridge did not stop cleanly")


def configure_ui_streamer(gui_app) -> UIStreamBridge:
  bridge = UIStreamBridge(BIG_UI_STREAM_FPS if gui_app.big_ui() else STREAM_FPS)
  bridge.start()  # Start before config_realtime_process so the bridge never inherits SCHED_FIFO.
  gui_app.configure_frame_capture(bridge.active, bridge.frame_due, bridge.submit_frame, STREAM_MAX_WIDTH)
  return bridge
