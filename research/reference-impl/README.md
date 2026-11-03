# autonomy_meter — reference implementation (v0.1.1)

Small Python package implementing the Autonomy Meter definitions
(`definitions.md`) and event format (`event.schema.json`). It turns a
JSON-Lines event log into state intervals and computes every metric of the
draft standard with a stated estimator and interval method. Statistical
primitives are taken from standard packages: numpy, pandas, scipy,
statsmodels, lifelines, jsonschema. No estimator is hand-rolled where a
package function exists; the only custom numerics are the cluster bootstrap
resampling loop, the Taylor-linearised ratio variance, and the CUSUM
recursion.

Layout

```
reference/
  definitions.md            taxonomy, edge-case rules, parameter defaults (normative text)
  event.schema.json         JSON Schema draft 2020-12 for one event (with 11 examples)
  autonomy_meter/
    events.py               read_jsonl, validate_events, verify_chain, build_tables, Params
    intervals.py            Wilson, Clopper-Pearson, Garwood, NB2, cluster bootstrap, cluster-robust GLM
    metrics.py              the metrics (a)-(h) and full_report
    planning.py             test-length planning and two-period power
  tests/                    pytest suite (44 tests, incl. tests/test_regressions_v011.py)
  examples/
    make_example_log.py     seeded synthetic log generator (594 events, 6 sessions, 2 robots)
    example_log.jsonl       the generated log (hash-chained)
    run_example_report.py   -> report_example.json
    report_example.json     example output
  pytest.ini, pytest_output.txt
```

## Install and run

```
pip install numpy pandas scipy statsmodels lifelines jsonschema pytest
cd reference
python -m pytest -q                      # 44 passed
python examples/make_example_log.py      # regenerate the example log
python examples/run_example_report.py    # write examples/report_example.json
```

## API

```python
import autonomy_meter as am

events = am.read_jsonl("examples/example_log.jsonl")
errors = am.validate_events(events)            # [] if every event conforms to event.schema.json
chain_ok, problems = am.verify_chain(events)   # prev_hash/hash chain, if present

params = am.Params(gap_tolerance_s=30.0)       # every parameter of definitions.md Section 9
tables = am.build_tables(events, params)       # Tables(sessions, intervals, whole, interventions, tasks, flags)

am.time_autonomy_share(tables)                 # (a)  cluster_level='auto'|'session'|'robot'
# every metric dict has estimate, ci (the default interval) and interval_method
am.task_autonomy_rate(tables)                  # (b)
am.interventions_per_hour(tables)              # (c)
am.gaps_between_interventions(tables, "time")  # (d)  or unit="distance"
am.human_minutes_per_task(tables)              # (e)
am.breakdowns(tables)                          # (f)
am.drift(tables, freq="D")                     # (g)
am.unmeasured(tables)                          # (h)
report = am.full_report(tables, alpha=0.05, n_boot=1000, seed=0)   # all of the above, JSON-serialisable

am.planning.hours_for_zero_events(0.1)         # 29.96 h for rate < 0.1/h at 95 % with zero events
am.planning.hours_for_zero_events_nb(0.1, 0.5) # 69.4 h under NB2 dispersion 0.5
am.planning.tasks_for_zero_failures(0.05)      # 59 tasks
```

`build_tables` returns pandas DataFrames:

| table | one row per | key columns |
|---|---|---|
| `sessions` | session | `observed_s, active_s, ai_s, human_s, human_local_s, human_remote_s, shared_s, safety_stop_s, idle_s, unknown_s`, scope-weighted `sw_*`, `n_interventions, n_interventions_whole, n_takeovers, n_approvals, n_hints, n_safety_stops, approval_human_s, approval_human_s_sens, reset_human_s, reset_planned_human_s, supervision_ratio, supervisor_hours_per_robot_hour, n_human_actors, human_actor_s, distance_m` |
| `intervals` | per-scope segment | `scope, state, start, end, dur_s, task_id, actor_id, reason` |
| `whole` | whole-robot segment (any-human rule) | `state, start, end, dur_s, task_id` |
| `interventions` | intervention or safety stop | `kind (takeover/approval/hint/safety_stop), reason, scope, ts, task_id, distance_m, dwell_s, simultaneous` |
| `tasks` | task | `task_definition_id, outcome, dur_s, human_s, human_s_incl_reset, unplanned_reset_s, n_interventions, has_unknown, autonomous, autonomous_lower, autonomous_upper, autonomous_success, success` |
| `flags` | loader warning | `code` in `time_gap, seq_gap, ts_regression, missing_initial_state, help_reason_recoded, undeclared_scope, nested_task, task_end_without_start, task_not_closed, distance_regression, no_session_start, no_session_end` |

