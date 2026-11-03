"""run_supplement_robot_level.py -- supplementary cells: intervals that use the ROBOT (not the
session) as the cluster unit, on the same simulated datasets as run_coverage.py (identical cell
seeds). Methods: ratio-linearised t over robots (rate, hmpt), cluster-robust GLM with robot
clusters (task rate). Scenarios i (iid sessions) and iv (robot/day clustering), n = 5, 20, 80.
Output: results/supplement_robot_level.csv
"""
from __future__ import annotations
import math, os, sys, time
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get("AUTONOMY_METER_REF", os.path.join(os.path.dirname(HERE), "work", "reference")))
import simulate as sim
from run_coverage import BASE_SEED, N_SESSIONS
from autonomy_meter import intervals as iv

SCEN = ("i_baseline_poisson", "iv_robot_day_clustering")


def main(reps=1000):
    truth = pd.read_csv(os.path.join(HERE, "results", "truth.csv")).set_index("scenario")
    rows = []
    for sname in SCEN:
        cfg = sim.scenarios()[sname]; si = list(sim.scenarios()).index(sname)
        for ni, n in enumerate(N_SESSIONS):
            cell_seed = BASE_SEED + 1000 * si + ni
            for rep in range(reps):
                d = sim.simulate_sessions(cfg, n, np.random.default_rng([cell_seed, rep]))
                s, tk = d["sessions"], d["tasks"]
                byr = s.groupby("robot_id").agg(k=("n_interventions", "sum"), e=("active_s", "sum"))
                lin = iv.ratio_linearised_ci(byr["k"].values.astype(float), byr["e"].values / 3600.0)
                rows.append(dict(scenario=sname, n_sessions=n, rep=rep, metric="rate", method="lin_t_robot",
                                 est=lin["estimate"], lo=lin["lo"], hi=lin["hi"], truth=truth.loc[sname, "rate_true"]))
                g = tk.groupby("robot_id").agg(sm=("human_s", "sum"), c=("human_s", "size"))
                lin2 = iv.ratio_linearised_ci(g["sm"].values / 60.0, g["c"].values.astype(float))
                rows.append(dict(scenario=sname, n_sessions=n, rep=rep, metric="hmpt", method="lin_t_robot",
                                 est=lin2["estimate"], lo=lin2["lo"], hi=lin2["hi"], truth=truth.loc[sname, "hmpt_true"]))
                cr = iv.proportion_cluster_robust_ci(tk["autonomous"].astype(int).values, tk["robot_id"].values)
                rows.append(dict(scenario=sname, n_sessions=n, rep=rep, metric="task_rate", method="cluster_glm_robot",
                                 est=cr["p"], lo=cr["lo"], hi=cr["hi"], truth=truth.loc[sname, "task_rate_true"]))
            print("done", sname, n, flush=True)
    raw = pd.DataFrame(rows)
    raw["defined"] = raw["lo"].notna() & raw["hi"].notna()
    raw["covers"] = raw["defined"] & (raw["lo"] <= raw["truth"]) & (raw["truth"] <= raw["hi"])
    raw["width"] = raw["hi"] - raw["lo"]
    def agg(g):
        nd = int(g["defined"].sum()); cov = g.loc[g["defined"], "covers"].mean() if nd else np.nan
        return pd.Series({"n_reps": len(g), "n_defined": nd, "truth": g["truth"].iloc[0], "bias": g["est"].mean() - g["truth"].iloc[0],
                          "coverage": cov, "coverage_mcse": math.sqrt(cov * (1 - cov) / nd) if nd else np.nan,
                          "mean_width": g.loc[g["defined"], "width"].mean()})
    summ = raw.groupby(["metric", "method", "scenario", "n_sessions"], sort=False).apply(agg).reset_index()
    summ.to_csv(os.path.join(HERE, "results", "supplement_robot_level.csv"), index=False)
    print(summ.round(3).to_string())


if __name__ == "__main__":
    main()
