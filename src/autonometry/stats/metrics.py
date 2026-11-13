"""The metrics (a) to (h) of the standard over the sessions of one group (definitions.md 3 to 7;
reference package ``autonomy_meter`` v0.1.1, ``metrics``).

Every metric block carries ``estimate``, ``ci`` (the default interval) and ``interval_method``;
secondary intervals keep their own ``ci_<method>`` keys. The time autonomy share, task autonomy
rate, autonomous success rate, interventions per active hour and human minutes per task also carry
``unknown_as_human`` and ``unknown_as_ai`` bounds with their own intervals and an
``outer_interval`` from the smallest to the largest of the six limits (definitions.md 7). Variants,
breakdowns and per-kind rates carry a point estimate and interval only. A metric that cannot be
computed (no tasks, no active time, no events, no distance) has a null estimate and a ``note``.

Default intervals (reference README, v0.1.1, chosen by the simulation study):

- (a) ratio-linearised t over clusters (sessions; robots under ``auto`` from 3 robots); Wilson
  with effective n (control segments) with one cluster;
- (b) cluster-robust GLM from 20 clusters, Wilson below; Clopper-Pearson beside it;
- (c) NB2 Wald with labelled Poisson-exact fallbacks; Garwood and the bootstrap beside it;
- (d) Kaplan-Meier median with its cloglog interval;
- (e) cluster bootstrap-t with relabelled clusters; t on tasks with one cluster;
- (f) Poisson exact per cell (common denominator), Wilson per task definition;
- (g) Poisson GLM trend (NB with the NB2 dispersion when the dispersion index exceeds 1.5) and a
  two-sided CUSUM.

Cluster unit: ``--cluster auto`` clusters (a) on robots when the group has at least 3 robots and
everything else on sessions; ``session`` or ``robot`` sets the unit of every clustered interval.
"""

import math
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import numpy as np
from statsmodels.genmod.families import NegativeBinomial, Poisson
from statsmodels.genmod.generalized_linear_model import GLM

from autonometry.spec import Rules
from autonometry.stats import intervals as iv
from autonometry.stats.facts import (
    NS_PER_S,
    SessionFacts,
    TaskFacts,
    active_clock,
    distance_at,
    moved_with_minimum_s,
)
from autonometry.timestamps import datetime_to_ns, ns_to_ts

Json = dict[str, Any]
ClusterChoice = Literal["auto", "session", "robot"]
ClusterUnit = Literal["session", "robot"]
DriftBin = Literal["day", "week"]
Measure = Literal["active_hours", "metres"]

HOUR_S: Final = 3600.0
MINUTE_S: Final = 60.0
#: ``--cluster auto`` clusters the time share on robots from this many robots (reference v0.1.1).
AUTO_ROBOT_CLUSTERS: Final = 3
#: The cluster-robust GLM is the default task-rate interval from this many clusters.
GLM_MIN_CLUSTERS: Final = 20
#: Horizons for S(h) of the gap analysis: active hours and metres.
TIME_HORIZONS_H: Final = (0.5, 1.0, 2.0, 4.0)
DISTANCE_HORIZONS_M: Final = (10.0, 100.0, 1000.0)
#: Drift: fewest bins for a trend, dispersion index above which the NB family is used, CUSUM
#: reference value and decision limit in standard deviations (reference v0.1.1).
DRIFT_MIN_BINS: Final = 3
DRIFT_NB_DISPERSION: Final = 1.5
CUSUM_K: Final = 0.5
CUSUM_H: Final = 4.0
_BIN_DAYS: Final = {"day": 1, "week": 7}
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Options:
    """Options of one report: two-sided level ``alpha``, bootstrap replicates and seed, cluster
    choice and the calendar bin of the drift metric."""

    alpha: float = 0.05
    n_boot: int = 2000
    seed: int = 0
    cluster: ClusterChoice = "auto"
    drift_bin: DriftBin = "day"


# -- blocks --------------------------------------------------------------------------------------


def _block(estimate: float | None, interval: iv.Interval, **extra: Any) -> Json:
    return {
        "estimate": estimate,
        "ci": interval.as_list(),
        "interval_method": interval.method,
        **extra,
    }


def _null(note: str, **extra: Any) -> Json:
    return {"estimate": None, "ci": None, "interval_method": None, "note": note, **extra}


def outer_interval(*blocks: Json) -> list[float] | None:
    """From the smallest to the largest confidence limit of the point estimate and both bounds,
    so it holds whether more is better or worse (definitions.md 7)."""
    limits = [value for block in blocks for value in (block.get("ci") or []) if value is not None]
    return [min(limits), max(limits)] if limits else None


def _bounded(definition: str, point: Json, lower: Json, upper: Json, **extra: Any) -> Json:
    return {
        "definition": definition,
        **point,
        "unknown_as_human": lower,
        "unknown_as_ai": upper,
        "outer_interval": outer_interval(point, lower, upper),
        **extra,
    }


