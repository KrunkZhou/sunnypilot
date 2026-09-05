import io
import os
import subprocess
from types import SimpleNamespace

import pytest

from openpilot.system.sentryd.jpeg import CaptureAborted, NativeJpegEncoder


@pytest.fixture
def diagnostic_events(monkeypatch):
  events = []
  monkeypatch.setattr("openpilot.system.sentryd.diagnostics.cloudlog.event", lambda event, **fields: events.append((event, fields)))
  return events


@pytest.fixture
def encoder_executable(tmp_path):
  executable = tmp_path / "ffmpeg"
  executable.write_bytes(b"test")
  os.chmod(executable, 0o700)
  return executable


class FakeEncoderProcess:
  def __init__(self, output=b"\xff\xd8test\xff\xd9", error=b"", returncode=0):
    self.output, self.error, self.exit_code = output, error, returncode
    self.returncode = None
    self.killed = False
    self.stdin, self.stdout, self.stderr = (io.BytesIO() for _ in range(3))

  def communicate(self, **kwargs):
    self.returncode = self.exit_code
    return self.output, self.error

  def poll(self):
    return self.returncode

  def kill(self):
    self.killed = True
    self.returncode = -9

  def wait(self, timeout=None):
    return self.returncode


def padded_nv12_frame():
  width, height, stride = 4, 2, 6
  uv_offset = stride * height
  data = bytearray(uv_offset + stride * 16)
  data[0:4] = bytes((16, 64, 128, 235))
  data[stride:stride + width] = bytes((20, 80, 140, 220))
  data[uv_offset:uv_offset + width] = bytes((128, 128, 128, 128))
  return SimpleNamespace(width=width, height=height, stride=stride, uv_offset=uv_offset, data=data)


def test_pinned_ffmpeg_encodes_padded_full_frame_nv12() -> None:
  ffmpeg = pytest.importorskip("ffmpeg")
  executable = os.path.join(ffmpeg.BIN_DIR, "ffmpeg")
  result = NativeJpegEncoder(executable).encode(padded_nv12_frame(), 5)
  assert result.width == 4 and result.height == 2
  assert result.data.startswith(b"\xff\xd8") and result.data.endswith(b"\xff\xd9")


def test_abort_kills_in_progress_encoder(encoder_executable, monkeypatch, diagnostic_events) -> None:
  process = FakeEncoderProcess()
  checks = 0

  def abort_during_process():
    nonlocal checks
    checks += 1
    return checks >= 5

  monkeypatch.setattr("openpilot.system.sentryd.jpeg.subprocess.Popen", lambda *_args, **_kwargs: process)
  with pytest.raises(CaptureAborted):
    NativeJpegEncoder(encoder_executable).encode(padded_nv12_frame(), 5, abort_during_process)
  assert process.killed
  assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))
  event, fields = diagnostic_events[-1]
  assert event == "sentry_jpeg_failed"
  assert fields["stage"] == "jpeg_encode"
  assert fields["exception_type"] == "CaptureAborted"
  assert fields["error_detail"] == "Sentry JPEG encoding was interrupted"


def test_success_records_bounded_stage_timings(encoder_executable, monkeypatch, diagnostic_events) -> None:
  process = FakeEncoderProcess()
  monkeypatch.setattr("openpilot.system.sentryd.jpeg.subprocess.Popen", lambda *_args, **_kwargs: process)
  result = NativeJpegEncoder(encoder_executable).encode(padded_nv12_frame(), 5)

  assert [event for event, _ in diagnostic_events] == ["sentry_jpeg_stage"] * 5
  assert [fields["stage"] for _, fields in diagnostic_events] == [
    "jpeg_validate", "jpeg_pack", "jpeg_start", "jpeg_encode", "jpeg_complete",
  ]
  for _, fields in diagnostic_events:
    assert fields["elapsed_seconds"] >= 0
    assert fields["previous_stage_seconds"] >= 0
    assert not any(isinstance(value, bytes | bytearray | memoryview) for value in fields.values())
  assert diagnostic_events[1][1]["width"] == 4
  assert diagnostic_events[1][1]["height"] == 2
  assert diagnostic_events[-1][1]["output_bytes"] == len(result.data)
  assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


def test_packing_timeout_identifies_stage(encoder_executable, monkeypatch, diagnostic_events) -> None:
  now = [0.0]
  monkeypatch.setattr("openpilot.system.sentryd.jpeg.time.monotonic", lambda: now[0])

  def advance_during_packing():
    now[0] = 6.0
    return False

  def unexpected_spawn(*_args, **_kwargs):
    pytest.fail("packing timeout must not launch ffmpeg")

  monkeypatch.setattr("openpilot.system.sentryd.jpeg.subprocess.Popen", unexpected_spawn)
  with pytest.raises(subprocess.TimeoutExpired) as caught:
    NativeJpegEncoder(encoder_executable).encode(padded_nv12_frame(), 5, advance_during_packing)
  assert caught.value.cmd == "ffmpeg NV12 packing"
  event, fields = diagnostic_events[-1]
  assert event == "sentry_jpeg_failed"
  assert fields["stage"] == "jpeg_pack"
  assert fields["exception_type"] == "TimeoutExpired"
  assert fields["stage_seconds"] == 6
  assert fields["elapsed_seconds"] == 6


