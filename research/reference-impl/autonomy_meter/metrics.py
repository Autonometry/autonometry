"""Autonomy metrics: definition, estimator and interval for each.

All functions take the :class:`~autonomy_meter.events.Tables` produced by
``events.build_tables`` and return plain dicts (JSON-serialisable after
``_clean``) so that a report is a dict of dicts. Every metric dict carries
``estimate``, ``ci`` (the default interval) and ``interval_method``; secondary
intervals keep their own keys. Defaults were set from the simulation track (v0.1.1).

Metric summary
--------------
(a) time_autonomy_share      ai_s / active_s; bounds with unknown as human / AI;
                             DEFAULT ratio-linearised t over clusters (sessions, or robots
                             when >= 3), cluster bootstrap percentile secondary,
                             Wilson-effective-n fallback (single cluster)
(b) task_autonomy_rate       share of tasks with zero human events; DEFAULT cluster-robust
                             GLM (>= 20 sessions) else Wilson; Clopper-Pearson; bounds
(c) interventions_per_hour   count / active hours; DEFAULT NB2 Wald (Poisson-exact fallback
                             only at the zero-dispersion boundary), Garwood and cluster
                             bootstrap percentile secondary
(d) gaps_between_interventions  time (or distance) from one intervention to the next,
                             right-censored at session end; Kaplan-Meier median + CI,
                             exponential and Weibull fits (lifelines)
(e) human_minutes_per_task   mean human minutes per task; DEFAULT cluster bootstrap-t,
                             percentile secondary
(f) breakdowns               (c) by reason and kind, (b)/(e) by task_definition_id
(g) drift                    interventions per hour by calendar bin; Poisson/NB GLM trend test
                             (log-linear in time with log-exposure offset) and a CUSUM of
                             standardised bin counts
(h) unmeasured               unknown share, flags, conformance
"""
from __future__ import annotations

import math
import warnings
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from .events import Tables, HUMAN_STATES, ACTIVE_STATES
from . import intervals as iv

HOUR = 3600.0


