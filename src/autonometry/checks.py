"""Structural checks of one session: the loader flags (definitions.md sections 1 to 3, 5 and 8).

Pure functions over validated events in ``seq`` order. A flag is a warning: the event stays and
later stages turn the problem into ``unknown`` time where the spec says so.
"""

import math
from collections.abc import Iterator, Mapping
from itertools import pairwise
from typing import Any, cast

from autonometry.events import Event, Flag, FlagCode, Session
from autonometry.spec import Rules
from autonometry.timestamps import ns_to_seconds, seconds_to_ns, ts_to_ns


def check_session(session: Session, rules: Rules) -> list[Flag]:
    """Return the loader flags of one session in the order found.

    Implements: ``no_session_start``, ``no_session_end``, ``repeated_session_marker`` (one flag
    per repeated marker) and ``event_outside_session`` (one flag per side with the count; the first
    ``session_start`` and ``session_end`` bound the session) (definitions.md 1, 2 rule 4);
    ``missing_initial_state`` (2 rule 1); ``undeclared_scope`` (1, control scope); ``seq_gap``,
    ``time_gap`` (2 rule 2; silence longer than the session's ``heartbeat_interval_s`` plus
    ``gap_tolerance_s``, in seconds); ``ts_regression`` (3, ``ts``); ``nested_task``,
    ``task_end_without_start``, ``task_not_closed`` (1, task; 5); ``distance_regression`` (8,
    ``distance_m`` in metres); ``ts_reconstructed`` (8, Time) and ``attribution_inferred`` (6.12),
    one flag per session with the count; ``clock_unsynced`` (8, Time; one flag per session).
    """

    def flag(seq: int, code: FlagCode, detail: str) -> Flag:
        return Flag(session.robot_id, session.session_id, seq, code, detail)

    events = session.events
    start = next((e for e in events if e["type"] == "session_start"), None)
    flags: list[Flag] = []
    if start is None:
        flags.append(flag(events[0]["seq"], "no_session_start", "no session_start event"))
    if not any(e["type"] == "session_end" for e in events):
        flags.append(flag(events[-1]["seq"], "no_session_end", "no session_end event"))
    if start is not None:
        flags += [flag(start["seq"], code, detail) for code, detail in _declarations(start)]
    heartbeat_s = rules.params.heartbeat_interval_s
    if start is not None:
        heartbeat_s = start.get("heartbeat_interval_s", heartbeat_s)
    max_silence_ns = seconds_to_ns(heartbeat_s + rules.params.gap_tolerance_s)
    scopes = frozenset(start.get("scopes", ())) if start is not None else None
    flags += [flag(seq, code, detail) for seq, code, detail in _markers(events)]
    flags += [flag(seq, code, detail) for seq, code, detail in _sequence(events, max_silence_ns)]
    flags += [flag(seq, code, detail) for seq, code, detail in _per_event(events, scopes)]
    flags += [flag(seq, code, detail) for seq, code, detail in _tasks(events)]
    flags += [flag(seq, code, detail) for seq, code, detail in _marked(events)]
    clock = _clock(events, rules.sealing.clock_tolerance_s)
    if clock is not None:
        flags.append(flag(*clock))
    return flags


_MARKERS: tuple[tuple[str, FlagCode], ...] = (
    ("x_ts_reconstructed", "ts_reconstructed"),
    ("x_attribution_inferred", "attribution_inferred"),
)

_UNSYNCED_SOURCES = frozenset({"none", "unknown"})


def _marked(events: tuple[Event, ...]) -> Iterator[tuple[int, FlagCode, str]]:
    """Events rebuilt after the fact (definitions.md 8, Time) or whose source was inferred
    (6.12): one flag per marker at its first event, with the count."""
    for field, code in _MARKERS:
        found = [e["seq"] for e in events if _extension(e, field) is True]
        if found:
            yield found[0], code, f"{_events(len(found))} with {field}: true"


def _clock(events: tuple[Event, ...], tolerance_s: float) -> tuple[int, FlagCode, str] | None:
    """``clock_unsynced`` when any heartbeat reports ``clock_source`` ``none`` or ``unknown``,
    reports no source, or reports ``clock_max_error_s`` above ``tolerance_s`` seconds or not at
    all; also when no heartbeat reports the clock (definitions.md 8, Time)."""
    heartbeats = [e for e in events if e["type"] == "heartbeat"]
    if not heartbeats:
        return events[0]["seq"], "clock_unsynced", "no heartbeat reports the clock status"
    unsynced = [
        e["seq"]
        for e in heartbeats
        if e.get("clock_source", "unknown") in _UNSYNCED_SOURCES
        or e.get("clock_max_error_s", math.inf) > tolerance_s
    ]
    if not unsynced:
        return None
    detail = (
        f"{len(unsynced)} of {len(heartbeats)} heartbeat(s) without a synchronised clock "
        f"(clock_source none, unknown or missing, or clock_max_error_s missing or above "
        f"{tolerance_s:g} s)"
    )
    return unsynced[0], "clock_unsynced", detail


