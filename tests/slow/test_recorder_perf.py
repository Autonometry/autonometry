"""Slow recorder measurements (run with ``uv run pytest -m slow --no-cov -s``): the events per
second it sustains, and its memory after an hour of traffic compressed with a fake clock."""

import os
import socket
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from autonometry.recorder.runtime import Recorder
from autonometry.recorder.store import EVENTS_FILE
from recorder_helpers import (
    SYNCED,
    Client,
    FakeClock,
    assert_all_verified,
    make_prepared,
    make_recorder,
    session_folders,
)

MiB = 1024 * 1024


def max_rss_mib() -> float:
    """The process's maximum resident set size in MiB; NaN where it cannot be read."""
    if sys.platform == "win32":
        return float("nan")
    import resource  # noqa: PLC0415 -- POSIX only

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / MiB if sys.platform == "darwin" else rss / 1024


def report(capsys: pytest.CaptureFixture[str], line: str) -> None:
    with capsys.disabled():
        sys.stdout.write(f"\n{line}\n")


@pytest.mark.slow
def test_memory_stays_flat_over_an_hour_at_50_events_per_s(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared = make_prepared(tmp_path)
    clock = FakeClock()
    recorder = make_recorder(prepared, clock)
    recorder.start()
    nav, ui = Client("nav"), Client("ui")
    tracemalloc.start()
    try:
        after_a_minute = 0
        for second in range(3600):
            for slot in range(50):
                if slot % 5 == 0:
                    state = "ai" if (second + slot) % 2 else "scripted"
                    recorder.receive(nav.state("base", state, actor_id="ai:nav2"))
                else:
                    recorder.receive(ui.event("help_request", scope="base"))
                clock.advance(0.02)
                recorder.tick()
            if second == 60:
                after_a_minute, _ = tracemalloc.get_traced_memory()
        after_an_hour, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    recorder.stop()
    events = sum(
        len((f / EVENTS_FILE).read_bytes().splitlines()) for f in session_folders(tmp_path / "data")
    )
    report(
        capsys,
        f"recorder memory: {after_a_minute / MiB:.2f} MiB after 1 min, "
        f"{after_an_hour / MiB:.2f} MiB after 1 h ({events} events), peak {peak / MiB:.2f} MiB "
        f"(Python allocations since the start); process maximum RSS {max_rss_mib():.1f} MiB",
    )
    assert events >= 0.99 * 3600 * 50  # a few assertions repeat the current state
    assert after_an_hour - after_a_minute < 1 * MiB
    assert peak < 16 * MiB
    assert_all_verified(prepared, tmp_path / "data")


@pytest.mark.slow
def test_events_per_second_in_process(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    prepared = make_prepared(tmp_path, max_events_per_s=10_000_000)
    clock = FakeClock()
    recorder = Recorder(prepared, clock, read_clock_status=lambda: SYNCED)
    recorder.start()
    ui = Client("ui")
    datagrams = [ui.event("help_request", scope="base", x_i=i) for i in range(50_000)]
    started = time.perf_counter()
    for index, data in enumerate(datagrams):
        recorder.receive(data)
        if index % 1000 == 0:
            clock.advance(1.0)
            recorder.tick()
    elapsed = time.perf_counter() - started
    recorder.stop()
    rate = len(datagrams) / elapsed
    report(capsys, f"recorder in-process: {rate:,.0f} events/s (validate, seal, write, fsync 1/s)")
    assert rate > 1000


@pytest.mark.slow
@pytest.mark.skipif(sys.platform == "win32", reason="the recorder service needs POSIX sockets")
def test_events_per_second_over_the_socket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from autonometry.recorder.service import Service  # noqa: PLC0415 -- POSIX only

    folder = Path(tempfile.mkdtemp(prefix="am", dir="/tmp"))
    path = folder / "rec.sock"
    prepared = make_prepared(tmp_path, socket_path=str(path), max_events_per_s=10_000_000)
    service = Service(
        prepared, read_clock_status=lambda: SYNCED, anchorer_factory=lambda p, n: None
    )
    thread = threading.Thread(target=service.run, kwargs={"install_signals": False})
    thread.start()
    while not path.exists():
        time.sleep(0.01)
    sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)  # blocking: waits for the recorder
    ui = Client("ui")
    count = 30_000
    started = time.perf_counter()
    for i in range(count):
        sender.sendto(ui.event("help_request", scope="base", x_i=i), str(path))
    sent = time.perf_counter() - started
    sender.close()
    time.sleep(1.0)
    service.request_stop()
    thread.join(30)
    os.rmdir(folder)
    written = sum(
        b'"help_request"' in line
        for f in session_folders(prepared.config.data_dir)
        for line in (f / EVENTS_FILE).read_bytes().splitlines()
    )
    rate = written / sent
    report(capsys, f"recorder over the socket: {rate:,.0f} events/s ({written} of {count})")
    assert written == count
    assert_all_verified(prepared, prepared.config.data_dir)
