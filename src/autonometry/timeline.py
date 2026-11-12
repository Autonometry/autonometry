"""The timeline of one session: exact, deterministic per-session facts (definitions.md 1 to 8).

:func:`build_timeline` turns one checked session (:class:`~autonometry.events.Session`) and the
registries that match its hashes into a frozen :class:`Timeline`: per-scope spans, the whole-robot
timeline, time totals, intervention episodes, approvals and hints, safety stops, tasks, resets,
supervision, human labour, physical support, distance and every flag raised. It is pure and uses
the standard library only. Times are integer nanoseconds since the Unix epoch and durations
integer nanoseconds, so every total is exact; scope-weighted totals are exact fractions. Seconds
appear only in :meth:`Timeline.to_json`.

How each rule is applied, in event (``seq``) order:

- Session window (2, rules 1 and 4): from the first ``session_start`` to the first ``session_end``
  after it, else to the last event. Events outside are ignored, and so is a repeated
  ``session_start``. A session without ``session_start`` has no observed time. A ``ts`` that goes
  backwards is read as the latest ``ts`` before it.
- Gaps (2, rule 2): silence longer than ``heartbeat_interval_s + gap_tolerance_s`` is ``unknown``
  from that limit after the earlier event to the later one; a gap in ``seq`` makes the whole
  interval ``unknown`` and every scope stays ``unknown`` until its next transition.
- Registry (6.10, 8): a transition into ``ai``, ``scripted``, ``human_*`` or ``shared`` takes the
  state of its declared device; a source not in the registry (or no registry) is ``human_remote``
  (``undeclared_device``); an initial state in those states has no named source and is
  ``human_remote`` too.
- Reasons (3): ``ai_requested_help`` without a ``help_request`` on the same scope within
  ``help_latency_max_s`` and ``planned`` without its plan item become ``human_initiated``, on
  transitions, approvals and hints; a transition the registry makes human without a reason takes
  ``human_initiated`` (6.10).
- Approval waits (6.1): an ``ai_requested_help`` approval more than ``approval_wait_max_s`` after
  the latest help request, with no transition or seq gap on its scope in between, turns the
  scope's ``ai`` time since the request into ``shared`` (``approval_wait_recoded``).
- Holds (6.8): ``idle`` while a task is open is ``unknown`` (``idle_in_task``).
- Interventions (3, 6.1, 6.2, 6.6, 6.7, 6.13): a run of ``unknown`` and ``safety_stop`` between
  AI control and human control does not hide one; an approval or hint inside an episode belongs
  to it. Safety stops (6.7), tasks (5; unplanned human time before a task makes it not
  autonomous), resets (6.3), supervision (6.4), several humans (6.5), support (6.15).
"""

import bisect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from itertools import pairwise
from types import MappingProxyType
from typing import Any, Final, Literal, cast

from autonometry.events import Event, Flag, Session, TimelineFlagCode
from autonometry.spec import Rules
from autonometry.timestamps import ns_to_seconds, ns_to_ts, seconds_to_ns, ts_to_ns

InterventionKind = Literal["takeover", "shared", "approval", "hint", "safety_stop", "preset"]
DiscreteType = Literal["approval", "hint"]
Trigger = Literal["human", "automatic"]
RegistryStatus = Literal["matched", "missing"]

#: States whose transitions are checked against the device registry (definitions.md 6.10).
CHECKED_STATES: Final = frozenset({"ai", "scripted", "human_local", "human_remote", "shared"})
_HUMAN_STAR: Final = frozenset({"human_local", "human_remote"})
_RESET_FROM: Final = frozenset({"idle", "unknown"})
#: States a scope passes through without leaving the state it held before (definitions.md 3).
_PASS_THROUGH: Final = frozenset({"unknown", "safety_stop"})
_LOCATION_STATE: Final = MappingProxyType({"local": "human_local", "remote": "human_remote"})
_KIND_STATE: Final = MappingProxyType({"ai": "ai", "script": "scripted"})
_WHOLE_ROBOT: Final = "whole_robot"
_NO_REASON: Final = "none"


# -- inputs -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Device:
    """One declared source (definitions.md 6.10): kind ``human``, ``ai``, ``script`` or
    ``support``; ``location`` (``local`` or ``remote``) for a human device."""

    device_id: str
    kind: str
    location: str | None = None


@dataclass(frozen=True, slots=True)
class Registries:
    """The registries that match a session's hashes; ``None`` when one is missing
    (definitions.md 8, Registries)."""

    devices: Mapping[str, Device] | None = None
    task_definitions: frozenset[str] | None = None
    plan_items: frozenset[str] | None = None

    @classmethod
    def from_documents(
        cls,
        devices: Mapping[str, Any] | None,
        tasks: Mapping[str, Any] | None,
        plan: Mapping[str, Any] | None,
    ) -> "Registries":
        """Build from parsed, schema-valid registry documents (``None`` for a missing one)."""
        return cls(
            None
            if devices is None
            else MappingProxyType(
                {
                    d["device_id"]: Device(d["device_id"], d["kind"], d.get("location"))
                    for d in devices["devices"]
                }
            ),
            None if tasks is None else frozenset(t["task_definition_id"] for t in tasks["tasks"]),
            None if plan is None else frozenset(i["plan_item_id"] for i in plan["items"]),
        )

    def status(self) -> Mapping[str, RegistryStatus]:
        """``matched`` or ``missing`` for ``devices``, ``tasks`` and ``plan``."""
        found = {
            "devices": self.devices,
            "tasks": self.task_definitions,
            "plan": self.plan_items,
        }
        status: dict[str, RegistryStatus] = {}
        for name, value in found.items():
            status[name] = "missing" if value is None else "matched"
        return MappingProxyType(status)


# -- results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Span:
    """A maximal piece of one scope's timeline with constant state, reason, actor, device and
    task, on ``[start_ns, end_ns)``; ``seq`` is the event that opened it (the ``session_start``
    for an initial state, the event after a gap for gap time)."""

    scope: str
    start_ns: int
    end_ns: int
    state: str
    reason: str | None
    actor_id: str | None
    device_id: str | None
    task_id: str | None
    task_index: int | None
    seq: int

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns


@dataclass(frozen=True, slots=True)
class WholeSpan:
    """A piece of the whole-robot timeline by the any-human rule (definitions.md 6.6).
    ``planned`` is true when every scope in a human state there has reason ``planned``."""

    start_ns: int
    end_ns: int
    state: str
    task_id: str | None
    task_index: int | None
    planned: bool

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns


@dataclass(frozen=True, slots=True)
class StateTotals[T: (int, Fraction)]:
    """Time per state over one timeline, nanoseconds; every state is present. ``active_ns``,
    ``idle_ns`` and ``unknown_ns`` follow definitions.md 4; the states sum to ``observed_ns``."""

    observed_ns: T
    states: Mapping[str, T]
    active_ns: T
    idle_ns: T
    unknown_ns: T


@dataclass(frozen=True, slots=True)
class Episode:
    """One intervention episode on one scope (definitions.md 3): from ``start_ns`` (the opening
    transition ``seq``) to ``end_ns`` (``end_seq``: the return to ``ai`` or to ``scripted`` no
    person started, the task end, or ``None`` at the session end). An automatic stop joined by a
    takeover opens at the stop, with ``takeover_seq`` set (6.7). ``human_ns`` is the time the scope
    spends in a human state inside; ``intervention`` indexes the whole-robot intervention."""

    scope: str
    start_ns: int
    end_ns: int
    seq: int
    end_seq: int | None
    kind: InterventionKind
    reason: str | None
    actor_id: str | None
    task_id: str | None
    task_index: int | None
    takeover_seq: int | None
    human_ns: int
    intervention: int


