"""Rule tests for autonometry.timeline; each test name cites the definitions.md section."""

from fractions import Fraction

import pytest

from autonometry.events import Flag
from autonometry.timeline import (
    DiscreteIntervention,
    Registries,
    dwell_human_ns,
    dwell_shift,
    overlap_ns,
)
from timeline_helpers import DEVICES, FULL, NS, PLAN, TASKS, Log, at, codes, seconds


def _states(totals: object) -> dict[str, float]:
    return {k: seconds(v) for k, v in getattr(totals, "states").items() if v}  # noqa: B009


_SOURCES: dict[str, tuple[str | None, str | None]] = {
    "ai": (None, "policy"),
    "human_local": ("human_initiated", "joy_local"),
    "human_remote": ("human_initiated", "vr_remote"),
    "shared": ("human_initiated", "vr_remote"),
    "scripted": (None, "replay"),
    "safety_stop": ("safety", None),
    "idle": (None, None),
    "unknown": (None, None),
}


# --- Section 2: states, gaps, observed time -----------------------------------------------------


def test_missing_initial_state_is_unknown_until_first_transition__defs_2_rule_1() -> None:
    log = Log(["a", "b"], initial={"a": "idle"}).ai(10, "b").ai(10, "a").end(20)
    timeline = log.build()
    assert _states(timeline.per_scope["b"]) == {"unknown": 10, "ai": 10}
    assert _states(timeline.per_scope["a"]) == {"idle": 10, "ai": 10}


def test_initial_control_state_has_no_named_source__defs_6_10() -> None:
    log = Log(["a", "b", "c"], initial={"a": "ai", "b": "safety_stop", "c": "unknown"})
    timeline = log.ai(5, "a").end(10).build()
    assert _states(timeline.per_scope["a"]) == {"human_remote": 5, "ai": 5}
    assert _states(timeline.per_scope["b"]) == {"safety_stop": 10}
    assert codes(timeline) == [(0, "undeclared_device")]
    assert "initial state ai has no named source" in timeline.flags[0].detail


def test_silence_beyond_heartbeat_plus_tolerance_is_unknown__defs_2_rule_2() -> None:
    log = Log(heartbeat_s=1.0).ai(1).add(2, "heartbeat").add(10, "heartbeat").end(11)
    timeline = log.build()
    # silence 8 s after seq 2; heartbeat 1 s + tolerance 2 s: unknown from 5 s to 10 s
    assert _states(timeline.whole_robot) == {"idle": 1, "ai": 5, "unknown": 5}


def test_silence_at_the_limit_is_not_a_gap__defs_2_rule_2() -> None:
    timeline = Log(heartbeat_s=1.0).ai(0).add(3, "heartbeat").end(6).build()
    assert _states(timeline.whole_robot) == {"ai": 6}


def test_seq_gap_is_unknown_until_the_next_transition__defs_2_rule_2() -> None:
    log = Log(["a", "b"]).ai(1, "a").ai(1, "b").add(2, "heartbeat").skip()
    timeline = log.add(4, "heartbeat").ai(6, "a").end(10).build()
    assert _states(timeline.per_scope["a"]) == {"idle": 1, "ai": 5, "unknown": 4}
    assert _states(timeline.per_scope["b"]) == {"idle": 1, "ai": 1, "unknown": 8}
    gap = [s for s in timeline.spans if s.scope == "b" and s.state == "unknown"]
    assert {s.seq for s in gap} == {5}


def test_observed_time_is_session_start_to_session_end__defs_2_rule_4() -> None:
    log = Log().ai(5).end(20).add(30, "heartbeat")
    timeline = log.build()
    assert timeline.observed_ns == 20 * NS
    assert timeline.session_end_seq == 2
    assert sum(timeline.whole_robot.states.values()) == timeline.observed_ns


def test_events_before_session_start_and_repeated_start_are_ignored__defs_2_rule_4() -> None:
    log = Log()
    start = log.events[0]
    log.events.insert(0, {**start, "type": "heartbeat", "ts": "2026-11-12T07:59:00.000Z"})
    start["seq"] = 1
    log.seq = 2
    repeated = {k: v for k, v in start.items() if k not in ("seq", "ts", "type")}
    timeline = log.ai(5).add(6, "session_start", **repeated).end(10).build()
    assert timeline.session_start_seq == 1
    assert timeline.observed_ns == 10 * NS
    assert _states(timeline.whole_robot) == {"idle": 5, "ai": 5}


