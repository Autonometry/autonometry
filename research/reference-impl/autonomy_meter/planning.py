"""Test-length planning: how many active hours or tasks are needed to claim a
level at a given confidence, including the zero-event case.

Conventions: ``alpha`` is the one-sided error (claim "rate < lambda_max" with
confidence 1 - alpha). For a two-sided (1 - alpha) interval whose upper limit
must fall below the threshold use alpha/2.

References (located by web search 2026-10-30):
  Hanley JA, Lippman-Hand A. (1983) "If nothing goes wrong, is everything all
  right? Interpreting zero numerators." JAMA 249(13):1743-1745. Author-hosted copy
  https://jhanley.biostat.mcgill.ca/c607/ch08/zero_numerator.pdf [VERIFIED: excerpt
  read, states the rule of three and the ln(0.01) 'rule of 4.6' variant]; DOI
  10.1001/jama.1983.03330370053031 as given in a citing reference list [CLAIM].
  The rule of three (upper 95% limit ~ 3/n after zero events) is the alpha = 0.05
  special case of n >= -ln(alpha)/lambda_max since -ln(0.05) = 2.996 (derivation
  below, checked in tests).
  Garwood F. (1936) Biometrika 28:437-442, https://doi.org/10.1093/biomet/28.3-4.437
  [CLAIM: publisher abstract page only].
"""
from __future__ import annotations

import math

import numpy as np
from scipy import stats

from .intervals import poisson_exact_ci


# --------------------------------------------------------------------------- #
# Zero-event planning
# --------------------------------------------------------------------------- #
def hours_for_zero_events(lambda_max: float, alpha: float = 0.05) -> float:
    """Exposure T (same unit as 1/lambda_max) such that observing zero events
    demonstrates rate < lambda_max at one-sided confidence 1 - alpha under a
    Poisson model: P(0 events | lambda_max, T) = exp(-lambda_max T) <= alpha
    => T >= -ln(alpha) / lambda_max. For alpha = 0.05 this is 2.996/lambda_max,
    the 'rule of three'. Assumes a constant rate over the test; if the rate is a
    single frailty draw for the system under test, use hours_for_zero_events_nb
    (the Poisson rule is anti-conservative there)."""
    if lambda_max <= 0 or not (0 < alpha < 1):
        raise ValueError("lambda_max > 0 and 0 < alpha < 1 required")
    return -math.log(alpha) / lambda_max


def rule_of_three(exposure: float) -> float:
    """Approximate upper 95% limit on a rate after zero events in ``exposure``: 3/exposure.
    Exact value is -ln(0.05)/exposure = 2.9957/exposure (Hanley & Lippman-Hand 1983)."""
    return 3.0 / exposure


def tasks_for_zero_failures(p_max: float, alpha: float = 0.05) -> int:
    """Number of independent tasks n with zero failures needed to claim failure
    probability < p_max at one-sided confidence 1 - alpha: (1 - p_max)^n <= alpha
    => n >= ln(alpha)/ln(1 - p_max) (exact binomial; Clopper-Pearson upper limit
    at k = 0 equals 1 - alpha^(1/n))."""
    if not (0 < p_max < 1) or not (0 < alpha < 1):
        raise ValueError("0 < p_max < 1 and 0 < alpha < 1 required")
    return int(math.ceil(math.log(alpha) / math.log(1.0 - p_max)))


def hours_for_zero_events_nb(lambda_max: float, alpha_nb: float, alpha: float = 0.05) -> float:
    """Zero-event exposure under a gamma-Poisson (NB2) model with dispersion
    alpha_nb (var = mu + alpha_nb mu^2): P(0) = (1 + alpha_nb * lambda_max * T)^(-1/alpha_nb)
    <= alpha  =>  T >= (alpha^(-alpha_nb) - 1) / (alpha_nb * lambda_max). Reduces to
    the Poisson formula as alpha_nb -> 0 and is always larger than it.

    Scope of validity (v0.1.1, from the simulation track): this formula is EXACT
    when one gamma frailty is shared by the whole test (one robot or one deployment
    whose rate is a single draw around lambda_max), and CONSERVATIVE when the test is
    made of independent sessions each with its own frailty draw (then the frailties
    average out and the Poisson exposure is closer to right). Under a shared frailty
    with alpha_nb between 0.3 and 1.0 the plain rule of three was wrong 12-25 % of the
    time at the threshold in simulation, so the shared-frailty case is the one to
    plan for when a claim concerns a single system."""
    if alpha_nb <= 0:
        return hours_for_zero_events(lambda_max, alpha)
    return (alpha ** (-alpha_nb) - 1.0) / (alpha_nb * lambda_max)


def tasks_for_zero_failures_clustered(p_max: float, cluster_size: float, icc: float, alpha: float = 0.05) -> int:
    """Binomial zero-failure sample size inflated by the design effect
    1 + (m - 1) * icc for tasks in clusters (sessions) of mean size m with
    intra-cluster correlation icc (Kish design effect). Returns total tasks."""
    n_eff = tasks_for_zero_failures(p_max, alpha)
    deff = 1.0 + (cluster_size - 1.0) * icc
    return int(math.ceil(n_eff * deff))


