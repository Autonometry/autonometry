# TurtleBot 4

`autonometry.connectors.tb4` and the ROS 2 package `connectors/ros2/autonometry_ros` observe a
TurtleBot 4 (ROS 2 Jazzy, Nav2, Create 3 base) and send its control events to the recorder. The
observer only subscribes: it never publishes on a robot topic, calls a service or sends an action
goal (it even turns off its `/rosout` publisher and parameter services), so it cannot slow or stop
the robot, and nothing on the robot depends on it. A simulator demo for Windows 11 with WSL2 is in
[`demo/tb4-sim/`](../../demo/tb4-sim/README.md) (untested; see there).

**Engine.** `Tb4Engine` is pure Python, standard library only, and runs and is tested on every
platform: it takes one small dataclass per topic, stamped with the observer's receive time, and
returns the state of the scope `base`, task events, hints and the odometer, in the order to send
them. Rules (integration notes B.2 under spec v0.3; the first that applies decides the state):

| Observed | State or event |
|---|---|
| kidnapped for 1 s or more | `human_local`, device `manual_handling` (6.15) |
| `EStop` on `/function_calls`, or wheels disabled | `safety_stop` triggered by a person, with the button's device (6.7) |
| a routine running: Create 3 dock, undock, wall follow; Nav2 spin, backup, drive on heading, assisted teleop | started by a button, a person or an undeclared client: `shared`, `human_initiated` (6.13); by a declared AI or script (bt_navigator's recoveries, a mission node): `ai` or `scripted` |
| `/cmd_vel` from a person, or relayed by Nav2's chain from one (below) | `human_local` or `human_remote` by the device registry; undeclared publisher, or a node name two publishers share: `human_remote` (6.10); with another source within `contention_window_s`: `shared`, `human_initiated` (6.12); ends at a neutral command, a dead-man release, or `goal_release_s` after the last command (6.11) |
| motion on `/odom` with no command for `gap_tolerance_s` | `unknown`, `x_out_of_band` |
| Nav2 left the graph, or nothing from it (status or feedback of a task action) for more than `gap_tolerance_s` during a task | `unknown`, `x_cause` `nav2_left_graph` or `nav2_silent` (definitions.md 2, 6.14) |
| Nav2 `NavigateToPose`, `NavigateThroughPoses` or `FollowWaypoints` goal, its status published by a declared AI node | `EXECUTING` with no task: `task_start` (`nav_to_pose`, `nav_through_poses`, `patrol`) and `ai`; another task action's goal inside the task (the waypoint follower's) is part of it; `SUCCEEDED`, `ABORTED`, `CANCELED`: `task_end` `success`, `failure`, `aborted`; a person's new goal or pose estimate: a hint (`shared` under `hint_as_shared`); a person's cancel: `shared` until the task ends (6.2, 6.8); collision monitor STOP: `safety_stop` with no actor (automatic) |
| `/initialpose` from a person before a task | `human_remote`, `planned`, plan item `initial_pose` |
| wheels enabled again, no commander yet | `unknown` |
| no task and no command for `idle_threshold_s` | `idle`, docked or not |
| otherwise | the last controller's state: outside a task it holds until idle (6.14) |

Under `hint_as_shared`, a hint that a higher rule hides (for example a cancel during an EStop) is
also sent as a `hint` event, so a person's input is never lost.

**The relay chain.** Nav2 reaches `/cmd_vel` through a chain: `controller_server` and
`behavior_server` publish `cmd_vel_nav`, `velocity_smoother` publishes `cmd_vel_smoothed`, and
`collision_monitor` relays it to `/cmd_vel`. Whatever is published upstream arrives as the
relay's, so the relay's identity alone proves nothing. A relayed command counts as `ai` only
when every publisher of `cmd_vel_nav` and `cmd_vel_smoothed` is a declared AI device and a Nav2
task, or a routine a declared AI started, runs or ended less than `gap_tolerance_s` ago.
Otherwise it is a person's (6.10, 6.12): `shared`, `human_initiated` while such an AI action
runs, else `human_remote`. Its device is the one possible origin when the chain leaves only one,
else undeclared with `x_attribution_inferred: true`. A chain topic whose publishers were never
seen counts as not declared. Declare the whole chain in the device registry, as
`demo/tb4-sim/config/devices.json` does. Nav2 behaviours (`spin`, `backup`, `drive_on_heading`,
`assisted_teleop`) are routines: one a person or an undeclared client starts (`ros2 action
send_goal /drive_on_heading ...` from a laptop) is `shared`; the commands it relays are its own.
The collision monitor relays each `/cmd_vel_smoothed` message once, so one relayed command counts
per smoothed message: further commands that match it are another publisher's, and a person
driving in Nav2's direction makes the scope `shared`, never `ai`.

