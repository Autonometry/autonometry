"""Estimators and confidence intervals (definitions.md 7; reference package ``autonomy_meter``
v0.1.1, ``intervals``, ported with the defects listed in ``research/README.md`` fixed).

Package functions are used wherever one exists:

- binomial proportions: statsmodels ``proportion_confint`` (``wilson``; ``beta`` is
  Clopper-Pearson) and a cluster-robust binomial GLM (``GLM(...).fit(cov_type="cluster")``);
- Poisson rates: exact Garwood (1936) limits from SciPy ``chi2``;
- over-dispersed rates: statsmodels ``NegativeBinomial`` (NB2, log-exposure offset);
- survival: statsmodels ``SurvfuncRight`` (Kaplan-Meier) and SciPy ``CensoredData`` (Weibull).

The custom numerics are the cluster bootstraps, the Taylor-linearised ratio variance, the
exponential-gap Wald interval and the profile-likelihood Weibull shape used inside the bootstrap.

Every call into statsmodels or SciPy that can fail numerically runs with warnings turned into
errors (:func:`strict`): an attempt that warns is rejected and the method name says why. Nothing is
silenced. Bootstraps take an explicit seed and draw from :func:`numpy.random.default_rng`.

References: Garwood F. (1936) Biometrika 28:437-442; Kalbfleisch J. D., Prentice R. L. (2002)
The Statistical Analysis of Failure Time Data, 2nd ed., 1.4 (exponential Greenwood band).
"""

import math
import warnings
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy import optimize, stats
from statsmodels.discrete.discrete_model import NegativeBinomial
from statsmodels.duration.survfunc import SurvfuncRight
from statsmodels.genmod.families import Binomial
from statsmodels.genmod.generalized_linear_model import GLM
from statsmodels.stats.proportion import proportion_confint
from statsmodels.tools.sm_exceptions import HessianInversionWarning

Array = npt.NDArray[np.float64]
Mask = npt.NDArray[np.bool_]

#: Two clusters, values or replicates are the fewest a spread can be estimated from.
MIN_SPREAD: Final = 2

#: A converged NB2 fit with a dispersion below this is at the zero boundary: its Hessian is
#: singular there, so standard errors are undefined (reference v0.1.1).
NB_ALPHA_BOUNDARY: Final = 1e-4
#: Log-likelihoods closer than this count as a tie between NB2 attempts.
_LLF_TIE: Final = 1e-6
#: What a numerical fit may raise once warnings are errors.
FIT_FAILURES: Final = (Warning, ArithmeticError, ValueError, np.linalg.LinAlgError)
#: Fewer events than this leave the Weibull shape undefined (reference v0.1.1).
WEIBULL_MIN_EVENTS: Final = 3
_LOG_SHAPE_BRACKET: Final = (-12.0, 12.0)


