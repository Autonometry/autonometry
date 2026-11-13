# Autonometry — Definitions (taxonomy v0.3)

Status: draft, 2026-11-12 (v0.3; v0.2 was 2026-11-01; v0.1 was 2026-10-30 under the working name Autonomy Meter). Normative words (MUST, SHOULD, MAY) are used in their ordinary engineering sense.
This document defines the control-state taxonomy, the event log, and the edge-case rules that the
`autonometry` package, the event schema (`event.schema.json`) and the rule tables (`rules.toml`) implement.
Everything here is a definition we propose, not a claim about the world, except where a source is cited.
Citations are tagged VERIFIED (primary text read; '(excerpt)' when only the quoted portion was read), CLAIM (secondary source or abstract only), UNKNOWN (not verified).
Retrieval date for all URLs: 2026-10-30.

## Changes in v0.3 (2026-11-12)

Resolves the cases that the TurtleBot 4 and Unitree G1 research found uncovered by v0.2.

1. `safety_stop` also covers protective manoeuvres (brake, a damping mode used as a safe state, an evasive reflex) and records who triggered it (Section 2).
2. On `safety_stop`, `actor_id` names the person who triggered the stop and is null when a safety function acted on its own (Section 3).
3. Interventions also include a `safety_stop` a person triggered and a `scripted` preset a person started (Section 3).
4. An intervention episode ends when the scope returns to `ai`, or to `scripted` that no person started (Section 3).
5. A stop a person triggered from `ai` or `scripted` is one intervention; stops are reported as `safety_stops_human_triggered` and `safety_stops_automatic` (Section 6.7).
6. A limp, damping or zero-torque mode is `safety_stop` only when a safety function or an emergency-stop control caused it; a control with both uses is decided from how it was used (Section 6.8).
7. A pre-recorded reference executed by a controller, learned or not, is `scripted` (Section 6.9).
8. The device registry declares every source that can command the robot (`human`, `ai`, `script`, `support`), how the recorder matches it, and its hash; an undeclared source counts as a remote human (Section 6.10, `device_registry.schema.json`).
9. New rules for continuous goal input, several sources at once, presets started by a person, hand-overs and stale input, and physical support (Sections 6.11 to 6.15).
10. Event format: `session_start.simulated` (required) and `unobserved`; clock status on heartbeats with flag `clock_unsynced`; events `support_start` and `support_end` (Section 8).
11. Parameters `goal_release_s` and `contention_window_s`; events with `x_attribution_inferred: true` are capped at trust level L1 like reconstructed ones (Sections 9, 10.6).
12. Task registry and plan schemas (`task_registry.schema.json`, `plan.schema.json`); registries are read from the session folder and checked against their hashes; registry mapping of states, undeclared initial states, `unknown` after a seq gap, dwell placement, and any intervention makes a task not autonomous; flags `registry_mismatch`, `registry_missing`, `unknown_task_definition` and `unplanned` (Sections 1 to 3, 5, 6.1, 6.10, 8).
13. The silence limit is `heartbeat_interval_s + gap_tolerance_s` (default 3 s); the part of a gap beyond it is `unknown` (Section 2).
14. An unbroken run of `unknown` and `safety_stop` between AI control and human control does not hide an intervention (Section 3).
15. An approval or discrete hint inside an intervention episode belongs to it and is not counted again (Section 3).
16. Unplanned human time between tasks is attributed to the next task, which is then not autonomous (Sections 5, 6.3).
17. A wait for an approval longer than `approval_wait_max_s` logged as `ai` is repaired to `shared` and flagged `approval_wait_recoded` (Section 6.1).
18. A transition the registry turns into `human_local` or `human_remote` without a reason takes `human_initiated` (Section 6.10).
19. The `help_request` and plan-item checks apply to approvals and discrete hints too (Section 3).
20. A takeover joins an automatic stop when it goes into `human_local`, `human_remote` or `shared`, and the window ends with the task (Section 6.7).
21. In the any-human rule, `unknown` comes right after the human states: the whole robot is `unknown` when no scope is human and some scope is `unknown` (Section 6.6).
22. Bounds are reported for the time autonomy share, the task autonomy rate, the autonomous success rate, interventions per active hour and human minutes per task, as `unknown_as_human` (lower bound) and `unknown_as_ai` (upper bound); variants, breakdowns and per-kind rates get a point estimate and confidence interval only (Section 7).
23. Time share bounds: `unknown_as_human` = ai / (active + unknown), `unknown_as_ai` = (ai + unknown) / (active + unknown) (Section 7).
24. Task rate bounds: the point estimate covers closed tasks; `unknown_as_human` covers all tasks and counts an unclosed task or one containing unknown time as not autonomous; `unknown_as_ai` equals the point estimate (Section 7).
25. Interventions per active hour bounds add unknown time to active hours; `unknown_as_human` also counts one intervention for each whole-robot run of `unknown` and `safety_stop` that contains unknown time and has `ai` or `scripted` on both sides (Section 7).
26. Human minutes per task: `unknown_as_human` adds each task's whole-robot unknown time; `unknown_as_ai` equals the point estimate (Section 7).
27. The outer interval runs from the smallest to the largest confidence limit of the point estimate and the two bounds (Section 7).
28. Simulated and real sessions are never pooled: a report has one block per group present (Section 8).
29. Headline intervention and safety-stop rates use whole-robot counts; per-scope counts are reported beside them (Section 6.6).
30. Dwell windows that overlap count once: the AI time moved is the AI time inside the union of the windows (Section 6.1).
31. The supported share is supported active time over active time, so it is a proportion (Section 6.15).
32. Distance at an instant is the latest `distance_m` at or before it, and 0 before the first sample (Section 8).
33. New Section 11, the report: provenance, trust, session census, exclusions, metrics per group, the statements printed beside them, and invariants (Section 11).
34. Outside an open task, a scope that no source commands keeps its last controller's state until it is `idle`; `unknown` is for lost input, not for a robot seen standing still (Section 6.14).

## Changes in v0.2 (2026-10-31)

Resolves the cases that the SO-101 / LeRobot dossier found uncovered by v0.1, and three defects found when testing the reference package.