**Nav2 counts only as itself.** A task action's status and feedback count only when every
publisher of the action's status topic in the ROS graph is a declared AI node and no name is
shared, so a goal status anyone else publishes opens no task. During a task, Nav2 leaving the
graph, or no status or feedback from it for more than `gap_tolerance_s` (2 s), makes `base`
`unknown` until it is heard again. The observer reads feedback with the `nav2_msgs` action types;
without that package an open task becomes `unknown` 2 s after its last status.

**Who sent it.** On ROS 2 Jazzy a subscription's message info has no publisher GID (rclpy adds it
only in later releases, and the observer uses it when present). The observer therefore maps
publishers from the ROS graph, refreshed when a topic's publisher count changes and every second,
and records the map once per change as `x_publishers` on a heartbeat (a node name that is not a
valid ROS 2 name of at most 255 characters appears there as `pseudonym:<12 hex>`, the first 12 hex
digits of the SHA-256 of the name). A map too large for one heartbeat (many publishers on many
topics) is sent in parts of at most 48 KiB, each with `x_publishers_part: [i, n]`; joining the
parts in order, a topic's list continuing where the previous part left it, gives the map back. It
is never dropped; an entry too large for any part, which bounded node names rule out, would be
counted in `x_publishers_dropped`. A topic with one publisher
is attributed to it. With several, a `/cmd_vel` message that relays the latest `/cmd_vel_smoothed`
(equal, or scaled by the collision monitor) is Nav2's and any other the remaining publisher's.
Since a relay and its `/cmd_vel_smoothed` can arrive in either order, a command that relays
nothing yet waits up to 0.25 s for a match before it counts. Such states carry
`x_attribution_inferred: true` and cap the session at trust level L1 (6.12, 10.6). A goal or
cancel is attributed to the action's clients in the graph, also inferred. Button presses on
`/interface_buttons` and `/hmi/buttons` are matched in the device registry as topics with the
field appended: `/interface_buttons/button_2`, `/hmi/buttons/1` to `/hmi/buttons/4`.

**Names are not identities.** A ROS node name is whatever its launcher says: a teleop started as
`collision_monitor` looks like Nav2's relay. When two publishers on a command topic (`/cmd_vel`,
`/cmd_vel_unstamped`, `cmd_vel_nav`, `cmd_vel_smoothed`) share a node name, that name matches no
device: their commands count as an undeclared person's (`human_remote`, inferred). One impostor
that replaces the real node cannot be told from it by name: authenticated identity needs SROS2
(DDS security with per-node certificates and access control), which is planned for the hardware
step, together with checking the publishers' enclaves.

**Run it** on the robot's computer (or the PC for the simulator), with the recorder running and
`autonometry` installed in a virtual environment made with `--system-site-packages`, so that it
sees `rclpy` from ROS:

```
python3 -m colcon build --base-paths connectors/ros2 && source install/setup.bash
ros2 launch autonometry_ros observer.launch.py namespace:=/robot1 \
    recorder_socket:=/run/autonometry/recorder.sock device_registry:=/etc/autonometry/devices.json
```

Parameters are in `config/observer.params.yaml` (topic names, node names of Nav2's relay, the
TurtleBot 4 node and the joystick teleop, `hint_policy`, which must match the recorder's, the task
definition of each Nav2 task action, the plan item of a pose estimate). Spec parameters come from
`rules.toml`. Example registries and a recorder config for the simulator are in
`demo/tb4-sim/config/`. CI builds the package in a `ros:jazzy` container, plays fake TurtleBot 4
nodes against the recorder and the observer, and checks the sealed log
(`connectors/ros2/ci/run_e2e.sh`).

**Deferred** (not observed yet): Create 3 reflexes and hazards (bump, cliff, wheel drop, stall,
backup limit); safety settings changed on `/parameter_events`; the HMI display, LEDs and light
ring; battery and power-off shutdown (`session_end` causes); the Create 3's own movement actions
(`drive_distance`, `rotate_angle`, `navigate_to_position`, `drive_arc`); speed limits and the
collision monitor's slowdown; behaviour-tree recovery counts; authenticated publisher identity
(SROS2).
