"""Estimators of autonometry.stats.intervals against independent values.

Each test says where its expected numbers come from: a closed form computed in the test, SciPy
called directly, or the reference package ``autonomy_meter`` v0.1.1 (research/reference-impl,
``intervals``) run once on the same input, its output copied here. Warnings are errors
(pyproject ``filterwarnings``), so an estimator that warns fails its test.
"""

import math
import warnings

import numpy as np
import pytest
from scipy import optimize, stats
from statsmodels.duration.survfunc import SurvfuncRight
from statsmodels.tools.sm_exceptions import HessianInversionWarning

from autonometry.stats import intervals as iv

Z95 = stats.norm.ppf(0.975)


# -- proportions ---------------------------------------------------------------------------------


def test_wilson_matches_its_closed_form() -> None:
    k, n = 7, 20
    p = k / n
    centre = (p + Z95**2 / (2 * n)) / (1 + Z95**2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95**2 / (4 * n**2)) / (1 + Z95**2 / n)
    interval = iv.wilson(k, n, 0.05)
    assert interval.method == "wilson"
    assert interval.as_list() == pytest.approx([centre - half, centre + half], rel=1e-12)


def test_proportions_without_trials_are_undefined() -> None:
    assert iv.wilson(0, 0, 0.05).method == "undefined(no_trials)"
    assert iv.clopper_pearson(0, 0, 0.05).as_list() is None
    assert iv.cluster_robust_proportion([], [], 0.05).defined is False


def test_clopper_pearson_matches_beta_quantiles() -> None:
    k, n = 3, 15
    expected = [stats.beta.ppf(0.025, k, n - k + 1), stats.beta.ppf(0.975, k + 1, n - k)]
    assert iv.clopper_pearson(k, n, 0.05).as_list() == pytest.approx(expected, rel=1e-10)
    # At k = 0 the lower limit is 0 and the upper one is 1 - (alpha/2)^(1/n).
    assert iv.clopper_pearson(0, 10, 0.05).as_list() == pytest.approx([0.0, 1 - 0.025**0.1])


def test_cluster_robust_proportion_matches_closed_form_sandwich() -> None:
    # Reference package run once: proportion_cluster_robust_ci gave lo 0.5530518954395236,
    # hi 0.8105844681968397 (identity-link GLM). Closed form: se = sqrt(G/(G-1) sum e_g^2) / N.
    y = [1, 0, 1, 1, 0, 1, 1, 1, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 0, 1, 1]
    clusters = list("aabbbccddeeeffgghhiijj")
    p = sum(y) / len(y)
    residual = dict.fromkeys(clusters, 0.0)
    for value, c in zip(y, clusters, strict=True):
        residual[c] += value - p
    g = len(residual)
    se = math.sqrt(g / (g - 1) * sum(e**2 for e in residual.values())) / len(y)
    interval = iv.cluster_robust_proportion(y, clusters, 0.05)
    assert interval.method == "cluster_robust_glm"
    assert interval.as_list() == pytest.approx([p - Z95 * se, p + Z95 * se], rel=1e-10)
    assert interval.as_list() == pytest.approx([0.5530518954395236, 0.8105844681968397])


@pytest.mark.parametrize(
    ("y", "clusters"),
    [([1, 0, 1], ["a", "a", "a"]), ([1, 1, 1, 1], ["a", "b", "c", "d"])],
)
def test_cluster_robust_proportion_falls_back_to_wilson(y: list[int], clusters: list[str]) -> None:
    interval = iv.cluster_robust_proportion(y, clusters, 0.05)
    assert interval.method == "wilson_fallback(single_cluster_or_degenerate)"
    assert interval.as_list() == iv.wilson(sum(y), len(y), 0.05).as_list()


# -- rates ---------------------------------------------------------------------------------------


def test_garwood_matches_gamma_quantiles() -> None:
    k, exposure = 7, 2.5
    expected = [stats.gamma.ppf(0.025, k) / exposure, stats.gamma.ppf(0.975, k + 1) / exposure]
    assert iv.garwood(k, exposure, 0.05).as_list() == pytest.approx(expected, rel=1e-10)


