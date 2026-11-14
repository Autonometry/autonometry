"""Unitree G1 engine (``autonometry.connectors.g1``): one test per rule of the integration notes
(research/unitree-g1, section 6), each citing the spec section it implements."""

import math
import struct

import pytest

from autonometry.connectors import g1
from autonometry.connectors.tb4 import Device, Source
from g1_helpers import (
    ARM_SERVICE,
    DEVICES,
    LOWSTATE,
    POLICY_ARM,
    POLICY_ARM_REQ,
    POLICY_LOWCMD,
    POLICY_SPORT,
    REPLAY_LOWCMD,
    SPORT,
    STRANGER_LOWCMD,
    STRANGER_SPORT,
    XR_ARM,
    XR_DEX3,
    XR_LOWCMD,
    XR_SPORT,
    XR_SWITCH,
    Sim,
    cause,
    command,
    engine,
    remote_bytes,
    request,
    seen,
    settings,
    velocity,
)

GAP = 2.0
IDLE = 30.0


def released(sim: Sim) -> None:
    """ReleaseMode from xr_teleoperate (S8): the built-in program stops commanding."""
    sim.step(request(0, XR_SWITCH, "motion_switcher", 1003, {}, request_id=7))


# -- rule 1 -------------------------------------------------------------------------------------


def test_no_lowstate_yet_is_unknown__defs_2_6_14() -> None:
    eng = engine()
    messages = eng.tick(0.0)
    assert [m.state for m in messages if isinstance(m, g1.StateAssertion)] == ["unknown"] * 2
    assert all(cause(m) == "robot_lost" for m in messages if isinstance(m, g1.StateAssertion))


def test_robot_lost_makes_every_scope_unknown_after_gap_tolerance__defs_2_6_14() -> None:
    sim = Sim(hands=True)
    sim.run(1.0)
    assert sim.state().state == "ai"
    sim.robot = False
    sim.run(2.9)
    assert sim.state().state == "ai"
    sim.run(3.2)
    for scope in (g1.LEGS, g1.UPPER_BODY, g1.HANDS):
        assert sim.state(scope).state == "unknown"
        assert cause(sim.state(scope)) == "robot_lost"
    sim.robot = True
    sim.run(3.5)
    assert sim.state().state == "ai"


# -- rule 2 -------------------------------------------------------------------------------------


def test_remote_l2_b_then_passive_is_a_stop_the_person_triggered__defs_6_7_6_8() -> None:
    sim = Sim()
    sim.run(1.0)
    sim.remote = remote_bytes("L2", "B")
    sim.run(1.2)
    assert sim.state().state == "ai"  # no passive state yet
    sim.fsm = (1, 0, 0)
    sim.run(1.4)
    for scope in (g1.LEGS, g1.UPPER_BODY):
        stop = sim.state(scope)
        assert (stop.state, stop.reason, stop.actor_id, stop.device_id) == (
            "safety_stop",
            "safety",
            "op_01",
            "g1_remote",
        )
        assert cause(stop) == "remote:L2+B"
    sim.remote = remote_bytes()
    sim.run(5.0)
    assert sim.state().state == "safety_stop"  # held until the state leaves passive
    sim.fsm = (500, 0, 0)
    sim.run(5.2)
    assert sim.state().state == "ai"


def test_l2_b_not_followed_by_passive_within_gap_tolerance_is_no_stop__defs_6_8() -> None:
    sim = Sim()
    sim.run(1.0)
    sim.remote = remote_bytes("L2", "B")
    sim.run(1.1)
    sim.remote = remote_bytes()
    sim.run(3.5)
    sim.fsm = (1, 0, 0)
    sim.run(4.0)
    assert "safety_stop" not in sim.states()
    assert sim.state().state == "unknown"  # passive with no observed cause, inside a task (6.8)


def test_damp_request_from_a_person_is_a_stop_and_names_them__defs_6_7() -> None:
    sim = Sim()
    sim.run(1.0)
    sim.step(request(0, XR_SPORT, "sport", 7101, {"data": 1}))
    sim.fsm = (1, 0, 0)
    sim.run(1.5)
    stop = sim.state()
    assert (stop.state, stop.actor_id, stop.device_id) == ("safety_stop", "op_02", "xr_teleop")
    assert cause(stop) == "request:sport:7101"


def test_damp_from_an_undeclared_source_is_a_stop_by_an_unidentified_person__defs_6_7() -> None:
    sim = Sim()
    sim.feed(seen(0.0, STRANGER_SPORT))
    sim.run(1.0)
    sim.step(request(0, STRANGER_SPORT, "sport", 7101, {"data": 0}))
    sim.fsm = (0, 0, 0)
    sim.run(1.5)
    stop = sim.state()
    assert (stop.state, stop.actor_id, stop.device_id) == (
        "safety_stop",
        "person:unidentified",
        None,
    )