1. New state `scripted` for pre-recorded or pre-programmed motion (replay, teach-and-repeat, fixed macros): active, neither AI nor human (Sections 2, 6.9).
2. Holds and pauses take the state of whoever caused them and are never `ai` or `idle` while a task is open (Section 6.8). A declared `idle` inside an open task is converted to `unknown` and flagged `idle_in_task`.
3. One intervention lasts from leaving `ai` (or `scripted`) until control returns, so multi-step hand-overs such as `ai → shared → human_local → shared → ai` count once (Section 3).
4. Local versus remote comes from a declared device registry; human control through an undeclared device is `human_remote` (Section 6.10).
5. `ts` is the recorder's UTC observation time; events rebuilt after the fact from files are marked and capped at trust level L1 (Section 8).
6. Hashing is fixed: RFC 8785 canonical JSON and a domain-separated genesis hash (Section 8).
7. Schema: `scripted` added to the state enum; `device_id` and `device_registry_hash` added; `x_` extension fields now validate, as the standard already allowed.
8. Stricter log format: `ts` needs 3 to 9 fraction digits and rejects leap seconds; identifiers and scope names have a fixed safe character set and length; each line is I-JSON with integer fields written as integers; a repeated `session_start` or `session_end` and events outside the session are flagged; rejected lines are counted and listed up to `list_max_per_code` per code (Sections 1, 3, 8).
9. Sealing and verification (hash chain, signed Merkle checkpoints, RFC 3161 timestamp anchors) for trust levels L1 and L2 (Section 10).

## 1. Scope and units of observation

| Term | Definition |
|---|---|
| **Robot** | One physical system with a stable `robot_id`. |
| **Session** | A contiguous period of logging for one robot, with a stable `session_id`, opened by a `session_start` event and closed by a `session_end` event. The session is the primary clustering unit for uncertainty (interventions and task outcomes within a session are not independent). A session has one `session_start` and one `session_end`. A repeated one is flagged `repeated_session_marker`; events before `session_start` or after `session_end` are flagged `event_outside_session` and are not observed time (Section 2, rule 4). |
| **Control scope** | The part of the robot that a control state applies to. `whole_robot` by default; otherwise a named channel such as `left_arm`, `right_arm`, `base`, `head`, `gripper_L`. Scopes within one robot MUST be disjoint and are declared at `session_start` (`scopes` attribute). |
| **Task** | A declared unit of work. A task MUST be declared before the session as a `task_definition_id` in a pre-registered task list (the task registry is an input to the report, not something derived from the log). Each task instance has a `task_id`, a `task_start` and a `task_end` event; `task_end` carries `outcome` in {`success`, `failure`, `aborted`, `unknown`}. Events with `task_id = null` are between-task time. Re-labelling or splitting tasks after the fact changes the task autonomy rate, so the registry is frozen and its hash is written into `session_start.task_registry_hash`. The task registry is a JSON file valid against `task_registry.schema.json`; a repeated `task_definition_id` is an error, and its hash is the lower-case hex SHA-256 of its RFC 8785 canonical form, like the device registry's (Section 6.10). |
| **Actor** | A human or AI agent that holds control of a scope. `actor_id` is a pseudonymous identifier; automated controllers are named `ai:<controller_id>` and scripts `script:<script_id>`. |

## 2. Control states

A control state is a property of one control scope at one instant. States are **mutually exclusive per scope**: at any instant each scope is in exactly one state. The state of a scope changes only through a transition event. Between transitions the state is constant (piecewise-constant interpretation; no interpolation).

| State | Definition | Counts as |
|---|---|---|
| `ai` | The scope's actuators are commanded by an automated controller that chooses actions from current sensing (a learned policy, or a classical planner and controller such as a navigation stack) with no human command input in the control loop. A human may be watching (Section 6.4). | AI time; active |
| `human_local` | A human physically co-located with the robot commands the scope (kinesthetic guidance, local joystick, hand-held pendant, physical reset of objects). | Human time; active |
| `human_remote` | A human not co-located commands the scope over a link (teleoperation, remote joystick, VR, remote waypoint). | Human time; active |
| `shared` | Human and AI command inputs are both in the loop and blended or arbitrated (haptic shared control, human sets goals while AI executes, AI acts under an active human hint, AI blocked waiting for a human decision). The human fraction within `shared` is unknown by default. | Human time (conservative default); AI time under the `shared_as_ai` sensitivity; active |
| `scripted` | The scope follows a pre-recorded or pre-programmed sequence that does not use current sensing to choose its actions beyond limit and safety checks: replay of a recorded demonstration, teach-and-repeat, a fixed macro or choreographed routine. No live human command input (Section 6.9). | Neither AI nor human; **active**; reported as `scripted_share` |
| `safety_stop` | A safety function has taken the scope: motion is inhibited or a protective manoeuvre runs (E-stop, protective stop, geofence stop, watchdog, brake, a damping mode used as a safe state, an evasive reflex such as backing away from a bump or cliff), regardless of who triggered it. No actor commands the scope; when a person triggered the stop, `actor_id` names that person (Section 6.7). | Neither AI nor human; **active** (time the robot was supposed to be working) |
| `idle` | The scope is powered and logging but has no open task and no commanded motion for at least `idle_threshold_s` (default 30 s). Idle is a declared state, not inferred from zero velocity during a task: a robot waiting for a part during a task is `ai` or `human_*`, not idle. | Not active |
| `unknown` | The logger cannot assert the state: a gap in the log longer than `heartbeat_interval_s + gap_tolerance_s` (default 3 s), a gap in `seq`, a corrupt segment, or an explicit `unknown` transition emitted by a logger that lost its source of truth. | Unmeasured time; reported with bounds (Section 7) |

Rules:

1. Every session MUST start with a defined state for every declared scope (`session_start.initial_states`). If a scope has no initial state, it is `unknown` from `session_start` until its first transition.
2. A gap between consecutive events of more than `heartbeat_interval_s + gap_tolerance_s` while heartbeats are expected (`heartbeat_interval_s` declared at `session_start`, default 1 s) is converted to `unknown` by the loader for the portion of the gap beyond that limit. Any gap in `seq` makes the whole interval between the surrounding events `unknown`. After a gap in seq, every scope stays `unknown` until its next transition, because the lost events may have changed it.
3. `safety_stop` is active time and is excluded from both the AI and the human numerators. It is reported separately (`safety_stop_share`). Stopping and waiting instead of handing over therefore does not raise the time autonomy share: the denominator still grows and `safety_stops_per_active_hour` is a required field.
4. Time after `session_end` or before `session_start` is not observed and is not `unknown`. The report MUST state total observed session time.
5. `scripted` is active time and is excluded from both the AI and the human numerators, like `safety_stop`; it is reported separately (`scripted_share`). A controller that reacts to current sensing is `ai`, not `scripted`, even when it is deterministic.
## 3. Transitions and reasons

