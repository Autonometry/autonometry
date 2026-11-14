"""The LeRobot log parser and live engine (connectors/lerobot.py): one test per rule of the module
docstring, named by spec section and dossier 14.1 row (research/lerobot-so101)."""

import pytest

from autonometry.connectors import lerobot
from autonometry.connectors.tb4 import Device, StateAssertion, TaskEnd, TaskStart
from lerobot_helpers import DEVICES, POLICY, engine, feed, line, settings, setup_lines, src

AI = ("ai", None, f"ai:{POLICY.replace('/', '_')}", "so101_policy")
PAUSED = ("shared", "human_initiated", "op_01", "so101_leader")
CORRECTING = ("human_local", "human_initiated", "op_01", "so101_leader")
SCRIPTED = ("scripted", None, "script:return_to_initial_position", "return_to_start")
IDLE = ("idle", None, None, None)
UNKNOWN = ("unknown", None, None, None)


def _value(message: StateAssertion) -> tuple[str, str | None, str | None, str | None]:
    return message.state, message.reason, message.actor_id, message.device_id


def _states(
    messages: list[lerobot.Message],
) -> list[tuple[str, str | None, str | None, str | None]]:
    return [_value(m) for m in messages if isinstance(m, StateAssertion)]


def _kinds(messages: list[lerobot.Message]) -> list[str]:
    names = {StateAssertion: "state", TaskStart: "task_start", TaskEnd: "task_end"}
    return [names.get(type(m), "note") for m in messages]


def _now(eng: lerobot.LeRobotEngine, t: float) -> tuple[str, str | None, str | None, str | None]:
    (value,) = eng.tick(t)
    assert isinstance(value, StateAssertion)
    return _value(value)


def _dagger(eng: lerobot.LeRobotEngine, continuous: bool = True) -> list[lerobot.Message]:
    started = (
        "DAgger continuous recording started (episode_duration=600s)"
        if continuous
        else "DAgger corrections-only recording started (target: 50 episodes)"
    )
    return feed(eng, [*setup_lines(0.0), (5.0, line(started, 5))])


# -- parser -------------------------------------------------------------------------------------


def test_parses_init_logging_lines_with_their_local_time__dossier_s2_defs_8() -> None:
    parsed = lerobot.parse_line(line("Phase transition: autonomous -> paused", 3))
    assert parsed == lerobot.Parsed(lerobot.Phase("autonomous", "paused"), src(3))
    with_pid = (
        "INFO [PID: 4242] 2026-11-14 10:00:07 s/dagger.py:659 "
        "Phase transition: paused -> correcting\r\n"
    )
    assert lerobot.parse_line(with_pid) == lerobot.Parsed(
        lerobot.Phase("paused", "correcting"), "2026-11-14 10:00:07"
    )


