from types import SimpleNamespace

import pytest

from cereal import custom
from openpilot.sunnypilot.selfdrive.selfdrived import events
from openpilot.sunnypilot.selfdrive.selfdrived.events import get_speed_limit_source_label


SpeedLimitSource = custom.LongitudinalPlanSP.SpeedLimit.Source


@pytest.mark.parametrize(("source", "map_source", "expected"), [
  (SpeedLimitSource.map, "ON", "ON"),
  (SpeedLimitSource.map, "OSM", "OSM"),
  (SpeedLimitSource.map, None, "OSM"),
  (SpeedLimitSource.car, None, "CAR"),
  (SpeedLimitSource.none, "ON", ""),
])
def test_speed_limit_source_label(source, map_source, expected):
  assert get_speed_limit_source_label(source, map_source) == expected


class FakeParams:
  def __init__(self, source):
    self.source = source

  def get(self, key):
    assert key == "MapSpeedLimitSource"
    return self.source


@pytest.mark.parametrize(("set_speed", "source", "button"), [
  (10.0, "ON", "+"),
  (30.0, "OSM", "-"),
])
def test_pre_active_alert_appends_source_after_button(monkeypatch, set_speed, source, button):
  monkeypatch.setattr(events, "IS_MICI", True)
  monkeypatch.setattr(events, "MEM_PARAMS", FakeParams(source))
  car_params = SimpleNamespace(openpilotLongitudinalControl=False, pcmCruise=False)
  car_state = SimpleNamespace(vCruiseCluster=0.0)
  sub_master = {
    "controlsState": SimpleNamespace(deprecated=SimpleNamespace(vCruise=set_speed)),
    "longitudinalPlanSP": SimpleNamespace(speedLimit=SimpleNamespace(resolver=SimpleNamespace(
      speedLimitFinalLast=20.0,
      source=SpeedLimitSource.map,
    ))),
  }

  alert = events.speed_limit_pre_active_alert(car_params, car_state, sub_master, True, 0, None)

  assert alert.alert_text_1 == f"72 km/h ? Press {button} | {source}"
