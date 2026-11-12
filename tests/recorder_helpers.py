"""Shared helpers for recorder tests. Not collected by pytest (no ``test_`` prefix)."""

import json
import os
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from autonometry import keys, sealfile, signednote
from autonometry.events import LogCheck, VerifyResult
from autonometry.reader import read_logs
from autonometry.recorder.clock import ClockStatus
from autonometry.recorder.config import Prepared, RecorderConfig, prepare
from autonometry.recorder.runtime import Recorder
from autonometry.recorder.store import CHECKPOINTS_FILE, EVENTS_FILE, OsFileOps
from autonometry.sealfile import SealFile
from autonometry.spec import load_rules
from autonometry.verify import verify_log

SPEC = "0.3"
ROBOT = "r1"
SCOPES = ("base", "arm")
#: 2026-11-12T08:00:00Z in nanoseconds since the Unix epoch.
START_NS = 1_794_470_400_000_000_000
SYNCED = ClockStatus("ntp", 0.0005)
REGISTRY = Path(__file__).parent / "golden" / "registry" / "turtlebot4.json"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the recorder needs POSIX sockets")


class FakeClock:
    """Monotonic and UTC clocks that move only when told to."""

    def __init__(self, wall_ns: int = START_NS, mono: float = 1000.0) -> None:
        self.wall = wall_ns
        self.mono = mono

    def monotonic(self) -> float:
        return self.mono

    def wall_ns(self) -> int:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += round(seconds * 1e9)


def write_inputs(folder: Path) -> dict[str, Path]:
    """A signing key with its vkey, a task registry, a plan and a device registry."""
    folder.mkdir(parents=True, exist_ok=True)
    key = folder / "robot.key"
    keys.generate(f"autonometry/{ROBOT}", key)
    tasks = folder / "tasks.json"
    tasks.write_text(json.dumps({"tasks": [{"task_definition_id": "dock_v1"}]}), encoding="utf-8")
    plan = folder / "plan.json"
    plan.write_text(json.dumps({"items": [{"plan_item_id": "reset_scene"}]}), encoding="utf-8")
    devices = folder / "devices.json"
    shutil.copyfile(REGISTRY, devices)
    return {"key_path": key, "task_registry": tasks, "plan": plan, "device_registry": devices}


def make_config(tmp_path: Path, **overrides: Any) -> RecorderConfig:
    inputs = write_inputs(tmp_path / "etc")
    data: dict[str, Any] = {
        "robot_id": ROBOT,
        "data_dir": str(tmp_path / "data"),
        "socket_path": str(tmp_path / "recorder.sock"),
        "scopes": list(SCOPES),
        "simulated": True,
        "clock_source": "ntp",
        **{name: str(path) for name, path in inputs.items()},
    }
    data.update(overrides)
    return RecorderConfig.model_validate(data)


def make_prepared(tmp_path: Path, **overrides: Any) -> Prepared:
    return prepare(make_config(tmp_path, **overrides))


def with_data_dir(prepared: Prepared, data_dir: Path) -> Prepared:
    return replace(prepared, config=prepared.config.model_copy(update={"data_dir": data_dir}))


def make_recorder(
    prepared: Prepared, clock: FakeClock, ops: OsFileOps | None = None, **kwargs: Any
) -> Recorder:
    return Recorder(prepared, clock, ops, read_clock_status=lambda: SYNCED, **kwargs)


