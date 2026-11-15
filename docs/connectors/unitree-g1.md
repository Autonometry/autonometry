# Unitree G1

`autonometry.connectors.g1` and `autonometry g1` observe a Unitree G1 humanoid (EDU, 29 DoF, with or
without hands) on its DDS network and send the control state of `legs`, `upper_body` and, when
the robot has hands, `hands` to the recorder. The design and its sources are in
[`research/unitree-g1/`](../../research/unitree-g1/g1_integration_notes.md).

> **Warning.** Never connect a Cyclone DDS 11 tool (an observer, `ros2 topic echo` on a recent
> ROS 2, a Python program with `cyclonedds` 11 from PyPI) to a G1 network. In our test an observer
> on Cyclone DDS 11.1 made the robot's 0.10 publishers crash as soon as it subscribed
> (`research/unitree-g1/dds_compat/`). The G1 runs Cyclone DDS 0.10; the watcher refuses to start
> with anything else.

**Observer only.** The watcher only subscribes. It creates no DataWriter on any topic and sends no
request to Unitree's services, not even a query such as GetFsmId or CheckMode, since a request is
a write to the robot. Its readers are best-effort and volatile, so the robot never waits for it
or resends to it. Run one watcher per robot network: on PC2 (192.168.123.164) or on a Linux
computer on 192.168.123.x. On the computer that runs the teleoperation and policy scripts it can
also tell Python scripts apart (below).

**Install, with Python 3.12.** Cyclone DDS 0.10 for Python does not load on Python 3.13 (its
extension needs a symbol 3.13 removed), so the G1 commands refuse any Python but 3.12 with one
line. It has no wheels for 3.12 either: build the C library from the 0.10.5 tag and the Python
binding from source, as Unitree's Python SDK does for 0.10.2:

```
git clone --depth 1 --branch 0.10.5 https://github.com/eclipse-cyclonedds/cyclonedds.git
cmake -S cyclonedds -B cyclonedds/build -DCMAKE_INSTALL_PREFIX="$HOME/cyclonedds" \
    -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF
cmake --build cyclonedds/build --target install --parallel
export CYCLONEDDS_HOME="$HOME/cyclonedds"
pip install --no-binary cyclonedds cyclonedds==0.10.5   # in a Python 3.12 environment
pip install 'autonometry[g1]'          # psutil, to name Python programs by their script
```

From a clone of this repository: `uv sync --python 3.12 --extra g1`, then
`uv pip install --no-binary cyclonedds cyclonedds==0.10.5` and `uv run --no-sync autonometry g1 ...`.

Keep `CYCLONEDDS_HOME` set when you run the watcher. Cyclone DDS is not a declared dependency of
autonometry, so `pip install autonometry` never pulls a version that would harm the robot.

**1. Discover.** With the robot and its programs running:

```
autonometry g1 discover --interface eth0            # --domain 0 by default; --json; --seconds 5
```

It lists every participant with its host (and, beside it, the pseudonym under which the log
names that host), program and pid, the topics and type names it publishes, and for each writer the
exact `match` to copy into the device registry. A writer is
observed as `(host, participant, topic)`: `host` is the participant's `__Hostname`; `participant`
is its `__ProcessName`, except that a Python interpreter (`python`, `python3`, `python3.x`;
`Python` on macOS) on the
watcher's own computer is named by its script file or `-m` module, read from its pid while that pid
still runs that program. Python programs on another computer all show as `python3`.

**2. Write the registry.** `connectors/g1/devices.json` is a template; replace its hosts and
participants with what `discover` printed. Typical devices:

| Device | `kind` | `match` |
|---|---|---|
| the built-in controller (ai_sport) | `ai` | its host and participant, topic `rt/sportmodestate` |
| the handheld remote | `human`, `local` | the host and participant of the `rt/lowstate` writer, topic `wireless_remote` (`discover` prints it as "handheld remote match") |
| Unitree's presets (arm actions, taught motions, postures) | `script` | `{"topic": "preset"}` |
| the arm action service | `script` | its host, participant and topic `rt/arm_sdk` |
| xr_teleoperate | `human`, `local` or `remote` by where the operator stands | its host and script |
| a policy, a replay | `ai`, `script` | their host and script |