def _core(block: Json) -> Json:
    """The estimate, interval and counts of a block, without its secondary intervals."""
    keep = ("estimate", "ci", "interval_method", "note", "k", "n", "count", "active_hours")
    return {key: block[key] for key in keep if key in block}


# -- clusters ------------------------------------------------------------------------------------


def share_unit(options: Options, sessions: Sequence[SessionFacts]) -> ClusterUnit:
    """The cluster unit of the time share (a): robots under ``auto`` from 3 robots."""
    if options.cluster == "auto":
        robots = len({s.robot_id for s in sessions})
        return "robot" if robots >= AUTO_ROBOT_CLUSTERS else "session"
    return options.cluster


def other_unit(options: Options) -> ClusterUnit:
    """The cluster unit of every other clustered interval: sessions unless ``robot``."""
    return "robot" if options.cluster == "robot" else "session"


def cluster_of(unit: ClusterUnit, session: SessionFacts) -> str:
    return session.robot_id if unit == "robot" else session.key


def _sums(
    sessions: Iterable[SessionFacts],
    unit: ClusterUnit,
    values: Callable[[SessionFacts], tuple[float, float]],
) -> list[tuple[float, float]]:
    """Per-cluster sums of a pair of session values, in cluster order."""
    out: dict[str, tuple[float, float]] = {}
    for session in sessions:
        a, b = values(session)
        x, y = out.get(cluster_of(unit, session), (0.0, 0.0))
        out[cluster_of(unit, session)] = (x + a, y + b)
    return [out[key] for key in sorted(out)]


# -- (a) time autonomy share ---------------------------------------------------------------------


def _share(
    rows: Sequence[tuple[float, float]],
    unit: ClusterUnit,
    n_eff: int,
    options: Options,
    secondary: bool = False,
) -> Json:
    """A ratio of sums over clusters with its default interval; clusters with nothing in the
    denominator carry no information and are left out."""
    usable = [(num, den) for num, den in rows if den > 0]
    total = sum(den for _, den in usable)
    if total <= 0:
        return _null("no time in the denominator")
    num, den = [row[0] for row in usable], [row[1] for row in usable]
    estimate = sum(num) / total
    wilson = iv.wilson_effective_n(estimate, n_eff, options.alpha)
    if len(usable) >= iv.MIN_SPREAD:
        linear = iv.ratio_linearised_t(num, den, options.alpha).clipped(0.0, 1.0)
        interval = linear.renamed(f"ratio_linearised_t({unit})")
    else:
        interval = wilson.renamed("wilson_effective_n(single_cluster)")
    block = _block(estimate, interval, n_clusters=len(usable))
    if secondary:
        boot = iv.bootstrap_ratio_percentile(num, den, options.n_boot, options.alpha, options.seed)
        block |= {
            "ci_bootstrap_percentile": boot.as_list(),
            "ci_wilson_effective_n": wilson.as_list(),
            "n_eff_segments": n_eff,
        }
    return block


def dwell_minimums(sessions: Sequence[SessionFacts], rules: Rules) -> tuple[float, float]:
    """Minimum approval and hint dwells of the with-approvals variant: the median of every
    logged dwell of that type in the group, or ``approval_dwell_sensitivity_s`` (5 s) when none
    was logged (definitions.md 6.1, 6.2)."""
    fallback = rules.params.approval_dwell_sensitivity_s
    approvals = [d for s in sessions for d in s.approval_dwells_s]
    hints = [d for s in sessions for d in s.hint_dwells_s]
    return (
        statistics.median(approvals) if approvals else fallback,
        statistics.median(hints) if hints else fallback,
    )


def time_autonomy_share(sessions: Sequence[SessionFacts], options: Options, rules: Rules) -> Json:
    """(a) Time autonomy share: AI time minus the dwell its approvals and hints moved, over
    active time, on the whole-robot timeline (definitions.md 4, 6.1, 6.6), with bounds (7),
    variants (6.1, 6.6, 6.15) and the shares the report must print (2, 4, 6.3, 6.15)."""
    unit = share_unit(options, sessions)
    n_eff = sum(s.segments for s in sessions)

    def share(values: Callable[[SessionFacts], tuple[float, float]], **kw: Any) -> Json:
        return _share(_sums(sessions, unit, values), unit, n_eff, options, **kw)

    point = share(lambda s: (s.ai_s - s.moved_s, s.active_s), secondary=True)
    lower = share(lambda s: (s.ai_s - s.moved_s, s.active_s + s.unknown_s))
    upper = share(lambda s: (s.ai_s - s.moved_s + s.unknown_s, s.active_s + s.unknown_s))
    approval_min, hint_min = dwell_minimums(sessions, rules)
    moved = {s.key: moved_with_minimum_s(s, approval_min, hint_min) for s in sessions}
    variants = {
        "with_approvals": share(lambda s: (s.ai_s - moved[s.key], s.active_s))
        | {"approval_min_dwell_s": approval_min, "hint_min_dwell_s": hint_min},
        "shared_as_ai": share(lambda s: (s.ai_s - s.moved_s + s.states_s["shared"], s.active_s)),
        "scope_weighted": share(
            lambda s: (s.scope_weighted_ai_s - s.scope_weighted_moved_s, s.scope_weighted_active_s)
        ),
        "unsupported": share(
            lambda s: (s.unsupported_ai_s - s.unsupported_moved_s, s.unsupported_active_s)
        ),
    }

    def of_active(state: str) -> Json:
        return share(lambda s: (s.states_s[state], s.active_s))

    shares = {
        f"{state}_share": of_active(state)
        for state in ("human_remote", "human_local", "shared", "safety_stop", "scripted")
    }
    return _bounded(
        "(ai_s - dwell moved from ai by approvals and hints) / active_s, whole-robot timeline",
        point,
        lower,
        upper,
        cluster=unit,
        active_hours=sum(s.active_s for s in sessions) / HOUR_S,
        variants=variants,
        **shares,
        idle_time_s=sum(s.idle_s for s in sessions),
        idle_share_of_observed=share(lambda s: (s.idle_s, s.observed_s)),
        reset_human_time_s=sum(s.reset_human_s for s in sessions),
        reset_human_share_of_active=share(lambda s: (s.reset_human_s, s.active_s)),
        supported_time_s=sum(s.supported_s for s in sessions),
        supported_active_time_s=sum(s.active_s - s.unsupported_active_s for s in sessions),
        supported_share_of_active=share(
            lambda s: (s.active_s - s.unsupported_active_s, s.active_s)
        ),
    )


