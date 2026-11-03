"""Interval estimators used by the metrics.

Every function states the estimator and the interval method in its docstring.
Package functions are used wherever one exists:

* binomial proportions  -> ``statsmodels.stats.proportion.proportion_confint``
  (``wilson`` = Wilson score, ``beta`` = Clopper-Pearson)
* Poisson rate          -> exact (Garwood 1936) limits from ``scipy.stats.chi2``
* over-dispersed rate   -> ``statsmodels.discrete.discrete_model.NegativeBinomial``
                           (NB2, intercept + log-exposure offset)
* cluster-robust GLM    -> ``statsmodels.GLM(...).fit(cov_type='cluster')``
* cluster bootstrap     -> numpy resampling of whole clusters (percentile or
                           bootstrap-t)

References (located by web search 2026-10-30; tags per the project source rules):
  Garwood F. (1936) Biometrika 28:437-442, https://doi.org/10.1093/biomet/28.3-4.437
      [CLAIM: publisher abstract page only (paywalled); DOI copied from
      https://academic.oup.com/biomet/article-abstract/28/3-4/437/220104]
  Hanley JA, Lippman-Hand A. (1983) JAMA 249(13):1743-1745
      [VERIFIED (excerpt): author-hosted PDF https://jhanley.biostat.mcgill.ca/c607/ch08/zero_numerator.pdf;
       DOI 10.1001/jama.1983.03330370053031 as listed in a citing article's reference list, CLAIM]
  statsmodels proportion_confint docs [VERIFIED: page read]:
      https://www.statsmodels.org/stable/generated/statsmodels.stats.proportion.proportion_confint.html
  Garwood chi-square/gamma form checked against scipy numerically in tests/test_intervals.py.
"""
from __future__ import annotations

import warnings
from typing import Callable, Sequence

import numpy as np
import pandas as pd
from scipy import stats
from statsmodels.stats.proportion import proportion_confint


# --------------------------------------------------------------------------- #
# Binomial
# --------------------------------------------------------------------------- #
def wilson_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (statsmodels, method='wilson').

    Returns (0, 0) if n == 0 is impossible; raises ValueError for n == 0.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    lo, hi = proportion_confint(k, n, alpha=alpha, method="wilson")
    return float(lo), float(hi)


