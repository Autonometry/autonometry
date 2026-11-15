# Timelines, metrics and the report

## Read a timeline

```
autonometry timeline /var/lib/autonometry/tb4_sim_01/*/events.jsonl          # summary per session
autonometry timeline robot.jsonl --devices devices.json --tasks tasks.json --plan plan.json --json
```

`timeline` turns each session into exact facts (definitions.md sections 1 to 8). It needs the
session's three registries: the device registry, the task registry and the plan. By default it reads
`devices.json`, `tasks.json` and `plan.json` from the folder of the log, where the recorder keeps
them; `--devices`, `--tasks` and `--plan` name one file for every session instead. Each file must
match its hash in `session_start`. A file that does not match, or is not a valid registry, is an
error: that session gets no timeline and the command exits 1. A missing file is not an error but
is counted against the robot: without the device registry every source counts as a remote human
(`registry_missing`, `undeclared_device`); without the task registry every task is flagged
`unknown_task_definition`; without the plan every `planned` reason becomes `human_initiated`
(`unplanned`). The command also exits 1 when lines were rejected, and 2 on bad usage.

For each session it prints:

- **Time.** Observed time (`session_start` to `session_end`), split into active, idle and unknown
  time and into the eight states, for the whole robot, scope-weighted and per scope. The whole
  robot is human when any scope is human (`human_remote` before `human_local` before `shared`),
  else `unknown` when any scope is unknown (an unknown arm beside an AI-controlled leg is never AI
  time), else `safety_stop`, `scripted`, `ai`, `idle` (all scopes idle) or `unknown`. Scope weights are
  the declared `scope_weights` normalised to 1, or equal. Every total is exact: the states of
  each timeline add up to observed time to the nanosecond.
- **Unknown time.** A silence longer than the heartbeat interval plus `gap_tolerance_s` is
  unknown from that limit on. A gap in `seq`, such as a rejected line, is unknown, and every scope
  then stays unknown until its next transition. `idle` while a task is open is unknown too
  (`idle_in_task`).
- **The registry decides the state.** A transition from a person's device takes `human_local` or
  `human_remote` by the device's location (or stays `shared`), from an AI controller `ai`, from a
  script `scripted`; when the log says otherwise the registry wins (`registry_mismatch`).
  Control from a device that is not in the registry, from no named device, or as an initial state
  at `session_start` counts as `human_remote` (`undeclared_device`).
- **Interventions.** Each episode on a scope runs from the change that makes it an intervention to
  the return to `ai` (or to `scripted` no person started), the task end or the session end. It
  counts once however many hand-overs it holds. The kinds are `takeover` (into `human_*`),
  `shared`, `safety_stop` (a stop a person triggered), `preset` (a fixed motion a person started),
  `approval` and `hint`. A run of `unknown` and `safety_stop` between AI control and human
  control does not hide an intervention: the scope still counts as leaving `ai`. An automatic stop
  followed within `takeover_window_s` (and before the task ends) by a person taking over is one
  `takeover` with reason `safety` that starts at the stop; a later takeover is a takeover of its
  own. The whole-robot count takes one person's takeovers of several scopes within
  `simultaneity_window_s` once. Between tasks, a person taking over from `idle` or `unknown`, or
  with reason `planned`, is a reset, not an intervention. On transitions, approvals and hints,
  `ai_requested_help` without a `help_request` on the scope within `help_latency_max_s` becomes
  `human_initiated` (`help_reason_recoded`), and so does `planned` without its plan item
  (`unplanned`); a transition the registry makes human without a reason takes `human_initiated`.
- **Approvals and hints** with their dwell. A dwell sits on [ts − dwell, ts); only the part that
  overlaps `ai` time moves from AI to human time (`moved_from_ai_s`), and windows that
  overlap count once (the AI time inside their union moves). One inside an intervention
  episode belongs to it and is not counted again (`in_episode`). An `ai_requested_help` approval
  more than `approval_wait_max_s` after the help request, with no transition, seq gap, task start,
  task end or other approval on the scope since, turns that wait's `ai` time into `shared`
  (`approval_wait_recoded`).
