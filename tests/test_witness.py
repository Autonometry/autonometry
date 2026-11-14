"""The witness reconciler (definitions.md 12): pairing, tolerance, classification, episodes and
the L3 verdict. Numbers are worked out by hand from the spec; the default clock errors are
0.01 s on both sides, so tau = 0.01 + 0.01 + 0.5 = 0.52 s."""

import json
from typing import Any

import pytest

from autonometry.checks import check_session
from autonometry.events import Session
from autonometry.reader import RejectedEventError, check_event
from autonometry.timeline import build_timeline
from autonometry.witness import SessionWitness, _Clock, _intersect, _subtract, _union
from timeline_helpers import FULL
from witness_helpers import RULES, events, run, secs, session, station

S = 1_000_000_000


def one(robot: Any, *stations: Any, **options: Any) -> SessionWitness:
    (result,) = run([robot], list(stations), **options).sessions
    return result


def times(result: SessionWitness) -> dict[str, float]:
    return {k: round(v, 6) for k, v in result.cells.to_json().items()}


def codes(result: SessionWitness) -> list[str]:
    return [r.code for r in result.reasons]


# -- 12.1 station log ---------------------------------------------------------------------------


START: dict[str, Any] = {
    "robot_id": "r01",
    "session_id": "st01",
    "task_id": None,
    "seq": 0,
    "ts": "2026-11-12T08:00:00.000Z",
    "type": "session_start",
    "scopes": ["whole_robot"],
    "initial_states": {"whole_robot": "idle"},
    "heartbeat_interval_s": 1.0,
    "hint_policy": "hint_as_shared",
    "task_registry_hash": "0" * 64,
    "plan_hash": "0" * 64,
    "simulated": False,
}


def test_station_session_start_needs_station_id__defs_12_1() -> None:
    check_event(START | {"role": "station", "station_id": "pc_1"}, "0.3")
    check_event(START | {"role": "robot"}, "0.3")
    with pytest.raises(RejectedEventError):
        check_event(START | {"role": "station"}, "0.3")
    with pytest.raises(RejectedEventError):
        check_event(START | {"role": "robot", "station_id": "pc_1"}, "0.3")
    with pytest.raises(RejectedEventError):
        check_event(START | {"role": "operator", "station_id": "pc_1"}, "0.3")


def test_role_and_station_id_only_on_session_start__defs_12_1() -> None:
    heartbeat = {k: START[k] for k in ("robot_id", "session_id", "task_id", "seq", "ts")}
    check_event(heartbeat | {"type": "heartbeat"}, "0.3")
    with pytest.raises(RejectedEventError):
        check_event(heartbeat | {"type": "heartbeat", "role": "robot"}, "0.3")
    with pytest.raises(RejectedEventError):
        check_event(heartbeat | {"type": "heartbeat", "station_id": "pc_1"}, "0.3")


def test_role_defaults_to_robot_and_timeline_keeps_station_id__defs_12_1() -> None:
    robot = session([(0, "ai")], 10)
    st = station([], 10, station_id="pc_7")
    assert (robot.role, robot.station_id) == ("robot", None)
    assert (st.role, st.station_id) == ("station", "pc_7")
    assert robot.to_json()["role"] == "robot"
    assert st.to_json()["station_id"] == "pc_7"


def test_station_states_human_idle_unknown__defs_12_1() -> None:
    robot = session([(0, "ai")], 60)
    st = station([(10, "human_remote"), (20, "idle"), (30, "unknown"), (40, "idle")], 60)
    result = one(robot, st)
    assert times(result)["station_only_s"] == 10.0
    assert times(result)["unwitnessed_s"] == 10.0
    assert times(result)["neither_s"] == 40.0


def test_other_station_state_counts_as_human_and_gives_no_l3__defs_12_1() -> None:
    robot = session([(0, "ai")], 60)
    st = station([(10, "ai"), (20, "idle")], 60)
    result = one(robot, st)
    assert times(result)["station_only_s"] == 10.0
    assert "invalid_station_state" in codes(result)
    assert result.reasons[codes(result).index("invalid_station_state")].detail.endswith(": ai")


def test_session_on_the_wrong_side_is_ignored__defs_12_1() -> None:
    robot = session([(0, "ai")], 60)
    st = station([], 60)
    result = run([robot, st], [st, robot])
    assert [(i.session_id, i.given_as, i.role) for i in result.ignored] == [
        ("s01", "station", "robot"),
        ("st01", "robot", "station"),
    ]
    assert [s.session_id for s in result.sessions] == ["s01"]
    assert [s.session_id for s in result.stations] == ["st01"]


# -- 12.2 pairing -------------------------------------------------------------------------------


