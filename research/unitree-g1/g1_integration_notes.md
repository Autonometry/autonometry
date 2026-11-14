# Unitree G1: integration notes

Research for milestone M9, 14 November 2026. Every fact cites `signal_map.yaml` (S1 to S12), which
pins each source to a commit and line. Facts tagged MIRROR come from official Unitree pages read
through a third-party copy; facts tagged TEST were checked by running code (`dds_compat/`). Where
this file and `spec/v0.3/definitions.md` disagree, the spec wins.

## 1. Summary

- Programs that command a G1 through Unitree's SDKs talk DDS (Cyclone DDS 0.10.2) on topics named
  `rt/...`: joint commands, an upper-body blend with a weight, hand commands, and JSON requests to
  Unitree's services. The handheld remote's data arrives inside `rt/lowstate`. (S1-S9)
- The same command topics carry a person's teleoperation, a policy and a replay alike. Only the
  writer's identity tells them apart: each Cyclone participant announces its host, process id and
  program name, and each sample maps to its writer. A Python program announces only its interpreter
  (`python`, `python3`), so the observer reads the script name from the process id when it runs on the
  same computer. (S10, TEST)
- The observer must use the Cyclone DDS 0.10 line. In our test an observer on Cyclone DDS 11.1 made
  0.10.2 and 0.10.5 publishers crash as soon as it subscribed; on 0.10.5 it read them without harm. (S11, TEST)
- Unitree's app (WebRTC) and voice service can move the robot without anything on DDS. Motion with no
  observed source is `unknown`. (S12)

## 2. Control paths

| Path | Where it shows | Typical sources |
|---|---|---|
| Built-in controller (ai_sport on PC1: balance, walking, running, postures) | `rt/sportmodestate` fsm_id and fsm_mode (S1) | runs from power-on |
| Handheld remote into the built-in controller | `rt/lowstate` wireless_remote (S2) | a person next to the robot |
| Requests to the built-in controller | `rt/api/sport/request` (S7) | xr_teleoperate controller mode (a person), user programs (AI or not), the app's WebRTC module |
| Built-in controller off (debug mode) | `rt/api/motion_switcher/request` 1003 (S8), or the remote's L2 + R2 | xr_teleoperate without `--motion`, low-level programs |
| Joint commands in debug mode | `rt/lowcmd` writers (S3) | xr_teleoperate, unitree_rl_gym and unitree_IL_lerobot policies, replay_robot.py, LeRobot's run_g1_server.py bridge |
| Joint commands in user control | `rt/user_lowcmd` writers after 7110 (S5) | user programs |
| Upper body blended into the built-in controller | `rt/arm_sdk` writers and weight motor_cmd[29].q (S4) | xr_teleoperate `--motion`, policies with motion_mode, Unitree's arm action service |
| Arm presets and taught motions | `rt/api/arm/request`, `rt/arm/action/state`, loco 7106, remote SELECT + Y/A/X (S9, S7, S2) | a person or a program |
| Hands | `rt/dex3/*/cmd`, `rt/inspire*`, `rt/brainco/*/cmd`, `rt/dex1/*/cmd`, `rt/hand_sdk` (S6) | teleoperation retargeting, policies |
| App, voice | nothing on DDS (S12) | a person |

## 3. Observer safety (product rule 1)

- Subscribe only. No DataWriter on any topic, and no RPC request of any kind, not even a query such
  as GetFsmId or CheckMode: a request is a write to the robot's services.
- Readers are best-effort and volatile, so the robot's writers never wait for or resend to the
  observer.
- Cyclone DDS 0.10 line only, the same as the robot (S11). The watcher refuses to start with any other
  version and says why. Never connect a Cyclone DDS 11 tool to a G1 network.
- Light load: one observer per robot network. `rt/lowstate` with keep-last 1, read about 20 times a
  second; command topics with a keep-last history (at least 64) so that every writer's samples are
  seen, taken every pass.
- Where it runs: on PC2 (192.168.123.164, an arm64 Jetson; MIRROR) or on an x86-64 Linux computer on
  192.168.123.x. On the computer that runs the teleoperation and policy scripts it can tell Python
  scripts apart (section 4). Cyclone DDS 0.10 has no wheels for Python 3.12: build the C library from
  the 0.10 branch and set `CYCLONEDDS_HOME`, as Unitree's Python SDK asks (S11).

## 4. Identity and the device registry

The observed source of a sample is `(host, participant, topic)`:

- `host`: the writer's participant property `__Hostname`.
- `participant`: for an interpreter process (python, python3, python3.x) on the observer's own
  computer, the script's file name or `-m` module, read from `__Pid` while the process with that id
  still has that program name; otherwise `__ProcessName`, for example `python3` or a compiled
  example's name.
- `topic`: the command topic, or `wireless_remote` for the handheld remote (S2).

