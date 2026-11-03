"""Regression tests for the defects found by the simulation and DMV tracks (v0.1.1)."""
import math

import numpy as np
import pandas as pd
import pytest
from scipy import stats

import autonomy_meter as am
from autonomy_meter import intervals as iv
from autonomy_meter import metrics as mx
from tests.test_events_metrics import simple_session, multi


def test_version():
    assert am.__version__ == "0.1.1"


# 1. bootstrap-t studentiser must see G clusters per replicate, not G_unique
def test_bootstrap_t_resample_relabels_clusters():
    rng = np.random.default_rng(3)
    G = 12
    df = pd.DataFrame({"c": np.repeat(np.arange(G), 4), "x": rng.normal(size=4 * G)})
    seen = []

    def se_fn(d):
        seen.append(d["c"].nunique())
        return d.groupby("c")["x"].mean().std(ddof=1) / math.sqrt(d["c"].nunique())

    r = iv.cluster_bootstrap(df, lambda d: d["x"].mean(), "c", n_boot=40, seed=1, method="bootstrap_t", se_fn=se_fn)
    # the last call is on the original data (G clusters); every replicate must also have exactly G
    assert all(k == G for k in seen), set(seen)
    assert r["lo"] < r["estimate"] < r["hi"]


def test_bootstrap_t_coverage_small_clusters():
    # 5 clusters of 6 tasks, skewed within-cluster data: the corrected bootstrap-t should
    # cover the true mean close to nominal (v0.1.0 construction gave ~0.83-0.85)
    rng = np.random.default_rng(11)
    hits = 0; R = 150
    for _ in range(R):
        mu_c = rng.normal(0, 0.5, size=5)
        df = pd.DataFrame({"c": np.repeat(np.arange(5), 6), "x": np.repeat(mu_c, 6) + rng.exponential(1.0, 30) - 1.0})
        se_fn = lambda d: d.groupby("c")["x"].mean().std(ddof=1) / math.sqrt(d["c"].nunique())
        r = iv.cluster_bootstrap(df, lambda d: d["x"].mean(), "c", n_boot=150, seed=int(rng.integers(1e9)), method="bootstrap_t", se_fn=se_fn)
        hits += r["lo"] <= 0.0 <= r["hi"]
    assert hits / R > 0.88, hits / R


# 2. exponential CI from the parameter table, not the survival band
def test_exponential_gap_ci_matches_closed_form():
    t = multi()
    r = mx.gaps_between_interventions(t, "time")
    g = mx.intervention_gaps(t, "time")
    d = int(g["observed"].sum()); T = float(g["duration"].sum())
    lam = T / d
    assert r["exponential"]["mean_gap"] == pytest.approx(lam, rel=1e-6)
    z = stats.norm.ppf(0.975)
    assert r["exponential"]["mean_gap_ci"][0] == pytest.approx(lam * math.exp(-z / math.sqrt(d)), rel=1e-4)
    assert r["exponential"]["mean_gap_ci"][1] == pytest.approx(lam * math.exp(z / math.sqrt(d)), rel=1e-4)
    assert r["exponential"]["rate_ci"][0] == pytest.approx(1 / r["exponential"]["mean_gap_ci"][1])
    assert 0 < r["exponential"]["mean_gap_ci"][0] < lam < r["exponential"]["mean_gap_ci"][1]


# 3. default interval for (c) is NB2 Wald
def test_interventions_per_hour_default_is_nb2():
    # multi(): alternating 1/0 interventions is under-dispersed -> labelled boundary fallback
    r = mx.interventions_per_hour(multi(), n_boot=50)
    assert r["interval_method"] == "poisson_exact_fallback(alpha_at_zero_boundary)" and r["ci"] == r["ci_poisson_exact"]
    # over-dispersed sessions (0,0,0,0,4,0,0,5 interventions) -> NB2 Wald default, wider than Garwood
    evs = []
    for i in range(8):
        appr = tuple((60 + 5 * j, 1.0) for j in range(4 if i == 4 else 5)) if i in (4, 7) else ()
        evs += simple_session(sid=f"s{i}", approvals=appr)
    r = mx.interventions_per_hour(am.build_tables(evs), n_boot=50)
    assert r["interval_method"] == "nb2_wald" and r["ci"] == r["ci_negative_binomial"]
    assert r["ci"][0] <= r["estimate"] <= r["ci"][1]
    assert (r["ci"][1] - r["ci"][0]) > (r["ci_poisson_exact"][1] - r["ci_poisson_exact"][0])
    # zero events -> labelled Poisson fallback, never silent
    t0 = am.build_tables(simple_session("a") + simple_session("b"))
    r0 = mx.interventions_per_hour(t0, n_boot=10)
    assert r0["interval_method"].startswith("poisson_exact_fallback") and r0["ci"][0] == 0.0


