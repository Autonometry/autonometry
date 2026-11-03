"""Generate a small synthetic event log (examples/example_log.jsonl) with known
structure: 2 robots, 6 sessions, pick-and-place tasks, AI-requested and
human-initiated takeovers, approvals, a planned reset, a safety stop, a hint,
one two-scope (humanoid) session, a remote-supervision block, and one
deliberate logging gap (-> unknown). Fixed seed so the report is reproducible.

Usage:  python make_example_log.py [out_path]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ZERO = "0" * 64
T0 = np.datetime64("2026-10-01T08:00:00.000")


def iso(t: np.datetime64) -> str:
    return str(np.datetime_as_string(t, unit="ms")) + "Z"


def canonical(ev: dict) -> bytes:
    return json.dumps({k: v for k, v in ev.items() if k != "hash"}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False).encode()


def make_log(seed: int = 7) -> list[dict]:
    rng = np.random.default_rng(seed)
    events: list[dict] = []
    day = 0
    for si in range(6):
        robot = "r01" if si % 2 == 0 else "r02"
        sid = f"s{si + 1:03d}"
        scopes = ["left_arm", "right_arm"] if si == 5 else ["whole_robot"]
        t = T0 + np.timedelta64(int(day * 86400 + si * 1800), "s")
        day += 1 if si % 2 == 1 else 0
        seq = 0
        prev_hash = None
        dist = 0.0

        def emit(ev: dict, t_: np.datetime64, task_id):
            nonlocal seq, prev_hash
            base = {"robot_id": robot, "session_id": sid, "task_id": task_id, "seq": seq, "ts": iso(t_)}
            base.update(ev)
            base["prev_hash"] = prev_hash
            base["hash"] = hashlib.sha256(canonical(base)).hexdigest()
            events.append(base)
            prev_hash = base["hash"]
            seq += 1

        emit({"type": "session_start", "scopes": scopes, "initial_states": {s: "idle" for s in scopes},
              "heartbeat_interval_s": 1.0, "hint_policy": "hint_as_shared",
              "task_registry_hash": ZERO, "plan_hash": ZERO}, t, None)
        t += np.timedelta64(5, "s")
        emit({"type": "supervision", "level": "remote_monitoring", "n_supervisors": 1}, t, None)
        n_tasks = int(rng.integers(4, 8))
        for k in range(n_tasks):
            tid = f"{sid}_t{k + 1}"
            tdef = "pick_place_v1" if rng.random() < 0.7 else "drawer_open_v1"
            t += np.timedelta64(int(rng.integers(5, 20)), "s")
            emit({"type": "task_start", "task_definition_id": tdef}, t, tid)
            for sc in scopes:
                emit({"type": "transition", "scope": sc, "state": "ai", "reason": None, "actor_id": "ai:pi0_v3"}, t, tid)
            dur = float(rng.gamma(4.0, 30.0))  # ~120 s tasks
            t_end = t + np.timedelta64(int(dur * 1000), "ms")
            tc = t
            # heartbeats every 10 s to keep the log alive (sparser than 1 s to keep the file small
            # -> gap_tolerance must be >= 10 s for this example; see run_example_report.py)
            n_int = rng.poisson(0.6 * (1.5 if si in (2, 3) else 1.0))
            int_times = sorted(rng.uniform(5, max(6, dur - 5), size=n_int))
            next_hb = tc + np.timedelta64(10, "s")
            cursor = t
            for it in int_times:
                ti = max(t + np.timedelta64(int(it * 1000), "ms"), cursor + np.timedelta64(2, "s"))
                while next_hb < ti:
                    dist += 0.4
                    emit({"type": "heartbeat", "distance_m": round(dist, 2)}, next_hb, tid)
                    next_hb += np.timedelta64(10, "s")
                sc = scopes[int(rng.integers(len(scopes)))]
                u = rng.random()
                if u < 0.45:
                    emit({"type": "help_request", "scope": sc}, ti, tid)
                    ti2 = ti + np.timedelta64(int(rng.uniform(1.0, 6.0) * 1000), "ms")
                    emit({"type": "transition", "scope": sc, "state": "human_remote", "reason": "ai_requested_help", "actor_id": "op_a"}, ti2, tid)
                    ti3 = ti2 + np.timedelta64(int(rng.gamma(2.0, 8.0) * 1000), "ms")
                    emit({"type": "transition", "scope": sc, "state": "ai", "reason": None, "actor_id": "ai:pi0_v3"}, ti3, tid)
                elif u < 0.7:
                    emit({"type": "transition", "scope": sc, "state": "human_local", "reason": "human_initiated", "actor_id": "tech_b"}, ti, tid)
                    ti3 = ti + np.timedelta64(int(rng.gamma(2.0, 10.0) * 1000), "ms")
                    emit({"type": "transition", "scope": sc, "state": "ai", "reason": None, "actor_id": "ai:pi0_v3"}, ti3, tid)
                elif u < 0.9:
                    emit({"type": "approval", "scope": sc, "actor_id": "op_a", "reason": "ai_requested_help", "dwell_s": round(float(rng.uniform(1, 5)), 1)}, ti, tid)
                else:
                    emit({"type": "transition", "scope": sc, "state": "safety_stop", "reason": "safety", "actor_id": None}, ti, tid)
                    ti2 = ti + np.timedelta64(int(rng.uniform(5, 20) * 1000), "ms")
                    emit({"type": "transition", "scope": sc, "state": "human_local", "reason": "safety", "actor_id": "tech_b"}, ti2, tid)
                    ti3 = ti2 + np.timedelta64(int(rng.gamma(2.0, 10.0) * 1000), "ms")
                    emit({"type": "transition", "scope": sc, "state": "ai", "reason": None, "actor_id": "ai:pi0_v3"}, ti3, tid)
                cursor = ti3 if (u < 0.7 or u >= 0.9) else ti
                next_hb = max(next_hb, cursor) + np.timedelta64(10, "s")
                t_end = max(t_end, cursor + np.timedelta64(5, "s"))
            while next_hb < t_end:
                dist += 0.4
                emit({"type": "heartbeat", "distance_m": round(dist, 2)}, next_hb, tid)
                next_hb += np.timedelta64(10, "s")
            t = max(t_end, next_hb - np.timedelta64(10, "s"))
            if si == 1 and k == 1:
                # deliberate 45 s silence inside the task -> unknown gap
                t += np.timedelta64(45, "s")
            if si == 0 and k == 2:
                emit({"type": "hint", "scope": scopes[0], "actor_id": "op_a", "ttl_s": 20.0}, t, tid)
                t += np.timedelta64(25, "s")
            outcome = "success" if rng.random() < 0.75 else "failure"
            emit({"type": "task_end", "outcome": outcome}, t, tid)
            # planned reset between tasks by a local technician, then idle
            for sc in scopes:
                emit({"type": "transition", "scope": sc, "state": "human_local", "reason": "planned", "actor_id": "tech_b", "plan_item_id": "reset_scene"}, t, None)
            t += np.timedelta64(int(rng.uniform(10, 25) * 1000), "ms")
            for sc in scopes:
                emit({"type": "transition", "scope": sc, "state": "idle", "reason": None, "actor_id": None}, t, None)
            t += np.timedelta64(3, "s")
        t += np.timedelta64(20, "s")
        emit({"type": "session_end"}, t, None)
    return events


def main(out: str | None = None) -> None:
    out_path = Path(out) if out else Path(__file__).with_name("example_log.jsonl")
    evs = make_log()
    with open(out_path, "w", encoding="utf-8") as fh:
        for e in evs:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"wrote {len(evs)} events to {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