def test_session_without_session_start_has_no_observed_time__defs_2_rule_4() -> None:
    log = Log().ai(5).end(10)
    log.events.pop(0)
    timeline = log.build()
    assert timeline.start_ns is None
    assert timeline.observed_ns == 0
    assert timeline.to_json()["time"]["whole_robot"]["observed_s"] == 0.0


def test_session_without_session_end_ends_at_its_last_event__defs_2_rule_4() -> None:
    log = Log().add(1, "task_start").ai(1).add(9, "heartbeat")
    timeline = log.build()
    assert timeline.observed_ns == 9 * NS
    assert timeline.session_end_seq is None
    (task,) = timeline.tasks
    assert (task.outcome, task.closed, task.end_seq, task.end_ns - task.start_ns) == (
        "unknown",
        False,
        None,
        8 * NS,
    )


def test_ts_going_backwards_takes_the_latest_ts_before__defs_3() -> None:
    timeline = Log().ai(10).human(5).ai(12).end(20).build()
    assert _states(timeline.whole_robot) == {"idle": 10, "human_remote": 2, "ai": 8}


# --- Section 3: reasons -------------------------------------------------------------------------


def test_ai_requested_help_without_help_request_is_recoded__defs_3() -> None:
    log = Log().add(1, "task_start").ai(1).add(2, "help_request", scope="other")
    timeline = log.human(5, reason="ai_requested_help").ai(8).end(10).build()
    assert timeline.episodes[0].reason == "human_initiated"
    assert codes(timeline) == [(4, "help_reason_recoded")]


def test_help_request_older_than_help_latency_is_recoded__defs_3() -> None:
    log = (
        Log(heartbeat_s=200).add(1, "task_start").ai(1).add(2, "help_request", scope="whole_robot")
    )
    timeline = log.human(122.5, reason="ai_requested_help").end(130).build()
    assert timeline.episodes[0].reason == "human_initiated"
    on_time = Log().add(1, "task_start").ai(1).add(2, "help_request", scope="whole_robot")
    kept = on_time.human(122, reason="ai_requested_help").end(130)
    assert kept.build().episodes[0].reason == "ai_requested_help"


def test_planned_without_its_plan_item_is_recoded_and_unplanned__defs_3() -> None:
    log = Log().human(1, "human_local", "planned", plan_item_id="other_item").end(5)
    timeline = log.build()
    assert timeline.spans[-1].reason == "human_initiated"
    assert codes(timeline) == [(1, "unplanned")]
    assert timeline.reset_unplanned_ns == 4 * NS


def test_missing_plan_recodes_every_planned_reason__defs_8() -> None:
    log = Log().human(1, "human_local", "planned", plan_item_id="reset_scene").end(5)
    timeline = log.build(Registries.from_documents(DEVICES, TASKS, None))
    assert codes(timeline) == [(1, "unplanned")]
    assert "cannot be checked: no plan" in timeline.flags[0].detail
    assert timeline.registries["plan"] == "missing"


# --- Section 3: interventions -------------------------------------------------------------------


def test_hand_over_with_several_steps_is_one_intervention__defs_3() -> None:
    log = Log().add(0, "task_start").ai(0).human(1, "shared").human(2, "human_local")
    timeline = log.human(3, "shared").ai(4).end(5).build()
    (episode,) = timeline.episodes
    assert (episode.kind, at(timeline, episode.start_ns), at(timeline, episode.end_ns)) == (
        "shared",
        1,
        4,
    )
    assert seconds(episode.human_ns) == 3
    assert timeline.interventions_per_scope == timeline.interventions_whole_robot == 1


def test_episode_ends_when_the_task_ends__defs_3() -> None:
    log = (
        Log().add(0, "task_start").ai(0).human(2).add(5, "task_end").add(7, "task_start", task="t2")
    )
    timeline = log.ai(9).end(10).build()
    (episode,) = timeline.episodes
    assert (at(timeline, episode.end_ns), episode.end_seq) == (5, 4)
    assert [t.autonomous for t in timeline.tasks] == [False, False]
    assert seconds(timeline.tasks[1].human_ns) == 2


def test_episode_open_at_session_end_ends_there__defs_3() -> None:
    timeline = Log().ai(0).human(2).end(6).build()
    (episode,) = timeline.episodes
    assert (at(timeline, episode.end_ns), episode.end_seq) == (6, 3)


