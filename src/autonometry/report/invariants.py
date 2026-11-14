"""The invariants of a report (definitions.md 11.7), each ``pass``, ``fail`` or
``not_checked`` with what failed; witnesses are ``not_available`` when no robot session is
witnessed (12). Pure standard library: the metrics arrive as the JSON blocks of
:mod:`autonometry.stats.metrics`."""

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Final

from autonometry.events import LogCheck, Session, VerifyResult
from autonometry.timeline import StateTotals, Timeline
from autonometry.witness import Reconciliation

Json = dict[str, Any]

#: Failures listed per invariant; the count is always exact.
LISTED_MAX: Final = 20
#: Gap codes of 10.6 that say a token's time disagrees with the log's clock (11.7, time).
TIME_GAPS: Final = ("clock_skew", "late_anchor")
#: Metrics whose bounds must bracket the point estimate (11.7, bounds).
BOUNDED: Final = ("time_autonomy_share", "task_autonomy_rate", "autonomous_success_rate")
#: Rounding slack when comparing a bound with the point estimate; never an equality test.
_SLACK: Final = 1e-12


def _result(status: str, detail: str, failures: Sequence[str] = ()) -> Json:
    return {
        "status": status,
        "detail": detail,
        "failure_count": len(failures),
        "failures": list(failures[:LISTED_MAX]),
    }


def _name(timeline: Timeline | Session) -> str:
    return f"{timeline.robot_id}/{timeline.session_id}"


def _partition_problem(totals: StateTotals[int], window_ns: int) -> str | None:
    states = sum(totals.states.values())
    by_category = totals.active_ns + totals.idle_ns + totals.unknown_ns
    if totals.observed_ns != window_ns or states != window_ns or by_category != window_ns:
        return (
            f"states sum to {states} ns, categories to {by_category} ns, observed "
            f"{totals.observed_ns} ns, session window {window_ns} ns"
        )
    return None


def partition(timelines: Sequence[Timeline]) -> Json:
    """In every session, state times sum to the observed time, which is the session window,
    per scope and for the whole robot (11.7; definitions.md 2, 4, 6.6)."""
    failures = []
    for t in timelines:
        if t.start_ns is None or t.end_ns is None:
            continue
        window = t.end_ns - t.start_ns
        scoped = [("whole robot", t.whole_robot)]
        scoped += [(f"scope {name}", totals) for name, totals in t.per_scope.items()]
        failures += [
            f"{_name(t)} {where}: {problem}"
            for where, totals in scoped
            if (problem := _partition_problem(totals, window)) is not None
        ]
    checked = sum(1 for t in timelines if t.start_ns is not None)
    return _result(
        "fail" if failures else "pass",
        f"{checked} session(s) with observed time checked per scope and for the whole robot",
        failures,
    )


def chain(result: VerifyResult) -> Json:
    """No session contradicts its seals (10.6); ``not_checked`` when a session has no sealed
    event, so nothing proves its chain."""
    failed = [
        f"{v.robot_id}/{v.session_id}: {', '.join(sorted({i.code for i in v.evidence}))}"
        for v in result.sessions
        if v.evidence
    ]
    unsealed = [
        f"{v.robot_id}/{v.session_id}"
        for v in result.sessions
        if any(i.code == "unsealed" for i in v.gaps)
    ]
    if failed:
        return _result("fail", f"{len(failed)} session(s) contradict their seals", failed)
    if unsealed or not result.sessions:
        return _result("not_checked", f"{len(unsealed)} session(s) have no sealed event", unsealed)
    return _result("pass", f"{len(result.sessions)} session(s): every hash and link recomputed")


def time(log_check: LogCheck, result: VerifyResult) -> Json:
    """No ``ts_regression`` flag (definitions.md 8) and no ``clock_skew`` or ``late_anchor``
    gap (10.6)."""
    regressions = Counter(
        f"{f.robot_id}/{f.session_id}" for f in log_check.flags if f.code == "ts_regression"
    )
    failures = [f"{key}: ts_regression x{count}" for key, count in sorted(regressions.items())]
    for verdict in result.sessions:
        codes = Counter(i.code for i in verdict.gaps if i.code in TIME_GAPS)
        failures += [f"{verdict.robot_id}/{verdict.session_id}: {code}" for code in sorted(codes)]
    return _result(
        "fail" if failures else "pass",
        "event times never go backwards and every verified token is inside its window",
        failures,
    )


