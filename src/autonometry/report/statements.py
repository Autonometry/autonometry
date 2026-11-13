"""The statements a report prints beside its metrics (definitions.md 11.6), from the metric
blocks of one group (:mod:`autonometry.stats.metrics`). Needs the ``report`` extra (SciPy)."""

from collections.abc import Mapping
from typing import Any, Final

from autonometry.stats.intervals import MIN_SPREAD, garwood_upper

Json = dict[str, Any]

#: Metrics with ``unknown_as_human`` and ``unknown_as_ai`` bounds (definitions.md 7).
BOUNDED: Final = (
    "time_autonomy_share",
    "task_autonomy_rate",
    "autonomous_success_rate",
    "interventions_per_active_hour",
    "human_minutes_per_task",
)
#: Metrics with no true worst case when unknown time exists (11.6, 1).
NO_WORST_CASE: Final = ("interventions_per_active_hour", "human_minutes_per_task")
#: Metrics whose interval can come from one cluster (11.6, 3).
CLUSTERED: Final = (*BOUNDED, "safety_stops_per_active_hour")
#: Rates per active hour that get a one-sided upper bound from few events (11.6, 4).
RATES: Final = (
    "interventions_per_active_hour",
    "safety_stops_per_active_hour",
    "approvals_per_active_hour",
    "hints_per_active_hour",
    "takeovers_per_active_hour",
)
#: Fewer events than this make a rare rate (11.6, 4), and its one-sided confidence level.
RARE_EVENTS: Final = 5
RARE_LEVEL: Final = 0.95


def _statement(group: str, code: str, metrics: list[str], text: str, **extra: Any) -> Json:
    return {"group": group, "code": code, "metrics": metrics, "text": text, **extra}


#: Why each metric of 11.6 (1) has no true worst case, and what its lower bound assumes.
_NO_WORST_CASE_TEXT: Final[Mapping[str, str]] = {
    "interventions_per_active_hour": "unknown_as_human assumes one hidden intervention per "
    "whole-robot run of unknown time between AI control; more could hide there.",
    "human_minutes_per_task": "unknown_as_human counts each task's unknown time as human time; "
    "it assumes no more than that was hidden.",
}


def _no_worst_case(group: str, metrics: Json) -> list[Json]:
    if not metrics["unmeasured"]["unknown_time_s"] > 0:
        return []
    return [
        _statement(
            group,
            "no_worst_case",
            [name],
            "Unknown time exists, so this number has no true worst case: events can hide in "
            f"unknown time. {_NO_WORST_CASE_TEXT[name]}",
        )
        for name in NO_WORST_CASE
    ]


def _width(pair: Any) -> float | None:
    if not isinstance(pair, list) or None in pair:
        return None
    return float(pair[1]) - float(pair[0])


def _data_quality(group: str, metrics: Json) -> list[Json]:
    out = []
    for name in BOUNDED:
        block = metrics[name]
        low = block["unknown_as_human"].get("estimate")
        high = block["unknown_as_ai"].get("estimate")
        ci = _width(block.get("ci"))
        if low is None or high is None or ci is None:
            continue
        spread = abs(high - low)
        if spread > ci:
            out.append(
                _statement(
                    group,
                    "data_quality",
                    [name],
                    "Data quality, not sample size, limits this number: its bounds for unknown "
                    "time are wider than its confidence interval.",
                    bounds_width=spread,
                    ci_width=ci,
                )
            )
    return out


def _single_cluster(group: str, metrics: Json) -> list[Json]:
    out = []
    for name in CLUSTERED:
        block = metrics[name]
        clusters = block.get("n_clusters")
        if block.get("estimate") is None or clusters is None or clusters >= MIN_SPREAD:
            continue
        out.append(
            _statement(
                group,
                "not_generalisable",
                [name],
                "This interval comes from a single cluster: it is not generalisable.",
                n_clusters=clusters,
            )
        )
    return out


def _rare(group: str, metrics: Json) -> list[Json]:
    out = []
    for name in RATES:
        block = metrics[name]
        count, hours = block.get("count"), block.get("active_hours")
        if count is None or hours is None or count >= RARE_EVENTS:
            continue
        upper = garwood_upper(count, hours, RARE_LEVEL)
        if upper is None:
            continue
        out.append(
            _statement(
                group,
                "rare_events",
                [name],
                f"Fewer than {RARE_EVENTS} events ({count}): the one-sided 95 % upper bound "
                "(exact Poisson, Garwood) is shown beside the two-sided interval.",
                count=count,
                level=RARE_LEVEL,
                upper_bound=upper,
            )
        )
    return out


def _unobserved(group: str, metrics: Json, depends_on: Mapping[str, tuple[str, ...]]) -> list[Json]:
    sessions = metrics["unmeasured"]["unobserved_sessions"]
    out = []
    for signal in sorted(sessions):
        names = sorted(name for name, classes in depends_on.items() if signal in classes)
        out.append(
            _statement(
                group,
                "unobserved",
                names,
                f"{sessions[signal]} session(s) declare the signal class {signal} unobserved: "
                "the recorder cannot see it, and these metrics depend on it.",
                signal=signal,
                sessions=sessions[signal],
            )
        )
    return out


def statements(groups: Mapping[str, Json], depends_on: Mapping[str, tuple[str, ...]]) -> list[Json]:
    """Every statement of 11.6 for every group, in group order then in the order of 11.6."""
    out: list[Json] = []
    for group in sorted(groups):
        metrics = groups[group]
        out += _no_worst_case(group, metrics)
        out += _data_quality(group, metrics)
        out += _single_cluster(group, metrics)
        out += _rare(group, metrics)
        out += _unobserved(group, metrics, depends_on)
    return out
