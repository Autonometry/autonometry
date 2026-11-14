"""LeRobot input and output (README, "LeRobot"): read a LeRobotDataset v3.0 and write one session
log per episode; follow a rollout's log for the live engine.

The edge of :mod:`autonometry.connectors.lerobot`, which makes every decision: this module reads
files and the clock and hands over what it read. pyarrow and watchfiles, the ``lerobot`` extra,
are imported inside the functions that need them, so the CLI starts without them. Dataset paths
and column names are LeRobot v0.6.1's (commit ``7e241bd630a3719a56157a497ce5d08f244784f1``);
the comments cite its files.
"""

import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import IO, Any, Final

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autonometry import strictjson
from autonometry.connectors.lerobot import (
    Episode,
    ImportSettings,
    LeRobotEngine,
    Message,
    controller_id,
    episode_events,
    frame_ns,
    parse_line,
    task_definition,
    wire_event,
)
from autonometry.connectors.tb4 import Device, devices_from_registry
from autonometry.fileio import atomic_write, make_private_dirs
from autonometry.recorder.config import (
    DEVICES_COPY,
    PLAN_COPY,
    TASKS_COPY,
    ConfigError,
    read_input,
)
from autonometry.registry import check_registry_bytes
from autonometry.spec import RegistryKind

__all__ = [
    "DATA_PATH",
    "DatasetInfo",
    "EpisodeMeta",
    "ImportResult",
    "LeRobotError",
    "LogFollower",
    "Registries",
    "import_dataset",
    "load_registries",
    "pump",
    "read_episode_meta",
    "read_info",
    "recorder_sender",
    "watch_log",
]

logger = logging.getLogger(__name__)

#: The data file template of LeRobotDataset v3.0 (lerobot/datasets/utils.py:99, 104); a dataset
#: that names another is refused rather than formatted, since a format string from a file could
#: reach any attribute.
DATA_PATH: Final = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
#: Where the per-episode metadata lives (lerobot/datasets/utils.py:103).
EPISODES_DIR: Final = "meta/episodes"
INFO_PATH: Final = "meta/info.json"
#: The dataset format this importer reads (lerobot/datasets/dataset_metadata.py:60).
CODEBASE_VERSION: Final = "v3.0"
#: Columns read from ``meta/episodes/chunk-*/file-*.parquet``.
EPISODE_COLUMNS: Final = (
    "episode_index",
    "tasks",
    "length",
    "data/chunk_index",
    "data/file_index",
)
#: Columns read from ``data/chunk-*/file-*.parquet``: the mandatory frame columns
#: (lerobot/datasets/dataset_tools.py:370) and DAgger's ``intervention`` (rollout/context.py:422).
DATA_COLUMNS: Final = ("episode_index", "frame_index", "intervention")
#: Largest metadata file and data file read, in bytes (LeRobot's own data files stop at 100 MB,
#: lerobot/datasets/utils.py:89).
MAX_META_BYTES: Final = 64 << 20
MAX_DATA_BYTES: Final = 1 << 30
#: Longest log line kept, in bytes; longer lines are dropped (rules.toml ``max_line_bytes``).
MAX_LINE_BYTES: Final = 65536
#: Most bytes read from the log per call, so one pass stays short.
MAX_READ_BYTES: Final = 8 << 20
#: How often the watcher re-asserts the state, seconds; the recorder needs it every second.
TICK_S: Final = 0.25

_CHUNK_RE = re.compile(r"chunk-[0-9]{3,6}", re.ASCII)
_FILE_RE = re.compile(r"file-[0-9]{3,6}\.parquet", re.ASCII)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)


class LeRobotError(Exception):
    """An input cannot be used; the message is one line and never quotes file contents."""