def test_scripted_started_by_a_program_ends_an_episode__defs_3() -> None:
    log = Log().ai(0).human(1).tr(2, "scripted", "human_initiated", "script:p", "replay")
    timeline = log.tr(3, "scripted", None, "script:q", "replay").human(4).end(5).build()
    assert [
        (e.kind, at(timeline, e.start_ns), at(timeline, e.end_ns)) for e in timeline.episodes
    ] == [
        ("takeover", 1, 3),
        ("takeover", 4, 5),
    ]


def test_human_from_idle_or_planned_between_tasks_is_a_reset__defs_3() -> None:
    log = Log().human(1, "human_local").ai(2)
    log.human(3, "human_remote", "planned", plan_item_id="reset_scene").ai(4)
    timeline = log.human(5, "human_local").ai(6).end(7).build()
    assert [at(timeline, e.start_ns) for e in timeline.episodes] == [5]
    assert (seconds(timeline.reset_planned_ns), seconds(timeline.reset_unplanned_ns)) == (1, 2)


def test_planned_transition_inside_a_task_is_an_intervention__defs_3() -> None:
    log = Log().add(0, "task_start").ai(0)
    timeline = log.human(1, reason="planned", plan_item_id="reset_scene").ai(3).end(4).build()
    assert [e.reason for e in timeline.episodes] == ["planned"]
    assert not timeline.tasks[0].autonomous


def test_human_after_unknown_inside_a_task_is_human_time_only__defs_3() -> None:
    log = Log().add(0, "task_start").tr(0, "unknown").human(1).end(3)
    timeline = log.build()
    assert timeline.episodes == ()
    assert seconds(timeline.tasks[0].human_ns) == 2


# --- 6.1, 6.2: approvals, hints and dwell -------------------------------------------------------


def test_dwell_moves_only_its_overlap_with_ai_time__defs_6_1() -> None:
    log = Log().ai(0).human(4).ai(6).add(7, "help_request", scope="whole_robot")
    log.add(
        8, "approval", scope="whole_robot", actor_id="op_b", reason="ai_requested_help", dwell_s=5.0
    )
    timeline = log.end(10).build()
    (item,) = timeline.discrete
    # [3, 8): ai 3-4 and 6-8, human 4-6
    assert (seconds(item.human_ns), seconds(item.moved_ns), seconds(item.whole_robot_moved_ns)) == (
        5,
        3,
        3,
    )


def test_dwell_sensitivity_uses_another_minimum__defs_6_1() -> None:
    log = (
        Log()
        .ai(0)
        .add(2, "approval", scope="whole_robot", actor_id="op_b", reason="ai_requested_help")
    )
    log.add(9, "hint", scope="whole_robot", actor_id="op_b", dwell_s=1.0)
    timeline = log.end(10).build()
    assert dwell_shift(timeline, 0.0, 0.0).human_ns == 1 * NS
    shifted = dwell_shift(timeline, 5.0, 0.5)
    # approval at 2 s with a 5 s minimum overlaps ai only from 0 s
    assert (seconds(shifted.human_ns), seconds(shifted.whole_robot_moved_ns)) == (6, 3)
    assert shifted.moved_ns_by_scope == {"whole_robot": 3 * NS}
    assert shifted.scope_weighted_moved_ns == Fraction(3 * NS)


def test_dwell_on_an_undeclared_scope_moves_nothing_per_scope__defs_6_1() -> None:
    log = Log().ai(0).add(2, "hint", scope="ghost", actor_id="op_b", dwell_s=1.0)
    timeline = log.end(3).build()
    assert timeline.discrete[0].moved_ns == 0
    assert seconds(timeline.discrete[0].whole_robot_moved_ns) == 1
    assert dwell_shift(timeline, 0, 0).moved_ns_by_scope == {"whole_robot": 0}


def test_dwell_human_ns_takes_the_larger_of_dwell_and_minimum__defs_6_2() -> None:
    item = DiscreteIntervention("hint", "a", 0, 0, None, None, None, None, None, 0, 0, 0)
    assert dwell_human_ns(item, 7) == 7


def test_overlap_of_intervals() -> None:
    intervals = [(0, 2), (4, 6), (8, 10)]
    assert overlap_ns(intervals, 1, 9) == 4
    assert overlap_ns(intervals, 5, 5) == 0
    assert overlap_ns(intervals, 10, 12) == 0


