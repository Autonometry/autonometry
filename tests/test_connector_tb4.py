"""TurtleBot 4 engine, one test per rule: each name cites the spec section (definitions.md v0.3)
and the row of the integration notes' table B.2 (research/turtlebot4) it implements."""

from dataclasses import replace

import pytest

from autonometry.connectors import tb4
from tb4_helpers import (
    BUTTON_2,
    DEVICES,
    GOAL,
    GOAL_2,
    JOY,
    KBD,
    MISSION,
    NAV2,
    POWER,
    ROOT,
    ROUTINE_GOAL,
    RVIZ,
    STRANGER,
    TB4_NODE,
    cmd,
    engine,
    last_state,
    nav,
    nav2_cmd,
    settings,
)


def started(**options: object) -> tb4.Tb4Engine:
    """An engine that has seen the docked robot since t = 0, with no task and no command."""
    e = engine(**options)
    e.observe(tb4.DockStatus(0.0, True))
    e.observe(tb4.WheelStatus(0.0, True))
    return e


def state(e: tb4.Tb4Engine, t: float) -> tb4.StateAssertion:
    return last_state(e.tick(t))


def fields(value: tb4.StateAssertion | tb4.Hint) -> dict[str, object]:
    return dict(value.fields)


def on_task(e: tb4.Tb4Engine, t: float = 1.0) -> list[tb4.Message]:
    return e.observe(nav(t, tb4.EXECUTING))


# -- Nav2 goals and tasks ---------------------------------------------------------------------


def test_goal_executing_after_idle_starts_a_task_in_ai__defs_5_notes_b2_uc8a() -> None:
    e = started()
    out = on_task(e)
    assert isinstance(out[0], tb4.TaskStart)
    assert (out[0].task_id, out[0].task_definition_id) == (f"nav-{GOAL}", "nav_to_pose")
    value = last_state(out)
    assert (value.state, value.actor_id, value.device_id) == ("ai", "ai:nav2", "nav2")
    assert out.index(value) > 0  # the transition follows task_start, so it carries the task id


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(tb4.SUCCEEDED, "success"), (tb4.ABORTED, "failure"), (tb4.CANCELED, "aborted")],
)
def test_terminal_status_ends_the_task__defs_5_notes_b2_nav2_14d(status: int, outcome: str) -> None:
    e = started()
    on_task(e)
    out = e.observe(nav(5.0, status, requesters=(MISSION,)))
    ends = [m for m in out if isinstance(m, tb4.TaskEnd)]
    assert [(m.task_id, m.outcome) for m in ends] == [(f"nav-{GOAL}", outcome)]


def test_a_goal_first_seen_finished_opens_no_task__notes_b2_uc16() -> None:
    e = started()
    assert not [m for m in e.observe(nav(1.0, tb4.SUCCEEDED)) if isinstance(m, tb4.TaskStart)]


def test_regoal_from_a_person_is_shared_until_nav2_follows_it__defs_6_2_notes_b2_uc8b() -> None:
    e = started()
    on_task(e)
    goals = (tb4.GoalStatus(GOAL, tb4.ABORTED), tb4.GoalStatus(GOAL_2, tb4.ACCEPTED))
    out = e.observe(tb4.ActionStatus(5.0, tb4.NAVIGATE, goals, (RVIZ,)))
    assert not [m for m in out if isinstance(m, tb4.TaskEnd)]  # a preemption, not an end
    hint = last_state(out)
    assert (hint.state, hint.reason, hint.actor_id) == ("shared", "human_initiated", "op_01")
    assert fields(hint) == {
        "x_hint": "regoal",
        "x_trigger": "/navigate_to_pose",
        "x_attribution_inferred": True,
    }
    assert not [m for m in out if isinstance(m, tb4.Hint)]
    e.observe(nav(5.2, tb4.EXECUTING, GOAL_2))
    for o in nav2_cmd(5.4, 0.2):
        e.observe(o)
    assert state(e, 5.5).state == "ai"