A source matches the first device whose given fields all equal what was observed (6.10). An
undeclared source counts as a remote person. The watcher refuses a registry in which one observed
source could match two devices of different kind or location: a bare `{"participant":
"python3"}` cannot tell a teleoperation script from a policy. Without the `preset` device, presets
count as remote human time. A relay such as LeRobot's `run_g1_server.py` writes for whichever
client it serves: declare it as the kind of its only client, or leave it undeclared.

**3. Watch.** Start the recorder, then the watcher, then the robot's programs:

```
autonometry record --config connectors/g1/recorder.toml &
autonometry g1 watch --config connectors/g1/recorder.toml --interface eth0
```

The watcher, like the recorder, runs on Linux and macOS. It reads the recorder's config for its
socket, scopes (`legs` and `upper_body`, and `hands` if declared), data folder and device registry,
sends every scope's state every 0.25 s and on each change, and records the active writers as
`x_g1_writers` on a heartbeat when they change. Tasks are the robot program's: it sends
`task_start` and `task_end` to the recorder through the client library, and the watcher follows
the recorder's current session log, read-only, to know whether a task is open. If it cannot read
that log, every moment counts as outside a task, and it says so once on a heartbeat
(`x_g1_tasks: "unreadable"`); run it as the recorder's user.

**What the log holds.** Control metadata only (product rule 5): states, reasons, device ids and
the pseudonymous actor ids of the registry, the `x_g1_` evidence above, and in `x_g1_writers` a
writer that matches a device by its topic and `device_id` only. An undeclared writer is listed by
its topic and pseudonyms of its program (script or process name) and its host: the first 12 hex
digits of the SHA-256 of the name, never the name itself, since machine and program names often
hold a person's name. `discover` prints the same pseudonyms beside each host and participant, and
`python3 -c "import hashlib; print(hashlib.sha256(b'NAME').hexdigest()[:12])"` computes one, so a
log entry can be traced to a machine or program. No video, audio, network address or process id
is recorded.

**Restart the watcher after restarting the robot.** Released (debug mode) and user control end
only on a request the watcher sees. A restarted robot comes back in Unitree's built-in program,
but the watcher keeps it released, which never counts towards `ai` and is safe, only wrong. When
`rt/lowstate` returns after more than `idle_threshold_s` (30 s) while released or in user control,
the watcher logs one warning and notes `x_g1_robot_returned` (`released` or `user_control`) on a
heartbeat.

Rules (notes section 6 under spec v0.3; for each scope, the first that applies decides):

| Observed | legs, upper_body | hands |
|---|---|---|
| no `rt/lowstate` for more than `gap_tolerance_s` | `unknown` | `unknown` |
| the remote's L2 + B, or Damp or ZeroTorque (7101, data 1 or 0) from a person or an undeclared source, followed within `gap_tolerance_s` by a passive state (at once while the built-in controller is unseen or released) | `safety_stop`, the person as actor, until the robot confirms a change: the state leaves passive, or the built-in controller answers a mode request with success; a request or remote combination alone never ends it (6.7, 6.8) | the same |
| released (ReleaseMode 1003 or L2 + R2, until SelectMode 1002) or user control (7110 until 7111) | the active writers of `rt/lowcmd` or `rt/user_lowcmd`: one, its device's state; several within `contention_window_s`, 6.12; none, `unknown` inside a task, else the last controller's state until `idle` (6.14) | |
| no `rt/sportmodestate` from the declared built-in controller | as released when an `rt/lowcmd` writer is active, else `unknown` | |
| a passive state (fsm 0, 1) | the hold of whoever caused it: an AI or script's request `ai` or `scripted`, the remote's L2 + Y `shared`; no observed cause `unknown`; outside a task the last controller's state (6.8) | |
| upper body: a preset runs (arm 7106, 7108; sport 7106; SELECT + Y, A, X) | `scripted`, `script:<preset>`, `human_initiated` when a person started it, until the arm state releases, `task_id` returns to 0 or 20 s after its last sign (6.9, 6.13) | |
| upper body: another `rt/arm_sdk` writer | weight ≥ 0.99 its state; above 0.01 blended with the built-in controller, `shared` for a person, else `ai` (6.12) | |
| legs: goal input (the remote's sticks outside 0.01 or buttons, a sport request other than a query) | a person `shared` (6.11), an undeclared source `human_remote`, a program its state; several sources, 6.12; a posture a source started (L2 + UP, LEFT, X, A; SetFsmId 2, 3, 4, 702, 706) `scripted` while it moves (6.13) | |
| legs: moving with no goal input for more than `gap_tolerance_s` | `unknown` (the app or the voice service may drive) | |
| legs: static | in a position posture (2, 3, 4) the hold of whoever put it there; else `ai` inside a task (the built-in controller balances), the last controller's state outside | |
| otherwise | as legs (upper body) | the active hand writers (`rt/hand_sdk` above weight 0.01): one, its state; several, 6.12; none, as released |

Evidence travels in `x_g1_cause` (for example `remote:L2+B`, `request:sport:7101`, `writer`,
`unobserved`), `x_g1_fsm_id` and `x_g1_weight`.

**Where the notes leave a case open** the watcher takes the more conservative state, never `ai`:
a person's live goal input contends (6.12) with an AI or scripted state of the legs, also while
released, so it becomes `shared`; every source with an open goal span contends, however far apart
their last inputs; a writer on `rt/arm_sdk` during a preset contends with it; an arm motion with no
observed start (sportmodestate `task_id` not 0, or the arm action state holding) is `unknown`; the
arm action state's "no hold" ends a preset only after it held; a stop that began at once (released
or the controller unseen) ends only at a confirmed change of mode; a stick value that is not a number counts
as input; a writer whose weight cannot be read counts as holding its joints; a malformed velocity
only extends a goal span; a response only ever keeps the robot released or in user control (a
refused SelectMode or SwitchToInternalCtrl, or a CheckMode answer naming no mode), never takes it
out. Each is marked `Open case` in the code.

