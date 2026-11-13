"""The world of the TurtleBot 4 simulator demo: the people and the mission around the robot.

UNTESTED: written for ROS 2 Jazzy, Gazebo Harmonic and turtlebot4_simulator 2.0.2 (integration
notes part A) and never run on a machine with them. It plays the integration notes' worked
example against the real simulated robot; the Autonometry observer is a separate process and
only listens. The nodes below stand in for people and programs, under the names the device
registry (config/devices.json) declares for them:

- ``rviz2``: the operator's RViz on the PC (2D Pose Estimate, Nav2 Goal, cancel);
- ``teleop_twist_keyboard``: the operator's keyboard on the PC;
- ``hmi_person``: a person at the robot pressing the Create 3 buttons, which in the simulator
  arrive on ``/interface_buttons`` just as the Gazebo HMI panel sends them;
- ``basic_navigator``: a mission script that docks the robot.

Steps: undock by Create 3 button 2, a pose estimate, a Nav2 goal, a keyboard drive during it, an
obstacle in front of the robot (collision-monitor stop) and a keyboard takeover within 60 s, an
EStop on the power button, the goal cancelled, the EStop released, a dock by the mission node,
then 35 s of nothing so that the base becomes idle.
"""

import argparse
import math
import subprocess
import threading
import time
from typing import Any

import rclpy
import rclpy.task
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, TwistStamped
from irobot_create_msgs.action import Dock
from irobot_create_msgs.msg import InterfaceButtons
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import Odometry
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

#: A 0.3 m box, spawned in front of the robot to make the collision monitor stop.
OBSTACLE_SDF = (
    "<sdf version='1.9'><model name='obstacle'><static>true</static><link name='l'>"
    "<collision name='c'><geometry><box><size>0.3 0.3 0.5</size></box></geometry></collision>"
    "<visual name='v'><geometry><box><size>0.3 0.3 0.5</size></box></geometry></visual>"
    "</link></model></sdf>"
)


class People:
    """The nodes that stand for people and the mission script."""

    def __init__(self) -> None:
        self.rviz = Node("rviz2")
        self.keyboard = Node("teleop_twist_keyboard")
        self.hmi = Node("hmi_person")
        self.mission = Node("basic_navigator")
        self.nodes = [self.rviz, self.keyboard, self.hmi, self.mission]
        self.pose = self.rviz.create_publisher(PoseWithCovarianceStamped, "initialpose", 10)
        self.goals = ActionClient(self.rviz, NavigateToPose, "navigate_to_pose")
        self.dock = ActionClient(self.mission, Dock, "dock")
        self.cmd_vel = self.keyboard.create_publisher(TwistStamped, "cmd_vel", 10)
        self.buttons = self.hmi.create_publisher(InterfaceButtons, "interface_buttons", 10)
        self.odom: Odometry | None = None
        self.rviz.create_subscription(Odometry, "odom", self._on_odom, 10)

    def _on_odom(self, msg: Odometry) -> None:
        self.odom = msg

    def press(self, button: str) -> None:
        """A short press (well under the 2 s of a long press)."""
        down = InterfaceButtons()
        getattr(down, button).is_pressed = True
        for _ in range(5):
            self.buttons.publish(down)
            time.sleep(0.05)
        self.buttons.publish(InterfaceButtons())

    def drive(self, seconds: float, vx: float, wz: float = 0.0) -> None:
        msg = TwistStamped()
        msg.header.frame_id = "base_link"
        msg.twist.linear.x, msg.twist.angular.z = vx, wz
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            msg.header.stamp = self.keyboard.get_clock().now().to_msg()
            self.cmd_vel.publish(msg)
            time.sleep(0.1)

    def spawn_obstacle(self, world: str, ahead_m: float) -> None:
        """Spawn the box ``ahead_m`` in front of the robot (fixed arguments, no shell)."""
        if self.odom is None:
            return
        p, q = self.odom.pose.pose.position, self.odom.pose.pose.orientation
        yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        x, y = p.x + ahead_m * math.cos(yaw), p.y + ahead_m * math.sin(yaw)
        args = ["ros2", "run", "ros_gz_sim", "create", "-world", world, "-name", "obstacle"]
        args += ["-string", OBSTACLE_SDF, "-x", f"{x:.3f}", "-y", f"{y:.3f}", "-z", "0.25"]
        subprocess.run(args, check=False, timeout=30)  # noqa: S603 -- fixed arguments


def goal(x: float, y: float) -> NavigateToPose.Goal:
    pose = PoseStamped()
    pose.header.frame_id = "map"
    pose.pose.position.x, pose.pose.position.y = x, y
    pose.pose.orientation.w = 1.0
    return NavigateToPose.Goal(pose=pose)


def wait(future: rclpy.task.Future, timeout_s: float) -> Any:
    end = time.monotonic() + timeout_s
    while not future.done() and time.monotonic() < end:
        time.sleep(0.05)
    return future.result() if future.done() else None


def play(people: People, world: str, log: Any) -> None:
    say = log.info
    say("docked, waiting 35 s so that the base is idle")
    time.sleep(35.0)
    say("undock by Create 3 button 2")
    people.press("button_2")
    time.sleep(15.0)
    say("2D Pose Estimate from RViz")
    initial = PoseWithCovarianceStamped()
    initial.header.frame_id = "map"
    initial.pose.pose.orientation.w = 1.0
    people.pose.publish(initial)
    time.sleep(3.0)
    say("Nav2 goal from RViz")
    people.goals.wait_for_server(timeout_sec=30.0)
    handle = wait(people.goals.send_goal_async(goal(4.0, 0.0)), 10.0)
    time.sleep(6.0)
    say("keyboard while Nav2 drives")
    people.drive(2.0, 0.1, 0.4)
    time.sleep(2.0)
    say("obstacle ahead: collision monitor stop, then a keyboard takeover")
    people.spawn_obstacle(world, 0.5)
    time.sleep(3.0)
    people.drive(6.0, -0.15)
    say("EStop on the power button, then the goal cancelled from RViz")
    people.press("button_power")
    time.sleep(0.5)
    if handle is not None:
        wait(handle.cancel_goal_async(), 10.0)
    time.sleep(6.0)
    say("EStop released")
    people.press("button_power")
    time.sleep(2.0)
    say("dock by the mission node")
    people.dock.wait_for_server(timeout_sec=30.0)
    docking = wait(people.dock.send_goal_async(Dock.Goal()), 10.0)
    if docking is not None:
        wait(docking.get_result_async(), 120.0)
    say("docked: 35 s for idle")
    time.sleep(35.0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--world", default="warehouse", help="Gazebo world name")
    options = parser.parse_args()
    rclpy.init()
    people = People()
    executor = MultiThreadedExecutor()
    for node in people.nodes:
        executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()
    try:
        play(people, options.world, people.rviz.get_logger())
    finally:
        executor.shutdown()
        for node in people.nodes:
            node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