def test_garwood_with_zero_events_is_the_rule_of_three_upper_limit() -> None:
    interval = iv.garwood(0, 10.0, 0.05)
    assert interval.as_list() == pytest.approx([0.0, -math.log(0.025) / 10.0])
    assert iv.garwood(3, 0.0, 0.05).method == "undefined(no_exposure)"


EXPOSURES = [0.4, 0.9, 0.3, 1.1, 0.5, 0.2, 1.6, 0.7]


def test_nb2_rate_matches_the_reference_on_overdispersed_counts() -> None:
    # Reference package run once: nb_rate_ci gave nb2_wald, alpha 0.9868035490121723,
    # lo 4.736458376657026, hi 20.62626897177382.
    fit = iv.nb2_rate([0, 12, 1, 25, 2, 0, 40, 3], EXPOSURES, 0.05)
    assert fit.interval.method == "nb2_wald"
    assert fit.converged
    assert fit.alpha_nb == pytest.approx(0.9868035490121723, rel=1e-4)
    assert fit.interval.as_list() == pytest.approx([4.736458376657026, 20.62626897177382], 1e-4)


def test_nb2_rate_matches_an_independent_likelihood_fit() -> None:
    counts = np.array([0, 12, 1, 25, 2, 0, 40, 3], dtype=float)
    exposure = np.array(EXPOSURES)

    def negative_loglik(theta: np.ndarray) -> float:
        b, log_alpha = theta
        alpha = math.exp(log_alpha)
        mu = np.exp(b) * exposure
        r = 1 / alpha
        terms = (
            stats.nbinom.logpmf(counts, r, r / (r + mu))  # NB2: var = mu + alpha mu^2
        )
        return float(-terms.sum())

    best = optimize.minimize(
        negative_loglik,
        [math.log(10), 0.0],
        method="Nelder-Mead",
        options={"xatol": 1e-10, "fatol": 1e-12, "maxiter": 5000},
    )
    fit = iv.nb2_rate(counts, exposure, 0.05)
    assert fit.alpha_nb == pytest.approx(math.exp(best.x[1]), rel=1e-3)
    assert fit.interval.lo is not None
    assert fit.interval.hi is not None
    centre = math.sqrt(fit.interval.lo * fit.interval.hi)  # Wald on the log scale
    assert centre == pytest.approx(math.exp(best.x[0]), rel=1e-4)


def test_nb2_rate_at_the_zero_dispersion_boundary_falls_back_to_garwood() -> None:
    # Reference package run once: poisson_exact_fallback(alpha_at_zero_boundary), lo
    # 8.032687885966187, hi 13.549454983621635.
    counts = [3, 9, 1, 14, 6, 0, 22, 5]
    fit = iv.nb2_rate(counts, EXPOSURES, 0.05)
    assert fit.interval.method == "poisson_exact_fallback(alpha_at_zero_boundary)"
    assert fit.alpha_nb == 0.0
    assert fit.interval.as_list() == pytest.approx(
        iv.garwood(sum(counts), sum(EXPOSURES), 0.05).as_list()
    )
    assert fit.interval.as_list() == pytest.approx([8.032687885966187, 13.549454983621635])


@pytest.mark.parametrize(
    ("counts", "exposures", "method"),
    [
        ([4], [1.0], "poisson_exact_fallback(<2 clusters or zero count)"),
        ([0, 0, 0], [1.0, 2.0, 0.5], "poisson_exact_fallback(<2 clusters or zero count)"),
        ([1, 2], [0.0, 0.0], "undefined(no_exposure)"),
    ],
)
def test_nb2_rate_fallbacks_are_labelled(
    counts: list[int], exposures: list[float], method: str
) -> None:
    assert iv.nb2_rate(counts, exposures, 0.05).interval.method == method


