"""simulate.py -- session-level generator with KNOWN population truth for the
Autonomy Meter coverage study (Part 6a).

Model (one session i on robot r(i), day d(i), calendar position c_i in [0,1]):

  active duration      T_i ~ LogNormal(log(session_median_s), session_sigma)
  rate multiplier      m_i = exp(b_r + b_d + e_i) * G_i * 2^(-drift_halvings * c_i)
                         b_r ~ N(0, sigma_robot^2)   (robot effect, redrawn per replicate)
                         b_d ~ N(0, sigma_day^2)     (day effect)
                         e_i ~ N(0, sigma_session^2) (iid session effect)
                         G_i ~ Gamma(1/a, a)         (NB2 frailty, mean 1, var a) if frailty_a > 0
  candidate events     Poisson process on [0, T_i] at rate lambda0 * m_i * max_type_mult / 3600,
                       OR a Hawkes cluster process with the same mean rate
                       (branching ratio hawkes_n, exponential offspring kernel, decay hawkes_beta_per_h)
  tasks                sequential partition of [0, T_i] into tasks, duration LogNormal, type drawn
                       from task_type_probs; a candidate event inside a task of type k is kept with
                       probability type_mult[k] / max_type_mult (type heterogeneity in the
                       intervention rate); weighted mean of type_mult is 1 so lambda0 is the
                       population-average candidate rate before within-human thinning
  kinds                takeover / approval / hint with kind_probs
  takeover dwell       LogNormal(log(takeover_dwell_median_s), takeover_dwell_sigma) * exp(dwell_robot_coef * b_r),
                       clipped at the end of the task (control returns at task end);
                       candidate events during an ongoing human interval are dropped (the human
                       already has control, so the loader would not count a takeover either)
  approval/hint dwell  LogNormal (seconds); counted in human minutes per task, not in time share
  outcome              success with probability 1 - p_fail[type, intervened]
  unknown windows      random: Poisson number of windows, Exponential(unknown_mean_len_s) length,
                       uniform start (MCAR);  informative: with probability unknown_informative_p
                       a dropout window starts 0.5 s before a takeover (hides the event and part
                       of the human time: missing-not-at-random)

Truth = population values from a large draw (population_truth):
  share  = 1 - sum(human_s) / sum(T)                    (time autonomy share, whole-robot)
  rate   = sum(n_events) / sum(T) * 3600                 (interventions per active hour)
  task   = mean(task has zero human events)              (task autonomy rate)
  hmpt   = mean(task human seconds incl. approval dwell) / 60
  gapmed = KM median of the population inter-intervention gaps (active hours, last gap censored)
All truths are computed from the hidden (complete) process, before any unknown masking.

Observed tables per replicate are in the shapes autonomy_meter.metrics works with
(sessions: ai_s, active_s, unknown_s, n_interventions, ...; tasks: autonomous, has_unknown,
human_s; gaps: duration, observed).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict, replace

import numpy as np
import pandas as pd

HOUR = 3600.0


# --------------------------------------------------------------------------------------
# configuration and scenario presets
# --------------------------------------------------------------------------------------
@dataclass
class ScenarioConfig:
    name: str = "baseline_poisson"
    lambda0: float = 8.0                 # candidate interventions per active hour (population average)
    session_median_s: float = 1200.0
    session_sigma: float = 0.4
    n_robots: int = 4
    sessions_per_day: int = 4
    sigma_robot: float = 0.0
    sigma_day: float = 0.0
    sigma_session: float = 0.0
    dwell_robot_coef: float = 0.0
    frailty_a: float = 0.0               # NB2 dispersion of the per-session gamma frailty
    hawkes_n: float = 0.0                # branching ratio (0 = Poisson)
    hawkes_beta_per_h: float = 60.0      # offspring decay rate per hour (mean delay 1 min)
    drift_halvings: float = 0.0          # rate multiplier 2^(-drift_halvings * calendar_position)
    unknown_random: float = 0.0          # target share of time masked at random
    unknown_informative_p: float = 0.0   # probability that a takeover triggers a logging dropout
    unknown_mean_len_s: float = 60.0
    kind_probs: tuple = (0.7, 0.2, 0.1)  # takeover, approval, hint
    takeover_dwell_median_s: float = 40.0
    takeover_dwell_sigma: float = 0.7
    approval_dwell_median_s: float = 3.0
    approval_dwell_sigma: float = 0.5
    hint_dwell_median_s: float = 2.0
    task_median_s: float = 300.0
    task_sigma: float = 0.5
    task_type_ids: tuple = ("pick_place_v1", "drawer_open_v1", "cable_route_v1")
    task_type_probs: tuple = (0.5, 0.35, 0.15)
    task_type_mult: tuple = (0.7, 1.0, 2.0)          # weighted mean = 0.35+0.35+0.30 = 1.0
    p_fail_autonomous: tuple = (0.05, 0.15, 0.30)
    p_fail_intervened: tuple = (0.30, 0.40, 0.50)

    def as_dict(self) -> dict:
        return asdict(self)


def scenarios() -> dict[str, ScenarioConfig]:
    base = ScenarioConfig()
    return {
        "i_baseline_poisson": replace(base, name="i_baseline_poisson"),
        "ii_nb_overdispersion": replace(base, name="ii_nb_overdispersion", frailty_a=1.0),
        "iii_hawkes_bursty": replace(base, name="iii_hawkes_bursty", hawkes_n=0.6),
        "iv_robot_day_clustering": replace(base, name="iv_robot_day_clustering",
                                           sigma_robot=0.30, sigma_day=0.30, sigma_session=0.35,
                                           dwell_robot_coef=0.5),
        "v_drift_halving": replace(base, name="v_drift_halving", drift_halvings=1.0),
        "vi_unknown_10pct": replace(base, name="vi_unknown_10pct", unknown_random=0.06,
                                    unknown_informative_p=0.55),
    }


# --------------------------------------------------------------------------------------
# interval helpers (small lists of [a, b) pairs)
# --------------------------------------------------------------------------------------
def _merge(iv):
    iv = sorted(iv)
    out = []
    for a, b in iv:
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _total(iv):
    return float(sum(b - a for a, b in iv))


def _intersect_len(A, B):
    i = j = 0
    tot = 0.0
    while i < len(A) and j < len(B):
        a = max(A[i][0], B[j][0]); b = min(A[i][1], B[j][1])
        if b > a:
            tot += b - a
        if A[i][1] < B[j][1]:
            i += 1
        else:
            j += 1
    return tot


def _clip(iv, lo, hi):
    return [[max(a, lo), min(b, hi)] for a, b in iv if min(b, hi) > max(a, lo)]


def _inside(t, iv):
    for a, b in iv:
        if a <= t < b:
            return True
    return False


def _clock(t, U):
    """Observed-active-time clock at hidden time t (unknown windows U removed)."""
    return t - sum(min(b, t) - a for a, b in U if a < t)


# --------------------------------------------------------------------------------------
# Hawkes cluster process (immigrant / offspring representation, exponential kernel)
# --------------------------------------------------------------------------------------
def hawkes_times(mu, n_branch, beta, T, rng, max_events=5000):
    """Event times on [0, T) of a Hawkes process with background rate mu, branching
    ratio n_branch and exponential offspring kernel with decay beta (all per second)."""
    imm = rng.poisson(mu * T)
    times = list(np.sort(rng.uniform(0.0, T, imm)))
    queue = list(times)
    while queue and len(times) < max_events:
        parent = queue.pop()
        k = rng.poisson(n_branch)
        if k:
            kids = parent + rng.exponential(1.0 / beta, k)
            kids = kids[kids < T]
            times.extend(kids.tolist())
            queue.extend(kids.tolist())
    return np.sort(np.asarray(times, dtype=float))


# --------------------------------------------------------------------------------------
# one session
# --------------------------------------------------------------------------------------
def simulate_session(cfg: ScenarioConfig, rng: np.random.Generator, sid: str, rid: str, day: int,
                     b_r: float, b_d: float, cal_pos: float):
    T = float(rng.lognormal(math.log(cfg.session_median_s), cfg.session_sigma))
    m = math.exp(b_r + b_d + (rng.normal(0.0, cfg.sigma_session) if cfg.sigma_session > 0 else 0.0))
    if cfg.frailty_a > 0:
        m *= rng.gamma(1.0 / cfg.frailty_a, cfg.frailty_a)
    if cfg.drift_halvings > 0:
        m *= 2.0 ** (-cfg.drift_halvings * cal_pos)
    max_mult = max(cfg.task_type_mult)
    rate_s = cfg.lambda0 * m * max_mult / HOUR
    if cfg.hawkes_n > 0:
        times = hawkes_times(rate_s * (1.0 - cfg.hawkes_n), cfg.hawkes_n, cfg.hawkes_beta_per_h / HOUR, T, rng)
    else:
        times = np.sort(rng.uniform(0.0, T, rng.poisson(rate_s * T)))

    # tasks: sequential partition of [0, T)
    starts, ends = [], []
    t = 0.0
    while t < T:
        d = float(rng.lognormal(math.log(cfg.task_median_s), cfg.task_sigma))
        starts.append(t); ends.append(min(t + d, T)); t += d
    n_tasks = len(starts)
    ttype = rng.choice(len(cfg.task_type_probs), size=n_tasks, p=cfg.task_type_probs)
    starts_a = np.asarray(starts)
    dwell_eff = math.exp(cfg.dwell_robot_coef * b_r) if cfg.dwell_robot_coef else 1.0

    human_iv = []           # takeover control intervals (hidden truth)
    events = []             # (t, kind, dwell_s, task_index)
    cur_h_end = -1.0
    for te in times:
        k = int(np.searchsorted(starts_a, te, side="right") - 1)
        if rng.random() > cfg.task_type_mult[ttype[k]] / max_mult:
            continue
        if te < cur_h_end:
            continue
        u = rng.random()
        if u < cfg.kind_probs[0]:
            dwell = rng.lognormal(math.log(cfg.takeover_dwell_median_s), cfg.takeover_dwell_sigma) * dwell_eff
            end = min(te + dwell, ends[k])
            if end <= te:
                continue
            human_iv.append([te, end]); cur_h_end = end
            events.append((te, "takeover", end - te, k))
        elif u < cfg.kind_probs[0] + cfg.kind_probs[1]:
            events.append((te, "approval", float(rng.lognormal(math.log(cfg.approval_dwell_median_s), cfg.approval_dwell_sigma)), k))
        else:
            events.append((te, "hint", float(rng.lognormal(math.log(cfg.hint_dwell_median_s), cfg.approval_dwell_sigma)), k))
    human_iv = _merge(human_iv)

    # unknown windows
    U = []
    if cfg.unknown_random > 0:
        nwin = rng.poisson(cfg.unknown_random * T / cfg.unknown_mean_len_s)
        for s0 in rng.uniform(0.0, T, nwin):
            U.append([s0, s0 + rng.exponential(cfg.unknown_mean_len_s)])
    if cfg.unknown_informative_p > 0:
        for (te, kind, dw, k) in events:
            if kind == "takeover" and rng.random() < cfg.unknown_informative_p:
                U.append([te - 0.5, te + rng.exponential(cfg.unknown_mean_len_s)])
    U = _clip(_merge(U), 0.0, T)
    unknown_s = _total(U)

    human_true = _total(human_iv)
    # observed human intervals: a takeover whose start transition fell inside an unknown window is
    # invisible to the loader, which resumes the last known state (ai) after the gap -> the rest
    # of that human interval is misattributed to AI. Visible intervals lose their unknown overlap.
    human_vis = [[a, b] for a, b in human_iv if not _inside(a, U)]
    human_obs = _total(human_vis) - _intersect_len(human_vis, U)
    active_obs = T - unknown_s
    obs_events = [e for e in events if not _inside(e[0], U)]
    n_seg_obs = 2 * sum(1 for a, b in human_vis if (b - a) - _intersect_len([[a, b]], U) > 0) + 1

    # tasks
    task_rows = []
    n_true_by_task = np.zeros(n_tasks, int); n_obs_by_task = np.zeros(n_tasks, int)
    appr_true = np.zeros(n_tasks); appr_obs = np.zeros(n_tasks)
    for (te, kind, dw, k) in events:
        n_true_by_task[k] += 1
        if kind != "takeover":
            appr_true[k] += dw
    for (te, kind, dw, k) in obs_events:
        n_obs_by_task[k] += 1
        if kind != "takeover":
            appr_obs[k] += dw
    for k in range(n_tasks):
        win = [[starts[k], ends[k]]]
        h_true = _intersect_len(human_iv, win)
        hv = _clip(human_vis, starts[k], ends[k])
        h_obs = _total(hv) - _intersect_len(hv, U)
        has_unknown = _intersect_len(win, U) > 0
        intervened = n_true_by_task[k] > 0
        pf = (cfg.p_fail_intervened if intervened else cfg.p_fail_autonomous)[ttype[k]]
        success = rng.random() >= pf
        task_rows.append({"session_id": sid, "robot_id": rid, "task_index": k,
                          "task_definition_id": cfg.task_type_ids[ttype[k]],
                          "start_s": starts[k], "end_s": ends[k],
                          "autonomous": bool(n_obs_by_task[k] == 0), "has_unknown": bool(has_unknown),
                          "success": bool(success),
                          "human_s": h_obs + appr_obs[k],
                          "autonomous_true": bool(n_true_by_task[k] == 0),
                          "human_s_true": h_true + appr_true[k]})

    # gaps (observed clock) and event rows
    ev_rows = []
    gap_rows = []
    prev = 0.0
    total_clock_h = active_obs / HOUR
    for (te, kind, dw, k) in obs_events:
        c = _clock(te, U) / HOUR
        gap_rows.append({"session_id": sid, "duration": max(c - prev, 0.0), "observed": 1})
        prev = c
        ev_rows.append({"session_id": sid, "robot_id": rid, "kind": kind, "t_s": te, "clock_h": c, "dwell_s": dw, "task_index": k})
    gap_rows.append({"session_id": sid, "duration": max(total_clock_h - prev, 0.0), "observed": 0})
    true_gap_rows = []
    prev = 0.0
    for (te, kind, dw, k) in events:
        true_gap_rows.append({"session_id": sid, "duration": max(te / HOUR - prev, 0.0), "observed": 1}); prev = te / HOUR
    true_gap_rows.append({"session_id": sid, "duration": max(T / HOUR - prev, 0.0), "observed": 0})

    srow = {"session_id": sid, "robot_id": rid, "day": day, "cal_pos": cal_pos,
            "observed_s": T, "active_s": active_obs, "ai_s": active_obs - human_obs, "human_s": human_obs,
            "unknown_s": unknown_s, "n_interventions": len(obs_events),
            "n_takeovers": sum(1 for e in obs_events if e[1] == "takeover"),
            "n_approvals": sum(1 for e in obs_events if e[1] == "approval"),
            "n_hints": sum(1 for e in obs_events if e[1] == "hint"),
            "approval_human_s": float(appr_obs.sum()), "n_segments": n_seg_obs, "n_tasks": n_tasks,
            "active_s_true": T, "human_s_true": human_true, "n_interventions_true": len(events),
            "rate_multiplier": m, "events_hidden": events, "human_iv_hidden": human_iv, "unknown_iv": U,
            "task_bounds": list(zip(starts, ends)), "task_types": ttype.tolist()}
    return srow, task_rows, ev_rows, gap_rows, true_gap_rows


# --------------------------------------------------------------------------------------
# a replicate: n sessions with robot/day structure
# --------------------------------------------------------------------------------------
def simulate_sessions(cfg: ScenarioConfig, n_sessions: int, rng: np.random.Generator, keep_hidden: bool = False):
    """Return dict(sessions, tasks, interventions, gaps, gaps_true) DataFrames for one replicate.
    Sessions are assigned round-robin to cfg.n_robots robots, cfg.sessions_per_day per day;
    robot and day effects are redrawn (superpopulation truth)."""
    n_days = int(math.ceil(n_sessions / cfg.sessions_per_day))
    b_r = rng.normal(0.0, cfg.sigma_robot, cfg.n_robots) if cfg.sigma_robot > 0 else np.zeros(cfg.n_robots)
    b_d = rng.normal(0.0, cfg.sigma_day, n_days) if cfg.sigma_day > 0 else np.zeros(n_days)
    S, TK, EV, GP, GT = [], [], [], [], []
    for i in range(n_sessions):
        r = i % cfg.n_robots
        d = i // cfg.sessions_per_day
        cal = rng.uniform(0.0, 1.0)
        srow, trows, erows, grows, gtrows = simulate_session(cfg, rng, f"s{i:05d}", f"r{r:02d}", d, b_r[r], b_d[d], cal)
        if not keep_hidden:
            for key in ("events_hidden", "human_iv_hidden", "unknown_iv", "task_bounds", "task_types"):
                srow.pop(key)
        S.append(srow); TK.extend(trows); EV.extend(erows); GP.extend(grows); GT.extend(gtrows)
    ev_cols = ["session_id", "robot_id", "kind", "t_s", "clock_h", "dwell_s", "task_index"]
    return {"sessions": pd.DataFrame(S), "tasks": pd.DataFrame(TK),
            "interventions": pd.DataFrame(EV, columns=ev_cols),
            "gaps": pd.DataFrame(GP, columns=["session_id", "duration", "observed"]),
            "gaps_true": pd.DataFrame(GT, columns=["session_id", "duration", "observed"])}


# --------------------------------------------------------------------------------------
# population truth
# --------------------------------------------------------------------------------------
def population_truth(cfg: ScenarioConfig, n_sessions: int = 40000, seed: int = 12345, chunk: int = 2000) -> dict:
    """Large-draw population values of the five estimands (from the hidden process), plus
    descriptive statistics (realised dispersion index, unknown share, tasks per session...).
    Monte Carlo SE of each truth is reported from chunk-wise means."""
    from lifelines import KaplanMeierFitter
    rng = np.random.default_rng(seed)
    sums = {"human_true": 0.0, "T": 0.0, "n_true": 0.0, "n_obs": 0.0, "active_obs": 0.0, "human_obs": 0.0,
            "unknown": 0.0, "tasks": 0, "auton_true": 0, "auton_obs": 0, "hmpt_true": 0.0, "hmpt_obs": 0.0,
            "tasks_unknown": 0, "success": 0}
    chunk_est = {"share": [], "rate": [], "task": [], "hmpt": [], "rate_obs": [], "share_obs": []}
    counts_all, expo_all = [], []
    gap_d, gap_o = [], []
    n_done = 0
    while n_done < n_sessions:
        n = min(chunk, n_sessions - n_done)
        d = simulate_sessions(cfg, n, rng)
        s, tk, gt = d["sessions"], d["tasks"], d["gaps_true"]
        sums["human_true"] += s["human_s_true"].sum(); sums["T"] += s["active_s_true"].sum()
        sums["n_true"] += s["n_interventions_true"].sum(); sums["n_obs"] += s["n_interventions"].sum()
        sums["active_obs"] += s["active_s"].sum(); sums["human_obs"] += s["human_s"].sum(); sums["unknown"] += s["unknown_s"].sum()
        sums["tasks"] += len(tk); sums["auton_true"] += tk["autonomous_true"].sum(); sums["auton_obs"] += tk["autonomous"].sum()
        sums["hmpt_true"] += tk["human_s_true"].sum(); sums["hmpt_obs"] += tk["human_s"].sum()
        sums["tasks_unknown"] += tk["has_unknown"].sum(); sums["success"] += tk["success"].sum()
        chunk_est["share"].append(1 - s["human_s_true"].sum() / s["active_s_true"].sum())
        chunk_est["rate"].append(s["n_interventions_true"].sum() / s["active_s_true"].sum() * HOUR)
        chunk_est["task"].append(tk["autonomous_true"].mean())
        chunk_est["hmpt"].append(tk["human_s_true"].mean() / 60)
        chunk_est["rate_obs"].append(s["n_interventions"].sum() / s["active_s"].sum() * HOUR)
        chunk_est["share_obs"].append(s["ai_s"].sum() / s["active_s"].sum())
        counts_all.append(s["n_interventions_true"].values); expo_all.append(s["active_s_true"].values / HOUR)
        gap_d.append(gt["duration"].values); gap_o.append(gt["observed"].values)
        n_done += n
    counts = np.concatenate(counts_all); expo = np.concatenate(expo_all)
    rate = sums["n_true"] / sums["T"] * HOUR
    mu = rate * expo
    disp = float(np.sum((counts - mu) ** 2 / mu) / (len(counts) - 1))
    gd = np.concatenate(gap_d); go = np.concatenate(gap_o)
    km = KaplanMeierFitter().fit(gd, go)
    nchunk = len(chunk_est["share"])
    out = {"scenario": cfg.name, "n_sessions_population": n_sessions, "seed": seed,
           "share_true": 1 - sums["human_true"] / sums["T"],
           # package definition (a): approval/hint dwell is moved from AI to human time
           "share_true_incl_approval_dwell": 1 - sums["hmpt_true"] / sums["T"],
           "rate_true": rate,
           "task_rate_true": sums["auton_true"] / sums["tasks"],
           "hmpt_true": sums["hmpt_true"] / sums["tasks"] / 60,
           "gap_median_true_h": float(km.median_survival_time_),
           "gap_median_exponential_h": math.log(2) / rate,
           "share_true_mcse": float(np.std(chunk_est["share"], ddof=1) / math.sqrt(nchunk)),
           "rate_true_mcse": float(np.std(chunk_est["rate"], ddof=1) / math.sqrt(nchunk)),
           "task_rate_true_mcse": float(np.std(chunk_est["task"], ddof=1) / math.sqrt(nchunk)),
           "hmpt_true_mcse": float(np.std(chunk_est["hmpt"], ddof=1) / math.sqrt(nchunk)),
           "share_observed_limit": sums["human_obs"] and (sums["active_obs"] - sums["human_obs"]) / sums["active_obs"],
           "rate_observed_limit": sums["n_obs"] / sums["active_obs"] * HOUR,
           "task_rate_observed_limit": sums["auton_obs"] / sums["tasks"],
           "hmpt_observed_limit": sums["hmpt_obs"] / sums["tasks"] / 60,
           "dispersion_index_true_counts": disp,
           "mean_events_per_session": float(counts.mean()),
           "mean_active_h_per_session": float(expo.mean()),
           "tasks_per_session": sums["tasks"] / n_sessions,
           "unknown_share": sums["unknown"] / sums["T"],
           "share_tasks_with_unknown": sums["tasks_unknown"] / sums["tasks"],
           "success_rate": sums["success"] / sums["tasks"]}
    return out


# --------------------------------------------------------------------------------------
# fast (vectorised) cluster bootstraps for ratio-of-sums statistics
# --------------------------------------------------------------------------------------
def boot_ratio_percentile(num, den, n_boot=500, alpha=0.05, seed=0):
    """Percentile cluster bootstrap of sum(num)/sum(den) over clusters (one row per cluster).
    Identical in distribution to autonomy_meter.intervals.cluster_bootstrap(method='percentile')
    with stat = ratio of sums; the same seed gives the same resampled clusters (verified in
    check_fast_bootstrap_matches_package)."""
    num = np.asarray(num, float); den = np.asarray(den, float)
    G = len(num)
    est = num.sum() / den.sum()
    if G < 2:
        return est, np.nan, np.nan
    rng = np.random.default_rng(seed)
    idx = rng.choice(G, size=(n_boot, G), replace=True)
    b = num[idx].sum(axis=1) / den[idx].sum(axis=1)
    b = b[np.isfinite(b)]
    lo, hi = np.quantile(b, [alpha / 2, 1 - alpha / 2])
    return float(est), float(lo), float(hi)


def boot_mean_t(sums, counts, n_boot=500, alpha=0.05, seed=0):
    """Bootstrap-t cluster bootstrap of a mean over units grouped in clusters, with the
    closed-form cluster-robust SE as studentiser (same construction as
    autonomy_meter.metrics.human_minutes_per_task). sums/counts: per-cluster sum and count."""
    sums = np.asarray(sums, float); counts = np.asarray(counts, float)
    G = len(sums)
    est = sums.sum() / counts.sum()
    if G < 2:
        return est, np.nan, np.nan

    def se(s, c, m):
        resid = s - m[..., None] * c
        return np.sqrt((resid ** 2).sum(axis=-1) * G / (G - 1)) / c.sum(axis=-1)

    se_hat = float(se(sums, counts, np.array(est)))
    rng = np.random.default_rng(seed)
    idx = rng.choice(G, size=(n_boot, G), replace=True)
    s_b = sums[idx]; c_b = counts[idx]
    m_b = s_b.sum(axis=1) / c_b.sum(axis=1)
    se_b = se(s_b, c_b, m_b)
    t = np.where(se_b > 0, (m_b - est) / np.where(se_b > 0, se_b, 1.0), 0.0)
    t_lo, t_hi = np.quantile(t[np.isfinite(t)], [alpha / 2, 1 - alpha / 2])
    return float(est), float(est - t_hi * se_hat), float(est - t_lo * se_hat)



def boot_mean_t_package_variant(sums, counts, n_boot=500, alpha=0.05, seed=0):
    """Mirror of autonomy_meter.intervals.cluster_bootstrap(method='bootstrap_t') as implemented
    in v0.1.0: inside each resample the studentiser groups rows by the ORIGINAL cluster label,
    so clusters drawn k times are merged into one group (G_unique instead of G). Kept for the
    coverage comparison with the standard construction in boot_mean_t."""
    sums = np.asarray(sums, float); counts = np.asarray(counts, float)
    G = len(sums)
    est = sums.sum() / counts.sum()
    if G < 2:
        return est, np.nan, np.nan
    resid0 = sums - est * counts
    se_hat = math.sqrt((resid0 ** 2).sum() * G / (G - 1)) / counts.sum()
    rng = np.random.default_rng(seed)
    idx = rng.choice(G, size=(n_boot, G), replace=True)
    M = (idx[:, :, None] == np.arange(G)[None, None, :]).sum(axis=1).astype(float)   # multiplicities (n_boot, G)
    s_b = M * sums[None, :]; c_b = M * counts[None, :]
    m_b = s_b.sum(axis=1) / c_b.sum(axis=1)
    Gu = (M > 0).sum(axis=1).astype(float)
    resid = s_b - m_b[:, None] * c_b
    se_b = np.sqrt((resid ** 2).sum(axis=1) * Gu / np.maximum(Gu - 1, 1)) / c_b.sum(axis=1)
    t = np.where(se_b > 0, (m_b - est) / np.where(se_b > 0, se_b, 1.0), 0.0)
    t_lo, t_hi = np.quantile(t[np.isfinite(t)], [alpha / 2, 1 - alpha / 2])
    return float(est), float(est - t_hi * se_hat), float(est - t_lo * se_hat)


def check_fast_bootstrap_matches_package(n_boot=200, seed=3):
    """Compare boot_ratio_percentile and boot_mean_t with the package's cluster_bootstrap
    on one simulated dataset using the same seed. Returns dict of max abs differences."""
    from autonomy_meter import intervals as iv
    rng = np.random.default_rng(seed)
    d = simulate_sessions(scenarios()["ii_nb_overdispersion"], 12, rng)
    s = d["sessions"]
    df = pd.DataFrame({"session_id": s["session_id"], "num": s["ai_s"], "den": s["active_s"]})
    pk = iv.cluster_bootstrap(df, lambda x: x["num"].sum() / x["den"].sum(), "session_id", n_boot=n_boot, seed=seed)
    est, lo, hi = boot_ratio_percentile(s["ai_s"], s["active_s"], n_boot=n_boot, seed=seed)
    out = {"ratio_lo_diff": abs(pk["lo"] - lo), "ratio_hi_diff": abs(pk["hi"] - hi)}
    tk = d["tasks"]
    dm = pd.DataFrame({"session_id": tk["session_id"], "m": tk["human_s"] / 60})

    def se_cluster(x):
        g = x.groupby("session_id")["m"].agg(["sum", "count"])
        G = len(g)
        mbar = x["m"].mean()
        resid = g["sum"] - mbar * g["count"]
        return float(np.sqrt((resid ** 2).sum() * G / (G - 1)) / len(x))

    pk_t = iv.cluster_bootstrap(dm, lambda x: x["m"].mean(), "session_id", n_boot=n_boot, seed=seed, method="bootstrap_t", se_fn=se_cluster)
    g = dm.groupby("session_id", sort=False)["m"].agg(["sum", "count"])
    est2, lo2, hi2 = boot_mean_t(g["sum"].values, g["count"].values, n_boot=n_boot, seed=seed)
    est3, lo3, hi3 = boot_mean_t_package_variant(g["sum"].values, g["count"].values, n_boot=n_boot, seed=seed)
    out.update({"boot_t_standard_vs_package_lo_diff": abs(pk_t["lo"] - lo2), "boot_t_standard_vs_package_hi_diff": abs(pk_t["hi"] - hi2),
                "boot_t_variant_vs_package_lo_diff": abs(pk_t["lo"] - lo3), "boot_t_variant_vs_package_hi_diff": abs(pk_t["hi"] - hi3)})
    return out


# --------------------------------------------------------------------------------------
# JSONL adapter: emit a schema-valid event log for a few simulated sessions
# --------------------------------------------------------------------------------------
def to_event_log(cfg: ScenarioConfig, n_sessions: int, seed: int, heartbeat_s: float = 5.0,
                 t0: str = "2026-10-01T08:00:00.000") -> tuple[list[dict], dict]:
    """Render simulated sessions (hidden process + unknown windows) as autonomy_meter events.
    Unknown windows are rendered as silence (no heartbeat/transition) so the loader's
    gap rule (> gap_tolerance) produces `unknown`. Returns (events, simulated_tables)."""
    rng = np.random.default_rng(seed)
    d = simulate_sessions(cfg, n_sessions, rng, keep_hidden=True)
    T0 = np.datetime64(t0)
    events = []
    cal_day = 0
    for i, s in d["sessions"].iterrows():
        sid, rid = s["session_id"], s["robot_id"]
        base_t = T0 + np.timedelta64(int(s["day"]) * 86400 + (i % cfg.sessions_per_day) * 7200, "s")
        seq = [0]
        U = s["unknown_iv"]

        def ts(off_s):
            return str(np.datetime_as_string(base_t + np.timedelta64(int(round(off_s * 1000)), "ms"), unit="ms")) + "Z"

        def emit(off_s, task_id, **kw):
            ev = {"robot_id": rid, "session_id": sid, "task_id": task_id, "seq": seq[0], "ts": ts(off_s)}
            ev.update(kw)
            events.append(ev); seq[0] += 1

        # marks: (time, priority, payload) ; priority orders simultaneous events
        marks = []
        marks.append((0.0, 0, {"type": "session_start", "scopes": ["whole_robot"], "initial_states": {"whole_robot": "idle"},
                               "heartbeat_interval_s": heartbeat_s, "hint_policy": "hint_as_event",
                               "task_registry_hash": "0" * 64, "plan_hash": "0" * 64}, None))
        tb = s["task_bounds"]; tt = s["task_types"]
        task_ids = [f"{sid}_t{k}" for k in range(len(tb))]
        tasks_df = d["tasks"][d["tasks"]["session_id"] == sid]
        for k, (a, b) in enumerate(tb):
            marks.append((a, 1, {"type": "task_start", "task_definition_id": cfg.task_type_ids[tt[k]]}, task_ids[k]))
            marks.append((a, 2, {"type": "transition", "scope": "whole_robot", "state": "ai", "reason": None, "actor_id": "ai:sim_policy"}, task_ids[k]))
            outcome = "success" if bool(tasks_df.iloc[k]["success"]) else "failure"
            marks.append((b, 0, {"type": "task_end", "outcome": outcome}, task_ids[k]))
        for (te, kind, dw, k) in s["events_hidden"]:
            if kind == "takeover":
                marks.append((te, 3, {"type": "transition", "scope": "whole_robot", "state": "human_remote", "reason": "human_initiated", "actor_id": "op_01"}, task_ids[k]))
                marks.append((te + dw, 0 if abs(te + dw - tb[k][1]) < 1e-9 else 3,
                              {"type": "transition", "scope": "whole_robot", "state": "ai", "reason": None, "actor_id": "ai:sim_policy"}, task_ids[k]))
            else:
                marks.append((max(te - 1.0, tb[k][0]), 3, {"type": "help_request", "scope": "whole_robot"}, task_ids[k]))
                marks.append((te, 3, {"type": kind, "scope": "whole_robot", "actor_id": "op_01", "reason": "ai_requested_help", "dwell_s": round(float(dw), 3)}, task_ids[k]))
        T = s["active_s_true"]
        # heartbeats
        hb = heartbeat_s
        while hb < T:
            k = int(np.searchsorted([a for a, _ in tb], hb, side="right") - 1)
            marks.append((hb, 1, {"type": "heartbeat"}, task_ids[k]))
            hb += heartbeat_s
        marks.append((T, 4, {"type": "transition", "scope": "whole_robot", "state": "idle", "reason": None, "actor_id": None}, None))
        marks.append((T, 5, {"type": "session_end"}, None))
        marks.sort(key=lambda m: (m[0], m[1]))
        for (t_m, pr, payload, tid) in marks:
            if _inside(t_m, U) and payload["type"] not in ("session_start", "session_end", "task_start", "task_end"):
                continue  # logging dropout: event lost
            # a human->ai return that falls inside an unknown window is lost too (handled above)
            emit(t_m, tid, **payload)
    return events, d


def write_jsonl(events: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
