"""Tests for the recorder's files: partial lines, session folders and the checks of crash
recovery (README "Crash recovery"; definitions.md 8, 10.1)."""

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import rfc8785

from autonometry.recorder import store
from autonometry.recorder.config import Prepared
from autonometry.recorder.store import (
    CHECKPOINTS_FILE,
    EVENTS_FILE,
    AppendFile,
    RecoveryError,
    is_closed,
    is_committed,
    new_session_dir,
    recover,
    session_dirs,
    truncate_partial_line,
)
from autonometry.sealing import Sealer
from recorder_helpers import (
    START_NS,
    Client,
    CrashingOps,
    FakeClock,
    RecordingOps,
    make_prepared,
    make_recorder,
    session_folders,
    with_data_dir,
)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> Prepared:
    return make_prepared(tmp_path_factory.mktemp("store"))


def test_truncate_partial_line(tmp_path: Path) -> None:
    path = tmp_path / "f"
    assert truncate_partial_line(path) == 0
    path.write_bytes(b"a\nb\n")
    assert truncate_partial_line(path) == 0
    path.write_bytes(b"a\nb\npart")
    assert truncate_partial_line(path) == 4
    assert path.read_bytes() == b"a\nb\n"
    path.write_bytes(b"no newline at all")
    assert truncate_partial_line(path) == 17
    assert path.read_bytes() == b""
    long_tail = b"x" * 200_000
    path.write_bytes(b"a\n" + long_tail)
    assert truncate_partial_line(path) == len(long_tail)


def test_is_closed_reads_only_the_last_complete_line(tmp_path: Path) -> None:
    folder = tmp_path / "s"
    folder.mkdir()
    events = folder / EVENTS_FILE
    events.write_bytes(b"")
    assert not is_closed(folder)
    events.write_bytes(b'{"type": "heartbeat"}\n' + b'{"type": "session_end"}\n')
    assert is_closed(folder)
    events.write_bytes(b'{"type": "session_end"}\n{"type": "heartbeat"}\n{"type": "sess')
    assert not is_closed(folder)
    big = json.dumps({"type": "session_end", "x": "y" * 100_000}).encode()
    events.write_bytes(b"x" * 70_000 + b"\n" + big + b"\n")
    assert is_closed(folder)
    events.write_bytes(big + b"\n")
    assert is_closed(folder)
    events.write_bytes(b"\xff\n")
    assert is_closed(folder)  # unreadable: never continued
    events.write_bytes(b'["not an object"]\n')
    assert not is_closed(folder)


def test_session_dirs_ignore_other_entries(tmp_path: Path) -> None:
    (tmp_path / "20261112T080000Z-00aa").mkdir()
    (tmp_path / "20261112T090000Z-00bb").mkdir()
    (tmp_path / "notes").mkdir()
    (tmp_path / "20261112T100000Z-00cc").write_text("a file", encoding="utf-8")
    (tmp_path / ".lock").write_text("", encoding="utf-8")
    names = [p.name for p in session_dirs(tmp_path)]
    assert names == ["20261112T080000Z-00aa", "20261112T090000Z-00bb"]


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_session_dirs_ignore_symbolic_links(tmp_path: Path) -> None:
    robot = tmp_path / "r1"
    robot.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (robot / "20261112T110000Z-00dd").symlink_to(outside)
    assert session_dirs(robot) == []


def test_new_session_dir_retries_a_taken_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suffixes = iter(["abcd", "abcd", "0001"])
    monkeypatch.setattr(store.secrets, "token_hex", lambda _: next(suffixes))
    first = new_session_dir(tmp_path, START_NS)
    second = new_session_dir(tmp_path, START_NS)
    assert (first.name, second.name) == ("20261112T080000Z-abcd", "20261112T080000Z-0001")


def test_append_file_writes_the_rest_of_a_short_write(tmp_path: Path) -> None:
    class Short(RecordingOps):
        def write(self, path: Path, fd: int, data: bytes) -> int:
            return super().write(path, fd, data[:3])

    ops = Short()
    target = AppendFile(tmp_path / "f", ops)
    target.write(b"0123456789\n")
    target.close()
    assert (tmp_path / "f").read_bytes() == b"0123456789\n"
    assert [op for op, *_ in ops.log].count("write") == 4


def open_session(prepared: Prepared, data_dir: Path, seconds: int = 3) -> Path:
    """A session with some events, left open as by a crash."""
    setup = with_data_dir(prepared, data_dir)
    clock = FakeClock()
    ops = CrashingOps(crash_at=10_000)
    recorder = make_recorder(setup, clock, ops)
    recorder.start()
    nav = Client("nav")
    for _ in range(seconds * 4):
        recorder.receive(nav.state("base", "ai", actor_id="ai:nav2"))
        recorder.receive(nav.event("task_start", task_id="t1", task_definition_id="d"))
        clock.advance(0.25)
        recorder.tick()
    ops.close_all()
    (folder,) = session_folders(data_dir)
    return folder


