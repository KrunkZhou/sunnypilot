import json
import sys
import types

import pytest

from openpilot.system.sentryd import runtime


def test_runtime_params_bypasses_stale_prebuilt_key_map_for_only_sentry_keys(tmp_path, monkeypatch) -> None:
  module = types.ModuleType("openpilot.common.params")

  class ParamKeyType:
    BOOL = 1
    JSON = 5

  class UnknownKeyName(Exception):
    pass

  opened_paths = []

  class PrebuiltParams:
    def __init__(self, path):
      opened_paths.append(path)
      self.values = {}

    def check_key(self, key):
      raise UnknownKeyName(key)

    def get_type(self, key):
      self.check_key(key)

    def put(self, key, value, block=False):
      encoded = self.check_key(key)
      assert self.get_type(encoded) == ParamKeyType.JSON
      self.values[encoded] = json.dumps(value)

    def get(self, key, block=False, return_default=False):
      encoded = self.check_key(key)
      value = self.values.get(encoded)
      return json.loads(value) if value is not None else None

    def put_bool(self, key, value, block=False):
      encoded = self.check_key(key)
      assert self.get_type(encoded) == ParamKeyType.BOOL
      self.values[encoded] = "1" if value else "0"

    def get_bool(self, key, block=False):
      return self.values.get(self.check_key(key)) == "1"

    def remove(self, key):
      self.values.pop(self.check_key(key), None)

  module.ParamKeyType = ParamKeyType
  module.Params = PrebuiltParams
  module.UnknownKeyName = UnknownKeyName
  module.ensure_bytes = lambda value: value.encode() if isinstance(value, str) else value
  monkeypatch.setitem(sys.modules, "openpilot.common.params", module)
  root = tmp_path / "volatile"
  monkeypatch.setenv("SENTRY_RUNTIME_ROOT", str(root))

  params = runtime.runtime_params()
  params.put_bool("SentryRuntimeEnabled", True, block=True)
  params.put("SentryRuntimeStatus", {"state": "armed"}, block=True)
  assert params.get_bool("SentryRuntimeEnabled")
  assert params.get("SentryRuntimeStatus") == {"state": "armed"}
  with pytest.raises(UnknownKeyName):
    params.put("enabled", {"persistent": True})
  assert opened_paths == [str(root)]


def test_capture_lease_expires_and_rejects_implausible_duration() -> None:
  class Params:
    def __init__(self):
      self.value = None

    def put(self, _key, value, block=False):
      self.value = value

    def get(self, _key):
      return self.value

  params = Params()
  runtime.set_capture_lease("request", 120.0, params)
  assert runtime.capture_lease_active(params, now=100.0)
  assert not runtime.capture_lease_active(params, now=120.0)
  runtime.set_capture_lease("request", 200.0, params)
  assert not runtime.capture_lease_active(params, now=100.0)


@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_enabled_verifies_successful_write(enabled) -> None:
  class Params:
    value = not enabled

    def put_bool(self, key, value, block=False):
      assert key == "SentryRuntimeEnabled" and block
      self.value = value

    def get_bool(self, key):
      assert key == "SentryRuntimeEnabled"
      return self.value

  params = Params()
  runtime.set_runtime_enabled(enabled, params)
  assert runtime.runtime_enabled(params) == enabled


@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_enabled_rejects_silently_failed_write(enabled) -> None:
  class Params:
    def put_bool(self, key, value, block=False):
      assert key == "SentryRuntimeEnabled" and value == enabled and block

    def get_bool(self, key):
      assert key == "SentryRuntimeEnabled"
      return not enabled

  with pytest.raises(RuntimeError, match="Could not update Sentry sensor demand"):
    runtime.set_runtime_enabled(enabled, Params())
