"""Two witnesses reconciled: trust level L3 (definitions.md 12).

:func:`reconcile` compares the timelines of robot sessions with the timelines of the
teleoperation station sessions that witness them, and decides for each robot session whether it
reaches trust level L3. It is pure and uses the standard library only. Times are integer
nanoseconds since the Unix epoch and every time is an exact sum of ``[start, end)`` intervals;
seconds appear only in :meth:`Reconciliation.to_json`.

How each rule is applied:

- Pairing (12.2): a station session witnesses the robot sessions of its ``robot_id`` whose
  session windows overlap its own. On each robot scope the witnesses are *human* where any of
  them has the scope in a state other than ``idle`` or ``unknown``; *unwitnessed* where none is
  human and none of those observed then declares the scope, or one has it ``unknown``.
- Tolerance (12.3): ``tau(t)`` adds both sides' ``clock_max_error_s`` from the heartbeat nearest
  to ``t`` and ``react_s``; an implausibly small error without PTP is replaced and flagged.
- Classification (12.4): per scope, the base (active time plus idle time under station input)
  is split into both-human (with its tolerated and held parts), robot-only, station-only,
  neither and unwitnessed; the scopes' times are added up.
- Episodes (12.5): unmatched station episodes raise an upper-bound intervention count.
- Verdict (12.6): verification, clocks, station states and the two shares, unwitnessed time
  counted as disputed.
"""

from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Final

from autonometry.events import TrustStatus, WitnessFlag, WitnessReason
from autonometry.spec import Rules
from autonometry.timeline import ClockSample, Span, Timeline
from autonometry.timestamps import ns_to_seconds, ns_to_ts, seconds_to_ns

Interval = tuple[int, int]
Key = tuple[str, str]

ROBOT: Final = "robot"
STATION: Final = "station"
_PTP: Final = "ptp"
_CLOCK_UNSYNCED: Final = "clock_unsynced"
_VERIFIED: Final = "verified"


# -- intervals ----------------------------------------------------------------------------------


def _union(intervals: Iterable[Interval]) -> list[Interval]:
    """The sorted, disjoint union of ``[start, end)`` intervals; empty ones are dropped."""
    out: list[Interval] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _intersect(a: Sequence[Interval], b: Sequence[Interval]) -> list[Interval]:
    """Intersection of two sorted, disjoint interval lists."""
    out: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        low, high = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if low < high:
            out.append((low, high))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _subtract(a: Sequence[Interval], b: Sequence[Interval]) -> list[Interval]:
    """``a`` without ``b``, both sorted and disjoint."""
    out: list[Interval] = []
    j = 0
    for start, end in a:
        low = start
        while j < len(b) and b[j][1] <= low:
            j += 1
        k = j
        while k < len(b) and b[k][0] < end:
            if b[k][0] > low:
                out.append((low, b[k][0]))
            low = max(low, b[k][1])
            k += 1
        if low < end:
            out.append((low, end))
    return out


def _length(intervals: Iterable[Interval]) -> int:
    return sum(end - start for start, end in intervals)


def _overlaps(interval: Interval, others: Sequence[Interval]) -> bool:
    """True when ``interval`` shares a stretch of positive length with sorted ``others``."""
    return bool(_intersect([interval], others))


