from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from openpilot.system.sentryd.diagnostics import log_capture_diagnostic
from openpilot.system.sentryd.store import MAX_MEDIA_BYTES, MediaData


class CaptureAborted(RuntimeError):
  pass


class NativeJpegEncoder:
  """Encode a padded VisionIPC NV12 frame with comma-deps-ffmpeg/libavcodec."""

  def __init__(self, executable: str | Path | None = None):
    override = os.environ.get("SENTRY_ENCODER")
    if executable is None and override is None:
      import ffmpeg
      executable = Path(ffmpeg.BIN_DIR) / "ffmpeg"
    self.executable = Path(executable or override or "")

  def encode(self, frame, timeout: float, abort_callback: Callable[[], bool] | None = None) -> MediaData:
    started = time.monotonic()
    current_stage = "jpeg_validate"
    stage_started = started

    def record_stage(stage: str, **fields) -> None:
      nonlocal current_stage, stage_started
      now = time.monotonic()
      previous_stage = current_stage
      previous_stage_seconds = max(0.0, now - stage_started)
      current_stage, stage_started = stage, now
      log_capture_diagnostic("sentry_jpeg_stage", stage=stage, elapsed_seconds=max(0.0, now - started),
                             previous_stage=previous_stage, previous_stage_seconds=previous_stage_seconds, **fields)

    record_stage("jpeg_validate", timeout_seconds=timeout)
    try:
      result = self._encode(frame, timeout, started + timeout, abort_callback, record_stage)
    except Exception as exc:
      now = time.monotonic()
      log_capture_diagnostic("sentry_jpeg_failed", stage=current_stage, elapsed_seconds=max(0.0, now - started),
                             stage_seconds=max(0.0, now - stage_started), exception_type=type(exc).__name__, error_detail=str(exc))
      raise
    record_stage("jpeg_complete", output_bytes=len(result.data), width=result.width, height=result.height)
    return result

  def _encode(self, frame, timeout: float, deadline: float, abort_callback: Callable[[], bool] | None,
              record_stage: Callable[..., None]) -> MediaData:
    abort_callback = abort_callback or (lambda: False)
    if timeout <= 0:
      raise subprocess.TimeoutExpired("ffmpeg", timeout)
    width, height, stride, uv_offset = frame.width, frame.height, frame.stride, frame.uv_offset
    uv_height = ((height // 2) + 15) // 16 * 16
    frame_size = uv_offset + stride * uv_height
    if (width <= 0 or height <= 0 or width % 2 or height % 2 or stride < width or
        uv_offset < stride * height or frame_size > len(frame.data)):
      raise ValueError("camera returned invalid NV12 geometry")
    if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
      raise OSError(f"Sentry JPEG encoder is unavailable: {self.executable}")

    # VisionIPC buffers may pad both planes. ffmpeg rawvideo expects tightly packed NV12.
    record_stage("jpeg_pack", width=width, height=height, stride=stride, uv_offset=uv_offset)
    source = memoryview(frame.data)
    raw = bytearray(width * height * 3 // 2)
    destination = memoryview(raw)
    for row in range(height):
      if abort_callback():
        raise CaptureAborted("Sentry JPEG packing was interrupted")
      if time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired("ffmpeg NV12 packing", timeout)
      destination[row * width:(row + 1) * width] = source[row * stride:row * stride + width]
    uv_destination = width * height
    for row in range(height // 2):
      if abort_callback():
        raise CaptureAborted("Sentry JPEG packing was interrupted")
      if time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired("ffmpeg NV12 packing", timeout)
      source_start = uv_offset + row * stride
      destination_start = uv_destination + row * width
      destination[destination_start:destination_start + width] = source[source_start:source_start + width]

    command = [
      str(self.executable), "-hide_banner", "-loglevel", "error", "-nostdin",
      "-f", "rawvideo", "-pixel_format", "nv12", "-video_size", f"{width}x{height}", "-framerate", "1",
      "-i", "pipe:0", "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "7",
      "-f", "image2", "pipe:1",
    ]
    record_stage("jpeg_start")
    if abort_callback():
      raise CaptureAborted("Sentry JPEG encoding was interrupted")
    if time.monotonic() >= deadline:
      raise subprocess.TimeoutExpired(command, timeout)
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout = b""
    stderr = b""
    input_data: bytes | bytearray | None = raw
    try:
      record_stage("jpeg_encode")
      while True:
        if abort_callback():
          raise CaptureAborted("Sentry JPEG encoding was interrupted")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          raise subprocess.TimeoutExpired(command, timeout)
        try:
          stdout, stderr = process.communicate(input=input_data, timeout=min(0.1, remaining))
          break
        except subprocess.TimeoutExpired:
          # communicate retains the partially-written input and captured output.
          input_data = None
    finally:
      if process.poll() is None:
        process.kill()
      if process.poll() is None:
        try:
          process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
          pass
      for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
          pipe.close()

    if process.returncode != 0:
      error = stderr.decode("utf-8", "replace")[-256:]
      raise RuntimeError(f"native Sentry JPEG encoder failed: {error}")
    jpeg = stdout
    if not 4 <= len(jpeg) <= MAX_MEDIA_BYTES or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
      raise RuntimeError("native Sentry JPEG encoder returned an invalid or oversized image")
    return MediaData(jpeg, width, height)