def test_station_of_same_robot_overlapping_witnesses__defs_12_2() -> None:
    robot = session([(0, "ai")], 60)
    other_robot = station([], 60, robot_id="r02")
    before = station([], -10, start=-30)
    result = one(robot, other_robot, before)
    assert codes(result) == ["no_station"]
    assert result.stations == ()
    assert times(result)["unwitnessed_s"] == 60.0


def test_sessions_without_session_start_witness_nothing__defs_12_2() -> None:
    raw = events([(0, "ai")], 60)[1:]
    headless = Session("r01", "s01", tuple(raw))
    robot = build_timeline(headless, FULL, RULES, check_session(headless, RULES))
    result = one(robot, station([], 60))
    assert (codes(result), result.cells.base_ns) == (["no_station"], 0)
    raw_station = events([], 60, role="station", session_id="st01")[1:]
    headless_station = Session("r01", "st01", tuple(raw_station))
    st = build_timeline(headless_station, FULL, RULES, check_session(headless_station, RULES))
    assert codes(one(session([(0, "ai")], 60), st)) == ["no_station"]


def test_restarted_station_sessions_witness_together__defs_12_2() -> None:
    robot = session([(0, "ai"), (25, "human_remote"), (35, "ai")], 60)
    first = station([(25, "human_remote")], 30, start=-5, session_id="st01")
    second = station([(30, "human_remote"), (35, "idle")], 61, start=30, session_id="st02")
    result = one(robot, first, second)
    assert [s.session_id for s in result.stations] == ["st01", "st02"]
    assert result.l3
    assert times(result)["both_human_s"] == 10.0
    assert times(result)["unwitnessed_s"] == 0.0


def test_unwitnessed_where_no_station_session_or_scope__defs_12_2() -> None:
    robot = session([(0, "ai", "base"), (0, "ai", "arm")], 60, scopes=("base", "arm"))
    st = station([], 61, start=30, scopes=("base", "lidar"))
    result = one(robot, st)
    assert times(result)["unwitnessed_s"] == 30.0 + 60.0
    assert {k: secs(v.unwitnessed_ns) for k, v in result.by_scope.items()} == {
        "base": 30.0,
        "arm": 60.0,
    }


# -- 12.3 tolerance -----------------------------------------------------------------------------


def test_tau_adds_both_clock_errors_and_react_s__defs_12_3() -> None:
    robot = session([(0, "ai"), (20, "human_remote"), (30, "ai")], 60, clock_error=0.2)
    st = station([(18.5, "human_remote"), (30, "idle")], 60, clock_error=0.3)
    result = one(robot, st)
    assert secs(result.tau_min_ns) == secs(result.tau_max_ns) == 1.0
    assert times(result)["station_only_s"] == 0.5
    assert times(result)["tolerated_s"] == 1.0


def test_eps_from_nearest_heartbeat_earlier_on_tie__defs_12_3() -> None:
    clock = _Clock((10 * S, 20 * S), (0.1, 0.2), 1.0)
    assert [clock.eps_s(t * S) for t in (5, 14, 15, 16, 25)] == [0.1, 0.1, 0.1, 0.2, 0.2]
    assert _Clock((), (), 1.0).eps_s(0) == 1.0


def test_no_reported_error_uses_clock_tolerance_and_gives_no_l3__defs_12_3() -> None:
    robot = session([(0, "ai")], 60, clock_error=None)
    result = one(robot, station([], 60))
    assert secs(result.tau_min_ns) == pytest.approx(1.0 + 0.01 + 0.5)
    assert codes(result) == ["robot_clock_unsynced"]


def test_implausible_error_without_ptp_is_replaced_and_flagged__defs_12_3() -> None:
    robot = session([(0, "ai")], 60)
    result = one(robot, station([], 60, clock_error=0.0001))
    assert secs(result.tau_min_ns) == pytest.approx(0.01 + 0.05 + 0.5)
    assert [(f.role, f.code) for f in result.flags] == [("station", "implausible_clock_error")]
    assert result.l3
    with_ptp = one(robot, station([], 60, clock_error=0.0001, clock_source="ptp"))
    assert secs(with_ptp.tau_min_ns) == pytest.approx(0.01 + 0.0001 + 0.5)
    assert with_ptp.flags == ()


def test_unsynced_side_gives_no_l3__defs_12_3() -> None:
    robot = session([(0, "ai")], 60)
    assert codes(one(robot, station([], 60, clock_source="none"))) == ["station_clock_unsynced"]
    assert codes(one(robot, station([], 60, clock_error=1.5))) == ["station_clock_unsynced"]
    assert codes(one(session([(0, "ai")], 60, clock_source="unknown"), station([], 60))) == [
        "robot_clock_unsynced"
    ]


# -- 12.4 classification ------------------------------------------------------------------------


