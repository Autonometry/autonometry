# Autonometry

Autonometry measures how much of a robot's work is done by its AI and how much by humans, local or remote.
It records every change of control in a sealed, tamper-evident log, verifies logs, reconciles a robot's log with a teleoperation station's log, and reports autonomy metrics with honest confidence intervals.
It is open source and the reference implementation of an open standard; it only observes and never sits in a robot's control loop.

**Status: pre-alpha.** Version 0.0.1.dev0 records sealed logs on a robot (`autonometry record`),
reads and checks event logs (`autonometry validate`), seals and verifies them (`autonometry
seal`, `timestamp`, `verify`; trust levels L1 and L2), turns each session into an exact
timeline (`autonometry timeline`) and computes the metrics of the standard with confidence
intervals (`autonometry metrics`), and writes a report a buyer, insurer or auditor can read
(`autonometry report`). Nothing is published on PyPI yet.

## Install

From a source checkout (Python 3.12 or newer, [uv](https://docs.astral.sh/uv/)):

```
uv sync --all-extras --dev
uv run autonometry --help
```

Once released: `pip install autonometry` (extras: `report`, `lerobot`).

## Try it

```
autonometry --version
autonometry spec list          # spec versions shipped with the package
autonometry spec show 0.3      # its files with SHA-256 hashes
autonometry validate robot.jsonl station.jsonl         # summary; exit 1 if any line is rejected
autonometry validate --json --spec 0.3 robot.jsonl     # the same as stable JSON
autonometry registry check devices.json                # device registry: valid? and its hash
autonometry registry check --kind tasks tasks.json     # the same for a task registry or a plan
```

`validate` reads each file line by line. A line that is not one valid event (bad UTF-8, longer than
64 KiB, not I-JSON, a lone surrogate or Unicode noncharacter, against the schema, an impossible
`ts`, or a `seq` already used in its session) is rejected with an error code and its line number.
Every rejected line is counted; the first 100 of each code are listed. Each session is then checked for structural
problems (missing `session_start` or `session_end`, gaps in `seq`, silences longer than the heartbeat
plus tolerance, `ts` going backwards, tasks that overlap or never close, and more), reported as
warning flags. The codes are listed in [`spec/v0.3/rules.toml`](spec/v0.3/rules.toml). Event contents,
such as actor ids, are never printed; the robot and session ids it does print are restricted by
the schema to letters, digits and `._:-`, so a log cannot send control codes to your terminal. Add
`-v` before the command for debug output.

Every `session_start` says whether the session ran in a simulator (`simulated`), and heartbeats
report the recorder's clock (`clock_source`, `clock_max_error_s`). A session whose clock is not
reported as synchronised within `clock_tolerance_s` is flagged `clock_unsynced`.

`registry check` validates a device registry, the JSON file that declares every source that can
command the robot (people's input devices with their location, AI controllers, scripts, hoists;
definitions.md 6.10), and prints its SHA-256, which goes into `session_start.device_registry_hash`.
It exits 1 for an invalid registry, including a repeated `device_id` or a device that an earlier
one shadows (the first match wins), and never prints values from the file. `--kind tasks` checks a
task registry (`{"tasks": [{"task_definition_id": ...}]}`, section 1) and `--kind plan` a plan
of pre-registered human actions (`{"items": [{"plan_item_id": ..., "kind": "reset"}]}`, section 3)
the same way; their hashes go into `task_registry_hash` and `plan_hash`.

## Sealing and verification

```
autonometry keys generate --name autonometry/<robot_id> --out robot.key   # writes robot.key, robot.key.vkey
autonometry seal robot.jsonl --key robot.key --out robot.sealed.jsonl     # + robot.sealed.checkpoints.jsonl
autonometry timestamp robot.sealed.checkpoints.jsonl \
    --tsa-url https://freetsa.org/tsr --tsa-root freetsa.pem             # optional, needs network
autonometry verify robot.sealed.jsonl --vkey robot.key.vkey --json       # trust level per session
```

`seal` hash-chains every event (definitions.md section 8) and emits signed Merkle checkpoints
(RFC 9162, C2SP's `signed-note` and `tlog-checkpoint` formats, Ed25519) as it goes (section 10.3),
refusing input that has rejected lines, is already sealed, or would overwrite an existing output.
`timestamp` anchors a checkpoint with a real RFC 3161 token from a Time Stamping Authority; an
anchor file -- a copy of a checkpoint kept outside the log, by the customer or a witness -- does
the same without a TSA (`verify --anchor FILE`). `verify` recomputes every event's hash from its
content and every Merkle root from the events, and never trusts a stored value; every checkpoint
and anchor is checked against them, so an anchor also catches events or whole sessions deleted
later, together with their checkpoints. Each session is reported `verified`, `partial`,
`unverified` or `failed`, with evidence codes (any one means the log contradicts its own seals:
tampering or corruption) and gap codes (never raise the trust level). Records that cannot be tied to
one session (a malformed checkpoint, a timestamp for no checkpoint) are file problems. The output
ends with `result: verified` or `result: NOT verified: <reasons>`; rejected lines of any file read
and file problems make the result not verified whatever the sessions' statuses. The codes are in
`spec/v0.3/rules.toml` `[sealing]`.

What L2 proves: nothing was dropped, re-timed or edited after a checkpoint was anchored, and any
such change is detected even without the original log. What it does not prove: a dishonest recorder
can still log false events as they happen, and a key held in software can sign a second, fabricated
log from scratch -- only an anchor made *before* the fabrication catches that (an anchor the
fabricator can also redo does not). Sealing an old, unanchored log late is indistinguishable from a
log that was sealed on time but anchored late, and shows up as `late_anchor`. Trust level L3 adds
a second, independent witness against the first class of problem (see [Station
witness](#station-witness-trust-level-l3)); L4 (hardware-rooted keys) follows in a later spec
version.

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
uv sync --extra report     # or, once released: pip install 'autonometry[report]'
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
uv sync --extra report     # or, once released: pip install 'autonometry[report]'
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
witness](#station-witness-trust-level-l3)). Title and about are shown escaped, never as markup. It
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
JSON and HTML apart from `generated_at`. The page shows times to the second (the JSON keeps full
precision), and the state table notes how much AI time approval and hint dwell moved to human
time. The verdict box is green only for a clean report, amber for one that is not clean and red
when a session failed. Exit status: 0 only when the report is clean: verified at L2 or L3, complete,
no census gap, no session left out and no invariant failed; 1 when it was written but is not clean
(the last output line says why); 2 on bad usage, unreadable input or without the `report` extra.

## Recorder

`autonometry record --config FILE` runs on the robot's computer (Linux; macOS for development). It
receives control events from *connectors* (small adapters next to the robot's software) over a
local Unix datagram socket and writes a sealed, crash-safe log that `autonometry verify` accepts at
L2. It never sits in a control loop: connectors send fire-and-forget datagrams and carry on, and a
recorder that is down or slow only costs events, which the log then shows as `unknown` time.

What it writes, per robot and session: `<data_dir>/<robot_id>/<session_id>/events.jsonl` and
`events.checkpoints.jsonl` (folders 0700, files 0600), where `session_id` is a per-robot counter,
the UTC start time and a random suffix, such as `000042-20261112T083000Z-7f3a`. The next session
takes the largest counter on disk plus 1, and the newest session is the one with the largest
counter, never decided by time: a computer without a real-time clock (a Raspberry Pi, as in the
TurtleBot 4) can boot with an old time. Each session folder also gets private copies of the device
registry, task registry and plan the session used, as `devices.json`, `tasks.json` and `plan.json`,
so the folder is complete for an auditor; their hashes are in `session_start`, and `autonometry
timeline` checks the copies against them. The recorder alone writes `session_start`,
`session_end`, heartbeats, `seq` and `ts` (its receive time from the system UTC clock, never earlier
than the previous event's). `session_start` carries the config's scopes, weights, hint policy,
`simulated` and `unobserved`, the scopes' current states, and the SHA-256 of the device registry
(checked first), the task registry and the plan, each in RFC 8785 form; a registry or plan that is
not valid against its schema stops the start. Heartbeats every second carry the clock status: on Linux it is read from the kernel
with `adjtimex(2)` (unsynchronised gives `none`, else the configured `clock_source` and the kernel's
maximum error), elsewhere `unknown`.

**Level-triggered states.** A connector re-sends the state of each scope it observes on every
change and at least once a second; the recorder writes a transition only when the state, reason,
actor or device changes. A scope with no assertion for `gap_tolerance_s` (2 s) becomes `unknown`
(definitions.md 6.14). A scope belongs to the first connector that asserts it until that connector
stops asserting it for `gap_tolerance_s`; another connector's assertions meanwhile are dropped and
counted (`x_foreign_assertions` on the next heartbeat). Discrete events (`task_start`, `task_end`,
`hint`, `approval`, `help_request`, `supervision`, `support_start`, `support_end`) are written as
received; transitions and heartbeats carry the id of the open task.

**Loss is visible.** A gap in a connector's counter `n`, a datagram that is not a valid event, or
one over the rate limit (`max_events_per_s` per connector) makes every scope `unknown` at once,
because nothing says which scope it touched. Assertions restore the scopes only once a whole
heartbeat interval (1 s) has passed without another of these, so a flood writes at most one
`unknown` per scope per interval. A connector first seen with `n` above 0 kept running while the
recorder restarted: every scope is `unknown` too, but it is counted as `x_resumed_clients`, not as
lost events (the previous run mostly recorded them). The next heartbeat carries
`x_lost_client_events`, `x_rejected_client_events`, `x_rate_limited_events` and
`x_resumed_clients`; a step of the system clock backwards is clamped and reported as
`x_clock_stepped_back_s`.

**Durability.** Each event is one `write` on an `O_APPEND` descriptor. Events are fsynced at most
every `fsync_interval_s` and always before a checkpoint that covers them; the checkpoint file is
fsynced after every record. So a checkpoint, and any timestamp of it, never covers an event that a
power cut can lose. With `tsa_url` and `tsa_roots`, a background thread fetches an RFC 3161 token
for each new checkpoint; failures are retried with backoff and never block the writer.

**Crash recovery.** On start, if the newest session has no `session_end`, the recorder cuts a final
partial line (never complete, so no checkpoint covers it), replays the session verifying its hash
chain and checking its checkpoint file against the events, and continues it: a heartbeat with
`x_recovered: true` and `x_truncated_bytes`, every scope `unknown`, and a checkpoint. The downtime
is a gap and so `unknown` time. A session whose chain does not verify, or whose checkpoints
contradict its events (a checkpoint covering more events than the log holds, a different root, a
bad signature by the recorder's key), is left as it is and a new one starts: the recorder never
appends to a log that contradicts its own seals. A session whose config fields changed is closed
(`x_cause: "config_changed"`) and a new one starts. SIGTERM or SIGINT writes a final heartbeat,
`session_end`, the final checkpoint, waits up to 10 s for its token and exits 0. A log about to
exceed `max_log_bytes` ends its session (`x_cause: "rotation"`) and a new one starts with the
current states, followed by one transition per scope that is not `unknown` repeating its full value
(state, reason, actor, device); an open task carries over and the new `session_start` names it in
`x_carried_task`. Exit status: 0 after a clean stop, 1 on a failure while recording (a restart
continues the same session), 2 when it cannot start (config, key, registry, socket or folder in
use). Operational messages go to stderr and never contain event contents.

### Install

On the robot (Python 3.12 or newer):

```
sudo useradd --system --home-dir /var/lib/autonometry --shell /usr/sbin/nologin autonometry
sudo python3 -m venv /opt/autonometry && sudo /opt/autonometry/bin/pip install /path/to/autonometry
sudo install -d -m 0750 -o autonometry -g autonometry /etc/autonometry
sudo -u autonometry /opt/autonometry/bin/autonometry keys generate \
    --name autonometry/tb4_sim_01 --out /etc/autonometry/tb4_sim_01.key
/opt/autonometry/bin/autonometry registry check /etc/autonometry/devices.json
```

Add the connectors' users to the `autonometry` group so they can reach the socket, and give
auditors `tb4_sim_01.key.vkey` (never the key).

### A config for the TurtleBot 4 simulator

`/etc/autonometry/recorder.toml` (relative paths are relative to this file; unknown keys are an
error; spec parameters such as `gap_tolerance_s` come from the spec, never from here):

```toml
robot_id = "tb4_sim_01"
data_dir = "/var/lib/autonometry"
socket_path = "/run/autonometry/recorder.sock"
socket_group = "autonometry"          # socket 0660 for this group; without it, 0600
scopes = ["whole_robot"]              # the default; add scope_weights = { ... } for several
hint_policy = "hint_as_shared"        # the default
simulated = true                      # Gazebo: reports never pool it with real runs
unobserved = []                       # signal classes the connectors cannot see on the platform
clock_source = "ntp"                  # ntp, ptp or gnss: what keeps this computer's clock in sync
key_path = "tb4_sim_01.key"
device_registry = "devices.json"      # required: every source that can command the robot (6.10)
task_registry = "tasks.json"          # required: the tasks declared before the session (section 1)
plan = "plan.json"                    # required: pre-registered human actions, may be empty (3)
# tsa_url = "https://freetsa.org/tsr" # optional RFC 3161 anchoring, together with tsa_roots
# tsa_roots = "freetsa.pem"
# role = "station"                    # a teleoperation station's recorder, with station_id (12.1)
# station_id = "operator_pc_1"
# Defaults: fsync_interval_s = 1.0, max_events_per_s = 1000, max_log_bytes = 268435456
```

### systemd

`/etc/systemd/system/autonometry-recorder.service`:

```ini
[Unit]
Description=Autonometry recorder
After=time-sync.target network-online.target
Wants=network-online.target

[Service]
User=autonometry
Group=autonometry
ExecStart=/opt/autonometry/bin/autonometry record --config /etc/autonometry/recorder.toml
Restart=always
RestartSec=2
# The final checkpoint waits up to 10 s for its timestamp token.
TimeoutStopSec=20
RuntimeDirectory=autonometry
RuntimeDirectoryMode=0750
StateDirectory=autonometry
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/autonometry
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=yes
LockPersonality=yes
# No ProtectClock= and no SystemCallFilter= without @clock: they block the read-only adjtimex(2)
# call that reports the clock status, and every heartbeat would then say "unknown".

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now autonometry-recorder`. For bursts of many small events, raise
`net.unix.max_dgram_qlen` (sysctl) so the socket queue holds more datagrams.

### Wire format

Connectors in any language can implement it: one UTF-8 JSON object per Unix datagram (`SOCK_DGRAM`)
sent to `socket_path`, at most 65536 bytes:

```json
{"client": "nav_connector", "n": 41, "event": {"type": "transition", "scope": "whole_robot",
 "state": "ai", "reason": null, "actor_id": "ai:nav2", "device_id": "nav2"}}
{"client": "nav_connector", "n": 42, "distance_m": 1234.5}
```

- `client`: the connector's name, an identifier (1 to 128 ASCII letters, digits, `.`, `_`, `:`,
  `-`). One name per connector process; a scope belongs to one connector at a time.
- `n`: counts every datagram the connector *tried* to send, from 0, including those it could not
  send; it restarts at 0 when the connector restarts. A gap tells the recorder what it lost.
- `event`: an event of `spec/v0.3/event.schema.json` without `robot_id`, `session_id`, `seq`, `ts`,
  `prev_hash` or `hash`, of type `transition` (a level-triggered assertion: send it on every change
  and at least once a second) or one of the discrete types above. `task_id` is optional: the
  recorder fills in the open task's id. Types `session_start` and `session_end`, and scopes the
  config does not declare, are rejected. A `heartbeat` may carry only `x_` fields that the
  recorder does not write itself (for example a connector's `x_publishers`): the recorder writes a
  heartbeat of its own at once, with its clock status and distance, and those fields added.
- `distance_m`: instead of `event`, an odometer reading in metres that never decreases while the
  connector runs; heartbeats carry the distance travelled since the session started.

Send without blocking and never wait for an answer: there is none. For Python 3.8 or newer, copy
[`clients/python/autonometry_client.py`](clients/python/autonometry_client.py) (standard library
only; every call returns at once and never raises):

```python
from autonometry_client import Recorder

rec = Recorder("/run/autonometry/recorder.sock", "nav_connector")
rec.state("whole_robot", "ai", actor_id="ai:nav2", device_id="nav2")  # on change and every second
rec.event("task_start", task_id="t17", task_definition_id="dock_v1")
rec.distance(odometer_metres)
rec.dropped  # events dropped: recorder down, socket full, or a value not serialisable
```

### Verify the logs

Copy the session folders off the robot and verify them with the vkey. Keep a copy of the
checkpoint files somewhere the robot cannot change (or configure a TSA) so that they anchor the log:

```
autonometry validate /var/lib/autonometry/tb4_sim_01/*/events.jsonl
autonometry verify /var/lib/autonometry/tb4_sim_01/*/events.jsonl --vkey tb4_sim_01.key.vkey \
    --anchor copy/000042-20261112T083000Z-7f3a/events.checkpoints.jsonl     # or --tsa-root freetsa.pem
```

## TurtleBot 4

`autonometry.connectors.tb4` and the ROS 2 package `connectors/ros2/autonometry_ros` observe a
TurtleBot 4 (ROS 2 Jazzy, Nav2, Create 3 base) and send its control events to the recorder. The
observer only subscribes: it never publishes on a robot topic, calls a service or sends an action
goal (it even turns off its `/rosout` publisher and parameter services), so it cannot slow or stop
the robot, and nothing on the robot depends on it. A simulator demo for Windows 11 with WSL2 is in
[`demo/tb4-sim/`](demo/tb4-sim/README.md) (untested; see there).

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
| Nav2 `NavigateToPose`, `NavigateThroughPoses` or `FollowWaypoints` goal | `EXECUTING` with no task: `task_start` (`nav_to_pose`, `nav_through_poses`, `patrol`) and `ai`; another task action's goal inside the task (the waypoint follower's) is part of it; `SUCCEEDED`, `ABORTED`, `CANCELED`: `task_end` `success`, `failure`, `aborted`; a person's new goal or pose estimate: a hint (`shared` under `hint_as_shared`); a person's cancel: `shared` until the task ends (6.2, 6.8); collision monitor STOP: `safety_stop` with no actor (automatic) |
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

**Who sent it.** On ROS 2 Jazzy a subscription's message info has no publisher GID (rclpy adds it
only in later releases, and the observer uses it when present). The observer therefore maps
publishers from the ROS graph, refreshed when a topic's publisher count changes and every second,
and records the map once per change as `x_publishers` on a heartbeat. A topic with one publisher
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

## LeRobot

`autonometry.connectors.lerobot` measures robots run by Hugging Face
[LeRobot](https://github.com/huggingface/lerobot), such as the SO-101 arm, in two ways: **live**,
from the log `lerobot-rollout` writes as it runs, and **after the fact**, from a
LeRobotDataset's per-frame `intervention` labels. Both follow LeRobot v0.6.1 (commit
`7e241bd6`); the log formats and dataset column names are copied from that source, and the code
cites the file and line of each. Neither path opens the robot's serial port or imports anything
into the control process, so neither can slow or stop the arm. Install the extra:
`pip install 'autonometry[lerobot]'` (pyarrow, watchfiles, psutil).

**Sources.** LeRobot has no ROS graph. The connector matches the components the rollout names
against the device registry's `match.node`: `policy:<--policy.path>` for the policy,
`teleop:<--teleop.type>` for the teleoperator, and `script:return_to_initial_position` for
LeRobot's own move back to the start pose. A source no device matches is sent without
`device_id`, and the loader counts it as `human_remote` (6.10). Example registries for an SO-101
(the leader arm local, a phone or network teleoperator remote, the policy, the return motion) and
a recorder config are in [`connectors/lerobot/`](connectors/lerobot/).

**Live: `autonometry lerobot watch`.** Start the recorder, then the watcher, then the rollout
with its output in a file. `PYTHONUNBUFFERED=1` matters: the re-record key's line is a `print`,
which Python otherwise holds in its buffer while the output goes to a pipe or a file.

```
autonometry record --config connectors/lerobot/recorder.toml &
autonometry lerobot watch --config connectors/lerobot/recorder.toml --log rollout.log \
    --task "Pick up the cube and place it in the bin." &
PYTHONUNBUFFERED=1 lerobot-rollout --strategy.type=dagger --strategy.record_autonomous=true \
    ... 2>&1 | tee rollout.log
```

The watcher, like the recorder, runs on Linux and macOS. It reads the recorder's config for its
socket, scopes and registries. It follows the log like `tail -F`: an existing log is read from
where it ended when the watcher started, unless you pass `--from-start`, and a truncated or
replaced log is read from its start. It sends the state every 0.25 s. `--task` is the rollout's
`--dataset.single_task`, matched against the task registry's `description` (or a
`task_definition_id`); it can be omitted when the registry declares one task.

**The rollout's process.** The watcher also watches the rollout's process, read-only (psutil; it
never signals or touches it), about once a second. A rollout killed without a word (kill -9,
out of memory, a native crash, its window closed) then becomes `unknown` instead of keeping its
last state. The watcher reads what is left in the log first, so the rollout's last lines still
count. Then, unless the rollout logged `Rollout finished`, it does what that line does: it closes
the open task with outcome `unknown`, makes every scope `unknown`, notes
`x_lerobot_process_exit: unlogged` on a heartbeat and logs a warning. The process is the one
`--pid` names. Without `--pid`, the watcher looks for it when it sees a rollout start, or joins
one mid-run: the process on this computer whose program is `lerobot-rollout`, its `.exe`, or a
Python running that script or `-m lerobot.scripts.lerobot_rollout`. A launcher and the Python it
starts (`uv run`, the Windows `.exe` launcher) count as one rollout, and the deepest is watched.
With none or several, the watcher warns once, notes `x_lerobot_liveness: unwatched` on a
heartbeat, and the log alone decides. A log written on another computer cannot be watched this
way.

**Logs written on Windows.** These commands, tested with Windows PowerShell 5.1 and cmd on
Windows 11, give a log the watcher reads in full:

```
# PowerShell (5.1 or 7)
$env:PYTHONUNBUFFERED = "1"
lerobot-rollout ... 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath rollout.log

:: cmd
set PYTHONUNBUFFERED=1
lerobot-rollout ... > rollout.log 2>&1
```

PowerShell 5.1 writes the file as UTF-16 with a byte-order mark, and Python writes a redirected
stream in the Windows code page. The watcher honours a byte-order mark and accepts LeRobot's dash
in any code page. Without `ForEach-Object { "$_" }`, PowerShell 5.1 turns each line of stderr
into an error record and wraps the long ones at the console width. That cuts the line that names
the policy. The captures are in `tests/fixtures/lerobot_windows/`. The recorder, and so the
watcher, does not run on Windows yet. PowerShell 7 was not tested.

Rules (dossier 14.1 under spec v0.3; the first that applies decides):

| Observed in the log | State or event |
|---|---|
| nothing that tells the state yet | no assertion: the recorder logs `unknown` |
| `Rollout finished`, or the rollout's process gone | `unknown`: nothing is observed any more |
| the rollout started (`Building rollout context...`), its loop not yet | `idle` |
| DAgger `AUTONOMOUS`, and the policy loops of `base`, `sentry`, `highlight` and `episodic` | `ai`, actor `ai:<policy path>` |
| DAgger `PAUSED` (`Phase transition: autonomous -> paused`) | `shared`, `human_initiated`: a human pause (6.8) |
| DAgger `CORRECTING` | `human_local`, or `human_remote` when the teleoperator's device is declared remote (6.10) |
| episodic `Reset the environment` with a teleoperator | `human_local` or `human_remote`, `planned` with the plan's `reset` item (6.3) |
| `Returning robot to initial position`, or an episodic reset without a teleoperator | `scripted`, actor `script:return_to_initial_position` (6.9, 6.13) |
| a control loop ended, the robot disconnected | the last controller's state, then `idle` after `idle_threshold_s` (6.14) |

Episodes are tasks (5): DAgger with `record_autonomous=true` and sentry open one with the control
loop and at each `Episode saved`; episodic opens one at `Recording episode N` and closes it at
`Reset the environment`. The outcome is `unknown` (LeRobot records no success), or `aborted` when
the re-record key was pressed during the episode. A re-record seen only after the episode closed
is noted as `x_lerobot_rerecorded_task` on a heartbeat. DAgger's corrections-only episodes are
clips of corrections, not units of work, so they open no task; declare `unobserved = ["tasks",
...]` for such runs. The log's own local time (1 s resolution, zone not recorded) travels as
`x_src_ts` with `x_src_clock` `wall`; `ts` is the recorder's. When the watcher stops it sends
`unknown`. When a rollout is unwatched and dies without a word, its last state stays until the
watcher stops, so stop the watcher, or the recorder, when such a rollout ends.

**After the fact: `autonometry lerobot import`.**

```
autonometry lerobot import DATASET_DIR --out logs --registry connectors/lerobot \
    --policy outputs/train/act_so101/checkpoints/last/pretrained_model --teleop so101_leader \
    [--robot-id so101_01] [--start 2026-10-19T06:10:24Z] [--simulated]
autonometry report logs/*/*/events.jsonl --out report
```

It reads a LeRobotDataset v3.0 recorded by DAgger (`meta/info.json`, `meta/episodes/`,
`data/chunk-*/file-*.parquet`) and writes one log per episode, `logs/<robot_id>/<session_id>/
events.jsonl`, with the registries copied beside it. Each episode is its own session, since the
time between episodes was never observed. The log holds `task_start` at frame 0, a transition at
each change of `intervention` (true: the teleoperator's state; false: `ai`), `unknown` for
missing frames or labels, and `task_end` with outcome `unknown`. Timestamps are rebuilt as an
anchor plus `frame_index / fps`: `--start` for the first episode, with episodes back to back, or
by default each data file's modification time as the end of its last episode. Every event
carries `x_ts_reconstructed: true`, so the logs are trust level L1 at most (8, 10.6). The
sessions declare `unobserved: ["safety_stops", "human_inputs"]`: the SO-101 has no safety
function, and pauses and resets are never recorded. Episodes listed in the metadata whose data
file is missing are named and skipped. A task text not in the task registry gets an id
`lerobot_task_<hash>`, and its tasks are flagged `unknown_task_definition`.

**Demo.** [`demo/lerobot/run_demo.py`](demo/lerobot/README.md) downloads the metadata and first
data files of a public DAgger dataset from the Hugging Face Hub, using the standard library only.
It prints the size first and stops above 100 MB. Then it imports and reports: a time autonomy
share of 0.779, 95 % CI [0.668, 0.891] over 18 episodes, and 20 interventions, as in the dossier.

**Deferred.** Bus taps on real hardware: a UART receiver on the leader and follower buses for
leader polling, torque writes and follower commands (integration notes part B). Capture of
asynchronous inference (`policy_server` over gRPC on port 8080). `lerobot-replay`, which would be
`scripted` replay of recorded actions. The Foxglove WebSocket tap. HIL-SERL's `is_intervention`,
which no dataset keeps. `lerobot-record` teleoperation logs.

## Unitree G1

`autonometry.connectors.g1` and `autonometry g1` observe a Unitree G1 humanoid (EDU, 29 DoF, with or
without hands) on its DDS network and send the control state of `legs`, `upper_body` and, when
the robot has hands, `hands` to the recorder. The design and its sources are in
[`research/unitree-g1/`](research/unitree-g1/g1_integration_notes.md).

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

**Install.** Cyclone DDS 0.10 has no wheels for Python 3.12, so build the C library from the
0.10.5 tag and the Python binding from source, as Unitree's Python SDK does for 0.10.2:

```
git clone --depth 1 --branch 0.10.5 https://github.com/eclipse-cyclonedds/cyclonedds.git
cmake -S cyclonedds -B cyclonedds/build -DCMAKE_INSTALL_PREFIX="$HOME/cyclonedds" \
    -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF
cmake --build cyclonedds/build --target install --parallel
export CYCLONEDDS_HOME="$HOME/cyclonedds"
pip install --no-binary cyclonedds cyclonedds==0.10.5
pip install 'autonometry[g1]'          # psutil, to name Python programs by their script
```

Keep `CYCLONEDDS_HOME` set when you run the watcher. Cyclone DDS is not a declared dependency of
autonometry, so `pip install autonometry` never pulls a version that would harm the robot.

**1. Discover.** With the robot and its programs running:

```
autonometry g1 discover --interface eth0            # --domain 0 by default; --json; --seconds 5
```

It lists every participant with its host, program and pid, the topics and type names it
publishes, and for each writer the exact `match` to copy into the device registry. A writer is
observed as `(host, participant, topic)`: `host` is the participant's `__Hostname`; `participant`
is its `__ProcessName`, except that a Python interpreter (`python`, `python3`, `python3.x`) on the
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
sends every scope's state every 0.25 s and on each change, and records the active writers with
their identities as `x_g1_writers` on a heartbeat when they change. Host names and script names go
into the log: choose host names that name no person. Tasks are the robot program's: it sends
`task_start` and `task_end` to the recorder through the client library, and the watcher follows
the recorder's current session log, read-only, to know whether a task is open. If it cannot read
that log, every moment counts as outside a task, and it says so once on a heartbeat
(`x_g1_tasks: "unreadable"`); run it as the recorder's user.

Rules (notes section 6 under spec v0.3; for each scope, the first that applies decides):

| Observed | legs, upper_body | hands |
|---|---|---|
| no `rt/lowstate` for more than `gap_tolerance_s` | `unknown` | `unknown` |
| the remote's L2 + B, or Damp or ZeroTorque (7101, data 1 or 0) from a person or an undeclared source, followed within `gap_tolerance_s` by a passive state (at once while the built-in controller is unseen or released) | `safety_stop`, the person as actor, until the state leaves passive or the next change of mode (6.7, 6.8) | the same |
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
or the controller unseen) ends only at a change of mode; a stick value that is not a number counts
as input; a writer whose weight cannot be read counts as holding its joints; a malformed velocity
only extends a goal span; a response only ever keeps the robot released or in user control (a
refused SelectMode or SwitchToInternalCtrl, or a CheckMode answer naming no mode), never takes it
out. Each is marked `Open case` in the code.

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

## Station witness (trust level L3)

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
  joystick, and only subscribes, like the robot's observer. Configs:
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

## Supported platforms

Python 3.12 or newer on:

- Linux x86-64 and ARM64, with glibc or musl
- macOS
- Windows (everything except the recorder, which needs Linux or macOS)

`jsonschema-rs` ships binary wheels for these. On other platforms, such as 32-bit ARM, installing it
needs a Rust toolchain to build it from source.

## Links

- [`spec/`](spec/): the normative definitions, the event JSON Schema and the machine-readable rules (current draft: v0.3). The wheel ships this folder as `autonometry/spec/`.
- [`research/`](research/): design-phase material, for reference only. It is not part of the package.
- [`CHANGELOG.md`](CHANGELOG.md), [`SECURITY.md`](SECURITY.md), [`RELEASING.md`](RELEASING.md)

## Development

```
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
uv run pytest -m slow --no-cov    # slow checks, such as memory use with a million rejected lines
uv run --no-sync pytest -m dds --no-cov tests/dds   # with Cyclone DDS 0.10.5 built (Unitree G1)
```

## Licence

Apache-2.0. See [`LICENSE`](LICENSE).
