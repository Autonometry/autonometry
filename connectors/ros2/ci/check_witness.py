"""Check the honest witness run (fake_honest.py): both logs and their reconciliation.

Usage: check_witness.py ROBOT_EVENTS.jsonl STATION_EVENTS.jsonl WITNESS.json

The robot's base should go unknown, idle, ai (first goal), human_remote (the station's
keyboard, held until the second goal), ai; the station's base idle, human_remote, idle; and
``autonometry reconcile`` should find the robot session at trust level L3 (definitions.md 12).
Exit status 0 when everything matches, 1 with the differences.
"""

import json
import sys
from pathlib import Path
from typing import Any

ROBOT_STATES = ["unknown", "idle", "ai", "human_remote", "ai"]
STATION_STATES = ["idle", "human_remote", "idle"]
#: The keyboard drives 8 s; at least this much of it must be both-human.
MIN_BOTH_HUMAN_S = 7.0


def collapse(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if not out or out[-1] != value:
            out.append(value)
    return out


def states(events: list[dict[str, Any]]) -> list[str]:
    initial = events[0]["initial_states"]["base"]
    return collapse([initial] + [e["state"] for e in events if e["type"] == "transition"])


def check(
    robot: list[dict[str, Any]], station: list[dict[str, Any]], witness: dict[str, Any]
) -> list[str]:
    problems: list[str] = []

    def expect(what: str, got: object, want: object) -> None:
        if got != want:
            problems.append(f"{what}: got {got!r}, want {want!r}")

    expect("robot states", states(robot), ROBOT_STATES)
    expect("station states", states(station), STATION_STATES)
    expect(
        "station role",
        (station[0].get("role"), station[0].get("station_id")),
        ("station", "operator_pc_1"),
    )
    inferred = [e["seq"] for e in robot + station if e.get("x_attribution_inferred") is True]
    expect("events with inferred attribution", inferred, [])
    (session,) = witness["sessions"]
    expect("robot session level", session["level"], "L3")
    expect("reasons it is not L3", session["reasons"], [])
    expect("station sessions", [s["status"] for s in session["stations"]], ["verified"])
    times = session["times"]
    if not times["both_human_s"] >= MIN_BOTH_HUMAN_S:
        problems.append(f"both-human time {times['both_human_s']} s, want the 8 s drive")
    if not times["held_s"] > 0.0:
        problems.append("no held time: the robot should hold human_remote until the next goal")
    expect("matched station episodes", session["episodes"]["matched_station"], 1)
    return problems


def main(argv: list[str]) -> int:
    robot_log, station_log, witness = (Path(a) for a in argv)

    def read(path: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    body = json.loads(witness.read_text(encoding="utf-8"))
    problems = check(read(robot_log), read(station_log), body)
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        (session,) = body["sessions"]
        print(f"OK: robot session at {session['level']}: {json.dumps(session['times'])}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