def test_nb2_rate_names_every_reason_when_every_attempt_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(iv, "_nb_attempt", lambda *_: "ConvergenceWarning")
    fit = iv.nb2_rate([0, 12, 1, 25], EXPOSURES[:4], 0.05)
    assert fit.interval.method == "poisson_exact_fallback(nb_fit_failed: ConvergenceWarning)"
    assert fit.interval.defined
    assert not fit.converged


def test_nb2_rate_with_an_undefined_standard_error_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = iv._Attempt(np.array([2.0, 0.5]), -10.0, math.nan)
    monkeypatch.setattr(iv, "_nb_attempt", lambda *_: attempt)
    fit = iv.nb2_rate([0, 12, 1, 25], EXPOSURES[:4], 0.05)
    assert fit.interval.method == "poisson_exact_fallback(nb_se_undefined)"
    assert fit.alpha_nb == 0.5


def test_nb2_attempt_that_warns_is_rejected_with_its_reason() -> None:
    counts = np.array([0.0, 12.0, 1.0, 25.0])
    offset = np.log(np.array(EXPOSURES[:4]))
    # Newton from a negative dispersion takes the log of a negative number: RuntimeWarning.
    reason = iv._nb_attempt(counts, offset, np.array([2.0, -5.0]), "newton")
    assert isinstance(reason, str)
    assert "Warning" in reason or reason in {"not_converged", "not_finite"}


def test_better_prefers_llf_then_a_finite_standard_error() -> None:
    finite = iv._Attempt(np.array([1.0, 0.1]), -5.0, 0.2)
    undefined_se = iv._Attempt(np.array([1.0, 0.1]), -5.0, math.nan)
    worse = iv._Attempt(np.array([1.0, 0.1]), -6.0, 0.2)
    assert iv._better(finite, None)
    assert iv._better(finite, undefined_se)
    assert not iv._better(undefined_se, finite)
    assert not iv._better(worse, finite)


def test_dispersion_index_matches_its_closed_form() -> None:
    # Reference package run once: dispersion_index gave 5.33480564186244 on these counts.
    counts = [0, 12, 1, 25, 2, 0, 40, 3]
    rate = sum(counts) / sum(EXPOSURES)
    pearson = sum((k - rate * e) ** 2 / (rate * e) for k, e in zip(counts, EXPOSURES, strict=True))
    value = iv.dispersion_index(counts, EXPOSURES)
    assert value == pytest.approx(pearson / (len(counts) - 1))
    assert value == pytest.approx(5.33480564186244)
    assert iv.dispersion_index([3], [1.0]) is None
    assert iv.dispersion_index([0, 0], [1.0, 1.0]) is None


# -- ratios and means ----------------------------------------------------------------------------

NUM = [0.9, 1.7, 0.4, 2.2, 0.8]
DEN = [1.0, 2.0, 0.5, 2.5, 1.1]


def test_ratio_linearised_t_matches_its_closed_form() -> None:
    # Reference package run once: ratio_linearised_ci gave lo 0.7718621863036358,
    # hi 0.9182786587667869.
    ratio = sum(NUM) / sum(DEN)
    g = len(NUM)
    variance = sum((n - ratio * d) ** 2 for n, d in zip(NUM, DEN, strict=True))
    se = math.sqrt(variance * g / (g - 1)) / sum(DEN)
    half = stats.t.ppf(0.975, g - 1) * se
    interval = iv.ratio_linearised_t(NUM, DEN, 0.05)
    assert interval.as_list() == pytest.approx([ratio - half, ratio + half], rel=1e-12)
    assert interval.as_list() == pytest.approx([0.7718621863036358, 0.9182786587667869])
    assert iv.ratio_linearised_t([1.0], [2.0], 0.05).method == "undefined(single_cluster)"
    assert iv.ratio_linearised_t([1.0, 1.0], [0.0, 0.0], 0.05).method == "undefined(no_denominator)"


def test_wilson_effective_n_matches_the_reference() -> None:
    # Reference package run once: wilson_effective_ci(0.82, 7) gave (0.45135851045241787,
    # 0.9618700111049019).
    interval = iv.wilson_effective_n(0.82, 7, 0.05)
    assert interval.as_list() == pytest.approx([0.45135851045241787, 0.9618700111049019])


