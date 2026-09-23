"""Wire snapshot validation shared by collection, persistence and Athena."""
import hashlib
import math
import re
from datetime import UTC, datetime

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