# -- parameters ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WitnessParams:
    """Everything the reconciler reads from ``rules.toml`` (definitions.md 9, 12): seconds,
    and shares as fractions of the base. ``robot_human_states`` make robot human time,
    ``station_states`` are the states a station scope may hold; a station state in
    ``idle_states`` is no human and one in ``lost_states`` is lost input. ``reasons`` and
    ``flags`` are the codes in the order reports list them."""

    react_s: float
    clock_tolerance_s: float
    eps_min_without_ptp_s: float
    eps_substitute_s: float
    station_only_max_share: float
    disputed_max_share: float
    simultaneity_window_s: float
    robot_human_states: frozenset[str]
    station_states: frozenset[str]
    active_states: frozenset[str]
    idle_states: frozenset[str]
    lost_states: frozenset[str]
    reasons: tuple[WitnessReason, ...]
    flags: tuple[WitnessFlag, ...]

    @classmethod
    def from_rules(cls, rules: Rules) -> "WitnessParams":
        """The parameters of a spec version's rules."""
        trust = rules.trust
        return cls(
            react_s=rules.params.react_s,
            clock_tolerance_s=rules.sealing.clock_tolerance_s,
            eps_min_without_ptp_s=trust.l3_eps_min_without_ptp_s,
            eps_substitute_s=trust.l3_eps_substitute_s,
            station_only_max_share=trust.l3_station_only_max_share,
            disputed_max_share=trust.l3_disputed_max_share,
            simultaneity_window_s=rules.params.simultaneity_window_s,
            robot_human_states=frozenset(trust.l3_robot_human_states),
            station_states=frozenset(trust.l3_station_states),
            active_states=frozenset(s for s, rule in rules.states.items() if rule.active),
            idle_states=frozenset(
                s for s, rule in rules.states.items() if rule.category == "inactive"
            ),
            lost_states=frozenset(
                s for s, rule in rules.states.items() if rule.category == "unmeasured"
            ),
            reasons=trust.l3_reasons,
            flags=trust.l3_flags,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "react_s": self.react_s,
            "clock_tolerance_s": self.clock_tolerance_s,
            "l3_eps_min_without_ptp_s": self.eps_min_without_ptp_s,
            "l3_eps_substitute_s": self.eps_substitute_s,
            "l3_station_only_max_share": self.station_only_max_share,
            "l3_disputed_max_share": self.disputed_max_share,
            "simultaneity_window_s": self.simultaneity_window_s,
        }


# -- clocks -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Clock:
    """One side's reported clock errors by time (seconds), implausible ones replaced (12.3)."""

    times: tuple[int, ...]
    errors: tuple[float, ...]
    default_s: float

    def eps_s(self, t: int) -> float:
        """The error of the heartbeat nearest to ``t``; the earlier of two equally near."""
        if not self.times:
            return self.default_s
        i = bisect_left(self.times, t)
        if i == len(self.times):
            return self.errors[-1]
        if i == 0:
            return self.errors[0]
        before, after = t - self.times[i - 1], self.times[i] - t
        return self.errors[i - 1] if before <= after else self.errors[i]


def _implausible(sample: ClockSample, params: WitnessParams) -> bool:
    return (
        sample.max_error_s is not None
        and sample.max_error_s < params.eps_min_without_ptp_s
        and sample.source != _PTP
    )


def _clock(timelines: Iterable[Timeline], params: WitnessParams) -> _Clock:
    """The clock of one side: every heartbeat that reports an error, from all its sessions."""
    samples = sorted(
        (s for t in timelines for s in t.clock if s.max_error_s is not None),
        key=lambda s: s.ts_ns,
    )
    errors = tuple(
        params.eps_substitute_s if _implausible(s, params) else float(s.max_error_s or 0.0)
        for s in samples
    )
    return _Clock(tuple(s.ts_ns for s in samples), errors, params.clock_tolerance_s)


def _clock_flags(timeline: Timeline, params: WitnessParams) -> list["WitnessFlagItem"]:
    count = sum(1 for s in timeline.clock if _implausible(s, params))
    if not count:
        return []
    detail = (
        f"{count} heartbeat(s) report clock_max_error_s below "
        f"{params.eps_min_without_ptp_s:g} s without PTP; each counts as "
        f"{params.eps_substitute_s:g} s"
    )
    return [
        WitnessFlagItem(
            timeline.role,
            timeline.session_id,
            timeline.station_id,
            "implausible_clock_error",
            detail,
        )
    ]