A transition event records that `scope` entered `state` at `ts`. It applies from `ts` until the next transition for the same scope.

| Field | Type | Rule |
|---|---|---|
| `seq` | integer >= 0 | Strictly increasing within a session, one sequence shared by all scopes. |
| `ts` | string | RFC 3339 date-time in UTC with the `Z` suffix and 3 to 9 fraction digits, e.g. `2026-10-30T09:15:02.250Z`. Second `60` (a leap second) is invalid; recorders use a clock that smears or steps leap seconds. `ts` MUST be non-decreasing with `seq`. |
| `state` | enum | One of the eight states. |
| `reason` | enum, nullable | `ai_requested_help`, `human_initiated`, `safety`, `planned`. Required when `state` is `human_local`, `human_remote`, `shared` or `safety_stop`; MAY be null for transitions into `ai`, `scripted`, `idle`, `unknown`. |
| `actor_id` | string, nullable | Who holds control after the transition. Required when `state` is `human_local`, `human_remote` or `shared`. For `safety_stop` it names the person who triggered the stop and is null when a safety function acted on its own. |

Reason definitions:

| Reason | Definition |
|---|---|
| `ai_requested_help` | The automated policy emitted a `help_request` (low confidence, explicit help call, timeout waiting for a decision) and a human responded. The transition MUST be preceded by a `help_request` on the same scope within `help_latency_max_s` (default 120 s); otherwise the loader re-codes the reason as `human_initiated` and records a flag. |
| `human_initiated` | A human took or was given control without an AI request (takeover, correction, demonstration, unplanned reset). |
| `safety` | A safety function, or a human acting for safety, stopped or took the scope. Transitions into `safety_stop` always carry `safety`. |
| `planned` | A pre-registered human action: a declared reset between tasks, a scheduled calibration, a planned demonstration block, a pre-registered approval gate. Planned transitions MUST reference a `plan_item_id`; the plan is frozen and its hash is in `session_start.plan_hash`. The plan is a JSON file valid against `plan.schema.json` (items of kind `reset`, `calibration`, `demonstration` or `approval_gate`); a repeated `plan_item_id` is an error, and its hash is the lower-case hex SHA-256 of its RFC 8785 canonical form. |

A transition with reason `planned` whose `plan_item_id` is not in the plan is recoded to `human_initiated` and flagged `unplanned`. The two checks above (a `help_request` on the same scope within `help_latency_max_s` for `ai_requested_help`; the `plan_item_id` in the plan for `planned`) apply to approvals and discrete hints too, with the same recoding and flags.

**Intervention.** An intervention is any of: (a) a transition of a scope out of `ai` or `scripted` into `human_local`, `human_remote` or `shared`, regardless of reason; (b) an `approval` or discrete `hint` event (Sections 6.1, 6.2); (c) a transition out of `ai` or `scripted` into `safety_stop` that a person triggered (Section 6.7); (d) a transition out of `ai` or `scripted` into `scripted` with reason `human_initiated`, a preset a person started (Section 6.13). For cases (a), (c) and (d), a scope also leaves `ai` or `scripted` when it was in one of them just before an unbroken run of `unknown` and `safety_stop` that leads up to the transition (`idle` while a task is open counts as `unknown`, Section 6.8). Putting one of these states between AI control and human control therefore does not hide an intervention. States are the counted states, after Section 6.10. A takeover that joins an automatic stop is still counted once, with the stop (Section 6.7). Interventions here are control hand-overs, not the interventions of machinery or AI regulation (such as tampering with safety functions under Regulation (EU) 2023/1230), which this standard does not measure.

**Intervention episode.** An intervention episode starts with the transition that makes it an intervention (cases a, c and d) and lasts until the scope returns to `ai`, or to `scripted` that no person started, the task ends, or the session ends. Every change between human and shared states inside that span (for example a pause-then-correct hand-over, `ai → shared → human_local → shared → ai`) belongs to the same intervention and is counted once, however long the steps are. An approval or discrete hint on a scope while that scope is inside an intervention episode belongs to that episode and is not counted again; its dwell is placed as in Section 6.1. `simultaneity_window_s` applies only to one human action taking several scopes (Section 6.6). Transitions into `safety_stop` are counted separately as `safety_stops` (Section 6.7). Between tasks (`task_id = null`), a transition into `human_*` from `idle` or `unknown`, or any transition into `human_*` with reason `planned`, is a reset (Section 6.3), not an intervention, but it is human time. A `planned` transition during an open task (e.g. a scheduled demonstration block) is an intervention with reason `planned`. This is deliberately broader than the California testing-regulation notion of a disengagement, which is a deactivation of the autonomous mode when a failure of the autonomous technology is detected or when safe operation requires the test driver (or, for driverless vehicles, the safety of vehicle, occupants or public) to deactivate the technology — Cal. Code Regs. tit. 13 § 227.50, https://www.law.cornell.edu/regulations/california/13-CCR-227.50 and the DMV adopted regulatory text https://www.dmv.ca.gov/portal/uploads/2020/06/Adopted-Regulatory-Text-2019.pdf [VERIFIED (excerpt): regulatory text excerpt read via web search 2026-10-30; full section not fetched]. The `reason` field lets a report recover the narrower count (`human_initiated` + `safety`, excluding `planned` and `ai_requested_help`) as a filter; the broad count is the default because it cannot be shrunk by relabelling.

## 4. Active time

```
observed_time = session_end.ts - session_start.ts            (per session, per scope)
active_time   = observed_time - idle_time - unknown_time
             = time in {ai, human_local, human_remote, shared, scripted, safety_stop}
```

Idle is excluded only when declared idle under Section 2. The report MUST print `idle_time`, `idle_threshold_s` and `idle_time / observed_time`, so that padding the log with idle time is visible even though idle is outside the denominator.

## 5. Task

