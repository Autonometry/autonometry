"""The per-session numbers the metrics need, read from a :class:`~autonometry.timeline.Timeline`.

The timeline (M4a) is the only source of per-session facts: nothing here reads events. Durations
stay integer nanoseconds where an exact comparison matters (zero human time, zero exposure) and are
otherwise converted to float seconds. Pure standard library.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from typing import Final

from autonometry.spec import Rules
from autonometry.timeline import Timeline, WholeSpan, dwell_shift, overlap_ns

NS_PER_S: Final = 1_000_000_000
#: Reasons of the narrow (DMV-like) count (definitions.md 3, Intervention episode).
NARROW_REASONS: Final = frozenset({"human_initiated", "safety"})
#: States a hidden intervention may sit in between AI control on both sides (definitions.md 7).
_HIDING: Final = frozenset({"unknown", "safety_stop"})
_NO_REASON: Final = "none"

Intervals = tuple[tuple[int, int], ...]


def seconds(ns: int | Fraction) -> float:
    """Nanoseconds as float seconds."""
    return float(Fraction(ns) / NS_PER_S) if isinstance(ns, Fraction) else ns / NS_PER_S


@dataclass(frozen=True, slots=True)
class TaskFacts:
    """One task (definitions.md 5, 6.3, 7). ``human_ns`` is human minutes per task's numerator:
    whole-robot human time inside, the unplanned reset time attributed to it and the dwell its
    approvals and hints moved from AI time. ``unknown_ns`` is whole-robot unknown time inside."""

    task_id: str
    definition: str
    known: bool
    closed: bool
    autonomous: bool
    contains_unknown: bool
    success: bool
    human_ns: int
    unknown_ns: int
    interventions: int
    duration_ns: int


@dataclass(frozen=True, slots=True)
class SessionFacts:
    """The numbers of one session the metrics use. Times in seconds unless named ``_ns``; counts
    are whole-robot unless named ``per_scope`` (definitions.md 6.6)."""

    robot_id: str
    session_id: str
    simulated: bool | None
    timeline: Timeline
    observed_s: float
    active_s: float
    idle_s: float
    unknown_s: float
    states_s: Mapping[str, float]
    moved_s: float
    scope_weighted_ai_s: float
    scope_weighted_active_s: float
    scope_weighted_moved_s: float
    unsupported_ai_s: float
    unsupported_active_s: float
    unsupported_moved_s: float
    supported_s: float
    reset_human_s: float
    segments: int
    interventions: int
    interventions_per_scope: int
    hidden_interventions: int
    kinds: Mapping[str, int]
    reasons: Mapping[str, int]
    narrow: int
    safety_stops: int
    safety_stops_human: int
    safety_stops_automatic: int
    safety_stops_per_scope: int
    marks_ns: tuple[int, ...]
    active: Intervals
    tasks: tuple[TaskFacts, ...]
    watched_s: float
    supervisor_s: float
    human_actor_s: float
    human_actors: tuple[str, ...]
    approval_dwells_s: tuple[float, ...]
    hint_dwells_s: tuple[float, ...]
    flag_counts: Mapping[str, int]
    unobserved: tuple[str, ...]

    @property
    def key(self) -> str:
        """The session as a cluster label: ``<robot_id>/<session_id>``."""
        return f"{self.robot_id}/{self.session_id}"

    @property
    def ai_s(self) -> float:
        return self.states_s["ai"]


def _intervals(spans: Iterable[WholeSpan], states: frozenset[str]) -> Intervals:
    """Whole-robot ``[start, end)`` intervals in ``states``, in time order, touching ones joined."""
    out: list[tuple[int, int]] = []
    for span in spans:
        if span.state not in states or span.end_ns <= span.start_ns:
            continue
        if out and out[-1][1] == span.start_ns:
            out[-1] = (out[-1][0], span.end_ns)
        else:
            out.append((span.start_ns, span.end_ns))
    return tuple(out)


def intersect(a: Sequence[tuple[int, int]], b: Sequence[tuple[int, int]]) -> Intervals:
    """The intersection of two sorted lists of disjoint ``[start, end)`` intervals."""
    out: list[tuple[int, int]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        low, high = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if low < high:
            out.append((low, high))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return tuple(out)


def _length(intervals: Iterable[tuple[int, int]]) -> int:
    return sum(end - start for start, end in intervals)


def hidden_interventions(spans: Sequence[WholeSpan]) -> int:
    """Runs of ``unknown`` and ``safety_stop`` on the whole-robot timeline that hold some unknown
    time and have ``ai`` or ``scripted`` on both sides: each may hide an intervention, so the
    ``unknown_as_human`` bound of interventions per active hour counts one (definitions.md 7)."""
    hiding = ("ai", "scripted")
    count = 0
    index = 0
    while index < len(spans):
        if spans[index].state not in _HIDING:
            index += 1
            continue
        end = index
        while end < len(spans) and spans[end].state in _HIDING:
            end += 1
        unknown = any(s.state == "unknown" and s.end_ns > s.start_ns for s in spans[index:end])
        before = index > 0 and spans[index - 1].state in hiding
        after = end < len(spans) and spans[end].state in hiding
        count += unknown and before and after
        index = end
    return count


def _segments(spans: Sequence[WholeSpan], rules: Rules) -> int:
    """Maximal runs of one active state on the whole-robot timeline (control segments)."""
    active = [s for s in spans if rules.states[s.state].active and s.end_ns > s.start_ns]
    joined = sum(1 for a, b in pairwise(active) if a.state == b.state and a.end_ns == b.start_ns)
    return len(active) - joined


def _tasks(timeline: Timeline) -> tuple[TaskFacts, ...]:
    unknown = [0] * len(timeline.tasks)
    for span in timeline.whole_spans:
        if span.task_index is not None and span.state == "unknown":
            unknown[span.task_index] += span.duration_ns
    return tuple(
        TaskFacts(
            task.task_id,
            task.task_definition_id,
            task.definition_known,
            task.closed,
            task.autonomous,
            task.contains_unknown,
            task.outcome == "success",
            task.human_ns + task.unplanned_reset_ns + task.dwell_moved_ns,
            unknown[index],
            task.interventions_whole_robot,
            task.end_ns - task.start_ns,
        )
        for index, task in enumerate(timeline.tasks)
    )


def _unsupported_moved_ns(timeline: Timeline, ai: Intervals) -> int:
    """Dwell moved from whole-robot AI time outside physical support (6.1, 6.15)."""
    supported_ai = intersect(ai, timeline.support)
    moved = 0
    for item in timeline.discrete:
        low = item.ts_ns - item.human_ns
        moved += overlap_ns(ai, low, item.ts_ns) - overlap_ns(supported_ai, low, item.ts_ns)
    return moved


def _stops_by_trigger(timeline: Timeline) -> Counter[str]:
    """Whole-robot safety stops by trigger; one stop group shares its actor (6.6, 6.7)."""
    first: dict[int, str] = {}
    for stop in timeline.safety_stops:
        first.setdefault(stop.whole_robot, stop.triggered_by)
    return Counter(first.values())


def session_facts(timeline: Timeline, rules: Rules) -> SessionFacts:
    """Read the numbers one session gives the metrics (definitions.md 4 to 7)."""
    whole = timeline.whole_robot
    active_states = frozenset(s for s, rule in rules.states.items() if rule.active)
    active = _intervals(timeline.whole_spans, active_states)
    ai = _intervals(timeline.whole_spans, frozenset({"ai"}))
    shift = dwell_shift(
        timeline,
        seconds(timeline.approval_min_dwell_ns),
        seconds(timeline.hint_min_dwell_ns),
    )
    weighted = timeline.scope_weighted
    counted = timeline.counted_discrete
    triggers = _stops_by_trigger(timeline)
    level_none = timeline.supervision.active_ns_by_level.get("none", 0)
    reasons = Counter(
        _NO_REASON if r is None else r
        for r in [i.reason for i in timeline.interventions] + [d.reason for d in counted]
    )
    return SessionFacts(
        robot_id=timeline.robot_id,
        session_id=timeline.session_id,
        simulated=timeline.simulated,
        timeline=timeline,
        observed_s=seconds(whole.observed_ns),
        active_s=seconds(whole.active_ns),
        idle_s=seconds(whole.idle_ns),
        unknown_s=seconds(whole.unknown_ns),
        states_s={state: seconds(value) for state, value in whole.states.items()},
        moved_s=seconds(shift.whole_robot_moved_ns),
        scope_weighted_ai_s=seconds(weighted.states["ai"]),
        scope_weighted_active_s=seconds(weighted.active_ns),
        scope_weighted_moved_s=seconds(shift.scope_weighted_moved_ns),
        unsupported_ai_s=seconds(_length(ai) - _length(intersect(ai, timeline.support))),
        unsupported_active_s=seconds(
            _length(active) - _length(intersect(active, timeline.support))
        ),
        unsupported_moved_s=seconds(_unsupported_moved_ns(timeline, ai)),
        supported_s=seconds(timeline.supported_ns),
        reset_human_s=seconds(timeline.reset_planned_ns + timeline.reset_unplanned_ns),
        segments=_segments(timeline.whole_spans, rules),
        interventions=timeline.interventions_whole_robot,
        interventions_per_scope=timeline.interventions_per_scope,
        hidden_interventions=hidden_interventions(timeline.whole_spans),
        kinds=Counter([i.kind for i in timeline.interventions] + [d.type for d in counted]),
        reasons=reasons,
        narrow=sum(1 for i in timeline.interventions if i.reason in NARROW_REASONS),
        safety_stops=timeline.safety_stops_whole_robot,
        safety_stops_human=triggers["human"],
        safety_stops_automatic=triggers["automatic"],
        safety_stops_per_scope=len(timeline.safety_stops),
        marks_ns=tuple(
            sorted([i.start_ns for i in timeline.interventions] + [d.ts_ns for d in counted])
        ),
        active=active,
        tasks=_tasks(timeline),
        watched_s=seconds(timeline.supervision.active_ns - level_none),
        supervisor_s=seconds(timeline.supervision.supervisor_ns),
        human_actor_s=seconds(timeline.human_actor_ns),
        human_actors=timeline.human_actors,
        approval_dwells_s=tuple(
            seconds(d.dwell_ns)
            for d in timeline.discrete
            if d.type == "approval" and d.dwell_ns is not None
        ),
        hint_dwells_s=tuple(
            seconds(d.dwell_ns)
            for d in timeline.discrete
            if d.type == "hint" and d.dwell_ns is not None
        ),
        flag_counts=Counter(flag.code for flag in timeline.flags),
        unobserved=timeline.unobserved,
    )


def moved_with_minimum_s(facts: SessionFacts, approval_min_s: float, hint_min_s: float) -> float:
    """Whole-robot dwell moved from AI time with other minimum dwells (6.1, 6.2), seconds."""
    return seconds(dwell_shift(facts.timeline, approval_min_s, hint_min_s).whole_robot_moved_ns)


def active_clock(active: Intervals, instant_ns: int) -> int:
    """Whole-robot active nanoseconds from the session start up to ``instant_ns``."""
    total = 0
    for start, end in active:
        if start >= instant_ns:
            break
        total += min(end, instant_ns) - start
    return total


def distance_at(samples: Sequence[tuple[int, float]], instant_ns: int) -> float:
    """Cumulative distance at an instant: the latest sample at or before it, or 0 before the
    first sample (``distance_m`` counts metres since ``session_start``, definitions.md 8)."""
    value = 0.0
    for time_ns, metres in samples:
        if time_ns > instant_ns:
            break
        value = metres
    return value
