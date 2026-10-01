# External navigation relay

The Waveshare relays complete CarPlay BLE navigation snapshots over authenticated UDP to the Comma hotspot. The receiver publishes `externalNavigationSP` at 5 Hz, using Event's existing reserved ordinal 136. This is optional input: neither model pipeline includes its subscriber in normal model health checks.

The settings choices are **Off** and **Assisted turns**. Selecting Assisted turns enables guidance reception and on-road hotspot ownership. Selecting Assisted turns allows eligible low-speed model turn inputs without a vehicle/model approval file. The separate **Navigation turn speed control** toggle defaults off and requires Assisted turns; its behavior is described below. There is no live Shadow mode. Highway right-branch intent is advisory only; no navigation lane-change input is generated. The separate PC-only replay launcher can compare `keepRight` offline.

## Storage and installation

All persistent files live under `/data/rtzs/navigation/` on AGNOS. Development hosts use `~/.comma${OPENPILOT_PREFIX}/rtzs/navigation`; `RTZS_NAVIGATION_ROOT` overrides that root for tests. This feature does not modify other RTZS or Sentry files.

- `config/schema_version`: plain `1` plus newline; `config/mode`: plain `0` (Off) or `1` (Assisted turns) plus newline. A fixed `config.lock` protects readers and atomic field replacement. Missing, malformed, unsupported or insecure settings fail Off.
- `config/turn_speed_control`: optional plain `0` (off) or `1` (on) plus newline. Missing, malformed or insecure values mean off. The toggle does not enable reception or Assisted turns by itself.
- `private/relay.json`: `relay_id` (32 hex characters) and `key` (64 hex characters). Use the same independently generated key on the Waveshare. The public fixture key is explicitly rejected. The Wi-Fi password is not a relay authentication key.
- `logs/navigation-*.jsonl`: private, bounded receive diagnostics, at most 20 files of approximately 2 MiB each. File names retain a strictly increasing sequence even if the clock rolls backward; use each record's `capture_wall_time_ns` and `monotonic_ms` for timing, not the file name. Keys and raw authenticated packets are excluded. A four-record replaceable queue keeps file IO outside the receiver.

Navigation directories must be owned by the running user with mode 0700; files are 0600. Manual Wi-Fi ownership revisions are transient in `/dev/shm/rtzs-navigation-revision` (host `runtime/network-revision`). Failed revision tracking disables automatic Wi-Fi ownership.

This checkout contains a prebuilt native Params registry. New feature settings deliberately use RTZS files instead of unregistered Params keys; native cleanup cannot delete them. Python Cereal reads the source schema dynamically. Prebuilt `loggerd` has a static service table and does not automatically subscribe to this new service, so the separate JSONL ring is required until loggerd is rebuilt. Existing `modelDataV2SP` logging preserves the added hint fields on the wire.

## Protocol and behavior

Relay v1 uses UDP port 28443 with a maximum 1,200-byte packet, HMAC-SHA256, one enrolled relay, a 16-byte boot identity and receiver-generated one-time challenges. A response must arrive within 250 ms; challenges run at most 5 Hz. Only the active hotspot's actual `wlan0` address is bound. Internet routing is not needed. Guidance expires after one second without a valid response; BLE resets/disconnects clear it sooner when received.

The complete BLE navigation frame stays v1 and no larger than 1,024 bytes. Optional TLVs carry current maneuver ID (16), distance observation counter (17), distance age (18), maneuver observation counter (19) and maneuver age (20). Unknown/absent fields remain guidance-only. Distance freshness adds BLE assembly/queue dwell, a conservative complete challenge round-trip, and local receive age. Repeated observations cannot reduce their effective age; heartbeat repeats cannot create a new observation. Numeric distance is metres and never substituted with total remaining distance.

Only active route types 1/2/20/21 (ordinary left/right or left/right at end) may propose a low-speed turn. Lateral engagement, fresh distance at most 1,000 ms old, speed below the configured existing turn threshold and below 20 mph, and two decreasing observations are required. Arm at more than 15 and no more than 40 metres; crossing 15 metres may propose one pulse. Attaching inside the boundary never triggers late. Steering/brake/indicators, blind spots and existing maneuver desires take precedence. Reset, stale data and rerouting discard pending approach state. Already-consumed recurrent model hints cannot be recalled by resetting the driving model, so no model reset is attempted.

`turnLeft`/`turnRight` are the only possible active navigation desires. The independent opt-in speed controller adds a cruise-speed constraint; it does not change these lateral turn-hint rules, actuator control or driver monitoring. Model predictions can also indirectly affect existing longitudinal behavior. Highway types 14/23/53 remain diagnostic intent only. Neither lane recommendations nor camera road edges establish that an exit is reachable from the current lane.

