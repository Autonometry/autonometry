"""Command line interface: ``autonometry``."""

import logging
import sys
from typing import Annotated, NoReturn

import typer

from autonometry import __version__
from autonometry.spec import SpecError, available_versions, spec_file_hashes

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


def main() -> None:
    """Console entry point: run the CLI and turn any unexpected failure into one line, exit 1."""
    try:
        app()
    except Exception as exc:  # process boundary: never show a traceback to the user
        logger.error("unexpected failure: %s", exc)
        logger.debug("traceback", exc_info=True)
        typer.echo(f"error: unexpected failure ({type(exc).__name__})", err=True)
        sys.exit(1)