@contextmanager
def strict() -> Iterator[None]:
    """Turn every warning raised inside the block into an exception."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        yield


@dataclass(frozen=True, slots=True)
class Interval:
    """A two-sided confidence interval and the name of its method. The limits are ``None`` when
    the method cannot give an interval; the method name then says why."""

    lo: float | None
    hi: float | None
    method: str

    @property
    def defined(self) -> bool:
        return self.lo is not None and self.hi is not None

    def as_list(self) -> list[float | None] | None:
        """``[lo, hi]``; ``None`` when neither limit exists. One open limit stays ``None``,
        as the upper limit of a Kaplan-Meier median beyond the last event."""
        if self.lo is None and self.hi is None:
            return None
        return [self.lo, self.hi]

    def clipped(self, low: float, high: float) -> "Interval":
        """The interval with both limits clipped to ``[low, high]``."""
        if self.lo is None or self.hi is None:
            return self
        return Interval(min(max(self.lo, low), high), min(max(self.hi, low), high), self.method)

    def renamed(self, method: str) -> "Interval":
        return Interval(self.lo, self.hi, method)


def undefined(reason: str) -> Interval:
    """An interval that cannot be computed, labelled ``undefined(<reason>)``."""
    return Interval(None, None, f"undefined({reason})")


def z_value(alpha: float) -> float:
    """The standard normal quantile ``1 - alpha / 2`` of a two-sided interval."""
    return float(stats.norm.ppf(1.0 - alpha / 2.0))


# -- proportions ---------------------------------------------------------------------------------


def wilson(k: int, n: int, alpha: float) -> Interval:
    """Wilson score interval for ``k`` successes in ``n`` trials (statsmodels
    ``proportion_confint``, method ``wilson``)."""
    if n <= 0:
        return undefined("no_trials")
    lo, hi = proportion_confint(k, n, alpha=alpha, method="wilson")
    return Interval(float(lo), float(hi), "wilson")


def clopper_pearson(k: int, n: int, alpha: float) -> Interval:
    """Clopper-Pearson exact central interval (statsmodels ``proportion_confint``, method
    ``beta``); coverage at least ``1 - alpha`` for every proportion."""
    if n <= 0:
        return undefined("no_trials")
    lo, hi = proportion_confint(k, n, alpha=alpha, method="beta")
    return Interval(float(lo), float(hi), "clopper_pearson")


def cluster_robust_proportion(y: Sequence[int], clusters: Sequence[str], alpha: float) -> Interval:
    """Proportion with a cluster-robust (sandwich) standard error and a Wald interval on the
    proportion scale, clipped to [0, 1].

    The intercept-only binomial GLM is fitted with the canonical logit link and
    ``cov_type="cluster"`` (small-sample correction on); the delta method gives the standard
    error of the proportion, ``se(logit) * p * (1 - p)``. For an intercept-only model this equals
    the identity-link sandwich of the reference package exactly, without the domain warning the
    identity link raises. With one cluster, or no variation, the sandwich is undefined and the
    Wilson interval is returned, labelled as a fallback; so is a fit that fails.
    """
    n = len(y)
    if n == 0:
        return undefined("no_trials")
    k = sum(y)
    labels = {label: index for index, label in enumerate(sorted(set(clusters)))}
    if len(labels) < MIN_SPREAD or k in (0, n):
        return wilson(k, n, alpha).renamed("wilson_fallback(single_cluster_or_degenerate)")
    groups = np.array([labels[c] for c in clusters])
    p = k / n
    try:
        with strict():
            result = GLM(np.asarray(y, dtype=float), np.ones((n, 1)), family=Binomial()).fit(
                cov_type="cluster", cov_kwds={"groups": groups}
            )
            se_logit = float(np.sqrt(np.asarray(result.cov_params())[0, 0]))
    except FIT_FAILURES as exc:
        return wilson(k, n, alpha).renamed(f"wilson_fallback(glm_failed: {type(exc).__name__})")
    se = se_logit * p * (1.0 - p)
    z = z_value(alpha)
    return Interval(max(0.0, p - z * se), min(1.0, p + z * se), "cluster_robust_glm")


# -- rates ---------------------------------------------------------------------------------------


def garwood(k: int, exposure: float, alpha: float) -> Interval:
    """Exact (Garwood 1936) central interval for a Poisson rate ``k / exposure``: lower
    ``chi2.ppf(alpha/2, 2k) / 2``, upper ``chi2.ppf(1 - alpha/2, 2k + 2) / 2``, both divided by
    the exposure; the lower limit is 0 when ``k`` is 0."""
    if exposure <= 0:
        return undefined("no_exposure")
    lo = 0.0 if k == 0 else float(stats.chi2.ppf(alpha / 2.0, 2 * k)) / 2.0
    hi = float(stats.chi2.ppf(1.0 - alpha / 2.0, 2 * k + 2)) / 2.0
    return Interval(lo / exposure, hi / exposure, "poisson_exact")


def garwood_upper(k: int, exposure: float, level: float) -> float | None:
    """One-sided exact (Garwood 1936) upper bound of a Poisson rate ``k / exposure`` at
    confidence ``level``: ``chi2.ppf(level, 2k + 2) / 2`` divided by the exposure; ``None``
    without exposure."""
    if exposure <= 0:
        return None
    return float(stats.chi2.ppf(level, 2 * k + 2)) / 2.0 / exposure


def dispersion_index(counts: Sequence[float], exposures: Sequence[float]) -> float | None:
    """Pearson dispersion against a common-rate Poisson model: ``sum((k_i - mu_i)^2 / mu_i) /
    (n - 1)`` with ``mu_i = rate * exposure_i`` over clusters with exposure; 1 is Poisson.
    ``None`` with fewer than two such clusters or no events."""
    k = np.asarray(counts, dtype=float)
    e = np.asarray(exposures, dtype=float)
    keep = e > 0
    k, e = k[keep], e[keep]
    if len(k) < MIN_SPREAD or k.sum() <= 0:
        return None
    mu = k.sum() / e.sum() * e
    return float(((k - mu) ** 2 / mu).sum() / (len(k) - 1))


@dataclass(frozen=True, slots=True)
class RateFit:
    """The NB2 interval of a rate (or its labelled Poisson-exact fallback), the dispersion
    ``alpha_nb`` (``var = mu + alpha_nb * mu^2``) and whether an NB2 fit converged."""

    interval: Interval
    alpha_nb: float | None
    converged: bool


@dataclass(frozen=True, slots=True)
class _Attempt:
    params: Array
    llf: float
    se: float

    @property
    def se_ok(self) -> bool:
        return math.isfinite(self.se)


def _nb_attempt(counts: Array, offset: Array, start: Array | None, method: str) -> _Attempt | str:
    """One NB2 fit; a reason string when it fails, warns, does not converge or is not finite.

    One warning does not reject the fit: statsmodels' ``HessianInversionWarning`` ("no bse or
    cov_params available") after a converged optimisation, which is how the zero-dispersion
    boundary shows; the fit is kept with an undefined standard error. So is a fit whose standard
    error warns.
    """
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            model = NegativeBinomial(
                counts, np.ones((len(counts), 1)), loglike_method="nb2", offset=offset
            )
            options: dict[str, object] = {
                "disp": 0,
                "maxiter": 2000 if method == "nm" else 500,
                "method": method,
            }
            if start is not None:
                options["start_params"] = start
            result = model.fit(**options)
            params = np.asarray(result.params, dtype=float)
            llf = float(result.llf)
            converged = bool(result.mle_retvals.get("converged", False))
    except FIT_FAILURES as exc:
        return type(exc).__name__
    hessian = [w for w in caught if issubclass(w.category, HessianInversionWarning)]
    others = sorted({w.category.__name__ for w in caught} - {HessianInversionWarning.__name__})
    if others:
        return ", ".join(others)
    if not converged:
        return "not_converged"
    if not (np.all(np.isfinite(params)) and math.isfinite(llf)):
        return "not_finite"
    se = math.nan
    if not hessian:
        try:
            with strict():
                se = float(np.asarray(result.bse, dtype=float)[0])
        except FIT_FAILURES:
            se = math.nan
    return _Attempt(params, llf, se)


def _better(candidate: _Attempt, best: _Attempt | None) -> bool:
    """The best log-likelihood wins; among near-ties a finite standard error wins."""
    if best is None or candidate.llf > best.llf + _LLF_TIE:
        return True
    return abs(candidate.llf - best.llf) <= _LLF_TIE and candidate.se_ok and not best.se_ok


def nb2_rate(counts: Sequence[float], exposures: Sequence[float], alpha: float) -> RateFit:
    """NB2 Wald interval for the rate of events over clusters (counts and exposures per
    cluster; clusters without exposure are left out): intercept-only statsmodels
    ``NegativeBinomial`` with offset ``log(exposure)``, interval ``exp(b +/- z se(b))``, method
    ``nb2_wald``.

    The fit is multi-start (reference v0.1.1): a Poisson and method-of-moments start and the
    statsmodels default, each with ``bfgs``, ``nm`` and ``newton``; the converged, finite fit
    with the best log-likelihood is kept. An attempt that warns is rejected (see
    :func:`_nb_attempt` for the one warning that only removes the standard error). Fallbacks to the
    Poisson-exact interval are labelled ``poisson_exact_fallback(<why>)``: ``<2 clusters or zero
    count``; ``alpha_at_zero_boundary`` (converged dispersion below 1e-4); ``nb_se_undefined``;
    ``nb_fit_failed: <reasons>`` naming why every attempt failed.
    """
    k = np.asarray(counts, dtype=float)
    e = np.asarray(exposures, dtype=float)
    keep = e > 0
    k, e = k[keep], e[keep]
    exposure = float(e.sum())
    total = round(float(k.sum()))
    if exposure <= 0:
        return RateFit(undefined("no_exposure"), None, False)

    def fallback(why: str, alpha_nb: float | None, converged: bool) -> RateFit:
        interval = garwood(total, exposure, alpha).renamed(f"poisson_exact_fallback({why})")
        return RateFit(interval, alpha_nb, converged)

    if len(k) < MIN_SPREAD or total == 0:
        return fallback("<2 clusters or zero count", None, False)
    offset = np.log(e)
    mu = total / exposure * e
    moments = max(float(((k - mu) ** 2 - mu).sum()) / max(float((mu**2).sum()), 1e-12), 0.05)
    starts: tuple[Array | None, ...] = (np.array([math.log(total / exposure), moments]), None)
    best: _Attempt | None = None
    reasons: set[str] = set()
    for start in starts:
        for method in ("bfgs", "nm", "newton"):
            attempt = _nb_attempt(k, offset, start, method)
            if isinstance(attempt, str):
                reasons.add(attempt)
            elif _better(attempt, best):
                best = attempt
    if best is None:
        return fallback(f"nb_fit_failed: {', '.join(sorted(reasons))}", None, False)
    alpha_nb = float(best.params[-1])
    if alpha_nb < NB_ALPHA_BOUNDARY:
        return fallback("alpha_at_zero_boundary", 0.0, True)
    if not best.se_ok:
        return fallback("nb_se_undefined", alpha_nb, True)
    b, z = float(best.params[0]), z_value(alpha)
    interval = Interval(math.exp(b - z * best.se), math.exp(b + z * best.se), "nb2_wald")
    return RateFit(interval, alpha_nb, True)


# -- ratios and means over clusters --------------------------------------------------------------


def ratio_linearised_t(num: Sequence[float], den: Sequence[float], alpha: float) -> Interval:
    """Ratio of sums ``R = sum(num) / sum(den)`` over clusters with the Taylor-linearised
    variance ``sum((num_i - R den_i)^2) * G / (G - 1) / sum(den)^2`` and a t interval with
    ``G - 1`` degrees of freedom (method ``ratio_linearised_t``)."""
    n = np.asarray(num, dtype=float)
    d = np.asarray(den, dtype=float)
    clusters = len(n)
    total = float(d.sum())
    if total <= 0:
        return undefined("no_denominator")
    if clusters < MIN_SPREAD:
        return undefined("single_cluster")
    ratio = float(n.sum()) / total
    variance = float(((n - ratio * d) ** 2).sum()) * clusters / (clusters - 1) / total**2
    half = float(stats.t.ppf(1.0 - alpha / 2.0, clusters - 1)) * math.sqrt(variance)
    return Interval(ratio - half, ratio + half, "ratio_linearised_t")


def wilson_effective_n(share: float, n_eff: float, alpha: float) -> Interval:
    """Wilson interval for a time share treated as a proportion over ``n_eff`` independent
    trials, ``n_eff`` the number of control segments (single-cluster fallback of the reference
    package; anti-conservative when control persists across segments). The share is clipped to
    [0, 1]."""
    n = max(float(n_eff), 1.0)
    share = min(max(share, 0.0), 1.0)  # a ratio of float sums may pass 1 by round-off
    lo, hi = proportion_confint(share * n, n, alpha=alpha, method="wilson")
    return Interval(float(lo), float(hi), "wilson_effective_n")


def t_interval(values: Sequence[float], alpha: float) -> Interval:
    """Student t interval for a mean of independent values (method ``t``)."""
    v = np.asarray(values, dtype=float)
    if len(v) < MIN_SPREAD:
        return undefined("fewer_than_two_values")
    mean = float(v.mean())
    half = float(stats.t.ppf(1.0 - alpha / 2.0, len(v) - 1)) * float(v.std(ddof=1))
    half /= math.sqrt(len(v))
    return Interval(mean - half, mean + half, "t")


def _draws(clusters: int, n_boot: int, seed: int) -> npt.NDArray[np.int64]:
    """``n_boot`` resamples of ``clusters`` cluster indices, with replacement."""
    return np.random.default_rng(seed).integers(0, clusters, size=(n_boot, clusters))


def bootstrap_ratio_percentile(
    num: Sequence[float], den: Sequence[float], n_boot: int, alpha: float, seed: int
) -> Interval:
    """Percentile cluster bootstrap of ``sum(num) / sum(den)``: whole clusters are resampled
    with replacement (one row per cluster), so within-cluster dependence is kept. Resamples
    whose denominator is 0 are left out (method ``cluster_bootstrap_percentile``)."""
    n = np.asarray(num, dtype=float)
    d = np.asarray(den, dtype=float)
    if len(n) < MIN_SPREAD:
        return undefined("single_cluster")
    index = _draws(len(n), n_boot, seed)
    top, bottom = n[index].sum(axis=1), d[index].sum(axis=1)
    usable = bottom > 0
    ratios = top[usable] / bottom[usable]
    if len(ratios) < MIN_SPREAD:
        return undefined("too_few_replicates")
    lo, hi = np.quantile(ratios, [alpha / 2.0, 1.0 - alpha / 2.0])
    return Interval(float(lo), float(hi), "cluster_bootstrap_percentile")


def _cluster_se(sums: Array, counts: Array, means: Array) -> Array:
    """Cluster-robust standard error of a mean over units grouped in clusters (last axis)."""
    clusters = sums.shape[-1]
    residuals = sums - means[..., None] * counts
    spread = (residuals**2).sum(axis=-1) * clusters / (clusters - 1)
    result: Array = np.sqrt(spread) / counts.sum(axis=-1)
    return result


def bootstrap_mean_t(
    sums: Sequence[float], counts: Sequence[float], n_boot: int, alpha: float, seed: int
) -> Interval:
    """Cluster bootstrap-t interval for a mean over units (tasks) in clusters, from each
    cluster's sum and count: studentised by the closed-form cluster-robust standard error.

    Each drawn cluster is its own cluster in the resample (the ``<cluster>#<draw>`` relabelling
    of reference v0.1.1), so the studentiser sees G clusters, not the distinct ones; v0.1.0
    merged repeated draws and covered 0.83 instead of 0.95. Method ``cluster_bootstrap_t``.
    """
    s = np.asarray(sums, dtype=float)
    c = np.asarray(counts, dtype=float)
    if len(s) < MIN_SPREAD:
        return undefined("single_cluster")
    estimate = float(s.sum() / c.sum())
    se_hat = float(_cluster_se(s, c, np.array(estimate)))
    index = _draws(len(s), n_boot, seed)
    s_b, c_b = s[index], c[index]
    m_b = s_b.sum(axis=1) / c_b.sum(axis=1)
    se_b = _cluster_se(s_b, c_b, m_b)
    t = np.divide(m_b - estimate, se_b, out=np.zeros_like(m_b), where=se_b > 0)
    t_lo, t_hi = np.quantile(t, [alpha / 2.0, 1.0 - alpha / 2.0])
    return Interval(
        estimate - float(t_hi) * se_hat, estimate - float(t_lo) * se_hat, "cluster_bootstrap_t"
    )


def bootstrap_mean_percentile(
    sums: Sequence[float], counts: Sequence[float], n_boot: int, alpha: float, seed: int
) -> Interval:
    """Percentile cluster bootstrap of a mean over units in clusters, from each cluster's sum
    and count (method ``cluster_bootstrap_percentile``)."""
    return bootstrap_ratio_percentile(sums, counts, n_boot, alpha, seed)


def bootstrap_clusters[T](
    clusters: Sequence[T],
    statistic: Callable[[list[T]], float | None],
    n_boot: int,
    alpha: float,
    seed: int,
) -> tuple[Interval, int]:
    """Percentile cluster bootstrap of any statistic of a list of clusters. A replicate whose
    statistic is ``None`` is left out; returns the interval and the number left out."""
    if len(clusters) < MIN_SPREAD:
        return undefined("single_cluster"), 0
    values: list[float] = []
    for row in _draws(len(clusters), n_boot, seed):
        value = statistic([clusters[i] for i in row])
        if value is not None:
            values.append(value)
    failed = n_boot - len(values)
    if len(values) < MIN_SPREAD:
        return undefined("too_few_replicates"), failed
    lo, hi = np.quantile(np.array(values), [alpha / 2.0, 1.0 - alpha / 2.0])
    return Interval(float(lo), float(hi), "cluster_bootstrap_percentile"), failed


# -- survival ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SurvivalAt:
    """The Kaplan-Meier survival probability at a horizon and its pointwise interval."""

    horizon: float
    survival: float
    interval: Interval


@dataclass(frozen=True, slots=True)
class KaplanMeier:
    """Kaplan-Meier median (the smallest time with S(t) <= 0.5, as lifelines) with its interval,
    and the survival probability at horizons."""

    median: float | None
    median_interval: Interval
    at: tuple[SurvivalAt, ...]
    n_events: int


#: S(t) counts as at or below one half within this slack, so a product of survival factors
#: that is exactly 0.5 in exact arithmetic is not lost to rounding.
_HALF: Final = 0.5 + 1e-12


def _band(prob: Array, se: Array, alpha: float) -> tuple[Array, Array]:
    """Pointwise cloglog (exponential Greenwood) band of a Kaplan-Meier curve, as lifelines; a
    point with S of 0 or 1, or no standard error, gets [S, S]."""
    lower, upper = prob.copy(), prob.copy()
    usable = (prob > 0) & (prob < 1) & np.isfinite(se) & (se > 0)
    s = prob[usable]
    theta = z_value(alpha) * se[usable] / (s * np.abs(np.log(s)))
    lower[usable] = s ** np.exp(theta)
    upper[usable] = s ** np.exp(-theta)
    return lower, upper


def _first_at_or_below_half(times: Array, curve: Array) -> float | None:
    """The smallest time at which ``curve`` is at or below one half; ``None`` if it never is."""
    hits = np.flatnonzero(curve <= _HALF)
    return float(times[hits[0]]) if len(hits) else None


def _median_interval(times: Array, prob: Array, se: Array, alpha: float) -> Interval:
    """Interval for the median from the pointwise band, as lifelines ``median_survival_times``
    (the method the simulation study validated): the medians of the lower and upper band
    curves, with the median's own convention (smallest t with the curve at or below 0.5). The
    upper limit is ``None`` when the upper curve never reaches 0.5."""
    lower, upper = _band(prob, se, alpha)
    lo = _first_at_or_below_half(times, lower)
    if lo is None:
        return undefined("median_not_bracketed")
    return Interval(lo, _first_at_or_below_half(times, upper), "km_cloglog")


def _survival_at(times: Array, prob: Array, se: Array, horizon: float, alpha: float) -> SurvivalAt:
    """S(horizon) as a step function and its pointwise cloglog interval (exponential
    Greenwood, as lifelines); a point with S of 0 or 1, or no standard error, gets [S, S]."""
    index = int(np.searchsorted(times, horizon, side="right")) - 1
    if index < 0:
        return SurvivalAt(horizon, 1.0, Interval(1.0, 1.0, "km_cloglog"))
    lower, upper = _band(prob[index : index + 1], se[index : index + 1], alpha)
    survival = float(prob[index])
    return SurvivalAt(horizon, survival, Interval(float(lower[0]), float(upper[0]), "km_cloglog"))


def kaplan_meier(
    durations: Sequence[float], observed: Sequence[int], alpha: float, horizons: Sequence[float]
) -> KaplanMeier:
    """Kaplan-Meier analysis of right-censored durations (``observed`` 1 for an event, 0 for
    censoring) with statsmodels ``SurvfuncRight``: the median (smallest time with S(t) <= 0.5,
    the convention of lifelines and of the reference package), its interval, and S at each
    horizon. Durations in the caller's unit."""
    d = np.asarray(durations, dtype=float)
    o = np.asarray(observed, dtype=int)
    events = int(o.sum())
    if len(d) == 0 or events == 0:
        flat = tuple(SurvivalAt(h, 1.0, Interval(1.0, 1.0, "km_cloglog")) for h in horizons)
        return KaplanMeier(None, undefined("no_events"), flat, events)
    curve = SurvfuncRight(d, o)
    times = np.asarray(curve.surv_times, dtype=float)
    prob = np.asarray(curve.surv_prob, dtype=float)
    se = np.asarray(curve.surv_prob_se, dtype=float)
    median = _first_at_or_below_half(times, prob)
    at = tuple(_survival_at(times, prob, se, h, alpha) for h in horizons)
    return KaplanMeier(median, _median_interval(times, prob, se, alpha), at, events)


