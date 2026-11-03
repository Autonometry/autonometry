"""Closed-form checks of the interval primitives and planning formulas."""
import math

import numpy as np
import pandas as pd
import pytest
from scipy import stats
from statsmodels.stats.proportion import proportion_confint

from autonomy_meter import intervals as iv
from autonomy_meter import planning as pl


# ---------------------------------------------------------------- binomial
def test_wilson_matches_closed_form():
    k, n, alpha = 7, 20, 0.05
    z = stats.norm.ppf(1 - alpha / 2)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    lo, hi = iv.wilson_ci(k, n, alpha)
    assert lo == pytest.approx(centre - half, abs=1e-12)
    assert hi == pytest.approx(centre + half, abs=1e-12)
    assert (lo, hi) == pytest.approx(proportion_confint(k, n, alpha, method="wilson"))


def test_clopper_pearson_matches_beta_quantiles():
    k, n, alpha = 3, 12, 0.05
    lo, hi = iv.clopper_pearson_ci(k, n, alpha)
    assert lo == pytest.approx(stats.beta.ppf(alpha / 2, k, n - k + 1))
    assert hi == pytest.approx(stats.beta.ppf(1 - alpha / 2, k + 1, n - k))


def test_clopper_pearson_zero_matches_rule_of_three_form():
    n = 100
    lo, hi = iv.clopper_pearson_ci(0, n, 0.05)
    assert lo == 0.0
    # exact: 1 - (alpha/2)^(1/n) for the central interval
    assert hi == pytest.approx(1 - (0.025) ** (1 / n))
    # one-sided 95% upper limit vs rule of three
    _, hi1 = proportion_confint(0, n, alpha=0.10, method="beta")
    assert hi1 == pytest.approx(1 - 0.05 ** (1 / n))
    assert abs(hi1 - 3 / n) < 0.0005


def test_cluster_robust_proportion_degenerate_and_single_cluster():
    r = iv.proportion_cluster_robust_ci([1, 1, 1, 1], ["a", "a", "b", "b"])
    assert r["p"] == 1.0 and "wilson_fallback" in r["method"]
    r = iv.proportion_cluster_robust_ci([1, 0, 1, 0], ["a", "a", "a", "a"])
    assert r["n_clusters"] == 1 and "wilson_fallback" in r["method"]
    r = iv.proportion_cluster_robust_ci([1, 0, 1, 1, 0, 1, 0, 0], list("aabbccdd"))
    assert r["method"].startswith("cluster_glm") and r["lo"] <= r["p"] <= r["hi"]


# ---------------------------------------------------------------- poisson / NB
def test_garwood_matches_chi2_formula_and_gamma():
    k, E, alpha = 5, 12.5, 0.05
    lo, hi = iv.poisson_exact_ci(k, E, alpha)
    assert lo == pytest.approx(stats.chi2.ppf(alpha / 2, 2 * k) / 2 / E)
    assert hi == pytest.approx(stats.chi2.ppf(1 - alpha / 2, 2 * k + 2) / 2 / E)
    assert lo == pytest.approx(stats.gamma.ppf(alpha / 2, k) / E)
    assert hi == pytest.approx(stats.gamma.ppf(1 - alpha / 2, k + 1) / E)


def test_garwood_zero_events_upper_is_minus_log_alpha_half():
    E = 40.0
    lo, hi = iv.poisson_exact_ci(0, E, 0.05)
    assert lo == 0.0
    assert hi == pytest.approx(-math.log(0.025) / E)
    # one-sided 95% version equals rule-of-three constant 2.9957
    _, hi1 = iv.poisson_exact_ci(0, E, 0.10)
    assert hi1 * E == pytest.approx(-math.log(0.05))
    assert abs(hi1 * E - 3.0) < 0.005


def test_nb_rate_ci_overdispersed_wider_than_poisson():
    rng = np.random.default_rng(1)
    expo = rng.uniform(1, 3, size=40)
    mu = 2.0 * expo
    counts = rng.negative_binomial(n=1 / 0.8, p=1 / (1 + 0.8 * mu))
    nb = iv.nb_rate_ci(counts, expo)
    plo, phi = iv.poisson_exact_ci(int(counts.sum()), expo.sum())
    assert nb["method"] == "nb2_wald"
    assert nb["alpha_nb"] > 0.2
    assert nb["hi"] - nb["lo"] > phi - plo
    assert nb["lo"] <= nb["rate"] <= nb["hi"]
    assert iv.dispersion_index(counts, expo) > 1.5


