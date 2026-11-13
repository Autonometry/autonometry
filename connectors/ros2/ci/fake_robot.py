"""A fake TurtleBot 4 for the ROS end-to-end check (README, "TurtleBot 4"; no Gazebo).

Nodes named like the real ones publish what a TurtleBot 4 running Nav2 publishes, and play the
storyline of the integration notes' worked example: docked and idle, an undock by a Create 3
button, a pose estimate from RViz, a Nav2 goal, a keyboard drive during it, a collision-monitor
stop with a keyboard takeover, an EStop, a cancelled goal, the EStop released, a dock by a mission
node, and idle again. Action status is published directly, as Nav2 and the Create 3 would; the
nodes that send goals in real life create action clients, so the ROS graph shows them as such.

This is the robot's world, not the observer: it publishes, the observer only listens. Run it with
ROS 2 Jazzy sourced, nav2_msgs and irobot_create_msgs installed.
"""

import threading
import time
from collections.abc import Callable

import rclpy
from action_msgs.msg import GoalStatus, GoalStatusArray
from geometry_msgs.msg import PoseWithCovarianceStamped, TwistStamped
from irobot_create_msgs.action import Dock, Undock
from irobot_create_msgs.msg import DockStatus, InterfaceButtons, KidnapStatus, WheelStatus
from nav2_msgs.action import NavigateToPose
from nav2_msgs.msg import CollisionMonitorState
from nav_msgs.msg import Odometry
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Joy
from std_msgs.msg import String

STATUS_QOS = QoSProfile(
    depth=10, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL
)
NAV_GOAL = bytes.fromhex("a1b2c3d4e5f60718293a4b5c6d7e8f90")
UNDOCK_GOAL = bytes.fromhex("0f0e0d0c0b0a09080706050403020100")
DOCK_GOAL = bytes.fromhex("1f1e1d1c1b1a19181716151413121110")


def status_array(goal: bytes, status: int) -> GoalStatusArray:
    entry = GoalStatus()
    entry.goal_info.goal_id.uuid = list(goal)
    entry.status = status
    return GoalStatusArray(status_list=[entry])


def twist(vx: float, wz: float = 0.0) -> TwistStamped:
    msg = TwistStamped()
    msg.twist.linear.x = vx
    msg.twist.angular.z = wz
    return msg


