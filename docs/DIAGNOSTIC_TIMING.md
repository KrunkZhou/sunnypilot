# Diagnostic timing provenance

Python and C++ swaglog producers retain `created` and add top-level
`wall_time_ns` and `monotonic_ns` decimal strings. `boot_id` is the canonical
kernel UUID from `/proc/sys/kernel/random/boot_id`; it is omitted when that
identity is unavailable or invalid. There is no process or random fallback.
Timing is captured when the record is created, before formatting or upload.

Both stats writers capture one timing sample per flush. The Influx line timestamp
uses exact `time.time_ns()`. Reserved **fields** `rtzs_monotonic_ns` (integer,
with the Influx `i` suffix) and `rtzs_boot_id` (string, when available) describe
the flush. They are not tags and do not change series identity. Raw metric
payloads cannot override these reserved fields. Aggregate stats retain their
existing flush-time semantics; this is not individual metric sample timing.

All these monotonic values use **CLOCK_MONOTONIC**, including C++ swaglog.
Generic C++ cereal events instead use CLOCK_BOOTTIME and must not be mixed into
this contract after suspend. The current Python qcomgpsd and ubloxd publishers
use CLOCK_MONOTONIC in their outer `logMonoTime`.

`timed` emits the structured swaglog event `diagnostic.clock_anchor` for a fresh,
valid GPS message with a fix, plausible UTC, and known kernel boot identity.
The event fields are `version: 1`, `source: "gps"`, `clock: "monotonic"`,
`boot_id`, `monotonic_ns`, and `wall_time_ns`. Nanosecond values are decimal
strings. The anchor pairs the GPS message's outer monotonic sample timestamp
with its exact `unixTimestampMillis * 1000000`, rather than the later receipt
or logging timestamp. Samples over two seconds old or from the future are
rejected. The existing ten-second cadence bounds anchor emission, including
when the system clock already agrees and no clock adjustment is needed.
This change does not alter clock setting or hardware behavior.

The existing human-readable sensord `time jumped: NEW OLD` warning remains.
When boot identity is available, a separate `diagnostic.clock_jump` event adds
`version: 1`, `clock: "monotonic"`, `boot_id`, `monotonic_ns`,
`old_offset_ns`, and `new_offset_ns`; nanosecond fields are decimal strings.
The offsets are wall time minus monotonic time. The old value is the saved
baseline, not necessarily the previous interrupt timestamp. This event is
evidence of an offset change, not a trusted UTC anchor.

A consumer may project time only within the same boot using an explicit,
validated GPS anchor. Missing provenance, arbitrary wall-clock jumps, upload
receipt ordering, and broad `clocks.valid` date plausibility do not establish a
trusted UTC mapping. CLOCK_MONOTONIC excludes suspend, so conflicting later GPS
anchors must invalidate or separate the mapping even within the same boot.
Existing diagnostics are not retroactively enriched.