def test_damp_from_an_ai_device_is_its_hold_not_a_stop__defs_6_8() -> None:
    sim = Sim()
    sim.run(1.0)
    sim.step(request(0, POLICY_SPORT, "sport", 7101, {"data": 1}))
    sim.fsm = (1, 0, 0)
    sim.run(4.0)
    value = sim.state()
    assert (value.state, value.actor_id, value.device_id) == ("ai", "ai:g1_policy", "g1_policy")
    assert "safety_stop" not in sim.states()


def test_stop_is_at_once_while_released_and_ends_at_the_next_mode_change__defs_6_7() -> None:
    sim = Sim()
    sim.run(0.5)
    released(sim)
    sim.run(1.0, command(0, XR_LOWCMD))
    assert sim.state().state == "human_remote"
    sim.remote = remote_bytes("L2", "B")
    sim.step(command(0, XR_LOWCMD))
    assert sim.state().state == "safety_stop"
    sim.remote = remote_bytes()
    sim.run(3.0, command(0, XR_LOWCMD))  # writers that keep writing do not end it
    assert sim.state().state == "safety_stop"
    sim.step(request(0, XR_SWITCH, "motion_switcher", 1002, {"name": "ai"}))
    assert sim.state().state != "safety_stop"


def test_stop_is_at_once_while_the_builtin_controller_is_unseen__defs_6_7() -> None:
    sim = Sim()
    sim.builtin = False
    sim.run(3.0)
    assert sim.state().state == "unknown"
    sim.remote = remote_bytes("L2", "B")
    sim.step()
    assert sim.state().state == "safety_stop"


# -- rule 3 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "state", "actor", "device"),
    [
        (XR_LOWCMD, "human_remote", "op_02", "xr_teleop"),
        (POLICY_LOWCMD, "ai", "ai:g1_policy", "g1_policy"),
        (REPLAY_LOWCMD, "scripted", "script:g1_replay", "g1_replay"),
        (STRANGER_LOWCMD, "human_remote", "person:unidentified", None),
    ],
)
def test_released_scopes_follow_the_one_lowcmd_writer__defs_6_10(
    key: str, state: str, actor: str, device: str | None
) -> None:
    sim = Sim()
    sim.feed(seen(0.0, STRANGER_LOWCMD))
    sim.run(0.5)
    released(sim)
    sim.run(1.5, command(0, key))
    for scope in (g1.LEGS, g1.UPPER_BODY):
        value = sim.state(scope)
        assert (value.state, value.actor_id, value.device_id) == (state, actor, device)


def test_two_lowcmd_sources_within_the_contention_window_are_shared__defs_6_12() -> None:
    sim = Sim()
    released(sim)
    sim.run(1.0, command(0, POLICY_LOWCMD), command(0, XR_LOWCMD))
    value = sim.state()
    assert (value.state, value.reason, value.actor_id) == ("shared", "human_initiated", "op_02")
    sim.run(2.0, command(0, POLICY_LOWCMD), command(0, REPLAY_LOWCMD))
    assert sim.state().state == "shared"  # the person's last sample is still within the window
    sim.run(4.0, command(0, POLICY_LOWCMD), command(0, REPLAY_LOWCMD))
    value = sim.state()
    assert (value.state, value.device_id) == ("ai", "g1_policy")


def test_a_hand_over_within_gap_tolerance_takes_the_incoming_writer__defs_6_14() -> None:
    sim = Sim()
    released(sim)
    sim.run(1.0, command(0, POLICY_LOWCMD))
    sim.run(2.0)
    assert sim.state().state == "ai"
    sim.run(2.5, command(0, XR_LOWCMD))
    assert sim.state().state == "human_remote"
    assert all(value.state != "unknown" for t, value in sim.history if t >= 1.0)


def test_no_writer_inside_a_task_is_unknown_after_gap_tolerance__defs_6_14() -> None:
    sim = Sim()
    released(sim)
    sim.run(1.0, command(0, POLICY_LOWCMD))
    sim.run(2.95)
    assert sim.state().state == "ai"
    sim.run(3.2)
    assert sim.state().state == "unknown"
    assert cause(sim.state()) == "no_writer:released"


def test_no_writer_outside_a_task_keeps_the_last_controller_until_idle__defs_6_14() -> None:
    sim = Sim(task=None)
    released(sim)
    sim.run(1.0, command(0, POLICY_LOWCMD))
    sim.run(30.9)
    assert sim.state().state == "ai"
    sim.run(31.2)
    assert sim.state().state == "idle"


def test_user_control_follows_user_lowcmd_until_internal_control__defs_6_10() -> None:
    sim = Sim()
    user = Source(host="g1-pc2", participant="eval_g1.py", topic=g1.USER_LOWCMD)
    sim.feed(g1.WriterSeen(0.0, "w-user", user, ""))
    sim.run(0.5)
    sim.step(request(0, POLICY_SPORT, "sport", 7110, {}))
    sim.run(1.5, g1.Command(0, "w-user", g1.USER_LOWCMD))
    value = sim.state()
    assert (value.state, value.device_id, cause(value)) == (
        "ai",
        "g1_policy",
        "writer:user_control",
    )
    sim.step(request(0, POLICY_SPORT, "sport", 7111, {}))
    assert cause(sim.state()) == "goal:request:sport:7111"
    sim.run(5.0)
    assert cause(sim.state()) == "builtin"