A task is **autonomous** (counts in the task autonomy rate numerator) if, between its `task_start` and `task_end`, no scope spends time in `human_local`, `human_remote` or `shared` and no intervention (Section 3, cases a to d) occurs, and no unplanned human time since the previous task (or the session start) is attributed to it (Section 6.3). `safety_stop` intervals do not by themselves disqualify a task. Two rates are reported and MUST NOT be conflated: `task_autonomy_rate` (no human help, any outcome) and `autonomous_success_rate` (no human help AND `outcome = success`).

A task with `task_start` but no `task_end` before `session_end` has `outcome = unknown`; it is counted as not autonomous in the lower bound and excluded in the upper bound (Section 7). Tasks whose `task_definition_id` is not in the registry are flagged `unknown_task_definition` and excluded from task-level metrics (their time still counts in time-level metrics).

## 6. Edge cases

### 6.1 Remote approval clicks

An approval is a discrete human decision that lets the AI proceed (confirm a grasp, accept a route, acknowledge a warning). It is logged as an `approval` event (not a transition) with `scope`, `actor_id`, `reason` in {`ai_requested_help`, `planned`} and optional `dwell_s` (measured decision time).

Rules: an approval **counts as one intervention** and disqualifies the task from the autonomous numerator. It contributes `max(dwell_s, approval_min_dwell_s)` seconds to human time; `approval_min_dwell_s` defaults to 0 s. Because the default is zero, the time share can hide approval gates, so the report MUST print both `time_autonomy_share` (default) and `time_autonomy_share_with_approvals` (`approval_min_dwell_s` = measured median dwell, or 5 s if unmeasured), together with `approvals_per_active_hour`. The dwell is placed on [ts − dwell, ts) of its scope; only the part that overlaps `ai` time (whole-robot `ai` time for the whole robot) moves from AI to human time. Dwell windows that overlap count once: the AI time moved is the AI time inside the union of the windows. If the AI waits for the approval longer than `approval_wait_max_s` (default 10 s), the wait MUST be logged as `shared` with reason `ai_requested_help` (the AI is blocked on a human). If it is not, the loader repairs it: when an approval with reason `ai_requested_help` (after the checks of Section 3) comes more than `approval_wait_max_s` after the latest `help_request` on its scope, and since that `help_request` its scope had no transition, seq gap, task start, task end or other approval, the scope's `ai` time in [`help_request.ts`, `approval.ts`) is counted as `shared` with reason `ai_requested_help`; when there is such time, the approval is flagged `approval_wait_recoded`. The approval stays the one intervention.

### 6.2 Hints

A hint is human information that changes AI behaviour without the human commanding actuators (text or language instruction, a clicked point, a corrected sub-goal). Two encodings are allowed; the one in use is declared in `session_start.hint_policy`:

- `hint_as_shared` (default): the scope enters `shared` (reason `human_initiated`, or `ai_requested_help` if preceded by a `help_request`) when the hint is issued and returns to `ai` when the hint is consumed or `hint_ttl_s` (default 60 s) expires. Human time grows by the hint-active duration.
- `hint_as_event`: a discrete `hint` event with optional `dwell_s`; counts as one intervention and contributes `max(dwell_s, hint_min_dwell_s)` to human time (default 0 s, same sensitivity reporting as approvals).

Either way a hint disqualifies the task from the autonomous numerator. The instruction that defines the task, issued before `task_start`, is the task definition, not a hint.

### 6.3 Resets between tasks

A reset is human activity that restores the scene between tasks (re-placing objects, untangling a cable, moving the robot to a start pose). It is logged as a transition to `human_local` or `human_remote` with `task_id = null` and reason `planned` (if in the pre-registered plan) or `human_initiated` (if not).

Rules: reset time is always human time and is always reported (`reset_human_time`, `reset_human_time / active_time`). It is included in `active_time` and `human_time` for the **session-level** time autonomy share. It is excluded from **task-level** metrics (human minutes per task, task autonomy rate) only when reason is `planned`; unplanned human time while no task is open (an unplanned reset, or an intervention between tasks) is attributed to the next task because it is work the AI could not do: it adds to that task's human minutes and the task is not autonomous. A robot that resets its own scene does so in state `ai` with `task_id = null`, which is AI active time.

### 6.4 Supervision without control

A human watching (locally or remotely) who issues no commands is **not** a control state; the scope stays `ai`. Supervision is a session attribute: `supervision` events carry `level` in {`none`, `remote_monitoring`, `local_monitoring`, `ready_to_takeover`} and `n_supervisors`. The loader computes `supervision_ratio` (fraction of active time with level above `none`) and `supervisor_hours_per_robot_hour`. Both are required report fields: "one remote operator per N robots" is the economic quantity of interest and it is not a control state.

### 6.5 Several humans at once

Each scope has at most one controlling `actor_id` at a time. Two humans on two scopes: both scopes are `human_*` and Section 6.6 applies. Two humans on one scope (one drives, one approves): the scope is `human_*` with the driver as `actor_id` and the approver generates `approval` events. Human **time** is scope-time-weighted, not per human: two humans for 10 s on one scope is 10 s of human time; `human_actor_seconds` (20 s) is reported separately as the labour measure, together with the number of distinct human `actor_id`s per session.

### 6.6 Per-body-part control (humanoids, multi-arm systems)

With K declared scopes, each scope has its own state timeline. Two aggregations are reported:

- **any-human rule (default, conservative):** the whole-robot state at t is `human` if any scope is in `human_local`, `human_remote` or `shared`; else `unknown` if any scope is `unknown` (a scope that cannot be asserted may be under human control, so it never counts as AI time); else `safety_stop` if any scope is in `safety_stop`; else `scripted` if any scope is `scripted`; else `ai` if any scope is `ai`; else `idle` if all scopes are `idle`; else `unknown`. When scopes are in different human states, the whole robot takes the first of `human_remote`, `human_local`, `shared` (conservative for the remote-operator question). The headline time autonomy share uses this timeline.
- **scope-weighted rule:** scope k has weight w_k (default 1/K; MAY be declared as actuator count or degrees of freedom in `session_start.scope_weights`). AI time = sum_k w_k * ai_time_k; active time = sum_k w_k * active_time_k. This is the "fraction of the body under AI control" and is always reported beside the any-human figure.

Intervention counts are per scope and summed; one human action that takes several scopes within `simultaneity_window_s` (default 0.5 s) counts once in the whole-robot count and once per scope in the per-scope count. Both are reported. The headline `interventions_per_active_hour` and `safety_stops_per_active_hour` use the whole-robot counts (one human action is one intervention); the per-scope counts are reported beside them with a point estimate and confidence interval.

