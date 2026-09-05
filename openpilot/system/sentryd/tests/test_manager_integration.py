import ast
import operator
from pathlib import Path


PROCESS_CONFIG = Path(__file__).parents[2] / "manager" / "process_config.py"


def _process_call(tree: ast.Module, name: str) -> ast.Call:
  for node in ast.walk(tree):
    if (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and
        node.args[0].value == name):
      return node
  raise AssertionError(f"missing managed process: {name}")


def test_sentry_process_lifecycle_predicates_are_wired() -> None:
  tree = ast.parse(PROCESS_CONFIG.read_text())
  sentryd = _process_call(tree, "sentryd")
  sensord = _process_call(tree, "sensord")
  camerad = _process_call(tree, "camerad")
  assert isinstance(sentryd.args[2], ast.Name) and sentryd.args[2].id == "sentry_offroad"
  assert isinstance(sensord.args[2], ast.Name) and sensord.args[2].id == "sentry_sensor"
  assert any(isinstance(node, ast.Name) and node.id == "sentry_capture" for node in ast.walk(camerad.args[3]))


def test_camerad_three_way_demand_uses_a_valid_nested_predicate() -> None:
  tree = ast.parse(PROCESS_CONFIG.read_text())
  or_definition = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "or_")
  namespace = {"operator": operator}
  exec(compile(ast.Module(body=[or_definition], type_ignores=[]), str(PROCESS_CONFIG), "exec"), namespace)
  namespace.update({
    "driverview": lambda *_: False,
    "livestream": lambda *_: False,
    "sentry_capture": lambda *_: True,
  })
  expression = ast.Expression(_process_call(tree, "camerad").args[3])
  predicate = eval(compile(expression, str(PROCESS_CONFIG), "eval"), namespace)
  assert predicate(False, object(), object())