The on-road UI shows only **Navigation running** while a current active route is available; no external road names, distances or branch instructions are drawn. It does not imply that turn assistance is eligible. After a successful model inference, both pipelines check the actual edge-filtered input vector. Only a navigation turn pulse present in that vector creates a latched `navigationTurnEvent`/`navigationTurnEventMonoTime`. The existing small, normal-priority alert renderer then displays **Turn left** or **Turn right** for at most two seconds (plus its existing fade). Existing safety/information alerts take precedence. This is a visual-only UI notice: it does not modify `selfdriveState`, audio or actuator commands. Repeated model publications retain the original event time, so a UI frame can miss the single input pulse without extending or fabricating the notice. Inference failure, eligibility alone and a suppressed rising edge create no notice. A confirmed input is not proof of physical turning or the model's response.

Existing `modelDataV2SP` rlogs also record distance age, receiver-to-model age, observation counter, transport token and the monotonic instant at which model input eligibility was evaluated. Missing ages use `0xffffffff`. These fields let a later audit distinguish source age from local delivery delay without reconstructing a transient model decision from separately sampled diagnostics. The one-second freshness limit and reset-on-stale behavior are unchanged: a 20 m observation that expires before the next 10 m observation still discards arming and cannot create a late turn pulse.

A captured authenticated old HELLO can interrupt relay availability by changing the pending boot binding; it cannot restore guidance without answering a fresh challenge. Old publisher sessions, duplicate replies and regressing source sequences are rejected. Bounded session/maneuver history fails closed when exhausted.

## Turn approach speed

This experimental setting contributes an optional speed cap to the existing longitudinal planner in ACC and blended modes. It requires Assisted turns, openpilot longitudinal capability and active longitudinal engagement. Stock longitudinal control is unsupported. Driver accelerator/brake input and disengagement remove the cap; resuming requires a new distance observation. The cap cannot raise the user's cruise setting or a lower existing speed constraint. Lead handling, collision-related behavior, curve controls and the planner's existing acceleration/jerk limits remain authoritative. Removing a cap restores ordinary planning rather than commanding acceleration or setting a zero-speed fallback.

The cap changes the cruise acceleration candidate, not the lead MPC trajectory. Its changes and releases pass through `get_cruise_accel`'s acceleration clipping and slew; the final acceleration still competes with lead and model constraints and receives vehicle limits. This planner has no universal hard jerk clamp on that final minimum-selected acceleration. The published trajectory arrays are lead MPC outputs; inspect final `aTarget` when validating response.

Only numeric maneuver types **1** (ordinary left), **2** (ordinary right), **20** (left at end), and **21** (right at end) are supported. This mapping matches the publisher's `Iap2Decoder` and the existing lateral policy. All other types, including keep-left/right, forks, ramps, highway changes, U-turns, roundabouts and separately typed sharp/slight turns, are excluded. Instruction text does not determine a turn speed. There is no traffic-light, stop-sign, right-of-way or lane-selection logic.

The development envelope is `sqrt(v_turn² + 2 * a_approach * max(distance - buffer, 0))`, using metres and m/s. The fixed candidates are `v_turn = 14 mph = 6.25856 m/s`, `a_approach = 0.45 m/s²`, and `buffer = 8 m`. These are unvalidated development values, not a promise of a safe speed for an intersection. No tuning controls are exposed. The envelope first falls below an unchanged 30, 50 or 80 km/h target at approximately 41.6, 178.8 or 513.2 metres respectively, before activation hysteresis. These are target intersections, not measurements of vehicle deceleration or achieved corner speed. Unlike lateral hints, speed control has no 20 mph gate or 15–40 m arming window; coherent guidance first received close to a turn can lower the target through the same planner limits.

The controller selects the cap when it is at least 0.25 m/s below the ordinary target and releases selection when the gap is at most 0.10 m/s. Fresh observed distances are used without extrapolation. Small upward jitter of at most 10 metres holds the previous accepted distance; a larger increase or a drop larger than `max(100 m, half the previous distance)` requires a second coherent, distinct observation before reacquisition. Coarse updates can therefore leave the target in steps; planner dynamics still constrain the response. A speed cap above current vehicle speed is a ceiling, not an acceleration instruction. Vehicle minimum steering speed does not raise the nominal turn-speed target; some vehicles can lose lateral assistance below their steering threshold.

Distance and delivery ages must each be at most 1000 ms; missing, negative, non-finite or over-5000-metre distance is rejected. Effective distance age includes the publisher's original observation age, BLE assembly/queue residence, conservative challenge RTT and receiver-to-planner delivery age. Repeated snapshots cannot renew an observation. A repeated equal numeric distance from the phone with an advancing observation counter is new evidence. `maneuverAgeMs` measures the age of the phone's current-selection observation: a known old selection remains usable with fresh distance bound to the same identity, because the phone need not repeat it on every update. Unknown selection age is rejected; it is never substituted with source/route age.

An approach is fenced by publisher session, cache epoch, stream, generation and maneuver ID, with separate receiver-session/token transport fencing. Counter regression, same-counter changed content or inconsistent maneuver type retires that identity. A transport reset removes the cap and requires an advancing distance observation. Route cancellation, missing/invalid/stale fields and unsupported guidance remove the cap. Another maneuver must supply its own distance. Zero metres remains a valid observation and is not proof of completion. A cap expires for that identity at most 120 seconds after first selection, or 10 seconds after first accepted distance at or below 8 metres. These deadlines survive temporary inhibition and transport interruption, and the retired-identity budget is bounded. Such expiry is fallback cancellation, not a claim that a physical turn finished.