# -- results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cells:
    """The cells of definitions.md 12.4 in nanoseconds (scope-nanoseconds when added over
    scopes). ``tolerated_ns`` and ``held_ns`` are parts of ``both_human_ns``."""

    both_human_ns: int = 0
    robot_only_ns: int = 0
    station_only_ns: int = 0
    neither_ns: int = 0
    unwitnessed_ns: int = 0
    tolerated_ns: int = 0
    held_ns: int = 0

    @property
    def base_ns(self) -> int:
        """The base: the five cells add up to it (12.4)."""
        return (
            self.both_human_ns
            + self.robot_only_ns
            + self.station_only_ns
            + self.neither_ns
            + self.unwitnessed_ns
        )

    @property
    def disputed_ns(self) -> int:
        return self.robot_only_ns + self.station_only_ns

    def __add__(self, other: "Cells") -> "Cells":
        return Cells(
            self.both_human_ns + other.both_human_ns,
            self.robot_only_ns + other.robot_only_ns,
            self.station_only_ns + other.station_only_ns,
            self.neither_ns + other.neither_ns,
            self.unwitnessed_ns + other.unwitnessed_ns,
            self.tolerated_ns + other.tolerated_ns,
            self.held_ns + other.held_ns,
        )

    def to_json(self) -> dict[str, float]:
        return {
            "base_s": ns_to_seconds(self.base_ns),
            "both_human_s": ns_to_seconds(self.both_human_ns),
            "tolerated_s": ns_to_seconds(self.tolerated_ns),
            "held_s": ns_to_seconds(self.held_ns),
            "robot_only_s": ns_to_seconds(self.robot_only_ns),
            "station_only_s": ns_to_seconds(self.station_only_ns),
            "neither_s": ns_to_seconds(self.neither_ns),
            "unwitnessed_s": ns_to_seconds(self.unwitnessed_ns),
            "disputed_s": ns_to_seconds(self.disputed_ns),
        }


@dataclass(frozen=True, slots=True)
class Stretch:
    """A station episode or a robot run on one scope, ``[start_ns, end_ns)`` (12.4, 12.5)."""

    scope: str
    start_ns: int
    end_ns: int

    def to_json(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "start": ns_to_ts(self.start_ns),
            "end": ns_to_ts(self.end_ns),
            "duration_s": ns_to_seconds(self.end_ns - self.start_ns),
        }


@dataclass(frozen=True, slots=True)
class Reason:
    """Why a robot session is not L3 (12.6)."""

    code: WitnessReason
    detail: str


@dataclass(frozen=True, slots=True)
class WitnessFlagItem:
    """A flag the reconciler raised on one session (12.3)."""

    role: str
    session_id: str
    station_id: str | None
    code: WitnessFlag
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "session_id": self.session_id,
            "station_id": self.station_id,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class WitnessRef:
    """A station session that witnesses a robot session, with its verification status."""

    station_id: str | None
    session_id: str
    status: TrustStatus | None


@dataclass(frozen=True, slots=True)
class SessionWitness:
    """The reconciliation of one robot session (definitions.md 12.2 to 12.6).

    ``cells`` are summed over the scopes in ``by_scope``; ``tau_min_ns`` and ``tau_max_ns`` span
    the tolerances used (at the robot session's start when no edge needed one);
    ``robot_interventions`` is the robot's whole-robot count and ``upper_bound_interventions``
    adds the unmatched station episodes (12.5). ``l3`` is true when ``reasons`` is empty.
    """

    robot_id: str
    session_id: str
    status: TrustStatus | None
    stations: tuple[WitnessRef, ...]
    cells: Cells
    by_scope: Mapping[str, Cells]
    station_human_robot_unknown_ns: int
    tau_min_ns: int
    tau_max_ns: int
    matched_station_episodes: int
    unmatched_station_episodes: tuple[Stretch, ...]
    unmatched_robot_runs: tuple[Stretch, ...]
    robot_interventions: int
    upper_bound_interventions: int
    reasons: tuple[Reason, ...]
    flags: tuple[WitnessFlagItem, ...]
    station_only_max_share: float
    disputed_max_share: float

    @property
    def l3(self) -> bool:
        return not self.reasons

    @property
    def level(self) -> str:
        """L3 when the witnesses agree (12.6), else L2 for a verified session, else L1 (11.2)."""
        if self.l3:
            return "L3"
        return "L2" if self.status == _VERIFIED else "L1"

    def shares(self) -> dict[str, float | None]:
        """Shares of the base; ``None`` without base time. The last two are the ones 12.6
        compares with its maxima."""
        cells, base = self.cells, self.cells.base_ns
        values = {
            "station_only": cells.station_only_ns,
            "disputed": cells.disputed_ns,
            "unwitnessed": cells.unwitnessed_ns,
            "station_only_with_unwitnessed": cells.station_only_ns + cells.unwitnessed_ns,
            "disputed_with_unwitnessed": cells.disputed_ns + cells.unwitnessed_ns,
        }
        return {name: (value / base if base else None) for name, value in values.items()}

    def to_json(self) -> dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "session_id": self.session_id,
            "status": self.status,
            "level": self.level,
            "l3": self.l3,
            "reasons": [{"code": r.code, "detail": r.detail} for r in self.reasons],
            "stations": [
                {"station_id": s.station_id, "session_id": s.session_id, "status": s.status}
                for s in self.stations
            ],
            "tau_s": {
                "min": ns_to_seconds(self.tau_min_ns),
                "max": ns_to_seconds(self.tau_max_ns),
            },
            "times": self.cells.to_json(),
            "shares": self.shares(),
            "maxima": {
                "station_only_with_unwitnessed": self.station_only_max_share,
                "disputed_with_unwitnessed": self.disputed_max_share,
            },
            "by_scope": {scope: cells.to_json() for scope, cells in self.by_scope.items()},
            "station_human_robot_unknown_s": ns_to_seconds(self.station_human_robot_unknown_ns),
            "episodes": {
                "matched_station": self.matched_station_episodes,
                "unmatched_station": [e.to_json() for e in self.unmatched_station_episodes],
                "unmatched_robot_runs": [r.to_json() for r in self.unmatched_robot_runs],
            },
            "interventions": {
                "robot": self.robot_interventions,
                "upper_bound": self.upper_bound_interventions,
            },
            "flags": [f.to_json() for f in self.flags],
        }


