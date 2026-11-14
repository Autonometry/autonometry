"""The teleoperation station engine (definitions.md 12.1, 6.11, 6.12, 6.14)."""

import pytest

from autonometry import spec
from autonometry.connectors.station import StationEngine, StationJoy, StationSettings
from autonometry.connectors.tb4 import CmdVel, CmdVelSmoothed, Device, Source, StateAssertion

KEYBOARD = Device("kbd_pc_1", "human", (("node", "/teleop_twist_keyboard"),), "remote", "op_01")
JOYSTICK = Device("joy_pc_1", "human", (("node", "/joy_node"),), "remote", "op_02")
SECOND = Device("kbd_pc_2", "human", (("node", "/teleop_2"),), "remote")
NAV2 = Device("nav2", "ai", (("node", "/collision_monitor"),))
KBD, RELAY = Source(node="/teleop_twist_keyboard"), Source(node="/collision_monitor")
RULES = spec.load_rules("0.3")


def engine(**options: object) -> StationEngine:
    settings = StationSettings.from_params(
        RULES.params, (KEYBOARD, JOYSTICK, SECOND, NAV2), **options
    )
    return StationEngine(settings)


def states(messages: list[StateAssertion]) -> list[tuple[str, str]]:
    return [(m.scope, m.state) for m in messages]


def test_live_keyboard_is_human_remote_until_goal_release__defs_12_1_6_11() -> None:
    station = engine()
    (value,) = station.observe(CmdVel(10.0, 0.2, 0.0, 0.0, KBD, (KBD,)))
    assert value == StateAssertion("base", "human_remote", "human_initiated", "op_01", "kbd_pc_1")
    assert station.observe(CmdVel(10.5, 0.2, 0.0, 0.0, KBD, (KBD,))) == []
    assert states(station.tick(11.5)) == [("base", "human_remote")]
    assert states(station.tick(11.6)) == [("base", "idle")]


def test_neutral_command_ends_input_at_once__defs_12_1_6_11() -> None:
    station = engine()
    station.observe(CmdVel(10.0, 0.2, 0.0, 0.0, KBD, (KBD,)))
    assert states(station.observe(CmdVel(10.2, 0.0, 0.0, 0.0, KBD, (KBD,)))) == [("base", "idle")]


def test_commands_of_other_sources_are_not_the_stations__defs_12_1() -> None:
    station = engine()
    assert states(station.observe(CmdVel(1.0, 0.3, 0.0, 0.0, RELAY, (RELAY,)))) == [
        ("base", "idle")
    ]
    assert station.observe(CmdVel(2.0, 0.3, 0.0, 0.0, None, (RELAY, Source(node="/x")))) == []


def test_unattributed_command_with_a_station_publisher_is_inferred__defs_12_1_6_12() -> None:
    station = engine()
    (value,) = station.observe(CmdVel(5.0, 0.3, 0.0, 0.0, None, (RELAY, KBD)))
    assert (value.state, value.device_id) == ("human_remote", "kbd_pc_1")
    assert value.fields == (("x_attribution_inferred", True),)


def test_relayed_nav2_command_is_not_the_stations__defs_12_1() -> None:
    station = engine()
    assert station.observe(CmdVelSmoothed(5.0, 0.3, 0.0, 0.1)) == [StateAssertion("base", "idle")]
    assert station.observe(CmdVel(5.1, 0.15, 0.0, 0.05, None, (RELAY, KBD))) == []
    assert states(station.tick(5.2)) == [("base", "idle")]


def test_two_station_publishers_leave_the_device_open__defs_12_1() -> None:
    station = engine()
    teleop_2 = Source(node="/teleop_2")
    (value,) = station.observe(CmdVel(5.0, 0.3, 0.0, 0.0, None, (KBD, teleop_2)))
    assert (value.actor_id, value.device_id) == ("person:unidentified", None)


def test_joystick_needs_deadman_and_a_stick__defs_12_1_6_11() -> None:
    station = engine()
    assert states(station.observe(StationJoy(1.0, (0.5, 0.0), (0, 0, 0, 0, 0, 0)))) == [
        ("base", "idle")
    ]
    (value,) = station.observe(StationJoy(1.1, (0.5, 0.0), (0, 0, 0, 0, 1, 0)))
    assert (value.state, value.device_id, value.actor_id) == ("human_remote", "joy_pc_1", "op_02")
    assert states(station.observe(StationJoy(1.2, (0.01, 0.0), (0, 0, 0, 0, 1, 0)))) == [
        ("base", "idle")
    ]


def test_joystick_silence_is_lost_input__defs_12_1_6_14() -> None:
    station = engine()
    station.observe(StationJoy(1.0, (0.0,), (0,)))
    assert states(station.tick(3.0)) == [("base", "idle")]
    (lost,) = station.tick(3.1)
    assert (lost.state, lost.fields) == ("unknown", (("x_input_lost", "joy"),))
    assert states(station.observe(StationJoy(4.0, (0.0,), (0,)))) == [("base", "idle")]


def test_every_witnessed_scope_gets_the_state__defs_12_1() -> None:
    station = engine(scopes=("base", "arm"))
    assert states(station.tick(0.0)) == [("base", "idle"), ("arm", "idle")]
    assert states(station.observe(CmdVel(1.0, 0.2, 0.0, 0.0, KBD, (KBD,)))) == [
        ("base", "human_remote"),
        ("arm", "human_remote"),
    ]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"scopes": ()}, "scopes"),
        ({"dead_zone": 0.0}, "dead_zone"),
        ({"smoothed_match_s": float("nan")}, "smoothed_match_s"),
    ],
)
def test_settings_are_checked(options: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        engine(**options)