@pytest.mark.parametrize(
    ("message", "signal"),
    [
        ("Building rollout context...", lerobot.ProcessStart()),
        ("Loading policy from 'user/act_so101'...", lerobot.PolicyPath("user/act_so101")),
        ("Connecting teleoperator (phone)...", lerobot.TeleopType("phone")),
        ("Dataset ready: a/rollout_x (12 existing episodes)", lerobot.ExistingEpisodes(12)),
        (
            "DAgger continuous recording started (episode_duration=600s)",
            lerobot.LoopStart("dagger_continuous"),
        ),
        (
            "DAgger corrections-only recording started (target: 5 episodes)",
            lerobot.LoopStart("dagger_corrections"),
        ),
        ("Sentry recording started (episode_duration=600s)", lerobot.LoopStart("sentry")),
        (
            "Highlight strategy recording started (press 's' to save)",
            lerobot.LoopStart("highlight"),
        ),
        ("Base strategy control loop started", lerobot.LoopStart("base")),
        ("Episode saved (total: 3, elapsed: 600.2s)", lerobot.EpisodeSaved(3, rotation=True)),
        ("Episode saved (total: 3)", lerobot.EpisodeSaved(3, rotation=False)),
        ("Correction 2/50 saved", lerobot.CorrectionSaved()),
        ("Recording episode 7", lerobot.EpisodeRecording(7)),
        ("Reset the environment", lerobot.ResetStart()),
        ("Re-record episode", lerobot.Rerecord()),
        ("DAgger continuous control loop ended — pausing engine", lerobot.LoopEnd()),
        ("DAgger corrections-only loop ended — pausing engine", lerobot.LoopEnd()),
        ("Episodic control loop ended — saving any in-progress episode", lerobot.LoopEnd()),
        ("Sentry control loop ended — saving final episode", lerobot.LoopEnd()),
        ("Highlight control loop ended", lerobot.LoopEnd()),
        ("Stopping inference engine...", lerobot.LoopEnd()),
        ("Returning robot to initial position before shutdown...", lerobot.ReturnHome()),
        ("Disconnecting robot...", lerobot.RobotDisconnect()),
        ("Rollout finished", lerobot.ProcessEnd()),
    ],
)
def test_parses_every_message_the_pinned_lerobot_writes__dossier_5a(
    message: str, signal: lerobot.Signal
) -> None:
    assert lerobot.parse_line(line(message, 1)) == lerobot.Parsed(signal, src(1))


def test_printed_rerecord_key_has_no_time__dossier_s14() -> None:
    text = "Left arrow key pressed. Exiting loop and rerecord the last episode..."
    assert lerobot.parse_line(text) == lerobot.Parsed(lerobot.RerecordKey(), None)


@pytest.mark.parametrize(
    "text",
    [
        "",
        line("Pausing engine - robot holds position"),
        line("Phase transition: autonomous -> flying"),
        line("Episode saved (total: 99999999999999999999, elapsed: 1.0s)"),
        "x" * (lerobot.MAX_LINE_CHARS + 1),
    ],
)
def test_lines_the_connector_does_not_use_are_ignored(text: str) -> None:
    assert lerobot.parse_line(text) is None


# -- identities ---------------------------------------------------------------------------------


def test_controller_id_is_an_identifier__defs_1_actor_8_identifiers() -> None:
    assert lerobot.controller_id("user/act so101") == "user_act_so101"
    assert lerobot.controller_id("  ") == "unidentified"
    long = lerobot.controller_id("/home/x/" + "a" * 300)
    assert len(long) == 125
    assert long == lerobot.controller_id("/home/x/" + "a" * 300)
    assert long != lerobot.controller_id("/home/y/" + "a" * 300)


def test_task_definition_through_the_task_registry__defs_1_task_5() -> None:
    registry = {"tasks": [{"task_definition_id": "cube", "description": "Pick the cube."}]}
    assert lerobot.task_definition("Pick the cube.", registry) == ("cube", True)
    assert lerobot.task_definition("cube", registry) == ("cube", True)
    other, known = lerobot.task_definition("Fold the cloth.", registry)
    assert not known
    assert other.startswith("lerobot_task_")
    assert lerobot.task_definition("Fold the cloth.", None) == (other, False)
    assert lerobot.task_definition(None, registry)[1] is False


def test_settings_are_checked() -> None:
    with pytest.raises(ValueError, match="scopes"):
        settings(scopes=())
    with pytest.raises(ValueError, match="idle_threshold_s"):
        settings(idle_threshold_s=0.0)


# -- live rules ---------------------------------------------------------------------------------


def test_nothing_is_asserted_before_a_line_tells_the_state__defs_2_unknown() -> None:
    eng = engine()
    assert eng.tick(1.0) == []
    assert feed(eng, [(2.0, line("Episode saved (total: 1, elapsed: 3.0s)", 2))]) == []
    assert feed(eng, [(2.0, line("Disconnecting robot...", 2))]) == []
    assert feed(eng, [(2.0, line("Rollout finished", 2))]) == []
    assert eng.tick(3.0) == []


