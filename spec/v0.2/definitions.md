# Autonometry — Definitions (taxonomy v0.2)

Status: draft, 2026-11-01 (v0.2; v0.1 was 2026-10-30 under the working name Autonomy Meter). Normative words (MUST, SHOULD, MAY) are used in their ordinary engineering sense.
This document defines the control-state taxonomy, the event log, and the edge-case rules that the
`autonometry` package, the event schema (`event.schema.json`) and the rule tables (`rules.toml`) implement.
Everything here is a definition we propose, not a claim about the world, except where a source is cited.
Citations are tagged VERIFIED (primary text read; '(excerpt)' when only the quoted portion was read), CLAIM (secondary source or abstract only), UNKNOWN (not verified).
Retrieval date for all URLs: 2026-10-30.

## Changes in v0.2 (2026-10-31)

Resolves the cases that the SO-101 / LeRobot dossier found uncovered by v0.1, and three defects found when testing the reference package.

1. New state `scripted` for pre-recorded or pre-programmed motion (replay, teach-and-repeat, fixed macros): active, neither AI nor human (Sections 2, 6.9).
2. Holds and pauses take the state of whoever caused them and are never `ai` or `idle` while a task is open (Section 6.8). A declared `idle` inside an open task is converted to `unknown` and flagged `idle_in_task`.
3. One intervention lasts from leaving `ai` (or `scripted`) until control returns, so multi-step hand-overs such as `ai → shared → human_local → shared → ai` count once (Section 3).
4. Local versus remote comes from a declared device registry; human control through an undeclared device is `human_remote` (Section 6.10).
5. `ts` is the recorder's UTC observation time; events rebuilt after the fact from files are marked and capped at trust level L1 (Section 8).
6. Hashing is fixed: RFC 8785 canonical JSON and a domain-separated genesis hash (Section 8).
7. Schema: `scripted` added to the state enum; `device_id` and `device_registry_hash` added; `x_` extension fields now validate, as the standard already allowed.

## 1. Scope and units of observation

| Term | Definition |
|---|---|
| **Robot** | One physical system with a stable `robot_id`. |
| **Session** | A contiguous period of logging for one robot, with a stable `session_id`, opened by a `session_start` event and closed by a `session_end` event. The session is the primary clustering unit for uncertainty (interventions and task outcomes within a session are not independent). |
| **Control scope** | The part of the robot that a control state applies to. `whole_robot` by default; otherwise a named channel such as `left_arm`, `right_arm`, `base`, `head`, `gripper_L`. Scopes within one robot MUST be disjoint and are declared at `session_start` (`scopes` attribute). |
| **Task** | A declared unit of work. A task MUST be declared before the session as a `task_definition_id` in a pre-registered task list (the task registry is an input to the report, not something derived from the log). Each task instance has a `task_id`, a `task_start` and a `task_end` event; `task_end` carries `outcome` in {`success`, `failure`, `aborted`, `unknown`}. Events with `task_id = null` are between-task time. Re-labelling or splitting tasks after the fact changes the task autonomy rate, so the registry is frozen and its hash is written into `session_start.task_registry_hash`. |
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
| `safety_stop` | Motion is inhibited by a safety function (E-stop, protective stop, geofence stop, watchdog), regardless of who triggered it. No actor commands the scope. | Neither AI nor human; **active** (time the robot was supposed to be working) |
| `idle` | The scope is powered and logging but has no open task and no commanded motion for at least `idle_threshold_s` (default 30 s). Idle is a declared state, not inferred from zero velocity during a task: a robot waiting for a part during a task is `ai` or `human_*`, not idle. | Not active |
| `unknown` | The logger cannot assert the state: a gap in the log longer than `gap_tolerance_s` (default 2 s), a gap in `seq`, a corrupt segment, or an explicit `unknown` transition emitted by a logger that lost its source of truth. | Unmeasured time; reported with bounds (Section 7) |

Rules:

