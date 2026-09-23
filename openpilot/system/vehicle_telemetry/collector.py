"""Independent passive collection hosted by Athena, never by the controls loop."""
import copy
import fcntl
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

from openpilot.system.vehicle_telemetry.clock import NANO, MessageClock, TrustedClock, boot_id, boot_ns, ntp_synchronized
from openpilot.system.vehicle_telemetry.state import METRICS, timestamp_seconds, utc_timestamp, valid_snapshot, vehicle_identity
from openpilot.system.vehicle_telemetry.store import Outbox, Staging
from openpilot.system.vehicle_telemetry.uploader import connection_recovered, upload_loop

ROOT = Path("/data/rtzs/vehicle_telemetry")
RUNTIME = Path("/dev/shm/vehicle_telemetry")


def current_segment(log_root, route):
  """Use native loggerd's actual segment lock, never elapsed wall-clock minutes."""
  if not route or "/" in route or "\\" in route:
    return None
  segments = []
  for path in Path(log_root).glob(f"{route}--*/rlog.lock"):
    suffix = path.parent.name[len(route) + 2:]
    if suffix.isdecimal():
      segments.append(int(suffix))
  return (route, max(segments)) if segments else None


def atomic_json(path, value):
  path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
  descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
  try:
    with os.fdopen(descriptor, "w") as handle:
      json.dump(value, handle, allow_nan=False)
    os.replace(temporary, path)
  finally:
    if os.path.exists(temporary):
      os.unlink(temporary)


def get_vehicle_state():
  try:
    envelope = json.loads((RUNTIME / "state.json").read_text())
    if envelope["boot_id"] == boot_id() and 0 <= boot_ns() - envelope["heartbeat"] <= 5 * NANO:
      snapshot = envelope["snapshot"]
      if snapshot is None or valid_snapshot(snapshot):
        return {"snapshot": snapshot, "availability": envelope["availability"]}
  except (OSError, ValueError, KeyError, TypeError):
    pass
  try:
    db = sqlite3.connect(f"file:{ROOT / 'outbox.sqlite3'}?mode=ro", uri=True, timeout=1)
    try:
      row = db.execute("SELECT value FROM metadata WHERE key='latest'").fetchone()
      snapshot = json.loads(row[0]) if row else None
      if valid_snapshot(snapshot):
        return {"snapshot": snapshot, "availability": "last_known" if snapshot["vehicle_supported"] else "unsupported"}
      invalidated = db.execute("SELECT value FROM metadata WHERE key='invalidated'").fetchone()
      if invalidated is not None and json.loads(invalidated[0]) is True:
        return {"snapshot": None, "availability": "unsupported"}
    finally:
      db.close()
  except (sqlite3.Error, ValueError, OSError):
    pass
  return {"snapshot": None, "availability": "unknown"}