def _events(count: int) -> str:
    return f"{count} event{'s' if count != 1 else ''}"


def _extension(event: Event, name: str) -> Any:
    return cast(Mapping[str, Any], event).get(name)


def _declarations(start: Event) -> Iterator[tuple[FlagCode, str]]:
    """Scopes declared at session_start against its initial_states and scope_weights."""
    scopes = set(start.get("scopes", ()))
    initial = set(start.get("initial_states", {}))
    for scope in sorted(scopes - initial):
        yield "missing_initial_state", f"scope {scope!r} has no initial state"
    for scope in sorted(initial - scopes):
        yield "undeclared_scope", f"initial_states names undeclared scope {scope!r}"
    for scope in sorted(set(start.get("scope_weights", {})) - scopes):
        yield "undeclared_scope", f"scope_weights names undeclared scope {scope!r}"


def _markers(events: tuple[Event, ...]) -> Iterator[tuple[int, FlagCode, str]]:
    """Repeated session_start or session_end, and events before the first session_start or after
    the first session_end."""
    first: dict[str, int] = {}
    for event in events:
        kind, seq = event["type"], event["seq"]
        if kind in ("session_start", "session_end"):
            if kind in first:
                yield seq, "repeated_session_marker", f"{kind} repeats the one at seq {first[kind]}"
            else:
                first[kind] = seq
    seqs = [event["seq"] for event in events]
    start, end = first.get("session_start"), first.get("session_end")
    outside = {
        "before session_start": [s for s in seqs if start is not None and s < start],
        "after session_end": [s for s in seqs if end is not None and s > end],
    }
    for where, found in outside.items():
        if found:
            yield found[0], "event_outside_session", f"{_events(len(found))} {where}"


def _sequence(
    events: tuple[Event, ...], max_silence_ns: int
) -> Iterator[tuple[int, FlagCode, str]]:
    """Gaps in seq, ts going backwards and silences between consecutive events."""
    limit = f"{ns_to_seconds(max_silence_ns):g} s"
    times_ns = [ts_to_ns(event["ts"]) for event in events]
    for (previous, event), (before_ns, now_ns) in zip(
        pairwise(events), pairwise(times_ns), strict=True
    ):
        before, seq = previous["seq"], event["seq"]
        if seq > before + 1:
            yield seq, "seq_gap", f"seq jumps from {before} to {seq}"
        elapsed = f"{ns_to_seconds(abs(now_ns - before_ns)):.3f} s"
        if now_ns < before_ns:
            yield seq, "ts_regression", f"ts is {elapsed} earlier than at seq {before}"
        elif now_ns - before_ns > max_silence_ns:
            yield seq, "time_gap", f"{elapsed} of silence after seq {before} (limit {limit})"


def _per_event(
    events: tuple[Event, ...], scopes: frozenset[str] | None
) -> Iterator[tuple[int, FlagCode, str]]:
    """Scopes not declared at session_start and cumulative distance going backwards."""
    last_distance: float | None = None
    for event in events:
        seq = event["seq"]
        scope = event.get("scope")
        if scopes is not None and scope is not None and scope not in scopes:
            yield seq, "undeclared_scope", f"scope {scope!r} is not declared at session_start"
        distance = event.get("distance_m")
        if distance is None:
            continue
        if last_distance is not None and distance < last_distance:
            yield seq, "distance_regression", f"distance_m falls from {last_distance} to {distance}"
        last_distance = distance


def _tasks(events: tuple[Event, ...]) -> Iterator[tuple[int, FlagCode, str]]:
    """Open tasks, derived only from task_start and task_end."""
    open_tasks: dict[str | None, int] = {}
    for event in events:
        seq, task_id = event["seq"], event["task_id"]
        if event["type"] == "task_start":
            if open_tasks:
                yield (
                    seq,
                    "nested_task",
                    f"task_start while the task opened at seq {min(open_tasks.values())} is open",
                )
            open_tasks.setdefault(task_id, seq)
        elif event["type"] == "task_end":
            if task_id in open_tasks:
                del open_tasks[task_id]
            else:
                yield seq, "task_end_without_start", "task_end for a task that is not open"
    for start_seq in sorted(open_tasks.values()):
        yield start_seq, "task_not_closed", "task_start has no task_end"