@dataclass(frozen=True, slots=True)
class Intervention:
    """One whole-robot intervention: episodes on several scopes opened by one actor within
    ``simultaneity_window_s`` of the first count once (definitions.md 6.6)."""

    start_ns: int
    seq: int
    kind: InterventionKind
    reason: str | None
    actor_id: str | None
    task_id: str | None
    task_index: int | None
    scopes: tuple[str, ...]
    episodes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DiscreteIntervention:
    """An approval (6.1) or a discrete hint (6.2). ``reason`` is after the checks of Section 3.
    ``dwell_ns`` is the logged ``dwell_s`` or ``None``; ``human_ns`` is ``max(dwell, min_dwell)``
    with the default minimum. The dwell sits on ``[ts - human_ns, ts)``; ``moved_ns`` is its
    overlap with the scope's ``ai`` time and ``whole_robot_moved_ns`` with whole-robot ``ai``
    time (6.1). ``in_episode``: its scope was inside an intervention episode, to which it belongs;
    it is not counted again (3)."""

    type: DiscreteType
    scope: str
    ts_ns: int
    seq: int
    actor_id: str | None
    reason: str | None
    dwell_ns: int | None
    task_id: str | None
    task_index: int | None
    human_ns: int
    moved_ns: int
    whole_robot_moved_ns: int
    in_episode: bool = False


@dataclass(frozen=True, slots=True)
class SafetyStop:
    """A transition into ``safety_stop`` from another state (definitions.md 6.7). It was
    triggered by a person when ``actor_id`` is set. ``from_state`` is the state the scope held
    before, looking through a run of ``unknown`` and ``safety_stop`` (3). ``takeover_seq`` is
    the first transition of the scope into a human state within ``takeover_window_s``, before it
    returned to ``ai`` or to ``scripted`` no person started or the task ended; ``whole_robot``
    indexes the whole-robot stop count."""

    scope: str
    ts_ns: int
    seq: int
    from_state: str
    triggered_by: Trigger
    actor_id: str | None
    task_id: str | None
    task_index: int | None
    takeover_seq: int | None
    whole_robot: int


@dataclass(frozen=True, slots=True)
class Task:
    """One task instance (definitions.md 1, 5, 6.3, 7).

    ``outcome`` is ``unknown`` when the task was not closed by its ``task_end``. ``autonomous``:
    no scope in a human state, no intervention, approval or hint inside, and no unplanned human
    time attributed to it (5, 6.3). ``contains_unknown``: some scope
    has ``unknown`` time inside. ``human_ns`` is whole-robot human time inside;
    ``dwell_moved_ns`` the whole-robot dwell moved from AI by its approvals and hints;
    ``unplanned_reset_ns`` the unplanned reset time since the previous task (6.3).
    """

    task_id: str
    task_definition_id: str
    definition_known: bool
    start_ns: int
    end_ns: int
    start_seq: int
    end_seq: int | None
    outcome: str
    closed: bool
    autonomous: bool
    contains_unknown: bool
    human_ns: int
    dwell_moved_ns: int
    interventions: int
    interventions_whole_robot: int
    unplanned_reset_ns: int


@dataclass(frozen=True, slots=True)
class Supervision:
    """Supervision over whole-robot active time (definitions.md 6.4): active time per level and
    supervisor-nanoseconds (``n_supervisors`` times active time)."""

    active_ns_by_level: Mapping[str, int]
    supervisor_ns: int
    active_ns: int

    @property
    def ratio(self) -> float | None:
        """Fraction of active time with a level above ``none``; ``None`` without active time."""
        if not self.active_ns:
            return None
        watched = self.active_ns - self.active_ns_by_level.get("none", 0)
        return watched / self.active_ns

    @property
    def supervisor_hours_per_robot_hour(self) -> float | None:
        """Supervisor-hours per active robot-hour; ``None`` without active time."""
        return self.supervisor_ns / self.active_ns if self.active_ns else None


@dataclass(frozen=True, slots=True)
class DwellShift:
    """Dwell of approvals and hints with given minimum dwells (definitions.md 6.1, 6.2):
    ``human_ns`` in total, and the part moved from AI to human time per scope, for the whole
    robot and scope-weighted."""

    human_ns: int
    moved_ns_by_scope: Mapping[str, int]
    whole_robot_moved_ns: int
    scope_weighted_moved_ns: Fraction


@dataclass(frozen=True, slots=True)
class Timeline:
    """Everything one session says, exactly (definitions.md 1 to 8). Times in nanoseconds."""

    robot_id: str
    session_id: str
    file: str | None
    start_ns: int | None
    end_ns: int | None
    session_start_seq: int | None
    session_end_seq: int | None
    simulated: bool | None
    unobserved: tuple[str, ...]
    hint_policy: str | None
    scopes: tuple[str, ...]
    weights: Mapping[str, Fraction]
    registries: Mapping[str, RegistryStatus]
    approval_min_dwell_ns: int
    hint_min_dwell_ns: int
    spans: tuple[Span, ...]
    whole_spans: tuple[WholeSpan, ...]
    per_scope: Mapping[str, StateTotals[int]]
    whole_robot: StateTotals[int]
    scope_weighted: StateTotals[Fraction]
    episodes: tuple[Episode, ...]
    interventions: tuple[Intervention, ...]
    discrete: tuple[DiscreteIntervention, ...]
    safety_stops: tuple[SafetyStop, ...]
    safety_stops_whole_robot: int
    tasks: tuple[Task, ...]
    reset_planned_ns: int
    reset_unplanned_ns: int
    supervision: Supervision
    human_actor_ns: int
    distinct_human_actors: int
    supported_ns: int
    distance_m: float | None
    flags: tuple[Flag, ...]

    @property
    def observed_ns(self) -> int:
        return self.whole_robot.observed_ns

    @property
    def counted_discrete(self) -> tuple[DiscreteIntervention, ...]:
        """Approvals and hints that count as interventions: those outside an episode (3)."""
        return tuple(d for d in self.discrete if not d.in_episode)

    @property
    def interventions_per_scope(self) -> int:
        """Episodes on every scope plus counted approvals and hints (definitions.md 3, 6.6)."""
        return len(self.episodes) + len(self.counted_discrete)

    @property
    def interventions_whole_robot(self) -> int:
        """Whole-robot interventions plus counted approvals and hints (definitions.md 3, 6.6)."""
        return len(self.interventions) + len(self.counted_discrete)

    def to_json(self) -> dict[str, Any]:
        """A JSON-serialisable form: durations in seconds, times as RFC 3339 strings with nine
        fraction digits; every list in a fixed order."""
        return _timeline_json(self)


# -- dwell --------------------------------------------------------------------------------------


def overlap_ns(intervals: Sequence[tuple[int, int]], low: int, high: int) -> int:
    """Nanoseconds of ``[low, high)`` covered by sorted, disjoint ``[start, end)`` intervals."""
    if high <= low:
        return 0
    index = bisect.bisect_right(intervals, low, key=lambda interval: interval[1])
    total = 0
    for start, end in intervals[index:]:
        if start >= high:
            break
        total += min(end, high) - max(start, low)
    return total


def dwell_human_ns(item: DiscreteIntervention, min_dwell_ns: int) -> int:
    """Human time of an approval or hint: ``max(dwell, min_dwell)`` (definitions.md 6.1, 6.2)."""
    return max(item.dwell_ns or 0, min_dwell_ns)


