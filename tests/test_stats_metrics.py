"""Metrics (a) to (h) and rules R1 to R8 of autonometry.stats; each test name cites the
definitions.md section. Expected numbers are worked out by hand in each test."""

import dataclasses

import numpy as np
import pytest

from autonometry.stats import facts as facts_
from autonometry.stats import metrics as m
from autonometry.stats.report import clean, metrics_report, to_json
from autonometry.timeline import WholeSpan
from stats_helpers import FAST, approval, build, facts, hint, metrics
from timeline_helpers import NS, RULES, Log

HOUR = 3600.0


def _unknown_log() -> Log:
    """ai 0-10, human 10-20, ai 20-52, unknown 52-100 (silence beyond 30 + 2 s), ai 100-110."""
    log = Log().ai(0).human(10).ai(20)
    return log.add(100, "heartbeat").end(110)


# --- Section 7: bounds (R1 to R6) ---------------------------------------------------------------


def test_time_share_bounds__defs_7() -> None:
    share = metrics(facts(_unknown_log()))["time_autonomy_share"]
    assert share["estimate"] == pytest.approx(52 / 62)
    assert share["unknown_as_human"]["estimate"] == pytest.approx(52 / 110)
    assert share["unknown_as_ai"]["estimate"] == pytest.approx(100 / 110)


def test_interventions_rate_bounds_add_unknown_and_hidden_runs__defs_7() -> None:
    rate = metrics(facts(_unknown_log()))["interventions_per_active_hour"]
    assert rate["count"] == 1
    assert rate["estimate"] == pytest.approx(1 / (62 / HOUR))
    assert rate["unknown_as_human"]["count"] == 2
    assert rate["unknown_as_human"]["estimate"] == pytest.approx(2 / (110 / HOUR))
    assert rate["unknown_as_ai"]["estimate"] == pytest.approx(1 / (110 / HOUR))


def _spans(*states: str) -> list[WholeSpan]:
    return [
        WholeSpan(index * NS, (index + 1) * NS, state, None, None, False)
        for index, state in enumerate(states)
    ]


@pytest.mark.parametrize(
    ("states", "hidden"),
    [
        (("ai", "unknown", "ai"), 1),
        (("ai", "unknown", "safety_stop", "scripted"), 1),
        (("scripted", "safety_stop", "unknown", "ai", "unknown", "ai"), 2),
        (("ai", "safety_stop", "ai"), 0),
        (("ai", "unknown", "human_remote"), 0),
        (("unknown", "ai"), 0),
        (("ai", "unknown"), 0),
        (("idle", "unknown", "ai"), 0),
    ],
)
def test_hidden_intervention_runs__defs_7(states: tuple[str, ...], hidden: int) -> None:
    assert facts_.hidden_interventions(_spans(*states)) == hidden


def _task_log() -> Log:
    """t1 autonomous; t2 autonomous with unknown time 38-46; t3 with 2 s of human control; t4
    autonomous and never closed."""
    log = Log().ai(0).add(1, "task_start", task="t1").add(5, "task_end")
    log.add(6, "task_start", task="t2").add(46, "heartbeat").add(47, "task_end")
    log.add(48, "task_start", task="t3").human(50).ai(52).add(54, "task_end", outcome="failure")
    return log.add(55, "task_start", task="t4").end(60)


def test_task_rate_bounds__defs_7() -> None:
    result = metrics(facts(_task_log()))
    rate = result["task_autonomy_rate"]
    assert (rate["k"], rate["n"]) == (2, 3)
    assert (rate["unknown_as_human"]["k"], rate["unknown_as_human"]["n"]) == (1, 4)
    assert rate["unknown_as_ai"]["estimate"] == rate["estimate"]
    assert rate["n_tasks_unclosed"] == 1
    assert rate["n_tasks_with_unknown"] == 1
    success = result["autonomous_success_rate"]
    assert (success["k"], success["n"]) == (2, 3)
    assert success["unknown_as_human"]["estimate"] == pytest.approx(1 / 4)