def test_a_refused_select_mode_keeps_the_robot_released__defs_6_10() -> None:
    """Open case: a response only ever keeps the robot released, never takes it out."""
    sim = Sim()
    released(sim)
    sim.step(request(0, XR_SWITCH, "motion_switcher", 1002, {"name": "ai"}, request_id=8))
    sim.step(g1.Response(0, "w-resp", "motion_switcher", 8, 1002, 3104, ""))
    sim.run(1.5, command(0, POLICY_LOWCMD))
    assert cause(sim.state()) == "writer:released"
    sim.step(request(0, XR_SWITCH, "motion_switcher", 1002, {"name": "ai"}, request_id=9))
    sim.step(g1.Response(0, "w-resp", "motion_switcher", 9, 1002, 0, ""))
    sim.run(2.0)
    assert cause(sim.state()) == "builtin"


def test_a_refused_release_request_is_not_undone__defs_6_10() -> None:
    """Open case: undoing it would move the robot towards the built-in AI."""
    sim = Sim()
    released(sim)
    sim.step(g1.Response(0, "w-resp", "motion_switcher", 7, 1003, 3104, ""))
    sim.step(g1.Response(0, "w-resp", "motion_switcher", 99, 1003, 3104, ""))  # unknown id
    sim.run(1.0, command(0, POLICY_LOWCMD))
    assert cause(sim.state()) == "writer:released"


def test_a_check_mode_answer_with_no_mode_means_released__defs_6_10() -> None:
    sim = Sim()
    sim.run(0.5)
    sim.step(g1.Response(0, "w-resp", "motion_switcher", 1, 1001, 0, '{"form":"0","name":"ai"}'))
    sim.run(1.0, command(0, POLICY_LOWCMD))
    assert cause(sim.state()) == "builtin"  # an answer that names a mode changes nothing
    sim.step(g1.Response(0, "w-resp", "motion_switcher", 2, 1001, 0, '{"form":"0","name":""}'))
    sim.run(1.5, command(0, POLICY_LOWCMD))
    assert cause(sim.state()) == "writer:released"


def test_a_persons_remote_input_contends_with_an_ai_lowcmd_writer__defs_6_12() -> None:
    """Open case: a person's goal span while released makes an AI's control shared."""
    sim = Sim()
    released(sim)
    sim.run(1.0, command(0, POLICY_LOWCMD))
    sim.remote = remote_bytes(lx=0.4)
    sim.step(command(0, POLICY_LOWCMD))
    value = sim.state()
    assert (value.state, value.actor_id) == ("shared", "op_01")


# -- rule 4 -------------------------------------------------------------------------------------


def test_builtin_unseen_is_unknown_or_follows_an_active_lowcmd_writer__defs_6_14() -> None:
    sim = Sim()
    sim.builtin = False
    sim.run(1.0)
    assert (sim.state().state, cause(sim.state())) == ("unknown", "builtin_unseen")
    sim.run(2.0, command(0, POLICY_LOWCMD))
    assert (sim.state().state, cause(sim.state())) == ("ai", "writer:builtin_unseen")


def test_sportmodestate_from_an_undeclared_writer_is_ignored__defs_6_10() -> None:
    sim = Sim()
    sim.builtin = False
    sim.feed(g1.WriterSeen(0.0, "fake", Source(None, g1.SPORTSTATE, "laptop", "python3")))
    sim.run(1.0, g1.SportState(0, "fake", 500, 0, 0))
    assert cause(sim.state()) == "builtin_unseen"


# -- rule 5 -------------------------------------------------------------------------------------


def test_zero_torque_from_the_remote_is_a_persons_hold__defs_6_8() -> None:
    sim = Sim()
    sim.run(0.5)
    sim.remote = remote_bytes("L2", "Y")
    sim.run(0.6)
    sim.fsm = (0, 0, 0)
    sim.run(0.8)
    sim.remote = remote_bytes()
    sim.run(5.0)
    value = sim.state()
    assert (value.state, value.reason, value.actor_id) == ("shared", "human_initiated", "op_01")
    assert cause(value) == "remote:L2+Y"


def test_passive_with_no_observed_cause_is_unknown_inside_a_task__defs_6_8() -> None:
    sim = Sim()
    sim.run(0.5)
    sim.fsm = (1, 0, 0)
    sim.run(1.0)
    assert (sim.state().state, cause(sim.state())) == ("unknown", "unobserved")


def test_passive_outside_a_task_keeps_the_last_controller_until_idle__defs_6_14() -> None:
    sim = Sim(task=None)
    sim.run(1.0, velocity(0, POLICY_SPORT, 0.3))
    sim.fsm = (1, 0, 0)
    sim.run(10.0)
    assert (sim.state().state, sim.state().device_id) == ("ai", "g1_policy")
    sim.run(32.0)
    assert sim.state().state == "idle"


