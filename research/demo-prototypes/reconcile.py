"""Two-witness reconciliation prototype (trust level L3), following trust.md section 4.3.

Robot witness: whole-robot segments from the reference loader (`tables.whole`).
Station witness: control windows logged by the teleop station in its own clock.
Steps: estimate the clock offset from matched control starts (median, MAD), shift the station,
then sweep a 0.1 s grid of the robot's active time into both-human / robot-only / station-only,
ignoring +-tau around every boundary. Station-only time is human control the robot did not log.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RES = 0.1  # grid resolution in seconds


def _sec(ts) -> np.ndarray:
    return pd.to_datetime(pd.Series(ts), utc=True).astype("int64").to_numpy() / 1e9


def _mask(starts, ends, t0: float, n: int) -> np.ndarray:
    m = np.zeros(n, bool)
    for s, e in zip(starts, ends):
        m[max(0, int((s - t0) / RES)):max(0, int(np.ceil((e - t0) / RES)))] = True
    return m


def _dilate(m: np.ndarray, k: int) -> np.ndarray:
    c = np.concatenate([[0], np.cumsum(m)])
    i = np.arange(len(m))
    return (c[np.clip(i + k + 1, 0, len(m))] - c[np.clip(i - k, 0, len(m))]) > 0


def reconcile(whole: pd.DataFrame, station: list[dict], tau: float = 1.0) -> dict:
    w = whole.assign(s=_sec(whole["start"]), e=_sec(whole["end"]))
    st = pd.DataFrame(station or [], columns=["robot_id", "session_id", "start", "end"])
    st = st.assign(s=_sec(st["start"]) if len(st) else [], e=_sec(st["end"]) if len(st) else [])
    human = w[w["state"].isin(["human_remote", "shared"])]

    # 1. clock offset from matched control starts
    d = []
    for sid, g in st.groupby("session_id"):
        r = human.loc[human["session_id"] == sid, "s"].to_numpy()
        for s in g["s"]:
            if len(r) and np.min(np.abs(r - s)) < 5.0:
                d.append(r[np.argmin(np.abs(r - s))] - s)
    delta = float(np.median(d)) if d else 0.0
    spread = float(np.median(np.abs(np.array(d) - delta))) if d else 0.0

    # 2-4. shift and sweep, per session
    k = int(round(tau / RES))
    rows = []
    for (rid, sid), g in w.groupby(["robot_id", "session_id"]):
        t0, n = g["s"].min(), int(np.ceil((g["e"].max() - g["s"].min()) / RES))
        active = _mask(g.loc[~g["state"].isin(["idle", "unknown"]), "s"], g.loc[~g["state"].isin(["idle", "unknown"]), "e"], t0, n)
        unknown = _mask(g.loc[g["state"] == "unknown", "s"], g.loc[g["state"] == "unknown", "e"], t0, n)
        R = _mask(g.loc[g["state"].isin(["human_remote", "shared"]), "s"], g.loc[g["state"].isin(["human_remote", "shared"]), "e"], t0, n)
        sg = st[st["session_id"] == sid]
        B = _mask(sg["s"] + delta, sg["e"] + delta, t0, n)
        Rd, Bd = _dilate(R, k), _dilate(B, k)
        unmatched = sum(
            1 for s, e in zip(sg["s"] + delta, sg["e"] + delta)
            if Rd[max(0, int((s - t0) / RES)):max(1, int((e - t0) / RES))].mean() < 0.5)
        rows.append({"robot_id": rid, "session_id": sid,
                     "active_s": active.sum() * RES,
                     "station_only_s": (B & ~Rd & active).sum() * RES,
                     "robot_only_s": (R & ~Bd & active).sum() * RES,
                     "station_during_unknown_s": (B & unknown).sum() * RES,
                     "unmatched_station_episodes": unmatched})
    per = pd.DataFrame(rows)
    by_robot = per.groupby("robot_id")[["active_s", "station_only_s", "robot_only_s", "station_during_unknown_s", "unmatched_station_episodes"]].sum()
    tot = by_robot.sum()
    disputed = tot["station_only_s"] + tot["robot_only_s"]
    return {"tau_s": tau, "clock_offset_s": round(delta, 3), "offset_spread_s": round(spread, 3),
            "active_s": float(tot["active_s"]), "station_only_s": float(tot["station_only_s"]),
            "robot_only_s": float(tot["robot_only_s"]), "disputed_s": float(disputed),
            "station_only_share": float(tot["station_only_s"] / tot["active_s"]),
            "disputed_share": float(disputed / tot["active_s"]),
            "station_during_unknown_s": float(tot["station_during_unknown_s"]),
            "unmatched_station_episodes": int(tot["unmatched_station_episodes"]),
            "L3_pass": bool(tot["station_only_s"] / tot["active_s"] <= 0.005 and disputed / tot["active_s"] <= 0.02 and spread <= tau),
            "by_robot": by_robot.assign(station_only_share=by_robot["station_only_s"] / by_robot["active_s"]).round(4).to_dict("index")}
