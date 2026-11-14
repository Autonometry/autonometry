"""Builders for witness tests (definitions.md 12): robot and station sessions as timelines.

Times are seconds after a fixed origin; every session sends a heartbeat each second with its
clock status, so no silence becomes ``unknown`` time.
"""

import math
from collections.abc import Sequence
from typing import Any, cast

from autonometry.checks import check_session
from autonometry.events import Event, Session, TrustStatus
from autonometry.timeline import Timeline, build_timeline
from autonometry.witness import Reconciliation, WitnessParams, reconcile
from timeline_helpers import FULL, RULES, Log, ts

PARAMS = WitnessParams.from_rules(RULES)
#: Seconds after 08:00 that time 0 of a scenario stands for, so sessions may start before it.
ORIGIN = 100.0
#: A change: (time, state) on ``whole_robot``, or (time, state, scope).
Change = tuple[float, str] | tuple[float, str, str]

_DEVICE = {
    "ai": ("ai:policy", "policy", None),
    "human_remote": ("op_a", "vr_remote", "human_initiated"),
    "shared": ("op_a", "vr_remote", "human_initiated"),
    "human_local": ("op_a", "joy_local", "human_initiated"),
    "scripted": ("script:replay", "replay", None),
    "safety_stop": (None, None, "safety"),
}


def _transition(log: Log, t: float, state: str, scope: str) -> None:
    actor, device, reason = _DEVICE.get(state, (None, None, None))
    log.tr(ORIGIN + t, state, reason, actor, device, scope)


def events(  # noqa: PLR0913 -- the knobs of one scenario
    changes: Sequence[Change],
    end: float,
    *,
    session_id: str = "s01",
    role: str = "robot",
    station_id: str = "pc_1",
    start: float = 0.0,
    scopes: Sequence[str] = ("whole_robot",),
    clock_error: float | None = 0.01,
    clock_source: str = "ntp",
    robot_id: str = "r01",
) -> list[Event]:
    """The events of one session from ``start`` to ``end`` (seconds), every scope ``idle`` at
    the start, the given changes, and a heartbeat each second reporting ``clock_source`` and
    ``clock_error`` (left out when ``None``)."""
    extra: dict[str, Any] = (
        {"role": "station", "station_id": station_id} if role == "station" else {}
    )
    log = Log(scopes, "idle", 1.0, **extra)
    log.events[0]["ts"] = ts(ORIGIN + start)
    beats = [float(s) for s in range(math.ceil(start), math.floor(end) + 1) if s > start]
    items: list[tuple[float, int, str, str]] = [(t, 1, "heartbeat", "") for t in beats if t < end]
    for change in changes:
        scope = change[2] if len(change) == 3 else "whole_robot"
        items.append((change[0], 0, change[1], scope))
    for t, kind, state, scope in sorted(items):
        if kind:
            clock: dict[str, Any] = {"clock_source": clock_source}
            if clock_error is not None:
                clock["clock_max_error_s"] = clock_error
            log.add(ORIGIN + t, "heartbeat", **clock)
        else:
            _transition(log, t, state, scope)
    log.end(ORIGIN + end)
    return [cast(Event, e | {"robot_id": robot_id, "session_id": session_id}) for e in log.events]


def session(changes: Sequence[Change], end: float, **options: Any) -> Timeline:
    """The timeline of :func:`events`, built with the reader's flags."""
    found = events(changes, end, **options)
    built = Session(found[0]["robot_id"], found[0]["session_id"], tuple(found))
    return build_timeline(built, FULL, RULES, check_session(built, RULES))


def station(changes: Sequence[Change], end: float, **options: Any) -> Timeline:
    """A station session (12.1): ``session_id`` ``st01`` unless given."""
    options.setdefault("session_id", "st01")
    return session(changes, end, role="station", **options)


def run(
    robots: Sequence[Timeline],
    stations: Sequence[Timeline],
    status: TrustStatus = "verified",
    station_status: TrustStatus | None = None,
) -> Reconciliation:
    """Reconcile with every robot session at ``status`` and every station session at
    ``station_status`` (``status`` when not given)."""
    return reconcile(
        robots,
        stations,
        PARAMS,
        {(t.robot_id, t.session_id): status for t in robots},
        {(t.robot_id, t.session_id): station_status or status for t in stations},
    )


def secs(ns: int) -> float:
    return ns / 1_000_000_000