def test_subprocess_timeout_identifies_stage_and_cleans_up(encoder_executable, monkeypatch, diagnostic_events) -> None:
  now = [0.0]
  monkeypatch.setattr("openpilot.system.sentryd.jpeg.time.monotonic", lambda: now[0])
  process = FakeEncoderProcess()

  def timeout_communicate(**_kwargs):
    now[0] = 6.0
    raise subprocess.TimeoutExpired("ffmpeg", 0.1, output=b"private partial JPEG bytes")

  process.communicate = timeout_communicate
  monkeypatch.setattr("openpilot.system.sentryd.jpeg.subprocess.Popen", lambda *_args, **_kwargs: process)
  with pytest.raises(subprocess.TimeoutExpired):
    NativeJpegEncoder(encoder_executable).encode(padded_nv12_frame(), 5)
  event, fields = diagnostic_events[-1]
  assert event == "sentry_jpeg_failed"
  assert fields["stage"] == "jpeg_encode"
  assert fields["exception_type"] == "TimeoutExpired"
  assert fields["stage_seconds"] == 6
  assert "private partial JPEG" not in repr(diagnostic_events)
  assert process.killed
  assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


@pytest.mark.parametrize(("failure", "exception_type", "stage", "detail"), [
  ("geometry", ValueError, "jpeg_validate", "camera returned invalid NV12 geometry"),
  ("missing_binary", OSError, "jpeg_validate", "Sentry JPEG encoder is unavailable"),
  ("spawn", OSError, "jpeg_start", "cannot launch ffmpeg"),
  ("nonzero_exit", RuntimeError, "jpeg_encode", "native Sentry JPEG encoder failed: test failure"),
  ("invalid_jpeg", RuntimeError, "jpeg_encode", "native Sentry JPEG encoder returned an invalid or oversized image"),
])
def test_encoder_failure_diagnostics(encoder_executable, monkeypatch, diagnostic_events, failure, exception_type, stage, detail) -> None:
  frame = padded_nv12_frame()
  if failure == "geometry":
    frame.width = 3
  if failure == "missing_binary":
    encoder_executable = encoder_executable.with_name("missing-ffmpeg")
  process = FakeEncoderProcess(output=b"invalid" if failure == "invalid_jpeg" else b"\xff\xd8test\xff\xd9",
                               error=b"test failure", returncode=1 if failure == "nonzero_exit" else 0)

  def spawn(*_args, **_kwargs):
    if failure == "spawn":
      raise OSError("cannot launch ffmpeg")
    return process

  monkeypatch.setattr("openpilot.system.sentryd.jpeg.subprocess.Popen", spawn)
  with pytest.raises(exception_type, match=detail):
    NativeJpegEncoder(encoder_executable).encode(frame, 5)
  event, fields = diagnostic_events[-1]
  assert event == "sentry_jpeg_failed"
  assert fields["stage"] == stage
  assert fields["exception_type"] == exception_type.__name__
  assert detail in fields["error_detail"]


def test_encoder_error_detail_is_bounded(encoder_executable, monkeypatch, diagnostic_events) -> None:
  process = FakeEncoderProcess(error=b"x" * 4096, returncode=1)
  monkeypatch.setattr("openpilot.system.sentryd.jpeg.subprocess.Popen", lambda *_args, **_kwargs: process)
  with pytest.raises(RuntimeError):
    NativeJpegEncoder(encoder_executable).encode(padded_nv12_frame(), 5)
  assert len(diagnostic_events[-1][1]["error_detail"]) <= 256


def test_logging_failure_does_not_change_encoder_outcome(encoder_executable, monkeypatch) -> None:
  def broken_logger(*_args, **_kwargs):
    raise OSError("log storage is unavailable")

  monkeypatch.setattr("openpilot.system.sentryd.diagnostics.cloudlog.event", broken_logger)
  process = FakeEncoderProcess()
  monkeypatch.setattr("openpilot.system.sentryd.jpeg.subprocess.Popen", lambda *_args, **_kwargs: process)
  assert NativeJpegEncoder(encoder_executable).encode(padded_nv12_frame(), 5).data == process.output
  frame = padded_nv12_frame()
  frame.width = 3
  with pytest.raises(ValueError, match="invalid NV12 geometry"):
    NativeJpegEncoder(encoder_executable).encode(frame, 5)


def test_initial_diagnostic_time_does_not_extend_deadline(encoder_executable, monkeypatch) -> None:
  now = [0.0]
  monkeypatch.setattr("openpilot.system.sentryd.jpeg.time.monotonic", lambda: now[0])

  def slow_logger(_event, **_fields):
    now[0] = 6.0

  def unexpected_spawn(*_args, **_kwargs):
    pytest.fail("logging time must remain inside the original encoder deadline")

  monkeypatch.setattr("openpilot.system.sentryd.diagnostics.cloudlog.event", slow_logger)
  monkeypatch.setattr("openpilot.system.sentryd.jpeg.subprocess.Popen", unexpected_spawn)
  with pytest.raises(subprocess.TimeoutExpired):
    NativeJpegEncoder(encoder_executable).encode(padded_nv12_frame(), 5)
