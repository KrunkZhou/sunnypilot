# Sentry Mode (Comma 4 / MICI)

Sentry Mode watches for motion while a MICI device is parked, captures one wide-road and one cabin still when possible, and durably retries delivery to RTZ. Capture and upload remain disabled until the on-device consent flow is accepted.

## Persistent configuration

Sentry settings are individual files and never persistent Params:

```text
/data/sentry/
├── config.lock
├── config/
│   ├── schema_version
│   ├── enabled
│   ├── capture_upload_consent_version
│   ├── motion_threshold_mps2
│   ├── warning_persistence_seconds
│   └── wait_for_driver_exit
├── outbox.sqlite3
└── media/<event-id>/<revision>/{wide,cabin}.jpg
```

Directories must be mode `0700`; the lock, configuration, database, sidecars, and images must be mode `0600`. Readers and writers use the fixed `config.lock` inode. Writes use a same-directory temporary file, file `fsync`, atomic replacement, and directory `fsync`. Missing, partial, corrupt, symlinked, incorrectly permissioned, or future-version configuration fails closed and is not overwritten. The UI reset action quarantines only `config/`; it preserves the outbox and media.

Schema 2 uses thresholds `0.02` (High), `0.04` (Standard), or `0.08` (Low) m/s² and warning persistence `0.5`, `1`, `2`, or `5` seconds. High is restored to `0.02`, replacing the temporary `0.01` setting. `wait_for_driver_exit` is `0` or `1` and defaults to `1` (on). Other defaults are unchanged: disabled, consent 0, `0.04` m/s², and one second. Effective enablement requires `enabled=1` and the current consent version. First enablement commits consent before enablement. A one-time legacy import reads only sensitivity/timing, forces disabled/no-consent, durably commits the folder, and then removes all safe regular legacy Sentry keys.

A complete valid schema-1 folder is upgraded under the exclusive configuration lock: the new default-on file is durably written before the schema marker changes to `2`. A valid staged file from an interrupted migration is preserved and re-synced; corrupt staged data is not overwritten. Schema-2 folders missing the new file fail closed. Migration preserves enablement, consent, settings, the outbox, and media. Older binaries that only support schema 1 cannot read the upgraded configuration.

An existing valid High file containing the temporary `0.01` is atomically migrated back to `0.02` on initialization/load under the exclusive config lock. The full configuration is validated first; corruption is never rewritten. This changes only the threshold file, preserving enablement, consent, warning timing, and queued captures. Existing `0.02` files need no rewrite. Legacy StarPilot High imports also use `0.02`. Current readers otherwise retain shared locks. The immediate first-capture behavior is unchanged.

`SENTRY_ROOT` overrides `/data/sentry` for host tests.

## Volatile coordination

The only runtime coordination keys are `SentryRuntimeEnabled`, `SentryCaptureLease`, `SentryRuntimeStatus`, and `SentryRuntimeCommand` in `Params("/dev/shm/sentry_params")`. They are cleared before daemon construction, at daemon start, and on exit. A narrow Params subclass supplies these four source-declared types when a prebuilt `libparams` predates the key names; the underlying atomic Params storage remains unchanged. No runtime value is written under `/data/params`.

Capture leases contain a UUID request ID and monotonic expiry no more than 20 seconds ahead. The camera-operation `flock` serializes Sentry capture with offroad livestream startup and the existing snapshot helper. Manager runs `sentryd` whenever MICI is offroad, `sensord` onroad or while Sentry requests it, and `camerad` for its existing demands or a valid capture lease.

Sensor-demand writes are verified by reading back the manager-visible value. The daemon reconciles demand once a second and retries failed writes, so loss of the volatile key does not require toggling the persistent setting.

Early offroad startup may precede GPIO device creation or udev permissions. `sensord` retries missing/permission-denied interrupt GPIO acquisition with a shutdown-interruptible backoff from 0.1 to 5 seconds. It does not change permissions or launch another process. GPIO descriptors are closed on acquisition failure and worker exit. This handles startup readiness only; Sentry still requires fresh, valid accelerometer messages before detecting motion.

## Detection and capture

By default, detection waits for the driver door to be observed open and then closed after ignition-off, then starts the 90-second arming timer. The MICI **wait for driver exit** toggle stores this choice in `/data/sentry/config/wait_for_driver_exit`. With it off, the existing timer starts once enabled and continuously offroad. Ignition-on clears the exit sequence and cancels capture. Changing the toggle cancels the current episode and begins the newly selected gate. Sensitivity changes and the 21-revision capture limit still restart the 90-second timer without requiring another exit once the door sequence was completed.

Driver-door detection is a passive, non-conflated CAN subscriber for classic Volkswagen MQB vehicles using `vw_mqb`, including Audi A3 Mk3. It selects the existing vehicle's powertrain CAN bus from read-only `CarParamsPersistent`, then validates `Gateway_72` (`0x3DB`, exactly eight bytes), specifically `ZV_FT_offen` (driver door, not passenger doors or hatch). Only fresh, valid, ordered CAN events received after the offroad/enable boundary count. `card` does not run offroad, so cached `carState.doorOpen` is not used. No CAN commands are sent and no Panda safety, vehicle-control, or process-lifecycle behavior is changed.