def test_honest_takeover_both_human_with_tolerated_edges__defs_12_4() -> None:
    robot = session([(0, "ai"), (20, "human_remote"), (30, "ai")], 60)
    st = station([(20.2, "human_remote"), (30.1, "idle")], 61, start=-5)
    result = one(robot, st)
    assert times(result) == {
        "base_s": 60.0,
        "both_human_s": 10.1,
        "tolerated_s": 0.3,
        "held_s": 0.0,
        "robot_only_s": 0.0,
        "station_only_s": 0.0,
        "neither_s": 49.9,
        "unwitnessed_s": 0.0,
        "disputed_s": 0.0,
    }
    assert result.l3


def test_shared_is_robot_human_time__defs_12_4() -> None:
    robot = session([(0, "ai"), (20, "shared"), (30, "ai")], 60)
    result = one(robot, station([(20, "human_remote"), (30, "idle")], 60))
    assert times(result)["both_human_s"] == 10.0
    assert result.l3


def test_local_control_station_saw_is_station_only__defs_12_4() -> None:
    robot = session([(0, "ai"), (20, "human_local"), (30, "ai")], 60)
    result = one(robot, station([(20, "human_remote"), (30, "idle")], 60))
    assert times(result)["station_only_s"] == 10.0
    assert codes(result) == ["station_only_above_max", "disputed_above_max"]


def test_station_input_while_robot_idle_is_station_only__defs_12_4() -> None:
    robot = session([(0, "ai"), (10, "idle")], 60)
    result = one(robot, station([(30, "human_remote"), (35, "idle")], 60))
    assert times(result)["base_s"] == 15.0
    assert times(result)["station_only_s"] == 5.0


def test_disagreement_excused_only_near_other_sides_human_time__defs_12_4() -> None:
    robot = session([(0, "ai")], 60)
    result = one(robot, station([(20, "human_remote"), (21, "idle")], 60))
    assert times(result)["station_only_s"] == 1.0
    assert times(result)["tolerated_s"] == 0.0


def test_hold_after_matched_run_is_held_not_disputed__defs_12_4() -> None:
    robot = session([(0, "ai"), (20, "human_remote"), (40, "idle")], 60)
    result = one(robot, station([(20, "human_remote"), (25, "idle")], 60))
    assert times(result)["both_human_s"] == 20.0
    assert times(result)["tolerated_s"] == 0.52
    assert times(result)["held_s"] == 14.48
    assert times(result)["robot_only_s"] == 0.0
    assert result.l3


def test_unmatched_robot_run_is_robot_only__defs_12_4() -> None:
    robot = session([(0, "ai"), (20, "human_remote"), (30, "ai")], 60)
    result = one(robot, station([], 60))
    assert times(result)["robot_only_s"] == 10.0
    assert codes(result) == ["disputed_above_max"]
    assert [(r.scope, secs(r.end_ns - r.start_ns)) for r in result.unmatched_robot_runs] == [
        ("whole_robot", 10.0)
    ]


def test_scopes_reconciled_one_by_one_and_summed__defs_12_4() -> None:
    robot = session(
        [(0, "ai", "base"), (0, "ai", "arm"), (20, "human_remote", "arm"), (30, "ai", "arm")],
        60,
        scopes=("base", "arm"),
    )
    st = station([(20, "human_remote", "base"), (30, "idle", "base")], 60, scopes=("base", "arm"))
    result = one(robot, st)
    assert times(result)["base_s"] == 120.0
    assert secs(result.by_scope["arm"].robot_only_ns) == 10.0
    assert secs(result.by_scope["base"].station_only_ns) == 10.0
    assert times(result)["disputed_s"] == 20.0


def test_station_human_while_robot_unknown_is_reported_not_disputed__defs_12_4() -> None:
    robot = session([(0, "ai"), (20, "unknown"), (30, "ai")], 60)
    result = one(robot, station([(22, "human_remote"), (28, "idle")], 60))
    assert secs(result.station_human_robot_unknown_ns) == 6.0
    assert times(result)["base_s"] == 50.0
    assert times(result)["disputed_s"] == 0.0


# -- 12.5 episodes ------------------------------------------------------------------------------


def test_unmatched_station_episodes_raise_upper_bound__defs_12_5() -> None:
    robot = session(
        [(0, "ai", "base"), (0, "ai", "arm"), (50, "human_remote", "base"), (55, "ai", "base")],
        60,
        scopes=("base", "arm"),
    )
    st = station(
        [
            (20, "human_remote", "base"),
            (21, "idle", "base"),
            (20.3, "human_remote", "arm"),
            (21, "idle", "arm"),
            (40, "human_remote", "base"),
            (41, "idle", "base"),
            (50, "human_remote", "base"),
            (55, "idle", "base"),
        ],
        60,
        scopes=("base", "arm"),
    )
    result = one(robot, st)
    assert result.matched_station_episodes == 1
    assert robot.start_ns is not None
    starts = [
        (e.scope, secs(e.start_ns - robot.start_ns)) for e in result.unmatched_station_episodes
    ]
    assert starts == [
        ("base", pytest.approx(20.0)),
        ("arm", pytest.approx(20.3)),
        ("base", pytest.approx(40.0)),
    ]
    assert (result.robot_interventions, result.upper_bound_interventions) == (1, 3)


