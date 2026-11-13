#!/usr/bin/env bash
# End-to-end check of the TurtleBot 4 observer without Gazebo (README, "TurtleBot 4"):
# build the ROS package, start the recorder and the observer, play fake_robot.py, then verify the
# sealed log and check its timeline (check_log.py). Needs ROS 2 Jazzy sourced, nav2_msgs and
# irobot_create_msgs installed, and `autonometry` importable by the python3 on PATH.
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

# 1. Build the ROS package with the python3 that has autonometry, so its scripts use it.
python3 -m colcon build --base-paths "$repo/connectors/ros2" \
    --build-base "$work/build" --install-base "$work/install" --event-handlers console_direct-
set +u
# shellcheck disable=SC1091
source "$work/install/setup.bash"
set -u

# 2. The recorder, with the demo's registries.
autonometry keys generate --name autonometry/tb4_ci_01 --out "$work/tb4.key"
cat > "$work/recorder.toml" <<TOML
robot_id = "tb4_ci_01"
data_dir = "$work/data"
socket_path = "$work/recorder.sock"
scopes = ["base"]
hint_policy = "hint_as_shared"
simulated = true
unobserved = ["safety_stops", "human_inputs"]
clock_source = "ntp"
key_path = "$work/tb4.key"
device_registry = "$config/devices.json"
task_registry = "$config/tasks.json"
plan = "$config/plan.json"
TOML
autonometry record --config "$work/recorder.toml" &
recorder=$!
pids+=("$recorder")
for _ in $(seq 50); do [ -S "$work/recorder.sock" ] && break; sleep 0.1; done
[ -S "$work/recorder.sock" ] || { echo "the recorder did not start"; exit 1; }

# 3. The observer.
ros2 launch autonometry_ros observer.launch.py \
    recorder_socket:="$work/recorder.sock" device_registry:="$config/devices.json" respawn:=false &
observer=$!
pids+=("$observer")
sleep 3

# 4. The fake robot plays the storyline (about two minutes).
python3 "$here/fake_robot.py"

# 5. Stop the observer, then the recorder (it seals the session), and check the log.
kill -TERM "$observer"; wait "$observer" || true
kill -TERM "$recorder"; wait "$recorder"
log="$(ls "$work"/data/tb4_ci_01/*/events.jsonl)"
autonometry verify "$log" --vkey "$work/tb4.key.vkey" \
    --anchor "${log%.jsonl}.checkpoints.jsonl" --require L1 --json > "$work/verify.json"
autonometry timeline --json "$log" > "$work/timeline.json"
autonometry timeline "$log"
python3 "$here/check_log.py" "$log" "$work/timeline.json" "$work/verify.json"
