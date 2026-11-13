"""The report's sections (definitions.md 11), built by pure functions."""

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from autonometry.events import (
    FileProblem,
    Flag,
    LineError,
    LogCheck,
    SealIssue,
    SessionVerdict,
    VerifyResult,
)
from autonometry.reader import read_logs
from autonometry.report import census, figures, html, invariants, statements, trust
from autonometry.report.build import InputFile, ReportInputs, build_report, verdict
from autonometry.spec import ReportRules, load_rules
from autonometry.stats.metrics import DEPENDS_ON, Options
from autonometry.stats.report import metrics_report
from autonometry.timeline import Registries, Timeline, build_timeline
from autonometry.verify import verify_log
from report_helpers import GENERATED_AT, LOGS, METRICS_LOGS, REGISTRIES

RULES = load_rules("0.3")
FAST = Options(n_boot=50)


def _golden(
    task_registry: bool = True, folder: Path = METRICS_LOGS
) -> tuple[LogCheck, list[Timeline]]:
    log_check = read_logs([folder / name for name in LOGS], "0.3")
    docs = {name: json.loads((folder / name).read_text("utf-8")) for name in REGISTRIES}
    registries = Registries.from_documents(
        docs["devices.json"], docs["tasks.json"] if task_registry else None, docs["plan.json"]
    )
    timelines = [
        build_timeline(
            s,
            registries,
            RULES,
            [
                f
                for f in log_check.flags
                if (f.robot_id, f.session_id) == (s.robot_id, s.session_id)
            ],
        )
        for s in log_check.sessions
    ]
    return log_check, timelines


def _verdict(
    session: str, status: str, evidence: tuple[str, ...] = (), gaps: tuple[str, ...] = ()
) -> SessionVerdict:
    return SessionVerdict(
        "r01",
        session,
        status,  # type: ignore[arg-type]
        None,
        tuple(SealIssue(code, "detail") for code in evidence),  # type: ignore[arg-type]
        tuple(SealIssue(code, "detail") for code in gaps),  # type: ignore[arg-type]
    )


def _result(*verdicts: SessionVerdict, rejected: int = 0, problems: int = 0) -> VerifyResult:
    lines = tuple(LineError("log.jsonl", i + 1, "invalid_json", "bad") for i in range(rejected))
    files = tuple(FileProblem("a.jsonl", i + 1, "orphan_timestamp", "x") for i in range(problems))
    return VerifyResult(
        "0.3",
        verdicts,
        lines,
        MappingProxyType({"invalid_json": rejected} if rejected else {}),
        files,
        MappingProxyType({"orphan_timestamp": problems} if problems else {}),
    )


def _inputs(**overrides: Any) -> ReportInputs:
    log_check, timelines = _golden()
    verification = verify_log(log_check, [], [], [], [], RULES)
    fields: dict[str, Any] = {
        "log_check": log_check,
        "verification": verification,
        "timelines": tuple(timelines),
        "generated_at": GENERATED_AT,
    }
    return ReportInputs(**(fields | overrides))


# -- 11.1 provenance ---------------------------------------------------------------------------


def test_provenance__defs_11_1() -> None:
    files = (InputFile("log", "s1.jsonl", "ab" * 32), InputFile("anchor", "a.jsonl", "cd" * 32))
    report = build_report(_inputs(files=files, spec_files={"rules.toml": "ef" * 32}), RULES, FAST)
    p = report["provenance"]
    assert p["spec_version"] == "0.3"
    assert p["software"]["name"] == "autonometry"
    assert p["generated_at"] == GENERATED_AT
    assert p["robots"] == ["r01", "r02"]
    assert p["period"] == {
        "start": "2026-11-10T08:00:00.000000000Z",
        "end": "2026-11-12T08:01:30.000000000Z",
    }
    assert p["counts"] == {"sessions": 4, "robots": 2, "utc_days": 3, "tasks": 7}
    assert p["registry_hashes"]["task_registry_hash"] == [
        "23edd05536c36dc1d83201c7443e834b6b1b7d55d0fb2fa7bf87025d1bda448d"
    ]
    assert [f["role"] for f in p["input_files"]] == ["anchor", "log"]
    assert p["spec_files"] == {"rules.toml": "ef" * 32}
    section_9 = {
        "idle_threshold_s", "gap_tolerance_s", "heartbeat_interval_s", "help_latency_max_s",
        "approval_min_dwell_s", "approval_wait_max_s", "hint_ttl_s", "hint_min_dwell_s",
        "takeover_window_s", "simultaneity_window_s", "goal_release_s", "contention_window_s",
        "unknown_share_max", "checkpoint_every_events", "checkpoint_every_s",
        "max_commit_delay_s", "clock_tolerance_s",
    }  # fmt: skip
    assert section_9 <= set(p["parameters"])
    assert p["statistics"] == {
        "alpha": 0.05, "n_boot": 50, "seed": 0, "cluster": "auto", "drift_bin": "day"
    }  # fmt: skip


