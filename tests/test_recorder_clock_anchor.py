"""Tests for the recorder's clock status (definitions.md 8, Time) and its token thread
(definitions.md 10.4, 10.5)."""

import ctypes
import hashlib
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from autonometry.recorder import clock as clock_module
from autonometry.recorder.anchor import Anchorer
from autonometry.recorder.clock import (
    STA_UNSYNC,
    TIME_ERROR,
    ClockReader,
    ClockStatus,
    status_from_timex,
)

# -- clock -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "status", "maxerror_us", "expected"),
    [
        (0, 0x2001, 500, ClockStatus("ptp", 0.0005)),  # TIME_OK, STA_PLL | STA_NANO
        (1, 0, 16_000, ClockStatus("ptp", 0.016)),  # TIME_INS: a leap second is announced
        (0, STA_UNSYNC, 16_000_000, ClockStatus("none", 16.0)),
        (TIME_ERROR, 0, 2_000, ClockStatus("none", 0.002)),
        (0, 0, -1, ClockStatus("ptp", None)),
        (-1, 0, 0, ClockStatus("unknown", None)),
    ],
)
def test_adjtimex_values_give_the_heartbeat_clock_status__defs_8(
    state: int, status: int, maxerror_us: int, expected: ClockStatus
) -> None:
    assert status_from_timex(state, status, maxerror_us, "ptp") == expected


def test_clock_reader_reads_the_timex_struct__defs_8() -> None:
    seen: list[int] = []

    def fake(pointer: Any) -> int:
        timex = pointer.contents
        seen.append(timex.modes)
        timex.maxerror = 250
        timex.status = 0
        return 0

    assert ClockReader("gnss", fake).read() == ClockStatus("gnss", 0.00025)
    assert seen == [0]  # modes = 0: read only


def test_clock_reader_reports_unsynchronised__defs_8() -> None:
    def fake(pointer: Any) -> int:
        pointer.contents.status = STA_UNSYNC
        pointer.contents.maxerror = 16_000_000
        return TIME_ERROR

    assert ClockReader("ntp", fake).read() == ClockStatus("none", 16.0)


def test_timex_struct_leaves_room_for_the_kernel() -> None:
    assert ctypes.sizeof(clock_module._Timex) >= 208  # glibc struct timex on 64-bit Linux


@pytest.mark.skipif(sys.platform != "linux", reason="adjtimex is Linux only")
def test_real_adjtimex_gives_a_status_on_linux() -> None:
    status = ClockReader("ntp").read()
    assert status.source in ("ntp", "none")
    assert status.max_error_s is not None


@pytest.mark.skipif(sys.platform == "linux", reason="elsewhere the status is unknown")
def test_clock_status_is_unknown_off_linux__defs_8() -> None:
    assert ClockReader("ntp").read() == ClockStatus("unknown", None)


def test_missing_adjtimex_gives_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoLibc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise OSError("no libc")

    monkeypatch.setattr(clock_module.ctypes, "CDLL", NoLibc)
    monkeypatch.setattr(clock_module.sys, "platform", "linux")
    assert ClockReader("ntp").read() == ClockStatus("unknown", None)


# -- anchorer ----------------------------------------------------------------------------------


class Tsa:
    """A fake TSA: fails the first ``failures`` calls, then returns a token per note."""

    def __init__(self, failures: int = 0, delay_s: float = 0.0) -> None:
        self.failures = failures
        self.delay_s = delay_s
        self.calls: list[bytes] = []
        self.release = threading.Event()
        self.release.set()

    def __call__(self, note: bytes) -> bytes:
        self.release.wait(5)
        self.calls.append(note)
        time.sleep(self.delay_s)
        if len(self.calls) <= self.failures:
            raise OSError("TSA unreachable (simulated)")
        return b"tsr:" + note


def test_a_submitted_checkpoint_gets_its_token__defs_10_4(tmp_path: Path) -> None:
    woken = threading.Event()
    anchorer = Anchorer(Tsa(), woken.set, give_up_after_s=60.0)
    anchorer.start()
    path = tmp_path / "events.checkpoints.jsonl"
    anchorer.submit(path, "note one\n")
    assert anchorer.wait_idle(5.0)
    assert woken.is_set()
    (token,) = anchorer.take_results()
    assert token.checkpoints_path == path
    assert token.note_sha256 == hashlib.sha256(b"note one\n").hexdigest()
    assert token.tsr == b"tsr:note one\n"
    assert anchorer.take_results() == []
    anchorer.close()


def test_failures_are_retried_with_backoff(tmp_path: Path) -> None:
    tsa = Tsa(failures=2)
    anchorer = Anchorer(tsa, lambda: None, give_up_after_s=60.0, retry_delays_s=(0.01, 0.02))
    anchorer.start()
    anchorer.submit(tmp_path / "c", "n")
    assert anchorer.wait_idle(5.0)
    assert len(tsa.calls) == 3
    assert len(anchorer.take_results()) == 1
    anchorer.close()


def test_a_checkpoint_is_given_up_after_max_commit_delay(tmp_path: Path) -> None:
    tsa = Tsa(failures=100)
    anchorer = Anchorer(tsa, lambda: None, give_up_after_s=0.05, retry_delays_s=(0.01,))
    anchorer.start()
    anchorer.submit(tmp_path / "c", "n")
    assert anchorer.wait_idle(5.0)
    assert anchorer.take_results() == []
    assert len(tsa.calls) >= 2
    anchorer.close()


def test_a_newer_checkpoint_replaces_a_waiting_one(tmp_path: Path) -> None:
    tsa = Tsa()
    tsa.release.clear()  # the first request blocks until released
    anchorer = Anchorer(tsa, lambda: None, give_up_after_s=60.0)
    anchorer.start()
    path, other = tmp_path / "a", tmp_path / "b"
    anchorer.submit(path, "first")
    time.sleep(0.1)  # the thread has taken "first"
    anchorer.submit(path, "second")
    anchorer.submit(path, "third")
    anchorer.submit(other, "other")
    assert not anchorer.wait_idle(0.05)
    tsa.release.set()
    assert anchorer.wait_idle(5.0)
    assert sorted(t.tsr for t in anchorer.take_results()) == [
        b"tsr:first",
        b"tsr:other",
        b"tsr:third",
    ]
    anchorer.close()


def test_a_failure_replaced_meanwhile_is_not_retried(tmp_path: Path) -> None:
    tsa = Tsa(failures=1)
    tsa.release.clear()
    anchorer = Anchorer(tsa, lambda: None, give_up_after_s=60.0, retry_delays_s=(0.01,))
    anchorer.start()
    anchorer.submit(tmp_path / "a", "old")
    time.sleep(0.1)
    anchorer.submit(tmp_path / "a", "new")
    tsa.release.set()
    assert anchorer.wait_idle(5.0)
    assert [t.tsr for t in anchorer.take_results()] == [b"tsr:new"]
    assert tsa.calls == [b"old", b"new"]
    anchorer.close()


def test_close_stops_the_thread(tmp_path: Path) -> None:
    anchorer = Anchorer(Tsa(), lambda: None, give_up_after_s=60.0)
    anchorer.start()
    anchorer.close()
    anchorer._thread.join(5.0)
    assert not anchorer._thread.is_alive()
