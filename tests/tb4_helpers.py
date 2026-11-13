"""Shared helpers for the TurtleBot 4 connector tests. Not collected by pytest (no ``test_``)."""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from autonometry.connectors import tb4
from autonometry.spec import load_rules
from recorder_helpers import Client

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "demo" / "tb4-sim" / "config"
GOLDEN = Path(__file__).resolve().parent / "golden" / "tb4"
RULES = load_rules("0.3")
DEVICES = tb4.devices_from_registry(
    json.loads((CONFIG / "devices.json").read_text(encoding="utf-8"))
)

NAV2 = tb4.Source(node="/collision_monitor")
KBD = tb4.Source(node="/teleop_twist_keyboard")
JOY = tb4.Source(node="/teleop_twist_joy_node")
RVIZ = tb4.Source(node="/rviz2")
TB4_NODE = tb4.Source(node="/turtlebot4_node")
MISSION = tb4.Source(node="/basic_navigator")
STRANGER = tb4.Source(node="/someone_else")
BT_NAVIGATOR = tb4.Source(node="/bt_navigator")
CONTROLLER = tb4.Source(node="/controller_server")
BEHAVIORS = tb4.Source(node="/behavior_server")
SMOOTHER = tb4.Source(node="/velocity_smoother")
WAYPOINTS = tb4.Source(node="/waypoint_follower")
HIDDEN = tb4.Source(node="/nav_helper")
BUTTON_2 = tb4.Source(topic="/interface_buttons/button_2")
POWER = tb4.Source(topic="/interface_buttons/button_power")
CMD_PUBLISHERS = (NAV2, KBD)
GOAL = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
GOAL_2 = "b1b2c3d4e5f60718293a4b5c6d7e8f90"
ROUTINE_GOAL = "0f0e0d0c0b0a09080706050403020100"

_TYPES: dict[str, type[Any]] = {
    cls.__name__: cls
    for cls in (
        tb4.CmdVel,
        tb4.CmdVelSmoothed,
        tb4.ChainPublishers,
        tb4.ActionStatus,
        tb4.CollisionMonitorState,
        tb4.FunctionCall,
        tb4.ButtonEdge,
        tb4.Joy,
        tb4.WheelStatus,
        tb4.DockStatus,
        tb4.KidnapStatus,
        tb4.InitialPose,
        tb4.Odometry,
    )
}


def settings(**options: Any) -> tb4.Settings:
    """Settings from the spec's parameters and the demo's device registry."""
    devices = options.pop("devices", DEVICES)
    return tb4.Settings.from_params(RULES.params, devices, **options)


def engine(**options: Any) -> tb4.Tb4Engine:
    return tb4.Tb4Engine(settings(**options))


def observation(record: dict[str, Any]) -> tb4.Observation:
    """An observation from its JSON form: ``kind`` names the class; sources are objects."""
    fields = dict(record)
    cls = _TYPES[fields.pop("kind")]
    if fields.get("source") is not None:
        fields["source"] = tb4.Source(**fields["source"])
    for name in ("candidates", "requesters", "publishers"):
        if name in fields:
            fields[name] = tuple(tb4.Source(**s) for s in fields[name])
    if "goals" in fields:
        fields["goals"] = tuple(tb4.GoalStatus(**g) for g in fields["goals"])
    if "buttons" in fields:
        fields["buttons"] = tuple(fields["buttons"])
    return cls(**fields)  # type: ignore[no-any-return]


def load_observations(path: Path) -> list[tb4.Observation]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [observation(json.loads(line)) for line in lines if line.strip()]


def datagrams(client: Client, messages: Iterable[tb4.Message]) -> list[bytes]:
    """The datagrams the ROS node sends for ``messages``, in order."""
    out = []
    for message in messages:
        if isinstance(message, tb4.Distance):
            out.append(client.distance(message.metres))
        else:
            event = tb4.wire_event(message)
            out.append(client.event(event.pop("type"), **event))
    return out


def states(messages: Iterable[tb4.Message]) -> list[str]:
    return [m.state for m in messages if isinstance(m, tb4.StateAssertion)]


def last_state(messages: Iterable[tb4.Message]) -> tb4.StateAssertion:
    found = [m for m in messages if isinstance(m, tb4.StateAssertion)]
    assert found, "no state assertion"
    return found[-1]


def nav(
    t: float, status: int, goal: str = GOAL, requesters: tuple[tb4.Source, ...] = (RVIZ,)
) -> tb4.ActionStatus:
    return tb4.ActionStatus(t, tb4.NAVIGATE, (tb4.GoalStatus(goal, status),), requesters)


def cmd(t: float, vx: float, source: tb4.Source | None = None, wz: float = 0.0) -> tb4.CmdVel:
    """A command; with no ``source``, the keyboard and Nav2 both publish on ``/cmd_vel``."""
    if source is None:
        return tb4.CmdVel(t, vx, 0.0, wz, None, CMD_PUBLISHERS)
    return tb4.CmdVel(t, vx, 0.0, wz, source)


def nav2_cmd(t: float, vx: float, relayed: float | None = None) -> list[tb4.Observation]:
    """Nav2's smoothed command and the collision monitor's relay of it (scaled by ``relayed``)."""
    out_vx = vx if relayed is None else relayed
    return [
        tb4.CmdVelSmoothed(t, vx, 0.0, 0.0),
        tb4.CmdVel(t, out_vx, 0.0, 0.0, None, CMD_PUBLISHERS),
    ]


def chain(t: float, *smoothed_extra: tb4.Source) -> list[tb4.Observation]:
    """Nav2's declared chain in the ROS graph, with ``smoothed_extra`` also publishing on
    ``cmd_vel_smoothed``."""
    return [
        tb4.ChainPublishers(t, "cmd_vel_nav", (CONTROLLER, BEHAVIORS)),
        tb4.ChainPublishers(t, "cmd_vel_smoothed", (SMOOTHER, *smoothed_extra)),
    ]
