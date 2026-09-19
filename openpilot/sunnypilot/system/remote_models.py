"""Typed Athena access to the existing Sunnylink/native model Params workflow.

The in-process record is a request receipt, not a manager job or acknowledgment.
The unchanged model manager owns all downloading, verification and activation.
"""
from __future__ import annotations

import math
import time
import uuid

from openpilot.sunnypilot.system.remote_settings import _message_value
from openpilot.sunnypilot.system.remote_workflows import BackgroundSizeCache, RequestState, WorkflowBusy, utc_now

SLOTS = {"qcom": "ModelManager_ActiveBundle", "chestnut": "ModelManager_ActiveBundleChestnut"}
SYNC_KEYS = ("ModelManager_LastSyncTime", "ModelManager_LastSyncTime_Chestnut")
FIELDS = {
  "select": {"source", "ref", "expected_ref", "expected_operation_id"},
  "reset": {"source", "expected_ref", "expected_operation_id"},
  "cancel": {"operation_id"},
  "refresh": {"expected_operation_id"},
  "clear_cache": {"expected_operation_id", "confirmed"},
  "set_favorite": {"ref", "enabled", "expected_membership"},
}
ACTIVE = {"pending", "running"}
_REQUESTS = RequestState()
_CACHE = None


def _ref(value, nullable=False):
  return (nullable and value is None) or (type(value) is str and 0 < len(value) <= 512 and ";" not in value)


def _selected(params, source):
  value = params.get(SLOTS[source])
  return value if isinstance(value, dict) and value else {}


def _favorites(params):
  value = params.get("ModelManager_Favs")
  return {ref for ref in value.split(";") if _ref(ref)} if isinstance(value, str) else set()


def _public_operation(operation):
  return {key: value for key, value in operation.items()
          if key in {"id", "action", "status", "message", "source", "ref", "progress"}} if operation else None


def _live_models():
  from openpilot.cereal import messaging
  sockets = {}
  result = {"started": None, "chestnut_present": None, "selected": {}}
  try:
    sockets = {name: messaging.sub_sock(name, timeout=1200, conflate=True) for name in ("deviceState", "modelManagerSP")}
    device_message = messaging.recv_one(sockets["deviceState"])
    manager_message = messaging.recv_one_or_none(sockets["modelManagerSP"])
    now = time.monotonic()
    device = _message_value(device_message, "deviceState", now)
    manager = _message_value(manager_message, "modelManagerSP", now)
    if device is not None:
      result.update(started=bool(device.started), chestnut_present=bool(device.chestnutPresent))
    if manager is not None:
      result["selected"] = manager.selectedBundle.to_dict()
      result["selected_at_ns"] = manager_message.logMonoTime
  except Exception:
    pass
  return result


def _progress(bundle):
  result, seen = [], set()
  for model in bundle.get("models", []):
    artifact = model.get("artifact", {}) if isinstance(model, dict) else {}
    name, value = artifact.get("fileName"), artifact.get("downloadProgress", {})
    if not isinstance(name, str) or not name or name in seen or not isinstance(value, dict):
      continue
    percent, eta = value.get("progress", 0), value.get("eta")
    if type(percent) not in (int, float) or not math.isfinite(percent):
      percent = 0
    seen.add(name)
    result.append({"name": name, "status": str(value.get("status", "notDownloading")), "percent": max(0, min(100, percent)),
                   **({"eta_seconds": eta} if type(eta) is int and eta >= 0 else {})})
  return result


