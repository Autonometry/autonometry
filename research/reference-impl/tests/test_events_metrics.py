"""Loader, metric and schema tests on hand-built logs with known answers."""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import autonomy_meter as am
from autonomy_meter import metrics as mx

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0" * 64
T0 = pd.Timestamp("2026-10-30T09:00:00Z")


def ts(sec: float) -> str:
    return (T0 + pd.Timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(round((sec % 1) * 1000)):03d}Z"


class Log:
    """Tiny builder: every call appends an event with auto seq; heartbeats fill gaps."""

    def __init__(self, sid="s1", rid="r1", scopes=("whole_robot",), init="idle", hb=1.0, policy="hint_as_shared"):
        self.sid, self.rid, self.seq, self.task, self.ev = sid, rid, 0, None, []
        self.add(0, type="session_start", scopes=list(scopes), initial_states={s: init for s in scopes},
                 heartbeat_interval_s=hb, hint_policy=policy, task_registry_hash=ZERO, plan_hash=ZERO)

    def add(self, sec, **kw):
        e = {"robot_id": self.rid, "session_id": self.sid, "task_id": self.task, "seq": self.seq, "ts": ts(sec)}
        e.update(kw); self.ev.append(e); self.seq += 1; return e

    def hb(self, a, b, step=1.0, dist=None):
        s = a + step
        while s < b:
            kw = {} if dist is None else {"distance_m": dist(s)}
            self.add(s, type="heartbeat", **kw); s += step

    def tr(self, sec, state, scope="whole_robot", reason=None, actor=None, **kw):
        return self.add(sec, type="transition", scope=scope, state=state, reason=reason, actor_id=actor, **kw)

    def task_start(self, sec, tid, tdef="t_v1"):
        self.task = tid; return self.add(sec, type="task_start", task_definition_id=tdef)

    def task_end(self, sec, outcome="success"):
        e = self.add(sec, type="task_end", outcome=outcome); self.task = None; return e

    def end(self, sec):
        return self.add(sec, type="session_end")


def simple_session(sid="s1", rid="r1", ai=(0, 100), human=None, approvals=(), total=120):
    """One task, AI from 0..100 with an optional human_remote window, idle after, hb every 1 s."""
    L = Log(sid, rid)
    L.task_start(0, f"{sid}_t1")
    L.tr(0, "ai", actor="ai:p")
    cur = 0
    if human:
        a, b = human
        L.hb(cur, a); L.add(a - 0.5, type="help_request", scope="whole_robot")
        L.tr(a, "human_remote", reason="ai_requested_help", actor="op"); L.hb(a, b)
        L.tr(b, "ai", actor="ai:p"); cur = b
    for sec, dwell in approvals:
        L.hb(cur, sec); L.add(sec, type="approval", scope="whole_robot", actor_id="op", reason="ai_requested_help", dwell_s=dwell); cur = sec
    L.hb(cur, 100); L.task_end(100); L.tr(100, "idle"); L.hb(100, total); L.end(total)
    return L.ev


# ---------------------------------------------------------------- schema
def test_schema_examples_validate_and_example_log_validates():
    schema = am.load_schema()
    assert am.validate_events(schema["examples"], schema) == []
    log = ROOT / "examples" / "example_log.jsonl"
    if log.exists():
        evs = am.read_jsonl(log)
        assert am.validate_events(evs) == []
        ok, problems = am.verify_chain(evs)
        assert ok, problems[:3]
    # negative: human state without reason; planned without plan_item_id
    bad = dict(schema["examples"][4]); bad["reason"] = None
    assert am.validate_events([bad], schema)
    bad2 = dict(schema["examples"][8]); bad2.pop("plan_item_id")
    assert am.validate_events([bad2], schema)


def test_hash_chain_detects_edit():
    evs = am.read_jsonl(ROOT / "examples" / "example_log.jsonl")
    evs[5] = dict(evs[5]); evs[5]["ts"] = evs[5]["ts"].replace("08:", "09:", 1)
    ok, problems = am.verify_chain(evs)
    assert not ok and any("hash mismatch" in p for p in problems)


# ---------------------------------------------------------------- loader
def test_time_accounting_single_session():
    t = am.build_tables(simple_session(human=(40, 50), approvals=((70, 3.0),)))
    s = t.sessions.iloc[0]
    assert s["observed_s"] == 120 and s["active_s"] == pytest.approx(100) and s["idle_s"] == pytest.approx(20)
    assert s["ai_s"] == pytest.approx(90) and s["human_remote_s"] == pytest.approx(10) and s["unknown_s"] == 0
    assert s["n_interventions"] == 2 and s["n_takeovers"] == 1 and s["n_approvals"] == 1
    assert s["approval_human_s"] == pytest.approx(3.0)  # dwell 3 > default min 0
    tk = t.tasks.iloc[0]
    assert tk["human_s"] == pytest.approx(13.0) and not tk["autonomous"] and tk["success"]
    assert t.flags.empty


