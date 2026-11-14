"""``autonometry lerobot``: follow a LeRobot rollout's log live, or import a LeRobotDataset after
the fact (README, "LeRobot"). The ``lerobot`` extra (pyarrow, watchfiles) is imported only when a
command runs."""

import logging
import re
import signal
import sys
import threading
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from autonometry.connectors.lerobot import LeRobotEngine, Settings, task_definition
from autonometry.connectors.lerobot_io import (
    LeRobotError,
    Registries,
    import_dataset,
    load_registries,
    recorder_sender,
    watch_log,
)
from autonometry.recorder.config import (
    DEVICES_COPY,
    PLAN_COPY,
    TASKS_COPY,
    ConfigError,
    RecorderConfig,
    load_config,
)
from autonometry.spec import SpecError, available_versions, known_version, load_rules
from autonometry.timestamps import ts_to_ns

logger = logging.getLogger(__name__)

lerobot_app = typer.Typer(
    no_args_is_help=True,
    help="LeRobot (SO-101) connector: follow a rollout log, or import a LeRobotDataset.",
)

_EXTRA_HINT = "install the lerobot extra: pip install 'autonometry[lerobot]'"
_START_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z")
_SHOWN = 10


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def _need(module: str, code: int) -> None:
    """Fail with one line when an extra's package is missing."""
    try:
        __import__(module)
    except ImportError:
        _fail(f"{module} is not installed; {_EXTRA_HINT}", code)


def _version(spec: str | None, code: int) -> str:
    try:
        return known_version(spec) if spec is not None else available_versions()[-1]
    except (SpecError, IndexError) as exc:
        _fail(str(exc) or "no specification is shipped with this package", code)


def _start_ns(text: str | None) -> int | None:
    """``--start`` as nanoseconds: RFC 3339 in UTC with ``Z``, fraction optional."""
    if text is None:
        return None
    if _START_RE.fullmatch(text) is None:
        _fail("--start must be an RFC 3339 UTC time such as 2026-10-19T08:10:24Z")
    stamp = text if "." in text else f"{text[:-1]}.000Z"
    try:
        return ts_to_ns(stamp)
    except ValueError:
        _fail("--start is not a valid time")


def _registries(folder: Path, version: str, code: int) -> Registries:
    try:
        return load_registries(
            folder / DEVICES_COPY, folder / TASKS_COPY, folder / PLAN_COPY, version
        )
    except LeRobotError as exc:
        _fail(str(exc), code)


@lerobot_app.command("import")
def import_cmd(  # noqa: PLR0913, PLR0917 -- matches the import CLI's documented options
    dataset: Annotated[
        Path,
        typer.Argument(
            help="LeRobotDataset v3.0 folder (meta/info.json, meta/episodes/, data/).",
            exists=True,
            file_okay=False,
            show_default=False,
        ),
    ],
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            file_okay=False,
            help="Folder for the logs: <out>/<robot_id>/<session_id>/events.jsonl.",
            show_default=False,
        ),
    ],
    registry: Annotated[
        Path,
        typer.Option(
            "--registry",
            exists=True,
            file_okay=False,
            help="Folder with devices.json, tasks.json and plan.json, copied beside each log.",
            show_default=False,
        ),
    ],
    robot_id: Annotated[
        str | None,
        typer.Option("--robot-id", help="Robot id (default: the dataset's robot_type)."),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option(
            "--start",
            help="UTC start of the first episode, episodes back to back (default: each data "
            "file's modification time is the end of its last episode).",
        ),
    ] = None,
    policy: Annotated[
        str | None,
        typer.Option(
            "--policy", help="The rollout's --policy.path, matched as node policy:<path>."
        ),
    ] = None,
    teleop: Annotated[
        str | None,
        typer.Option(
            "--teleop", help="The rollout's --teleop.type, matched as node teleop:<type>."
        ),
    ] = None,
    simulated: Annotated[
        bool, typer.Option("--simulated", help="The episodes ran in a simulator.")
    ] = False,
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Import a LeRobotDataset v3.0 recorded by DAgger: one session log per episode, from its
    per-frame intervention labels (definitions.md 8; README, "LeRobot").

    Times are rebuilt as start + frame_index / fps and marked x_ts_reconstructed, so the logs are
    trust level L1 at most. Pauses, resets and the time between episodes were never recorded.
    Exit status: 0 when the logs were written, 1 on an error.
    """
    _need("pyarrow", 1)
    version = _version(spec, 1)
    rules = load_rules(version)
    registries = _registries(registry, version, 1)
    try:
        result = import_dataset(
            dataset,
            out,
            registries,
            heartbeat_interval_s=rules.params.heartbeat_interval_s,
            robot_id=robot_id,
            start_ns=_start_ns(start),
            policy=policy,
            teleop=teleop,
            simulated=simulated,
        )
    except LeRobotError as exc:
        _fail(str(exc))
    if result.missing_data:
        shown = ", ".join(str(i) for i in result.missing_data[:_SHOWN])
        more = "" if len(result.missing_data) <= _SHOWN else ", ..."
        logger.warning(
            "%d episode(s) have no data file and were not imported: %s%s",
            len(result.missing_data),
            shown,
            more,
        )
    if result.undeclared_tasks:
        logger.warning(
            "%d task text(s) are not in the task registry; their tasks will be flagged "
            "unknown_task_definition",
            len(result.undeclared_tasks),
        )
    typer.echo(f"wrote {len(result.sessions)} session log(s) under {out}")


def _task(registries: Registries, task: str | None) -> str:
    """The task definition of the rollout's episodes: ``--task`` through the task registry, or
    the registry's only task."""
    if task is not None:
        definition, known = task_definition(task, registries.tasks)
        if not known:
            logger.warning(
                "--task is not in the task registry; tasks will be flagged unknown_task_definition"
            )
        return definition
    declared = registries.tasks["tasks"]
    if len(declared) != 1:
        _fail("the task registry declares several tasks: name the rollout's one with --task", 2)
    return str(declared[0]["task_definition_id"])