def test_t_interval_matches_scipy() -> None:
    values = [1.0, 2.5, 0.5, 4.0]
    expected = stats.t.interval(0.95, len(values) - 1, loc=np.mean(values), scale=stats.sem(values))
    assert iv.t_interval(values, 0.05).as_list() == pytest.approx(list(expected))
    assert iv.t_interval([1.0], 0.05).method == "undefined(fewer_than_two_values)"


def test_percentile_bootstrap_matches_the_reference_with_the_same_seed() -> None:
    # Reference package run once: cluster_bootstrap(percentile, n_boot=400, seed=7) of the ratio
    # of sums gave lo 0.7708333333333333, hi 0.8800000000000001 (rng.choice draws the same
    # indices as rng.integers).
    interval = iv.bootstrap_ratio_percentile(NUM, DEN, 400, 0.05, 7)
    assert interval.as_list() == pytest.approx([0.7708333333333333, 0.8800000000000001])
    assert iv.bootstrap_ratio_percentile([1.0], [1.0], 10, 0.05, 0).defined is False
    assert iv.bootstrap_ratio_percentile([1.0, 2.0], [0.0, 0.0], 10, 0.05, 0).method == (
        "undefined(too_few_replicates)"
    )


def test_percentile_bootstrap_matches_an_explicit_loop() -> None:
    rng = np.random.default_rng(3)
    ratios = []
    for _ in range(200):
        draw = rng.integers(0, len(NUM), size=len(NUM))
        ratios.append(sum(NUM[i] for i in draw) / sum(DEN[i] for i in draw))
    expected = np.quantile(ratios, [0.025, 0.975])
    assert iv.bootstrap_ratio_percentile(NUM, DEN, 200, 0.05, 3).as_list() == pytest.approx(
        list(expected)
    )


def test_bootstrap_t_relabels_resampled_clusters_as_the_reference() -> None:
    # Reference package v0.1.1 run once: cluster_bootstrap(method="bootstrap_t", n_boot=400,
    # seed=11) on tasks of five sessions (sums 1.5, 0.2, 3.4, 2.2, 1.0; counts 2, 1, 3, 1, 2)
    # with the closed-form cluster SE gave lo 0.48658721475612504, hi 1.9950436825282416.
    interval = iv.bootstrap_mean_t([1.5, 0.2, 3.4, 2.2, 1.0], [2, 1, 3, 1, 2], 400, 0.05, 11)
    assert interval.method == "cluster_bootstrap_t"
    assert interval.as_list() == pytest.approx([0.48658721475612504, 1.9950436825282416])
    assert iv.bootstrap_mean_t([1.0], [1.0], 10, 0.05, 0).defined is False


def test_bootstrap_t_with_no_spread_is_the_estimate() -> None:
    interval = iv.bootstrap_mean_t([0.0, 0.0, 0.0], [1, 2, 1], 50, 0.05, 0)
    assert interval.as_list() == [0.0, 0.0]


def test_bootstrap_clusters_leaves_out_failed_replicates() -> None:
    clusters = [1.0, 2.0, 3.0, 4.0]
    interval, failed = iv.bootstrap_clusters(
        clusters, lambda draw: None if 1.0 in draw else sum(draw), 100, 0.05, 0
    )
    assert interval.defined
    assert 0 < failed < 100
    assert iv.bootstrap_clusters([1.0], sum, 10, 0.05, 0) == (iv.undefined("single_cluster"), 0)
    assert iv.bootstrap_clusters([1.0, 2.0], lambda _: None, 10, 0.05, 0)[1] == 10


def test_interval_helpers() -> None:
    interval = iv.Interval(-0.2, 1.3, "m")
    assert interval.clipped(0.0, 1.0) == iv.Interval(0.0, 1.0, "m")
    assert iv.undefined("x").clipped(0.0, 1.0) == iv.undefined("x")
    assert iv.Interval(None, 1.0, "m").as_list() == [None, 1.0]
    assert iv.Interval(None, None, "m").as_list() is None


