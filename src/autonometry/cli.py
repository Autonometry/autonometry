"""Command line interface: ``autonometry``."""

import contextlib
import enum
import hashlib
import io
import json
import logging
import sys
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn, cast

import typer
from cryptography import x509

from autonometry import __version__, demo, keys, sealfile, signednote, tsa
from autonometry import verify as verify_
from autonometry.cli_g1 import g1_app
from autonometry.cli_lerobot import lerobot_app
from autonometry.display import echo, printable
from autonometry.events import LogCheck, Session, TrustLevel, TrustStatus, VerifyResult
from autonometry.fileio import PUBLIC_FILE_MODE, append_durably, atomic_write, make_private_dirs
from autonometry.reader import LogReadError, read_logs, read_streams
from autonometry.registry import (
    LAYOUTS,
    RegistryCheck,
    RegistryReadError,
    check_registry,
    hash_problem,
)
from autonometry.sealfile import SealFile
from autonometry.signednote import SignedNoteError, VerifierKey
from autonometry.spec import (
    RegistryKind,
    Rules,
    SpecError,
    available_versions,
    known_version,
    load_rules,
    spec_file_hashes,
)
from autonometry.timeline import Registries, StateTotals, Timeline, build_timeline
from autonometry.witness import Reconciliation, WitnessParams, reconcile

if TYPE_CHECKING:
    from autonometry.stats import metrics

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="autonometry",
    help="Measure how much of a robot's work is done by its AI and how much by humans.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