### 6.7 Safety stops and takeovers

A `safety_stop` that a person triggered from `ai` or `scripted` is one intervention with reason `safety`: the person interrupted the robot, whether the control was meant as an emergency stop or a pause. An automatic `safety_stop` followed within `takeover_window_s` (default 60 s) by a transition into `human_local`, `human_remote` or `shared` on the same scope is also one intervention with reason `safety`. Either way a takeover within `takeover_window_s` belongs to the same intervention episode. The window also ends when the task ends. Every stop is counted in `safety_stops`, reported as `safety_stops_human_triggered` and `safety_stops_automatic`. An automatic `safety_stop` that returns to `ai` without a human is a safety stop only. `safety_stops_per_active_hour` is a required report field so that stopping instead of handing over is visible.

### 6.8 Holds and pauses

A hold is any period in which a scope keeps still or waits while a task is open: a pause key, a frozen pose, a wait for a decision, the alignment step of a hand-over. A hold takes the state of whoever caused it and is never `ai` or `idle` while a task is open.

| Cause of the hold | State | Reason |
|---|---|---|
| A human paused or froze the robot (pause key, operator freeze, hand-over alignment) | `shared` | `human_initiated` |
| The AI is blocked waiting for a human decision (Section 6.1) | `shared` | `ai_requested_help` |
| A safety function stopped motion, or a person used an emergency-stop control | `safety_stop` | `safety` |
| The AI chose to wait as part of its own plan (a part has not arrived) | `ai` | — |
| No task is open | `idle` after `idle_threshold_s` | — |
| The cause cannot be observed | `unknown` | — |

A limp, damping or zero-torque mode is `safety_stop` only when a safety function or an emergency-stop control caused it. Used as a routine step (start-up, staging, a pause key) it is a hold of whoever caused it. When one control serves both purposes (for example a key that pauses when tapped and stops when held), the recorder decides from how it was used and keeps the evidence in `x_` fields.

A declared `idle` while a task is open is converted to `unknown` by the loader and flagged `idle_in_task`. A pause followed by human control is one intervention episode (Section 3).

### 6.9 Scripted motion

Replay of recorded actions (for example LeRobot `lerobot-replay`), teach-and-repeat and fixed macros are `scripted`. Replaying a recorded human demonstration moves human work in time; it MUST NOT be reported as `ai`. Commands that match a stored action sequence, or motion that does not react to a scene perturbation, are evidence of `scripted` whatever the log says. A pre-recorded reference executed by a controller, learned or not (for example a motion-tracking policy following a recorded dance), is `scripted`: the recording decides what the robot does.

### 6.10 Device registry and location

