"""LeRobot input and output (README, "LeRobot"): read a LeRobotDataset v3.0 and write one session
log per episode; follow a rollout's log for the live engine.

The edge of :mod:`autonometry.connectors.lerobot`, which makes every decision: this module reads
files, the clock and the process table and hands over what it read. pyarrow, watchfiles and
psutil, the ``lerobot`` extra, are imported inside the functions that need them, so the CLI
starts without them. Dataset paths and column names are LeRobot v0.6.1's (commit
``7e241bd630a3719a56157a497ce5d08f244784f1``); the comments cite its files.
"""

import codecs
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path, PureWindowsPath
from typing import IO, Any, Final, Protocol

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autonometry import strictjson
from autonometry.connectors.lerobot import (
    Episode,
    ImportSettings,
    LeRobotEngine,
    Message,
    Note,
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
#: Longest log line kept, in characters; longer lines are dropped (rules.toml ``max_line_bytes``).
MAX_LINE_CHARS: Final = 65536
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


#: Byte-order marks a log may start with, and the encodings they name. Windows PowerShell 5.1
#: writes ``Tee-Object`` and ``>`` files as UTF-16 LE with a mark.
_BOMS: Final = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
_BOM_MAX: Final = max(len(bom) for bom, _ in _BOMS)


class LogFollower:
    """Reads the complete lines appended to a text file, like ``tail -F``: a file that exists
    when following starts is read from where it ended then unless ``from_start``; a file created
    later, cut short or replaced is read from its start. A byte-order mark at the start of the
    file chooses UTF-8, UTF-16 LE or UTF-16 BE; without one the file is read as UTF-8, and bytes
    that are not UTF-8 (a dash in a Windows code page) become U+FFFD. Lines longer than
    :data:`MAX_LINE_CHARS` are dropped."""

    def __init__(self, path: Path, from_start: bool = False) -> None:
        self._path = path
        self._handle: IO[bytes] | None = None
        self._identity: tuple[int, int] | None = None
        self._position = 0
        self._decoder: codecs.IncrementalDecoder | None = None
        self._buffer = ""
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
        self._decoder = None
        self._buffer, self._skipping = "", False

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
        handle = self._handle
        if self._decoder is None and not self._choose_decoder(handle):
            return []
        handle.seek(self._position)
        data = handle.read(MAX_READ_BYTES)
        self._position += len(data)
        assert self._decoder is not None  # noqa: S101 -- chosen above
        return self._split(self._decoder.decode(data))

    def _choose_decoder(self, handle: IO[bytes]) -> bool:
        """Pick the decoder from the file's first bytes; False while they could still become a
        byte-order mark."""
        handle.seek(0)
        head = handle.read(_BOM_MAX)
        if any(len(head) < len(bom) and bom.startswith(head) for bom, _ in _BOMS):
            return False
        encoding, mark = next(
            ((name, len(bom)) for bom, name in _BOMS if head.startswith(bom)), ("utf-8", 0)
        )
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self._position = max(self._position, mark)
        return True

    def _split(self, text: str) -> list[str]:
        lines: list[str] = []
        *complete, rest = (self._buffer + text).split("\n")
        for raw in complete:
            if self._skipping:
                self._skipping = False
            elif len(raw) <= MAX_LINE_CHARS:
                lines.append(raw.rstrip("\r"))
        if len(rest) > MAX_LINE_CHARS:
            rest, self._skipping = "", True
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


# -- the rollout's process ----------------------------------------------------------------------

#: The program ``lerobot-rollout`` (console script, Windows launcher) and its module.
ROLLOUT_NAMES: Final = frozenset({"lerobot-rollout", "lerobot_rollout"})
ROLLOUT_MODULE: Final = "lerobot.scripts.lerobot_rollout"
_PROGRAM_SUFFIXES: Final = frozenset({"", ".exe", ".py", ".pyw"})
_INTERPRETER = re.compile(r"(?:python[0-9.]*w?|pyw?)(?:\.exe)?", re.ASCII | re.IGNORECASE)
#: Python options followed by a separate value.
_OPTION_VALUES: Final = frozenset({"-W", "-X", "--check-hash-based-pycs"})
#: How often the rollout's process is checked, seconds.
PROCESS_CHECK_S: Final = 1.0
_MAX_DEPTH = 64


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """One process of this computer: its id, its parent's, its name and its command line."""

    pid: int
    ppid: int | None
    name: str
    cmdline: tuple[str, ...]


def _is_rollout_file(path: str) -> bool:
    """True for ``lerobot-rollout`` itself, its ``.exe`` or a ``.py`` script of that name; a file
    such as ``lerobot-rollout.log`` is not one."""
    name = PureWindowsPath(path).name.lower()
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    return stem in ROLLOUT_NAMES and (f".{suffix}" if suffix else "") in _PROGRAM_SUFFIXES


def is_rollout(info: ProcessInfo) -> bool:
    """True when the process runs ``lerobot-rollout``: its program has that name, or it is a
    Python interpreter whose script is that file or whose module (``-m``) is
    ``lerobot.scripts.lerobot_rollout``. Only the program and the script or module are looked at,
    never other arguments, so ``tail -F lerobot-rollout.log`` does not match."""
    argv = info.cmdline or (info.name,)
    if _is_rollout_file(argv[0]) or _is_rollout_file(info.name):
        return True
    return bool(_INTERPRETER.fullmatch(PureWindowsPath(argv[0]).name)) and _runs_rollout(argv[1:])


def _runs_rollout(python_args: Sequence[str]) -> bool:
    """True when a Python interpreter's arguments run the rollout as script or module."""
    args = iter(python_args)
    for arg in args:
        if arg == "-m":
            return next(args, "") == ROLLOUT_MODULE
        if arg.startswith("-m") and not arg.startswith("--"):
            return arg[2:] == ROLLOUT_MODULE
        if arg.startswith("-c") and not arg.startswith("--"):
            return False
        if arg in _OPTION_VALUES:
            next(args, None)
        elif not arg.startswith("-") or arg == "-":
            return _is_rollout_file(arg)
    return False


def rollout_pids(processes: Iterable[ProcessInfo], own_pid: int) -> list[int]:
    """The ``lerobot-rollout`` processes, one per rollout: a launcher and the Python it starts
    (the Windows ``.exe`` launcher, a virtual environment's ``python.exe``) are one rollout, of
    which the deepest is kept. Never this process."""
    table = list(processes)
    parents = {p.pid: p.ppid for p in table}
    matched = {p.pid for p in table if p.pid != own_pid and is_rollout(p)}
    ancestors: set[int] = set()
    for pid in matched:
        current, seen = parents.get(pid), {pid}
        while current is not None and current not in seen and len(seen) < _MAX_DEPTH:
            ancestors.add(current)
            seen.add(current)
            current = parents.get(current)
    return sorted(matched - ancestors)


class Watched(Protocol):
    """A process being watched, read-only."""

    def alive(self) -> bool:
        """False once it has ended or is a zombie."""
        ...


def list_processes() -> list[ProcessInfo]:
    """Every process psutil can read on this computer; a field it may not read is empty."""
    import psutil  # noqa: PLC0415 -- the lerobot extra

    found = []
    for process in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        info = process.info
        cmdline = tuple(str(arg) for arg in info.get("cmdline") or ())
        found.append(ProcessInfo(info["pid"], info.get("ppid"), info.get("name") or "", cmdline))
    return found


@dataclass(slots=True)
class _PsutilProcess:
    process: Any

    def alive(self) -> bool:
        import psutil  # noqa: PLC0415 -- the lerobot extra

        try:
            running = bool(self.process.is_running())
            return running and self.process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return bool(self.process.is_running())


def open_process(pid: int) -> Watched | None:
    """A read-only handle on process ``pid``, ``None`` when there is none. psutil's
    ``is_running`` also compares the creation time, so a reused pid is not mistaken for it.
    Nothing is ever sent to the process."""
    import psutil  # noqa: PLC0415 -- the lerobot extra

    try:
        return _PsutilProcess(psutil.Process(pid))
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return None


class Liveness:
    """Watches the rollout's process, read-only, so that a rollout that dies without a word
    does not leave its last state in place (definitions.md 2, 6.14). The process is ``pid`` when
    given, else the one :func:`rollout_pids` finds when the engine starts following a rollout.
    With none or several the rollout is unwatched: one warning, ``x_lerobot_liveness:
    unwatched`` on a heartbeat, and the log alone decides, as without this class."""

    def __init__(
        self,
        pid: int | None = None,
        *,
        lister: Callable[[], list[ProcessInfo]] = list_processes,
        opener: Callable[[int], Watched | None] = open_process,
        own_pid: int | None = None,
    ) -> None:
        self._pid = pid
        self._lister = lister
        self._opener = opener
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._generation = 0
        self._process: Watched | None = None
        self._checked: float | None = None

    def check(self, now: float, follower: LogFollower, engine: LeRobotEngine, send: Sender) -> None:
        """Find the process of a newly followed rollout; then, at most every
        :data:`PROCESS_CHECK_S` seconds, see whether it is still there. When it is gone, the
        lines left in the log are read first, then the engine is told, unless those lines ended
        the rollout."""
        if engine.following and engine.generation != self._generation:
            self._generation = engine.generation
            self._process = self._find(send)
            self._checked = now
        if self._process is None or not engine.following:
            return
        if self._checked is not None and now - self._checked < PROCESS_CHECK_S:
            return
        self._checked = now
        if self._process.alive():
            return
        self._process = None
        while lines := follower.read():
            _observe(lines, engine, send, now)
        if engine.following:
            logger.warning(
                "the rollout's process ended without logging 'Rollout finished'; "
                "its state is unknown from now on"
            )
            send(engine.process_gone(now))

    def _find(self, send: Sender) -> Watched | None:
        if self._pid is not None:
            process = self._opener(self._pid)
            if process is not None and process.alive():
                return process
            reason = f"no running process has --pid {self._pid}"
        else:
            pids = rollout_pids(self._lister(), self._own_pid)
            process = self._opener(pids[0]) if len(pids) == 1 else None
            if process is not None:
                return process
            if len(pids) > 1:
                reason = f"{len(pids)} lerobot-rollout processes here; name one with --pid"
            else:
                reason = "no lerobot-rollout process on this computer"
        logger.warning("%s: the rollout's process is not watched", reason)
        send([Note((("x_lerobot_liveness", "unwatched"),))])
        return None


def _observe(lines: Iterable[str], engine: LeRobotEngine, send: Sender, now: float) -> None:
    for line in lines:
        parsed = parse_line(line)
        if parsed is not None:
            send(engine.observe(now, parsed))


def pump(
    follower: LogFollower,
    engine: LeRobotEngine,
    send: Sender,
    now: float,
    liveness: Liveness | None = None,
) -> None:
    """One pass: every new line to the engine, the rollout's process checked, then the current
    state (seconds, ``now``)."""
    _observe(follower.read(), engine, send, now)
    if liveness is not None:
        liveness.check(now, follower, engine, send)
    send(engine.tick(now))


def watch_log(  # noqa: PLR0913 -- the watch loop's collaborators
    path: Path,
    engine: LeRobotEngine,
    send: Sender,
    stop: threading.Event,
    *,
    from_start: bool = False,
    liveness: Liveness | None = None,
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
                pump(follower, engine, send, clock(), liveness)
            except Exception:  # process boundary: keep following
                logger.warning("a pass over the rollout log failed; continuing", exc_info=True)
    finally:
        follower.close()
        send(engine.stop())