def test_setup_is_idle_until_the_control_loop__defs_2_idle__dossier_14_1_connect() -> None:
    eng = engine()
    out = feed(eng, setup_lines(0.0))
    assert _states(out) == [IDLE]
    assert _now(eng, 4.0) == IDLE


def test_autonomous_is_ai_with_the_policy__defs_2_6_10__dossier_14_1_dagger_autonomous() -> None:
    eng = engine()
    out = _dagger(eng)
    assert _states(out) == [IDLE, AI]
    assert _now(eng, 100.0) == AI


def test_paused_is_shared_human_initiated__defs_6_8__dossier_14_1_dagger_paused() -> None:
    eng = engine()
    _dagger(eng)
    out = feed(eng, [(10.0, line("Phase transition: autonomous -> paused", 10))])
    assert _states(out) == [PAUSED]


def test_correcting_is_human_local__defs_2__dossier_14_1_dagger_correcting() -> None:
    eng = engine()
    _dagger(eng)
    out = feed(
        eng,
        [
            (10.0, line("Phase transition: autonomous -> paused", 10)),
            (12.0, line("Phase transition: paused -> correcting", 12)),
            (20.0, line("Phase transition: correcting -> paused", 20)),
            (21.0, line("Phase transition: paused -> autonomous", 21)),
        ],
    )
    assert _states(out) == [PAUSED, CORRECTING, PAUSED, AI]


def test_correcting_by_a_remote_teleoperator_is_remote__defs_6_10__dossier_14_1_phone() -> None:
    eng = engine()
    feed(
        eng,
        [*setup_lines(0.0, teleop="phone"), (5.0, line("Base strategy control loop started", 5))],
    )
    out = feed(
        eng,
        [
            (10.0, line("Phase transition: autonomous -> paused", 10)),
            (12.0, line("Phase transition: paused -> correcting", 12)),
        ],
    )
    assert _states(out) == [
        ("shared", "human_initiated", "op_02", "phone_teleop"),
        ("human_remote", "human_initiated", "op_02", "phone_teleop"),
    ]


def test_undeclared_sources_carry_no_device__defs_6_10() -> None:
    eng = engine()
    feed(
        eng,
        [
            (0.0, line("Loading policy from 'someone/else'...")),
            (0.0, line("Connecting teleoperator (keyboard_ee)...")),
            (1.0, line("DAgger corrections-only recording started (target: 5 episodes)", 1)),
        ],
    )
    assert _now(eng, 2.0) == ("ai", None, "ai:someone_else", None)
    feed(eng, [(3.0, line("Phase transition: autonomous -> paused", 3))])
    feed(eng, [(4.0, line("Phase transition: paused -> correcting", 4))])
    assert _now(eng, 5.0) == ("human_local", "human_initiated", "person:unidentified", None)


def test_policy_option_names_the_policy_when_the_log_does_not() -> None:
    eng = engine(policy_path=POLICY)
    feed(eng, [(1.0, line("Phase transition: paused -> autonomous", 1))])
    assert _now(eng, 2.0) == AI
    unnamed = engine()
    feed(unnamed, [(1.0, line("Phase transition: paused -> autonomous", 1))])
    assert _now(unnamed, 2.0) == ("ai", None, "ai:unidentified", None)


def test_continuous_dagger_episodes_are_tasks__defs_5__dossier_14_1_record_episode() -> None:
    eng = engine()
    out = _dagger(eng)
    assert _kinds(out) == ["state", "state", "task_start"]
    start = out[-1]
    assert start == TaskStart(
        "episode_000000",
        "pick_place_cube",
        (("x_episode_index", 0), ("x_src_ts", src(5)), ("x_src_clock", "wall")),
    )
    out = feed(eng, [(65.0, line("Episode saved (total: 1, elapsed: 60.0s)", 65))])
    assert out == [
        TaskEnd("episode_000000", "unknown", (("x_src_ts", src(65)), ("x_src_clock", "wall"))),
        TaskStart(
            "episode_000001",
            "pick_place_cube",
            (("x_episode_index", 1), ("x_src_ts", src(65)), ("x_src_clock", "wall")),
        ),
    ]
    out = feed(eng, [(70.0, line("DAgger continuous control loop ended — pausing engine", 70))])
    assert _kinds(out) == ["task_end"]


