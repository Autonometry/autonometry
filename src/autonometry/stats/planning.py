"""Test-length planning: how many active hours or tasks are needed to claim a level at a given
confidence, including the zero-event case (reference package ``autonomy_meter`` v0.1.1,
``planning``, ported unchanged).

``alpha`` is the one-sided error (claim "rate < lambda_max" with confidence ``1 - alpha``); for a
two-sided ``1 - alpha`` interval whose upper limit must fall below the threshold, use ``alpha/2``.
Rates are per hour and exposures in hours unless stated.

References: Hanley J.A., Lippman-Hand A. (1983) "If nothing goes wrong, is everything all right?
Interpreting zero numerators." JAMA 249(13):1743-1745 (the rule of three is the ``alpha = 0.05``
case of ``-ln(alpha) / lambda_max``); Garwood F. (1936) Biometrika 28:437-442.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from scipy import stats
from statsmodels.stats.proportion import proportion_confint

from autonometry.stats.intervals import garwood

#: Exposure and task counts grow by 5 % per step in the demonstration searches (reference).
_GROWTH = 1.05
#: Below this many tasks the task search steps by one (reference).
_SMALL_N = 20


def hours_for_zero_events(lambda_max: float, alpha: float = 0.05) -> float:
    """Exposure T such that zero events demonstrate rate < ``lambda_max`` at one-sided
    confidence ``1 - alpha`` under a Poisson model: ``exp(-lambda_max T) <= alpha``, so
    ``T >= -ln(alpha) / lambda_max`` (2.996 / lambda_max at alpha 0.05, the rule of three).
    Assumes one constant rate; if the rate is a single frailty draw for the system under test,
    use :func:`hours_for_zero_events_nb`.

    Raises:
        ValueError: ``lambda_max <= 0`` or ``alpha`` outside (0, 1).
    """
    if lambda_max <= 0 or not 0 < alpha < 1:
        raise ValueError("lambda_max > 0 and 0 < alpha < 1 required")
    return -math.log(alpha) / lambda_max


def rule_of_three(exposure: float) -> float:
    """Approximate upper 95 % limit on a rate after zero events in ``exposure``: ``3 /
    exposure`` (exactly ``-ln(0.05) / exposure = 2.9957 / exposure``)."""
    return 3.0 / exposure


def tasks_for_zero_failures(p_max: float, alpha: float = 0.05) -> int:
    """Independent tasks with zero failures needed to claim failure probability < ``p_max`` at
    one-sided confidence ``1 - alpha``: ``(1 - p_max)^n <= alpha``, so ``n >= ln(alpha) /
    ln(1 - p_max)`` (the Clopper-Pearson upper limit at k = 0 is ``1 - alpha^(1/n)``).

    Raises:
        ValueError: ``p_max`` or ``alpha`` outside (0, 1).
    """
    if not 0 < p_max < 1 or not 0 < alpha < 1:
        raise ValueError("0 < p_max < 1 and 0 < alpha < 1 required")
    return math.ceil(math.log(alpha) / math.log(1.0 - p_max))


def hours_for_zero_events_nb(lambda_max: float, alpha_nb: float, alpha: float = 0.05) -> float:
    """Zero-event exposure under a gamma-Poisson (NB2) model with dispersion ``alpha_nb``:
    ``P(0) = (1 + alpha_nb lambda_max T)^(-1/alpha_nb) <= alpha``, so ``T >= (alpha^(-alpha_nb) -
    1) / (alpha_nb lambda_max)``; the Poisson formula when ``alpha_nb <= 0``.

    Exact when one gamma frailty is shared by the whole test (one robot or deployment);
    conservative when the test is made of independent sessions with their own frailty draws.
    """
    if alpha_nb <= 0:
        return hours_for_zero_events(lambda_max, alpha)
    return float((alpha ** (-alpha_nb) - 1.0) / (alpha_nb * lambda_max))


def tasks_for_zero_failures_clustered(
    p_max: float, cluster_size: float, icc: float, alpha: float = 0.05
) -> int:
    """Zero-failure task count inflated by the Kish design effect ``1 + (m - 1) icc`` for tasks
    in clusters (sessions) of mean size ``m`` with intra-cluster correlation ``icc``."""
    design_effect = 1.0 + (cluster_size - 1.0) * icc
    return math.ceil(tasks_for_zero_failures(p_max, alpha) * design_effect)


@dataclass(frozen=True, slots=True)
class Demonstration:
    """The smallest exposure (hours) found, the expected events at it and the upper limit there;
    ``hours`` is infinite and the rest ``None`` when the search found none."""

    hours: float
    expected_events: int | None
    upper_limit_at_t: float | None


def hours_to_demonstrate_rate(
    lambda_max: float,
    expected_rate: float,
    alpha: float = 0.05,
    alpha_nb: float = 0.0,
    max_hours: float = 1e7,
) -> Demonstration:
    """Smallest exposure T such that, if the true rate is ``expected_rate`` and the count equals
    its expectation ``k = round(expected_rate T)``, the one-sided ``1 - alpha`` Garwood upper
    limit is below ``lambda_max``. With ``alpha_nb > 0`` the limit is ``(k / T) exp(z sqrt(1/k +
    alpha_nb))`` as an NB approximation. A planning heuristic (expected-count substitution), not
    a power calculation; see :func:`power_two_periods`.

    Raises:
        ValueError: ``expected_rate >= lambda_max``.
    """
    if expected_rate >= lambda_max:
        raise ValueError("expected_rate must be below lambda_max")
    if expected_rate <= 0:
        return Demonstration(hours_for_zero_events_nb(lambda_max, alpha_nb, alpha), 0, lambda_max)
    hours = hours_for_zero_events(lambda_max, alpha)
    while hours < max_hours:
        k = round(expected_rate * hours)
        upper = garwood(k, hours, 2 * alpha).hi
        if alpha_nb > 0 and k > 0:
            z = float(stats.norm.ppf(1 - alpha))
            upper = (k / hours) * math.exp(z * math.sqrt(1.0 / k + alpha_nb))
        if upper is not None and upper < lambda_max:
            return Demonstration(hours, k, upper)
        hours *= _GROWTH
    return Demonstration(math.inf, None, None)


def tasks_to_demonstrate_rate(
    p_max: float, expected_p: float, alpha: float = 0.05, max_n: int = 10**7
) -> int:
    """Smallest n such that, with ``k = round(expected_p n)`` failures, the one-sided
    ``1 - alpha`` Clopper-Pearson upper limit is below ``p_max``; ``max_n`` if none is.

    Raises:
        ValueError: ``expected_p >= p_max``.
    """
    if expected_p >= p_max:
        raise ValueError("expected_p must be below p_max")
    n = tasks_for_zero_failures(p_max, alpha)
    while n < max_n:
        k = round(expected_p * n)
        _, upper = proportion_confint(k, n, alpha=2 * alpha, method="beta")
        if upper < p_max:
            return n
        n = math.ceil(n * _GROWTH) if n > _SMALL_N else n + 1
    return max_n


def power_two_periods(  # noqa: PLR0913, PLR0917 -- the reference signature
    rate1: float,
    rate2: float,
    hours1: float,
    hours2: float,
    alpha: float = 0.05,
    alpha_nb: float = 0.0,
) -> float:
    """Power of a two-sided Wald test of equal rates in two periods (log rate ratio), with
    ``var(log RR) = (1/mu1 + alpha_nb) + (1/mu2 + alpha_nb)``, ``mu_i = rate_i hours_i``."""
    mu1, mu2 = rate1 * hours1, rate2 * hours2
    if mu1 <= 0 or mu2 <= 0:
        return 0.0
    se = math.sqrt(1 / mu1 + alpha_nb + 1 / mu2 + alpha_nb)
    delta = abs(math.log(rate2 / rate1))
    z = float(stats.norm.ppf(1 - alpha / 2))
    return float(stats.norm.cdf(delta / se - z) + stats.norm.cdf(-delta / se - z))


def hours_for_power_two_periods(  # noqa: PLR0913, PLR0917 -- the reference signature
    rate1: float,
    rate2: float,
    power: float = 0.8,
    alpha: float = 0.05,
    alpha_nb: float = 0.0,
    ratio: float = 1.0,
) -> float:
    """Hours in period 1 (period 2 has ``ratio`` times as many) for :func:`power_two_periods`
    to reach ``power``: ``(1/rate1 + 1/(ratio rate2)) / ((delta / z)^2 - 2 alpha_nb)``. Infinite
    when the rates are equal or the between-period dispersion alone exceeds the detectable
    variance (no exposure is enough)."""
    delta = abs(math.log(rate2 / rate1))
    if delta <= 0:
        return math.inf
    z = float(stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power))
    budget = (delta / z) ** 2 - 2.0 * alpha_nb
    if budget <= 0:
        return math.inf
    return float((1 / rate1 + 1 / (ratio * rate2)) / budget)


@dataclass(frozen=True, slots=True)
class PlanningRow:
    """Hours of zero-intervention operation for one target rate (per hour)."""

    lambda_max_per_hour: float
    hours_poisson: float
    hours_rule_of_three: float
    hours_nb: float


def planning_table(
    lambda_targets: Sequence[float] = (0.1, 0.05, 0.02, 0.01),
    alpha: float = 0.05,
    alpha_nb: float = 0.5,
) -> list[PlanningRow]:
    """Hours of zero-intervention operation per target rate under Poisson, the rule of three
    and NB(``alpha_nb``)."""
    return [
        PlanningRow(
            target,
            hours_for_zero_events(target, alpha),
            rule_of_three(1.0) / target,
            hours_for_zero_events_nb(target, alpha_nb, alpha),
        )
        for target in lambda_targets
    ]
