"""Event-log loading, validation and interval construction.

Implements definitions.md Sections 2-8. The public entry point is
:func:`build_tables`, which turns a list of event dicts into five pandas
tables (sessions, intervals, interventions, tasks, flags).

Conventions
-----------
* All times are pandas UTC Timestamps; durations are float seconds.
* A *segment* is a maximal run during which (session, scope, state, task_id,
  actor_id) is constant. Segments are split at every event so that task
  attribution is exact, then adjacent identical segments are merged.
* ``unknown`` is produced by (a) explicit transitions, (b) missing initial
  state, (c) a gap between consecutive events longer than ``gap_tolerance_s``
  (the part beyond the tolerance), (d) any gap in ``seq``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np
import pandas as pd

HUMAN_STATES = ("human_local", "human_remote", "shared")
ACTIVE_STATES = ("ai", "human_local", "human_remote", "shared", "safety_stop")
ALL_STATES = ("ai", "human_local", "human_remote", "shared", "safety_stop", "idle", "unknown")
REASONS = ("ai_requested_help", "human_initiated", "safety", "planned")

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "event.schema.json"


@dataclass
class Params:
    """Loader parameters (definitions.md Section 9). All are reported."""
    idle_threshold_s: float = 30.0
    gap_tolerance_s: float = 2.0
    heartbeat_interval_s: float = 1.0
    help_latency_max_s: float = 120.0
    approval_min_dwell_s: float = 0.0
    approval_sensitivity_dwell_s: float = 5.0
    approval_wait_max_s: float = 10.0
    hint_ttl_s: float = 60.0
    hint_min_dwell_s: float = 0.0
    takeover_window_s: float = 60.0
    simultaneity_window_s: float = 0.5
    unknown_share_max: float = 0.05
    shared_as_ai: bool = False  # sensitivity: count `shared` as AI time

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Tables:
    sessions: pd.DataFrame
    intervals: pd.DataFrame      # per-scope segments
    whole: pd.DataFrame          # whole-robot segments (any-human rule)
    interventions: pd.DataFrame
    tasks: pd.DataFrame
    flags: pd.DataFrame
    params: Params = field(default_factory=Params)


# --------------------------------------------------------------------------- #
# Loading and validation
# --------------------------------------------------------------------------- #
def load_schema(path: str | Path = SCHEMA_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path: str | Path) -> list[dict]:
    """Read a JSON Lines file into a list of event dicts (blank lines skipped)."""
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def validate_events(events: Iterable[dict], schema: dict | None = None) -> list[str]:
    """Validate every event against event.schema.json (JSON Schema 2020-12).

    Returns a list of human-readable error strings (empty if all valid).
    Uses ``jsonschema.Draft202012Validator`` with a FormatChecker so that
    ``format: date-time`` is enforced in addition to the regex pattern.
    """
    import jsonschema

    schema = schema or load_schema()
    v = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = []
    for i, ev in enumerate(events):
        for err in v.iter_errors(ev):
            errors.append(f"event[{i}] seq={ev.get('seq')}: {err.message}")
    return errors


def canonical_json(ev: dict) -> bytes:
    """Canonical serialisation used for the hash chain: sorted keys, no spaces,
    own ``hash`` field removed."""
    ev2 = {k: v for k, v in ev.items() if k != "hash"}
    return json.dumps(ev2, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def event_hash(ev: dict) -> str:
    return hashlib.sha256(canonical_json(ev)).hexdigest()


def verify_chain(events: list[dict]) -> tuple[bool, list[str]]:
    """Verify prev_hash/hash chain per session when hashes are present.

    Returns (chain_ok, problems). Sessions with no hashes at all are skipped
    (chain_ok stays True); a partially hashed session is a problem.
    """
    problems: list[str] = []
    for sid, evs in _group_sessions(events).items():
        hashed = [e for e in evs if "hash" in e]
        if not hashed:
            continue
        if len(hashed) != len(evs):
            problems.append(f"{sid}: {len(evs) - len(hashed)} events without hash")
        prev = None
        for e in evs:
            if "hash" not in e:
                continue
            if event_hash(e) != e["hash"]:
                problems.append(f"{sid} seq={e['seq']}: hash mismatch")
            if e.get("prev_hash") != prev:
                problems.append(f"{sid} seq={e['seq']}: prev_hash mismatch")
            prev = e["hash"]
    return (len(problems) == 0), problems


def _group_sessions(events: Iterable[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for e in events:
        groups.setdefault(e["session_id"], []).append(e)
    for sid in groups:
        groups[sid].sort(key=lambda e: e["seq"])
    return groups


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s)  # 'Z' parses to UTC


# --------------------------------------------------------------------------- #
# Interval construction
# --------------------------------------------------------------------------- #
class _Flags:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, session_id: str, seq, code: str, detail: str = "") -> None:
        self.rows.append({"session_id": session_id, "seq": seq, "code": code, "detail": detail})


def _session_tables(evs: list[dict], p: Params, flags: _Flags):
    """Build per-scope segments, interventions and tasks for one session."""
    sid = evs[0]["session_id"]
    rid = evs[0]["robot_id"]
    start_ev = evs[0]
    if start_ev["type"] != "session_start":
        flags.add(sid, start_ev["seq"], "no_session_start", "first event is not session_start")
        scopes = ["whole_robot"]
        init = {}
        hint_policy = "hint_as_shared"
        weights = {}
        hb = p.heartbeat_interval_s
        t0 = _ts(start_ev["ts"])
    else:
        scopes = list(start_ev["scopes"])
        init = dict(start_ev.get("initial_states", {}))
        hint_policy = start_ev.get("hint_policy", "hint_as_shared")
        weights = dict(start_ev.get("scope_weights", {}))
        hb = float(start_ev.get("heartbeat_interval_s", p.heartbeat_interval_s))
        t0 = _ts(start_ev["ts"])
    if evs[-1]["type"] != "session_end":
        flags.add(sid, evs[-1]["seq"], "no_session_end", "session closed at last event")
    t_end = _ts(evs[-1]["ts"])
    gap_tol = max(p.gap_tolerance_s, hb)

    # per-scope running state
    state = {s: init.get(s, "unknown") for s in scopes}
    actor = {s: (None if state[s] != "ai" else None) for s in scopes}
    reason = {s: None for s in scopes}
    for s in scopes:
        if s not in init:
            flags.add(sid, start_ev["seq"], "missing_initial_state", s)
    seg_rows: list[dict] = []
    interventions: list[dict] = []
    tasks: dict[str, dict] = {}
    task_order: list[str] = []
    open_task: str | None = None
    last_help: dict[str, pd.Timestamp] = {}
    last_safety_from_ai: dict[str, pd.Timestamp] = {}
    last_takeover_ts: pd.Timestamp | None = None
    sup_level = "none"
    sup_n = 0
    sup_rows: list[dict] = []
    hint_expiry: dict[str, pd.Timestamp] = {}
    dist = 0.0
    dist_rows: list[tuple[pd.Timestamp, float]] = [(t0, 0.0)]
    prev_ts = t0
    prev_seq = start_ev["seq"]
    seg_start = t0

    def emit(a: pd.Timestamp, b: pd.Timestamp, override: str | None = None) -> None:
        """Emit one segment per scope on [a, b)."""
        if b <= a:
            return
        for s in scopes:
            st = override or state[s]
            seg_rows.append({
                "robot_id": rid, "session_id": sid, "scope": s, "state": st,
                "start": a, "end": b, "task_id": open_task,
                "actor_id": None if override else actor[s],
                "reason": None if override else reason[s],
            })
        sup_rows.append({"start": a, "end": b, "level": sup_level, "n": sup_n})

    for ev in evs[1:] if start_ev["type"] == "session_start" else evs:
        ts = _ts(ev["ts"])
        seq = ev["seq"]
        if ts < prev_ts:
            flags.add(sid, seq, "ts_regression", f"{ev['ts']} < previous")
            ts = prev_ts
        # hint expiry (hint_as_shared): revert shared->ai at expiry if no explicit transition
        for s, texp in list(hint_expiry.items()):
            if texp <= ts and state[s] == "shared":
                emit(seg_start, texp)
                state[s], reason[s], actor[s] = "ai", None, actor.get(s)
                seg_start = texp
                del hint_expiry[s]
        # gaps
        if seq != prev_seq + 1:
            flags.add(sid, seq, "seq_gap", f"expected {prev_seq + 1}")
            emit(seg_start, prev_ts)
            emit(prev_ts, ts, override="unknown")
            seg_start = ts
        elif (ts - prev_ts).total_seconds() > gap_tol:
            cut = prev_ts + pd.Timedelta(seconds=gap_tol)
            flags.add(sid, seq, "time_gap", f"{(ts - prev_ts).total_seconds():.1f}s > {gap_tol}s")
            emit(seg_start, cut)
            emit(cut, ts, override="unknown")
            seg_start = ts
        typ = ev["type"]
        if typ == "transition":
            sc = ev["scope"]
            if sc not in scopes:
                flags.add(sid, seq, "undeclared_scope", sc)
                scopes.append(sc); state[sc] = "unknown"; actor[sc] = None; reason[sc] = None
            new = ev["state"]
            rsn = ev.get("reason")
            if rsn == "ai_requested_help":
                lh = last_help.get(sc)
                if lh is None or (ts - lh).total_seconds() > p.help_latency_max_s:
                    flags.add(sid, seq, "help_reason_recoded", "no help_request within window")
                    rsn = "human_initiated"
            emit(seg_start, ts)
            seg_start = ts
            old = state[sc]
            if new in HUMAN_STATES:
                # Section 6.3: between tasks, a human transition from idle/unknown, or any
                # pre-registered ('planned') human transition, is a reset, not an intervention
                is_reset = (open_task is None) and (old in ("idle", "unknown") or rsn == "planned")
                from_ai = old == "ai" and not is_reset
                from_safety = (old == "safety_stop" and sc in last_safety_from_ai and
                               (ts - last_safety_from_ai[sc]).total_seconds() <= p.takeover_window_s)
                if from_ai or from_safety:
                    simultaneous = (last_takeover_ts is not None and
                                    (ts - last_takeover_ts).total_seconds() <= p.simultaneity_window_s)
                    interventions.append({
                        "robot_id": rid, "session_id": sid, "scope": sc, "ts": ts, "seq": seq,
                        "kind": "takeover", "to_state": new,
                        "reason": "safety" if from_safety else rsn,
                        "actor_id": ev.get("actor_id"), "task_id": open_task,
                        "distance_m": dist, "dwell_s": np.nan, "simultaneous": simultaneous,
                        "is_reset": False,
                    })
                    last_takeover_ts = ts
                elif is_reset:
                    pass  # reset: human time, not an intervention (Section 6.3)
            if new == "safety_stop" and old == "ai":
                last_safety_from_ai[sc] = ts
                interventions.append({
                    "robot_id": rid, "session_id": sid, "scope": sc, "ts": ts, "seq": seq,
                    "kind": "safety_stop", "to_state": new, "reason": "safety",
                    "actor_id": None, "task_id": open_task, "distance_m": dist,
                    "dwell_s": np.nan, "simultaneous": False, "is_reset": False,
                })
            state[sc], reason[sc], actor[sc] = new, rsn, ev.get("actor_id")
            if new == "shared" and sc in hint_expiry:
                pass
        elif typ in ("approval", "hint"):
            sc = ev["scope"]
            min_dwell = p.approval_min_dwell_s if typ == "approval" else p.hint_min_dwell_s
            dwell = float(ev.get("dwell_s", 0.0) or 0.0)
            if typ == "hint" and hint_policy == "hint_as_shared":
                # encode as shared interval of ttl (Section 6.2) -- counts as a takeover
                emit(seg_start, ts); seg_start = ts
                ttl = float(ev.get("ttl_s", p.hint_ttl_s))
                if state[sc] == "ai":
                    interventions.append({
                        "robot_id": rid, "session_id": sid, "scope": sc, "ts": ts, "seq": seq,
                        "kind": "hint", "to_state": "shared",
                        "reason": "ai_requested_help" if (sc in last_help and (ts - last_help[sc]).total_seconds() <= p.help_latency_max_s) else "human_initiated",
                        "actor_id": ev.get("actor_id"), "task_id": open_task, "distance_m": dist,
                        "dwell_s": np.nan, "simultaneous": False, "is_reset": False,
                    })
                state[sc], reason[sc], actor[sc] = "shared", "human_initiated", ev.get("actor_id")
                hint_expiry[sc] = ts + pd.Timedelta(seconds=ttl)
            else:
                interventions.append({
                    "robot_id": rid, "session_id": sid, "scope": sc, "ts": ts, "seq": seq,
                    "kind": typ, "to_state": state[sc], "reason": ev.get("reason", "human_initiated"),
                    "actor_id": ev.get("actor_id"), "task_id": open_task, "distance_m": dist,
                    "dwell_s": max(dwell, min_dwell), "simultaneous": False, "is_reset": False,
                })
        elif typ == "help_request":
            last_help[ev["scope"]] = ts
        elif typ == "task_start":
            tid = ev["task_id"]
            if open_task is not None:
                flags.add(sid, seq, "nested_task", f"{open_task} still open")
                tasks[open_task]["end"] = ts
                tasks[open_task]["outcome"] = "unknown"
            emit(seg_start, ts); seg_start = ts
            open_task = tid
            tasks[tid] = {"robot_id": rid, "session_id": sid, "task_id": tid,
                          "task_definition_id": ev["task_definition_id"], "start": ts,
                          "end": pd.NaT, "outcome": "unknown", "start_distance_m": dist}
            task_order.append(tid)
        elif typ == "task_end":
            tid = ev["task_id"]
            emit(seg_start, ts); seg_start = ts
            if tid in tasks and pd.isna(tasks[tid]["end"]):
                tasks[tid]["end"] = ts
                tasks[tid]["outcome"] = ev["outcome"]
                tasks[tid]["end_distance_m"] = dist
            else:
                flags.add(sid, seq, "task_end_without_start", tid)
            if open_task == tid:
                open_task = None
        elif typ == "supervision":
            emit(seg_start, ts); seg_start = ts
            sup_level, sup_n = ev["level"], int(ev["n_supervisors"])
        elif typ == "heartbeat":
            pass
        if "distance_m" in ev and ev["distance_m"] is not None:
            d = float(ev["distance_m"])
            if d < dist:
                flags.add(sid, seq, "distance_regression", f"{d} < {dist}")
            dist = max(dist, d)
            dist_rows.append((ts, dist))
        if typ == "session_end":
            emit(seg_start, ts); seg_start = ts
        prev_ts, prev_seq = ts, seq
    if seg_start < t_end:
        emit(seg_start, t_end)
    for tid in task_order:
        if pd.isna(tasks[tid]["end"]):
            tasks[tid]["end"] = t_end
            tasks[tid]["outcome"] = "unknown"
            tasks[tid]["end_distance_m"] = dist
            flags.add(sid, None, "task_not_closed", tid)
    sess = {"robot_id": rid, "session_id": sid, "start": t0, "end": t_end,
            "observed_s": (t_end - t0).total_seconds(), "scopes": tuple(scopes),
            "scope_weights": weights, "hint_policy": hint_policy, "distance_m": dist}
    return sess, seg_rows, interventions, list(tasks.values()), sup_rows


def _merge_segments(df: pd.DataFrame) -> pd.DataFrame:
    """Merge adjacent segments with identical (session, scope, state, task_id, actor_id, reason)."""
    if df.empty:
        return df
    df = df.sort_values(["session_id", "scope", "start"]).reset_index(drop=True)
    key_cols = ["session_id", "scope", "state", "task_id", "actor_id", "reason"]
    key = df[key_cols].astype(str).agg("|".join, axis=1)
    contiguous = (df["start"] == df["end"].shift(1)) & (key == key.shift(1))
    grp = (~contiguous).cumsum()
    out = df.groupby(grp, sort=False).agg(
        robot_id=("robot_id", "first"), session_id=("session_id", "first"), scope=("scope", "first"),
        state=("state", "first"), start=("start", "first"), end=("end", "last"),
        task_id=("task_id", "first"), actor_id=("actor_id", "first"), reason=("reason", "first"),
    ).reset_index(drop=True)
    out["dur_s"] = (out["end"] - out["start"]).dt.total_seconds()
    return out


def whole_robot_timeline(intervals: pd.DataFrame) -> pd.DataFrame:
    """Collapse per-scope segments to a whole-robot timeline (any-human rule, Section 6.6).

    Priority: human (any scope in human_local/human_remote/shared) > safety_stop >
    ai > idle (all idle) > unknown. The human sub-state recorded is 'human_remote' if
    any scope is remote-controlled, else 'human_local' if any local, else 'shared'.
    """
    rows = []
    for sid, g in intervals.groupby("session_id", sort=False):
        pts = np.unique(np.concatenate([g["start"].values, g["end"].values]))
        scopes = g["scope"].unique()
        # per-scope state lookup at each point via searchsorted
        per_scope = {}
        for sc, gs in g.groupby("scope"):
            gs = gs.sort_values("start")
            per_scope[sc] = (gs["start"].values, gs["state"].values, gs["task_id"].values)
        for a, b in zip(pts[:-1], pts[1:]):
            states = []
            task = None
            for sc in scopes:
                st_arr, s_arr, t_arr = per_scope[sc]
                i = np.searchsorted(st_arr, a, side="right") - 1
                if i >= 0:
                    states.append(s_arr[i])
                    task = t_arr[i] if task is None else task
                else:
                    states.append("unknown")
            if "human_remote" in states:
                w = "human_remote"
            elif "human_local" in states:
                w = "human_local"
            elif "shared" in states:
                w = "shared"
            elif "safety_stop" in states:
                w = "safety_stop"
            elif "ai" in states:
                w = "ai"
            elif all(s == "idle" for s in states):
                w = "idle"
            else:
                w = "unknown"
            rows.append({"robot_id": g["robot_id"].iloc[0], "session_id": sid, "state": w,
                         "start": pd.Timestamp(a), "end": pd.Timestamp(b), "task_id": task})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["dur_s"] = (out["end"] - out["start"]).dt.total_seconds()
    # merge adjacent identical
    key = out[["session_id", "state", "task_id"]].astype(str).agg("|".join, axis=1)
    contiguous = (out["start"] == out["end"].shift(1)) & (key == key.shift(1))
    grp = (~contiguous).cumsum()
    out = out.groupby(grp, sort=False).agg(
        robot_id=("robot_id", "first"), session_id=("session_id", "first"), state=("state", "first"),
        start=("start", "first"), end=("end", "last"), task_id=("task_id", "first")).reset_index(drop=True)
    out["dur_s"] = (out["end"] - out["start"]).dt.total_seconds()
    return out


def _state_times(df: pd.DataFrame, weights: dict | None = None) -> dict:
    """Sum dur_s by state; optional per-scope weights (scope-weighted rule)."""
    if df.empty:
        return {s: 0.0 for s in ALL_STATES}
    if weights is not None and "scope" in df:
        scopes = df["scope"].unique()
        w = {s: float(weights.get(s, np.nan)) for s in scopes}
        if any(np.isnan(v) for v in w.values()):
            w = {s: 1.0 / len(scopes) for s in scopes}
        else:
            tot = sum(w.values()); w = {k: v / tot for k, v in w.items()}
        d = df.assign(wd=df["dur_s"] * df["scope"].map(w)).groupby("state")["wd"].sum()
    else:
        d = df.groupby("state")["dur_s"].sum()
    return {s: float(d.get(s, 0.0)) for s in ALL_STATES}


def build_tables(events: Iterable[dict], params: Params | None = None, validate: bool = False) -> Tables:
    """Build the analysis tables from events.

    Parameters
    ----------
    events : iterable of dict
        Events conforming to event.schema.json (any order; sorted by seq per session).
    params : Params
    validate : bool
        If True, run JSON-Schema validation first and raise ValueError on errors.

    Returns
    -------
    Tables with
      sessions: one row per session with observed_s, per-state seconds under the
                any-human rule (ai_s, human_s, shared_s, safety_stop_s, idle_s,
                unknown_s, active_s), scope-weighted analogues (sw_*), counts of
                interventions/approvals/hints/safety stops, approval_human_s,
                reset_human_s, supervision_ratio, supervisor_hours_per_robot_hour,
                n_human_actors, human_actor_s, distance_m.
      intervals: per-scope merged segments.
      whole: whole-robot segments (any-human rule).
      interventions: one row per intervention event (kind in takeover/approval/hint)
                     and per safety_stop (kind='safety_stop', not an intervention).
      tasks: one row per task with human_s, n_interventions, has_unknown,
             autonomous (point), autonomous_lower, autonomous_upper, success.
      flags: loader warnings (code, detail).
    """
    p = params or Params()
    events = list(events)
    if validate:
        errs = validate_events(events)
        if errs:
            raise ValueError("schema validation failed:\n" + "\n".join(errs[:20]))
    flags = _Flags()
    sess_rows, seg_rows, int_rows, task_rows, sup_by_sess = [], [], [], [], {}
    for sid, evs in _group_sessions(events).items():
        sess, segs, ints, tks, sups = _session_tables(evs, p, flags)
        sess_rows.append(sess); seg_rows += segs; int_rows += ints; task_rows += tks
        sup_by_sess[sid] = sups
    intervals = _merge_segments(pd.DataFrame(seg_rows))
    whole = whole_robot_timeline(intervals)
    int_cols = ["robot_id", "session_id", "scope", "ts", "seq", "kind", "to_state", "reason",
                "actor_id", "task_id", "distance_m", "dwell_s", "simultaneous", "is_reset"]
    interventions = pd.DataFrame(int_rows, columns=int_cols)
    task_cols = ["robot_id", "session_id", "task_id", "task_definition_id", "start", "end", "outcome",
                 "start_distance_m", "end_distance_m"]
    tasks = pd.DataFrame(task_rows, columns=task_cols)

    # ---- session summary ----
    srows = []
    for s in sess_rows:
        sid = s["session_id"]
        w = whole[whole["session_id"] == sid]
        iv = intervals[intervals["session_id"] == sid]
        st = _state_times(w)
        sw = _state_times(iv, weights=s["scope_weights"] or {})
        ints = interventions[interventions["session_id"] == sid]
        appr = ints[ints["kind"].isin(["approval", "hint"]) & ints["dwell_s"].notna()]
        human_s = st["human_local"] + st["human_remote"] + st["shared"]
        active_s = sum(st[k] for k in ACTIVE_STATES)
        reset = iv[iv["task_id"].isna() & iv["state"].isin(HUMAN_STATES)]
        reset_planned = reset[reset["reason"] == "planned"]["dur_s"].sum()
        sups = pd.DataFrame(sup_by_sess[sid])
        if not sups.empty:
            sups["dur"] = (sups["end"] - sups["start"]).dt.total_seconds()
            sup_ratio = float(sups.loc[sups["level"] != "none", "dur"].sum() / max(sups["dur"].sum(), 1e-9))
            sup_hours = float((sups["dur"] * sups["n"]).sum() / max(sups["dur"].sum(), 1e-9))
        else:
            sup_ratio, sup_hours = 0.0, 0.0
        human_iv = iv[iv["state"].isin(HUMAN_STATES)]
        srows.append({
            "robot_id": s["robot_id"], "session_id": sid, "start": s["start"], "end": s["end"],
            "observed_s": s["observed_s"], "n_scopes": len(s["scopes"]),
            "ai_s": st["ai"], "human_s": human_s, "human_local_s": st["human_local"],
            "human_remote_s": st["human_remote"], "shared_s": st["shared"],
            "safety_stop_s": st["safety_stop"], "idle_s": st["idle"], "unknown_s": st["unknown"],
            "active_s": active_s,
            "sw_ai_s": sw["ai"], "sw_human_s": sw["human_local"] + sw["human_remote"] + sw["shared"],
            "sw_shared_s": sw["shared"], "sw_active_s": sum(sw[k] for k in ACTIVE_STATES),
            "sw_unknown_s": sw["unknown"],
            "n_interventions": int((ints["kind"] != "safety_stop").sum()),
            "n_interventions_whole": int(((ints["kind"] != "safety_stop") & ~ints["simultaneous"]).sum()),
            "n_takeovers": int((ints["kind"] == "takeover").sum()),
            "n_approvals": int((ints["kind"] == "approval").sum()),
            "n_hints": int((ints["kind"] == "hint").sum()),
            "n_safety_stops": int((ints["kind"] == "safety_stop").sum()),
            "approval_human_s": float(appr["dwell_s"].sum()),
            "approval_human_s_sens": float(np.maximum(appr["dwell_s"], p.approval_sensitivity_dwell_s).sum()),
            "reset_human_s": float(reset["dur_s"].sum()),
            "reset_planned_human_s": float(reset_planned),
            "supervision_ratio": sup_ratio, "supervisor_hours_per_robot_hour": sup_hours,
            "n_human_actors": int(human_iv["actor_id"].nunique()),
            "human_actor_s": float(human_iv["dur_s"].sum()),  # per-scope sum = actor-seconds
            "distance_m": s["distance_m"], "hint_policy": s["hint_policy"],
        })
    sessions = pd.DataFrame(srows)

    # ---- task table ----
    if not tasks.empty:
        tasks["dur_s"] = (tasks["end"] - tasks["start"]).dt.total_seconds()
        tasks["distance_m"] = tasks["end_distance_m"].fillna(0) - tasks["start_distance_m"].fillna(0)
        hs, unk, nint, nhuman_ev, succ = [], [], [], [], []
        for _, t in tasks.iterrows():
            w = whole[(whole["session_id"] == t["session_id"]) & (whole["task_id"] == t["task_id"])]
            ints = interventions[(interventions["session_id"] == t["session_id"]) &
                                 (interventions["task_id"] == t["task_id"]) &
                                 (interventions["kind"] != "safety_stop")]
            h = float(w.loc[w["state"].isin(HUMAN_STATES), "dur_s"].sum()) + float(ints["dwell_s"].fillna(0).sum())
            hs.append(h)
            unk.append(bool((w["state"] == "unknown").any()) or t["outcome"] == "unknown")
            nint.append(int(len(ints)))
            nhuman_ev.append(int(len(ints)) + int((w["state"].isin(HUMAN_STATES)).sum()))
            succ.append(t["outcome"] == "success")
        tasks["human_s"] = hs
        tasks["has_unknown"] = unk
        tasks["n_interventions"] = nint
        tasks["n_human_events"] = nhuman_ev
        tasks["success"] = succ
        no_help = (tasks["n_human_events"] == 0)
        tasks["autonomous"] = no_help                                   # point: unknown ignored
        tasks["autonomous_lower"] = no_help & ~tasks["has_unknown"]      # unknown -> not autonomous
        tasks["autonomous_upper"] = no_help                             # unknown -> autonomous if otherwise clean
        tasks["autonomous_success"] = no_help & tasks["success"]
        # unplanned reset before task -> attributed to this task (Section 6.3)
        extra = []
        for _, t in tasks.iterrows():
            iv = intervals[(intervals["session_id"] == t["session_id"]) & intervals["task_id"].isna() &
                           intervals["state"].isin(HUMAN_STATES) & (intervals["reason"] != "planned") &
                           (intervals["end"] <= t["start"])]
            # only the resets since the previous task end
            prev_end = tasks[(tasks["session_id"] == t["session_id"]) & (tasks["end"] <= t["start"])]["end"].max()
            if pd.notna(prev_end):
                iv = iv[iv["start"] >= prev_end]
            extra.append(float(iv["dur_s"].sum()))
        tasks["unplanned_reset_s"] = extra
        tasks["human_s_incl_reset"] = tasks["human_s"] + tasks["unplanned_reset_s"]

    flags_df = pd.DataFrame(flags.rows, columns=["session_id", "seq", "code", "detail"])
    return Tables(sessions=sessions, intervals=intervals, whole=whole, interventions=interventions,
                  tasks=tasks, flags=flags_df, params=p)