def test_utc_days_count_each_day_a_session_touches__defs_11_1() -> None:
    _, timelines = _golden()
    day = 86_400 * 10**9
    first = timelines[0]
    spanning = replace(first, start_ns=10 * day - 1, end_ns=12 * day + 1)
    ending_at_midnight = replace(first, start_ns=20 * day - 5, end_ns=20 * day)
    report = build_report(_inputs(timelines=(spanning, ending_at_midnight)), RULES, FAST)
    # Days 9 (its last nanosecond), 10, 11 and 12 (its first), then day 19 only: an end at
    # midnight does not touch the next day.
    assert report["provenance"]["counts"]["utc_days"] == 4 + 1


def test_provenance_without_observed_time__defs_11_1() -> None:
    _, timelines = _golden()
    empty = replace(timelines[0], start_ns=None, end_ns=None, simulated=None)
    report = build_report(_inputs(timelines=(empty,)), RULES, FAST)
    assert report["provenance"]["period"] is None
    assert report["verdict"]["completeness"] == "incomplete"
    assert report["verdict"]["reasons"][-2:] == [
        "incomplete: no group has metrics",
        "4 session(s) left out of the metrics",
    ]


# -- 11.2 trust --------------------------------------------------------------------------------


def test_level_is_l2_only_for_verified__defs_11_2() -> None:
    assert trust.session_level("verified") == "L2"
    for status in ("partial", "unverified", "failed"):
        assert trust.session_level(status) == "L1"  # type: ignore[arg-type]


def test_report_level_is_the_lowest__defs_11_2() -> None:
    assert trust.report_level(_result(_verdict("a", "verified"), _verdict("b", "verified"))) == "L2"
    assert trust.report_level(_result(_verdict("a", "verified"), _verdict("b", "partial"))) == "L1"
    assert trust.report_level(_result()) == "L1"


def test_report_status__defs_11_2() -> None:
    verified = _verdict("a", "verified")
    assert trust.report_status(_result(verified)) == "verified"
    assert trust.report_status(_result(verified, rejected=1)) == "not_verified"
    assert trust.report_status(_result(verified, problems=1)) == "not_verified"
    assert trust.report_status(_result(verified, _verdict("b", "unverified"))) == "not_verified"
    failed = _verdict("b", "failed", evidence=("chain_break",))
    assert trust.report_status(_result(verified, failed)) == "failed"


def test_trust_notes_missing_key_and_tsa_root_and_l3__defs_11_2() -> None:
    section = trust.trust_section(_result(_verdict("a", "unverified")), 0, 0, 0)
    assert section["notes"] == [trust.NO_KEY_NOTE, trust.NO_TSA_ROOT_NOTE]
    assert section["levels"]["L3"] == "not available yet"
    assert trust.trust_section(_result(), 1, 1, 0)["notes"] == []


def test_trust_lists_rejected_lines_and_file_problems__defs_11_2() -> None:
    section = trust.trust_section(
        _result(_verdict("a", "verified"), rejected=2, problems=1), 1, 1, 1
    )
    assert section["rejected_lines"]["count"] == 2
    assert section["file_problems"]["by_code"] == {"orphan_timestamp": 1}
    assert section["sessions"][0]["level"] == "L2"
    assert section["status"] == "not_verified"


# -- 11.3 census -------------------------------------------------------------------------------


