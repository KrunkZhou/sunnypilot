import ast
from pathlib import Path
from types import SimpleNamespace

from openpilot.cereal.visionipc import VisionStreamType
from openpilot.system.sentryd.capture import SentryCapture
from openpilot.system.sentryd.jpeg import CaptureAborted
from openpilot.system.sentryd.store import MediaData


class FakeParams:
  def __init__(self):
    self.values = {"IsOffroad": True, "IsLiveStreaming": False}

  def get_bool(self, key):
    return bool(self.values.get(key, False))

  def put(self, key, value, block=False):
    self.values[key] = value

  def remove(self, key):
    self.values.pop(key, None)


class Clock:
  def __init__(self):
    self.now = 0.0

  def __call__(self):
    current = self.now
    self.now += 0.05
    return current


class Lock:
  def __enter__(self):
    return self

  def __exit__(self, *_args):
    pass


class Client:
  def connect(self, _blocking):
    return True

  def recv(self, _timeout):
    return SimpleNamespace()


def test_camera_roles_connect_and_report_unavailable_independently(monkeypatch) -> None:
  import openpilot.system.sentryd.capture as capture_module
  monkeypatch.setattr(capture_module, "CAMERA_WARMUP_SECONDS", 0.0)
  monkeypatch.setattr(capture_module, "MAX_CAPTURE_LEASE_SECONDS", 0.5)
  monkeypatch.setattr(capture_module.time, "sleep", lambda _: None)
  params = FakeParams()
  stream_calls = []

  def available_streams(name, *, block):
    stream_calls.append((name, block))
    return [VisionStreamType.VISION_STREAM_WIDE_ROAD]

  class Encoder:
    def encode(self, _frame, _timeout, _abort):
      return MediaData(b"\xff\xd8wide\xff\xd9", 4, 2)

  result = SentryCapture(
    params=params, volatile_params=params, encoder=Encoder(), clock=Clock(), lock_factory=Lock,
    client_factory=lambda *_: Client(), available_streams=available_streams,
  ).capture()
  assert set(result.media) == {"wide"}
  assert result.omissions == {"cabin": "camera_unavailable"}
  assert stream_calls and all(call == ("camerad", False) for call in stream_calls)
  assert "SentryCaptureLease" not in params.values


def test_disable_before_capture_is_stale_not_ignition(monkeypatch) -> None:
  params = FakeParams()
  result = SentryCapture(
    params=params, volatile_params=params, encoder=object(), clock=Clock(), lock_factory=Lock,
    client_factory=lambda *_: Client(), available_streams=lambda *_args, **_kwargs: [],
  ).capture(lambda: True)
  assert result.omissions == {"wide": "stale_capture", "cabin": "stale_capture"}


def test_ignition_change_interrupts_active_encoder_and_releases_lease(monkeypatch) -> None:
  import openpilot.system.sentryd.capture as capture_module
  monkeypatch.setattr(capture_module, "CAMERA_WARMUP_SECONDS", 0.0)
  monkeypatch.setattr(capture_module.time, "sleep", lambda _: None)
  params = FakeParams()

  class Encoder:
    def encode(self, _frame, _timeout, abort_callback):
      params.values["IsOffroad"] = False
      assert abort_callback()
      raise CaptureAborted("ignition on")

  result = SentryCapture(
    params=params, volatile_params=params, encoder=Encoder(), clock=Clock(), lock_factory=Lock,
    client_factory=lambda *_: Client(),
    available_streams=lambda *_args, **_kwargs: [VisionStreamType.VISION_STREAM_WIDE_ROAD],
  ).capture()
  assert result.media == {}
  assert result.omissions == {"wide": "ignition_on", "cabin": "ignition_on"}
  assert "SentryCaptureLease" not in params.values


def test_one_camera_client_failure_does_not_discard_healthy_capture(monkeypatch) -> None:
  import openpilot.system.sentryd.capture as capture_module
  monkeypatch.setattr(capture_module, "CAMERA_WARMUP_SECONDS", 0.0)
  monkeypatch.setattr(capture_module, "MAX_CAPTURE_LEASE_SECONDS", 0.5)
  monkeypatch.setattr(capture_module.time, "sleep", lambda _: None)
  params = FakeParams()

  class Encoder:
    def encode(self, _frame, _timeout, _abort):
      return MediaData(b"\xff\xd8wide\xff\xd9", 4, 2)

  def client_factory(_name, stream, _conflate):
    if stream == VisionStreamType.VISION_STREAM_CABIN:
      raise RuntimeError("cabin unavailable")
    return Client()

  result = SentryCapture(
    params=params, volatile_params=params, encoder=Encoder(), clock=Clock(), lock_factory=Lock,
    client_factory=client_factory,
    available_streams=lambda *_args, **_kwargs: list(CAMERA_STREAMS_FOR_TEST),
  ).capture()
  assert set(result.media) == {"wide"}
  assert result.omissions == {"cabin": "camera_unavailable"}


def test_one_camera_receive_failure_is_isolated(monkeypatch) -> None:
  import openpilot.system.sentryd.capture as capture_module
  monkeypatch.setattr(capture_module, "CAMERA_WARMUP_SECONDS", 0.0)
  monkeypatch.setattr(capture_module, "MAX_CAPTURE_LEASE_SECONDS", 0.5)
  monkeypatch.setattr(capture_module.time, "sleep", lambda _: None)
  params = FakeParams()

  class FailingClient(Client):
    def recv(self, _timeout):
      raise OSError("camera read failed")

  class Encoder:
    def encode(self, _frame, _timeout, _abort):
      return MediaData(b"\xff\xd8wide\xff\xd9", 4, 2)

  result = SentryCapture(
    params=params, volatile_params=params, encoder=Encoder(), clock=Clock(), lock_factory=Lock,
    client_factory=lambda _name, stream, _conflate: (
      FailingClient() if stream == VisionStreamType.VISION_STREAM_CABIN else Client()),
    available_streams=lambda *_args, **_kwargs: list(CAMERA_STREAMS_FOR_TEST),
  ).capture()
  assert set(result.media) == {"wide"}
  assert result.omissions == {"cabin": "capture_failed"}