# -- rule 6 -------------------------------------------------------------------------------------


def test_remote_select_y_preset_is_scripted_started_by_a_person__defs_6_9_6_13() -> None:
    sim = Sim()
    sim.run(0.5)
    sim.remote = remote_bytes("SELECT", "Y")
    sim.run(0.6)
    sim.remote = remote_bytes()
    sim.run(1.0)
    upper = sim.state(g1.UPPER_BODY)
    assert (upper.state, upper.reason, upper.actor_id, upper.device_id) == (
        "scripted",
        "human_initiated",
        "script:wave_hand",
        "unitree_presets",
    )
    assert sim.state(g1.LEGS).state == "ai"  # SELECT + Y is not goal input
    sim.run(20.5)
    assert sim.state(g1.UPPER_BODY).state == "scripted"
    sim.run(21.0)
    assert sim.state(g1.UPPER_BODY).state == "ai"  # 20 s after the last sign


def test_preset_ends_when_the_arm_action_state_releases__defs_6_13() -> None:
    sim = Sim()
    sim.step(request(0, XR_SPORT, "sport", 7106, {"data": 0}))
    sim.step(g1.ArmState(0, "w-state", '{"holding": true, "id": 1, "name": "wave"}'))
    sim.run(3.0)
    assert sim.state(g1.UPPER_BODY).actor_id == "script:arm_task_0"
    sim.step(g1.ArmState(0, "w-state", '{"holding": false, "id": 1, "name": "wave"}'))
    assert sim.state(g1.UPPER_BODY).state == "ai"


def test_preset_ends_at_release_id_99_and_at_task_id_back_to_zero__defs_6_13() -> None:
    sim = Sim()
    sim.step(request(0, POLICY_ARM_REQ, "arm", 7106, {"action_id": 18}))
    sim.run(1.0)
    upper = sim.state(g1.UPPER_BODY)
    assert (upper.state, upper.reason, upper.actor_id) == ("scripted", None, "script:arm_action_18")
    sim.step(request(0, POLICY_ARM_REQ, "arm", 7106, {"action_id": 99}))
    assert sim.state(g1.UPPER_BODY).state == "ai"
    sim.remote = remote_bytes("SELECT", "A")
    sim.step()
    sim.remote = remote_bytes()
    sim.fsm = (500, 0, 2)
    sim.run(3.0)
    assert sim.state(g1.UPPER_BODY).actor_id == "script:handshake"
    sim.fsm = (500, 0, 0)
    sim.step()
    assert sim.state(g1.UPPER_BODY).state == "ai"


def test_custom_action_ends_at_stop_custom_action__defs_6_13() -> None:
    sim = Sim()
    sim.step(request(0, POLICY_ARM_REQ, "arm", 7108, {"action_name": "bow deeply"}))
    assert sim.state(g1.UPPER_BODY).actor_id == "script:custom_bow_deeply"
    sim.step(request(0, POLICY_ARM_REQ, "arm", 7113, {}))
    assert sim.state(g1.UPPER_BODY).state == "ai"


def test_arm_action_service_writes_keep_the_preset_running__defs_6_9() -> None:
    sim = Sim()
    sim.step(request(0, POLICY_ARM_REQ, "arm", 7106, {"action_id": 3}))
    sim.run(25.0, command(0, ARM_SERVICE, 1.0))
    assert sim.state(g1.UPPER_BODY).actor_id == "script:arm_action_3"
    sim.run(45.1)
    assert sim.state(g1.UPPER_BODY).state == "ai"


def test_a_person_writing_arm_sdk_during_a_preset_is_shared__defs_6_12() -> None:
    """Open case: another writer contends with a preset."""
    sim = Sim()
    sim.step(request(0, POLICY_ARM_REQ, "arm", 7106, {"action_id": 3}))
    sim.run(1.0, command(0, XR_ARM, 1.0))
    upper = sim.state(g1.UPPER_BODY)
    assert (upper.state, upper.actor_id, cause(upper)) == ("shared", "op_02", "contention:preset")


def test_an_arm_motion_with_no_observed_start_is_unknown__defs_7() -> None:
    """Open case: the app or the voice service may have started it (S12)."""
    sim = Sim()
    sim.fsm = (500, 0, 1)
    sim.run(1.0)
    assert (sim.state(g1.UPPER_BODY).state, sim.state(g1.LEGS).state) == ("unknown", "ai")
    sim.fsm = (500, 0, 0)
    sim.step(g1.ArmState(0, "w-state", '{"holding": true, "id": 2, "name": "x"}'))
    assert sim.state(g1.UPPER_BODY).state == "unknown"
    sim.step(g1.ArmState(0, "w-state", '{"holding": false, "id": 2, "name": "x"}'))
    assert sim.state(g1.UPPER_BODY).state == "ai"