def test_regoal_hint_lasts_at_most_hint_ttl__defs_6_2() -> None:
    e = started()
    on_task(e)
    e.observe(nav(5.0, tb4.EXECUTING, GOAL_2))
    assert state(e, 64.9).state == "shared"
    assert state(e, 65.0).state == "ai"


def test_regoal_under_hint_as_event_is_a_hint_event__defs_6_2_notes_b2_uc8b() -> None:
    e = started(hint_policy="hint_as_event")
    on_task(e)
    out = e.observe(nav(5.0, tb4.EXECUTING, GOAL_2))
    hints = [m for m in out if isinstance(m, tb4.Hint)]
    assert [(h.actor_id, h.device_id, fields(h)["x_kind"]) for h in hints] == [
        ("op_01", "rviz_pc_1", "regoal")
    ]
    assert state(e, 5.1).state == "ai"


def test_regoal_from_a_mission_node_is_not_a_hint__defs_6_2() -> None:
    e = started()
    on_task(e)
    out = e.observe(nav(5.0, tb4.EXECUTING, GOAL_2, requesters=(MISSION,)))
    assert not [m for m in out if isinstance(m, tb4.Hint)]
    assert state(e, 5.1).state == "ai"


def test_regoal_with_several_clients_counts_as_an_undeclared_person__defs_6_10() -> None:
    e = started()
    on_task(e)
    out = e.observe(nav(5.0, tb4.EXECUTING, GOAL_2, requesters=(RVIZ, MISSION)))
    value = last_state(out)
    assert (value.state, value.device_id, value.actor_id) == (
        "human_remote",
        None,
        "person:unidentified",
    )


def test_cancel_is_shared_while_open_then_task_end_aborted__defs_6_8_notes_b2_uc8c() -> None:
    e = started()
    on_task(e)
    out = e.observe(nav(5.0, tb4.CANCELING))
    value = last_state(out)
    assert (value.state, fields(value)["x_hint"]) == ("shared", "cancel")
    assert state(e, 9.0).state == "shared"
    out = e.observe(nav(9.5, tb4.CANCELED))
    assert [(m.outcome) for m in out if isinstance(m, tb4.TaskEnd)] == ["aborted"]


def test_cancel_seen_only_as_canceled_still_gives_its_hint_inside_the_task__notes_b2_uc8c() -> None:
    e = started()
    on_task(e)
    out = e.observe(nav(5.0, tb4.CANCELED))
    kinds = [type(m).__name__ for m in out]
    assert kinds == ["StateAssertion", "TaskEnd"]
    assert out[0].state == "shared"  # type: ignore[union-attr]


def test_cancel_under_hint_as_event_sends_the_hold_then_the_hint__defs_3_6_2() -> None:
    e = started(hint_policy="hint_as_event")
    on_task(e)
    out = e.observe(nav(5.0, tb4.CANCELING))
    assert [type(m).__name__ for m in out] == ["StateAssertion", "Hint"]
    assert out[0].state == "shared"  # type: ignore[union-attr]


def test_cancel_by_the_mission_node_is_not_a_hint__defs_6_2() -> None:
    e = started()
    on_task(e)
    out = e.observe(nav(5.0, tb4.CANCELING, requesters=(MISSION,)))
    assert not [m for m in out if isinstance(m, (tb4.Hint, tb4.StateAssertion))]


# -- /cmd_vel -----------------------------------------------------------------------------------


def test_keyboard_alone_is_human_remote_by_the_registry__defs_6_10_notes_b2() -> None:
    e = started()
    value = last_state(e.observe(cmd(1.0, 0.2, KBD)))
    assert (value.state, value.reason, value.actor_id, value.device_id) == (
        "human_remote",
        "human_initiated",
        "op_01",
        "teleop_kbd_pc_1",
    )
    assert "x_attribution_inferred" not in fields(value)


def test_joystick_alone_is_human_local__defs_6_10_notes_b2() -> None:
    e = started()
    value = last_state(e.observe(cmd(1.0, 0.2, JOY)))
    assert (value.state, value.device_id) == ("human_local", "tb4_controller")


