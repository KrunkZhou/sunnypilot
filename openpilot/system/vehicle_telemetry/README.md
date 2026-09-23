# Vehicle telemetry

Primary Athena hosts two optional background threads: a passive CAN collector and
an HTTP batch uploader. They start before its WebSocket connection attempt, keep
running through disconnections, and never participate in driving CAN health.
There is no additional managed process, cereal service/schema, Params key or
native binary change. `getVehicleState()` reads the worker's latest validated
snapshot; uncertain fresh timestamps are never exposed.

The MQB adapter supports identified Volkswagen MQB platforms, including
`AUDI_A3_MK3`, on `CanBus.pt` (including bus offsets). It reads odometer km from
`Kombi_02.KBI_Kilometerstand`, fuel liters from valid high-resolution
`Kombi_03.KBI_Tankinhalt_hochaufl` with `Kombi_02.KBI_Inhalt_Tank` fallback, and
native percentage from `Kombi_03.KBI_Tankfuellstand_Prozent`. Received frames,
lengths, faults and sentinels are checked; genuine zero is valid. No tank capacity
or estimated range is inferred. Hardware validation on the 2020 Audi A3 8V
gateway setup remains required.

## Clocks and storage

Measurements use Linux `CLOCK_BOOTTIME` and the kernel boot ID. Native CAN
timestamps already use that clock. Python GPS messages use `CLOCK_MONOTONIC` and
are explicitly normalized with a clock sample bracket of at most 50 ms. Valid,
fresh GPS measurements need two advancing consistent UTC observations at least
one second apart. OS NTP synchronization requires two observations at least five
seconds apart. A plausible wall date alone is insufficient. Wall-clock steps over
two seconds revoke trust until synchronization is re-established.

Only UTC-corrected data is stored durably in
`/data/vehicle_telemetry/outbox.sqlite3`. Unresolved measurements and segment
checkpoints remain boot-tagged in `/dev/shm/vehicle_telemetry/staging.sqlite3`:
worker restarts recover them, device reboots discard them. A confirmed anchor
maps all measurements from the same boot using elapsed time, including suspend.
First valid readings, 30-second updates, trust recovery and segment transitions
checkpoint data. Trusted unfinished-segment checkpoints are durable separately
from ready uploads; after a power loss their original corrected times are
finalized without rebasing onto the next boot. An abrupt power loss may lose
measurements since the last checkpoint.

`CurrentRoute` plus actual native logger `rlog.lock` paths identify segments.
One immutable record is finalized per segment. Queue data survives route cleanup
and firmware/device restart. Legacy telemetry state is read only to preserve the
ordering high-water mark and compatible cached readings. Subsequent epochs and
sequences are persistent counters independent of UTC.

## Delivery

`POST /v1/devices/:dongle_id/vehicle-telemetry/batch` uses the primary device JWT.
Batches contain at most 100 records and 1 MiB, with stored payload JSON embedded
verbatim so its SHA-256 digest is stable across retries. Matching accepted or
duplicate acknowledgements retire payloads; small ID/digest receipts prevent
reconstruction after interrupted staging cleanup. Rejected records are
quarantined. Missing or malformed acknowledgements retain the data.

Claims and retries use boot-relative deadlines and are recovered across reboot.
Transient retries back off with jitter from 5 seconds to 1 hour; authentication
and unsupported-server errors recheck hourly. Clock trust recovery and Athena
reconnection clear pending retry delays. Successful batches drain immediately.
Redirects are disabled, response bodies are bounded, and `Retry-After` is honored.
There is no automatic age/quota eviction; storage errors are reported and retain
existing records. Deploy compatible RTZS batch support before this firmware.

Run host tests with `PYTHONPATH=.:opendbc_repo python -m unittest discover -s
openpilot/system/vehicle_telemetry/tests`. Native IPC validation additionally
requires the ARM/Linux runtime matching the supplied binaries.
