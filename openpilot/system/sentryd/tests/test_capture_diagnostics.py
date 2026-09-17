import json
import subprocess
from types import SimpleNamespace

import pytest

from openpilot.common.swaglog import cloudlog
from openpilot.system.sentryd import capture as capture_module
from openpilot.system.sentryd.diagnostics import bounded_diagnostic, log_capture_diagnostic
from openpilot.system.sentryd.store import MediaData
from openpilot.system.sentryd.tests.test_capture import Client, FakeParams, Lock


@pytest.fixture
def setup(monkeypatch):
  now = [0.0]
  records = []
  params = FakeParams()
  monkeypatch.setattr(capture_module, "MAX_CAPTURE_LEASE_SECONDS", 0.5)
  monkeypatch.setattr(capture_module, "CAMERA_WARMUP_SECONDS", 0.0)
  monkeypatch.setattr(capture_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
  monkeypatch.setattr(cloudlog, "event", lambda event, **fields: records.append((event, fields, cloudlog.get_ctx())))

  class Encoder:
    def encode(self, _frame, _timeout, _abort):
      return MediaData(b"\xff\xd8private image bytes\xff\xd9", 4, 2)

  def make(**overrides):
    kwargs = {
      "params": params, "volatile_params": params, "encoder": Encoder(), "clock": lambda: now[0], "lock_factory": Lock,
      "client_factory": lambda *_args: Client(), "available_streams": lambda *_args, **_kwargs: list(capture_module.CAMERA_STREAMS.values()),
    }
    return capture_module.SentryCapture(**(kwargs | overrides))

  return SimpleNamespace(now=now, records=records, params=params, make=make)


def summary(setup):
  return next(fields for event, fields, _ctx in setup.records if event == "sentry_capture_result")


@pytest.mark.parametrize("stage", ["discovery", "connect", "warmup", "frame_wait"])
def test_capture_timeout_reports_last_reached_stage_without_poll_spam(setup, monkeypatch, stage):
  class WaitingClient(Client):
    def connect(self, _blocking):
      return stage != "connect"

    def recv(self, _timeout):
      return None

  available = [] if stage == "discovery" else list(capture_module.CAMERA_STREAMS.values())
  if stage == "warmup":
    monkeypatch.setattr(capture_module, "CAMERA_WARMUP_SECONDS", 1.0)
  result = setup.make(client_factory=lambda *_: WaitingClient(), available_streams=lambda *_args, **_kwargs: available).capture()
  reason = "camera_unavailable" if stage in ("discovery", "connect") else "capture_timeout"
  assert result.omissions == {"wide": reason, "cabin": reason}
  for details in summary(setup)["cameras"].values():
    assert details["stage"] == stage
    assert details["frame_received"] is False
    assert (details["receive_attempts"] > 1) == (stage == "frame_wait")
  assert len(setup.records) <= 11  # Many polls, but each stage is logged only once per camera.
  assert "SentryCaptureLease" not in setup.params.values


def test_wide_encoder_timeout_preserves_cabin_capture_budget(setup):
  timeouts = []

  class Encoder:
    def encode(self, _frame, timeout, _abort):
      timeouts.append(timeout)
      if len(timeouts) == 1:
        setup.now[0] += timeout
        raise subprocess.TimeoutExpired("ffmpeg", timeout)
      return MediaData(b"\xff\xd8cabin\xff\xd9", 4, 2)

  result = setup.make(encoder=Encoder()).capture()
  details = summary(setup)
  assert result.omissions == {"wide": "capture_timeout"}
  assert set(result.media) == {"cabin"}
  assert timeouts == [0.25, 0.25]
  assert details["encoder_timeout_role"] == "wide"
  assert details["cameras"]["wide"]["stage"] == "jpeg_encode"
  assert details["cameras"]["wide"]["frame_received"] is True
  assert details["cameras"]["wide"]["error"]["exception_type"] == "TimeoutExpired"
  assert details["cameras"]["cabin"]["receive_attempts"] == 1
  assert details["cameras"]["cabin"]["frame_received"] is True
  assert details["cameras"]["cabin"]["outcome"] == "complete"
  assert "SentryCaptureLease" not in setup.params.values


def test_receive_failure_retains_stage_and_bounded_detail_without_losing_other_camera(setup):
  class FailingClient(Client):
    def recv(self, _timeout):
      raise RuntimeError("receive failure\n\x00" + "x" * 1000)

  wide = capture_module.CAMERA_STREAMS["wide"]
  result = setup.make(client_factory=lambda _name, stream, _conflate: FailingClient() if stream == wide else Client()).capture()
  assert set(result.media) == {"cabin"}
  details = summary(setup)["cameras"]["wide"]
  assert details["stage"] == "frame_wait"
  assert details["error"]["exception_type"] == "RuntimeError"
  assert len(details["error"]["error_detail"]) <= 256
  assert "\n" not in details["error"]["error_detail"] and "\x00" not in details["error"]["error_detail"]


def test_encoder_exception_is_not_reported_as_frame_wait(setup):
  class Encoder:
    def encode(self, _frame, _timeout, _abort):
      raise ValueError("invalid frame geometry")

  result = setup.make(encoder=Encoder()).capture()
  assert result.omissions == {"wide": "capture_failed", "cabin": "capture_failed"}
  for details in summary(setup)["cameras"].values():
    assert details["stage"] == "jpeg_encode" and details["frame_received"] is True
    assert details["error"]["error_detail"] == "invalid frame geometry"


def test_success_records_outcomes_and_session_context_without_image_bytes(setup):
  with cloudlog.ctx(sentry_event_id="episode", sentry_revision=3):
    result = setup.make().capture()
  assert set(result.media) == {"wide", "cabin"} and not result.omissions
  for details in summary(setup)["cameras"].values():
    assert details["outcome"] == "complete" and details["stage"] == "complete"
    assert details["frame_received"] is True and details["receive_attempts"] == 1
    assert "jpeg_encode" in details["stage_seconds"]
  _event, _fields, context = next(row for row in setup.records if row[0] == "sentry_capture_result")
  assert context["sentry_event_id"] == "episode" and context["sentry_revision"] == 3
  assert context["sentry_request_id"] and context["sentry_capture_index"] == 1
  assert "private image bytes" not in json.dumps(setup.records)
  assert "sentry_request_id" not in cloudlog.get_ctx()


def test_logger_failure_does_not_change_capture_or_lease_cleanup(setup, monkeypatch):
  def failing_logger(*_args, **_kwargs):
    raise OSError("log storage unavailable")

  monkeypatch.setattr(cloudlog, "event", failing_logger)
  result = setup.make().capture()
  assert set(result.media) == {"wide", "cabin"} and not result.omissions
  assert "SentryCaptureLease" not in setup.params.values


def test_diagnostic_fields_are_bounded_and_never_repr_buffers(monkeypatch):
  class Frame:
    def __repr__(self):
      raise AssertionError("camera frames must not be inspected")

  records = []
  monkeypatch.setattr(cloudlog, "event", lambda event, **fields: records.append(fields))
  log_capture_diagnostic("sentry_test", frame=Frame(), raw=b"private image", nested={"detail": "x" * 5000},
                         nan=float("nan"), sequence=[b"private image"])
  assert records == [{"frame": "<omitted>", "raw": "<omitted>", "nested": {"detail": "x" * 256},
                      "nan": None, "sequence": "<omitted>"}]
  assert len(bounded_diagnostic({str(i): i for i in range(100)})) == 16


def test_reconnects_restarted_camera_and_restarts_warmup(setup, monkeypatch):
  monkeypatch.setattr(capture_module, "MAX_CAPTURE_LEASE_SECONDS", 2.0)
  monkeypatch.setattr(capture_module, "CAMERA_WARMUP_SECONDS", 0.2)
  monkeypatch.setattr(capture_module.time, "monotonic", lambda: setup.now[0])
  wide = capture_module.CAMERA_STREAMS["wide"]
  connections = {stream: [] for stream in capture_module.CAMERA_STREAMS.values()}
  encoded = []

  class RestartedClient(Client):
    connected = True

    def is_connected(self):
      return self.connected

    def recv(self, _timeout):
      self.connected = False
      return None

  class HealthyClient(Client):
    def __init__(self, stream):
      self.stream = stream

    def recv(self, _timeout):
      return self.stream

  def factory(_name, stream, _conflate):
    connections[stream].append(setup.now[0])
    if stream == wide and len(connections[stream]) == 1:
      return RestartedClient()
    return HealthyClient(stream)

  class Encoder:
    def encode(self, frame, _timeout, _abort):
      encoded.append((frame, setup.now[0]))
      return MediaData(b"\xff\xd8image\xff\xd9", 4, 2)

  result = setup.make(client_factory=factory, encoder=Encoder()).capture()
  assert set(result.media) == {"wide", "cabin"} and not result.omissions
  assert len(connections[wide]) == 2
  assert len(connections[capture_module.CAMERA_STREAMS["cabin"]]) == 1
  assert next(at for stream, at in encoded if stream == wide) >= connections[wide][-1] + 0.2
  assert summary(setup)["cameras"]["wide"]["stage_seconds"]["warmup"] >= 0.4
  assert len(encoded) == 2  # Reconnecting wide must not replace the completed cabin image.
  assert setup.now[0] < 2.0 and "SentryCaptureLease" not in setup.params.values


def test_transient_connection_failure_recovers_same_capture(setup):
  wide = capture_module.CAMERA_STREAMS["wide"]
  attempts = dict.fromkeys(capture_module.CAMERA_STREAMS.values(), 0)

  def factory(_name, stream, _conflate):
    attempts[stream] += 1
    if stream == wide and attempts[stream] == 1:
      raise OSError("camerad is still starting")
    return Client()

  result = setup.make(client_factory=factory).capture()
  assert set(result.media) == {"wide", "cabin"} and not result.omissions
  assert attempts[wide] == 2
  assert attempts[capture_module.CAMERA_STREAMS["cabin"]] == 1
  assert summary(setup)["cameras"]["wide"]["error"] is None
  assert "SentryCaptureLease" not in setup.params.values


def test_repeated_disconnects_stop_at_original_deadline(setup):
  attempts = []

  class RestartedClient(Client):
    connected = True

    def is_connected(self):
      return self.connected

    def recv(self, _timeout):
      self.connected = False
      return None

  def factory(*_args):
    attempts.append(setup.now[0])
    return RestartedClient()

  result = setup.make(client_factory=factory).capture()
  assert not result.media and result.omissions == {"wide": "capture_timeout", "cabin": "capture_timeout"}
  assert len(attempts) > 2
  assert setup.now[0] == 0.5
  assert "SentryCaptureLease" not in setup.params.values


@pytest.mark.parametrize("reason", ["ignition_on", "stale_capture"])
def test_abort_after_disconnect_prevents_reconnect(setup, reason):
  attempts = []
  abort = [False]

  class RestartedClient(Client):
    connected = True

    def is_connected(self):
      return self.connected

    def recv(self, _timeout):
      self.connected = False
      if reason == "ignition_on":
        setup.params.values["IsOffroad"] = False
      else:
        abort[0] = True
      return None

  def factory(*_args):
    attempts.append(setup.now[0])
    return RestartedClient()

  capture = setup.make(client_factory=factory, available_streams=lambda *_args, **_kwargs: [capture_module.CAMERA_STREAMS["wide"]])
  result = capture.capture(lambda: abort[0])
  assert result.omissions == {"wide": reason, "cabin": reason} and not result.media
  assert len(attempts) == 1
  assert setup.now[0] < 0.5 and "SentryCaptureLease" not in setup.params.values


def test_both_encoder_timeouts_still_attempt_both_cameras_within_lease(setup):
  timeouts = []

  class Encoder:
    def encode(self, _frame, timeout, _abort):
      timeouts.append(timeout)
      setup.now[0] += timeout
      raise subprocess.TimeoutExpired("ffmpeg", timeout)

  result = setup.make(encoder=Encoder()).capture()
  assert result.omissions == {"wide": "capture_timeout", "cabin": "capture_timeout"}
  assert timeouts == [0.25, 0.25]
  assert all(details["frame_received"] for details in summary(setup)["cameras"].values())
  assert setup.now[0] == 0.5 and "SentryCaptureLease" not in setup.params.values


def test_reconnects_camera_between_pairs_in_same_lease(setup, monkeypatch):
  monkeypatch.setattr(capture_module, "MAX_CAPTURE_LEASE_SECONDS", 2.0)
  monkeypatch.setattr(capture_module, "CAMERA_WARMUP_SECONDS", 0.1)
  wide = capture_module.CAMERA_STREAMS["wide"]
  connections = dict.fromkeys(capture_module.CAMERA_STREAMS.values(), 0)
  results = []

  class RestartOnSecondFrame(Client):
    connected = True
    received = 0

    def is_connected(self):
      return self.connected

    def recv(self, _timeout):
      self.received += 1
      if self.received >= 2:
        self.connected = False
        return None
      return SimpleNamespace()

  def factory(_name, stream, _conflate):
    connections[stream] += 1
    return RestartOnSecondFrame() if stream == wide and connections[stream] == 1 else Client()

  setup.make(client_factory=factory).capture_repeated(results.append, next_capture=lambda: True,
                                                    abort_callback=lambda: len(results) == 2)
  assert len(results) == 2 and all(set(result.media) == {"wide", "cabin"} for result in results)
  assert connections == {wide: 2, capture_module.CAMERA_STREAMS["cabin"]: 1}
  assert setup.now[0] < 2.0 and "SentryCaptureLease" not in setup.params.values


def test_connection_health_failure_preserves_completed_sibling(setup):
  wide = capture_module.CAMERA_STREAMS["wide"]
  connections = dict.fromkeys(capture_module.CAMERA_STREAMS.values(), 0)

  class BrokenConnectionStatus(Client):
    def is_connected(self):
      raise OSError("camera connection failed")

    def recv(self, _timeout):
      return None

  def factory(_name, stream, _conflate):
    connections[stream] += 1
    return BrokenConnectionStatus() if stream == wide and connections[stream] == 1 else Client()

  result = setup.make(client_factory=factory).capture()
  assert set(result.media) == {"wide", "cabin"} and not result.omissions
  assert connections == {wide: 2, capture_module.CAMERA_STREAMS["cabin"]: 1}
  assert summary(setup)["cameras"]["cabin"]["receive_attempts"] == 1
  assert "SentryCaptureLease" not in setup.params.values


def test_unavailable_cabin_does_not_shorten_wide_encoding_budget(setup):
  timeouts = []

  class SlowEncoder:
    def encode(self, _frame, timeout, _abort):
      timeouts.append(timeout)
      setup.now[0] += min(timeout, 0.35)
      if timeout < 0.35:
        raise subprocess.TimeoutExpired("ffmpeg", timeout)
      return MediaData(b"\xff\xd8wide\xff\xd9", 4, 2)

  capture = setup.make(encoder=SlowEncoder(), available_streams=lambda *_args, **_kwargs: [capture_module.CAMERA_STREAMS["wide"]])
  result = capture.capture()
  assert set(result.media) == {"wide"} and result.omissions == {"cabin": "camera_unavailable"}
  assert timeouts == [0.5]
  assert setup.now[0] == 0.5 and "SentryCaptureLease" not in setup.params.values
