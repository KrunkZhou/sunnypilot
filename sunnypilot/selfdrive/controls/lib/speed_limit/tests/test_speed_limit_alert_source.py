from types import SimpleNamespace

import pytest

from cereal import custom
from openpilot.sunnypilot.selfdrive.selfdrived import events
from openpilot.sunnypilot.selfdrive.selfdrived.events import get_speed_limit_source_label


SpeedLimitSource = custom.LongitudinalPlanSP.SpeedLimit.Source


@pytest.mark.parametrize(("source", "map_source", "expected"), [
  (SpeedLimitSource.map, "ontario", "ON"),
  (SpeedLimitSource.map, "osm", "OSM"),
  (SpeedLimitSource.map, None, "OSM"),
  (SpeedLimitSource.car, None, "CAR"),
  (SpeedLimitSource.none, "ON", ""),
])
def test_speed_limit_source_label(source, map_source, expected):
  assert get_speed_limit_source_label(source, map_source) == expected


@pytest.mark.parametrize(("set_speed", "source", "button"), [
  (10.0, "ontario", "+"),
  (30.0, "osm", "-"),
])
def test_pre_active_alert_appends_source_after_button(monkeypatch, set_speed, source, button):
  monkeypatch.setattr(events, "IS_MICI", True)
  car_params = SimpleNamespace(openpilotLongitudinalControl=False, pcmCruise=False)
  car_state = SimpleNamespace(vCruiseCluster=0.0)
  sub_master = {
    "controlsState": SimpleNamespace(deprecated=SimpleNamespace(vCruise=set_speed)),
    "longitudinalPlanSP": SimpleNamespace(speedLimit=SimpleNamespace(resolver=SimpleNamespace(
      speedLimitFinalLast=20.0,
      source=SpeedLimitSource.map,
    ))),
    "liveMapDataSP": SimpleNamespace(speedLimitSource=source),
  }

  alert = events.speed_limit_pre_active_alert(car_params, car_state, sub_master, True, 0, None)

  expected_source = "ON" if source == "ontario" else "OSM"
  assert alert.alert_text_1 == f"72 km/h ? Press {button} | {expected_source}"