def test_human_minutes_bounds_add_task_unknown_time__defs_7() -> None:
    minutes = metrics(facts(_task_log()))["human_minutes_per_task"]
    assert minutes["n"] == 4  # unclosed t4 counts
    assert minutes["estimate"] == pytest.approx(2 / 4 / 60)
    assert minutes["unknown_as_human"]["estimate"] == pytest.approx((2 + 8) / 4 / 60)
    assert minutes["unknown_as_ai"]["estimate"] == minutes["estimate"]
    assert minutes["share_tasks_zero_human"] == pytest.approx(3 / 4)


def test_outer_interval_spans_every_limit__defs_7() -> None:
    blocks = [{"ci": [2.0, 3.0]}, {"ci": [1.5, 2.5]}, {"ci": [2.2, 4.0]}]
    assert m.outer_interval(*blocks) == [1.5, 4.0]
    assert m.outer_interval({"ci": None}, {"ci": [1.0, 2.0]}) == [1.0, 2.0]
    assert m.outer_interval({"ci": None}) is None
    rate = metrics(facts(_unknown_log()))["interventions_per_active_hour"]
    limits = [v for key in ("ci",) for v in rate[key]]
    limits += rate["unknown_as_human"]["ci"] + rate["unknown_as_ai"]["ci"]
    assert rate["outer_interval"] == [min(limits), max(limits)]


def test_bounds_only_on_the_five_bounded_metrics__defs_7() -> None:
    result = metrics(facts(_task_log()))
    bounded = [
        "time_autonomy_share",
        "task_autonomy_rate",
        "autonomous_success_rate",
        "interventions_per_active_hour",
        "human_minutes_per_task",
    ]
    for name in bounded:
        assert {"unknown_as_human", "unknown_as_ai", "outer_interval"} <= set(result[name])
    for variant in result["time_autonomy_share"]["variants"].values():
        assert "unknown_as_human" not in variant
        assert {"estimate", "ci", "interval_method"} <= set(variant)
    for name in ("safety_stops_per_active_hour", "approvals_per_active_hour", "breakdowns"):
        assert "unknown_as_human" not in result[name]


# --- 6.6: whole-robot counts (R8) ---------------------------------------------------------------


def test_headline_rates_use_whole_robot_counts__defs_6_6() -> None:
    log = Log(["a", "b"]).ai(0, "a").ai(0, "b").human(10, scope="a").human(10.2, scope="b")
    log.ai(20, "a").ai(20, "b")
    log.tr(30, "safety_stop", "safety", scope="a").tr(30, "safety_stop", "safety", scope="b")
    log.ai(35, "a").ai(35, "b").end(40)
    result = metrics(facts(log))
    rate = result["interventions_per_active_hour"]
    assert (rate["count"], rate["per_scope"]["count"]) == (1, 2)
    stops = result["safety_stops_per_active_hour"]
    assert (stops["count"], stops["per_scope"]["count"]) == (1, 2)
    assert (stops["automatic"]["count"], stops["human_triggered"]["count"]) == (1, 0)


def test_safety_stops_by_trigger__defs_6_7() -> None:
    log = Log().ai(0).tr(10, "safety_stop", "safety", actor="op_a").ai(15)
    log.tr(20, "safety_stop", "safety").ai(25).end(40)
    stops = metrics(facts(log))["safety_stops_per_active_hour"]
    assert stops["count"] == 2
    assert stops["human_triggered"]["count"] == 1
    assert stops["automatic"]["count"] == 1
    assert stops["estimate"] == pytest.approx(2 / (40 / HOUR))


# --- 8: groups (R7) -----------------------------------------------------------------------------


def test_simulated_and_real_sessions_are_never_pooled__defs_8() -> None:
    real = build(Log().ai(0).human(5).ai(10).end(20), session="s01")
    simulated = build(Log(simulated=True).ai(0).end(20), session="s02")
    empty = build(Log().add(5, "heartbeat").end(10), session="s03")
    empty = dataclasses.replace(empty, start_ns=None, simulated=None)
    report = metrics_report([simulated, real, empty], RULES, FAST)
    assert sorted(report["groups"]) == ["real", "simulated"]
    assert report["groups"]["real"]["n_sessions"] == 1
    assert report["groups"]["real"]["interventions_per_active_hour"]["count"] == 1
    assert report["groups"]["simulated"]["interventions_per_active_hour"]["count"] == 0
    assert report["sessions_without_session_start"] == [{"robot_id": "r01", "session_id": "s03"}]