spec_app = typer.Typer(
    help="Inspect the specification shipped with this package.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(spec_app, name="spec")
registry_app = typer.Typer(
    help="Check device registries, task registries and plans (definitions.md 1, 3, 6.10, 8).",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(registry_app, name="registry")
app.add_typer(lerobot_app, name="lerobot")
app.add_typer(g1_app, name="g1")

_SHOWN_PER_CODE = 20
_VERBOSE_FORMAT = "%(levelname)s %(name)s: %(message)s"


class _OneLineFormatter(logging.Formatter):
    """``warning: message``: the first line of the message only, never exception text."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage().splitlines() or [""]
        return f"{record.levelname.lower()}: {printable(message[0])}"


def _configure_logging() -> None:
    """Send warnings and errors to stderr as one clean line each; called once, by ``main``."""
    handler = logging.StreamHandler()
    handler.setFormatter(_OneLineFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)


def _set_verbose(value: bool) -> None:
    """``--verbose``: debug messages and full exception text on the handlers ``main`` set up."""
    if not value:
        return
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in root.handlers:
        if isinstance(handler.formatter, _OneLineFormatter):
            handler.setFormatter(logging.Formatter(_VERBOSE_FORMAT))


def _fail(message: str) -> NoReturn:
    echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def _print_version(value: bool) -> None:
    if value:
        echo(f"autonometry {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show the version."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose", "-v", callback=_set_verbose, help="Show debug output and full errors."
        ),
    ] = False,
) -> None:
    """Measure how much of a robot's work is done by its AI and how much by humans."""


@spec_app.command("list")
def spec_list() -> None:
    """List the spec versions shipped with this package."""
    try:
        versions = available_versions()
    except SpecError as exc:
        _fail(str(exc))
    for version in versions:
        echo(version)


@spec_app.command("show")
def spec_show(
    version: Annotated[str, typer.Argument(help="Spec version, for example 0.3.")],
) -> None:
    """Show the files of one spec version with their SHA-256 hashes."""
    try:
        hashes = spec_file_hashes(version)
    except SpecError as exc:
        _fail(str(exc))
    for name, digest in hashes.items():
        echo(f"{digest}  {name}")


def _spec_version(spec: str | None) -> str:
    """Resolve ``--spec`` to a concrete, shipped spec version; the newest one when ``None``."""
    try:
        return known_version(spec) if spec is not None else available_versions()[-1]
    except SpecError as exc:
        raise typer.BadParameter(str(exc), param_hint="'--spec'") from None
    except IndexError:
        _fail("no specification is shipped with this package")


@app.command("validate")
def validate(
    paths: Annotated[
        list[Path],
        typer.Argument(
            help="JSON Lines event log files.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON instead of a summary.")
    ] = False,
    spec: Annotated[
        str | None,
        typer.Option(
            "--spec", help="Spec version, for example 0.3. Default: the newest one shipped."
        ),
    ] = None,
) -> None:
    """Check event logs: reject unreadable lines and flag structural problems.

    Exit status: 0 when no line was rejected (flags are warnings), 1 when any line was rejected,
    2 on bad usage. Event contents are never printed.
    """
    version = _spec_version(spec)
    try:
        result = read_logs(paths, version)
    except (SpecError, LogReadError) as exc:
        _fail(str(exc))
    if as_json:
        echo(json.dumps(result.to_json(), indent=2, sort_keys=True))
    else:
        for line in summary_lines(result):
            echo(line)
    if result.errors:
        raise typer.Exit(1)


_ENTRY_NAMES: Mapping[RegistryKind, str] = {
    "devices": "device(s)",
    "tasks": "task(s)",
    "plan": "plan item(s)",
}


def _registry_kind(kind: str) -> RegistryKind:
    if kind not in LAYOUTS:
        raise typer.BadParameter("must be 'devices', 'tasks' or 'plan'", param_hint="'--kind'")
    return cast(RegistryKind, kind)


@registry_app.command("check")
def registry_check(
    path: Annotated[
        Path,
        typer.Argument(
            help="Registry JSON file.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    kind: Annotated[
        str, typer.Option("--kind", help="Kind of registry: devices, tasks or plan.")
    ] = "devices",
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON instead of a summary.")
    ] = False,
    spec: Annotated[
        str | None,
        typer.Option(
            "--spec", help="Spec version, for example 0.3. Default: the newest one shipped."
        ),
    ] = None,
) -> None:
    """Check a device registry, task registry or plan and print its hash for session_start
    (device_registry_hash, task_registry_hash or plan_hash).

    Exit status: 0 when the registry is valid, 1 when it is not (or cannot be read), 2 on bad
    usage. Values from the file are never printed.
    """
    registry_kind = _registry_kind(kind)
    version = _spec_version(spec)
    try:
        result = check_registry(path, version, registry_kind)
    except (SpecError, RegistryReadError) as exc:
        _fail(str(exc))
    if as_json:
        echo(json.dumps(result.to_json(), indent=2, sort_keys=True))
    elif result.sha256 is not None:
        echo(f"spec {version}: {result.entries} {_ENTRY_NAMES[registry_kind]}, valid")
        echo(f"sha256 {result.sha256}")
    else:
        echo(f"spec {version}: NOT valid")
        for problem in result.problems:
            echo(f"  {problem}")
    if not result.valid:
        raise typer.Exit(1)


def summary_lines(result: LogCheck) -> list[str]:
    """Return the human summary of a check: totals, then errors and flags grouped by code.

    Each group shows its exact count, the first 20 items and how many more there are, also when
    the result lists only some of the rejected lines.
    """
    counts = result.counts
    lines = [
        f"spec {result.spec_version}: {counts.files} file(s), {counts.lines} line(s), "
        f"{counts.events} event(s) in {counts.sessions} session(s), "
        f"{counts.blank_lines} blank, {counts.rejected_lines} rejected, {len(result.flags)} flag(s)"
    ]
    lines += _grouped(
        "rejected lines",
        result.errors,
        counts.errors_by_code,
        code=lambda e: e.code,
        describe=lambda e: f"{e.file}:{e.line}: {e.message}",
    )
    lines += _grouped(
        "flags (warnings)",
        result.flags,
        counts.flags_by_code,
        code=lambda f: f.code,
        describe=lambda f: f"robot {f.robot_id} session {f.session_id} seq {f.seq}: {f.detail}",
    )
    return lines


def _grouped[Item](
    title: str,
    items: Sequence[Item],
    totals: Mapping[str, int],
    code: Callable[[Item], str],
    describe: Callable[[Item], str],
) -> list[str]:
    if not items:
        return []
    by_code: dict[str, list[Item]] = {}
    for item in items:
        by_code.setdefault(code(item), []).append(item)
    lines = [f"{title}:"]
    for name in sorted(by_code):
        shown = by_code[name][:_SHOWN_PER_CODE]
        total = totals[name]
        lines.append(f"  {name} ({total})")
        lines += [f"    {describe(item)}" for item in shown]
        if total > len(shown):
            lines.append(f"    ... and {total - len(shown)} more")
    return lines


def _registries_for(
    session: Session,
    given: Mapping[RegistryKind, Path | None],
    version: str,
    cache: dict[tuple[str, RegistryKind], RegistryCheck],
    used: set[tuple[RegistryKind, Path]] | None = None,
) -> tuple[Registries, list[str]]:
    """The registries of one session (definitions.md 8, Registries): the files the user named,
    else the copies in the session folder; each must match its hash in ``session_start``.
    Returns the matching registries (``None`` for a missing one) and one line per problem; every
    file read is added to ``used``."""
    start = next((e for e in session.events if e["type"] == "session_start"), None)
    folder = Path(session.file).parent if session.file is not None else None
    documents: dict[RegistryKind, Mapping[str, Any] | None] = {}
    problems: list[str] = []
    for kind, layout in LAYOUTS.items():
        documents[kind] = None
        path = given[kind]
        if path is None and folder is not None and (folder / layout.copy_name).is_file():
            path = folder / layout.copy_name
        if path is None or start is None:
            continue
        if used is not None:
            used.add((kind, path))
        key = (str(path), kind)
        try:
            check = cache[key] if key in cache else check_registry(path, version, kind)
        except RegistryReadError as exc:
            problems.append(str(exc))
            continue
        cache[key] = check
        problem = hash_problem(check, cast(Mapping[str, Any], start).get(layout.hash_field))
        if problem is None:
            documents[kind] = check.document
        else:
            problems.append(problem)
    registries = Registries.from_documents(
        documents["devices"], documents["tasks"], documents["plan"]
    )
    return registries, problems


def _seconds(ns: float) -> str:
    return f"{ns / 1e9:.3f} s"


def _states_text(totals: StateTotals[int] | StateTotals[Fraction]) -> str:
    states = ", ".join(
        f"{state} {_seconds(float(value))}" for state, value in totals.states.items() if value
    )
    return (
        f"active {_seconds(float(totals.active_ns))}, idle {_seconds(float(totals.idle_ns))}, "
        f"unknown {_seconds(float(totals.unknown_ns))}" + (f"; {states}" if states else "")
    )


def _counts_text(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items()) or "none"


def timeline_lines(timeline: Timeline) -> list[str]:
    """Return the human summary of one session's timeline (docs/metrics-and-report.md,
    "Read a timeline")."""
    data = timeline.to_json()
    name = f"{timeline.robot_id}/{timeline.session_id}"
    if timeline.start_ns is None:
        return [f"{name}: no session_start, nothing observed"]
    closed = "closed" if timeline.session_end_seq is not None else "no session_end"
    lines = [
        f"{name}: observed {_seconds(timeline.observed_ns)} from {data['start']} ({closed})",
        f"  whole robot: {_states_text(timeline.whole_robot)}",
        f"  scope-weighted: {_states_text(timeline.scope_weighted)}",
    ]
    if len(timeline.scopes) > 1:
        lines += [
            f"  scope {scope}: {_states_text(totals)}"
            for scope, totals in timeline.per_scope.items()
        ]
    interventions = data["interventions"]
    stops = data["safety_stops"]
    lines += [
        f"  interventions: {interventions['per_scope']} per scope, "
        f"{interventions['whole_robot']} whole robot; by kind: "
        f"{_counts_text(interventions['by_kind'])}; by reason: "
        f"{_counts_text(interventions['by_reason'])}",
        f"  safety stops: {stops['per_scope']} per scope, {stops['whole_robot']} whole robot "
        f"({stops['human_triggered']} human-triggered, {stops['automatic']} automatic, "
        f"{stops['joined_by_takeover']} joined by a takeover)",
        f"  tasks: {len(timeline.tasks)} "
        f"({sum(1 for task in timeline.tasks if task.autonomous)} autonomous)",
    ]
    for task in timeline.tasks[:_SHOWN_PER_CODE]:
        known = "" if task.definition_known else ", definition not in the registry"
        lines.append(
            f"    {task.task_id} ({task.task_definition_id}{known}): {task.outcome}, "
            f"{'autonomous' if task.autonomous else 'not autonomous'}"
            f"{', contains unknown time' if task.contains_unknown else ''}, "
            f"human {_seconds(task.human_ns)}, {task.interventions} intervention(s)"
        )
    if len(timeline.tasks) > _SHOWN_PER_CODE:
        lines.append(f"    ... and {len(timeline.tasks) - _SHOWN_PER_CODE} more")
    supervision = timeline.supervision
    ratio = "n/a" if supervision.ratio is None else f"{supervision.ratio:.3f}"
    per_hour = supervision.supervisor_hours_per_robot_hour
    distance = "n/a" if timeline.distance_m is None else f"{timeline.distance_m:.3f} m"
    lines += [
        f"  resets: planned {_seconds(timeline.reset_planned_ns)}, "
        f"unplanned {_seconds(timeline.reset_unplanned_ns)}",
        f"  supervision: ratio {ratio}, supervisor-hours per robot-hour "
        f"{'n/a' if per_hour is None else f'{per_hour:.3f}'}",
        f"  humans: {timeline.distinct_human_actors} distinct, "
        f"{_seconds(timeline.human_actor_ns)} actor-time; supported "
        f"{_seconds(timeline.supported_ns)}; distance {distance}",
    ]
    return lines


@app.command("timeline")
def timeline_cmd(  # noqa: PLR0913, PLR0917 -- matches the timeline CLI's documented options
    logs: Annotated[
        list[Path],
        typer.Argument(
            help="JSON Lines event log files.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    devices: Annotated[
        Path | None,
        typer.Option(
            "--devices",
            help="Device registry for every session. Default: devices.json beside each log.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    tasks: Annotated[
        Path | None,
        typer.Option(
            "--tasks",
            help="Task registry for every session. Default: tasks.json beside each log.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    plan: Annotated[
        Path | None,
        typer.Option(
            "--plan",
            help="Plan for every session. Default: plan.json beside each log.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON instead of a summary.")
    ] = False,
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Build each session's timeline (definitions.md 1 to 8): time per state, interventions,
    safety stops, tasks, resets, supervision and flags.

    Registries come from --devices/--tasks/--plan or from devices.json, tasks.json and plan.json
    beside the log; each must match its hash in session_start. Exit status: 0; 1 when a registry
    does not match its hash (that session gets no timeline) or lines were rejected; 2 on bad
    usage. Identifiers from the log are printed; other event contents never are.
    """
    given: dict[RegistryKind, Path | None] = {"devices": devices, "tasks": tasks, "plan": plan}
    result, _, timelines, registry_errors = _build_timelines(logs, given, _spec_version(spec))
    if as_json:
        report = _timeline_report(result, timelines, registry_errors)
        echo(json.dumps(report, indent=2, sort_keys=True))
    else:
        for line in _timeline_summary(result, timelines, registry_errors):
            echo(line)
    if result.errors or registry_errors:
        raise typer.Exit(1)


def _build_timelines(
    logs: Sequence[Path],
    given: Mapping[RegistryKind, Path | None],
    version: str,
    used: set[tuple[RegistryKind, Path]] | None = None,
) -> tuple[LogCheck, Rules, list[Timeline], list[tuple[Session, str]]]:
    """Read the logs and build the timeline of every session whose registries match their
    hashes (definitions.md 8, Registries); the others are returned with their problems. Every
    registry file read is added to ``used``."""
    try:
        result = read_logs(logs, version)
        rules = load_rules(version)
    except (SpecError, LogReadError) as exc:
        _fail(str(exc))
    cache: dict[tuple[str, RegistryKind], RegistryCheck] = {}
    timelines: list[Timeline] = []
    registry_errors: list[tuple[Session, str]] = []
    for session in result.sessions:
        registries, problems = _registries_for(session, given, version, cache, used)
        if problems:
            registry_errors += [(session, problem) for problem in problems]
            continue
        own_flags = [
            f
            for f in result.flags
            if (f.robot_id, f.session_id) == (session.robot_id, session.session_id)
        ]
        timelines.append(build_timeline(session, registries, rules, own_flags))
    return result, rules, timelines, registry_errors


def _timeline_report(
    result: LogCheck, timelines: Sequence[Timeline], errors: Sequence[tuple[Session, str]]
) -> dict[str, Any]:
    return {
        "spec_version": result.spec_version,
        "counts": {
            "files": result.counts.files,
            "events": result.counts.events,
            "sessions": result.counts.sessions,
            "rejected_lines": result.counts.rejected_lines,
            "registry_errors": len(errors),
        },
        "rejected_lines": [
            {"file": e.file, "line": e.line, "code": e.code, "message": e.message}
            for e in result.errors
        ],
        "registry_errors": [
            {"robot_id": s.robot_id, "session_id": s.session_id, "problem": problem}
            for s, problem in errors
        ],
        "sessions": [timeline.to_json() for timeline in timelines],
    }


def _timeline_summary(
    result: LogCheck, timelines: Sequence[Timeline], errors: Sequence[tuple[Session, str]]
) -> list[str]:
    counts = result.counts
    lines = [
        f"spec {result.spec_version}: {counts.sessions} session(s), {counts.events} event(s), "
        f"{counts.rejected_lines} rejected line(s), {len(errors)} registry error(s)"
    ]
    for timeline in timelines:
        lines += timeline_lines(timeline)
    flags = [flag for timeline in timelines for flag in timeline.flags]
    lines += _grouped(
        "flags (warnings)",
        flags,
        Counter(flag.code for flag in flags),
        code=lambda f: f.code,
        describe=lambda f: f"robot {f.robot_id} session {f.session_id} seq {f.seq}: {f.detail}",
    )
    if errors:
        lines.append("registry errors:")
        lines += [f"  {s.robot_id}/{s.session_id}: {problem}" for s, problem in errors]
    lines += _grouped(
        "rejected lines",
        result.errors,
        counts.errors_by_code,
        code=lambda e: e.code,
        describe=lambda e: f"{e.file}:{e.line}: {e.message}",
    )
    return lines


class ClusterOption(enum.StrEnum):
    """``--cluster`` of ``autonometry metrics``."""

    auto = "auto"
    session = "session"
    robot = "robot"


class DriftBinOption(enum.StrEnum):
    """``--drift-bin`` of ``autonometry metrics``."""

    day = "day"
    week = "week"


def _alpha(value: float) -> float:
    if not 0 < value < 1:
        raise typer.BadParameter("must be between 0 and 1, both excluded")
    return value


@app.command("metrics")
def metrics_cmd(  # noqa: PLR0913, PLR0917 -- matches the metrics CLI's documented options
    logs: Annotated[
        list[Path],
        typer.Argument(
            help="JSON Lines event log files.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    alpha: Annotated[
        float,
        typer.Option(
            "--alpha", callback=_alpha, help="Two-sided level: 0.05 gives 95 % intervals."
        ),
    ] = 0.05,
    n_boot: Annotated[
        int, typer.Option("--n-boot", min=2, help="Bootstrap replicates per interval.")
    ] = 2000,
    seed: Annotated[int, typer.Option("--seed", min=0, help="Seed of every bootstrap.")] = 0,
    cluster: Annotated[
        ClusterOption,
        typer.Option(
            "--cluster",
            help="Cluster unit. auto: robots for the time share when there are 3 or more "
            "robots, sessions for everything else; session or robot: that unit everywhere.",
        ),
    ] = ClusterOption.auto,
    drift_bin: Annotated[
        DriftBinOption, typer.Option("--drift-bin", help="Calendar bin of the drift metric (UTC).")
    ] = DriftBinOption.day,
    out: Annotated[
        Path | None,
        typer.Option("--out", dir_okay=False, help="Write the JSON here instead of stdout."),
    ] = None,
    devices: Annotated[
        Path | None,
        typer.Option(
            "--devices",
            help="Device registry for every session. Default: devices.json beside each log.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    tasks: Annotated[
        Path | None,
        typer.Option(
            "--tasks",
            help="Task registry for every session. Default: tasks.json beside each log.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    plan: Annotated[
        Path | None,
        typer.Option(
            "--plan",
            help="Plan for every session. Default: plan.json beside each log.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Compute the metrics of the standard (definitions.md 3 to 7) with confidence intervals and
    unknown-time bounds, one block per group (real, simulated), as JSON.

    Registries are found as for ``autonometry timeline``. A session that gets no timeline (a
    registry does not match its hash) is left out and named on stderr. Exit status: 0; 1 when a
    session was left out or lines were rejected; 2 on bad usage or without the report extra.
    The same logs and options always give the same JSON.
    """
    try:
        from autonometry.stats import metrics, report  # noqa: PLC0415 -- optional extra
    except ImportError:
        echo(
            "error: autonometry metrics needs the report extra: pip install 'autonometry[report]'",
            err=True,
        )
        raise typer.Exit(2) from None
    given: dict[RegistryKind, Path | None] = {"devices": devices, "tasks": tasks, "plan": plan}
    result, rules, timelines, registry_errors = _build_timelines(logs, given, _spec_version(spec))
    options = metrics.Options(alpha, n_boot, seed, cluster.value, drift_bin.value)
    left_out = sorted({(s.robot_id, s.session_id) for s, _ in registry_errors})
    body = report.metrics_report(timelines, rules, options) | {
        "spec_version": result.spec_version,
        "counts": {
            "files": result.counts.files,
            "events": result.counts.events,
            "sessions": result.counts.sessions,
            "rejected_lines": result.counts.rejected_lines,
            "sessions_left_out": len(left_out),
        },
        "sessions_left_out": [
            {"robot_id": s.robot_id, "session_id": s.session_id, "problem": problem}
            for s, problem in registry_errors
        ],
    }
    text = report.to_json(body)
    for robot_id, session_id in left_out:
        echo(f"error: session {robot_id}/{session_id} left out: no timeline", err=True)
    for session, problem in registry_errors:
        echo(f"  {session.robot_id}/{session.session_id}: {problem}", err=True)
    if result.errors:
        echo(f"error: {result.counts.rejected_lines} rejected line(s)", err=True)
    if out is None:
        echo(text, nl=False)
    else:
        atomic_write(out, text.encode("utf-8"), PUBLIC_FILE_MODE)
    if result.errors or registry_errors:
        raise typer.Exit(1)


keys_app = typer.Typer(
    help="Manage Ed25519 signing keys for sealing.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(keys_app, name="keys")


@keys_app.command("generate")
def keys_generate(
    name: Annotated[str, typer.Option("--name", help="Key name, e.g. autonometry/<robot_id>.")],
    out: Annotated[
        Path, typer.Option("--out", help="Path for the private key; its vkey is written beside it.")
    ],
) -> None:
    """Generate an Ed25519 signing key and its vkey (definitions.md section 10.3)."""
    try:
        vkey_path = keys.generate(name, out)
    except (SignedNoteError, keys.SigningKeyError) as exc:
        _fail(str(exc))
    echo(f"private key: {out}")
    echo(f"vkey file:   {vkey_path}")
    echo(vkey_path.read_text(encoding="utf-8").strip())


def _key_name(key_path: Path) -> str:
    try:
        return keys.load_key_name(key_path)
    except keys.SigningKeyError as exc:
        _fail(str(exc))


@app.command("seal")
def seal(
    log: Annotated[
        Path,
        typer.Argument(
            help="Input JSON Lines event log.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    key: Annotated[
        Path,
        typer.Option(
            "--key", help="Ed25519 private key (PKCS8 PEM); its name is read from <key>.vkey."
        ),
    ],
    out: Annotated[Path, typer.Option("--out", help="Output sealed log.")],
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Seal an event log: hash chain, Merkle checkpoints, Ed25519 signatures (definitions.md 10).

    Refuses input with rejected lines, input that is already sealed, and existing outputs.
    """
    version = _spec_version(spec)
    try:
        log_check = read_logs([log], version)
    except (SpecError, LogReadError) as exc:
        _fail(str(exc))
    if log_check.errors:
        _fail(f"{log} has {log_check.counts.rejected_lines} rejected line(s); refusing to seal it")
    if any(
        event.get("hash") is not None or event.get("prev_hash") is not None
        for session in log_check.sessions
        for event in session.events
    ):
        _fail(
            f"{log} already carries hash/prev_hash fields; refusing to seal an already-sealed log"
        )
    checkpoints_out = sealfile.checkpoints_path(out)
    if out.exists() or checkpoints_out.exists():
        _fail(f"refusing to overwrite an existing output: {out} or {checkpoints_out}")

    try:
        private_key = keys.load(key)
    except keys.SigningKeyError as exc:
        _fail(str(exc))
    key_name = _key_name(key)
    rules = load_rules(version)

    sealed, checkpoint_lines = sealfile.seal_sessions(
        log_check.sessions, rules, key_name, private_key
    )
    atomic_write(out, sealed)
    atomic_write(checkpoints_out, checkpoint_lines)
    total_events = sum(len(session.events) for session in log_check.sessions)
    echo(f"sealed {total_events} event(s) in {len(log_check.sessions)} session(s)")
    echo(f"wrote {out} and {checkpoints_out}")


@app.command("timestamp")
def timestamp(
    checkpoints_file: Annotated[
        Path,
        typer.Argument(
            help="A <name>.checkpoints.jsonl file.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    tsa_url: Annotated[
        str, typer.Option("--tsa-url", help="RFC 3161 TSA URL (http:// or https://).")
    ],
    tsa_root: Annotated[
        Path, typer.Option("--tsa-root", help="Trusted TSA root certificate(s) (PEM).")
    ],
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Append a verified RFC 3161 timestamp record for each checkpoint that lacks one."""
    version = _spec_version(spec)
    rules = load_rules(version)
    roots = _load_tsa_roots([tsa_root])
    (seal_file,) = _read_seal_files([checkpoints_file], version, rules)
    if rejected := sum(seal_file.errors_by_code.values()):
        _fail(f"{checkpoints_file} has {rejected} rejected line(s); refusing")

    records = seal_file.records
    have = {r.note_sha256 for r in records if isinstance(r, sealfile.TimestampLine)}
    checkpoints = [r for r in records if isinstance(r, sealfile.CheckpointLine)]
    appended = failures = 0
    for checkpoint in checkpoints:
        note_bytes = checkpoint.note.encode("utf-8")
        note_sha256 = hashlib.sha256(note_bytes).hexdigest()
        if note_sha256 in have:
            continue
        try:
            tsr = tsa.request_timestamp(note_bytes, tsa_url, roots)
        except tsa.TsaError as exc:
            logger.warning("could not timestamp a checkpoint: %s", exc)
            failures += 1
            continue
        append_durably(checkpoints_file, sealfile.encode_timestamp_line(note_sha256, tsr))
        have.add(note_sha256)
        appended += 1
    echo(f"appended {appended} timestamp record(s); {failures} checkpoint(s) not timestamped")
    if failures:
        raise typer.Exit(1)


def _load_vkeys(paths: Sequence[Path]) -> list[VerifierKey]:
    out: list[VerifierKey] = []
    for path in paths:
        try:
            lines = keys.load_vkeys(path)
        except keys.SigningKeyError as exc:
            _fail(str(exc))
        for line in lines:
            try:
                out.append(signednote.parse_vkey(line))
            except SignedNoteError as exc:
                _fail(f"{path}: {exc}")
    return out


def _load_tsa_roots(paths: Sequence[Path]) -> list[x509.Certificate]:
    out: list[x509.Certificate] = []
    for path in paths:
        try:
            pem = path.read_bytes()
        except OSError as exc:
            _fail(f"cannot read {path}: {exc.strerror or exc}")
        try:
            out.extend(tsa.load_tsa_roots(pem))
        except tsa.TsaError as exc:
            _fail(f"{path}: {exc}")
    return out


def _read_seal_files(paths: Sequence[Path], version: str, rules: Rules) -> list[SealFile]:
    seal_files: list[SealFile] = []
    for path in paths:
        try:
            with open(path, "rb") as stream:
                seal_files.append(
                    sealfile.read_seal_file(
                        str(path),
                        stream,
                        rules.errors.max_line_bytes,
                        rules.errors.list_max_per_code,
                        version,
                    )
                )
        except OSError as exc:
            _fail(f"cannot read {path}: {exc.strerror or exc}")
    return seal_files


def verify_summary_lines(result: VerifyResult, require: TrustLevel) -> list[str]:
    """Return the human summary of a verification: one line per session with its evidence and
    gaps, then rejected lines and file problems grouped by code (the first 20 of each), and a
    final ``result:`` line."""
    lines = [f"spec {result.spec_version}: {len(result.sessions)} session(s)"]
    for verdict in result.sessions:
        through = (
            f" (sealed through seq {verdict.sealed_through_seq})"
            if verdict.sealed_through_seq is not None
            else ""
        )
        lines.append(f"  {verdict.robot_id}/{verdict.session_id}: {verdict.status}{through}")
        lines += [f"    evidence {i.code}: {i.detail}" for i in verdict.evidence]
        lines += [f"    gap {i.code}: {i.detail}" for i in verdict.gaps]
        if (warning := verdict.end_warning()) is not None:
            lines.append(f"    warning: {warning}")
    lines += _grouped(
        "rejected lines",
        result.rejected_lines,
        result.rejected_by_code,
        code=lambda e: e.code,
        describe=lambda e: f"{e.file}:{e.line}: {e.message}",
    )
    lines += _grouped(
        "file problems",
        result.file_problems,
        result.file_problems_by_code,
        code=lambda p: p.code,
        describe=lambda p: f"{p.file}:{p.line}: {p.detail}",
    )
    lines.append(_result_line(result, require))
    return lines


def _result_line(result: VerifyResult, require: TrustLevel) -> str:
    """The ``result:`` line. Only L2 is "verified" (definitions.md 10.6): at L1 the line says
    whether the level was met and, separately, why the log is not verified."""
    reasons, l2_reasons = result.reasons(require), result.reasons("L2")
    if require == "L2":
        return "result: verified" if not reasons else f"result: NOT verified: {'; '.join(reasons)}"
    if reasons:
        return f"result: does NOT meet {require}: {'; '.join(reasons)}"
    if not l2_reasons:
        return f"result: meets {require}; verified (L2)"
    return f"result: meets {require}; NOT verified at L2: {'; '.join(l2_reasons)}"


@app.command("verify")
def verify_cmd(  # noqa: PLR0913, PLR0917 -- matches the verify CLI's documented options
    logs: Annotated[
        list[Path],
        typer.Argument(
            help="JSON Lines event log files.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    vkey: Annotated[
        list[Path] | None,
        typer.Option("--vkey", help="File of trusted vkeys, one per line; repeatable."),
    ] = None,
    tsa_root: Annotated[
        list[Path] | None,
        typer.Option("--tsa-root", help="Trusted TSA root certificate (PEM); repeatable."),
    ] = None,
    anchor: Annotated[
        list[Path] | None,
        typer.Option(
            "--anchor", help="Anchor file, same format as a checkpoints file; repeatable."
        ),
    ] = None,
    require: Annotated[
        str,
        typer.Option("--require", help="Minimum trust level every session must meet: L1 or L2."),
    ] = "L2",
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON instead of a summary.")
    ] = False,
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Verify sealed logs (definitions.md section 10.6): recompute, never trust a stored value.

    Exit status: 0 only when every session meets ``--require`` (L1: not failed; L2: verified),
    no line of any file read (logs, checkpoint and anchor files) was rejected and no file has a
    problem; 1 otherwise; 2 on bad usage. Event contents are never printed.
    """
    if require not in ("L1", "L2"):
        raise typer.BadParameter("must be 'L1' or 'L2'", param_hint="'--require'")
    version = _spec_version(spec)
    rules = load_rules(version)
    try:
        log_check = read_logs(logs, version)
    except (SpecError, LogReadError) as exc:
        _fail(str(exc))

    trusted_keys = _load_vkeys(vkey or [])
    trusted_roots = _load_tsa_roots(tsa_root or [])
    checkpoint_paths = [
        p for log_path in logs if (p := sealfile.checkpoints_path(log_path)).exists()
    ]
    checkpoint_files = _read_seal_files(checkpoint_paths, version, rules)
    anchor_files = _read_seal_files(anchor or [], version, rules)
    level = cast(TrustLevel, require)
    try:
        result = verify_.verify_log(
            log_check, checkpoint_files, anchor_files, trusted_keys, trusted_roots, rules
        )
    except SpecError as exc:
        _fail(str(exc))

    if as_json:
        echo(json.dumps(result.to_json(level), indent=2, sort_keys=True))
    else:
        for line in verify_summary_lines(result, level):
            echo(line)
    if not result.verified(level):
        raise typer.Exit(1)


@dataclass(frozen=True, slots=True)
class _Side:
    """What one side of a reconciliation read (definitions.md 12): its checked logs, the
    timelines of the sessions whose registries match, the others with their problems, and the
    checkpoint files and registries it read."""

    log_check: LogCheck
    timelines: tuple[Timeline, ...]
    registry_errors: tuple[tuple[Session, str], ...]
    checkpoint_paths: tuple[Path, ...]
    checkpoint_files: tuple[SealFile, ...]
    registries: tuple[tuple[RegistryKind, Path], ...]

    def keys(self) -> set[tuple[str, str]]:
        return {(s.robot_id, s.session_id) for s in self.log_check.sessions}


def _read_side(
    logs: Sequence[Path], given: Mapping[RegistryKind, Path | None], version: str
) -> _Side:
    """Read and check one side's logs and checkpoint files (definitions.md 8, 10.4) and build
    the timelines of its sessions; stops with exit status 1 on input it cannot read."""
    used: set[tuple[RegistryKind, Path]] = set()
    log_check, rules, timelines, errors = _build_timelines(logs, given, version, used)
    checkpoint_paths = [p for log in logs if (p := sealfile.checkpoints_path(log)).exists()]
    return _Side(
        log_check,
        tuple(timelines),
        tuple(errors),
        tuple(checkpoint_paths),
        tuple(_read_seal_files(checkpoint_paths, version, rules)),
        tuple(sorted(used)),
    )


def _merged(first: LogCheck, second: LogCheck) -> LogCheck:
    """Two reads as one, for a verifier that must see every session an anchor file names."""
    codes = Counter(first.counts.errors_by_code) + Counter(second.counts.errors_by_code)
    return replace(
        first,
        sessions=first.sessions + second.sessions,
        errors=first.errors + second.errors,
        counts=replace(first.counts, errors_by_code=dict(sorted(codes.items()))),
    )


def _verify_sides(  # noqa: PLR0913, PLR0917 -- both sides and the verifier's trust roots
    robots: _Side,
    stations: _Side | None,
    anchor_files: Sequence[SealFile],
    trusted_keys: Sequence[VerifierKey],
    trusted_roots: Sequence[x509.Certificate],
    rules: Rules,
) -> tuple[VerifyResult, VerifyResult | None]:
    """Verify both sides in one pass (definitions.md 10.6), since one anchor file may hold the
    checkpoints of both, then split the sessions: the station's are those its logs hold. The
    rejected lines and file problems of every file read stay with the robot side, once."""
    log_check, files = robots.log_check, robots.checkpoint_files
    if stations is not None:
        log_check = _merged(log_check, stations.log_check)
        files += stations.checkpoint_files
    try:
        result = verify_.verify_log(
            log_check, files, anchor_files, trusted_keys, trusted_roots, rules
        )
    except SpecError as exc:
        _fail(str(exc))
    if stations is None:
        return result, None
    mine = stations.keys()
    robot = replace(
        result,
        sessions=tuple(v for v in result.sessions if (v.robot_id, v.session_id) not in mine),
    )
    station = replace(
        result,
        sessions=tuple(v for v in result.sessions if (v.robot_id, v.session_id) in mine),
        rejected_lines=(),
        rejected_by_code={},
        file_problems=(),
        file_problems_by_code={},
    )
    return robot, station


def _statuses(result: VerifyResult) -> dict[tuple[str, str], TrustStatus]:
    return {(v.robot_id, v.session_id): v.status for v in result.sessions}


def _rules(version: str) -> Rules:
    try:
        return load_rules(version)
    except SpecError as exc:
        _fail(str(exc))


def _side_files(prefix: str, logs: Sequence[Path], side: _Side) -> list[tuple[str, Path]]:
    """The input files of one side for the report's provenance (11.1), roles prefixed."""
    return [
        *((f"{prefix}log", p) for p in logs),
        *((f"{prefix}checkpoints", p) for p in side.checkpoint_paths),
        *((f"{prefix}registry_{kind}", p) for kind, p in side.registries),
    ]


def _registry_errors_json(role: str, side: _Side) -> list[dict[str, str]]:
    return [
        {"role": role, "robot_id": s.robot_id, "session_id": s.session_id, "problem": problem}
        for s, problem in side.registry_errors
    ]


def _reconcile_sides(
    robots: _Side,
    stations: _Side,
    robot_result: VerifyResult,
    station_result: VerifyResult,
    rules: Rules,
) -> Reconciliation:
    return reconcile(
        robots.timelines,
        stations.timelines,
        WitnessParams.from_rules(rules),
        _statuses(robot_result),
        _statuses(station_result),
    )


_NO_REGISTRY: Mapping[RegistryKind, Path | None] = {"devices": None, "tasks": None, "plan": None}


@app.command("reconcile")
def reconcile_cmd(  # noqa: PLR0913, PLR0917 -- matches the reconcile CLI's documented options
    robot: Annotated[
        list[Path],
        typer.Option(
            "--robot",
            help="Robot event log; repeatable.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    station: Annotated[
        list[Path],
        typer.Option(
            "--station",
            help="Teleoperation station event log (role station); repeatable.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    vkey: Annotated[
        list[Path] | None,
        typer.Option("--vkey", help="File of trusted vkeys, one per line; repeatable."),
    ] = None,
    tsa_root: Annotated[
        list[Path] | None,
        typer.Option("--tsa-root", help="Trusted TSA root certificate (PEM); repeatable."),
    ] = None,
    anchor: Annotated[
        list[Path] | None,
        typer.Option(
            "--anchor", help="Anchor file, same format as a checkpoints file; repeatable."
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", dir_okay=False, help="Write the JSON here instead of stdout."),
    ] = None,
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Reconcile robot logs with teleoperation station logs (definitions.md 12) and write JSON:
    per robot session the five times, the disputed share, tau, matched and unmatched episodes
    and whether it reaches trust level L3, with the reasons when it does not.

    Both sides are verified (section 10.6) with the same trusted keys, TSA roots and anchor
    files; registries are read from each log's folder. Exit status: 0 only when every robot
    session is L3 and every session's registries match; 1 otherwise; 2 on bad usage or input
    that cannot be read. No network access; actor ids never appear in the output.
    """
    with _input_errors_exit(2):
        version = _spec_version(spec)
        rules = _rules(version)
        trusted_keys = _load_vkeys(vkey or [])
        trusted_roots = _load_tsa_roots(tsa_root or [])
        anchors = _read_seal_files(anchor or [], version, rules)
        robots = _read_side(robot, _NO_REGISTRY, version)
        stations = _read_side(station, _NO_REGISTRY, version)
        robot_result, station_result = _verify_sides(
            robots, stations, anchors, trusted_keys, trusted_roots, rules
        )
    assert station_result is not None  # noqa: S101 -- _verify_sides was given stations
    result = _reconcile_sides(robots, stations, robot_result, station_result, rules)
    errors = _registry_errors_json("robot", robots) + _registry_errors_json("station", stations)
    body = {
        "spec_version": version,
        **result.to_json(),
        "registry_errors": errors,
        "verification": {
            "robot": {
                "verified": robot_result.verified("L2"),
                "reasons": robot_result.reasons("L2"),
            },
            "station": {
                "verified": station_result.verified("L2"),
                "reasons": station_result.reasons("L2"),
            },
        },
    }
    text = json.dumps(body, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if out is None:
        echo(text, nl=False)
    else:
        try:
            atomic_write(out, text.encode("utf-8"), PUBLIC_FILE_MODE)
        except OSError as exc:
            echo(f"error: cannot write {out}: {exc.strerror or exc}", err=True)
            raise typer.Exit(2) from None
        counts = result.to_json()["counts"]
        echo(
            f"reconciled {counts['robot_sessions']} robot session(s) with "
            f"{counts['station_sessions']} station session(s): {counts['l3']} at L3"
        )
    if errors or not result.all_l3:
        raise typer.Exit(1)


#: Limits on text the report shows as given (definitions.md 11; CLAUDE.md, input limits).
_TITLE_MAX_CHARS = 200
_ABOUT_MAX_BYTES = 65536
_HASH_CHUNK = 1 << 20


@contextmanager
def _input_errors_exit(code: int) -> Iterator[None]:
    """Run helpers that stop with exit status 1 on an input error (:func:`_fail`) under a
    command whose contract gives ``code`` for input and I/O errors."""
    try:
        yield
    except typer.Exit as exc:
        if exc.exit_code == 1:
            raise typer.Exit(code) from None
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            while chunk := stream.read(_HASH_CHUNK):
                digest.update(chunk)
    except OSError as exc:
        _fail(f"cannot read {path}: {exc.strerror or exc}")
    return digest.hexdigest()


def _has_control(text: str, allowed: str = "") -> bool:
    """True when ``text`` holds a control character (Unicode category Cc) not in ``allowed``."""
    return any(unicodedata.category(c) == "Cc" and c not in allowed for c in text)


def _title(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > _TITLE_MAX_CHARS or _has_control(value):
        raise typer.BadParameter(
            f"must be at most {_TITLE_MAX_CHARS} characters without control characters"
        )
    return value


def _read_about(path: Path) -> str:
    """The ``--about`` file as plain text: UTF-8, at most 64 KiB, line breaks and tabs the only
    control characters; shown escaped, never as markup."""
    try:
        with open(path, "rb") as stream:
            data = stream.read(_ABOUT_MAX_BYTES + 1)
    except OSError as exc:
        _fail(f"cannot read {path}: {exc.strerror or exc}")
    if len(data) > _ABOUT_MAX_BYTES:
        _fail(f"{path} is larger than {_ABOUT_MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8").removeprefix("﻿").replace("\r\n", "\n")
    except UnicodeDecodeError:
        _fail(f"{path} is not valid UTF-8")
    if _has_control(text, "\n\t"):
        _fail(f"{path} holds control characters; plain text only")
    return text


def _utc_now() -> str:
    """The current UTC time as RFC 3339 with a ``Z`` offset, to the second."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _input_files(roles: Sequence[tuple[str, Path]]) -> list[tuple[str, str, str]]:
    """``(role, path as given, SHA-256)`` of every input file, once each, sorted."""
    return [(role, str(path), _sha256_file(path)) for role, path in sorted(set(roles))]


def _require_report_extra(command: str) -> None:
    """Stop with one line and exit status 2 when the ``report`` extra is not installed."""
    try:
        from autonometry.report import build, html  # noqa: F401, PLC0415 -- optional extra
        from autonometry.stats import metrics, report  # noqa: F401, PLC0415 -- optional extra
    except ImportError:
        echo(
            f"error: autonometry {command} needs the report extra: "
            "pip install 'autonometry[report]'",
            err=True,
        )
        raise typer.Exit(2) from None


@dataclass(frozen=True, slots=True)
class _ReportJob:
    """The inputs of one report (definitions.md 11): logs, trust roots, registries given on the
    command line (``None`` reads the copy beside each log), station logs and the page's text."""

    logs: tuple[Path, ...]
    out: Path
    vkeys: tuple[Path, ...]
    tsa_roots: tuple[Path, ...]
    anchors: tuple[Path, ...]
    given: Mapping[RegistryKind, Path | None]
    stations: tuple[Path, ...]
    title: str | None
    about: Path | None
    version: str


def _write_report(job: _ReportJob, options: "metrics.Options") -> dict[str, Any]:
    """Verify, reconcile, build and write ``report.json`` and ``report.html`` into ``job.out``
    atomically (definitions.md 11, 12); returns the report. Input and write errors stop with
    exit status 2. Needs the ``report`` extra (:func:`_require_report_extra`)."""
    from autonometry.report import build, html  # noqa: PLC0415 -- optional extra
    from autonometry.stats import report as stats_report  # noqa: PLC0415 -- optional extra

    with _input_errors_exit(2):
        rules = _rules(job.version)
        trusted_keys = _load_vkeys(job.vkeys)
        trusted_roots = _load_tsa_roots(job.tsa_roots)
        anchor_files = _read_seal_files(job.anchors, job.version, rules)
        robots = _read_side(job.logs, job.given, job.version)
        stations = _read_side(job.stations, _NO_REGISTRY, job.version) if job.stations else None
        robot_result, station_result = _verify_sides(
            robots, stations, anchor_files, trusted_keys, trusted_roots, rules
        )
        about_text = None if job.about is None else _read_about(job.about)
        files = _input_files(
            [
                *_side_files("", job.logs, robots),
                *(("anchor", p) for p in job.anchors),
                *(("vkey", p) for p in job.vkeys),
                *(("tsa_root", p) for p in job.tsa_roots),
                *([] if job.about is None else [("about", job.about)]),
                *([] if stations is None else _side_files("station_", job.stations, stations)),
            ]
        )
    problems: dict[tuple[str, str], tuple[str, ...]] = {}
    for session, problem in robots.registry_errors:
        key = (session.robot_id, session.session_id)
        problems[key] = (*problems.get(key, ()), problem)
    inputs = build.ReportInputs(
        log_check=robots.log_check,
        verification=robot_result,
        timelines=robots.timelines,
        generated_at=_utc_now(),
        registry_problems=problems,
        files=tuple(build.InputFile(*row) for row in files),
        spec_files=spec_file_hashes(job.version),
        trusted_keys=len(trusted_keys),
        trusted_tsa_roots=len(trusted_roots),
        anchor_files=len(job.anchors),
        title=job.title,
        about=about_text,
        witness=None
        if stations is None or station_result is None
        else _reconcile_sides(robots, stations, robot_result, station_result, rules),
        station_verification=station_result,
    )
    body = build.build_report(inputs, rules, options)
    page = html.render_html(body)
    try:
        job.out.mkdir(parents=True, exist_ok=True)
        text = stats_report.to_json(body)
        atomic_write(job.out / "report.json", text.encode("utf-8"), PUBLIC_FILE_MODE)
        atomic_write(job.out / "report.html", page.encode("utf-8"), PUBLIC_FILE_MODE)
    except OSError as exc:
        echo(f"error: cannot write the report to {job.out}: {exc.strerror or exc}", err=True)
        raise typer.Exit(2) from None
    return body


@app.command("report")
def report_cmd(  # noqa: PLR0913, PLR0917 -- matches the report CLI's documented options
    logs: Annotated[
        list[Path],
        typer.Argument(
            help="JSON Lines event log files.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            file_okay=False,
            help="Folder for report.json and report.html; created if missing.",
            show_default=False,
        ),
    ],
    vkey: Annotated[
        list[Path] | None,
        typer.Option("--vkey", help="File of trusted vkeys, one per line; repeatable."),
    ] = None,
    tsa_root: Annotated[
        list[Path] | None,
        typer.Option("--tsa-root", help="Trusted TSA root certificate (PEM); repeatable."),
    ] = None,
    anchor: Annotated[
        list[Path] | None,
        typer.Option(
            "--anchor", help="Anchor file, same format as a checkpoints file; repeatable."
        ),
    ] = None,
    devices: Annotated[
        Path | None,
        typer.Option(
            "--devices",
            help="Device registry for every session. Default: devices.json beside each log.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    tasks: Annotated[
        Path | None,
        typer.Option(
            "--tasks",
            help="Task registry for every session. Default: tasks.json beside each log.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    plan: Annotated[
        Path | None,
        typer.Option(
            "--plan",
            help="Plan for every session. Default: plan.json beside each log.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    alpha: Annotated[
        float,
        typer.Option(
            "--alpha", callback=_alpha, help="Two-sided level: 0.05 gives 95 % intervals."
        ),
    ] = 0.05,
    n_boot: Annotated[
        int, typer.Option("--n-boot", min=2, help="Bootstrap replicates per interval.")
    ] = 2000,
    seed: Annotated[int, typer.Option("--seed", min=0, help="Seed of every bootstrap.")] = 0,
    cluster: Annotated[
        ClusterOption,
        typer.Option("--cluster", help="Cluster unit, as for autonometry metrics."),
    ] = ClusterOption.auto,
    drift_bin: Annotated[
        DriftBinOption, typer.Option("--drift-bin", help="Calendar bin of the drift metric (UTC).")
    ] = DriftBinOption.day,
    title: Annotated[
        str | None,
        typer.Option("--title", callback=_title, help="Title of the report (plain text)."),
    ] = None,
    about: Annotated[
        Path | None,
        typer.Option(
            "--about",
            help="Plain-text file describing the deployment, shown escaped.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    station: Annotated[
        list[Path] | None,
        typer.Option(
            "--station",
            help="Teleoperation station log (role station) witnessing the robots; repeatable.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Write a report a buyer, insurer or auditor can read (definitions.md 11): verification,
    census, exclusions, metrics per group with their uncertainty, statements and invariants.
    With ``--station`` logs (definitions.md 12), the robot sessions are reconciled with them:
    a session whose two witnesses agree is trust level L3, and a Witness section shows the
    discrepancies. Station logs are verified like the robot's, with the same trust roots.

    Writes DIR/report.json and DIR/report.html (self-contained: no scripts, no links to other
    sites) atomically. Exit status: 0 only when the report is verified at L2, complete, has no
    census gap and leaves no session out; 1 when the report was written but any of those fails;
    2 on bad usage, unreadable input or without the report extra. No network access; actor ids
    never appear in the report.
    """
    _require_report_extra("report")
    from autonometry.stats import metrics  # noqa: PLC0415 -- optional extra

    given: dict[RegistryKind, Path | None] = {"devices": devices, "tasks": tasks, "plan": plan}
    with _input_errors_exit(2):
        version = _spec_version(spec)
    body = _write_report(
        _ReportJob(
            logs=tuple(logs),
            out=out,
            vkeys=tuple(vkey or ()),
            tsa_roots=tuple(tsa_root or ()),
            anchors=tuple(anchor or ()),
            given=given,
            stations=tuple(station or ()),
            title=title,
            about=about,
            version=version,
        ),
        metrics.Options(alpha, n_boot, seed, cluster.value, drift_bin.value),
    )
    verdict = body["verdict"]
    echo(
        f"report: {verdict['status']}, level {verdict['level']}, {verdict['completeness']}; "
        f"census {verdict['census']}; {verdict['exclusions']} session(s) left out"
    )
    echo(f"wrote {out / 'report.json'} and {out / 'report.html'}")
    if not verdict["clean"]:
        echo(f"not clean: {'; '.join(verdict['reasons'])}")
        raise typer.Exit(1)


def _check_empty(out: Path) -> None:
    """Stop with exit status 2 unless ``out`` is missing or an empty folder."""
    try:
        empty = not out.exists() or (out.is_dir() and not any(out.iterdir()))
    except OSError as exc:
        echo(f"error: cannot read {printable(str(out))}: {exc.strerror or exc}", err=True)
        raise typer.Exit(2) from None
    if not empty:
        echo(f"error: {printable(str(out))} is not an empty folder; give a new one", err=True)
        raise typer.Exit(2)


@dataclass(frozen=True, slots=True)
class _DemoRun:
    """What the demo did: the session it read, its checkpoints, verification and report."""

    events: int
    checkpoints: int
    verification: VerifyResult
    report: dict[str, Any]


def _run_demo(options: "metrics.Options") -> _DemoRun:
    """The demo's steps, in the current folder (the output folder): copy the shipped session's
    registries, generate a throwaway key, seal, verify, write the report. Stops with exit
    status 1, before any metric, when the sealed session does not meet L1 (fail closed)."""
    version = demo.SPEC_VERSION
    rules = load_rules(version)
    raw = read_streams([("events.jsonl", io.BytesIO(demo.data("events.jsonl")))], version)
    make_private_dirs(demo.SESSION_DIR)
    for name in demo.REGISTRIES:
        atomic_write(demo.SESSION_DIR / name, demo.data(name))
    atomic_write(demo.ABOUT, demo.data("about.txt"), PUBLIC_FILE_MODE)
    try:
        keys.generate(demo.KEY_NAME, demo.KEY)
        private_key = keys.load(demo.KEY)
    except (SignedNoteError, keys.SigningKeyError) as exc:
        _fail(str(exc))
    sealed, checkpoints = sealfile.seal_sessions(raw.sessions, rules, demo.KEY_NAME, private_key)
    atomic_write(demo.LOG, sealed)
    atomic_write(demo.CHECKPOINTS, checkpoints)
    result = verify_.verify_log(
        read_logs([demo.LOG], version),
        _read_seal_files([demo.CHECKPOINTS], version, rules),
        [],
        _load_vkeys([demo.VKEY]),
        [],
        rules,
    )
    text = json.dumps(result.to_json("L1"), indent=2, sort_keys=True) + "\n"
    atomic_write(demo.VERIFICATION, text.encode("utf-8"), PUBLIC_FILE_MODE)
    if not result.verified("L1"):
        _fail(f"the sealed demo session does not meet L1: {'; '.join(result.reasons('L1'))}")
    report = _write_report(
        _ReportJob(
            logs=(demo.LOG,),
            out=Path(),
            vkeys=(demo.VKEY,),
            tsa_roots=(),
            anchors=(),
            given=_NO_REGISTRY,
            stations=(),
            title=demo.TITLE,
            about=demo.ABOUT,
            version=version,
        ),
        options,
    )
    return _DemoRun(
        events=sum(len(s.events) for s in raw.sessions),
        checkpoints=checkpoints.count(b"\n"),
        verification=result,
        report=report,
    )


@app.command("demo")
def demo_cmd(
    out: Annotated[
        Path,
        typer.Option("--out", help="New or empty folder for the demo's files.", show_default=False),
    ],
) -> None:
    """Run the whole chain on a session shipped with the package: no robot, no network.

    Generates a throwaway key in the folder, seals the session, verifies it (L1: no timestamp
    authority), computes the metrics and writes report.html and report.json. The same every
    run apart from the key, its signatures and the report's generated-at time. Exit status: 0
    when every step ran; 1 if the sealed session does not meet L1; 2 when the folder is not
    empty, a file cannot be written or the report extra is missing.
    """
    _require_report_extra("demo")
    from autonometry.stats import metrics  # noqa: PLC0415 -- optional extra

    _check_empty(out)
    try:
        make_private_dirs(out)
        with contextlib.chdir(out):
            run = _run_demo(metrics.Options(0.05, 2000, 0, "auto", "day"))
    except OSError as exc:
        echo(
            f"error: cannot write the demo to {printable(str(out))}: {exc.strerror or exc}",
            err=True,
        )
        raise typer.Exit(2) from None
    verdict = run.report["verdict"]
    echo(demo.TITLE)
    echo(f"1. session:  {run.events} events from the package ({demo.ROBOT_ID}, {demo.SUMMARY})")
    echo(f"2. key:      a throwaway Ed25519 key in {demo.KEY.parent.as_posix()}/")
    echo(f"3. sealed:   every event hash-chained, {run.checkpoints} signed checkpoint(s)")
    echo("4. verify:")
    for line in demo.why_l1(run.checkpoints):
        echo(f"  {line}")
    echo("5. metrics:  with 95 % confidence intervals (one session, so they are wide)")
    for line in demo.headline_lines(run.report):
        echo(f"  {line}")
    echo(f"6. report:   {verdict['status']}, level {verdict['level']}, {verdict['completeness']}")
    echo("  not clean, as expected without an anchor:")
    for reason in verdict["reasons"]:
        echo(f"    - {reason}")
    echo("")
    echo(f"Files in {printable(str(out))}:")
    for line in demo.file_lines():
        echo(line)
    echo("")
    echo(f"Open {printable(str(out / demo.REPORT_HTML))} in a browser.")
    echo("To redo the checks by hand, inside the folder:")
    for line in demo.next_steps():
        echo(line)


@app.command("record")
def record(
    config: Annotated[
        Path,
        typer.Option(
            "--config", help="Recorder config (TOML).", dir_okay=False, show_default=False
        ),
    ],
) -> None:
    """Record a robot's control events into a sealed, crash-safe log (Linux and macOS).

    Receives events from connectors on a local Unix datagram socket until SIGTERM or SIGINT.
    Exit status: 0 after a clean stop, 1 on a failure while recording (restarting continues the
    same session), 2 when it cannot start (bad config, key or registry; socket or folder in use).
    Operational messages go to stderr and never contain event contents.
    """
    if sys.platform == "win32":
        echo("error: the recorder runs on Linux and macOS only", err=True)
        raise typer.Exit(2)
    from autonometry.recorder.config import ConfigError, load_config, prepare  # noqa: PLC0415
    from autonometry.recorder.service import serve  # noqa: PLC0415 -- POSIX-only modules

    try:
        prepared = prepare(load_config(config))
    except ConfigError as exc:
        echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    recorder_logger = logging.getLogger("autonometry.recorder")
    if recorder_logger.getEffectiveLevel() > logging.INFO:
        recorder_logger.setLevel(logging.INFO)
    raise typer.Exit(serve(prepared))


def main() -> None:
    """Console entry point: configure logging, run the CLI, turn any crash into one line, exit 1."""
    _configure_logging()
    try:
        app()
    except Exception as exc:  # process boundary: never show a traceback unless --verbose
        logger.debug("unexpected failure", exc_info=True)
        echo(f"error: unexpected failure ({type(exc).__name__})", err=True)
        sys.exit(1)
