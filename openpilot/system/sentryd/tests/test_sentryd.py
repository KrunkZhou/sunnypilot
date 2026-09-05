import sqlite3
import sys
import threading
import time
import types

import pytest

from openpilot.cereal import log
from openpilot.system.sentryd.capture import CaptureResult
from openpilot.system.sentryd.config import CURRENT_CONSENT_VERSION, SentryConfig
from openpilot.system.sentryd.sentryd import CaptureJob, SentryMode, main
from openpilot.system.sentryd.store import OutboxStats, SentryStore


class ConfigStore:
  def __init__(self, config=None):
    self.config = config or SentryConfig(
      enabled=True, capture_upload_consent_version=CURRENT_CONSENT_VERSION, wait_for_driver_exit=False)

  def initialize(self):
    return self.config

  def load(self):
    return self.config


class Params:
  def __init__(self):
    self.values = {"IsOffroad": True}

  def get_bool(self, key):
    return bool(self.values.get(key, False))

  def get(self, key):
    return self.values.get(key)

  def put_bool(self, key, value, block=False):
    self.values[key] = value

  def put(self, key, value, block=False):
    self.values[key] = value

  def remove(self, key):
    self.values.pop(key, None)


class SubMaster:
  def __init__(self):
    self.updated = {"accelerometer": False}
    self.accelerometer = log.SensorEventData.new_message(acceleration={"v": [0.0, 0.0, 9.81]}).as_reader()
    self.device_state = type("DeviceState", (), {"started": False})()
    self.panda_states = [type("PandaState", (), {
      "pandaType": "dos", "ignitionLine": False, "ignitionCan": False,
    })()]
    self.panda_checks = True
    self.seen = {"accelerometer": True}
    self.valid = {"accelerometer": True}
    self.alive = {"accelerometer": True}
    self.freq_ok = {"accelerometer": True}
    self.recv_time = {"accelerometer": 100.0}
    self.logMonoTime = {"accelerometer": 100_000_000_000}

  def update(self, _timeout):
    pass

  def all_checks(self, services):
    if services == ["pandaStates"]:
      return self.panda_checks
    if services == ["accelerometer"]:
      return self.alive["accelerometer"] and self.valid["accelerometer"] and self.freq_ok["accelerometer"]
    return False

  def __getitem__(self, key):
    if key == "accelerometer":
      return self.accelerometer
    if key == "deviceState":
      return self.device_state
    if key == "pandaStates":
      return self.panda_states
    raise KeyError(key)


class FailingStore:
  path = None
  media_quota_bytes = 1024

  def close_open_events(self):
    return 0

  def begin_revision(self, **_kwargs):
    raise sqlite3.OperationalError("disk full")

  def close_event(self, _event_id):
    raise sqlite3.OperationalError("database is locked")

  def stats(self):
    return OutboxStats(0, 0, 0)


def test_persistence_failures_do_not_escape_detection_or_strand_episode() -> None:
  mode = SentryMode(
    config_store=ConfigStore(), store=FailingStore(), params=Params(), volatile_params=Params(), sm=SubMaster(),
    capture=object(), clock=lambda: 100.0,
  )
  mode._process_detection("motion", 100.0)
  assert mode.active_event_id is None and mode.capture_queue == []
  assert "disk full" in mode.persistence_error
  assert not mode.detector.episode_active

  mode.active_event_id = "event"
  mode.active_episode_started_at = "time"
  mode._close_active_episode()
  assert mode.active_event_id is None and mode.active_episode_started_at is None
  assert "database is locked" in mode.persistence_error


def test_default_submaster_tracks_the_intentional_ten_hz_consumer(monkeypatch) -> None:
  calls = []

  def make_submaster(services, **kwargs):
    calls.append((services, kwargs))
    return SubMaster()

  messaging = types.ModuleType("openpilot.cereal.messaging")
  messaging.SubMaster = make_submaster
  monkeypatch.setitem(sys.modules, "openpilot.cereal.messaging", messaging)
  SentryMode(
    config_store=ConfigStore(), store=FailingStore(), params=Params(), volatile_params=Params(),
    capture=object(), clock=lambda: 100.0,
  )
  assert calls == [(["accelerometer", "deviceState", "pandaStates"], {"frequency": 10.0})]


def test_physical_panda_ignition_wins_over_always_offroad_state() -> None:
  sm = SubMaster()
  sm.panda_states = [type("PandaState", (), {
    "pandaType": "dos", "ignitionLine": False, "ignitionCan": True,
  })()]
  mode = SentryMode(
    config_store=ConfigStore(), store=FailingStore(), params=Params(), volatile_params=Params(), sm=sm,
    capture=object(), clock=lambda: 100.0,
  )
  assert mode._is_onroad()


