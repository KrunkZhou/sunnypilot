import importlib.util
import math
import sqlite3
import struct
import zlib
from types import SimpleNamespace
from pathlib import Path

import pytest

# Load this stdlib-only matcher independently of live_map_data's native runtime imports.
spec = importlib.util.spec_from_file_location("speed_limit_database", Path(__file__).parents[1] / "live_map_data" / "speed_limit_database.py")
speed_limit_database = importlib.util.module_from_spec(spec)
spec.loader.exec_module(speed_limit_database)
SpeedLimitDatabase = speed_limit_database.SpeedLimitDatabase


EASTBOUND_ROAD = [(-79.001, 43.0), (-78.999, 43.0)]


def position(latitude=43.0, longitude=-79.0):
  return SimpleNamespace(latitude=latitude, longitude=longitude)


def encode_geometry(points):
  quantized = [(round(longitude * 1_000_000), round(latitude * 1_000_000)) for longitude, latitude in points]
  values = [len(quantized), quantized[0][0], quantized[0][1]]
  for previous, current in zip(quantized, quantized[1:], strict=False):
    values.extend((current[0] - previous[0], current[1] - previous[1]))
  return zlib.compress(struct.pack(f"<I{len(values) - 1}i", *values))


def create_database(path, roads):
  with sqlite3.connect(path) as connection:
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
  connection.close()


@pytest.fixture
def database_path(tmp_path):
  path = tmp_path / "speed_limits.sqlite"
  # Two crossing roads: eastbound is 80 km/h, north/south is 50 km/h.
  create_database(path, [
    (80, "Positive", EASTBOUND_ROAD),
    (50, "Both", [(-79.0, 42.999), (-79.0, 43.001)]),
  ])
  return path


@pytest.fixture
def database(database_path):
  database = SpeedLimitDatabase(database_path)
  yield database
  database.close()


def test_lookup_uses_heading_to_select_crossing_road(database):
  assert database.lookup(position(), 90.0) == pytest.approx(80 / 3.6)
  assert database.lookup(position(), 0.0) == pytest.approx(50 / 3.6)


def test_lookup_respects_positive_traffic_direction(database):
  assert database.lookup(position(longitude=-79.0005), 90.0) == pytest.approx(80 / 3.6)
  assert database.lookup(position(longitude=-79.0005), 270.0) == 0.0


@pytest.mark.parametrize(("direction", "bearing", "expected"), [
  ("Negative", 270.0, 80 / 3.6),
  ("Negative", 90.0, 0.0),
  ("Both", 90.0, 80 / 3.6),
  ("Both", 270.0, 80 / 3.6),
  (None, 270.0, 80 / 3.6),
])
def test_lookup_traffic_directions(database_path, direction, bearing, expected):
  with sqlite3.connect(database_path) as connection:
    connection.execute("UPDATE roads SET direction = ? WHERE id = 1", (direction,))
  connection.close()
  database = SpeedLimitDatabase(database_path)
  try:
    assert database.lookup(position(longitude=-79.0005), bearing) == pytest.approx(expected)
  finally:
    database.close()


@pytest.mark.parametrize(("distance", "expected"), [(34.0, 80 / 3.6), (36.0, 0.0), (65.0, 0.0)])
def test_lookup_requires_nearby_road(database, distance, expected):
  latitude = 43.0 + distance / speed_limit_database.METERS_PER_LATITUDE_DEGREE
  assert database.lookup(position(latitude=latitude, longitude=-79.0005), 90.0) == pytest.approx(expected)


@pytest.mark.parametrize(("bearing", "expected"), [
  (150.0, 80 / 3.6), (151.0, 0.0), (30.0, 80 / 3.6), (29.0, 0.0), (450.0, 80 / 3.6),
])
def test_lookup_heading_threshold_and_wrapping(database, bearing, expected):
  assert database.lookup(position(longitude=-79.0005), bearing) == pytest.approx(expected)


@pytest.mark.parametrize("invalid_position", [
  None, SimpleNamespace(), position(latitude=math.nan), position(latitude=math.inf), position(latitude=90.01),
  position(latitude=-90.01), position(longitude=math.nan), position(longitude=-math.inf),
  position(longitude=180.01), position(longitude=-180.01), position(latitude="invalid"),
])
def test_lookup_rejects_invalid_position(database, invalid_position):
  assert database.lookup(invalid_position, 90.0) == 0.0


@pytest.mark.parametrize("bearing", [None, math.nan, math.inf, -math.inf, "invalid"])
def test_lookup_rejects_invalid_bearing(database, bearing):
  assert database.lookup(position(), bearing) == 0.0