# --- 6.3: resets --------------------------------------------------------------------------------


def test_unplanned_reset_counts_toward_the_next_task_only__defs_6_3() -> None:
    log = Log().human(1, "human_local").tr(3, "idle").add(4, "task_start").ai(4).add(5, "task_end")
    log.tr(5, "idle").add(6, "task_start", task="t2").ai(6).add(7, "task_end")
    log.human(8, "human_local")
    timeline = log.end(9).build()
    assert [seconds(t.unplanned_reset_ns) for t in timeline.tasks] == [2, 0]
    assert seconds(timeline.reset_unplanned_ns) == 3


# --- 6.4, 6.5: supervision and several humans ---------------------------------------------------


def test_supervision_ratio_and_hours__defs_6_4() -> None:
    log = Log().ai(0).add(2, "supervision", level="ready_to_takeover", n_supervisors=2)
    timeline = log.tr(6, "idle").end(8).build()
    supervision = timeline.supervision
    assert supervision.active_ns_by_level == {"none": 2 * NS, "ready_to_takeover": 4 * NS}
    assert supervision.ratio == pytest.approx(4 / 6)
    assert supervision.supervisor_hours_per_robot_hour == pytest.approx(8 / 6)


def test_supervision_without_active_time_has_no_ratio__defs_6_4() -> None:
    supervision = Log().end(5).build().supervision
    assert (supervision.ratio, supervision.supervisor_hours_per_robot_hour) == (None, None)


def test_two_humans_on_one_scope_count_actor_time_twice__defs_6_5() -> None:
    log = (
        Log()
        .human(0, actor="driver")
        .add(
            10,
            "approval",
            scope="whole_robot",
            actor_id="approver",
            reason="planned",
            plan_item_id="reset_scene",
            dwell_s=10.0,
        )
    )
    timeline = log.end(10).build()
    assert seconds(timeline.whole_robot.states["human_remote"]) == 10
    assert seconds(timeline.human_actor_ns) == 20
    assert timeline.distinct_human_actors == 2


# --- 6.6: several scopes -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "whole"),
    [
        (["shared", "human_local", "human_remote"], "human_remote"),
        (["shared", "human_local", "ai"], "human_local"),
        (["shared", "safety_stop", "ai"], "shared"),
        (["scripted", "safety_stop", "ai"], "safety_stop"),
        (["scripted", "unknown", "ai"], "scripted"),
        (["idle", "unknown", "ai"], "ai"),
        (["idle", "idle", "idle"], "idle"),
        (["idle", "unknown", "idle"], "unknown"),
    ],
)
def test_whole_robot_any_human_rule__defs_6_6(states: list[str], whole: str) -> None:
    log = Log(["a", "b", "c"], initial="unknown")
    for scope, state in zip("abc", states, strict=True):
        reason, device = _SOURCES[state]
        log.tr(
            0, state, reason, None if device in (None, "policy", "replay") else "op", device, scope
        )
    timeline = log.end(1).build()
    assert timeline.flags == ()
    assert [(s.state, s.start_ns) for s in timeline.whole_spans] == [(whole, timeline.start_ns)]


def test_scope_weights_are_normalised_and_sum_to_observed__defs_6_6() -> None:
    log = Log(["a", "b"], scope_weights={"a": 3, "b": 1}).ai(0, "a").ai(2, "b").end(4)
    timeline = log.build()
    assert timeline.weights == {"a": Fraction(3, 4), "b": Fraction(1, 4)}
    assert timeline.scope_weighted.states["ai"] == Fraction(7, 2) * NS
    assert timeline.scope_weighted.observed_ns == 4 * NS


def test_partial_weights_and_undeclared_scopes_get_equal_weights__defs_6_6() -> None:
    log = Log(["a", "b"], scope_weights={"a": 3, "b": 1}).ai(0, "a").ai(0, "b").ai(2, "c")
    timeline = log.end(4).build()
    assert timeline.scopes == ("a", "b", "c")
    assert set(timeline.weights.values()) == {Fraction(1, 3)}
    assert _states(timeline.per_scope["c"]) == {"unknown": 2, "ai": 2}