# -- survival ------------------------------------------------------------------------------------


def test_kaplan_meier_median_by_hand() -> None:
    # Five events at 1..5: S = 0.8, 0.6, 0.4, 0.2, 0; the first time with S < 0.5 is 3.
    km = iv.kaplan_meier([1.0, 2.0, 3.0, 4.0, 5.0], [1, 1, 1, 1, 1], 0.05, (2.5,))
    assert km.median == 3.0
    assert km.n_events == 5
    point = km.at[0]
    assert point.survival == pytest.approx(0.6)
    # Greenwood: var S = S^2 sum d/(n(n-d)) = 0.36 (1/20 + 1/12); cloglog interval.
    se = math.sqrt(0.36 * (1 / 20 + 1 / 12))
    theta = Z95 * se / (0.6 * abs(math.log(0.6)))
    assert point.interval.as_list() == pytest.approx(
        [0.6 ** math.exp(theta), 0.6 ** math.exp(-theta)]
    )


def test_kaplan_meier_median_interval_matches_statsmodels() -> None:
    rng = np.random.default_rng(4)
    durations = rng.exponential(1.0, 40)
    observed = (rng.random(40) < 0.7).astype(int)
    observed[np.argmax(durations)] = 0  # last time censored: S never reaches 0, no log(0)
    expected = SurvfuncRight(durations, observed).quantile_ci(0.5, alpha=0.05)
    km = iv.kaplan_meier(list(durations), list(observed), 0.05, ())
    assert km.median_interval.method == "km_cloglog"
    assert km.median_interval.lo == pytest.approx(expected[0])
    assert km.median_interval.hi == pytest.approx(expected[1])


def test_kaplan_meier_without_events_has_no_median() -> None:
    km = iv.kaplan_meier([1.0, 2.0], [0, 0], 0.05, (1.0,))
    assert km.median is None
    assert km.median_interval.method == "undefined(no_events)"
    assert km.at[0].survival == 1.0


def test_kaplan_meier_median_not_reached_and_unbracketed() -> None:
    km = iv.kaplan_meier([1.0, 2.0, 3.0], [1, 0, 0], 0.05, (0.5, 5.0))
    assert km.median is None
    assert [p.survival for p in km.at] == [1.0, pytest.approx(2 / 3)]
    single = iv.kaplan_meier([1.0], [1], 0.05, ())
    assert single.median == 1.0
    assert single.median_interval.method == "undefined(median_not_bracketed)"


def test_kaplan_meier_upper_limit_beyond_the_last_event_is_open() -> None:
    km = iv.kaplan_meier([0.0, 0.0, 1.0, 2.0], [1, 1, 1, 0], 0.05, ())
    assert km.median_interval.lo == 0.0
    assert km.median_interval.hi is None


def test_exponential_fit_matches_its_closed_form() -> None:
    durations, observed = [0.5, 1.5, 2.0, 1.0], [1, 1, 0, 1]
    fit = iv.exponential_fit(durations, observed, 0.05)
    assert fit is not None
    mean = 5.0 / 3
    assert fit.mean == pytest.approx(mean)
    spread = math.exp(Z95 / math.sqrt(3))
    assert fit.mean_interval.as_list() == pytest.approx([mean / spread, mean * spread])
    assert fit.rate_interval.as_list() == pytest.approx([1 / (mean * spread), spread / mean])
    assert iv.exponential_fit([1.0], [0], 0.05) is None


def test_weibull_fit_matches_scipy_on_uncensored_data() -> None:
    data = stats.weibull_min.rvs(0.7, scale=2.0, size=80, random_state=5)
    shape, _, scale = stats.weibull_min.fit(data, floc=0)
    fit = iv.weibull_fit(list(data), [1] * len(data))
    assert fit == pytest.approx((shape, scale), rel=1e-6)


def test_profile_weibull_shape_equals_the_scipy_censored_fit() -> None:
    rng = np.random.default_rng(6)
    durations = list(rng.weibull(1.6, 60) * 3.0)
    observed = list((rng.random(60) < 0.75).astype(int))
    fit = iv.weibull_fit(durations, observed)
    assert fit is not None
    assert iv.weibull_shape(durations, observed) == pytest.approx(fit[0], rel=1e-3)