@lerobot_app.command("watch")
def watch_cmd(  # noqa: PLR0913, PLR0917 -- matches the watch CLI's documented options
    log: Annotated[
        Path,
        typer.Option(
            "--log",
            dir_okay=False,
            help="The rollout's log file (lerobot-rollout ... 2>&1 | tee FILE).",
            show_default=False,
        ),
    ],
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            exists=True,
            dir_okay=False,
            help="The recorder's config (TOML): socket, scopes and registries.",
            show_default=False,
        ),
    ],
    task: Annotated[
        str | None,
        typer.Option("--task", help="The rollout's --dataset.single_task, or a task id."),
    ] = None,
    policy: Annotated[
        str | None,
        typer.Option("--policy", help="The policy path, when the log does not show it."),
    ] = None,
    client_name: Annotated[
        str, typer.Option("--client-name", help="Name this connector gives the recorder.")
    ] = "lerobot_watch",
    from_start: Annotated[
        bool,
        typer.Option("--from-start", help="Read a log that already exists from its first line."),
    ] = False,
) -> None:
    """Follow a lerobot-rollout log and send the robot's control state and tasks to the recorder
    (Linux and macOS; README, "LeRobot"). Read-only: it never touches the robot or its process.

    A log that exists when the watcher starts is followed from its end unless --from-start.
    Exit status: 0 after SIGINT or SIGTERM, 2 when it cannot start.
    """
    if sys.platform == "win32":
        _fail("the recorder and its socket run on Linux and macOS only", 2)
    _need("watchfiles", 2)
    from autonometry.connectors.autonometry_client import Recorder  # noqa: PLC0415 -- POSIX use

    try:
        recorder_config = load_config(config)
    except ConfigError as exc:
        _fail(str(exc), 2)
    if recorder_config.role != "robot":
        _fail("the config is a station's; the watcher feeds a robot's recorder", 2)
    if not log.parent.is_dir():
        _fail(f"{log.parent} is not a folder", 2)
    version = _version(None, 2)
    registries = _recorder_registries(recorder_config, version)
    settings = Settings(
        devices=registries.device_list(),
        idle_threshold_s=load_rules(version).params.idle_threshold_s,
        task_definition_id=_task(registries, task),
        scopes=recorder_config.scopes,
        policy_path=policy,
        reset_plan_item_id=registries.reset_plan_item(),
    )
    stop = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stop.set())
    with Recorder(str(recorder_config.socket_path), client_name) as recorder:
        watch_log(
            log, LeRobotEngine(settings), recorder_sender(recorder), stop, from_start=from_start
        )


def _recorder_registries(recorder_config: RecorderConfig, version: str) -> Registries:
    """The registries a recorder config names, or exit 2 with one line."""
    try:
        return load_registries(
            recorder_config.device_registry,
            recorder_config.task_registry,
            recorder_config.plan,
            version,
        )
    except LeRobotError as exc:
        _fail(str(exc), 2)