# -- registries ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Registries:
    """The device registry, task registry and plan of a session, checked against their schemas
    (definitions.md 8, Registries): the documents, their hashes for ``session_start``, and the
    exact bytes to copy into each session folder."""

    devices: Mapping[str, Any]
    tasks: Mapping[str, Any]
    plan: Mapping[str, Any]
    device_registry_hash: str
    task_registry_hash: str
    plan_hash: str
    copies: Mapping[str, bytes]

    def device_list(self) -> tuple[Device, ...]:
        """The devices in order, the first match winning (6.10)."""
        return devices_from_registry(self.devices)

    def reset_plan_item(self) -> str | None:
        """The first plan item of kind ``reset``, ``None`` when the plan has none."""
        return next(
            (str(i["plan_item_id"]) for i in self.plan["items"] if i["kind"] == "reset"), None
        )


def _registry(path: Path, kind: RegistryKind, version: str) -> tuple[Mapping[str, Any], str, bytes]:
    try:
        data = read_input(path)
    except ConfigError as exc:
        raise LeRobotError(str(exc)) from None
    check = check_registry_bytes(data, str(path), version, kind)
    if check.sha256 is None or check.document is None:
        raise LeRobotError(f"{path} is not a valid {kind} registry: {check.problems[0]}")
    return check.document, check.sha256, data


def load_registries(devices: Path, tasks: Path, plan: Path, version: str) -> Registries:
    """Read and check the three registry files (definitions.md 1, 3, 6.10, 8).

    Raises:
        LeRobotError: a file cannot be read or is not valid; the message is one line.
    """
    device_doc, device_hash, device_bytes = _registry(devices, "devices", version)
    task_doc, task_hash, task_bytes = _registry(tasks, "tasks", version)
    plan_doc, plan_hash, plan_bytes = _registry(plan, "plan", version)
    copies = {DEVICES_COPY: device_bytes, TASKS_COPY: task_bytes, PLAN_COPY: plan_bytes}
    return Registries(device_doc, task_doc, plan_doc, device_hash, task_hash, plan_hash, copies)


# -- dataset ------------------------------------------------------------------------------------


class _Info(BaseModel):
    """The fields of ``meta/info.json`` the importer uses (lerobot/datasets/utils.py:128-148)."""

    model_config = ConfigDict(extra="ignore")

    codebase_version: str
    fps: float = Field(gt=0)
    robot_type: str | None = None
    data_path: str
    features: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    """What the importer takes from ``meta/info.json``: frames per second and the robot type."""

    fps: Fraction
    robot_type: str | None


@dataclass(frozen=True, slots=True)
class EpisodeMeta:
    """One row of ``meta/episodes``: the episode, its first task, its frame count and its data
    file relative to the dataset root."""

    index: int
    task: str | None
    length: int
    data_file: str