# --- (a) time autonomy share --------------------------------------------------------------------


def test_time_share_subtracts_moved_dwell__defs_6_1() -> None:
    log = approval(Log().ai(0), 20, dwell=4).end(40)
    share = metrics(facts(log))["time_autonomy_share"]
    assert share["estimate"] == pytest.approx(36 / 40)
    assert share["interval_method"] == "wilson_effective_n(single_cluster)"


def test_with_approvals_uses_median_logged_dwell_per_type__defs_6_1() -> None:
    log = approval(approval(Log().ai(0), 10, dwell=2), 20, dwell=6)
    log = hint(log, 30).end(60)
    variant = metrics(facts(log))["time_autonomy_share"]["variants"]["with_approvals"]
    assert variant["approval_min_dwell_s"] == 4.0
    assert variant["hint_min_dwell_s"] == RULES.params.approval_dwell_sensitivity_s
    # Approval dwells max(2, 4) and max(6, 4), hint max(none, 5): 4 + 6 + 5 s moved from AI.
    assert variant["estimate"] == pytest.approx((60 - 15) / 60)


def test_shared_as_ai_and_shares_of_active_time__defs_2_and_6_6() -> None:
    log = Log().ai(0).tr(10, "shared", "human_initiated", "op_a", "vr_remote").ai(20)
    log.tr(30, "scripted", device="replay").ai(35).tr(40, "safety_stop", "safety").ai(45)
    share = metrics(facts(log.end(50)))["time_autonomy_share"]
    assert share["estimate"] == pytest.approx(30 / 50)
    assert share["variants"]["shared_as_ai"]["estimate"] == pytest.approx(40 / 50)
    assert share["shared_share"]["estimate"] == pytest.approx(10 / 50)
    assert share["scripted_share"]["estimate"] == pytest.approx(5 / 50)
    assert share["safety_stop_share"]["estimate"] == pytest.approx(5 / 50)
    assert share["human_remote_share"]["estimate"] == 0.0


def test_scope_weighted_variant__defs_6_6() -> None:
    log = Log(["a", "b"]).ai(0, "a").ai(0, "b").human(10, scope="b").ai(30, "b").end(40)
    share = metrics(facts(log))["time_autonomy_share"]
    assert share["estimate"] == pytest.approx(20 / 40)
    assert share["variants"]["scope_weighted"]["estimate"] == pytest.approx(30 / 40)


def test_unsupported_variant_and_supported_time__defs_6_15() -> None:
    log = Log().ai(0).add(0, "support_start").add(10, "support_end").human(20).ai(25)
    log = approval(log, 5, dwell=2)  # inside support: moves nothing outside it
    share = metrics(facts(log.end(40)))["time_autonomy_share"]
    assert share["variants"]["unsupported"]["estimate"] == pytest.approx(25 / 30)
    assert share["supported_time_s"] == 10.0
    assert share["supported_share_of_active"]["estimate"] == pytest.approx(10 / 40)


def test_supported_share_counts_supported_active_time_only__defs_6_15() -> None:
    log = Log().add(0, "support_start").ai(10).add(20, "support_end").end(40)
    share = metrics(facts(log))["time_autonomy_share"]
    assert share["supported_time_s"] == 20.0
    assert share["supported_active_time_s"] == 10.0
    assert share["supported_share_of_active"]["estimate"] == pytest.approx(10 / 30)


def test_idle_and_reset_time_are_printed__defs_4_and_6_3() -> None:
    log = Log().human(10, "human_local", "planned", plan_item_id="reset_scene").ai(20).end(40)
    share = metrics(facts(log))["time_autonomy_share"]
    assert share["idle_time_s"] == 10.0
    assert share["idle_share_of_observed"]["estimate"] == pytest.approx(10 / 40)
    assert share["reset_human_time_s"] == 10.0
    assert share["reset_human_share_of_active"]["estimate"] == pytest.approx(10 / 30)