@pytest.mark.parametrize(
    ("key", "weight", "state", "device", "why"),
    [
        (XR_ARM, 1.0, "human_remote", "xr_teleop", "writer"),
        (XR_ARM, 0.5, "shared", "xr_teleop", "blend"),
        (POLICY_ARM, 0.995, "ai", "g1_policy", "writer"),
        (POLICY_ARM, 0.5, "ai", "g1_policy", "blend"),
        (ARM_SERVICE, 1.0, "scripted", "arm_action_service", "writer"),
        (ARM_SERVICE, 0.4, "ai", "g1_builtin", "blend"),
        (XR_ARM, 0.01, "ai", "g1_builtin", "builtin"),
    ],
)
def test_arm_sdk_weight_decides_the_upper_body__defs_6_6_6_12(
    key: str, weight: float, state: str, device: str, why: str
) -> None:
    sim = Sim()
    sim.run(1.0, command(0, key, weight))
    upper = sim.state(g1.UPPER_BODY)
    assert (upper.state, upper.device_id, cause(upper)) == (state, device, why)
    assert sim.state(g1.LEGS).state == "ai"
    if why != "builtin":
        assert dict(upper.fields)["x_g1_weight"] == pytest.approx(weight)


def test_an_unreadable_arm_sdk_writer_counts_as_holding_the_arms__defs_6_12() -> None:
    """Open case: a weight that cannot be read counts as 1."""
    sim = Sim()
    sim.feed(seen(0.0, POLICY_ARM, decoded=False))
    sim.run(1.0)
    upper = sim.state(g1.UPPER_BODY)
    assert (upper.state, upper.device_id, cause(upper)) == ("ai", "g1_policy", "writer")
    sim.feed(g1.WriterGone(sim.t, POLICY_ARM))
    sim.run(1.2)
    assert cause(sim.state(g1.UPPER_BODY)) == "builtin"


# -- rule 7 -------------------------------------------------------------------------------------


def test_remote_sticks_are_a_persons_goal_input_shared_until_neutral__defs_6_11() -> None:
    sim = Sim()
    sim.run(1.0)
    sim.remote = remote_bytes(ly=0.6)
    sim.run(2.0)
    legs = sim.state()
    assert (legs.state, legs.reason, legs.actor_id, legs.device_id) == (
        "shared",
        "human_initiated",
        "op_01",
        "g1_remote",
    )
    assert cause(legs) == "goal:remote"
    sim.remote = remote_bytes(ly=0.005)  # inside the dead zone: neutral, ends the span at once
    sim.step()
    assert sim.state().state == "ai"


def test_moving_with_no_goal_span_beyond_gap_tolerance_is_unknown__defs_6_14() -> None:
    sim = Sim()
    sim.run(1.0)
    sim.remote = remote_bytes(ly=0.6)
    sim.fsm = (500, 1, 0)
    sim.run(2.0)
    sim.remote = remote_bytes()
    sim.run(3.95)
    assert sim.state().state == "shared"  # the last controller holds while it decelerates
    sim.run(4.1)
    assert (sim.state().state, cause(sim.state())) == ("unknown", "unobserved")
    assert sim.state(g1.UPPER_BODY).state == "unknown"


def test_set_velocity_lasts_its_duration_and_stop_move_ends_it__defs_6_11() -> None:
    sim = Sim()
    sim.step(velocity(0, XR_SPORT, 0.3, duration=3.0))
    sim.run(3.0)
    assert sim.state().state == "shared"
    sim.run(3.2)
    assert sim.state().state == "ai"
    sim.step(velocity(0, XR_SPORT, 0.3, duration=864000.0))
    sim.run(10.0)
    assert sim.state().state == "shared"
    sim.step(velocity(0, XR_SPORT, 0.0))
    assert sim.state().state == "ai"


@pytest.mark.parametrize(
    ("key", "state", "device"),
    [
        (POLICY_SPORT, "ai", "g1_policy"),
        (STRANGER_SPORT, "human_remote", None),
    ],
)
def test_goal_input_from_a_program_or_an_undeclared_source__defs_6_10_6_11(
    key: str, state: str, device: str | None
) -> None:
    sim = Sim()
    sim.feed(seen(0.0, STRANGER_SPORT))
    sim.step(velocity(0, key, 0.3))
    legs = sim.state()
    assert (legs.state, legs.device_id) == (state, device)


def test_two_goal_sources_at_once_are_shared__defs_6_12() -> None:
    sim = Sim()
    sim.step(velocity(0, POLICY_SPORT, 0.3, duration=5.0))
    assert sim.state().state == "ai"
    sim.remote = remote_bytes(lx=0.5)
    sim.step()
    legs = sim.state()
    assert (legs.state, legs.actor_id, cause(legs)) == ("shared", "op_01", "contention:goal")


def test_a_posture_a_person_started_is_scripted_while_it_moves__defs_6_13() -> None:
    sim = Sim()
    sim.run(0.5)
    sim.remote = remote_bytes("L2", "LEFT")
    sim.run(1.0)
    assert sim.state().state == "shared"  # the person holds the combination: goal input
    sim.remote = remote_bytes()
    sim.fsm = (3, 1, 0)
    sim.run(3.0)
    legs = sim.state()
    assert (legs.state, legs.reason, legs.actor_id, legs.device_id) == (
        "scripted",
        "human_initiated",
        "script:fsm_3",
        "unitree_presets",
    )
    sim.fsm = (3, 0, 0)
    sim.run(4.0)
    legs = sim.state()  # rule 7c: static in a position-control posture, the person's hold
    assert (legs.state, legs.actor_id, cause(legs)) == ("shared", "op_01", "remote:L2+LEFT")