def test_nb_rate_ci_zero_events_falls_back():
    nb = iv.nb_rate_ci([0, 0, 0], [1.0, 2.0, 3.0])
    assert nb["method"].startswith("poisson_exact_fallback")
    assert nb["lo"] == 0.0 and nb["hi"] == pytest.approx(-math.log(0.025) / 6.0)


# ---------------------------------------------------------------- bootstrap / ratio
def test_cluster_bootstrap_percentile_and_t_cover_point_and_single_cluster_undefined():
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"c": np.repeat(np.arange(30), 5), "x": rng.normal(1.0, 1.0, 150)})
    r = iv.cluster_bootstrap(df, lambda d: d["x"].mean(), "c", n_boot=300, seed=1)
    assert r["lo"] < r["estimate"] < r["hi"] and r["n_clusters"] == 30
    rt = iv.cluster_bootstrap(df, lambda d: d["x"].mean(), "c", n_boot=100, seed=1, method="bootstrap_t",
                              se_fn=lambda d: d.groupby("c")["x"].mean().std(ddof=1) / np.sqrt(d["c"].nunique()))
    assert rt["lo"] < rt["estimate"] < rt["hi"]
    one = iv.cluster_bootstrap(df[df["c"] == 0], lambda d: d["x"].mean(), "c", n_boot=10)
    assert np.isnan(one["lo"]) and "single_cluster" in one["method"]


def test_ratio_linearised_matches_manual():
    num = np.array([3.0, 5.0, 2.0]); den = np.array([4.0, 6.0, 5.0])
    r = iv.ratio_linearised_ci(num, den)
    R = num.sum() / den.sum()
    resid = num - R * den
    se = math.sqrt((resid ** 2).sum() * 3 / 2) / den.sum()
    assert r["estimate"] == pytest.approx(R) and r["se"] == pytest.approx(se)
    assert r["hi"] - r["estimate"] == pytest.approx(stats.t.ppf(0.975, 2) * se)


# ---------------------------------------------------------------- planning
def test_rule_of_three_is_alpha_005_special_case():
    assert pl.hours_for_zero_events(0.01, 0.05) == pytest.approx(-math.log(0.05) / 0.01)
    assert pl.hours_for_zero_events(0.01, 0.05) == pytest.approx(299.57, abs=0.01)
    assert pl.rule_of_three(300.0) == pytest.approx(0.01)
    # consistency: Garwood one-sided upper at zero events over that exposure equals lambda_max
    T = pl.hours_for_zero_events(0.01, 0.05)
    _, hi = iv.poisson_exact_ci(0, T, 0.10)
    assert hi == pytest.approx(0.01)


def test_tasks_for_zero_failures_binomial_exact():
    n = pl.tasks_for_zero_failures(0.05, 0.05)
    assert n == 59  # ln(0.05)/ln(0.95) = 58.4
    assert (1 - 0.05) ** n <= 0.05 < (1 - 0.05) ** (n - 1)
    assert pl.tasks_for_zero_failures_clustered(0.05, cluster_size=5, icc=0.2) == math.ceil(59 * 1.8)


def test_nb_inflated_zero_event_exposure_exceeds_poisson_and_converges():
    T0 = pl.hours_for_zero_events(0.1)
    assert pl.hours_for_zero_events_nb(0.1, 0.5) > T0
    assert pl.hours_for_zero_events_nb(0.1, 1e-9) == pytest.approx(T0, rel=1e-6)
    # P(0) under NB at the returned exposure equals alpha
    a = 0.5; T = pl.hours_for_zero_events_nb(0.1, a, 0.05)
    assert (1 + a * 0.1 * T) ** (-1 / a) == pytest.approx(0.05)


def test_hours_to_demonstrate_rate_and_power():
    r = pl.hours_to_demonstrate_rate(0.1, 0.05, 0.05)
    assert r["upper_limit_at_T"] < 0.1 and r["hours"] > pl.hours_for_zero_events(0.1)
    assert pl.tasks_to_demonstrate_rate(0.1, 0.02) > pl.tasks_for_zero_failures(0.1)
    p1 = pl.power_two_periods(1.0, 0.5, 100, 100)
    p2 = pl.power_two_periods(1.0, 0.5, 100, 100, alpha_nb=0.5)
    assert 0.95 < p1 <= 1.0 and p2 < p1  # 100 vs 50 expected events: Wald power ~0.98
    h = pl.hours_for_power_two_periods(1.0, 0.5, power=0.8)
    assert pl.power_two_periods(1.0, 0.5, h, h) == pytest.approx(0.8, abs=1e-6)
    assert pl.hours_for_power_two_periods(1.0, 0.9, alpha_nb=1.0) == math.inf