@pytest.mark.parametrize("speed", [0, -80, math.inf, -math.inf, "nan", "invalid"])
def test_lookup_rejects_invalid_speed(database_path, speed):
  with sqlite3.connect(database_path) as connection:
    connection.execute("UPDATE roads SET speed_kph = ? WHERE id = 1", (speed,))
  connection.close()
  database = SpeedLimitDatabase(database_path)
  try:
    assert database.lookup(position(longitude=-79.0005), 90.0) == 0.0
  finally:
    database.close()


@pytest.mark.parametrize("geometry", [
  b"invalid", zlib.compress(b""), zlib.compress(struct.pack("<I2i", 2, 0, 0)),
  encode_geometry([(181.0, 43.0), (182.0, 43.0)]), encode_geometry([(-79.0, 91.0), (-78.0, 91.0)]),
  encode_geometry([(-79.0, 43.0), (-79.0, 43.0)]),
])
def test_lookup_skips_corrupt_or_degenerate_geometry(database_path, geometry):
  with sqlite3.connect(database_path) as connection:
    connection.execute("UPDATE roads SET geometry = ? WHERE id = 1", (geometry,))
  connection.close()
  database = SpeedLimitDatabase(database_path)
  try:
    assert database.lookup(position(longitude=-79.0005), 90.0) == 0.0
    # A corrupt candidate must not prevent another road from matching.
    assert database.lookup(position(), 0.0) == pytest.approx(50 / 3.6)
  finally:
    database.close()


def test_missing_database_can_be_installed_later(tmp_path):
  path = tmp_path / "missing.sqlite"
  database = SpeedLimitDatabase(path)
  try:
    assert database.lookup(position(), 90.0) == 0.0
    assert not path.exists()
    create_database(path, [(80, "Positive", EASTBOUND_ROAD)])
    assert database.lookup(position(), 90.0) == pytest.approx(80 / 3.6)
  finally:
    database.close()


def test_corrupt_database_returns_unavailable(tmp_path):
  path = tmp_path / "corrupt.sqlite"
  path.write_bytes(b"not a sqlite database")
  database = SpeedLimitDatabase(path)
  assert database.lookup(position(), 90.0) == 0.0
  assert database.unavailable
  assert database.connection is None


@pytest.mark.parametrize("version", ["2", "invalid"])
def test_invalid_schema_closes_connection(database_path, monkeypatch, version):
  with sqlite3.connect(database_path) as connection:
    connection.execute("UPDATE metadata SET value = ? WHERE key = 'schema_version'", (version,))
  connection.close()
  connections = []
  connect = sqlite3.connect

  def track_connection(*args, **kwargs):
    connection = connect(*args, **kwargs)
    connections.append(connection)
    return connection

  monkeypatch.setattr(speed_limit_database.sqlite3, "connect", track_connection)
  database = SpeedLimitDatabase(database_path)
  assert database.lookup(position(), 90.0) == 0.0
  assert database.unavailable
  assert database.connection is None
  assert len(connections) == 1
  with pytest.raises(sqlite3.ProgrammingError, match="closed"):
    connections[0].execute("SELECT 1")


def test_missing_road_schema_returns_unavailable(database_path):
  with sqlite3.connect(database_path) as connection:
    connection.execute("DROP TABLE roads_rtree")
  connection.close()
  database = SpeedLimitDatabase(database_path)
  try:
    assert database.lookup(position(), 90.0) == 0.0
  finally:
    database.close()


def test_database_is_read_only_and_close_releases_connection(database_path):
  special_path = database_path.with_name("speed limits #1?.sqlite")
  database_path.rename(special_path)
  database = SpeedLimitDatabase(special_path)
  assert database.lookup(position(), 90.0) == pytest.approx(80 / 3.6)
  connection = database.connection
  with pytest.raises(sqlite3.OperationalError, match="readonly"):
    connection.execute("DELETE FROM roads")
  database.close()
  database.close()
  assert database.connection is None
  with pytest.raises(sqlite3.ProgrammingError, match="closed"):
    connection.execute("SELECT 1")


def test_default_database_path_prefers_bundle(tmp_path, monkeypatch):
  bundled = tmp_path / "bundled.sqlite"
  bundled.touch()
  monkeypatch.setattr(speed_limit_database, "BUNDLED_SPEED_LIMIT_DATABASE_PATH", bundled)
  assert speed_limit_database.default_speed_limit_database_path() == bundled
