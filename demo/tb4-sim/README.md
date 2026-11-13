# TurtleBot 4 simulator demo

**Status: untested.** These steps follow the integration notes, part A
(`research/turtlebot4/tb4_integration_notes.md`), and were never run end to end on a machine with
Gazebo. What *is* tested: the engine on every CI platform, and the observer, recorder and checks
with fake ROS 2 nodes in a `ros:jazzy` container (CI job "TurtleBot 4 observer on ROS 2 Jazzy",
`connectors/ros2/ci/run_e2e.sh`).

The demo runs a simulated TurtleBot 4 with Nav2, the Autonometry recorder, the read-only observer
and `scenario.py`, which plays the people and the mission around the robot: an undock by a Create 3
button, a pose estimate, a Nav2 goal, a keyboard drive during it, a collision-monitor stop with a
keyboard takeover, an EStop, a cancelled goal, a dock, and idle. It ends with a verified log, its
timeline and an HTML report.

## Windows 11: WSL2 with Ubuntu 24.04

In PowerShell as administrator:

```
wsl --install -d Ubuntu-24.04
```

Restart when asked, open "Ubuntu 24.04" from the Start menu and create your user. WSLg shows Linux
windows (Gazebo, RViz) on the Windows desktop; update the GPU driver from the vendor first. Do the
rest inside Ubuntu.

## ROS 2 Jazzy and Gazebo Harmonic (notes A.1 to A.4)

1. ROS 2 Jazzy desktop, from the
   [official instructions](https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html),
   then `sudo apt install ros-dev-tools`.
2. Gazebo Harmonic from packages.osrfoundation.org: add the OSRF apt key and repository as in the
   [TurtleBot 4 manual](https://turtlebot.github.io/turtlebot4-user-manual/software/turtlebot4_simulator.html),
   then `sudo apt-get update && sudo apt-get install gz-harmonic`.
3. The simulator and the message packages:

   ```
   sudo apt install ros-jazzy-turtlebot4-simulator ros-jazzy-irobot-create-nodes \
       ros-jazzy-irobot-create-msgs ros-jazzy-nav2-msgs ros-jazzy-turtlebot4-msgs \
       ros-jazzy-teleop-twist-keyboard python3-venv
   ```

## Autonometry

Install it in a virtual environment that also sees the ROS 2 Python packages (`rclpy` comes from
ROS, never from PyPI):

```
source /opt/ros/jazzy/setup.bash
python3 -m venv --system-site-packages ~/autonometry-venv
~/autonometry-venv/bin/pip install "autonometry[report]"     # or: pip install "/path/to/repo[report]"
source ~/autonometry-venv/bin/activate
```

## Run

From the repository, with ROS 2 sourced and the virtual environment active:

```
demo/tb4-sim/run_demo.sh            # world: warehouse; or run_demo.sh maze
```

It builds the observer package into `demo/tb4-sim/run/`, makes the signing key there on the first
run, and then:

1. starts `autonometry record --config demo/tb4-sim/config/recorder.toml`
   (`simulated = true`; `unobserved = ["safety_stops", "human_inputs"]`, since firmware reflexes
   and hazards are not relayed and the Bluetooth LE and webserver paths leave no ROS 2 trace);
2. starts the simulator with SLAM, Nav2 and RViz (`turtlebot4_gz.launch.py`, notes A.5); the
   robot spawns docked;
3. starts the observer (`ros2 launch autonometry_ros observer.launch.py`), which only subscribes;
4. runs `scenario.py` (about three minutes);
5. stops the observer, the simulator and the recorder, then runs `autonometry verify`,
   `autonometry timeline` and `autonometry report --out demo/tb4-sim/run/report`.

The registries are in `config/`: `devices.json` (the TurtleBot 4 template of notes B.1: the Create
3 and HMI buttons, the Bluetooth controller and manual handling are local; the keyboard, RViz and
the Gazebo GUI teleop are remote; Nav2's whole command chain and a mission script are AI),
`tasks.json` (`nav_to_pose`, `nav_through_poses`, `patrol`) and `plan.json` (`initial_pose`, `place_on_dock`).

## What to expect

The log is trust level L1: on ROS 2 Jazzy a subscriber cannot see which publisher sent a
`/cmd_vel` message, so with Nav2 and the keyboard both on the topic the observer attributes
commands by the topic-level rule and marks them `x_attribution_inferred` (definitions.md 6.12).
The states should follow `tests/golden/tb4/README.md`: idle, `shared` (button undock),
`human_remote` (planned pose estimate), `ai` (Nav2), `shared` (keyboard contention),
`safety_stop` then `human_remote` (one intervention), `safety_stop` (EStop by a person), the task
ending `aborted`, `unknown` after the EStop is released, `ai` (the mission's dock), and idle.

Things that may differ in the real simulator (the reasons this is marked untested): the obstacle
spawned in front of the robot may not trigger the collision monitor's stop polygon; `/initialpose`
is ignored by SLAM; the Create 3 button presses are sent on `/interface_buttons` the way the Gazebo
HMI panel sends them, and the simulated `turtlebot4_node` must map them to Undock and EStop.