1. Every session MUST start with a defined state for every declared scope (`session_start.initial_states`). If a scope has no initial state, it is `unknown` from `session_start` until its first transition.
2. A gap between consecutive events of more than `gap_tolerance_s` while heartbeats are expected (`heartbeat_interval_s` declared at `session_start`, default 1 s) is converted to `unknown` by the loader for the portion of the gap beyond the tolerance. Any gap in `seq` makes the whole interval between the surrounding events `unknown`.
3. `safety_stop` is active time and is excluded from both the AI and the human numerators. It is reported separately (`safety_stop_share`). Stopping and waiting instead of handing over therefore does not raise the time autonomy share: the denominator still grows and `safety_stops_per_active_hour` is a required field.
4. Time after `session_end` or before `session_start` is not observed and is not `unknown`. The report MUST state total observed session time.
5. `scripted` is active time and is excluded from both the AI and the human numerators, like `safety_stop`; it is reported separately (`scripted_share`). A controller that reacts to current sensing is `ai`, not `scripted`, even when it is deterministic.
## 3. Transitions and reasons

A transition event records that `scope` entered `state` at `ts`. It applies from `ts` until the next transition for the same scope.

| Field | Type | Rule |
|---|---|---|
| `seq` | integer >= 0 | Strictly increasing within a session, one sequence shared by all scopes. |
| `ts` | string | RFC 3339 / ISO 8601 date-time in UTC with the `Z` suffix and at least millisecond precision, e.g. `2026-10-30T09:15:02.250Z`. `ts` MUST be non-decreasing with `seq`. |
| `state` | enum | One of the eight states. |
| `reason` | enum, nullable | `ai_requested_help`, `human_initiated`, `safety`, `planned`. Required when `state` is `human_local`, `human_remote`, `shared` or `safety_stop`; MAY be null for transitions into `ai`, `scripted`, `idle`, `unknown`. |
| `actor_id` | string, nullable | Who holds control after the transition. Required when `state` is `human_local`, `human_remote` or `shared`. |

Reason definitions:

| Reason | Definition |
|---|---|
| `ai_requested_help` | The automated policy emitted a `help_request` (low confidence, explicit help call, timeout waiting for a decision) and a human responded. The transition MUST be preceded by a `help_request` on the same scope within `help_latency_max_s` (default 120 s); otherwise the loader re-codes the reason as `human_initiated` and records a flag. |
| `human_initiated` | A human took or was given control without an AI request (takeover, correction, demonstration, unplanned reset). |
| `safety` | A safety function, or a human acting for safety, stopped or took the scope. Transitions into `safety_stop` always carry `safety`. |
| `planned` | A pre-registered human action: a declared reset between tasks, a scheduled calibration, a planned demonstration block, a pre-registered approval gate. Planned transitions MUST reference a `plan_item_id`; the plan is frozen and its hash is in `session_start.plan_hash`. |

**Intervention.** An intervention is any transition of a scope out of `ai` or `scripted` into `human_local`, `human_remote` or `shared`, regardless of reason, plus any `approval` or discrete `hint` event (Sections 6.1, 6.2).

**Intervention episode.** An intervention lasts from the moment a scope leaves `ai` or `scripted` for a human or shared state until the scope returns to `ai` or `scripted`, the task ends, or the session ends. Every change between human and shared states inside that span (for example a pause-then-correct hand-over, `ai → shared → human_local → shared → ai`) belongs to the same intervention and is counted once, however long the steps are. `simultaneity_window_s` applies only to one human action taking several scopes (Section 6.6). Transitions into `safety_stop` are counted separately as `safety_stops` (Section 6.7). Between tasks (`task_id = null`), a transition into `human_*` from `idle` or `unknown`, or any transition into `human_*` with reason `planned`, is a reset (Section 6.3), not an intervention, but it is human time. A `planned` transition during an open task (e.g. a scheduled demonstration block) is an intervention with reason `planned`. This is deliberately broader than the California testing-regulation notion of a disengagement, which is a deactivation of the autonomous mode when a failure of the autonomous technology is detected or when safe operation requires the test driver (or, for driverless vehicles, the safety of vehicle, occupants or public) to deactivate the technology — Cal. Code Regs. tit. 13 § 227.50, https://www.law.cornell.edu/regulations/california/13-CCR-227.50 and the DMV adopted regulatory text https://www.dmv.ca.gov/portal/uploads/2020/06/Adopted-Regulatory-Text-2019.pdf [VERIFIED (excerpt): regulatory text excerpt read via web search 2026-10-30; full section not fetched]. The `reason` field lets a report recover the narrower count (`human_initiated` + `safety`, excluding `planned` and `ai_requested_help`) as a filter; the broad count is the default because it cannot be shrunk by relabelling.