def test_partial_lease_write_is_cleared(monkeypatch) -> None:
  class FailingParams(FakeParams):
    def put(self, key, value, block=False):
      super().put(key, value, block)
      if key == "SentryCaptureLease":
        raise OSError("lease write failed")

  params = FailingParams()
  result = SentryCapture(
    params=params, volatile_params=params, encoder=object(), clock=Clock(), lock_factory=Lock,
    client_factory=lambda *_: Client(), available_streams=lambda *_args, **_kwargs: [],
  ).capture()
  assert result.media == {}
  assert "SentryCaptureLease" not in params.values


CAMERA_STREAMS_FOR_TEST = (
  VisionStreamType.VISION_STREAM_WIDE_ROAD,
  VisionStreamType.VISION_STREAM_CABIN,
)


def test_existing_snapshot_helper_uses_shared_camera_operation_lock() -> None:
  snapshot_path = Path(__file__).parents[2] / "camerad" / "snapshot.py"
  tree = ast.parse(snapshot_path.read_text())
  function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "get_snapshots")
  lock = function.body[0]
  assert isinstance(lock, ast.With)
  context = lock.items[0].context_expr
  assert isinstance(context, ast.Call) and isinstance(context.func, ast.Name) and context.func.id == "CameraOperationLock"


def test_repeated_capture_reuses_warm_cameras_and_one_bounded_lease(monkeypatch) -> None:
  import openpilot.system.sentryd.capture as capture_module
  now = [0.0]
  monkeypatch.setattr(capture_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
  params = FakeParams()
  connections, encoded_at, lock_events, results, completed_at = [], [], [], [], []

  class TrackingLock(Lock):
    def __enter__(self):
      lock_events.append("enter")

    def __exit__(self, *_args):
      lock_events.append("exit")

  class Encoder:
    def encode(self, _frame, _timeout, _abort):
      encoded_at.append(now[0])
      return MediaData(b"\xff\xd8" + str(len(encoded_at)).encode() + b"\xff\xd9", 4, 2)

  def client_factory(*args):
    connections.append(args)
    return Client()

  def completed(result):
    results.append(result)
    completed_at.append(now[0])
    assert "SentryCaptureLease" in params.values

  capture = SentryCapture(
    params=params, volatile_params=params, encoder=Encoder(), clock=lambda: now[0], lock_factory=TrackingLock,
    client_factory=client_factory, available_streams=lambda *_args, **_kwargs: CAMERA_STREAMS_FOR_TEST,
  )
  capture.capture_repeated(completed, next_capture=lambda: now[0] - completed_at[-1] >= 1,
                           abort_callback=lambda: len(results) == 4)
  assert len(results) == 4 and len(connections) == 2
  assert encoded_at[0] >= capture_module.CAMERA_WARMUP_SECONDS
  assert all(1 <= later - earlier < 1.2 for earlier, later in zip(completed_at[:-1], completed_at[1:], strict=True))
  assert len({result.media["wide"].data for result in results}) == 4
  assert lock_events == ["enter", "exit"]
  assert now[0] < capture_module.MAX_CAPTURE_LEASE_SECONDS
  assert "SentryCaptureLease" not in params.values


def test_repeated_session_expires_without_extending_camera_lease(monkeypatch) -> None:
  import openpilot.system.sentryd.capture as capture_module
  now = [0.0]
  monkeypatch.setattr(capture_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
  params = FakeParams()
  results = []

  class Encoder:
    def encode(self, _frame, _timeout, _abort):
      return MediaData(b"\xff\xd8frame\xff\xd9", 4, 2)

  capture = SentryCapture(
    params=params, volatile_params=params, encoder=Encoder(), clock=lambda: now[0], lock_factory=Lock,
    client_factory=lambda *_args: Client(), available_streams=lambda *_args, **_kwargs: CAMERA_STREAMS_FOR_TEST,
  )
  capture.capture_repeated(results.append, next_capture=lambda: False)
  assert len(results) == 1
  assert now[0] == capture_module.MAX_CAPTURE_LEASE_SECONDS
  assert "SentryCaptureLease" not in params.values


def test_ignition_between_repeated_pairs_preserves_emitted_result_and_releases_lease(monkeypatch) -> None:
  import openpilot.system.sentryd.capture as capture_module
  now = [0.0]
  monkeypatch.setattr(capture_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
  params = FakeParams()
  results = []

  class Encoder:
    def encode(self, _frame, _timeout, _abort):
      return MediaData(b"\xff\xd8frame\xff\xd9", 4, 2)

  def completed(result):
    results.append(result)
    params.values["IsOffroad"] = False

  capture = SentryCapture(
    params=params, volatile_params=params, encoder=Encoder(), clock=lambda: now[0], lock_factory=Lock,
    client_factory=lambda *_args: Client(), available_streams=lambda *_args, **_kwargs: CAMERA_STREAMS_FOR_TEST,
  )
  capture.capture_repeated(completed, next_capture=lambda: True)
  assert len(results) == 1 and set(results[0].media) == {"wide", "cabin"}
  assert "SentryCaptureLease" not in params.values
