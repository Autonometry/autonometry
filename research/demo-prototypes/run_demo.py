"""Autonomy Meter demo: six dummy scenarios through the research reference package.

Usage:  python run_demo.py [path/to/reference]      (folder that contains autonomy_meter/)
Writes out/results.json, out/report_<scenario>.md and the fixture logs in out/.
"""
from __future__ import annotations

import copy
import json
import sys
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "reference"))
import autonomy_meter as am  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import gen  # noqa: E402
import seal  # noqa: E402
from reconcile import reconcile  # noqa: E402

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)
N_BOOT = 500
pct = lambda x: f"{100 * x:.1f}%"
ci_pct = lambda c: f"[{100 * c[0]:.1f}%, {100 * c[1]:.1f}%]"


def save_jsonl(name: str, events: list[dict]) -> None:
    (OUT / name).write_text("".join(json.dumps(e) + "\n" for e in events))


def analyse(events: list[dict]):
    t = am.build_tables(events, am.Params())
    return t, am.full_report(t, n_boot=N_BOOT, seed=1)


def headline(t, rep: dict) -> dict:
    ts, tr, ip, hm, un = (rep[k] for k in ("time_autonomy_share", "task_autonomy_rate",
                                            "interventions_per_active_hour", "human_minutes_per_task", "unmeasured"))
    s = t.sessions.groupby("robot_id")[["n_interventions", "active_s", "ai_s", "human_s", "reset_human_s"]].sum()
    return {
        "time_share": ts["estimate"], "time_share_ci": ts["ci"], "time_share_method": ts["interval_method"],
        "time_share_bounds": [ts["lower"]["estimate"], ts["upper"]["estimate"]], "time_share_outer": ts["outer_interval"],
        "task_rate": tr["estimate"], "task_rate_ci": tr["ci"], "task_rate_method": tr["interval_method"], "n_tasks": tr["n_tasks"],
        "per_hour": ip["estimate"], "per_hour_ci": ip["ci"], "per_hour_method": ip["interval_method"],
        "dispersion_index": ip["dispersion_index"], "interventions": ip["count"],
        "human_min_per_task": hm["estimate"], "human_min_ci": hm["ci"],
        "unknown_share": un["unknown_share"], "conformance": un["conformance"], "flags": un["flag_counts"],
        "active_h": ts["active_hours"], "reset_share_of_active": ts["reset_human_share_of_active"],
        "safety_stop_share": ts["safety_stop_share"],
        "by_reason": {k: v["count"] for k, v in rep["breakdowns"]["by_reason"].items()},
        "per_robot_rate": (s["n_interventions"] / (s["active_s"] / 3600)).round(2).to_dict(),
        "per_robot_share": (s["ai_s"] / s["active_s"]).round(4).to_dict(),
        "ai_s": float(s["ai_s"].sum()), "active_s": float(s["active_s"].sum()), "human_s": float(s["human_s"].sum()),
        "reset_s": float(s["reset_human_s"].sum()), "n_sessions": int(len(t.sessions)), "n_robots": int(len(s)),
    }


