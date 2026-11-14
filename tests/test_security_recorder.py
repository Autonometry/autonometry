"""Security review M10 (docs/security-review-2026-11.md): the recorder against a misbehaving
connector on its socket and another local user on its computer. Whatever arrives, the recorder
keeps running, makes the time ``unknown`` (fail visible) and its log still verifies."""

import json
import os
import shutil
import socket
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from autonometry import keys
from autonometry.recorder.store import AppendFile, OsFileOps
from autonometry.strictjson import MAX_SAFE_INTEGER
from recorder_helpers import (
    FakeClock,
    all_events,
    assert_all_verified,
    heartbeats,
    make_prepared,
    make_recorder,
    posix_only,
)

M = MAX_SAFE_INTEGER


def _datagram(n: int, **body: Any) -> bytes:
    return json.dumps({"client": "c1", "n": n, **body}).encode()


def _run(tmp_path: Path, datagrams: list[bytes]) -> Path:
    prepared = make_prepared(tmp_path)
    clock = FakeClock()
    recorder = make_recorder(prepared, clock)
    recorder.start()
    for data in datagrams:
        recorder.receive(data)
        clock.advance(0.3)
        recorder.tick()
    clock.advance(1.5)
    recorder.tick()
    recorder.stop(0.0)
    assert_all_verified(prepared, prepared.config.data_dir)
    return prepared.config.data_dir


def test_a_counter_jump_past_2_53_never_crashes_the_recorder__s04(tmp_path: Path) -> None:
    """S-04: n jumping to 2^53 - 1 twice made the lost count exceed I-JSON's integers; the next
    heartbeat could not be written and the recorder died."""
    data_dir = _run(tmp_path, [_datagram(n, distance_m=1.0) for n in (0, M, 0, M)])
    lost = [
        h["x_lost_client_events"]
        for h in heartbeats(all_events(data_dir))
        if "x_lost_client_events" in h
    ]
    assert lost
    assert max(lost) == M


def test_odometer_readings_that_overflow_are_rejected__s04(tmp_path: Path) -> None:
    """S-04: two clients each reading 1.7e308 m made the distance infinite, which no JSON line
    can hold: the recorder died on its next heartbeat."""
    big = 1.7e308
    datagrams = [
        json.dumps({"client": client, "n": n, "distance_m": reading}).encode()
        for client in ("a", "b")
        for n, reading in ((0, 0.0), (1, big))
    ]
    data_dir = _run(tmp_path, datagrams)
    events = all_events(data_dir)
    assert all(abs(h.get("distance_m", 0.0)) <= big for h in heartbeats(events))
    assert any(h.get("x_rejected_client_events") for h in heartbeats(events))


@pytest.mark.parametrize("depth", [200, 990, 1000, 5000])
def test_deeply_nested_datagrams_are_rejected__s03(tmp_path: Path, depth: int) -> None:
    """S-03: a datagram nested 1000 deep raised RecursionError inside the recorder's loop."""
    data = (
        b'{"client":"c1","n":0,"event":{"type":"heartbeat","x_d":'
        + b"[" * depth
        + b"]" * depth
        + b"}}"
    )
    data_dir = _run(tmp_path, [data])
    assert any(h.get("x_rejected_client_events") for h in heartbeats(all_events(data_dir)))