def test_a_resumed_dataset_numbers_episodes_after_the_existing_ones__defs_5() -> None:
    eng = engine()
    out = feed(
        eng,
        [
            (0.0, line("Dataset ready: op/rollout_x (12 existing episodes)")),
            (1.0, line("Sentry recording started (episode_duration=600s)", 1)),
        ],
    )
    starts = [m for m in out if isinstance(m, TaskStart)]
    assert [s.task_id for s in starts] == ["episode_000012"]


def test_corrections_only_episodes_are_not_tasks__defs_5__dossier_14_1_dagger() -> None:
    eng = engine()
    out = _dagger(eng, continuous=False)
    out += feed(
        eng,
        [
            (10.0, line("Phase transition: autonomous -> paused", 10)),
            (11.0, line("Phase transition: paused -> correcting", 11)),
            (15.0, line("Phase transition: correcting -> paused", 15)),
            (15.0, line("Correction 1/50 saved", 15)),
        ],
    )
    assert not any(isinstance(m, TaskStart | TaskEnd) for m in out)


def test_a_dagger_run_joined_midway_rotates_tasks_once_a_save_shows_it__defs_5() -> None:
    eng = engine(policy_path=POLICY)
    out = feed(
        eng,
        [
            (1.0, line("Phase transition: paused -> autonomous", 1)),
            (2.0, line("Correction 1/50 saved", 2)),
            (3.0, line("Episode saved (total: 4, elapsed: 600.0s)", 3)),
        ],
    )
    assert not any(isinstance(m, TaskStart) for m in out)
    joined = engine(policy_path=POLICY)
    out = feed(
        joined,
        [
            (1.0, line("Phase transition: paused -> autonomous", 1)),
            (3.0, line("Episode saved (total: 4, elapsed: 600.0s)", 3)),
        ],
    )
    assert [m.task_id for m in out if isinstance(m, TaskStart)] == ["episode_000004"]


def test_highlight_clips_are_not_tasks__defs_5() -> None:
    eng = engine()
    out = feed(
        eng,
        [
            (1.0, line("Highlight strategy recording started (press 's' to save)", 1)),
            (9.0, line("Episode saved (total: 1)", 9)),
        ],
    )
    assert not any(isinstance(m, TaskStart | TaskEnd) for m in out)
    assert _now(eng, 10.0) == ("ai", None, "ai:unidentified", None)


def test_episodic_episode_then_reset_by_the_leader_arm__defs_6_3__dossier_14_1_episodic_reset() -> (
    None
):
    eng = engine(reset_plan_item_id="reset")
    out = feed(eng, [*setup_lines(0.0), (5.0, line("Recording episode 0", 5))])
    assert _kinds(out) == ["state", "state", "task_start"]
    assert _states(out)[-1] == AI
    out = feed(eng, [(65.0, line("Reset the environment", 65))])
    assert _kinds(out) == ["task_end", "state"]
    assert out[0] == TaskEnd(
        "episode_000000", "unknown", (("x_src_ts", src(65)), ("x_src_clock", "wall"))
    )
    (reset,) = [m for m in out if isinstance(m, StateAssertion)]
    assert _value(reset) == ("human_local", "planned", "op_01", "so101_leader")
    assert reset.fields[0] == ("plan_item_id", "reset")
    out = feed(eng, [(125.0, line("Recording episode 1", 125))])
    assert _kinds(out) == ["state", "task_start"]
    assert _states(out) == [AI]


