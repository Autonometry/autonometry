"""Autonometry station observer: the teleoperation station's witness (README, "Station witness").

Runs on the operator's PC, beside an Autonometry recorder configured with ``role = "station"``
and its own key (definitions.md 12.1). A ROS 2 node that only subscribes, like the robot's
observer: it never publishes, calls a service or sends an action goal. It watches what the
operator's own input sends: ``/cmd_vel`` from ``teleop_twist_keyboard`` (or ``teleop_twist_joy``)
on this PC and ``/joy`` from the PC's joystick, and asserts ``human_remote`` while that input is
live, ``unknown`` when the joystick stops reporting, ``idle`` otherwise
(:mod:`autonometry.connectors.station`).

Which publisher sent a ``/cmd_vel`` comes from the ROS graph: a topic with one publisher is
attributed to it; with several, the command counts as the station's only when a station device
is among them and it does not relay Nav2's latest ``/cmd_vel_smoothed``, and the state is marked
``x_attribution_inferred``. A station device is a ``human`` device of the station's own registry,
matched by node name.
"""

import math
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import rclpy
import rclpy.executors
from geometry_msgs.msg import Twist, TwistStamped
from rclpy.node import Node
from sensor_msgs.msg import Joy

from autonometry.connectors import station, tb4
from autonometry.registry import MAX_REGISTRY_BYTES, check_registry_bytes
from autonometry.spec import SpecError, load_rules
from autonometry_ros.autonometry_client import Recorder
from autonometry_ros.observer import MAX_BUTTONS, MAX_ENDPOINTS, SENSOR_QOS

#: Most joystick axes read per message.
MAX_AXES = 16