def rewrite(folder: Path, change: object) -> None:
    path = folder / EVENTS_FILE
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(change(lines)))  # type: ignore[operator]


def test_recover_rebuilds_state_from_a_session(prepared: Prepared, tmp_path: Path) -> None:
    folder = open_session(prepared, tmp_path / "data")
    with open(folder / EVENTS_FILE, "ab") as stream:
        stream.write(b'{"partial')
    recovered = recover(folder, with_data_dir(prepared, tmp_path / "data"))
    assert recovered.truncated_bytes == 9
    assert recovered.values["base"].state == "ai"
    assert recovered.values["arm"].state == "unknown"
    assert recovered.open_task == "t1"
    assert not recovered.closed
    assert recovered.events == len((folder / EVENTS_FILE).read_bytes().splitlines())
    assert recovered.declaration()["scopes"] == ["base", "arm"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda lines: [lines[0], *lines[2:]], "where 1 was expected"),
        (lambda lines: [lines[0], lines[0]], "seq 0 where 1 was expected"),
        (lambda lines: [*lines[:-1], b"x" * 70_000 + b"\n"], "longer than"),
        (lambda lines: [*lines, b"\xff\n"], "not valid UTF-8"),
        (lambda lines: [*lines, b"{}\n"], "not a valid event"),
        (lambda lines: [*lines, b"{\n"], "not a valid event"),
        (lambda lines: [], "no complete event"),
        (
            lambda lines: [lines[0].replace(b'"simulated":true', b'"simulated":false'), *lines[1:]],
            "does not verify",
        ),
        (lambda lines: [lines[0].replace(b'"robot_id":"r1"', b'"robot_id":"r2"')], "another"),
    ],
)
def test_recover_refuses_a_session_that_does_not_verify__defs_10_1(
    prepared: Prepared, tmp_path: Path, change: object, message: str
) -> None:
    folder = open_session(prepared, tmp_path / "data")
    rewrite(folder, change)
    with pytest.raises(RecoveryError, match=message):
        recover(folder, with_data_dir(prepared, tmp_path / "data"))


def test_a_closed_session_recovers_as_closed(prepared: Prepared, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    setup = with_data_dir(prepared, data_dir)
    recorder = make_recorder(setup, FakeClock())
    recorder.start()
    recorder.stop()
    (folder,) = session_folders(data_dir)
    assert recover(folder, setup).closed
    assert is_committed(folder, setup)


def write_sealed(prepared: Prepared, folder: Path, events: list[dict[str, Any]]) -> None:
    """Seal ``events`` as the recorder would, whatever they are."""
    sealer = Sealer("autonometry/chain-v1", "r1", folder.name, 1000, 600.0)
    lines = []
    for seq, event in enumerate(events):
        full = {"robot_id": "r1", "session_id": folder.name, "task_id": None, "seq": seq}
        full.update(ts="2026-11-12T08:00:00.000Z", **event)
        lines.append(rfc8785.dumps(sealer.add(full)[0]) + b"\n")
    (folder / EVENTS_FILE).write_bytes(b"".join(lines))


START = {
    "type": "session_start",
    "scopes": ["base"],
    "initial_states": {"base": "unknown"},
    "heartbeat_interval_s": 1,
    "hint_policy": "hint_as_shared",
    "task_registry_hash": "0" * 64,
    "plan_hash": "0" * 64,
    "simulated": True,
}


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([{"type": "heartbeat"}, START], "does not open with session_start"),
        ([START, START], "follows the session's start or end"),
        ([START, {"type": "session_end"}, {"type": "heartbeat"}], "start or end"),
    ],
)
def test_recover_refuses_misplaced_session_markers__defs_1(
    prepared: Prepared, tmp_path: Path, events: list[dict[str, Any]], message: str
) -> None:
    folder = tmp_path / "20261112T080000Z-0000"
    folder.mkdir()
    write_sealed(prepared, folder, events)
    with pytest.raises(RecoveryError, match=message):
        recover(folder, prepared)


def test_is_committed_reads_the_last_checkpoint(prepared: Prepared, tmp_path: Path) -> None:
    folder = open_session(prepared, tmp_path / "data")
    setup = with_data_dir(prepared, tmp_path / "data")
    assert not is_committed(folder, setup)
    checkpoints = folder / CHECKPOINTS_FILE
    good = checkpoints.read_bytes()
    checkpoints.write_bytes(good + b'{"type": "checkpoint", "note": "not a note"}\n')
    assert not is_committed(folder, setup)
    os.remove(checkpoints)
    assert not is_committed(folder, setup)
