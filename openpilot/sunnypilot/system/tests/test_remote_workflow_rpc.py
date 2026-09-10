"""Exercise the real lazy Athena wrappers and request redaction without hardware imports."""
import ast
import functools
import json
import queue
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpilot.sunnypilot.system import remote_maps, remote_models
from openpilot.system.athena.rpc import Dispatcher


SOURCE = Path(__file__).resolve().parents[3] / "system/athena/athenad.py"
METHODS = {"getDeviceModels", "manageDeviceModels", "getDeviceMaps", "manageDeviceMaps"}


class TestWorkflowRPC(unittest.TestCase):
  def test_lazy_wrappers_register_and_forward_exact_fields(self):
    tree = ast.parse(SOURCE.read_text())
    wrappers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in METHODS]
    dispatcher = Dispatcher()
    namespace = {"dispatcher": dispatcher}
    exec(compile(ast.Module(body=wrappers, type_ignores=[]), str(SOURCE), "exec"), namespace)
    self.assertEqual(set(dispatcher), METHODS)
    for module, suffix in ((remote_models, "Models"), (remote_maps, "Maps")):
      with self.subTest(suffix=suffix), patch.object(module, f"get_device_{suffix.lower()}", return_value={"version": 1}) as read:
        self.assertEqual(namespace[f"getDevice{suffix}"](), {"version": 1})
        read.assert_called_once_with()
      with self.subTest(suffix=suffix), patch.object(module, f"manage_device_{suffix.lower()}", return_value={"status": "accepted"}) as write:
        self.assertEqual(namespace[f"manageDevice{suffix}"]("refresh", expected_operation_id="observed"), {"status": "accepted"})
        write.assert_called_once_with("refresh", expected_operation_id="observed")

  def test_each_workflow_method_redacts_all_request_values(self):
    tree = ast.parse(SOURCE.read_text())
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "jsonrpc_handler")
    for method in METHODS:
      with self.subTest(method=method):
        event = threading.Event()
        pending = [json.dumps({"method": method, "params": {"ref": "private reference", "country": "private region"}, "id": 1})]

        def receive(pending=pending, event=event, **_kwargs):
          if pending:
            return pending.pop()
          event.set()
          raise queue.Empty

        log = Mock()
        namespace = {"threading": threading, "dispatcher": {}, "partial": functools.partial, "startLocalProxy": Mock(),
                     "recv_queue": SimpleNamespace(get=receive), "loads": json.loads, "is_call": lambda message: "method" in message,
                     "cloudlog": log, "send_queue_push": Mock(), "handle": Mock(return_value="{}"), "SEND_PRIORITY_HIGH": 0, "queue": queue}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), str(SOURCE), "exec"), namespace)
        namespace["jsonrpc_handler"](event)
        log.event.assert_called_once_with("athena.jsonrpc_handler.call_method", method=method)


if __name__ == "__main__":
  unittest.main()
