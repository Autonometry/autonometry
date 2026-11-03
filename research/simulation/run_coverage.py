"""run_coverage.py -- Monte Carlo coverage study for the Autonomy Meter interval methods.

Stages (all resumable; outputs under simulation/results/):
  1. truth.csv            population truth per scenario (population_truth, N=40,000 sessions)
  2. raw/<scenario>_n<n>.csv.gz   one row per replicate x metric x method (estimate, lo, hi)
  3. coverage_summary.csv  bias, coverage (+ Monte Carlo SE), mean width per cell
  4. run_manifest.json     seeds, replicate counts, n_boot, runtime

Usage: python run_coverage.py [--reps 1000] [--n-boot 500] [--workers 3] [--pop 40000]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import warnings
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
REF = os.environ.get("AUTONOMY_METER_REF", os.path.join(os.path.dirname(HERE), "work", "reference"))
sys.path.insert(0, REF)

import simulate as sim  # noqa: E402

BASE_SEED = 20261030
N_SESSIONS = (5, 20, 80)
RESULTS = os.path.join(HERE, "results")
RAW = os.path.join(RESULTS, "raw")


def _lifelines():
    from lifelines import KaplanMeierFitter, ExponentialFitter
    from lifelines.utils import median_survival_times
    return KaplanMeierFitter, ExponentialFitter, median_survival_times


def evaluate_replicate(d: dict, n_boot: int, seed: int, alpha: float = 0.05) -> list[dict]:
    """All metric x method intervals for one replicate's observed tables."""
    from autonomy_meter import intervals as iv
    KM, EF, med_ci = _lifelines()
    s, tk, g = d["sessions"], d["tasks"], d["gaps"]
    rows = []

    def add(metric, method, est, lo, hi, **extra):
        rows.append(dict(metric=metric, method=method, est=est, lo=lo, hi=hi, **extra))

    # ---------------- (a) time autonomy share ----------------
    ai, act, unk = s["ai_s"].values, s["active_s"].values, s["unknown_s"].values
    est = ai.sum() / act.sum()
    e, lo, hi = sim.boot_ratio_percentile(ai, act, n_boot, alpha, seed)
    add("share", "boot_pct_session", est, lo, hi)
    lin = iv.ratio_linearised_ci(ai, act, alpha)
    add("share", "lin_t_session", est, lin["lo"], lin["hi"])
    wl, wh = iv.wilson_effective_ci(est, float(s["n_segments"].sum()), alpha)
    add("share", "wilson_eff_n", est, wl, wh, n_eff=float(s["n_segments"].sum()))
    byr = s.groupby("robot_id")[["ai_s", "active_s"]].sum()
    if len(byr) >= 2:
        _, rlo, rhi = sim.boot_ratio_percentile(byr["ai_s"].values, byr["active_s"].values, n_boot, alpha, seed)
        linr = iv.ratio_linearised_ci(byr["ai_s"].values, byr["active_s"].values, alpha)
    else:
        rlo = rhi = np.nan; linr = {"lo": np.nan, "hi": np.nan}
    add("share", "boot_pct_robot", est, rlo, rhi, n_clusters=len(byr))
    add("share", "lin_t_robot", est, linr["lo"], linr["hi"], n_clusters=len(byr))
    # bounds + bootstrap (package outer_interval): lower = unknown as human, upper = unknown as AI
    _, lo_l, _ = sim.boot_ratio_percentile(ai, act + unk, n_boot, alpha, seed)
    _, _, hi_u = sim.boot_ratio_percentile(ai + unk, act + unk, n_boot, alpha, seed)
    add("share", "outer_bounds_boot", est, lo_l, hi_u, lower_bound=ai.sum() / (act + unk).sum(), upper_bound=(ai + unk).sum() / (act + unk).sum())

    # ---------------- (c) interventions per active hour ----------------
    counts = s["n_interventions"].values.astype(float); expo = act / sim.HOUR
    K, E = counts.sum(), expo.sum()
    rate = K / E
    glo, ghi = iv.poisson_exact_ci(int(K), E, alpha)
    add("rate", "garwood_poisson", rate, glo, ghi)
    nb = iv.nb_rate_ci(counts, expo, alpha)
    add("rate", "nb2_wald", rate, nb["lo"], nb["hi"], nb_method=nb["method"], nb_alpha=nb["alpha_nb"])
    _, blo, bhi = sim.boot_ratio_percentile(counts, expo, n_boot, alpha, seed)
    add("rate", "boot_pct_session", rate, blo, bhi)
    byr2 = s.groupby("robot_id").agg(k=("n_interventions", "sum"), e=("active_s", "sum"))
    if len(byr2) >= 2:
        _, rblo, rbhi = sim.boot_ratio_percentile(byr2["k"].values.astype(float), byr2["e"].values / sim.HOUR, n_boot, alpha, seed)
    else:
        rblo = rbhi = np.nan
    add("rate", "boot_pct_robot", rate, rblo, rbhi, n_clusters=len(byr2))
    disp = iv.dispersion_index(counts, expo)
    use_boot = (not np.isnan(disp)) and disp > 1.5 and len(s) >= 2
    add("rate", "package_default_rule", rate, blo if use_boot else glo, bhi if use_boot else ghi, dispersion_index=disp, chose_boot=bool(use_boot))

    # ---------------- (b) task autonomy rate ----------------
    y = tk["autonomous"].astype(int).values
    n_t, k_t = len(y), int(y.sum())
    p_hat = k_t / n_t if n_t else np.nan
    wl, wh = iv.wilson_ci(k_t, n_t, alpha)
    add("task_rate", "wilson", p_hat, wl, wh, n_tasks=n_t)
    cl, ch = iv.clopper_pearson_ci(k_t, n_t, alpha)
    add("task_rate", "clopper_pearson", p_hat, cl, ch, n_tasks=n_t)
    cr = iv.proportion_cluster_robust_ci(y, tk["session_id"].values, alpha)
    add("task_rate", "cluster_glm_session", p_hat, cr["lo"], cr["hi"], n_tasks=n_t, glm_method=cr["method"])

    # ---------------- (e) human minutes per task ----------------
    gsum = tk.groupby("session_id", sort=False)["human_s"].agg(["sum", "count"])
    sums, cnts = gsum["sum"].values / 60.0, gsum["count"].values.astype(float)
    m_hat = sums.sum() / cnts.sum()
    _, plo, phi = sim.boot_ratio_percentile(sums, cnts, n_boot, alpha, seed)
    add("hmpt", "boot_pct_session", m_hat, plo, phi, n_tasks=n_t)
    _, tlo, thi = sim.boot_mean_t(sums, cnts, n_boot, alpha, seed)
    add("hmpt", "boot_t_session", m_hat, tlo, thi, n_tasks=n_t)
    _, vlo, vhi = sim.boot_mean_t_package_variant(sums, cnts, n_boot, alpha, seed)
    add("hmpt", "boot_t_session_pkg_v010", m_hat, vlo, vhi, n_tasks=n_t)

    # ---------------- (d) gaps between interventions ----------------
    gg = g[g["duration"] > 0] if (g["observed"] == 1).any() else g
    n_ev = int(gg["observed"].sum())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        km = KM(alpha=alpha).fit(gg["duration"], gg["observed"])
        med = float(km.median_survival_time_)
        mc = med_ci(km.confidence_interval_)
        add("gap_median", "km_exp_greenwood", med if np.isfinite(med) else np.nan, float(mc.iloc[0, 0]), float(mc.iloc[0, 1]), n_events=n_ev)
        if n_ev > 0:
            ef = EF(alpha=alpha).fit(gg["duration"], gg["observed"])
            sm_ = ef.summary
            lo_c = [c for c in sm_.columns if "lower" in c][0]; hi_c = [c for c in sm_.columns if "upper" in c][0]
            ln2 = math.log(2)
            add("gap_median", "exponential_wald", float(ef.lambda_) * ln2, float(sm_.loc["lambda_", lo_c]) * ln2, float(sm_.loc["lambda_", hi_c]) * ln2, n_events=n_ev)
        else:
            add("gap_median", "exponential_wald", np.nan, np.nan, np.nan, n_events=0)
    return rows