def _catalog(params, compatible=None):
  # ModelParser writes chunk manifests, even when parsing cached metadata. Reads
  # must instead map the cached dictionaries without touching model artifacts.
  if compatible is None:
    from openpilot.sunnypilot.models.helpers import is_bundle_version_compatible
    compatible = is_bundle_version_compatible
  result, seen = [], set()
  for source, suffix in (("qcom", ""), ("chestnut", "_Chestnut")):
    cache = params.get(f"ModelManager_ModelsCache{suffix}")
    if not isinstance(cache, dict) or not isinstance(cache.get("bundles"), list):
      continue
    ordered = []
    for bundle in cache["bundles"]:
      if not isinstance(bundle, dict) or not _ref(bundle.get("ref")):
        continue
      try:
        if not compatible({"minimumSelectorVersion": int(bundle.get("minimum_selector_version", 0))}):
          continue
        index = int(bundle.get("index", 0))
      except (ValueError, TypeError, OverflowError):
        continue
      ordered.append((index, bundle))
    for _, bundle in sorted(ordered, key=lambda entry: entry[0], reverse=True):
      identity = (source, bundle["ref"])
      if identity in seen:
        continue
      if len(result) >= 4096:
        return result
      seen.add(identity)
      overrides = bundle.get("overrides") or {}
      result.append({"ref": bundle["ref"], "source": source, "name": str(bundle.get("display_name", "")),
                     "short_name": str(bundle.get("short_name", "")),
                     "folder": str(overrides.get("folder", "")) if isinstance(overrides, dict) else "",
                     **{field: bundle[key] for field, key in (("build_date", "build_time"), ("description", "description"))
                        if isinstance(bundle.get(key), str) and len(bundle[key]) <= 8192}})
  return result