def dwell_shift(
    timeline: Timeline, approval_min_dwell_s: float, hint_min_dwell_s: float
) -> DwellShift:
    """Place every approval and hint dwell on ``[ts - max(dwell, min_dwell), ts)`` of its scope
    and return how much of it overlaps ``ai`` time, which moves from AI to human time
    (definitions.md 6.1, 6.2). Minimum dwells in seconds, for the sensitivity variants."""
    minimum = _min_dwell_ns(approval_min_dwell_s, hint_min_dwell_s)
    scope_ai, whole_ai = _ai_intervals(timeline.spans, timeline.whole_spans)
    human = 0
    moved = dict.fromkeys(timeline.scopes, 0)
    whole = 0
    for item in timeline.discrete:
        length = dwell_human_ns(item, minimum[item.type])
        human += length
        low = item.ts_ns - length
        if item.scope in moved:
            moved[item.scope] += overlap_ns(scope_ai.get(item.scope, []), low, item.ts_ns)
        whole += overlap_ns(whole_ai, low, item.ts_ns)
    weighted = sum(
        (timeline.weights.get(scope, Fraction(0)) * value for scope, value in moved.items()),
        Fraction(0),
    )
    return DwellShift(human, MappingProxyType(moved), whole, weighted)


def _min_dwell_ns(approval_s: float, hint_s: float) -> Mapping[str, int]:
    return {"approval": seconds_to_ns(approval_s), "hint": seconds_to_ns(hint_s)}


def _ai_intervals(
    spans: Iterable[Span], whole: Iterable[WholeSpan]
) -> tuple[dict[str, list[tuple[int, int]]], list[tuple[int, int]]]:
    """The ``ai`` intervals of each scope and of the whole robot, in time order."""
    whole_ai = [(s.start_ns, s.end_ns) for s in whole if s.state == "ai"]
    return _intervals_by_scope(spans, frozenset({"ai"})), whole_ai


# -- building -----------------------------------------------------------------------------------


@dataclass(slots=True)
class _ScopeState:
    """The declared state of one scope and the start of its current, not yet emitted span."""

    state: str
    reason: str | None
    actor_id: str | None
    device_id: str | None
    seq: int
    start_ns: int
    episode: int | None = None
    watch: int | None = None
    held: str | None = None


@dataclass(slots=True)
class _OpenEpisode:
    scope: str
    start_ns: int
    seq: int
    kind: InterventionKind
    reason: str | None
    actor_id: str | None
    task_index: int | None
    takeover_seq: int | None = None
    end_ns: int | None = None
    end_seq: int | None = None


@dataclass(slots=True)
class _OpenStop:
    scope: str
    ts_ns: int
    seq: int
    from_state: str
    actor_id: str | None
    task_index: int | None
    takeover_seq: int | None = None


@dataclass(slots=True)
class _OpenTask:
    task_id: str
    definition: str
    known: bool
    start_ns: int
    start_seq: int
    end_ns: int | None = None
    end_seq: int | None = None
    outcome: str = "unknown"
    closed: bool = False


@dataclass(frozen=True, slots=True)
class _Discrete:
    type: DiscreteType
    scope: str
    ts_ns: int
    seq: int
    actor_id: str | None
    reason: str | None
    dwell_ns: int | None
    task_index: int | None
    in_episode: bool


@dataclass(frozen=True, slots=True)
class _Repair:
    """An approval wait logged as ``ai``: ``[start_ns, end_ns)`` of ``scope`` becomes ``shared``
    with reason ``ai_requested_help`` (6.1)."""

    scope: str
    start_ns: int
    end_ns: int
    seq: int
    actor_id: str | None
    device_id: str | None


