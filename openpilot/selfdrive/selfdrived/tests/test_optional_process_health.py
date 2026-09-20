"""Exercise the production process-health checks without device-native sockets."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


SELFDRIVED = Path(__file__).parents[1] / "selfdrived.py"


class ManagerState(dict):
  def __init__(self, processes, camera_alive=True, camera_frequency_ok=True, received=True):
    super().__init__(managerState=SimpleNamespace(processes=processes))
    self.recv_frame = {"managerState": int(received)}
    self.camera_alive = camera_alive
    self.camera_frequency_ok = camera_frequency_ok

  def all_alive(self, _services):
    return self.camera_alive

  def all_freq_ok(self, _services):
    return self.camera_frequency_ok


def process(name, running=False, should_be_running=True):
  return SimpleNamespace(name=name, running=running, shouldBeRunning=should_be_running)


class TestOptionalProcessHealth(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    # The checkout contains device-native dependencies. Compile the real
    # constructor assignment and complete process-health branch, including its
    # camera fallback, rather than reimplementing their decisions in this test.
    tree = ast.parse(SELFDRIVED.read_text())
    control = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SelfdriveD")
    constructor = next(node for node in control.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    ignored = next(node for node in constructor.body if isinstance(node, ast.Assign) and any(
      isinstance(target, ast.Attribute) and target.attr == "ignored_processes" for target in node.targets))
    events = next(node for node in control.body if isinstance(node, ast.FunctionDef) and node.name == "update_events")
    start = next(i for i, node in enumerate(events.body) if isinstance(node, ast.Assign) and any(
      isinstance(target, ast.Name) and target.id == "not_running" for target in node.targets))
    branch = events.body[start:start + 3]
    assert isinstance(branch[1], ast.If) and isinstance(branch[2], ast.If) and branch[2].orelse
    cls.ignored_code = compile(ast.Module(body=[ignored], type_ignores=[]), str(SELFDRIVED), "exec")
    cls.health_code = compile(ast.Module(body=branch, type_ignores=[]), str(SELFDRIVED), "exec")

  def setUp(self):
    self.logged = []
    self.control = SimpleNamespace(events=set(), not_running_prev=None, rk=SimpleNamespace(lagging=False),
                                   camera_packets=["narrowRoadCameraState", "wideRoadCameraState"])
    self.env = {"self": self.control, "SIMULATION": False,
                "EventName": SimpleNamespace(processNotRunning="processNotRunning", cameraMalfunction="cameraMalfunction",
                                             cameraFrameRate="cameraFrameRate"),
                "cloudlog": SimpleNamespace(event=lambda event, **fields: self.logged.append((event, fields)))}
    exec(self.ignored_code, self.env)

  def update(self, processes, **health):
    self.control.events.clear()
    self.control.sm = ManagerState(processes, **health)
    exec(self.health_code, self.env)
    return self.control.events

  def test_optional_telemetry_failure_does_not_block_driving_and_stays_diagnostic(self):
    self.assertEqual(self.update([process("vehicle_telemetryd")]), set())
    self.assertEqual(self.logged, [("process_not_running", {"not_running": {"vehicle_telemetryd"}, "error": True})])
    self.assertEqual(self.update([process("vehicle_telemetryd"), process("mapd")]), set())

  def test_essential_process_failures_still_block_with_or_without_telemetry(self):
    for essential in ("controlsd", "modeld", "camerad", "pandad"):
      for optional in ([], [process("vehicle_telemetryd")]):
        with self.subTest(essential=essential, telemetry_failed=bool(optional)):
          self.assertEqual(self.update([process(essential), *optional]), {"processNotRunning"})

  def test_recovered_essential_process_clears_block_while_telemetry_is_still_down(self):
    self.assertEqual(self.update([process("vehicle_telemetryd"), process("modeld")]), {"processNotRunning"})
    self.assertEqual(self.update([process("vehicle_telemetryd"), process("modeld", running=True)]), set())

  def test_optional_failure_does_not_skip_camera_health_checks(self):
    telemetry = [process("vehicle_telemetryd")]
    self.assertEqual(self.update(telemetry, camera_alive=False), {"cameraMalfunction"})
    self.assertEqual(self.update(telemetry, camera_frequency_ok=False), {"cameraFrameRate"})

  def test_only_received_expected_process_failures_block(self):
    self.assertEqual(self.update([process("modeld", should_be_running=False)]), set())
    self.assertEqual(self.update([process("modeld")], received=False), set())


if __name__ == "__main__":
  unittest.main()