def clopper_pearson_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Clopper-Pearson exact (central) interval (statsmodels, method='beta').

    Coverage >= 1-alpha for every p; conservative. At k == 0 or k == n the
    interval is one-sided in effect (coverage 1-alpha/2, see statsmodels notes).
    """
    if n <= 0:
        raise ValueError("n must be positive")
    lo, hi = proportion_confint(k, n, alpha=alpha, method="beta")
    return float(lo), float(hi)


def proportion_cluster_robust_ci(y: Sequence[int], clusters: Sequence, alpha: float = 0.05) -> dict:
    """Proportion with cluster-robust (sandwich) standard error.

    Estimator: intercept-only binomial GLM with identity link (so the
    coefficient *is* the proportion), fitted by ``statsmodels.GLM`` with
    ``cov_type='cluster'`` (groups = clusters, small-sample correction on).
    Interval: Wald on the identity scale, clipped to [0, 1]. Falls back to
    logit link + delta method if identity link fails to converge.

    Returns dict(p, se, lo, hi, n, n_clusters, method).
    """
    import statsmodels.api as sm

    y = np.asarray(y, dtype=float)
    groups = pd.factorize(pd.Series(list(clusters)))[0]
    n, g = len(y), int(groups.max() + 1) if len(groups) else 0
    p = float(y.mean()) if n else float("nan")
    if n == 0:
        return {"p": p, "se": np.nan, "lo": np.nan, "hi": np.nan, "n": 0, "n_clusters": 0, "method": "cluster_glm"}
    if g < 2 or p in (0.0, 1.0):
        # sandwich undefined (single cluster) or degenerate (no variation): fall back to Wilson
        lo, hi = wilson_ci(int(y.sum()), n, alpha)
        return {"p": p, "se": np.nan, "lo": lo, "hi": hi, "n": n, "n_clusters": g,
                "method": "wilson_fallback(single_cluster_or_degenerate)"}
    X = np.ones((n, 1))
    z = stats.norm.ppf(1 - alpha / 2)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fam = sm.families.Binomial(link=sm.families.links.Identity())
            res = sm.GLM(y, X, family=fam).fit(cov_type="cluster", cov_kwds={"groups": groups})
        se = float(np.sqrt(res.cov_params().iloc[0, 0] if hasattr(res.cov_params(), "iloc") else res.cov_params()[0, 0]))
        est = float(res.params[0])
        lo, hi = max(0.0, est - z * se), min(1.0, est + z * se)
        method = "cluster_glm_identity"
    except Exception:  # pragma: no cover - numerical fallback
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = sm.GLM(y, X, family=sm.families.Binomial()).fit(cov_type="cluster", cov_kwds={"groups": groups})
        b = float(res.params[0]); se_b = float(np.sqrt(np.asarray(res.cov_params())[0, 0]))
        lo, hi = 1 / (1 + np.exp(-(b - z * se_b))), 1 / (1 + np.exp(-(b + z * se_b)))
        se = se_b * p * (1 - p)
        method = "cluster_glm_logit"
    return {"p": p, "se": se, "lo": float(lo), "hi": float(hi), "n": n, "n_clusters": g, "method": method}


# --------------------------------------------------------------------------- #
# Poisson / negative binomial
# --------------------------------------------------------------------------- #
def poisson_exact_ci(k: int, exposure: float, alpha: float = 0.05) -> tuple[float, float]:
    """Exact (Garwood 1936) central interval for a Poisson rate.

    Estimator: k / exposure. Interval: lower = chi2.ppf(alpha/2, 2k)/2,
    upper = chi2.ppf(1-alpha/2, 2k+2)/2, divided by exposure; lower = 0 when k = 0.
    Equivalent to gamma quantiles Gamma(k, 1) and Gamma(k+1, 1).
    """
    if exposure <= 0:
        raise ValueError("exposure must be positive")
    k = int(k)
    lo = 0.0 if k == 0 else stats.chi2.ppf(alpha / 2, 2 * k) / 2.0
    hi = stats.chi2.ppf(1 - alpha / 2, 2 * k + 2) / 2.0
    return float(lo / exposure), float(hi / exposure)


def nb_rate_ci(counts: Sequence[int], exposures: Sequence[float], alpha: float = 0.05) -> dict:
    """Over-dispersed event rate via negative binomial regression (default interval for (c)).

    Estimator: intercept-only NB2 model (statsmodels ``NegativeBinomial``,
    ``loglike_method='nb2'``) with offset log(exposure); rate = exp(intercept),
    dispersion alpha_nb estimated by ML (var = mu + alpha_nb * mu^2).
    Interval: Wald on the log scale, exponentiated; method label 'nb2_wald'.

    Fitting (v0.1.1): multi-start. Starting values are the Poisson GLM intercept
    and a method-of-moments alpha; optimisers 'bfgs', 'nm' and 'newton' are tried
    from that start and from the statsmodels default, and the converged fit with
    finite llf and a finite intercept standard error and the best llf is kept. The default
    Newton start diverges (NaN/inf parameters) on strongly over-dispersed groups
    such as per-vehicle disengagement counts, which v0.1.0 mislabelled as a
    zero-boundary case.
    Fallbacks (all labelled): 'poisson_exact_fallback(<2 clusters or zero count)'
    when fewer than 2 clusters or total count 0; 'poisson_exact_fallback(alpha_at_zero_boundary)'
    only when a CONVERGED fit with finite parameters has alpha_hat < 1e-4 (the optimisers
    stop at ~1e-5..1e-8 on the boundary, so a bare 1e-8 test does not fire reliably);
    'poisson_exact_fallback(nb_se_undefined)' when alpha_hat > 1e-4 but the intercept SE is
    not finite; 'poisson_exact_fallback(nb_fit_failed)'
    when no start converged (this should be rare and is worth inspecting).

    Returns dict(rate, lo, hi, alpha_nb, n_clusters, method, converged).
    """
    from statsmodels.discrete.discrete_model import NegativeBinomial

    counts = np.asarray(counts, dtype=float)
    exposures = np.asarray(exposures, dtype=float)
    mask = exposures > 0
    counts, exposures = counts[mask], exposures[mask]
    K, E = float(counts.sum()), float(exposures.sum())
    out = {"rate": K / E if E > 0 else np.nan, "alpha_nb": np.nan, "n_clusters": int(len(counts)), "converged": False}
    if len(counts) < 2 or K == 0:
        lo, hi = poisson_exact_ci(int(K), E, alpha) if E > 0 else (np.nan, np.nan)
        out.update(lo=lo, hi=hi, method="poisson_exact_fallback(<2 clusters or zero count)")
        return out
    X = np.ones((len(counts), 1))
    off = np.log(exposures)
    # method-of-moments start: alpha = (s^2 - mean) / mean^2 on exposure-scaled counts
    mu = K / E * exposures
    mom = max(((counts - mu) ** 2 - mu).sum() / max((mu ** 2).sum(), 1e-12), 0.05)
    starts = [np.array([np.log(K / E), mom]), None]
    best = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mod = NegativeBinomial(counts, X, loglike_method="nb2", offset=off)
        for sp in starts:
            for meth in ("bfgs", "nm", "newton"):
                try:
                    kw = {"disp": 0, "maxiter": 2000 if meth == "nm" else 500, "method": meth}
                    if sp is not None:
                        kw["start_params"] = sp
                    res = mod.fit(**kw)
                    params = np.asarray(res.params, float)
                    conv = bool(getattr(res, "mle_retvals", {}).get("converged", True))
                    if not (np.all(np.isfinite(params)) and np.isfinite(res.llf) and conv):
                        continue
                    se_ok = bool(np.isfinite(np.asarray(res.bse, float)[0]))
                    # prefer the best llf; among near-ties prefer a fit with a finite intercept SE
                    if best is None or res.llf > best[0].llf + 1e-6 or (abs(res.llf - best[0].llf) <= 1e-6 and se_ok and not best[1]):
                        best = (res, se_ok)
                except Exception:
                    continue
    if best is None:
        lo, hi = poisson_exact_ci(int(K), E, alpha)
        out.update(lo=lo, hi=hi, method="poisson_exact_fallback(nb_fit_failed)")
        return out
    best, se_ok = best
    a_hat = float(best.params[-1])
    out["converged"] = True
    # boundary rule: a CONVERGED fit with finite parameters and alpha_hat < 1e-4 is a genuine
    # zero-boundary MLE (the optimisers stop at ~1e-5..1e-8 there and the Hessian is singular, so
    # SEs are undefined). Divergent fits (NaN/inf params) never reach this branch any more.
    if a_hat < 1e-4:
        lo, hi = poisson_exact_ci(int(K), E, alpha)
        out.update(lo=lo, hi=hi, alpha_nb=0.0, method="poisson_exact_fallback(alpha_at_zero_boundary)")
        return out
    if not se_ok:
        lo, hi = poisson_exact_ci(int(K), E, alpha)
        out.update(lo=lo, hi=hi, alpha_nb=a_hat, method="poisson_exact_fallback(nb_se_undefined)")
        return out
    b = float(best.params[0]); se = float(best.bse[0])
    z = stats.norm.ppf(1 - alpha / 2)
    out.update(rate=float(np.exp(b)), lo=float(np.exp(b - z * se)), hi=float(np.exp(b + z * se)),
               alpha_nb=a_hat, method="nb2_wald")
    return out


def dispersion_index(counts: Sequence[int], exposures: Sequence[float]) -> float:
    """Pearson dispersion of counts relative to a common-rate Poisson model:
    sum((k_i - mu_i)^2 / mu_i) / (n - 1) with mu_i = rate * exposure_i. 1 = Poisson."""
    counts = np.asarray(counts, float); exposures = np.asarray(exposures, float)
    if len(counts) < 2 or counts.sum() == 0:
        return float("nan")
    mu = counts.sum() / exposures.sum() * exposures
    return float(((counts - mu) ** 2 / mu).sum() / (len(counts) - 1))


# --------------------------------------------------------------------------- #
# Cluster bootstrap
# --------------------------------------------------------------------------- #
def cluster_bootstrap(
    df: pd.DataFrame,
    stat: Callable[[pd.DataFrame], float],
    cluster_col: str,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int | None = 0,
    method: str = "percentile",
    se_fn: Callable[[pd.DataFrame], float] | None = None,
    n_inner: int = 50,
) -> dict:
    """Non-parametric cluster bootstrap of a statistic of a DataFrame.

    Clusters (e.g. sessions) are resampled with replacement, keeping all rows of
    each drawn cluster, so within-cluster dependence is preserved.

    method='percentile': interval = (alpha/2, 1-alpha/2) quantiles of the
        bootstrap distribution.
    method='bootstrap_t': studentised interval; the standard error inside each
        replicate is ``se_fn`` if supplied, else an inner cluster bootstrap of
        ``n_inner`` replicates (slow; default n_inner=50). Resampled clusters are
        relabelled "<cluster>#<draw>" so ``se_fn`` must group by ``cluster_col``
        (it then sees exactly G clusters per replicate).

    Returns dict(estimate, lo, hi, se, n_clusters, n_boot, method). With a
    single cluster the interval is NaN and method is 'undefined(single_cluster)'.
    """
    rng = np.random.default_rng(seed)
    clusters = df[cluster_col].unique()
    G = len(clusters)
    est = float(stat(df))
    if G < 2:
        return {"estimate": est, "lo": np.nan, "hi": np.nan, "se": np.nan,
                "n_clusters": G, "n_boot": 0, "method": "undefined(single_cluster)"}
    groups = {c: g for c, g in df.groupby(cluster_col, sort=False)}

    def resample(r):
        # v0.1.1: each drawn cluster gets a unique label "<cluster>#<draw index>" so that a
        # cluster drawn k times is k clusters in the resample (standard construction); the
        # studentiser se_fn groups by cluster_col and therefore sees G clusters, not G_unique.
        draw = r.choice(clusters, size=G, replace=True)
        parts = []
        for i, c in enumerate(draw):
            g = groups[c].copy()
            g[cluster_col] = f"{c}#{i}"
            parts.append(g)
        return pd.concat(parts, ignore_index=True)

    boots = np.empty(n_boot)
    tstats = np.empty(n_boot) if method == "bootstrap_t" else None
    for b in range(n_boot):
        d = resample(rng)
        boots[b] = stat(d)
        if method == "bootstrap_t":
            if se_fn is not None:
                se_b = se_fn(d)
            else:
                inner = np.array([stat(resample(rng)) for _ in range(n_inner)])
                se_b = inner.std(ddof=1)
            tstats[b] = (boots[b] - est) / se_b if se_b > 0 else 0.0
    boots = boots[np.isfinite(boots)]
    se = float(boots.std(ddof=1)) if len(boots) > 1 else np.nan
    if method == "percentile":
        lo, hi = np.quantile(boots, [alpha / 2, 1 - alpha / 2])
    elif method == "bootstrap_t":
        if se_fn is not None:
            se_hat = se_fn(df)
        else:
            se_hat = se
        t_lo, t_hi = np.quantile(tstats[np.isfinite(tstats)], [alpha / 2, 1 - alpha / 2])
        lo, hi = est - t_hi * se_hat, est - t_lo * se_hat
    else:
        raise ValueError("method must be 'percentile' or 'bootstrap_t'")
    return {"estimate": est, "lo": float(lo), "hi": float(hi), "se": se,
            "n_clusters": G, "n_boot": int(n_boot), "method": f"cluster_bootstrap_{method}"}


def ratio_linearised_ci(num: Sequence[float], den: Sequence[float], alpha: float = 0.05) -> dict:
    """Ratio-of-sums estimator R = sum(num)/sum(den) over clusters with the
    Taylor-linearised (design-based) variance
    var(R) = sum((num_i - R den_i)^2) * G/(G-1) / (sum den_i)^2 and a t_{G-1} interval.
    Used as the closed-form companion to the cluster bootstrap for time shares.
    """
    num = np.asarray(num, float); den = np.asarray(den, float)
    G = len(num)
    R = num.sum() / den.sum()
    if G < 2:
        return {"estimate": float(R), "lo": np.nan, "hi": np.nan, "se": np.nan, "n_clusters": G, "method": "undefined(single_cluster)"}
    resid = num - R * den
    var = (resid ** 2).sum() * G / (G - 1) / den.sum() ** 2
    se = float(np.sqrt(var))
    t = stats.t.ppf(1 - alpha / 2, G - 1)
    return {"estimate": float(R), "lo": float(R - t * se), "hi": float(R + t * se), "se": se,
            "n_clusters": G, "method": "ratio_linearised_t"}


def wilson_effective_ci(share: float, n_eff: float, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson-type fallback for a time share with an *effective* number of trials.

    The share (e.g. ai_time/active_time) is treated as a proportion observed on
    ``n_eff`` independent Bernoulli trials, where n_eff is the number of control
    segments (each contiguous run of one state is one 'trial'). This is the
    fallback when only one session exists and no cluster resampling is possible.
    It is anti-conservative if control persists across segments; it is reported
    with method='wilson_effective_n' so readers can see the assumption.
    """
    n_eff = max(float(n_eff), 1.0)
    lo, hi = proportion_confint(share * n_eff, n_eff, alpha=alpha, method="wilson")
    return float(lo), float(hi)
