"""The session census of a report (definitions.md 11.3): counters missing between each robot's
lowest and highest session counter, for session ids of the recorder's form. Pure standard
library."""

from collections.abc import Iterable
from itertools import pairwise
from typing import Any, Literal

from autonometry.recorder.store import COUNTER_DIGITS, SESSION_ID_RE

CheckResult = Literal["pass", "fail", "not_checked"]


def counter_of(session_id: str) -> int | None:
    """The counter of a session id of the recorder's form, else ``None``."""
    match = SESSION_ID_RE.fullmatch(session_id)
    return int(match.group("counter")) if match else None


def _label(counter: int) -> str:
    return f"{counter:0{COUNTER_DIGITS}d}"


def missing_ranges(counters: Iterable[int]) -> list[tuple[int, int]]:
    """The runs ``(first, last)`` of counters absent between the lowest and highest present one,
    in order. Ranges, not single counters, keep the list as short as the sessions present."""
    present = sorted(set(counters))
    return [(low + 1, high - 1) for low, high in pairwise(present) if high > low + 1]


def _robot(robot_id: str, session_ids: list[str]) -> dict[str, Any]:
    counters = [c for s in session_ids if (c := counter_of(s)) is not None]
    gaps = missing_ranges(counters)
    return {
        "robot_id": robot_id,
        "sessions_checked": len(counters),
        "sessions_not_checked": sorted(s for s in session_ids if counter_of(s) is None),
        "lowest": _label(min(counters)) if counters else None,
        "highest": _label(max(counters)) if counters else None,
        "missing_count": sum(last - first + 1 for first, last in gaps),
        "missing_ranges": [[_label(first), _label(last)] for first, last in gaps],
    }


def census(sessions: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """The census of the ``(robot_id, session_id)`` pairs present in the logs (11.3). Its
    ``status`` is ``fail`` when a counter is missing, ``not_checked`` when no id has the
    recorder's form or some id has another form, else ``pass``."""
    by_robot: dict[str, list[str]] = {}
    for robot_id, session_id in sessions:
        by_robot.setdefault(robot_id, []).append(session_id)
    robots = [_robot(robot_id, by_robot[robot_id]) for robot_id in sorted(by_robot)]
    status: CheckResult
    if any(r["missing_count"] for r in robots):
        status = "fail"
    elif not any(r["sessions_checked"] for r in robots) or any(
        r["sessions_not_checked"] for r in robots
    ):
        status = "not_checked"
    else:
        status = "pass"
    return {"status": status, "robots": robots}
