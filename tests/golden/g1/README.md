# Unitree G1 golden session

`test_connector_g1_golden.py` runs `session.json` through the engine
(`autonometry.connectors.g1`), the real recorder (fake clock), `autonometry timeline` and
`autonometry report`, and compares them with `expected.json`. Every number below was worked out
by hand from spec v0.3 and the integration notes (research/unitree-g1, section 6) before the code
ran.

## The run

The example registry of `connectors/g1/` and a recorder with scopes `legs` (weight 12) and
`upper_body` (weight 17). Every 0.1 s from t = 0 to 110 s the robot publishes `rt/lowstate`, with
the remote's bytes, and `rt/sportmodestate`; the events of that instant follow (in the order of
`session.json`), then the watcher's tick. `robot` and `remote` give the state from each time on;
`streams` are commands sent at every step from `from` to `to`; `events` are requests and the
robot program's tasks, which reach the recorder from their own client and the engine as the
session log follower would report them. Times are seconds after 2026-11-14T10:00:00Z.

| t (s) | Observed | legs | upper_body | Why |
|---|---|---|---|---|
| 0 | the robot, standing (fsm 500), no task | `unknown` | `unknown` | no controller seen yet, not idle before 30 s (6.14) |
| 1 | `task_start` t1 | `ai`, built-in | `ai` | rule 7d: static in an open task, the AI keeps balance |
| 5 to 15 | remote stick ly 0.5, robot moving | `shared`, op_01 | as legs | rule 7a: a person's goal input (6.11); neutral at 15 ends it at once |
| 20 to 30.5 | the policy's SetVelocity every 0.5 s (1 s each) | `ai`, g1_policy | as legs | rule 7a: an AI's goal span, until 29.5 + 1 s |
| 35 to 45 | xr_teleoperate on `rt/arm_sdk`, weight 1.0 | `ai` | `human_remote`, op_02 | rule 6b; its last sample is active until 47.0, so the arms return at the tick of 47.1 |
| 50 | remote SELECT + Y; `task_id` 1 from 50.5 | `ai` | `scripted`, `human_initiated`, script:wave_hand | rule 6a (6.13); SELECT + Y is no goal input |
| 55 | `task_id` back to 0 | `ai` | `ai` | the preset ended |
| 60 | remote L2 + B; fsm 1 (damping) from 60.1 | `safety_stop`, op_01 | `safety_stop` | rule 2: the stop starts when the passive state follows, at 60.1 (6.7) |
| 70 | fsm 500 | `ai` | `ai` | the built-in state left passive |
| 75 to 76 | SetVelocity from an undeclared laptop | `human_remote`, no device | as legs | rule 7a: undeclared, counted as a remote person (6.10) |
| 80 | ReleaseMode (1003) from xr_teleoperate | `unknown` | `unknown` | rule 3: released, no `rt/lowcmd` writer, inside a task |
| 80.5 to 90 | xr_teleoperate on `rt/lowcmd` | `human_remote`, op_02 | `human_remote` | rule 3; active until 92.0 |
| 92.1 | nobody writes | `unknown` | `unknown` | rule 3: no writer for more than `gap_tolerance_s` inside a task (6.14) |
| 93 to 101 | the policy on `rt/lowcmd` | `ai`, g1_policy | `ai` | rule 3 |
| 101.5 | SelectMode (1002) | `ai`, built-in | `ai` | rule 7d |
| 104 | `task_end` t1, success | `ai` | `ai` | outside a task the last controller holds until idle (6.14) |
| 110 | the recorder stops | | | |

## Hand-worked totals

legs: unknown 1.0 + 0.5 + 0.9 = 2.4 s; shared 10 s; human_remote 1 + 11.6 = 12.6 s; safety_stop
70.0 − 60.1 = 9.9 s; ai the rest, 110 − 2.4 − 10 − 12.6 − 9.9 = 75.1 s (that is 4 + 45.1 + 5 +
4 + 17).

upper_body: as legs, except human_remote 35.0 to 47.1 (12.1 s) and scripted 50 to 55 (5 s), both
taken from ai: human_remote 24.7 s, scripted 5 s, ai 75.1 − 17.1 = 58.0 s.

Whole robot (any-human rule, 6.6): human_remote 24.7 s, shared 10 s, unknown 2.4 s, safety_stop
9.9 s, scripted 5 s (legs ai, arms scripted), ai 58.0 s. Observed 110 s, active 107.6 s.

Interventions (section 3): 5.0 (ai to shared), 35.0 (arms ai to human_remote), 50.0 (a preset a
person started, case d), 60.1 (a stop a person triggered, case c), 75.0 (ai to human_remote) and
80.5 (ai to human_remote across the unknown run 80.0 to 80.5): 6 on the whole robot; per scope
legs 4 (5.0, 60.1, 75.0, 80.5) and upper_body 6, 10 in all. One safety stop on the whole robot,
triggered by a person (op_01); it takes both scopes, so the timeline's per-scope counts (`per_scope`,
`human_triggered`) are 2.

Task t1 (1 to 104 s): outcome success, not autonomous (interventions), contains unknown time
(80.0 to 80.5 and 92.1 to 93.0), human time 10 + 24.7 = 34.7 s, 6 interventions, no unplanned
time before it. Human actors: op_01, op_02 and `person:unidentified`, 3. Flag
`undeclared_device` (the laptop's two transitions).

## Report

The log verifies at L2 with its own checkpoint file as the anchor file (definitions.md 10.5 b)
and the recorder's vkey. Time autonomy share 58.0 / 107.6 = 0.53903; bounds `unknown_as_human`
58.0 / 110 = 0.52727 and `unknown_as_ai` (58.0 + 2.4) / 110 = 0.54909; scripted share
5 / 107.6 = 0.04647; safety-stop share 9.9 / 107.6 = 0.09201; 6 interventions over 107.6 / 3600
active hours = 200.74 per active hour; task autonomy rate 0 of 1. Unknown time 2.4 / 110 = 2.2 %
is below `unknown_share_max`, so the report is complete; one session, no census gap: clean.