def test_an_undeclared_publisher_is_human_remote__defs_6_10_notes_b2() -> None:
    e = started()
    value = last_state(e.observe(cmd(1.0, 0.2, STRANGER)))
    assert (value.state, value.device_id, value.actor_id) == (
        "human_remote",
        None,
        "person:unidentified",
    )


def test_without_a_registry_nav2_counts_as_a_remote_person__defs_6_10() -> None:
    e = started(devices=())
    assert last_state(e.observe(cmd(1.0, 0.2, NAV2))).state == "human_remote"


def test_collision_monitor_commands_outside_a_task_are_ai__defs_6_10_notes_b2() -> None:
    e = started()
    value = last_state(e.observe(cmd(1.0, 0.2, NAV2)))
    assert (value.state, value.actor_id, value.device_id) == ("ai", "ai:nav2", "nav2")


def test_a_script_device_commanding_is_scripted__defs_6_9_6_10() -> None:
    script = tb4.Device("replay", "script", (("node", "/replay"),))
    e = started(devices=(script, *DEVICES))
    value = last_state(e.observe(cmd(1.0, 0.2, tb4.Source(node="/replay"))))
    assert (value.state, value.actor_id) == ("scripted", "script:replay")


def test_person_and_nav2_within_contention_window_are_shared__defs_6_12_notes_b2_uc1() -> None:
    e = started()
    on_task(e)
    for o in nav2_cmd(2.0, 0.25):
        e.observe(o)
    assert not [m for m in e.observe(cmd(2.5, 0.1, wz=0.4)) if isinstance(m, tb4.StateAssertion)]
    value = state(e, 2.8)  # it waited smoothed_match_s for a relay match
    assert (value.state, value.reason, value.device_id) == (
        "shared",
        "human_initiated",
        "teleop_kbd_pc_1",
    )
    assert fields(value)["x_contention"] is True
    assert fields(value)["x_attribution_inferred"] is True


def test_two_people_at_once_are_shared__defs_6_12() -> None:
    e = started()
    e.observe(cmd(1.0, 0.2, KBD))
    assert last_state(e.observe(cmd(1.2, 0.1, JOY))).state == "shared"


def test_commands_that_relay_cmd_vel_smoothed_are_nav2s__defs_6_12_notes_a() -> None:
    e = started()
    for o in nav2_cmd(1.0, 0.25):
        out = e.observe(o)
    value = last_state(out)
    assert (value.state, value.device_id) == ("ai", "nav2")
    assert fields(value)["x_attribution_inferred"] is True


def test_a_scaled_relay_is_still_nav2s__defs_6_12_notes_uc5() -> None:
    e = started()
    for o in nav2_cmd(1.0, 0.3, relayed=0.15):
        out = e.observe(o)
    assert last_state(out).state == "ai"


def test_a_stale_smoothed_command_does_not_match__defs_6_12() -> None:
    e = started()
    e.observe(tb4.CmdVelSmoothed(1.0, 0.25, 0.0, 0.0))
    e.observe(cmd(1.5, 0.25))
    assert state(e, 1.8).state == "human_remote"


def test_a_relay_arriving_before_its_smoothed_command_is_still_nav2s__defs_6_12() -> None:
    e = started()
    on_task(e)
    e.observe(cmd(2.0, 0.25))
    e.observe(tb4.CmdVelSmoothed(2.05, 0.25, 0.0, 0.0))
    value = state(e, 2.5)
    assert (value.state, value.device_id) == ("ai", "nav2")
    assert not [c for c in e._commanders.values() if c.device and c.device.kind == "human"]


def test_dead_man_release_ends_joystick_control__defs_6_11_notes_b2_uc14() -> None:
    e = started()
    on_task(e)
    e.observe(tb4.Joy(2.0, (0, 0, 0, 0, 1, 0)))
    assert last_state(e.observe(cmd(2.0, 0.2, JOY))).state == "human_local"
    assert last_state(e.observe(tb4.Joy(2.3, (0, 0, 0, 0, 0, 0)))).state == "ai"


