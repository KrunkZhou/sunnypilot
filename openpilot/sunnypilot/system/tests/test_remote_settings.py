"""Portable policy tests: no native Params library, UI, or Sunnylink service."""
import ast
import functools
import json
import queue
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpilot.sunnypilot.system import remote_settings as remote
from openpilot.system.athena.rpc import Dispatcher


class FakeParams:
  def __init__(self, values=None, kinds=None, defaults=None):
    self.values = {"IsOffroad": True, "IsMetric": False, "RecordFrontLock": False, **(values or {})}
    self.kinds = {key: {bool: 1, int: 2, float: 3, str: 0}[type(value)] for key, value in self.values.items()}
    self.kinds.update(kinds or {})
    self.defaults = defaults or {}
    self.writes = []
    self.fail = set()
    self.after_write = None
    self.unreadable = set()

  def get_type(self, key):
    return self.kinds[key]

  def get(self, key, return_default=False):
    if key in self.unreadable:
      raise OSError("test read failure")
    return self.values.get(key, self.defaults.get(key) if return_default else None)

  def put(self, key, value, block=False):
    if key in self.fail:
      raise OSError("test write failure")
    assert block is True
    self.values[key] = value
    self.writes.append((key, value))
    if self.after_write:
      self.after_write(key, value)

  def remove(self, key):
    if key in self.fail:
      raise OSError("test delete failure")
    self.values.pop(key, None)
    self.writes.append((key, None))


def toggle(key, **kwargs):
  return {"key": key, "title": key, "widget": "toggle", **kwargs}


def schema(items=None, **panel):
  return {"schema_version": "1.0", "panels": [{"id": "device", "label": "Device", "remote_configurable": True,
                                                 "items": items or [], **panel}], "vehicle_settings": {}}


def rows(snapshot):
  return {row["key"]: row for panel in snapshot["panels"] for row in panel["settings"]}


