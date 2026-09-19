"""The six reviewed model settings use real persisted units, not widget units."""
import json
import unittest
from pathlib import Path

from openpilot.sunnypilot.system import remote_settings as remote
from openpilot.sunnypilot.system.tests.test_remote_settings import FakeParams, rows


class TestModelSettings(unittest.TestCase):
  def setUp(self):
    source = Path(__file__).resolve().parents[2] / "sunnylink" / "settings_ui.json"
    definition = json.loads(source.read_text())
    definition["panels"] = [panel for panel in definition["panels"] if panel["id"] == "models"]
    definition["vehicle_settings"] = {}
    self.params = FakeParams({"IsMetric": True, "ShowAdvancedControls": True, "LaneTurnDesire": True,
                              "LaneTurnValue": 19.62, "LagdToggle": False, "LagdToggleDelay": 0.2, "CameraOffset": 0.0,
                              "NeuralNetworkLateralControl": False, "EnforceTorqueControl": False, "LateralJerkTorqueController": False})
    self.custom = True
    self.capabilities = {"torque_allowed": True, "steer_control_type": "torque"}
    self.service = remote.RemoteSettings(self.params, definition, lambda: self.capabilities, lambda: remote.DeviceState(False, False),
                                         custom_model=lambda: self.custom)

  def test_exact_allowlist(self):
    self.assertEqual(set(rows(self.service.snapshot())), remote._MODEL_SETTINGS)

  def test_lane_turn_value_stays_mph_even_when_metric(self):
    row = rows(self.service.snapshot())["LaneTurnValue"]
    self.assertEqual((row["value"], row["min"], row["max"], row["step"], row["unit"]), (19.62, 5, 20, 0.01, "mph"))
    self.assertEqual(self.service.set_setting("LaneTurnValue", 19.26, 19.62)["status"], "saved")
    self.assertEqual(self.params.get("LaneTurnValue"), 19.26)
    self.assertEqual(self.service.set_setting("LaneTurnValue", 21, 19.26)["status"], "rejected")
    self.assertEqual(self.service.set_setting("LaneTurnValue", 4.99, 19.26)["status"], "rejected")
    self.assertEqual(self.service.set_setting("LaneTurnValue", 19.265, 19.26)["status"], "rejected")

  def test_conflict_compares_full_stored_value_not_rounded_metric_display(self):
    self.assertEqual(self.service.set_setting("LaneTurnValue", 19.26, 19.61)["status"], "conflict")
    self.assertEqual(self.params.writes, [])

  def test_seconds_and_meters_are_not_scaled_by_one_hundred(self):
    self.assertEqual(self.service.set_setting("LagdToggleDelay", 0.21, 0.2)["status"], "saved")
    self.assertEqual(self.service.set_setting("CameraOffset", -0.13, 0.0)["status"], "saved")
    self.assertEqual(self.params.get("LagdToggleDelay"), 0.21)
    self.assertEqual(self.params.get("CameraOffset"), -0.13)
    self.assertEqual(self.service.set_setting("CameraOffset", 0.351, -0.13)["status"], "rejected")

  def test_camera_offset_requires_custom_active_model(self):
    self.custom = False
    self.assertFalse(rows(self.service.snapshot())["CameraOffset"]["editable"])
    self.assertEqual(self.service.set_setting("CameraOffset", 0.1, 0.0)["status"], "rejected")

  def test_tuning_dependencies_and_bool_types(self):
    self.params.values["LagdToggle"] = True
    self.assertFalse(rows(self.service.snapshot())["LagdToggleDelay"]["editable"])
    self.params.values["LaneTurnDesire"] = False
    self.assertFalse(rows(self.service.snapshot())["LaneTurnValue"]["editable"])
    self.assertEqual(self.service.set_setting("LagdToggle", 0, True)["status"], "rejected")
    self.params.values["ShowAdvancedControls"] = False
    self.assertFalse(rows(self.service.snapshot())["CameraOffset"]["editable"])

  def test_nnlc_retains_offroad_exclusions_and_cycle_metadata(self):
    row = rows(self.service.snapshot())["NeuralNetworkLateralControl"]
    self.assertIn("next normal onroad cycle", row["requires_restart"])
    self.params.values["LateralJerkTorqueController"] = True
    self.assertFalse(rows(self.service.snapshot())["NeuralNetworkLateralControl"]["editable"])
    self.params.values["LateralJerkTorqueController"] = False
    self.service.state = lambda: remote.DeviceState(True, False)
    self.assertFalse(rows(self.service.snapshot())["NeuralNetworkLateralControl"]["editable"])
    self.service.state = lambda: remote.DeviceState(False, False)
    self.capabilities.update(torque_allowed=False, steer_control_type="angle")
    self.assertFalse(rows(self.service.snapshot())["NeuralNetworkLateralControl"]["visible"])


if __name__ == "__main__":
  unittest.main()
