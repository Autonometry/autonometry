"""Tests for the recorder process over a real Unix datagram socket, driven by the real client
(README "Recorder"; definitions.md 8, 10.6). POSIX only."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip("the recorder service needs POSIX sockets", allow_module_level=True)

import grp
import importlib.util
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

from typer.testing import CliRunner

from autonometry import cli
from autonometry.recorder.config import Prepared
from autonometry.recorder.service import Service, StartError
from autonometry.recorder.store import CHECKPOINTS_FILE, EVENTS_FILE
from recorder_helpers import (
    SYNCED,
    all_events,
    heartbeats,
    make_prepared,
    session_folders,
    transitions,
    write_inputs,
)

CLIENT_FILE = Path(__file__).parent.parent / "clients" / "python" / "autonometry_client.py"
runner = CliRunner()


def load_client() -> ModuleType:
    spec = importlib.util.spec_from_file_location("autonometry_client", CLIENT_FILE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A folder with a short path: Unix socket paths are limited to about 100 bytes."""
    folder = tempfile.mkdtemp(prefix="am", dir="/tmp")
    yield Path(folder)
    shutil.rmtree(folder, ignore_errors=True)


def prepared_for(tmp_path: Path, short_dir: Path, **overrides: Any) -> Prepared:
    return make_prepared(tmp_path, socket_path=str(short_dir / "rec.sock"), **overrides)


class Running:
    """A service on a thread, stopped by :meth:`stop`."""

    def __init__(self, prepared: Prepared) -> None:
        self.service = Service(
            prepared, read_clock_status=lambda: SYNCED, anchorer_factory=lambda p, n: None
        )
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not prepared.config.socket_path.exists() and self.thread.is_alive():
            assert time.monotonic() < deadline, "the socket never appeared"
            time.sleep(0.01)

    def _run(self) -> None:
        try:
            self.service.run(install_signals=False)
        except BaseException as exc:
            self.error = exc

    def stop(self) -> None:
        self.service.request_stop()
        self.thread.join(20)
        assert not self.thread.is_alive()
        if self.error is not None:
            raise self.error


def test_end_to_end_with_the_client_verifies__defs_10_6(tmp_path: Path, short_dir: Path) -> None:
    prepared = prepared_for(tmp_path, short_dir)
    client_module = load_client()
    running = Running(prepared)
    nav = client_module.Recorder(str(prepared.config.socket_path), "nav")
    ui = client_module.Recorder(str(prepared.config.socket_path), "ui")
    ui.event("task_start", task_id="t1", task_definition_id="dock_v1")
    for tick in range(12):
        state = "ai" if tick < 6 else "human_remote"
        extra = {"reason": "human_initiated", "actor_id": "op_17"} if state != "ai" else {}
        nav.state("base", state, device_id="nav2", **({"actor_id": "ai:nav2"} | extra))
        nav.state("arm", "idle")
        nav.distance(tick * 0.5)
        time.sleep(0.2)
    ui.event("task_end", task_id="t1", outcome="success")
    time.sleep(0.2)
    running.stop()
    nav.close()
    ui.close()
    assert (nav.dropped, ui.dropped) == (0, 0)
    assert not prepared.config.socket_path.exists()

    data_dir = prepared.config.data_dir
    events = all_events(data_dir)
    assert [t["state"] for t in transitions(events, "base")] == ["ai", "human_remote"]
    assert transitions(events, "base")[1]["task_id"] == "t1"
    assert heartbeats(events)[-1]["distance_m"] == pytest.approx(5.5)
    logs = [str(f / EVENTS_FILE) for f in session_folders(data_dir)]
    validated = runner.invoke(cli.app, ["validate", *logs])
    assert validated.exit_code == 0, validated.output
    assert " 0 rejected" in validated.output
    anchors = [
        arg for f in session_folders(data_dir) for arg in ("--anchor", str(f / CHECKPOINTS_FILE))
    ]
    vkey = str(prepared.config.key_path) + ".vkey"
    verified = runner.invoke(cli.app, ["verify", *logs, "--vkey", vkey, *anchors])
    assert verified.exit_code == 0, verified.output
    assert "result: verified" in verified.output


def test_socket_is_private_by_default(tmp_path: Path, short_dir: Path) -> None:
    prepared = prepared_for(tmp_path, short_dir)
    running = Running(prepared)
    mode = stat.S_IMODE(os.stat(prepared.config.socket_path).st_mode)
    running.stop()
    assert mode == 0o600


def test_socket_group_makes_it_0660(tmp_path: Path, short_dir: Path) -> None:
    try:
        group = grp.getgrgid(os.getgid()).gr_name
    except KeyError:
        pytest.skip("the current group has no name")
    prepared = prepared_for(tmp_path, short_dir, socket_group=group)
    running = Running(prepared)
    info = os.stat(prepared.config.socket_path)
    running.stop()
    assert stat.S_IMODE(info.st_mode) == 0o660
    assert info.st_gid == os.getgid()


def test_a_stale_socket_is_replaced(tmp_path: Path, short_dir: Path) -> None:
    prepared = prepared_for(tmp_path, short_dir)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    stale.bind(str(prepared.config.socket_path))
    stale.close()
    Running(prepared).stop()