Software often cannot tell which source sent a command, or whether a person is next to the robot. Every source that can command the robot is declared before the session in a device registry, a JSON file valid against `device_registry.schema.json`. Each device has a `device_id`; a `kind`: `human` (an input device a person uses), `ai` (an automated controller, including the robot's own built-in controllers), `script` (a fixed sequence) or `support` (a hoist or harness, Section 6.15); for `human`, a `location` (`local` or `remote`) and optionally the pseudonymous `actor_id` who uses it; and `match`, the identity under which the recorder observes the device (host, ROS node, topic or DDS participant). A source matches the first device whose given `match` fields all equal what the recorder observed. `session_start.device_registry_hash` is the lower-case hex SHA-256 of the registry's RFC 8785 canonical form. Transitions, approvals and hints carry the `device_id` of their source, and the state follows the declared kind and location. A transition from a `human` device takes `human_local` or `human_remote` by the declared location, unless it is `shared`; from an `ai` device it is `ai`; from a `script` device it is `scripted`. When the logged state differs, the registry wins and the loader flags `registry_mismatch`. A transition naming a `support` device is not control: it becomes `human_remote` and is flagged `registry_mismatch`. A transition that the registry turns into `human_local` or `human_remote` and that has no reason takes reason `human_initiated`. Transitions into `safety_stop`, `idle` or `unknown` are not checked against the registry. Control through an undeclared source is `human_remote` and is flagged `undeclared_device`; an AI controller left out of the registry therefore counts as a remote human. A scope whose initial state is `ai`, `scripted`, `human_local`, `human_remote` or `shared` has no named source: it counts as `human_remote` and is flagged `undeclared_device` until a transition with a declared `device_id`. Initial `safety_stop`, `idle` and `unknown` are not checked. Remote control declared as local is exposed at trust level L3 when it passes through the instrumented station.

### 6.11 Continuous goal input

A person who streams goals to an AI controller (joystick or thumbstick velocities into a balance or navigation controller, assisted teleoperation) makes the scope `shared` with reason `human_initiated`, from the first input outside the device's dead zone until `goal_release_s` (default 1 s) after the last such input; a command that carries its own duration lasts until that duration ends. A person whose commands reach the actuators without an AI controller in between (for example wheel velocities sent straight to a base) holds the scope: `human_local` or `human_remote`. A neutral command (zero velocity, centred sticks, a released dead-man key) ends a human or shared span at its `ts` and never opens one.

### 6.12 Several sources at once

When two or more sources command one scope within `contention_window_s` (default 1 s) of each other, interleaved on one channel, blended with weights or summed, the scope is `shared` with reason `human_initiated` if any of them is a person; otherwise `ai` if any is an AI controller; otherwise `scripted`. A span of contention is one intervention episode. Sources are told apart by the identity the recorder observes live (Section 6.10). When the recorder can only infer the source, for example from a recording that does not keep sender identity, the events carry `x_attribution_inferred: true`; the loader flags them `attribution_inferred` and spans built from them are trust level L1 at most.

### 6.13 Presets started by a person

A preset behaviour that a person starts (a posture macro, a gesture, docking, undocking, wall-following) is `scripted`, with actor `script:<name>` and reason `human_initiated`, when it is a fixed motion; it is `shared` with reason `human_initiated` when it is a closed-loop routine that senses as it runs, because the person chose the goal and the AI executes it (Section 2). Either way it is an intervention (Section 3). Started by a program, the same behaviours are `scripted` or `ai`. Between tasks the same states apply with `task_id = null` (Section 6.3).

### 6.14 Hand-overs and stale input

Inside an open task, while no source commands a scope between two controllers (for example a limp gap while one program releases the robot and another starts), the scope takes the state of the incoming controller if it starts within `gap_tolerance_s`, else it is `unknown`. Outside an open task, a scope that no source commands keeps the state of its last controller until it is `idle` (Section 2): `unknown` is for lost input, not for a robot seen standing still. A human state needs live input: when the declared input device stops reporting (tracking lost, link dropped) while its last command keeps being repeated, the scope is `unknown` from `gap_tolerance_s` after the last live input.

### 6.15 Physical support

A robot held up by a hoist, a harness or a person is physically supported. Support is not a control state: it is recorded with `support_start` and `support_end` events (optional `scope`, default the whole robot, and `device_id`), and an unclosed support lasts until `session_end`. Reports print `supported_time`, the supported share of active time (supported active time / active_time) and the time autonomy share over unsupported active time only. Moving the robot by hand (lifting, carrying, pushing) is `human_local`.

## 7. Unmeasured time and bounds

The time autonomy share, the task autonomy rate, the autonomous success rate, interventions per active hour and human minutes per task are reported as a point estimate plus two bounds:

- **lower bound (unknown as human)**, reported as `unknown_as_human`: all `unknown` intervals are counted as human time; tasks with unknown outcome or containing unknown gaps are counted as not autonomous.
- **upper bound (unknown as AI)**, reported as `unknown_as_ai`: all `unknown` intervals are counted as AI time; tasks containing unknown gaps are counted as autonomous if they otherwise qualify.

The point estimate excludes `unknown` from both numerator and denominator (`active_time` as defined in Section 4). Variants (such as `time_autonomy_share_with_approvals`), breakdowns and per-kind rates are reported with a point estimate and confidence interval only. With unknown time taken from the whole-robot timeline (Section 6.6), the bounds are:

| Metric | Point estimate | `unknown_as_human` | `unknown_as_ai` |
|---|---|---|---|
| time autonomy share | ai / active | ai / (active + unknown) | (ai + unknown) / (active + unknown) |
| task autonomy rate, autonomous success rate | closed tasks (Section 5), judged with unknown time ignored | all tasks; a task that was not closed, or that contains unknown time on any scope, is not autonomous | equals the point estimate (unclosed tasks are excluded, Section 5) |
| interventions per active hour | interventions / active hours | interventions plus one for each run of `unknown` and `safety_stop` on the whole-robot timeline that contains unknown time and has `ai` or `scripted` on both sides, over active + unknown hours | interventions / (active + unknown) hours |
| human minutes per task | mean over tasks of human time | adds each task's whole-robot unknown time | equals the point estimate |

The report MUST print `unknown_time / observed_time`; a report whose unknown share exceeds `unknown_share_max` (default 0.05) is marked `incomplete`. Confidence intervals are computed for the point estimate and for each bound; the reported outer interval runs from the smallest to the largest of their six confidence limits, which combines sampling and measurement uncertainty and also holds for metrics where more is worse.

## 8. Event log format

The log is JSON Lines (one JSON object per line) validated against `event.schema.json`, written in JSON Schema draft 2020-12 (metaschema https://json-schema.org/draft/2020-12/schema; specification https://json-schema.org/draft/2020-12/json-schema-core.html [VERIFIED (excerpt): specification index page read via web search 2026-10-30]). Timestamps use the RFC 3339 `date-time` profile of ISO 8601 (https://www.rfc-editor.org/info/rfc3339/ [VERIFIED (excerpt): RFC 3339 Section 5.6 grammar read via web search 2026-10-30]) and MUST use the `Z` offset.

| `type` | Required fields beyond the common ones | Purpose |
|---|---|---|
| `session_start` | `scopes`, `initial_states`, `heartbeat_interval_s`, `hint_policy`, `task_registry_hash`, `plan_hash`, `simulated`; optional `scope_weights`, `device_registry_hash`, `unobserved` | Opens a session; declares scopes and parameters. |
| `session_end` | — | Closes a session. |
| `transition` | `scope`, `state`; `reason`/`actor_id` per Section 3; optional `plan_item_id`, `related_seq`, `device_id` | State change. |
| `approval` | `scope`, `actor_id`, `reason`; optional `dwell_s`, `device_id` | Discrete human decision (6.1). |
| `hint` | `scope`, `actor_id`; optional `dwell_s`, `ttl_s`, `device_id` | Discrete hint under `hint_as_event` (6.2). |
| `help_request` | `scope` | AI asked for help. |
| `task_start` | `task_id`, `task_definition_id` | Opens a task. |
| `task_end` | `task_id`, `outcome` | Closes a task. |
| `supervision` | `level`, `n_supervisors` | Supervision level change (6.4). |
| `heartbeat` | optional `distance_m`, `clock_source`, `clock_max_error_s` | Liveness; cumulative odometry for distance-based metrics. |
| `support_start` | optional `scope`, `device_id` | Physical support begins (6.15). |
| `support_end` | optional `scope`, `device_id` | Physical support ends. |

Common fields on every event: `robot_id`, `session_id`, `task_id` (nullable), `seq`, `ts`, `type`; optional `distance_m` (cumulative metres since `session_start`, non-decreasing), `prev_hash`, `hash`. Distance at an instant is the latest `distance_m` at or before it, and 0 before the first sample. `hash` is the lower-case hex SHA-256 of the event serialised with the RFC 8785 JSON Canonicalization Scheme (https://www.rfc-editor.org/rfc/rfc8785), without its own `hash` field and including `prev_hash`. `prev_hash` is the previous event's `hash`; for the first event of a session it is the genesis hash SHA-256 of the UTF-8 string `autonometry/chain-v1|<robot_id>|<session_id>` (the prefix names the hashing scheme, not the spec version, so it changes only if the hashing itself changes). `prev_hash` may be null only in unsealed (trust level L1) logs; a verifier asked for L2 or above MUST fail when any event lacks `hash` or `prev_hash`. Section 10 specifies sealing and trust levels L1 and L2; L3 and L4 follow in later versions.

**Identifiers.** `robot_id`, `session_id`, `task_id`, `task_definition_id`, `actor_id`, `device_id` and `plan_item_id` are 1 to 128 ASCII letters, digits, `.`, `_`, `:` or `-`, starting with a letter or digit. Scope names are 1 to 64 ASCII letters, digits, `_`, `.` or `-`. Identifiers are therefore safe to print in terminals and reports.

**JSON.** Each line is one I-JSON object (RFC 7493): no duplicate keys, no lone surrogates or Unicode noncharacters, numbers within the range of an IEEE 754 double, integers within ±(2^53 − 1). Integer fields (`seq`, `related_seq`, `n_supervisors`) are written as JSON integers, without a fraction or exponent (`5`, not `5.0`).

**Time.** `ts` is the time the recorder observed the event, from a UTC clock synchronised by NTP or PTP; the recorder reports its clock status on heartbeats: `clock_source` (`ntp`, `ptp`, `gnss`, `none`, `unknown`) and `clock_max_error_s`, its estimated maximum error in seconds. The loader flags `clock_unsynced` for a session whose clock source is `none` or `unknown` or whose error exceeds `clock_tolerance_s` or is not reported. A source's own timestamp MAY be kept as `x_src_ts` with `x_src_clock` (`wall`, `monotonic`, `sim`, `frame_index`). Events rebuilt after the fact from files rather than observed live (for example from LeRobot dataset frames, whose `timestamp` is `frame_index / fps`) MUST carry `x_ts_reconstructed: true`; the loader counts them under the flag `ts_reconstructed`, and spans built from them are reported at trust level L1 at most.

**Simulation and blind spots.** `session_start.simulated` is true for a session run in a simulator; reports MUST NOT pool simulated and real sessions: a report has one block of metrics per group present (real, simulated). `session_start.unobserved` lists signal classes the recorder cannot see on the platform (`safety_stops`, `human_inputs`, `tasks`), for example safety events a robot does not publish by default; reports print them beside every metric that depends on them.

**Extensions.** Fields whose names start with `x_` are allowed on every event and ignored by the metrics. Any other field not defined here is a schema error.

**Registries.** The loader reads the device registry, task registry and plan from the session folder (devices.json, tasks.json, plan.json) or from files the user names; each must match its hash in session_start, and a file that does not match is an error. A missing device registry makes every source undeclared (Section 6.10) and is flagged `registry_missing`; a missing task registry flags every task `unknown_task_definition`; a missing plan recodes every `planned` reason as in Section 3.

**Rejected lines.** A line that cannot be read as one valid event is rejected with an error code from `rules.toml` `[errors]` and reported with its line number. A rejected line leaves a gap in `seq`, so the interval around it becomes `unknown` (Section 2, rule 2). Readers count every rejected line and MAY list only the first `list_max_per_code` (`rules.toml` `[errors]`) of each code.

## 9. Parameter table (defaults)

| Parameter | Default | Section |
|---|---|---|
| `idle_threshold_s` | 30 | 2 |
| `gap_tolerance_s` | 2 | 2 |
| `heartbeat_interval_s` | 1 | 2 |
| `help_latency_max_s` | 120 | 3 |
| `approval_min_dwell_s` | 0 (sensitivity: 5) | 6.1 |
| `approval_wait_max_s` | 10 | 6.1 |
| `hint_ttl_s` | 60 | 6.2 |
| `hint_min_dwell_s` | 0 | 6.2 |
| `takeover_window_s` | 60 | 6.7 |
| `simultaneity_window_s` | 0.5 | 6.6 |
| `goal_release_s` | 1 | 6.11 |
| `contention_window_s` | 1 | 6.12 |
| `unknown_share_max` | 0.05 | 7 |
| `checkpoint_every_events` | 1000 | 10.3 |
| `checkpoint_every_s` | 600 | 10.3 |
| `max_commit_delay_s` | 3600 | 10.5 |
| `clock_tolerance_s` | 1 | 8, 10.5 |

All parameters MUST be printed in the report; changing a default is allowed but is itself a reported fact.

## 10. Sealing and verification

**10.1 Sealed session.** A session is sealed when every event has `hash` and `prev_hash` (Section 8) and `seq` rises by exactly 1 from each event to the next. Event *i* is the *i*-th event in `seq` order, counting from 0.

**10.2 Merkle tree.** The events of a session are, in order, the leaves of the Merkle tree of RFC 9162 Section 2.1.1 (identical to RFC 6962 Section 2.1). A leaf's data is the 32 bytes of the event's `hash`: leaf hash = SHA-256(0x00 || hash); interior node = SHA-256(0x01 || left || right).

**10.3 Checkpoints.** A checkpoint commits to the first `size` events of a session. It is a C2SP checkpoint (https://c2sp.org/tlog-checkpoint) in signed-note format (https://c2sp.org/signed-note): three lines of note text (the origin `autonometry/<robot_id>/<session_id>`, the tree size in ASCII decimal, the base64 root hash) and no extension lines, then a blank line and one or more signature lines. The recorder signs with an Ed25519 key (signature type 0x01). The key name is fixed when the key is made (1 to 256 ASCII letters, digits, `.`, `_`, `:`, `/` or `-`, for example `autonometry/<robot_id>`) and need not equal the origin, so one key serves every session of a robot. Auditors obtain the verifier key (vkey) outside the log. The recorder writes a checkpoint after the first event of a session, after every further `checkpoint_every_events` events, after any event whose `ts` is at least `checkpoint_every_s` later than that of the last event of the previous checkpoint, and after the last event.

**10.4 Checkpoint file.** The checkpoints of a log `<name>.jsonl` are appended in order to `<name>.checkpoints.jsonl`, one record per line, under the line rules of Section 8 and `checkpoint.schema.json`: `{"type": "checkpoint", "note": "<signed note>"}` and, later, `{"type": "timestamp", "note_sha256": "<hex SHA-256 of the signed note's UTF-8 bytes>", "tsr": "<base64 of the DER RFC 3161 TimeStampResp>"}`. An anchor file has the same format.

**10.5 Anchors.** A checkpoint is anchored when either (a) a timestamp record for it holds a granted RFC 3161 response for the signed note that verifies against a TSA root certificate the verifier trusts, with a time at most `max_commit_delay_s` after and at most `clock_tolerance_s` before the `ts` of the last event the checkpoint covers; or (b) the verifier holds the same checkpoint (origin, size, root, valid signature) in an anchor file: a copy kept outside the log, such as the customer's or a witness's.

**10.6 Verification.** A verifier recomputes every event hash from the event and every root from the events; it never trusts a stored value. Anchors are checked against the events exactly like checkpoints, and a valid anchor counts as an anchored checkpoint. A span of events is trust level L2 when every event in it is sealed and it is covered by a checkpoint with a valid signature from a trusted key, a matching root and an anchor. Everything else is L1, and so are sessions holding events with `x_ts_reconstructed: true` or `x_attribution_inferred: true` (Section 8). Each session gets one status:
- `verified`: L2 up to the last event;
- `partial`: L2 up to `sealed_through_seq`, L1 after it;
- `unverified`: no L2 span, and nothing contradicts the log;
- `failed`: the log contradicts its own seals (tampering or corruption), and no span is L2.

Evidence codes; any one makes the status `failed`:
- `hash_mismatch`: a stored `hash` differs from the recomputed one.
- `chain_break`: in a session with any sealed event, an event is not sealed, or a `prev_hash` or `seq` does not follow.
- `bad_signature`: a signature line with a trusted key's name and key ID does not verify.
- `root_mismatch`: a checkpoint or anchor root differs from the events.
- `missing_events`: a checkpoint or anchor covers more events than the log holds (including a session absent from the log).
- `bad_timestamp_token`: a timestamp record for one of the session's checkpoints whose token cannot be decoded, or whose message imprint is not the SHA-256 given in its `note_sha256`.

Gap codes; they never raise the level:
- `unsealed`: no event is sealed.
- `no_checkpoint`: events after the last valid checkpoint.
- `untrusted_checkpoint`: no signature from a trusted key; the note is ignored.
- `not_anchored`: a valid checkpoint without an anchor, including when its token does not verify against the trusted TSA roots or no TSA root is trusted.
- `late_anchor`, `clock_skew`: a verified token outside the window of 10.5.
- `reconstructed`: events carry `x_ts_reconstructed: true` or `x_attribution_inferred: true`.

**File problems.** A record that cannot be tied to one session is reported for its file: `malformed_checkpoint` (a checkpoint note that is not a well-formed signed checkpoint) and `orphan_timestamp` (a timestamp record whose `note_sha256` matches no checkpoint record in the same file). File problems, like rejected lines, make the result not verified whatever the sessions' statuses.

A verifier MUST NOT report L2 for a span it did not prove, and MUST report a log with rejected lines as not verified.

## 11. Report

A report turns one or more logs into a document a buyer, insurer or auditor can read: what was measured, how far to trust it, and the numbers with their uncertainty. It adds no measurement rule: it combines verification (Section 10), the timeline (Sections 1 to 8) and the metrics with their bounds (Section 7). A report is produced as JSON and MAY also be rendered for reading; both carry the same content. The same inputs and options MUST give the same report apart from `generated_at`: every resampling is seeded and every list and map is in a fixed order. A report contains no actor ids, only counts (Section 1, Actor), and no event contents beyond identifiers (Section 8).

**11.1 Provenance.** The spec version; the software name and version; `generated_at` (RFC 3339 UTC); the robots; the period, from the first to the last observed instant; the counts of sessions, robots, UTC calendar days with observed time, and tasks; the registry hashes the sessions declare (`device_registry_hash`, `task_registry_hash`, `plan_hash`); the SHA-256 of every input file (logs, checkpoint and anchor files, registries, trusted keys, trusted TSA roots and any other file read); and every parameter of Section 9 and the statistics options (confidence level, resampling count and seed, cluster choice, drift bin).

**11.2 Trust.** Each session's status (Section 10.6) and its level: L2 only for a `verified` session, else L1; with its evidence and gap codes. The rejected lines and file problems of every file read. The report's level is the lowest of its sessions'. The report is `verified` only when every session is `verified` and there are no rejected lines and no file problems; it is `failed` when any session is `failed`, and the report MUST then say `failed` at the top. A failed session stays in the metrics: leaving it out would reward corrupting a bad session. The report MUST say so when no trusted key or no trusted TSA root was given. L3 is shown as not available until a later version specifies it.

**11.3 Session census.** For the session ids of the recorder's form, `<counter>-<YYYYMMDD>T<hhmmss>Z-<4 lower-case hex digits>` with a counter of 6 to 16 digits, the report lists for each robot the counters missing between its lowest and its highest present counter. Sessions with ids of any other form are listed as not checked.

**11.4 Exclusions.** Every session left out of the metrics, with the reason: a registry that does not match its hash (Section 8, Registries), no `session_start` (no observed time and no group), or a session named by a checkpoint or anchor but absent from the logs.

**11.5 Metrics.** The metrics of Section 7 with their bounds and intervals, one block per group present (real, simulated), never pooled (Section 8).

**11.6 Statements.** Printed beside the metrics they concern, for each group:
1. When unknown time exists, interventions per active hour and human minutes per task have no true worst case, since events can hide in unknown time; `unknown_as_human` assumes one hidden intervention per run of unknown time (Section 7).
2. For a metric with bounds whose bounds are wider than the confidence interval of its point estimate: data quality, not sample size, limits this number.
3. An interval from fewer than `min_clusters` clusters (default 2, that is a single cluster) is not generalisable.
4. A rate from fewer than `rare_rate_events` events (default 5) also shows its one-sided 95 % upper bound (exact Poisson, Garwood).

Both thresholds are in `rules.toml` `[report]` and are printed with the parameters of Section 9.
5. The signal classes sessions declare `unobserved`, with the metrics that depend on them (Section 8).

**11.7 Invariants.** Each with its result, `pass` or `fail`, or `not_checked` when the inputs do not allow the check, with what failed:
- partition: in every session, the state times sum to the observed time, per scope and for the whole robot;
- chain: no session contradicts its seals (Section 10.6); `not_checked` when a session has no sealed event;
- time: no `ts_regression` flag (Section 8) and no `clock_skew` or `late_anchor` gap (Section 10.6);
- census: no counter missing (11.3); `not_checked` when some session id has another form;
- tasks: in every session, tasks started = tasks closed + tasks not closed, and every task definition is in the task registry;
- bounds: `unknown_as_human` ≤ point estimate ≤ `unknown_as_ai` for the time autonomy share, the task autonomy rate and the autonomous success rate;
- witnesses: not available until trust level L3 is specified.

A report is `complete` when every group present is complete (Section 7) and `incomplete` otherwise, including when no group is present. A report is clean only when it is `verified` at L2, `complete`, has no census gap, leaves no session out and no invariant is `fail`; it then says so at the top, and otherwise lists why not.