def test_cluster_choice_for_the_time_share_and_the_rest() -> None:
    sessions = [facts(Log().ai(0).human(5).ai(8).end(20), robot=r) for r in ("r1", "r2", "r3")]
    auto = metrics(*sessions)
    assert auto["time_autonomy_share"]["cluster"] == "robot"
    assert auto["time_autonomy_share"]["interval_method"] == "ratio_linearised_t(robot)"
    assert auto["interventions_per_active_hour"]["cluster"] == "session"
    robot = metrics(*sessions, options=dataclasses.replace(FAST, cluster="robot"))
    assert robot["interventions_per_active_hour"]["cluster"] == "robot"
    session = metrics(*sessions, options=dataclasses.replace(FAST, cluster="session"))
    assert session["time_autonomy_share"]["cluster"] == "session"
    two = metrics(*sessions[:2])
    assert two["time_autonomy_share"]["cluster"] == "session"


# --- (b) task rates -----------------------------------------------------------------------------


def test_task_rate_default_switches_to_the_cluster_glm_at_20_sessions__defs_5() -> None:
    def session(index: int) -> facts_.SessionFacts:
        log = Log().ai(0).add(1, "task_start", task="a").add(2, "task_end")
        log.add(3, "task_start", task="b")
        if index % 3 == 0:
            log.human(4).ai(5)
        log.add(6, "task_end")
        return facts(log.end(10), session=f"s{index:02d}")

    many = metrics(*[session(i) for i in range(20)])["task_autonomy_rate"]
    assert many["interval_method"] == "cluster_robust_glm(session)"
    assert many["ci"] == many["ci_cluster_robust"]
    few = metrics(*[session(i) for i in range(19)])["task_autonomy_rate"]
    assert few["interval_method"] == "wilson"
    assert few["ci_clopper_pearson"] is not None


def test_tasks_with_unknown_definition_are_left_out__defs_5() -> None:
    log = Log().ai(0).add(1, "task_start", task="t1", task_definition_id="other").add(3, "task_end")
    rate = metrics(facts(log.end(5)))["task_autonomy_rate"]
    assert rate["estimate"] is None
    assert rate["n_tasks_unknown_definition"] == 1


# --- (c), (f) rates and breakdowns --------------------------------------------------------------


def test_kind_rates_and_breakdowns__defs_3_and_6_1() -> None:
    log = Log().ai(0).add(1, "task_start", task="t1").human(5).ai(10)
    log.human(15, reason="planned", plan_item_id="reset_scene").ai(20)
    log.add(22, "help_request", scope="whole_robot")
    log.tr(24, "shared", "ai_requested_help", "op_a", "vr_remote").ai(26)
    log.tr(28, "safety_stop", "safety", actor="op_b").ai(30)
    log = approval(log, 32, dwell=1).add(34, "task_end").end(36)
    result = metrics(facts(log))
    hours = 36 / HOUR
    assert result["approvals_per_active_hour"]["count"] == 1
    assert result["approvals_per_active_hour"]["estimate"] == pytest.approx(1 / hours)
    assert result["takeovers_per_active_hour"]["count"] == 2
    breakdowns = result["breakdowns"]
    assert {k: v["count"] for k, v in breakdowns["by_kind"].items()} == {
        "approval": 1,
        "safety_stop": 1,
        "shared": 1,
        "takeover": 2,
    }
    assert {k: v["count"] for k, v in breakdowns["by_reason"].items()} == {
        "ai_requested_help": 1,
        "human_initiated": 1,
        "planned": 2,
        "safety": 1,
    }
    # Narrow: hand-overs with reason human_initiated or safety (not the planned takeover, the
    # ai_requested_help shared span or the approval).
    assert breakdowns["narrow_human_initiated_or_safety"]["count"] == 2
    assert breakdowns["by_kind"]["takeover"]["interval_method"] == "poisson_exact"
    definition = breakdowns["by_task_definition"]["pick_v1"]
    assert definition["n_tasks"] == 1
    assert definition["task_autonomy_rate"]["estimate"] == 0.0
    assert definition["interventions_per_task"]["estimate"] == 5.0


# --- (d) gaps -----------------------------------------------------------------------------------


