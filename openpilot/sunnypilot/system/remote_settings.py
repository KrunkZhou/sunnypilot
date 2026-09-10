"""Device-authored settings over Athena, independent of the Sunnylink service.

The Sunnylink schema describes the controls; validation and live-state checks stay
on the device. Native UI callbacks with persistent side effects are handled by a
small adapter below rather than importing UI code into Athena.
"""
# JSON booleans must not pass integer checks; preserve exact primitive types.
from __future__ import annotations

import math
import operator
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


VERSION = 1
MAX_STATE_AGE = 5.0
STATE_TIMEOUT_MS = 1200
_LOCK = threading.Lock()
_UNKNOWN = object()
_TYPES = {0: "string", 1: "bool", 2: "int", 3: "float"}
_COMPARE = {"==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
_WIDGETS = {"toggle", "option", "multiple_button", "info"}
_MODEL_SETTINGS = {"LaneTurnDesire", "LaneTurnValue", "LagdToggle", "LagdToggleDelay", "CameraOffset", "NeuralNetworkLateralControl"}
_BLOCKED = {"AdbEnabled", "SshEnabled", "GithubUsername", "GithubSshKeys", "OnroadCycleRequested", "ParamsVersion",
            "HasAcceptedTerms", "HasAcceptedTermsSP", "CompletedTrainingVersion", "CompletedSunnylinkConsentVersion"}
_EXCLUSIVE = {
  "JoystickDebugMode": ("LongitudinalManeuverMode", "LateralManeuverMode"),
  "LongitudinalManeuverMode": ("JoystickDebugMode", "LateralManeuverMode"),
  "LateralManeuverMode": ("ExperimentalMode", "JoystickDebugMode", "LongitudinalManeuverMode"),
}


def _number(value: Any) -> bool:
  try:
    return type(value) in (int, float) and math.isfinite(value)
  except OverflowError:
    return False


def _same(left: Any, right: Any) -> bool:
  if type(left) in (int, float) and type(right) in (int, float):
    return _number(left) and _number(right) and left == right
  return type(left) is type(right) and left == right


def _supported_rule(rule: Any) -> bool:
  if not isinstance(rule, dict):
    return False
  kind = rule.get("type")
  if kind in ("offroad_only", "not_engaged"):
    return set(rule) == {"type"}
  if kind in ("param", "capability"):
    field = "key" if kind == "param" else "field"
    return (set(rule) == {"type", field, "equals"} and type(rule[field]) is str and bool(rule[field]) and
            (type(rule["equals"]) in (bool, str) or _number(rule["equals"])))
  if kind == "param_compare":
    return (set(rule) == {"type", "key", "op", "value"} and type(rule["key"]) is str and bool(rule["key"]) and
            type(rule["op"]) is str and rule["op"] in _COMPARE and _number(rule["value"]))
  if kind == "not":
    return set(rule) == {"type", "condition"} and _supported_rule(rule["condition"])
  if kind in ("all", "any"):
    return (set(rule) == {"type", "conditions"} and isinstance(rule["conditions"], list) and bool(rule["conditions"]) and
            all(_supported_rule(condition) for condition in rule["conditions"]))
  return False


@dataclass(frozen=True)
class DeviceState:
  started: bool | None = None
  engaged: bool | None = None
  network_metered: bool | None = None


def _message_value(message, service: str, now: float):
  if message is None or not message.valid:
    return None
  age = now - message.logMonoTime / 1e9
  if not -0.5 <= age <= MAX_STATE_AGE:
    return None
  return getattr(message, service)


def _live_state() -> DeviceState:
  from openpilot.cereal import messaging

  services = ("deviceState", "selfdriveState", "selfdriveStateSP")
  sockets = {}
  try:
    # Subscribe together so onroad control messages arrive while waiting for
    # deviceState. Offroad control publishers are legitimately absent.
    sockets = {name: messaging.sub_sock(name, timeout=STATE_TIMEOUT_MS, conflate=True) for name in services}
    message = messaging.recv_one(sockets["deviceState"])
    device = _message_value(message, "deviceState", time.monotonic())
    if device is None:
      return DeviceState()
    metered = getattr(device, "networkMetered", None)
    metered = metered if type(metered) is bool else None
    if not device.started:
      return DeviceState(False, False, metered)
    controls = [messaging.recv_one_or_none(sockets[name]) for name in services[1:]]
    now = time.monotonic()
    # A deviceState sampled before a slow receive must still be fresh.
    if _message_value(message, "deviceState", now) is None:
      return DeviceState()
    standard = _message_value(controls[0], "selfdriveState", now)
    sunny = _message_value(controls[1], "selfdriveStateSP", now)
    if (standard is not None and standard.enabled) or (sunny is not None and sunny.mads.enabled):
      return DeviceState(True, True, metered)
    return DeviceState(True, False if standard is not None and sunny is not None else None, metered)
  except Exception:
    return DeviceState()
  finally:
    sockets.clear()


@dataclass
class Setting:
  item: dict
  panel: str
  section: str
  visibility: list
  enablement: list
  confirmation: str = ""
  remote: bool = True


class Context:
  def __init__(self, params, capabilities: dict, state: DeviceState):
    self.params, self.capabilities, self.state = params, capabilities, state
    self.values: dict[str, Any] = {}
    self.types: dict[str, str | None] = {}

  def read(self, key: str):
    if key not in self.values:
      try:
        kind = _TYPES.get(int(self.params.get_type(key)))
        value = self.params.get(key, return_default=True)
        if kind == "bool" and value is None:
          value = False  # Matches Params.get_bool for an unset boolean.
        valid = ((kind == "bool" and type(value) is bool) or (kind == "int" and type(value) is int and abs(value) <= 2 ** 53 - 1) or
                 (kind == "float" and _number(value)) or
                 (kind == "string" and type(value) is str))
        self.types[key] = kind
        self.values[key] = value if valid else _UNKNOWN
      except Exception:
        self.types[key], self.values[key] = None, _UNKNOWN
    return self.values[key]

  def rule(self, rule: dict) -> bool | None:
    if not _supported_rule(rule):
      return None
    kind = rule.get("type")
    if kind == "offroad_only":
      if self.state.started is None:
        return None
      return self.state.started is False and self.read("IsOffroad") is True
    if kind == "not_engaged":
      return None if self.state.engaged is None else not self.state.engaged
    if kind in ("param", "capability"):
      if "equals" not in rule:
        return None
      value = self.read(rule.get("key", "")) if kind == "param" else self.capabilities.get(rule.get("field"), _UNKNOWN)
      return None if value is _UNKNOWN else _same(value, rule["equals"])
    if kind == "param_compare":
      value, target, op = self.read(rule.get("key", "")), rule.get("value"), _COMPARE.get(rule.get("op"))
      if type(value) not in (int, float) or type(target) not in (int, float) or not op:
        return None
      return op(value, target) if _number(value) and _number(target) else None
    if kind == "not":
      value = self.rule(rule.get("condition"))
      return None if value is None else not value
    if kind in ("all", "any"):
      conditions = rule.get("conditions")
      if not isinstance(conditions, list) or not conditions:
        return None
      values = [self.rule(condition) for condition in conditions]
      if kind == "all":
        return False if False in values else (None if None in values else True)
      return True if True in values else (None if None in values else False)
    return None

  def reason(self, rules: list) -> str:
    if not isinstance(rules, list):
      return "Unsupported settings rule."
    for rule in rules:
      if not _supported_rule(rule):
        return "Unsupported settings rule."
      passed = self.rule(rule)
      if passed is not True:
        if passed is None:
          return "Required device state or setting is unavailable."
        kind = rule.get("type")
        if kind == "offroad_only":
          return "Available only while the device is offroad."
        if kind == "not_engaged":
          return "Disengage sunnypilot and MADS before changing this setting."
        return "Unavailable with the current vehicle or settings."
    return ""


class RemoteSettings:
  def __init__(self, params, schema: dict, capabilities, state=_live_state, prebuilt_path: Path | None = None, custom_model=None):
    self.params, self.schema = params, schema
    self.capabilities, self.state = capabilities, state
    self.prebuilt_path = prebuilt_path
    self.custom_model = custom_model or self._custom_model_available
    self.panels: list[dict] = []
    self.settings: dict[str, Setting] = {}
    if schema.get("schema_version") != "1.0":
      raise ValueError("Unsupported device settings schema.")
    self._index()

  def _index(self):
    def children(container, panel, label="", visibility=None, enablement=None, confirmation="", remote=True):
      visibility = [*(visibility or []), *container.get("visibility", [])]
      enablement = [*(enablement or []), *container.get("enablement", [])]
      if container.get("trigger_condition") is not None:
        enablement.append(container["trigger_condition"])
      if container.get("attestation_required") or container.get("requires_attestation"):
        confirmation = container.get("description") or "Confirm this change. Use test maneuvers only in a closed environment."
      for item in container.get("items", []) + container.get("sub_items", []):
        key = item.get("key")
        if panel == "models" and key not in _MODEL_SETTINGS:
          continue
        if not isinstance(key, str) or not key or key in self.settings:
          raise ValueError("Invalid or duplicate device setting.")
        own_confirmation = confirmation
        if item.get("requires_attestation"):
          own_confirmation = item.get("description") or "Confirm this setting change."
        self.settings[key] = Setting(item, panel, label, [*visibility, *item.get("visibility", [])],
                                     [*enablement, *item.get("enablement", [])], own_confirmation, remote)
        children({"sub_items": item.get("sub_items", [])}, panel, label, self.settings[key].visibility,
                 self.settings[key].enablement, own_confirmation, remote)
      for section in container.get("sections", []) + container.get("sub_panels", []):
        title = section.get("title") or section.get("label") or ""
        children(section, panel, " / ".join(x for x in (label, title) if x), visibility, enablement, confirmation, remote)

    for panel in self.schema.get("panels", []):
      self.panels.append({"id": panel["id"], "title": panel.get("label", panel["id"]), "description": panel.get("description", "")})
      children(panel, panel["id"], remote=panel.get("remote_configurable") is True or panel["id"] == "models")
    if self.schema.get("vehicle_settings"):
      self.panels.append({"id": "vehicle", "title": "Vehicle", "description": "Vehicle-specific settings"})
      for brand, group in self.schema["vehicle_settings"].items():
        if isinstance(group, list):
          group = {"items": group}
        children(group, "vehicle", group.get("title", brand), [{"type": "capability", "field": "brand", "equals": brand}])

  def _context(self) -> Context:
    try:
      capabilities = self.capabilities()
    except Exception:
      capabilities = {}
    return Context(self.params, capabilities, self.state())

  def _value(self, key: str, context: Context):
    value = context.read(key)
    # The schema uses an empty string for the native UI's remove-param Default.
    if key == "TorqueControlTune" and _same(value, 0.0):
      return ""
    return value

  def _custom_model_available(self):
    try:
      from openpilot.sunnypilot.models.helpers import get_active_bundle
      return get_active_bundle(self.params) is not None
    except Exception:
      return False

  def _row(self, setting: Setting, context: Context) -> dict:
    item, key = setting.item, setting.item["key"]
    value = self._value(key, context)
    kind = context.types.get(key)
    visibility_reason = context.reason(setting.visibility)
    reason = ""
    if not setting.remote or item.get("blocked") or key in _BLOCKED or item.get("widget") == "info":
      reason = "This setting can only be changed on the device."
    elif item.get("widget") not in _WIDGETS or kind is None or (item.get("widget") == "toggle" and kind != "bool"):
      reason = "This setting type is not supported for remote changes."
    elif value is _UNKNOWN:
      reason = "The current setting value is unavailable."
    elif context.state.started is None:
      reason = "Live device state is unavailable."
    else:
      reason = visibility_reason or context.reason(setting.enablement)
    if not reason and key == "OffroadMode":
      reason = context.reason([{"type": "not_engaged"}])
    if not reason and key == "RecordFront" and context.read("RecordFrontLock") is not False:
      reason = "Driver camera recording is locked on the device."
    if not reason and key == "CameraOffset" and not self.custom_model():
      reason = "Camera offset requires a selected custom model for the active hardware."

    title = item.get("title", key)
    suffix = item.get("title_param_suffix", {})
    if suffix:
      suffix_value = context.read(suffix.get("param", ""))
      lookup = str(suffix_value).lower() if type(suffix_value) is bool else str(suffix_value)
      title = " ".join(x for x in (title, suffix.get("values", {}).get(lookup, "")) if x)
    row = {"key": key, "title": title, "description": item.get("description", ""), "section": setting.section,
           "widget": item.get("widget") if item.get("widget") in _WIDGETS else "info", "type": kind or "string",
           "value": None if value is _UNKNOWN else value, "visible": not visibility_reason, "editable": not reason}
    if kind is None or (row["widget"] == "toggle" and kind != "bool"):
      row["widget"] = "info"
    if item.get("details"):
      row["description"] = "\n\n".join(x for x in (row["description"], item["details"]) if x)
    if reason:
      row["reason"] = reason
    for field in ("min", "max", "step"):
      if field in item:
        bound = item[field]
        if isinstance(bound, dict):
          bound = bound.get("metric" if context.read("IsMetric") is True else "imperial")
        if _number(bound):
          row[field] = bound
        else:
          row["editable"], row["reason"] = False, "This control has unsupported value constraints."
    unit = item.get("unit")
    if isinstance(unit, dict):
      unit = unit.get("metric" if context.read("IsMetric") is True else "imperial", "")
    if unit:
      row["unit"] = unit
    if key == "LaneTurnValue":
      # Stored and consumed in mph regardless of IsMetric. Native metric steps
      # can leave hundredth-mph values, so integer-mph validation is incorrect.
      row.update(min=5, max=20, step=0.01, unit="mph")
    elif key == "LagdToggleDelay":
      row["unit"] = "s"
    if "options" in item:
      row["options"] = []
      for option in item["options"]:
        option_reason = context.reason(option.get("visibility", [])) or context.reason(option.get("enablement", []))
        entry = {"value": option["value"], "label": option.get("label", str(option["value"])), "enabled": not (reason or option_reason)}
        if reason or option_reason:
          entry["reason"] = reason or option_reason
        row["options"].append(entry)
    elif row["widget"] in ("option", "multiple_button") and not all(_number(row.get(field)) for field in ("min", "max", "step")):
      row["editable"], row["reason"] = False, "This control has no supported value constraints."
    if ("step" in row and row["step"] <= 0) or ("min" in row and "max" in row and row["min"] > row["max"]):
      row["editable"], row["reason"] = False, "This control has invalid value constraints."
    if (row["widget"] == "multiple_button" and not row.get("options")) or row.get("reason", "").startswith("This control has"):
      row["widget"], row["editable"] = "info", False
      row.setdefault("reason", "This control has no supported value constraints.")
      for field in ("options", "min", "max", "step"):
        row.pop(field, None)
    confirmation = setting.confirmation
    if key == "OffroadMode":
      confirmation = "Changing Always Offroad mode changes the device's running state. Confirm this change while disengaged."
    elif key in ("AlphaLongitudinalEnabled", "ToyotaEnforceStockLongitudinal", "ToyotaStopAndGoHack", "ExperimentalMode"):
      confirmation = item.get("description") or f"Confirm changing {title}."
    if confirmation:
      row["confirmation"] = confirmation
    if item.get("needs_onroad_cycle"):
      row["requires_restart"] = "Saved settings take effect on the next normal onroad cycle."
    if key == "NeuralNetworkLateralControl":
      row["requires_restart"] = "Saved settings take effect on the next normal onroad cycle."
    if key == "DisableUpdates":
      row["requires_restart"] = "Reboot the device for this setting to take effect."
    elif key == "ShowDebugInfo":
      row["requires_restart"] = "Restart the device UI to apply the debug overlay setting."
    return row

  def snapshot(self, context: Context | None = None) -> dict:
    context = context or self._context()
    panels = [{**panel, "settings": []} for panel in self.panels]
    by_id = {panel["id"]: panel for panel in panels}
    for setting in self.settings.values():
      by_id[setting.panel]["settings"].append(self._row(setting, context))
    return {"version": VERSION, "observed_at": datetime.now(UTC).isoformat(), "panels": panels}

  def _validate(self, row: dict, value):
    key, kind = row["key"], row["type"]
    default = key == "TorqueControlTune" and value == "" and type(value) is str
    valid = ((kind == "bool" and type(value) is bool) or (kind == "int" and type(value) is int and abs(value) <= 2 ** 53 - 1) or
             (kind == "float" and _number(value)) or
             (kind == "string" and type(value) is str and len(value) <= 4096) or default)
    if not valid:
      return "The value has the wrong type or is not finite."
    if "options" in row:
      option = next((option for option in row["options"] if _same(option["value"], value)), None)
      if option is None:
        return "Choose one of the supported options."
      if not option["enabled"]:
        return option.get("reason", "This option is currently unavailable.")
    elif row["widget"] in ("option", "multiple_button"):
      if not all(_number(row.get(field)) for field in ("min", "max", "step")):
        return "This control has no supported value constraints."
    if not default and type(value) in (int, float):
      if ("min" in row and value < row["min"]) or ("max" in row and value > row["max"]):
        return "The value is outside the supported range."
      if "step" in row:
        step = row["step"]
        if step <= 0:
          return "This control has invalid value constraints."
        offset = (value - row.get("min", 0)) / step
        if not math.isclose(offset, round(offset), rel_tol=0, abs_tol=1e-7):
          return "The value does not match the supported step."
    return ""

  def _write(self, key, value):
    if key == "TorqueControlTune" and value == "":
      self.params.remove(key)
      return
    if key == "QuickBootToggle":
      if self.prebuilt_path is None:
        raise RuntimeError("Quickboot marker is unavailable.")
      if value:
        self.prebuilt_path.touch(exist_ok=True)
      else:
        self.prebuilt_path.unlink(missing_ok=True)
    if _TYPES.get(int(self.params.get_type(key))) == "float":
      value = float(value)
    self.params.put(key, value, block=True)

  def set_setting(self, key, value, expected_value, confirmed=False) -> dict:
    context = self._context()

    def result(status, message, changed=None, snapshot_context=None):
      return {"status": status, "message": message, "changed": changed or [], "snapshot": self.snapshot(snapshot_context or self._context())}

    if type(key) is not str or key not in self.settings:
      return result("rejected", "This setting is not available for remote changes.", snapshot_context=context)
    row = self._row(self.settings[key], context)
    if not row["editable"]:
      return result("rejected", row["reason"], snapshot_context=context)
    if not _same(row["value"], expected_value):
      return result("conflict", "The setting changed on the device. Review its current value and try again.", snapshot_context=context)
    reason = self._validate(row, value)
    if reason:
      return result("rejected", reason, snapshot_context=context)
    if row.get("confirmation") and confirmed is not True:
      return result("rejected", "Explicit confirmation is required for this change.", snapshot_context=context)
    if _same(row["value"], value):
      return result("saved", "The device already has this value.", snapshot_context=context)

    writes = [(other, False) for other in _EXCLUSIVE.get(key, ())]
    if key == "ToyotaEnforceStockLongitudinal" and value is True:
      writes.extend((other, False) for other in ("AlphaLongitudinalEnabled", "ToyotaStopAndGoHack"))
    writes.append((key, value))
    # Preflight all companion reads before making any changes.
    before = {other: self._value(other, context) for other, _ in writes}
    if any(previous is _UNKNOWN for previous in before.values()):
      return result("rejected", "A related device setting is unavailable.", snapshot_context=context)
    changed = []
    attempted = []
    try:
      for other, requested in writes:
        if _same(before[other], requested):
          continue
        attempted.append(other)
        self._write(other, requested)
        fresh = Context(self.params, context.capabilities, context.state)
        if not _same(self._value(other, fresh), requested):
          raise RuntimeError("The device did not retain the requested value.")
        changed.append(other)
      # Verify the complete set again, including companions and device-side
      # normalization that can occur while individual writes are persisted.
      fresh = self._context()
      if any(not _same(self._value(other, fresh), requested) for other, requested in writes):
        return result("failed", "The device changed a setting while saving. Review the current values.", changed, fresh)
      return result("saved", row.get("requires_restart", "Saved on the device."), changed, fresh)
    except Exception:
      fresh = self._context()
      changed = [other for other in attempted if (current := self._value(other, fresh)) is not _UNKNOWN and not _same(current, before[other])]
      # Quickboot's filesystem change may precede a failed Params write.
      if key == "QuickBootToggle" and key in attempted and self.prebuilt_path is not None:
        try:
          if self.prebuilt_path.exists() != before[key] and key not in changed:
            changed.append(key)
        except OSError:
          pass
      message = "The device could not save the setting."
      if changed:
        message += " Some changes were applied; review the current values before retrying."
      return result("failed", message, changed, fresh)


def _service() -> RemoteSettings:
  from openpilot.common.hardware import PC
  from openpilot.common.hardware.hw import Paths
  from openpilot.common.params import Params
  from openpilot.sunnypilot.sunnylink.capabilities import generate_capabilities
  from openpilot.sunnypilot.sunnylink.tools.generate_settings_schema import generate_schema

  params = Params()
  marker = Path(Paths.comma_home()) / "prebuilt" if PC else Path("/data/openpilot/prebuilt")
  return RemoteSettings(params, generate_schema(), lambda: generate_capabilities(params), prebuilt_path=marker)


def get_device_settings() -> dict:
  with _LOCK:
    return _service().snapshot()


def set_device_setting(key, value, expected_value, confirmed=False) -> dict:
  with _LOCK:
    return _service().set_setting(key, value, expected_value, confirmed)
