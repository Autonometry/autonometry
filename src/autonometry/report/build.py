"""The report as a JSON-ready dict (definitions.md 11), built by pure functions from what the
command line read: the checked logs, their verification, the timelines and the hashes of the
input files. Nothing here reads a file, the clock or the network: ``generated_at`` is an input.

Order of the sections: verdict, provenance, trust, witness, census, exclusions, metrics,
statements, invariants, sessions, figures. ``figures`` holds the numbers the rendered figures
draw that the metrics do not carry (Kaplan-Meier steps, drift intervals), so the HTML is a
function of the JSON alone.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from autonometry import __version__
from autonometry.events import LogCheck, Session, VerifyResult
from autonometry.report import census as census_
from autonometry.report import invariants as invariants_
from autonometry.report import statements as statements_
from autonometry.report import trust as trust_
from autonometry.spec import Rules
from autonometry.stats import intervals as iv
from autonometry.stats.facts import SessionFacts, session_facts
from autonometry.stats.metrics import DEPENDS_ON, Json, Options, gap_table, parameters
from autonometry.stats.report import GROUPS, clean, metrics_report
from autonometry.timeline import Timeline
from autonometry.timestamps import ns_to_ts
from autonometry.witness import Reconciliation

SOFTWARE: Final = "autonometry"
#: The four numbers of the verdict box, per group.
HEADLINE: Final = (
    "time_autonomy_share",
    "task_autonomy_rate",
    "interventions_per_active_hour",
    "human_minutes_per_task",
)
#: Whole-robot states in the order the report draws and lists them.
STATE_ORDER: Final = (
    "ai",
    "scripted",
    "shared",
    "human_local",
    "human_remote",
    "safety_stop",
    "idle",
    "unknown",
)
#: ``session_start`` fields holding registry hashes (definitions.md 8, Registries).
REGISTRY_HASH_FIELDS: Final = ("device_registry_hash", "task_registry_hash", "plan_hash")
_NS_PER_DAY: Final = 86_400 * 1_000_000_000

Key = tuple[str, str]


@dataclass(frozen=True, slots=True)
class InputFile:
    """One file the report read: its role (``log``, ``checkpoints``, ``anchor``, ``devices``,
    ``tasks``, ``plan``, ``vkey``, ``tsa_root``, ``about``), its path as given, its SHA-256."""

    role: str
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ReportInputs:
    """Everything a report is built from. ``registry_problems`` names, per session left without
    a timeline, why its registries could not be used; ``spec_files`` maps each file of the spec
    version to its SHA-256; ``trusted_keys``, ``trusted_tsa_roots`` and ``anchor_files`` count
    what the verifier was given. ``witness`` is the reconciliation with the station logs given
    and ``station_verification`` their verification (definitions.md 12); both ``None``
    without station logs."""

    log_check: LogCheck
    verification: VerifyResult
    timelines: tuple[Timeline, ...]
    generated_at: str
    registry_problems: Mapping[Key, tuple[str, ...]] = field(default_factory=dict)
    files: tuple[InputFile, ...] = ()
    spec_files: Mapping[str, str] = field(default_factory=dict)
    trusted_keys: int = 0
    trusted_tsa_roots: int = 0
    anchor_files: int = 0
    title: str | None = None
    about: str | None = None
    witness: Reconciliation | None = None
    station_verification: VerifyResult | None = None


def _key(item: Timeline | SessionFacts) -> Key:
    return (item.robot_id, item.session_id)


def _all_keys(inputs: ReportInputs) -> list[Key]:
    keys = {(v.robot_id, v.session_id) for v in inputs.verification.sessions}
    keys |= {(s.robot_id, s.session_id) for s in inputs.log_check.sessions}
    return sorted(keys)


def _station_keys(inputs: ReportInputs) -> set[Key]:
    """Sessions of the robot logs whose ``session_start`` says ``role`` ``station`` (12.1)."""
    return {(s.robot_id, s.session_id) for s in inputs.log_check.sessions if _is_station(s)}


def _is_station(session: Session) -> bool:
    start = next((e for e in session.events if e["type"] == "session_start"), None)
    return start is not None and start.get("role") == "station"


def _l3_keys(inputs: ReportInputs) -> set[Key]:
    if inputs.witness is None:
        return set()
    return {(s.robot_id, s.session_id) for s in inputs.witness.sessions if s.l3}


def witness_section(witness: Reconciliation | None) -> Json:
    """The Witness section (11.2, 12): per robot session the five times with the tolerated and
    held parts, the shares, tau, unmatched episodes, the upper-bound intervention count and the
    reasons it is not L3; the station sessions; and what L3 cannot show (12.7)."""
    if witness is None:
        return {
            "available": False,
            "detail": trust_.L3_UNAVAILABLE,
            "statement": trust_.WITNESS_STATEMENT,
        }
    body = witness.to_json()
    return {
        "available": True,
        "statement": trust_.WITNESS_STATEMENT,
        "parameters": body["parameters"],
        "counts": body["counts"],
        "sessions": body["sessions"],
        "stations": body["stations"],
        "ignored": body["ignored"],
    }


def _utc_days(timelines: Sequence[Timeline]) -> int:
    """UTC calendar days holding observed time (POSIX time has 86 400 s per day)."""
    days: set[int] = set()
    for t in timelines:
        if t.start_ns is None or t.end_ns is None or t.end_ns <= t.start_ns:
            continue
        days.update(range(t.start_ns // _NS_PER_DAY, (t.end_ns - 1) // _NS_PER_DAY + 1))
    return len(days)


def _registry_hashes(log_check: LogCheck) -> Json:
    found: dict[str, set[str]] = {name: set() for name in REGISTRY_HASH_FIELDS}
    for session in log_check.sessions:
        for event in session.events:
            if event["type"] != "session_start":
                continue
            for name in REGISTRY_HASH_FIELDS:
                value = event.get(name)
                if isinstance(value, str):
                    found[name].add(value)
    return {name: sorted(values) for name, values in found.items()}


def provenance(inputs: ReportInputs, rules: Rules, options: Options) -> Json:
    """Spec and software, generated-at, robots, period, counts, registry hashes, input file
    hashes, every parameter of definitions.md 9 and the statistics options (11.1)."""
    keys = _all_keys(inputs)
    observed = [t for t in inputs.timelines if t.start_ns is not None and t.end_ns is not None]
    return {
        "spec_version": inputs.log_check.spec_version,
        "software": {"name": SOFTWARE, "version": __version__},
        "generated_at": inputs.generated_at,
        "robots": sorted({robot for robot, _ in keys}),
        "period": {
            "start": ns_to_ts(min(t.start_ns for t in observed if t.start_ns is not None)),
            "end": ns_to_ts(max(t.end_ns for t in observed if t.end_ns is not None)),
        }
        if observed
        else None,
        "counts": {
            "sessions": len(keys),
            "robots": len({robot for robot, _ in keys}),
            "utc_days": _utc_days(inputs.timelines),
            "tasks": sum(len(t.tasks) for t in inputs.timelines),
        },
        "registry_hashes": _registry_hashes(inputs.log_check),
        "input_files": [
            {"role": f.role, "path": f.path, "sha256": f.sha256}
            for f in sorted(inputs.files, key=lambda f: (f.role, f.path))
        ],
        "spec_files": dict(sorted(inputs.spec_files.items())),
        "parameters": parameters(rules) | rules.report.model_dump(),
        "statistics": {
            "alpha": options.alpha,
            "n_boot": options.n_boot,
            "seed": options.seed,
            "cluster": options.cluster,
            "drift_bin": options.drift_bin,
        },
    }


def _ratio(num: float, den: float) -> float | None:
    return num / den if den > 0 else None


def sessions_table(
    inputs: ReportInputs,
    facts: Mapping[Key, SessionFacts],
    in_metrics: set[Key],
    l3: set[Key] | None = None,
) -> list[Json]:
    """One row per session: robot, session, simulated, status, level, observed and active
    seconds, AI share (as metric (a)), whole-robot interventions, unknown share, flag counts."""
    verdicts = {(v.robot_id, v.session_id): v for v in inputs.verification.sessions}
    rows = []
    for key in _all_keys(inputs):
        verdict = verdicts.get(key)
        f = facts.get(key)
        rows.append(
            {
                "robot_id": key[0],
                "session_id": key[1],
                "simulated": None if f is None else f.simulated,
                "status": None if verdict is None else verdict.status,
                "level": "L1"
                if verdict is None
                else trust_.session_level(verdict.status, key in (l3 or set())),
                "in_metrics": key in in_metrics,
                "observed_s": None if f is None else f.observed_s,
                "active_s": None if f is None else f.active_s,
                "ai_share": None if f is None else _ratio(f.ai_s - f.moved_s, f.active_s),
                "interventions": None if f is None else f.interventions,
                "unknown_share": None if f is None else _ratio(f.unknown_s, f.observed_s),
                "flags": {} if f is None else dict(sorted(f.flag_counts.items())),
            }
        )
    return rows


def _km(members: Sequence[SessionFacts], alpha: float) -> Json | None:
    rows, _ = gap_table(members, "session", "active_hours")
    curve = iv.kaplan_meier_curve([g.duration for g in rows], [g.observed for g in rows], alpha)
    if curve is None:
        return None
    return {
        "unit": "active_hours",
        "times": list(curve.times),
        "survival": list(curve.survival),
        "lower": list(curve.lower),
        "upper": list(curve.upper),
    }


def figure_data(
    grouped: Mapping[str, Sequence[SessionFacts]], groups: Mapping[str, Json], alpha: float
) -> Json:
    """Numbers the figures draw: whole-robot seconds per state and observed seconds per group,
    the Kaplan-Meier steps of the gaps (active hours) with their band, and each drift bin's rate
    with its exact Poisson (Garwood) interval."""
    states: Json = {}
    km: Json = {}
    drift: Json = {}
    for name in sorted(grouped):
        members = grouped[name]
        states[name] = {
            "observed_s": sum(s.observed_s for s in members),
            "moved_s": sum(s.moved_s for s in members),
            "states_s": {state: sum(s.states_s[state] for s in members) for state in STATE_ORDER},
        }
        km[name] = _km(members, alpha)
        drift[name] = [
            {
                "bin": b["bin"],
                "rate": b["rate"],
                "ci": iv.garwood(b["count"], b["active_hours"], alpha).as_list()
                if b["active_hours"] > 0
                else None,
            }
            for b in groups[name]["drift"]["bins"]
        ]
    return {"state_seconds": states, "gaps_km": km, "drift": drift}


def _headline(groups: Mapping[str, Json]) -> Json:
    return {
        name: {
            metric: {
                key: groups[name][metric].get(key)
                for key in ("estimate", "ci", "interval_method", "outer_interval")
            }
            for metric in HEADLINE
        }
        for name in sorted(groups)
    }


def verdict(
    trust: Json,
    groups: Mapping[str, Json],
    census: Json,
    exclusions: Sequence[Json],
    invariants: Mapping[str, Json],
) -> Json:
    """The verdict box: status and level, completeness, census, exclusions and the headline
    numbers. ``clean`` is true only when the report is verified at L2 or L3, complete, has no
    census gap, leaves nothing out and no invariant fails (11.7); ``reasons`` says why not."""
    complete = bool(groups) and all(
        g["unmeasured"]["conformance"] == "complete" for g in groups.values()
    )
    reasons = []
    if trust["status"] != "verified":
        reasons.append(f"trust status {trust['status']}")
    if trust["level"] not in ("L2", "L3"):
        reasons.append(f"trust level {trust['level']}")
    if not complete:
        reasons.append("incomplete" if groups else "incomplete: no group has metrics")
    missing = sum(r["missing_count"] for r in census["robots"])
    if missing:
        reasons.append(f"census: {missing} session counter(s) missing")
    if exclusions:
        reasons.append(f"{len(exclusions)} session(s) left out of the metrics")
    reasons += [
        f"invariant {name} failed" for name, i in invariants.items() if i["status"] == "fail"
    ]
    return {
        "status": trust["status"],
        "level": trust["level"],
        "completeness": "complete" if complete else "incomplete",
        "census": census["status"],
        "exclusions": len(exclusions),
        "clean": not reasons,
        "reasons": reasons,
        "headline": _headline(groups),
    }


def build_report(inputs: ReportInputs, rules: Rules, options: Options) -> Json:
    """The report of definitions.md 11 as a JSON-ready dict (NumPy numbers already converted,
    non-finite floats as ``None``). Deterministic: the same inputs give the same dict."""
    stations = _station_keys(inputs)
    robots = [t for t in inputs.timelines if t.role != "station"]
    facts = {_key(t): session_facts(t, rules) for t in robots}
    metrics = metrics_report(robots, rules, options)
    groups: Mapping[str, Json] = metrics["groups"]
    grouped: dict[str, list[SessionFacts]] = {}
    for key in sorted(facts):
        f = facts[key]
        if f.timeline.start_ns is not None and f.simulated is not None:
            grouped.setdefault(GROUPS[f.simulated], []).append(f)
    in_metrics = {_key(f) for members in grouped.values() for f in members}

    l3 = _l3_keys(inputs)
    trust = trust_.trust_section(
        inputs.verification,
        inputs.trusted_keys,
        inputs.trusted_tsa_roots,
        inputs.anchor_files,
        inputs.station_verification,
        l3,
        stations,
    )
    census = census_.census(
        (s.robot_id, s.session_id)
        for s in inputs.log_check.sessions
        if (s.robot_id, s.session_id) not in stations
    )
    exclusions = trust_.exclusions(
        _all_keys(inputs),
        ((s.robot_id, s.session_id) for s in inputs.log_check.sessions),
        in_metrics,
        inputs.registry_problems,
        stations,
    )
    checks = invariants_.invariants(
        inputs.log_check,
        inputs.verification,
        inputs.timelines,
        census["status"],
        groups,
        inputs.witness,
    )
    report: Json = {
        "title": inputs.title,
        "about": inputs.about,
        "verdict": verdict(trust, groups, census, exclusions, checks),
        "provenance": provenance(inputs, rules, options),
        "trust": trust,
        "witness": witness_section(inputs.witness),
        "census": census,
        "exclusions": exclusions,
        "metrics": metrics,
        "statements": statements_.statements(groups, DEPENDS_ON, rules.report),
        "invariants": checks,
        "sessions": sessions_table(inputs, facts, in_metrics, l3),
        "figures": figure_data(grouped, groups, options.alpha),
    }
    cleaned: Json = clean(report)
    return cleaned
