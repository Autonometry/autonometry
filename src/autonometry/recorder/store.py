"""Sealed session logs on disk (README, "Recorder"; definitions.md 8, 10.3, 10.4).

Layout: ``<data_dir>/<robot_id>/<session_id>/events.jsonl`` and ``events.checkpoints.jsonl``;
folders 0700, files 0600. Every write goes through one :class:`FileOps`, so tests can record
the order of writes and fsyncs, or crash between them. Durability before commitment: an event is
one ``write`` on an ``O_APPEND`` descriptor; the events are fsynced before a checkpoint that
covers them is written, and the checkpoint file is fsynced after every record, so a checkpoint
never covers an event a power cut can lose.

Crash recovery replays a session that has no ``session_end`` line by line, verifying its chain,
and rebuilds the :class:`~autonometry.sealing.Sealer` and the recorder's state; memory stays
constant whatever the size of the log.
"""

import hashlib
import logging
import os
import re
import secrets
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import rfc8785

from autonometry import sealfile, signednote, strictjson
from autonometry.fileio import (
    PRIVATE_FILE_MODE,
    atomic_write,
    fsync_directory,
    make_private_dirs,
)
from autonometry.reader import RejectedEventError, check_event, iter_lines
from autonometry.recorder.config import Prepared
from autonometry.recorder.engine import ScopeValue
from autonometry.sealing import Checkpoint, Sealer
from autonometry.timestamps import ts_to_ns

logger = logging.getLogger(__name__)