def _read_capped(path: Path, limit: int) -> bytes:
    try:
        with open(path, "rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise LeRobotError(f"cannot read {path}: {exc.strerror or exc}") from None
    if len(data) > limit:
        raise LeRobotError(f"{path} is larger than {limit} bytes")
    return data


def read_info(root: Path) -> DatasetInfo:
    """Read ``meta/info.json`` of a LeRobotDataset v3.0 that has DAgger's ``intervention``
    feature (bool, shape [1]; lerobot/rollout/context.py:422).

    Raises:
        LeRobotError: no such file, not JSON, not v3.0, another data path, no intervention.
    """
    path = root / INFO_PATH
    data = _read_capped(path, MAX_META_BYTES)
    try:
        info = _Info.model_validate(strictjson.parse(data.decode("utf-8")))
    except (UnicodeDecodeError, strictjson.StrictJsonError) as exc:
        raise LeRobotError(f"{path} is not valid JSON ({type(exc).__name__})") from None
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "info"
        raise LeRobotError(f"{path}: {where}: {first['msg']}") from None
    if info.codebase_version != CODEBASE_VERSION:
        raise LeRobotError(f"{path}: codebase_version must be {CODEBASE_VERSION}")
    if info.data_path != DATA_PATH:
        raise LeRobotError(f"{path}: data_path is not LeRobot's default {DATA_PATH}")
    if not math.isfinite(info.fps):
        raise LeRobotError(f"{path}: fps must be a finite number")
    if info.features.get("intervention", {}).get("dtype") != "bool":
        raise LeRobotError(
            f"{path}: no boolean intervention feature (only DAgger rollouts record one)"
        )
    return DatasetInfo(Fraction(str(info.fps)), info.robot_type)


def _parquet(path: Path, columns: Sequence[str], limit: int) -> dict[str, list[Any]]:
    """Columns of a parquet file as Python lists; one-line errors for anything unreadable."""
    import pyarrow as pa  # noqa: PLC0415 -- the lerobot extra
    import pyarrow.parquet as pq  # noqa: PLC0415

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise LeRobotError(f"cannot read {path}: {exc.strerror or exc}") from None
    if size > limit:
        raise LeRobotError(f"{path} is larger than {limit} bytes")
    try:
        schema = pq.read_schema(path)
        missing = [name for name in columns if name not in schema.names]
        if missing:
            raise LeRobotError(f"{path}: no column {missing[0]!r}")
        table = pq.read_table(path, columns=list(columns))
    except (pa.ArrowException, OSError) as exc:
        raise LeRobotError(
            f"{path} is not a readable parquet file ({type(exc).__name__})"
        ) from None
    return {name: table.column(name).to_pylist() for name in columns}


def _index(value: Any, what: str, path: Path) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LeRobotError(f"{path}: {what} must be a non-negative integer")
    return value


def _task_text(value: Any) -> str | None:
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    return value if isinstance(value, str) else None


def read_episode_meta(root: Path) -> list[EpisodeMeta]:
    """Every episode ``meta/episodes/chunk-*/file-*.parquet`` lists, in index order
    (lerobot/datasets/utils.py:103).

    Raises:
        LeRobotError: no episode file, an unreadable one, a bad value or a repeated index.
    """
    folder = root / EPISODES_DIR
    files = sorted(
        path
        for chunk in (folder.iterdir() if folder.is_dir() else ())
        if _CHUNK_RE.fullmatch(chunk.name) and chunk.is_dir()
        for path in chunk.iterdir()
        if _FILE_RE.fullmatch(path.name) and path.is_file()
    )
    if not files:
        raise LeRobotError(f"{folder} holds no chunk-*/file-*.parquet")
    episodes: dict[int, EpisodeMeta] = {}
    for path in files:
        columns = _parquet(path, EPISODE_COLUMNS, MAX_META_BYTES)
        for row in zip(*(columns[name] for name in EPISODE_COLUMNS), strict=True):
            index, tasks, length, chunk, file = row
            meta = EpisodeMeta(
                _index(index, "episode_index", path),
                _task_text(tasks),
                _index(length, "length", path),
                DATA_PATH.format(
                    chunk_index=_index(chunk, "data/chunk_index", path),
                    file_index=_index(file, "data/file_index", path),
                ),
            )
            if meta.index in episodes:
                raise LeRobotError(f"{path}: episode {meta.index} is listed twice")
            episodes[meta.index] = meta
    return [episodes[index] for index in sorted(episodes)]


def _label(value: Any) -> bool | None:
    """A frame's ``intervention``: a boolean, or a list holding one (shape [1])."""
    if isinstance(value, bool):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], bool):
        return value[0]
    return None


def _frames(root: Path, data_file: str) -> dict[int, list[tuple[int, bool | None]]]:
    """(frame_index, label) of every row of one data file, by episode."""
    path = root / data_file
    columns = _parquet(path, DATA_COLUMNS, MAX_DATA_BYTES)
    by_episode: dict[int, list[tuple[int, bool | None]]] = {}
    for episode, frame, label in zip(*(columns[name] for name in DATA_COLUMNS), strict=True):
        by_episode.setdefault(_index(episode, "episode_index", path), []).append(
            (_index(frame, "frame_index", path), _label(label))
        )
    return by_episode


def _episode(meta: EpisodeMeta, rows: Sequence[tuple[int, bool | None]]) -> Episode:
    ordered = sorted(rows, key=lambda row: row[0])
    return Episode(
        meta.index,
        meta.task,
        meta.length,
        tuple(frame for frame, _ in ordered),
        tuple(label for _, label in ordered),
    )


# -- import -------------------------------------------------------------------------------------


@dataclass(slots=True)
class ImportResult:
    """What an import wrote: session folders, episodes without a data file, and task texts that
    the task registry does not declare."""

    sessions: list[Path] = field(default_factory=list)
    missing_data: list[int] = field(default_factory=list)
    undeclared_tasks: list[str] = field(default_factory=list)


