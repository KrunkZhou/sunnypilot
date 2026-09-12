# Ontario speed-limit fallback

`ontario_speed_limits.sqlite` is copied unchanged from
`release-mici-test` commit `798a79a16cb60b60b8f36b3caf1920e187c92cc5`,
at `sunnypilot/mapd/data/ontario_speed_limits.sqlite`.

- Size: 87,330,816 bytes.
- Git blob: `1d25c050734c3e0be8affcbb0fa12a37b9bba759`.
- SHA-256: `28086c6cf1f9722b4cda1a20e43054bec8503f2eef7954fa71281a69f41ad7e3`.

The database metadata names `ontario_road_network.geojson` as the source file.
Its `created_unix` value, `1786324340`, is 2026-08-10 01:12:20 UTC; this is the
database creation timestamp, not a verified source snapshot date. The original
source URL and snapshot date are unknown. No converter or source GeoJSON was
found in that commit, so the database cannot be regenerated from that tree alone.

The metadata records schema version `1`, CRS `EPSG:4326`, speeds in `km/h`,
coordinate scale `1000000`, and geometry encoding
`zlib:int32-le:count,lon,lat,delta_lon,delta_lat`. It also records
`runtime_slim=1`, `features_seen=630359`, `features_rejected=1`, and
`roads_written=627198`.

The schema contains `metadata(key, value)`,
`roads(id, speed_kph, direction, geometry)`, and the spatial index
`roads_rtree(id, min_lon, max_lon, min_lat, max_lat)`. Verification of the copied
asset returned `PRAGMA integrity_check = ok`; `PRAGMA user_version` is `0`
(the schema version is stored in `metadata`).

| Table | Rows |
| --- | ---: |
| `metadata` | 11 |
| `roads` | 627,198 |
| `roads_rtree` | 627,198 |
| `roads_rtree_node` | 19,165 |
| `roads_rtree_parent` | 19,164 |
| `roads_rtree_rowid` | 627,198 |

Road speeds range from 5 to 110 km/h. Direction values are `Both` (578,515),
`Negative` (23,810), and `Positive` (24,873).

Runtime lookup opens the database read-only, queries nearby R-tree bounds,
then matches road geometry using distance and direction-aware vehicle bearing.
Matched speeds are converted to metres per second. A valid current map
`MapSpeedLimit` has priority. Ontario is consulted only when the current map
limit is unavailable and GPS/location data are valid. Existing map lookahead
and car-source policy are unchanged.
