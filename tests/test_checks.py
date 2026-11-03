from typing import Any, cast

from autonometry.checks import check_session
from autonometry.events import Event, Session
from autonometry.spec import load_rules

RULES = load_rules("0.2")
ZERO_HASH = "0" * 64


def _event(seq: int, second: float, type_: str = "heartbeat", **fields: Any) -> Event:
    event = {
        "robot_id": "r07",
        "session_id": "s42",
        "task_id": None,
        "seq": seq,
        "ts": f"2026-10-30T09:00:{second:06.3f}Z",
        "type": type_,
    }
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
    ) | cast(Event, fields)


def _codes(*events: Event) -> list[tuple[int, str]]:
    flags = check_session(Session("r07", "s42", events), RULES)
    return sorted((flag.seq, flag.code) for flag in flags)


def test_clean_session_has_no_flags__defs_2() -> None:
    assert _codes(_start(), _event(1, 1), _event(2, 2, "session_end")) == []


def test_flags_carry_the_session_identity__defs_1() -> None:
    (flag,) = check_session(Session("r07", "s42", (_event(0, 0, "session_end"),)), RULES)
    assert (flag.robot_id, flag.session_id, flag.code) == ("r07", "s42", "no_session_start")


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
        _event(4, 4, "session_end"),
    )
    assert _codes(*events) == [(2, "nested_task")]


def test_every_unclosed_task_is_flagged_at_its_start__defs_5() -> None:
    events = (
        _start(),
        _event(1, 1, "task_start", task_id="a", task_definition_id="d"),
        _event(2, 2, "task_start", task_id="b", task_definition_id="d"),
        _event(3, 3, "session_end"),
    )
    assert _codes(*events) == [(1, "task_not_closed"), (2, "nested_task"), (2, "task_not_closed")]


def test_scopes_are_not_checked_without_session_start__defs_1() -> None:
    events = (_event(0, 0, "help_request", scope="left_arm"), _event(1, 1, "session_end"))
    assert _codes(*events) == [(0, "no_session_start")]


def test_first_session_start_declares_the_scopes__defs_1() -> None:
    events = (
        _start(),
        _start(1, scopes=["left_arm"], initial_states={"left_arm": "ai"}),
        _event(2, 1, "help_request", scope="left_arm"),
        _event(3, 2, "session_end"),
    )
    assert _codes(*events) == [(2, "undeclared_scope")]


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