def test_zero_interventions_task_autonomous_and_rate_zero():
    t = am.build_tables(simple_session())
    assert t.sessions.iloc[0]["n_interventions"] == 0 and t.tasks.iloc[0]["autonomous"]
    r = mx.interventions_per_hour(t, n_boot=50)
    assert r["count"] == 0 and r["estimate"] == 0 and r["ci_poisson_exact"][0] == 0
    assert r["ci_poisson_exact"][1] == pytest.approx(-math.log(0.025) / (100 / 3600))
    share = mx.time_autonomy_share(t, n_boot=50)
    assert share["estimate"] == 1.0 and share["default"]["interval_method"].startswith("wilson_effective_n")


def test_unknown_gap_and_bounds():
    # heartbeats at 1..39 and 71..99 -> 32 s silence inside AI time -> 30 s unknown (2 s tolerance)
    L = Log(); L.task_start(0, "t1"); L.tr(0, "ai", actor="ai:p"); L.hb(0, 40); L.hb(70, 100); L.task_end(100); L.tr(100, "idle"); L.end(100)
    t = am.build_tables(L.ev)
    s = t.sessions.iloc[0]
    assert s["unknown_s"] == pytest.approx(30) and s["ai_s"] == pytest.approx(70) and s["active_s"] == pytest.approx(70)
    assert (t.flags["code"] == "time_gap").sum() == 1
    r = mx.time_autonomy_share(t, n_boot=20)
    assert r["estimate"] == 1.0 and r["lower"]["estimate"] == pytest.approx(70 / 100) and r["upper"]["estimate"] == 1.0
    assert t.tasks.iloc[0]["has_unknown"] and not t.tasks.iloc[0]["autonomous_lower"] and t.tasks.iloc[0]["autonomous_upper"]
    u = mx.unmeasured(t); assert u["conformance"] == "incomplete" and u["unknown_share"] == pytest.approx(0.30)


def test_all_unknown_time():
    L = Log(init="unknown"); L.add(1, type="heartbeat"); L.end(60)
    L.ev[0]["initial_states"] = {}
    t = am.build_tables(L.ev)
    s = t.sessions.iloc[0]
    assert s["active_s"] == 0 and s["unknown_s"] > 0  # after the gap rule everything is unknown
    assert mx.time_autonomy_share(t)["estimate"] is None and mx.unmeasured(t)["conformance"] == "incomplete"
    assert mx.interventions_per_hour(t)["estimate"] is None


def test_seq_gap_marks_unknown_and_help_reason_recoded():
    L = Log(); L.task_start(0, "t1"); L.tr(0, "ai", actor="ai:p"); L.hb(0, 10)
    L.seq += 3  # three lost events
    L.tr(10, "human_local", reason="ai_requested_help", actor="tech")  # no help_request -> recoded
    L.hb(10, 20); L.tr(20, "ai", actor="ai:p"); L.hb(20, 30); L.task_end(30); L.tr(30, "idle"); L.end(30)
    t = am.build_tables(L.ev)
    assert set(t.flags["code"]) == {"seq_gap", "help_reason_recoded"}
    assert t.sessions.iloc[0]["unknown_s"] == pytest.approx(1.0)  # interval between the surrounding events
    assert t.interventions.iloc[0]["reason"] == "human_initiated"


def test_planned_reset_counts_as_human_time_not_intervention_and_excluded_from_task():
    L = Log(); L.task_start(0, "t1"); L.tr(0, "ai", actor="ai:p"); L.hb(0, 50); L.task_end(50)
    L.tr(50, "human_local", reason="planned", actor="tech", plan_item_id="reset"); L.hb(50, 60)
    L.tr(60, "idle"); L.hb(60, 70); L.task_start(70, "t2"); L.tr(70, "ai", actor="ai:p"); L.hb(70, 100); L.task_end(100); L.tr(100, "idle"); L.end(100)
    t = am.build_tables(L.ev)
    s = t.sessions.iloc[0]
    assert s["reset_human_s"] == pytest.approx(10) and s["reset_planned_human_s"] == pytest.approx(10)
    assert s["n_interventions"] == 0 and s["human_s"] == pytest.approx(10) and s["active_s"] == pytest.approx(90)
    assert mx.time_autonomy_share(t, n_boot=10)["estimate"] == pytest.approx(80 / 90)
    assert t.tasks["autonomous"].all() and (t.tasks["unplanned_reset_s"] == 0).all()
    # unplanned reset is attributed to the next task
    L.ev[[i for i, e in enumerate(L.ev) if e["type"] == "transition" and e.get("reason") == "planned"][0]]["reason"] = "human_initiated"
    t2 = am.build_tables(L.ev)
    assert t2.tasks.set_index("task_id").loc["t2", "unplanned_reset_s"] == pytest.approx(10)