def test_census_ranges_and_forms__defs_11_3() -> None:
    def sid(n: int) -> str:
        return f"{n:06d}-20261112T080000Z-00ff"

    result = census.census(
        [
            ("r1", sid(1)),
            ("r1", sid(2)),
            ("r1", sid(6)),
            ("r1", sid(8)),
            ("r2", sid(5)),
            ("r2", "s1"),
        ]
    )
    r1, r2 = result["robots"]
    assert r1["missing_ranges"] == [["000003", "000005"], ["000007", "000007"]]
    assert r1["missing_count"] == 4
    assert (r2["sessions_checked"], r2["sessions_not_checked"]) == (1, ["s1"])
    assert result["status"] == "fail"
    assert census.census([("r1", sid(1)), ("r1", sid(2))])["status"] == "pass"
    assert census.census([("r1", sid(1)), ("r1", "x")])["status"] == "not_checked"
    assert census.census([("r1", "x")])["status"] == "not_checked"
    assert census.census([])["status"] == "not_checked"
    assert census.counter_of("1234567-20261112T080000Z-00ff") == 1234567
    assert census.counter_of("000001-20261112T080000Z-00FF") is None


# -- 11.4 exclusions ---------------------------------------------------------------------------


def test_exclusions_say_why__defs_11_4() -> None:
    problem = "tasks.json does not match session_start.task_registry_hash"
    out = trust.exclusions(
        keys=[("r1", "a"), ("r1", "b"), ("r1", "c"), ("r1", "d")],
        in_logs=[("r1", "b"), ("r1", "c"), ("r1", "d")],
        in_metrics=[("r1", "b")],
        registry_problems={("r1", "d"): (problem,)},
    )
    assert out == [
        {"robot_id": "r1", "session_id": "a", "reasons": [trust.ABSENT_REASON]},
        {"robot_id": "r1", "session_id": "c", "reasons": [trust.NO_START_REASON]},
        {"robot_id": "r1", "session_id": "d", "reasons": [problem]},
    ]


def test_session_without_start_is_excluded__defs_11_4() -> None:
    folder = Path(__file__).parent / "golden" / "flags"
    log_check = read_logs([folder / "no_session_start.jsonl"], "0.3")
    timelines = tuple(build_timeline(s, Registries(), RULES) for s in log_check.sessions)
    verification = verify_log(log_check, [], [], [], [], RULES)
    report = build_report(
        ReportInputs(log_check, verification, timelines, GENERATED_AT), RULES, FAST
    )
    assert [e["reasons"] for e in report["exclusions"]] == [[trust.NO_START_REASON]]
    assert report["metrics"]["groups"] == {}


def test_session_named_only_by_a_checkpoint_is_excluded__defs_11_4() -> None:
    inputs = _inputs()
    absent = _verdict("s9", "failed", evidence=("missing_events",))
    verification = replace(inputs.verification, sessions=(*inputs.verification.sessions, absent))
    report = build_report(replace(inputs, verification=verification), RULES, FAST)
    assert report["exclusions"] == [
        {"robot_id": "r01", "session_id": "s9", "reasons": [trust.ABSENT_REASON]}
    ]
    row = next(r for r in report["sessions"] if r["session_id"] == "s9")
    assert (row["status"], row["in_metrics"], row["observed_s"]) == ("failed", False, None)
    assert report["verdict"]["status"] == "failed"


# -- 11.5 metrics ------------------------------------------------------------------------------


def test_metrics_per_group_never_pooled__defs_11_5() -> None:
    inputs = _inputs()
    report = build_report(inputs, RULES, FAST)
    assert sorted(report["metrics"]["groups"]) == ["real", "simulated"]
    assert report["metrics"]["groups"]["real"]["n_sessions"] == 3
    assert report["metrics"]["groups"]["simulated"]["n_sessions"] == 1
    expected = json.loads(
        json.dumps(metrics_report(list(inputs.timelines), RULES, FAST), default=float)
    )
    assert report["metrics"]["options"] == expected["options"]
    assert report["metrics"]["groups"]["real"]["n_robots"] == expected["groups"]["real"]["n_robots"]


# -- 11.6 statements ---------------------------------------------------------------------------


def _block(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "estimate": 0.5,
        "ci": [0.4, 0.6],
        "n_clusters": 5,
        "unknown_as_human": {"estimate": 0.45},
        "unknown_as_ai": {"estimate": 0.55},
        "count": 10,
        "active_hours": 2.0,
    }
    return base | fields


def _group(
    unknown_s: float = 0.0, unobserved: dict[str, int] | None = None, **blocks: Any
) -> dict[str, Any]:
    names = {*statements.BOUNDED, *statements.CLUSTERED, *statements.RATES}
    group: dict[str, Any] = {name: _block() for name in names}
    group |= blocks
    group["unmeasured"] = {"unknown_time_s": unknown_s, "unobserved_sessions": unobserved or {}}
    return group


def _codes(group: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (s["code"], tuple(s["metrics"]))
        for s in statements.statements({"real": group}, DEPENDS_ON, RULES.report)
    ]


