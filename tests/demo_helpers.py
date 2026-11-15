"""The demo's shipped session, rebuilt from its scenario.

``autonometry demo`` reads ``src/autonometry/demo/session/events.jsonl``. This module writes that
file the way the recorder would have written it (README, "Recorder"): ``session_start`` with
every scope ``unknown``, a heartbeat every second with the clock status and the distance, a
transition only when a connector's state changes, ``unknown`` with ``x_cause: "silent"`` after
``gap_tolerance_s`` without an assertion, and ``session_end`` after a last heartbeat. The test
``test_the_shipped_demo_log_is_its_scenario`` checks the shipped file against it; run
``uv run python tests/demo_helpers.py`` to rewrite it after changing the scenario.
"""

import json
from pathlib import Path
from typing import Any

import rfc8785

from autonometry.registry import registry_hash
from autonometry.timestamps import ns_to_ts, ts_to_ns

DATA = Path(__file__).resolve().parents[1] / "src" / "autonometry" / "demo" / "session"
ROBOT = "demo_amr_01"
SESSION = "000001-20261110T080000Z-5d1e"
START_NS = ts_to_ns("2026-11-10T08:00:00.000000000Z")
END_S = 740.0
SCOPE = "whole_robot"
NAV = {"actor_id": "ai:nav_stack", "device_id": "nav_stack", "reason": None}
OPERATOR = {"actor_id": "op_7f3a", "device_id": "remote_console"}
#: Speed in metres per second while the scope holds each state; others stand still.
SPEED = {"ai": 0.8, "human_remote": 0.3}

#: (seconds after the start, event): what the connector sent, as the recorder logged it.
SCENARIO: list[tuple[float, dict[str, Any]]] = [
    (1.312, {"type": "transition", "scope": SCOPE, "state": "idle", "reason": None,
             "actor_id": None}),
    # Task 1: an autonomous delivery.
    (20.061, {"type": "transition", "scope": SCOPE, "state": "ai", **NAV}),
    (20.104, {"type": "task_start", "task_id": "t1", "task_definition_id": "deliver_tote"}),
    (125.430, {"type": "task_end", "task_id": "t1", "outcome": "success"}),
    # Task 2: the robot asks for help in a blocked aisle; a remote operator drives it out.
    (140.022, {"type": "task_start", "task_id": "t2", "task_definition_id": "deliver_tote"}),
    (190.517, {"type": "help_request", "scope": SCOPE}),
    (198.236, {"type": "transition", "scope": SCOPE, "state": "human_remote",
               "reason": "ai_requested_help", **OPERATOR}),
    (238.904, {"type": "transition", "scope": SCOPE, "state": "ai", **NAV}),
    (290.118, {"type": "task_end", "task_id": "t2", "outcome": "success"}),
    # Task 3: the safety scanner stops the robot when a person crosses its path.
    (305.009, {"type": "task_start", "task_id": "t3", "task_definition_id": "deliver_tote"}),
    (350.276, {"type": "transition", "scope": SCOPE, "state": "safety_stop",
               "reason": "safety", "actor_id": None}),
    (354.391, {"type": "transition", "scope": SCOPE, "state": "ai", **NAV}),
    (420.555, {"type": "task_end", "task_id": "t3", "outcome": "success"}),
    # Task 4: the connector crashes and restarts; the recorder logs the silence as unknown.
    (435.071, {"type": "task_start", "task_id": "t4", "task_definition_id": "deliver_tote"}),
    (462.250, {"type": "transition", "scope": SCOPE, "state": "unknown", "reason": None,
               "actor_id": None, "x_cause": "silent"}),
    (475.613, {"type": "transition", "scope": SCOPE, "state": "ai", **NAV}),
    (520.340, {"type": "task_end", "task_id": "t4", "outcome": "success"}),
    # Task 5: the tote is not where the order said.
    (535.198, {"type": "task_start", "task_id": "t5", "task_definition_id": "deliver_tote"}),
    (600.877, {"type": "task_end", "task_id": "t5", "outcome": "failure"}),
    # Task 6: back to the dock, then idle until the shift ends.
    (610.402, {"type": "task_start", "task_id": "t6", "task_definition_id": "return_to_dock"}),
    (680.733, {"type": "task_end", "task_id": "t6", "outcome": "success"}),
    (710.845, {"type": "transition", "scope": SCOPE, "state": "idle", "reason": None,
               "actor_id": None}),
]  # fmt: skip


def _ns(seconds: float) -> int:
    return START_NS + round(seconds * 1e9)


def registries() -> dict[str, Any]:
    """The device registry, task registry and plan the session declares, by file name."""
    return {
        name: json.loads((DATA / name).read_text(encoding="utf-8"))
        for name in ("devices.json", "tasks.json", "plan.json")
    }


def _session_start(docs: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "session_start",
        "task_id": None,
        "scopes": [SCOPE],
        "heartbeat_interval_s": 1.0,
        "hint_policy": "hint_as_shared",
        "task_registry_hash": registry_hash(docs["tasks.json"]),
        "plan_hash": registry_hash(docs["plan.json"]),
        "device_registry_hash": registry_hash(docs["devices.json"]),
        "simulated": True,
        "initial_states": {SCOPE: "unknown"},
    }


def _heartbeat(second: int, distance_m: float) -> tuple[int, dict[str, Any]]:
    """A heartbeat about ``second`` s after the start, a few hundred microseconds late as the
    recorder's loop is, with an NTP error that drifts between 2 and 6 ms."""
    late_ns = 200_000 + (second * 7919) % 400_000
    error_s = round(0.002 + ((second * 37) % 41) / 10_000, 4)
    event = {
        "type": "heartbeat",
        "clock_source": "ntp",
        "clock_max_error_s": error_s,
        "distance_m": round(distance_m, 3),
    }
    return _ns(second) + late_ns, event


def build_events() -> list[dict[str, Any]]:
    """Every event of the demo session in log order, with ``seq``, ``ts`` and ``task_id``."""
    timed: list[tuple[int, dict[str, Any]]] = [(_ns(0.0), _session_start(registries()))]
    state, distance = "unknown", 0.0
    changes = [(t, e) for t, e in SCENARIO if e["type"] == "transition"]
    for second in range(int(END_S)):
        for t, event in changes:
            if second - 1 <= t < second:
                state = event["state"]
        distance += SPEED.get(state, 0.0) if second else 0.0
        timed.append(_heartbeat(second, distance))
    timed += [(_ns(t), dict(event)) for t, event in SCENARIO]
    final_ns, final = _heartbeat(int(END_S), distance)
    timed.append((final_ns - 400_000, final))
    timed.append((final_ns, {"type": "session_end", "task_id": None, "x_cause": "shutdown"}))
    timed.sort(key=lambda pair: pair[0])

    events, open_task = [], None
    for seq, (ns, event) in enumerate(timed):
        if event["type"] == "task_start":
            open_task = event["task_id"]
        elif event["type"] not in ("session_start", "session_end", "task_end"):
            event["task_id"] = open_task
        events.append({**event, "robot_id": ROBOT, "session_id": SESSION, "seq": seq,
                       "ts": ns_to_ts(ns)})  # fmt: skip
        if event["type"] == "task_end":
            open_task = None
    return events


def log_bytes() -> bytes:
    """The shipped log: one RFC 8785 line per event."""
    return b"".join(rfc8785.dumps(event) + b"\n" for event in build_events())


if __name__ == "__main__":
    (DATA / "events.jsonl").write_bytes(log_bytes())