def test_a_neutral_command_ends_human_control_and_never_opens_it__defs_6_11_notes_uc14() -> None:
    e = started()
    on_task(e)
    e.observe(cmd(2.0, 0.2, KBD))
    assert last_state(e.observe(cmd(2.3, 0.0, KBD))).state == "ai"
    assert not [m for m in e.observe(cmd(3.0, 0.0, KBD)) if isinstance(m, tb4.StateAssertion)]


def test_human_control_lasts_goal_release_after_the_last_command__defs_6_11() -> None:
    e = started()
    on_task(e)
    e.observe(cmd(2.0, 0.2, KBD))
    assert state(e, 2.9).state == "human_remote"
    assert state(e, 3.0).state == "ai"


# -- safety stops -------------------------------------------------------------------------------


def test_collision_monitor_stop_is_an_automatic_safety_stop__defs_6_7_notes_b2_n9() -> None:
    e = started()
    on_task(e)
    value = last_state(e.observe(tb4.CollisionMonitorState(2.0, tb4.COLLISION_STOP)))
    assert (value.state, value.reason, value.actor_id) == ("safety_stop", "safety", None)
    assert last_state(e.observe(tb4.CollisionMonitorState(3.0, 0))).state == "ai"


def test_collision_stop_outside_a_task_changes_nothing__notes_b2_n9() -> None:
    e = started()
    e.observe(tb4.CollisionMonitorState(31.0, tb4.COLLISION_STOP))
    assert state(e, 31.0).state == "idle"


def test_a_person_driving_during_a_collision_stop_takes_over__defs_6_7() -> None:
    e = started()
    on_task(e)
    e.observe(tb4.CollisionMonitorState(2.0, tb4.COLLISION_STOP))
    for o in nav2_cmd(2.5, 0.25, relayed=0.0):
        e.observe(o)
    e.observe(cmd(3.0, -0.15))
    assert state(e, 3.3).state == "human_remote"


def test_estop_button_is_a_safety_stop_a_person_triggered__defs_6_7_notes_b2_uc7() -> None:
    e = started()
    on_task(e)
    e.observe(tb4.ButtonEdge(2.0, POWER, True))
    e.observe(tb4.ButtonEdge(2.1, POWER, False))
    e.observe(tb4.FunctionCall(2.2, "EStop"))
    value = last_state(e.observe(tb4.WheelStatus(2.25, False)))
    assert (value.state, value.reason, value.actor_id, value.device_id) == (
        "safety_stop",
        "safety",
        "op_02",
        "create3_button_power",
    )
    assert fields(value) == {"x_trigger": "/function_calls:EStop", "x_trigger_kind": "human_button"}


def test_wheels_disabled_without_a_button_is_a_stop_by_an_unidentified_person__defs_6_7() -> None:
    e = started()
    value = last_state(e.observe(tb4.WheelStatus(1.0, False)))
    assert (value.state, value.actor_id, value.device_id) == (
        "safety_stop",
        "person:unidentified",
        None,
    )
    assert fields(value) == {"x_trigger": "/wheel_status"}


def test_wheels_enabled_again_gives_the_next_commander_or_unknown__notes_b2_uc7() -> None:
    e = started()
    e.observe(tb4.WheelStatus(1.0, False))
    value = last_state(e.observe(tb4.WheelStatus(5.0, True)))
    assert (value.state, fields(value)) == ("unknown", {"x_cause": "stop_released"})
    assert last_state(e.observe(cmd(6.0, 0.2, KBD))).state == "human_remote"


def test_wheels_enabled_again_during_a_task_returns_to_nav2__notes_b2_uc7() -> None:
    e = started()
    on_task(e)
    e.observe(tb4.WheelStatus(2.0, False))
    assert last_state(e.observe(tb4.WheelStatus(3.0, True))).state == "ai"