Status reports **waiting for door open**, **waiting for door close**, or **door signal unavailable**. Missing, invalid, stale, unsupported, or USB-only door data never silently means closed and never bypasses the gate; turn the toggle off for USB testing or unsupported vehicles. The sequence is only a proxy for driver exit, not cabin-occupancy detection. It is kept in memory: daemon/device restart or re-enabling Sentry requires another observed post-start open/close cycle. An exit before the daemon subscribes can be missed. Once the exit is accepted, a sleeping parked CAN bus does not block later motion episodes or their arming periods. Pending uploads and explicit manual tests remain available while waiting. Real-car testing must confirm that the A3 gateway continues publishing the door signal through key-off and exit.

Once armed, Sentry compares the magnitude of the three-axis acceleration vector difference with the configured physical threshold at 10 Hz. The first fresh sample that differs from the preceding sample by at least that threshold immediately opens an episode and queues revision 1; it does not wait for warning persistence. A baseline sample, stale/invalid sample, or subthreshold change never triggers capture. Camera warm-up/encoding still takes time, so an immediate trigger is not an instantaneous JPEG.

Evidence accumulates above threshold and decays while quiet. Device status starts as `motion`, advances to `warning` after the configured warning persistence (shown as "warning status delay" in settings), and advances to `alarm` after 30 seconds of evidence. The warning status transition does not create another event, capture, or webhook. The episode closes after 60 quiet seconds, including during an accelerometer gap. The first wire revision keeps kind `warning` for compatibility with RTZ; that server label does not gate capture or upload. Configuration file names/values, consent, and the arming delay are unchanged.

While qualifying motion continues, another wide-road + cabin pair becomes eligible one second after the previous pair is durably finalized. These are sequential `follow_up` revisions of the same UUID, not new warning events. Recapture needs a fresh sample and qualifying motion within the last second, newer than the previous request; merely displaying `warning` during the 60-second quiet period does not keep taking photos. Motion resuming before episode closure uses the same event. After 30 seconds of accumulated evidence, the next eligible revision is the episode's single `alarm`; follow-ups can continue after it within the same cap. Only revision 1 creates an RTZ webhook. The manual test remains one warning/capture/webhook.

Each episode permits the first capture plus at most 20 subsequent revisions (21 total, including any alarm or failed-camera revisions). Once the last revision is durably finalized, the episode closes, the warm camera session stops, and detection waits through a fresh 90-second arming period even if movement continues. Uploads keep draining during that wait. Storage retries must finish before this arming period starts; they neither consume another revision nor allow another episode to bypass the wait. After arming, fresh samples can open a new UUID starting at revision 1.

Only one capture may be queued/in flight at a time for a motion episode. Durable-write failures retry the same captured bytes and pause new captures. Warm camera clients are reused between pairs within a single, non-renewable 20-second lease; the shared lock and lease are released on expiry, cancellation, or quiet. A fresh session reconnects and warms each camera for four seconds. Therefore one second is a minimum cooldown, not a guaranteed one-photo-per-second frame rate: startup, session renewal, a missing camera, encoding, and storage can lengthen it.

The 100 ms loop period includes processing time. Accelerometer health requires a received, valid message with both receive age and publication age below one second, matching the detector's stale-sample limit. Consumer frequency and the messaging library's 104 Hz publisher-derived `alive` flag are diagnostic only: a slow iteration or a single missed 10 Hz poll must not invalidate otherwise fresh samples. Missing, invalid, future-dated, or truly stale samples still stop motion detection and clear partial evidence. Runtime status includes the accelerometer flags and ages, and reports distinct errors for missing, invalid, and stale data.

Both `deviceState` and physical `pandaStates` ignition are checked, so Sunnypilot Always Offroad cannot mask ignition from Sentry. Ignition, disablement, consent loss, configuration corruption, or daemon shutdown aborts queued/in-flight camera work. Each camera connects and warms independently. Never-available cameras report `camera_unavailable`; connected cameras or encoding that miss the deadline report `capture_timeout`.

VisionIPC's padded NV12 planes are repacked at full frame resolution and encoded as MJPEG by the native ffmpeg binary from the pinned `comma-deps-ffmpeg` package. Pillow is not used. Encoding polls ignition/cancellation, kills its child on abort, and shares the capture's absolute 20-second deadline.

### Capture diagnostics

Stage diagnostics use the existing rotating `/data/log/swaglog.*` logs. Filter for `sentry_capture_` and `sentry_jpeg_` events. Their log context includes `sentry_event_id` and `sentry_revision`, updated for every follow-up within a warm session; camera work also carries `sentry_request_id`, `sentry_capture_index`, and JPEG work's `sentry_camera_role`.

`sentry_capture_stage` marks discovery, connection, warmup, frame waiting, and JPEG encoding. `sentry_capture_result` records each camera's last reached stage, stage durations, receive-attempt count, whether a frame was received, outcome, and bounded exception details. Stages are logged on transitions, not on every poll. `sentry_jpeg_stage` separates geometry/binary validation, NV12 packing, subprocess startup, encoding, and completion; `sentry_jpeg_failed` identifies the failing stage. A start record without its completion helps identify a blocked operation, although diagnostics do not make native IPC calls interruptible.