@pytest.mark.parametrize("failure", ["stale", "empty", "unknown"])
def test_missing_physical_ignition_status_fails_closed(failure) -> None:
  sm = SubMaster()
  if failure == "stale":
    sm.panda_checks = False
  elif failure == "empty":
    sm.panda_states = []
  else:
    from openpilot.cereal import log
    sm.panda_states[0].pandaType = log.PandaState.PandaType.unknown
  mode = SentryMode(
    config_store=ConfigStore(), store=FailingStore(), params=Params(), volatile_params=Params(), sm=sm,
    capture=object(), clock=lambda: 100.0,
  )
  assert mode._is_onroad()
  assert "ignition status" in mode.ignition_error


def test_ignition_abort_is_signaled_before_config_load(tmp_path) -> None:
  params = Params()
  sm = SubMaster()
  sm.panda_states[0].ignitionCan = True
  store = SentryStore(tmp_path / "outbox.sqlite3")

  class CheckingConfigStore(ConfigStore):
    def load(inner_self):
      assert mode.capture_abort_event.is_set()
      assert mode.capture_abort_reason == "ignition_on"
      return inner_self.config

  mode = SentryMode(
    config_store=CheckingConfigStore(), store=store, params=params, volatile_params=params, sm=sm,
    capture=object(), clock=lambda: 100.0,
  )
  mode.update()


def test_stale_accelerometer_is_actionable_and_clears_evidence(tmp_path) -> None:
  params = Params()
  sm = SubMaster()
  sm.recv_time["accelerometer"] = 98.0
  sm.logMonoTime["accelerometer"] = 98_000_000_000
  store = SentryStore(tmp_path / "outbox.sqlite3")
  mode = SentryMode(
    config_store=ConfigStore(), store=store, params=params, volatile_params=params, sm=sm,
    capture=object(), clock=lambda: 100.0,
  )
  mode.arm_started_at = 0.0
  mode.detector.motion_evidence_seconds = 0.9
  mode.update()
  assert mode.state == "sensor_unavailable"
  assert "Accelerometer" in mode.state_error
  assert mode.detector.motion_evidence_seconds == 0


def test_real_accelerometer_reader_captures_before_warning_and_warns_without_duplicate(tmp_path, monkeypatch) -> None:
  params = Params()
  sm = SubMaster()
  sm.updated["accelerometer"] = True
  store = SentryStore(tmp_path / "outbox.sqlite3")
  now = [100.0]
  mode = SentryMode(
    config_store=ConfigStore(SentryConfig(enabled=True, capture_upload_consent_version=CURRENT_CONSENT_VERSION,
                                         warning_persistence_seconds=0.5, wait_for_driver_exit=False)),
    store=store, params=params, volatile_params=params, sm=sm, capture=object(), clock=lambda: now[0],
  )
  mode.arm_started_at = 0.0
  monkeypatch.setattr(mode, "_start_capture_if_needed", lambda: None)
  try:
    mode.update()
    assert mode.state == "armed"
    assert mode.state_error is None
    assert params.get_bool("SentryRuntimeEnabled")
    assert mode.detector.previous_acceleration == pytest.approx((0.0, 0.0, 9.81))

    for index in range(1, 6):
      now[0] = 100.0 + index / 10
      sm.recv_time["accelerometer"] = now[0]
      sm.logMonoTime["accelerometer"] = round(now[0] * 1e9)
      sm.accelerometer = log.SensorEventData.new_message(acceleration={"v": [index / 10, 0.0, 9.81]}).as_reader()
      mode.update()
      if index == 1:
        assert mode.state == "motion"
        assert mode.active_event_id is not None
        first_event_id = mode.active_event_id
        assert mode.capture_queue == [CaptureJob(first_event_id, 1)]
        assert store.revision_state(first_event_id, 1) == "capturing"

    assert mode.state == "warning"
    assert mode.state_error is None
    assert mode.active_event_id == first_event_id
    assert mode.capture_queue == [CaptureJob(mode.active_event_id, 1)]
    assert store.revision_state(mode.active_event_id, 1) == "capturing"
    assert store.connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 1
  finally:
    store.close()


