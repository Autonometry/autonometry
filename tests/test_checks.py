from typing import Any, cast

from autonometry.checks import check_session
from autonometry.events import Event, Session
from autonometry.spec import load_rules

RULES = load_rules("0.3")
ZERO_HASH = "0" * 64
SYNCED = {"clock_source": "ntp", "clock_max_error_s": 0.001}


def _event(seq: int, second: float, type_: str = "heartbeat", **fields: Any) -> Event:
    event = {
        "robot_id": "r07",
        "session_id": "s42",
        "task_id": None,
        "seq": seq,
        "ts": f"2026-10-30T09:00:{second:06.3f}Z",
        "type": type_,
    }
    if type_ == "heartbeat":
        event |= SYNCED
    return cast(Event, event | fields)


def _start(seq: int = 0, **fields: Any) -> Event:
    return _event(
        seq,
        0,
        "session_start",
        scopes=["whole_robot"],
        initial_states={"whole_robot": "idle"},
        heartbeat_interval_s=1.0,
        hint_policy="hint_as_shared",
        task_registry_hash=ZERO_HASH,
        plan_hash=ZERO_HASH,
        simulated=False,
    ) | cast(Event, fields)


def _codes(*events: Event) -> list[tuple[int, str]]:
    flags = check_session(Session("r07", "s42", events), RULES)
    return sorted((flag.seq, flag.code) for flag in flags)


def test_clean_session_has_no_flags__defs_2() -> None:
    assert _codes(_start(), _event(1, 1), _event(2, 2, "session_end")) == []


def test_flags_carry_the_session_identity__defs_1() -> None:
    flags = check_session(Session("r07", "s42", (_event(0, 0, "session_end"),)), RULES)
    assert {(flag.robot_id, flag.session_id, flag.code) for flag in flags} == {
        ("r07", "s42", "no_session_start"),
        ("r07", "s42", "clock_unsynced"),
    }


def test_silence_uses_the_declared_heartbeat__defs_2_2() -> None:
    slow = _start(heartbeat_interval_s=5.0)
    assert _codes(slow, _event(1, 7), _event(2, 14.5, "session_end")) == [(2, "time_gap")]


def test_silence_without_session_start_uses_the_default_heartbeat__defs_2_2() -> None:
    assert RULES.params.heartbeat_interval_s + RULES.params.gap_tolerance_s == 3.0
    assert _codes(_event(0, 0), _event(1, 3), _event(2, 6.001, "session_end")) == [
        (0, "no_session_start"),
        (2, "time_gap"),
    ]


def test_seq_gap_and_time_gap_are_both_reported__defs_2_2() -> None:
    assert _codes(_start(), _event(3, 9), _event(4, 10, "session_end")) == [
        (3, "seq_gap"),
        (3, "time_gap"),
    ]


def test_ts_regression_is_not_also_a_time_gap__defs_3() -> None:
    assert _codes(_start(), _event(1, 2), _event(2, 1), _event(3, 2, "session_end")) == [
        (2, "ts_regression")
    ]


def test_open_task_comes_only_from_task_start_and_end__defs_1() -> None:
    events = (
        _start(),
        _event(1, 1, task_id="t1"),
        _event(2, 2, "task_start", task_id="t2", task_definition_id="d"),
        _event(3, 3, task_id="t9"),
        _event(4, 4, "task_end", task_id="t1", outcome="success"),
        _event(5, 5, "task_end", task_id="t2", outcome="success"),
        _event(6, 6, "session_end"),
    )
    assert _codes(*events) == [(4, "task_end_without_start")]


def test_restarting_an_open_task_is_nested_and_closes_once__defs_5() -> None:
    events = (
        _start(),
        _event(1, 1, "task_start", task_id="t1", task_definition_id="d"),
        _event(2, 2, "task_start", task_id="t1", task_definition_id="d"),
        _event(3, 3, "task_end", task_id="t1", outcome="success"),
        _event(4, 4),
        _event(5, 5, "session_end"),
    )
    assert _codes(*events) == [(2, "nested_task")]


def test_every_unclosed_task_is_flagged_at_its_start__defs_5() -> None:
    events = (
        _start(),
        _event(1, 1, "task_start", task_id="a", task_definition_id="d"),
        _event(2, 2, "task_start", task_id="b", task_definition_id="d"),
        _event(3, 3),
        _event(4, 4, "session_end"),
    )
    assert _codes(*events) == [(1, "task_not_closed"), (2, "nested_task"), (2, "task_not_closed")]


def test_scopes_are_not_checked_without_session_start__defs_1() -> None:
    events = (
        _event(0, 0, "help_request", scope="left_arm"),
        _event(1, 1),
        _event(2, 2, "session_end"),
    )
    assert _codes(*events) == [(0, "no_session_start")]


def test_first_session_start_declares_the_scopes__defs_1() -> None:
    events = (
        _start(),
        _start(1, scopes=["left_arm"], initial_states={"left_arm": "ai"}),
        _event(2, 1, "help_request", scope="left_arm"),
        _event(3, 2),
        _event(4, 3, "session_end"),
    )
    assert _codes(*events) == [(1, "repeated_session_marker"), (2, "undeclared_scope")]


def test_repeated_session_marker__defs_1() -> None:
    events = (
        _start(),
        _event(1, 1, "session_end"),
        _start(2),
        _event(3, 2, "session_end"),
        _event(4, 2.5, "session_end"),
    )
    flags = check_session(Session("r07", "s42", events), RULES)
    repeated = [(f.seq, f.detail) for f in flags if f.code == "repeated_session_marker"]
    assert repeated == [
        (2, "session_start repeats the one at seq 0"),
        (3, "session_end repeats the one at seq 1"),
        (4, "session_end repeats the one at seq 1"),
    ]