# -- (b) task rates ------------------------------------------------------------------------------


def _proportion(pairs: Sequence[tuple[str, bool]], unit: ClusterUnit, options: Options) -> Json:
    """A proportion over tasks in clusters with its default interval: cluster-robust GLM from
    20 clusters, Wilson below; Wilson, Clopper-Pearson and the GLM beside it."""
    n = len(pairs)
    if n == 0:
        return _null("no tasks", k=0, n=0)
    y = [int(value) for _, value in pairs]
    clusters = [cluster for cluster, _ in pairs]
    k = sum(y)
    groups = len(set(clusters))
    glm = iv.cluster_robust_proportion(y, clusters, options.alpha)
    wilson = iv.wilson(k, n, options.alpha)
    if groups >= GLM_MIN_CLUSTERS and glm.method == "cluster_robust_glm":
        interval = glm.renamed(f"cluster_robust_glm({unit})")
    else:
        interval = wilson
    return _block(
        k / n,
        interval,
        k=k,
        n=n,
        n_clusters=groups,
        ci_wilson=wilson.as_list(),
        ci_clopper_pearson=iv.clopper_pearson(k, n, options.alpha).as_list(),
        ci_cluster_robust=glm.as_list(),
        cluster_robust_method=glm.method,
    )


def _not_doubtful(task: TaskFacts) -> bool:
    """Autonomous in the ``unknown_as_human`` bound: closed and no unknown time (7)."""
    return task.autonomous and task.closed and not task.contains_unknown


def task_rates(sessions: Sequence[SessionFacts], options: Options) -> tuple[Json, Json]:
    """(b) Task autonomy rate and autonomous success rate over tasks with a known definition
    (definitions.md 5): the point estimate over closed tasks, ``unknown_as_human`` over all
    tasks with an unclosed task or one containing unknown time not autonomous, ``unknown_as_ai``
    equal to the point estimate (7)."""
    unit = other_unit(options)
    tasks = [(cluster_of(unit, s), t) for s in sessions for t in s.tasks if t.known]
    closed = [(c, t) for c, t in tasks if t.closed]
    counts = {
        "n_tasks": len(tasks),
        "n_tasks_closed": len(closed),
        "n_tasks_unclosed": len(tasks) - len(closed),
        "n_tasks_with_unknown": sum(1 for _, t in tasks if t.contains_unknown),
        "n_tasks_unknown_definition": sum(1 for s in sessions for t in s.tasks if not t.known),
        "cluster": unit,
    }

    def rate(definition: str, point_ok: Callable[[TaskFacts], bool]) -> Json:
        point = _proportion([(c, point_ok(t)) for c, t in closed], unit, options)
        lower = _proportion(
            [(c, point_ok(t) and _not_doubtful(t)) for c, t in tasks], unit, options
        )
        return _bounded(definition, point, _core(lower), _core(point), **counts)

    return (
        rate("closed tasks with no human help / closed tasks", lambda t: t.autonomous),
        rate(
            "closed tasks with no human help and outcome success / closed tasks",
            lambda t: t.autonomous and t.success,
        ),
    )


# -- (c) rates per active hour -------------------------------------------------------------------