class RemoteModels:
  def __init__(self, params, requests=None, *, state=_live_models, catalog=None, defaults=None, cache_size=None):
    self.params, self.requests, self.state = params, requests if requests is not None else _REQUESTS, state
    self.catalog = catalog or (lambda: _catalog(params))
    self.defaults = defaults or {"qcom": "Default", "chestnut": "Default"}
    self.cache_size = cache_size or (lambda: None)

  def _reason(self, live):
    if live.get("started") is None:
      return "Live device state is unavailable."
    if live["started"] is not False or self.params.get("IsOffroad") is not True:
      return "Model changes require the vehicle to be offroad."
    return ""

  def _observe(self, record, live, bundles):
    operation = record.get("operation") or {}
    marker = self.params.get("ModelManager_DownloadRef")
    selected = live.get("selected") or {}
    if _ref(marker) and (operation.get("action") != "select" or operation.get("ref") != marker or
                         (operation.get("status") not in ACTIVE and record.get("download_ref") != marker)):
      # A native request can be cancelled too. The identifier describes this
      # Athena observation; it is not an identifier issued by the manager.
      source = next((bundle["source"] for bundle in bundles if bundle["ref"] == marker), None)
      operation = {"id": str(uuid.uuid4()), "action": "select", "ref": marker, "status": "pending", "requested_ns": time.monotonic_ns(),
                   **({"source": source} if source else {})}
    if operation.get("action") == "select":
      if (operation.get("status") in ACTIVE and selected.get("ref") == operation.get("ref") and selected.get("status") == "failed" and
          live.get("selected_at_ns", 0) >= operation.get("requested_ns", float("inf"))):
        # Native `failed` includes cancellation and hash/download errors. Keep
        # exactly that observed fact without inventing the cause or retrying.
        operation.update(status="failed", message="The model manager reported a failure or interruption for this request.", progress=_progress(selected))
      elif marker == operation.get("ref") and operation.get("status") in ACTIVE:
        matching = selected.get("ref") == marker
        running = matching and selected.get("status") in ("downloading", "verifying")
        operation.update(status="running" if running else "pending", message="Download request saved; waiting for the selected model to be observed.")
        operation.pop("progress", None)
        if matching:
          operation["progress"] = _progress(selected)
      elif operation.get("status") in ACTIVE:
        source = operation.get("source")
        if source in SLOTS and _selected(self.params, source).get("ref") == operation.get("ref"):
          operation.update(status="completed", message="Model selection observed on the device for the next drive.")
        else:
          operation.update(status="unknown", message="The download request is no longer present; its outcome is unconfirmed.")
    elif operation.get("action") == "refresh" and operation.get("status") in ACTIVE:
      if all(type(value := self.params.get(key)) is int and value >= operation["requested_ns"] for key in SYNC_KEYS):
        operation.update(status="completed", message="Both model catalogs have refreshed on the device.")
      elif time.monotonic_ns() - operation["requested_ns"] > 30_000_000_000:
        operation.update(status="unknown", message="Catalog refresh has not been confirmed. Cached models remain available.")
    elif operation.get("action") == "clear_cache" and operation.get("status") in ACTIVE:
      if not self.params.get("ModelManager_ClearCache"):
        operation.update(status="unknown", message="The cache-clear request is no longer present; the manager does not report its outcome.")
    if (self.params.get("ModelManager_ClearCache") and not marker and operation.get("status") not in ACTIVE and
        selected.get("status") not in ("downloading", "verifying")):
      operation = {"id": str(uuid.uuid4()), "action": "clear_cache", "status": "pending", "message": "A cache-clear request is present on the device."}
    record["operation"] = operation or None
    record["download_ref"] = marker
    self.requests.write(record)
    return operation

  def _snapshot(self, record, live, *, observe=True):
    reason = self._reason(live)
    try:
      bundles = self.catalog()
    except Exception:
      bundles = []
    operation = self._observe(record, live, bundles) if observe else (record.get("operation") or {})
    marker = self.params.get("ModelManager_DownloadRef")
    busy = bool(marker or self.params.get("ModelManager_ClearCache") or operation.get("status") in ACTIVE or
                (live.get("selected") or {}).get("status") in ("downloading", "verifying"))
    actions = {}
    for action in FIELDS:
      blocked = reason
      if not blocked and action == "cancel":
        if not marker or operation.get("action") != "select" or operation.get("ref") != marker:
          blocked = "There is no current model download request to cancel."
      elif not blocked and action != "set_favorite" and busy:
        blocked = "A model operation is already in progress."
      if not blocked and action == "select" and not bundles:
        blocked = "No compatible models are available. Refresh the model list."
      actions[action] = {"allowed": not blocked, **({"reason": blocked} if blocked else {})}
    slots = []
    for source, title in (("qcom", "Small model"), ("chestnut", "Big model")):
      selected = _selected(self.params, source)
      slots.append({"source": source, "title": title, "available": source == "qcom" or live.get("chestnut_present") is True,
                    "selected_ref": selected.get("ref") if _ref(selected.get("ref")) else None,
                    "selected_name": str(selected.get("displayName") or self.defaults[source]), "default_name": self.defaults[source]})
    return {"version": 1, "observed_at": utc_now(), "available": not reason, **({"reason": reason} if reason else {}),
            "actions": actions, "operation": _public_operation(operation), "slots": slots, "bundles": bundles,
            "favorites": sorted(_favorites(self.params))[:4096], "cache_bytes": self.cache_size(),
            "hardware": {"chestnut_present": live.get("chestnut_present")}}

  def snapshot(self):
    live = self.state()
    try:
      with self.requests.locked():
        return self._snapshot(self.requests.read(), live)
    except WorkflowBusy:
      # A read can still return Params without modifying the in-process receipt.
      return self._snapshot(self.requests.read(), {**live, "started": None}, observe=False)

  def manage(self, action, **command):
    def result(status, message):
      return {"status": status, "message": message, "snapshot": self.snapshot()}

    if type(action) is not str or action not in FIELDS or set(command) != FIELDS[action]:
      return result("rejected", "Unsupported model command or fields.")
    if ("source" in command and (type(command["source"]) is not str or command["source"] not in SLOTS)) or any(
        not _ref(command[key], nullable=key.startswith("expected_"))
        for key in ("ref", "expected_ref", "expected_operation_id", "operation_id") if key in command):
      return result("rejected", "Invalid model source or reference.")
    if any(type(command[key]) is not bool for key in ("enabled", "expected_membership", "confirmed") if key in command):
      return result("rejected", "Model command flags must be booleans.")
    if action == "clear_cache" and command["confirmed"] is not True:
      return result("rejected", "Confirm clearing downloaded model cache.")
    try:
      with self.requests.locked():
        # Check live state inside the process lock immediately before writing.
        # Native UI/manager do not share this lock; these are optimistic checks.
        live = self.state()
        record = self.requests.read()
        snapshot = self._snapshot(record, live)
        status, message = self._apply(action, command, record, snapshot)
        record = self.requests.read()
        receipt = (record.get("operation") or {}).get("id")
        snapshot = self._snapshot(record, live)
        if status == "accepted" and (snapshot["operation"] or {}).get("id") != receipt:
          status, message = "conflict", "The device request changed while saving. Refresh before retrying."
        return {"status": status, "message": message, "snapshot": snapshot}
    except WorkflowBusy:
      return result("rejected", "Another remote model request is in progress. Refresh before retrying.")
    except Exception:
      return result("failed", "The model request could not be confirmed. Refresh before retrying.")

  def _apply(self, action, command, record, snapshot):
    operation = record.get("operation") or {}
    if "expected_operation_id" in command and command["expected_operation_id"] != operation.get("id"):
      return "conflict", "The model request changed. Refresh and try again."
    if "expected_ref" in command and command["expected_ref"] != (_selected(self.params, command["source"]).get("ref") or None):
      return "conflict", "The selected model changed on the device."
    if action == "cancel" and command["operation_id"] != operation.get("id"):
      return "conflict", "The download request changed. Refresh before cancelling."
    allowed = snapshot["actions"][action]
    if not allowed["allowed"]:
      return "rejected", allowed.get("reason", "This operation is unavailable.")
    if action == "select":
      matches = [bundle for bundle in snapshot["bundles"] if bundle["ref"] == command["ref"]]
      if not matches or matches[0]["source"] != command["source"]:
        return "rejected", "The native model manager cannot resolve this reference to the requested source. Refresh the catalog."
    if action == "set_favorite":
      favorites, ref = _favorites(self.params), command["ref"]
      if (ref in favorites) != command["expected_membership"]:
        return "conflict", "The favorite changed on the device."
      if command["enabled"] and not any(bundle["ref"] == ref for bundle in snapshot["bundles"]):
        return "rejected", "Choose a model from the device catalog."
      favorites.add(ref) if command["enabled"] else favorites.discard(ref)
      self.params.put("ModelManager_Favs", ";".join(sorted(favorites)), block=True)
      if _favorites(self.params) != favorites:
        return "conflict", "Favorites changed while saving. Refresh before retrying."
      return "saved", "Favorite saved on the device."
    if action == "cancel":
      if self.params.get("ModelManager_DownloadRef") != operation.get("ref"):
        return "conflict", "The download request changed. Refresh before cancelling."
      self.params.remove("ModelManager_DownloadRef")
      if self.params.get("ModelManager_DownloadRef"):
        return "conflict", "Another download request is present. Refresh before cancelling."
      operation.update(status="unknown", message="Cancellation request saved. The manager does not report a cancellation outcome.")
      record["download_ref"] = None
      self.requests.write(record)
      return "accepted", "Cancellation request saved; the final outcome is unconfirmed."
    operation = {"id": str(uuid.uuid4()), "action": action, "status": "pending", "requested_ns": time.monotonic_ns(),
                 **{key: command[key] for key in ("source", "ref") if key in command}}
    if action == "select":
      self.params.put("ModelManager_DownloadRef", command["ref"], block=True)
      marker = self.params.get("ModelManager_DownloadRef")
      if marker and marker != command["ref"]:
        return "conflict", "Another download request replaced this request. Refresh before retrying."
      message = "Download request saved. The existing model manager will process it."
    elif action == "reset":
      self.params.remove(SLOTS[command["source"]])
      if self.params.get(SLOTS[command["source"]]):
        return "conflict", "The model selection changed while saving. Refresh before retrying."
      operation["status"] = "completed"
      message = "Default model selected for the next drive."
    elif action == "refresh":
      for key in SYNC_KEYS:
        self.params.put(key, 0, block=True)
      message = "Catalog refresh requested. Cached models remain available."
    else:
      self.params.put("ModelManager_ClearCache", True, block=True)
      message = "Cache clearing requested. The existing manager does not report its final outcome."
    operation["message"] = message
    record["operation"] = operation
    self.requests.write(record)
    return "saved" if action == "reset" else "accepted", message


def _service():
  from openpilot.common.hardware.hw import Paths
  from openpilot.common.params import Params
  from openpilot.sunnypilot.models.model_name import DEFAULT_MODEL, DEFAULT_BIG_MODEL
  global _CACHE
  with _REQUESTS.locked():
    if _CACHE is None:
      _CACHE = BackgroundSizeCache(Paths.model_root())
  return RemoteModels(Params(), defaults={"qcom": DEFAULT_MODEL, "chestnut": DEFAULT_BIG_MODEL}, cache_size=_CACHE.get)


def get_device_models():
  return _service().snapshot()


def manage_device_models(action, **kwargs):
  return _service().manage(action, **kwargs)