class _Builder:
    """Walks one session's events once, in ``seq`` order; the only mutable state."""

    def __init__(self, start: Event, registries: Registries, rules: Rules) -> None:
        params = rules.params
        self.rules = rules
        self.registries = registries
        self.start_ns = ts_to_ns(start["ts"])
        heartbeat_s = start.get("heartbeat_interval_s", params.heartbeat_interval_s)
        self.silence_ns = seconds_to_ns(heartbeat_s + params.gap_tolerance_s)
        self.help_ns = seconds_to_ns(params.help_latency_max_s)
        self.takeover_ns = seconds_to_ns(params.takeover_window_s)
        self.wait_ns = seconds_to_ns(params.approval_wait_max_s)
        self.flags: list[tuple[int, TimelineFlagCode, str]] = []
        self.spans: dict[str, list[Span]] = {}
        self.scopes: dict[str, _ScopeState] = {}
        self.tasks: list[_OpenTask] = []
        self.task: int | None = None
        self.episodes: list[_OpenEpisode] = []
        self.stops: list[_OpenStop] = []
        self.discrete: list[_Discrete] = []
        self.supervision: list[tuple[int, str, int]] = [(self.start_ns, "none", 0)]
        self.support_open: dict[str, int] = {}
        self.support: list[tuple[int, int]] = []
        self.distance_m: float | None = None
        self.help: dict[str, int] = {}
        self.quiet_since_help: dict[str, bool] = {}
        self.repairs: list[_Repair] = []
        self.idle_flagged: set[tuple[str, int]] = set()

    # flags and spans

    def flag(self, seq: int, code: TimelineFlagCode, detail: str) -> None:
        self.flags.append((seq, code, detail))

    def flag_idle(self, name: str, seq: int) -> None:
        """``idle_in_task`` once per scope and idle stretch: the later of the idle transition and
        the task start (6.8)."""
        if (name, seq) not in self.idle_flagged:
            self.idle_flagged.add((name, seq))
            self.flag(
                seq,
                "idle_in_task",
                f"scope {name!r} is declared idle while a task is open; counted as unknown",
            )

    def effective(self, scope: _ScopeState) -> str:
        """The declared state, except that ``idle`` inside an open task is unknown (6.8)."""
        if scope.state == "idle" and self.task is not None:
            return self.rules.holds.idle_in_open_task_becomes
        return scope.state

    def hold(self, scope: _ScopeState) -> None:
        """Remember the counted state the scope is about to leave unless it is ``unknown`` or
        ``safety_stop`` (idle inside an open task counts as unknown) (3)."""
        state = self.effective(scope)
        if state not in _PASS_THROUGH:
            scope.held = state

    def origin(self, scope: _ScopeState, previous: str) -> str:
        """The state a transition leaves for Section 3 cases a, c, d and 6.7: through a run of
        ``unknown`` and ``safety_stop``, the counted state held before it."""
        if previous in _PASS_THROUGH and scope.held is not None:
            return scope.held
        return previous

    def _task_id(self) -> str | None:
        return None if self.task is None else self.tasks[self.task].task_id

    def split(self, name: str, at_ns: int) -> None:
        """Emit the scope's current span up to ``at_ns`` and start a new one there."""
        scope = self.scopes[name]
        if at_ns > scope.start_ns:
            state = self.effective(scope)
            if state != scope.state and self.task is not None:
                self.flag_idle(name, max(scope.seq, self.tasks[self.task].start_seq))
            self.spans[name].append(
                Span(
                    name,
                    scope.start_ns,
                    at_ns,
                    state,
                    scope.reason,
                    scope.actor_id,
                    scope.device_id,
                    self._task_id(),
                    self.task,
                    scope.seq,
                )
            )
        scope.start_ns = at_ns

    def split_all(self, at_ns: int) -> None:
        for name in self.scopes:
            self.split(name, at_ns)

    # session start

    def open_scopes(self, start: Event, names: Sequence[str]) -> None:
        """Initial states (2, rule 1); an initial control state has no named source (6.10)."""
        initial = start.get("initial_states", {})
        seq = start["seq"]
        if self.registries.devices is None:
            self.flag(seq, "registry_missing", "no device registry: every source is undeclared")
        for name in names:
            state = initial.get(name, "unknown")
            if state in CHECKED_STATES:
                self.flag(
                    seq,
                    "undeclared_device",
                    f"scope {name!r}: initial state {state} has no named source; "
                    f"counted as {self.rules.device_location.undeclared_device_state}",
                )
                state = self.rules.device_location.undeclared_device_state
            self.scopes[name] = _ScopeState(state, None, None, None, seq, self.start_ns)
            self.spans[name] = []

    # gaps

    def gap(self, before_ns: int, now_ns: int, seq: int, lost: bool) -> None:
        """Unknown time for a silence or a gap in seq (2, rule 2)."""
        cut = before_ns if lost else before_ns + self.silence_ns
        if lost:
            self.quiet_since_help = dict.fromkeys(self.quiet_since_help, False)
        for name, scope in self.scopes.items():
            if lost:
                self.hold(scope)
            self.split(name, cut)
            if now_ns > cut:
                self.spans[name].append(
                    Span(
                        name,
                        cut,
                        now_ns,
                        "unknown",
                        None,
                        None,
                        None,
                        self._task_id(),
                        self.task,
                        seq,
                    )
                )
            scope.start_ns = now_ns
            if lost:
                scope.state, scope.reason, scope.actor_id, scope.device_id = (
                    "unknown",
                    None,
                    None,
                    None,
                )
                scope.seq = seq

    # transitions

    def mapped_state(self, seq: int, scope: str, logged: str, device_id: str | None) -> str:
        """The state the registry gives a transition (6.10)."""
        if logged not in CHECKED_STATES:
            return logged
        undeclared = self.rules.device_location.undeclared_device_state
        devices = self.registries.devices
        device = None if devices is None or device_id is None else devices.get(device_id)
        if device is None:
            if device_id is None:
                source = "names no device"
            elif devices is None:
                source = f"from device {device_id!r} with no device registry"
            else:
                source = f"from device {device_id!r}, which is not in the device registry"
            self.flag(
                seq,
                "undeclared_device",
                f"scope {scope!r}: {logged} {source}; counted as {undeclared}",
            )
            return undeclared
        if device.kind == "support":
            self.flag(
                seq,
                "registry_mismatch",
                f"scope {scope!r}: {logged} from support device {device_id!r} is not control; "
                f"counted as {undeclared}",
            )
            return undeclared
        if device.kind == "human":
            located = _LOCATION_STATE.get(device.location or "", undeclared)
            state = "shared" if logged == "shared" else located
        else:
            state = _KIND_STATE.get(device.kind, undeclared)
        if state != logged:
            self.flag(
                seq,
                "registry_mismatch",
                f"scope {scope!r}: {logged} from {device.kind} device {device_id!r}; "
                f"counted as {state}",
            )
        return state

    def recoded_reason(self, event: Event, now_ns: int, state: str | None = None) -> str | None:
        """The reason of a transition, approval or hint after Section 3: ``ai_requested_help``
        without a recent help request and ``planned`` without its plan item become
        ``human_initiated``; so does no reason on a transition into ``state`` ``human_local`` or
        ``human_remote``, which only the registry can give (6.10)."""
        reason = event.get("reason")
        seq = event["seq"]
        asked = self.help.get(event["scope"])
        if reason == "ai_requested_help" and (asked is None or now_ns - asked > self.help_ns):
            self.flag(
                seq,
                "help_reason_recoded",
                f"no help_request on scope {event.get('scope')!r} within "
                f"{self.rules.params.help_latency_max_s:g} s; recoded as human_initiated",
            )
            return "human_initiated"
        if reason == "planned":
            plan = self.registries.plan_items
            item = event.get("plan_item_id")
            if plan is None or item not in plan:
                problem = "cannot be checked: no plan" if plan is None else "is not in the plan"
                self.flag(
                    seq,
                    "unplanned",
                    f"plan item {item!r} {problem}; recoded as human_initiated",
                )
                return "human_initiated"
        if reason is None and state in _HUMAN_STAR:
            return "human_initiated"
        return reason

    def transition(self, event: Event, now_ns: int) -> None:
        name = event["scope"]
        scope = self.scopes[name]
        seq = event["seq"]
        state = self.mapped_state(seq, name, event["state"], event.get("device_id"))
        reason = self.recoded_reason(event, now_ns, state)
        actor = event.get("actor_id")
        previous = self.effective(scope)
        origin = self.origin(scope, previous)
        self.quiet_since_help[name] = False
        self.watch_stop(scope, state, reason, actor, previous, now_ns, seq)
        self.episode_change(name, scope, (previous, origin), state, reason, actor, now_ns, seq)
        if state == "safety_stop" and previous != "safety_stop":
            scope.watch = len(self.stops)
            self.stops.append(_OpenStop(name, now_ns, seq, origin, actor, self.task))
        self.hold(scope)
        self.split(name, now_ns)
        scope.state, scope.reason, scope.actor_id = state, reason, actor
        scope.device_id, scope.seq = event.get("device_id"), seq

    def returns(self, state: str, reason: str | None) -> bool:
        """The scope returns to ``ai``, or to ``scripted`` that no person started (3)."""
        rules = self.rules.interventions
        if state not in rules.episode_ends_when_scope_returns_to:
            return False
        return not (state in rules.person_started_states and reason == "human_initiated")

    def is_reset(self, previous: str, state: str, reason: str | None) -> bool:
        """Between tasks, ``human_*`` from ``idle``/``unknown`` or ``planned`` is a reset (3)."""
        return (
            self.task is None
            and state in _HUMAN_STAR
            and (previous in _RESET_FROM or reason == "planned")
        )

    def watch_stop(  # noqa: PLR0913, PLR0917 -- the facts of one transition
        self,
        scope: _ScopeState,
        state: str,
        reason: str | None,
        actor: str | None,
        previous: str,
        now_ns: int,
        seq: int,
    ) -> None:
        """A takeover within ``takeover_window_s`` joins the stop before it (6.7)."""
        if scope.watch is None:
            return
        stop = self.stops[scope.watch]
        if now_ns - stop.ts_ns > self.takeover_ns or self.returns(state, reason):
            scope.watch = None
            return
        if state not in self.rules.interventions.into_states:
            return
        stop.takeover_seq = seq
        scope.watch = None
        automatic = stop.actor_id is None
        if (
            automatic
            and stop.from_state in self.rules.interventions.from_states
            and scope.episode is None
            and not self.is_reset(previous, state, reason)
        ):
            scope.episode = len(self.episodes)
            self.episodes.append(
                _OpenEpisode(
                    stop.scope,
                    stop.ts_ns,
                    stop.seq,
                    "takeover",
                    self.rules.reasons.safety_stop_reason,
                    actor,
                    stop.task_index,
                    takeover_seq=seq,
                )
            )

    def episode_change(  # noqa: PLR0913, PLR0917 -- the facts of one transition
        self,
        name: str,
        scope: _ScopeState,
        left: tuple[str, str],
        state: str,
        reason: str | None,
        actor: str | None,
        now_ns: int,
        seq: int,
    ) -> None:
        """Open or close an intervention episode (3, cases a, c and d; 6.7; 6.13). ``left`` is
        the state just before and the state held before a run of unknown and stops; the first
        decides resets (6.3), the second whether the scope left ``ai`` or ``scripted``."""
        if scope.episode is not None:
            if self.returns(state, reason):
                self.close_episode(scope, now_ns, seq)
            return
        previous, origin = left
        rules = self.rules.interventions
        if origin not in rules.from_states:
            return
        kind: InterventionKind | None = None
        if state in rules.into_states:
            if not self.is_reset(previous, state, reason):
                kind = "shared" if state == "shared" else "takeover"
        elif state == "safety_stop" and actor is not None:
            kind, reason = "safety_stop", self.rules.reasons.safety_stop_reason
        elif state == "scripted" and reason == "human_initiated":
            kind = "preset"
        if kind is not None:
            scope.episode = len(self.episodes)
            self.episodes.append(_OpenEpisode(name, now_ns, seq, kind, reason, actor, self.task))

    def close_episode(self, scope: _ScopeState, now_ns: int, seq: int | None) -> None:
        if scope.episode is not None:
            episode = self.episodes[scope.episode]
            episode.end_ns, episode.end_seq = now_ns, seq
            scope.episode = None

    # tasks

    def end_task(self, now_ns: int, seq: int | None, outcome: str, closed: bool) -> None:
        """Close the open task at the event ``seq``; its episodes end there and stops stop
        waiting for a takeover (3). An unclosed task keeps outcome ``unknown`` and no end seq."""
        if self.task is None:
            return
        self.split_all(now_ns)
        task = self.tasks[self.task]
        task.end_ns, task.end_seq = now_ns, seq if closed else None
        task.outcome, task.closed = outcome, closed
        for scope in self.scopes.values():
            self.close_episode(scope, now_ns, seq)
            scope.watch = None
        self.task = None

    def task_start(self, event: Event, now_ns: int) -> None:
        seq = event["seq"]
        self.end_task(now_ns, seq, "unknown", False)
        self.split_all(now_ns)
        for scope in self.scopes.values():
            self.hold(scope)
        definition = event.get("task_definition_id", "")
        known = self.registries.task_definitions
        if known is None or definition not in known:
            problem = (
                "cannot be checked: no task registry"
                if known is None
                else "is not in the task registry"
            )
            self.flag(seq, "unknown_task_definition", f"task definition {definition!r} {problem}")
        task_id = event["task_id"] or ""
        self.task = len(self.tasks)
        self.tasks.append(
            _OpenTask(task_id, definition, known is not None and definition in known, now_ns, seq)
        )

    def task_end(self, event: Event, now_ns: int) -> None:
        if self.task is not None and self.tasks[self.task].task_id == event["task_id"]:
            self.end_task(now_ns, event["seq"], event.get("outcome", "unknown"), True)

    # other events

    def discrete_event(self, event: Event, now_ns: int) -> None:
        """An approval or hint: its reason checked (3), whether it falls inside an episode (3),
        and an approval wait logged as ``ai`` repaired (6.1)."""
        kind = cast(DiscreteType, event["type"])
        name = event["scope"]
        dwell = event.get("dwell_s")
        reason = self.recoded_reason(event, now_ns)
        scope = self.scopes.get(name)
        if kind == "approval" and reason == "ai_requested_help" and scope is not None:
            self.wait_repair(event, name, now_ns)
        self.discrete.append(
            _Discrete(
                kind,
                name,
                now_ns,
                event["seq"],
                event.get("actor_id"),
                reason,
                None if dwell is None else seconds_to_ns(dwell),
                self.task,
                scope is not None and scope.episode is not None,
            )
        )

    def wait_repair(self, event: Event, name: str, now_ns: int) -> None:
        """A wait longer than ``approval_wait_max_s`` with no transition or seq gap since the
        help request becomes ``shared`` (6.1)."""
        asked = self.help.get(name)
        if asked is None or now_ns - asked <= self.wait_ns or not self.quiet_since_help[name]:
            return
        seq = event["seq"]
        self.repairs.append(
            _Repair(name, asked, now_ns, seq, event.get("actor_id"), event.get("device_id"))
        )
        self.flag(
            seq,
            "approval_wait_recoded",
            f"approval {ns_to_seconds(now_ns - asked):.3f} s after the help_request on scope "
            f"{name!r} (limit {self.rules.params.approval_wait_max_s:g} s); the ai time since "
            "the help_request counted as shared with reason ai_requested_help",
        )

    def support_event(self, event: Event, now_ns: int) -> None:
        key = event.get("scope") or _WHOLE_ROBOT
        if event["type"] == "support_start":
            self.support_open.setdefault(key, now_ns)
        elif key in self.support_open:
            self.support.append((self.support_open.pop(key), now_ns))

    def handle(self, event: Event, now_ns: int) -> None:
        kind = event["type"]
        if kind == "transition":
            self.transition(event, now_ns)
        elif kind in ("approval", "hint"):
            self.discrete_event(event, now_ns)
        elif kind == "help_request":
            self.help[event["scope"]] = now_ns
            self.quiet_since_help[event["scope"]] = True
        elif kind == "task_start":
            self.task_start(event, now_ns)
        elif kind == "task_end":
            self.task_end(event, now_ns)
        elif kind == "supervision":
            self.supervision.append((now_ns, event["level"], event["n_supervisors"]))
        elif kind in ("support_start", "support_end"):
            self.support_event(event, now_ns)
        elif kind == "heartbeat" and "distance_m" in event:
            self.distance_m = max(self.distance_m or 0.0, event["distance_m"])

    def finish(self, end_ns: int, end_seq: int | None) -> None:
        self.end_task(end_ns, end_seq, "unknown", False)
        self.split_all(end_ns)
        for scope in self.scopes.values():
            self.close_episode(scope, end_ns, end_seq)
        self.support += [(start, end_ns) for start in self.support_open.values()]
        self.support_open.clear()