def _rate(
    rows: Sequence[tuple[float, float]],
    unit: ClusterUnit,
    options: Options,
    bootstrap: bool = False,
) -> Json:
    """Events per hour from per-cluster (count, hours): NB2 Wald with labelled fallbacks, the
    Poisson-exact interval and the dispersion beside it, optionally the cluster bootstrap."""
    count = round(sum(k for k, _ in rows))
    usable = [(k, e) for k, e in rows if e > 0]
    hours = sum(e for _, e in usable)
    if hours <= 0:
        return _null("no active time", count=count, active_hours=0.0)
    counts, exposures = [k for k, _ in usable], [e for _, e in usable]
    fit = iv.nb2_rate(counts, exposures, options.alpha)
    block = _block(
        count / hours,
        fit.interval,
        count=count,
        active_hours=hours,
        n_clusters=len(usable),
        cluster=unit,
        nb_alpha=fit.alpha_nb,
        dispersion_index=iv.dispersion_index(counts, exposures),
        ci_poisson_exact=iv.garwood(count, hours, options.alpha).as_list(),
    )
    if bootstrap:
        boot = iv.bootstrap_ratio_percentile(
            counts, exposures, options.n_boot, options.alpha, options.seed
        )
        block["ci_bootstrap_percentile"] = boot.as_list()
    return block


def _hours(seconds: float) -> float:
    return seconds / HOUR_S


def interventions_rate(sessions: Sequence[SessionFacts], options: Options) -> Json:
    """(c) Interventions per active hour with whole-robot counts (definitions.md 3, 6.6), the
    bounds of Section 7 and the per-scope count beside it."""
    unit = other_unit(options)

    def rate(values: Callable[[SessionFacts], tuple[float, float]], **kw: Any) -> Json:
        return _rate(_sums(sessions, unit, values), unit, options, **kw)

    point = rate(lambda s: (s.interventions, _hours(s.active_s)), bootstrap=True)
    lower = rate(
        lambda s: (s.interventions + s.hidden_interventions, _hours(s.active_s + s.unknown_s))
    )
    upper = rate(lambda s: (s.interventions, _hours(s.active_s + s.unknown_s)))
    per_scope = rate(lambda s: (s.interventions_per_scope, _hours(s.active_s)))
    return _bounded(
        "whole-robot interventions / active hours",
        point,
        _core(lower),
        _core(upper),
        per_scope=_core(per_scope),
    )


def safety_stops_rate(sessions: Sequence[SessionFacts], options: Options) -> Json:
    """(c) Safety stops per active hour (definitions.md 2 rule 3, 6.7): whole-robot stops, split
    by trigger, and the per-scope count."""
    unit = other_unit(options)

    def rate(values: Callable[[SessionFacts], float]) -> Json:
        return _rate(
            _sums(sessions, unit, lambda s: (values(s), _hours(s.active_s))), unit, options
        )

    return {
        "definition": "whole-robot safety stops / active hours",
        **rate(lambda s: s.safety_stops),
        "human_triggered": _core(rate(lambda s: s.safety_stops_human)),
        "automatic": _core(rate(lambda s: s.safety_stops_automatic)),
        "per_scope": _core(rate(lambda s: s.safety_stops_per_scope)),
    }


def kind_rate(sessions: Sequence[SessionFacts], options: Options, kind: str) -> Json:
    """(c) Whole-robot interventions of one kind per active hour (``approval``, ``hint``,
    ``takeover``; definitions.md 6.1 requires approvals per active hour)."""
    unit = other_unit(options)
    rows = _sums(sessions, unit, lambda s: (s.kinds.get(kind, 0), _hours(s.active_s)))
    return {"definition": f"whole-robot {kind} interventions / active hours"} | _core(
        _rate(rows, unit, options)
    )


# -- (d) gaps between interventions --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Gap:
    """One gap: from an intervention (or the session start) to the next intervention
    (``observed`` 1) or to the session end (``observed`` 0, right-censored)."""

    cluster: str
    duration: float
    observed: int


def _positions(session: SessionFacts, measure: Measure) -> tuple[list[float], float] | None:
    """Where each intervention and the session end fall on the gap scale: active hours, or
    metres (the latest distance sample at or before the instant). ``None`` without distance."""
    end_ns = session.timeline.end_ns or 0
    if measure == "metres":
        samples = session.timeline.distance_samples
        if not samples:
            return None
        return [distance_at(samples, m) for m in session.marks_ns], distance_at(samples, end_ns)
    per_hour = NS_PER_S * HOUR_S
    marks = [active_clock(session.active, m) / per_hour for m in session.marks_ns]
    return marks, active_clock(session.active, end_ns) / per_hour


def gap_table(
    sessions: Sequence[SessionFacts], unit: ClusterUnit, measure: Measure
) -> tuple[list[Gap], int]:
    """The gaps of every session and the number of sessions without distance samples. A
    censored gap of zero length carries no information and is left out."""
    rows: list[Gap] = []
    skipped = 0
    for session in sessions:
        positions = _positions(session, measure)
        if positions is None:
            skipped += 1
            continue
        marks, end = positions
        previous = 0.0
        for mark in marks:
            rows.append(Gap(cluster_of(unit, session), max(mark - previous, 0.0), 1))
            previous = mark
        if end - previous > 0:
            rows.append(Gap(cluster_of(unit, session), end - previous, 0))
    return rows, skipped