def test_a_datagram_the_engine_fails_on_is_counted_and_the_recorder_runs_on__s04(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-datagram process boundary: an unexpected error in the engine makes every scope
    unknown and is counted as a rejection; the next datagrams are handled."""
    prepared = make_prepared(tmp_path)
    clock = FakeClock()
    recorder = make_recorder(prepared, clock)
    recorder.start()
    state = {"type": "transition", "scope": "base", "state": "ai", "reason": None}
    recorder.receive(_datagram(0, event=state | {"actor_id": "ai:nav2", "device_id": "nav2"}))
    engine = recorder._engine
    original = engine.receive

    def failing(data: bytes) -> list[dict[str, Any]]:
        raise RuntimeError("a bug")

    monkeypatch.setattr(engine, "receive", failing)
    recorder.receive(_datagram(1, distance_m=1.0))
    monkeypatch.setattr(engine, "receive", original)
    clock.advance(1.5)
    recorder.tick()
    recorder.stop(0.0)
    events = all_events(prepared.config.data_dir)
    base = [e["state"] for e in events if e["type"] == "transition" and e["scope"] == "base"]
    assert "unknown" in base[base.index("ai") :]
    assert any(h.get("x_rejected_client_events") for h in heartbeats(events))
    assert_all_verified(prepared, prepared.config.data_dir)


_HOSTILE = st.one_of(
    st.integers(min_value=0, max_value=M).map(lambda n: {"distance_m": 1e308, "n": n}),
    st.integers(min_value=0, max_value=M).map(lambda n: {"distance_m": 5e-324, "n": n}),
    st.integers(min_value=0, max_value=M).map(
        lambda n: {
            "n": n,
            "event": {
                "type": "hint",
                "scope": "base",
                "actor_id": "op",
                "dwell_s": 1e308,
                "ttl_s": 1e308,
            },
        }
    ),
    st.integers(min_value=0, max_value=M).map(
        lambda n: {"n": n, "event": {"type": "heartbeat", "x_n": [[[[[M]]]]], "x_f": -1e308}}
    ),
)


@given(st.lists(_HOSTILE, max_size=12))
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_extreme_values_in_any_order_never_stop_the_recorder__s04(
    tmp_path_factory: pytest.TempPathFactory, bodies: list[dict[str, Any]]
) -> None:
    datagrams = [json.dumps({"client": "c1", **body}).encode() for body in bodies]
    _run(tmp_path_factory.mktemp("rec"), datagrams)


# -- files and the socket (POSIX) ---------------------------------------------------------------


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A 0700 folder with a short path: Unix socket paths are limited to about 100 bytes, and
    macOS's temporary folders are longer."""
    folder = tempfile.mkdtemp(prefix="am", dir="/tmp")
    yield Path(folder)
    shutil.rmtree(folder, ignore_errors=True)


@posix_only
def test_a_log_file_replaced_by_a_link_is_refused__s07(tmp_path: Path) -> None:
    """S-07: the recorder opened its logs through symbolic links; one planted in place of
    events.jsonl would have made it append to any file the recorder's user can write."""
    target = tmp_path / "victim"
    target.write_text("keep\n")
    link = tmp_path / "events.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError, match="symbolic link"):
        AppendFile(link, OsFileOps())
    assert target.read_text() == "keep\n"


@posix_only
@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_a_robot_folder_others_can_write_is_refused__s07(tmp_path: Path, mode: int) -> None:
    from autonometry.recorder.service import StartError, check_robot_dir  # noqa: PLC0415

    robot = tmp_path / "data" / "r1"
    robot.mkdir(parents=True)
    os.chmod(robot, mode)
    with pytest.raises(StartError, match="writable by its group or others"):
        check_robot_dir(robot)


@posix_only
def test_a_data_folder_every_user_can_write_is_refused_unless_sticky__s07(tmp_path: Path) -> None:
    from autonometry.recorder.service import StartError, check_robot_dir  # noqa: PLC0415

    data = tmp_path / "data"
    robot = data / "r1"
    robot.mkdir(parents=True, mode=0o700)
    os.chmod(robot, 0o700)
    os.chmod(data, 0o777)  # noqa: S103 -- the unsafe folder under test
    with pytest.raises(StartError, match="writable by every user"):
        check_robot_dir(robot)
    os.chmod(data, 0o1777)  # noqa: S103 -- sticky, like /tmp
    check_robot_dir(robot)


@posix_only
def test_a_socket_folder_every_user_can_write_is_refused__s06(tmp_path: Path) -> None:
    """S-06: in a folder any user can write, another user can bind the configured path first
    and receive every connector's datagrams, or swap the socket for a link while the recorder
    sets its mode."""
    from autonometry.recorder.service import StartError, bind_socket, clear_stale_socket  # noqa: PLC0415

    folder = tmp_path / "run"
    folder.mkdir()
    os.chmod(folder, 0o777)  # noqa: S103 -- the unsafe folder under test
    for step in (clear_stale_socket, lambda p: bind_socket(p, None)):
        with pytest.raises(StartError, match="writable by every user"):
            step(folder / "rec.sock")


@posix_only
def test_a_socket_swapped_for_a_link_while_set_up_is_refused__s06(tmp_path: Path) -> None:
    from autonometry.recorder.service import _set_socket_mode  # noqa: PLC0415

    target = tmp_path / "victim.key"
    target.write_text("secret")
    os.chmod(target, 0o600)
    path = tmp_path / "rec.sock"
    path.symlink_to(target)
    with pytest.raises(OSError, match="replaced"):
        _set_socket_mode(path, None, 0o660)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


@posix_only
def test_the_socket_is_created_with_its_final_mode__s06(short_dir: Path) -> None:
    from autonometry.recorder.service import bind_socket  # noqa: PLC0415

    old = os.umask(0)
    try:
        sock = bind_socket(short_dir / "r.sock", None)
    finally:
        os.umask(old)
    try:
        assert stat.S_IMODE(os.lstat(short_dir / "r.sock").st_mode) == 0o600
    finally:
        sock.close()


@posix_only
def test_a_stale_socket_that_cannot_be_removed_is_a_start_error__s06(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autonometry.recorder import service  # noqa: PLC0415

    path = short_dir / "r.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as stale:
        stale.bind(str(path))

    def refuse(target: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(service.os, "unlink", refuse)
    with pytest.raises(service.StartError, match="cannot remove the stale socket"):
        service.clear_stale_socket(path)


@posix_only
def test_the_key_checked_is_the_key_read__s08(tmp_path: Path) -> None:
    """S-08: the key's permissions were checked by path after the file was read; they are now
    checked on the open file, and a group-readable key is refused."""
    key = tmp_path / "k" / "robot.key"
    keys.generate("autonometry/r1", key)
    keys.load(key)
    os.chmod(key, 0o640)
    with pytest.raises(keys.SigningKeyError, match="group or others"):
        keys.load(key)


def test_an_oversized_key_file_is_refused__s08(tmp_path: Path) -> None:
    key = tmp_path / "huge.key"
    key.write_bytes(b"x" * (1 << 20))
    if sys.platform != "win32":
        os.chmod(key, 0o600)
    with pytest.raises(keys.SigningKeyError, match="too large"):
        keys.load(key)
