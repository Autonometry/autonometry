"""Dummy pilot generator for the Autonomy Meter demo.

One simulated pilot: 4 robot arms x 10 days, one 2-hour session per robot per day.
For every session it produces
  * the robot's event log (event.schema.json v0.1, what the meter records),
  * the teleop station's control windows (the second witness, station clock),
  * the ground truth timeline (what really happened), which the meter never sees.
Faults (hidden remote operator, logger crash) are switched on per scenario.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict

import numpy as np

T0 = dt.datetime(2026, 11, 2, 9, 0, tzinfo=dt.timezone.utc)
TASK_WORK_S = {"pick_place_v1": 120, "sort_parts_v1": 150, "pack_box_v1": 200}  # median AI work per task
REGISTRY_HASH = hashlib.sha256(",".join(TASK_WORK_S).encode()).hexdigest()
PLAN_HASH = hashlib.sha256(b"refill_bin after every 10th task").hexdigest()
ROBOT_FRAILTY = {"r1": 0.7, "r2": 1.0, "r3": 1.1, "r4": 1.8}  # robot effect on the intervention rate
BASE_RATE = 2.5          # interventions per hour of AI time
SESSION_S = 7200.0
HEARTBEAT_S = 25.0       # emitted every 25 s, 30 s declared
STATION_OFFSET_S = 0.35  # teleop station clock runs 0.35 s behind the robot clock
AI = "ai:policy_v3"


def iso(t: float) -> str:
    return (T0 + dt.timedelta(seconds=float(t))).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Session:
    def __init__(self, robot_id: str, day: int):
        self.robot_id, self.day = robot_id, day
        self.sid = f"{robot_id}-s{day + 1:03d}"
        self.t0 = day * 86400.0
        self.raw: list[tuple[float, int, dict]] = []   # (t, insertion order, event)
        self.truth: list[tuple[float, float, str]] = []
        self.station: list[tuple[float, float]] = []   # robot-clock seconds

    def ev(self, t: float, type_: str, task_id: str | None = None, **kw) -> None:
        e = {"robot_id": self.robot_id, "session_id": self.sid, "task_id": task_id, "type": type_, **kw}
        self.raw.append((t, len(self.raw), e))

    def seg(self, s: float, e: float, state: str) -> None:
        if e > s:
            self.truth.append((s, e, state))

    def to(self, t: float, state: str, task_id: str | None, **kw) -> None:
        self.ev(t, "transition", task_id, scope="whole_robot", state=state, **kw)


def simulate_session(rng: np.random.Generator, robot_id: str, day: int, rate: float, hidden_rate: float = 0.0) -> Session:
    S = Session(robot_id, day)
    t, end = S.t0, S.t0 + SESSION_S
    S.ev(t, "session_start", scopes=["whole_robot"], initial_states={"whole_robot": "idle"},
         heartbeat_interval_s=30.0, hint_policy="hint_as_shared",
         task_registry_hash=REGISTRY_HASH, plan_hash=PLAN_HASH)
    S.ev(t, "supervision", level="remote_monitoring", n_supervisors=1)
    S.seg(t, t + 5, "idle")
    t += 5
    k = 0
    while t < end - 600:
        k += 1
        tid, tdef = f"{S.sid}-t{k:03d}", list(TASK_WORK_S)[rng.integers(len(TASK_WORK_S))]
        S.ev(t, "task_start", tid, task_definition_id=tdef)
        S.to(t, "ai", tid, reason=None, actor_id=AI)
        remaining, helped = rng.lognormal(np.log(TASK_WORK_S[tdef]), 0.3), False
        while True:
            g_obs = rng.exponential(3600 / rate)
            g_hid = rng.exponential(3600 / hidden_rate) if hidden_rate > 0 else np.inf
            g = min(g_obs, g_hid)
            if g >= remaining:
                S.seg(t, t + remaining, "ai")
                t += remaining
                break
            S.seg(t, t + g, "ai")
            t, remaining = t + g, remaining - g
            if g_hid < g_obs:  # hidden remote operator: robot log keeps saying 'ai'
                d = rng.lognormal(np.log(50), 0.5)
                S.seg(t, t + d, "human_remote")
                S.station.append((t, t + d))
                t, remaining = t + d, max(remaining - 0.8 * d, 1.0)
                continue
            helped, u = True, rng.random()
            if u < 0.55 or 0.85 <= u < 0.95:  # the AI asks for help first
                S.ev(t, "help_request", tid, scope="whole_robot")
                lat = rng.uniform(2, 8)
                S.seg(t, t + lat, "ai")
                t += lat
            if u < 0.85:  # takeover
                if u < 0.55:
                    state, reason = "human_remote", "ai_requested_help"
                else:
                    state, reason = ("human_local" if rng.random() < 0.6 else "human_remote"), "human_initiated"
                d = rng.lognormal(np.log(40), 0.6)
                S.to(t, state, tid, reason=reason, actor_id="op_07" if state == "human_remote" else "tech_02")
                S.seg(t, t + d, state)
                if state == "human_remote":
                    S.station.append((t, t + d))
                t, remaining = t + d, max(remaining - 0.5 * d, 1.0)
                S.to(t, "ai", tid, reason=None, actor_id=AI)
            elif u < 0.95:  # one approval click
                S.ev(t, "approval", tid, scope="whole_robot", actor_id="op_07", reason="ai_requested_help",
                     dwell_s=round(float(rng.lognormal(np.log(4), 0.4)), 2))
            else:  # protective stop, AI resumes on its own
                S.to(t, "safety_stop", tid, reason="safety", actor_id=None)
                d = rng.uniform(5, 20)
                S.seg(t, t + d, "safety_stop")
                t += d
                S.to(t, "ai", tid, reason=None, actor_id=AI)
        S.ev(t, "task_end", tid, outcome="success" if rng.random() < (0.85 if helped else 0.93) else "failure")
        if k % 10 == 0:  # pre-registered human reset: refill the parts bin
            d = rng.lognormal(np.log(60), 0.3)
            S.to(t, "human_local", None, reason="planned", actor_id="tech_02", plan_item_id="refill_bin")
            S.seg(t, t + d, "human_local")
            t += d
            S.to(t, "ai", None, reason=None, actor_id=AI)
        else:  # the robot resets its own scene
            d = rng.uniform(5, 12)
            S.seg(t, t + d, "ai")
            t += d
    S.to(t, "idle", None, reason=None, actor_id=None)
    S.seg(t, end, "idle")
    S.ev(end, "session_end")
    return S


def overwrite_truth(S: Session, s: float, e: float, state: str) -> None:
    """Force the true state on [s, e) (e.g. a human drove while the logger was down)."""
    out = []
    for a, b, st in S.truth:
        if b <= s or a >= e:
            out.append((a, b, st))
            continue
        if a < s:
            out.append((a, s, st))
        if b > e:
            out.append((e, b, st))
    out.append((s, e, state))
    S.truth = sorted(out)


def truth_state_at(S: Session, t: float) -> str:
    for a, b, st in S.truth:
        if a <= t < b:
            return st
    return "idle"


def robot_log(S: Session, crash: tuple[float, float] | None = None) -> list[dict]:
    """Robot event log with heartbeats and seq numbers. `crash` = logger down on (start, end) seconds."""
    raw = list(S.raw)
    hb = S.t0 + HEARTBEAT_S
    while hb < S.t0 + SESSION_S:
        raw.append((hb, 10**9, {"robot_id": S.robot_id, "session_id": S.sid, "task_id": None, "type": "heartbeat"}))
        hb += HEARTBEAT_S
    if crash:
        c0, c1 = S.t0 + crash[0], S.t0 + crash[1]
        raw = [r for r in raw if not (c0 < r[0] < c1)]
        st = truth_state_at(S, c1)  # logger restarts and re-asserts the current state
        kw = {"reason": "human_initiated", "actor_id": "op_07"} if st.startswith("human") or st == "shared" else \
             {"reason": "safety", "actor_id": None} if st == "safety_stop" else {"reason": None, "actor_id": AI if st == "ai" else None}
        raw.append((c1, -1, {"robot_id": S.robot_id, "session_id": S.sid, "task_id": None, "type": "transition",
                             "scope": "whole_robot", "state": st, **kw}))
    raw.sort(key=lambda r: (r[0], r[1]))
    return [{**e, "seq": i, "ts": iso(t)} for i, (t, _, e) in enumerate(raw)]


def station_log(S: Session, rng: np.random.Generator) -> list[dict]:
    """Teleop-station control windows in the station's own clock."""
    clock = lambda t: t - STATION_OFFSET_S + rng.normal(0, 0.05)
    return [{"robot_id": S.robot_id, "session_id": S.sid, "start": iso(clock(a)), "end": iso(clock(b))} for a, b in S.station]


def truth_summary(sessions: list[Session]) -> dict:
    tot = defaultdict(float)
    for S in sessions:
        for a, b, st in S.truth:
            tot[st] += b - a
    active = sum(v for k, v in tot.items() if k not in ("idle", "unknown"))
    return {"ai_share": tot["ai"] / active, "active_h": active / 3600,
            "human_h": sum(v for k, v in tot.items() if k.startswith("human") or k == "shared") / 3600}


def simulate_pilot(seed: int = 7, hidden: dict | None = None, days: int = 10) -> list[Session]:
    """hidden = {robot_id: (first_day_index, hidden_rate_per_hour)} for a covert remote operator.
    Each session has its own random stream, so a fault changes only the sessions it touches."""
    day_frailty = np.random.default_rng(seed).gamma(5.0, 0.2, size=days)
    out = []
    for day in range(days):
        for i, (rid, f) in enumerate(ROBOT_FRAILTY.items()):
            h = (hidden or {}).get(rid)
            hidden_rate = h[1] if h and day >= h[0] else 0.0
            rng = np.random.default_rng([seed, i, day])
            out.append(simulate_session(rng, rid, day, BASE_RATE * f * day_frailty[day], hidden_rate))
    return out
