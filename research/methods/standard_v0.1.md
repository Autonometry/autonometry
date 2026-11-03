# Autonomy Reporting Standard v0.1 (draft)

Status: draft for public comment. Date: 2026-10-30.
Normative companions: `definitions.md` (taxonomy v0.1), `event.schema.json` (JSON Schema draft 2020-12), `trust.md` (trust levels), `gaming.md` (checks). Evidence base: `methods_note.md` (prior art, estimator validation, real-data re-analyses).

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be read as in RFC 2119 / RFC 8174 (https://www.rfc-editor.org/info/rfc2119, https://www.rfc-editor.org/info/rfc8174).

## 0. Purpose and scope

This standard specifies how a party that operates robots (the *reporter*) records who is controlling a robot at every instant, how it computes a small set of autonomy metrics from that record, how it states the uncertainty of each metric, and how a third party can check that the record was not altered. It applies to any robot whose control can pass between an automated system and one or more humans, whether the humans are co-located or remote: mobile manipulators, humanoids, warehouse and delivery robots, vehicles under test, surgical and inspection robots.

The standard measures *who controlled the robot*, not whether the robot was safe or useful. Task success is recorded because autonomy claims are conditioned on it, but the standard does not define success criteria beyond requiring that they be pre-registered.

Out of scope: certification of control software, safety cases, the content of task definitions.

## 1. Definitions (normative summary)

Full text in `definitions.md`; the following are the terms used in this document.

1.1 **Scope**: a control channel of the robot (default `whole_robot`; optionally named body parts or subsystems such as `left_arm`, `base`). Every scope is in exactly one control state at every instant of a session.

1.2 **Control states** (exclusive per scope):

| state | meaning |
|---|---|
| `ai` | the automated system is issuing the control commands |
| `human_local` | a co-located human is issuing control commands (joystick, lead-through, pendant) |
| `human_remote` | a human not co-located with the robot is issuing control commands |
| `shared` | commands are a blend or arbitration of AI and human input, or a human hint is in force |
| `safety_stop` | motion is inhibited by a safety function or an emergency stop |
| `idle` | the robot is powered and not executing a task and no one is controlling it (after `idle_threshold_s`) |
| `unknown` | the log cannot determine the state (gap, dropout, undeclared scope) |

1.3 **Transition**: a timestamped change of state on a scope, carrying a `reason` in {`ai_requested_help`, `human_initiated`, `safety`, `planned`} and, for human states, an `actor_id`.

1.4 **Intervention**: a transition into `human_local`, `human_remote` or `shared` from `ai` (a *takeover*), or a discrete `approval` or `hint` event. Planned transitions (declared in the pre-registered plan, e.g. a scheduled reset) are human time but are not interventions. A `safety_stop` is neither AI nor human time and is reported separately.

1.5 **Active time**: time in any state other than `idle` and `unknown`.

1.6 **Task**: a pre-registered unit of work with `task_start` and `task_end` events and a `task_definition_id` from a registry whose hash is declared at `session_start`. Tasks MUST be declared before the session begins.

1.7 **Session**: a contiguous period of observation of one robot opened by `session_start` and closed by `session_end`. A session SHOULD NOT exceed one day of operation.

1.8 **Edge-case rules** (binding; details in `definitions.md` section 6): an approval click is one intervention and adds `max(dwell, approval_min_dwell_s)` of human time, with a 5 s sensitivity figure always reported; a hint places the scope in `shared` for `hint_ttl_s` unless the reporter declares `hint_as_event`; a reset between tasks is human time and is excluded from task-level metrics only when pre-registered as `planned`; supervision without control is not a control state and is reported as `supervision_ratio`; with several humans the whole-robot state is human if any scope is human-controlled (the *any-human rule*) and a scope-weighted share is reported alongside; per-body-part control uses named scopes.

## 2. Event format

2.1 The record is a JSON Lines file, one event per line, each conforming to `event.schema.json` (`$id` https://autonomy-meter.org/schemas/event.schema.json, draft 2020-12).

2.2 Event types: `session_start`, `session_end`, `transition`, `approval`, `hint`, `help_request`, `task_start`, `task_end`, `supervision`, `heartbeat`.

2.3 Common fields on every event: `robot_id`, `session_id`, `task_id` (nullable), `seq` (monotone integer per session), `ts` (RFC 3339 UTC with `Z`), `type`. Optional on every event: `distance_m` (cumulative, non-decreasing), `prev_hash`, `hash`, `note`.

2.4 `session_start` MUST declare `scopes`, `initial_states`, `heartbeat_interval_s`, `hint_policy`, `task_registry_hash` and `plan_hash`. `heartbeat` events MUST be emitted at least every `heartbeat_interval_s`; a silence longer than `heartbeat_interval_s + gap_tolerance_s` is `unknown` time.

2.5 A reporter MAY add fields under an `x_` prefix. Unknown fields without the prefix are a schema error.

2.6 The reference loader (`autonomy_meter.events`) MUST accept any conforming log and MUST emit the flags listed in `definitions.md` section 8 (`time_gap`, `seq_gap`, `ts_regression`, `missing_initial_state`, `undeclared_scope`, `nested_task`, `task_not_closed`, `distance_regression`, ...). Flag counts are part of the report.

## 3. Required report fields

A conforming report is a JSON document (reference: `autonomy_meter.full_report`) with the following sections. Every quantity listed as *required* MUST be present even when it is zero or undefined (then with an explicit `null` and a reason).

### 3.1 Provenance

`standard_version` (`0.1`), `reporter`, `robot_ids`, `period_start`, `period_end`, `n_sessions`, `n_robots`, `n_days`, `n_tasks`, `task_registry_hash`, `plan_hash`, `params` (every parameter of `definitions.md` section 9 with its value), `software` (name and version of the implementation), `trust_level` (section 5), `log_digest` (SHA-256 of the event log or the final checkpoint hash).

### 3.2 Exposure and completeness

`observed_s`, `active_s`, `idle_s`, `unknown_s`, `safety_stop_s`, `distance_m` (if available), `unknown_share = unknown_s / observed_s`, `completeness` (`complete` if `unknown_share <= unknown_share_max`, else `incomplete`), `flags` (counts by code), `supervision_ratio`, `supervisor_hours_per_robot_hour`, `n_human_actors`, `human_actor_hours`.

### 3.3 Metrics

Each metric is reported as an object `{estimate, ci_lower, ci_upper, alpha, method, cluster_unit, n_clusters, lower_bound, upper_bound, outer_lower, outer_upper, secondary: [...]}`.

| id | metric | definition | required breakdowns |
|---|---|---|---|
| (a) | time autonomy share | `ai_s / active_s` on the whole-robot any-human timeline | variants `with_approvals`, `shared_as_ai`, `scope_weighted`; `safety_stop_share`, `idle_share_of_observed` |
| (b) | task autonomy rate | tasks with zero interventions / tasks | `autonomous_and_successful`, `success_rate_all_tasks`; by `task_definition_id` |
| (c) | interventions per active hour | (takeovers + approvals + hints) / active hours | by `reason`, by `kind` (takeover/approval/hint), `safety_stops_per_active_hour`, narrow DMV-like count (`human_initiated` + `safety` takeovers only) |
| (d) | gap between interventions | active hours (and metres when `distance_m` is present) between consecutive interventions; the last gap in each session is right-censored | KM median, `S(h)` at declared horizons, Weibull shape as burstiness indicator |
| (e) | human minutes per task | mean over tasks of human control time + approval/hint dwell (+ unplanned resets) | median; share of tasks with zero human minutes; by `task_definition_id` |
| (f) | breakdowns | (c) by reason and kind; (b) and (e) by task type | — |
| (g) | drift | (c) by calendar bin with trend test and CUSUM first-signal bin | bin width declared |
| (h) | unmeasured | `unknown_share`, bounds for (a) and (b) | — |

### 3.4 Exclusions

Every session, task or robot excluded from the report MUST be listed with its identifier and a reason. Session identifiers MUST be a contiguous counter per robot so that omissions are visible (`gaming.md` invariant 4).

### 3.5 Invariants

The report MUST state the result of the eight invariant checks of `gaming.md` section 3 (partition, chain, time, census, tasks, floors and caps, bounds, witnesses).

## 4. Required uncertainty reporting

4.1 Every metric in 3.3 MUST carry a two-sided 95 % confidence interval (`alpha = 0.05`), the name of the interval method, the clustering unit and the number of clusters. The clustering unit MUST be the highest level at which the claim is meant to generalise: `session` for a claim about this fleet in this period, `robot` for a claim about the robot type, `deployment` for a claim across sites. Simulation (methods note section 6.1) shows session-level intervals lose coverage (0.67-0.73 at 80 sessions) when the claim is about new robots or days.

4.2 Default interval methods (selected by the coverage study in `simulation/`; coverage closest to 0.95 across Poisson, over-dispersed, bursty, clustered and drifting scenarios). Alternatives listed as *secondary* MUST also be reported when they disagree with the default by more than a factor of 2 in width.

| metric | default | secondary | do not use as primary |
|---|---|---|---|
| (a) time share | ratio-linearised t interval over clusters (`ratio_linearised_ci`) | cluster bootstrap percentile when n_clusters >= 20 | Wilson with effective n (single-cluster fallback only; label it) |
| (b) task rate | cluster-robust binomial GLM (sandwich, clusters = sessions or higher) when n_clusters >= 20; Wilson score below | Clopper-Pearson | Wilson as primary when tasks nest in clusters with ICC > 0 |
| (c) interventions per hour | negative-binomial (NB2) Wald on log rate with offset log(exposure), multi-start fit; Poisson exact (Garwood) only when the dispersion MLE is at zero | cluster bootstrap percentile (n_clusters >= 20); Garwood always shown for comparison | Garwood as primary when dispersion index > 1.5 (coverage 0.72 under dispersion ~3) |
| (d) gap median | Kaplan-Meier median with exponential-Greenwood CI, labelled *descriptive* | — | exponential-model CI (coverage 0.18 under over-dispersion) |
| (e) human minutes per task | cluster bootstrap-t with cluster-robust SE (resampled clusters kept distinct) | percentile bootstrap when n_clusters >= 20 | — |
| (g) drift | Poisson or NB GLM Wald on the log rate ratio per bin; NB when dispersion index > 1.5 | CUSUM (k = 0.5, h = 4) first-signal bin | — |

4.3 **Bounds for unmeasured time.** For (a) and (b) the report MUST give the lower bound (all `unknown` counted as human / tasks with unknown gaps counted as not autonomous), the upper bound (all `unknown` counted as AI), and the *outer interval* (lower CI limit of the lower bound to upper CI limit of the upper bound). Simulation with informative dropout (hidden takeovers, 10 % unknown) shows every sampling interval misses the truth while the outer interval covers it. For (c) and (e) no upper bound exists when events can be hidden; the report MUST state this when `unknown_share > 0`.

4.4 **Minimum reporting units.** A metric computed from fewer than 2 clusters MUST be labelled `single_cluster` and its interval `not_generalisable`. A rate based on fewer than 5 events MUST be shown with the one-sided upper bound in addition to the two-sided interval.

4.5 **Zero-event claims.** A claim of the form "rate below lambda_max" after T hours with zero interventions MUST state which structure it assumes. Under independent sessions the exposure required is `T = -ln(alpha) / lambda_max` (the rule of three at alpha = 0.05). When the test is one robot or one deployment (shared frailty), the exposure is `T = (alpha^(-a) - 1) / (a lambda_max)` for between-unit NB dispersion `a`; the rule of three is wrong 12-25 % of the time at the threshold for a in [0.3, 1.0] (simulation, section 7). Reporters MUST declare `a` (from a prior period or a stated default of 0.5) and the structure.

4.6 **Test-length planning.** Claims of a *level* (e.g. "task autonomy rate above 90 %") MUST be accompanied by the sample size at which the one-sided 95 % Clopper-Pearson (tasks) or Garwood (events) bound crosses the threshold, inflated by the design effect `1 + (m - 1) ICC` for clustered tasks (ArmnetBench re-analysis: design effects 6-16 with 8 tasks).

4.7 Point estimates MUST NOT be reported as "miles per disengagement", "hours per intervention" or any other inverse-rate whose value is undefined at zero events; rates per unit exposure with intervals are the normative form.

## 5. Trust levels L1-L4

Full specification in `trust.md`. Each level includes the requirements of the levels below it.

| level | name | mechanism | what it rules out | report flag |
|---|---|---|---|---|
| L1 | self-reported | schema-conformant log and report; no signatures | nothing beyond schema errors | `trust_level: 1` |
| L2 | sealed log | per-event SHA-256 hash chain (`prev_hash`/`hash`); Merkle checkpoints in the C2SP checkpoint format signed by the reporter key; RFC 3161 timestamps on checkpoints at least daily; publication of checkpoints to a transparency log RECOMMENDED | post hoc editing, dropping, re-ordering or re-timing after a checkpoint; gaps become bounded `unknown` time | `trust_level: 2`, `checkpoint_count`, `tsa_count` |
| L3 | two witnesses | independent robot log and teleoperation-station log, each at L2 with distinct keys, cross-stamping each other's checkpoints, reconciled with clock tolerance `tau`; the reconciler reports `station_only_s`, `robot_only_s`, `disputed_s` | real-time mislabelling of remote control that passes through the instrumented station; clock manipulation beyond `tau` | `trust_level: 3`, `tau_s`, `disputed_share` |
| L4 | hardware-rooted and externally witnessed | L3 with keys held in a TPM 2.0 / secure element and RFC 9334 attestation of the logger binary on both witnesses; plus at least one of: a network witness run by the customer or auditor, or independent witness cosignatures on all checkpoints | key cloning, logger replacement, split views; the network witness adds an independent lower bound on live remote control | `trust_level: 4`, `attestation_ok`, `witness_type` |

5.1 Downgrade rules: an L3 period with `disputed_s / active_s > 0.02` or `station_only_s / active_s > 0.005` is published at L2 with the discrepancy stated; an L2 period containing a chain break or a missing timestamp is published at L1 for the affected span.

5.2 Clock tolerance `tau` MUST be declared and justified from the synchronisation method (NTP or PTP) and the measured offset dispersion; default 1 s. State intervals from the two witnesses that disagree by more than `tau` on a boundary are `disputed`.

5.3 The network witness (encrypted-traffic classification of live bidirectional low-latency control traffic) is **advisory** in v0.1. Its output MAY be included as a labelled lower bound on `human_remote` time with the calibration record (labelled sessions, recall `r`, false-positive rate `f`) attached. No primary study of teleoperation detection exists (UNKNOWN); accuracy estimates are extrapolations from cloud-gaming and IoT-activity classification (CLAIM) and are not conformance evidence.

5.4 What V1 implementations ship: L2 is REQUIRED for any report that uses the word "autonomy" in marketing or contractual material; the L3 station logger and reconciler MUST be available as software even if not deployed; L4 is OPTIONAL.

## 6. Anti-gaming requirements

Derived from `gaming.md`. A conforming report MUST include:

6.1 Pre-registered task registry and plan (hashes at `session_start`); task duration distribution per `task_definition_id` with a comparison to the previous period (task redefinition check).

6.2 `idle_share_of_observed` with the declared idle cap (default 0.5) and dual reporting (with and without idle) when exceeded (idle padding check).

6.3 `safety_stops_per_active_hour`, mean safety-stop dwell, and the `stop_dominated` flag when `safety_stop_s / active_s > 0.05` (stop-instead-of-takeover check).

6.4 Approval and hint counts per task, and the `with_approvals` share variant with the 5 s dwell sensitivity (assist relabelling check).

6.5 `human_remote` time, number of distinct remote `actor_id`s, and at L3 the disputed-time report (hidden remote operator check).

6.6 Chain and checkpoint verification results and the session census (log editing check).

6.7 `plan_received` events with a human author MUST be recorded as `human_remote` time of declared duration (store-and-forward planning check; carried as an open question for the schema, see `open_questions.md`).

6.8 Environment and task-mix descriptors (free text plus `task_definition_id` distribution): choosing an easy deployment is legitimate and the standard can only make it visible.

## 7. Conformance checklist

A report conforms to this standard at trust level Lk when every item below is satisfied; items marked (L2+), (L3+) apply from that level.

- [ ] C1 Event log validates against `event.schema.json` with zero errors; flag counts reported.
- [ ] C2 Every session has `session_start` and `session_end`; session ids are a contiguous counter per robot; exclusions listed with reasons.
- [ ] C3 Task registry and plan hashes declared at `session_start`; every `task_definition_id` is in the registry.
- [ ] C4 All parameters of `definitions.md` section 9 printed; deviations from defaults stated.
- [ ] C5 Partition invariant holds for every session within one event resolution.
- [ ] C6 Metrics (a)-(h) present with estimate, 95 % interval, method, cluster unit and number of clusters.
- [ ] C7 Default interval methods of section 4.2 used, or the deviation justified; secondary intervals shown when widths differ by more than 2x.
- [ ] C8 Bounds and outer interval for (a) and (b); the "no upper bound" statement for (c) and (e) when `unknown_share > 0`.
- [ ] C9 `unknown_share` and `completeness` reported; `incomplete` when above `unknown_share_max`.
- [ ] C10 Zero-event and level claims state the assumed structure, `a`, and the exposure or sample size per 4.5-4.6.
- [ ] C11 No inverse-rate point estimates (4.7).
- [ ] C12 Anti-gaming fields of section 6 present.
- [ ] C13 (L2+) Hash chain verifies end to end; signed checkpoints at least daily; RFC 3161 tokens verify; checkpoint publication location stated.
- [ ] C14 (L3+) Both witness logs provided; `tau` declared and justified; reconciliation report with `station_only_s`, `robot_only_s`, `disputed_s`; downgrade rules applied.
- [ ] C15 (L4) Attestation evidence for both loggers; external witness type and calibration record.
- [ ] C16 Software name and version; the report is reproducible from the log with the reference implementation or a documented equivalent.

## 8. Relationship to existing instruments

SAE J3016 levels describe design intent and the allocation of the dynamic driving task, not measured control time; California 13 CCR §227.50/§227.56 counts disengagements per autonomous mile without intervals, excludes deployment-permit operations and leaves the "requires" judgement to the filer; NIST ALFUS places human independence on a 0-10 scale; ISO 8373 defines autonomy as the ability to perform tasks without human intervention; IEEE 7001 requires time-stamped logging of inputs, commands and outputs for investigators but does not prescribe a control-state taxonomy or metrics. None of these specify interval methods, clustering, censoring, unmeasured-time bounds or tamper evidence. See `methods_note.md` section 1 and `prior_art.md` for the sourced comparison.

## 9. Versioning

This is v0.1. Breaking changes to the schema increment the minor version before 1.0. Reports MUST state the version. The parameter defaults in `definitions.md` section 9 and the interval defaults in section 4.2 are expected to be revised after the first public-comment period and after field calibration of `tau` and of the network witness.