def test_a_reset_after_a_missed_episode_start_is_still_a_reset__defs_6_3() -> None:
    eng = engine()
    feed(eng, setup_lines(0.0))
    out = feed(eng, [(65.0, line("Reset the environment", 65))])
    assert _states(out) == [("human_local", "planned", "op_01", "so101_leader")]
    assert (
        lerobot.LeRobotEngine(settings()).observe(1.0, lerobot.Parsed(lerobot.ResetStart(), None))
        == []
    )


def test_episodic_reset_without_a_plan_item_is_human_initiated__defs_3_planned() -> None:
    eng = engine(reset_plan_item_id=None)
    feed(eng, [*setup_lines(0.0), (5.0, line("Recording episode 0", 5))])
    out = feed(eng, [(65.0, line("Reset the environment", 65))])
    assert _states(out) == [("human_local", "human_initiated", "op_01", "so101_leader")]


def test_episodic_reset_without_a_teleoperator_is_scripted_then_idle__defs_6_9_6_13_6_14() -> None:
    eng = engine()
    feed(eng, [*setup_lines(0.0, teleop=None), (5.0, line("Recording episode 0", 5))])
    out = feed(eng, [(65.0, line("Reset the environment", 65))])
    assert _states(out) == [SCRIPTED]
    assert _now(eng, 94.0) == SCRIPTED
    assert _now(eng, 95.0) == IDLE


def test_rerecord_key_during_the_episode_aborts_it__dossier_14_1_rerecord() -> None:
    eng = engine()
    feed(eng, [*setup_lines(0.0), (5.0, line("Recording episode 3", 5))])
    out = feed(
        eng,
        [
            (30.0, "Left arrow key pressed. Exiting loop and rerecord the last episode..."),
            (30.0, line("Reset the environment", 30)),
            (90.0, line("Re-record episode", 90)),
            (90.0, line("Recording episode 3", 90)),
        ],
    )
    ends = [m for m in out if isinstance(m, TaskEnd)]
    assert [(e.task_id, e.outcome) for e in ends] == [("episode_000003", "aborted")]
    assert not any(isinstance(m, lerobot.Note) for m in out)
    assert [m.task_id for m in out if isinstance(m, TaskStart)] == ["episode_000003_2"]


def test_rerecord_after_the_episode_closed_is_noted__dossier_14_1_rerecord() -> None:
    eng = engine()
    feed(eng, [*setup_lines(0.0), (5.0, line("Recording episode 3", 5))])
    out = feed(
        eng,
        [
            (65.0, line("Reset the environment", 65)),
            (70.0, "Left arrow key pressed. Exiting loop and rerecord the last episode..."),
            (70.0, line("Re-record episode", 70)),
        ],
    )
    assert [(e.task_id, e.outcome) for e in out if isinstance(e, TaskEnd)] == [
        ("episode_000003", "unknown")
    ]
    notes = [m for m in out if isinstance(m, lerobot.Note)]
    assert notes == [lerobot.Note((("x_lerobot_rerecorded_task", "episode_000003"),))]
    assert lerobot.wire_event(notes[0]) == {
        "type": "heartbeat",
        "x_lerobot_rerecorded_task": "episode_000003",
    }
    assert feed(eng, [(71.0, line("Re-record episode", 71))]) == []


def test_a_controller_keeps_its_state_after_the_loop_until_idle__defs_6_14() -> None:
    eng = engine()
    _dagger(eng)
    feed(
        eng,
        [
            (10.0, line("Phase transition: autonomous -> paused", 10)),
            (11.0, line("Phase transition: paused -> correcting", 11)),
            (20.0, line("DAgger continuous control loop ended — pausing engine", 20)),
            (21.0, line("Stopping inference engine...", 21)),
        ],
    )
    assert _now(eng, 49.0) == CORRECTING
    assert _now(eng, 50.0) == IDLE