def test_sample_age_uses_time_after_receive(tmp_path) -> None:
  now = [100.0]
  params = Params()

  class ReceivingSubMaster(SubMaster):
    def update(self, _timeout):
      now[0] += 0.01
      self.recv_time["accelerometer"] = now[0]
      self.logMonoTime["accelerometer"] = round(now[0] * 1e9)
      self.updated["accelerometer"] = True

  store = SentryStore(tmp_path / "outbox.sqlite3")
  mode = SentryMode(
    config_store=ConfigStore(), store=store, params=params, volatile_params=params, sm=ReceivingSubMaster(),
    capture=object(), clock=lambda: now[0],
  )
  mode.arm_started_at = 0.0
  try:
    mode.update()
    assert mode.state == "armed"
    assert mode.state_error is None
    assert mode.accelerometer_status["receive_age_seconds"] == 0
  finally:
    store.close()


def test_missing_sensor_demand_is_restored_without_toggling_setting(tmp_path) -> None:
  now = [100.0]
  params = Params()
  store = SentryStore(tmp_path / "outbox.sqlite3")
  mode = SentryMode(
    config_store=ConfigStore(), store=store, params=params, volatile_params=params, sm=SubMaster(),
    capture=object(), clock=lambda: now[0],
  )
  try:
    mode.update()
    assert params.get_bool("SentryRuntimeEnabled")
    params.remove("SentryRuntimeEnabled")
    now[0] += 1.0
    mode.update()
    assert params.get_bool("SentryRuntimeEnabled")
    assert mode.runtime_error is None
  finally:
    store.close()


def test_failed_sensor_demand_is_reported_and_retried(tmp_path) -> None:
  class FailingDemandParams(Params):
    fail = True

    def put_bool(self, key, value, block=False):
      if key != "SentryRuntimeEnabled" or not self.fail:
        super().put_bool(key, value, block)

  params = FailingDemandParams()
  store = SentryStore(tmp_path / "outbox.sqlite3")
  mode = SentryMode(
    config_store=ConfigStore(), store=store, params=params, volatile_params=params, sm=SubMaster(),
    capture=object(), clock=lambda: 100.0,
  )
  try:
    mode.update()
    assert not params.get_bool("SentryRuntimeEnabled")
    assert mode.last_runtime_enabled is None
    assert "sensor process" in params.get("SentryRuntimeStatus")["error"]
    params.fail = False
    mode.update()
    assert params.get_bool("SentryRuntimeEnabled")
    assert mode.runtime_error is None
  finally:
    store.close()


def test_run_accounts_for_work_time_without_catch_up_bursts(tmp_path, monkeypatch) -> None:
  now = [100.0]
  starts = []
  waits = []
  work_times = iter([0.03, 0.14, 0.03, 0.03])

  class StopEvent:
    def wait(self, timeout):
      if len(starts) == 4:
        return True
      waits.append(timeout)
      now[0] += timeout
      return False

    def set(self):
      pass

  params = Params()
  mode = SentryMode(
    config_store=ConfigStore(), store=SentryStore(tmp_path / "outbox.sqlite3"), params=params, volatile_params=params,
    sm=SubMaster(), capture=object(), clock=lambda: now[0],
  )

  def update():
    starts.append(now[0])
    now[0] += next(work_times)

  monkeypatch.setattr(mode, "update", update)
  mode.run(StopEvent())
  assert starts == pytest.approx([100.1, 100.2, 100.34, 100.44])
  assert waits == pytest.approx([0.1, 0.07, 0.0, 0.07])


def test_disabling_aborts_inflight_capture_and_persists_stale_outcome(tmp_path) -> None:
  config_store = ConfigStore()
  params = Params()
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = "2efb19cb-3bf6-49a7-865a-f15e87f50743"
  timestamp = "2026-09-04T12:00:00+00:00"
  store.begin_revision(
    event_id=event_id, revision=1, kind="warning", source="motion",
    episode_started_at=timestamp, detected_at=timestamp, message="Movement detected while parked.",
  )
  started = threading.Event()

  class Capture:
    def capture(self, abort_callback):
      started.set()
      deadline = time.monotonic() + 2
      while not abort_callback() and time.monotonic() < deadline:
        time.sleep(0.001)
      return CaptureResult({}, {"wide": "stale_capture", "cabin": "stale_capture"})

  now = [100.0]
  mode = SentryMode(
    config_store=config_store, store=store, params=params, volatile_params=params, sm=SubMaster(),
    capture=Capture(), clock=lambda: now[0],
  )
  mode.capture_queue.append(CaptureJob(event_id, 1))
  mode.update()
  assert started.wait(1)
  config_store.config = SentryConfig()
  now[0] += 2
  mode.update()
  mode.active_capture.thread.join(1)
  mode.update()
  queued = store.next_pending(now[0])
  assert queued is not None
  assert {item["reason"] for item in queued.metadata["omitted_media"]} == {"stale_capture"}