def test_event_outside_session_once_per_side_with_count__defs_1() -> None:
    events = (
        _event(0, 0),
        _event(1, 0),
        _start(2),
        _event(3, 1),
        _event(4, 2, "session_end"),
        _event(5, 3),
    )
    flags = check_session(Session("r07", "s42", events), RULES)
    outside = [(f.seq, f.detail) for f in flags if f.code == "event_outside_session"]
    assert outside == [(0, "2 events before session_start"), (5, "1 event after session_end")]
    assert _codes(*events) == [(0, "event_outside_session"), (5, "event_outside_session")]


def test_no_outside_flag_without_the_marker__defs_1() -> None:
    events = (_event(0, 0), _event(1, 1, "session_end"))
    assert _codes(*events) == [(0, "no_session_start")]
    assert _codes(_start(), _event(1, 1)) == [(1, "no_session_end")]


def test_distance_compares_with_the_previous_reading__defs_8() -> None:
    events = (
        _start(distance_m=0),
        _event(1, 1, distance_m=5.0),
        _event(2, 2),
        _event(3, 3, distance_m=4.0),
        _event(4, 4, distance_m=4.5),
        _event(5, 5, "session_end", distance_m=4.5),
    )
    assert _codes(*events) == [(3, "distance_regression")]


def test_only_json_true_marks_a_reconstructed_ts__defs_8() -> None:
    events = (
        _start(),
        _event(1, 1, x_ts_reconstructed="true"),
        _event(2, 2, x_ts_reconstructed=1),
        _event(3, 3, "session_end", x_ts_reconstructed=True),
    )
    (flag,) = check_session(Session("r07", "s42", events), RULES)
    assert (flag.seq, flag.code, flag.detail) == (
        3,
        "ts_reconstructed",
        "1 event with x_ts_reconstructed: true",
    )


def _clock_flags(*heartbeats: Event) -> list[tuple[int, str]]:
    events = (_start(), *heartbeats, _event(len(heartbeats) + 1, 9, "session_end"))
    flags = check_session(Session("r07", "s42", events), RULES)
    return [(flag.seq, flag.detail) for flag in flags if flag.code == "clock_unsynced"]


def test_clock_unsynced__defs_8() -> None:
    """One flag per session, at the first unsynced heartbeat, with the count."""
    assert _clock_flags(
        _event(1, 1),
        _event(2, 2, clock_source="none"),
        _event(3, 3, clock_source="unknown"),
        _event(4, 4, clock_source="ptp", clock_max_error_s=0.0001),
    ) == [
        (
            2,
            "2 of 4 heartbeat(s) without a synchronised clock "
            "(clock_source none, unknown or missing, or clock_max_error_s above 1 s)",
        )
    ]


def test_synced_clock_sources_are_not_flagged__defs_8() -> None:
    heartbeats = [
        _event(seq, seq, clock_source=source, clock_max_error_s=RULES.sealing.clock_tolerance_s)
        for seq, source in enumerate(("ntp", "ptp", "gnss"), start=1)
    ]
    assert _clock_flags(*heartbeats) == []
    assert _clock_flags(_event(1, 1, clock_source="gnss")) == []


def test_clock_error_above_tolerance_is_unsynced__defs_8() -> None:
    tolerance = RULES.sealing.clock_tolerance_s
    assert _clock_flags(_event(1, 1, clock_max_error_s=tolerance + 0.001)) == [
        (
            1,
            "1 of 1 heartbeat(s) without a synchronised clock "
            "(clock_source none, unknown or missing, or clock_max_error_s above 1 s)",
        )
    ]


def test_heartbeat_without_clock_source_is_unknown__defs_8() -> None:
    """A heartbeat that does not report its clock counts as clock_source unknown (fail visible)."""
    bare = _event(1, 1)
    del bare["clock_source"]  # type: ignore[misc]
    assert _clock_flags(bare)[0][0] == 1


def test_session_without_heartbeats_is_clock_unsynced__defs_8() -> None:
    events = (_start(), _event(1, 1, "help_request", scope="whole_robot"))
    events += (_event(2, 2, "session_end"),)
    flags = check_session(Session("r07", "s42", events), RULES)
    assert [(f.seq, f.code, f.detail) for f in flags] == [
        (0, "clock_unsynced", "no heartbeat reports the clock status")
    ]


def test_attribution_inferred__defs_6_12() -> None:
    """Like ts_reconstructed: one flag per session at the first marked event, with the count;
    only JSON true marks an event."""
    events = (
        _start(),
        _event(1, 1, x_attribution_inferred="true"),
        _event(2, 2, "help_request", scope="whole_robot", x_attribution_inferred=True),
        _event(3, 3, x_attribution_inferred=False),
        _event(4, 4, "session_end", x_attribution_inferred=True),
    )
    (flag,) = check_session(Session("r07", "s42", events), RULES)
    assert (flag.seq, flag.code, flag.detail) == (
        2,
        "attribution_inferred",
        "2 events with x_attribution_inferred: true",
    )


def test_both_markers_give_two_flags__defs_6_12() -> None:
    events = (
        _start(x_ts_reconstructed=True, x_attribution_inferred=True),
        _event(1, 1),
        _event(2, 2, "session_end"),
    )
    assert _codes(*events) == [(0, "attribution_inferred"), (0, "ts_reconstructed")]