def _window(session: Session) -> tuple[Event | None, list[Event], Event | None]:
    """The first ``session_start``, the events after it up to the first ``session_end`` after
    it, and that ``session_end`` (2, rule 4). A repeated ``session_start`` stays in the body,
    where it changes nothing but keeps ``seq`` continuous."""
    start = next((e for e in session.events if e["type"] == "session_start"), None)
    if start is None:
        return None, [], None
    body: list[Event] = []
    for event in session.events:
        if event["seq"] <= start["seq"]:
            continue
        if event["type"] == "session_end":
            return start, body, event
        body.append(event)
    return start, body, None


def _scope_names(start: Event, body: Iterable[Event]) -> tuple[str, ...]:
    """Declared scopes in their order, then scopes only transitions name, sorted (1)."""
    declared = list(start.get("scopes", ()))
    extra = {e["scope"] for e in body if e["type"] == "transition"} - set(declared)
    return (*declared, *sorted(extra))


def build_timeline(
    session: Session, registries: Registries, rules: Rules, log_flags: Iterable[Flag] = ()
) -> Timeline:
    """Build the timeline of one session (definitions.md 1 to 8) from its events in ``seq``
    order, the registries matching its hashes, and the spec rules. ``log_flags`` are the flags
    the reader raised for the session; they are kept with the timeline's own, sorted by
    ``(seq, code, detail)``. Never raises on a valid session."""
    start, body, end = _window(session)
    if start is None:
        return _empty(session, registries, rules, log_flags)
    builder = _Builder(start, registries, rules)
    names = _scope_names(start, body)
    builder.open_scopes(start, names)
    last_ns, last_seq = builder.start_ns, start["seq"]
    for event in [*body, *([] if end is None else [end])]:
        now_ns = max(ts_to_ns(event["ts"]), last_ns)
        lost = event["seq"] > last_seq + 1
        if lost or now_ns - last_ns > builder.silence_ns:
            builder.gap(last_ns, now_ns, event["seq"], lost)
        if event is not end:
            builder.handle(event, now_ns)
        last_ns, last_seq = now_ns, event["seq"]
    builder.finish(last_ns, None if end is None else end["seq"])
    return _assemble(session, start, end, builder, registries, rules, log_flags)


# -- assembling ---------------------------------------------------------------------------------


def _empty(
    session: Session, registries: Registries, rules: Rules, log_flags: Iterable[Flag]
) -> Timeline:
    """A session without ``session_start`` has no observed time (2, rule 4)."""
    zero = _totals_int([], rules)
    return Timeline(
        session.robot_id,
        session.session_id,
        session.file,
        None,
        None,
        None,
        None,
        None,
        (),
        None,
        (),
        MappingProxyType({}),
        registries.status(),
        seconds_to_ns(rules.params.approval_min_dwell_s),
        seconds_to_ns(rules.params.hint_min_dwell_s),
        (),
        (),
        MappingProxyType({}),
        zero,
        _weighted({}, {}, rules),
        (),
        (),
        (),
        (),
        0,
        (),
        0,
        0,
        Supervision(MappingProxyType({}), 0, 0),
        0,
        0,
        0,
        None,
        _sorted_flags(session, [], log_flags),
    )