def test_teardown_return_to_the_start_pose_is_scripted__defs_6_9_6_13__user_ruling_m8() -> None:
    eng = engine()
    _dagger(eng)
    out = feed(
        eng,
        [
            (20.0, line("DAgger continuous control loop ended — pausing engine", 20)),
            (21.0, line("Stopping inference engine...", 21)),
            (21.0, line("Returning robot to initial position before shutdown...", 21)),
        ],
    )
    assert _states(out) == [SCRIPTED]
    assert _now(eng, 23.0) == SCRIPTED
    feed(eng, [(24.0, line("Disconnecting robot...", 24))])
    assert _now(eng, 53.0) == SCRIPTED
    assert _now(eng, 54.0) == IDLE


def test_rollout_finished_is_unknown_and_closes_the_task__defs_2_unknown() -> None:
    eng = engine()
    _dagger(eng)
    out = feed(eng, [(30.0, line("Rollout finished", 30))])
    assert _kinds(out) == ["task_end", "state"]
    assert _states(out) == [UNKNOWN]
    (value,) = eng.tick(31.0)
    assert isinstance(value, StateAssertion)
    assert value.fields == (("x_src_ts", src(30)), ("x_src_clock", "wall"))
    assert feed(eng, [(32.0, line("Rollout finished", 32))]) == []


def test_a_new_rollout_closes_the_task_of_one_that_died__defs_5() -> None:
    eng = engine()
    _dagger(eng)
    out = feed(eng, setup_lines(100.0))
    assert _kinds(out) == ["task_end", "state"]
    assert _states(out) == [IDLE]
    out = feed(
        eng, [(105.0, line("DAgger continuous recording started (episode_duration=600s)", 105))]
    )
    assert [m.task_id for m in out if isinstance(m, TaskStart)] == ["episode_000000_2"]


def test_lines_after_the_end_start_a_new_rollout__defs_2() -> None:
    eng = engine()
    _dagger(eng)
    feed(eng, [(30.0, line("Rollout finished", 30))])
    out = feed(eng, [(40.0, line("Loading policy from 'x'...", 40))])
    assert _states(out) == [IDLE]


def test_log_time_travels_as_x_src_ts_with_clock_wall__defs_8_time() -> None:
    eng = engine()
    _dagger(eng)
    out = feed(eng, [(12.0, line("Phase transition: autonomous -> paused", 12))])
    (paused,) = out
    assert isinstance(paused, StateAssertion)
    assert paused.fields == (("x_src_ts", src(12)), ("x_src_clock", "wall"))
    assert lerobot.wire_event(paused) == {
        "type": "transition",
        "scope": "whole_robot",
        "state": "shared",
        "reason": "human_initiated",
        "actor_id": "op_01",
        "device_id": "so101_leader",
        "x_src_ts": src(12),
        "x_src_clock": "wall",
    }


def test_both_arms_of_a_bimanual_robot_take_the_same_state__dossier_14_7() -> None:
    eng = engine(scopes=("left_arm", "right_arm"))
    out = _dagger(eng)
    assert [m.scope for m in out if isinstance(m, StateAssertion)] == [
        "left_arm",
        "right_arm",
        "left_arm",
        "right_arm",
    ]
    assert len(eng.tick(6.0)) == 2


def test_stopping_the_watcher_makes_every_scope_unknown__defs_6_14() -> None:
    eng = engine(scopes=("left_arm", "right_arm"))
    _dagger(eng)
    assert [_value(m) for m in eng.stop() if isinstance(m, StateAssertion)] == [UNKNOWN, UNKNOWN]


def test_repeated_state_is_not_sent_again_but_tick_always_is() -> None:
    eng = engine()
    _dagger(eng)
    assert feed(eng, [(6.0, line("Phase transition: paused -> autonomous", 5))]) == []
    assert _now(eng, 7.0) == AI


def test_a_device_registry_without_the_sources_still_works__defs_6_10() -> None:
    lonely = (Device("other", "ai", (("node", "/nav"),)),)
    eng = engine(devices=lonely)
    _dagger(eng)
    assert _now(eng, 6.0) == ("ai", None, AI[2], None)
    assert DEVICES[0].device_id == "so101_policy"