## Metric -> estimator -> interval

| Metric | Definition | Estimator | Default interval | Also reported |
|---|---|---|---|---|
| (a) time autonomy share | `ai_s / active_s`, whole-robot any-human timeline | ratio of sums over clusters (sessions; robots when >= 3 robots, `cluster_level`) | ratio-linearised t over clusters (`ratio_linearised_ci`), clipped to [0, 1] | cluster bootstrap percentile; Wilson with effective n = number of control segments (single-cluster fallback); bounds `lower` (unknown as human) / `upper` (unknown as AI) and the `outer_interval`; variants `with_approvals`, `shared_as_ai`, `scope_weighted`; `safety_stop_share`, `idle_share_of_observed`, `reset_human_share_of_active` |
| (b) task autonomy rate | tasks with zero human events / tasks | sample proportion | cluster-robust Wald from identity-link binomial GLM with `cov_type='cluster'` (groups = session) when >= 20 sessions; Wilson score (`statsmodels.proportion_confint(method='wilson')`) below | Wilson and Clopper-Pearson (`method='beta'`) always; `lower`/`upper` bounds for unknown; `autonomous_success` and `success_rate_all_tasks` |
| (c) interventions per active hour | (takeovers + approvals + hints) / active hours | pooled count / exposure | NB2 Wald (`statsmodels NegativeBinomial`, offset log exposure, multi-start fit); labelled Poisson-exact fallback only with < 2 sessions, zero events, or a converged dispersion MLE at the zero boundary | Poisson exact (Garwood; `scipy.stats.chi2`); cluster bootstrap percentile; dispersion index; per-kind rates (`takeovers, approvals, hints, safety_stops`); whole-robot count |
| (d) gaps between interventions | active hours (or metres) between consecutive interventions; last gap per session right-censored | Kaplan-Meier (`lifelines.KaplanMeierFitter`) | median with CI from `median_survival_times(confidence_interval_)` (exponential Greenwood) | `S(h)` at horizons with CI; `ExponentialFitter` mean gap and rate (`rate = 1/lambda_`) with a log-scale Wald CI from `summary['se(coef)']`; `WeibullFitter` `rho_` with CI (rho < 1 = bursty); naive mean gap for comparison |
| (e) human minutes per task | mean over tasks of human control time + approval/hint dwell (+ unplanned resets) | sample mean | cluster (session) bootstrap-t with the cluster-robust SE as studentiser; t on tasks with a single session | cluster bootstrap percentile; median; share of tasks with zero human minutes |
| (f) breakdowns | (c) by `reason` and `kind`; (b)/(e) by `task_definition_id` | as above | Poisson exact per cell (common denominator); Wilson per task type | narrow DMV-like count (`human_initiated` + `safety` takeovers only) |
| (g) drift | interventions per hour by calendar bin | GLM log link, offset log(active hours), linear time index; Poisson, or NB with alpha from (c) when dispersion > 1.5 | Wald CI on rate ratio per bin, p-value | two-sided tabular CUSUM of standardised bin counts (`k = 0.5, h = 4`, phi-inflated), first signal bin |
| (h) unmeasured | `unknown_s / observed_s` | sum | — | conformance `complete`/`incomplete` vs `unknown_share_max`; flag counts; supervision ratio; human actor hours |

Planning (`planning.py`):