def test_safety_stop_then_takeover_is_one_safety_intervention():
    L = Log(); L.task_start(0, "t1"); L.tr(0, "ai", actor="ai:p"); L.hb(0, 30)
    L.tr(30, "safety_stop", reason="safety"); L.hb(30, 40); L.tr(40, "human_local", reason="safety", actor="tech"); L.hb(40, 50)
    L.tr(50, "ai", actor="ai:p"); L.hb(50, 100); L.task_end(100, "aborted"); L.tr(100, "idle"); L.end(100)
    t = am.build_tables(L.ev)
    s = t.sessions.iloc[0]
    assert s["n_safety_stops"] == 1 and s["n_interventions"] == 1 and s["safety_stop_s"] == pytest.approx(10)
    assert t.interventions.query("kind == 'takeover'").iloc[0]["reason"] == "safety"
    r = mx.time_autonomy_share(t, n_boot=10)
    assert r["estimate"] == pytest.approx(80 / 100) and r["safety_stop_share"] == pytest.approx(0.1)


def test_hint_as_shared_expires_and_counts_as_intervention():
    L = Log(); L.task_start(0, "t1"); L.tr(0, "ai", actor="ai:p"); L.hb(0, 20)
    L.add(20, type="hint", scope="whole_robot", actor_id="op", ttl_s=15.0); L.hb(20, 100); L.task_end(100); L.tr(100, "idle"); L.end(100)
    t = am.build_tables(L.ev)
    s = t.sessions.iloc[0]
    assert s["shared_s"] == pytest.approx(15) and s["ai_s"] == pytest.approx(85) and s["n_hints"] == 1
    assert not t.tasks.iloc[0]["autonomous"]
    r = mx.time_autonomy_share(t, n_boot=10)
    assert r["estimate"] == pytest.approx(0.85) and r["shared_as_ai"]["estimate"] == pytest.approx(1.0)


def test_per_scope_any_human_vs_scope_weighted():
    L = Log(scopes=("left_arm", "right_arm"))
    L.task_start(0, "t1")
    L.tr(0, "ai", "left_arm", actor="ai:p"); L.tr(0, "ai", "right_arm", actor="ai:p"); L.hb(0, 40)
    L.tr(40, "human_remote", "left_arm", reason="human_initiated", actor="op"); L.hb(40, 60)
    L.tr(60, "ai", "left_arm", actor="ai:p"); L.hb(60, 100); L.task_end(100)
    L.tr(100, "idle", "left_arm"); L.tr(100, "idle", "right_arm"); L.end(100)
    t = am.build_tables(L.ev)
    s = t.sessions.iloc[0]
    assert s["n_scopes"] == 2 and s["human_s"] == pytest.approx(20) and s["ai_s"] == pytest.approx(80)
    assert s["sw_ai_s"] == pytest.approx(0.5 * 80 + 0.5 * 100) and s["sw_active_s"] == pytest.approx(100)
    r = mx.time_autonomy_share(t, n_boot=10)
    assert r["estimate"] == pytest.approx(0.8) and r["scope_weighted"]["estimate"] == pytest.approx(0.9)
    assert s["human_actor_s"] == pytest.approx(20) and s["n_human_actors"] == 1
    # simultaneous two-scope takeover counts once in the whole-robot count
    L2 = Log(scopes=("a", "b")); L2.task_start(0, "t1"); L2.tr(0, "ai", "a", actor="ai:p"); L2.tr(0, "ai", "b", actor="ai:p"); L2.hb(0, 10)
    L2.tr(10, "human_local", "a", reason="human_initiated", actor="x"); L2.tr(10.2, "human_local", "b", reason="human_initiated", actor="x")
    L2.hb(10.2, 20); L2.task_end(20); L2.end(20)
    s2 = am.build_tables(L2.ev).sessions.iloc[0]
    assert s2["n_interventions"] == 2 and s2["n_interventions_whole"] == 1


def test_supervision_ratio():
    L = Log(); L.task_start(0, "t1"); L.tr(0, "ai", actor="ai:p"); L.hb(0, 50)
    L.add(50, type="supervision", level="remote_monitoring", n_supervisors=2); L.hb(50, 100); L.task_end(100); L.tr(100, "idle"); L.end(100)
    s = am.build_tables(L.ev).sessions.iloc[0]
    assert s["supervision_ratio"] == pytest.approx(0.5) and s["supervisor_hours_per_robot_hour"] == pytest.approx(1.0)