def run_cell(args):
    scen_name, n, reps, n_boot = args
    cfg = sim.scenarios()[scen_name]
    scen_idx = list(sim.scenarios()).index(scen_name)
    n_idx = N_SESSIONS.index(n)
    cell_seed = BASE_SEED + 1000 * scen_idx + n_idx
    out_path = os.path.join(RAW, f"{scen_name}_n{n}.csv.gz")
    if os.path.exists(out_path):
        return out_path, 0.0
    t0 = time.time()
    rows = []
    for rep in range(reps):
        rng = np.random.default_rng([cell_seed, rep])
        d = sim.simulate_sessions(cfg, n, rng)
        for r in evaluate_replicate(d, n_boot, seed=rep):
            r.update(scenario=scen_name, n_sessions=n, rep=rep)
            rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(out_path, index=False, compression="gzip")
    return out_path, time.time() - t0


def summarise(truth: pd.DataFrame) -> pd.DataFrame:
    files = sorted(f for f in os.listdir(RAW) if f.endswith(".csv.gz"))
    raw = pd.concat([pd.read_csv(os.path.join(RAW, f)) for f in files], ignore_index=True)
    tmap = {"share": "share_true", "rate": "rate_true", "task_rate": "task_rate_true", "hmpt": "hmpt_true", "gap_median": "gap_median_true_h"}
    omap = {"share": "share_observed_limit", "rate": "rate_observed_limit", "task_rate": "task_rate_observed_limit", "hmpt": "hmpt_observed_limit", "gap_median": "gap_median_true_h"}
    tr = truth.set_index("scenario")
    raw["truth"] = [tr.loc[sc, tmap[m]] for sc, m in zip(raw["scenario"], raw["metric"])]
    raw["truth_obs_limit"] = [tr.loc[sc, omap[m]] for sc, m in zip(raw["scenario"], raw["metric"])]
    raw["defined"] = raw["lo"].notna() & raw["hi"].notna()
    raw["covers"] = raw["defined"] & (raw["lo"] <= raw["truth"]) & (raw["truth"] <= raw["hi"])
    raw["covers_obs_limit"] = raw["defined"] & (raw["lo"] <= raw["truth_obs_limit"]) & (raw["truth_obs_limit"] <= raw["hi"])
    raw["width"] = raw["hi"] - raw["lo"]
    raw["finite_width"] = raw["defined"] & np.isfinite(raw["width"])
    raw["below"] = raw["defined"] & (raw["truth"] < raw["lo"])
    raw["above"] = raw["defined"] & (raw["truth"] > raw["hi"])

    def agg(gdf):
        nd = int(gdf["defined"].sum())
        cov = gdf.loc[gdf["defined"], "covers"].mean() if nd else np.nan
        fw = gdf.loc[gdf["finite_width"], "width"]
        return pd.Series({
            "n_reps": len(gdf), "n_defined": nd, "frac_undefined": 1 - nd / len(gdf),
            "truth": gdf["truth"].iloc[0], "truth_obs_limit": gdf["truth_obs_limit"].iloc[0],
            "mean_est": gdf["est"].mean(), "bias": gdf["est"].mean() - gdf["truth"].iloc[0],
            "rel_bias": (gdf["est"].mean() - gdf["truth"].iloc[0]) / gdf["truth"].iloc[0],
            "est_sd": gdf["est"].std(ddof=1),
            "coverage": cov, "coverage_mcse": math.sqrt(cov * (1 - cov) / nd) if nd and not np.isnan(cov) else np.nan,
            "miss_below": gdf.loc[gdf["defined"], "below"].mean() if nd else np.nan,
            "miss_above": gdf.loc[gdf["defined"], "above"].mean() if nd else np.nan,
            "coverage_obs_limit": gdf.loc[gdf["defined"], "covers_obs_limit"].mean() if nd else np.nan,
            "mean_width": fw.mean() if len(fw) else np.nan, "median_width": fw.median() if len(fw) else np.nan,
            "frac_infinite_width": 1 - len(fw) / nd if nd else np.nan,
            "mean_n_tasks": gdf["n_tasks"].mean() if "n_tasks" in gdf and gdf["n_tasks"].notna().any() else np.nan,
        })

    summ = raw.groupby(["metric", "method", "scenario", "n_sessions"], sort=False).apply(agg).reset_index()
    return summ, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=1000)
    ap.add_argument("--n-boot", type=int, default=500)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--pop", type=int, default=40000)
    ap.add_argument("--scenarios", nargs="*", default=None)
    ap.add_argument("--summarise-only", action="store_true")
    a = ap.parse_args()
    os.makedirs(RAW, exist_ok=True)
    t_start = time.time()
    truth_path = os.path.join(RESULTS, "truth.csv")
    if os.path.exists(truth_path):
        truth = pd.read_csv(truth_path)
    else:
        rows = []
        for name, cfg in sim.scenarios().items():
            t0 = time.time()
            r = sim.population_truth(cfg, n_sessions=a.pop, seed=BASE_SEED + 777)
            r["seconds"] = time.time() - t0
            rows.append(r)
            print(f"truth {name}: share={r['share_true']:.4f} rate={r['rate_true']:.3f} task={r['task_rate_true']:.4f} hmpt={r['hmpt_true']:.4f} gapmed={r['gap_median_true_h']:.4f} disp={r['dispersion_index_true_counts']:.2f} ({r['seconds']:.0f}s)", flush=True)
        truth = pd.DataFrame(rows)
        truth.to_csv(truth_path, index=False)
    scen = a.scenarios or list(sim.scenarios())
    cells = [(sname, n, a.reps, a.n_boot) for sname in scen for n in N_SESSIONS]
    cells.sort(key=lambda c: -c[1])  # big cells first for load balance
    timings = {}
    if a.summarise_only:
        cells = []
    if a.workers > 1:
        # NOTE: multiprocessing.Pool needs named pipes, which the sandbox used for this study
        # denies (WinError 5); the study was run as 3 independent shard processes (--workers 1
        # --scenarios ...) followed by --summarise-only.
        with Pool(a.workers) as pool:
            for path, dt in pool.imap_unordered(run_cell, cells):
                timings[os.path.basename(path)] = dt
                print(f"done {os.path.basename(path)} in {dt:.0f}s", flush=True)
    else:
        for c in cells:
            path, dt = run_cell(c)
            timings[os.path.basename(path)] = dt
            print(f"done {os.path.basename(path)} in {dt:.0f}s", flush=True)
    if not a.summarise_only:
        json.dump(timings, open(os.path.join(RESULTS, f"timings_{os.getpid()}.json"), "w"))
        if a.scenarios:
            print("shard finished", flush=True)
            return
    for f in os.listdir(RESULTS):
        if f.startswith("timings_") and f.endswith(".json"):
            timings.update(json.load(open(os.path.join(RESULTS, f))))
    summ, raw = summarise(truth)
    summ.to_csv(os.path.join(RESULTS, "coverage_summary.csv"), index=False)
    manifest = {"base_seed": BASE_SEED, "cell_seed_rule": "BASE_SEED + 1000*scenario_index + n_index; replicate rng = default_rng([cell_seed, rep]); bootstrap seed = rep",
                "reps_per_cell": a.reps, "n_boot": a.n_boot, "n_sessions": N_SESSIONS, "population_truth_sessions": a.pop,
                "truth_seed": BASE_SEED + 777, "scenarios": {k: v.as_dict() for k, v in sim.scenarios().items()},
                "cell_seconds": timings, "total_seconds": time.time() - t_start,
                "numpy": np.__version__, "pandas": pd.__version__}
    import scipy, statsmodels, lifelines
    manifest.update(scipy=scipy.__version__, statsmodels=statsmodels.__version__, lifelines=lifelines.__version__)
    json.dump(manifest, open(os.path.join(RESULTS, "run_manifest.json"), "w"), indent=1, default=str)
    print(f"total {time.time() - t_start:.0f}s; summary rows {len(summ)}")


if __name__ == "__main__":
    main()
