# Station witness (trust level L3)

A sealed log proves nothing was changed afterwards; it cannot prove the recorder told the truth
when it wrote. Trust level L3 (definitions.md 12) adds a second, independent witness: the
teleoperation station keeps its own sealed log of when its operator really held control and gave
input, and `autonometry reconcile` compares it with the robot's.

**The station's log.** The station runs its own `autonometry record` under its own key, with
`role = "station"`, a `station_id` and, as `robot_id`, the robot it can command (one recorder per
robot). Its scopes are the robot scopes it witnesses, each `human_remote` while the operator's
input is live (outside the dead zone within the last `goal_release_s`, 1 s; a neutral command
ends it at once), `unknown` when the station loses its input device, `idle` otherwise. Its device
registry declares only the station's own inputs. Two ways to feed it:

- `clients/python/station_example.py`: a few lines around the recorder client for any teleop
  stack (joystick axes, a VR controller); copy it beside `autonometry_client.py`.
- For the TurtleBot 4, the ROS 2 node `station_observer` (`ros2 launch autonometry_ros
  station.launch.py recorder_socket:=... device_registry:=...`) on the operator's PC: it watches
  `/cmd_vel` from `teleop_twist_keyboard` (or `teleop_twist_joy`) on that PC and `/joy` from its
  joystick, and only subscribes, like the robot's observer. `/joy` counts only when every
  publisher of the topic is a `human` device of the station's registry (a witness that guesses
  cannot vouch): anyone else's joystick message is ignored, and if the station's own joystick
  then cannot be told apart, its input counts as lost (`unknown`). Configs:
  `demo/tb4-sim/config/station-recorder.toml` and `station-devices.json`.

**Reconcile.**

```
autonometry reconcile --robot robot/events.jsonl --station station/events.jsonl \
    --vkey robot.vkey --vkey station.vkey --anchor customer-anchors.jsonl --out witness.json
```

Both sides are verified with the same trust roots, in one pass (one anchor file may hold both).
A station session witnesses the robot sessions of its robot that it overlaps in time. Per robot
scope, every instant of the base (the robot's active time, plus its idle time while the station
shows input) is one of: *both-human*; *robot-only* (robot `human_remote` or `shared`, station
not); *station-only* (station input, robot not: this is the hidden remote operator, and also
remote control the robot logged as `human_local`); *neither*; or *unwitnessed* (no station
session open, the station's input lost, or the scope not witnessed). A disagreement within tau of
the other side's human time is *tolerated*: tau = robot clock error + station clock error (each
side's `clock_max_error_s` on the heartbeat nearest the edge) + `react_s` (0.5 s). After the
operator stops, a robot that keeps the last controller's state (6.14) is *held*, not disputed,
when the station saw that run. Station input while the robot logs `unknown` is reported on its
own line (U). A session is L3 when both logs are verified, neither clock is unsynced, and
(station-only + unwitnessed + U) / (base + U) ≤ 0.5 % and (robot-only + station-only +
unwitnessed + U) / (base + U) ≤ 2 %: unwitnessed time counts as disputed, since a station that
cannot see cannot vouch, and a robot that logs `unknown` while the station sees an operator does
not show that person. Station episodes the robot never logged raise an upper-bound intervention count.
The JSON lists, per robot session, the times, shares, tau, unmatched episodes and runs, and the
reasons a session is not L3. Exit status: 0 only when every robot session is L3; 1 otherwise; 2 on
bad usage or unreadable input. `autonometry report ... --station LOG` puts the same in the
report.

**What L3 cannot show.** An operator using a client the station does not witness (another
station, a laptop, a direct radio link) is invisible to it: only a witness of the control channel
itself, the network witness of L4, can expose that. The two keys must belong to separate software
identities, and the station logger should be one the customer or auditor installs.

**On ROS 2 Jazzy**, a subscriber cannot see which publisher sent a message, so when Nav2's relay
and a keyboard both publish `/cmd_vel`, both observers infer the source and their logs are L1
(6.12): no L3. A run reaches L3 when each commander is the only publisher while it drives (CI:
`connectors/ros2/ci/run_witness.sh`, an honest run with a station that must reach L3), or with an
rclpy that reports `publisher_gid`.
