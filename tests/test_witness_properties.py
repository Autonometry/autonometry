"""Property tests of the witness reconciler (definitions.md 12): random robot and station
timelines never crash it, the five cells always add up to the base, and the base is the robot's
active time plus its idle time under station input (12.4)."""

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from autonometry.witness import _intersect, _length, _subtract, _union
from witness_helpers import PARAMS, run, session, station

ROBOT_STATES = (
    "ai",
    "human_remote",
    "shared",
    "human_local",
    "scripted",
    "safety_stop",
    "idle",
    "unknown",
)
STATION_STATES = ("human_remote", "idle", "unknown")
SCOPES = ("base", "arm")


def changes(states: tuple[str, ...]) -> Any:
    change = st.tuples(
        st.floats(min_value=0.0, max_value=40.0, allow_nan=False).map(lambda t: round(t, 3)),
        st.sampled_from(states),
        st.sampled_from(SCOPES),
    )
    return st.lists(change, max_size=8)


@settings(max_examples=60, deadline=None)
@given(
    robot_changes=changes(ROBOT_STATES),
    station_changes=changes(STATION_STATES),
    robot_error=st.sampled_from([0.0001, 0.01, 0.3, None]),
    station_error=st.sampled_from([0.0001, 0.01, 0.3, None]),
    station_start=st.sampled_from([-5.0, 0.0, 12.5]),
    station_end=st.sampled_from([20.0, 41.0]),
    station_scopes=st.sampled_from([("base", "arm"), ("base",), ("arm", "lidar")]),
)
def test_cells_always_add_up_to_the_base__defs_12_4(  # noqa: PLR0913, PLR0917 -- the drawn values
    robot_changes: list[tuple[float, str, str]],
    station_changes: list[tuple[float, str, str]],
    robot_error: float | None,
    station_error: float | None,
    station_start: float,
    station_end: float,
    station_scopes: tuple[str, ...],
) -> None:
    robot = session(sorted(robot_changes), 40, scopes=SCOPES, clock_error=robot_error)
    witness = station(
        [
            c
            for c in sorted(station_changes)
            if station_start < c[0] < station_end and c[2] in station_scopes
        ],
        station_end,
        start=station_start,
        scopes=station_scopes,
        clock_error=station_error,
    )
    (result,) = run([robot], [witness]).sessions
    total = result.cells
    assert total.base_ns == sum(c.base_ns for c in result.by_scope.values())
    assert total.tolerated_ns + total.held_ns <= total.both_human_ns
    assert (
        min(
            total.both_human_ns,
            total.robot_only_ns,
            total.station_only_ns,
            total.neither_ns,
            total.unwitnessed_ns,
        )
        >= 0
    )
    assert robot.start_ns is not None
    assert robot.end_ns is not None
    window = [(robot.start_ns, robot.end_ns)]
    for scope in SCOPES:
        spans = [s for s in robot.spans if s.scope == scope]
        active = _union((s.start_ns, s.end_ns) for s in spans if s.state in PARAMS.active_states)
        idle = _union((s.start_ns, s.end_ns) for s in spans if s.state in PARAMS.idle_states)
        human = _intersect(
            _union(
                (s.start_ns, s.end_ns)
                for s in witness.spans
                if s.scope == scope and s.state == "human_remote"
            ),
            window,
        )
        expected = _length(active) + _length(_intersect(idle, human))
        assert result.by_scope[scope].base_ns == expected
    assert result.l3 == (result.reasons == ())
    assert result.tau_min_ns <= result.tau_max_ns


interval_lists = st.lists(st.tuples(st.integers(0, 50), st.integers(0, 50)), max_size=6).map(_union)


@given(a=interval_lists, b=interval_lists)
def test_interval_algebra_matches_point_sets(
    a: list[tuple[int, int]], b: list[tuple[int, int]]
) -> None:
    def points(intervals: list[tuple[int, int]]) -> set[int]:
        return {p for start, end in intervals for p in range(start, end)}

    assert points(_intersect(a, b)) == points(a) & points(b)
    assert points(_subtract(a, b)) == points(a) - points(b)
    assert _union(_intersect(a, b)) == _intersect(a, b)
    assert _union(_subtract(a, b)) == _subtract(a, b)
