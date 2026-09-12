"""Exercise the real map publisher with isolated native messaging/Params boundaries."""

from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.mark.parametrize("scenario", [
  "map_priority", "invalid_map", "invalid_database", "location_validity", "lost_location",
  "map_returns", "publish_lookahead", "constructor", "missing_database", "corrupt_database", "source_age", "startup", "suspend_clock",
])
def test_map_first_ontario_fallback(scenario, tmp_path):
  # Keep boundary replacements out of other tests that import native services.
  result = subprocess.run(
    [sys.executable, str(Path(__file__).resolve()), scenario, str(tmp_path)],
    capture_output=True, text=True, timeout=30,
  )
  assert result.returncode == 0, result.stdout + result.stderr


def _exercise(scenario: str, temporary_root: Path) -> None:
  class FakeParams:
    def __init__(self, *_args):
      self.values = {}

    def get(self, key):
      return self.values.get(key)

    def put(self, key, value, **_kwargs):
      self.values[key] = value

  class FakeSubMaster:
    def __init__(self, _services):
      self.location = SimpleNamespace(
        status="valid", gpsOK=True,
        positionGeodetic=SimpleNamespace(valid=True, value=[43.0, -79.0, 0.0]),
        calibratedOrientationNED=SimpleNamespace(valid=True, value=[0.0, 0.0, 0.0]),
      )
      self.alive = {"liveLocationKalman": True}
      self.valid = {"liveLocationKalman": True}
      # Match the clock used by C++ MessageBuilder::initEvent().
      self.logMonoTime = {"liveLocationKalman": time.clock_gettime_ns(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))}

    def __getitem__(self, _service):
      return self.location

    def update(self, _timeout):
      pass

  class FakePubMaster:
    def __init__(self, _services):
      self.messages = []

    def send(self, service, message):
      assert service == "liveMapDataSP"
      self.messages.append(message)

  class FakeDatabase:
    def __init__(self, speed=50 / 3.6):
      self.speed = speed
      self.calls = []

    def lookup(self, position, bearing):
      self.calls.append((position, bearing))
      return self.speed

  cereal = ModuleType("openpilot.cereal")
  cereal.log = SimpleNamespace(LiveLocationKalman=SimpleNamespace(Status=SimpleNamespace(valid="valid")))
  messaging = ModuleType("openpilot.cereal.messaging")
  messaging.SubMaster = FakeSubMaster
  messaging.PubMaster = FakePubMaster
  messaging.new_message = lambda _service: SimpleNamespace(valid=False, liveMapDataSP=SimpleNamespace())
  cereal.messaging = messaging
  params = ModuleType("openpilot.common.params")
  params.Params = FakeParams
  cruise = ModuleType("openpilot.selfdrive.car.cruise")
  cruise.V_CRUISE_UNSET = 255
  swaglog = ModuleType("openpilot.common.swaglog")
  swaglog.cloudlog = SimpleNamespace(debug=lambda *_args: None)

  with patch.dict(sys.modules, {
    "openpilot.cereal": cereal,
    "openpilot.cereal.messaging": messaging,
    "openpilot.common.params": params,
    "openpilot.selfdrive.car.cruise": cruise,
    "openpilot.common.swaglog": swaglog,
  }):
    from openpilot.sunnypilot.mapd.live_map_data.base_map_data import MAX_SPEED_LIMIT
    from openpilot.sunnypilot.mapd.live_map_data.osm_map_data import OsmMapData
    from openpilot.sunnypilot.mapd.live_map_data.speed_limit_database import SpeedLimitDatabase

    database = FakeDatabase()
    map_data = OsmMapData(database)
    map_data.mem_params.values["MapSpeedLimit"] = 0.0
    map_data.update_location()

    if scenario == "map_priority":
      for limit in (80 / 3.6, "22.5"):
        map_data.mem_params.values["MapSpeedLimit"] = limit
        assert map_data.get_current_speed_limit() == float(limit)
      # Valid map limits keep their existing behavior independently of fallback GPS gates.
      map_data.sm.location.gpsOK = False
      assert map_data.get_current_speed_limit() == 22.5
      assert database.calls == []

    elif scenario == "invalid_map":
      for limit in (None, "", 0.0, -1.0, "bad", [], float("nan"), float("inf"), MAX_SPEED_LIMIT):
        map_data.mem_params.values["MapSpeedLimit"] = limit
        previous = len(database.calls)
        assert map_data.get_current_speed_limit() == database.speed
        assert len(database.calls) == previous + 1

    elif scenario == "invalid_database":
      for limit in (0.0, -1.0, float("nan"), float("inf"), MAX_SPEED_LIMIT):
        database.speed = limit
        map_data.publish()
        message = map_data.pm.messages[-1].liveMapDataSP
        assert message.speedLimit == 0.0
        assert not message.speedLimitValid

    elif scenario == "location_validity":
      for invalid in ("alive", "valid", "status", "position", "orientation", "gps"):
        item = OsmMapData(FakeDatabase())
        if invalid in ("alive", "valid"):
          getattr(item.sm, invalid)["liveLocationKalman"] = False
        elif invalid == "status":
          item.sm.location.status = "invalid"
        elif invalid == "gps":
          item.sm.location.gpsOK = False
        else:
          field = "positionGeodetic" if invalid == "position" else "calibratedOrientationNED"
          getattr(item.sm.location, field).valid = False
        item.tick()
        assert item.speed_limit_database.calls == []
        assert item.pm.messages[-1].liveMapDataSP.speedLimit == 0.0

    elif scenario == "lost_location":
      map_data.tick()
      assert map_data.pm.messages[-1].liveMapDataSP.speedLimit == database.speed
      assert len(database.calls) == 1
      old_position = map_data.last_position
      map_data.sm.location.positionGeodetic.valid = False
      map_data.tick()
      assert map_data.last_position is old_position
      assert map_data.pm.messages[-1].liveMapDataSP.speedLimit == 0.0
      assert len(database.calls) == 1

    elif scenario == "map_returns":
      map_data.tick()
      assert map_data.pm.messages[-1].liveMapDataSP.speedLimit == database.speed
      map_data.mem_params.values["MapSpeedLimit"] = 80 / 3.6
      map_data.tick()
      assert map_data.pm.messages[-1].liveMapDataSP.speedLimit == 80 / 3.6
      assert len(database.calls) == 1

    elif scenario == "publish_lookahead":
      map_data.mem_params.values.update({
        "RoadName": "Map road", "NextMapSpeedLimit": {"speedlimit": 30 / 3.6, "latitude": 43.001, "longitude": -79.0},
      })
      map_data.tick()
      message = map_data.pm.messages[-1]
      assert message.valid
      assert message.liveMapDataSP.speedLimitValid
      assert message.liveMapDataSP.speedLimit == database.speed
      assert message.liveMapDataSP.speedLimitAheadValid
      assert message.liveMapDataSP.speedLimitAhead == 30 / 3.6
      assert 110 < message.liveMapDataSP.speedLimitAheadDistance < 112
      assert message.liveMapDataSP.roadName == "Map road"
      assert map_data.mem_params.values["MapSpeedLimit"] == 0.0

    elif scenario == "constructor":
      assert isinstance(OsmMapData().speed_limit_database, SpeedLimitDatabase)
      assert map_data.speed_limit_database is database

    elif scenario in ("missing_database", "corrupt_database"):
      database_path = temporary_root / "speed_limits.sqlite"
      if scenario == "corrupt_database":
        database_path.write_bytes(b"corrupt database")
      map_data.speed_limit_database = SpeedLimitDatabase(database_path)
      map_data.tick()
      assert map_data.pm.messages[-1].liveMapDataSP.speedLimit == 0.0
      map_data.mem_params.values["MapSpeedLimit"] = 80 / 3.6
      map_data.tick()
      assert map_data.pm.messages[-1].liveMapDataSP.speedLimit == 80 / 3.6

    elif scenario == "source_age":
      # A newly received envelope must not revive an old location sample.
      fresh_time = map_data.sm.logMonoTime["liveLocationKalman"]
      for timestamp in (0, fresh_time - 5_000_000_000, fresh_time + 5_000_000_000):
        map_data.sm.logMonoTime["liveLocationKalman"] = timestamp
        map_data.tick()
        assert map_data.pm.messages[-1].liveMapDataSP.speedLimit == 0.0
      assert database.calls == []

    elif scenario == "startup":
      item = OsmMapData(database)
      item.last_position = map_data.last_position
      item.last_bearing = map_data.last_bearing
      assert item.get_current_speed_limit() == 0.0
      assert database.calls == []

    elif scenario == "suspend_clock":
      # Suspend advances BOOTTIME while MONOTONIC stands still; fresh fixes remain usable.
      map_data.sm.logMonoTime["liveLocationKalman"] = 199_900_000_000
      with (patch.object(time, "CLOCK_BOOTTIME", 123, create=True),
            patch.object(time, "clock_gettime", return_value=200.0) as read_clock,
            patch.object(time, "monotonic", return_value=100.0)):
        assert map_data.get_current_speed_limit() == database.speed
        read_clock.assert_called_once_with(123)
    else:
      raise AssertionError(scenario)


if __name__ == "__main__":
  _exercise(sys.argv[1], Path(sys.argv[2]))
