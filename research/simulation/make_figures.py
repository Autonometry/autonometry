"""make_figures.py -- coverage vs n and width vs n, one panel per metric, lines per method,
small multiples per scenario. Reads results/coverage_summary.csv."""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd
import matplotlib as mpl
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results"); FIG = os.path.join(HERE, "figures")

METRIC_LABEL = {"share": "time autonomy share", "rate": "interventions per active hour", "task_rate": "task autonomy rate",
                "hmpt": "human minutes per task", "gap_median": "median gap between interventions"}
METHOD_LABEL = {
    "boot_pct_session": "cluster bootstrap percentile (session)", "lin_t_session": "ratio-linearised t (session)",
    "wilson_eff_n": "Wilson, effective n = segments", "boot_pct_robot": "cluster bootstrap percentile (robot)",
    "lin_t_robot": "ratio-linearised t (robot)", "outer_bounds_boot": "outer interval (unknown bounds + bootstrap)",
    "garwood_poisson": "Poisson exact (Garwood)", "nb2_wald": "NB2 Wald", "package_default_rule": "package rule (Garwood if dispersion <= 1.5 else bootstrap)",
    "wilson": "Wilson", "clopper_pearson": "Clopper-Pearson", "cluster_glm_session": "cluster-robust GLM (session)",
    "boot_t_session": "cluster bootstrap-t (session)", "boot_t_session_pkg_v010": "bootstrap-t as in package v0.1.0 (duplicates merged)",
    "km_exp_greenwood": "Kaplan-Meier median (exp. Greenwood)", "exponential_wald": "exponential fit (Wald)",
}
SCEN_LABEL = {"i_baseline_poisson": "(i) Poisson, iid sessions", "ii_nb_overdispersion": "(ii) NB over-dispersion",
              "iii_hawkes_bursty": "(iii) Hawkes bursts", "iv_robot_day_clustering": "(iv) robot/day clustering",
              "v_drift_halving": "(v) drift (rate halves)", "vi_unknown_10pct": "(vi) 10 % unknown (informative)"}
# methods shown per metric (skip the robot-bootstrap which is dominated and clutters the plots)
SHOW = {"share": ["boot_pct_session", "lin_t_session", "lin_t_robot", "wilson_eff_n", "outer_bounds_boot"],
        "rate": ["garwood_poisson", "nb2_wald", "boot_pct_session", "package_default_rule"],
        "task_rate": ["wilson", "clopper_pearson", "cluster_glm_session"],
        "hmpt": ["boot_pct_session", "boot_t_session", "boot_t_session_pkg_v010"],
        "gap_median": ["km_exp_greenwood", "exponential_wald"]}
PALETTE = ["#1b6ca8", "#d95f02", "#1b9e77", "#7570b3", "#a6761d", "#e7298a"]
MARKERS = ["o", "s", "^", "D", "v", "P"]


def plot(summ: pd.DataFrame, value: str, fname: str, ylabel: str, ylim=None, nominal=None, logy=False):
    scen = list(SCEN_LABEL)
    metrics = list(METRIC_LABEL)
    fig, axes = plt.subplots(len(metrics), len(scen), figsize=(15, 11), sharex=True, sharey="row")
    xpos = {5: 0, 20: 1, 80: 2}
    for i, m in enumerate(metrics):
        for j, sc in enumerate(scen):
            ax = axes[i, j]
            sub = summ[(summ.metric == m) & (summ.scenario == sc)]
            for k, meth in enumerate(SHOW[m]):
                d = sub[sub.method == meth].sort_values("n_sessions")
                if d.empty:
                    continue
                x = d["n_sessions"].map(xpos)
                ax.plot(x, d[value], marker=MARKERS[k], ms=4, lw=1.2, color=PALETTE[k], label=METHOD_LABEL[meth])
                if value == "coverage":
                    ax.errorbar(x, d[value], yerr=1.96 * d["coverage_mcse"], fmt="none", ecolor=PALETTE[k], elinewidth=0.6, capsize=0)
            if nominal is not None:
                ax.axhline(nominal, color="0.35", lw=0.8, ls="--")
            ax.set_xticks([0, 1, 2]); ax.set_xticklabels(["5", "20", "80"]); ax.set_xlim(-0.3, 2.3)
            if logy:
                ax.set_yscale("log")
                ax.yaxis.set_minor_formatter(mpl.ticker.NullFormatter())
                ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
            if ylim:
                ax.set_ylim(*ylim)
            ax.margins(x=0.08)
            if i == 0:
                ax.set_title(SCEN_LABEL[sc], fontsize=8, loc="left")
            if j == 0:
                ax.set_ylabel(f"{METRIC_LABEL[m]}\n{ylabel}", fontsize=8)
            if i == len(metrics) - 1:
                ax.set_xlabel("sessions (n)", fontsize=8)
            if j == len(scen) - 1:
                ax.legend(frameon=False, fontsize=6.5, loc="center left", bbox_to_anchor=(1.02, 0.5))
            ax.tick_params(labelsize=7)
            for sp in ("top", "right"):
                ax.spines[sp].set_visible(False)
    fig.suptitle({"coverage": "Interval coverage of the 95 % intervals vs number of sessions (1000 replicates per cell; bars = 95 % Monte Carlo interval)",
                  "mean_width": "Mean interval width vs number of sessions (finite intervals only)"}[value], fontsize=9, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 0.84, 0.97))
    out = os.path.join(FIG, fname)
    fig.savefig(out, dpi=200)
    plt.close(fig)
    return out


def main():
    os.makedirs(FIG, exist_ok=True)
    summ = pd.read_csv(os.path.join(RES, "coverage_summary.csv"))
    p1 = plot(summ, "coverage", "coverage_vs_n.png", "coverage", ylim=(0.0, 1.02), nominal=0.95)
    p2 = plot(summ, "mean_width", "width_vs_n.png", "mean 95 % width", logy=True)
    print(p1, p2)


if __name__ == "__main__":
    main()
