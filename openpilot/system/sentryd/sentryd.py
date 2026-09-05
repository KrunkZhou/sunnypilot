#!/usr/bin/env python3
from __future__ import annotations

import math
import queue
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from openpilot.cereal import log
from openpilot.common.swaglog import cloudlog
from openpilot.system.sentryd.capture import CaptureResult, SentryCapture
from openpilot.system.sentryd.config import SentryConfig, SentryConfigError, SentryConfigStore
from openpilot.system.sentryd.detector import MotionDetector
from openpilot.system.sentryd.diagnostics import log_capture_diagnostic
from openpilot.system.sentryd.door import DriverDoorSource
from openpilot.system.sentryd.runtime import (
  clear_runtime,
  runtime_enabled,
  runtime_params,
  set_runtime_enabled,
  set_status,
  take_command,
)
from openpilot.system.sentryd.store import SentryStore
from openpilot.system.sentryd.uploader import SentryUploader

if TYPE_CHECKING:
  from openpilot.common.params import Params


ARM_DELAY_SECONDS = 90.0
LOOP_INTERVAL_SECONDS = 0.1
CONFIG_REFRESH_SECONDS = 1.0
STATUS_REFRESH_SECONDS = 1.0
STATUS_HEARTBEAT_SECONDS = 5.0
UPLOAD_POLL_SECONDS = 1.0
CONFIG_LOCK_TIMEOUT_SECONDS = 0.1
REPEAT_CAPTURE_SECONDS = 1.0
MAX_FOLLOW_UP_REVISIONS = 20
EVENT_SCHEMA_VERSION = 2


@dataclass
class CaptureJob:
  event_id: str
  revision: int
  abort_reason: str | None = None
  finalize_attempts: int = 0
  next_finalize_at: float = 0.0


@dataclass
class ActiveCapture:
  job: CaptureJob | None
  thread: threading.Thread
  result: list[CaptureResult]
  finalize_attempts: int = 0
  next_finalize_at: float = 0.0
  requests: queue.SimpleQueue[bool] | None = None
  idle_stop: threading.Event = field(default_factory=threading.Event)
  request_started: threading.Event = field(default_factory=threading.Event)