@dataclass(frozen=True, slots=True)
class KaplanMeierCurve:
    """A Kaplan-Meier curve as steps: S and its pointwise cloglog band just after each time,
    starting at time 0 with S = 1."""

    times: tuple[float, ...]
    survival: tuple[float, ...]
    lower: tuple[float, ...]
    upper: tuple[float, ...]


def kaplan_meier_curve(
    durations: Sequence[float], observed: Sequence[int], alpha: float
) -> KaplanMeierCurve | None:
    """The Kaplan-Meier curve of right-censored durations with the pointwise band of
    :func:`kaplan_meier`; ``None`` without an event. Durations in the caller's unit."""
    d = np.asarray(durations, dtype=float)
    o = np.asarray(observed, dtype=int)
    if len(d) == 0 or int(o.sum()) == 0:
        return None
    curve = SurvfuncRight(d, o)
    times = np.asarray(curve.surv_times, dtype=float)
    prob = np.asarray(curve.surv_prob, dtype=float)
    lower, upper = _band(prob, np.asarray(curve.surv_prob_se, dtype=float), alpha)

    def steps(values: Array) -> tuple[float, ...]:
        return (1.0, *(float(v) for v in values))

    return KaplanMeierCurve(
        (0.0, *(float(t) for t in times)), steps(prob), steps(lower), steps(upper)
    )