## 4. Active time

```
observed_time = session_end.ts - session_start.ts            (per session, per scope)
active_time   = observed_time - idle_time - unknown_time
             = time in {ai, human_local, human_remote, shared, scripted, safety_stop}
```

Idle is excluded only when declared idle under Section 2. The report MUST print `idle_time`, `idle_threshold_s` and `idle_time / observed_time`, so that padding the log with idle time is visible even though idle is outside the denominator.

## 5. Task

A task is **autonomous** (counts in the task autonomy rate numerator) if, between its `task_start` and `task_end`, no scope has any interval in `human_local`, `human_remote` or `shared`, and no `approval` or `hint` event occurs. `safety_stop` intervals do not by themselves disqualify a task. Two rates are reported and MUST NOT be conflated: `task_autonomy_rate` (no human help, any outcome) and `autonomous_success_rate` (no human help AND `outcome = success`).

A task with `task_start` but no `task_end` before `session_end` has `outcome = unknown`; it is counted as not autonomous in the lower bound and excluded in the upper bound (Section 7). Tasks whose `task_definition_id` is not in the registry are flagged and excluded from task-level metrics (their time still counts in time-level metrics).

## 6. Edge cases

### 6.1 Remote approval clicks

An approval is a discrete human decision that lets the AI proceed (confirm a grasp, accept a route, acknowledge a warning). It is logged as an `approval` event (not a transition) with `scope`, `actor_id`, `reason` in {`ai_requested_help`, `planned`} and optional `dwell_s` (measured decision time).

Rules: an approval **counts as one intervention** and disqualifies the task from the autonomous numerator. It contributes `max(dwell_s, approval_min_dwell_s)` seconds to human time; `approval_min_dwell_s` defaults to 0 s. Because the default is zero, the time share can hide approval gates, so the report MUST print both `time_autonomy_share` (default) and `time_autonomy_share_with_approvals` (`approval_min_dwell_s` = measured median dwell, or 5 s if unmeasured), together with `approvals_per_active_hour`. If the AI waits for the approval longer than `approval_wait_max_s` (default 10 s), the wait MUST be logged as `shared` with reason `ai_requested_help` (the AI is blocked on a human).

### 6.2 Hints

A hint is human information that changes AI behaviour without the human commanding actuators (text or language instruction, a clicked point, a corrected sub-goal). Two encodings are allowed; the one in use is declared in `session_start.hint_policy`:

- `hint_as_shared` (default): the scope enters `shared` (reason `human_initiated`, or `ai_requested_help` if preceded by a `help_request`) when the hint is issued and returns to `ai` when the hint is consumed or `hint_ttl_s` (default 60 s) expires. Human time grows by the hint-active duration.
- `hint_as_event`: a discrete `hint` event with optional `dwell_s`; counts as one intervention and contributes `max(dwell_s, hint_min_dwell_s)` to human time (default 0 s, same sensitivity reporting as approvals).

Either way a hint disqualifies the task from the autonomous numerator. The instruction that defines the task, issued before `task_start`, is the task definition, not a hint.

### 6.3 Resets between tasks

A reset is human activity that restores the scene between tasks (re-placing objects, untangling a cable, moving the robot to a start pose). It is logged as a transition to `human_local` or `human_remote` with `task_id = null` and reason `planned` (if in the pre-registered plan) or `human_initiated` (if not).

Rules: reset time is always human time and is always reported (`reset_human_time`, `reset_human_time / active_time`). It is included in `active_time` and `human_time` for the **session-level** time autonomy share. It is excluded from **task-level** metrics (human minutes per task, task autonomy rate) only when reason is `planned`; an unplanned reset is attributed to the next task's human minutes because it is work the AI could not do. A robot that resets its own scene does so in state `ai` with `task_id = null`, which is AI active time.

### 6.4 Supervision without control

A human watching (locally or remotely) who issues no commands is **not** a control state; the scope stays `ai`. Supervision is a session attribute: `supervision` events carry `level` in {`none`, `remote_monitoring`, `local_monitoring`, `ready_to_takeover`} and `n_supervisors`. The loader computes `supervision_ratio` (fraction of active time with level above `none`) and `supervisor_hours_per_robot_hour`. Both are required report fields: "one remote operator per N robots" is the economic quantity of interest and it is not a control state.