# ---------------------------------------------------------------- metrics across sessions
def multi(n=8, seed=0):
    rng = np.random.default_rng(seed)
    evs = []
    for i in range(n):
        human = (30, 30 + int(rng.integers(5, 20))) if i % 2 == 0 else None
        evs += simple_session(sid=f"s{i}", rid=f"r{i % 2}", human=human)
    return am.build_tables(evs)


def test_task_rate_wilson_vs_statsmodels_and_cluster_robust():
    t = multi()
    r = mx.task_autonomy_rate(t)
    assert r["point"]["k"] == 4 and r["point"]["n"] == 8 and r["estimate"] == 0.5
    from statsmodels.stats.proportion import proportion_confint
    assert tuple(r["point"]["ci_wilson"]) == pytest.approx(proportion_confint(4, 8, method="wilson"))
    assert tuple(r["point"]["ci_clopper_pearson"]) == pytest.approx(proportion_confint(4, 8, method="beta"))
    assert r["point"]["n_clusters"] == 8 and r["point"]["ci_cluster_robust"][0] < 0.5 < r["point"]["ci_cluster_robust"][1]


def test_time_share_bootstrap_contains_estimate_and_bounds_order():
    t = multi()
    r = mx.time_autonomy_share(t, n_boot=200)
    assert r["lower"]["estimate"] <= r["estimate"] <= r["upper"]["estimate"]
    assert r["default"]["ci_bootstrap"][0] <= r["estimate"] <= r["default"]["ci_bootstrap"][1]
    assert r["outer_interval"][0] <= r["default"]["ci_bootstrap"][0] and r["outer_interval"][1] >= r["default"]["ci_bootstrap"][1]


def test_km_no_censoring_equals_empirical_and_all_censored():
    t = multi()
    g = mx.intervention_gaps(t, "time")
    assert (g["observed"] == 0).sum() == 8  # one censored tail per session
    from lifelines import KaplanMeierFitter
    d = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    km = KaplanMeierFitter().fit(d, np.ones(5))
    for x, emp in zip(d, 1 - np.arange(1, 6) / 5):
        assert km.predict(x) == pytest.approx(emp)
    assert km.median_survival_time_ == 3.0
    # all sessions censored (no interventions): KM median undefined, exponential skipped
    t0 = am.build_tables(simple_session("a") + simple_session("b"))
    r = mx.gaps_between_interventions(t0)
    assert r["n_events"] == 0 and r["km"]["median"] is None and "note" in r["exponential"]
    assert r["km"]["survival_at"]["0.5"] == 1.0


def test_human_minutes_and_breakdowns_and_drift():
    t = multi()
    hm = mx.human_minutes_per_task(t, n_boot=100)
    assert hm["estimate"] == pytest.approx(t.tasks["human_s"].mean() / 60) and hm["ci_cluster_bootstrap_percentile"][0] <= hm["estimate"]
    b = mx.breakdowns(t)
    assert b["by_reason"]["ai_requested_help"]["count"] == 4 and b["by_task_type"]["t_v1"]["n_tasks"] == 8
    assert b["narrow_takeovers_human_initiated_or_safety"]["count"] == 0
    d = mx.drift(t, freq="h")
    assert d["n_bins"] == 1 and "note" in d["trend"]
    rep = mx.full_report(t, n_boot=50)
    json.dumps(rep)  # serialisable
    assert rep["n_sessions"] == 8 and rep["unmeasured"]["conformance"] == "complete"


def test_drift_trend_detects_increase():
    evs = []
    for day in range(10):
        L = Log(sid=f"d{day}")
        for e in L.ev: e["ts"] = ts(day * 86400)
        base = day * 86400
        L.task_start(base, f"d{day}_t"); L.tr(base, "ai", actor="ai:p")
        cur = base
        for k in range(day + 1):  # interventions grow with day
            a = base + 10 + 40 * k
            L.hb(cur, a); L.tr(a, "human_local", reason="human_initiated", actor="x"); L.hb(a, a + 5); L.tr(a + 5, "ai", actor="ai:p"); cur = a + 5
        L.hb(cur, base + 600); L.task_end(base + 600); L.tr(base + 600, "idle"); L.end(base + 600)
        evs += L.ev
    t = am.build_tables(evs)
    d = mx.drift(t, freq="D", cusum_h=1.5)  # trend inflates phi (~1.67), so the standardised CUSUM only reaches ~2 by bin 10
    assert d["n_bins"] == 10 and d["trend"]["slope_log_rate_per_bin"] > 0 and d["trend"]["p_value"] < 0.01
    # centred on the pooled rate, the early (low) bins trip the lower CUSUM first; the upper CUSUM climbs at the end
    assert d["cusum"]["first_signal"] is not None and d["cusum"]["first_signal"]["direction"] == "down"
    assert d["cusum"]["upper"][-1] > 1.5 and d["cusum"]["upper"][-1] > d["cusum"]["upper"][5]