def test_queued_abort_retries_storage_failure_without_later_capture(tmp_path, monkeypatch) -> None:
  params = Params()
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = "2efb19cb-3bf6-49a7-865a-f15e87f50743"
  timestamp = "2026-09-04T12:00:00+00:00"
  store.begin_revision(
    event_id=event_id, revision=1, kind="warning", source="motion",
    episode_started_at=timestamp, detected_at=timestamp, message="Movement detected while parked.",
  )
  now = [100.0]
  mode = SentryMode(
    config_store=ConfigStore(), store=store, params=params, volatile_params=params, sm=SubMaster(),
    capture=object(), clock=lambda: now[0],
  )
  mode.capture_queue.append(CaptureJob(event_id, 1))
  original_finish = store.finish_capture
  calls = [0]

  def fail_once(*args, **kwargs):
    calls[0] += 1
    if calls[0] == 1:
      raise sqlite3.OperationalError("disk full")
    return original_finish(*args, **kwargs)

  monkeypatch.setattr(store, "finish_capture", fail_once)
  mode._abort_capture_work("stale_capture")
  assert len(mode.capture_queue) == 1 and mode.capture_queue[0].abort_reason == "stale_capture"
  now[0] = mode.capture_queue[0].next_finalize_at
  mode._start_capture_if_needed()
  assert mode.capture_queue == [] and mode.active_capture is None
  queued = store.next_pending(0)
  assert {item["reason"] for item in queued.metadata["omitted_media"]} == {"stale_capture"}


def test_queued_abort_does_not_retry_after_post_commit_failure(tmp_path, monkeypatch) -> None:
  params = Params()
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = "2efb19cb-3bf6-49a7-865a-f15e87f50743"
  timestamp = "2026-09-04T12:00:00+00:00"
  store.begin_revision(
    event_id=event_id, revision=1, kind="warning", source="motion",
    episode_started_at=timestamp, detected_at=timestamp, message="Movement detected while parked.",
  )
  mode = SentryMode(
    config_store=ConfigStore(), store=store, params=params, volatile_params=params, sm=SubMaster(),
    capture=object(), clock=lambda: 100.0,
  )
  mode.capture_queue.append(CaptureJob(event_id, 1))
  monkeypatch.setattr(store, "enforce_media_quota", lambda: (_ for _ in ()).throw(OSError("post-commit failure")))
  mode._abort_capture_work("stale_capture")
  assert mode.capture_queue == []
  assert store.revision_state(event_id, 1) == "ready"


def test_upload_poll_retries_acknowledged_media_cleanup_in_same_session(tmp_path, monkeypatch) -> None:
  params = Params()
  store = SentryStore(tmp_path / "outbox.sqlite3")
  now = [100.0]
  mode = SentryMode(
    config_store=ConfigStore(), store=store, params=params, volatile_params=params, sm=SubMaster(),
    capture=object(), clock=lambda: now[0],
  )
  calls = []

  def cleanup():
    calls.append(now[0])
    if len(calls) == 1:
      raise OSError("transient cleanup failure")
    return 0

  monkeypatch.setattr(store, "cleanup_acknowledged_media", cleanup)
  mode._start_upload_if_needed()
  assert calls == [100.0]
  assert "transient cleanup failure" in mode.persistence_error

  now[0] += 1.0
  mode._start_upload_if_needed()
  assert calls == [100.0, 101.0]
  assert mode.upload_thread is None


def test_main_clears_stale_runtime_before_fallible_initialization(monkeypatch) -> None:
  import openpilot.system.sentryd.sentryd as sentryd_module
  params = Params()
  params.values.update(dict.fromkeys(
    ("SentryRuntimeEnabled", "SentryCaptureLease", "SentryRuntimeStatus", "SentryRuntimeCommand"), "stale"))
  constructed = []

  def fail_mode(**_kwargs):
    constructed.append(True)
    assert not any(key.startswith("Sentry") for key in params.values)
    raise RuntimeError("simulated config failure")

  monkeypatch.setattr(sentryd_module, "runtime_params", lambda: params)
  monkeypatch.setattr(sentryd_module, "SentryMode", fail_mode)
  monkeypatch.setattr(sentryd_module.signal, "signal", lambda *_: None)
  with pytest.raises(RuntimeError, match="config failure"):
    main()
  assert constructed == [True]