class StationObserver(Node):
    """Subscribes to the station's own input topics; see the module docstring."""

    def __init__(self) -> None:
        super().__init__(
            "autonometry_station_observer", enable_rosout=False, start_parameter_services=False
        )
        defaults: dict[str, Any] = {
            "recorder_socket": "/run/autonometry/station.sock",
            "client_name": "station_observer",
            "device_registry": "/etc/autonometry/station-devices.json",
            "spec_version": "0.3",
            "scopes": ["base"],
            "joy_node": "joy_node",
            "dead_zone": 0.05,
            "deadman_buttons": [4, 5],
            "unidentified_actor_id": "person:unidentified",
            "tick_period_s": 0.25,
            "topics.cmd_vel": "cmd_vel",
            "topics.cmd_vel_unstamped": "cmd_vel_unstamped",
            "topics.cmd_vel_smoothed": "cmd_vel_smoothed",
            "topics.joy": "joy",
        }
        for name, value in defaults.items():
            self.declare_parameter(name, value)
        self._topics = {
            key: self.resolve_topic_name(self._param(f"topics.{key}"))
            for key in ("cmd_vel", "cmd_vel_unstamped", "cmd_vel_smoothed", "joy")
        }
        self._engine = station.StationEngine(self._settings())
        self._recorder = Recorder(self._param("recorder_socket"), self._param("client_name"))
        self._publishers: dict[str, tuple[tb4.Source, ...]] = {}
        self._counts: dict[str, int] = {}
        topics = self._topics
        self._sub(TwistStamped, topics["cmd_vel"], lambda m: self._cmd_vel("cmd_vel", m.twist))
        self._sub(
            Twist, topics["cmd_vel_unstamped"], lambda m: self._cmd_vel("cmd_vel_unstamped", m)
        )
        self._sub(TwistStamped, topics["cmd_vel_smoothed"], self._on_smoothed)
        self._sub(Joy, topics["joy"], self._on_joy)
        self.create_timer(float(self._param("tick_period_s")), self._guard(self._tick))
        self.get_logger().info(
            f"witnessing for the station recorder at {self._param('recorder_socket')}"
        )

    def _param(self, name: str) -> Any:
        return self.get_parameter(name).value

    def _node(self, name: str) -> tb4.Source:
        if name.startswith("/"):
            return tb4.Source(node=name)
        return tb4.Source(node=f"{self.get_namespace().rstrip('/')}/{name}")

    def _settings(self) -> station.StationSettings:
        path = Path(self._param("device_registry"))
        with path.open("rb") as stream:
            data = stream.read(MAX_REGISTRY_BYTES + 1)
        version = str(self._param("spec_version"))
        check = check_registry_bytes(data, str(path), version, "devices")
        if check.document is None:
            raise ValueError(f"device registry {path} is not valid: {check.problems[0]}")
        return station.StationSettings.from_params(
            load_rules(version).params,
            tb4.devices_from_registry(check.document),
            scopes=tuple(str(s) for s in self._param("scopes")),
            dead_zone=float(self._param("dead_zone")),
            deadman_buttons=tuple(int(i) for i in self._param("deadman_buttons")),
            joy_source=self._node(str(self._param("joy_node"))),
            unidentified_actor_id=str(self._param("unidentified_actor_id")),
        )

    def _sub(self, kind: Any, topic: str, callback: Callable[[Any], None]) -> None:
        self.create_subscription(kind, topic, self._guard(callback), SENSOR_QOS)

    def _guard(self, callback: Callable[..., None]) -> Callable[..., None]:
        """Run a callback; an error is logged and never stops the node."""

        def guarded(*args: Any) -> None:
            try:
                callback(*args)
            except Exception as exc:  # the process boundary: log and keep observing
                self.get_logger().error(f"{type(exc).__name__}: {exc}", throttle_duration_sec=10.0)

        return guarded

    def _send(self, messages: Iterable[tb4.StateAssertion]) -> None:
        for message in messages:
            event = tb4.wire_event(message)
            self._recorder.event(event.pop("type"), **event)

    def _tick(self) -> None:
        self._send(self._engine.tick(time.monotonic()))

    def _attribution(self, key: str) -> tuple[tb4.Source | None, tuple[tb4.Source, ...]]:
        """The publisher when it is the topic's only one, and the topic's publishers."""
        topic = self._topics[key]
        if self.count_publishers(topic) != self._counts.get(topic):
            infos = self.get_publishers_info_by_topic(topic)[:MAX_ENDPOINTS]
            self._publishers[topic] = tuple(
                sorted(
                    (self._node(f"{i.node_namespace.rstrip('/')}/{i.node_name}") for i in infos),
                    key=lambda s: s.node or "",
                )
            )
            self._counts[topic] = len(infos)
        found = self._publishers.get(topic, ())
        return (found[0] if len(found) == 1 else None), found

    def _cmd_vel(self, key: str, twist: Any) -> None:
        linear, angular = twist.linear, twist.angular
        if not all(math.isfinite(v) for v in (linear.x, linear.y, angular.z)):
            return
        source, candidates = self._attribution(key)
        now = time.monotonic()
        self._send(
            self._engine.observe(tb4.CmdVel(now, linear.x, linear.y, angular.z, source, candidates))
        )

    def _on_smoothed(self, msg: TwistStamped) -> None:
        linear, angular = msg.twist.linear, msg.twist.angular
        if all(math.isfinite(v) for v in (linear.x, linear.y, angular.z)):
            now = time.monotonic()
            self._send(self._engine.observe(tb4.CmdVelSmoothed(now, linear.x, linear.y, angular.z)))

    def _on_joy(self, msg: Joy) -> None:
        axes = tuple(float(a) for a in list(msg.axes)[:MAX_AXES])
        buttons = tuple(int(b) for b in list(msg.buttons)[:MAX_BUTTONS])
        self._send(self._engine.observe(station.StationJoy(time.monotonic(), axes, buttons)))


def main(args: list[str] | None = None) -> int:
    """Run the station observer until interrupted. Exit status 0 on Ctrl-C, 2 when it cannot
    start."""
    rclpy.init(args=args)
    try:
        node = StationObserver()
    except (OSError, ValueError, SpecError) as exc:
        print(f"autonometry station observer: cannot start: {exc}", file=sys.stderr)
        rclpy.try_shutdown()
        return 2
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
