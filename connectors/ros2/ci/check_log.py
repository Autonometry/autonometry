"""Check the sealed log of the ROS end-to-end run (fake_robot.py) against the storyline.

Usage: check_log.py EVENTS.jsonl TIMELINE.json VERIFY.json

The states, episodes and tasks expected here follow from the storyline by the rules of
``autonometry.connectors.tb4`` and spec v0.3, as worked out for the golden test
(tests/golden/tb4/README.md); the run has real timing, so durations are not compared, only the
order of states and the counts. Exit status 0 when everything matches, 1 with the differences.
"""

import json
import sys
from pathlib import Path
from typing import Any

#: The base's states in order, repeats collapsed (README of tests/golden/tb4, plus the dock).
STATES = [
    "unknown",  # the recorder starts before the observer has watched for 30 s
    "idle",
    "shared",  # undock by Create 3 button 2
    "human_remote",  # 2D Pose Estimate, planned
    "ai",  # the Nav2 goal
    "shared",  # keyboard contention
    "ai",
    "safety_stop",  # collision monitor STOP
    "human_remote",  # keyboard takeover
    "safety_stop",  # EStop on the power button
    "unknown",  # EStop released, no commander
    "ai",  # dock by the mission node
    "idle",
    "human_remote",  # a hidden node drives through cmd_vel_smoothed (6.10, 6.12)
    "shared",  # drive_on_heading from an undeclared laptop (6.13)
    "idle",
]
#: (state, reason, actor_id, device_id) of the transitions that name a source.
SOURCES = [
    ("shared", "human_initiated", "op_02", "create3_button_2"),
    ("human_remote", "planned", "op_01", "rviz_pc_1"),
    ("ai", None, "ai:nav2", "nav2"),
    ("shared", "human_initiated", "op_01", "teleop_kbd_pc_1"),
    ("safety_stop", "safety", None, None),
    ("human_remote", "human_initiated", "op_01", "teleop_kbd_pc_1"),
    ("safety_stop", "safety", "op_02", "create3_button_power"),
    ("ai", None, "ai:mission_navigator", "mission_navigator"),
    ("human_remote", "human_initiated", "person:unidentified", None),
    ("shared", "human_initiated", "person:unidentified", None),
]


def collapse(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if not out or out[-1] != value:
            out.append(value)
    return out


def check(
    events: list[dict[str, Any]], timeline: dict[str, Any], verify: dict[str, Any]
) -> list[str]:
    problems: list[str] = []

    def expect(what: str, got: object, want: object) -> None:
        if got != want:
            problems.append(f"{what}: got {got!r}, want {want!r}")

    transitions = [e for e in events if e["type"] == "transition"]
    initial = events[0]["initial_states"]["base"]
    expect("states", collapse([initial] + [t["state"] for t in transitions]), STATES)
    seen = [
        (t["state"], t.get("reason"), t.get("actor_id"), t.get("device_id")) for t in transitions
    ]
    for source in SOURCES:
        if source not in seen:
            problems.append(f"no transition {source}")
    hints = [e for e in events if e["type"] == "hint"]
    expect(
        "hints", [(h.get("x_kind"), h.get("device_id")) for h in hints], [("cancel", "rviz_pc_1")]
    )
    ends = [e.get("outcome") for e in events if e["type"] == "task_end"]
    expect("task outcomes", ends, ["aborted"])
    publishers = [e["x_publishers"] for e in events if "x_publishers" in e]
    nodes = {p["node"] for found in publishers for p in found.get("/cmd_vel", [])}
    expect(
        "publishers of /cmd_vel", sorted(nodes), ["/collision_monitor", "/teleop_twist_keyboard"]
    )

    (session,) = timeline["sessions"]
    interventions = session["interventions"]
    expect("interventions", interventions["whole_robot"], 2)
    expect("intervention kinds", interventions["by_kind"], {"shared": 1, "takeover": 1})
    stops = session["safety_stops"]
    expect(
        "safety stops",
        {
            k: stops[k]
            for k in ("whole_robot", "human_triggered", "automatic", "joined_by_takeover")
        },
        {"whole_robot": 2, "human_triggered": 1, "automatic": 1, "joined_by_takeover": 1},
    )
    expect(
        "tasks",
        [(t["task_definition_id"], t["outcome"], t["autonomous"]) for t in session["tasks"]],
        [("nav_to_pose", "aborted", False)],
    )
    if not session["resets"]["planned_s"] > 0:
        problems.append("no planned reset time (the pose estimate)")
    if not session["resets"]["unplanned_s"] > 0:
        problems.append("no unplanned human time (the undock, the hidden operators)")
    codes = {f["code"] for f in session["flags"]}
    if "attribution_inferred" not in codes:
        problems.append("no attribution_inferred flag")
    if "undeclared_device" not in codes:
        problems.append("the hidden operators were not flagged undeclared_device")
    unexpected = codes - {"attribution_inferred", "clock_unsynced", "undeclared_device"}
    if unexpected:
        problems.append(f"unexpected flags: {sorted(unexpected)}")

    expect("verified at L1", verify["verified"], True)
    (checked,) = verify["sessions"]
    if checked["status"] == "failed" or checked["evidence"]:
        problems.append(f"verification failed: {checked['evidence']}")
    if "reconstructed" not in {g["code"] for g in checked["gaps"]}:
        problems.append("inferred attribution did not cap the session at L1")
    return problems


def main(argv: list[str]) -> int:
    log, timeline, verify = (Path(a) for a in argv)
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    problems = check(
        events,
        json.loads(timeline.read_text(encoding="utf-8")),
        json.loads(verify.read_text(encoding="utf-8")),
    )
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print(f"OK: {len(events)} events match the storyline")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