@dataclass(frozen=True, slots=True)
class ExponentialFit:
    """Exponential mean gap (total time over events) and rate with log-scale Wald intervals."""

    mean: float
    mean_interval: Interval
    rate: float
    rate_interval: Interval


def exponential_fit(
    durations: Sequence[float], observed: Sequence[int], alpha: float
) -> ExponentialFit | None:
    """Exponential MLE for right-censored durations: mean gap ``sum(durations) / events`` with
    the log-scale Wald interval ``mean * exp(-/+ z / sqrt(events))``, and the rate as its
    inverse. ``None`` without events or without positive total time."""
    events = int(sum(observed))
    total = float(sum(durations))
    if events == 0 or total <= 0:
        return None
    mean = total / events
    spread = z_value(alpha) / math.sqrt(events)
    lo, hi = mean * math.exp(-spread), mean * math.exp(spread)
    method = "wald_log_scale"
    return ExponentialFit(
        mean, Interval(lo, hi, method), 1.0 / mean, Interval(1.0 / hi, 1.0 / lo, method)
    )


def _positive(durations: Sequence[float], observed: Sequence[int]) -> tuple[Array, Mask]:
    """Durations above zero (a Weibull has no mass at zero) and their event indicators."""
    d = np.asarray(durations, dtype=float)
    o = np.asarray(observed, dtype=int)
    keep = d > 0
    return d[keep], o[keep] == 1


