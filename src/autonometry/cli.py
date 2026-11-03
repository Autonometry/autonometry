"""Command line interface: ``autonometry``."""

import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from autonometry import __version__
from autonometry.events import LogCheck
from autonometry.reader import LogReadError, read_logs
from autonometry.spec import SpecError, available_versions, known_version, spec_file_hashes

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

_SHOWN_PER_CODE = 20
_VERBOSE_FORMAT = "%(levelname)s %(name)s: %(message)s"


class _OneLineFormatter(logging.Formatter):
    """``warning: message``: the first line of the message only, never exception text."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage().splitlines() or [""]
        return f"{record.levelname.lower()}: {message[0]}"


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
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"autonometry {__version__}")
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
        typer.echo(version)


@spec_app.command("show")
def spec_show(
    version: Annotated[str, typer.Argument(help="Spec version, for example 0.2.")],
) -> None:
    """Show the files of one spec version with their SHA-256 hashes."""
    try:
        hashes = spec_file_hashes(version)
    except SpecError as exc:
        _fail(str(exc))
    for name, digest in hashes.items():
        typer.echo(f"{digest}  {name}")


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
            "--spec", help="Spec version, for example 0.2. Default: the newest one shipped."
        ),
    ] = None,
) -> None:
    """Check event logs: reject unreadable lines and flag structural problems.

    Exit status: 0 when no line was rejected (flags are warnings), 1 when any line was rejected,
    2 on bad usage. Event contents are never printed.
    """
    try:
        version = known_version(spec) if spec is not None else available_versions()[-1]
    except SpecError as exc:
        raise typer.BadParameter(str(exc), param_hint="'--spec'") from None
    except IndexError:
        _fail("no specification is shipped with this package")
    try:
        result = read_logs(paths, version)
    except (SpecError, LogReadError) as exc:
        _fail(str(exc))
    if as_json:
        typer.echo(json.dumps(result.to_json(), indent=2, sort_keys=True))
    else:
        for line in summary_lines(result):
            typer.echo(line)
    if result.errors:
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


def main() -> None:
    """Console entry point: configure logging, run the CLI, turn any crash into one line, exit 1."""
    _configure_logging()
    try:
        app()
    except Exception as exc:  # process boundary: never show a traceback unless --verbose
        logger.debug("unexpected failure", exc_info=True)
        typer.echo(f"error: unexpected failure ({type(exc).__name__})", err=True)
        sys.exit(1)