| Question | Function | Formula |
|---|---|---|
| hours of zero-intervention operation to claim rate < lambda_max at 1-alpha | `hours_for_zero_events` | `-ln(alpha)/lambda_max`; alpha = 0.05 gives 2.996/lambda_max = the rule of three |
| same with a rate frailty | `hours_for_zero_events_nb` | `(alpha^(-a) - 1)/(a lambda_max)` for gamma frailty a; exact for a frailty shared by the whole test (one robot/deployment), conservative for independent sessions |
| tasks with zero failures to claim p < p_max | `tasks_for_zero_failures` | `ceil(ln(alpha)/ln(1-p_max))`; clustered version inflates by `1+(m-1)icc` |
| exposure when some events are expected | `hours_to_demonstrate_rate`, `tasks_to_demonstrate_rate` | smallest T with one-sided Garwood (Clopper-Pearson) upper limit at the expected count below the threshold |
| compare two periods | `power_two_periods`, `hours_for_power_two_periods` | Wald test on log rate ratio with `var = 1/mu1 + 1/mu2 + 2 alpha_nb` |

Note on the two-period power: with NB dispersion the between-period variance term `2*alpha_nb` does not shrink with exposure, so a rate halving with `alpha_nb = 0.3` has power 0.14 at 100 h per period and cannot reach 80 % power at any exposure (`hours_for_power_two_periods` returns `inf`). This is the intended message: between-session variability, not sampling noise, limits what short comparisons can show.

## Interval-method choices and known limits

Defaults were chosen from the simulation track's coverage study (Part 6a, v0.1.1); the v0.1.0 defaults are listed in the CHANGELOG with the coverage figures that displaced them.

* **(a) time share: ratio-linearised t over clusters.** Coverage 0.91-0.96 in simulation against 0.81 for the percentile cluster bootstrap at 5 sessions. The cluster is the session; when sessions nest in >= 3 robots `cluster_level='auto'` moves the cluster to the robot (the t interval then has robots - 1 degrees of freedom), and `cluster_level='session'` forces the session. The percentile bootstrap is kept as a secondary row. With one cluster the interval falls back to Wilson with effective n = number of control segments and is labelled as such; it is anti-conservative if control persists across segments.
* **(b) task rate: cluster-robust GLM from 20 sessions, Wilson below.** Wilson ignores the session clustering and under-covers once tasks within a session are correlated; the sandwich estimator needs enough clusters to be stable, hence the switch at 20. Clopper-Pearson is always reported.
* **(c) interventions per hour: NB2 Wald.** Simulated coverage 0.946-0.958 under Poisson data and 0.889/0.935/0.954 at 5/20/80 sessions under NB dispersion ~3; Garwood gave 0.716 under that dispersion and the percentile bootstrap 0.794 at 5 sessions. The NB fit is multi-start (Poisson-GLM + method-of-moments start, then `bfgs`, `nm`, `newton`; best converged log-likelihood kept) because the statsmodels default Newton start diverges to NaN on strongly over-dispersed groups such as per-vehicle disengagement counts. Fallbacks to Poisson exact are always labelled in `interval_method`: `<2 clusters or zero count`, `alpha_at_zero_boundary` (converged MLE with alpha < 1e-4; the Hessian is singular there so SEs are undefined), `nb_se_undefined`, `nb_fit_failed`.
* **(d) gaps:** `ExponentialFitter.confidence_interval_` is the survival-function band, not the parameter CI; the mean-gap CI is built from `summary['se(coef)']` on the log scale (for the exponential MLE this is exp(log lambda ± z/sqrt(n_events))). The KM analysis treats gaps within a session as independent; its interval understates uncertainty when interventions cluster. (c) is the primary inferential quantity; (d) describes the gap distribution.
* **(e) human minutes: bootstrap-t.** v0.1.0 grouped the studentiser by the original session id inside each resample, so a session drawn k times collapsed into one cluster (coverage 0.83-0.85). Resampled clusters are now relabelled `<cluster>#<draw>` (coverage 0.94-0.96); any user-supplied `se_fn` must group by `cluster_col`.
* **Planning:** `hours_for_zero_events_nb` is exact when one gamma frailty is shared by the whole test (one robot or deployment) and conservative for independent sessions; under a shared frailty with a = 0.3-1.0 the plain rule of three was wrong 12-25 % of the time at the threshold in simulation.
* `drift` assigns each session's active hours to the bin of its start time; sessions spanning a bin boundary are not split.