def _weibull(rows: Sequence[Gap], unit: ClusterUnit, options: Options) -> Json:
    """Weibull shape (SciPy ``CensoredData``) with a cluster-bootstrap percentile interval of
    the profile-likelihood shape."""
    durations = [g.duration for g in rows]
    observed = [g.observed for g in rows]
    fit = iv.weibull_fit(durations, observed)
    if fit is None:
        return _null("fewer than 3 events above zero, or the fit failed")
    shape, scale = fit
    by_cluster: dict[str, list[Gap]] = {}
    for gap in rows:
        by_cluster.setdefault(gap.cluster, []).append(gap)
    clusters = [by_cluster[key] for key in sorted(by_cluster)]

    def statistic(draw: list[list[Gap]]) -> float | None:
        gaps = [g for cluster in draw for g in cluster]
        return iv.weibull_shape([g.duration for g in gaps], [g.observed for g in gaps])

    interval, failed = iv.bootstrap_clusters(
        clusters, statistic, options.n_boot, options.alpha, options.seed
    )
    if interval.defined:
        interval = interval.renamed(f"cluster_bootstrap_percentile({unit})")
    return _block(
        shape,
        interval,
        scale=scale,
        failed_replicates=failed,
        note="shape below 1: decreasing hazard (bursty); 1: exponential",
    )


def gaps(sessions: Sequence[SessionFacts], options: Options, measure: Measure) -> Json:
    """(d) Gaps between whole-robot interventions in active hours or metres, right-censored at
    the session end: Kaplan-Meier median (the estimate) and S at horizons, exponential mean gap,
    Weibull shape. Gaps within a session are treated as independent, so the median interval
    understates uncertainty when interventions cluster; (c) is the inferential quantity."""
    unit = other_unit(options)
    rows, skipped = gap_table(sessions, unit, measure)
    horizons = TIME_HORIZONS_H if measure == "active_hours" else DISTANCE_HORIZONS_M
    extra: Json = {"unit": measure, "cluster": unit}
    if measure == "metres":
        extra["n_sessions_without_distance"] = skipped
    if not rows:
        note = "no distance samples" if measure == "metres" else "no active time"
        return _null(note, **extra)
    durations = [g.duration for g in rows]
    observed = [g.observed for g in rows]
    events = sum(observed)
    km = iv.kaplan_meier(durations, observed, options.alpha, horizons)
    exponential = iv.exponential_fit(durations, observed, options.alpha)
    block = _block(
        km.median,
        km.median_interval,
        definition=f"gap between consecutive whole-robot interventions ({measure}), "
        "right-censored at the session end; Kaplan-Meier median",
        n_gaps=len(rows),
        n_events=events,
        n_censored=len(rows) - events,
        survival_at={
            f"{point.horizon:g}": _block(point.survival, point.interval) for point in km.at
        },
        exponential=None
        if exponential is None
        else {
            "mean_gap": exponential.mean,
            "ci_mean_gap": exponential.mean_interval.as_list(),
            "rate": exponential.rate,
            "ci_rate": exponential.rate_interval.as_list(),
            "interval_method": exponential.mean_interval.method,
        },
        weibull=_weibull(rows, unit, options),
        naive_mean_gap=sum(durations) / events if events else None,
        **extra,
    )
    if km.median is None:
        block["note"] = "no events" if events == 0 else "median not reached"
    return block


# -- (e) human minutes per task ------------------------------------------------------------------


def _mean(
    pairs: Sequence[tuple[str, float]], unit: ClusterUnit, options: Options, secondary: bool = False
) -> Json:
    """A mean over tasks in clusters: cluster bootstrap-t (relabelled clusters), t on tasks with
    one cluster; the percentile bootstrap beside it."""
    if not pairs:
        return _null("no tasks", n=0)
    by_cluster: dict[str, list[float]] = {}
    for cluster, value in pairs:
        by_cluster.setdefault(cluster, []).append(value)
    keys = sorted(by_cluster)
    sums = [sum(by_cluster[k]) for k in keys]
    counts = [float(len(by_cluster[k])) for k in keys]
    values = [value for _, value in pairs]
    estimate = sum(values) / len(values)
    if len(keys) >= iv.MIN_SPREAD:
        interval = iv.bootstrap_mean_t(sums, counts, options.n_boot, options.alpha, options.seed)
        interval = interval.renamed(f"cluster_bootstrap_t({unit})")
    else:
        interval = iv.t_interval(values, options.alpha)
        if interval.defined:
            interval = interval.renamed("t_on_tasks(single_cluster)")
    block = _block(estimate, interval, n=len(values), n_clusters=len(keys))
    if secondary:
        boot = iv.bootstrap_mean_percentile(
            sums, counts, options.n_boot, options.alpha, options.seed
        )
        block |= {"ci_bootstrap_percentile": boot.as_list(), "median": statistics.median(values)}
    return block


