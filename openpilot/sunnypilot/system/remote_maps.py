"""Typed Athena adapter for the same map Params requests used by the native UI.

Receipts live only in Athena. The existing map manager and mapd remain the sole
workers; their flags do not provide an execution acknowledgment or job identity.
"""
from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from openpilot.sunnypilot.system.remote_settings import _live_state
from openpilot.sunnypilot.system.remote_workflows import BackgroundSizeCache, RequestState, WorkflowBusy, utc_now


ACTIVE = {"pending", "running"}
ACTIONS = {"download", "check_updates", "delete"}


def _selection(params) -> dict:
  country = params.get("OsmLocationName") or None
  country = country if isinstance(country, str) and len(country) <= 128 else None
  state = (params.get("OsmStateName", return_default=True) or "All") if country == "US" else None
  state = state if isinstance(state, str) and len(state) <= 128 else None
  return {"country": country, "state": state}


def _valid_selection(value) -> bool:
  return (isinstance(value, dict) and set(value) == {"country", "state"} and
          all(v is None or (isinstance(v, str) and 0 < len(v) <= 128) for v in value.values()))


def _valid_region(region: dict) -> bool:
  # RTZS validates catalog membership; the device accepts only the native ID
  # format and never fetches or maintains a second region catalog.
  def code(value):
    return isinstance(value, str) and len(value) == 2 and all("A" <= char <= "Z" for char in value)

  return (code(region["country"]) and
          (region["state"] == "All" or code(region["state"]) if region["country"] == "US" else region["state"] is None))


def _valid_title(value) -> bool:
  return isinstance(value, str) and 1 <= len(value) <= 256


def _mapd_version(params) -> str:
  value = params.get("MapdVersion")
  return value if isinstance(value, str) else ""


def _targets(region: dict) -> dict:
  if region["country"] == "US" and region["state"] != "All":
    return {"nations": [], "states": [region["state"]]}
  return {"nations": [region["country"]], "states": []}


def _object(value) -> dict:
  return value if isinstance(value, dict) else {}


def _busy(params, mem_params) -> bool:
  return bool(mem_params.get("OSMDownloadLocations") or mem_params.get("OSMDownloadBounds") or
              params.get("OsmDbUpdatesCheck") or params.get("Mapd_ClearCache"))


def _progress(params, targets: dict) -> dict | None:
  progress = _object(params.get("OSMDownloadProgress"))
  expected = targets["nations"] + targets["states"]
  locations = progress.get("locations_to_download")
  if not isinstance(locations, list) or any(not isinstance(x, str) for x in locations) or sorted(locations) != sorted(expected):
    return None
  done, total = progress.get("downloaded_files"), progress.get("total_files")
  if type(done) is int and type(total) is int and 0 <= done <= total and total > 0:
    return {"downloaded": done, "total": total}
  return None


def _public_operation(operation) -> dict | None:
  if not isinstance(operation, dict) or not operation.get("id"):
    return None
  return {key: operation[key] for key in ("id", "action", "status", "message", "region", "progress") if key in operation}


