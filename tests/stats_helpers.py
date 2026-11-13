"""Builders for statistics tests: timelines of several robots, sessions and dates."""

from typing import Any, cast

from autonometry.events import Event, Session
from autonometry.stats.facts import SessionFacts, session_facts
from autonometry.stats.metrics import Json, Options, group_metrics
from autonometry.timeline import Registries, Timeline, build_timeline
from timeline_helpers import FULL, RULES, Log

#: Few bootstrap replicates keep the tests fast; intervals are tested in test_stats_intervals.
FAST = Options(n_boot=50)


def build(
    log: Log,
    robot: str = "r01",
    session: str = "s01",
    date: str | None = None,
    registries: Registries = FULL,
) -> Timeline:
    """The timeline of ``log`` under other ids and, optionally, another date (``YYYY-MM-DD``)."""
    events: list[dict[str, Any]] = []
    for event in log.events:
        copy = dict(event) | {"robot_id": robot, "session_id": session}
        if date is not None:
            copy["ts"] = date + copy["ts"][10:]
        events.append(copy)
    found = Session(robot, session, tuple(cast(Event, e) for e in events))
    return build_timeline(found, registries, RULES)


def facts(
    log: Log, robot: str = "r01", session: str = "s01", date: str | None = None
) -> SessionFacts:
    return session_facts(build(log, robot, session, date), RULES)


def metrics(*sessions: SessionFacts, options: Options = FAST) -> Json:
    return group_metrics(list(sessions), options, RULES)


def approval(log: Log, t: float, dwell: float | None = None, actor: str = "op_a") -> Log:
    """A planned approval on the whole robot (its plan item is in the test plan)."""
    fields: dict[str, Any] = {
        "scope": "whole_robot",
        "actor_id": actor,
        "reason": "planned",
        "plan_item_id": "reset_scene",
    }
    if dwell is not None:
        fields["dwell_s"] = dwell
    return log.add(t, "approval", **fields)


def hint(log: Log, t: float, dwell: float | None = None) -> Log:
    fields: dict[str, Any] = {"scope": "whole_robot", "actor_id": "op_a"}
    if dwell is not None:
        fields["dwell_s"] = dwell
    return log.add(t, "hint", **fields)