# --------------------------------------------------------------------------- #
# Non-zero expected events
# --------------------------------------------------------------------------- #
def hours_to_demonstrate_rate(lambda_max: float, expected_rate: float, alpha: float = 0.05,
                              alpha_nb: float = 0.0, max_hours: float = 1e7) -> dict:
    """Smallest exposure T such that, if the true rate is ``expected_rate``
    (< lambda_max) and the observed count equals its expectation
    k = round(expected_rate * T), the one-sided (1 - alpha) Poisson exact upper
    limit (Garwood) falls below lambda_max. With alpha_nb > 0 the upper limit is
    inflated by sqrt(1 + alpha_nb * k) on the log scale as an NB approximation.
    Returns dict(hours, expected_events, upper_limit_at_T). This is a planning
    heuristic (expected-count substitution), not a power calculation; see
    :func:`power_two_periods` for a comparison of two periods."""
    if expected_rate >= lambda_max:
        raise ValueError("expected_rate must be below lambda_max")
    if expected_rate <= 0:
        T = hours_for_zero_events_nb(lambda_max, alpha_nb, alpha)
        return {"hours": T, "expected_events": 0, "upper_limit_at_T": lambda_max}
    T = hours_for_zero_events(lambda_max, alpha)
    while T < max_hours:
        k = int(round(expected_rate * T))
        _, hi = poisson_exact_ci(k, T, 2 * alpha)  # one-sided upper at level 1-alpha
        if alpha_nb > 0 and k > 0:
            z = stats.norm.ppf(1 - alpha)
            hi = (k / T) * math.exp(z * math.sqrt(1.0 / k + alpha_nb)) if k > 0 else hi
        if hi < lambda_max:
            return {"hours": T, "expected_events": k, "upper_limit_at_T": hi}
        T *= 1.05
    return {"hours": float("inf"), "expected_events": None, "upper_limit_at_T": None}


def tasks_to_demonstrate_rate(p_max: float, expected_p: float, alpha: float = 0.05, max_n: int = 10 ** 7) -> int:
    """Smallest n such that, with k = round(expected_p * n) failures, the one-sided
    (1 - alpha) Clopper-Pearson upper limit is below p_max."""
    from statsmodels.stats.proportion import proportion_confint
    if expected_p >= p_max:
        raise ValueError("expected_p must be below p_max")
    n = tasks_for_zero_failures(p_max, alpha)
    while n < max_n:
        k = int(round(expected_p * n))
        _, hi = proportion_confint(k, n, alpha=2 * alpha, method="beta")
        if hi < p_max:
            return n
        n = int(math.ceil(n * 1.05)) if n > 20 else n + 1
    return max_n


# --------------------------------------------------------------------------- #
# Two-period comparison
# --------------------------------------------------------------------------- #
def power_two_periods(rate1: float, rate2: float, hours1: float, hours2: float,
                      alpha: float = 0.05, alpha_nb: float = 0.0) -> float:
    """Power of a two-sided Wald test of equal rates between two periods
    (log rate ratio), with per-period count variance inflated by NB2 dispersion:
    var(log RR) = (1/mu1 + alpha_nb) + (1/mu2 + alpha_nb), mu_i = rate_i * hours_i."""
    mu1, mu2 = rate1 * hours1, rate2 * hours2
    if mu1 <= 0 or mu2 <= 0:
        return 0.0
    se = math.sqrt(1 / mu1 + alpha_nb + 1 / mu2 + alpha_nb)
    delta = abs(math.log(rate2 / rate1))
    z = stats.norm.ppf(1 - alpha / 2)
    return float(stats.norm.cdf(delta / se - z) + stats.norm.cdf(-delta / se - z))


def hours_for_power_two_periods(rate1: float, rate2: float, power: float = 0.8, alpha: float = 0.05,
                                alpha_nb: float = 0.0, ratio: float = 1.0) -> float:
    """Hours in period 1 (period 2 = ratio * hours1) so that power_two_periods >= power.
    Closed form from the Wald test: hours1 = (z_{1-a/2} + z_power)^2 / delta^2 *
    (1/rate1 + 1/(ratio*rate2)) adjusted for alpha_nb by iteration."""
    delta = abs(math.log(rate2 / rate1))
    if delta == 0:
        return float("inf")
    z = stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power)
    # power condition: var(log RR) <= (delta/z)^2 with
    # var = (1/rate1 + 1/(ratio*rate2))/hours1 + 2*alpha_nb
    budget = (delta / z) ** 2 - 2.0 * alpha_nb
    if budget <= 0:
        return float("inf")  # between-period dispersion alone exceeds the detectable variance
    return float((1 / rate1 + 1 / (ratio * rate2)) / budget)


def planning_table(lambda_targets=(0.1, 0.05, 0.02, 0.01), alpha: float = 0.05, alpha_nb: float = 0.5) -> list[dict]:
    """Convenience table: hours of zero-intervention operation needed per target
    rate under Poisson and NB(alpha_nb) assumptions."""
    return [{"lambda_max_per_hour": lm, "hours_poisson": hours_for_zero_events(lm, alpha),
             "hours_rule_of_three": rule_of_three(1.0) / lm,
             "hours_nb": hours_for_zero_events_nb(lm, alpha_nb, alpha)} for lm in lambda_targets]