These fill the registry's `match` fields `host`, `participant` and `topic` (spec 6.10). A source
matches the first device whose given fields all equal what was observed. The watcher refuses a
registry in which one observed source could match two devices of different kind or location: a
generic `python3` on another computer cannot tell a teleoperation script from a policy. Sources from
participants that announce no properties (other DDS vendors) have no host or participant.

`autonometry g1 discover` lists every participant and what it publishes, with the exact strings for
`match`, so the registry is copied from what the robot shows. Typical devices: the built-in
controller (kind `ai`, the writer of `rt/sportmodestate`), the handheld remote (kind `human`,
location `local`, topic `wireless_remote`), the arm action service (kind `script`, its `rt/arm_sdk`
writer), xr_teleoperate (`human`, local or remote by where the person stands), policy scripts
(`ai`), replay scripts (`script`). A relay such as LeRobot's run_g1_server.py writes for whichever
client it serves; declare it as the kind of its only client, or leave it undeclared so that it
counts as a remote human.

## 5. Scopes

| Scope | Joints | DoF |
|---|---|---|
| `legs` | both legs | 12 |
| `upper_body` | waist (3) and both arms (14): the `rt/arm_sdk` joints (S4) | 17 |
| `hands` (only when the robot has hands and the config declares it) | Dex3-1: 7 per hand | 14 |

## 6. Rules

Definitions used below:

- Person: the handheld remote or a device of kind `human`. Undeclared: a source no device matches.
- Active writer: a writer of the topic whose latest sample is at most `gap_tolerance_s` old.
- Built-in state: `fsm_id` and `fsm_mode` of the latest `rt/sportmodestate` sample from the declared
  built-in controller. Passive: fsm_id 0 or 1. Moving: fsm_mode 1.
- Released: from a ReleaseMode request (1003) or the remote's L2 + R2 until a SelectMode request (1002).
  User control: from SwitchToUserCtrl (7110) until SwitchToInternalCtrl (7111).
- Goal span of a source: opened by its goal input (remote sticks outside the dead zone of 0.01; a
  remote button other than the combinations named in rules 2, 3, 5 and 6; a request on
  `rt/api/sport/request` other than a query); it lasts until `goal_release_s` after its last
  non-neutral input, or to the end of the duration its SetVelocity carries, whichever is later. A
  neutral input from the same source (sticks centred, zero velocity) ends it at once (6.11).
- Open task: known from the task events in the recorder's current session log (section 8).

For each scope at each pass, the first rule that applies decides.

1. **Robot lost.** No `rt/lowstate` sample for more than `gap_tolerance_s`: every scope `unknown`
   (Section 2, 6.14). The remote's bytes are taken only from the `rt/lowstate` writer that the
   handheld remote's device names (its `host` and `participant`).
2. **Emergency stop.** An emergency-stop control: the remote's L2 + B (Unitree's emergency stop,
   which also works in debug mode, S2), or a Damp or ZeroTorque request (7101 with data 1 or 0) from a
   person or an undeclared source (xr_teleoperate's both-thumbstick press is one, S7), followed within
   `gap_tolerance_s` by a passive built-in state, or at once while the built-in controller is unseen
   or released: every scope `safety_stop`, reason `safety`, `actor_id` the person's (or
   `person:unidentified`), until the built-in state leaves passive or the next change of mode (a
   SelectMode, ReleaseMode or user-control request, or a remote mode combination). Writers that keep
   writing do not end it (6.7, 6.8).
3. **Released or user control.** `legs` and `upper_body` follow the active writers of `rt/lowcmd`
   (released) or `rt/user_lowcmd` (user control): one writer, its device's state (`human` to
   `human_local` or `human_remote` by location, `ai` to `ai`, `script` to `scripted`; undeclared: sent
   without `device_id`, which the loader counts as `human_remote`); several within
   `contention_window_s`, Section 6.12; none, inside an open task `unknown` after `gap_tolerance_s`,
   outside the last controller's state until `idle` (6.14).
4. **Built-in controller unseen** (`legs`, `upper_body`). No `rt/sportmodestate` from the declared
   controller for more than `gap_tolerance_s` (firmware before 1.5.1, or it stops in debug mode) and
   not released: rule 3 when a `rt/lowcmd` writer is active (debug mode entered before the observer
   started), else `unknown`.
5. **Other passive states** (`legs`, `upper_body`). The hold of whoever caused it (6.8): a request from an `ai` or `script`
   device, `ai` or `scripted`; the remote's L2 + Y, `shared` with reason `human_initiated`; no observed
   cause, `unknown`. Outside an open task, the last controller's state until `idle` instead (6.14).