def suggestions(h: dict, levels: tuple[dict, dict] | None = None, rec: dict | None = None, robot_scope: str = "") -> list[str]:
    out = []
    if rec and not rec["L3_pass"]:
        worst = max(rec["by_robot"], key=lambda r: rec["by_robot"][r]["station_only_share"])
        adj = (h["ai_s"] - rec["station_only_s"]) / h["active_s"]
        out.append(f"Teleop station logged {rec['station_only_s'] / 60:.0f} min of remote control that the robots logged as AI "
                   f"({pct(rec['station_only_share'])} of active time, mostly on {worst}). Report downgraded from L3 to L2; "
                   f"witness-adjusted time autonomy is about {pct(adj)}, not {pct(h['time_share'])}.")
    if h["unknown_share"] > 0.001:
        lo, hi = h["time_share_bounds"]
        out.append(f"The meter could not see {pct(h['unknown_share'])} of the time{robot_scope}. True time autonomy is somewhere between "
                   f"{pct(lo)} (all gaps human) and {pct(hi)} (all gaps AI); quote the outer interval {ci_pct(h['time_share_outer'])}.")
        if h["conformance"] == "incomplete":
            out.append("Above the 5 % unknown limit: the report is INCOMPLETE. Fix the logger before publishing any autonomy number.")
    if levels:
        sess, robot = levels
        if robot["ci"][1] - robot["ci"][0] > 1.5 * (sess["ci"][1] - sess["ci"][0]):
            out.append(f"For this pilot the time share is {pct(sess['estimate'])} {ci_pct(sess['ci'])} ({h['n_sessions']} sessions). For a "
                       f"claim about the robot model only {h['n_robots']} robots back it: {ci_pct(robot['ci'])}. Add robots, not hours, to tighten it.")
    rates = h["per_robot_rate"]
    if len(rates) > 2:
        worst = max(rates, key=rates.get)
        others = np.median([v for k, v in rates.items() if k != worst])
        if rates[worst] > 1.5 * others:
            out.append(f"{worst} needs help {rates[worst] / others:.1f}x as often as the other robots ({rates[worst]:.1f}/h vs {others:.1f}/h). Check it first.")
    if h["reset_s"] > 0:
        lift = (h["ai_s"] + h["reset_s"]) / h["active_s"]
        out.append(f"Planned bin refills are {pct(h['reset_s'] / h['human_s'])} of all human time. "
                   f"Automating them would lift time autonomy from {pct(h['time_share'])} to about {pct(lift)}.")
    if h["n_tasks"]:
        out.append(f"Only {pct(h['task_rate'])} of tasks finished with zero human help, although {pct(h['time_share'])} of time was AI. "
                   f"Buyers paying per task should use the task rate.")
    return out


def render(title: str, h: dict, trust: str, sug: list[str], truth: dict | None = None) -> str:
    rows = [
        ("Time autonomy share", f"{pct(h['time_share'])} {ci_pct(h['time_share_ci'])}", h["time_share_method"]),
        ("Task autonomy rate", f"{pct(h['task_rate'])} {ci_pct(h['task_rate_ci'])} of {h['n_tasks']} tasks", h["task_rate_method"]),
        ("Interventions per active hour", f"{h['per_hour']:.2f} [{h['per_hour_ci'][0]:.2f}, {h['per_hour_ci'][1]:.2f}]", h["per_hour_method"]),
        ("Human minutes per task", f"{h['human_min_per_task']:.2f} [{h['human_min_ci'][0]:.2f}, {h['human_min_ci'][1]:.2f}]", "cluster bootstrap-t"),
        ("Unmeasured time", f"{pct(h['unknown_share'])} ({h['conformance']})", "bounds " + ci_pct(h["time_share_bounds"])),
    ]
    md = [f"# {title}", "", f"Trust level: {trust}  ·  active hours {h['active_h']:.1f}  ·  interventions {h['interventions']}", "",
          "| Metric | Estimate [95% CI] | Method |", "|---|---|---|"] + [f"| {a} | {b} | {c} |" for a, b, c in rows]
    if truth:
        md += ["", f"Ground truth (hidden from the meter): time autonomy {pct(truth['ai_share'])}"]
    md += ["", "## What the report suggests", ""] + [f"{i}. {s}" for i, s in enumerate(sug, 1)]
    return "\n".join(md) + "\n"


