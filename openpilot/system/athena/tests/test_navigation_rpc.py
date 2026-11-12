"""Exercise the navigation RPCs without compiled device dependencies."""
import ast
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from openpilot.system.athena.rpc import Dispatcher, handle


@pytest.fixture
def navigation_rpc():
  source = Path(__file__).resolve().parents[1] / "athenad.py"
  methods = [node for node in ast.parse(source.read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name in ("setNavDestination", "clearNavDestination")]
  dispatcher = Dispatcher()
  params = Mock()
  namespace = {"dispatcher": dispatcher, "Params": lambda: params}
  exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)

  def call(method, arguments=None):
    return json.loads(handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": arguments}, dispatcher))

  return call, params


@pytest.mark.parametrize("metadata", [{}, {"place_name": "Home", "place_details": "Toronto"},
                                      {"place_name": "", "place_details": ""},
                                      {"place_name": None, "place_details": None}])
def test_set_destination_accepts_current_and_legacy_clients(navigation_rpc, metadata):
  call, params = navigation_rpc
  coordinates = {"latitude": 43.65, "longitude": -79.38}
  response = call("setNavDestination", coordinates | metadata)
  assert response == {"jsonrpc": "2.0", "id": 1, "result": {"success": 1}}
  expected = coordinates | {key: value for key, value in metadata.items() if value is not None}
  params.put.assert_called_once_with("NavDestination", expected, block=True)


@pytest.mark.parametrize("latitude,longitude", [(91, 0), (0, -181), (float("nan"), 0), (0, float("inf"))])
def test_invalid_legacy_destination_does_not_overwrite_route(navigation_rpc, latitude, longitude):
  call, params = navigation_rpc
  response = call("setNavDestination", {"latitude": latitude, "longitude": longitude, "place_name": "Home", "place_details": "Toronto"})
  assert response["result"] == {"success": 0, "error": "Invalid destination coordinates"}
  params.put.assert_not_called()


def test_clear_destination_remains_registered(navigation_rpc):
  call, params = navigation_rpc
  assert call("clearNavDestination")["result"] == {"success": 1}
  params.remove.assert_called_once_with("NavDestination")