Both cameras still share one deadline. If wide encoding consumes the budget, the summary's `encoder_timeout_role=wide`, cabin receive-attempt count of zero, and absent cabin JPEG-stage records distinguish an unattempted cabin from a frame-reception failure. The last reached stage is not proof that the camera spent the whole duration actively working on that stage: it may have been waiting for the other camera's turn. Diagnostics include no image bytes and bound error text to 256 characters. Logging failures are ignored so capture outcomes and lease cleanup are preserved. RTZ metadata, omission reason codes, uploads, webhooks, and capture timing are unchanged; these records are diagnostic logs, not additional persistent Sentry settings or event history.

## Durable outbox and RTZ request

SQLite alone owns descriptors and locking for its live database and `-wal`, `-shm`, and `-journal` files. The database is created privately (`0600`) before its first SQLite connection, and SQLite's Unix VFS inherits that mode for new sidecars. Sentry validates sidecars with `lstat` only: they must remain regular, non-symlink files with mode `0600` and the recorded inode. Unsafe permissions fail closed instead of being repaired while the database is open. Opening and closing an extra sidecar descriptor would cancel SQLite's process-wide POSIX locks and can cause a `SIGBUS` when a concurrent UI reader resets its shared-memory index. This validation does not reset, truncate, replace, or delete the outbox or queued images.

SQLite uses WAL and verified `synchronous=FULL`. Revision metadata is committed before camera/network work; JPEGs and SHA-256 manifests are durably committed before a revision becomes ready. Interrupted captures recover as `stale_capture`. Upload selection atomically changes a revision to `uploading` with a two-minute durable lease before opening any image, preventing quota/upload payload races. Expired claims are recovered on startup and whenever the uploader next polls.

The image-byte quota is 1 GiB. A bundle enters a durable, non-uploadable eviction state before any unlink; interrupted or partially synced cleanup is resumed on startup and before uploader polling. Oldest never-attempted or known-rejected bundles can be rewritten with `queue_quota`. A payload that might have reached RTZ remains byte-for-byte immutable: its local media manifests are retained, the missing bytes are recorded locally as `queue_quota`, and the revision becomes non-retriable to avoid an event/revision conflict. Actively leased uploads are not evicted. Startup also validates the complete private media tree and removes safe files that no durable manifest references.

The uploader sends device-authenticated multipart `POST /v1/devices/<dongle_id>/sentry-events`. Its normal `Authorization` JWT uses the ordinary `BaseApi` key selection so it remains compatible with devices registered using RSA. Each request also carries a purpose-scoped ES256 JWT and the bounded, base64-encoded P-256 public-key PEM in `X-RTZ-Sentry-ES256-Token` and `X-RTZ-Sentry-ES256-Public-Key`, allowing RTZ to pin the Sentry signing key without changing existing device authentication.

- `event`: strict schema-2 JSON for new events, at most 32 KiB.
- Optional `wide` and `cabin`: JPEG, at most 8 MiB each.
- Exactly one manifest or omission for each camera role.
- Warning is revision 1; revisions 2 through 2147483647 are `follow_up` or the episode's single `alarm` under the same UUIDv4. This wire compatibility range is unchanged; new motion episodes now stop at revision 21.

The event-wire schema is independent of the folder configuration schema, which is now 2. The door option does not change the wire contract. An atomic SQLite migration preserves existing schema-1 events and immutable queued request bytes (`warning` 1 / `alarm` 2). Every preceding revision must be acknowledged before a later one can upload. Deploy the updated RTZ ingestion first, before installing this device code; older RTZ servers reject schema 2. Existing webhook deliveries/retries are preserved, while newly accepted revisions after the first do not create deliveries.

Success is accepted only when RTZ returns the exact event ID, revision, and media hashes, or an explicit deleted tombstone acknowledgement. Local image bytes are deleted and directory-synced only after that acknowledgement. Network failures, timeouts, HTTP 408/425/429, and 5xx retry with jittered exponential backoff from five seconds to one hour and bounded `Retry-After`. Other 4xx responses remain terminal and inspectable until manual retry.

## UI and validation

MICI settings expose consent/enablement, sensitivity, warning persistence, live daemon/error status, queue usage, manual end-to-end alert, retry, and configuration reset. UI writes reload authoritative files and revert on failure. Runtime status has a five-second heartbeat and is treated as unavailable after 15 seconds.

Host tests use a temporary `SENTRY_ROOT`; they cover file validation/durability, consent ordering, legacy migration, detector episodes, SQLite recovery/quota/claims, exact wire metadata and acknowledgements, retries, ffmpeg encoding/abort, independent cameras, physical ignition, and daemon failure containment. Final acceptance still requires real Comma 4 wide+cabin capture and focus/exposure, ignition interruption, Wi-Fi/LTE outage and reboot, RTZ restart/ack cleanup, and server history/webhook recovery.