class Client:
    """Builds datagrams in the wire format, numbering them like the real client."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.n = 0

    def _wrap(self, body: dict[str, Any]) -> bytes:
        data = json.dumps({"client": self.name, "n": self.n, **body}).encode()
        self.n += 1
        return data

    def state(self, scope: str, state: str, **fields: Any) -> bytes:
        event = {"type": "transition", "scope": scope, "state": state}
        event.setdefault("reason", None)
        event.setdefault("actor_id", None)
        event.update(fields)
        return self._wrap({"event": event})

    def event(self, kind: str, **fields: Any) -> bytes:
        return self._wrap({"event": {"type": kind, **fields}})

    def distance(self, metres: float) -> bytes:
        return self._wrap({"distance_m": metres})


def session_folders(data_dir: Path) -> list[Path]:
    return sorted(p for p in (data_dir / ROBOT).iterdir() if p.is_dir())


def events_of(folder: Path) -> list[dict[str, Any]]:
    path = folder / EVENTS_FILE
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def all_events(data_dir: Path) -> list[dict[str, Any]]:
    return [event for folder in session_folders(data_dir) for event in events_of(folder)]


def transitions(events: Iterable[dict[str, Any]], scope: str | None = None) -> list[dict[str, Any]]:
    return [
        e for e in events if e["type"] == "transition" and (scope is None or e["scope"] == scope)
    ]


def heartbeats(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == "heartbeat"]


def _seal_file(path: Path, drop_timestamps: bool) -> SealFile:
    rules = load_rules(SPEC)
    with open(path, "rb") as stream:
        seal_file = sealfile.read_seal_file(
            str(path),
            stream,
            rules.errors.max_line_bytes,
            rules.errors.list_max_per_code,
            SPEC,
        )
    if drop_timestamps:
        records = tuple(r for r in seal_file.records if isinstance(r, sealfile.CheckpointLine))
        seal_file = replace(seal_file, records=records)
    return seal_file


def check_logs(
    prepared: Prepared, data_dir: Path, drop_timestamps: bool = False
) -> tuple[LogCheck, VerifyResult]:
    """Read every session log and verify it with its checkpoint file also used as the anchor
    file (definitions.md 10.5 b). ``drop_timestamps`` ignores timestamp records (fake tokens)."""
    folders = [f for f in session_folders(data_dir) if (f / EVENTS_FILE).stat().st_size]
    log_check = read_logs([f / EVENTS_FILE for f in folders], SPEC)
    seal_files = [_seal_file(f / CHECKPOINTS_FILE, drop_timestamps) for f in folders]
    vkey = keys.load_vkeys(keys.vkey_path(prepared.config.key_path))
    trusted = [signednote.parse_vkey(line) for line in vkey]
    result = verify_log(log_check, seal_files, seal_files, trusted, [], load_rules(SPEC))
    return log_check, result


def assert_all_verified(prepared: Prepared, data_dir: Path, drop_timestamps: bool = False) -> None:
    log_check, result = check_logs(prepared, data_dir, drop_timestamps)
    assert log_check.counts.rejected_lines == 0, log_check.errors
    statuses = {(v.session_id, v.status) for v in result.sessions}
    assert all(v.status == "verified" for v in result.sessions), (statuses, result.sessions)
    assert result.verified("L2"), result.reasons("L2")


class SimulatedCrash(BaseException):
    """The process dies here; not an Exception, so nothing in the recorder catches it."""


class RecordingOps(OsFileOps):
    """Real file operations that also record their order: ``(op, file name, size, data)``."""

    def __init__(self) -> None:
        self.log: list[tuple[str, Path, int, bytes]] = []
        self.durable: dict[Path, int] = {}
        self.fds: set[int] = set()

    def write(self, path: Path, fd: int, data: bytes) -> int:
        self.fds.add(fd)
        written = super().write(path, fd, data)
        self.log.append(("write", path, os.fstat(fd).st_size, data[:written]))
        return written

    def fsync(self, path: Path, fd: int) -> None:
        self.fds.add(fd)
        super().fsync(path, fd)
        size = os.fstat(fd).st_size
        self.durable[path] = size
        self.log.append(("fsync", path, size, b""))

    def close_all(self) -> None:
        """Close every descriptor seen, as the death of the process would."""
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                continue
        self.fds.clear()


class CrashingOps(RecordingOps):
    """Dies at the ``crash_at``-th file operation (counting from 1): before an fsync, or in a
    write, after writing the first half of it when ``torn``."""

    def __init__(self, crash_at: int, torn: bool = False) -> None:
        super().__init__()
        self.crash_at = crash_at
        self.torn = torn
        self.count = 0
        self.crashed = False

    def _tick(self) -> bool:
        self.count += 1
        return self.count == self.crash_at

    def write(self, path: Path, fd: int, data: bytes) -> int:
        if self._tick():
            self.crashed = True
            if self.torn and len(data) > 1:
                super().write(path, fd, data[: len(data) // 2])
            raise SimulatedCrash
        return super().write(path, fd, data)

    def fsync(self, path: Path, fd: int) -> None:
        self.fds.add(fd)
        if self._tick():
            self.crashed = True
            raise SimulatedCrash
        super().fsync(path, fd)

    def power_cut(self) -> None:
        """Lose everything written but not fsynced."""
        self.close_all()
        for path, size in self.durable.items():
            if path.exists() and path.stat().st_size > size:
                os.truncate(path, size)


def run_until_crash(actions: Sequence[Any], step: Any) -> bool:
    """Apply ``step`` to each action; return True when a :class:`SimulatedCrash` stopped it."""
    try:
        for action in actions:
            step(action)
    except SimulatedCrash:
        return True
    return False