**Names on the robot network are claims, not proof.** Host, program and pid come from each DDS
participant itself: anyone on the robot's network can claim a declared program's identity. The
watcher believes RPC responses only from the declared built-in controller's identity (the host and
participant of its `rt/sportmodestate` writer) and notes any other on a heartbeat
(`x_g1_ignored_response`). It believes `rt/lowstate` (the robot's presence and the remote's
bytes) and `rt/arm/action/state` only from the robot: the host of that same writer, remembered
once seen, so that the robot is not lost if the controller's writer goes away in debug mode. A
sample from any other computer, or from a writer discovery has not shown, is ignored and noted
(`x_g1_ignored_robot_state`), so it can neither keep a silent robot present nor pass for the
remote. (Unitree's services on PC1 are separate programs and the notes name none for these two
topics, so the robot's identity is its host: an open case.) Two writers with the same observed identity that command at the same
time are both undeclared, `human_remote`, and noted (`x_g1_duplicate_identity`), as the TurtleBot 4
does for a node name two publishers share. What a name cannot show, trust level L3 can: the
station's own sealed log proves remote control (see [Station
witness](../witness.md)). DDS Security would make identities authenticated.

**Blind spots.** Unitree's app (WebRTC) and voice service command the robot without anything on
DDS: their motion is `unknown`, and a posture or stop they cause has no observed cause. A relay
hides its clients. Debug mode needs the robot hung on a rack, which is physical support (6.15):
record it with `support_start` and `support_end`; the watcher cannot see the rack. What the
remote's bytes hold when it is off is not documented. Participants of other DDS vendors announce
no host or program. The template config declares `unobserved = ["safety_stops", "human_inputs"]`.

**Tested** in CI (`dds` job, Linux and macOS): Cyclone DDS C 0.10.5 built from its tag (cached)
and the 0.10.5 binding from source; five fake G1 programs in separate processes (the built-in
controller, the robot's lowstate with remote bytes, xr_teleoperate, a policy, the arm action
service) over loopback; the observer names each writer by host and script and gives the expected
states; a second participant confirms through DCPSPublication that the observer has no writer;
every program survives; `discover` lists them; `watch` feeds a real recorder end to end and the
log verifies. Not yet run against a real G1: the program names of PC1's services, whether
`rt/sportmodestate` is published in debug mode, and the type of `rt/arm/action/state` (read only
when discovery shows `std_msgs::msg::dds_::String_`) are open questions for a real robot (notes
section 11).

**Deferred.** ROS 2 node names through `ros_discovery_info`; a station witness (L3) for
xr_teleoperate; the G1-D's wheeled base; Unitree H1, H2 and R1.