### 6.5 Several humans at once

Each scope has at most one controlling `actor_id` at a time. Two humans on two scopes: both scopes are `human_*` and Section 6.6 applies. Two humans on one scope (one drives, one approves): the scope is `human_*` with the driver as `actor_id` and the approver generates `approval` events. Human **time** is scope-time-weighted, not per human: two humans for 10 s on one scope is 10 s of human time; `human_actor_seconds` (20 s) is reported separately as the labour measure, together with the number of distinct human `actor_id`s per session.

### 6.6 Per-body-part control (humanoids, multi-arm systems)

With K declared scopes, each scope has its own state timeline. Two aggregations are reported:

- **any-human rule (default, conservative):** the whole-robot state at t is `human` if any scope is in `human_local`, `human_remote` or `shared`; else `safety_stop` if any scope is in `safety_stop`; else `scripted` if any scope is `scripted`; else `ai` if any scope is `ai`; else `idle` if all scopes are `idle`; else `unknown`. When scopes are in different human states, the whole robot takes the first of `human_remote`, `human_local`, `shared` (conservative for the remote-operator question). The headline time autonomy share uses this timeline.
- **scope-weighted rule:** scope k has weight w_k (default 1/K; MAY be declared as actuator count or degrees of freedom in `session_start.scope_weights`). AI time = sum_k w_k * ai_time_k; active time = sum_k w_k * active_time_k. This is the "fraction of the body under AI control" and is always reported beside the any-human figure.

Intervention counts are per scope and summed; one human action that takes several scopes within `simultaneity_window_s` (default 0.5 s) counts once in the whole-robot count and once per scope in the per-scope count. Both are reported.

### 6.7 Safety stops and takeovers

A `safety_stop` followed within `takeover_window_s` (default 60 s) by a `human_*` transition on the same scope is one intervention with reason `safety`; the stop itself is counted in `safety_stops`. A `safety_stop` that returns to `ai` without a human is a safety stop only, not an intervention. `safety_stops_per_active_hour` is a required report field so that stopping instead of handing over is visible.

### 6.8 Holds and pauses

A hold is any period in which a scope keeps still or waits while a task is open: a pause key, a frozen pose, a wait for a decision, the alignment step of a hand-over. A hold takes the state of whoever caused it and is never `ai` or `idle` while a task is open.

| Cause of the hold | State | Reason |
|---|---|---|
| A human paused or froze the robot (pause key, operator freeze, hand-over alignment) | `shared` | `human_initiated` |
| The AI is blocked waiting for a human decision (Section 6.1) | `shared` | `ai_requested_help` |
| A safety function stopped motion (e-stop, protective stop, a limp or damping mode used as a safe state) | `safety_stop` | `safety` |
| The AI chose to wait as part of its own plan (a part has not arrived) | `ai` | — |
| No task is open | `idle` after `idle_threshold_s` | — |
| The cause cannot be observed | `unknown` | — |

A declared `idle` while a task is open is converted to `unknown` by the loader and flagged `idle_in_task`. A pause followed by human control is one intervention episode (Section 3).

### 6.9 Scripted motion

Replay of recorded actions (for example LeRobot `lerobot-replay`), teach-and-repeat and fixed macros are `scripted`. Replaying a recorded human demonstration moves human work in time; it MUST NOT be reported as `ai`. Commands that match a stored action sequence, or motion that does not react to a scene perturbation, are evidence of `scripted` whatever the log says.

### 6.10 Device location (local versus remote)

Software often cannot tell whether a human is next to the robot or far away. Every human input device is declared before the session in a device registry (`device_id`, device kind, location `local` or `remote`) whose hash is written into `session_start.device_registry_hash`. Human transitions, approvals and hints carry `device_id`. The state follows the declared location. Human control through an undeclared device, or a device without a declared location, is `human_remote` and is flagged `undeclared_device`. Remote control declared as local is exposed at trust level L3 when it passes through the instrumented station.

## 7. Unmeasured time and bounds

Every metric with time or tasks in the denominator is reported as a point estimate plus two bounds:

- **lower bound (unknown as human):** all `unknown` intervals are counted as human time; tasks with unknown outcome or containing unknown gaps are counted as not autonomous.
- **upper bound (unknown as AI):** all `unknown` intervals are counted as AI time; tasks containing unknown gaps are counted as autonomous if they otherwise qualify.