def _sorted_flags(
    session: Session,
    own: Iterable[tuple[int, TimelineFlagCode, str]],
    log_flags: Iterable[Flag],
) -> tuple[Flag, ...]:
    flags = [*log_flags, *(Flag(session.robot_id, session.session_id, *f) for f in own)]
    return tuple(sorted(flags, key=lambda f: (f.seq, f.code, f.detail)))


def _totals_int(spans: Iterable[Span | WholeSpan], rules: Rules) -> StateTotals[int]:
    states = dict.fromkeys(rules.states, 0)
    for span in spans:
        states[span.state] += span.duration_ns
    return StateTotals(
        sum(states.values()),
        MappingProxyType(states),
        sum(v for s, v in states.items() if rules.states[s].active),
        sum(v for s, v in states.items() if rules.states[s].category == "inactive"),
        sum(v for s, v in states.items() if rules.states[s].category == "unmeasured"),
    )


def _weights(start: Event, scopes: Sequence[str]) -> Mapping[str, Fraction]:
    """Declared ``scope_weights`` normalised when they cover every scope, else 1/K (6.6)."""
    declared = start.get("scope_weights") or {}
    if scopes and all(scope in declared for scope in scopes):
        raw = {scope: Fraction(declared[scope]) for scope in scopes}
    else:
        raw = dict.fromkeys(scopes, Fraction(1))
    total = sum(raw.values(), Fraction(0))
    return MappingProxyType({scope: value / total for scope, value in raw.items()})


def _weighted(
    per_scope: Mapping[str, StateTotals[int]], weights: Mapping[str, Fraction], rules: Rules
) -> StateTotals[Fraction]:
    def mix(pick: Any) -> Fraction:
        return sum(
            (weights[scope] * pick(totals) for scope, totals in per_scope.items()), Fraction(0)
        )

    states = {state: mix(lambda t, s=state: t.states[s]) for state in rules.states}
    return StateTotals(
        mix(lambda t: t.observed_ns),
        MappingProxyType(states),
        mix(lambda t: t.active_ns),
        mix(lambda t: t.idle_ns),
        mix(lambda t: t.unknown_ns),
    )


def _whole_spans(
    spans: Mapping[str, Sequence[Span]], rules: Rules, human: frozenset[str]
) -> list[WholeSpan]:
    """The whole-robot timeline by the any-human rule (6.6), merged where nothing changes."""
    lists = [list(s) for s in spans.values() if s]
    if not lists:
        return []
    bounds = sorted({span.start_ns for spans_ in lists for span in spans_} | {lists[0][-1].end_ns})
    position = [0] * len(lists)
    out: list[WholeSpan] = []
    for low, high in pairwise(bounds):
        current: list[Span] = []
        for index, scope_spans in enumerate(lists):
            while scope_spans[position[index]].end_ns <= low:
                position[index] += 1
            current.append(scope_spans[position[index]])
        state = _whole_state([span.state for span in current], rules)
        in_human = [span for span in current if span.state in human]
        planned = bool(in_human) and all(span.reason == "planned" for span in in_human)
        task = current[0]
        if (
            out
            and out[-1].end_ns == low
            and (out[-1].state, out[-1].task_index, out[-1].planned)
            == (state, task.task_index, planned)
        ):
            last = out[-1]
            out[-1] = WholeSpan(
                last.start_ns, high, state, last.task_id, last.task_index, last.planned
            )
        else:
            out.append(WholeSpan(low, high, state, task.task_id, task.task_index, planned))
    return out


def _whole_state(states: Sequence[str], rules: Rules) -> str:
    """First state of the precedence held by any scope; idle only when all are (6.6)."""
    held = set(states)
    for state in rules.whole_robot.precedence:
        if state in held:
            return state
    if rules.whole_robot.idle_requires_all_scopes and held == {"idle"}:
        return "idle"
    return rules.whole_robot.fallback


def _intervals_by_scope(
    spans: Iterable[Span], states: frozenset[str]
) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for span in spans:
        if span.state in states:
            out.setdefault(span.scope, []).append((span.start_ns, span.end_ns))
    return out


def _union_ns(intervals: Iterable[tuple[int, int]]) -> int:
    total = 0
    reach: int | None = None
    for start, end in sorted(intervals):
        if reach is None or start >= reach:
            total += end - start
            reach = end
        elif end > reach:
            total += end - reach
            reach = end
    return total


def _group(items: Sequence[tuple[int, str, str | None]], window_ns: int) -> tuple[list[int], int]:
    """Group ``(start, scope, actor)`` items in start order: an item joins the open group of the
    same actor when it starts within ``window_ns`` of that group's first item and its scope is
    not in it yet (6.6). Returns each item's group and the number of groups."""
    order = sorted(range(len(items)), key=lambda i: (items[i][0], items[i][1]))
    group_of = [0] * len(items)
    groups: list[tuple[int, str | None, set[str]]] = []
    for index in order:
        start, scope, actor = items[index]
        found: int | None = None
        for candidate in range(len(groups) - 1, -1, -1):
            first, owner, scopes = groups[candidate]
            if start - first > window_ns:
                break
            if owner == actor and scope not in scopes:
                found = candidate
                break
        if found is None:
            found = len(groups)
            groups.append((start, actor, set()))
        groups[found][2].add(scope)
        group_of[index] = found
    return group_of, len(groups)


def _assemble(  # noqa: PLR0913, PLR0917 -- one place that puts the timeline together
    session: Session,
    start: Event,
    end: Event | None,
    b: _Builder,
    registries: Registries,
    rules: Rules,
    log_flags: Iterable[Flag],
) -> Timeline:
    human = frozenset(s for s, rule in rules.states.items() if rule.category == "human")
    spans = {name: _repaired(scope_spans, b.repairs) for name, scope_spans in b.spans.items()}
    all_spans = tuple(span for scope_spans in spans.values() for span in scope_spans)
    whole = _whole_spans(spans, rules, human)
    per_scope = {name: _totals_int(scope_spans, rules) for name, scope_spans in spans.items()}
    weights = _weights(start, tuple(spans))
    window_ns = seconds_to_ns(rules.params.simultaneity_window_s)
    human_by_scope = _intervals_by_scope(all_spans, human)

    def task_id(index: int | None) -> str | None:
        return None if index is None else b.tasks[index].task_id

    episode_groups, _ = _group([(e.start_ns, e.scope, e.actor_id) for e in b.episodes], window_ns)
    episodes = tuple(
        Episode(
            e.scope,
            e.start_ns,
            _end(e),
            e.seq,
            e.end_seq,
            e.kind,
            e.reason,
            e.actor_id,
            task_id(e.task_index),
            e.task_index,
            e.takeover_seq,
            overlap_ns(human_by_scope.get(e.scope, []), e.start_ns, _end(e)),
            episode_groups[i],
        )
        for i, e in enumerate(b.episodes)
    )
    interventions = _interventions(episodes)
    discrete = _discrete(b, task_id, rules, all_spans, whole, tuple(spans))
    stop_groups, stop_count = _group([(s.ts_ns, s.scope, s.actor_id) for s in b.stops], window_ns)
    stops = tuple(
        SafetyStop(
            s.scope,
            s.ts_ns,
            s.seq,
            s.from_state,
            "automatic" if s.actor_id is None else "human",
            s.actor_id,
            task_id(s.task_index),
            s.task_index,
            s.takeover_seq,
            stop_groups[i],
        )
        for i, s in enumerate(b.stops)
    )
    planned, unplanned = _resets(whole, human)
    return Timeline(
        session.robot_id,
        session.session_id,
        session.file,
        b.start_ns,
        whole[-1].end_ns if whole else b.start_ns,
        start["seq"],
        None if end is None else end["seq"],
        start.get("simulated"),
        tuple(start.get("unobserved", ())),
        start.get("hint_policy"),
        tuple(spans),
        weights,
        registries.status(),
        seconds_to_ns(rules.params.approval_min_dwell_s),
        seconds_to_ns(rules.params.hint_min_dwell_s),
        all_spans,
        tuple(whole),
        MappingProxyType(per_scope),
        _totals_int(whole, rules),
        _weighted(per_scope, weights, rules),
        episodes,
        interventions,
        discrete,
        stops,
        stop_count,
        _tasks(b, all_spans, whole, episodes, interventions, discrete, human),
        planned,
        unplanned,
        _supervision(b.supervision, whole, rules),
        _human_actor_ns(all_spans, discrete, human),
        _distinct_humans(all_spans, discrete, stops, human),
        _union_ns(b.support),
        b.distance_m,
        _sorted_flags(session, b.flags, log_flags),
    )


