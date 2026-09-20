"""Versioned snapshots and storage; no CAN, Params or network imports at load time."""
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

DURABLE_KEY = "VehicleTelemetryState"
METRICS = {"fuel_percent": 100, "fuel_liters": 10000, "odometer_km": 1e8}
MAX_ORDER = (1 << 53) - 1


def utc_timestamp(seconds: float) -> str:
  return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp_seconds(value) -> float:
  if not isinstance(value, str):
    raise ValueError("missing timestamp")
  parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
  if parsed.tzinfo is None:
    raise ValueError("timestamp must have a timezone")
  return parsed.timestamp()


def valid_snapshot(value) -> bool:
  try:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1 or value.get("firmware_supported") is not True:
      return False
    for key in ("collector_epoch", "snapshot_seq"):
      if type(value.get(key)) is not int or not 0 < value[key] <= MAX_ORDER:
        return False
    if type(value.get("vehicle_supported")) is not bool or type(value.get("onroad")) is not bool:
      return False
    if not all(isinstance(value.get(key), str) and 0 < len(value[key]) <= 256 for key in ("vehicle_id", "vehicle_fingerprint")):
      return False
    generated = timestamp_seconds(value["generated_at"])
    if set(value["metrics"]) != set(METRICS):
      return False
    for key, maximum in METRICS.items():
      metric = value["metrics"][key]
      if type(metric.get("supported")) is not bool:
        return False
      if metric["supported"] and not value["vehicle_supported"]:
        return False
      if "value" not in metric or (not metric["supported"] and metric["value"] is not None):
        return False
      if metric["value"] is None:
        if "observed_at" in metric or "source" in metric:
          return False
        continue
      if type(metric["value"]) not in (int, float):
        return False
      if not math.isfinite(metric["value"]) or not 0 <= metric["value"] <= maximum:
        return False
      if timestamp_seconds(metric["observed_at"]) > generated:
        return False
      if not isinstance(metric.get("source"), str) or not metric["source"] or len(metric["source"]) > 256:
        return False
    return True
  except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
    return False


def vehicle_identity(CP, trip_id: str) -> str:
  vin = CP.carVin.upper()
  if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) and vin != "0" * 17:
    return "vin:" + hashlib.sha256(f"{vin}:{CP.carFingerprint}".encode()).hexdigest()
  return "trip:" + trip_id


def valid_durable(stored) -> bool:
  if not isinstance(stored, dict) or type(stored.get("collector_epoch")) is not int or not 0 < stored["collector_epoch"] < MAX_ORDER:
    return False
  return all(stored.get(key) is None or (valid_snapshot(stored[key]) and stored[key]["collector_epoch"] <= stored["collector_epoch"])
             for key in ("snapshot", "pending"))


def telemetry_storage_paths() -> tuple[str, str | None]:
  from openpilot.common.hardware import PC
  from openpilot.common.hardware.hw import Paths

  legacy_root = str(Path(Paths.persist_root()) / "vehicle_telemetry_params")
  # AGNOS mounts /persist read-only. /data belongs to the comma user and
  # survives firmware updates. Keep the existing writable location on PCs.
  return (legacy_root, None) if PC else ("/data/vehicle_telemetry_params", legacy_root)


def telemetry_params(root: str | None = None):
  from openpilot.common.params import ParamKeyType, Params, UnknownKeyName, ensure_bytes

  legacy_root = None
  if root is None:
    root, legacy_root = telemetry_storage_paths()

  class VehicleTelemetryParams(Params):
    """One private durable key, compatible with older prebuilt libparams maps.

    Never store this compatibility key in default Params: old native clearAll
    deletes unknown keys and loggerd logs them without the new DONT_LOG flag.
    """
    def check_key(self, key):
      encoded = ensure_bytes(key)
      if encoded != DURABLE_KEY.encode():
        raise UnknownKeyName(encoded)
      return encoded

    def get_type(self, key):
      self.check_key(key)
      return ParamKeyType.JSON

    def _default(self, key):
      self.check_key(key)
      return None

    def get(self, key, block=False, return_default=False):
      self.check_key(key)
      try:
        raw = Path(self.get_param_path(key)).read_bytes()
      except FileNotFoundError:
        if legacy_root is None:
          return None
        # Read only: constructing legacy Params could try to create files on
        # read-only /persist. SnapshotStore validates this bundle, then imports
        # its state and incremented epoch atomically to the writable root before
        # publishing. Athena can still read the legacy bundle as last-known.
        legacy_path = Path(legacy_root) / os.environ.get("OPENPILOT_PREFIX", "d") / DURABLE_KEY
        try:
          raw = legacy_path.read_bytes()
        except FileNotFoundError:
          return None
      # Params' normal JSON conversion logs malformed contents and returns
      # None. Neither is safe for private readings and monotonic ordering.
      try:
        value = json.loads(raw)
      except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Corrupt VehicleTelemetryState JSON; refusing to reset ordering") from exc
      if not isinstance(value, dict):
        raise ValueError("Corrupt VehicleTelemetryState JSON object; refusing to reset ordering")
      return value

  return VehicleTelemetryParams(root)


