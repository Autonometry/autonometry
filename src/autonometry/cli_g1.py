"""``autonometry g1``: list a Unitree G1's DDS participants, or watch who commands it (README,
"Unitree G1"). Cyclone DDS 0.10 and psutil are imported only when a command runs; both commands
only subscribe."""

import json
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from autonometry.connectors import g1
from autonometry.connectors.g1_dds import (
    MAX_DOMAIN,
    G1Error,
    Observer,
    TaskFollower,
    check_cyclonedds,
    cyclonedds_version,
    discover,
    domain_config,
    format_discovery,
    open_backend,
    recorder_sender,
    run_watch,
)
from autonometry.connectors.tb4 import Device, devices_from_registry
from autonometry.recorder.config import ConfigError, RecorderConfig, load_config, read_input
from autonometry.registry import check_registry_bytes
from autonometry.spec import SpecError, available_versions, load_rules

logger = logging.getLogger(__name__)

g1_app = typer.Typer(
    no_args_is_help=True,
    help="Unitree G1 connector: list the robot's DDS participants, or watch who commands it.",
)

_EXTRA_HINT = "install the g1 extra: pip install 'autonometry[g1]'"
_SCOPES = frozenset({g1.LEGS, g1.UPPER_BODY, g1.HANDS})

Interface = Annotated[
    str | None,
    typer.Option("--interface", help="Network interface on the robot's network, for example eth0."),
]
Domain = Annotated[
    int, typer.Option("--domain", min=0, max=MAX_DOMAIN, help="DDS domain id (the G1 uses 0).")
]


def _fail(message: str, code: int = 2) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def _cyclonedds() -> None:
    """Refuse anything but Cyclone DDS 0.10 (S11), and run without psutil only with a hint."""
    try:
        check_cyclonedds(cyclonedds_version())
    except G1Error as exc:
        _fail(str(exc))


def _need_psutil() -> None:
    try:
        __import__("psutil")
    except ImportError:
        _fail(f"psutil is not installed; {_EXTRA_HINT}")


@g1_app.command("discover")
def discover_cmd(
    interface: Interface = None,
    domain: Domain = 0,
    seconds: Annotated[
        float, typer.Option("--seconds", min=0.5, max=600.0, help="How long to listen.")
    ] = 5.0,
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """List every DDS participant on the robot's network with its host, program and pid, the
    topics and type names it publishes, and the exact match strings for the device registry
    (README, "Unitree G1"). Read-only: it creates no writer and sends no request.

    Exit status: 0 after listening, 2 when it cannot start.
    """
    _cyclonedds()
    _need_psutil()
    try:
        domain_config(interface)
        backend = open_backend(domain, interface)
    except G1Error as exc:
        _fail(str(exc))
    report = discover(backend, seconds)
    if json_out:
        typer.echo(json.dumps(report, indent=2, sort_keys=True))
        return
    for line in format_discovery(report):
        typer.echo(line)
    if not report["participants"]:
        typer.echo("no participant found: check --interface and --domain")


def _devices(config: RecorderConfig, version: str) -> tuple[Device, ...]:
    """The device registry the config names, checked against its schema and for sources two
    devices of different kind or location could share (notes section 4)."""
    try:
        data = read_input(config.device_registry)
    except ConfigError as exc:
        _fail(str(exc))
    check = check_registry_bytes(data, str(config.device_registry), version, "devices")
    if check.document is None or check.sha256 is None:
        _fail(f"device registry {config.device_registry} is not valid: {check.problems[0]}")
    devices = devices_from_registry(check.document)
    problems = g1.registry_problems(devices)
    if problems:
        _fail(f"device registry {config.device_registry} is ambiguous: {problems[0]}")
    return devices


def _settings(config: RecorderConfig) -> g1.Settings:
    scopes = set(config.scopes)
    if not {g1.LEGS, g1.UPPER_BODY} <= scopes or not scopes <= _SCOPES:
        _fail("the G1 watcher reports the scopes legs and upper_body, and hands if declared")
    try:
        version = available_versions()[-1]
        rules = load_rules(version)
    except (SpecError, IndexError):
        _fail("no specification is shipped with this package")
    return g1.Settings.from_params(
        rules.params, _devices(config, version), hands=g1.HANDS in scopes
    )


@g1_app.command("watch")
def watch_cmd(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            exists=True,
            dir_okay=False,
            help="The recorder's config (TOML): socket, scopes, data folder and registries.",
            show_default=False,
        ),
    ],
    interface: Interface = None,
    domain: Domain = 0,
    client_name: Annotated[
        str, typer.Option("--client-name", help="Name this connector gives the recorder.")
    ] = "g1_watch",
) -> None:
    """Watch a Unitree G1's DDS traffic and send the control state of legs, upper_body and
    hands to the recorder (Linux and macOS; README, "Unitree G1"). Subscribe-only: it creates no
    writer and sends no request to the robot, not even a query. Tasks are read from the
    recorder's current session log.

    Exit status: 0 after SIGINT or SIGTERM, 2 when it cannot start.
    """
    _cyclonedds()
    try:
        recorder_config = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    if recorder_config.role != "robot":
        _fail("the config is a station's; the watcher feeds a robot's recorder")
    settings = _settings(recorder_config)
    try:
        domain_config(interface)
    except G1Error as exc:
        _fail(str(exc))
    if sys.platform == "win32":
        _fail("the recorder and its socket run on Linux and macOS only")
    _need_psutil()
    _watch(recorder_config, settings, interface, domain, client_name)


def _watch(  # pragma: no cover -- needs Cyclone DDS: the dds tests run it
    config: RecorderConfig,
    settings: g1.Settings,
    interface: str | None,
    domain: int,
    client_name: str,
) -> None:
    from autonometry.connectors.autonometry_client import Recorder  # noqa: PLC0415 -- POSIX use

    backend = open_backend(domain, interface)
    tasks = TaskFollower(config.data_dir / config.robot_id)
    stop = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stop.set())
    logger.info("watching DDS domain %d for robot %s", domain, config.robot_id)
    with Recorder(str(config.socket_path), client_name) as recorder:
        try:
            engine = g1.G1Engine(settings)
            run_watch(Observer(backend), tasks, engine, recorder_sender(recorder), stop)
        finally:
            tasks.close()