def _repaired(spans: Sequence[Span], repairs: Sequence[_Repair]) -> tuple[Span, ...]:
    """The scope's spans with the ``ai`` time of each approval wait made ``shared`` (6.1)."""
    out = list(spans)
    for repair in repairs:
        if not out or repair.scope != out[0].scope:
            continue
        pieces: list[Span] = []
        for span in out:
            if (
                span.state != "ai"
                or span.end_ns <= repair.start_ns
                or span.start_ns >= repair.end_ns
            ):
                pieces.append(span)
                continue
            low, high = max(span.start_ns, repair.start_ns), min(span.end_ns, repair.end_ns)
            if span.start_ns < low:
                pieces.append(replace(span, end_ns=low))
            pieces.append(
                replace(
                    span,
                    start_ns=low,
                    end_ns=high,
                    state="shared",
                    reason="ai_requested_help",
                    actor_id=repair.actor_id,
                    device_id=repair.device_id,
                    seq=repair.seq,
                )
            )
            if high < span.end_ns:
                pieces.append(replace(span, start_ns=high))
        out = pieces
    return tuple(out)


def _end(episode: _OpenEpisode) -> int:
    return episode.start_ns if episode.end_ns is None else episode.end_ns


def _interventions(episodes: Sequence[Episode]) -> tuple[Intervention, ...]:
    """Whole-robot interventions, in group order (6.6); each takes its first episode's facts."""
    members: dict[int, list[int]] = {}
    for index, episode in enumerate(episodes):
        members.setdefault(episode.intervention, []).append(index)
    out: list[Intervention] = []
    for group in sorted(members):
        indexes = sorted(members[group], key=lambda i: (episodes[i].start_ns, episodes[i].scope))
        first = episodes[indexes[0]]
        out.append(
            Intervention(
                first.start_ns,
                first.seq,
                first.kind,
                first.reason,
                first.actor_id,
                first.task_id,
                first.task_index,
                tuple(episodes[i].scope for i in indexes),
                tuple(indexes),
            )
        )
    return tuple(out)


def _discrete(  # noqa: PLR0913, PLR0917 -- the pieces dwell placement needs
    b: _Builder,
    task_id: Any,
    rules: Rules,
    spans: Sequence[Span],
    whole: Sequence[WholeSpan],
    scopes: Sequence[str],
) -> tuple[DiscreteIntervention, ...]:
    """Approvals and hints with their default dwell placed (6.1, 6.2)."""
    minimum = _min_dwell_ns(rules.params.approval_min_dwell_s, rules.params.hint_min_dwell_s)
    scope_ai, whole_ai = _ai_intervals(spans, whole)
    out: list[DiscreteIntervention] = []
    for d in b.discrete:
        length = max(d.dwell_ns or 0, minimum[d.type])
        low = d.ts_ns - length
        out.append(
            DiscreteIntervention(
                d.type,
                d.scope,
                d.ts_ns,
                d.seq,
                d.actor_id,
                d.reason,
                d.dwell_ns,
                task_id(d.task_index),
                d.task_index,
                length,
                overlap_ns(scope_ai.get(d.scope, []), low, d.ts_ns) if d.scope in scopes else 0,
                overlap_ns(whole_ai, low, d.ts_ns),
                d.in_episode,
            )
        )
    return tuple(out)


def _tasks(  # noqa: PLR0913, PLR0917 -- every fact a task needs
    b: _Builder,
    spans: Sequence[Span],
    whole: Sequence[WholeSpan],
    episodes: Sequence[Episode],
    interventions: Sequence[Intervention],
    discrete: Sequence[DiscreteIntervention],
    human: frozenset[str],
) -> tuple[Task, ...]:
    """Tasks (5) with unplanned resets since the previous task attributed to them (6.3)."""
    count = len(b.tasks)
    states: list[set[str]] = [set() for _ in range(count)]
    for span in spans:
        if span.task_index is not None and span.duration_ns:
            states[span.task_index].add(span.state)
    human_ns = [0] * count
    for piece in whole:
        if piece.task_index is not None and piece.state in human:
            human_ns[piece.task_index] += piece.duration_ns
    own_episodes = _count_by_task((e.task_index for e in episodes), count)
    own_groups = _count_by_task((i.task_index for i in interventions), count)
    own_discrete: list[list[DiscreteIntervention]] = [[] for _ in range(count)]
    for item in discrete:
        if item.task_index is not None:
            own_discrete[item.task_index].append(item)
    unplanned = [
        (s.start_ns, s.end_ns)
        for s in whole
        if s.task_index is None and s.state in human and not s.planned
    ]
    out: list[Task] = []
    previous_end = b.start_ns
    for index, task in enumerate(b.tasks):
        end_ns = task.end_ns if task.end_ns is not None else task.start_ns
        inside = states[index]
        signals = own_discrete[index]
        counted = [d for d in signals if not d.in_episode]
        reset = overlap_ns(unplanned, previous_end, task.start_ns)
        out.append(
            Task(
                task.task_id,
                task.definition,
                task.known,
                task.start_ns,
                end_ns,
                task.start_seq,
                task.end_seq,
                task.outcome if task.closed else "unknown",
                task.closed,
                not (inside & human) and not own_episodes[index] and not signals and not reset,
                "unknown" in inside,
                human_ns[index],
                sum(d.whole_robot_moved_ns for d in signals),
                own_episodes[index] + len(counted),
                own_groups[index] + len(counted),
                reset,
            )
        )
        previous_end = end_ns
    return tuple(out)


def _count_by_task(indexes: Iterable[int | None], count: int) -> list[int]:
    out = [0] * count
    for index in indexes:
        if index is not None:
            out[index] += 1
    return out


def _resets(whole: Sequence[WholeSpan], human: frozenset[str]) -> tuple[int, int]:
    """Planned and unplanned whole-robot human time while no task is open (6.3)."""
    between = [s for s in whole if s.task_index is None and s.state in human]
    planned = sum(s.duration_ns for s in between if s.planned)
    return planned, sum(s.duration_ns for s in between) - planned


def _supervision(
    changes: Sequence[tuple[int, str, int]], whole: Sequence[WholeSpan], rules: Rules
) -> Supervision:
    """Active time per supervision level and supervisor time (6.4)."""
    active = [(s.start_ns, s.end_ns) for s in whole if rules.states[s.state].active]
    end_ns = whole[-1].end_ns if whole else changes[0][0]
    by_level: dict[str, int] = {}
    supervisor = 0
    bounds = [*changes, (end_ns, "", 0)]
    for (start, level, count), (stop, _, _) in pairwise(bounds):
        covered = overlap_ns(active, start, max(start, stop))
        by_level[level] = by_level.get(level, 0) + covered
        supervisor += count * covered
    total = sum(end - start for start, end in active)
    return Supervision(MappingProxyType(dict(sorted(by_level.items()))), supervisor, total)