def _started(session: Session, timeline: Timeline) -> int:
    """``task_start`` events inside the session window (definitions.md 2, rule 4)."""
    start, end = timeline.session_start_seq, timeline.session_end_seq
    if start is None:
        return 0
    return sum(
        1
        for e in session.events
        if e["type"] == "task_start" and e["seq"] > start and (end is None or e["seq"] < end)
    )


def tasks(log_check: LogCheck, timelines: Sequence[Timeline]) -> Json:
    """Tasks started = closed + not closed, and every definition is in the task registry
    (definitions.md 1, 5)."""
    sessions = {(s.robot_id, s.session_id): s for s in log_check.sessions}
    failures = []
    total = 0
    for t in timelines:
        started = _started(sessions[(t.robot_id, t.session_id)], t)
        closed = sum(1 for task in t.tasks if task.closed)
        unclosed = len(t.tasks) - closed
        total += len(t.tasks)
        if started != closed + unclosed:
            failures.append(
                f"{_name(t)}: {started} started, {closed} closed, {unclosed} not closed"
            )
        unknown = sum(1 for task in t.tasks if not task.definition_known)
        if unknown:
            failures.append(f"{_name(t)}: {unknown} task(s) with a definition not in the registry")
    return _result(
        "fail" if failures else "pass", f"{total} task(s) in {len(timelines)} session(s)", failures
    )


def bounds(groups: Mapping[str, Json]) -> Json:
    """``unknown_as_human`` <= point <= ``unknown_as_ai`` for the time share and the task
    rates of every group (definitions.md 7)."""
    failures = []
    checked = 0
    for group in sorted(groups):
        for metric in BOUNDED:
            block = groups[group][metric]
            point = block.get("estimate")
            low = block["unknown_as_human"].get("estimate")
            high = block["unknown_as_ai"].get("estimate")
            if point is None or low is None or high is None:
                continue
            checked += 1
            if not low - _SLACK <= point <= high + _SLACK:
                failures.append(f"{group} {metric}: {low} <= {point} <= {high} does not hold")
    if not checked:
        return _result("not_checked", "no metric with bounds has an estimate")
    return _result("fail" if failures else "pass", f"{checked} metric(s) checked", failures)


#: Reasons of 12.6 that say the two witnesses disagree (conditions 4 and 5).
DISAGREEMENT: Final = ("station_only_above_max", "disputed_above_max")


def witnesses(witness: Reconciliation | None) -> Json:
    """Every robot session a station session witnesses meets conditions 4 and 5 of 12.6;
    ``not_available`` when no robot session is witnessed (11.7)."""
    witnessed = [] if witness is None else [s for s in witness.sessions if s.stations]
    if not witnessed:
        return _result("not_available", "no robot session is witnessed by a station log")
    failures = [
        f"{s.robot_id}/{s.session_id}: {r.detail}"
        for s in witnessed
        for r in s.reasons
        if r.code in DISAGREEMENT
    ]
    return _result(
        "fail" if failures else "pass",
        f"{len(witnessed)} witnessed session(s): station-only, disputed and unwitnessed time "
        "within the maxima",
        failures,
    )


def invariants(  # noqa: PLR0913, PLR0917 -- every input of 11.7
    log_check: LogCheck,
    result: VerifyResult,
    timelines: Sequence[Timeline],
    census_status: str,
    groups: Mapping[str, Json],
    witness: Reconciliation | None = None,
) -> Json:
    """Every invariant of 11.7 by name."""
    census_detail = {
        "pass": "no session counter missing",
        "fail": "session counters missing (see the census)",
        "not_checked": "some session ids are not of the recorder's form",
    }[census_status]
    return {
        "partition": partition(timelines),
        "chain": chain(result),
        "time": time(log_check, result),
        "census": _result(census_status, census_detail),
        "tasks": tasks(log_check, timelines),
        "bounds": bounds(groups),
        "witnesses": witnesses(witness),
    }