def test_no_statement_for_a_clean_group__defs_11_6() -> None:
    assert _codes(_group()) == []


def test_statement_no_worst_case_with_unknown_time__defs_11_6_1() -> None:
    found = statements.statements({"real": _group(unknown_s=1.0)}, DEPENDS_ON, RULES.report)
    assert [(s["code"], s["metrics"]) for s in found] == [
        ("no_worst_case", ["interventions_per_active_hour"]),
        ("no_worst_case", ["human_minutes_per_task"]),
    ]
    assert "one hidden intervention per" in found[0]["text"]


def test_statement_data_quality_when_bounds_wider_than_ci__defs_11_6_2() -> None:
    wide = _block(unknown_as_human={"estimate": 0.2}, unknown_as_ai={"estimate": 0.9})
    found = statements.statements(
        {"real": _group(time_autonomy_share=wide)}, DEPENDS_ON, RULES.report
    )
    assert [(s["code"], s["metrics"]) for s in found] == [("data_quality", ["time_autonomy_share"])]
    assert found[0]["text"].startswith("Data quality, not sample size, limits this number")
    open_ci = _block(
        ci=[0.4, None], unknown_as_human={"estimate": 0.0}, unknown_as_ai={"estimate": 1.0}
    )
    assert _codes(_group(time_autonomy_share=open_ci)) == []


def test_statement_single_cluster__defs_11_6_3() -> None:
    found = statements.statements(
        {"real": _group(task_autonomy_rate=_block(n_clusters=1))}, DEPENDS_ON, RULES.report
    )
    assert [(s["code"], s["metrics"]) for s in found] == [
        ("not_generalisable", ["task_autonomy_rate"])
    ]
    assert "not generalisable" in found[0]["text"]
    assert _codes(_group(task_autonomy_rate=_block(n_clusters=1, estimate=None))) == []


def test_statement_rare_rate_upper_bound__defs_11_6_4() -> None:
    found = statements.statements(
        {"real": _group(hints_per_active_hour=_block(count=0, active_hours=1.0))},
        DEPENDS_ON,
        RULES.report,
    )
    assert [(s["code"], s["metrics"]) for s in found] == [
        ("rare_events", ["hints_per_active_hour"])
    ]
    # Zero events in one hour: the one-sided 95 % Garwood bound is -ln(0.05) = 2.9957 per hour.
    assert found[0]["upper_bound"] == pytest.approx(2.995732, rel=1e-6)
    assert found[0]["level"] == 0.95
    assert _codes(_group(hints_per_active_hour=_block(count=0, active_hours=0.0))) == []
    assert _codes(_group(hints_per_active_hour=_block(count=5))) == []


def test_statement_unobserved_signals__defs_11_6_5() -> None:
    found = statements.statements(
        {"real": _group(unobserved={"safety_stops": 2})}, DEPENDS_ON, RULES.report
    )
    assert [s["code"] for s in found] == ["unobserved"]
    assert found[0]["sessions"] == 2
    assert "safety_stops_per_active_hour" in found[0]["metrics"]
    assert "task_autonomy_rate" not in found[0]["metrics"]


# -- 11.7 invariants ---------------------------------------------------------------------------


def test_partition_passes_on_real_timelines__defs_11_7() -> None:
    _, timelines = _golden()
    assert invariants.partition(timelines)["status"] == "pass"


def test_partition_fails_when_states_miss_time__defs_11_7() -> None:
    _, timelines = _golden()
    broken = replace(timelines[0], whole_robot=replace(timelines[0].whole_robot, observed_ns=1))
    result = invariants.partition([broken])
    assert result["status"] == "fail"
    assert result["failures"][0].startswith("r01/s1 whole robot")


def test_chain_invariant__defs_11_7() -> None:
    assert invariants.chain(_result(_verdict("a", "verified")))["status"] == "pass"
    assert (
        invariants.chain(_result(_verdict("a", "partial", gaps=("no_checkpoint",))))["status"]
        == "pass"
    )
    unsealed = invariants.chain(_result(_verdict("a", "unverified", gaps=("unsealed",))))
    assert (unsealed["status"], unsealed["failures"]) == ("not_checked", ["r01/a"])
    assert invariants.chain(_result())["status"] == "not_checked"
    failed = invariants.chain(
        _result(_verdict("a", "failed", evidence=("hash_mismatch", "chain_break")))
    )
    assert (failed["status"], failed["failures"]) == ("fail", ["r01/a: chain_break, hash_mismatch"])


