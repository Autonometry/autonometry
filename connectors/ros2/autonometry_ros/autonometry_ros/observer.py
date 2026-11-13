"""Autonometry observer for the TurtleBot 4 (README, "TurtleBot 4").

A ROS 2 node that only subscribes: it never publishes on a robot topic, never calls a service and
never sends an action goal, so it cannot slow or stop the robot, and a crash of this process
leaves the robot as it was. Each message becomes an observation of
:mod:`autonometry.connectors.tb4`, stamped with this process's monotonic receive time; the
engine's output goes to the Autonometry recorder through the fire-and-forget client, which never
blocks or raises.

Publisher identity: on ROS 2 Jazzy a subscription's message info carries timestamps and sequence
numbers but no publisher GID, so the node maps publishers from the ROS graph, refreshed when the
publisher count changes and every ``graph_period_s``. A topic with one publisher is attributed to
it; with several, the engine applies the topic-level rule and marks its states
``x_attribution_inferred``. Where a newer rclpy gives ``publisher_gid``, it is used. Who sent an
action goal comes from the action's clients in the graph. The publisher map is recorded as
``x_publishers`` on a heartbeat each time it changes.
"""

import contextlib
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import rclpy
import rclpy.executors
from action_msgs.msg import GoalStatusArray
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist, TwistStamped
from nav_msgs.msg import Odometry
from rclpy.action import graph as action_graph
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Joy
from std_msgs.msg import String

from autonometry.connectors import tb4
from autonometry.registry import MAX_REGISTRY_BYTES, check_registry_bytes
from autonometry.spec import SpecError, load_rules
from autonometry_ros.autonometry_client import Recorder

#: Longest ``/function_calls`` text kept, and most goals or joystick buttons read per message.
MAX_TEXT = 64
MAX_GOALS = 64
MAX_BUTTONS = 32
#: Most publishers or clients recorded per topic or action.
MAX_ENDPOINTS = 64
#: ROS domain ids are 0 to 232.
MAX_DOMAIN_DIGITS = 3

#: Sensor-style topics: best effort, which also matches reliable publishers.
SENSOR_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=50,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)
#: Change-only topics, where a lost message is a lost transition: reliable.
EVENT_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=50,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)
#: Action status: transient local, so a late joiner gets the current goals at once.
STATUS_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

#: Action name (relative) for each action the engine follows.
ACTIONS = (tb4.NAVIGATE, "dock", "undock", "wall_follow")
#: ``/interface_buttons`` fields, as they appear in device registry topics.
CREATE3_BUTTONS = ("button_1", "button_2", "button_power")

#: Topic parameters: name and default (relative to the robot namespace).
TOPICS = {
    "cmd_vel": "cmd_vel",
    "cmd_vel_unstamped": "cmd_vel_unstamped",
    "cmd_vel_smoothed": "cmd_vel_smoothed",
    "collision_monitor_state": "collision_monitor_state",
    "function_calls": "function_calls",
    "interface_buttons": "interface_buttons",
    "hmi_buttons": "hmi/buttons",
    "joy": "joy",
    "wheel_status": "wheel_status",
    "dock_status": "dock_status",
    "kidnap_status": "kidnap_status",
    "initialpose": "initialpose",
    "odom": "odom",
}


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def _fqn(name: str, namespace: str) -> str:
    return f"{namespace.rstrip('/')}/{name}"


def _gid_hex(gid: Iterable[int] | bytes) -> str:
    return bytes(gid).hex()