def test_station_summary_human_time_outside_robot_sessions__defs_12_5() -> None:
    robot = session([(0, "ai")], 60)
    st = station([(70, "human_remote"), (75, "idle")], 80, start=-5)
    (summary,) = run([robot], [st]).stations
    assert summary.witnesses == ("s01",)
    assert (secs(summary.human_ns), secs(summary.human_outside_robot_sessions_ns)) == (5.0, 5.0)


# -- 12.6 verdict -------------------------------------------------------------------------------


def test_scenario_s2_hidden_remote_operator_fails_l3__defs_12_6() -> None:
    """Research scenario S2: the robot logs ai while the station shows live input."""
    robot = session([(0, "ai")], 300)
    st = station([(100, "human_remote"), (130, "idle")], 301, start=-5)
    result = one(robot, st)
    assert times(result)["station_only_s"] == 30.0
    assert not result.l3
    assert result.level == "L2"
    assert codes(result) == ["station_only_above_max", "disputed_above_max"]
    assert "0.1000 > 0.005" in result.reasons[0].detail
    assert result.upper_bound_interventions == 1


def test_honest_pair_reaches_l3__defs_12_6() -> None:
    robot = session([(0, "ai"), (100, "human_remote"), (130, "ai")], 300)
    st = station([(100.1, "human_remote"), (130.05, "idle")], 301, start=-5)
    result = one(robot, st)
    assert (result.l3, result.level, result.reasons) == (True, "L3", ())


def test_shares_at_the_maximum_pass_and_above_fail__defs_12_6() -> None:
    robot = session([(0, "ai")], 1000)
    at_max = one(robot, station([(100, "human_remote"), (105, "idle")], 1000))
    assert at_max.shares()["station_only_with_unwitnessed"] == 0.005
    assert at_max.l3
    above = one(robot, station([(100, "human_remote"), (105.001, "idle")], 1000))
    assert codes(above) == ["station_only_above_max"]


def test_unwitnessed_counts_as_disputed__defs_12_6() -> None:
    robot = session([(0, "ai")], 1000)
    result = one(robot, station([(990, "unknown")], 1000))
    assert times(result)["unwitnessed_s"] == 10.0
    assert times(result)["disputed_s"] == 0.0
    assert codes(result) == ["station_only_above_max"]


def test_both_logs_must_be_verified__defs_12_6() -> None:
    robot = session([(0, "ai")], 60)
    st = station([], 60)
    unverified = one(robot, st, status="unverified")
    assert codes(unverified) == ["robot_not_verified", "station_not_verified"]
    assert unverified.level == "L1"
    tampered = one(robot, st, status="verified", station_status="failed")
    assert codes(tampered) == ["station_not_verified"]
    assert tampered.reasons[0].detail == "st01 (pc_1) failed"
    assert tampered.level == "L2"


def test_no_base_time_has_nothing_to_dispute__defs_12_6() -> None:
    result = one(session([], 60), station([], 60))
    assert result.cells.base_ns == 0
    assert result.shares()["disputed"] is None
    assert result.l3


def test_result_does_not_depend_on_input_order__defs_12_6() -> None:
    robots = [session([(0, "ai")], 60), session([(0, "ai")], 60, session_id="s02")]
    stations = [station([(5, "human_remote")], 60), station([], 60, session_id="st02")]
    forward = json.dumps(run(robots, stations).to_json())
    assert forward == json.dumps(run(robots[::-1], stations[::-1]).to_json())


# -- interval helpers ---------------------------------------------------------------------------


def test_interval_helpers() -> None:
    a = _union([(0, 10), (5, 15), (20, 30), (40, 40)])
    assert a == [(0, 15), (20, 30)]
    assert _intersect(a, [(10, 25)]) == [(10, 15), (20, 25)]
    assert _subtract(a, [(2, 4), (12, 22), (29, 50)]) == [(0, 2), (4, 12), (22, 29)]
    assert _subtract(a, []) == a
    assert _intersect(a, []) == []


def test_metrics_leave_station_sessions_out__defs_12_1() -> None:
    from autonometry.stats.metrics import Options  # noqa: PLC0415 -- report extra
    from autonometry.stats.report import metrics_report  # noqa: PLC0415 -- report extra

    report = metrics_report([station([(5, "human_remote")], 60)], RULES, Options(n_boot=10))
    assert report["groups"] == {}
    assert report["station_sessions"] == [{"robot_id": "r01", "session_id": "st01"}]