# 4. multi-start NB fit on strongly over-dispersed clusters
@pytest.mark.parametrize("counts,expo", [
    ([0, 0, 0, 0, 40], [1.0] * 5),
    ([0] * 9 + [120], [500.0] * 10),
    ([3, 0, 0, 1] + [0] * 17 + [25, 0, 0], [1000.0] * 24),
])
def test_nb_multistart_overdispersed(counts, expo):
    r = iv.nb_rate_ci(counts, expo)
    glo, ghi = iv.poisson_exact_ci(int(sum(counts)), float(sum(expo)))
    assert r["method"] == "nb2_wald" and r["converged"]
    assert np.isfinite(r["lo"]) and np.isfinite(r["hi"]) and r["alpha_nb"] > 1
    assert (r["hi"] - r["lo"]) > (ghi - glo)
    assert r["lo"] <= r["rate"] <= r["hi"]


def test_nb_boundary_only_with_converged_fit():
    for counts in ([2, 2, 2, 2, 2, 2], [1, 2, 1, 3, 2, 2], [1, 0, 1, 0, 1, 0, 1, 0]):  # equi-/under-dispersed
        r = iv.nb_rate_ci(counts, [1.0] * len(counts))
        assert r["method"] == "poisson_exact_fallback(alpha_at_zero_boundary)" and r["converged"]
        assert r["alpha_nb"] == 0.0
    # genuinely over-dispersed data must never be labelled boundary
    r = iv.nb_rate_ci([0, 0, 0, 0, 40], [1.0] * 5)
    assert r["method"] == "nb2_wald"


# 5. time share default is the ratio-linearised t, with cluster_level
def test_time_share_default_linearised_and_cluster_level():
    t = multi()  # 8 sessions on 2 robots -> auto = session
    r = mx.time_autonomy_share(t, n_boot=50)
    assert r["cluster_level"] == "session" and r["interval_method"] == "ratio_linearised_t(session)"
    assert r["ci"][0] == pytest.approx(max(0.0, r["default"]["ci_linearised"][0]))
    assert r["lower"]["estimate"] <= r["estimate"] <= r["upper"]["estimate"]
    # 3 robots -> auto = robot, 3 clusters
    evs = []
    for i in range(6):
        evs += simple_session(sid=f"s{i}", rid=f"r{i % 3}", human=(30, 40) if i % 2 else None)
    t3 = am.build_tables(evs)
    r3 = mx.time_autonomy_share(t3, n_boot=50)
    assert r3["cluster_level"] == "robot" and r3["default"]["n_clusters"] == 3
    r3s = mx.time_autonomy_share(t3, n_boot=50, cluster_level="session")
    assert r3s["default"]["n_clusters"] == 6 and r3s["estimate"] == pytest.approx(r3["estimate"])
    # single session -> Wilson effective-n fallback
    r1 = mx.time_autonomy_share(am.build_tables(simple_session(human=(30, 40))), n_boot=10)
    assert r1["interval_method"] == "wilson_effective_n(single_cluster)"


# 6. task rate default switches to cluster-robust at >= 20 sessions
def test_task_rate_default_switch():
    assert mx.task_autonomy_rate(multi(8))["interval_method"] == "wilson"
    evs = []
    for i in range(24):
        evs += simple_session(sid=f"s{i}", rid=f"r{i % 2}", human=(30, 40) if i % 3 == 0 else None)
    r = mx.task_autonomy_rate(am.build_tables(evs))
    assert r["interval_method"] == "cluster_robust_glm" and r["ci"] == r["point"]["ci_cluster_robust"]
    assert r["point"]["n_clusters"] == 24


# 7. human minutes default is bootstrap-t
def test_human_minutes_default_bootstrap_t():
    r = mx.human_minutes_per_task(multi(), n_boot=100)
    assert r["interval_method"] == "cluster_bootstrap_bootstrap_t" and r["ci"] == r["ci_cluster_bootstrap_t"]
    r1 = mx.human_minutes_per_task(am.build_tables(simple_session()), n_boot=10)
    assert r1["interval_method"] == "t_on_tasks(single_session)"


# 8. planning docstring states the frailty scope
def test_planning_docstring_frailty_scope():
    doc = am.planning.hours_for_zero_events_nb.__doc__
    assert "EXACT" in doc and "shared" in doc and "CONSERVATIVE" in doc and "independent sessions" in doc