class SentryMode:
  def __init__(self, *, config_store: SentryConfigStore | None = None, store: SentryStore | None = None,
               params: Params | None = None, volatile_params: Params | None = None, sm=None,
               capture: SentryCapture | None = None, door_source: DriverDoorSource | None = None, clock=time.monotonic):
    self.config_store = config_store or SentryConfigStore(lock_timeout_seconds=CONFIG_LOCK_TIMEOUT_SECONDS)
    if params is None:
      from openpilot.common.params import Params
      params = Params()
    if sm is None:
      import openpilot.cereal.messaging as messaging
      # Track receive frequency at the 10 Hz consumer rate. Accelerometer
      # freshness is checked separately because SubMaster's alive deadline
      # still uses the 104 Hz publisher rate.
      sm = messaging.SubMaster(["accelerometer", "deviceState", "pandaStates"], frequency=10.0)
    self.params = params
    self.volatile_params = volatile_params if volatile_params is not None else runtime_params()
    self.sm = sm
    self.capture = capture or SentryCapture(params=self.params, volatile_params=self.volatile_params)
    self.clock = clock
    self.door_source = door_source if door_source is not None else DriverDoorSource(self.params)
    self.driver_exit_started_at: float | None = None
    self.driver_door_open_seen = False
    self.driver_exit_completed = False

    self.config = SentryConfig()
    self.config_error: str | None = None
    try:
      self.config = self.config_store.initialize()
    except SentryConfigError as exc:
      self.config_error = str(exc)

    self.store = store or SentryStore()
    startup_error: str | None = None
    try:
      self.store.close_open_events()
    except (OSError, sqlite3.Error, ValueError) as exc:
      startup_error = self._persistence_error_message("Could not recover Sentry episodes", exc)
      cloudlog.exception("Could not recover open Sentry episodes")
    self.detector = self._new_detector()
    self.arm_started_at: float | None = None
    self.active_event_id: str | None = None
    self.active_episode_started_at: str | None = None
    self.next_revision = 2
    self.next_capture_at = float("inf")
    self.last_capture_motion_at = float("-inf")
    self.pending_alarm = False
    self.capture_queue: list[CaptureJob] = []
    self.active_capture: ActiveCapture | None = None
    self.upload_thread: threading.Thread | None = None
    self.last_upload_started = float("-inf")
    self.last_config_refresh = float("-inf")
    self.last_status_refresh = float("-inf")
    self.last_status_published_at = float("-inf")
    self.last_status: dict[str, object] | None = None
    self.last_runtime_enabled: bool | None = None
    self.last_runtime_check = float("-inf")
    self.runtime_error: str | None = None
    self.accelerometer_status: dict[str, object] = {}
    self.state = "starting"
    self.state_error: str | None = None
    self.persistence_error: str | None = startup_error
    self.stop_event = threading.Event()
    self.capture_abort_event = threading.Event()
    self.capture_abort_reason: str | None = None
    self.ignition_error: str | None = None

  def _new_detector(self) -> MotionDetector:
    return MotionDetector(
      threshold_mps2=self.config.motion_threshold_mps2,
      warning_persistence_seconds=self.config.warning_persistence_seconds,
      clock=self.clock,
    )

  def _is_onroad(self) -> bool:
    try:
      if not self.params.get_bool("IsOffroad"):
        self.ignition_error = None
        return True
    except (OSError, RuntimeError):
      self.ignition_error = "Physical ignition status is unavailable"
      return True
    try:
      if self.sm["deviceState"].started:
        self.ignition_error = None
        return True
    except (KeyError, TypeError, AttributeError):
      pass
    try:
      all_checks = getattr(self.sm, "all_checks", None)
      if all_checks is not None and not all_checks(["pandaStates"]):
        self.ignition_error = "Physical ignition status is stale or unavailable"
        return True
      pandas = [panda for panda in self.sm["pandaStates"]
                if panda.pandaType != log.PandaState.PandaType.unknown]
      if not pandas:
        self.ignition_error = "Physical ignition status is unavailable"
        return True
      self.ignition_error = None
      return any(panda.ignitionLine or panda.ignitionCan for panda in pandas)
    except (KeyError, TypeError, AttributeError):
      self.ignition_error = "Physical ignition status is unavailable"
      return True

  def _refresh_config(self, now: float) -> None:
    if now - self.last_config_refresh < CONFIG_REFRESH_SECONDS:
      return
    self.last_config_refresh = now
    previous = self.config
    try:
      current = self.config_store.load()
      self.config_error = None
    except SentryConfigError as exc:
      current = SentryConfig()
      self.config_error = str(exc)
    if (current.motion_threshold_mps2, current.warning_persistence_seconds) != (
        previous.motion_threshold_mps2, previous.warning_persistence_seconds):
      self._close_active_episode()
      self.config = current
      self.detector = self._new_detector()
      self.arm_started_at = now if current.effective_enabled else None
    else:
      self.config = current

    if previous.effective_enabled and not current.effective_enabled:
      self._close_active_episode()
      self.detector.reset()
      self.arm_started_at = None
    elif not previous.effective_enabled and current.effective_enabled:
      self.detector.reset()
      self.arm_started_at = now

    if previous.wait_for_driver_exit != current.wait_for_driver_exit:
      self._close_active_episode()
      self._abort_capture_work("stale_capture")
      self.detector.reset()
      self.arm_started_at = now if current.effective_enabled else None
      self._reset_driver_exit()
    elif previous.effective_enabled != current.effective_enabled:
      self._reset_driver_exit()

  def _reset_driver_exit(self) -> None:
    self.driver_exit_started_at = None
    self.driver_door_open_seen = False
    self.driver_exit_completed = False

  def _driver_exit_ready(self, now: float) -> bool:
    if not self.config.wait_for_driver_exit or self.driver_exit_completed:
      return True
    if self.driver_exit_started_at is None:
      # sentryd starts offroad, so startup/enable is the earliest trustworthy
      # boundary. Never infer an exit from cached onroad carState or door data.
      self.driver_exit_started_at = now
    self.arm_started_at = None
    self.detector.reset()
    try:
      samples = self.door_source.poll(now, after=self.driver_exit_started_at)
      error = self.door_source.error
    except (OSError, RuntimeError, ValueError) as exc:
      samples = []
      error = f"Could not read driver-door CAN data: {str(exc)[:256]}"
    closed_after_open = False
    for sample in samples:
      if sample.open:
        self.driver_door_open_seen = True
        closed_after_open = False
      elif self.driver_door_open_seen:
        closed_after_open = True
    if closed_after_open:
      # Process every ordered sample before latching: open/close/open in one
      # receive batch must still wait for the final close. This is an exit
      # sequence, not proof of cabin occupancy. Later episode-cap rearming
      # retains the latch until ignition returns (or the option is changed).
      self.driver_exit_completed = True
      self.arm_started_at = self.clock()
      self.state_error = None
      return True
    self.state = ("door_signal_unavailable" if error else
                  "waiting_for_door_close" if self.driver_door_open_seen else "waiting_for_door_open")
    self.state_error = error
    return False

  def _handle_command(self) -> None:
    command = take_command(self.volatile_params)
    if command is None:
      return
    name = command.get("command")
    if name == "retry_uploads":
      try:
        count = self.store.retry_terminal()
      except (OSError, sqlite3.Error, ValueError) as exc:
        self._record_persistence_failure("Could not retry Sentry uploads", exc)
        return
      self.state = "uploads_retried"
      self.state_error = None
      self.persistence_error = None
      cloudlog.info("Requeued %d terminal Sentry uploads", count)
    elif name == "manual_test":
      if not self.config.effective_enabled:
        self.state_error = "Enable Sentry Mode and accept capture/upload consent before testing"
      elif self._is_onroad():
        self.state_error = "Sentry test is only available while parked and offroad"
      else:
        detected_at = self._utc_now()
        event_id = str(uuid4())
        try:
          self.store.begin_revision(
            event_id=event_id,
            revision=1,
            kind="warning",
            source="manual_test",
            episode_started_at=detected_at,
            detected_at=detected_at,
            message="Manual Sentry Mode test from the device.",
            schema_version=EVENT_SCHEMA_VERSION,
          )
        except (OSError, sqlite3.Error, ValueError) as exc:
          self._record_persistence_failure("Could not persist manual Sentry test", exc)
          return
        self.capture_queue.append(CaptureJob(event_id, 1))
        try:
          self.store.close_event(event_id)
        except (OSError, sqlite3.Error, ValueError) as exc:
          self._record_persistence_failure("Could not close manual Sentry test", exc)
          return
        self.persistence_error = None
        self.state = "manual_test"
        self.state_error = None

  def _process_detection(self, detection: str, now: float) -> None:
    if detection == "motion" and self.active_event_id is None:
      event_id = str(uuid4())
      first_motion = self.detector.first_motion_at if self.detector.first_motion_at is not None else now
      episode_started_at = (datetime.now(UTC) - timedelta(seconds=max(0.0, now - first_motion))).isoformat()
      detected_at = self._utc_now()
      try:
        self.store.begin_revision(
          event_id=event_id,
          revision=1,
          kind="warning",
          source="motion",
          episode_started_at=episode_started_at,
          detected_at=detected_at,
          message="Movement detected while parked.",
          schema_version=EVENT_SCHEMA_VERSION,
        )
      except (OSError, sqlite3.Error, ValueError) as exc:
        self.detector.reset()
        self.arm_started_at = now
        self._record_persistence_failure("Could not persist first Sentry capture", exc)
        return
      self.active_event_id = event_id
      self.active_episode_started_at = episode_started_at
      self.next_revision = 2
      self.next_capture_at = float("inf")
      self.last_capture_motion_at = now
      self.pending_alarm = False
      self.capture_queue.append(CaptureJob(event_id, 1))
      self.persistence_error = None
      self.state = "warning" if self.detector.warning_triggered else "motion"
      self.state_error = None
    elif detection == "warning" and self.active_event_id is not None:
      # Warning persistence is status-only. The first qualifying motion already
      # queued revision 1 (wire kind="warning" for RTZ compatibility), so this
      # transition must not create another event, capture, or webhook.
      self.state = "warning"
      self.state_error = None
    elif detection == "alarm" and self.active_event_id is not None and self.active_episode_started_at is not None:
      # Promotion changes status immediately; its capture obeys the same
      # serial/cooldown limits instead of racing an in-flight follow-up.
      self.pending_alarm = True
      self.state = "alarm"
      self.state_error = None
    elif detection == "closed":
      self._close_active_episode()
      self.state = "armed"

  def _recent_motion(self, now: float) -> bool:
    return (
      self.detector.previous_sample_at is not None
      and 0 <= now - self.detector.previous_sample_at < self.detector.sample_stale_seconds
      and self.detector.last_motion_at is not None
      and 0 <= now - self.detector.last_motion_at < REPEAT_CAPTURE_SECONDS
    )

  def _schedule_motion_capture(self, now: float) -> None:
    active = self.active_capture
    busy = bool(self.capture_queue) or (active is not None and active.job is not None)
    recent_motion = self.active_event_id is not None and self._recent_motion(now)
    if not busy and active is not None and not recent_motion and not self.pending_alarm:
      active.idle_stop.set()
    if self.active_event_id is None or self.active_episode_started_at is None or busy or now < self.next_capture_at:
      return
    if self.next_revision > 1 + MAX_FOLLOW_UP_REVISIONS:
      return
    if not self.pending_alarm and (not recent_motion or self.detector.last_motion_at <= self.last_capture_motion_at):
      return
    kind = "alarm" if self.pending_alarm else "follow_up"
    try:
      self.store.begin_revision(
        event_id=self.active_event_id,
        revision=self.next_revision,
        kind=kind,
        source="motion",
        episode_started_at=self.active_episode_started_at,
        detected_at=self._utc_now(),
        message="Sustained movement detected while parked." if self.pending_alarm else "Continued movement while parked.",
        schema_version=EVENT_SCHEMA_VERSION,
      )
    except (OSError, sqlite3.Error, ValueError) as exc:
      self.next_capture_at = now + REPEAT_CAPTURE_SECONDS
      self._record_persistence_failure("Could not persist Sentry capture", exc)
      return
    self.capture_queue.append(CaptureJob(self.active_event_id, self.next_revision))
    self.next_revision += 1
    self.last_capture_motion_at = self.detector.last_motion_at or now
    self.pending_alarm = False
    self.next_capture_at = float("inf")
    self.persistence_error = None

  def _start_capture_if_needed(self) -> None:
    if not self.capture_queue:
      return
    active = self.active_capture
    if active is not None:
      if active.job is not None:
        return
      if not active.thread.is_alive():
        self.active_capture = None
      elif active.idle_stop.is_set() or active.requests is None:
        return
      elif self.capture_queue[0].abort_reason is None:
        active.request_started.clear()
        active.job = self.capture_queue.pop(0)
        active.requests.put(True)
        return
    job = self.capture_queue[0]
    if job.abort_reason is not None:
      if self.clock() < job.next_finalize_at:
        return
      try:
        self.store.finish_capture(
          job.event_id, job.revision, {}, {"wide": job.abort_reason, "cabin": job.abort_reason})
      except (OSError, sqlite3.Error, ValueError) as exc:
        if self._revision_is_finalized(job):
          self.capture_queue.pop(0)
        else:
          job.finalize_attempts += 1
          job.next_finalize_at = self.clock() + min(2 ** min(job.finalize_attempts, 6), 60)
        self._record_persistence_failure("Could not finalize aborted Sentry capture", exc)
        return
      self.capture_queue.pop(0)
      self.persistence_error = None
      return
    self.capture_abort_event.clear()
    self.capture_abort_reason = None
    job = self.capture_queue.pop(0)
    result: list[CaptureResult] = []
    requests: queue.SimpleQueue[bool] = queue.SimpleQueue()
    idle_stop = threading.Event()
    request_started = threading.Event()
    request_started.set()  # the first request is implicit in capture_repeated
    repeated = job.event_id == self.active_event_id and hasattr(self.capture, "capture_repeated")

    def next_capture() -> bool:
      try:
        requested = requests.get_nowait()
        if requested and active_capture.job is not None:
          cloudlog.bind(sentry_event_id=active_capture.job.event_id, sentry_revision=active_capture.job.revision)
          log_capture_diagnostic("sentry_capture_requested")
        request_started.set()
        return requested
      except queue.Empty:
        return False

    def aborted() -> bool:
      return self.stop_event.is_set() or self.capture_abort_event.is_set() or idle_stop.is_set()

    def capture_worker() -> None:
      with cloudlog.ctx(sentry_event_id=job.event_id, sentry_revision=job.revision):
        log_capture_diagnostic("sentry_capture_requested")
        try:
          if repeated:
            self.capture.capture_repeated(result.append, next_capture=next_capture, abort_callback=aborted)
          else:
            result.append(self.capture.capture(aborted))
        except Exception:
          cloudlog.exception("Sentry camera capture failed")
          if active_capture.job is not None and request_started.is_set() and not result:
            result.append(CaptureResult({}, {"wide": "capture_failed", "cabin": "capture_failed"}))

    thread = threading.Thread(target=capture_worker, name=f"sentry-capture-{job.revision}", daemon=True)
    active_capture = ActiveCapture(job, thread, result, requests=requests if repeated else None,
                                   idle_stop=idle_stop, request_started=request_started)
    self.active_capture = active_capture
    thread.start()

  def _finish_capture_if_ready(self) -> None:
    active = self.active_capture
    if active is None:
      return
    if active.job is None:
      if not active.thread.is_alive():
        self.active_capture = None
      return
    if (not active.result and active.thread.is_alive()) or self.clock() < active.next_finalize_at:
      return
    if not active.result and not active.request_started.is_set():
      # A bounded camera session may expire between is_alive() and dispatch.
      # No frame was attempted: retain the durable job for a fresh lease.
      if self.capture_abort_reason is not None:
        active.job.abort_reason = self.capture_abort_reason
      self.capture_queue.insert(0, active.job)
      self.active_capture = None
      return
    if self.capture_abort_reason is not None:
      result = CaptureResult({}, {"wide": self.capture_abort_reason, "cabin": self.capture_abort_reason})
    else:
      result = active.result[0] if active.result else CaptureResult(
        {}, {"wide": "capture_failed", "cabin": "capture_failed"})
    try:
      self.store.finish_capture(active.job.event_id, active.job.revision, result.media, result.omissions)
      self.persistence_error = None
    except Exception as exc:
      try:
        revision_state = self.store.revision_state(active.job.event_id, active.job.revision)
      except Exception:
        revision_state = None
      if revision_state in (
          "ready", "uploading", "terminal", "acknowledged",
          "evicting_ready", "evicting_terminal", "evicting_uncertain"):
        self._capture_finalized(active)
      else:
        active.finalize_attempts += 1
        active.next_finalize_at = self.clock() + min(2 ** min(active.finalize_attempts, 6), 60)
      self._record_persistence_failure("Could not finalize Sentry capture", exc)
      return
    self._capture_finalized(active)

  def _capture_finalized(self, active: ActiveCapture) -> None:
    if active.job is not None and active.job.event_id == self.active_event_id:
      if active.job.revision >= 1 + MAX_FOLLOW_UP_REVISIONS:
        # Count alarm and failed-camera revisions too, but only rearm once the
        # last result is durable. Stop the warm session without aborting its
        # already finalized images; upload retries continue during arming.
        active.idle_stop.set()
        self._close_active_episode()
        self.detector.reset()
        self.arm_started_at = self.clock()
        self.state = "arming"
        self.state_error = None
      else:
        # Start the cooldown only after a result has been durably finalized.
        # Slow encoding/storage never causes a burst of queued catch-up captures.
        self.next_capture_at = self.clock() + REPEAT_CAPTURE_SECONDS
    else:
      active.idle_stop.set()
    active.job = None
    active.result.clear()
    active.finalize_attempts = 0
    active.next_finalize_at = 0.0
    if not active.thread.is_alive():
      self.active_capture = None

  def _abort_capture_work(self, reason: str) -> None:
    self.capture_abort_event.set()
    self.capture_abort_reason = reason
    had_error = False
    processed = False
    now = self.clock()
    remaining_jobs = []
    while self.capture_queue:
      processed = True
      job = self.capture_queue.pop(0)
      if job.abort_reason is None or reason == "ignition_on":
        job.abort_reason = reason
      if now < job.next_finalize_at:
        remaining_jobs.append(job)
        continue
      try:
        self.store.finish_capture(
          job.event_id, job.revision, {}, {"wide": job.abort_reason, "cabin": job.abort_reason})
      except (OSError, sqlite3.Error, ValueError) as exc:
        had_error = True
        if not self._revision_is_finalized(job):
          job.finalize_attempts += 1
          job.next_finalize_at = now + min(2 ** min(job.finalize_attempts, 6), 60)
          remaining_jobs.append(job)
        self._record_persistence_failure("Could not abort queued Sentry capture", exc)
    self.capture_queue = remaining_jobs
    if processed and not had_error:
      self.persistence_error = None

  def _revision_is_finalized(self, job: CaptureJob) -> bool:
    try:
      return self.store.revision_state(job.event_id, job.revision) in (
        "ready", "uploading", "terminal", "acknowledged",
        "evicting_ready", "evicting_terminal", "evicting_uncertain",
      )
    except (OSError, sqlite3.Error, ValueError):
      return False

  def _start_upload_if_needed(self) -> None:
    now = self.clock()
    if self.upload_thread is not None and self.upload_thread.is_alive():
      return
    if now - self.last_upload_started < UPLOAD_POLL_SECONDS:
      return
    self.last_upload_started = now
    try:
      # acknowledge() commits delivery before deleting local bytes. Retry that
      # idempotent cleanup during the same daemon session, and do it before any
      # quota-sensitive/upload work so acknowledged bytes cannot displace an
      # unrelated pending bundle after a transient filesystem failure.
      self.store.cleanup_acknowledged_media()
      self.store.recover_interrupted_evictions()
      if not self.store.upload_work_due(datetime.now(UTC).timestamp()):
        return
    except (OSError, sqlite3.Error, ValueError) as exc:
      self._record_persistence_failure("Could not inspect Sentry upload queue", exc)
      return
    try:
      dongle_id = self.params.get("DongleId")
    except (OSError, RuntimeError):
      return
    if not isinstance(dongle_id, str) or not dongle_id:
      return
    store_path = self.store.path
    media_quota = self.store.media_quota_bytes

    def upload_worker() -> None:
      worker_store = None
      try:
        worker_store = SentryStore(store_path, media_quota_bytes=media_quota, run_maintenance=False)
        SentryUploader(dongle_id, worker_store).upload_once()
      except Exception:
        cloudlog.exception("Unexpected Sentry uploader failure")
      finally:
        if worker_store is not None:
          worker_store.close()

    self.upload_thread = threading.Thread(target=upload_worker, name="sentry-uploader", daemon=True)
    self.upload_thread.start()

  def _close_active_episode(self) -> None:
    event_id = self.active_event_id
    self.active_event_id = None
    self.active_episode_started_at = None
    self.pending_alarm = False
    self.next_capture_at = float("inf")
    if self.active_capture is not None and self.active_capture.job is None:
      self.active_capture.idle_stop.set()
    if event_id is not None:
      try:
        self.store.close_event(event_id)
        self.persistence_error = None
      except (OSError, sqlite3.Error, ValueError) as exc:
        self._record_persistence_failure("Could not close Sentry episode", exc)

  def _write_status(self, now: float, *, force: bool = False) -> None:
    if not force and now - self.last_status_refresh < STATUS_REFRESH_SECONDS:
      return
    self.last_status_refresh = now
    try:
      stats = self.store.stats()
    except (OSError, sqlite3.Error, ValueError) as exc:
      self._record_persistence_failure("Could not read Sentry outbox status", exc)
      stats = None
    status: dict[str, object] = {
      "state": self.state,
      "enabled": self.config.effective_enabled,
      "pending": stats.pending if stats is not None else 0,
      "terminal": stats.terminal if stats is not None else 0,
      "media_bytes": stats.media_bytes if stats is not None else 0,
      "accelerometer": self.accelerometer_status,
      "driver_exit": {
        "required": self.config.wait_for_driver_exit,
        "door_open_seen": self.driver_door_open_seen,
        "completed": self.driver_exit_completed,
      },
    }
    error = self.config_error or self.persistence_error or self.runtime_error or self.state_error
    if error:
      status["error"] = error[:512]
    if (status == self.last_status and not force and
        now - self.last_status_published_at < STATUS_HEARTBEAT_SECONDS):
      return
    status["updated_at"] = self._utc_now()
    try:
      set_status(status, self.volatile_params)
      self.last_status = {key: value for key, value in status.items() if key != "updated_at"}
      self.last_status_published_at = now
    except (OSError, RuntimeError):
      cloudlog.exception("Could not publish Sentry runtime status")

  def _accelerometer_error(self, now: float) -> str | None:
    """Validate real sample freshness without treating slow consumption as a failed IMU."""
    self.accelerometer_status = {}
    try:
      self.accelerometer_status = {
        "seen": self.sm.seen["accelerometer"],
        "valid": self.sm.valid["accelerometer"],
        "alive": self.sm.alive["accelerometer"],
        "frequency_ok": self.sm.freq_ok["accelerometer"],
      }
      if not self.accelerometer_status["seen"]:
        return "No accelerometer samples received. Check that sensord is running."
      if not self.accelerometer_status["valid"]:
        return "Accelerometer is reporting invalid samples."

      received_at = self.sm.recv_time["accelerometer"]
      published_ns = self.sm.logMonoTime["accelerometer"]
      if (type(received_at) not in (int, float) or not math.isfinite(received_at) or received_at <= 0 or
          type(published_ns) is not int or published_ns <= 0 or not math.isfinite(now)):
        return "Accelerometer sample timing is invalid."
      receive_age = now - received_at
      sample_age = now - published_ns / 1e9
      self.accelerometer_status.update({
        "receive_age_seconds": round(receive_age, 3),
        "sample_age_seconds": round(sample_age, 3),
      })
      if receive_age < 0 or sample_age < 0:
        return "Accelerometer sample timing is invalid (timestamp is in the future)."
      if receive_age >= self.detector.sample_stale_seconds:
        return f"Accelerometer samples are stale (last received {receive_age:.1f}s ago). Check sensord."
      if sample_age >= self.detector.sample_stale_seconds:
        return f"Accelerometer samples are stale (published {sample_age:.1f}s ago). Check sensord."
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
      return "Accelerometer sample status is unavailable."
    return None

  def update(self) -> None:
    self.sm.update(0)
    # SubMaster stamps receive times inside update; sample age must use a clock
    # read after it returns or a newly received sample can appear to be future-dated.
    now = self.clock()
    # Physical ignition wins over all filesystem/configuration work. In
    # particular, do not let a contended config flock delay capture abortion.
    onroad = self._is_onroad()
    if onroad:
      self.capture_abort_event.set()
      self.capture_abort_reason = "ignition_on"
    self._refresh_config(now)
    onroad = onroad or self._is_onroad()
    enabled = self.config.effective_enabled and not onroad
    if enabled != self.last_runtime_enabled or now - self.last_runtime_check >= CONFIG_REFRESH_SECONDS:
      self.last_runtime_check = now
      try:
        if enabled != self.last_runtime_enabled or runtime_enabled(self.volatile_params) != enabled:
          set_runtime_enabled(enabled, self.volatile_params)
        self.last_runtime_enabled = enabled
        self.runtime_error = None
      except (OSError, RuntimeError):
        self.runtime_error = "Could not request the Sentry sensor process. Check volatile runtime storage."
        cloudlog.exception("Could not publish Sentry sensor demand")

    self._handle_command()
    if onroad:
      self._abort_capture_work("ignition_on")
    elif self.config_error or not self.config.effective_enabled:
      self._abort_capture_work("stale_capture")
    self._finish_capture_if_ready()
    self._start_upload_if_needed()

    if onroad:
      self.state = "disabled"
      self.state_error = self.ignition_error
      self._close_active_episode()
      self.detector.reset()
      self.arm_started_at = None
      self._reset_driver_exit()
      self._write_status(now)
      return
    if self.config_error:
      self.state = "configuration_error"
      self._write_status(now)
      return
    if not self.config.effective_enabled:
      self.state = "disabled"
      self.state_error = None
      self._write_status(now)
      return

    if self.active_capture is None:
      self.capture_abort_event.clear()
    self._start_capture_if_needed()

    # Configuration and outbox work can take time; don't accept a sample that
    # became stale while that work ran.
    now = self.clock()
    if not self._driver_exit_ready(now):
      self._write_status(now)
      return
    now = self.clock()
    if self.arm_started_at is None:
      self.arm_started_at = now
    elapsed = now - self.arm_started_at
    accelerometer_error = self._accelerometer_error(now)
    if elapsed < ARM_DELAY_SECONDS:
      self.state = "arming"
      self.state_error = None
      self._write_status(now)
      return

    if self.active_event_id is not None and self.next_revision > 1 + MAX_FOLLOW_UP_REVISIONS:
      # The final allowed revision is queued, capturing, or awaiting a durable
      # write. Do not let a quiet/sensor gap close it and open another episode
      # before finalization starts the full arming period.
      self._write_status(now)
      return

    if self.state not in ("motion", "warning", "alarm"):
      if self.detector.episode_active:
        self.state = ("alarm" if self.detector.alarm_triggered else
                      "warning" if self.detector.warning_triggered else "motion")
      else:
        self.state = "armed"
      self.state_error = None
    timeout_detection = self.detector.tick(now)
    if timeout_detection is not None:
      self._process_detection(timeout_detection, now)
    if accelerometer_error is not None:
      self.detector.invalidate_samples()
      if self.active_capture is not None and self.active_capture.job is None:
        self.active_capture.idle_stop.set()
      self.state = "sensor_unavailable"
      self.state_error = accelerometer_error
      self._write_status(now)
      return
    if self.sm.updated.get("accelerometer", False):
      try:
        acceleration = self.sm["accelerometer"].acceleration.v
        detection = self.detector.update(acceleration, now)
        if detection is not None:
          self._process_detection(detection, now)
      except (AttributeError, TypeError, ValueError):
        self.detector.invalidate_samples()
        self.state = "sensor_unavailable"
        self.state_error = "Accelerometer samples are unavailable"
    self._schedule_motion_capture(now)
    self._write_status(now)

  def run(self, stop_event: threading.Event | None = None) -> None:
    if stop_event is not None:
      self.stop_event = stop_event
    clear_runtime(self.volatile_params)
    self._write_status(self.clock(), force=True)
    next_update = self.clock() + LOOP_INTERVAL_SECONDS
    try:
      while not self.stop_event.wait(max(0.0, next_update - self.clock())):
        # Include work in the 100 ms period instead of sleeping another 100 ms
        # after it. Anchor to this start so an overrun cannot create catch-up bursts.
        next_update = self.clock() + LOOP_INTERVAL_SECONDS
        try:
          self.update()
        except (OSError, sqlite3.Error) as exc:
          self._record_persistence_failure("Sentry storage operation failed", exc)
          self.detector.reset()
          self.arm_started_at = self.clock()
          self._write_status(self.clock(), force=True)
    finally:
      self.stop_event.set()
      self.capture_abort_event.set()
      if self.active_capture is not None:
        self.active_capture.thread.join(timeout=2.0)
        self._finish_capture_if_ready()
      self._close_active_episode()
      try:
        if self.last_runtime_enabled is not False:
          set_runtime_enabled(False, self.volatile_params)
      except (OSError, RuntimeError):
        pass
      clear_runtime(self.volatile_params)
      try:
        self.store.close()
      except sqlite3.Error:
        pass

  @staticmethod
  def _persistence_error_message(context: str, exc: Exception) -> str:
    detail = str(exc).replace("\x00", "")[:384]
    return f"{context}: {detail}"[:512]

  def _record_persistence_failure(self, context: str, exc: Exception) -> None:
    self.state = "storage_error"
    self.persistence_error = self._persistence_error_message(context, exc)
    cloudlog.exception(context)

  @staticmethod
  def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
  stop_event = threading.Event()
  signal.signal(signal.SIGINT, lambda *_: stop_event.set())
  signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
  # Clear a stale sensor/camera demand before any fallible config or DB setup.
  volatile_params = runtime_params()
  clear_runtime(volatile_params)
  SentryMode(volatile_params=volatile_params).run(stop_event)


if __name__ == "__main__":
  main()
