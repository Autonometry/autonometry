"""Test-length planning (autonometry.stats.planning). Expected values: closed forms in the test,
or the reference package ``autonomy_meter`` v0.1.1 ``planning`` run once on the same input."""

import math

import pytest

from autonometry.stats import planning as pl


def test_zero_event_hours_are_the_rule_of_three() -> None:
    assert pl.hours_for_zero_events(0.1) == pytest.approx(-math.log(0.05) / 0.1)
    assert pl.hours_for_zero_events(0.1) == pytest.approx(29.957, abs=1e-3)
    assert pl.rule_of_three(10.0) == 0.3


@pytest.mark.parametrize(("rate", "alpha"), [(0.0, 0.05), (0.1, 0.0), (0.1, 1.0)])
def test_zero_event_hours_reject_bad_input(rate: float, alpha: float) -> None:
    with pytest.raises(ValueError, match="required"):
        pl.hours_for_zero_events(rate, alpha)


def test_zero_event_hours_under_nb_dispersion() -> None:
    # Reference README: 69.4 h for rate < 0.1/h under NB2 dispersion 0.5.
    assert pl.hours_for_zero_events_nb(0.1, 0.5) == pytest.approx((0.05**-0.5 - 1) / 0.05)
    assert pl.hours_for_zero_events_nb(0.1, 0.5) == pytest.approx(69.44271909999159)
    assert pl.hours_for_zero_events_nb(0.1, 0.0) == pl.hours_for_zero_events(0.1)


def test_tasks_for_zero_failures() -> None:
    assert pl.tasks_for_zero_failures(0.05) == 59
    assert (1 - 0.05) ** 59 <= 0.05 < (1 - 0.05) ** 58
    # Reference run once: tasks_for_zero_failures_clustered(0.05, 5, 0.1) gave 83.
    assert pl.tasks_for_zero_failures_clustered(0.05, 5, 0.1) == 83
    with pytest.raises(ValueError, match="required"):
        pl.tasks_for_zero_failures(1.0)


def test_hours_to_demonstrate_rate_matches_the_reference() -> None:
    # Reference run once: {'hours': 106.5184342546938, 'expected_events': 5,
    # 'upper_limit_at_T': 0.09869685920846392}; with alpha_nb 0.3: hours inf.
    found = pl.hours_to_demonstrate_rate(0.1, 0.05)
    assert found.hours == pytest.approx(106.5184342546938)
    assert found.expected_events == 5
    assert found.upper_limit_at_t == pytest.approx(0.09869685920846392)
    assert pl.hours_to_demonstrate_rate(0.1, 0.05, alpha_nb=0.3) == pl.Demonstration(
        math.inf, None, None
    )
    zero = pl.hours_to_demonstrate_rate(0.1, 0.0)
    assert zero.hours == pl.hours_for_zero_events(0.1)
    with pytest.raises(ValueError, match="below"):
        pl.hours_to_demonstrate_rate(0.1, 0.2)


def test_tasks_to_demonstrate_rate_matches_the_reference() -> None:
    # Reference run once: tasks_to_demonstrate_rate(0.1, 0.03) gave 47.
    assert pl.tasks_to_demonstrate_rate(0.1, 0.03) == 47
    assert pl.tasks_to_demonstrate_rate(0.1, 0.099, max_n=100) == 100
    with pytest.raises(ValueError, match="below"):
        pl.tasks_to_demonstrate_rate(0.1, 0.1)


def test_two_period_power_matches_the_reference() -> None:
    # Reference README: a rate halving with alpha_nb 0.3 has power 0.14 at 100 h per period and
    # cannot reach 80 % at any exposure. Reference run once for the other values.
    assert pl.power_two_periods(2.0, 1.0, 100, 100, alpha_nb=0.3) == pytest.approx(
        0.1431711100184138
    )
    assert pl.power_two_periods(2.0, 1.0, 20, 20) == pytest.approx(0.716021231816099)
    assert pl.power_two_periods(2.0, 1.0, 0, 20) == 0.0
    assert pl.hours_for_power_two_periods(2.0, 1.0) == pytest.approx(24.504622222076595)
    assert pl.hours_for_power_two_periods(2.0, 1.0, alpha_nb=0.02) == pytest.approx(
        70.71155214040353
    )
    assert pl.hours_for_power_two_periods(2.0, 1.0, alpha_nb=0.3) == math.inf
    assert pl.hours_for_power_two_periods(1.0, 1.0) == math.inf


def test_planning_table() -> None:
    row = pl.planning_table((0.1,))[0]
    assert row.hours_poisson == pytest.approx(29.957, abs=1e-3)
    assert row.hours_rule_of_three == pytest.approx(30.0)
    assert row.hours_nb == pytest.approx(69.44271909999159)