def _gap_log() -> Log:
    log = Log(heartbeat_s=3600).ai(0).add(300, "heartbeat", distance_m=10.0)
    log.human(600, distance_m=25.0).ai(660).human(1800).ai(1860)
    return log.add(3000, "session_end", distance_m=100.0)


def test_gaps_in_active_hours__defs_3() -> None:
    session = facts(_gap_log())
    rows, _ = m.gap_table([session], "session", "active_hours")
    assert [(round(g.duration * HOUR, 6), g.observed) for g in rows] == [
        (600, 1),
        (1200, 1),
        (1200, 0),
    ]
    gaps = metrics(session)["gaps_between_interventions"]["active_hours"]
    assert gaps["n_events"] == 2
    assert gaps["unit"] == "active_hours"
    assert gaps["estimate"] == pytest.approx(1200 / HOUR)  # S drops below 0.5 at the 2nd gap
    assert gaps["exponential"]["mean_gap"] == pytest.approx(3000 / HOUR / 2)


def test_gaps_in_metres_use_the_latest_sample_at_or_before__defs_8() -> None:
    rows, skipped = m.gap_table([facts(_gap_log())], "session", "metres")
    assert skipped == 0
    assert [(g.duration, g.observed) for g in rows] == [(25.0, 1), (0.0, 1), (75.0, 0)]
    assert facts_.distance_at(((10, 5.0),), 9) == 0.0


def test_gaps_without_distance_samples_have_no_estimate__defs_8() -> None:
    gaps = metrics(facts(Log().ai(0).human(5).ai(6).end(10)))["gaps_between_interventions"]
    assert gaps["metres"]["estimate"] is None
    assert gaps["metres"]["note"] == "no distance samples"
    assert gaps["metres"]["n_sessions_without_distance"] == 1


def test_gaps_weibull_shape_with_enough_events() -> None:
    log = Log(heartbeat_s=3600).ai(0)
    for start in (100, 250, 300, 900, 1000, 1700, 2600):
        log.human(start).ai(start + 10)
    sessions = [facts(log.end(3000), session=s) for s in ("s1", "s2")]
    weibull = metrics(*sessions)["gaps_between_interventions"]["active_hours"]["weibull"]
    assert weibull["estimate"] > 0
    assert weibull["interval_method"] == "cluster_bootstrap_percentile(session)"


# --- (e) human minutes per task -----------------------------------------------------------------


def test_human_minutes_add_unplanned_reset_and_moved_dwell__defs_6_3() -> None:
    log = Log().ai(0).human(1).ai(5).add(6, "task_start", task="t1")
    log = approval(log, 10, dwell=3).add(20, "task_end").end(25)
    minutes = metrics(facts(log))["human_minutes_per_task"]
    assert minutes["estimate"] == pytest.approx((4 + 3) / 60)


def test_human_minutes_interval_methods__defs_6_3() -> None:
    def log(human: float) -> Log:
        return Log().ai(0).add(1, "task_start").human(2).ai(2 + human).add(9, "task_end").end(10)

    two = metrics(facts(log(1), session="a"), facts(log(3), session="b"))["human_minutes_per_task"]
    assert two["interval_method"] == "cluster_bootstrap_t(session)"
    assert two["ci_bootstrap_percentile"] is not None
    assert two["median"] == pytest.approx(2 / 60)
    one = Log().ai(0).add(1, "task_start", task="a").human(2).ai(3).add(4, "task_end")
    one.add(5, "task_start", task="b").add(6, "task_end")
    single = metrics(facts(one.end(7)))["human_minutes_per_task"]
    assert single["interval_method"] == "t_on_tasks(single_cluster)"


# --- (g) drift ----------------------------------------------------------------------------------


def _day(date: str, takeovers: int) -> facts_.SessionFacts:
    log = Log(heartbeat_s=3600).ai(0)
    for index in range(takeovers):
        log.human(100 + 200 * index).ai(150 + 200 * index)
    return facts(log.end(1800), session=date, date=date)