def test_a_live_socket_is_refused(tmp_path: Path, short_dir: Path) -> None:
    first = prepared_for(tmp_path / "a", short_dir)
    second = prepared_for(tmp_path / "b", short_dir)
    running = Running(first)
    try:
        with pytest.raises(StartError, match="another process is receiving"):
            Service(second, anchorer_factory=lambda p, n: None).run(install_signals=False)
    finally:
        running.stop()
    assert not (second.config.data_dir / "r1").exists() or not session_folders(
        second.config.data_dir
    )


def test_a_second_recorder_for_the_same_robot_is_refused(tmp_path: Path, short_dir: Path) -> None:
    first = prepared_for(tmp_path, short_dir)
    other_socket = first.config.model_copy(update={"socket_path": short_dir / "other.sock"})
    second = replace(first, config=other_socket)
    running = Running(first)
    try:
        with pytest.raises(StartError, match="already writing"):
            Service(second, anchorer_factory=lambda p, n: None).run(install_signals=False)
    finally:
        running.stop()


def test_a_path_that_is_not_a_socket_is_refused(tmp_path: Path, short_dir: Path) -> None:
    prepared = prepared_for(tmp_path, short_dir)
    prepared.config.socket_path.write_text("not a socket", encoding="utf-8")
    with pytest.raises(StartError, match="is not a socket"):
        Service(prepared, anchorer_factory=lambda p, n: None).run(install_signals=False)


def test_an_unknown_group_closes_the_session_it_started(tmp_path: Path, short_dir: Path) -> None:
    prepared = prepared_for(tmp_path, short_dir, socket_group="no_such_group_xyz")
    with pytest.raises(StartError, match="is not a group"):
        Service(prepared, anchorer_factory=lambda p, n: None).run(install_signals=False)
    events = all_events(prepared.config.data_dir)
    assert (events[-1]["type"], events[-1]["x_cause"]) == ("session_end", "shutdown")


def test_a_missing_socket_folder_is_refused(tmp_path: Path, short_dir: Path) -> None:
    prepared = make_prepared(tmp_path, socket_path=str(short_dir / "missing" / "rec.sock"))
    with pytest.raises(StartError, match="does not exist"):
        Service(prepared, anchorer_factory=lambda p, n: None).run(install_signals=False)


def write_config(tmp_path: Path, short_dir: Path) -> Path:
    inputs = write_inputs(tmp_path / "etc")
    lines = [
        'robot_id = "r1"',
        f'data_dir = "{tmp_path / "data"}"',
        f'socket_path = "{short_dir / "rec.sock"}"',
        'scopes = ["base"]',
        "simulated = true",
        'clock_source = "ntp"',
        *(f'{name} = "{path}"' for name, path in inputs.items()),
    ]
    path = tmp_path / "recorder.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_sigterm_stops_the_recorder_cleanly(tmp_path: Path, short_dir: Path) -> None:
    config = write_config(tmp_path, short_dir)
    process = subprocess.Popen(  # noqa: S603 -- fixed arguments, no shell
        [
            sys.executable,
            "-c",
            "from autonometry.cli import main; main()",
            "record",
            "--config",
            str(config),
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while not (short_dir / "rec.sock").exists():
        assert process.poll() is None, process.communicate()[1]
        assert time.monotonic() < deadline
        time.sleep(0.05)
    with load_client().Recorder(str(short_dir / "rec.sock"), "nav") as client:
        client.state("base", "ai", actor_id="ai:nav2")
    time.sleep(0.3)
    process.send_signal(signal.SIGTERM)
    _, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, stderr
    assert "info: started session" in stderr
    assert "ai:nav2" not in stderr  # never event contents
    events = all_events(tmp_path / "data")
    assert (events[-1]["type"], events[-1]["x_cause"]) == ("session_end", "shutdown")
    assert transitions(events, "base")[0]["state"] == "ai"


def test_record_with_a_bad_config_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["record", "--config", str(tmp_path / "missing.toml")])
    assert result.exit_code == 2
    assert "cannot read" in result.output


def test_record_with_a_socket_in_use_exits_2(tmp_path: Path, short_dir: Path) -> None:
    config = write_config(tmp_path, short_dir)
    (short_dir / "rec.sock").write_text("x", encoding="utf-8")
    result = runner.invoke(cli.app, ["record", "--config", str(config)])
    assert result.exit_code == 2


def test_datagrams_are_read_in_order_and_counted(tmp_path: Path, short_dir: Path) -> None:
    prepared = prepared_for(tmp_path, short_dir)
    running = Running(prepared)
    sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 18)
    path = str(prepared.config.socket_path)
    sender.sendto(b"x" * 70_000, path)  # too long
    sender.sendto(
        json.dumps(
            {"client": "c", "n": 0, "event": {"type": "help_request", "scope": "base"}}
        ).encode(),
        path,
    )
    time.sleep(1.5)
    running.stop()
    sender.close()
    events = all_events(prepared.config.data_dir)
    assert [e["type"] for e in events].count("help_request") == 1
    assert sum(h.get("x_rejected_client_events", 0) for h in heartbeats(events)) == 1