class RemoteMaps:
  def __init__(self, params, mem_params, store: RequestState | None = None, size_reader=lambda: None, state=_live_state):
    self.params, self.mem_params = params, mem_params
    self.store = store if store is not None else RequestState()
    self.size_reader, self.state = size_reader, state

  def _observe(self, record: dict) -> dict:
    operation = record.get("operation") or {}
    raw_active = self.mem_params.get("OSMDownloadLocations")
    active = _object(raw_active)
    bounds = bool(self.mem_params.get("OSMDownloadBounds"))
    update = bool(self.params.get("OsmDbUpdatesCheck"))
    delete = bool(self.params.get("Mapd_ClearCache"))
    visible = bool(raw_active or bounds or update or delete)
    new_activity = visible and (not operation or operation.get("status") not in ACTIVE and record.get("activity_visible") is False)
    record["activity_visible"] = visible
    if new_activity:
      # No Athena receipt survived for this work. Existing flags cannot establish
      # its provenance, and previously stored progress may belong to an older job.
      operation = {"id": str(uuid.uuid4()), "action": "delete" if delete else "download" if raw_active or bounds else "check_updates",
                   "status": "unknown", "region": _selection(self.params),
                   "message": "A map request is visible on the device; its outcome cannot be correlated with an Athena receipt."}
      record["operation"] = operation
    if operation.get("status") not in ACTIVE:
      return record
    if _mapd_version(self.params) != record.get("mapd_version"):
      operation.update(status="unknown", message="The mapd version changed; the request outcome is unknown.")
    elif operation["action"] == "delete":
      if not delete:
        operation.update(status="unknown", message="The delete request is no longer pending. The native worker does not confirm deletion success.")
    elif _selection(self.params) != operation["region"] or delete or bounds or (raw_active and active != record["targets"]):
      operation.pop("progress", None)
      operation.update(status="unknown", message="Map state changed outside this request; its outcome is unknown.")
    else:
      progress = _progress(self.params, record["targets"])
      fresh_progress = progress is not None and (record.get("fresh_progress") or progress != record.get("baseline_progress"))
      if raw_active:
        record["saw_marker"] = True
        operation.update(status="running", message="A matching map download is active on the device.")
        if fresh_progress:
          record["fresh_progress"] = True
          operation["progress"] = progress
      elif not update:
        if record.get("saw_marker") and fresh_progress:
          complete = progress["downloaded"] == progress["total"]
          operation.update(status="completed" if complete else "incomplete", progress=progress,
                           message="Mapd reported all files downloaded." if complete else "Mapd stopped with some files not downloaded.")
        else:
          operation.pop("progress", None)
          operation.update(status="unknown", message="The request is no longer pending, but fresh download completion could not be confirmed.")
    return record

  def _snapshot(self, record: dict, live, busy_reason="") -> dict:
    action_reason = busy_reason
    if not action_reason and (live.started is not False or self.params.get("IsOffroad") is not True):
      action_reason = "Available only while fresh device state confirms the device is offroad."
    if not action_reason and _busy(self.params, self.mem_params):
      action_reason = "A map request or download is already visible on the device."
    selection = _selection(self.params)
    actions = {}
    for action in sorted(ACTIONS):
      disabled = action_reason
      if action == "check_updates" and not disabled and not _valid_region(selection):
        disabled = "Select a country and, for the United States, a state first."
      actions[action] = {"allowed": not disabled, **({"reason": disabled} if disabled else {})}
    last_checked = None
    try:
      timestamp = float(self.params.get("OsmDownloadedDate") or 0)
      if timestamp > 0:
        last_checked = datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
      pass
    return {"version": 1, "observed_at": utc_now(), "available": True,
            "actions": actions, "operation": _public_operation(record.get("operation")), "selection": selection,
            "mapd_version": _mapd_version(self.params),
            "cache_bytes": self.size_reader(), "last_checked": last_checked}

  def snapshot(self) -> dict:
    live = self.state()
    try:
      with self.store.locked():
        record = self._observe(self.store.read())
        self.store.write(record)
        return self._snapshot(record, live)
    except WorkflowBusy:
      return self._snapshot(self.store.read(), live, "Another remote map request is being submitted.")

  def manage(self, action, **kwargs) -> dict:
    def result(status, message, record=None, live=None):
      snapshot = self.snapshot() if record is None else self._snapshot(record, live)
      return {"status": status, "message": message, "snapshot": snapshot}

    fields = {"expected_selection", "expected_operation_id"}
    if action == "download":
      fields |= {"country", "state", "country_title", "state_title"}
    elif action == "delete":
      fields.add("confirmed")
    if not isinstance(action, str) or action not in ACTIONS or set(kwargs) != fields:
      return result("rejected", "Invalid map operation arguments.")
    expected, expected_id = kwargs["expected_selection"], kwargs["expected_operation_id"]
    if not _valid_selection(expected) or not (expected_id is None or isinstance(expected_id, str) and 0 < len(expected_id) <= 128):
      return result("rejected", "Invalid expected map state.")
    if action == "delete" and kwargs["confirmed"] is not True:
      return result("rejected", "Explicit confirmation is required to delete all downloaded maps.")
    region = {"country": kwargs.get("country"), "state": kwargs.get("state")} if action == "download" else expected
    if action != "delete" and not _valid_region(region):
      return result("rejected", "Invalid country or state identifier.")
    if action == "download" and (not _valid_title(kwargs["country_title"]) or
                                 (not _valid_title(kwargs["state_title"]) if region["country"] == "US" else kwargs["state_title"] is not None)):
      return result("rejected", "Invalid country or state title.")
    try:
      with self.store.locked():
        live = self.state()  # Admission only: the native worker owns execution.
        record = self._observe(self.store.read())
        self.store.write(record)
        current_id = (record.get("operation") or {}).get("id")
        if expected != _selection(self.params) or expected_id != current_id:
          return result("conflict", "Map state changed on the device. Review the current state and try again.", record, live)
        action_state = self._snapshot(record, live)["actions"][action]
        if not action_state["allowed"]:
          return result("rejected", action_state["reason"], record, live)
        operation = {"id": str(uuid.uuid4()), "action": action, "status": "pending", "region": region,
                     "message": "Request sent through the native map settings."}
        record = {"operation": operation, "mapd_version": _mapd_version(self.params)}
        if action != "delete":
          targets = _targets(region)
          record.update(targets=targets, baseline_progress=_progress(self.params, targets))
        self.store.write(record)
        try:
          if action == "download":
            self.params.put_bool("OsmLocal", True, block=True)
            self.params.remove("OsmStateName")
            self.params.remove("OsmStateTitle")
            self.params.put("OsmLocationName", region["country"], block=True)
            self.params.put("OsmLocationTitle", kwargs["country_title"], block=True)
            if region["country"] == "US":
              self.params.put("OsmStateName", region["state"], block=True)
              self.params.put("OsmStateTitle", kwargs["state_title"], block=True)
          if action != "delete" and _selection(self.params) != region:
            operation.update(status="unknown", message="Map selection changed while submitting the request.")
            self.store.write(record)
            return result("conflict", operation["message"], record, live)
          self.params.put_bool("Mapd_ClearCache" if action == "delete" else "OsmDbUpdatesCheck", True, block=True)
        except Exception:
          operation.update(status="unknown", message="The map request could not be confirmed. Review current state before retrying.")
          self.store.write(record)
          return result("failed", operation["message"], record, live)
        # This acknowledges only the Params write. Do not invent worker success
        # if the native loop consumes the trigger before this response is built.
        return result("accepted", "The device accepted the map request.", record, live)
    except WorkflowBusy:
      return result("rejected", "Another remote map request is being submitted. Refresh its status.")


_SERVICE = None
_SERVICE_LOCK = threading.Lock()


def _service() -> RemoteMaps:
  global _SERVICE
  with _SERVICE_LOCK:
    if _SERVICE is None:
      import platform
      from openpilot.common.hardware.hw import Paths
      from openpilot.common.params import Params
      params = Params()
      mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else params
      size = BackgroundSizeCache(Path(Paths.mapd_root()) / "offline")
      _SERVICE = RemoteMaps(params, mem_params, size_reader=size.get)
    return _SERVICE


def get_device_maps() -> dict:
  return _service().snapshot()


def manage_device_maps(action, **kwargs) -> dict:
  return _service().manage(action, **kwargs)
