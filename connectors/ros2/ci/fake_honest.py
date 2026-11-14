"""An honest TurtleBot 4 run with a teleoperation station, for the witness check (README,
"Station witness"; no Gazebo).

Every command has a source the ROS graph can name, so neither observer has to infer one
(definitions.md 6.12) and both logs can be trust level L2: Nav2's collision monitor is the only
``/cmd_vel`` publisher while Nav2 drives, the station's ``teleop_twist_keyboard`` the only one
while the operator drives. On ROS 2 Jazzy, where a subscription does not see the publisher's
GID, an L3 run needs exactly that. Storyline: idle; a Nav2 goal sent by the mission node; the
operator drives from the station's keyboard between tasks; a second goal; then nobody commands.

This is the robot's world and the operator's keyboard: it publishes, the observers only listen.
"""

import threading
import time
from collections.abc import Callable

import rclpy
from action_msgs.msg import GoalStatus, GoalStatusArray
from fake_robot import NAV_GOAL, STATUS_QOS, at, status_array, twist
from geometry_msgs.msg import TwistStamped
from irobot_create_msgs.msg import DockStatus, KidnapStatus, WheelStatus
from nav2_msgs.action import NavigateToPose
from nav2_msgs.msg import CollisionMonitorState
from nav_msgs.msg import Odometry
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

SECOND_GOAL = bytes.fromhex("b1b2c3d4e5f60718293a4b5c6d7e8f91")
#: Longer than both observers' graph refresh (1 s): a replaced publisher is known before it sends.
GRAPH_SETTLE_S = 2.5
#: Between a goal's status and Nav2's first command, so the task is open when the command comes.
STATUS_LEAD_S = 0.5


class HonestWorld:
    """The fake robot with one ``/cmd_vel`` publisher at a time."""

    def __init__(self) -> None:
        self.motion = Node("motion_control")
        self.nav = Node("bt_navigator")
        self.smoother = Node("velocity_smoother")
        self.controller = Node("controller_server")
        self.monitor = Node("collision_monitor")
        self.mission = Node("basic_navigator")
        self.keyboard = Node("teleop_twist_keyboard")
        self.nodes = [
            self.motion,
            self.nav,
            self.smoother,
            self.controller,
            self.monitor,
            self.mission,
            self.keyboard,
        ]
        m = self.motion
        self.wheels = m.create_publisher(WheelStatus, "wheel_status", 10)
        self.dock_status = m.create_publisher(DockStatus, "dock_status", 10)
        self.kidnap = m.create_publisher(KidnapStatus, "kidnap_status", 10)
        self.odom = m.create_publisher(Odometry, "odom", 10)
        self.nav_status = self.nav.create_publisher(
            GoalStatusArray, "navigate_to_pose/_action/status", STATUS_QOS
        )
        self.smoothed = self.smoother.create_publisher(TwistStamped, "cmd_vel_smoothed", 10)
        self.controller_out = self.controller.create_publisher(TwistStamped, "cmd_vel_nav", 10)
        self.monitor_state = self.monitor.create_publisher(
            CollisionMonitorState, "collision_monitor_state", 10
        )
        self.relay: object = self.monitor.create_publisher(TwistStamped, "cmd_vel", 10)
        self.teleop: object = None
        self.client = ActionClient(self.mission, NavigateToPose, "navigate_to_pose")
        self.lock = threading.Lock()
        self.x = 0.0
        self.speed = 0.0
        self.nav_speed: float | None = None
        m.create_timer(0.1, self._fast)
        m.create_timer(1.0, self._slow)

    def _fast(self) -> None:
        with self.lock:
            self.wheels.publish(WheelStatus(wheels_enabled=True))
            if self.nav_speed is not None and self.relay is not None:
                self.controller_out.publish(twist(self.nav_speed))
                self.smoothed.publish(twist(self.nav_speed))
                self.relay.publish(twist(self.nav_speed))  # type: ignore[attr-defined]
            self.x += self.speed * 0.1
            odom = Odometry()
            odom.pose.pose.position.x = self.x
            odom.twist.twist.linear.x = self.speed
            self.odom.publish(odom)

    def _slow(self) -> None:
        with self.lock:
            self.dock_status.publish(DockStatus(is_docked=False, dock_visible=True))
            self.kidnap.publish(KidnapStatus(is_kidnapped=False))

    def keyboard_drives(self, yes: bool) -> None:
        """Make the keyboard, or Nav2's relay, the only ``/cmd_vel`` publisher."""
        with self.lock:
            if yes and self.relay is not None:
                self.monitor.destroy_publisher(self.relay)
                self.relay = None
                self.teleop = self.keyboard.create_publisher(TwistStamped, "cmd_vel", 10)
            elif not yes and self.teleop is not None:
                self.keyboard.destroy_publisher(self.teleop)
                self.teleop = None
                self.relay = self.monitor.create_publisher(TwistStamped, "cmd_vel", 10)
        time.sleep(GRAPH_SETTLE_S)

    def goal(self, goal_id: bytes, seconds: float) -> None:
        """A NavigateToPose goal the mission node sent, executed for ``seconds``."""
        self.nav_status.publish(status_array(goal_id, GoalStatus.STATUS_EXECUTING))
        time.sleep(STATUS_LEAD_S)
        with self.lock:
            self.nav_speed, self.speed = 0.25, 0.25
        time.sleep(seconds)
        with self.lock:
            self.nav_speed, self.speed = None, 0.0
        time.sleep(STATUS_LEAD_S)
        self.nav_status.publish(status_array(goal_id, GoalStatus.STATUS_SUCCEEDED))

    def drive(self, seconds: float, vx: float) -> None:
        """The operator's keyboard at 5 Hz for ``seconds``, then a stop key (a neutral twist)."""
        with self.lock:
            self.speed = vx
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.teleop.publish(twist(vx))  # type: ignore[attr-defined]
            time.sleep(0.2)
        self.teleop.publish(twist(0.0))  # type: ignore[attr-defined]
        with self.lock:
            self.speed = 0.0


def storyline(world: HonestWorld, log: Callable[[str], None]) -> None:
    """Offsets in seconds from the start."""
    start = time.monotonic()
    world.monitor_state.publish(CollisionMonitorState(action_type=0))
    log("no task, nobody commands: idle after 30 s")
    at(start, 35.0)
    log("Nav2 goal from the mission node")
    world.goal(NAV_GOAL, 10.0)
    log("the operator takes the keyboard on the station")
    world.keyboard_drives(True)
    log("the operator drives from the station")
    world.drive(8.0, 0.2)
    at(start, 64.0)
    world.keyboard_drives(False)
    log("second Nav2 goal from the mission node")
    world.goal(SECOND_GOAL, 10.0)
    log("nobody commands")
    at(start, 85.0)


def main() -> None:
    rclpy.init()
    world = HonestWorld()
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