def test_a_posture_a_program_started_is_scripted_without_a_person__defs_6_13() -> None:
    sim = Sim()
    sim.step(request(0, POLICY_SPORT, "sport", 7101, {"data": 702}))
    sim.fsm = (702, 1, 0)
    sim.run(2.0)
    legs = sim.state()
    assert (legs.state, legs.reason, legs.actor_id) == ("scripted", None, "script:fsm_702")
    sim.fsm = (702, 0, 0)
    sim.run(3.0)
    assert cause(sim.state()) == "builtin"  # 7d: balance posture, inside a task


def test_static_inside_a_task_is_the_builtin_ai__defs_2() -> None:
    sim = Sim()
    sim.run(1.0)
    legs = sim.state()
    assert (legs.state, legs.actor_id, legs.device_id) == ("ai", "ai:g1_builtin", "g1_builtin")
    assert dict(legs.fields)["x_g1_fsm_id"] == 500


def test_static_outside_a_task_holds_the_last_controller_then_idle__defs_2_6_14() -> None:
    sim = Sim(task=None)
    sim.run(1.0)
    assert sim.state().state == "unknown"  # nobody has controlled the robot yet
    sim.step(velocity(0, XR_SPORT, 0.3))
    sim.run(20.0)
    assert sim.state().state == "shared"
    sim.run(31.2)
    assert sim.state().state == "idle"
    sim.feed(g1.TaskOpened(sim.t, "t2"))
    sim.step()
    assert sim.state().state == "ai"


# -- rule 8 -------------------------------------------------------------------------------------


def test_hands_follow_their_writers__defs_6_6_6_12() -> None:
    sim = Sim(hands=True)
    sim.run(1.0)
    assert sim.state(g1.HANDS).state == "unknown"
    sim.run(2.0, command(0, XR_DEX3))
    hands = sim.state(g1.HANDS)
    assert (hands.state, hands.device_id) == ("human_remote", "xr_teleop")
    sim.feed(g1.WriterSeen(0.0, "w-hsdk", Source(None, g1.HAND_SDK, "g1-pc2", "eval_g1.py")))
    sim.run(2.5, command(0, XR_DEX3), g1.Command(0, "w-hsdk", g1.HAND_SDK, 0.6))
    assert sim.state(g1.HANDS).state == "shared"
    sim.run(5.0, g1.Command(0, "w-hsdk", g1.HAND_SDK, 0.005))
    assert sim.state(g1.HANDS).state == "unknown"
    sim.run(6.0, g1.Command(0, "w-hsdk", g1.HAND_SDK, 0.8))
    assert sim.state(g1.HANDS).state == "ai"


def test_hands_outside_a_task_keep_the_last_controller_until_idle__defs_6_14() -> None:
    sim = Sim(hands=True, task=None)
    sim.run(1.0, command(0, XR_DEX3))
    sim.run(20.0)
    assert sim.state(g1.HANDS).state == "human_remote"
    sim.run(31.1)
    assert sim.state(g1.HANDS).state == "idle"


def test_an_unreadable_hand_writer_counts_while_it_exists__defs_6_6() -> None:
    sim = Sim(hands=True)
    sim.feed(seen(0.0, XR_DEX3, decoded=False))
    sim.run(1.0)
    assert sim.state(g1.HANDS).state == "human_remote"


# -- tasks, notes, messages ---------------------------------------------------------------------


def test_unreadable_tasks_count_as_outside_and_say_so_once__notes_8_defs_6_14() -> None:
    sim = Sim()
    sim.run(1.0)
    sim.feed(g1.TasksUnreadable(sim.t), g1.TasksUnreadable(sim.t))
    notes = [n for n in sim.notes if dict(n.fields).get("x_g1_tasks")]
    assert [dict(n.fields) for n in notes] == [{"x_g1_tasks": "unreadable"}]
    assert not sim.engine.task_open
    sim.feed(g1.TaskClosed(sim.t))
    sim.feed(g1.TasksUnreadable(sim.t))
    assert len([n for n in sim.notes if dict(n.fields).get("x_g1_tasks")]) == 2


def test_task_closed_for_another_task_keeps_the_open_one__notes_8() -> None:
    sim = Sim()
    sim.feed(g1.TaskClosed(0.0, "other"))
    assert sim.engine.task_open
    sim.feed(g1.TaskClosed(0.0, "t1"))
    assert not sim.engine.task_open