@dataclass(frozen=True, slots=True)
class StationWitness:
    """One station session (12.1): its status, the robot sessions it witnesses, its human time
    (scope-nanoseconds) and the part outside every robot session of its robot given, the
    states it holds that 12.1 does not allow, and its flags."""

    robot_id: str
    session_id: str
    station_id: str | None
    status: TrustStatus | None
    witnesses: tuple[str, ...]
    human_ns: int
    human_outside_robot_sessions_ns: int
    invalid_states: tuple[str, ...]
    flags: tuple[WitnessFlagItem, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "session_id": self.session_id,
            "station_id": self.station_id,
            "status": self.status,
            "witnesses": list(self.witnesses),
            "human_s": ns_to_seconds(self.human_ns),
            "human_outside_robot_sessions_s": ns_to_seconds(self.human_outside_robot_sessions_ns),
            "invalid_states": list(self.invalid_states),
            "flags": [f.to_json() for f in self.flags],
        }


@dataclass(frozen=True, slots=True)
class Ignored:
    """A session given on the wrong side: a station log as a robot log or the reverse."""

    robot_id: str
    session_id: str
    given_as: str
    role: str

    def to_json(self) -> dict[str, str]:
        return {
            "robot_id": self.robot_id,
            "session_id": self.session_id,
            "given_as": self.given_as,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The result of :func:`reconcile`: robot sessions and station sessions sorted by
    ``(robot_id, session_id)``, and the sessions given on the wrong side."""

    params: WitnessParams
    sessions: tuple[SessionWitness, ...]
    stations: tuple[StationWitness, ...]
    ignored: tuple[Ignored, ...] = field(default=())

    @property
    def all_l3(self) -> bool:
        """True when there is a robot session and every one is L3."""
        return bool(self.sessions) and all(s.l3 for s in self.sessions)

    def to_json(self) -> dict[str, Any]:
        """A JSON-serialisable form: seconds and RFC 3339 times, lists in a fixed order."""
        return {
            "parameters": self.params.to_json(),
            "all_l3": self.all_l3,
            "counts": {
                "robot_sessions": len(self.sessions),
                "l3": sum(1 for s in self.sessions if s.l3),
                "station_sessions": len(self.stations),
                "ignored": len(self.ignored),
            },
            "sessions": [s.to_json() for s in self.sessions],
            "stations": [s.to_json() for s in self.stations],
            "ignored": [i.to_json() for i in self.ignored],
        }


# -- one scope ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Witnessed:
    """What the witnesses together say on one scope inside the robot window (12.2)."""

    human: list[Interval]
    unwitnessed: list[Interval]


def _station_human(state: str, params: WitnessParams) -> bool:
    """A station scope in any state but its idle and lost-input states shows a human (12.1)."""
    return state not in params.idle_states and state not in params.lost_states


def _witnessed(
    scope: str, window: Interval, stations: Sequence[Timeline], params: WitnessParams
) -> _Witnessed:
    covered: list[Interval] = []
    human: list[Interval] = []
    lost: list[Interval] = []
    for station in stations:
        if scope not in station.scopes or station.start_ns is None or station.end_ns is None:
            continue
        covered.append((station.start_ns, station.end_ns))
        for span in station.spans:
            if span.scope != scope:
                continue
            if span.state in params.lost_states:
                lost.append((span.start_ns, span.end_ns))
            elif _station_human(span.state, params):
                human.append((span.start_ns, span.end_ns))
    clip = [window]
    human_in = _intersect(_union(human), clip)
    unseen = _union([*_subtract(clip, _union(covered)), *lost])
    return _Witnessed(human_in, _intersect(_subtract(unseen, human_in), clip))


@dataclass(frozen=True, slots=True)
class _ScopeResult:
    cells: Cells
    station_unknown_ns: int
    matched: int
    unmatched_episodes: list[Stretch]
    unmatched_runs: list[Stretch]
    taus: list[int]


def _states(spans: Iterable[Span], states: frozenset[str]) -> list[Interval]:
    return _union((s.start_ns, s.end_ns) for s in spans if s.state in states)


def _widen(intervals: Sequence[Interval], tau: Callable[[int], int]) -> list[Interval]:
    return [(start - tau(start), end + tau(end)) for start, end in intervals]


def _scope(
    scope: str,
    spans: Sequence[Span],
    witnessed: _Witnessed,
    tau: Callable[[int], int],
    params: WitnessParams,
) -> _ScopeResult:
    """Cells, episodes and runs of one robot scope (12.4, 12.5)."""
    human, unwitnessed = witnessed.human, witnessed.unwitnessed
    runs = _states(spans, params.robot_human_states)
    idle_under_input = _intersect(_states(spans, params.idle_states), human)
    base = _union([*_states(spans, params.active_states), *idle_under_input])
    wide_runs = _widen(runs, tau)
    wide_episodes = _widen(human, tau)
    runs_wide = _union(wide_runs)
    episodes_wide = _union(wide_episodes)
    matched_runs = _union(r for r, w in zip(runs, wide_runs, strict=True) if _overlaps(w, human))
    on_base = [e for e in human if _overlaps(e, base)]
    unmatched = [e for e in on_base if not _overlaps(e, runs_wide)]

    station_human = _intersect(base, human)
    robot_rest = _subtract(_subtract(runs, human), unwitnessed)
    robot_out = _subtract(robot_rest, episodes_wide)
    tolerated = _length(_intersect(_subtract(station_human, runs), runs_wide)) + _length(
        _intersect(robot_rest, episodes_wide)
    )
    held = _length(_intersect(robot_out, matched_runs))
    unwitnessed_base = _intersect(base, unwitnessed)
    cells = Cells(
        both_human_ns=_length(_intersect(runs, station_human)) + tolerated + held,
        robot_only_ns=_length(_subtract(robot_out, matched_runs)),
        station_only_ns=_length(_subtract(station_human, runs_wide)),
        neither_ns=_length(
            _subtract(_subtract(base, unwitnessed_base), _union([*runs, *station_human]))
        ),
        unwitnessed_ns=_length(unwitnessed_base),
        tolerated_ns=tolerated,
        held_ns=held,
    )
    edges = [edge for interval in [*runs, *human] for edge in interval]
    return _ScopeResult(
        cells,
        _length(_intersect(human, _states(spans, params.lost_states))),
        len(on_base) - len(unmatched),
        [Stretch(scope, *e) for e in unmatched],
        [
            Stretch(scope, *r)
            for r, w in zip(runs, wide_runs, strict=True)
            if not _overlaps(w, human)
        ],
        [tau(edge) for edge in edges],
    )


# -- one robot session --------------------------------------------------------------------------


def _window(timeline: Timeline) -> Interval | None:
    if timeline.start_ns is None or timeline.end_ns is None:
        return None
    return (timeline.start_ns, timeline.end_ns)


def _witnesses_of(robot: Timeline, stations: Sequence[Timeline]) -> list[Timeline]:
    """Station sessions of the same robot whose window overlaps the robot session's (12.2)."""
    window = _window(robot)
    if window is None:
        return []
    return [
        s
        for s in stations
        if s.robot_id == robot.robot_id and (w := _window(s)) is not None and _overlaps(window, [w])
    ]


def _invalid_states(station: Timeline, params: WitnessParams) -> tuple[str, ...]:
    return tuple(sorted({s.state for s in station.spans} - params.station_states))


def _unsynced(timeline: Timeline) -> bool:
    return any(f.code == _CLOCK_UNSYNCED for f in timeline.flags)


def _group_count(stretches: Sequence[Stretch], window_ns: int) -> int:
    """Unmatched episodes on several scopes that start within the window of the first of a
    group count once (12.5, as definitions.md 6.6)."""
    count = 0
    first: int | None = None
    for start in sorted(s.start_ns for s in stretches):
        if first is None or start - first > window_ns:
            count += 1
            first = start
    return count


def _share_above(numerator: int, base: int, maximum: float) -> bool:
    return base > 0 and Fraction(numerator, base) > Fraction(maximum)


def _name(timeline: Timeline) -> str:
    station = f" ({timeline.station_id})" if timeline.station_id else ""
    return f"{timeline.session_id}{station}"


def _reasons(  # noqa: PLR0913, PLR0917 -- every fact the verdict of 12.6 weighs
    robot: Timeline,
    witnesses: Sequence[Timeline],
    status: TrustStatus | None,
    station_status: Mapping[Key, TrustStatus],
    cells: Cells,
    params: WitnessParams,
) -> tuple[Reason, ...]:
    """Every reason of 12.6 that holds, in the order of ``l3_reasons``."""
    found: dict[WitnessReason, str] = {}
    if not witnesses:
        found["no_station"] = "no station session of this robot overlaps the session"
    if status != _VERIFIED:
        found["robot_not_verified"] = f"robot session is {status or 'not verified'}"
    unverified = [
        f"{_name(s)} {station_status.get((s.robot_id, s.session_id)) or 'not verified'}"
        for s in witnesses
        if station_status.get((s.robot_id, s.session_id)) != _VERIFIED
    ]
    if unverified:
        found["station_not_verified"] = "; ".join(unverified)
    if _unsynced(robot):
        found["robot_clock_unsynced"] = "robot session is flagged clock_unsynced"
    unsynced = [_name(s) for s in witnesses if _unsynced(s)]
    if unsynced:
        found["station_clock_unsynced"] = "flagged clock_unsynced: " + ", ".join(unsynced)
    invalid = [
        f"{_name(s)}: {', '.join(states)}"
        for s in witnesses
        if (states := _invalid_states(s, params))
    ]
    if invalid:
        found["invalid_station_state"] = "states a station may not hold: " + "; ".join(invalid)
    if witnesses:
        found |= _share_reasons(cells, params)
    return tuple(Reason(code, found[code]) for code in params.reasons if code in found)


def _share_reasons(cells: Cells, params: WitnessParams) -> dict[WitnessReason, str]:
    base = cells.base_ns
    found: dict[WitnessReason, str] = {}
    station_only = cells.station_only_ns + cells.unwitnessed_ns
    if _share_above(station_only, base, params.station_only_max_share):
        found["station_only_above_max"] = (
            f"(station-only + unwitnessed) / base = {station_only / base:.4f} "
            f"> {params.station_only_max_share:g}"
        )
    disputed = cells.disputed_ns + cells.unwitnessed_ns
    if _share_above(disputed, base, params.disputed_max_share):
        found["disputed_above_max"] = (
            f"(disputed + unwitnessed) / base = {disputed / base:.4f} "
            f"> {params.disputed_max_share:g}"
        )
    return found


def _session(
    robot: Timeline,
    stations: Sequence[Timeline],
    robot_status: Mapping[Key, TrustStatus],
    station_status: Mapping[Key, TrustStatus],
    params: WitnessParams,
) -> SessionWitness:
    witnesses = _witnesses_of(robot, stations)
    robot_clock, station_clock = _clock([robot], params), _clock(witnesses, params)

    def tau(t: int) -> int:
        return seconds_to_ns(robot_clock.eps_s(t) + station_clock.eps_s(t) + params.react_s)

    window = _window(robot)
    by_scope: dict[str, Cells] = {}
    results: list[_ScopeResult] = []
    if window is not None:
        for scope in robot.scopes:
            spans = [s for s in robot.spans if s.scope == scope]
            result = _scope(scope, spans, _witnessed(scope, window, witnesses, params), tau, params)
            by_scope[scope] = result.cells
            results.append(result)
    cells = sum((r.cells for r in results), Cells())
    taus = [t for r in results for t in r.taus] or [tau(robot.start_ns or 0)]
    unmatched = sorted(
        (e for r in results for e in r.unmatched_episodes),
        key=lambda e: (e.start_ns, e.scope, e.end_ns),
    )
    window_ns = seconds_to_ns(params.simultaneity_window_s)
    status = robot_status.get((robot.robot_id, robot.session_id))
    flags = [f for t in (robot, *witnesses) for f in _clock_flags(t, params)]
    return SessionWitness(
        robot_id=robot.robot_id,
        session_id=robot.session_id,
        status=status,
        stations=tuple(
            WitnessRef(s.station_id, s.session_id, station_status.get((s.robot_id, s.session_id)))
            for s in witnesses
        ),
        cells=cells,
        by_scope=by_scope,
        station_human_robot_unknown_ns=sum(r.station_unknown_ns for r in results),
        tau_min_ns=min(taus),
        tau_max_ns=max(taus),
        matched_station_episodes=sum(r.matched for r in results),
        unmatched_station_episodes=tuple(unmatched),
        unmatched_robot_runs=tuple(
            sorted(
                (run for r in results for run in r.unmatched_runs),
                key=lambda e: (e.start_ns, e.scope, e.end_ns),
            )
        ),
        robot_interventions=robot.interventions_whole_robot,
        upper_bound_interventions=robot.interventions_whole_robot
        + _group_count(unmatched, window_ns),
        reasons=_reasons(robot, witnesses, status, station_status, cells, params),
        flags=tuple(flags),
        station_only_max_share=params.station_only_max_share,
        disputed_max_share=params.disputed_max_share,
    )


# -- station sessions ---------------------------------------------------------------------------


def _station(
    station: Timeline,
    robots: Sequence[Timeline],
    station_status: Mapping[Key, TrustStatus],
    params: WitnessParams,
) -> StationWitness:
    human = [
        _union(
            (s.start_ns, s.end_ns)
            for s in station.spans
            if s.scope == scope and _station_human(s.state, params)
        )
        for scope in station.scopes
    ]
    windows = _union(w for r in robots if r.robot_id == station.robot_id and (w := _window(r)))
    return StationWitness(
        robot_id=station.robot_id,
        session_id=station.session_id,
        station_id=station.station_id,
        status=station_status.get((station.robot_id, station.session_id)),
        witnesses=tuple(r.session_id for r in robots if _witnesses_of(r, [station])),
        human_ns=sum(_length(h) for h in human),
        human_outside_robot_sessions_ns=sum(_length(_subtract(h, windows)) for h in human),
        invalid_states=_invalid_states(station, params),
        flags=tuple(_clock_flags(station, params)),
    )


# -- entry point --------------------------------------------------------------------------------


def _key(timeline: Timeline) -> Key:
    return (timeline.robot_id, timeline.session_id)


def reconcile(
    robots: Iterable[Timeline],
    stations: Iterable[Timeline],
    params: WitnessParams,
    robot_status: Mapping[Key, TrustStatus] | None = None,
    station_status: Mapping[Key, TrustStatus] | None = None,
) -> Reconciliation:
    """Reconcile robot sessions with the station sessions that witness them (definitions.md
    12): per robot session the cells of 12.4, tau, matched and unmatched episodes (12.5) and
    the L3 verdict with its reasons (12.6).

    ``robots`` and ``stations`` are timelines built with the reader's flags, so that
    ``clock_unsynced`` is among them; a timeline whose ``role`` does not match its side is
    listed in ``ignored``. ``robot_status`` and ``station_status`` map ``(robot_id,
    session_id)`` to the verification status (10.6); a session missing there is not verified.
    Never raises on valid timelines; the result does not depend on the input order.
    """
    robot_status = robot_status or {}
    station_status = station_status or {}
    given = [(ROBOT, t) for t in robots] + [(STATION, t) for t in stations]
    ignored = sorted(
        (Ignored(t.robot_id, t.session_id, side, t.role) for side, t in given if t.role != side),
        key=lambda i: (i.robot_id, i.session_id, i.given_as),
    )
    robot_list = sorted((t for side, t in given if side == t.role == ROBOT), key=_key)
    station_list = sorted((t for side, t in given if side == t.role == STATION), key=_key)
    return Reconciliation(
        params,
        tuple(_session(r, station_list, robot_status, station_status, params) for r in robot_list),
        tuple(_station(s, robot_list, station_status, params) for s in station_list),
        tuple(ignored),
    )