def human_minutes(sessions: Sequence[SessionFacts], options: Options) -> Json:
    """(e) Human minutes per task over every task with a known definition, closed or not: the
    task's whole-robot human time, the unplanned reset time attributed to it and the dwell its
    approvals and hints moved (definitions.md 6.1, 6.3); ``unknown_as_human`` adds the task's
    whole-robot unknown time, ``unknown_as_ai`` equals the point estimate (7)."""
    unit = other_unit(options)
    tasks = [(cluster_of(unit, s), t) for s in sessions for t in s.tasks if t.known]
    per_minute = NS_PER_S * MINUTE_S
    point = _mean([(c, t.human_ns / per_minute) for c, t in tasks], unit, options, secondary=True)
    lower = _mean([(c, (t.human_ns + t.unknown_ns) / per_minute) for c, t in tasks], unit, options)
    zero = sum(1 for _, t in tasks if t.human_ns == 0)
    return _bounded(
        "mean over tasks of (human time + unplanned reset + moved dwell) in minutes",
        point,
        _core(lower),
        _core(point),
        cluster=unit,
        share_tasks_zero_human=zero / len(tasks) if tasks else None,
    )


# -- (f) breakdowns ------------------------------------------------------------------------------


def _poisson_cell(count: int, hours: float, alpha: float) -> Json:
    if hours <= 0:
        return _null("no active time", count=count)
    return _block(count / hours, iv.garwood(count, hours, alpha), count=count)


def _wilson_rate(values: Sequence[bool], alpha: float) -> Json:
    if not values:
        return _null("no closed tasks", k=0, n=0)
    k = sum(values)
    return _block(k / len(values), iv.wilson(k, len(values), alpha), k=k, n=len(values))


def _definition_block(
    pairs: Sequence[tuple[str, TaskFacts]], unit: ClusterUnit, options: Options
) -> Json:
    closed = [t for _, t in pairs if t.closed]
    per_minute = NS_PER_S * MINUTE_S
    return {
        "n_tasks": len(pairs),
        "n_tasks_closed": len(closed),
        "task_autonomy_rate": _wilson_rate([t.autonomous for t in closed], options.alpha),
        "autonomous_success_rate": _wilson_rate(
            [t.autonomous and t.success for t in closed], options.alpha
        ),
        "success_rate": _wilson_rate([t.success for t in closed], options.alpha),
        "human_minutes_per_task": _mean(
            [(c, t.human_ns / per_minute) for c, t in pairs], unit, options
        ),
        "interventions_per_task": _mean(
            [(c, float(t.interventions)) for c, t in pairs], unit, options
        ),
        "task_minutes": _mean([(c, t.duration_ns / per_minute) for c, t in pairs], unit, options),
    }


def breakdowns(sessions: Sequence[SessionFacts], options: Options) -> Json:
    """(f) Whole-robot interventions per active hour by reason and by kind, and the narrow
    (DMV-like) count: hand-overs (not approvals or hints) with reason ``human_initiated`` or
    ``safety`` (definitions.md 3); Poisson exact per cell over the common active hours. Per task
    definition: rates with Wilson intervals and means with the (e) interval."""
    hours = sum(s.active_s for s in sessions) / HOUR_S
    reasons: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    for session in sessions:
        reasons.update(session.reasons)
        kinds.update(session.kinds)
    unit = other_unit(options)
    by_definition: dict[str, list[tuple[str, TaskFacts]]] = {}
    for session in sessions:
        for task in session.tasks:
            if task.known:
                by_definition.setdefault(task.definition, []).append(
                    (cluster_of(unit, session), task)
                )
    return {
        "active_hours": hours,
        "by_reason": {r: _poisson_cell(reasons[r], hours, options.alpha) for r in sorted(reasons)},
        "by_kind": {k: _poisson_cell(kinds[k], hours, options.alpha) for k in sorted(kinds)},
        "narrow_human_initiated_or_safety": _poisson_cell(
            sum(s.narrow for s in sessions), hours, options.alpha
        ),
        "by_task_definition": {
            name: _definition_block(by_definition[name], unit, options)
            for name in sorted(by_definition)
        },
    }


# -- (g) drift -----------------------------------------------------------------------------------