class Observer(Node):
    """Subscribes to the TurtleBot 4's control topics; see the module docstring."""

    def __init__(self) -> None:
        # No /rosout publisher and no parameter services: the node offers nothing to the graph.
        super().__init__(
            "autonometry_observer", enable_rosout=False, start_parameter_services=False
        )
        self._declare()
        param = self._param
        self._topics = {key: self.resolve_topic_name(param(f"topics.{key}")) for key in TOPICS}
        self._actions = {a: self.resolve_topic_name(a) for a in ACTIONS}
        self._engine = tb4.Tb4Engine(self._settings())
        self._recorder = Recorder(param("recorder_socket"), param("client_name"))
        self._publishers: dict[str, tuple[tuple[tb4.Source, str], ...]] = {}
        self._counts: dict[str, int] = {}
        self._requesters: dict[str, tuple[tb4.Source, ...]] = {}
        self._recorded: str | None = None
        self._buttons: dict[str, bool] = {}
        self._subscribe()
        self.create_timer(float(param("tick_period_s")), self._guard(self._tick))
        self.create_timer(float(param("graph_period_s")), self._guard(self._refresh_graph))
        self._refresh_graph()
        self.get_logger().info(
            f"observing {param('scope')} for the recorder at {param('recorder_socket')}"
        )

    # -- configuration ----------------------------------------------------------------------

    def _declare(self) -> None:
        defaults: dict[str, Any] = {
            "recorder_socket": "/run/autonometry/recorder.sock",
            "client_name": "tb4_observer",
            "device_registry": "/etc/autonometry/devices.json",
            "spec_version": "0.3",
            "hint_policy": "hint_as_shared",
            "scope": "base",
            "nav_task_definition_id": "nav_to_pose",
            "initial_pose_plan_item_id": "initial_pose",
            "nav2_actor_id": "ai:nav2",
            "nav2_node": "collision_monitor",
            "function_caller_node": "turtlebot4_node",
            "joy_teleop_node": "teleop_twist_joy_node",
            "unidentified_actor_id": "person:unidentified",
            "trigger_window_s": 2.0,
            "kidnap_min_s": 1.0,
            "deadman_buttons": [4, 5],
            "tick_period_s": 0.25,
            "graph_period_s": 1.0,
        }
        for name, value in defaults.items():
            self.declare_parameter(name, value)
        for key, topic in TOPICS.items():
            self.declare_parameter(f"topics.{key}", topic)

    def _param(self, name: str) -> Any:
        return self.get_parameter(name).value

    def _node_source(self, name: str) -> tb4.Source:
        """A node named relative to the robot namespace, as the graph reports it."""
        if name.startswith("/"):
            return tb4.Source(node=name)
        return tb4.Source(node=_fqn(name, self.get_namespace()))

    def _settings(self) -> tb4.Settings:
        param = self._param
        path = Path(param("device_registry"))
        with path.open("rb") as stream:
            data = stream.read(MAX_REGISTRY_BYTES + 1)
        version = str(param("spec_version"))
        check = check_registry_bytes(data, str(path), version, "devices")
        if check.document is None:
            raise ValueError(f"device registry {path} is not valid: {check.problems[0]}")
        rules = load_rules(version)
        return tb4.Settings.from_params(
            rules.params,
            tb4.devices_from_registry(check.document),
            hint_policy=str(param("hint_policy")),
            scope=str(param("scope")),
            nav_task_definition_id=str(param("nav_task_definition_id")),
            initial_pose_plan_item_id=str(param("initial_pose_plan_item_id")),
            nav2_actor_id=str(param("nav2_actor_id")),
            nav2_source=self._node_source(str(param("nav2_node"))),
            function_caller=self._node_source(str(param("function_caller_node"))),
            joy_teleop=self._node_source(str(param("joy_teleop_node"))),
            kidnap_source=tb4.Source(topic=self._topic_name("kidnap_status")),
            unidentified_actor_id=str(param("unidentified_actor_id")),
            trigger_window_s=float(param("trigger_window_s")),
            kidnap_min_s=float(param("kidnap_min_s")),
            deadman_buttons=tuple(int(i) for i in param("deadman_buttons")),
        )

    def _topic_name(self, key: str) -> str:
        return self.resolve_topic_name(self._param(f"topics.{key}"))

    # -- subscriptions ----------------------------------------------------------------------

    def _subscribe(self) -> None:
        topics, sub = self._topics, self._sub
        sub(TwistStamped, topics["cmd_vel"], SENSOR_QOS, self._on_cmd_vel_stamped, info=True)
        sub(Twist, topics["cmd_vel_unstamped"], SENSOR_QOS, self._on_cmd_vel_unstamped, info=True)
        sub(TwistStamped, topics["cmd_vel_smoothed"], SENSOR_QOS, self._on_smoothed)
        sub(String, topics["function_calls"], EVENT_QOS, self._on_function_call)
        sub(Joy, topics["joy"], SENSOR_QOS, self._on_joy)
        sub(
            PoseWithCovarianceStamped,
            topics["initialpose"],
            SENSOR_QOS,
            self._on_initial_pose,
            info=True,
        )
        sub(Odometry, topics["odom"], SENSOR_QOS, self._on_odom)
        for action, name in self._actions.items():
            sub(
                GoalStatusArray,
                f"{name}/_action/status",
                STATUS_QOS,
                lambda msg, action=action: self._on_status(action, msg),
            )
        self._subscribe_optional()

    def _subscribe_optional(self) -> None:
        """Topics whose message packages may be missing (a TurtleBot 4 Lite has no HMI)."""
        topics, sub, log = self._topics, self._sub, self.get_logger()
        try:
            from nav2_msgs.msg import CollisionMonitorState  # noqa: PLC0415
        except ImportError:
            log.warning("nav2_msgs is not installed: collision monitor stops are not observed")
        else:
            sub(CollisionMonitorState, topics["collision_monitor_state"], EVENT_QOS, self._on_cm)
        try:
            from irobot_create_msgs.msg import (  # noqa: PLC0415
                DockStatus,
                InterfaceButtons,
                KidnapStatus,
                WheelStatus,
            )
        except ImportError:
            log.warning("irobot_create_msgs is not installed: buttons, wheels, dock not observed")
        else:
            sub(InterfaceButtons, topics["interface_buttons"], SENSOR_QOS, self._on_buttons)
            sub(WheelStatus, topics["wheel_status"], SENSOR_QOS, self._on_wheels)
            sub(DockStatus, topics["dock_status"], SENSOR_QOS, self._on_dock)
            sub(KidnapStatus, topics["kidnap_status"], SENSOR_QOS, self._on_kidnap)
        try:
            from turtlebot4_msgs.msg import UserButton  # noqa: PLC0415
        except ImportError:
            log.warning("turtlebot4_msgs is not installed: HMI buttons are not observed")
        else:
            sub(UserButton, topics["hmi_buttons"], SENSOR_QOS, self._on_hmi)

    def _sub(
        self,
        kind: Any,
        topic: str,
        qos: QoSProfile,
        callback: Callable[..., None],
        info: bool = False,
    ) -> None:
        """Subscribe; with ``info``, the callback also gets the message info, which rclpy passes
        only to a callback that requires two arguments."""
        guarded = self._guard(callback)

        def with_info(msg: Any, meta: Any) -> None:
            guarded(msg, meta)

        def message_only(msg: Any) -> None:
            guarded(msg)

        self.create_subscription(kind, topic, with_info if info else message_only, qos)

    def _guard(self, callback: Callable[..., None]) -> Callable[..., None]:
        """Run a callback; an error is logged and never stops the node."""

        def guarded(*args: Any) -> None:
            try:
                callback(*args)
            except Exception as exc:  # the process boundary: log and keep observing
                self.get_logger().error(
                    f"{getattr(callback, '__name__', 'callback')}: {type(exc).__name__}: {exc}",
                    throttle_duration_sec=10.0,
                )

        return guarded

    # -- engine -----------------------------------------------------------------------------

    def _observe(self, observation: tb4.Observation) -> None:
        self._send(self._engine.observe(observation))

    def _tick(self) -> None:
        self._send(self._engine.tick(time.monotonic()))

    def _send(self, messages: Iterable[tb4.Message]) -> None:
        for message in messages:
            if isinstance(message, tb4.Distance):
                self._recorder.distance(message.metres)
            else:
                event = tb4.wire_event(message)
                self._recorder.event(event.pop("type"), **event)

    # -- identities -------------------------------------------------------------------------

    def _refresh_graph(self) -> None:
        for key in ("cmd_vel", "cmd_vel_unstamped", "cmd_vel_smoothed", "initialpose"):
            self._refresh_topic(self._topics[key])
        for action in ACTIONS:
            self._refresh_requesters(action)
        self._record_publishers()

    def _refresh_topic(self, topic: str) -> None:
        infos = self.get_publishers_info_by_topic(topic)[:MAX_ENDPOINTS]
        self._publishers[topic] = tuple(
            sorted(
                (
                    (tb4.Source(node=_fqn(i.node_name, i.node_namespace)), _gid_hex(i.endpoint_gid))
                    for i in infos
                ),
                key=lambda item: (item[0].node or "", item[1]),
            )
        )
        self._counts[topic] = len(infos)

    def _refresh_requesters(self, action: str) -> None:
        name, me = self._actions[action], self.get_fully_qualified_name()
        found = []
        for node, namespace in self.get_node_names_and_namespaces():
            fqn = _fqn(node, namespace)
            if fqn == me:
                continue
            clients = action_graph.get_action_client_names_and_types_by_node(self, node, namespace)
            if any(client == name for client, _ in clients):
                found.append(tb4.Source(node=fqn))
        self._requesters[action] = tuple(sorted(found, key=lambda s: s.node or ""))[:MAX_ENDPOINTS]

    def _record_publishers(self) -> None:
        """Send ``x_publishers`` on a heartbeat when the map changed (notes B.1 item 3)."""
        publishers = {
            topic: [{"node": source.node, "gid": gid} for source, gid in found]
            for topic, found in sorted(self._publishers.items())
        }
        text = json.dumps(publishers, sort_keys=True)
        if text == self._recorded:
            return
        self._recorded = text
        extra: dict[str, Any] = {"x_publishers": publishers}
        with contextlib.suppress(ImportError):
            from rclpy.utilities import get_rmw_implementation_identifier  # noqa: PLC0415

            extra["x_rmw"] = get_rmw_implementation_identifier()
        domain = os.environ.get("ROS_DOMAIN_ID", "0")
        valid = domain.isdigit() and len(domain) <= MAX_DOMAIN_DIGITS
        extra["x_ros_domain_id"] = int(domain) if valid else None
        self._recorder.event("heartbeat", **extra)

    def _attribution(
        self, topic: str, info: Any
    ) -> tuple[tb4.Source | None, tuple[tb4.Source, ...]]:
        """The publisher of a message, or the topic's publishers when it cannot be told."""
        if self.count_publishers(topic) != self._counts.get(topic):
            self._refresh_topic(topic)
            self._record_publishers()
        found = self._publishers.get(topic, ())
        gid = info.get("publisher_gid") if isinstance(info, dict) else None
        if isinstance(gid, dict) and isinstance(gid.get("data"), bytes | bytearray):
            wanted = bytes(gid["data"]).hex()
            for source, known in found:
                if wanted.startswith(known) or known.startswith(wanted):
                    return source, ()
        sources = tuple(source for source, _ in found)
        if len(sources) == 1:
            return sources[0], ()
        return None, sources

    # -- callbacks --------------------------------------------------------------------------

    def _cmd_vel_with_info(self, topic: str, twist: Any, info: Any) -> None:
        linear, angular = twist.linear, twist.angular
        if not _finite(linear.x, linear.y, angular.z):
            return
        source, candidates = self._attribution(topic, info)
        now = time.monotonic()
        self._observe(tb4.CmdVel(now, linear.x, linear.y, angular.z, source, candidates))

    def _on_cmd_vel_stamped(self, msg: TwistStamped, info: Any = None) -> None:
        self._cmd_vel_with_info(self._topics["cmd_vel"], msg.twist, info)

    def _on_cmd_vel_unstamped(self, msg: Twist, info: Any = None) -> None:
        self._cmd_vel_with_info(self._topics["cmd_vel_unstamped"], msg, info)

    def _on_smoothed(self, msg: TwistStamped) -> None:
        linear, angular = msg.twist.linear, msg.twist.angular
        if _finite(linear.x, linear.y, angular.z):
            self._observe(tb4.CmdVelSmoothed(time.monotonic(), linear.x, linear.y, angular.z))

    def _on_initial_pose(self, msg: PoseWithCovarianceStamped, info: Any = None) -> None:
        source, candidates = self._attribution(self._topics["initialpose"], info)
        self._observe(tb4.InitialPose(time.monotonic(), source, candidates))

    def _on_status(self, action: str, msg: GoalStatusArray) -> None:
        self._refresh_requesters(action)
        goals = tuple(
            tb4.GoalStatus(_gid_hex(s.goal_info.goal_id.uuid), int(s.status))
            for s in msg.status_list[:MAX_GOALS]
        )
        now = time.monotonic()
        self._observe(tb4.ActionStatus(now, action, goals, self._requesters.get(action, ())))

    def _on_cm(self, msg: Any) -> None:
        self._observe(tb4.CollisionMonitorState(time.monotonic(), int(msg.action_type)))

    def _on_function_call(self, msg: String) -> None:
        self._observe(tb4.FunctionCall(time.monotonic(), str(msg.data)[:MAX_TEXT]))

    def _edges(self, topic: str, pressed: dict[str, bool]) -> None:
        now = time.monotonic()
        for field, down in pressed.items():
            name = f"{topic}/{field}"
            if self._buttons.get(name, False) != down:
                self._buttons[name] = down
                self._observe(tb4.ButtonEdge(now, tb4.Source(topic=name), down))

    def _on_buttons(self, msg: Any) -> None:
        pressed = {name: bool(getattr(msg, name).is_pressed) for name in CREATE3_BUTTONS}
        self._edges(self._topics["interface_buttons"], pressed)

    def _on_hmi(self, msg: Any) -> None:
        pressed = {str(i + 1): bool(b) for i, b in enumerate(list(msg.button)[:4])}
        self._edges(self._topics["hmi_buttons"], pressed)

    def _on_joy(self, msg: Joy) -> None:
        buttons = tuple(int(b) for b in list(msg.buttons)[:MAX_BUTTONS])
        self._observe(tb4.Joy(time.monotonic(), buttons))

    def _on_wheels(self, msg: Any) -> None:
        self._observe(tb4.WheelStatus(time.monotonic(), bool(msg.wheels_enabled)))

    def _on_dock(self, msg: Any) -> None:
        self._observe(tb4.DockStatus(time.monotonic(), bool(msg.is_docked)))

    def _on_kidnap(self, msg: Any) -> None:
        self._observe(tb4.KidnapStatus(time.monotonic(), bool(msg.is_kidnapped)))

    def _on_odom(self, msg: Odometry) -> None:
        p, v = msg.pose.pose.position, msg.twist.twist
        if _finite(p.x, p.y, v.linear.x, v.angular.z):
            now = time.monotonic()
            self._observe(tb4.Odometry(now, p.x, p.y, v.linear.x, v.angular.z))


def main(args: list[str] | None = None) -> int:
    """Run the observer until interrupted. Exit status 0 on Ctrl-C, 2 when it cannot start."""
    rclpy.init(args=args)
    try:
        observer = Observer()
    except (OSError, ValueError, SpecError) as exc:
        print(f"autonometry observer: cannot start: {exc}", file=sys.stderr)
        rclpy.try_shutdown()
        return 2
    try:
        rclpy.spin(observer)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        observer.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
