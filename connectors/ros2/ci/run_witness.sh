#!/usr/bin/env bash
# End-to-end check of the station witness (README, "Station witness"; definitions.md 12) without
# Gazebo: build the ROS package, start the robot's recorder and observer and, beside them, the
# teleoperation station's own recorder (role station, its own key) and station observer; play
# fake_honest.py; then reconcile the two sealed logs and require trust level L3. Needs ROS 2
# Jazzy sourced, nav2_msgs and irobot_create_msgs installed, and `autonometry` on PATH.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
config="$repo/demo/tb4-sim/config"
work="$(mktemp -d)"
echo "working folder: $work"

pids=()
cleanup() {
    for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT

python3 -m colcon build --base-paths "$repo/connectors/ros2" \
    --build-base "$work/build" --install-base "$work/install" --event-handlers console_direct-
set +u
# shellcheck disable=SC1091
source "$work/install/setup.bash"
set -u

# Two witnesses, two keys: the robot's and the station's (definitions.md 12.1).
autonometry keys generate --name autonometry/tb4_ci_01 --out "$work/robot.key"
autonometry keys generate --name autonometry/station/operator_pc_1 --out "$work/station.key"
cat > "$work/robot.toml" <<TOML
robot_id = "tb4_ci_01"
data_dir = "$work/robot-data"
socket_path = "$work/robot.sock"
scopes = ["base"]
simulated = true
clock_source = "ntp"
key_path = "$work/robot.key"
device_registry = "$config/devices.json"
task_registry = "$config/tasks.json"
plan = "$config/plan.json"
TOML
cat > "$work/station.toml" <<TOML
robot_id = "tb4_ci_01"
role = "station"
station_id = "operator_pc_1"
data_dir = "$work/station-data"
socket_path = "$work/station.sock"
scopes = ["base"]
simulated = true
clock_source = "ntp"
key_path = "$work/station.key"
device_registry = "$config/station-devices.json"
task_registry = "$config/tasks.json"
plan = "$config/plan.json"
TOML

# The station starts first, so it witnesses everything the robot logs.
autonometry record --config "$work/station.toml" &
station_recorder=$!
pids+=("$station_recorder")
autonometry record --config "$work/robot.toml" &
robot_recorder=$!
pids+=("$robot_recorder")
for socket in station robot; do
    for _ in $(seq 50); do [ -S "$work/$socket.sock" ] && break; sleep 0.1; done
    [ -S "$work/$socket.sock" ] || { echo "the $socket recorder did not start"; exit 1; }
done

ros2 launch autonometry_ros station.launch.py \
    recorder_socket:="$work/station.sock" device_registry:="$config/station-devices.json" \
    respawn:=false &
station_observer=$!
pids+=("$station_observer")
ros2 launch autonometry_ros observer.launch.py \
    recorder_socket:="$work/robot.sock" device_registry:="$config/devices.json" respawn:=false &
robot_observer=$!
pids+=("$robot_observer")
sleep 3

python3 "$here/fake_honest.py"

# The robot stops first, then the station, so the station witnessed the whole robot session.
kill -TERM "$robot_observer"; wait "$robot_observer" || true
kill -TERM "$robot_recorder"; wait "$robot_recorder"
kill -TERM "$station_observer"; wait "$station_observer" || true
kill -TERM "$station_recorder"; wait "$station_recorder"

robot_log="$(ls "$work"/robot-data/tb4_ci_01/*/events.jsonl)"
station_log="$(ls "$work"/station-data/tb4_ci_01/*/events.jsonl)"
autonometry timeline "$robot_log"
autonometry timeline "$station_log"
status=0
autonometry reconcile --robot "$robot_log" --station "$station_log" \
    --vkey "$work/robot.key.vkey" --vkey "$work/station.key.vkey" \
    --anchor "${robot_log%.jsonl}.checkpoints.jsonl" \
    --anchor "${station_log%.jsonl}.checkpoints.jsonl" \
    --out "$work/witness.json" || status=$?
cat "$work/witness.json"
python3 "$here/check_witness.py" "$robot_log" "$station_log" "$work/witness.json"
exit "$status"
