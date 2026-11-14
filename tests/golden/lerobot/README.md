# LeRobot golden set

`test_connector_lerobot_golden.py` checks two runs against `expected.json`, whose numbers were
worked out by hand from spec v0.3 and dossier 14.1 (research/lerobot-so101) before the code ran.

## Live: `rollout.log`

A DAgger rollout with `record_autonomous=true`, in LeRobot v0.6.1's log format
(`LEVEL YYYY-MM-DD HH:MM:SS <location:15> message`, lerobot/utils/utils.py:70). Each line is
timed by its printed clock, seconds after 10:00:00. The engine, the recorder (fake clock, ticks
every 0.1 s) and `autonometry timeline` run over 160 s, with the example SO-101 registries in
`connectors/lerobot/`.

| Time (s) | Line | State | Why |
|---|---|---|---|
| 0 | `Building rollout context...` | `idle` | the rollout started, its loop not yet (module rule 3) |
| 10 | `DAgger continuous recording started` | `ai`, task `episode_000000` | the policy drives (14.1 AUTONOMOUS); episodes are tasks (5) |
| 40 | `autonomous -> paused` | `shared`, `human_initiated` | a human pause (6.8; 14.1 PAUSED) |
| 42 | `paused -> correcting` | `human_local` | the leader arm is local (6.10; 14.1 CORRECTING) |
| 52 | `correcting -> paused` | `shared` | |
| 53 | `paused -> autonomous` | `ai` | the intervention episode ends (3) |
| 70 | `Episode saved (total: 1, ...)` | task `episode_000001` | rotation: outcome `unknown` (5) |
| 100 to 104 | pause and resume | `shared` | an intervention without a correction (3, case a) |
| 130 | `Episode saved (total: 2, ...)` | task `episode_000002` | |
| 140 | `... control loop ended` | `ai` kept, task closed | the last controller holds (6.14) |
| 145 | `Returning robot to initial position...` | `scripted` | a fixed motion LeRobot starts (6.9, 6.13; user ruling) |
| 148 | `Disconnecting robot...` | `scripted` kept | released; idle only after 30 s (6.14) |
| 149 | `Rollout finished` | `unknown` | nothing is observed any more (2) |
| 160 | | session end | |

Whole robot: idle 0-10 = 10 s; ai 10-40, 53-100, 104-145 = 30 + 47 + 41 = 118 s; shared 40-42,
52-53, 100-104 = 2 + 1 + 4 = 7 s; human_local 42-52 = 10 s; scripted 145-149 = 4 s; unknown
149-160 = 11 s. Observed 160 s, active 160 - 10 - 11 = 139 s.

Tasks: `episode_000000` (10-70) has human time 2 + 10 + 1 = 13 s and one intervention
(`ai -> shared -> human_local -> shared -> ai` is one episode, section 3); `episode_000001`
(70-130) has 4 s and one; `episode_000002` (130-140) has none and is autonomous. Two
interventions in all; one human actor (`op_01`).

## Import: a dataset built by the test

Four episodes at 10 fps, imported with `--start 2026-11-14T10:00:00Z`, laid back to back:

| Episode | Frames | Session | Expected |
|---|---|---|---|
| 0 | 20 false, 10 true, 20 false | 0-5 s | ai 4 s, human_local 1 s, one intervention, not autonomous |
| 1 | frames 0-9 and 15-29 false, length 30 | 5-8 s | ai 2.5 s, unknown 0.5 s (frames 10-14 missing); autonomous in the point estimate, `contains_unknown` (section 7) |
| 2 | 5 true, 5 false, task "Fold the cloth." | 8-9 s | human_local 0.5 s, ai 0.5 s; the scope starts `unknown`, so the first human frame is no intervention (3); `unknown_task_definition` |
| 3 | its data file is absent | none | listed as missing, not imported |

Every session is flagged `ts_reconstructed` (8) and `clock_unsynced` (its heartbeats report
`clock_source` `unknown`).