def main() -> None:
    results = {}
    key = Ed25519PrivateKey.generate()

    # S1 honest pilot ------------------------------------------------------------------------
    S1 = gen.simulate_pilot(seed=7)
    ev1 = [e for S in S1 for e in gen.robot_log(S)]
    clock_rng = np.random.default_rng(1)
    st1 = [x for S in S1 for x in gen.station_log(S, clock_rng)]
    t1, r1 = analyse(ev1)
    h1 = headline(t1, r1)
    sess_lvl = am.time_autonomy_share(t1, cluster_level="session", n_boot=N_BOOT, seed=1)
    robot_lvl = am.time_autonomy_share(t1, cluster_level="robot", n_boot=N_BOOT, seed=1)
    rec1 = reconcile(t1.whole, st1)
    sug1 = suggestions(h1, (sess_lvl, robot_lvl), rec1)
    results["S1_honest"] = {"headline": h1, "session_level_ci": sess_lvl["ci"], "robot_level_ci": robot_lvl["ci"],
                            "reconcile": rec1, "truth": gen.truth_summary(S1),
                            "schema_errors": len(am.validate_events(ev1)), "suggestions": sug1}
    (OUT / "report_S1_honest.md").write_text(render("S1 · Honest pilot (4 arms, 10 days)", h1,
                                                    "L3 passed" if rec1["L3_pass"] else "L3 failed", sug1, gen.truth_summary(S1)))
    save_jsonl("s1_robot_log.jsonl", ev1)
    (OUT / "s1_station_log.json").write_text(json.dumps(st1))

    # S2 hidden remote operator on the weakest robot from day 6 -------------------------------
    S2 = gen.simulate_pilot(seed=7, hidden={"r4": (5, 6.0)})
    ev2 = [e for S in S2 for e in gen.robot_log(S)]
    clock_rng = np.random.default_rng(1)
    st2 = [x for S in S2 for x in gen.station_log(S, clock_rng)]
    t2, r2 = analyse(ev2)
    h2 = headline(t2, r2)
    rec2 = reconcile(t2.whole, st2)
    sug2 = suggestions(h2, None, rec2)
    results["S2_hidden_operator"] = {"headline": h2, "reconcile": rec2, "truth": gen.truth_summary(S2), "suggestions": sug2,
                                     "truth_r4": gen.truth_summary([S for S in S2 if S.robot_id == "r4"])}
    (OUT / "report_S2_hidden_operator.md").write_text(render("S2 · Hidden remote operator on r4 (days 6-10)", h2,
                                                             "L2 (L3 failed)" if not rec2["L3_pass"] else "L3 passed", sug2, gen.truth_summary(S2)))
    save_jsonl("s2_robot_log.jsonl", ev2)
    (OUT / "s2_station_log.json").write_text(json.dumps(st2))

    # S3 logger crashes on r2 (day 4: 90 min with a 30-min human takeover inside; day 5: 60 min) ---
    S3 = gen.simulate_pilot(seed=7)
    crashes = {"r2-s004": (1500, 6900), "r2-s005": (2400, 6000)}
    for S in S3:
        if S.sid == "r2-s004":
            gen.overwrite_truth(S, S.t0 + 2100, S.t0 + 3900, "human_remote")
    ev3 = [e for S in S3 for e in gen.robot_log(S, crashes.get(S.sid))]
    t3, r3 = analyse(ev3)
    h3 = headline(t3, r3)
    t3r2, r3r2 = analyse([e for e in ev3 if e["robot_id"] == "r2"])
    h3r2 = headline(t3r2, r3r2)
    sug3 = suggestions(h3, None, None) + ["Robot r2 alone: " + s for s in suggestions(h3r2, None, None, " on r2")[:2]]
    results["S3_logger_crash"] = {"headline": h3, "headline_r2": h3r2, "truth": gen.truth_summary(S3),
                                  "truth_r2": gen.truth_summary([S for S in S3 if S.robot_id == "r2"]), "suggestions": sug3}
    (OUT / "report_S3_logger_crash.md").write_text(render("S3 · Logger crashes on r2 (days 4-5)", h3, "L2 with gaps", sug3, gen.truth_summary(S3)))
    save_jsonl("s3_robot_log.jsonl", ev3)

    # S4 tampering with one sealed session ------------------------------------------------------
    sess = next(S for S in S1 if S.sid == "r1-s002")
    plain = gen.robot_log(sess)
    sealed, cps = seal.seal(plain, key)
    anchored = copy.deepcopy(cps)   # copies sent out live, before any attack
    ref_sealed = seal.reference_chain(plain, am.events.event_hash)
    i_edit = next(i for i, e in enumerate(plain) if e.get("state") == "human_remote")
    cut = gen.iso(sess.t0 + gen.SESSION_S - 1200)

    def relabel(evs):
        out = [dict(e) for e in evs]
        out[i_edit].update(state="ai", reason=None, actor_id=gen.AI)
        return out

    def cut_and_close(evs):
        kept = [dict(e) for e in evs if e["ts"] < cut]
        end = {k: v for k, v in kept[-1].items() if k in ("robot_id", "session_id")}
        return kept + [end | {"task_id": None, "type": "session_end", "seq": kept[-1]["seq"] + 1, "ts": cut}]

    strip = lambda evs: [{k: v for k, v in e.items() if k not in ("hash", "prev_hash")} for e in evs]
    attacks = {
        "Change one remote takeover to 'ai'": (lambda: relabel(ref_sealed), lambda: (relabel(sealed), cps)),
        "Same edit, then recompute every hash and re-sign": (
            lambda: seal.reference_chain(relabel(plain), am.events.event_hash), lambda: seal.seal(relabel(plain), key)),
        "Delete the last 20 min, add a new session_end, re-sign": (
            lambda: seal.reference_chain(cut_and_close(plain), am.events.event_hash), lambda: seal.seal(cut_and_close(plain), key)),
        "Strip all hashes": (lambda: strip(ref_sealed), lambda: (strip(sealed), cps)),
    }
    tamper = {}
    for name, (ref_attack, our_attack) in attacks.items():
        ref_ok, _ = am.verify_chain(ref_attack())
        ev_t, own_cps = our_attack()
        signed_only_ok, _ = seal.verify(ev_t, key.public_key(), own_cps)
        anchored_ok, why = seal.verify(ev_t, key.public_key(), anchored)
        tamper[name] = {"plain_hash_chain (research package)": "passed" if ref_ok else "caught",
                        "signed checkpoints, no outside copy": "passed" if signed_only_ok else "caught",
                        "signed + anchored checkpoints": "passed" if anchored_ok else "caught", "why": why[:1]}
    clean_ok, _ = seal.verify(sealed, key.public_key(), anchored)
    results["S4_tamper"] = {"session": sess.sid, "events": len(sealed), "checkpoints": len(cps), "untouched_log_verifies": clean_ok, "attacks": tamper}
    save_jsonl("s4_sealed_session.jsonl", sealed)
    (OUT / "s4_checkpoints.json").write_text(json.dumps(cps, indent=1))

    # S5 "zero takeovers in 8 hours" on one robot -------------------------------------------------
    rng = np.random.default_rng(11)
    demo = [gen.simulate_session(rng, "demo1", d, rate=1e-9) for d in range(4)]
    ev5 = [e for S in demo for e in gen.robot_log(S)]
    t5 = am.build_tables(ev5, am.Params())
    try:
        am.full_report(t5, n_boot=N_BOOT, seed=1)
        crash = None
    except Exception as exc:  # the research package fails on zero interventions (drift GLM)
        crash = f"{type(exc).__name__}: {str(exc)[:90]}"
    ip5 = am.interventions_per_hour(t5, n_boot=N_BOOT, seed=1)
    T = am.time_autonomy_share(t5, n_boot=N_BOOT, seed=1)["active_hours"]
    a, alpha = 0.5, 0.05
    results["S5_zero_takeovers"] = {
        "full_report_crash": crash,
        "active_hours": T, "interventions": ip5["count"], "meter_ci_per_hour": ip5["ci"], "method": ip5["interval_method"],
        "honest_upper_independent": -np.log(alpha) / T,
        "honest_upper_one_robot_frailty_a0.5": (alpha ** -a - 1) / (a * T),
        "hours_needed_rate_below_0.1_independent": am.planning.hours_for_zero_events(0.1),
        "hours_needed_rate_below_0.1_one_robot": am.planning.hours_for_zero_events_nb(0.1, a)}

    # S6 LeRobot DAgger: the same run mapped two ways ---------------------------------------------
    rng = np.random.default_rng(3)
    phases, t = [], 0.0
    for k in range(1, 26):
        phases.append((t, "task_start", f"ep{k:02d}"))
        phases.append((t, "autonomous", f"ep{k:02d}"))
        t += rng.lognormal(np.log(60), 0.3)
        if rng.random() < 0.6:
            for ph, dur in (("paused", rng.uniform(5, 20)), ("correcting", rng.lognormal(np.log(25), 0.4)),
                            ("paused", rng.uniform(2, 5)), ("autonomous", rng.lognormal(np.log(30), 0.3))):
                phases.append((t, ph, f"ep{k:02d}"))
                t += dur
        phases.append((t, "task_end", f"ep{k:02d}"))
    end = t + 5

    def dagger_log(paused_as: str) -> list[dict]:
        base = {"robot_id": "so101_a", "session_id": "dagger-001"}
        evs = [base | {"task_id": None, "type": "session_start", "scopes": ["whole_robot"], "initial_states": {"whole_robot": "idle"},
                       "heartbeat_interval_s": 30.0, "hint_policy": "hint_as_shared", "task_registry_hash": gen.REGISTRY_HASH,
                       "plan_hash": gen.PLAN_HASH, "_t": 0.0}]
        state_kw = {"autonomous": {"state": "ai", "reason": None, "actor_id": "ai:act_v2"},
                    "correcting": {"state": "human_local", "reason": "human_initiated", "actor_id": "op_01"},
                    "paused": ({"state": "idle", "reason": None, "actor_id": None} if paused_as == "idle"
                               else {"state": "shared", "reason": "human_initiated", "actor_id": "op_01"})}
        for tt, ph, ep in phases:
            if ph == "task_start":
                evs.append(base | {"task_id": ep, "type": "task_start", "task_definition_id": "pick_place_v1", "_t": tt})
            elif ph == "task_end":
                evs.append(base | {"task_id": ep, "type": "task_end", "outcome": "success", "_t": tt})
            else:
                evs.append(base | {"task_id": ep, "type": "transition", "scope": "whole_robot", **state_kw[ph], "_t": tt})
        evs.append(base | {"task_id": None, "type": "transition", "scope": "whole_robot", "state": "idle", "reason": None, "actor_id": None, "_t": end - 4})
        hb = 25.0
        while hb < end:
            evs.append(base | {"task_id": None, "type": "heartbeat", "_t": hb})
            hb += 25.0
        evs.append(base | {"task_id": None, "type": "session_end", "_t": end})
        evs.sort(key=lambda e: e["_t"])
        return [{k: v for k, v in e.items() if k != "_t"} | {"seq": i, "ts": gen.iso(e["_t"])} for i, e in enumerate(evs)]

    s6 = {}
    for label, paused_as in (("PAUSED as idle (dossier mapping)", "idle"), ("PAUSED as shared (standard's rules)", "shared")):
        evs = dagger_log(paused_as)
        t6, r6 = analyse(evs)
        s6[label] = {"time_share": r6["time_autonomy_share"]["estimate"],
                     "interventions": r6["interventions_per_active_hour"]["count"],
                     "per_hour": r6["interventions_per_active_hour"]["estimate"],
                     "task_rate": r6["task_autonomy_rate"]["estimate"],
                     "active_h": r6["time_autonomy_share"]["active_hours"], "flags": r6["unmeasured"]["flag_counts"]}
        save_jsonl(f"s6_dagger_paused_as_{paused_as}.jsonl", evs)
    s6["true_correction_cycles"] = sum(1 for _, ph, _ in phases if ph == "correcting")
    results["S6_dagger_mapping"] = s6

    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=float))
    print(json.dumps(results, indent=1, default=float))


if __name__ == "__main__":
    main()