def _human_actor_ns(
    spans: Sequence[Span], discrete: Sequence[DiscreteIntervention], human: frozenset[str]
) -> int:
    """Sum over human actors of the time each holds a scope in a human state or spends on an
    approval or hint dwell, overlaps of one actor counted once (6.5)."""
    by_actor: dict[str, list[tuple[int, int]]] = {}
    for span in spans:
        if span.state in human and span.actor_id is not None:
            by_actor.setdefault(span.actor_id, []).append((span.start_ns, span.end_ns))
    for item in discrete:
        if item.actor_id is not None and item.human_ns:
            by_actor.setdefault(item.actor_id, []).append((item.ts_ns - item.human_ns, item.ts_ns))
    return sum(_union_ns(intervals) for intervals in by_actor.values())


def _distinct_humans(
    spans: Sequence[Span],
    discrete: Sequence[DiscreteIntervention],
    stops: Sequence[SafetyStop],
    human: frozenset[str],
) -> int:
    """Distinct actors in a human state, approving, hinting or triggering a stop (6.5)."""
    actors = {s.actor_id for s in spans if s.state in human and s.duration_ns}
    actors |= {d.actor_id for d in discrete}
    actors |= {s.actor_id for s in stops}
    actors.discard(None)
    return len(actors)


# -- JSON ---------------------------------------------------------------------------------------


def _s(ns: int | Fraction) -> float:
    return float(Fraction(ns) / 1_000_000_000) if isinstance(ns, Fraction) else ns_to_seconds(ns)


def _ts(ns: int | None) -> str | None:
    return None if ns is None else ns_to_ts(ns)


def _totals_json(totals: StateTotals[int] | StateTotals[Fraction]) -> dict[str, Any]:
    return {
        "observed_s": _s(totals.observed_ns),
        "active_s": _s(totals.active_ns),
        "idle_s": _s(totals.idle_ns),
        "unknown_s": _s(totals.unknown_ns),
        "states": {state: _s(value) for state, value in totals.states.items()},
    }


def _count(values: Iterable[str | None]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = _NO_REASON if value is None else value
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _timeline_json(t: Timeline) -> dict[str, Any]:
    shift = dwell_shift(
        t, ns_to_seconds(t.approval_min_dwell_ns), ns_to_seconds(t.hint_min_dwell_ns)
    )
    whole_kinds = [i.kind for i in t.interventions] + [d.type for d in t.counted_discrete]
    whole_reasons = [i.reason for i in t.interventions] + [d.reason for d in t.counted_discrete]
    return {
        "robot_id": t.robot_id,
        "session_id": t.session_id,
        "start": _ts(t.start_ns),
        "end": _ts(t.end_ns),
        "session_start_seq": t.session_start_seq,
        "session_end_seq": t.session_end_seq,
        "simulated": t.simulated,
        "unobserved": list(t.unobserved),
        "hint_policy": t.hint_policy,
        "scopes": list(t.scopes),
        "scope_weights": {scope: float(w) for scope, w in t.weights.items()},
        "registries": dict(t.registries),
        "time": {
            "whole_robot": _totals_json(t.whole_robot),
            "scope_weighted": _totals_json(t.scope_weighted),
            "per_scope": {scope: _totals_json(v) for scope, v in t.per_scope.items()},
        },
        "interventions": {
            "per_scope": t.interventions_per_scope,
            "whole_robot": t.interventions_whole_robot,
            "by_kind": _count(whole_kinds),
            "by_reason": _count(whole_reasons),
            "list": [_intervention_json(i) for i in t.interventions],
            "episodes": [_episode_json(e) for e in t.episodes],
            "approvals": [_discrete_json(d) for d in t.discrete if d.type == "approval"],
            "hints": [_discrete_json(d) for d in t.discrete if d.type == "hint"],
        },
        "dwell": {
            "approval_min_dwell_s": _s(t.approval_min_dwell_ns),
            "hint_min_dwell_s": _s(t.hint_min_dwell_ns),
            "human_s": _s(shift.human_ns),
            "moved_from_ai_s": {
                "whole_robot": _s(shift.whole_robot_moved_ns),
                "scope_weighted": _s(shift.scope_weighted_moved_ns),
                "per_scope": {k: _s(v) for k, v in shift.moved_ns_by_scope.items()},
            },
        },
        "safety_stops": {
            "per_scope": len(t.safety_stops),
            "whole_robot": t.safety_stops_whole_robot,
            "human_triggered": sum(1 for s in t.safety_stops if s.triggered_by == "human"),
            "automatic": sum(1 for s in t.safety_stops if s.triggered_by == "automatic"),
            "joined_by_takeover": sum(1 for s in t.safety_stops if s.takeover_seq is not None),
            "list": [_stop_json(s) for s in t.safety_stops],
        },
        "tasks": [_task_json(task) for task in t.tasks],
        "resets": {"planned_s": _s(t.reset_planned_ns), "unplanned_s": _s(t.reset_unplanned_ns)},
        "supervision": {
            "ratio": t.supervision.ratio,
            "supervisor_hours_per_robot_hour": t.supervision.supervisor_hours_per_robot_hour,
            "active_s_by_level": {k: _s(v) for k, v in t.supervision.active_ns_by_level.items()},
        },
        "humans": {
            "human_actor_s": _s(t.human_actor_ns),
            "distinct_human_actors": t.distinct_human_actors,
        },
        "supported_s": _s(t.supported_ns),
        "distance_m": t.distance_m,
        "flags": [{"seq": f.seq, "code": f.code, "detail": f.detail} for f in t.flags],
    }


def _intervention_json(i: Intervention) -> dict[str, Any]:
    return {
        "start": _ts(i.start_ns),
        "seq": i.seq,
        "kind": i.kind,
        "reason": i.reason,
        "actor_id": i.actor_id,
        "task_id": i.task_id,
        "scopes": list(i.scopes),
    }


def _episode_json(e: Episode) -> dict[str, Any]:
    return {
        "scope": e.scope,
        "start": _ts(e.start_ns),
        "end": _ts(e.end_ns),
        "seq": e.seq,
        "end_seq": e.end_seq,
        "kind": e.kind,
        "reason": e.reason,
        "actor_id": e.actor_id,
        "task_id": e.task_id,
        "takeover_seq": e.takeover_seq,
        "human_s": _s(e.human_ns),
        "intervention": e.intervention,
    }


def _discrete_json(d: DiscreteIntervention) -> dict[str, Any]:
    return {
        "scope": d.scope,
        "ts": _ts(d.ts_ns),
        "seq": d.seq,
        "actor_id": d.actor_id,
        "reason": d.reason,
        "dwell_s": None if d.dwell_ns is None else _s(d.dwell_ns),
        "human_s": _s(d.human_ns),
        "moved_from_ai_s": _s(d.moved_ns),
        "whole_robot_moved_from_ai_s": _s(d.whole_robot_moved_ns),
        "task_id": d.task_id,
        "in_episode": d.in_episode,
    }


def _stop_json(s: SafetyStop) -> dict[str, Any]:
    return {
        "scope": s.scope,
        "ts": _ts(s.ts_ns),
        "seq": s.seq,
        "from_state": s.from_state,
        "triggered_by": s.triggered_by,
        "actor_id": s.actor_id,
        "task_id": s.task_id,
        "takeover_seq": s.takeover_seq,
    }


def _task_json(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "task_definition_id": task.task_definition_id,
        "definition_known": task.definition_known,
        "start": _ts(task.start_ns),
        "end": _ts(task.end_ns),
        "start_seq": task.start_seq,
        "end_seq": task.end_seq,
        "outcome": task.outcome,
        "closed": task.closed,
        "autonomous": task.autonomous,
        "contains_unknown": task.contains_unknown,
        "human_s": _s(task.human_ns),
        "dwell_moved_from_ai_s": _s(task.dwell_moved_ns),
        "interventions": task.interventions,
        "interventions_whole_robot": task.interventions_whole_robot,
        "unplanned_reset_s": _s(task.unplanned_reset_ns),
    }