def test_estop_toggles_on_function_calls_when_wheel_status_is_not_seen__notes_b2_uc7() -> None:
    e = engine()
    e.observe(tb4.DockStatus(0.0, True))
    assert last_state(e.observe(tb4.FunctionCall(1.0, "EStop"))).state == "safety_stop"
    assert last_state(e.observe(tb4.FunctionCall(2.0, "EStop"))).state == "unknown"


# -- dock, undock, wall follow ------------------------------------------------------------------


def routine(
    t: float, status: int, action: str = "undock", *requesters: tb4.Source
) -> tb4.ActionStatus:
    return tb4.ActionStatus(
        t, action, (tb4.GoalStatus(ROUTINE_GOAL, status),), requesters or (TB4_NODE,)
    )


def test_undock_by_a_button_is_shared_human_initiated__defs_6_13_notes_b2_uc3() -> None:
    e = started()
    e.observe(tb4.ButtonEdge(1.0, BUTTON_2, True))
    e.observe(tb4.ButtonEdge(1.1, BUTTON_2, False))
    e.observe(tb4.FunctionCall(1.2, "Undock"))
    value = last_state(e.observe(routine(1.3, tb4.EXECUTING)))
    assert (value.state, value.reason, value.actor_id, value.device_id) == (
        "shared",
        "human_initiated",
        "op_02",
        "create3_button_2",
    )
    assert fields(value) == {"x_trigger": "/undock/_action/status:EXECUTING", "x_routine": "undock"}
    assert state(e, 5.0).state == "shared"


def test_routine_started_by_the_mission_node_is_ai__defs_6_13_notes_b2_uc3() -> None:
    e = started()
    value = last_state(e.observe(routine(1.0, tb4.EXECUTING, "dock", MISSION, TB4_NODE)))
    assert (value.state, value.actor_id, value.device_id) == (
        "ai",
        "ai:mission_navigator",
        "mission_navigator",
    )
    assert fields(value)["x_attribution_inferred"] is True


def test_routine_started_by_a_script_is_scripted__defs_6_13() -> None:
    script = tb4.Device("macro", "script", (("node", "/macro"),))
    e = started(devices=(script, *DEVICES))
    value = last_state(
        e.observe(routine(1.0, tb4.EXECUTING, "wall_follow", tb4.Source(node="/macro")))
    )
    assert value.state == "scripted"


def test_routine_via_turtlebot4_node_without_a_seen_call_is_undeclared__defs_6_10() -> None:
    e = started()
    value = last_state(e.observe(routine(1.0, tb4.EXECUTING)))
    assert (value.state, value.reason, value.device_id) == ("human_remote", "human_initiated", None)


def test_routine_by_a_person_holds_until_idle_after_it_ends__defs_6_14() -> None:
    e = started()
    e.observe(tb4.FunctionCall(1.0, "Wall Follow Left"))
    e.observe(routine(1.1, tb4.EXECUTING, "wall_follow"))
    e.observe(routine(11.0, tb4.SUCCEEDED, "wall_follow"))
    assert state(e, 40.9).state == "human_remote"  # no button seen: an undeclared person
    assert state(e, 41.0).state == "idle"


def test_a_function_call_starts_only_one_routine__notes_b2_uc3() -> None:
    e = started()
    e.observe(tb4.ButtonEdge(1.0, BUTTON_2, True))
    e.observe(tb4.FunctionCall(1.1, "Dock"))
    e.observe(routine(1.2, tb4.EXECUTING, "dock"))
    e.observe(routine(2.0, tb4.SUCCEEDED, "dock"))
    second = tb4.ActionStatus(2.5, "dock", (tb4.GoalStatus(GOAL, tb4.EXECUTING),), (MISSION,))
    assert last_state(e.observe(second)).state == "ai"


# -- pose estimates, kidnap, idle, motion -------------------------------------------------------