class TestRemoteSettings(unittest.TestCase):
  def service(self, items=None, values=None, **kwargs):
    params = FakeParams(values or {"QuietMode": False})
    service = remote.RemoteSettings(params, schema(items or [toggle("QuietMode")]), dict,
                                    lambda: remote.DeviceState(False, False), **kwargs)
    return service, params

  def test_reads_defaults_without_writing(self):
    service, params = self.service()
    params.values.pop("QuietMode")
    row = rows(service.snapshot())["QuietMode"]
    self.assertIs(row["value"], False)
    self.assertTrue(row["editable"])
    self.assertEqual(params.writes, [])

  def test_saved_requires_persistence_and_returns_snapshot(self):
    service, params = self.service()
    result = service.set_setting("QuietMode", True, False)
    self.assertEqual(result["status"], "saved")
    self.assertEqual(result["changed"], ["QuietMode"])
    self.assertIs(rows(result["snapshot"])["QuietMode"]["value"], True)
    self.assertEqual(params.writes, [("QuietMode", True)])

  def test_compare_and_set_conflict_does_not_write(self):
    service, params = self.service(values={"QuietMode": True})
    result = service.set_setting("QuietMode", False, False)
    self.assertEqual(result["status"], "conflict")
    self.assertEqual(params.writes, [])

  def test_bool_and_integer_are_not_interchangeable(self):
    service, params = self.service()
    self.assertEqual(service.set_setting("QuietMode", True, 0)["status"], "conflict")
    self.assertEqual(service.set_setting("QuietMode", 1, False)["status"], "rejected")
    self.assertEqual(params.writes, [])

  def test_unknown_blocked_and_info_settings(self):
    items = [toggle("SshEnabled"), toggle("AdbEnabled"), toggle("QuietMode", blocked=True),
             {"key": "LanguageSetting", "widget": "info", "title": "Language"}]
    service, params = self.service(items, {"SshEnabled": False, "AdbEnabled": False, "QuietMode": False, "LanguageSetting": "en_US"})
    for key in ("SshEnabled", "AdbEnabled", "QuietMode", "LanguageSetting", "GithubSshKeys", "OnroadCycleRequested"):
      with self.subTest(key=key):
        self.assertEqual(service.set_setting(key, True, False)["status"], "rejected")
    self.assertEqual(params.writes, [])

  def test_models_allowlist_and_other_nonremote_panels_readonly(self):
    definition = schema([toggle("QuietMode")], remote_configurable=False)
    definition["panels"].append({"id": "models", "items": [toggle("LagdToggle"), toggle("UnreviewedModelSetting")]})
    service = remote.RemoteSettings(FakeParams({"QuietMode": False, "LagdToggle": True}), definition, dict, lambda: remote.DeviceState(False, False))
    self.assertTrue(rows(service.snapshot())["LagdToggle"]["editable"])
    self.assertNotIn("UnreviewedModelSetting", rows(service.snapshot()))
    self.assertFalse(rows(service.snapshot())["QuietMode"]["editable"])

  def test_offroad_and_disengagement_rules_are_independent(self):
    service, params = self.service([toggle("Parked", enablement=[{"type": "offroad_only"}]),
                                    toggle("Disengaged", enablement=[{"type": "not_engaged"}]), toggle("Ungated")],
                                   {"Parked": False, "Disengaged": False, "Ungated": False})
    for state, expected in ((remote.DeviceState(False, False), (True, True, True)),
                            (remote.DeviceState(True, False), (False, True, True)),
                            (remote.DeviceState(True, True), (False, False, True)),
                            (remote.DeviceState(True, None), (False, False, True)),
                            (remote.DeviceState(), (False, False, False))):
      with self.subTest(state=state):
        service.state = lambda state=state: state
        actual = rows(service.snapshot())
        self.assertEqual(tuple(actual[key]["editable"] for key in ("Parked", "Disengaged", "Ungated")), expected)
    service.state = lambda: remote.DeviceState(False, False)
    params.values["IsOffroad"] = False
    self.assertFalse(rows(service.snapshot())["Parked"]["editable"])

  def test_state_change_between_read_and_write_is_checked(self):
    service, params = self.service([toggle("QuietMode", enablement=[{"type": "not_engaged"}])])
    self.assertTrue(rows(service.snapshot())["QuietMode"]["editable"])
    service.state = lambda: remote.DeviceState(True, True)
    self.assertEqual(service.set_setting("QuietMode", True, False)["status"], "rejected")
    self.assertEqual(params.writes, [])

  def test_nested_rules_unknown_negation_and_option_gates(self):
    definition = schema([], sections=[{"title": "Parent", "enablement": [{"type": "capability", "field": "supported", "equals": True}],
      "sub_panels": [{"label": "Child", "trigger_condition": {"type": "param", "key": "Parent", "equals": True}, "items": [{
        "key": "Mode", "widget": "multiple_button", "title": "Mode", "options": [
          {"value": 0, "label": "Off"}, {"value": 1, "label": "On", "enablement": [{"type": "param_compare", "key": "Count", "op": ">", "value": 2}]}],
        "enablement": [{"type": "all", "conditions": [{"type": "any", "conditions": [
          {"type": "param", "key": "Parent", "equals": True}, {"type": "capability", "field": "missing", "equals": True}]}]}]}]}]}])
    params = FakeParams({"Mode": 0, "Parent": True, "Count": 1})
    service = remote.RemoteSettings(params, definition, lambda: {"supported": True}, lambda: remote.DeviceState(False, False))
    row = rows(service.snapshot())["Mode"]
    self.assertEqual(row["section"], "Parent / Child")
    self.assertTrue(row["editable"])
    self.assertFalse(row["options"][1]["enabled"])
    self.assertEqual(service.set_setting("Mode", 1, 0)["status"], "rejected")
    params.values["Count"] = 3
    self.assertEqual(service.set_setting("Mode", 1, 0)["status"], "saved")
    params.values["Parent"] = False
    self.assertFalse(rows(service.snapshot())["Mode"]["editable"])
    context = service._context()
    self.assertIsNone(context.rule({"type": "not", "condition": {"type": "future_rule"}}))
    self.assertIsNone(context.rule({"type": "not", "condition": {"type": "param", "key": "Unknown", "equals": False}}))

  def test_subitem_parent_rules_and_vehicle_brand(self):
    definition = schema([toggle("Parent", enablement=[{"type": "offroad_only"}], sub_items=[toggle("Child")])])
    definition["vehicle_settings"] = {"toyota": {"title": "Toyota", "items": [toggle("ToyotaSetting")]}}
    params = FakeParams({"Parent": False, "Child": False, "ToyotaSetting": False})
    service = remote.RemoteSettings(params, definition, lambda: {"brand": "hyundai"}, lambda: remote.DeviceState(True, True))
    snapshot_rows = rows(service.snapshot())
    self.assertFalse(snapshot_rows["Child"]["editable"])
    self.assertFalse(snapshot_rows["ToyotaSetting"]["visible"])
    self.assertFalse(snapshot_rows["ToyotaSetting"]["editable"])

  def test_unknown_rule_cannot_be_hidden_in_a_true_any_branch(self):
    service, _ = self.service([toggle("QuietMode", enablement=[{"type": "any", "conditions": [
      {"type": "param", "key": "QuietMode", "equals": False}, {"type": "future_rule"}]}])])
    row = rows(service.snapshot())["QuietMode"]
    self.assertFalse(row["editable"])
    self.assertEqual(row["reason"], "Unsupported settings rule.")
    self.assertEqual(service.set_setting("QuietMode", True, False)["status"], "rejected")

  def test_unsupported_numeric_control_is_readonly_in_snapshot(self):
    for constraints in ({}, {"min": 0, "max": 10, "step": 0}, {"min": 2, "max": 1, "step": 1}):
      with self.subTest(constraints=constraints):
        service, _ = self.service([{"key": "Amount", "widget": "option", "title": "Amount", **constraints}], {"Amount": 1})
        row = rows(service.snapshot())["Amount"]
        self.assertFalse(row["editable"])
        self.assertEqual(row["widget"], "info")
        self.assertFalse(set(row) & {"options", "min", "max", "step"})

  def test_number_constraints_and_finite_types(self):
    service, params = self.service([{"key": "Amount", "widget": "option", "title": "Amount", "min": 0.1, "max": 0.5, "step": 0.1}],
                                   {"Amount": 0.1})
    for value in (True, "0.2", None, float("nan"), float("inf"), -0.1, 0.6, 0.15, 10 ** 1000):
      with self.subTest(value=type(value).__name__):
        self.assertEqual(service.set_setting("Amount", value, 0.1)["status"], "rejected")
    self.assertEqual(service.set_setting("Amount", 0.3, 0.1)["status"], "saved")
    self.assertEqual(params.values["Amount"], 0.3)

  def test_dynamic_units_constraints_and_title(self):
    service, params = self.service([{"key": "Speed", "widget": "option", "title": "Speed", "unit": {"metric": "km/h", "imperial": "mph"},
      "min": {"metric": 0, "imperial": 0}, "max": {"metric": 20, "imperial": 10}, "step": 1,
      "title_param_suffix": {"param": "IsMetric", "values": {"true": "Metric", "false": "Imperial"}}}], {"Speed": 5})
    row = rows(service.snapshot())["Speed"]
    self.assertEqual((row["title"], row["unit"], row["max"]), ("Speed Imperial", "mph", 10))
    params.values["IsMetric"] = True
    row = rows(service.snapshot())["Speed"]
    self.assertEqual((row["title"], row["unit"], row["max"]), ("Speed Metric", "km/h", 20))

  def test_unreadable_and_unsupported_values_are_readonly(self):
    service, params = self.service()
    params.unreadable.add("QuietMode")
    row = rows(service.snapshot())["QuietMode"]
    self.assertIsNone(row["value"])
    self.assertFalse(row["editable"])
    self.assertEqual(row["widget"], "info")
    self.assertEqual(service.set_setting("QuietMode", True, None)["status"], "rejected")

  def test_confirmation_required_each_write(self):
    definition = schema([], sections=[{"title": "Tests", "attestation_required": True, "description": "Danger: test only.",
                                       "items": [toggle("TestMode")]}])
    params = FakeParams({"TestMode": False})
    service = remote.RemoteSettings(params, definition, dict, lambda: remote.DeviceState(False, False))
    self.assertEqual(rows(service.snapshot())["TestMode"]["confirmation"], "Danger: test only.")
    for confirmed in (False, None, 1, "true"):
      self.assertEqual(service.set_setting("TestMode", True, False, confirmed)["status"], "rejected")
    self.assertEqual(service.set_setting("TestMode", True, False, True)["status"], "saved")
    self.assertEqual(service.set_setting("TestMode", False, True)["status"], "rejected")

  def test_maneuver_companions_are_cleared_before_target(self):
    values = {"LateralManeuverMode": False, "LongitudinalManeuverMode": True, "JoystickDebugMode": True, "ExperimentalMode": True}
    service, params = self.service([toggle(key) for key in values], values)
    result = service.set_setting("LateralManeuverMode", True, False)
    self.assertEqual(result["status"], "saved")
    self.assertEqual(params.writes, [("ExperimentalMode", False), ("JoystickDebugMode", False),
                                   ("LongitudinalManeuverMode", False), ("LateralManeuverMode", True)])

  def test_toyota_companions_and_next_cycle_only(self):
    values = {"ToyotaEnforceStockLongitudinal": False, "AlphaLongitudinalEnabled": True, "ToyotaStopAndGoHack": True}
    service, params = self.service([toggle(key, needs_onroad_cycle=True) for key in values], values)
    result = service.set_setting("ToyotaEnforceStockLongitudinal", True, False, True)
    self.assertEqual(result["status"], "saved")
    self.assertEqual(params.writes, [("AlphaLongitudinalEnabled", False), ("ToyotaStopAndGoHack", False),
                                   ("ToyotaEnforceStockLongitudinal", True)])
    self.assertIn("next normal onroad cycle", result["message"])
    self.assertNotIn("OnroadCycleRequested", dict(params.writes))

  def test_offroad_mode_requires_disengagement_and_confirmation(self):
    service, params = self.service([toggle("OffroadMode")], {"OffroadMode": False})
    service.state = lambda: remote.DeviceState(True, True)
    self.assertEqual(service.set_setting("OffroadMode", True, False, True)["status"], "rejected")
    service.state = lambda: remote.DeviceState(True, False)
    self.assertEqual(service.set_setting("OffroadMode", True, False)["status"], "rejected")
    self.assertEqual(service.set_setting("OffroadMode", True, False, True)["status"], "saved")

  def test_recording_lock(self):
    service, params = self.service([toggle("RecordFront")], {"RecordFront": True, "RecordFrontLock": True})
    self.assertEqual(service.set_setting("RecordFront", False, True)["status"], "rejected")
    self.assertEqual(params.writes, [])

  def test_torque_default_removes_param_and_normalizes_readback(self):
    service, params = self.service([{"key": "TorqueControlTune", "widget": "multiple_button", "title": "Torque", "options": [
      {"value": "", "label": "Default"}, {"value": 2.0, "label": "2.0"}]}], {"TorqueControlTune": 2.0})
    params.defaults["TorqueControlTune"] = 0.0
    result = service.set_setting("TorqueControlTune", "", 2.0)
    self.assertEqual(result["status"], "saved")
    self.assertEqual(rows(result["snapshot"])["TorqueControlTune"]["value"], "")
    self.assertNotIn("TorqueControlTune", params.values)
    self.assertEqual(service.set_setting("TorqueControlTune", 2, "")["status"], "saved")
    self.assertIsInstance(params.values["TorqueControlTune"], float)

  def test_quickboot_updates_file_and_surfaces_partial_failure(self):
    with tempfile.TemporaryDirectory() as directory:
      marker = Path(directory) / "prebuilt"
      service, params = self.service([toggle("QuickBootToggle")], {"QuickBootToggle": False}, prebuilt_path=marker)
      self.assertEqual(service.set_setting("QuickBootToggle", True, False)["status"], "saved")
      self.assertTrue(marker.exists())
      self.assertEqual(service.set_setting("QuickBootToggle", False, True)["status"], "saved")
      self.assertFalse(marker.exists())
      params.fail.add("QuickBootToggle")
      result = service.set_setting("QuickBootToggle", True, False)
      self.assertEqual(result["status"], "failed")
      self.assertEqual(result["changed"], ["QuickBootToggle"])
      self.assertTrue(marker.exists())

  def test_write_failure_reports_partial_companions(self):
    values = {"JoystickDebugMode": False, "LongitudinalManeuverMode": True, "LateralManeuverMode": True}
    service, params = self.service([toggle(key) for key in values], values)
    params.fail.add("LateralManeuverMode")
    result = service.set_setting("JoystickDebugMode", True, False)
    self.assertEqual(result["status"], "failed")
    self.assertEqual(result["changed"], ["LongitudinalManeuverMode"])
    self.assertIs(params.values["JoystickDebugMode"], False)

  def test_device_normalization_is_not_reported_saved(self):
    service, params = self.service()
    params.after_write = lambda key, value: params.values.update({key: False})
    self.assertEqual(service.set_setting("QuietMode", True, False)["status"], "failed")

  def test_experimental_confirmation_does_not_set_acceptance(self):
    service, params = self.service([toggle("ExperimentalMode")], {"ExperimentalMode": False})
    self.assertEqual(service.set_setting("ExperimentalMode", True, False, True)["status"], "saved")
    self.assertNotIn("ExperimentalModeConfirmed", dict(params.writes))

  def test_real_schema_supported_shape_and_parameter_types(self):
    source = Path(__file__).resolve().parents[3]
    definition = json.loads((source / "sunnypilot/sunnylink/settings_ui.json").read_text())
    header = (source / "common/params_keys.h").read_text()
    kinds, defaults = {}, {}
    type_ids = {"STRING": 0, "BOOL": 1, "INT": 2, "FLOAT": 3}
    for key, body in re.findall(r'\{"([^"\n]+)", \{([^}]+)\}', header):
      fields = body.split(",", 2)
      kind = type_ids.get(fields[1].strip())
      if kind is None:
        continue
      kinds[key] = kind
      if len(fields) == 3:
        literal = fields[2].strip().strip('"')
        try:
          defaults[key] = {0: str, 1: lambda v: v == "1", 2: int, 3: float}[kind](literal)
        except ValueError:
          pass
    params = FakeParams(kinds=kinds, defaults=defaults)
    service = remote.RemoteSettings(params, definition, lambda: {"brand": "toyota"}, lambda: remote.DeviceState(False, False))
    snapshot = service.snapshot()
    self.assertEqual(snapshot["version"], 1)
    self.assertEqual(len(rows(snapshot)), 88)
    self.assertLess(len(json.dumps(snapshot, allow_nan=False)), 1024 * 1024)
    self.assertIn("models", {panel["id"] for panel in snapshot["panels"]})
    allowed = {"key", "title", "description", "section", "widget", "type", "value", "visible", "editable", "reason", "options",
               "min", "max", "step", "unit", "confirmation", "requires_restart"}
    for row in rows(snapshot).values():
      with self.subTest(key=row["key"]):
        self.assertFalse(set(row) - allowed)
        self.assertIn(row["type"], {"bool", "int", "float", "string"})
        for field in ("min", "max", "step"):
          if field in row:
            self.assertIsInstance(row[field], (int, float))
    self.assertEqual(params.writes, [])