def test_simultaneous_takeovers_by_one_actor_count_once_for_the_whole_robot__defs_6_6() -> None:
    log = Log(["a", "b", "c", "d"]).add(0, "task_start")
    for scope in "abcd":
        log.ai(0, scope)
    log.human(1.0, scope="a").human(1.5, scope="b").human(1.6, scope="c", actor="op_b")
    log.human(1.7, scope="d")
    timeline = log.end(3).build()
    assert [e.intervention for e in timeline.episodes] == [0, 0, 1, 2]
    assert timeline.interventions[0].scopes == ("a", "b")
    assert (timeline.interventions_per_scope, timeline.interventions_whole_robot) == (4, 3)
    assert timeline.tasks[0].interventions_whole_robot == 3


# --- 6.7: safety stops ---------------------------------------------------------------------------


def test_stop_returning_to_ai_without_a_human_is_a_stop_only__defs_6_7() -> None:
    timeline = Log().ai(0).tr(1, "safety_stop", "safety").ai(2).human(3).end(4).build()
    (stop,) = timeline.safety_stops
    assert (stop.triggered_by, stop.takeover_seq) == ("automatic", None)
    assert [at(timeline, e.start_ns) for e in timeline.episodes] == [3]


def test_reasserted_stop_is_one_stop__defs_6_7() -> None:
    log = Log().ai(0).tr(1, "safety_stop", "safety").tr(2, "safety_stop", "safety", "op_a")
    assert len(log.end(3).build().safety_stops) == 1


def test_simultaneous_stops_count_once_for_the_whole_robot__defs_6_7() -> None:
    log = Log(["a", "b"]).ai(0, "a").ai(0, "b")
    log.tr(1, "safety_stop", "safety", scope="a").tr(1.2, "safety_stop", "safety", scope="b")
    log.tr(3, "safety_stop", "safety", "op_a", scope="a")
    timeline = log.end(4).build()
    assert len(timeline.safety_stops) == 2
    assert timeline.safety_stops_whole_robot == 1


def test_person_stop_from_unknown_is_not_an_intervention__defs_6_7() -> None:
    timeline = Log(initial="unknown").tr(1, "safety_stop", "safety", "op_a").end(2).build()
    assert timeline.episodes == ()
    assert timeline.safety_stops[0].triggered_by == "human"


def test_takeover_window_ends_at_task_end__defs_6_7() -> None:
    log = Log().add(0, "task_start").ai(0).tr(1, "safety_stop", "safety").add(2, "task_end")
    timeline = log.human(3).end(4).build()
    assert timeline.safety_stops[0].takeover_seq is None
    assert timeline.episodes == ()


# --- 6.8: holds ----------------------------------------------------------------------------------


def test_idle_in_open_task_is_unknown__defs_6_8() -> None:
    log = Log().add(1, "task_start").tr(1, "idle").add(3, "heartbeat").ai(5).add(6, "task_end")
    timeline = log.end(8).build()
    assert _states(timeline.whole_robot) == {"idle": 1, "unknown": 4, "ai": 3}
    assert codes(timeline) == [(2, "idle_in_task")]
    assert timeline.tasks[0].contains_unknown


def test_idle_scope_when_a_task_opens_is_flagged_once__defs_6_8() -> None:
    log = Log(["a", "b"]).ai(0, "b").add(2, "task_start").add(3, "heartbeat").ai(4, "a")
    timeline = log.add(5, "task_end").end(6).build()
    assert codes(timeline) == [(2, "idle_in_task")]
    assert _states(timeline.per_scope["a"]) == {"idle": 2, "unknown": 2, "ai": 2}


# --- 6.10: device registry -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("logged", "device", "state", "flag"),
    [
        ("ai", "joy_local", "human_local", "registry_mismatch"),
        ("shared", "joy_local", "shared", None),
        ("human_local", "vr_remote", "human_remote", "registry_mismatch"),
        ("human_remote", "policy", "ai", "registry_mismatch"),
        ("ai", "replay", "scripted", "registry_mismatch"),
        ("scripted", "replay", "scripted", None),
        ("ai", "hoist", "human_remote", "registry_mismatch"),
        ("human_remote", "hoist", "human_remote", "registry_mismatch"),
        ("ai", "unknown_device", "human_remote", "undeclared_device"),
        ("ai", None, "human_remote", "undeclared_device"),
    ],
)
def test_registry_decides_the_state__defs_6_10(
    logged: str, device: str | None, state: str, flag: str | None
) -> None:
    reason = None if logged in ("ai", "scripted") else "human_initiated"
    timeline = Log().tr(1, logged, reason, "x", device).end(2).build()
    assert timeline.spans[-1].state == state
    assert [c for _, c in codes(timeline)] == ([] if flag is None else [flag])