def _robot_id(robot_type: str | None) -> str:
    text = controller_id(robot_type or "lerobot_robot")
    return text if _ID_RE.fullmatch(text) else f"r{text}"[:128]


def _starts(
    metas: Sequence[EpisodeMeta], fps: Fraction, start_ns: int | None, mtimes: Mapping[str, int]
) -> dict[int, int]:
    """Each episode's start, back to back in index order: from ``start_ns``, or per data file
    ending at the file's modification time (the file is written after its last episode)."""
    starts: dict[int, int] = {}
    if start_ns is not None:
        frames = 0
        for meta in metas:
            starts[meta.index] = frame_ns(start_ns, frames, fps)
            frames += meta.length
        return starts
    for data_file, mtime in mtimes.items():
        group = [meta for meta in metas if meta.data_file == data_file]
        total = sum(meta.length for meta in group)
        origin = mtime - frame_ns(0, total, fps)
        frames = 0
        for meta in group:
            starts[meta.index] = frame_ns(origin, frames, fps)
            frames += meta.length
    return starts


def _write_session(
    out: Path, events: Sequence[Mapping[str, Any]], copies: Mapping[str, bytes]
) -> Path:
    first = events[0]
    folder = out / str(first["robot_id"]) / str(first["session_id"])
    make_private_dirs(folder)
    atomic_write(folder / "events.jsonl", b"".join(rfc8785.dumps(e) + b"\n" for e in events))
    for name, data in copies.items():
        atomic_write(folder / name, data)
    return folder


def import_dataset(  # noqa: PLR0913 -- the import CLI's documented options
    root: Path,
    out: Path,
    registries: Registries,
    *,
    heartbeat_interval_s: float,
    robot_id: str | None = None,
    start_ns: int | None = None,
    policy: str | None = None,
    teleop: str | None = None,
    simulated: bool = False,
) -> ImportResult:
    """Import a LeRobotDataset v3.0 as one session log per episode under
    ``<out>/<robot_id>/<session_id>/events.jsonl``, with the registries copied beside each
    (definitions.md 8; module docstring of :mod:`autonometry.connectors.lerobot`).

    Raises:
        LeRobotError: the dataset or an option cannot be used; the message is one line.
    """
    info = read_info(root)
    robot = robot_id if robot_id is not None else _robot_id(info.robot_type)
    if not _ID_RE.fullmatch(robot):
        raise LeRobotError("robot id must be 1 to 128 letters, digits, '.', '_', ':' or '-'")
    metas = read_episode_meta(root)
    present = sorted({m.data_file for m in metas if (root / m.data_file).is_file()})
    mtimes = {name: (root / name).stat().st_mtime_ns for name in present}
    starts = _starts(metas, info.fps, start_ns, mtimes)
    settings = ImportSettings(
        robot_id=robot,
        fps=info.fps,
        task_registry_hash=registries.task_registry_hash,
        plan_hash=registries.plan_hash,
        device_registry_hash=registries.device_registry_hash,
        heartbeat_interval_s=heartbeat_interval_s,
        devices=registries.device_list(),
        policy_path=policy,
        teleop=teleop,
        simulated=simulated,
        anchor="start" if start_ns is not None else "file_mtime",
    )
    result = ImportResult()
    out_root = out.resolve()
    for data_file in present:
        frames = _frames(root, data_file)
        for meta in (m for m in metas if m.data_file == data_file):
            definition, known = task_definition(meta.task, registries.tasks)
            if not known and meta.task is not None and meta.task not in result.undeclared_tasks:
                result.undeclared_tasks.append(meta.task)
            episode = _episode(meta, frames.get(meta.index, ()))
            events = episode_events(settings, episode, starts[meta.index], definition)
            result.sessions.append(_write_session(out_root, events, registries.copies))
    result.missing_data = [m.index for m in metas if m.data_file not in present]
    result.sessions.sort()
    return result


# -- live ---------------------------------------------------------------------------------------