class TestPortableAthenaRegistration(unittest.TestCase):
  def test_real_wrappers_register_without_importing_sunnylink(self):
    source = Path(__file__).resolve().parents[3] / "system/athena/athenad.py"
    tree = ast.parse(source.read_text())
    names = {"getDeviceSettings", "setDeviceSetting"}
    wrappers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    dispatcher = Dispatcher()
    namespace = {"dispatcher": dispatcher}
    exec(compile(ast.Module(body=wrappers, type_ignores=[]), str(source), "exec"), namespace)
    self.assertEqual(set(dispatcher), names)
    with patch.object(remote, "get_device_settings", return_value={"version": 1}) as get_settings:
      self.assertEqual(namespace["getDeviceSettings"](), {"version": 1})
      get_settings.assert_called_once_with()
    with patch.object(remote, "set_device_setting", return_value={"status": "saved"}) as set_setting:
      self.assertEqual(namespace["setDeviceSetting"]("QuietMode", True, False), {"status": "saved"})
      set_setting.assert_called_once_with("QuietMode", True, False, False)

  def test_settings_rpc_logs_only_method_not_values(self):
    source = Path(__file__).resolve().parents[3] / "system/athena/athenad.py"
    tree = ast.parse(source.read_text())
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "jsonrpc_handler")
    event = threading.Event()
    pending = [json.dumps({"method": "setDeviceSetting", "params": {"key": "QuietMode", "value": "private test value"}, "id": 1})]

    def receive(**kwargs):
      if pending:
        return pending.pop()
      event.set()
      raise queue.Empty

    log = Mock()
    namespace = {"threading": threading, "dispatcher": {}, "partial": functools.partial, "startLocalProxy": Mock(),
                 "recv_queue": SimpleNamespace(get=receive), "loads": json.loads, "is_call": lambda message: "method" in message,
                 "cloudlog": log, "send_queue_push": Mock(), "handle": Mock(return_value="{}"), "SEND_PRIORITY_HIGH": 0, "queue": queue}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), "exec"), namespace)
    namespace["jsonrpc_handler"](event)
    log.event.assert_called_once_with("athena.jsonrpc_handler.call_method", method="setDeviceSetting")

  def test_message_freshness_and_validity(self):
    message = SimpleNamespace(valid=True, logMonoTime=int(100e9), deviceState=SimpleNamespace(started=False))
    self.assertIsNotNone(remote._message_value(message, "deviceState", 104.9))
    self.assertIsNone(remote._message_value(message, "deviceState", 106))
    self.assertIsNone(remote._message_value(message, "deviceState", 99))
    message.valid = False
    self.assertIsNone(remote._message_value(message, "deviceState", 100))

  def test_live_sampler_accounts_for_mads_missing_and_stale_messages(self):
    def event(service, payload, age=0):
      return SimpleNamespace(valid=True, logMonoTime=int((100 - age) * 1e9), **{service: payload})

    for started, enabled, mads, missing, age, expected in (
      (False, False, False, True, 0, remote.DeviceState(False, False)),
      (True, False, False, False, 0, remote.DeviceState(True, False)),
      (True, False, True, False, 0, remote.DeviceState(True, True)),
      (True, True, False, False, 0, remote.DeviceState(True, True)),
      (True, False, False, True, 0, remote.DeviceState(True, None)),
      (True, False, False, False, 6, remote.DeviceState()),
    ):
      with self.subTest(expected=expected, mads=mads, missing=missing):
        events = {"deviceState": event("deviceState", SimpleNamespace(started=started), age),
                  "selfdriveState": event("selfdriveState", SimpleNamespace(enabled=enabled)),
                  "selfdriveStateSP": None if missing else event("selfdriveStateSP", SimpleNamespace(mads=SimpleNamespace(enabled=mads)))}
        messaging = SimpleNamespace(sub_sock=lambda service, **kwargs: service, recv_one=lambda service, events=events: events[service],
                                    recv_one_or_none=Mock(side_effect=lambda service, events=events: events[service]))
        with patch.dict(sys.modules, {"openpilot.cereal": SimpleNamespace(messaging=messaging)}), patch.object(remote.time, "monotonic", return_value=100):
          self.assertEqual(remote._live_state(), expected)
        if not started:
          messaging.recv_one_or_none.assert_not_called()


if __name__ == "__main__":
  unittest.main()
