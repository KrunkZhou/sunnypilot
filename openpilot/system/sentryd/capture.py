from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from openpilot.cereal.visionipc import VisionStreamType
from openpilot.common.swaglog import cloudlog
from openpilot.system.sentryd.diagnostics import bounded_diagnostic, log_capture_diagnostic
from openpilot.system.sentryd.jpeg import CaptureAborted, NativeJpegEncoder
from openpilot.system.sentryd.runtime import (
  MAX_CAPTURE_LEASE_SECONDS,
  CameraOperationLock,
  clear_capture_lease,
  runtime_params,
  set_capture_lease,
)
from openpilot.system.sentryd.store import MediaData

if TYPE_CHECKING:
  from openpilot.common.params import Params


CAMERA_STREAMS = {
  "wide": VisionStreamType.VISION_STREAM_WIDE_ROAD,
  "cabin": VisionStreamType.VISION_STREAM_CABIN,
}
CAMERA_WARMUP_SECONDS = 4.0


@dataclass(frozen=True)
class CaptureResult:
  media: dict[str, MediaData]
  omissions: dict[str, str]


class SentryCapture:
  def __init__(self, *, params: Params | None = None, volatile_params: Params | None = None,
               encoder: NativeJpegEncoder | None = None, clock: Callable[[], float] = time.monotonic,
               lock_factory: Callable[[], CameraOperationLock] = CameraOperationLock,
               client_factory=None, available_streams=None):
    if params is None:
      from openpilot.common.params import Params
      params = Params()
    if client_factory is None or available_streams is None:
      from msgq.visionipc import VisionIpcClient
      client_factory = client_factory or VisionIpcClient
      available_streams = available_streams or VisionIpcClient.available_streams
    self.params = params
    self.volatile_params = volatile_params if volatile_params is not None else runtime_params()
    self.encoder = encoder or NativeJpegEncoder()
    self.clock = clock
    self.lock_factory = lock_factory
    self.client_factory = client_factory
    self.available_streams = available_streams

  def capture(self, abort_callback: Callable[[], bool] | None = None) -> CaptureResult:
    results: list[CaptureResult] = []
    self.capture_repeated(results.append, abort_callback=abort_callback)
    return results[0]

  def capture_repeated(self, completed: Callable[[CaptureResult], None], *,
                       next_capture: Callable[[], bool] | None = None,
                       abort_callback: Callable[[], bool] | None = None) -> None:
    """Reuse warm clients for serial requests within one bounded camera lease.

    The first request is implicit. Later requests must already have durable
    metadata before next_capture returns True. The owner paces requests and
    persists each result; this worker never accesses its SQLite connection.
    """
    abort_callback = abort_callback or (lambda: False)
    emitted = False

    def emit(result: CaptureResult) -> None:
      nonlocal emitted
      emitted = True
      completed(result)

    try:
      log_capture_diagnostic("sentry_capture_stage", stage="camera_lock")
      with self.lock_factory():
        self._capture_locked(abort_callback, emit, next_capture)
    except OSError as exc:
      log_capture_diagnostic("sentry_capture_error", exception_type=type(exc).__name__, error_detail=str(exc))
      if not emitted:
        emit(self._failure("camera_unavailable"))

  def _capture_locked(self, abort_callback: Callable[[], bool], completed: Callable[[CaptureResult], None],
                      next_capture: Callable[[], bool] | None) -> None:
    if not self._is_offroad():
      log_capture_diagnostic("sentry_capture_rejected", reason="ignition_on")
      completed(self._failure("ignition_on"))
      return
    if abort_callback():
      log_capture_diagnostic("sentry_capture_rejected", reason="stale_capture")
      completed(self._failure("stale_capture"))
      return
    if self._get_bool("IsLiveStreaming", fail_closed=True):
      log_capture_diagnostic("sentry_capture_rejected", reason="camera_unavailable", stage="livestream_check")
      completed(self._failure("camera_unavailable"))
      return

    started_at = self.clock()
    deadline = started_at + MAX_CAPTURE_LEASE_SECONDS
    request_id = str(uuid4())
    clients: dict[str, object] = {}
    connected_at: dict[str, float] = {}
    try:
      log_capture_diagnostic("sentry_capture_stage", stage="lease_write", request_id=request_id)
      set_capture_lease(request_id, deadline, self.volatile_params)
      capture_index = 0
      while True:
        capture_index += 1
        with cloudlog.ctx(sentry_request_id=request_id, sentry_capture_index=capture_index):
          completed(self._capture_pair(abort_callback, clients, connected_at, deadline))
        if next_capture is None:
          return
        # Keep cameras warm while the main loop waits for the next eligible
        # motion capture. Never extend a lease: yield the lock after 20 seconds.
        while self.clock() < deadline:
          if abort_callback() or not self._is_offroad():
            return
          if next_capture():
            break
          time.sleep(min(0.05, max(0.0, deadline - self.clock())))
        else:
          return
    finally:
      clear_capture_lease(self.volatile_params)

  def _capture_pair(self, abort_callback: Callable[[], bool], clients: dict[str, object],
                    connected_at: dict[str, float], deadline: float) -> CaptureResult:
    media: dict[str, MediaData] = {}
    omissions: dict[str, str] = {}
    # Use a separate diagnostic clock: instrumentation must not consume the
    # injected capture clock used by deterministic deadline tests.
    diagnostic_started_at = time.monotonic()
    stages: dict[str, str] = {}
    stage_started_at: dict[str, float] = {}
    stage_seconds: dict[str, dict[str, float]] = {role: {} for role in CAMERA_STREAMS}
    receive_attempts = dict.fromkeys(CAMERA_STREAMS, 0)
    frame_received = dict.fromkeys(CAMERA_STREAMS, False)
    errors: dict[str, object] = {}
    encoder_timeout_role: str | None = None

    def set_stage(role: str, stage: str) -> None:
      if stages.get(role) == stage:
        return
      now = time.monotonic()
      if role in stages:
        stage_seconds[role][stages[role]] = now - stage_started_at[role]
      stages[role] = stage
      stage_started_at[role] = now
      log_capture_diagnostic("sentry_capture_stage", role=role, stage=stage,
                             elapsed_seconds=now - diagnostic_started_at)

    def record_error(role: str, exc: Exception) -> None:
      errors[role] = bounded_diagnostic({"exception_type": type(exc).__name__, "error_detail": str(exc)})

    for role in CAMERA_STREAMS:
      set_stage(role, "warmup" if role in clients else "discovery")
    while self.clock() < deadline and len(media) + len(omissions) < len(CAMERA_STREAMS):
      offroad = self._is_offroad()
      aborted = abort_callback()
      if not offroad or aborted:
        reason = "ignition_on" if not offroad else "stale_capture"
        for role in CAMERA_STREAMS:
          if role not in media:
            omissions[role] = reason
        break

      try:
        available = self.available_streams("camerad", block=False)
      except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        available = ()
        for role in CAMERA_STREAMS:
          if role not in clients:
            record_error(role, exc)
      for role, stream in CAMERA_STREAMS.items():
        if role not in clients and role not in omissions and stream in available:
          try:
            set_stage(role, "connect")
            client = self.client_factory("camerad", stream, True)
            if client.connect(False):
              clients[role] = client
              connected_at[role] = self.clock()
              set_stage(role, "warmup")
              errors.pop(role, None)
          except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            record_error(role, exc)
            omissions[role] = "camera_unavailable"

      for role, client in clients.items():
        if role in media or role in omissions:
          continue
        if self.clock() - connected_at[role] < CAMERA_WARMUP_SECONDS:
          continue
        set_stage(role, "frame_wait")
        receive_remaining = deadline - self.clock()
        if receive_remaining <= 0:
          break
        try:
          receive_attempts[role] += 1
          frame = client.recv(max(0, min(100, int(receive_remaining * 1000))))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
          record_error(role, exc)
          omissions[role] = "capture_failed"
          continue
        if frame is None:
          continue
        frame_received[role] = True
        set_stage(role, "jpeg_encode")
        remaining = deadline - self.clock()
        if remaining <= 0:
          break
        try:
          with cloudlog.ctx(sentry_camera_role=role):
            media[role] = self.encoder.encode(frame, remaining, lambda: abort_callback() or not self._is_offroad())
          set_stage(role, "complete")
        except CaptureAborted as exc:
          record_error(role, exc)
          reason = "ignition_on" if not self._is_offroad() else "stale_capture"
          for pending_role in CAMERA_STREAMS:
            if pending_role not in media:
              omissions[pending_role] = reason
          break
        except subprocess.TimeoutExpired as exc:
          record_error(role, exc)
          encoder_timeout_role = role
          omissions[role] = "capture_timeout"
        except (OSError, RuntimeError, ValueError) as exc:
          record_error(role, exc)
          omissions[role] = "capture_failed"
      sleep_remaining = deadline - self.clock()
      if sleep_remaining > 0:
        time.sleep(min(0.05, sleep_remaining))

    for role in CAMERA_STREAMS:
      if role not in media and role not in omissions:
        omissions[role] = "capture_timeout" if role in clients else "camera_unavailable"
    diagnostic_finished_at = time.monotonic()
    for role in CAMERA_STREAMS:
      stage_seconds[role][stages[role]] = diagnostic_finished_at - stage_started_at[role]
    log_capture_diagnostic("sentry_capture_result", elapsed_seconds=diagnostic_finished_at - diagnostic_started_at,
                           encoder_timeout_role=encoder_timeout_role, cameras={role: {
                             "stage": stages[role], "stage_seconds": stage_seconds[role],
                             "receive_attempts": receive_attempts[role], "frame_received": frame_received[role],
                             "outcome": "complete" if role in media else omissions[role],
                             "error": errors.get(role),
                           } for role in CAMERA_STREAMS})
    return CaptureResult(media, omissions)

  @staticmethod
  def _failure(reason: str) -> CaptureResult:
    return CaptureResult({}, dict.fromkeys(CAMERA_STREAMS, reason))

  def _is_offroad(self) -> bool:
    return self._get_bool("IsOffroad", fail_closed=False)

  def _get_bool(self, key: str, *, fail_closed: bool) -> bool:
    try:
      return self.params.get_bool(key)
    except (OSError, RuntimeError):
      return fail_closed