EVENTS_FILE = "events.jsonl"
CHECKPOINTS_FILE = "events.checkpoints.jsonl"
_SESSION_RE = re.compile(r"(?P<counter>[0-9]{6,16})-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}", re.ASCII)
#: Width the session counter is padded to; it grows beyond when it must.
_COUNTER_DIGITS = 6
_BINARY = getattr(os, "O_BINARY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_TAIL_CHUNK = 65536
#: Fields of ``session_start`` that the config decides; a recovered session continues only when
#: they are unchanged.
DECLARED_FIELDS = (
    "scopes",
    "scope_weights",
    "heartbeat_interval_s",
    "hint_policy",
    "task_registry_hash",
    "plan_hash",
    "device_registry_hash",
    "simulated",
    "unobserved",
)


class FileOps(Protocol):
    """The two system calls through which every recorder file write passes; ``path`` names the
    file open on ``fd``."""

    def write(self, path: Path, fd: int, data: bytes) -> int:
        """Write bytes; return how many were written."""
        ...

    def fsync(self, path: Path, fd: int) -> None:
        """Make everything written to ``fd`` durable."""
        ...


class OsFileOps:
    """:class:`FileOps` of the operating system."""

    def write(self, path: Path, fd: int, data: bytes) -> int:
        """``os.write``."""
        return os.write(fd, data)

    def fsync(self, path: Path, fd: int) -> None:
        """``os.fsync``."""
        os.fsync(fd)


class AppendFile:
    """A private file opened with ``O_APPEND``; tracks bytes written and bytes made durable."""

    def __init__(self, path: Path, ops: FileOps) -> None:
        created = not path.exists()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _BINARY | _CLOEXEC
        self.path = path
        self._ops = ops
        self._fd = os.open(path, flags, PRIVATE_FILE_MODE)
        if created:
            fsync_directory(path.parent)
        self.size = os.fstat(self._fd).st_size
        # Whatever is already in the file may not be durable yet (a crash before its fsync).
        self._ops.fsync(path, self._fd)
        self.synced = self.size

    def write(self, data: bytes) -> None:
        """Append ``data`` with one ``write`` (more only if the system writes part of it)."""
        view = memoryview(data)
        while view:
            view = view[self._ops.write(self.path, self._fd, view.tobytes()) :]
        self.size += len(data)

    def sync(self) -> None:
        """fsync, unless everything written is already durable."""
        if self.synced < self.size:
            self._ops.fsync(self.path, self._fd)
            self.synced = self.size

    def close(self) -> None:
        """fsync and close."""
        self.sync()
        os.close(self._fd)


def append_record(path: Path, line: bytes, ops: FileOps) -> None:
    """Append one line to a file durably: open, write, fsync, close."""
    target = AppendFile(path, ops)
    try:
        target.write(line)
    finally:
        target.close()


@dataclass(frozen=True, slots=True)
class SignedCheckpoint:
    """A checkpoint written to a checkpoint file: the file and the signed note."""

    checkpoints_path: Path
    note: str


class SessionLog:
    """One session's ``events.jsonl`` and ``events.checkpoints.jsonl``, sealed as written."""

    def __init__(
        self,
        directory: Path,
        robot_id: str,
        prepared: Prepared,
        ops: FileOps,
        sealer: Sealer | None = None,
    ) -> None:
        self.directory = directory
        self.robot_id = robot_id
        self.session_id = directory.name
        self._prepared = prepared
        sealing = prepared.rules.sealing
        self._sealer = sealer or Sealer(
            prepared.rules.chain_genesis_prefix,
            robot_id,
            self.session_id,
            sealing.checkpoint_every_events,
            sealing.checkpoint_every_s,
        )
        self._events = AppendFile(directory / EVENTS_FILE, ops)
        self._checkpoints = AppendFile(directory / CHECKPOINTS_FILE, ops)
        self._last_sync: float | None = None

    @property
    def size_bytes(self) -> int:
        """Bytes in ``events.jsonl``."""
        return self._events.size

    @property
    def checkpoints_path(self) -> Path:
        """The checkpoint file of this session."""
        return self._checkpoints.path

    def append(self, event: Mapping[str, Any]) -> SignedCheckpoint | None:
        """Seal and append one event (``seq`` is the next one); write a checkpoint when the
        policy of definitions.md 10.3 says so, after fsyncing the events it covers."""
        full = {**event, "robot_id": self.robot_id, "session_id": self.session_id}
        full["seq"] = self._sealer.size
        sealed, checkpoint = self._sealer.add(full)
        self._events.write(rfc8785.dumps(sealed) + b"\n")
        return self._commit(checkpoint) if checkpoint is not None else None

    def checkpoint_now(self) -> SignedCheckpoint | None:
        """Commit every event so far with a checkpoint, unless one already does."""
        checkpoint = self._sealer.checkpoint_now()
        return self._commit(checkpoint) if checkpoint is not None else None

    def _commit(self, checkpoint: Checkpoint) -> SignedCheckpoint:
        self._events.sync()
        body = signednote.checkpoint_body(
            self.robot_id, self.session_id, checkpoint.size, checkpoint.root
        )
        note = signednote.sign_note(body, self._prepared.key_name, self._prepared.key)
        self._checkpoints.write(sealfile.encode_checkpoint_line(note))
        self._checkpoints.sync()
        return SignedCheckpoint(self._checkpoints.path, note)

    def append_timestamp(self, line: bytes) -> None:
        """Append a timestamp record to this session's checkpoint file and fsync it."""
        self._checkpoints.write(line)
        self._checkpoints.sync()

    def sync_deadline(self, interval_s: float) -> float | None:
        """Monotonic time by which unsynced events must be fsynced; None when all are."""
        if self._events.synced == self._events.size:
            return None
        return self._last_sync + interval_s if self._last_sync is not None else 0.0

    def sync(self, now: float) -> None:
        """fsync the events now (``now``: monotonic seconds)."""
        self._events.sync()
        self._last_sync = now

    def finish(self) -> SignedCheckpoint | None:
        """The final checkpoint after the last event (definitions.md 10.3)."""
        checkpoint = self._sealer.finish()
        return self._commit(checkpoint) if checkpoint is not None else None

    def close(self) -> None:
        """fsync and close both files."""
        self._events.close()
        self._checkpoints.close()


# -- session folders --------------------------------------------------------------------------


def robot_dir(prepared: Prepared) -> Path:
    """``<data_dir>/<robot_id>``, created 0700 if missing. ``robot_id`` is an identifier
    (definitions.md 8), so the folder cannot leave ``data_dir``."""
    path = prepared.config.data_dir / prepared.config.robot_id
    make_private_dirs(path)
    return path


def _counter(entry: Path) -> int | None:
    match = _SESSION_RE.fullmatch(entry.name)
    return int(match.group("counter")) if match else None


def session_dirs(robot: Path) -> list[Path]:
    """Session folders of a robot, oldest first by their counter, never by time: a computer
    without a real-time clock can boot with an old time. Anything else, including symbolic links
    and names that are not session ids, is ignored."""
    root = robot.resolve()
    found = []
    for entry in robot.iterdir():
        if (
            _counter(entry) is not None
            and not entry.is_symlink()
            and entry.is_dir()
            and entry.resolve().parent == root
        ):
            found.append(entry)
    return sorted(found, key=lambda p: (_counter(p), p.name))


def new_session_dir(robot: Path, now_ns: int, inputs: Mapping[str, bytes]) -> Path:
    """Create the folder of a new session, 0700, holding private copies of ``inputs`` (the device
    registry, task registry and plan the session's hashes cover). Its id is a per-robot counter,
    one more than the largest on disk, then the UTC start time and a random suffix, for example
    ``000042-20261112T083000Z-7f3a``."""
    stamp = datetime.fromtimestamp(now_ns // 1_000_000_000, UTC).strftime("%Y%m%dT%H%M%SZ")
    # Every entry with a session id's form counts, so the new name is never taken.
    counters = [c for entry in robot.iterdir() if (c := _counter(entry)) is not None]
    counter = max(counters, default=0) + 1
    path = robot / f"{counter:0{_COUNTER_DIGITS}d}-{stamp}-{secrets.token_hex(2)}"
    make_private_dirs(path)
    for name, data in sorted(inputs.items()):
        atomic_write(path / name, data)
    return path


def _tail_cut(path: Path) -> int:
    """Byte offset just after the last newline of a file (0 if none): what stays when a final
    partial line is cut off."""
    with open(path, "rb") as stream:
        end = stream.seek(0, os.SEEK_END)
        position = end
        while position > 0:
            start = max(0, position - _TAIL_CHUNK)
            stream.seek(start)
            chunk = stream.read(position - start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            position = start
    return 0


def truncate_partial_line(path: Path) -> int:
    """Cut a final line that has no newline (a write a crash interrupted; never complete, so no
    checkpoint covers it) and fsync. Returns the number of bytes cut."""
    if not path.exists():
        return 0
    size = path.stat().st_size
    cut = _tail_cut(path)
    if cut == size:
        return 0
    fd = os.open(path, os.O_WRONLY | _BINARY | _CLOEXEC)
    try:
        os.ftruncate(fd, cut)
        os.fsync(fd)
    finally:
        os.close(fd)
    return size - cut


def repair_checkpoint_files(robot: Path) -> None:
    """Cut a partial final line from every session's checkpoint file: a timestamp record may
    have been interrupted in a session that was already closed."""
    for directory in session_dirs(robot):
        cut = truncate_partial_line(directory / CHECKPOINTS_FILE)
        if cut:
            logger.warning(
                "cut %d bytes of a partial last line from %s", cut, directory / CHECKPOINTS_FILE
            )


def _last_line(path: Path) -> bytes | None:
    """The last complete line of a file, without its newline; None when it has none."""
    cut = _tail_cut(path)
    if cut == 0:
        return None
    with open(path, "rb") as stream:
        position = cut - 1
        while position > 0:
            start = max(0, position - _TAIL_CHUNK)
            stream.seek(start)
            chunk = stream.read(position - start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                start += newline + 1
                break
            position = start
        else:
            start = 0
        stream.seek(start)
        return stream.read(cut - 1 - start)


def _line_count(path: Path) -> int:
    count = 0
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            count += chunk.count(b"\n")
    return count


def _last_checkpoint_size(path: Path, prepared: Prepared) -> int:
    """Tree size of the last well-formed checkpoint record of a checkpoint file (0 if none)."""
    if not path.exists():
        return 0
    rules = prepared.rules
    with open(path, "rb") as stream:
        seal_file = sealfile.read_seal_file(
            str(path),
            stream,
            rules.errors.max_line_bytes,
            rules.errors.list_max_per_code,
            prepared.spec_version,
        )
    for record in reversed(seal_file.records):
        if isinstance(record, sealfile.CheckpointLine):
            try:
                return signednote.parse_signed_checkpoint(record.note)[0].size
            except signednote.SignedNoteError:
                continue
    return 0


def is_committed(directory: Path, prepared: Prepared) -> bool:
    """True when the session's last checkpoint covers every complete line of its log: false
    after a crash between a session's last event and its final checkpoint."""
    events = _line_count(directory / EVENTS_FILE)
    return _last_checkpoint_size(directory / CHECKPOINTS_FILE, prepared) == events


def is_closed(directory: Path) -> bool:
    """True when the session's last complete line is its ``session_end``. A line that cannot be
    read counts as closed: such a session is not continued."""
    line = _last_line(directory / EVENTS_FILE)
    if line is None:
        return False
    try:
        value = strictjson.parse(line.decode("utf-8"))
    except (UnicodeDecodeError, strictjson.StrictJsonError):
        return True
    return isinstance(value, dict) and value.get("type") == "session_end"


# -- crash recovery ---------------------------------------------------------------------------


class RecoveryError(Exception):
    """A session cannot be continued: its events do not verify. The message is one line."""


@dataclass(slots=True)
class Recovered:
    """A session rebuilt from disk: its sealer, ``session_start`` and the recorder's state."""

    directory: Path
    sealer: Sealer
    session_start: Mapping[str, Any]
    values: dict[str, ScopeValue] = field(default_factory=dict)
    open_task: str | None = None
    distance_m: float | None = None
    last_ts_ns: int = 0
    events: int = 0
    truncated_bytes: int = 0
    closed: bool = False

    def declaration(self) -> dict[str, Any]:
        """The fields of its ``session_start`` that the config decides."""
        return {name: self.session_start.get(name) for name in DECLARED_FIELDS}

    def follow(self, event: Mapping[str, Any]) -> None:
        """Update the state from one more event of the session."""
        kind = event["type"]
        self.last_ts_ns = ts_to_ns(event["ts"])
        if "distance_m" in event:
            self.distance_m = event["distance_m"]
        if kind == "transition":
            self.values[event["scope"]] = ScopeValue(
                event["state"], event.get("reason"), event.get("actor_id"), event.get("device_id")
            )
        elif kind == "task_start":
            self.open_task = event["task_id"]
        elif kind == "task_end" and event["task_id"] == self.open_task:
            self.open_task = None


def _own_key(prepared: Prepared) -> signednote.VerifierKey:
    public = prepared.key.public_key().public_bytes_raw()
    return signednote.parse_vkey(signednote.format_vkey(prepared.key_name, public))


def _committed_roots(directory: Path, prepared: Prepared) -> dict[int, bytes]:
    """The tree size and root of every well-formed checkpoint in a session's checkpoint file.

    Raises:
        RecoveryError: a checkpoint names another session, its signature by the recorder's own
            key does not verify, or two checkpoints of one size differ.
    """
    path = directory / CHECKPOINTS_FILE
    if not path.exists():
        return {}
    rules = prepared.rules
    with open(path, "rb") as stream:
        seal_file = sealfile.read_seal_file(
            str(path),
            stream,
            rules.errors.max_line_bytes,
            rules.errors.list_max_per_code,
            prepared.spec_version,
        )
    own_key = _own_key(prepared)
    roots: dict[int, bytes] = {}
    for record in seal_file.records:
        if not isinstance(record, sealfile.CheckpointLine):
            continue
        try:
            body, note = signednote.parse_signed_checkpoint(record.note)
        except signednote.SignedNoteError:
            continue  # not well formed: the verifier reports it as a file problem
        where = f"the checkpoint at line {record.line}"
        if (body.robot_id, body.session_id) != (prepared.config.robot_id, directory.name):
            raise RecoveryError(f"{where} names another session")
        try:
            signednote.trusted_signers(note, [own_key])
        except signednote.BadSignatureError:
            raise RecoveryError(f"{where} has a signature that does not verify") from None
        if roots.setdefault(body.size, body.root) != body.root:
            raise RecoveryError(f"{where} contradicts an earlier checkpoint of the same size")
    return roots


def _check_seals(
    committed: Mapping[int, bytes], replayed: Mapping[int, bytes], events: int
) -> None:
    """Every checkpoint covers at most the events on disk, with the root they have."""
    for size, root in sorted(committed.items()):
        if size > events:
            raise RecoveryError(
                f"a checkpoint covers {size} events but the log holds {events}: "
                "the log contradicts its own seals"
            )
        if replayed[size] != root:
            raise RecoveryError(f"the checkpoint of {size} events has another root than the log")


def recover(directory: Path, prepared: Prepared) -> Recovered:
    """Rebuild a session from disk: cut its final partial line (a write a crash interrupted,
    never complete, so no checkpoint covers it), then replay its events, verifying every hash and
    link of its chain (definitions.md 8, 10.1), and check its checkpoint file
    against the events: every well-formed checkpoint must cover at most the events on disk, with
    their root, and carry no bad signature by the recorder's key (10.6). ``closed`` tells whether
    it ends with ``session_end``.

    Raises:
        RecoveryError: a line does not verify, the session does not open with ``session_start``,
            has no complete event, has a second ``session_start`` or an event after
            ``session_end``, or its checkpoints contradict its events: never append to a log that
            contradicts its own seals. Only the partial line was cut then.
    """
    rules = prepared.rules
    robot_id, session_id = prepared.config.robot_id, directory.name
    events_path = directory / EVENTS_FILE
    truncated = truncate_partial_line(events_path)
    sealer = Sealer(
        rules.chain_genesis_prefix,
        robot_id,
        session_id,
        rules.sealing.checkpoint_every_events,
        rules.sealing.checkpoint_every_s,
    )
    committed = _committed_roots(directory, prepared)
    replayed: dict[int, bytes] = {0: hashlib.sha256(b"").digest()}  # the empty tree (RFC 9162)
    recovered: Recovered | None = None
    for event in _replay(events_path, rules.errors.max_line_bytes, prepared.spec_version):
        if (event["robot_id"], event["session_id"]) != (robot_id, session_id):
            raise RecoveryError(f"seq {event['seq']} belongs to another robot or session")
        if event["seq"] != sealer.size:
            raise RecoveryError(f"seq {event['seq']} where {sealer.size} was expected")
        sealed, _ = sealer.add(event)
        if (event.get("prev_hash"), event.get("hash")) != (sealed["prev_hash"], sealed["hash"]):
            raise RecoveryError(f"seq {event['seq']} does not verify against the chain")
        if recovered is None:
            if event["type"] != "session_start":
                raise RecoveryError("the session does not open with session_start")
            recovered = Recovered(directory, sealer, event, truncated_bytes=truncated)
            recovered.values = {
                scope: ScopeValue(state) for scope, state in event["initial_states"].items()
            }
        elif event["type"] == "session_start" or recovered.closed:
            raise RecoveryError(f"seq {event['seq']} follows the session's start or end marker")
        recovered.closed = event["type"] == "session_end"
        recovered.follow(event)
        recovered.events += 1
        if sealer.size in committed:
            replayed[sealer.size] = sealer.root()
    _check_seals(committed, replayed, sealer.size)
    if recovered is None:
        raise RecoveryError("the session has no complete event")
    return recovered


def _replay(path: Path, limit: int, spec_version: str) -> Iterator[dict[str, Any]]:
    """The events of a log, each checked as ``validate`` checks it; the first bad line ends it."""
    if not path.exists():
        return
    with open(path, "rb") as stream:
        for number, content in enumerate(iter_lines(stream, limit), start=1):
            if content is None:
                raise RecoveryError(f"line {number} is longer than {limit} bytes")
            try:
                value = strictjson.parse(content.decode("utf-8"))
                yield cast(dict[str, Any], check_event(value, spec_version))
            except UnicodeDecodeError:
                raise RecoveryError(f"line {number} is not valid UTF-8") from None
            except (strictjson.StrictJsonError, RejectedEventError) as exc:
                raise RecoveryError(f"line {number} is not a valid event: {exc}") from None