class Collector:
  """Testable state machine: all incoming measurements use BOOTTIME nanoseconds."""
  def __init__(self, outbox, staging, clock, boot):
    self.outbox, self.staging, self.clock, self.boot = outbox, staging, clock, boot
    self.outbox.new_epoch()
    self.identity = self.fingerprint = None
    self.supported = False
    self.samples = {}
    self.active = None
    self.last_checkpoint = 0
    self.latest = outbox.meta("latest")
    self.invalidated = outbox.meta("invalidated", False)
    self.trip_id = str(uuid.uuid4())

  def select_vehicle(self, identity, fingerprint, supported):
    previous_identity = self.identity is not None and identity != self.identity
    incompatible_cache = self.latest is not None and self.latest["vehicle_id"] != identity
    if not supported or incompatible_cache or (self.latest is None and previous_identity):
      self.latest = None
      self.invalidated = True
      self.outbox.set_meta("latest", None)
      self.outbox.set_meta("invalidated", True)
    if not supported:
      self.samples = {}
    if identity != self.identity:
      self.samples = {}
      if self.latest is not None and self.latest["vehicle_id"] != identity:
        self.latest = None
      if supported and self.latest is not None:
        for key, metric in self.latest["metrics"].items():
          if metric["supported"] and metric["value"] is not None:
            # A prior-boot measurement already has trustworthy UTC, but has
            # no timestamp in this boot's elapsed clock. Never fabricate one.
            self.samples[key] = (metric["value"], None, metric["source"], metric["observed_at"])
      if self.active is not None and self.active["identity"] != identity:
        # One immutable record per segment; discard an unfinished sample for
        # the previous vehicle rather than attach it to the newly selected car.
        self.staging.remove(self.active["record_id"])
        self.active = None
    self.identity, self.fingerprint, self.supported = identity, fingerprint, supported

  def select_segment(self, segment, now):
    active_key = (self.active["route"], self.active["segment"]) if self.active else None
    if self.active is not None and segment == active_key:
      return
    if self.active is not None:
      self.checkpoint(now, final=True, onroad=segment is not None)
      self.active = None
    # Recover checkpoints after a worker restart. An obsolete unfinished
    # segment becomes a final record using its last checkpoint's own time.
    for record, ready in self.staging.records():
      if not ready:
        if segment == (record["route"], record["segment"]) and record["identity"] == self.identity:
          self.active = record
          self.samples = record["samples"]
          self.trip_id = record["trip_id"]
        else:
          self.staging.save(record, ready=True)
    if segment is not None and self.active is None and self.identity is not None:
      route, number = segment
      record_id = str(uuid.uuid5(uuid.UUID(self.boot), f"{route}/{number}"))
      self.active = {"record_id": record_id, "route": route, "segment": number, "boot_id": self.boot,
                     "identity": self.identity, "fingerprint": self.fingerprint, "supported": self.supported, "trip_id": self.trip_id}
      self.checkpoint(now)
    self.flush()
    self.outbox.recover_checkpoints(segment, {record["record_id"] for record, _ in self.staging.records()})

  def checkpoint(self, now, final=False, onroad=True):
    if self.active is None:
      return
    self.resolve_samples(self.samples)
    self.active.update(samples=copy.deepcopy(self.samples), generated_boot=now,
                       order=self.outbox.next_order(), onroad=onroad)
    self.active.pop("generated_at", None)
    self.staging.save(self.active, ready=final)
    if self.clock.anchor is not None:
      self.outbox.checkpoint(self.active["record_id"], self.payload(self.active))
    self.last_checkpoint = now

  def payload(self, record):
    self.resolve_samples(record["samples"])
    record.setdefault("generated_at", self.clock.utc(record["generated_boot"]))
    snapshot = self.snapshot(record["identity"], record["fingerprint"], record["supported"], record["samples"],
                             record["generated_boot"], record["order"], record["onroad"], record["generated_at"])
    return {"version": 1, "route": record["route"], "segment": record["segment"],
            "boot_id": self.boot, "clock_source": self.clock.anchor[0], "snapshot": snapshot}

  def update_samples(self, updates):
    for key, sample in updates.items():
      previous = self.samples.get(key)
      if previous is None or tuple(previous[:3]) != tuple(sample[:3]):
        self.samples[key] = sample

  def resolve_samples(self, samples):
    if self.clock.anchor is not None:
      for key, sample in samples.items():
        if len(sample) == 3:
          samples[key] = (*sample, utc_timestamp(self.clock.utc(int(sample[1]))))

  def snapshot(self, identity, fingerprint, supported, samples, generated, order, onroad, generated_utc=None):
    self.resolve_samples(samples)
    if generated_utc is None:
      generated_utc = self.clock.utc(generated)
    metrics = {name: {"supported": False, "value": None} for name in METRICS}
    for name, (value, measured, source, observed) in samples.items():
      if supported and (measured is None or measured <= generated):
        metrics[name] = {"supported": True, "value": value, "observed_at": observed, "source": source}
        # Small anchor refinements must not move an existing measurement or
        # make its containing snapshot appear earlier than that measurement.
        generated_utc = max(generated_utc, timestamp_seconds(observed))
    result = {"version": 1, "collector_epoch": order[0], "snapshot_seq": order[1],
              "generated_at": utc_timestamp(generated_utc), "firmware_supported": True,
              "vehicle_supported": supported, "vehicle_id": identity, "vehicle_fingerprint": fingerprint,
              "onroad": onroad, "metrics": metrics}
    if not valid_snapshot(result):
      raise ValueError("Invalid corrected telemetry snapshot")
    return result

  def flush(self):
    if self.clock.anchor is None:
      return
    for record, ready in self.staging.records():
      if not ready:
        continue
      # The durable insert is committed before deleting the volatile checkpoint.
      # If interrupted between them, never resolve the same ID a second time.
      exists = self.outbox.db.execute("SELECT 1 FROM records WHERE id=?", (record["record_id"],)).fetchone()
      if exists is None:
        payload = self.payload(record)
        self.staging.save(record, ready=True)
        self.outbox.enqueue(record["record_id"], payload)
      self.staging.remove(record["record_id"])

  def publish(self, now, onroad):
    if self.clock.anchor is not None and self.identity is not None:
      self.latest = self.snapshot(self.identity, self.fingerprint, self.supported, self.samples, now, self.outbox.next_order(), onroad)
      if self.invalidated:
        self.invalidated = False
        self.outbox.set_meta("invalidated", False)
    elif self.latest is not None and self.identity is not None and self.latest["vehicle_id"] != self.identity:
      self.latest = None
    availability = ("unsupported" if self.invalidated else "unknown") if self.latest is None else "last_known"
    if self.latest is not None:
      if not self.latest["vehicle_supported"]:
        availability = "unsupported"
      elif self.clock.anchor is not None and onroad and any(sample[1] is not None and 0 <= now - sample[1] <= 10 * NANO
                                                          for sample in self.samples.values()):
        availability = "current"
    return {"snapshot": self.latest, "availability": availability, "boot_id": self.boot, "heartbeat": now}


