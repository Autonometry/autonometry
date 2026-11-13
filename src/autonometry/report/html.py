"""The report as one self-contained HTML page (definitions.md 11): inline CSS and inline SVG
figures, no scripts, no web fonts and no links to other sites; it prints cleanly to PDF from a
browser.

jinja2 renders it with autoescape on, so every string from a log, a registry, ``--title`` or
``--about`` is escaped. The only markup marked safe is the SVG this package drew itself from
numbers and fixed labels (:mod:`autonometry.report.figures`). The page is a function of the report
dict alone: the same dict gives the same bytes.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup

from autonometry.report import figures as figures_
from autonometry.report.build import HEADLINE, STATE_ORDER
from autonometry.stats.metrics import HOUR_S, MINUTE_S

Json = dict[str, Any]
Row = dict[str, Any]
Format = Callable[[Any], str]

NA: Final = "n/a"


def pct(value: Any) -> str:
    """A proportion as a percentage with one decimal."""
    return NA if value is None else f"{float(value) * 100:.1f} %"


def num(value: Any) -> str:
    """A number with four significant digits."""
    return NA if value is None else f"{float(value):.4g}"


def seconds(value: Any) -> str:
    """A duration in seconds to the nearest second, in s, min and s, or h, min and s by size
    (the JSON keeps full precision)."""
    if value is None:
        return NA
    total = round(float(value))
    hours, rest = divmod(total, round(HOUR_S))
    minutes, secs = divmod(rest, round(MINUTE_S))
    if hours:
        return f"{hours} h {minutes} min {secs} s"
    return f"{minutes} min {secs} s" if minutes else f"{secs} s"


def hours(value: Any) -> str:
    """A duration in hours, shown as :func:`seconds` shows it."""
    return NA if value is None else seconds(float(value) * HOUR_S)


def instant(value: Any) -> str:
    """An RFC 3339 UTC time to the second: the fraction is cut, never rounded up."""
    return NA if value is None else re.sub(r"\.[0-9]+Z$", "Z", str(value))


def interval(pair: Any, fmt: Format = num, open_text: str = "") -> str:
    """``[lo, hi]``; an open limit shows as ``open_text``."""
    if not pair:
        return NA
    lo, hi = pair
    return f"[{open_text if lo is None else fmt(lo)}, {open_text if hi is None else fmt(hi)}]"


#: Metrics of the metrics table: key, label, formatter.
METRIC_ROWS: Final[Sequence[tuple[str, str, Format]]] = (
    ("time_autonomy_share", "Time autonomy share", pct),
    ("task_autonomy_rate", "Task autonomy rate", pct),
    ("autonomous_success_rate", "Autonomous success rate", pct),
    ("interventions_per_active_hour", "Interventions per active hour", num),
    ("human_minutes_per_task", "Human minutes per task", num),
    ("safety_stops_per_active_hour", "Safety stops per active hour", num),
    ("approvals_per_active_hour", "Approvals per active hour", num),
    ("hints_per_active_hour", "Hints per active hour", num),
    ("takeovers_per_active_hour", "Takeovers per active hour", num),
)
LABELS: Final[Mapping[str, str]] = {key: label for key, label, _ in METRIC_ROWS}
FORMATS: Final[Mapping[str, Format]] = {key: fmt for key, _, fmt in METRIC_ROWS}
VARIANTS: Final = ("with_approvals", "shared_as_ai", "scope_weighted", "unsupported")
INVARIANTS: Final = ("partition", "chain", "time", "census", "tasks", "bounds", "witnesses")
STATUS_TEXT: Final[Mapping[str, str]] = {
    "verified": "verified",
    "not_verified": "not verified",
    "failed": "FAILED",
}


def _bound(block: Json, name: str, fmt: Format) -> str:
    bound = block.get(name)
    if bound is None:
        return ""
    return f"{fmt(bound.get('estimate'))} {interval(bound.get('ci'), fmt)}"


def _metric_row(key: str, block: Json, notes: Sequence[str]) -> Row:
    fmt = FORMATS[key]
    return {
        "label": LABELS[key],
        "estimate": fmt(block.get("estimate")),
        "ci": interval(block.get("ci"), fmt),
        "method": block.get("interval_method") or block.get("note") or "",
        "lower": _bound(block, "unknown_as_human", fmt),
        "upper": _bound(block, "unknown_as_ai", fmt),
        "outer": interval(block["outer_interval"], fmt) if "outer_interval" in block else "",
        "clusters": "" if block.get("n_clusters") is None else str(block["n_clusters"]),
        "notes": list(notes),
    }


def _note(statement: Json) -> str:
    if statement["code"] == "rare_events":
        return f"95 % upper bound {num(statement['upper_bound'])}"
    return {
        "no_worst_case": "no true worst case",
        "data_quality": "limited by data quality",
        "not_generalisable": "single cluster: not generalisable",
        "unobserved": f"{statement.get('signal')} unobserved",
    }[statement["code"]]


def _group(name: str, metrics: Json, report: Json) -> Row:
    notes: dict[str, list[str]] = {}
    for statement in report["statements"]:
        if statement["group"] == name:
            for metric in statement["metrics"]:
                notes.setdefault(metric, []).append(_note(statement))
    share = metrics["time_autonomy_share"]
    unmeasured = metrics["unmeasured"]
    states = report["figures"]["state_seconds"].get(name)
    observed = states["observed_s"] if states else 0.0
    drift = {b["bin"]: b for b in report["figures"]["drift"].get(name, [])}
    gaps = metrics["gaps_between_interventions"]["active_hours"]
    return {
        "name": name,
        "n_sessions": metrics["n_sessions"],
        "n_robots": metrics["n_robots"],
        "conformance": unmeasured["conformance"],
        "unknown_share": pct(unmeasured["unknown_share"]),
        "unknown_share_max": pct(unmeasured["unknown_share_max"]),
        "observed": seconds(unmeasured["observed_time_s"]),
        "moved": seconds(states["moved_s"]) if states else NA,
        "active": seconds(unmeasured["active_time_s"]),
        "cluster": share.get("cluster", ""),
        "rows": [_metric_row(key, metrics[key], notes.get(key, [])) for key, _, _ in METRIC_ROWS],
        "variants": [
            {
                "name": variant,
                "estimate": pct(share["variants"][variant]["estimate"]),
                "ci": interval(share["variants"][variant]["ci"], pct),
            }
            for variant in VARIANTS
        ],
        "states": [
            {
                "state": figures_.STATE_LABELS[state],
                "seconds": seconds(states["states_s"][state]) if states else NA,
                "share": pct(states["states_s"][state] / observed) if observed > 0 else NA,
            }
            for state in STATE_ORDER
        ],
        "gap_median": f"{hours(gaps.get('estimate'))} "
        f"{interval(gaps.get('ci'), hours, 'not reached')}",
        "gap_note": gaps.get("note", ""),
        "drift": [
            {
                "bin": b["bin"],
                "active": hours(b["active_hours"]),
                "count": b["count"],
                "rate": num(b["rate"]),
                "ci": interval(drift[b["bin"]]["ci"]) if b["bin"] in drift else NA,
            }
            for b in metrics["drift"]["bins"]
        ],
        "trend": metrics["drift"]["trend"],
    }


#: How the page names each kind of statement (11.6), so a kind is listed once per group.
STATEMENT_KINDS: Final[Mapping[str, str]] = {
    "no_worst_case": "No true worst case while unknown time exists",
    "data_quality": "Data quality, not sample size, limits these numbers",
    "not_generalisable": "Single cluster, not generalisable",
    "rare_events": "Few events: one-sided 95 % upper bound (exact Poisson, Garwood)",
    "unobserved": "Signal class {signal} declared unobserved by {sessions} session(s)",
}
_KIND_DETAIL: Final[Mapping[str, str]] = {
    "no_worst_case": "Events can hide in unknown time. unknown_as_human assumes one hidden "
    "intervention per run of unknown time between AI control and counts each task's unknown "
    "time as human time.",
    "data_quality": "Their bounds for unknown time are wider than their confidence intervals.",
    "not_generalisable": "The interval describes these sessions, not new robots or days.",
    "rare_events": "Bounds are per active hour.",
    "unobserved": "The recorder cannot see it; these metrics depend on it.",
}


def _metric_name(statement: Json, metric: str) -> str:
    name = LABELS.get(metric, metric.replace("_", " ")).lower()
    if statement["code"] == "rare_events":
        return f"{name} ({statement['count']} event(s); bound {num(statement['upper_bound'])})"
    return name


def statement_rows(statements: Sequence[Json]) -> list[Row]:
    """Statements grouped by group and kind, in the order they first appear: one line per
    kind naming every metric it concerns (11.6)."""
    rows: dict[tuple[str, str], Row] = {}
    for s in statements:
        key = (s["group"], s["code"] + "/" + str(s.get("signal", "")))
        if key not in rows:
            kind = STATEMENT_KINDS[s["code"]].format(
                signal=s.get("signal", ""), sessions=s.get("sessions", "")
            )
            rows[key] = {
                "group": s["group"],
                "kind": kind,
                "metrics": [],
                "detail": _KIND_DETAIL[s["code"]],
            }
        rows[key]["metrics"] += [_metric_name(s, metric) for metric in s["metrics"]]
    return [row | {"metrics": ", ".join(row["metrics"])} for row in rows.values()]


def _headline(report: Json) -> list[Row]:
    rows = []
    for group, metrics in sorted(report["verdict"]["headline"].items()):
        for key in HEADLINE:
            block = metrics[key]
            fmt = FORMATS[key]
            rows.append(
                {
                    "group": group,
                    "label": LABELS[key],
                    "estimate": fmt(block["estimate"]),
                    "ci": interval(block["ci"], fmt),
                    "outer": interval(block["outer_interval"], fmt),
                }
            )
    return rows


def _session_row(row: Json) -> Row:
    return row | {
        "simulated": NA if row["simulated"] is None else ("yes" if row["simulated"] else "no"),
        "status": row["status"] or NA,
        "observed": seconds(row["observed_s"]),
        "active": seconds(row["active_s"]),
        "ai_share": pct(row["ai_share"]),
        "interventions": NA if row["interventions"] is None else str(row["interventions"]),
        "unknown": pct(row["unknown_share"]),
        "flags": ", ".join(f"{code} {count}" for code, count in row["flags"].items()),
    }


def context(report: Json) -> Json:
    """The values the template shows, formatted; strings stay plain text for autoescape."""
    groups = report["metrics"]["groups"]
    svgs = figures_.figures(report)
    return {
        "report": report,
        "status_text": STATUS_TEXT[report["verdict"]["status"]],
        "verdict_class": "failed"
        if report["verdict"]["status"] == "failed"
        else ("clean" if report["verdict"]["clean"] else "unclean"),
        "statement_rows": statement_rows(report["statements"]),
        "headline": _headline(report),
        "groups": [_group(name, groups[name], report) for name in sorted(groups)],
        "invariants": [{"name": name} | report["invariants"][name] for name in INVARIANTS],
        "sessions": [_session_row(row) for row in report["sessions"]],
        "figures": {name: None if svg is None else Markup(svg) for name, svg in svgs.items()},  # noqa: S704 -- SVG drawn here from numbers and fixed labels
        "labels": LABELS,
    }


def _environment() -> Environment:
    environment = Environment(
        loader=PackageLoader("autonometry.report", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["num"] = num
    environment.filters["pct"] = pct
    environment.filters["seconds"] = seconds
    environment.filters["instant"] = instant
    return environment


def render_html(report: Json) -> str:
    """The report dict as one self-contained HTML page."""
    return _environment().get_template("report.html.j2").render(context(report))