@pytest.mark.parametrize("state", ["safety_stop", "idle", "unknown"])
def test_stops_idle_and_unknown_are_not_checked__defs_6_10(state: str) -> None:
    reason = "safety" if state == "safety_stop" else None
    timeline = Log().tr(1, state, reason, None, "hoist").end(2).build()
    assert timeline.spans[-1].state == state
    assert timeline.flags == ()


def test_missing_device_registry_makes_every_source_undeclared__defs_8() -> None:
    timeline = Log().ai(1).end(2).build(Registries.from_documents(None, TASKS, PLAN))
    assert codes(timeline) == [(0, "registry_missing"), (1, "undeclared_device")]
    assert "with no device registry" in timeline.flags[1].detail
    assert _states(timeline.whole_robot) == {"idle": 1, "human_remote": 1}


def test_registries_report_their_status__defs_8() -> None:
    assert dict(Registries().status()) == dict.fromkeys(("devices", "tasks", "plan"), "missing")
    assert dict(FULL.status()) == dict.fromkeys(("devices", "tasks", "plan"), "matched")


# --- Section 5: tasks -----------------------------------------------------------------------------


def test_task_definition_not_in_the_registry_is_flagged__defs_5() -> None:
    log = Log().ai(0).add(1, "task_start", task_definition_id="other").add(2, "task_end").end(3)
    timeline = log.build()
    assert codes(timeline) == [(2, "unknown_task_definition")]
    assert not timeline.tasks[0].definition_known
    missing = log.build(Registries.from_documents(DEVICES, None, PLAN))
    assert "no task registry" in missing.flags[0].detail


@pytest.mark.parametrize(
    "intervene",
    [
        lambda log: log.tr(2, "safety_stop", "safety", "op_a").ai(3),
        lambda log: log.tr(2, "scripted", "human_initiated", "script:p", "replay").ai(3),
        lambda log: log.add(2, "hint", scope="whole_robot", actor_id="op_a"),
    ],
    ids=["person_stop", "preset", "hint"],
)
def test_any_intervention_makes_a_task_not_autonomous__defs_5(intervene: object) -> None:
    log = Log().add(1, "task_start").ai(1)
    intervene(log)  # type: ignore[operator]
    timeline = log.add(4, "task_end").end(5).build()
    assert not timeline.tasks[0].autonomous
    clean = Log().add(1, "task_start").ai(1).tr(2, "safety_stop", "safety").ai(3)
    assert clean.add(4, "task_end").end(5).build().tasks[0].autonomous


def test_task_start_while_a_task_is_open_closes_it_unknown__defs_5() -> None:
    log = Log().add(1, "task_start").add(2, "task_start", task="t2").add(3, "task_end")
    timeline = log.end(4).build()
    assert [(t.task_id, t.outcome, t.closed, t.end_seq) for t in timeline.tasks] == [
        ("t1", "unknown", False, None),
        ("t2", "success", True, 3),
    ]


def test_task_end_of_another_task_is_ignored__defs_5() -> None:
    log = Log().add(1, "task_start").add(2, "task_end", task_id="other").end(4)
    (task,) = log.build().tasks
    assert (task.closed, seconds(task.end_ns - task.start_ns)) == (False, 3)


# --- 6.15, 8: support and distance ----------------------------------------------------------------


def test_supported_time_is_the_union_of_supports__defs_6_15() -> None:
    log = Log().add(1, "support_start", device_id="hoist").add(2, "support_start", scope="a")
    log.add(3, "support_end").add(4, "support_end", scope="nothing").end(6)
    assert seconds(log.build().supported_ns) == 5


def test_distance_is_the_largest_heartbeat_distance__defs_8() -> None:
    log = Log().add(1, "heartbeat", distance_m=3.0).add(2, "heartbeat", distance_m=1.0)
    assert log.end(3).build().distance_m == 3.0
    assert Log().end(1).build().distance_m is None


# --- flags ---------------------------------------------------------------------------------------


def test_flags_from_the_reader_and_timeline_are_sorted() -> None:
    log = Log().tr(2, "ai").end(3)
    reader_flag = Flag("r01", "s01", 0, "clock_unsynced", "no heartbeat")
    timeline = log.build(flags=[reader_flag])
    assert codes(timeline) == [(0, "clock_unsynced"), (1, "undeclared_device")]
