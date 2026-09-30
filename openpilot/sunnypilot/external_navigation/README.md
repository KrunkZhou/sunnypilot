# External navigation relay

The Waveshare relays complete CarPlay BLE navigation snapshots over authenticated UDP to the Comma hotspot. The receiver publishes `externalNavigationSP` at 5 Hz, using Event's existing reserved ordinal 136. This is optional input: neither model pipeline includes its subscriber in normal model health checks.

The settings choices are **Off** and **Assisted turns**. Selecting Assisted turns enables guidance reception and on-road hotspot ownership. Selecting Assisted turns allows eligible low-speed model turn inputs without a vehicle/model approval file. There is no live Shadow mode. Highway right-branch intent is advisory only; no navigation lane-change input is generated. The separate PC-only replay launcher can compare `keepRight` offline.

## Storage and installation

All persistent files live under `/data/rtzs/navigation/` on AGNOS. Development hosts use `~/.comma${OPENPILOT_PREFIX}/rtzs/navigation`; `RTZS_NAVIGATION_ROOT` overrides that root for tests. This feature does not modify other RTZS or Sentry files.

- `config/schema_version`: plain `1` plus newline; `config/mode`: plain `0` (Off) or `1` (Assisted turns) plus newline. A fixed `config.lock` protects readers and atomic field replacement. Missing, malformed, unsupported or insecure settings fail Off.
- `private/relay.json`: `relay_id` (32 hex characters) and `key` (64 hex characters). Use the same independently generated key on the Waveshare. The public fixture key is explicitly rejected. The Wi-Fi password is not a relay authentication key.
- `logs/navigation-*.jsonl`: private, bounded receive diagnostics, at most 20 files of approximately 2 MiB each. Keys and raw authenticated packets are excluded. A four-record replaceable queue keeps file IO outside the receiver.

Navigation directories must be owned by the running user with mode 0700; files are 0600. Manual Wi-Fi ownership revisions are transient in `/dev/shm/rtzs-navigation-revision` (host `runtime/network-revision`). Failed revision tracking disables automatic Wi-Fi ownership.

This checkout contains a prebuilt native Params registry. New feature settings deliberately use RTZS files instead of unregistered Params keys; native cleanup cannot delete them. Python Cereal reads the source schema dynamically. Prebuilt `loggerd` has a static service table and does not automatically subscribe to this new service, so the separate JSONL ring is required until loggerd is rebuilt. Existing `modelDataV2SP` logging preserves the added hint fields on the wire.

## Protocol and behavior

Relay v1 uses UDP port 28443 with a maximum 1,200-byte packet, HMAC-SHA256, one enrolled relay, a 16-byte boot identity and receiver-generated one-time challenges. A response must arrive within 250 ms; challenges run at most 5 Hz. Only the active hotspot's actual `wlan0` address is bound. Internet routing is not needed. Guidance expires after one second without a valid response; BLE resets/disconnects clear it sooner when received.

The complete BLE navigation frame stays v1 and no larger than 1,024 bytes. Optional TLVs carry current maneuver ID (16), distance observation counter (17), distance age (18), maneuver observation counter (19) and maneuver age (20). Unknown/absent fields remain guidance-only. Distance freshness adds BLE assembly/queue dwell, a conservative complete challenge round-trip, and local receive age. Repeated observations cannot reduce their effective age; heartbeat repeats cannot create a new observation. Numeric distance is metres and never substituted with total remaining distance.

Only active route types 1/2/20/21 (ordinary left/right or left/right at end) may propose a low-speed turn. Lateral engagement, fresh distance at most 1,000 ms old, speed below the configured existing turn threshold and below 20 mph, and two decreasing observations are required. Arm at more than 15 and no more than 40 metres; crossing 15 metres may propose one pulse. Attaching inside the boundary never triggers late. Steering/brake/indicators, blind spots and existing maneuver desires take precedence. Reset, stale data and rerouting discard pending approach state. Already-consumed recurrent model hints cannot be recalled by resetting the driving model, so no model reset is attempted.

`turnLeft`/`turnRight` are the only possible active navigation desires; existing longitudinal, actuator and driver-monitoring code is unchanged. Model predictions can still indirectly affect existing longitudinal behavior. Highway types 14/23/53 produce right-branch HUD intent only. Neither lane recommendations nor camera road edges establish that an exit is reachable from the current lane.

A captured authenticated old HELLO can interrupt relay availability by changing the pending boot binding; it cannot restore guidance without answering a fresh challenge. Old publisher sessions, duplicate replies and regressing source sequences are rejected. Bounded session/maneuver history fails closed when exhausted.

## Validation and development replay

Run `python -m unittest discover -s openpilot/sunnypilot/external_navigation/tests -v`. The portable tests cover the shared Java/C++ packet corpus, real localhost UDP, malformed/authentication/reset cases, age progression, manual priority, both pipeline call sites, approval-independent turn eligibility, private files, hotspot ownership and UI geometry. Install pycapnp 2.1.0 to include the source-schema round-trip test.

Policy-only replay accepts JSONL with `sample` fields matching `policy.Sample`, and `vehicle` containing `lateral_active`, `speed`, `speed_limit`, and optional `manual`. It never emits active desires:

```sh
python -m openpilot.sunnypilot.external_navigation.replay policy /path/to/trace.jsonl
```

On a development PC with the normal compiled model/replay dependencies and local camera recordings:

```sh
python -m openpilot.sunnypilot.external_navigation.replay camera \
  --log /path/to/rlog.zst --narrow /path/to/fcamera.hevc --wide /path/to/ecamera.hevc \
  --pulse-frame 123 --pipeline stock --hint keepRight --output /tmp/nav-replay
```

The tool runs neutral and hinted modeld in separate existing replay namespaces. It writes paired frame outputs and differences in lateral position, predicted velocity and desired acceleration. `--pipeline tinygrad`, `--hint turnLeft`, and `--hint turnRight` are also supported. `replay compare neutral.jsonl hinted.jsonl` compares existing exports. Exact frame IDs and trajectory shapes must match. The research launcher refuses vehicle hardware and non-replay invocation; it is not registered with manager.

Host tests and schema checks do not validate physical Wi-Fi/BLE coexistence, live end-to-end latency, recorded-camera model behavior, closed-course turns or a one-hour soak. Assisted turns uses the runtime checks above; replay and host checks do not establish actual driving behavior.