def test_weibull_needs_three_positive_events() -> None:
    assert iv.weibull_fit([0.0, 1.0, 2.0], [1, 1, 1]) is None
    assert iv.weibull_shape([0.0, 1.0, 2.0], [1, 1, 1]) is None
    # Equal event times: the shape grows without bound, so there is no root.
    assert iv.weibull_shape([1.0, 1.0, 1.0], [1, 1, 1]) is None


def test_weibull_fit_that_warns_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def warn(*_: object, **__: object) -> tuple[float, float, float]:
        raise RuntimeWarning("overflow")

    monkeypatch.setattr(iv.stats.weibull_min, "fit", warn)
    assert iv.weibull_fit([1.0, 2.0, 3.0, 4.0], [1, 1, 1, 1]) is None
    monkeypatch.setattr(iv.stats.weibull_min, "fit", lambda *_, **__: (math.inf, 0.0, 1.0))
    assert iv.weibull_fit([1.0, 2.0, 3.0, 4.0], [1, 1, 1, 1]) is None


def test_glm_failure_falls_back_to_wilson(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def fit(self, **_: object) -> None:
            raise RuntimeWarning("bad")

    monkeypatch.setattr(iv, "GLM", Failing)
    interval = iv.cluster_robust_proportion([1, 0, 1, 0], list("abcd"), 0.05)
    assert interval.method == "wilson_fallback(glm_failed: RuntimeWarning)"


class _FakeFit:
    def __init__(self, params: list[float], converged: bool, bse: object) -> None:
        self.params = params
        self.llf = -3.0
        self.mle_retvals = {"converged": converged}
        self._bse = bse

    @property
    def bse(self) -> object:
        if isinstance(self._bse, Warning):
            raise self._bse
        return self._bse


def _fake_model(fit: _FakeFit, warning: type[Warning] | None = None) -> object:
    class Model:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def fit(self, **_: object) -> _FakeFit:
            if warning is not None:
                warnings.warn("fake", warning, stacklevel=1)
            return fit

    return Model


@pytest.mark.parametrize(
    ("fit", "warning", "expected"),
    [
        (_FakeFit([2.0, 0.5], True, [0.1, 0.2]), UserWarning, "UserWarning"),
        (_FakeFit([2.0, 0.5], False, [0.1, 0.2]), None, "not_converged"),
        (_FakeFit([math.inf, 0.5], True, [0.1, 0.2]), None, "not_finite"),
    ],
)
def test_nb2_attempt_rejections(
    monkeypatch: pytest.MonkeyPatch, fit: _FakeFit, warning: type[Warning] | None, expected: str
) -> None:
    monkeypatch.setattr(iv, "NegativeBinomial", _fake_model(fit, warning))
    assert iv._nb_attempt(np.ones(3), np.zeros(3), None, "bfgs") == expected


def test_nb2_attempt_with_a_failed_hessian_or_se_keeps_the_fit_without_se(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fit = _FakeFit([2.0, 0.5], True, [0.1, 0.2])
    monkeypatch.setattr(iv, "NegativeBinomial", _fake_model(fit, HessianInversionWarning))
    attempt = iv._nb_attempt(np.ones(3), np.zeros(3), None, "bfgs")
    assert isinstance(attempt, iv._Attempt)
    assert not attempt.se_ok
    warned = _FakeFit([2.0, 0.5], True, RuntimeWarning("sqrt of negative variance"))
    monkeypatch.setattr(iv, "NegativeBinomial", _fake_model(warned))
    attempt = iv._nb_attempt(np.ones(3), np.zeros(3), None, "bfgs")
    assert isinstance(attempt, iv._Attempt)
    assert not attempt.se_ok
    fine = iv._nb_attempt(np.ones(3), np.zeros(3), np.array([0.0, 0.1]), "nm")
    assert isinstance(fine, iv._Attempt | str)