def _clean(obj: Any) -> Any:
    """Make numpy/pandas scalars JSON-serialisable; NaN -> None."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        return None if (isinstance(obj, float) and math.isnan(obj)) or (isinstance(obj, np.floating) and np.isnan(obj)) else float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


# --------------------------------------------------------------------------- #
# (a) time autonomy share
# --------------------------------------------------------------------------- #
def _share_components(s: pd.DataFrame, params, variant: str = "default") -> pd.DataFrame:
    """Per-session numerator/denominator for the time share under a variant.

    variant: 'default' (unknown excluded, approvals at approval_min_dwell_s),
             'lower' (unknown -> human), 'upper' (unknown -> AI),
             'with_approvals' (approval dwell at the sensitivity value),
             'shared_as_ai' (shared counted as AI), 'scope_weighted'.
    """
    d = pd.DataFrame({"session_id": s["session_id"]})
    if variant == "scope_weighted":
        ai, act, unk = s["sw_ai_s"], s["sw_active_s"], s["sw_unknown_s"]
        if params.shared_as_ai:
            ai = ai + s["sw_shared_s"]
        d["num"], d["den"] = ai, act
        return d
    ai, act, unk = s["ai_s"].copy(), s["active_s"].copy(), s["unknown_s"]
    if params.shared_as_ai or variant == "shared_as_ai":
        ai = ai + s["shared_s"]
    appr = s["approval_human_s_sens"] if variant == "with_approvals" else s["approval_human_s"]
    # approvals happen during ai time: move dwell from ai to human (capped at ai time)
    appr = np.minimum(appr, ai)
    ai = ai - appr
    if variant == "lower":
        act = act + unk            # unknown counted as human
    elif variant == "upper":
        ai, act = ai + unk, act + unk
    d["num"], d["den"] = ai, act
    return d


def time_autonomy_share(t: Tables, alpha: float = 0.05, n_boot: int = 2000, seed: int = 0,
                        cluster_level: str = "auto") -> dict:
    """Time autonomy share = AI-controlled active seconds / active seconds.

    Estimator: ratio of sums over clusters (whole-robot any-human timeline).
    Intervals: (1) ratio-linearised t interval over clusters (default since v0.1.1;
    simulated coverage 0.91-0.96 vs 0.81 for the percentile bootstrap at 5 sessions);
    (2) cluster bootstrap percentile (secondary); (3) Wilson with effective n =
    number of whole-robot control segments (single-cluster fallback).
    cluster_level: 'session' (default unit), 'robot' (sessions nested in robots),
    or 'auto' = 'robot' when there are >= 3 robots, else 'session'.
    Bounds: 'lower' counts unknown as human, 'upper' counts unknown as AI; the
    outer interval is [lower.ci_lo, upper.ci_hi].
    Variants: with_approvals (approval dwell at sensitivity value), shared_as_ai,
    scope_weighted (Section 6.6).
    """
    s = t.sessions
    if s.empty or s["active_s"].sum() <= 0:
        return {"estimate": None, "note": "no active time"}
    n_robots = int(s["robot_id"].nunique())
    if cluster_level == "auto":
        cluster_level = "robot" if n_robots >= 3 else "session"
    if cluster_level not in ("session", "robot"):
        raise ValueError("cluster_level must be 'session', 'robot' or 'auto'")
    out: dict[str, Any] = {"definition": "ai_s / active_s (any-human whole-robot timeline)",
                           "active_hours": float(s["active_s"].sum() / HOUR),
                           "n_sessions": int(len(s)), "n_robots": n_robots, "cluster_level": cluster_level}
    n_segments = int((t.whole["state"].isin(ACTIVE_STATES)).sum()) if not t.whole.empty else 1
    for variant in ("default", "lower", "upper", "with_approvals", "shared_as_ai", "scope_weighted"):
        d = _share_components(s, t.params, variant)
        if cluster_level == "robot":
            d = d.assign(cluster=s["robot_id"].values).groupby("cluster", as_index=False)[["num", "den"]].sum()
        else:
            d = d.rename(columns={"session_id": "cluster"})
        if d["den"].sum() <= 0:
            out[variant] = {"estimate": None}
            continue
        lin = iv.ratio_linearised_ci(d["num"], d["den"], alpha)
        boot = iv.cluster_bootstrap(d, lambda x: x["num"].sum() / x["den"].sum(), "cluster",
                                    n_boot=n_boot, alpha=alpha, seed=seed)
        share = float(d["num"].sum() / d["den"].sum())
        w_lo, w_hi = iv.wilson_effective_ci(share, n_segments, alpha)
        if lin["n_clusters"] >= 2:
            ci, method = [max(0.0, lin["lo"]), min(1.0, lin["hi"])], f"ratio_linearised_t({cluster_level})"
        else:
            ci, method = [w_lo, w_hi], "wilson_effective_n(single_cluster)"
        out[variant] = {"estimate": share, "ci": ci, "interval_method": method,
                        "ci_linearised": [lin["lo"], lin["hi"]],
                        "ci_bootstrap": [boot["lo"], boot["hi"]],
                        "ci_wilson_effective_n": [w_lo, w_hi], "n_eff_segments": n_segments,
                        "n_clusters": lin["n_clusters"]}
    out["estimate"] = out["default"]["estimate"]
    out["ci"] = out["default"]["ci"]
    out["interval_method"] = out["default"]["interval_method"]
    out["outer_interval"] = [float(out["lower"]["ci"][0]), float(out["upper"]["ci"][1])]
    out["safety_stop_share"] = float(s["safety_stop_s"].sum() / s["active_s"].sum())
    out["human_share_by_state"] = {k: float(s[f"{k}_s"].sum() / s["active_s"].sum()) for k in ("human_local", "human_remote", "shared")}
    out["idle_share_of_observed"] = float(s["idle_s"].sum() / s["observed_s"].sum())
    out["reset_human_share_of_active"] = float(s["reset_human_s"].sum() / s["active_s"].sum())
    return _clean(out)


# --------------------------------------------------------------------------- #
# (b) task autonomy rate
# --------------------------------------------------------------------------- #
def task_autonomy_rate(t: Tables, alpha: float = 0.05, cluster_col: str = "session_id") -> dict:
    """Task autonomy rate = tasks with zero human events / tasks.

    Estimator: sample proportion. Intervals: cluster-robust GLM Wald (clusters =
    ``cluster_col``, default session) is the default when there are >= 20 clusters
    (v0.1.1; simulation showed Wilson under-covers with clustered tasks at that size),
    Wilson score below 20 clusters; Clopper-Pearson exact always reported. The
    chosen interval is in ``ci`` with ``interval_method``.
    Bounds: lower counts tasks with unknown gaps/outcome as not autonomous; upper
    excludes nothing (unknown gaps ignored). Also reports autonomous_success_rate
    (zero human events AND outcome == success) with the same intervals.
    """
    tk = t.tasks
    if tk.empty:
        return {"estimate": None, "n_tasks": 0, "note": "no tasks"}
    n = int(len(tk))

    def block(col: str) -> dict:
        k = int(tk[col].sum())
        wl, wh = iv.wilson_ci(k, n, alpha)
        cl, ch = iv.clopper_pearson_ci(k, n, alpha)
        cr = iv.proportion_cluster_robust_ci(tk[col].astype(int).values, tk[cluster_col].values, alpha)
        use_cr = cr["n_clusters"] >= 20 and cr["method"].startswith("cluster_glm")
        return {"k": k, "n": n, "estimate": k / n,
                "ci": [cr["lo"], cr["hi"]] if use_cr else [wl, wh],
                "interval_method": "cluster_robust_glm" if use_cr else "wilson",
                "ci_wilson": [wl, wh], "ci_clopper_pearson": [cl, ch],
                "ci_cluster_robust": [cr["lo"], cr["hi"]], "cluster_robust_method": cr["method"],
                "n_clusters": cr["n_clusters"]}

    out = {"definition": "tasks with zero human events (takeover/approval/hint) / tasks",
           "n_tasks": n, "n_tasks_with_unknown": int(tk["has_unknown"].sum()),
           "point": block("autonomous"), "lower": block("autonomous_lower"), "upper": block("autonomous_upper"),
           "autonomous_success": block("autonomous_success"),
           "success_rate_all_tasks": block("success")}
    out["estimate"] = out["point"]["estimate"]
    out["ci"] = out["point"]["ci"]
    out["interval_method"] = out["point"]["interval_method"]
    return _clean(out)


# --------------------------------------------------------------------------- #
# (c) interventions per active hour
# --------------------------------------------------------------------------- #
def interventions_per_hour(t: Tables, alpha: float = 0.05, n_boot: int = 2000, seed: int = 0,
                           kinds=("takeover", "approval", "hint"), count_col: str = "n_interventions") -> dict:
    """Interventions per active hour.

    Estimator: total interventions / total active hours (sessions pooled).
    Intervals: negative binomial NB2 Wald (dispersion across sessions) is the default
    since v0.1.1 (simulated coverage 0.95-0.96 under Poisson and 0.89/0.94/0.95 at
    5/20/80 sessions under NB dispersion ~3, where Garwood gives 0.72 and the
    percentile bootstrap 0.79 at 5 sessions). The NB fit falls back to Poisson exact
    (Garwood) only with < 2 sessions, zero events, or a converged dispersion MLE at the
    zero boundary; the label is in ``interval_method``. Garwood and the cluster
    bootstrap are reported as secondary rows; the Pearson dispersion index is reported.
    Per-scope count (``n_interventions``) is the default; ``n_interventions_whole``
    collapses simultaneous multi-scope takeovers to one.
    Also reports safety stops per active hour and approvals per active hour.
    """
    s = t.sessions
    if s.empty or s["active_s"].sum() <= 0:
        return {"estimate": None, "note": "no active time"}
    expo_h = s["active_s"].values / HOUR
    ints = t.interventions
    ints_sel = ints[ints["kind"].isin(kinds)] if not ints.empty else ints
    counts = s["session_id"].map(ints_sel.groupby("session_id").size()).fillna(0).values if not ints_sel.empty else np.zeros(len(s))
    if count_col == "n_interventions_whole":
        counts = s[count_col].values.astype(float)
    K, E = float(counts.sum()), float(expo_h.sum())
    g_lo, g_hi = iv.poisson_exact_ci(int(K), E, alpha)
    nb = iv.nb_rate_ci(counts, expo_h, alpha)
    d = pd.DataFrame({"session_id": s["session_id"], "k": counts, "e": expo_h})
    boot = iv.cluster_bootstrap(d, lambda x: x["k"].sum() / x["e"].sum(), "session_id", n_boot=n_boot, alpha=alpha, seed=seed)
    disp = iv.dispersion_index(counts, expo_h)
    rate = K / E

    def simple(kind_mask_count: float) -> dict:
        lo, hi = iv.poisson_exact_ci(int(kind_mask_count), E, alpha)
        return {"count": int(kind_mask_count), "rate_per_hour": kind_mask_count / E, "ci_poisson_exact": [lo, hi]}

    out = {"definition": "interventions (takeover + approval + hint) / active hours",
           "count": int(K), "active_hours": E, "estimate": rate,
           "ci_poisson_exact": [g_lo, g_hi],
           "ci_negative_binomial": [nb["lo"], nb["hi"]], "nb_alpha": nb["alpha_nb"], "nb_method": nb["method"],
           "ci_cluster_bootstrap": [boot["lo"], boot["hi"]], "bootstrap_method": boot["method"],
           "dispersion_index": disp,
           "ci": [nb["lo"], nb["hi"]],
           "interval_method": nb["method"],
           "n_sessions": int(len(s)),
           "safety_stops": simple(float(s["n_safety_stops"].sum())),
           "approvals": simple(float(s["n_approvals"].sum())),
           "hints": simple(float(s["n_hints"].sum())),
           "takeovers": simple(float(s["n_takeovers"].sum())),
           "whole_robot_count": int(s["n_interventions_whole"].sum())}
    return _clean(out)


# --------------------------------------------------------------------------- #
# (d) gaps between interventions (censored)
# --------------------------------------------------------------------------- #
def intervention_gaps(t: Tables, unit: str = "time", kinds=("takeover", "approval", "hint")) -> pd.DataFrame:
    """Build the gap table: one row per gap between consecutive interventions in a
    session plus the final censored gap from the last intervention (or session start)
    to session end. unit='time' -> active hours between events (idle/unknown excluded);
    unit='distance' -> metres (cumulative distance_m).

    Columns: session_id, duration, observed (1 = ended in an intervention, 0 = censored).
    """
    rows = []
    for _, s in t.sessions.iterrows():
        sid = s["session_id"]
        ints = t.interventions[(t.interventions["session_id"] == sid) & t.interventions["kind"].isin(kinds)].sort_values("ts")
        if unit == "time":
            w = t.whole[(t.whole["session_id"] == sid) & t.whole["state"].isin(ACTIVE_STATES)].sort_values("start")
            # active-time clock: cumulative active seconds at any timestamp
            starts, ends = w["start"].values, w["end"].values
            cum = np.concatenate([[0.0], np.cumsum((ends - starts) / np.timedelta64(1, "s"))])

            def clock(ts):
                ts64 = pd.Timestamp(ts).to_datetime64()
                i = np.searchsorted(starts, ts64, side="right") - 1
                if i < 0:
                    return 0.0
                return float(cum[i] + min(max((ts64 - starts[i]) / np.timedelta64(1, "s"), 0.0), (ends[i] - starts[i]) / np.timedelta64(1, "s")))
            marks = [clock(ts) / HOUR for ts in ints["ts"]]
            total = float(cum[-1]) / HOUR
        else:
            marks = list(ints["distance_m"].astype(float).values)
            total = float(s["distance_m"])
        prev = 0.0
        for m in marks:
            rows.append({"session_id": sid, "duration": max(m - prev, 0.0), "observed": 1})
            prev = m
        rows.append({"session_id": sid, "duration": max(total - prev, 0.0), "observed": 0})
    return pd.DataFrame(rows, columns=["session_id", "duration", "observed"])


def gaps_between_interventions(t: Tables, unit: str = "time", alpha: float = 0.05,
                               horizons=(0.5, 1.0, 2.0, 4.0)) -> dict:
    """Time (active hours) or distance (metres) between interventions with right censoring.

    Each session contributes its inter-intervention gaps as observed events and
    its final gap (from the last intervention, or session start if none, to the
    end) as a right-censored observation; a session with no intervention is one
    censored observation of its full active duration.
    Estimators (lifelines): KaplanMeierFitter median and S(h) at horizons with
    exponential-Greenwood CI; ExponentialFitter (lifelines ``lambda_`` is the scale,
    rate = 1/lambda_) with a log-scale Wald CI built from ``summary['se(coef)']``
    (``confidence_interval_`` is the survival band, not the parameter CI);
    WeibullFitter (lambda_, rho_; rho_ < 1 means a decreasing hazard = bursty).
    Note: gaps within a session are treated as independent, which understates
    uncertainty if interventions cluster; the session-level rate interval in (c)
    is the primary inferential quantity.
    """
    from lifelines import KaplanMeierFitter, ExponentialFitter, WeibullFitter
    from lifelines.utils import median_survival_times

    g = intervention_gaps(t, unit)
    g = g[g["duration"] > 0] if (g["observed"] == 1).any() else g
    if g.empty:
        return {"estimate": None, "note": "no gaps"}
    n_ev = int(g["observed"].sum())
    out: dict[str, Any] = {"definition": f"gap between consecutive interventions ({'active hours' if unit == 'time' else 'metres'}), right-censored at session end",
                           "unit": "active_hours" if unit == "time" else "metres",
                           "n_gaps": int(len(g)), "n_events": n_ev, "n_censored": int(len(g) - n_ev),
                           "naive_mean_gap_including_censored": float(g["duration"].sum() / max(n_ev, 1))}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        km = KaplanMeierFitter(alpha=alpha).fit(g["duration"], g["observed"])
        med = float(km.median_survival_time_)
        med_ci = median_survival_times(km.confidence_interval_)
        out["km"] = {"median": med if np.isfinite(med) else None,
                     "median_ci": [float(med_ci.iloc[0, 0]) if np.isfinite(med_ci.iloc[0, 0]) else None,
                                   float(med_ci.iloc[0, 1]) if np.isfinite(med_ci.iloc[0, 1]) else None],
                     "survival_at": {str(h): float(km.predict(h)) for h in horizons},
                     "survival_ci_at": {str(h): [float(v) for v in km.confidence_interval_survival_function_.apply(lambda c: np.interp(h, c.index, c.values)).values] for h in horizons},
                     "interval_method": "exponential_greenwood (lifelines)"}
        if n_ev > 0:
            ef = ExponentialFitter(alpha=alpha).fit(g["duration"], g["observed"])
            # v0.1.1: confidence_interval_ is the survival-function band, not the CI of lambda_.
            # Use the parameter table (summary: coef, se(coef)) and a log-scale Wald interval
            # (for the exponential MLE se(coef)/lambda_ = 1/sqrt(n_events)).
            lam = float(ef.lambda_); se_lam = float(ef.summary.loc["lambda_", "se(coef)"])
            zq = stats.norm.ppf(1 - alpha / 2)
            lo_l, hi_l = lam * math.exp(-zq * se_lam / lam), lam * math.exp(zq * se_lam / lam)
            out["exponential"] = {"mean_gap": lam, "rate_per_unit": 1.0 / lam,
                                  "mean_gap_ci": [lo_l, hi_l], "rate_ci": [1.0 / hi_l, 1.0 / lo_l],
                                  "interval_method": "wald_log_scale_from_summary_se"}
            if n_ev >= 3:
                try:
                    wf = WeibullFitter(alpha=alpha).fit(g["duration"], g["observed"])
                    sm_ = wf.summary  # parameter table: coef, se, coef lower/upper 95%
                    lo_col = [c for c in sm_.columns if "lower" in c][0]; hi_col = [c for c in sm_.columns if "upper" in c][0]
                    out["weibull"] = {"lambda_scale": float(wf.lambda_), "rho_shape": float(wf.rho_),
                                      "rho_ci": [float(sm_.loc["rho_", lo_col]), float(sm_.loc["rho_", hi_col])],
                                      "median": float(wf.median_survival_time_),
                                      "note": "rho < 1: decreasing hazard (bursty); rho = 1: exponential"}
                except Exception as exc:  # pragma: no cover
                    out["weibull"] = {"error": type(exc).__name__}
        else:
            out["exponential"] = {"note": "no events: rate upper bound from (c) Poisson exact applies"}
    out["estimate"] = out["km"]["median"]
    return _clean(out)


# --------------------------------------------------------------------------- #
# (e) human minutes per task
# --------------------------------------------------------------------------- #
def human_minutes_per_task(t: Tables, alpha: float = 0.05, n_boot: int = 2000, seed: int = 0,
                           include_unplanned_reset: bool = True, bootstrap_t: bool = True) -> dict:
    """Mean human minutes per task (task-level human control time + approval/hint dwell,
    plus unplanned resets preceding the task when include_unplanned_reset).

    Estimator: sample mean over tasks. Intervals: cluster (session) bootstrap-t with the
    cluster-robust SE of the mean as the studentiser (closed form, no inner bootstrap) is
    the default since v0.1.1 (simulated coverage 0.94-0.96 after the resample-labelling
    fix, vs 0.83-0.85 before); the percentile bootstrap is secondary. With a single
    session the percentile/t intervals are undefined and a t interval on tasks is used.
    Also reports median and the share of tasks with zero human minutes.
    """
    tk = t.tasks
    if tk.empty:
        return {"estimate": None, "n_tasks": 0}
    col = "human_s_incl_reset" if include_unplanned_reset else "human_s"
    d = pd.DataFrame({"session_id": tk["session_id"], "m": tk[col] / 60.0})
    mean_fn = lambda x: x["m"].mean()

    def se_cluster(x: pd.DataFrame) -> float:
        g = x.groupby("session_id")["m"].agg(["sum", "count"])
        G = len(g)
        if G < 2:
            return float(x["m"].std(ddof=1) / np.sqrt(len(x))) if len(x) > 1 else 0.0
        mbar = x["m"].mean()
        resid = g["sum"] - mbar * g["count"]
        return float(np.sqrt((resid ** 2).sum() * G / (G - 1)) / len(x))

    pct = iv.cluster_bootstrap(d, mean_fn, "session_id", n_boot=n_boot, alpha=alpha, seed=seed)
    out = {"definition": f"mean over tasks of human minutes ({col})", "n_tasks": int(len(tk)),
           "estimate": float(d["m"].mean()), "median": float(d["m"].median()),
           "share_tasks_zero_human": float((d["m"] == 0).mean()),
           "ci_cluster_bootstrap_percentile": [pct["lo"], pct["hi"]]}
    if pct["n_clusters"] < 2:
        n = len(d); se = float(d["m"].std(ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
        tq = stats.t.ppf(1 - alpha / 2, max(n - 1, 1))
        out["ci"] = [out["estimate"] - tq * se, out["estimate"] + tq * se]
        out["interval_method"] = "t_on_tasks(single_session)"
    elif bootstrap_t:
        bt = iv.cluster_bootstrap(d, mean_fn, "session_id", n_boot=n_boot, alpha=alpha, seed=seed,
                                  method="bootstrap_t", se_fn=se_cluster)
        out["ci_cluster_bootstrap_t"] = [bt["lo"], bt["hi"]]
        out["ci"], out["interval_method"] = [bt["lo"], bt["hi"]], bt["method"]
    else:
        out["ci"], out["interval_method"] = [pct["lo"], pct["hi"]], pct["method"]
    return _clean(out)


# --------------------------------------------------------------------------- #
# (f) breakdowns
# --------------------------------------------------------------------------- #
def breakdowns(t: Tables, alpha: float = 0.05) -> dict:
    """Interventions per active hour by reason and by kind (Poisson exact intervals,
    common active-hour denominator), and per task_definition_id: task autonomy rate
    (Wilson), success rate, mean human minutes, interventions per task."""
    s = t.sessions
    E = float(s["active_s"].sum() / HOUR) if not s.empty else 0.0
    ints = t.interventions[t.interventions["kind"] != "safety_stop"] if not t.interventions.empty else t.interventions
    out: dict[str, Any] = {"active_hours": E, "by_reason": {}, "by_kind": {}, "by_task_type": {}}
    if E > 0 and not ints.empty:
        for key, col in (("by_reason", "reason"), ("by_kind", "kind")):
            for val, g in ints.groupby(ints[col].fillna("null")):
                lo, hi = iv.poisson_exact_ci(len(g), E, alpha)
                out[key][str(val)] = {"count": int(len(g)), "rate_per_hour": len(g) / E, "ci_poisson_exact": [lo, hi]}
        # narrow (DMV-like) count: human_initiated + safety takeovers only
        narrow = ints[(ints["kind"] == "takeover") & ints["reason"].isin(["human_initiated", "safety"])]
        lo, hi = iv.poisson_exact_ci(len(narrow), E, alpha)
        out["narrow_takeovers_human_initiated_or_safety"] = {"count": int(len(narrow)), "rate_per_hour": len(narrow) / E, "ci_poisson_exact": [lo, hi]}
    tk = t.tasks
    if not tk.empty:
        for td, g in tk.groupby("task_definition_id"):
            n, k = len(g), int(g["autonomous"].sum())
            wl, wh = iv.wilson_ci(k, n, alpha)
            out["by_task_type"][str(td)] = {
                "n_tasks": int(n), "task_autonomy_rate": k / n, "ci_wilson": [wl, wh],
                "success_rate": float(g["success"].mean()),
                "autonomous_success_rate": float(g["autonomous_success"].mean()),
                "mean_human_minutes": float(g["human_s"].mean() / 60.0),
                "interventions_per_task": float(g["n_interventions"].mean()),
                "mean_task_minutes": float(g["dur_s"].mean() / 60.0)}
    return _clean(out)


# --------------------------------------------------------------------------- #
# (g) drift
# --------------------------------------------------------------------------- #
def drift(t: Tables, freq: str = "D", alpha: float = 0.05, cusum_k: float = 0.5, cusum_h: float = 4.0,
          kinds=("takeover", "approval", "hint")) -> dict:
    """Drift of the intervention rate over calendar time.

    Bins active time and intervention counts by calendar period ``freq`` (pandas
    offset alias; 'D' = day). Trend test: GLM with log link, count ~ time_index,
    offset log(active hours); Poisson family, then NB (statsmodels NegativeBinomial
    GLM family with alpha from the NB2 fit in intervals.nb_rate_ci when dispersion > 1.5).
    Reports the slope (log rate ratio per bin), its p-value and the implied rate ratio
    per bin. CUSUM: one-sided upper/lower tabular CUSUM of standardised bin counts
    z_i = (k_i - r*e_i)/sqrt(r*e_i * phi), reference k, decision h (in sigma units);
    reports the first bin at which either CUSUM exceeds h (None if never).
    """
    import statsmodels.api as sm

    s = t.sessions
    if s.empty:
        return {"estimate": None}
    ints = t.interventions[t.interventions["kind"].isin(kinds)] if not t.interventions.empty else t.interventions
    # exposure by bin: split each session's active time by the session start bin (approximation documented)
    bins = s["start"].dt.floor(freq)
    expo = s.groupby(bins)["active_s"].sum() / HOUR
    cnt = ints.groupby(ints["ts"].dt.floor(freq)).size() if not ints.empty else pd.Series(dtype=float)
    idx = expo.index.union(cnt.index).sort_values()
    e = expo.reindex(idx).fillna(0.0)
    k = cnt.reindex(idx).fillna(0.0)
    mask = e > 0
    e, k = e[mask], k[mask]
    out: dict[str, Any] = {"freq": freq, "n_bins": int(len(e)),
                           "bins": [{"bin": str(b), "active_hours": float(e[b]), "count": int(k[b]),
                                     "rate": float(k[b] / e[b])} for b in e.index]}
    if len(e) < 3:
        out["trend"] = {"note": "fewer than 3 bins; no trend test"}
        return _clean(out)
    x = np.arange(len(e), dtype=float)
    X = sm.add_constant(x)
    disp = iv.dispersion_index(k.values, e.values)
    fam = sm.families.Poisson()
    family_used = "poisson"
    if not np.isnan(disp) and disp > 1.5:
        nb = iv.nb_rate_ci(k.values, e.values)
        if nb["alpha_nb"] is not None and not np.isnan(nb["alpha_nb"]) and nb["alpha_nb"] > 0:
            fam = sm.families.NegativeBinomial(alpha=float(nb["alpha_nb"]))
            family_used = f"negative_binomial(alpha={nb['alpha_nb']:.3g})"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = sm.GLM(k.values, X, family=fam, offset=np.log(e.values)).fit()
    slope, se = float(res.params[1]), float(res.bse[1])
    out["trend"] = {"family": family_used, "slope_log_rate_per_bin": slope, "se": se,
                    "rate_ratio_per_bin": float(np.exp(slope)),
                    "rate_ratio_ci": [float(np.exp(slope - stats.norm.ppf(1 - alpha / 2) * se)),
                                      float(np.exp(slope + stats.norm.ppf(1 - alpha / 2) * se))],
                    "p_value": float(res.pvalues[1]), "dispersion_index": disp}
    # CUSUM
    r = k.sum() / e.sum()
    phi = max(disp, 1.0) if not np.isnan(disp) else 1.0
    z = (k.values - r * e.values) / np.sqrt(r * e.values * phi)
    up, lo_ = np.zeros(len(z)), np.zeros(len(z))
    first = None
    for i, zi in enumerate(z):
        up[i] = max(0.0, (up[i - 1] if i else 0.0) + zi - cusum_k)
        lo_[i] = max(0.0, (lo_[i - 1] if i else 0.0) - zi - cusum_k)
        if first is None and (up[i] > cusum_h or lo_[i] > cusum_h):
            first = {"bin": str(e.index[i]), "direction": "up" if up[i] > cusum_h else "down"}
    out["cusum"] = {"k": cusum_k, "h": cusum_h, "overdispersion_phi": float(phi),
                    "upper": [float(v) for v in up], "lower": [float(v) for v in lo_], "first_signal": first}
    return _clean(out)


# --------------------------------------------------------------------------- #
# (h) unmeasured time
# --------------------------------------------------------------------------- #
def unmeasured(t: Tables) -> dict:
    """Unknown-time reporting and conformance flag (Section 7)."""
    s = t.sessions
    if s.empty:
        return {"unknown_share": None}
    obs = float(s["observed_s"].sum())
    unk = float(s["unknown_s"].sum())
    share = unk / obs if obs > 0 else float("nan")
    codes = t.flags["code"].value_counts().to_dict() if not t.flags.empty else {}
    return _clean({"observed_hours": obs / HOUR, "unknown_hours": unk / HOUR, "unknown_share": share,
                   "unknown_share_max": t.params.unknown_share_max,
                   "conformance": "incomplete" if share > t.params.unknown_share_max else "complete",
                   "idle_hours": float(s["idle_s"].sum() / HOUR), "active_hours": float(s["active_s"].sum() / HOUR),
                   "flag_counts": codes, "n_flags": int(len(t.flags)),
                   "supervision_ratio": float((s["supervision_ratio"] * s["active_s"]).sum() / max(s["active_s"].sum(), 1e-9)),
                   "supervisor_hours_per_robot_hour": float((s["supervisor_hours_per_robot_hour"] * s["active_s"]).sum() / max(s["active_s"].sum(), 1e-9)),
                   "n_human_actors_total": int(s["n_human_actors"].sum()),
                   "human_actor_hours": float(s["human_actor_s"].sum() / HOUR)})


# --------------------------------------------------------------------------- #
# full report
# --------------------------------------------------------------------------- #
def full_report(t: Tables, alpha: float = 0.05, n_boot: int = 1000, seed: int = 0, drift_freq: str = "D") -> dict:
    """Compute every metric and return one JSON-serialisable dict."""
    rep = {"alpha": alpha, "n_boot": n_boot, "seed": seed, "params": t.params.as_dict(),
           "n_sessions": int(len(t.sessions)), "n_robots": int(t.sessions["robot_id"].nunique()) if not t.sessions.empty else 0,
           "time_autonomy_share": time_autonomy_share(t, alpha, n_boot, seed),
           "task_autonomy_rate": task_autonomy_rate(t, alpha),
           "interventions_per_active_hour": interventions_per_hour(t, alpha, n_boot, seed),
           "gaps_between_interventions_time": gaps_between_interventions(t, "time", alpha),
           "human_minutes_per_task": human_minutes_per_task(t, alpha, n_boot, seed),
           "breakdowns": breakdowns(t, alpha),
           "drift": drift(t, drift_freq, alpha),
           "unmeasured": unmeasured(t)}
    if not t.sessions.empty and t.sessions["distance_m"].sum() > 0:
        rep["gaps_between_interventions_distance"] = gaps_between_interventions(t, "distance", alpha)
    return _clean(rep)