## Example output (examples/report_example.json)

From the seeded synthetic log (594 events, 6 sessions, 2 robots, 37 tasks, 1.26 active hours, 24 interventions, 0.49 % unknown time):

* time autonomy share 0.782, default ratio-linearised t over 6 sessions [0.693, 0.872]; percentile bootstrap [0.706, 0.834]; outer interval (unknown-time bounds + sampling) [0.683, 0.871]
* task autonomy rate 21/37 = 0.568, Wilson [0.409, 0.713] (6 sessions < 20, so Wilson is the default)
* interventions per active hour 19.1; the NB2 dispersion MLE converges to the zero boundary (dispersion index 1.15), so the labelled fallback `poisson_exact_fallback(alpha_at_zero_boundary)` gives Garwood [12.2, 28.4]
* human minutes per task 0.154, bootstrap-t [0.074, 0.326] (percentile [0.100, 0.225])
* by reason: ai_requested_help 16, human_initiated 7, safety 1 (the 44 planned resets are human time, not interventions)

## CHANGELOG

### v0.1.1 (2026-10-30) — fixes from the simulation (Part 6a) and DMV (Part 6b) validation tracks

1. `intervals.cluster_bootstrap(method='bootstrap_t')`: resampled clusters are relabelled `<cluster>#<draw>` so the studentiser sees G clusters, not G_unique (coverage 0.830/0.854 -> standard construction 0.943/0.964 in simulation).
2. `metrics.gaps_between_interventions`: the exponential mean-gap CI was read from `confidence_interval_` (survival band); now a log-scale Wald CI from `summary['se(coef)']`.
3. `metrics.interventions_per_hour`: default interval is NB2 Wald (was Garwood if dispersion index <= 1.5 else bootstrap); Garwood and bootstrap kept as secondary rows.
4. `intervals.nb_rate_ci`: multi-start fit (Poisson/MoM start; `bfgs`, `nm`, `newton`); divergent fits are no longer mislabelled as a zero boundary; method label `nb2_wald`; fallbacks labelled.
5. `metrics.time_autonomy_share`: default interval is the ratio-linearised t (was percentile bootstrap); new `cluster_level` argument (`auto` = robots when >= 3).
6. `metrics.task_autonomy_rate`: default is cluster-robust GLM at >= 20 sessions, Wilson below (was Wilson).
7. `metrics.human_minutes_per_task`: default is the corrected bootstrap-t (was percentile).
8. `planning.hours_for_zero_events_nb` docstring states the shared-frailty (exact) vs independent-session (conservative) scope.
Every metric dict now carries `ci` and `interval_method` for the default interval. 11 regression test functions added (one parametrised over 3 fixtures) (`tests/test_regressions_v011.py`); 44 tests total.

### v0.1.0 (2026-10-30) — initial release.

## Sources used in this package (retrieved or located 2026-10-30)

* statsmodels `proportion_confint` documentation (methods `wilson`, `beta`) — https://www.statsmodels.org/stable/generated/statsmodels.stats.proportion.proportion_confint.html [VERIFIED: page read]
* lifelines `KaplanMeierFitter` documentation (median, exponential-Greenwood CI) — https://lifelines.readthedocs.io/en/latest/fitters/univariate/KaplanMeierFitter.html [VERIFIED: page read]
* JSON Schema draft 2020-12 — https://json-schema.org/draft/2020-12/schema [VERIFIED (excerpt)]
* RFC 3339 — https://www.rfc-editor.org/info/rfc3339/ [VERIFIED (excerpt)]
* Garwood F. (1936) Biometrika 28:437-442, https://doi.org/10.1093/biomet/28.3-4.437 [CLAIM: abstract page only; formula checked numerically against `scipy.stats.chi2`/`gamma` in tests]
* Hanley JA, Lippman-Hand A. (1983) JAMA 249(13):1743-1745, author copy https://jhanley.biostat.mcgill.ca/c607/ch08/zero_numerator.pdf [VERIFIED (excerpt)]
* Cal. Code Regs. tit. 13 § 227.50 (disengagement definition) — https://www.law.cornell.edu/regulations/california/13-CCR-227.50 [VERIFIED (excerpt)]