def test_initial_pose_before_a_task_is_planned_human_remote__defs_6_3_notes_b2_uc8e() -> None:
    e = started()
    value = last_state(e.observe(tb4.InitialPose(1.0, RVIZ)))
    assert (value.state, value.reason, value.device_id) == ("human_remote", "planned", "rviz_pc_1")
    assert fields(value)["plan_item_id"] == "initial_pose"
    assert last_state(on_task(e, 2.0)).state == "ai"


def test_initial_pose_from_the_mission_node_is_ignored__notes_uc8d() -> None:
    e = started()
    e.observe(tb4.InitialPose(1.0, None, (MISSION,)))
    assert state(e, 30.0).state == "idle"


def test_initial_pose_during_a_task_is_a_hint__defs_6_2_notes_b2_uc8d() -> None:
    e = started()
    on_task(e)
    value = last_state(e.observe(tb4.InitialPose(2.0, RVIZ)))
    assert (value.state, fields(value)["x_hint"]) == ("shared", "initial_pose")


def test_kidnapped_for_a_second_is_human_local__defs_6_15_notes_b2_uc20() -> None:
    e = started()
    e.observe(tb4.KidnapStatus(1.0, True))
    assert state(e, 1.9).state == "unknown"
    value = state(e, 2.0)
    assert (value.state, value.reason, value.device_id) == (
        "human_local",
        "human_initiated",
        "manual_handling",
    )
    e.observe(tb4.KidnapStatus(5.0, False))
    assert state(e, 34.9).state == "human_local"
    assert state(e, 35.0).state == "idle"


def test_a_short_kidnap_changes_nothing__notes_uc20() -> None:
    e = started()
    e.observe(tb4.KidnapStatus(1.0, True))
    e.observe(tb4.KidnapStatus(1.5, False))
    assert state(e, 30.0).state == "idle"


def test_idle_after_the_threshold_with_no_task_or_command_even_undocked__defs_2() -> None:
    e = started()
    e.observe(tb4.DockStatus(0.0, False))
    assert state(e, 29.9) == tb4.StateAssertion(
        "base", "unknown", fields=(("x_cause", "not_yet_observed"),)
    )
    assert state(e, 30.0) == tb4.StateAssertion("base", "idle", fields=(("x_docked", False),))


def test_the_last_controller_holds_outside_a_task_until_idle__defs_6_14() -> None:
    e = started()
    on_task(e)
    e.observe(nav(5.0, tb4.SUCCEEDED))
    assert state(e, 34.9).state == "ai"
    assert state(e, 35.0).state == "idle"
    e.observe(tb4.FunctionCall(36.0, "Undock"))  # touches the idle clock, commands nothing yet
    assert state(e, 36.1).state == "idle"


def test_motion_without_a_command_is_unknown_out_of_band__notes_b6() -> None:
    e = started()
    e.observe(tb4.Odometry(31.0, 0.0, 0.0, 0.0, 0.0))
    value = last_state(e.observe(tb4.Odometry(31.5, 0.1, 0.0, 0.2, 0.0)))
    assert (value.state, fields(value)) == ("unknown", {"x_out_of_band": True})
    assert state(e, 34.0).state == "idle"  # the odometry went stale


def test_motion_after_a_command_is_not_out_of_band__notes_b6() -> None:
    e = started()
    e.observe(cmd(1.0, 0.2, KBD))
    e.observe(tb4.Odometry(2.5, 0.2, 0.0, 0.2, 0.0))
    assert state(e, 2.5).state == "human_remote"
    assert state(e, 3.1).state == "unknown"  # still moving 2.1 s after the last command


def test_distance_is_the_odometry_path_without_pose_resets__defs_8() -> None:
    e = started()
    for t, x, y in [(1.0, 0.0, 0.0), (2.0, 0.3, 0.4), (3.0, 5.0, 5.0), (4.0, 5.0, 5.5)]:
        e.observe(tb4.Odometry(t, x, y, 0.0, 0.0))
    distances = [m for m in e.tick(4.0) if isinstance(m, tb4.Distance)]
    assert distances == [tb4.Distance(pytest.approx(1.0))]