def runtime_path() -> Path:
  root = "/tmp" if sys.platform == "darwin" else "/dev/shm"
  return Path(os.environ.get("VEHICLE_TELEMETRY_RUNTIME_ROOT", root)) / "vehicle_telemetry" / "state.json"


def write_runtime(envelope: dict) -> None:
  path = runtime_path()
  path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
  fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
  try:
    with os.fdopen(fd, "w") as handle:
      json.dump(envelope, handle, allow_nan=False)
    os.replace(temporary, path)
  finally:
    if os.path.exists(temporary):
      os.unlink(temporary)


def get_vehicle_state(params=None, monotonic_now=None) -> dict:
  now = time.monotonic() if monotonic_now is None else monotonic_now
  try:
    envelope = json.loads(runtime_path().read_text())
    snapshot = envelope.get("snapshot")
    age = now - envelope["heartbeat"]
    if 0 <= age <= 5:
      if snapshot is None:
        return {"snapshot": None, "availability": "unknown"}
      if valid_snapshot(snapshot):
        availability = "unsupported" if not snapshot["vehicle_supported"] else "last_known"
        frame = envelope.get("last_frame")
        if snapshot["vehicle_supported"] and snapshot["onroad"] and type(frame) in (int, float) and 0 <= now - frame <= 10:
          availability = "current"
        return {"snapshot": snapshot, "availability": availability}
  except (OSError, ValueError, TypeError, KeyError, AttributeError):
    pass
  try:
    if params is None:
      params = telemetry_params()
    stored = params.get(DURABLE_KEY)
    if valid_durable(stored) and valid_snapshot(stored.get("snapshot")):
      return {"snapshot": stored["snapshot"], "availability": "last_known"}
  except (OSError, RuntimeError, ValueError):
    pass
  return {"snapshot": None, "availability": "unknown"}


class SnapshotStore:
  def __init__(self, params, wall_now: float):
    self.params = params
    stored = params.get(DURABLE_KEY)
    if stored is None:
      stored = {"collector_epoch": 0, "snapshot": None, "pending": None}
    elif not valid_durable(stored):
      raise ValueError("Corrupt VehicleTelemetryState; preserving it and refusing to reset snapshot ordering")
    self.epoch = max(stored["collector_epoch"] + 1, int(wall_now * 1000))
    if self.epoch > MAX_ORDER:
      raise ValueError("Vehicle telemetry epoch exhausted")
    self.seq = 0
    self.snapshot = copy.deepcopy(stored.get("snapshot"))
    self.pending = copy.deepcopy(stored.get("pending"))
    self.persisted_snapshot = copy.deepcopy(self.snapshot)
    self.persist()  # Persist the incremented epoch before anything can expose it.

  def persist(self):
    value = {"collector_epoch": self.epoch, "snapshot": self.persisted_snapshot, "pending": self.pending}
    self.params.put(DURABLE_KEY, value, block=True)
    if self.params.get(DURABLE_KEY) != value:
      raise RuntimeError("Vehicle telemetry persistence failed")

  def publish(self, identity: str, fingerprint: str, supported: bool, onroad: bool, samples: dict, wall_now: float) -> dict:
    metrics = {key: {"supported": False, "value": None} for key in METRICS}
    if supported and self.snapshot is not None and self.snapshot["vehicle_id"] == identity:
      metrics = copy.deepcopy(self.snapshot["metrics"])
      for key, metric in metrics.items():
        if metric["value"] is not None and timestamp_seconds(metric["observed_at"]) > wall_now:
          # UTC may be corrected after startup. Do not claim that an old
          # untrusted clock reading was observed after this snapshot.
          metrics[key] = {"supported": True, "value": None}
    for key, (value, observed, source) in samples.items():
      if observed <= wall_now:
        metrics[key] = {"supported": True, "value": value, "observed_at": utc_timestamp(observed), "source": source}
    self.seq += 1
    snapshot = {"version": 1, "collector_epoch": self.epoch, "snapshot_seq": self.seq,
                "generated_at": utc_timestamp(wall_now), "firmware_supported": True,
                "vehicle_supported": supported, "vehicle_id": identity, "vehicle_fingerprint": fingerprint,
                "onroad": onroad, "metrics": metrics}
    if not valid_snapshot(snapshot):
      raise ValueError("Invalid vehicle telemetry snapshot")
    self.snapshot = snapshot
    return snapshot

  def queue_latest(self):
    self.persisted_snapshot = copy.deepcopy(self.snapshot)
    self.pending = copy.deepcopy(self.snapshot)
    self.persist()

  def acknowledge(self, snapshot):
    if self.pending is not None and (self.pending["collector_epoch"], self.pending["snapshot_seq"]) == (
      snapshot["collector_epoch"], snapshot["snapshot_seq"]):
      self.pending = None
      self.persist()