def test_time_invariant__defs_11_7() -> None:
    log_check, _ = _golden()
    assert invariants.time(log_check, _result(_verdict("a", "verified")))["status"] == "pass"
    regressed = replace(log_check, flags=(Flag("r01", "s1", 3, "ts_regression", "x"),) * 2)
    skewed = _result(_verdict("a", "partial", gaps=("clock_skew", "late_anchor")))
    result = invariants.time(regressed, skewed)
    assert result["status"] == "fail"
    assert result["failures"] == [
        "r01/s1: ts_regression x2",
        "r01/a: clock_skew",
        "r01/a: late_anchor",
    ]


def test_tasks_invariant__defs_11_7() -> None:
    log_check, timelines = _golden()
    assert invariants.tasks(log_check, timelines)["status"] == "pass"
    _, no_registry = _golden(task_registry=False)
    result = invariants.tasks(log_check, no_registry)
    assert result["status"] == "fail"
    assert "definition not in the registry" in result["failures"][0]
    lost = replace(timelines[0], tasks=timelines[0].tasks[1:])
    assert "started" in invariants.tasks(log_check, [lost])["failures"][0]


def test_bounds_invariant__defs_11_7() -> None:
    assert invariants.bounds({"real": _group()})["status"] == "pass"
    bad = _block(unknown_as_human={"estimate": 0.7})
    result = invariants.bounds({"real": _group(time_autonomy_share=bad)})
    assert result["status"] == "fail"
    assert result["failures"][0].startswith("real time_autonomy_share")
    empty = _group(**{m: _block(estimate=None) for m in invariants.BOUNDED})
    assert invariants.bounds({"real": empty})["status"] == "not_checked"


def test_witnesses_not_available__defs_11_7() -> None:
    assert invariants.witnesses() == {
        "status": "not_available",
        "detail": "not available (L3)",
        "failure_count": 0,
        "failures": [],
    }


def test_invariants_listed_in_order__defs_11_7() -> None:
    report = build_report(_inputs(), RULES, FAST)
    assert list(report["invariants"]) == [
        "partition", "chain", "time", "census", "tasks", "bounds", "witnesses"
    ]  # fmt: skip
    assert report["invariants"]["census"]["status"] == report["census"]["status"]


def test_failures_list_is_bounded__defs_11_7() -> None:
    verdicts = [_verdict(f"s{i:03d}", "failed", evidence=("chain_break",)) for i in range(30)]
    result = invariants.chain(_result(*verdicts))
    assert result["failure_count"] == 30
    assert len(result["failures"]) == invariants.LISTED_MAX


# -- verdict, figures, page --------------------------------------------------------------------


def test_verdict_clean_only_when_everything_holds__defs_11() -> None:
    trust_ok = {"status": "verified", "level": "L2"}
    complete = {"real": {"unmeasured": {"conformance": "complete"}}}
    passing = {"partition": {"status": "pass"}, "census": {"status": "not_checked"}}
    no_gap = {"status": "pass", "robots": [{"missing_count": 0}]}

    def check(groups: dict[str, Any], census_: dict[str, Any], excluded: list[Any]) -> list[str]:
        headline = {g: {m: _block() for m in ("time_autonomy_share", "task_autonomy_rate",
                    "interventions_per_active_hour", "human_minutes_per_task")} | groups[g]
                    for g in groups}  # fmt: skip
        result = verdict(trust_ok, headline, census_, excluded, passing)
        assert result["clean"] is (not result["reasons"])
        return list(result["reasons"])

    assert check(complete, no_gap, []) == []
    assert check(complete, {"status": "fail", "robots": [{"missing_count": 2}]}, []) == [
        "census: 2 session counter(s) missing"
    ]
    assert check(complete, no_gap, [{}]) == ["1 session(s) left out of the metrics"]
    incomplete = {"real": {"unmeasured": {"conformance": "incomplete"}}}
    assert check(incomplete, no_gap, []) == ["incomplete"]


def test_figures_skip_what_has_no_data() -> None:
    assert figures.state_shares({}) is None
    assert figures.state_shares({"real": {"observed_s": 0.0, "states_s": {}}}) is None
    assert figures.gaps_km({"real": None}) is None
    assert figures.drift({"real": [{"bin": "2026-11-10", "rate": None, "ci": None}]}) is None
    assert figures.time_share({"real": {"time_autonomy_share": {"estimate": None}}}) is None