def test_drift_bins_and_trend__defs_7() -> None:
    days = [_day("2026-11-10", 1), _day("2026-11-11", 2), _day("2026-11-13", 4)]
    drift = metrics(*days)["drift"]
    assert [b["bin"] for b in drift["bins"]] == ["2026-11-10", "2026-11-11", "2026-11-13"]
    assert [b["count"] for b in drift["bins"]] == [1, 2, 4]
    assert drift["bins"][0]["active_hours"] == pytest.approx(0.5)
    trend = drift["trend"]
    assert trend["family"] == "poisson"
    assert trend["rate_ratio_per_bin"] > 1
    assert drift["cusum"]["first_signal"] is None
    week = metrics(*days, options=dataclasses.replace(FAST, drift_bin="week"))["drift"]
    assert [b["bin"] for b in week["bins"]] == ["2026-W46"]
    assert week["trend"] == {"note": "fewer than 3 bins with active time"}


def test_drift_splits_active_time_at_midnight__defs_7() -> None:
    base = _day("2026-11-10", 0)
    start = 1_794_355_200 * NS - 1800 * NS  # 2026-11-15T23:30:00Z
    crossing = dataclasses.replace(base, active=((start, start + HOUR * NS),), marks_ns=())
    hours, counts = m.drift_bins([crossing], 1)
    assert sorted(hours.values()) == [pytest.approx(0.5), pytest.approx(0.5)]
    assert counts == {}
    assert m._bin_label(m._bin_start(start, 7), 7) == "2026-W46"


def test_drift_without_interventions_has_no_trend__defs_7() -> None:
    days = [_day(d, 0) for d in ("2026-11-10", "2026-11-11", "2026-11-12")]
    drift = metrics(*days)["drift"]
    assert drift["trend"] == {"note": "no interventions"}
    assert drift["cusum"] is None


def test_drift_trend_fit_that_warns_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def fit(self) -> None:
            raise RuntimeWarning("bad")

    monkeypatch.setattr(m, "GLM", Failing)
    days = [_day("2026-11-10", 1), _day("2026-11-11", 2), _day("2026-11-12", 4)]
    assert metrics(*days)["drift"]["trend"]["note"] == "trend fit failed (RuntimeWarning)"


def test_cusum_signals_a_shift__defs_7() -> None:
    k = np.array([1.0, 1.0, 1.0, 1.0, 12.0, 12.0, 12.0, 12.0])
    cusum = m._cusum(k, np.ones(8), [f"b{i}" for i in range(8)], None)
    # z = (1 - 6.5) / sqrt(6.5) = -2.16 per early bin: the lower side passes 4 at the third bin.
    assert cusum["first_signal"] == {"bin": "b2", "direction": "down"}
    assert cusum["overdispersion_phi"] == 1.0
    reversed_ = m._cusum(k[::-1], np.ones(8), [f"b{i}" for i in range(8)], 0.5)
    assert reversed_["first_signal"] == {"bin": "b2", "direction": "up"}
    assert m._cusum(k, np.ones(8), [f"b{i}" for i in range(8)], 3.0)["overdispersion_phi"] == 3.0


def test_drift_uses_the_negative_binomial_when_overdispersed__defs_7() -> None:
    counts = [0, 9, 0, 12, 1, 0, 15, 0]
    days = [_day(f"2026-11-{10 + i:02d}", c) for i, c in enumerate(counts)]
    drift = metrics(*days)["drift"]
    assert drift["trend"]["family"] == "negative_binomial"
    assert drift["trend"]["nb_alpha"] > 0
    assert len(drift["cusum"]["upper"]) == len(counts)


# --- (h) unmeasured -----------------------------------------------------------------------------


def test_unmeasured_conformance_flags_and_parameters__defs_7_and_9() -> None:
    complete = metrics(facts(Log().ai(0).end(10)))["unmeasured"]
    assert complete["conformance"] == "complete"
    assert complete["unknown_share"] == 0.0
    incomplete = metrics(facts(_unknown_log()))["unmeasured"]
    assert incomplete["unknown_share"] == pytest.approx(48 / 110)
    assert incomplete["conformance"] == "incomplete"
    assert incomplete["observed_time_s"] == 110.0
    params = set(complete["parameters"])
    table_9 = {
        "idle_threshold_s", "gap_tolerance_s", "heartbeat_interval_s", "help_latency_max_s",
        "approval_min_dwell_s", "approval_wait_max_s", "hint_ttl_s", "hint_min_dwell_s",
        "takeover_window_s", "simultaneity_window_s", "goal_release_s", "contention_window_s",
        "unknown_share_max", "checkpoint_every_events", "checkpoint_every_s",
        "max_commit_delay_s", "clock_tolerance_s",
    }  # fmt: skip
    assert table_9 <= params