The point estimate excludes `unknown` from both numerator and denominator (`active_time` as defined in Section 4). The report MUST print `unknown_time / observed_time`; a report whose unknown share exceeds `unknown_share_max` (default 0.05) is marked `incomplete`. Confidence intervals are computed for the point estimate and for each bound; the reported outer interval is the union (lower CI limit of the lower bound, upper CI limit of the upper bound), which combines sampling and measurement uncertainty.

## 8. Event log format

The log is JSON Lines (one JSON object per line) validated against `event.schema.json`, written in JSON Schema draft 2020-12 (metaschema https://json-schema.org/draft/2020-12/schema; specification https://json-schema.org/draft/2020-12/json-schema-core.html [VERIFIED (excerpt): specification index page read via web search 2026-10-30]). Timestamps use the RFC 3339 `date-time` profile of ISO 8601 (https://www.rfc-editor.org/info/rfc3339/ [VERIFIED (excerpt): RFC 3339 Section 5.6 grammar read via web search 2026-10-30]) and MUST use the `Z` offset.

| `type` | Required fields beyond the common ones | Purpose |
|---|---|---|
| `session_start` | `scopes`, `initial_states`, `heartbeat_interval_s`, `hint_policy`, `task_registry_hash`, `plan_hash`; optional `scope_weights`, `device_registry_hash` | Opens a session; declares scopes and parameters. |
| `session_end` | — | Closes a session. |
| `transition` | `scope`, `state`; `reason`/`actor_id` per Section 3; optional `plan_item_id`, `related_seq`, `device_id` | State change. |
| `approval` | `scope`, `actor_id`, `reason`; optional `dwell_s`, `device_id` | Discrete human decision (6.1). |
| `hint` | `scope`, `actor_id`; optional `dwell_s`, `ttl_s`, `device_id` | Discrete hint under `hint_as_event` (6.2). |
| `help_request` | `scope` | AI asked for help. |
| `task_start` | `task_id`, `task_definition_id` | Opens a task. |
| `task_end` | `task_id`, `outcome` | Closes a task. |
| `supervision` | `level`, `n_supervisors` | Supervision level change (6.4). |
| `heartbeat` | optional `distance_m` | Liveness; cumulative odometry for distance-based metrics. |

Common fields on every event: `robot_id`, `session_id`, `task_id` (nullable), `seq`, `ts`, `type`; optional `distance_m` (cumulative metres since `session_start`, non-decreasing), `prev_hash`, `hash`. `hash` is the lower-case hex SHA-256 of the event serialised with the RFC 8785 JSON Canonicalization Scheme (https://www.rfc-editor.org/rfc/rfc8785), without its own `hash` field and including `prev_hash`. `prev_hash` is the previous event's `hash`; for the first event of a session it is the genesis hash SHA-256 of the UTF-8 string `autonometry/chain-v1|<robot_id>|<session_id>` (the prefix names the hashing scheme, not the spec version, so it changes only if the hashing itself changes). `prev_hash` may be null only in unsealed (trust level L1) logs; a verifier asked for L2 or above MUST fail when any event lacks `hash` or `prev_hash`. The trust levels that use these fields are specified by the sibling trust review.

**Time.** `ts` is the time the recorder observed the event, from a UTC clock synchronised by NTP or PTP; the clock status is recorded at every checkpoint (trust review, Section 4.2). A source's own timestamp MAY be kept as `x_src_ts` with `x_src_clock` (`wall`, `monotonic`, `sim`, `frame_index`). Events rebuilt after the fact from files rather than observed live (for example from LeRobot dataset frames, whose `timestamp` is `frame_index / fps`) MUST carry `x_ts_reconstructed: true`; the loader counts them under the flag `ts_reconstructed`, and spans built from them are reported at trust level L1 at most.

**Extensions.** Fields whose names start with `x_` are allowed on every event and ignored by the metrics. Any other field not defined here is a schema error.

**Rejected lines.** A line that cannot be read as one valid event is rejected with an error code from `rules.toml` `[errors]` and reported with its line number. A rejected line leaves a gap in `seq`, so the interval around it becomes `unknown` (Section 2, rule 2).

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
| `unknown_share_max` | 0.05 | 7 |

All parameters MUST be printed in the report; changing a default is allowed but is itself a reported fact.