def _bin_start(instant_ns: int, days: int) -> int:
    """The UTC midnight starting the day, or the Monday starting the ISO week, of an instant."""
    moment = _EPOCH + timedelta(seconds=instant_ns // NS_PER_S)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if days == _BIN_DAYS["week"]:
        start -= timedelta(days=start.weekday())
    return datetime_to_ns(start)


def _bin_label(start_ns: int, days: int) -> str:
    moment = _EPOCH + timedelta(seconds=start_ns // NS_PER_S)
    return moment.strftime("%G-W%V") if days == _BIN_DAYS["week"] else moment.strftime("%Y-%m-%d")


def drift_bins(
    sessions: Sequence[SessionFacts], days: int
) -> tuple[dict[int, float], dict[int, int]]:
    """Active hours and whole-robot interventions per calendar bin; active time is split
    exactly at bin boundaries."""
    width = days * 86_400 * NS_PER_S
    hours: dict[int, float] = {}
    counts: dict[int, int] = {}
    for session in sessions:
        for start, end in session.active:
            cursor = start
            while cursor < end:
                low = _bin_start(cursor, days)
                high = min(end, low + width)
                hours[low] = hours.get(low, 0.0) + (high - cursor) / NS_PER_S / HOUR_S
                cursor = high
        for mark in session.marks_ns:
            low = _bin_start(mark, days)
            counts[low] = counts.get(low, 0) + 1
    return hours, counts


def _trend(k: np.ndarray, e: np.ndarray, x: np.ndarray, alpha: float) -> Json:
    """Log-linear GLM of counts on the bin index with offset log(active hours): Poisson, or NB
    with the NB2 dispersion when the dispersion index exceeds 1.5. A fit that warns is reported
    as failed."""
    dispersion = iv.dispersion_index(list(k), list(e))
    family: Any = Poisson()
    name, alpha_nb = "poisson", None
    if dispersion is not None and dispersion > DRIFT_NB_DISPERSION:
        fit = iv.nb2_rate(list(k), list(e), alpha)
        if fit.interval.method == "nb2_wald" and fit.alpha_nb is not None:
            family, name, alpha_nb = (
                NegativeBinomial(alpha=fit.alpha_nb),
                "negative_binomial",
                fit.alpha_nb,
            )
    design = np.column_stack([np.ones(len(x)), x])
    try:
        with iv.strict():
            result = GLM(k, design, family=family, offset=np.log(e)).fit()
            slope = float(np.asarray(result.params)[1])
            se = float(np.asarray(result.bse)[1])
            p_value = float(np.asarray(result.pvalues)[1])
    except iv.FIT_FAILURES as exc:
        return {"family": name, "note": f"trend fit failed ({type(exc).__name__})"}
    z = iv.z_value(alpha)
    return {
        "family": name,
        "nb_alpha": alpha_nb,
        "dispersion_index": dispersion,
        "slope_log_rate_per_bin": slope,
        "se": se,
        "rate_ratio_per_bin": math.exp(slope),
        "ci_rate_ratio": [math.exp(slope - z * se), math.exp(slope + z * se)],
        "interval_method": "wald",
        "p_value": p_value,
    }


def _cusum(k: np.ndarray, e: np.ndarray, labels: Sequence[str], dispersion: float | None) -> Json:
    """Two-sided tabular CUSUM of standardised bin counts ``(k - r e) / sqrt(r e phi)``,
    ``phi`` the dispersion index when above 1; the first bin where either side exceeds h."""
    rate = float(k.sum() / e.sum())
    phi = max(dispersion or 1.0, 1.0)
    z = (k - rate * e) / np.sqrt(rate * e * phi)
    upper: list[float] = []
    lower: list[float] = []
    first: Json | None = None
    for index, value in enumerate(z):
        upper.append(max(0.0, (upper[-1] if upper else 0.0) + float(value) - CUSUM_K))
        lower.append(max(0.0, (lower[-1] if lower else 0.0) - float(value) - CUSUM_K))
        if first is None and (upper[-1] > CUSUM_H or lower[-1] > CUSUM_H):
            first = {"bin": labels[index], "direction": "up" if upper[-1] > CUSUM_H else "down"}
    return {
        "k": CUSUM_K,
        "h": CUSUM_H,
        "overdispersion_phi": phi,
        "upper": upper,
        "lower": lower,
        "first_signal": first,
    }


def drift(sessions: Sequence[SessionFacts], options: Options) -> Json:
    """(g) Whole-robot interventions per active hour by UTC calendar bin (day, or ISO week from
    Monday): a log-linear trend in the bin index (calendar distance, so empty bins keep their
    place) and a CUSUM. Fewer than 3 bins with active time, or no interventions: no trend."""
    days = _BIN_DAYS[options.drift_bin]
    hours, counts = drift_bins(sessions, days)
    starts = sorted(set(hours) | set(counts))
    bins = [
        {
            "bin": _bin_label(start, days),
            "start": ns_to_ts(start),
            "active_hours": hours.get(start, 0.0),
            "count": counts.get(start, 0),
            "rate": counts.get(start, 0) / hours[start] if hours.get(start, 0.0) > 0 else None,
        }
        for start in starts
    ]
    out: Json = {"bin": options.drift_bin, "n_bins": len(bins), "bins": bins}
    modelled = [s for s in starts if hours.get(s, 0.0) > 0]
    if len(modelled) < DRIFT_MIN_BINS:
        return out | {"trend": {"note": "fewer than 3 bins with active time"}, "cusum": None}
    k = np.array([float(counts.get(s, 0)) for s in modelled])
    e = np.array([hours[s] for s in modelled])
    if k.sum() <= 0:
        return out | {"trend": {"note": "no interventions"}, "cusum": None}
    width = days * 86_400 * NS_PER_S
    x = np.array([float((s - modelled[0]) // width) for s in modelled])
    labels = [_bin_label(s, days) for s in modelled]
    return out | {
        "trend": _trend(k, e, x, options.alpha),
        "cusum": _cusum(k, e, labels, iv.dispersion_index(list(k), list(e))),
    }


# -- (h) unmeasured ------------------------------------------------------------------------------


def parameters(rules: Rules) -> Json:
    """Every parameter of definitions.md 9 with the value used."""
    sealing = rules.sealing
    return dict(
        sorted(
            (
                rules.params.model_dump()
                | {
                    "checkpoint_every_events": sealing.checkpoint_every_events,
                    "checkpoint_every_s": sealing.checkpoint_every_s,
                    "max_commit_delay_s": sealing.max_commit_delay_s,
                    "clock_tolerance_s": sealing.clock_tolerance_s,
                }
            ).items()
        )
    )


def _ratio(num: float, den: float) -> float | None:
    return num / den if den > 0 else None


def unmeasured(sessions: Sequence[SessionFacts], rules: Rules) -> Json:
    """(h) Unknown share and conformance (definitions.md 7), flag counts, supervision (6.4),
    human labour (6.5), blind spots (8) and every parameter (9). The report is ``complete`` only
    when the unknown share is known and at most ``unknown_share_max``."""
    observed = sum(s.observed_s for s in sessions)
    unknown = sum(s.unknown_s for s in sessions)
    active = sum(s.active_s for s in sessions)
    share = _ratio(unknown, observed)
    limit = rules.params.unknown_share_max
    flags: Counter[str] = Counter()
    blind: Counter[str] = Counter()
    for session in sessions:
        flags.update(session.flag_counts)
        blind.update(session.unobserved)
    human_actor_s = sum(s.human_actor_s for s in sessions)
    return {
        "observed_time_s": observed,
        "active_time_s": active,
        "idle_time_s": sum(s.idle_s for s in sessions),
        "unknown_time_s": unknown,
        "unknown_share": share,
        "unknown_share_max": limit,
        "conformance": "complete" if share is not None and share <= limit else "incomplete",
        "flag_counts": dict(sorted(flags.items())),
        "n_flags": sum(flags.values()),
        "supervision_ratio": _ratio(sum(s.watched_s for s in sessions), active),
        "supervisor_hours_per_robot_hour": _ratio(sum(s.supervisor_s for s in sessions), active),
        "human_actor_seconds": human_actor_s,
        "human_actor_hours": human_actor_s / HOUR_S,
        "distinct_human_actors": len({a for s in sessions for a in s.human_actors}),
        "unobserved_sessions": dict(sorted(blind.items())),
        "parameters": parameters(rules),
    }


#: The signal classes of ``session_start.unobserved`` each metric depends on (definitions.md 8).
DEPENDS_ON: Final[Mapping[str, tuple[str, ...]]] = {
    "time_autonomy_share": ("human_inputs", "safety_stops"),
    "task_autonomy_rate": ("human_inputs", "tasks"),
    "autonomous_success_rate": ("human_inputs", "tasks"),
    "interventions_per_active_hour": ("human_inputs", "safety_stops"),
    "safety_stops_per_active_hour": ("safety_stops",),
    "approvals_per_active_hour": ("human_inputs",),
    "hints_per_active_hour": ("human_inputs",),
    "takeovers_per_active_hour": ("human_inputs", "safety_stops"),
    "gaps_between_interventions": ("human_inputs", "safety_stops"),
    "human_minutes_per_task": ("human_inputs", "tasks"),
    "breakdowns": ("human_inputs", "safety_stops", "tasks"),
    "drift": ("human_inputs", "safety_stops"),
}


def group_metrics(sessions: Sequence[SessionFacts], options: Options, rules: Rules) -> Json:
    """Every metric of one group of sessions (definitions.md 7, 8), sessions in id order."""
    ordered = sorted(sessions, key=lambda s: (s.robot_id, s.session_id))
    task_rate, success_rate = task_rates(ordered, options)
    metrics: Json = {
        "time_autonomy_share": time_autonomy_share(ordered, options, rules),
        "task_autonomy_rate": task_rate,
        "autonomous_success_rate": success_rate,
        "interventions_per_active_hour": interventions_rate(ordered, options),
        "safety_stops_per_active_hour": safety_stops_rate(ordered, options),
        "approvals_per_active_hour": kind_rate(ordered, options, "approval"),
        "hints_per_active_hour": kind_rate(ordered, options, "hint"),
        "takeovers_per_active_hour": kind_rate(ordered, options, "takeover"),
        "gaps_between_interventions": {
            "active_hours": gaps(ordered, options, "active_hours"),
            "metres": gaps(ordered, options, "metres"),
        },
        "human_minutes_per_task": human_minutes(ordered, options),
        "breakdowns": breakdowns(ordered, options),
        "drift": drift(ordered, options),
    }
    blind = {c for s in ordered for c in s.unobserved}
    for name, classes in DEPENDS_ON.items():
        metrics[name]["unobserved"] = sorted(blind & set(classes))
    return {
        "sessions": [{"robot_id": s.robot_id, "session_id": s.session_id} for s in ordered],
        "n_sessions": len(ordered),
        "n_robots": len({s.robot_id for s in ordered}),
        **metrics,
        "unmeasured": unmeasured(ordered, rules),
    }