- **Safety stops**, each human-triggered or automatic, and whether a takeover joined it.
- **Tasks**: outcome (`unknown` when not closed), autonomous (no human time, no intervention, no
  approval or hint inside, and no unplanned human time since the previous task), whether it
  contains unknown time, its human time, and the unplanned human time since the previous task
  (`unplanned_reset_s`, which adds to the task's human minutes).
- **Resets** (human time between tasks, planned or not), **supervision** (`supervision_ratio` and
  supervisor-hours per robot-hour over active time), **humans** (distinct people and actor-time:
  two people on one scope count twice), **supported time** and **distance** (the largest
  `distance_m` any event reported).
- **Flags**, the reader's and the timeline's, with the `seq` where each shows.

`--json` gives the same as stable JSON: durations in seconds, times in RFC 3339 with nine fraction
digits. Unlike `validate`, it prints identifiers from the log (actor, device, task and plan item
ids): they are pseudonyms, and the schema restricts them to letters, digits and `._:-`. Statistics
and confidence intervals come from `autonometry metrics`.

## Compute metrics

```
pip install 'autonometry[report]'    # or, in a source checkout: uv sync --extra report
autonometry metrics /var/lib/autonometry/tb4_sim_01/*/events.jsonl --out metrics.json
autonometry metrics robot.jsonl --alpha 0.1 --n-boot 2000 --seed 0 --cluster auto --drift-bin week
```

`metrics` builds every session's timeline (registries are found as for `timeline`) and turns the
timelines of many sessions into the metrics of the standard, as JSON on stdout or in `--out`. It
needs the `report` extra (NumPy, SciPy, statsmodels); without it, it prints one line and exits 2.
A session that gets no timeline (a registry that does not match its hash) is left out and named on
stderr, and the command exits 1; so do rejected lines. The same logs and options give byte-identical
JSON: every bootstrap is seeded from `--seed`, keys and lists are sorted, and there is no
generated-at time.

Simulated and real sessions are never pooled: the report has one block per group present
(`groups.real`, `groups.simulated`). Every metric has an `estimate`, a default interval `ci` and
its `interval_method`; secondary intervals sit beside it as `ci_<method>`. A metric that cannot be
computed (no tasks, no active time, no events, no distance) has a null estimate and a `note`, never a
crash. Five metrics also get bounds for unknown time (definitions.md 7): `unknown_as_human`
(unknown time counted as human, tasks with unknown time or unclosed not autonomous) and
`unknown_as_ai`, each with its own interval, and an `outer_interval` from the smallest to the largest
of the six limits. Counts are whole-robot (one person's action is one intervention); per-scope
counts are reported beside them.

| Metric | Estimator | Default interval | Also reported |
|---|---|---|---|
| (a) `time_autonomy_share` | (AI time − dwell its approvals and hints moved) / active time, whole-robot timeline, ratio of sums over clusters | ratio-linearised t over sessions, or robots under `--cluster auto` with 3 or more robots; Wilson with effective n (control segments) with one cluster | bounds; percentile cluster bootstrap; variants `with_approvals` (minimum dwell = median logged dwell per type, else 5 s), `shared_as_ai`, `scope_weighted`, `unsupported` (active time outside physical support); human_remote, human_local, shared, safety_stop and scripted shares; idle, reset and supported time and shares |
| (b) `task_autonomy_rate`, `autonomous_success_rate` | closed tasks with a known definition and no human help (and outcome success) / closed tasks | cluster-robust GLM from 20 sessions, Wilson below | bounds (all tasks; an unclosed task or one with unknown time is not autonomous); Clopper-Pearson; Wilson |
| (c) `interventions_per_active_hour` | whole-robot interventions / active hours | NB2 Wald (multi-start fit) with labelled Poisson-exact fallbacks | bounds (unknown hours added; the lower bound counts one intervention per run of unknown and safety stop between AI control); Garwood; percentile bootstrap; dispersion index; per-scope count |
| (c) `safety_stops_per_active_hour`, `approvals_`, `hints_`, `takeovers_per_active_hour` | whole-robot count / active hours | NB2 Wald | stops human-triggered and automatic; per-scope stops; Garwood |
| (d) `gaps_between_interventions` | active hours and metres between interventions, the last gap per session right-censored; Kaplan-Meier median, the smallest gap with S(t) ≤ 0.5 (statsmodels `SurvfuncRight`) | medians of the pointwise cloglog band, as lifelines | S(h) at horizons; exponential mean gap (log-scale Wald); Weibull shape (SciPy `CensoredData`) with a cluster-bootstrap interval |
| (e) `human_minutes_per_task` | mean over tasks with a known definition of human time + unplanned reset + moved dwell | cluster bootstrap-t with relabelled clusters; t on tasks with one session; limits clipped at 0 (the method name then ends in `, clipped at 0`) | bounds (task unknown time added); percentile bootstrap; median; share of tasks with no human time |
| (f) `breakdowns` | interventions per active hour by reason and kind; narrow count (hand-overs with reason `human_initiated` or `safety`); rates and means per task definition | Poisson exact per cell; Wilson per task definition | — |
| (g) `drift` | interventions per active hour by UTC day or ISO week, active time split at bin edges | Poisson GLM trend on the bin index (NB when the dispersion index exceeds 1.5), Wald interval of the rate ratio | two-sided CUSUM with its first signal |
| (h) `unmeasured` | unknown time / observed time | — | `complete` or `incomplete` against `unknown_share_max`; flag counts; `supervision_ratio`; supervisor-hours per robot-hour; human actor hours and distinct actors; sessions with blind spots (`unobserved`); every parameter of definitions.md 9 |

`--cluster session` or `--cluster robot` sets the cluster unit of every clustered interval. Every
statsmodels or SciPy fit runs with warnings turned into errors: an attempt that warns is rejected,
and a fallback's method name says why (for example `poisson_exact_fallback(alpha_at_zero_boundary)`).
Metrics that depend on a signal a session declares `unobserved` list it in their `unobserved` field.
The estimators are the reference package's, chosen by the coverage study in `research/simulation`;
`tests/slow` checks their coverage on its baseline scenario. Test-length planning (zero-event
hours, tasks for a failure rate, two-period power) is in `autonometry.stats.planning`.

## Write a report

```
pip install 'autonometry[report]'    # or, in a source checkout: uv sync --extra report
autonometry report /var/lib/autonometry/tb4_01/*/events.jsonl --out report/ \
    --vkey tb4_01.vkey --anchor customer-anchors.jsonl --tsa-root freetsa.pem \
    --title "TurtleBot 4 fleet, November" --about deployment.txt
```

`report` combines verification, timelines and metrics into one JSON document and one HTML page
(definitions.md 11): `report.json` and `report.html` in `--out`, each written atomically. The page is
self-contained: inline CSS and inline SVG figures, no scripts, no web fonts and no links to other
sites, so it opens offline and prints cleanly to PDF from a browser. It takes the options of
`verify` (`--vkey`, `--tsa-root`, `--anchor`; checkpoint files are found beside each log) and of
`metrics` (`--devices`, `--tasks`, `--plan`, `--alpha`, `--n-boot`, `--seed`, `--cluster`,
`--drift-bin`, `--spec`), plus `--title` (plain text, at most 200 characters), `--about` (a
plain-text UTF-8 file of at most 64 KiB describing the deployment) and `--station LOG`
(repeatable: teleoperation station logs that witness the robots, see [Station
witness](witness.md)). Title and about are shown escaped, never as markup. It
makes no network call.

What the report holds, in the order of the page:

- **Verdict** (at the top): trust status (`verified`, `not verified` or `FAILED`) and level,
  `complete` or `incomplete`, the census result, how many sessions were left out, and per group the
  four headline numbers (time autonomy share, task autonomy rate, interventions per active hour,
  human minutes per task) with their confidence interval and outer interval.
- **Trust**: each session's status and level with its evidence and gap codes: L3 when its station
  witnesses agree (`--station`), else L2 for a `verified` session, else L1; the station sessions
  with their status; rejected lines and file problems of every file read. The report's level is
  the lowest of its robot sessions'; it is verified only when every session, the stations'
  included, is verified and nothing was rejected. When a session is `failed`, the top says so and
  the session **stays in the metrics**: leaving it out would reward corrupting a bad session. The
  report says when no trusted key or TSA root was given.
- **Witness** (with `--station`): per robot session the five times of the reconciliation
  (both-human with its tolerated and held parts, robot-only, station-only, neither, unwitnessed),
  the share that decides L3, the range of tau, the unmatched station episodes, the upper-bound
  intervention count and why the session is not L3; and what L3 cannot show. Without station
  logs, L3 is shown as not available.
- **Census**: for session ids of the recorder's form (`000042-20261112T083000Z-7f3a`), the counters
  missing between each robot's lowest and highest; other id forms are listed as not checked.
- **Exclusions**: every session left out of the metrics and why (a registry that does not match
  its hash, no `session_start`, a session named only by a checkpoint or anchor, or a station log
  given as a robot log).
- **Metrics**: the `autonometry metrics` JSON per group (real, simulated), never pooled, with
  tables and up to four figures in colour-blind-safe colours, each state always in the same colour:
  whole-robot state shares, the time autonomy share with its interval, bounds, outer interval and
  variants, the Kaplan-Meier curve of the gaps between interventions, and the drift rate per bin.
- **Statements** beside the metrics: no true worst case for interventions per active hour and human
  minutes per task when unknown time exists; "data quality, not sample size, limits this number"
  when a metric's bounds are wider than its confidence interval; "not generalisable" for an
  interval from fewer than `min_clusters` clusters (2); the one-sided 95 % Garwood upper bound of a
  rate from fewer than `rare_rate_events` events (5); the signal classes sessions declare
  `unobserved`. Both thresholds come from `rules.toml` `[report]` and are printed with the
  parameters. The page lists each kind of statement once per group, naming its metrics.
- **Invariants**, each `pass`, `fail` or `not checked`: partition (state times sum to the session
  window, per scope and whole robot), chain (no session contradicts its seals; not checked for
  unsealed sessions), time (no `ts_regression`, `clock_skew` or `late_anchor`), census, tasks
  (started = closed + not closed, every definition in the registry), bounds (`unknown_as_human` ≤
  estimate ≤ `unknown_as_ai`), witnesses (every witnessed robot session within the L3 maxima;
  `not available` when no station log witnesses a robot session).
- **Sessions**: one row per session (robot, session, simulated, status, level, observed and
  active time, AI share, interventions, unknown share, flags).
- **Parameters** (every parameter of definitions.md 9 and the statistics options) and
  **provenance**: spec and software version, `generated_at`, robots, period, counts of sessions,
  robots, UTC days and tasks, the registry hashes the sessions declare, the SHA-256 of every input
  file (paths as given on the command line) and of every spec file.

The report contains no actor ids, only counts. The same inputs and options give byte-identical
JSON and HTML apart from `generated_at`, on every system: file names are written with forward
slashes, relative where they were given relative. The page shows times to the second (the JSON keeps full
precision), and the state table notes how much AI time approval and hint dwell moved to human
time. The verdict box is green only for a clean report, amber for one that is not clean and red
when a session failed. Exit status: 0 only when the report is clean: verified at L2 or L3, complete,
no census gap, no session left out, no invariant failed, and every session's `session_end` covered
by an anchored checkpoint (else the reasons say that events after its last anchor, at the time
given, may have been removed); 1 when it was written but is not clean
(the last output line says why); 2 on bad usage, unreadable input or without the `report` extra.