def collect_loop(stopped, outbox_path=ROOT / "outbox.sqlite3", runtime=RUNTIME):
  import openpilot.cereal.messaging as messaging
  from opendbc.car.structs import car
  from openpilot.common.params import Params
  from openpilot.common.hardware.hw import Paths
  from openpilot.common.gps import get_gps_location_service
  from openpilot.system.vehicle_telemetry.adapters import adapter_for

  boot = boot_id()
  outbox = Outbox(outbox_path, boot)
  staging = Staging(runtime / "staging.sqlite3", boot)
  collector = Collector(outbox, staging, TrustedClock(), boot)
  params = Params()
  can_socket = messaging.sub_sock("can", timeout=0)
  gps_service = get_gps_location_service(params)
  gps_sockets = [(gps_service, messaging.sub_sock(gps_service, timeout=0))]
  message_clock = MessageClock()
  before = time.monotonic_ns()
  baseline_boot = boot_ns()
  message_clock.normalize(0, before, baseline_boot, time.monotonic_ns())
  current_cp, adapter = None, None
  restored_segment = None
  was_onroad = False
  initialized = False
  next_ntp = next_publish = 0
  first_sample = True
  try:
    while not stopped.is_set():
      now = boot_ns()
      collector.clock.check_wall(now, time.time_ns())
      was_trusted = collector.clock.anchor is not None
      onroad = params.get("IsOffroad") is False
      if not onroad and (was_onroad or not initialized):
        collector.select_segment(None, now)
        collector.trip_id = str(uuid.uuid4())
        current_cp, adapter = None, None
        restored_segment = None
      was_onroad = onroad
      initialized = True
      segment = current_segment(Paths.log_root(), params.get("CurrentRoute")) if onroad else None
      cp = params.get("CarParams") if onroad else None
      restore_segment = segment is not None and restored_segment is None
      if cp and (cp != current_cp or restore_segment) and params.get("IsOffroad") is False:
        if restore_segment:
          for record, ready in staging.records():
            if not ready and segment == (record["route"], record["segment"]):
              collector.trip_id = record["trip_id"]
          restored_segment = segment
        with car.CarParams.from_bytes(cp) as CP:
          identity = vehicle_identity(CP, collector.trip_id)
          adapter = adapter_for(CP)
          collector.select_vehicle(identity, CP.carFingerprint or "unknown", adapter is not None)
        current_cp = cp
        first_sample = True

      if onroad and current_cp is not None:
        # A brief absence during rotation must not finalize/reopen the same
        # immutable segment; offroad finalizes the last segment explicitly.
        if segment is not None:
          collector.select_segment(segment, now)
      packets = [(msg.logMonoTime, [(frame.address, frame.dat, frame.src) for frame in msg.can])
                 for msg in messaging.drain_sock(can_socket)]
      if adapter is not None and onroad:
        received_now = boot_ns()
        updates = adapter.update(packets, received_now / NANO, received_now / NANO)
        collector.update_samples({key: (value, int(measured * NANO), source) for key, (value, measured, source) in updates.items()})

      for name, sock in gps_sockets:
        for message in messaging.drain_sock(sock):
          before = time.monotonic_ns()
          gps_boot = boot_ns()
          after = time.monotonic_ns()
          gps = getattr(message, name)
          measured = message_clock.normalize(message.logMonoTime, before, gps_boot, after)
          if measured is not None and message.valid and gps.hasFix:
            collector.clock.observe("gnss", measured, int(gps.unixTimestampMillis * 1_000_000), gps_boot)
      now = boot_ns()
      if now >= next_ntp:
        if ntp_synchronized():
          probe_boot = boot_ns()
          collector.clock.observe("ntp", probe_boot, time.time_ns(), probe_boot)
        next_ntp = now + 10 * NANO
      if not was_trusted and collector.clock.anchor is not None:
        collector.checkpoint(now)
        connection_recovered()
      if (first_sample and collector.samples) or now - collector.last_checkpoint >= 30 * NANO:
        collector.checkpoint(now)
        if collector.samples:
          first_sample = False
      collector.flush()
      if now >= next_publish:
        envelope = collector.publish(now, onroad)
        atomic_json(runtime / "state.json", envelope)
        if envelope["snapshot"] is not None and (not onroad or now - collector.last_checkpoint < NANO):
          outbox.save_latest(envelope["snapshot"])
        next_publish = now + NANO
      stopped.wait(0.1)
  finally:
    # A worker exception must not finalize an ongoing segment; resume the
    # checkpoint after restart. Athena shutdown does not imply logger shutdown.
    collector.checkpoint(boot_ns())
    staging.db.close()
    outbox.close()


def start_workers(stopped):
  """Only primary Athena calls this; failures never escape into its RPC loop."""
  def run():
    from openpilot.common.swaglog import cloudlog
    while not stopped.is_set():
      lock = None
      try:
        RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = (RUNTIME / "collector.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        collect_loop(stopped)
      except Exception:
        cloudlog.exception("vehicle telemetry collection failed; retrying independently of Athena")
        stopped.wait(5)
      finally:
        if lock is not None:
          lock.close()
  collector = threading.Thread(target=run, name="vehicle_telemetry_collect", daemon=True)
  uploader = threading.Thread(target=upload_loop, args=(ROOT / "outbox.sqlite3", boot_id(), stopped), name="vehicle_telemetry_upload", daemon=True)
  collector.start()
  uploader.start()
  return collector, uploader