def test_html_formatters() -> None:
    assert html.pct(None) == "n/a"
    assert html.pct(0.1234) == "12.3 %"
    assert html.num(51.575931) == "51.58"
    assert html.seconds(42.4) == "42 s"
    assert html.seconds(90.0) == "1 min 30 s"
    assert html.seconds(7265.6) == "2 h 1 min 6 s"
    assert html.seconds(None) == "n/a"
    assert html.hours(0.5) == "30 min 0 s"
    assert html.instant("2026-11-10T08:00:00.999999999Z") == "2026-11-10T08:00:00Z"
    assert html.instant(None) == "n/a"
    assert html.interval(None) == "n/a"
    assert html.interval([0.1, None], html.pct) == "[10.0 %, ]"
    assert figures.time_unit(2.0) == ("h", 1.0)
    assert figures.time_unit(0.02)[0] == "min"
    assert figures.time_unit(0.001)[0] == "s"


def test_page_is_a_function_of_the_dict__defs_11() -> None:
    report = build_report(_inputs(), RULES, FAST)
    first = html.render_html(report)
    assert html.render_html(json.loads(json.dumps(report))) == first


def test_failed_invariant_makes_the_report_unclean__defs_11_7() -> None:
    trust_ok = {"status": "verified", "level": "L2"}
    groups = {
        "real": {"unmeasured": {"conformance": "complete"}}
        | {m: _block() for m in ("time_autonomy_share", "task_autonomy_rate",
           "interventions_per_active_hour", "human_minutes_per_task")}
    }  # fmt: skip
    checks = {"partition": {"status": "fail"}, "witnesses": {"status": "not_available"}}
    result = verdict(trust_ok, groups, {"status": "pass", "robots": []}, [], checks)
    assert result["reasons"] == ["invariant partition failed"]
    assert result["clean"] is False


def test_statement_thresholds_come_from_rules__defs_11_6() -> None:
    assert (RULES.report.rare_rate_events, RULES.report.min_clusters) == (5, 2)
    strict = ReportRules(rare_rate_events=11, min_clusters=6)
    found = statements.statements({"real": _group()}, DEPENDS_ON, strict)
    assert {s["code"] for s in found} == {"rare_events", "not_generalisable"}
    report = build_report(_inputs(), RULES, FAST)
    assert report["provenance"]["parameters"]["rare_rate_events"] == 5
    assert report["provenance"]["parameters"]["min_clusters"] == 2


def test_statements_grouped_by_group_and_kind__defs_11_6() -> None:
    found = statements.statements(
        {
            "real": _group(
                task_autonomy_rate=_block(n_clusters=1), time_autonomy_share=_block(n_clusters=1)
            )
        },
        DEPENDS_ON,
        RULES.report,
    )
    rows = html.statement_rows(found)
    assert rows == [
        {
            "group": "real",
            "kind": "Single cluster, not generalisable",
            "metrics": "time autonomy share, task autonomy rate",
            "detail": rows[0]["detail"],
        }
    ]


def test_moved_dwell_is_shown_under_the_state_table__defs_11_5() -> None:
    report = build_report(_inputs(), RULES, FAST)
    assert report["figures"]["state_seconds"]["simulated"]["moved_s"] == pytest.approx(3.0)
    page = html.render_html(report)
    assert "Approval and hint dwell moved 3 s of this AI time to human time (Section 6.1)" in page


def test_verdict_colour_follows_cleanliness__defs_11() -> None:
    report = build_report(_inputs(), RULES, FAST)
    assert 'class="verdict unclean"' in html.render_html(report)
    clean = report | {"verdict": report["verdict"] | {"clean": True, "status": "verified"}}
    assert 'class="verdict clean"' in html.render_html(clean)
    failed = report | {"verdict": report["verdict"] | {"status": "failed"}}
    assert 'class="verdict failed"' in html.render_html(failed)


def test_gap_median_open_limit_reads_not_reached() -> None:
    assert html.interval([0.5, None], html.hours, "not reached") == "[30 min 0 s, not reached]"


def test_notes_sit_on_the_method_row_not_a_column__defs_11_6() -> None:
    page = html.render_html(build_report(_inputs(), RULES, FAST))
    assert ">Notes</th>" not in page
    assert "method: wilson_effective_n(single_cluster) · single cluster: not generalisable" in page
    assert "@media (max-width: 40rem)" in page
