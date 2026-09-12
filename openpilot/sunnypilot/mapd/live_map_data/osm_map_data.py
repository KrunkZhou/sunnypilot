"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import math
import platform
import time

from openpilot.cereal import log
from openpilot.common.params import Params
from openpilot.sunnypilot.mapd.live_map_data.base_map_data import BaseMapData, MAX_SPEED_LIMIT
from openpilot.sunnypilot.mapd.live_map_data.speed_limit_database import SpeedLimitDatabase
from openpilot.sunnypilot.navd.helpers import Coordinate


MAX_DATABASE_LOCATION_AGE = 1.0  # seconds; mapd_manager polls location once per second.


class OsmMapData(BaseMapData):
  def __init__(self, speed_limit_database: SpeedLimitDatabase | None = None):
    super().__init__()
    self.mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else self.params
    self.speed_limit_database = speed_limit_database if speed_limit_database is not None else SpeedLimitDatabase()

  def update_location(self) -> None:
    location = self.sm['liveLocationKalman']
    self.localizer_valid = (location.status == log.LiveLocationKalman.Status.valid) and location.positionGeodetic.valid

    if self.localizer_valid:
      self.last_bearing = math.degrees(location.calibratedOrientationNED.value[2])
      self.last_position = Coordinate(location.positionGeodetic.value[0], location.positionGeodetic.value[1])

    if self.last_position is None:
      return

    params = {
      "latitude": self.last_position.latitude,
      "longitude": self.last_position.longitude,
    }

    if self.last_bearing is not None:
      params['bearing'] = self.last_bearing

    self.mem_params.put("LastGPSPosition", json.dumps(params), block=True)

  def get_current_speed_limit(self) -> float:
    try:
      map_speed_limit = float(self.mem_params.get("MapSpeedLimit") or 0.0)
    except (TypeError, ValueError):
      map_speed_limit = 0.0
    if 0.0 < map_speed_limit < MAX_SPEED_LIMIT:
      return map_speed_limit

    # The database is a fallback for the current limit, using only a live GPS fix.
    location = self.sm['liveLocationKalman']
    if not (self.sm.alive['liveLocationKalman'] and self.sm.valid['liveLocationKalman'] and
            self.localizer_valid and location.gpsOK and location.calibratedOrientationNED.valid):
      return 0.0
    location_time = self.sm.logMonoTime['liveLocationKalman'] * 1e-9
    # C++ locationd timestamps include suspend time on Linux (CLOCK_BOOTTIME).
    now = time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))
    if location_time <= 0.0 or not 0.0 <= now - location_time <= MAX_DATABASE_LOCATION_AGE:
      return 0.0

    database_speed_limit = self.speed_limit_database.lookup(self.last_position, self.last_bearing)
    return database_speed_limit if 0.0 < database_speed_limit < MAX_SPEED_LIMIT else 0.0

  def get_current_road_name(self) -> str:
    return str(self.mem_params.get("RoadName") or "")

  def get_next_speed_limit_and_distance(self) -> tuple[float, float]:
    next_speed_limit_section_str = self.mem_params.get("NextMapSpeedLimit")
    next_speed_limit_section = next_speed_limit_section_str if next_speed_limit_section_str else {}
    next_speed_limit = next_speed_limit_section.get('speedlimit', 0.0)
    next_speed_limit_latitude = next_speed_limit_section.get('latitude')
    next_speed_limit_longitude = next_speed_limit_section.get('longitude')
    next_speed_limit_distance = 0.0

    if next_speed_limit_latitude and next_speed_limit_longitude:
      next_speed_limit_coordinates = Coordinate(next_speed_limit_latitude, next_speed_limit_longitude)
      next_speed_limit_distance = (self.last_position or Coordinate(0, 0)).distance_to(next_speed_limit_coordinates)

    return next_speed_limit, next_speed_limit_distance