def test_active_writers_are_noted_when_they_change__notes_6() -> None:
    sim = Sim()
    sim.run(0.5)
    sim.run(1.0, command(0, XR_ARM, 1.0))
    noted = [dict(n.fields)["x_g1_writers"] for n in sim.notes if "x_g1_writers" in dict(n.fields)]
    assert len(noted) == 2
    assert {"topic": "rt/arm_sdk", "device_id": "xr_teleop"} in noted[-1]
    sim.run(2.0, command(0, XR_ARM, 1.0))
    assert len([n for n in sim.notes if "x_g1_writers" in dict(n.fields)]) == 2


def test_writers_note_names_no_host__claude_md_product_rule_5() -> None:
    """A declared writer by topic and device; an undeclared one by topic, program and a
    pseudonym of its host (the first 12 hex digits of its SHA-256), never the host name."""
    sim = Sim()
    released(sim)
    sim.run(1.0, command(0, STRANGER_LOWCMD), g1.Command(0, "w-new", g1.LOWCMD))
    writers = [dict(n.fields)["x_g1_writers"] for n in sim.notes][-1]
    assert {"topic": "rt/lowcmd", "program": "python3", "host": "5eec0dc419aa"} in writers
    assert {"topic": "rt/lowcmd", "program": None, "host": None} in writers
    assert {"topic": "rt/lowstate", "device_id": None} not in writers
    text = str([dict(n.fields) for n in sim.notes])
    for host in ("laptop", "teleop-pc", "g1-pc1", "g1-pc2"):
        assert host not in text
    assert g1.host_pseudonym("laptop") == "5eec0dc419aa"
    assert g1.host_pseudonym(None) is None


@pytest.mark.parametrize(("api", "mode"), [(1003, "released"), (7110, "user_control")])
def test_the_robot_returning_while_released_is_noted_once__defs_6_14(api: int, mode: str) -> None:
    """A restart ends a release or user control without a request: the mode stands (never
    towards ai) and the log says the robot returned in it."""
    sim = Sim()
    key = XR_SWITCH if api == 1003 else POLICY_SPORT
    service = "motion_switcher" if api == 1003 else "sport"
    sim.step(request(0, key, service, api, {}))
    sim.run(1.0)
    sim.robot = False
    sim.run(20.0)
    sim.robot = True
    sim.run(21.0)  # back within idle_threshold_s: nothing to say
    sim.robot = False
    sim.run(52.0)
    sim.robot = True
    sim.run(53.0)
    returned = [dict(n.fields) for n in sim.notes if "x_g1_robot_returned" in dict(n.fields)]
    assert returned == [{"x_g1_robot_returned": mode}]
    assert cause(sim.state()).endswith(mode)


def test_writers_note_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g1, "MAX_NOTED_WRITERS", 1)
    sim = Sim()
    sim.run(0.5, command(0, XR_ARM, 1.0))
    fields = dict(sim.notes[-1].fields)
    assert len(fields["x_g1_writers"]) == 1
    assert fields["x_g1_writers_more"] == 2


def test_only_a_change_of_value_is_sent_between_ticks__readme_wire_format() -> None:
    eng = engine()
    eng.observe(seen(0.0, LOWSTATE))
    eng.observe(seen(0.0, SPORT))
    eng.observe(g1.TaskOpened(0.0, "t1"))
    first = eng.observe(g1.LowState(0.1, LOWSTATE, remote_bytes()))
    first += eng.observe(g1.SportState(0.1, SPORT, 500, 0, 0))
    again = eng.observe(g1.SportState(0.15, SPORT, 501, 0, 0))  # x_g1_fsm_id changes only
    assert [m.state for m in first if isinstance(m, g1.StateAssertion)][-2:] == ["ai", "ai"]
    assert again == []
    assert len(eng.tick(0.2)) >= 2


def test_stop_sends_unknown_for_every_scope__defs_2() -> None:
    eng = engine(hands=True)
    values = eng.stop()
    assert [(m.scope, m.state) for m in values if isinstance(m, g1.StateAssertion)] == [
        ("legs", "unknown"),
        ("upper_body", "unknown"),
        ("hands", "unknown"),
    ]


def test_wire_event_of_a_note_is_a_client_heartbeat__readme_wire_format() -> None:
    assert g1.wire_event(g1.Note((("x_g1_tasks", "unreadable"),))) == {
        "type": "heartbeat",
        "x_g1_tasks": "unreadable",
    }


def test_commands_of_unknown_topics_and_requests_of_unknown_services_are_ignored() -> None:
    sim = Sim()
    sim.run(0.5, g1.Command(0, "x", "rt/other"), request(0, "y", "audio", 1, {}))
    assert sim.state().state == "ai"


def test_a_writer_not_yet_discovered_is_undeclared__defs_6_10() -> None:
    sim = Sim()
    released(sim)
    sim.run(1.0, g1.Command(0, "w-new", g1.LOWCMD))
    legs = sim.state()
    assert (legs.state, legs.device_id) == ("human_remote", None)