def test_unmeasured_supervision_and_humans__defs_6_4_and_6_5() -> None:
    first = Log().ai(0).add(0, "supervision", level="remote_monitoring", n_supervisors=2)
    first.human(10, actor="op_a").ai(20).end(40)
    second = Log().ai(0).human(5, actor="op_a").ai(10).human(12, actor="op_b").ai(14).end(20)
    result = metrics(facts(first, session="s1"), facts(second, session="s2"))["unmeasured"]
    assert result["supervision_ratio"] == pytest.approx(40 / 60)
    assert result["supervisor_hours_per_robot_hour"] == pytest.approx(80 / 60)
    assert result["distinct_human_actors"] == 2
    assert result["human_actor_seconds"] == 17.0


def test_unobserved_signal_classes_are_printed_beside_metrics__defs_8() -> None:
    result = metrics(facts(Log(unobserved=["safety_stops"]).ai(0).end(10)))
    assert result["safety_stops_per_active_hour"]["unobserved"] == ["safety_stops"]
    assert result["approvals_per_active_hour"]["unobserved"] == []
    assert result["unmeasured"]["unobserved_sessions"] == {"safety_stops": 1}


# --- edge cases and determinism -----------------------------------------------------------------


@pytest.mark.parametrize(
    "log",
    [
        Log().ai(0).end(10),  # zero interventions, zero tasks, no distance
        Log().end(10),  # no active time
        Log().ai(0).add(1, "task_start").add(3, "task_end").end(5),  # one task
        Log().ai(0).human(2).ai(3).end(4),  # one intervention
    ],
)
def test_every_metric_survives_degenerate_input(log: Log) -> None:
    result = metrics(facts(log))
    text = to_json(result)
    assert "NaN" not in text
    assert result["n_sessions"] == 1


def test_no_active_time_gives_null_estimates_with_notes() -> None:
    result = metrics(facts(Log().end(10)))
    assert result["time_autonomy_share"]["estimate"] is None
    assert result["time_autonomy_share"]["note"]
    assert result["interventions_per_active_hour"]["estimate"] is None
    assert result["human_minutes_per_task"]["note"] == "no tasks"
    assert result["gaps_between_interventions"]["active_hours"]["note"] == "no active time"
    assert result["breakdowns"]["narrow_human_initiated_or_safety"]["estimate"] is None


def test_zero_interventions_give_a_zero_rate() -> None:
    result = metrics(
        facts(Log(heartbeat_s=3600).ai(0).end(3500)),
        facts(Log(heartbeat_s=3600).ai(0).end(1800), session="s2"),
    )
    rate = result["interventions_per_active_hour"]
    assert rate["estimate"] == 0.0
    assert rate["interval_method"] == "poisson_exact_fallback(<2 clusters or zero count)"
    assert rate["ci"][0] == 0.0
    gaps = result["gaps_between_interventions"]["active_hours"]
    assert gaps["estimate"] is None
    assert gaps["note"] == "no events"
    assert gaps["exponential"] is None
    assert gaps["weibull"]["estimate"] is None


def test_report_is_deterministic_and_independent_of_order() -> None:
    timelines = [
        build(_task_log(), session="a"),
        build(_unknown_log(), session="b"),
        build(_gap_log(), robot="r02", session="c"),
    ]
    first = to_json(metrics_report(timelines, RULES, FAST))
    again = to_json(metrics_report(list(reversed(timelines)), RULES, FAST))
    assert first == again


def test_clean_turns_numpy_and_non_finite_values_into_json() -> None:
    assert clean({"a": (np.float64(1.5), np.int64(2), np.bool_(True), float("inf"))}) == {
        "a": [1.5, 2, True, None]
    }
