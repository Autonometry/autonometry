#!/usr/bin/env bash
# TurtleBot 4 simulator demo (README.md beside this file). UNTESTED: written for Ubuntu 24.04,
# ROS 2 Jazzy, Gazebo Harmonic and turtlebot4_simulator 2.0.2, never run with them.
#
# Starts the Autonometry recorder, the simulator with Nav2, the read-only observer and the
# scenario; stops them in reverse order; then verifies the sealed log and writes a report.
# Usage: run_demo.sh [WORLD]   (default: warehouse)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
world="${1:-warehouse}"
run="$here/run"
mkdir -p "$run"
chmod 700 "$run"

pids=()
cleanup() {
    for ((i = ${#pids[@]} - 1; i >= 0; i--)); do kill -TERM "${pids[i]}" 2>/dev/null || true; done
}
trap cleanup EXIT

# The observer package, built once into run/ (needs autonometry importable by python3).
if [ ! -f "$run/install/setup.bash" ]; then
    python3 -m colcon build --base-paths "$repo/connectors/ros2" \
        --build-base "$run/build" --install-base "$run/install"
fi
set +u
# shellcheck disable=SC1091
source "$run/install/setup.bash"
set -u

# 1. The recorder (its signing key is made on the first run).
[ -f "$run/tb4_sim_01.key" ] || autonometry keys generate --name autonometry/tb4_sim_01 \
    --out "$run/tb4_sim_01.key"
autonometry record --config "$here/config/recorder.toml" &
pids+=($!)
for _ in $(seq 50); do [ -S "$run/recorder.sock" ] && break; sleep 0.1; done

# 2. The simulator with SLAM, Nav2 and RViz (integration notes A.5). The robot spawns docked.
ros2 launch turtlebot4_gz_bringup turtlebot4_gz.launch.py \
    slam:=true nav2:=true rviz:=true model:=standard world:="$world" &
pids+=($!)
sleep 45

# 3. The observer: subscribes only.
ros2 launch autonometry_ros observer.launch.py \
    recorder_socket:="$run/recorder.sock" device_registry:="$here/config/devices.json" &
pids+=($!)
sleep 3

# 4. The scenario: the people and the mission around the robot.
python3 "$here/scenario.py" --world "$world"

# 5. Stop everything (the recorder last, so it seals the session), verify and report.
cleanup
for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
pids=()
mapfile -t logs < <(ls "$run"/data/tb4_sim_01/*/events.jsonl)
anchors=()
for log in "${logs[@]}"; do anchors+=(--anchor "${log%.jsonl}.checkpoints.jsonl"); done
autonometry verify "${logs[@]}" --vkey "$run/tb4_sim_01.key.vkey" "${anchors[@]}" --require L1 || true
autonometry timeline "${logs[@]}" || true
autonometry report "${logs[@]}" --vkey "$run/tb4_sim_01.key.vkey" "${anchors[@]}" \
    --out "$run/report" --title "TurtleBot 4 simulator demo" || true
echo "report: $run/report/report.html"