def test_malformed_parameters_are_still_input__defs_6_11() -> None:
    sim = Sim()
    sim.step(request(0, XR_SPORT, "sport", 7105, "not json"))
    assert sim.state().state == "shared"
    sim.step(request(0, XR_SPORT, "sport", 7105, '{"velocity": [1, 2]}'))
    sim.step(request(0, XR_SPORT, "sport", 7105, "x" * (g1.MAX_PARAMETER_CHARS + 1)))
    sim.step(request(0, XR_SPORT, "sport", 7101, {"data": "one"}))
    sim.step(request(0, XR_SPORT, "sport", 7106, {"data": 1.5}))
    assert sim.state(g1.UPPER_BODY).actor_id == "script:arm_task"
    sim.step(request(0, POLICY_ARM_REQ, "arm", 7106, {"action_id": True}))
    assert sim.state(g1.UPPER_BODY).actor_id == "script:arm_action"
    sim.step(request(0, POLICY_ARM_REQ, "arm", 7108, {"action_name": 5}))
    assert sim.state(g1.UPPER_BODY).actor_id == "script:custom"
    sim.step(request(0, XR_SPORT, "sport", 7001, {}))  # a query is no input


def test_writers_beyond_the_limit_drop_the_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g1, "MAX_WRITERS", 3)
    eng = engine()
    for k in range(5):
        eng.observe(g1.Command(float(k), f"w{k}", g1.LOWCMD))
    assert len(eng._writers) == 3


def test_pending_mode_requests_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g1, "MAX_PENDING", 2)
    eng = engine()
    for k in range(4):
        eng.observe(request(float(k), XR_SWITCH, "motion_switcher", 1003, {}, request_id=k))
    assert len(eng._pending) == 2


# -- remote, registry, settings -----------------------------------------------------------------


def test_decode_remote_reads_the_gamepad_layout__notes_s2() -> None:
    data = remote_bytes("L2", "B", lx=0.25, ly=-1.0)
    remote = g1.decode_remote(data)
    assert remote is not None
    assert remote.buttons == g1.BUTTONS["L2"] | g1.BUTTONS["B"] == 0x0220
    assert (remote.lx, remote.ly) == (0.25, -1.0)
    assert g1.decode_remote(bytes(23)) is None
    assert g1.decode_remote(bytes(40)) == g1.RemoteInput(0, 0.0, 0.0, 0.0, 0.0)


def test_a_stick_value_that_is_not_a_number_is_outside_the_dead_zone__notes_9() -> None:
    nan = struct.pack("<2xH5f", 0, math.nan, 0, 0, 0, 0) + bytes(16)
    remote = g1.decode_remote(nan)
    assert remote is not None
    assert not remote.centred(0.01)


def test_remote_bytes_from_another_lowstate_writer_are_ignored__notes_4() -> None:
    sim = Sim()
    sim.feed(g1.WriterSeen(0.0, "w-sim", Source(None, g1.LOWSTATE, "laptop", "sim.py")))
    sim.run(1.0, g1.LowState(0, "w-sim", remote_bytes(lx=1.0)))
    assert sim.state().state == "ai"


def test_registry_problems_refuse_a_source_two_kinds_could_match__notes_4() -> None:
    assert g1.registry_problems(DEVICES) == []
    teleop = Device("teleop", "human", (("participant", "python3"),), "local")
    policy = Device("policy", "ai", (("host", "pc2"), ("participant", "python3")))
    other = Device("other", "ai", (("participant", "python3"), ("host", "pc3")))
    (problem,) = g1.registry_problems((teleop, policy))
    assert "'teleop' (human local) and 'policy' (ai)" in problem
    assert g1.registry_problems((policy, other)) == []  # same kind
    remote = Device("remote", "human", (("participant", "python3"),), "remote")
    assert len(g1.registry_problems((teleop, remote))) == 1  # same kind, other location
    ros = Device("ros", "ai", (("node", "/nav"),))
    assert g1.registry_problems((teleop, ros)) == []  # a ROS node is never observed here
    preset = Device("presets", "script", (("topic", "preset"),))
    assert g1.registry_problems((teleop, preset)) == []  # the preset source has no participant
    by_topic = Device("lowcmd_ai", "ai", (("topic", "rt/lowcmd"),))
    assert len(g1.registry_problems((teleop, by_topic))) == 1


def test_settings_are_checked() -> None:
    with pytest.raises(ValueError, match="gap_tolerance_s"):
        g1.Settings(DEVICES, 0.0, 1.0, 1.0, 30.0)
    with pytest.raises(ValueError, match="weight limits"):
        settings(weight_min=0.5, weight_max=0.4)
    assert settings(hands=True).scopes == ("legs", "upper_body", "hands")


def test_request_and_response_topics__notes_s7() -> None:
    assert g1.request_topic("sport") == "rt/api/sport/request"
    assert g1.response_topic("arm") == "rt/api/arm/response"


def test_a_long_custom_action_name_keeps_a_valid_identifier__defs_8() -> None:
    sim = Sim()
    sim.step(request(0, POLICY_ARM_REQ, "arm", 7108, {"action_name": "é" * 300}))
    actor = sim.state(g1.UPPER_BODY).actor_id
    assert actor is not None
    assert actor.startswith("script:")
    assert len(actor) <= 128
