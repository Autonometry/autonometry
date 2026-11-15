"""The recorder's free-space floor (product rule 1, security review M10 open point 2): it never
fills the robot's disk. Below ``min_free_bytes`` it stops writing with one error and resumes
when space returns; the time in between is ``unknown``, like any lost data (definitions.md 2)."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autonometry import cli
from autonometry.recorder.config import DEFAULT_MIN_FREE_BYTES, Prepared, RecorderConfig
from autonometry.recorder.runtime import DISK_CHECK_S, Recorder
from autonometry.recorder.store import EVENTS_FILE
from autonometry.recorder.wire import RECORDER_X_FIELDS
from recorder_helpers import (
    Client,
    FakeClock,
    all_events,
    assert_all_verified,
    heartbeats,
    make_config,
    make_prepared,
    make_recorder,
    session_folders,
)

FLOOR = 1 << 30


class Disk:
    """A disk whose free space the test sets."""

    def __init__(self, free: int) -> None:
        self.free = free
        self.looks = 0

    def __call__(self, path: Path) -> int:
        self.looks += 1
        return self.free


def _setup(tmp_path: Path) -> tuple[Recorder, FakeClock, Disk, Client, Prepared]:
    prepared = make_prepared(tmp_path, min_free_bytes=FLOOR)
    clock = FakeClock()
    disk = Disk(FLOOR * 2)
    recorder = make_recorder(prepared, clock, disk_free=disk)
    recorder.start()
    return recorder, clock, disk, Client("nav"), prepared


def _drive(recorder: Recorder, clock: FakeClock, client: Client, seconds: float) -> None:
    """The connector asserts ``base`` as the AI's every 0.25 s for ``seconds``."""
    for _ in range(round(seconds / 0.25)):
        recorder.receive(client.state("base", "ai", actor_id="ai:nav2", device_id="nav2"))
        clock.advance(0.25)
        recorder.tick()


def _size(data_dir: Path) -> int:
    return sum((folder / EVENTS_FILE).stat().st_size for folder in session_folders(data_dir))


def test_the_default_floor_is_1_gib(tmp_path: Path) -> None:
    data = {k: v for k, v in make_config(tmp_path).model_dump().items() if k != "min_free_bytes"}
    assert DEFAULT_MIN_FREE_BYTES == 1 << 30
    assert RecorderConfig.model_validate(data).min_free_bytes == 1 << 30


def test_below_the_floor_nothing_is_written_and_one_error_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    recorder, clock, disk, client, prepared = _setup(tmp_path)
    data_dir = prepared.config.data_dir
    _drive(recorder, clock, client, 3.0)
    disk.free = FLOOR - 1
    clock.advance(DISK_CHECK_S)
    with caplog.at_level(logging.WARNING, logger="autonometry.recorder"):
        recorder.tick()
        before = _size(data_dir)
        _drive(recorder, clock, client, 30.0)
    assert _size(data_dir) == before
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "below min_free_bytes" in errors[0].getMessage()
    assert disk.looks <= 2 + round(33.0 / DISK_CHECK_S) + 2  # about once a second, not per datagram
    recorder.stop(0.0)


def test_when_space_returns_recording_resumes_and_the_gap_is_unknown(tmp_path: Path) -> None:
    recorder, clock, disk, client, prepared = _setup(tmp_path)
    data_dir = prepared.config.data_dir
    _drive(recorder, clock, client, 3.0)
    disk.free = FLOOR - 1
    clock.advance(DISK_CHECK_S)
    recorder.tick()
    _drive(recorder, clock, client, 20.0)
    disk.free = FLOOR
    clock.advance(DISK_CHECK_S)
    recorder.tick()
    _drive(recorder, clock, client, 3.0)
    recorder.stop(0.0)

    events = all_events(data_dir)
    resumed = [h for h in heartbeats(events) if "x_disk_full_s" in h]
    assert len(resumed) == 1
    assert resumed[0]["x_disk_full_s"] == pytest.approx(21.0, abs=1.0)
    after = events[events.index(resumed[0]) + 1]
    assert (after["type"], after["state"], after["x_cause"]) == (
        "transition",
        "unknown",
        "disk_full",
    )
    base = [e["state"] for e in events if e.get("scope") == "base" and e["type"] == "transition"]
    assert base[-1] == "ai"  # the connector's next assertions restore the scope
    assert_all_verified(prepared, data_dir)

    log = session_folders(data_dir)[-1] / EVENTS_FILE
    timeline = json.loads(CliRunner().invoke(cli.app, ["timeline", "--json", str(log)]).stdout)
    whole = timeline["sessions"][0]["time"]["whole_robot"]
    assert whole["unknown_s"] >= 20.0
    assert any(f["code"] == "time_gap" for f in timeline["sessions"][0]["flags"])


def test_a_floor_of_zero_never_stops_the_recorder(tmp_path: Path) -> None:
    prepared = make_prepared(tmp_path, min_free_bytes=0)
    clock = FakeClock()
    disk = Disk(0)
    recorder = make_recorder(prepared, clock, disk_free=disk)
    recorder.start()
    _drive(recorder, clock, Client("nav"), 3.0)
    recorder.stop(0.0)
    assert disk.looks == 0
    assert not [h for h in heartbeats(all_events(prepared.config.data_dir)) if "x_disk_full_s" in h]


def test_a_disk_that_cannot_be_read_does_not_stop_the_recorder(tmp_path: Path) -> None:
    def unreadable(path: Path) -> Any:
        raise OSError("no such device")

    prepared = make_prepared(tmp_path, min_free_bytes=FLOOR)
    clock = FakeClock()
    recorder = make_recorder(prepared, clock, disk_free=unreadable)
    recorder.start()
    _drive(recorder, clock, Client("nav"), 3.0)
    recorder.stop(0.0)
    assert_all_verified(prepared, prepared.config.data_dir)


def test_a_connector_cannot_fake_the_disk_note() -> None:
    assert "x_disk_full_s" in RECORDER_X_FIELDS
