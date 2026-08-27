"""Indexed offline road speed-limit lookup for mapd."""

from __future__ import annotations

import math
import sqlite3
import struct
import zlib
from pathlib import Path
from typing import Protocol

from openpilot.common.basedir import BASEDIR
from openpilot.common.hardware.hw import Paths


SPEED_LIMIT_DATABASE_NAME = "ontario_speed_limits.sqlite"
BUNDLED_SPEED_LIMIT_DATABASE_PATH = Path(BASEDIR) / "sunnypilot" / "mapd" / "data" / SPEED_LIMIT_DATABASE_NAME
SEARCH_RADIUS_METERS = 60.0
MAX_ROAD_DISTANCE_METERS = 35.0
MAX_HEADING_ERROR_DEGREES = 60.0
METERS_PER_LATITUDE_DEGREE = 111_132.0
METERS_PER_LONGITUDE_DEGREE = 111_320.0
COORDINATE_SCALE = 1_000_000


class Position(Protocol):
  latitude: float
  longitude: float


def default_speed_limit_database_path() -> Path:
  if BUNDLED_SPEED_LIMIT_DATABASE_PATH.is_file():
    return BUNDLED_SPEED_LIMIT_DATABASE_PATH
  return Path(Paths.mapd_root()) / SPEED_LIMIT_DATABASE_NAME


def angular_difference(first: float, second: float) -> float:
  return abs((first - second + 180.0) % 360.0 - 180.0)


def decode_geometry(blob: bytes) -> list[tuple[float, float]]:
  raw = zlib.decompress(blob)
  if len(raw) < 12 or len(raw) % 4:
    raise ValueError("invalid speed-limit geometry")
  count = struct.unpack_from("<I", raw)[0]
  values = struct.unpack_from(f"<{(len(raw) - 4) // 4}i", raw, 4)
  if count < 2 or len(values) != count * 2:
    raise ValueError("speed-limit geometry point count mismatch")

  longitude, latitude = values[0], values[1]
  points = [(longitude / COORDINATE_SCALE, latitude / COORDINATE_SCALE)]
  for index in range(2, len(values), 2):
    longitude += values[index]
    latitude += values[index + 1]
    points.append((longitude / COORDINATE_SCALE, latitude / COORDINATE_SCALE))
  return points


def closest_segment(position: Position, points: list[tuple[float, float]]) -> tuple[float, float]:
  """Return distance in metres and geometry-positive heading of the nearest segment."""
  longitude_scale = METERS_PER_LONGITUDE_DEGREE * math.cos(math.radians(position.latitude))
  closest_distance = math.inf
  closest_heading = 0.0

  for start, end in zip(points, points[1:]):
    start_x = (start[0] - position.longitude) * longitude_scale
    start_y = (start[1] - position.latitude) * METERS_PER_LATITUDE_DEGREE
    end_x = (end[0] - position.longitude) * longitude_scale
    end_y = (end[1] - position.latitude) * METERS_PER_LATITUDE_DEGREE
    delta_x, delta_y = end_x - start_x, end_y - start_y
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared <= 0:
      continue
    projection = max(0.0, min(1.0, -(start_x * delta_x + start_y * delta_y) / length_squared))
    projected_x = start_x + projection * delta_x
    projected_y = start_y + projection * delta_y
    distance = math.hypot(projected_x, projected_y)
    if distance < closest_distance:
      closest_distance = distance
      closest_heading = math.degrees(math.atan2(delta_x, delta_y)) % 360.0

  return closest_distance, closest_heading


def direction_heading_error(direction: str | None, geometry_heading: float, vehicle_heading: float) -> float:
  positive_error = angular_difference(geometry_heading, vehicle_heading)
  negative_error = angular_difference((geometry_heading + 180.0) % 360.0, vehicle_heading)
  if direction == "Positive":
    return positive_error
  if direction == "Negative":
    return negative_error
  return min(positive_error, negative_error)


class SpeedLimitDatabase:
  def __init__(self, path: Path | str | None = None) -> None:
    self.path = Path(path) if path is not None else default_speed_limit_database_path()
    self.connection: sqlite3.Connection | None = None
    self.unavailable = False

  def _connect(self) -> sqlite3.Connection | None:
    if self.connection is not None:
      return self.connection
    if self.unavailable or not self.path.is_file():
      return None
    try:
      connection = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True)
      schema_version = connection.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
      ).fetchone()
      if schema_version is None or int(schema_version[0]) != 1:
        connection.close()
        self.unavailable = True
        return None
      self.connection = connection
    except (OSError, sqlite3.Error, ValueError):
      self.unavailable = True
      return None
    return self.connection

  def lookup(self, position: Position | None, bearing: float | None) -> float:
    connection = self._connect()
    if connection is None or position is None or bearing is None:
      return 0.0

    latitude_delta = SEARCH_RADIUS_METERS / METERS_PER_LATITUDE_DEGREE
    longitude_denominator = METERS_PER_LONGITUDE_DEGREE * max(0.01, math.cos(math.radians(position.latitude)))
    longitude_delta = SEARCH_RADIUS_METERS / longitude_denominator
    try:
      rows = connection.execute("""
        SELECT roads.speed_kph, roads.direction, roads.geometry
        FROM roads_rtree
        JOIN roads ON roads.id = roads_rtree.id
        WHERE roads_rtree.min_lon <= ? AND roads_rtree.max_lon >= ?
          AND roads_rtree.min_lat <= ? AND roads_rtree.max_lat >= ?
          AND roads.speed_kph > 0
      """, (
        position.longitude + longitude_delta,
        position.longitude - longitude_delta,
        position.latitude + latitude_delta,
        position.latitude - latitude_delta,
      ))

      best_speed = 0.0
      best_score = math.inf
      for speed_kph, direction, geometry in rows:
        distance, geometry_heading = closest_segment(position, decode_geometry(geometry))
        heading_error = direction_heading_error(direction, geometry_heading, bearing % 360.0)
        if distance > MAX_ROAD_DISTANCE_METERS or heading_error > MAX_HEADING_ERROR_DEGREES:
          continue
        # Heading strongly disambiguates parallel, divided, and crossing roads.
        score = distance + heading_error * 0.5
        if score < best_score:
          best_score = score
          best_speed = float(speed_kph) / 3.6
      return best_speed
    except (sqlite3.Error, TypeError, ValueError, zlib.error):
      return 0.0

  def close(self) -> None:
    if self.connection is not None:
      self.connection.close()
      self.connection = None
