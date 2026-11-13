# TurtleBot 4 golden session

The integration notes' worked example (`research/turtlebot4/tb4_example_events.jsonl`, notes
section B.5) rebuilt as what the observer sees on a TurtleBot 4, then run through the engine
(`autonometry.connectors.tb4`), the real recorder with a fake clock, and `autonometry timeline`
(`tests/test_connector_tb4_golden.py`).

- `observations.jsonl`: one observation per line, `kind` naming the class of
  `autonometry.connectors.tb4` and `t` the receive time in seconds from 2026-11-03T10:00:00Z, when
  the recorder starts. The simulator runs from t = −31 s, so the engine has seen the docked robot
  for 30 s when the session opens. `/cmd_vel` has two publishers in the ROS graph, Nav2's collision
  monitor and the keyboard, so every command is attributed by the topic-level rule (a command that
  relays `/cmd_vel_smoothed` is Nav2's, any other the keyboard's), as on ROS 2 Jazzy, whose
  subscriptions do not see the publisher.
- The two `ChainPublishers` lines at t = −31 s give the publishers of Nav2's chain topics
  (`controller_server` and `behavior_server` on `cmd_vel_nav`, `velocity_smoother` on
  `cmd_vel_smoothed`), all declared AI. They were added after review, when a relayed command
  became the AI's only on a declared chain: without them every relayed command would count as a
  person's. Nothing in `expected.json` changed: this session's chain is clean, and nothing in it
  comes from an undeclared device.
- `expected.json`: the events the log must hold (heartbeats left out) and the timeline facts,
  worked out by hand under spec v0.3 before the code ran.

## The session

| t (s) | Observed | Log |
|---|---|---|
| 0 | docked, no task, no command for 30 s | `idle` |
| 12.45 | Create 3 button 2, `Undock`, undock action executing | `shared`, `human_initiated`, `create3_button_2` (6.13) |
| 19 | undock succeeded | nothing: the person stays the last controller (6.14) |
| 25 | 2D Pose Estimate from RViz | `human_remote`, `planned`, `initial_pose` |
| 31 | Nav2 goal executing | `task_start`, then `ai` |
| 52.6–54.1 | keyboard from 52.3 while Nav2 drives | `shared`, `human_initiated`, `x_contention` (6.12) |
| 61 | collision monitor STOP | `safety_stop`, no actor (automatic) |
| 62.5 | keyboard from 62.2, Nav2 stopped | `human_remote`: takeover within 60 s, one intervention with the stop (6.7) |
| 70.4 | Create 3 power button, `EStop`, wheels disabled | `safety_stop`, actor `op_02`, `create3_button_power` |
| 70.45 | goal cancelling | `hint` (`x_kind: cancel`): the shared span is hidden by the stop, so it is sent as an event |
| 70.5 | goal cancelled | `task_end`, `aborted` |
| 80 | wheels enabled again, no commander | `unknown` (notes UC-7) |
| 100.5 | 30 s since the last command (the task's end) | `idle` |
| 130 | recorder stops | `session_end` |

A keyboard command is counted `smoothed_match_s` (0.25 s) after it arrives, at the next tick: with
several publishers on `/cmd_vel`, a command that relays nothing yet waits that long for the
`/cmd_vel_smoothed` it might relay, since the two can arrive in either order.

Hand-worked result: `ai` 28.5 s, `shared` 14.05 s, `human_remote` 13.9 s, `safety_stop` 11.1 s,
`idle` 41.95 s, `unknown` 20.5 s; 2 interventions (the contention, and the takeover joined to the
automatic stop); 2 safety stops (1 automatic and joined by the takeover, 1 triggered by a person);
the hint lies inside the takeover's episode; task t1 not autonomous, outcome `aborted`, 9.4 s of
human time inside it and 12.55 s of unplanned human time before it (the undock); planned resets
6.0 s; 9.0 m travelled.

## Differences from the notes' v0.2 expectation

The notes expected 2 safety stops, one followed by a takeover; 3 interventions; task t1 not
autonomous, outcome aborted. Under v0.3:

1. **2 interventions, not 3.** The cancel hint at 70.45 s falls inside the takeover's intervention
   episode (it runs from the automatic stop at 61 s until the task ends), so it belongs to that
   episode and is not counted again (definitions.md 3, v0.3 change 15). The EStop at 70.4 s is
   not an intervention either: the robot was already under human control (6.7, case c needs `ai`
   or `scripted` before it).
2. **The EStop names its person.** v0.3 sets `actor_id` on a `safety_stop` a person triggered
   (op_02, the Create 3 power button), so it counts in `safety_stops_human_triggered` (6.7).
3. **The undock span lasts until the pose estimate (12.45–25 s), not until the undock ended.**
   Outside an open task a scope nobody commands keeps its last controller's state until it is idle
   (6.14 as amended); v0.2 wrote `idle` the moment the undock ended. It is unplanned human time
   between tasks, so it is attributed to t1 (6.3), which was not autonomous anyway.
4. **After the EStop is released (80 s) the base is `unknown` until idle (100.5 s)**, where v0.2
   kept `safety_stop` until idle: the notes' UC-7 rule, since a stop is not a controller.
5. **No `supervision` event and no recovery heartbeat.** The TurtleBot 4 publishes no supervision
   signal, and behaviour-tree recovery counts are deferred.
6. **No `related_seq`.** A connector cannot know the recorder's `seq`; the timeline joins the
   takeover to the stop by `takeover_window_s` instead (6.7).
7. **Attribution is marked inferred.** The keyboard's two spans and the cancel carry
   `x_attribution_inferred: true` (6.12), which caps the session at trust level L1 (10.6).
8. **The publisher map is a heartbeat field**, `x_publishers`, sent by the observer when the ROS
   graph changes, since connectors cannot add fields to `session_start` (README, "TurtleBot 4").
