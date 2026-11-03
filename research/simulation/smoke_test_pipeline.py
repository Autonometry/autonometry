"""smoke_test_pipeline.py -- end-to-end check: simulated sessions -> schema-valid JSONL event log
-> autonomy_meter.validate_events / build_tables / full_report -> compare with the generator's
population truth (results/truth.csv) and with the generator's own observed tables.

Writes results/smoke_log.jsonl, results/smoke_report.json, results/smoke_comparison.csv.
"""
from __future__ import annotations
import json, os, sys
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get("AUTONOMY_METER_REF", os.path.join(os.path.dirname(HERE), "work", "reference")))
import simulate as sim
import autonomy_meter as am

SEED = 4242
N_SESSIONS = 12


def main():
    truth = pd.read_csv(os.path.join(HERE, "results", "truth.csv")).set_index("scenario")
    rows = []
    for scen in ("i_baseline_poisson", "vi_unknown_10pct"):
        cfg = sim.scenarios()[scen]
        events, dsim = sim.to_event_log(cfg, n_sessions=N_SESSIONS, seed=SEED, heartbeat_s=5.0)
        path = os.path.join(HERE, "results", f"smoke_log_{scen}.jsonl")
        sim.write_jsonl(events, path)
        errs = am.validate_events(events)
        params = am.Params(heartbeat_interval_s=5.0, gap_tolerance_s=5.0)
        tables = am.build_tables(events, params)
        rep = am.full_report(tables, alpha=0.05, n_boot=500, seed=0)
        json.dump(rep, open(os.path.join(HERE, "results", f"smoke_report_{scen}.json"), "w"), indent=1, default=str)
        s_pkg, s_sim = tables.sessions, dsim["sessions"]
        tr = truth.loc[scen]
        a = rep["time_autonomy_share"]; b = rep["task_autonomy_rate"]; c = rep["interventions_per_active_hour"]; e = rep["human_minutes_per_task"]
        d = rep["gaps_between_interventions_time"] if "gaps_between_interventions_time" in rep else rep.get("gaps_between_interventions", {})
        km = d.get("km", {}) if isinstance(d, dict) else {}
        def row(metric, pkg_est, pkg_ci, sim_est, tru, obs_lim):
            lo, hi = (pkg_ci or [np.nan, np.nan])
            rows.append(dict(scenario=scen, metric=metric, n_events_in_log=len(events), schema_errors=len(errs),
                             pipeline_estimate=pkg_est, ci_lo=lo, ci_hi=hi, generator_observed_estimate=sim_est,
                             truth=tru, truth_observed_limit=obs_lim, truth_in_ci=(lo <= tru <= hi) if lo == lo else np.nan,
                             observed_limit_in_ci=(lo <= obs_lim <= hi) if lo == lo else np.nan))
        # package default (a) moves approval/hint dwell from AI to human time; the generator's ai_s counts
        # takeover control time only, so compare like with like on both rows
        row("time_autonomy_share_pkg_default(approval_dwell_as_human)", a["estimate"], a["default"]["ci_bootstrap"],
            (s_sim["ai_s"].sum() - s_sim["approval_human_s"].sum()) / s_sim["active_s"].sum(), tr["share_true_incl_approval_dwell"],
            tr["share_observed_limit"] - (tr["share_true"] - tr["share_true_incl_approval_dwell"]))
        ai_only = s_pkg["ai_s"].sum() / s_pkg["active_s"].sum()
        _, blo, bhi = sim.boot_ratio_percentile(s_pkg["ai_s"].values, s_pkg["active_s"].values, 500, 0.05, 0)
        row("time_autonomy_share_takeover_only", ai_only, [blo, bhi], s_sim["ai_s"].sum() / s_sim["active_s"].sum(), tr["share_true"], tr["share_observed_limit"])
        row("task_autonomy_rate", b["estimate"], b["point"]["ci_wilson"], dsim["tasks"]["autonomous"].mean(), tr["task_rate_true"], tr["task_rate_observed_limit"])
        row("interventions_per_hour", c["estimate"], c["ci_poisson_exact"] if c["interval_method"] == "poisson_exact" else c["ci_cluster_bootstrap"],
            s_sim["n_interventions"].sum() / s_sim["active_s"].sum() * 3600, tr["rate_true"], tr["rate_observed_limit"])
        row("human_minutes_per_task", e["estimate"], e["ci_cluster_bootstrap_percentile"], dsim["tasks"]["human_s"].mean() / 60, tr["hmpt_true"], tr["hmpt_observed_limit"])
        row("gap_median_h", km.get("median"), km.get("median_ci"), np.nan, tr["gap_median_true_h"], tr["gap_median_true_h"])
        rows.append(dict(scenario=scen, metric="unknown_share", n_events_in_log=len(events), schema_errors=len(errs),
                         pipeline_estimate=rep["unmeasured"]["unknown_share"] if "unknown_share" in rep["unmeasured"] else np.nan,
                         generator_observed_estimate=s_sim["unknown_s"].sum() / s_sim["observed_s"].sum(), truth=tr["unknown_share"], truth_observed_limit=tr["unknown_share"]))
        print(scen, "events", len(events), "schema errors", len(errs), "flags", tables.flags["code"].value_counts().to_dict() if not tables.flags.empty else {})
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(HERE, "results", "smoke_comparison.csv"), index=False)
    print(df.round(4).to_string())
    return df


if __name__ == "__main__":
    main()
