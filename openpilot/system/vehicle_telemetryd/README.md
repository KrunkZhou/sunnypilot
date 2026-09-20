# Vehicle fuel and odometer telemetry

`vehicle_telemetryd` runs independently of controls and receives CAN passively.
Its private MQB parser observes `Kombi_02` (`0x6B7`) and `Kombi_03` (`0x6B8`) on
`CanBus.pt`, including the configured panda bus offset. It never sends CAN,
changes the driving parser, or uses the camera bus as a fallback.

The MQB adapter includes `AUDI_A3_MK3`. Numeric ranges reject initialization and
error values; the coarse fuel fallback also requires `KBI_FStatus_Tank == 0`.
High resolution liters take precedence while fresh. Fuel percentage comes from
the cluster without assuming tank capacity. A metric becomes supported only
after a valid received frame. The existing DBC and packed-frame tests establish
decoding behavior; **2020 Audi A3 8V gateway hardware support still requires a
real capture and dashboard comparison**. Verify both addresses on the mapped bus,
fuel readings against the cluster, and odometer units before accepting that car.

The daemon selects only current-trip `CarParams`, after manager has cleared the
prior trip and published `IsOffroad=False`. A valid VIN and fingerprint form a
hashed identity; no VIN uses a trip UUID, preventing carryover into another trip.
Unknown platforms publish explicit unsupported state. Waiting for current
CarParams preserves last-known data without inventing an unsupported vehicle.

Snapshots follow the RTZS version 1 wire contract. Immutable `(collector_epoch,
snapshot_seq)` ordering covers automatic uploads and `getVehicleState()` reads.
UTC measurement timestamps remain unchanged during retries; monotonic time
controls freshness, periodic persistence and retry timing. The runtime envelope
is written atomically at 1 Hz in `/dev/shm/vehicle_telemetry/state.json` (macOS:
`/tmp/vehicle_telemetry/state.json`). Read freshness requires a heartbeat no older
than five seconds and an accepted frame no older than ten seconds. Durable reads
are always last-known.

One JSON Params value, `VehicleTelemetryState`, holds the epoch, latest durable
snapshot and latest pending upload. It lives in the **isolated persistent Params
root** `/data/vehicle_telemetry_params` on comma devices, with native atomic write
and fsync semantics. AGNOS mounts `/persist` read-only; `/data` is writable by the
comma user and survives firmware updates. PCs retain their existing writable
`Paths.persist_root()/vehicle_telemetry_params` location.

If the new on-device store is absent, a readable old
`/persist/vehicle_telemetry_params/<params-prefix>/VehicleTelemetryState` is
validated and imported with its readings, pending upload, and incremented epoch
in one atomic write before the collector exposes new snapshots. Legacy files
are never modified. A corrupt or unreadable legacy record fails closed; an
existing new record always takes precedence. Athena can return legacy data as
last-known before the collector imports it.

This checkout ships prebuilt native libraries without a
native build configuration. A narrow Params subclass recognizes only this JSON
key, and strict reads reject corrupt data without logging its contents or
silently resetting ordering. The collector does not depend on the global Params
registry recognizing this key, and the key does not enable or disable the daemon.
Isolation is essential: older native maps would delete an unknown key in default
Params on transitions, and log it without DONT_LOG.
Ordinary device, road-state and CarParams reads still use default Params.

The manager always starts this optional daemon, but its failure is excluded from
driving's blocking `processNotRunning` check. Its stopped state remains visible
in manager diagnostics; essential driving processes retain their existing checks.

Persistence and queuing occur on vehicle selection, first valid reading, every
30 seconds, and transition offroad. A restart first persists a new epoch before
exposing any snapshot. Corrupt existing ordering fails closed with a diagnostic;
it is never automatically reset. On a parked restart, a previously onroad cache
is republished offroad without changing measurement timestamps.

A separate network worker uploads only the newest pending snapshot to the
primary `API_HOST` endpoint `POST /v1/devices/{dongle_id}/vehicle-state` using the
normal device JWT. Transient/authentication failures retry from five seconds up
to five minutes, including rejected snapshots that may follow a UTC correction;
unsupported endpoints are reprobed hourly. Successful acknowledgement only
removes that exact pending snapshot, so an in-flight older upload cannot discard
newer data.

Run focused checks from the repository root in a Python 3.12 environment with
pycapnp, numpy and ruff:

```sh
PYTHONPATH=.:opendbc_repo:msgq_repo python -m unittest discover -s openpilot/system/vehicle_telemetryd/tests -v
python -m unittest discover -s openpilot/selfdrive/selfdrived/tests -p test_optional_process_health.py -v
ruff check openpilot/system/vehicle_telemetryd openpilot/system/athena/athenad.py openpilot/system/manager/process_config.py
```