6. **`upper_body`, built-in controller active.**
   a. An arm preset runs: from a request (`rt/api/arm/request` 7106 or 7108, `rt/api/sport/request`
      7106) or the remote's SELECT + Y, A or X, until `rt/arm/action/state` reports release (id 99)
      or no hold, the custom action stops, sportmodestate `task_id` returns to 0, or 20 s after the
      last sign of it: `scripted`, actor `script:<preset name or id>`, reason `human_initiated` when a
      person started it (6.9, 6.13). The arm action service's own `rt/arm_sdk` writes are this preset.
   b. Another active `rt/arm_sdk` writer: weight at least 0.99, that writer's state; between 0.01 and
      0.99, a blend with the built-in controller (6.12): `shared` when the writer is a person or
      undeclared, else `ai`; at most 0.01, as `legs`.
   c. Otherwise as `legs`: the built-in controller moves the upper body with the gait.
7. **`legs`, built-in controller active.**
   a. Open goal spans: one source, a person `shared` with reason `human_initiated` (6.11), an `ai`
      device `ai`, a `script` device `scripted`, undeclared `human_remote` (sent without `device_id`);
      several within `contention_window_s`, Section 6.12. A posture a person started (L2 + UP, LEFT,
      X or A; SetFsmId 2, 3, 4, 702 or 706 from a person) is a fixed motion: `scripted`, actor
      `script:fsm_<id>`, reason `human_initiated`, while it moves (6.13).
   b. No goal span and moving for more than `gap_tolerance_s`: `unknown` (S12: the app or the voice
      service may be driving).
   c. No goal span and static in a position-control posture (fsm_id 2, 3 or 4, no balance control):
      as rule 5, the hold of whoever put it there.
   d. No goal span and static otherwise: inside an open task `ai`, actor the built-in controller's (the
      AI keeps balance as part of the task); outside, the last controller's state until `idle` (6.14).
8. **`hands`.** The active hand writers (S6; for `rt/hand_sdk` a weight above 0.01): one, its state;
   several, Section 6.12; none, as in rule 3.

Evidence goes in `x_` fields: `x_g1_fsm_id`, `x_g1_cause` (for example `remote:L2+B`,
`request:sport:7101`, `writer`, `unobserved`) and `x_g1_weight` on transitions; the active writers
with their observed identities on a heartbeat note, as TB4's `x_publishers`.

## 7. Thresholds

The spec's parameters come from `rules.toml` (`gap_tolerance_s` 2, `goal_release_s` 1,
`contention_window_s` 1, `idle_threshold_s` 30). The connector's own constants: stick dead zone
0.01 (Unitree's example, S2); weight limits 0.01 and 0.99; preset hold limit 20 s (S9).

## 8. Tasks

The connector opens no tasks. The robot program sends `task_start` and `task_end` to the recorder
through the client library. The connector learns which task is open by following the recorder's
current session log, read-only, as it already follows files for LeRobot. If it cannot read the log
it treats every moment as outside a task and says so once on a heartbeat.

## 9. Blind spots

- App and voice control (S12): motion with no observed source is `unknown` (rule 7b); a posture or
  a stop they cause is a passive or posture state with no observed cause.
- A relay (run_g1_server.py) hides its clients (section 4).
- Debug mode needs the robot hung on a rack (MIRROR): that is physical support (6.15), which the
  operator records; the connector cannot see the rack.
- What the remote's bytes hold when it is off is not documented.
- Participants of DDS vendors other than Cyclone announce no host or program name.

## 10. Testing

- Engine: unit tests per rule citing the spec section; Hypothesis: random observation sequences never
  crash, never yield `ai` while a person's goal span is open, and yield `unknown` once the robot is lost.
- DDS (Linux and macOS CI job): build the Cyclone DDS 0.10.5 C library (about 20 s, cache it) and the
  0.10.5 Python binding from source; publishers in separate processes play the built-in controller,
  the robot's lowstate with remote bytes, xr_teleoperate, a policy and the arm action service; the
  observer must name each writer and give the expected states. A second participant checks through
  DCPSPublication that the observer created no writer.
- Version guard: the watcher refuses Cyclone DDS 0.11 or later.
- Golden: a scripted session through the recorder to a report, with numbers worked out by hand first.
- Optional by hand: unitree_mujoco simulates the G1's low-level topics on domain 1 over `lo`
  (a clone is about 411 MB with meshes); it does not run Unitree's built-in controller.

## 11. Open questions for a real robot

1. The program names and hosts of PC1's services (built-in controller, arm action service, WebRTC
   module, voice): read them with `discover`.
2. Whether `rt/sportmodestate` is published at all (firmware 1.5.1 or later) and in debug mode.
3. The type of `rt/arm/action/state`.
4. Whether `rt/lowcmd` writes are ignored while the built-in program is active.
5. fsm_id 801 or 802 for running on your firmware.
6. Whether the remote can end debug mode without a SelectMode request.

## 12. Deferred

ROS 2 node names through `ros_discovery_info`; station witness (L3) for xr_teleoperate; the G1-D's
wheeled base (`agv` service); Unitree H1, H2 and R1, which share the SDK.
