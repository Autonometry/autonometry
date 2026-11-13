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
log that was sealed on time but anchored late, and shows up as `late_anchor`. Trust levels L3 (a
second, independent witness) and L4 (hardware-rooted keys) follow in later spec versions.

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
`--drift-bin`, `--spec`), plus `--title` (plain text, at most 200 characters) and `--about` (a
plain-text UTF-8 file of at most 64 KiB describing the deployment). Both are shown escaped, never as
markup. It makes no network call.

What the report holds, in the order of the page:

- **Verdict** (at the top): trust status (`verified`, `not verified` or `FAILED`) and level,
  `complete` or `incomplete`, the census result, how many sessions were left out, and per group the
  four headline numbers (time autonomy share, task autonomy rate, interventions per active hour,
  human minutes per task) with their confidence interval and outer interval.
- **Trust**: each session's status and level (L2 only for a `verified` session) with its evidence
  and gap codes; rejected lines and file problems. The report's level is the lowest of its
  sessions'; it is verified only when every session is and nothing was rejected. When a session
  is `failed`, the top says so and the session **stays in the metrics**: leaving it out would reward
  corrupting a bad session. The report says when no trusted key or TSA root was given; L3 is shown
  as not available yet.
- **Census**: for session ids of the recorder's form (`000042-20261112T083000Z-7f3a`), the counters
  missing between each robot's lowest and highest; other id forms are listed as not checked.
- **Exclusions**: every session left out of the metrics and why (a registry that does not match
  its hash, no `session_start`, or a session named only by a checkpoint or anchor).
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
  estimate ≤ `unknown_as_ai`), witnesses (not available until L3).
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
when a session failed. Exit status: 0 only when the report is clean: verified at L2, complete, no
census gap, no session left out and no invariant failed; 1 when it was written but is not clean
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
  recorder fills in the open task's id. Types `session_start`, `session_end` and `heartbeat`, and
  scopes the config does not declare, are rejected.
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
```

## Licence

Apache-2.0. See [`LICENSE`](LICENSE).
