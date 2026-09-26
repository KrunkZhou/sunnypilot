"""Run real producer loops with host substitutes for device IPC and persistence."""
import base64
import datetime
import importlib.util
import io
import json
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from openpilot import cereal
from openpilot.common import diagnostic_timing

BOOT_ID = "c08e8b38-8f57-4f39-a8c0-8bf894992b54"
WALL_NS = 1_790_394_870_545_273_123
MONO_NS = 48_104_948_503
ROOT = Path(__file__).parents[2]


def install_module(monkeypatch, name, **attributes):
  module = types.ModuleType(name)
  module.__dict__.update(attributes)
  monkeypatch.setitem(sys.modules, name, module)
  return module


def load_source(monkeypatch, name, relative):
  spec = importlib.util.spec_from_file_location(name, ROOT / relative)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  monkeypatch.setitem(sys.modules, name, module)
  spec.loader.exec_module(module)
  return module


@pytest.mark.parametrize("valid", [True, False])
def test_timed_emits_fresh_anchor_even_when_time_adjustment_is_unnecessary(monkeypatch, valid):
  gps = types.SimpleNamespace(hasFix=True, unixTimestampMillis=time.time_ns() // 1_000_000)
  class GPSSubMaster:
    updated = {"gpsLocation": True}
    logMonoTime = {"gpsLocation": MONO_NS - 1_000_000_000}
    def __init__(self):
      self.valid = {"gpsLocation": valid}
    def __getitem__(self, key):
      return gps
    def update(self, _):
      pass

  messaging = install_module(monkeypatch, "openpilot.cereal.messaging", PubMaster=Mock(), SubMaster=lambda _: GPSSubMaster(),
                             new_message=lambda _: types.SimpleNamespace(clocks=types.SimpleNamespace()))
  monkeypatch.setattr(cereal, "messaging", messaging, raising=False)
  install_module(monkeypatch, "openpilot.common.swaglog", cloudlog=Mock())
  install_module(monkeypatch, "openpilot.common.params", Params=Mock())
  install_module(monkeypatch, "openpilot.common.gps", get_gps_location_service=lambda _: "gpsLocation")
  timed = load_source(monkeypatch, "_diagnostic_timed", "system/timed.py")
  monkeypatch.setattr(diagnostic_timing, "get_boot_id", lambda: BOOT_ID)
  monkeypatch.setattr(diagnostic_timing, "time", types.SimpleNamespace(monotonic_ns=lambda: MONO_NS))
  monkeypatch.setattr(diagnostic_timing, "min_date", lambda: datetime.datetime(2025, 2, 21))
  timed.min_date = diagnostic_timing.min_date
  timed.time = types.SimpleNamespace(time_ns=lambda: WALL_NS, monotonic=lambda: MONO_NS / 1e9,
                                    sleep=Mock(side_effect=StopIteration))
  timed.subprocess = types.SimpleNamespace(run=Mock())
  with pytest.raises(StopIteration):
    timed.main()
  timed.subprocess.run.assert_not_called()
  timed.time.sleep.assert_called_once_with(10)
  if valid:
    timed.cloudlog.event.assert_called_once_with(
      "diagnostic.clock_anchor", version=1, source="gps", clock="monotonic", boot_id=BOOT_ID,
      monotonic_ns=str(MONO_NS - 1_000_000_000), wall_time_ns=str(gps.unixTimestampMillis * 1_000_000),
    )
  else:
    timed.cloudlog.event.assert_not_called()


@pytest.mark.parametrize("sunnylink", [False, True])
@pytest.mark.parametrize("boot_id", [BOOT_ID, None])
def test_stats_flush_has_exact_timestamp_and_reserved_fields(monkeypatch, tmp_path, sunnylink, boot_id):
  class Again(Exception):
    pass
  class EndFlush(Exception):
    pass
  socket = Mock()
  context = Mock(socket=Mock(return_value=socket))
  install_module(monkeypatch, "zmq", Context=types.SimpleNamespace(instance=lambda: context), PULL=1, PUSH=2, LINGER=3,
                 NOBLOCK=4, error=types.SimpleNamespace(Again=Again))
  state = types.SimpleNamespace(started=False)
  class StatsSubMaster:
    def __init__(self):
      self.calls = 0
    def __getitem__(self, key):
      return state
    def update(self):
      self.calls += 1
      if self.calls > 1:
        raise EndFlush
      state.started = True

  install_module(monkeypatch, "openpilot.cereal.messaging", SubMaster=lambda _: StatsSubMaster())
  install_module(monkeypatch, "openpilot.common.params", Params=lambda: types.SimpleNamespace(get=lambda key: "test-device"))
  install_module(monkeypatch, "openpilot.common.hardware", HARDWARE=types.SimpleNamespace(get_device_type=lambda: "mici"))
  install_module(monkeypatch, "openpilot.common.hardware.hw", Paths=types.SimpleNamespace(stats_root=lambda: str(tmp_path),
                                                                                         stats_sp_root=lambda: str(tmp_path)))
  install_module(monkeypatch, "openpilot.common.swaglog", cloudlog=Mock())
  output = []
  @contextmanager
  def atomic_write(path):
    stream = io.StringIO()
    yield stream
    output.extend(stream.getvalue().splitlines())
  install_module(monkeypatch, "openpilot.common.utils", atomic_write=atomic_write)
  metadata = types.SimpleNamespace(channel="test", openpilot=types.SimpleNamespace(version="test", is_dirty=False, git_normalized_origin="test"))
  install_module(monkeypatch, "openpilot.common.version", get_build_metadata=lambda: metadata)
  install_module(monkeypatch, "openpilot.system.loggerd.config", STATS_DIR_FILE_LIMIT=100, STATS_SOCKET="ipc://test", STATS_FLUSH_TIME_S=60)
  install_module(monkeypatch, "openpilot.common.realtime", Ratekeeper=Mock())
  module = load_source(monkeypatch, "openpilot.sunnypilot.system.statsd", "sunnypilot/system/statsd.py")
  if sunnylink:
    module = load_source(monkeypatch, "_diagnostic_sunnylink_statsd", "sunnypilot/sunnylink/statsd.py")
  module.capture_timing = Mock(return_value=diagnostic_timing.DiagnosticTiming(WALL_NS, MONO_NS, boot_id))
  values = ["voltage:12.5|g", "latency:2.0|sa", "latency:4.0|sa"]
  if sunnylink:
    payload = base64.b64encode(json.dumps({"value": 1, "rtzs_boot_id": "spoof", "rtzs_monotonic_ns": 1}).encode()).decode()
    values.append(f"raw_metric:{payload}|r")
  socket.recv_string.side_effect = [*values, Again()]
  if sunnylink:
    module.stats_main(types.SimpleNamespace(is_set=Mock(side_effect=[False, True])))
  else:
    with pytest.raises(EndFlush):
      module.main()
  assert len(output) == (3 if sunnylink else 2)
  module.capture_timing.assert_called_once()
  for line in output:
    series, fields, timestamp = line.split(" ")
    assert "rtzs_" not in series
    assert f"rtzs_monotonic_ns={MONO_NS}i" in fields
    assert fields.count("rtzs_monotonic_ns=") == 1
    assert timestamp == str(WALL_NS)
    assert "spoof" not in fields
    if boot_id is None:
      assert "rtzs_boot_id=" not in fields
    else:
      assert fields.count(f'rtzs_boot_id="{BOOT_ID}"') == 1
  socket.close.assert_called_once()
  context.term.assert_called_once()
