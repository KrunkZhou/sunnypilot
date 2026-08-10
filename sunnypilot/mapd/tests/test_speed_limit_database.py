import sqlite3
import struct
import zlib

import pytest

from openpilot.sunnypilot.mapd.live_map_data.osm_map_data import OsmMapData
from openpilot.sunnypilot.mapd.live_map_data.speed_limit_database import SpeedLimitDatabase
from openpilot.sunnypilot.navd.helpers import Coordinate


def encode_geometry(points: list[tuple[float, float]]) -> bytes:
  quantized = [(round(longitude * 1_000_000), round(latitude * 1_000_000)) for longitude, latitude in points]
  values = [len(quantized), quantized[0][0], quantized[0][1]]
  for previous, current in zip(quantized, quantized[1:]):
    values.extend((current[0] - previous[0], current[1] - previous[1]))
  return zlib.compress(struct.pack(f"<I{len(values) - 1}i", *values))


def create_database(path, roads):
  connection = sqlite3.connect(path)
  connection.executescript("""
    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
    INSERT INTO metadata VALUES ('schema_version', '1');
    CREATE TABLE roads (
      id INTEGER PRIMARY KEY,
      source_id INTEGER,
      part INTEGER NOT NULL DEFAULT 0,
      speed_kph INTEGER NOT NULL,
      direction TEXT,
      road_class TEXT,
      name TEXT,
      length_m REAL,
      geometry BLOB NOT NULL
    );
    CREATE VIRTUAL TABLE roads_rtree USING rtree(id, min_lon, max_lon, min_lat, max_lat);
  """)
  for speed, direction, points in roads:
    cursor = connection.execute(
      "INSERT INTO roads(speed_kph, direction, geometry) VALUES (?, ?, ?)",
      (speed, direction, encode_geometry(points)),
    )
    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    connection.execute(
      "INSERT INTO roads_rtree VALUES (?, ?, ?, ?, ?)",
      (cursor.lastrowid, min(longitudes), max(longitudes), min(latitudes), max(latitudes)),
    )
  connection.commit()
  connection.close()


@pytest.fixture
def database_path(tmp_path):
  path = tmp_path / "speed_limits.sqlite"
  # Two crossing roads at the origin: eastbound is 80 km/h, north/south is 50 km/h.
  create_database(path, [
    (80, "Positive", [(-79.001, 43.0), (-78.999, 43.0)]),
    (50, "Both", [(-79.0, 42.999), (-79.0, 43.001)]),
  ])
  return path


def test_lookup_uses_heading_to_select_crossing_road(database_path):
  database = SpeedLimitDatabase(database_path)
  position = Coordinate(43.0, -79.0)

  assert database.lookup(position, 90.0) == pytest.approx(80 / 3.6)
  assert database.lookup(position, 0.0) == pytest.approx(50 / 3.6)


def test_lookup_respects_positive_traffic_direction(database_path):
  database = SpeedLimitDatabase(database_path)
  position = Coordinate(43.0, -79.0005)

  assert database.lookup(position, 90.0) == pytest.approx(80 / 3.6)
  assert database.lookup(position, 270.0) == 0.0


def test_lookup_returns_unavailable_outside_matching_distance(database_path):
  database = SpeedLimitDatabase(database_path)

  assert database.lookup(Coordinate(43.01, -79.01), 90.0) == 0.0


class FakeParams:
  def __init__(self, osm_speed_limit):
    self.osm_speed_limit = osm_speed_limit
    self.values = {}

  def get(self, key):
    assert key == "MapSpeedLimit"
    return self.osm_speed_limit

  def put(self, key, value):
    self.values[key] = value


class FakeSpeedLimitDatabase:
  def __init__(self, speed_limit):
    self.speed_limit = speed_limit

  def lookup(self, position, bearing):
    return self.speed_limit


@pytest.mark.parametrize(("database_limit", "osm_limit", "expected"), [
  (80 / 3.6, 50 / 3.6, 80 / 3.6),
  (0.0, 50 / 3.6, 50 / 3.6),
  (0.0, 0.0, 0.0),
])
def test_ontario_database_has_priority_then_osm_fallback(database_limit, osm_limit, expected):
  map_data = OsmMapData.__new__(OsmMapData)
  map_data.last_position = Coordinate(43.0, -79.0)
  map_data.last_bearing = 90.0
  map_data.speed_limit_database = FakeSpeedLimitDatabase(database_limit)
  map_data.mem_params = FakeParams(osm_limit)

  assert map_data.get_current_speed_limit() == pytest.approx(expected)
  expected_source = "ON" if database_limit else "OSM" if osm_limit else ""
  assert map_data.mem_params.values["MapSpeedLimitSource"] == expected_source