def weibull_fit(durations: Sequence[float], observed: Sequence[int]) -> tuple[float, float] | None:
    """Weibull shape and scale (location 0) for right-censored durations, by maximum likelihood
    with SciPy ``CensoredData`` and ``weibull_min.fit``. Durations of zero are left out.
    ``None`` with fewer than three events or when the fit warns or fails. A shape below 1 is a
    decreasing hazard (bursty interventions)."""
    d, events = _positive(durations, observed)
    if int(events.sum()) < WEIBULL_MIN_EVENTS:
        return None
    data = stats.CensoredData(uncensored=d[events], right=d[~events])
    try:
        with strict():
            shape, _, scale = stats.weibull_min.fit(data, floc=0)
    except FIT_FAILURES:
        return None
    if not (math.isfinite(shape) and math.isfinite(scale)):
        return None
    return float(shape), float(scale)


def weibull_shape(durations: Sequence[float], observed: Sequence[int]) -> float | None:
    """Weibull shape MLE by profile likelihood (the scale maximised in closed form,
    ``scale^c = sum(t^c) / events``): the root of ``events / c + sum(log t_events) - events *
    sum(t^c log t) / sum(t^c)``, which decreases in ``c``, found by Brent's method on log c.
    It equals :func:`weibull_fit`'s shape and takes about a millisecond, so the cluster bootstrap
    can refit it for every replicate. ``None`` with fewer than three events or no root."""
    d, events = _positive(durations, observed)
    count = int(events.sum())
    if count < WEIBULL_MIN_EVENTS:
        return None
    log_t = np.log(d)
    event_log_sum = float(log_t[events].sum())

    def score(log_shape: float) -> float:
        shape = math.exp(log_shape)
        exponent = shape * log_t
        weights = np.exp(exponent - exponent.max())
        return count / shape + event_log_sum - count * float(weights @ log_t) / float(weights.sum())

    low, high = _LOG_SHAPE_BRACKET
    if not score(low) > 0 > score(high):
        return None
    return math.exp(float(optimize.brentq(score, low, high, xtol=1e-12)))