class World:
    """The fake robot: its nodes, its state, and the periodic topics."""

    def __init__(self) -> None:
        self.motion = Node("motion_control")
        self.nav = Node("bt_navigator")
        self.smoother = Node("velocity_smoother")
        self.monitor = Node("collision_monitor")
        self.tb4 = Node("turtlebot4_node")
        self.keyboard = Node("teleop_twist_keyboard")
        self.rviz = Node("rviz2")
        self.mission = Node("basic_navigator")
        self.joy = Node("joy_node")
        self.nodes = [
            self.motion,
            self.nav,
            self.smoother,
            self.monitor,
            self.tb4,
            self.keyboard,
            self.rviz,
            self.mission,
            self.joy,
        ]
        m = self.motion
        self.wheels = m.create_publisher(WheelStatus, "wheel_status", 10)
        self.dock_status = m.create_publisher(DockStatus, "dock_status", 10)
        self.kidnap = m.create_publisher(KidnapStatus, "kidnap_status", 10)
        self.odom = m.create_publisher(Odometry, "odom", 10)
        self.buttons = m.create_publisher(InterfaceButtons, "interface_buttons", 10)
        self.undock_status = m.create_publisher(
            GoalStatusArray, "undock/_action/status", STATUS_QOS
        )
        self.dock_action_status = m.create_publisher(
            GoalStatusArray, "dock/_action/status", STATUS_QOS
        )
        self.nav_status = self.nav.create_publisher(
            GoalStatusArray, "navigate_to_pose/_action/status", STATUS_QOS
        )
        self.smoothed = self.smoother.create_publisher(TwistStamped, "cmd_vel_smoothed", 10)
        self.relay = self.monitor.create_publisher(TwistStamped, "cmd_vel", 10)
        self.monitor_state = self.monitor.create_publisher(
            CollisionMonitorState, "collision_monitor_state", 10
        )
        self.function_calls = self.tb4.create_publisher(String, "function_calls", 10)
        self.teleop = self.keyboard.create_publisher(TwistStamped, "cmd_vel", 10)
        self.initial_pose = self.rviz.create_publisher(PoseWithCovarianceStamped, "initialpose", 10)
        self.joy_pub = self.joy.create_publisher(Joy, "joy", 10)
        # Goal senders, as in real life: the graph shows them as action clients.
        self.clients = [
            ActionClient(self.tb4, Undock, "undock"),
            ActionClient(self.tb4, Dock, "dock"),
            ActionClient(self.rviz, NavigateToPose, "navigate_to_pose"),
            ActionClient(self.mission, Dock, "dock"),
        ]
        self.lock = threading.Lock()
        self.wheels_enabled = True
        self.docked = True
        self.x = 0.0
        self.speed = 0.0
        self.nav_speed: float | None = None  # Nav2's wish; None when no goal runs
        self.stopped = False  # the collision monitor's STOP
        m.create_timer(0.1, self._fast)
        m.create_timer(1.0, self._slow)

    def _fast(self) -> None:
        with self.lock:
            self.wheels.publish(WheelStatus(wheels_enabled=self.wheels_enabled))
            if self.nav_speed is not None:
                self.smoothed.publish(twist(self.nav_speed))
                self.relay.publish(twist(0.0 if self.stopped else self.nav_speed))
            self.x += self.speed * 0.1
            odom = Odometry()
            odom.pose.pose.position.x = self.x
            odom.twist.twist.linear.x = self.speed
            self.odom.publish(odom)
            self.joy_pub.publish(Joy(buttons=[0] * 12, axes=[0.0] * 6))

    def _slow(self) -> None:
        with self.lock:
            self.dock_status.publish(DockStatus(is_docked=self.docked, dock_visible=True))
            self.kidnap.publish(KidnapStatus(is_kidnapped=False))

    def set(self, **values: object) -> None:
        with self.lock:
            for name, value in values.items():
                setattr(self, name, value)

    def press(self, button: str) -> None:
        pressed = InterfaceButtons()
        getattr(pressed, button).is_pressed = True
        self.buttons.publish(pressed)
        time.sleep(0.1)
        self.buttons.publish(InterfaceButtons())

    def call(self, name: str) -> None:
        self.function_calls.publish(String(data=name))

    def drive(self, seconds: float, vx: float, wz: float = 0.0) -> None:
        """The keyboard at 5 Hz for ``seconds``."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.teleop.publish(twist(vx, wz))
            time.sleep(0.2)


def at(start: float, offset: float) -> None:
    """Sleep until ``offset`` seconds after ``start`` (monotonic)."""
    time.sleep(max(0.0, start + offset - time.monotonic()))


def storyline(world: World, log: Callable[[str], None]) -> None:  # noqa: PLR0915 -- a script
    """The notes' worked example, with the dock and idle at the end; offsets in seconds."""
    start = time.monotonic()
    log("docked, no task: idle after 30 s")
    world.monitor_state.publish(CollisionMonitorState(action_type=0))
    at(start, 35.0)
    log("undock by Create 3 button 2")
    world.press("button_2")
    world.call("Undock")
    time.sleep(0.1)
    world.undock_status.publish(status_array(UNDOCK_GOAL, GoalStatus.STATUS_EXECUTING))
    world.set(speed=-0.05, docked=False)
    at(start, 40.0)
    world.set(speed=0.0)
    world.undock_status.publish(status_array(UNDOCK_GOAL, GoalStatus.STATUS_SUCCEEDED))
    at(start, 43.0)
    log("2D Pose Estimate from RViz")
    world.initial_pose.publish(PoseWithCovarianceStamped())
    at(start, 46.0)
    log("Nav2 goal from RViz")
    world.nav_status.publish(status_array(NAV_GOAL, GoalStatus.STATUS_EXECUTING))
    world.set(nav_speed=0.25, speed=0.25)
    at(start, 56.0)
    log("keyboard while Nav2 drives: contention")
    world.drive(2.0, 0.1, 0.4)
    at(start, 62.0)
    log("collision monitor STOP")
    world.set(stopped=True, speed=0.0)
    world.monitor_state.publish(CollisionMonitorState(action_type=1))
    at(start, 63.5)
    log("keyboard takeover")
    world.set(speed=-0.15)
    world.drive(6.5, -0.15)
    world.set(speed=0.0)
    at(start, 71.0)
    log("EStop on the power button")
    world.press("button_power")
    world.call("EStop")
    world.set(wheels_enabled=False)
    at(start, 71.5)
    log("goal cancelled from RViz")
    world.nav_status.publish(status_array(NAV_GOAL, GoalStatus.STATUS_CANCELING))
    at(start, 72.0)
    world.nav_status.publish(status_array(NAV_GOAL, GoalStatus.STATUS_CANCELED))
    world.set(nav_speed=None, stopped=False)
    world.monitor_state.publish(CollisionMonitorState(action_type=0))
    at(start, 78.0)
    log("EStop released")
    world.press("button_power")
    world.call("EStop")
    world.set(wheels_enabled=True)
    at(start, 80.0)
    log("dock by the mission node")
    world.dock_action_status.publish(status_array(DOCK_GOAL, GoalStatus.STATUS_EXECUTING))
    world.set(speed=0.05)
    at(start, 88.0)
    world.set(speed=0.0, docked=True)
    world.dock_action_status.publish(status_array(DOCK_GOAL, GoalStatus.STATUS_SUCCEEDED))
    log("docked: idle 30 s after the dock")
    at(start, 122.0)


def main() -> None:
    rclpy.init()
    world = World()
    executor = MultiThreadedExecutor()
    for node in world.nodes:
        executor.add_node(node)
    spinner = threading.Thread(target=executor.spin, daemon=True)
    spinner.start()
    try:
        storyline(world, world.motion.get_logger().info)
    finally:
        executor.shutdown()
        for node in world.nodes:
            node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