`longitudinalPlanSP.navigationSpeedControl` records eligibility/rejection, approach state, release reason, identity, observed and accepted distance, effective ages, receiver/evaluation timestamps, counters, nominal speed and calculated cap. `capAvailable` means a valid envelope exists; `speedSelected` means it lowers the existing cruise-speed candidate; `cruiseCandidateSelected` additionally means the cruise acceleration candidate wins final arbitration. The appended `externalNavigation` speed-source value does not assert that navigation overrode a lead or model acceleration constraint. The baseline source/target remain recorded. These fields ride the existing logged planner service and contain no instruction, road, destination or authentication material; no extra per-frame text log is added.

## Validation and development replay

Run `python -m unittest discover -s openpilot/sunnypilot/external_navigation/tests -v`. The portable tests cover the shared Java/C++ packet corpus, real localhost UDP, malformed/authentication/reset cases, age progression, manual priority, both pipeline call sites, approval-independent turn eligibility, private files, hotspot ownership and UI geometry. Install pycapnp 2.1.0 to include the source-schema round-trip test.

Run `python -m unittest openpilot.sunnypilot.selfdrive.controls.lib.tests.test_nav_speed_planner -v` for speed arbitration and native planner integration tests. The native cases explicitly skip when compiled dependencies cannot import; a skip is not a successful planner validation.

Policy-only replay accepts JSONL with `sample` fields matching `policy.Sample`, and `vehicle` containing `lateral_active`, `speed`, `speed_limit`, and optional `manual`. It never emits active desires:

```sh
python -m openpilot.sunnypilot.external_navigation.replay policy /path/to/trace.jsonl
```

Speed-cap replay runs with an explicit monotonic clock and never subscribes to messaging or sends actuator commands:

```sh
python -m openpilot.sunnypilot.external_navigation.replay speed /path/to/speed-trace.jsonl
```

Each JSONL row contains `now_ns` (nonnegative, nondecreasing integer nanoseconds), `sample` (the fields of `speed_control.NavigationSpeedSample`, or null), and `vehicle` with `v_ego` and `baseline_target` in m/s. Optional vehicle fields are `enabled`, `longitudinal_active`, `driver_override`, and `inhibit_reason`. Encode the first element of `sample.identity` and `sample.transport` as a 32-character hexadecimal session string; the remaining identity/token fields are integers. Supply observation ages already effective at `now_ns`, including delivery delay. Output includes the raw envelope, selected optional cap, eligibility, state and reason, and always marks `activation_approved` false. Identical input gives identical output. The replay evaluates target selection only; vehicle-dynamics validation requires planner simulation and controlled testing.

`python -m openpilot.sunnypilot.external_navigation.tests.speed_replay` also exercises the actual cruise acceleration helper with an ideal point-mass plant at 30, 50 and 80 km/h. This portable check measures envelope timing and existing cruise slew; it does not execute the native lead MPC, simulate actuator delay/tyres/lateral control, or establish achieved speed on a real vehicle. Follow deterministic replay with full planner simulation and controlled validation before public-road deployment.

The PC-only native planner launcher uses the recorded model evaluation clock so archived receiver timestamps retain their original delivery age:

```sh
python -m openpilot.sunnypilot.external_navigation.replay planner --log /path/to/rlog.zst --output /tmp/nav-planner
python -m openpilot.sunnypilot.external_navigation.replay planner --log /path/to/rlog.zst --output /tmp/nav-baseline --disabled
```

This requires synchronized recorded `externalNavigationSP` inputs alongside normal planner inputs and compatible compiled messaging/MPC dependencies. Older logs from the prebuilt logger may omit that optional navigation service; do not reconstruct observations from aggregate health statistics or invent receipt timestamps. A baseline-only run can omit navigation input. The checked-in Linux aarch64 native binaries do not establish that a macOS host can execute native planner replay; a successful portable run is not native planner verification.

On a development PC with the normal compiled model/replay dependencies and local camera recordings:

```sh
python -m openpilot.sunnypilot.external_navigation.replay camera \
  --log /path/to/rlog.zst --narrow /path/to/fcamera.hevc --wide /path/to/ecamera.hevc \
  --pulse-frame 123 --pipeline stock --hint keepRight --output /tmp/nav-replay
```

The tool runs neutral and hinted modeld in separate existing replay namespaces. It writes paired frame outputs and differences in lateral position, predicted velocity and desired acceleration. `--pipeline tinygrad`, `--hint turnLeft`, and `--hint turnRight` are also supported. `replay compare neutral.jsonl hinted.jsonl` compares existing exports. Exact frame IDs and trajectory shapes must match. The research launcher refuses vehicle hardware and non-replay invocation; it is not registered with manager.

Host tests and schema checks do not validate physical Wi-Fi/BLE coexistence, live end-to-end latency, recorded-camera model behavior, closed-course turns or a one-hour soak. Assisted turns uses the runtime checks above; replay and host checks do not establish actual driving behavior.