def test_tick_always_asserts_the_state__readme_level_triggered() -> None:
    e = started()
    assert [type(m).__name__ for m in e.tick(1.0)] == ["StateAssertion"]
    assert [type(m).__name__ for m in e.tick(1.25)] == ["StateAssertion"]
    assert not e.observe(tb4.DockStatus(1.3, True))


# -- settings and registry ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"hint_policy": "hint_as_magic"},
        {"gap_tolerance_s": 0.0},
        {"trigger_window_s": float("nan")},
    ],
)
def test_settings_reject_bad_values(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must be"):
        replace(settings(), **change)


def test_devices_match_on_every_given_field_and_the_first_match_wins__defs_6_10() -> None:
    registry = {
        "devices": [
            {"device_id": "a", "kind": "ai", "match": {"node": "/n", "topic": "/cmd_vel"}},
            {"device_id": "b", "kind": "human", "location": "local", "match": {"node": "/n"}},
            {"device_id": "c", "kind": "human", "location": "remote", "match": {"node": "/n"}},
        ]
    }
    devices = tb4.devices_from_registry(registry)
    assert devices[0].match == (("node", "/n"), ("topic", "/cmd_vel"))
    e = started(devices=devices)
    value = last_state(e.observe(cmd(1.0, 0.2, tb4.Source(node="/n"))))
    assert value.device_id == "b"


def test_wire_events_carry_the_fields_the_recorder_expects__readme_wire_format() -> None:
    value = tb4.StateAssertion("base", "ai", None, "ai:nav2", "nav2", (("x_trigger", "t"),))
    assert tb4.wire_event(value) == {
        "type": "transition",
        "scope": "base",
        "state": "ai",
        "reason": None,
        "actor_id": "ai:nav2",
        "device_id": "nav2",
        "x_trigger": "t",
    }
    assert tb4.wire_event(tb4.TaskStart("t1", "nav_to_pose")) == {
        "type": "task_start",
        "task_id": "t1",
        "task_definition_id": "nav_to_pose",
    }
    assert tb4.wire_event(tb4.TaskEnd("t1", "success")) == {
        "type": "task_end",
        "task_id": "t1",
        "outcome": "success",
    }
    assert tb4.wire_event(tb4.Hint("base", "op_01", None, (("x_kind", "cancel"),))) == {
        "type": "hint",
        "scope": "base",
        "actor_id": "op_01",
        "x_kind": "cancel",
    }


def test_the_ros_package_vendors_the_recorder_client_unchanged__readme_wire_format() -> None:
    vendored = ROOT / "connectors" / "ros2" / "autonometry_ros" / "autonometry_ros"
    original = ROOT / "clients" / "python" / "autonometry_client.py"
    assert (vendored / "autonometry_client.py").read_bytes() == original.read_bytes()


def test_an_estop_call_arriving_after_the_wheels_still_names_the_button__notes_b2_uc7() -> None:
    e = started()
    e.observe(tb4.ButtonEdge(2.0, POWER, True))
    assert last_state(e.observe(tb4.WheelStatus(2.1, False))).actor_id == "person:unidentified"
    value = last_state(e.observe(tb4.FunctionCall(2.15, "EStop")))
    assert (value.state, value.actor_id, value.device_id) == (
        "safety_stop",
        "op_02",
        "create3_button_power",
    )


def test_menu_and_power_calls_are_not_control__notes_b2_uc15() -> None:
    e = started()
    for name in ("Select", "Scroll Up", "Power", "x" * 64):
        e.observe(tb4.FunctionCall(1.0, name))
    assert state(e, 30.0).state == "idle"


def test_silent_publishers_are_forgotten__readme_limits() -> None:
    e = started()
    for i in range(50):
        e.observe(cmd(1.0 + i * 0.01, 0.0, tb4.Source(node=f"/_ros2cli_{i}")))
    e.tick(40.0)
    assert not e._commanders  # the bound is internal