class LogFollower:
    """Reads the complete lines appended to a text file, like ``tail -F``: a file that exists
    when following starts is read from its end unless ``from_start``; a file created later, cut
    short or replaced is read from its start. Lines longer than :data:`MAX_LINE_BYTES` are
    dropped; bytes that are not UTF-8 become U+FFFD."""

    def __init__(self, path: Path, from_start: bool = False) -> None:
        self._path = path
        self._handle: IO[bytes] | None = None
        self._identity: tuple[int, int] | None = None
        self._position = 0
        self._buffer = b""
        self._skipping = False
        #: The file and its size when following started: what is skipped unless ``from_start``.
        self._skip: tuple[tuple[int, int], int] | None = None
        if not from_start:
            try:
                status = os.stat(path)
            except OSError:
                return
            self._skip = ((status.st_dev, status.st_ino), status.st_size)

    def close(self) -> None:
        """Close the file; a later read opens it again from its start."""
        if self._handle is not None:
            self._handle.close()
        self._handle = None
        self._buffer, self._skipping = b"", False

    def read(self) -> list[str]:
        """The complete lines written since the last call, without line endings."""
        try:
            status = os.stat(self._path)
        except OSError:
            self.close()
            self._skip = None
            return []
        identity = (status.st_dev, status.st_ino)
        if self._handle is None or identity != self._identity or status.st_size < self._position:
            self.close()
            try:
                self._handle = open(self._path, "rb")  # noqa: SIM115 -- kept open between reads
            except OSError:
                return []
            skip, self._skip = self._skip, None
            same = skip is not None and skip[0] == identity and skip[1] <= status.st_size
            self._identity = identity
            self._position = skip[1] if same and skip is not None else 0
            self._handle.seek(self._position)
        data = self._handle.read(MAX_READ_BYTES)
        self._position += len(data)
        return self._split(data)

    def _split(self, data: bytes) -> list[str]:
        lines: list[str] = []
        *complete, rest = (self._buffer + data).split(b"\n")
        for raw in complete:
            if self._skipping:
                self._skipping = False
            elif len(raw) <= MAX_LINE_BYTES:
                lines.append(raw.decode("utf-8", errors="replace").rstrip("\r"))
        if len(rest) > MAX_LINE_BYTES:
            rest, self._skipping = b"", True
        self._buffer = rest
        return lines


Sender = Callable[[Iterable[Message]], None]


def recorder_sender(recorder: Any) -> Sender:
    """Send messages through the recorder client (``autonometry_client.Recorder``), which never
    blocks or raises."""

    def send(messages: Iterable[Message]) -> None:
        for message in messages:
            event = wire_event(message)
            recorder.event(event.pop("type"), **event)

    return send


def pump(follower: LogFollower, engine: LeRobotEngine, send: Sender, now: float) -> None:
    """One pass: every new line to the engine, then the current state (seconds, ``now``)."""
    for line in follower.read():
        parsed = parse_line(line)
        if parsed is not None:
            send(engine.observe(now, parsed))
    send(engine.tick(now))


def watch_log(  # noqa: PLR0913 -- the watch loop's collaborators
    path: Path,
    engine: LeRobotEngine,
    send: Sender,
    stop: threading.Event,
    *,
    from_start: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Follow ``path`` until ``stop`` is set: wake on each write to its folder and at least every
    :data:`TICK_S` seconds, pass new lines to the engine and send what it returns. A failure in
    one pass is logged and the next pass runs (the process boundary of CLAUDE.md). On the way
    out every scope is sent ``unknown``."""
    import watchfiles  # noqa: PLC0415 -- the lerobot extra

    follower = LogFollower(path, from_start)
    try:
        for _ in watchfiles.watch(
            path.parent,
            watch_filter=None,
            debounce=100,
            step=50,
            stop_event=stop,
            rust_timeout=round(TICK_S * 1000),
            yield_on_timeout=True,
            raise_interrupt=False,
            recursive=False,
        ):
            try:
                pump(follower, engine, send, clock())
            except Exception:  # process boundary: keep following
                logger.warning("a pass over the rollout log failed; continuing", exc_info=True)
    finally:
        follower.close()
        send(engine.stop())
