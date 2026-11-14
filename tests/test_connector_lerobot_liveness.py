"""The rollout's process, watched read-only (connectors/lerobot_io.py ``Liveness``), and logs
captured on Windows. A rollout killed without a word must not keep its last state: once its
process is gone the robot is ``unknown`` (definitions.md 2, rule "unknown"; 6.14, lost input)."""

import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

from autonometry.connectors import lerobot, lerobot_io
from autonometry.connectors.lerobot_io import Liveness, LogFollower, ProcessInfo, is_rollout
from autonometry.connectors.tb4 import StateAssertion, TaskEnd
from lerobot_helpers import engine, line, setup_lines

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lerobot_windows"
OWN = 4242
GONE = lerobot.Note((("x_lerobot_process_exit", "unlogged"),))
UNWATCHED = lerobot.Note((("x_lerobot_liveness", "unwatched"),))


class _Process:
    def __init__(self) -> None:
        self.running = True

    def alive(self) -> bool:
        return self.running


def _info(pid: int, *cmdline: str, ppid: int | None = 1, name: str = "") -> ProcessInfo:
    return ProcessInfo(pid, ppid, name or Path(cmdline[0]).name if cmdline else name, cmdline)


ROLLOUT = _info(100, "/venv/bin/python3", "/venv/bin/lerobot-rollout", "--strategy.type=dagger")


class _Run:
    """A log, an engine, a liveness with a fake process table, and what was sent."""

    def __init__(self, tmp_path: Path, table: list[ProcessInfo], pid: int | None = None) -> None:
        self.log = tmp_path / "rollout.log"
        self.log.write_text("", encoding="utf-8")
        self.follower = LogFollower(self.log)
        self.engine = engine()
        self.processes = {p.pid: _Process() for p in table}
        self.opened: list[int] = []
        self.liveness = Liveness(pid, lister=lambda: list(table), opener=self._open, own_pid=OWN)
        self.sent: list[lerobot.Message] = []

    def _open(self, pid: int) -> lerobot_io.Watched | None:
        self.opened.append(pid)
        return self.processes.get(pid)

    def write(self, *messages: str, at: float = 0.0) -> None:
        with open(self.log, "a", encoding="utf-8") as stream:
            stream.writelines(line(m, at) + "\n" for m in messages)

    def pump(self, now: float) -> list[lerobot.Message]:
        start = len(self.sent)
        lerobot_io.pump(self.follower, self.engine, self.sent.extend, now, self.liveness)
        return self.sent[start:]

    def close(self) -> None:
        self.follower.close()


@pytest.fixture
def run(tmp_path: Path) -> Iterable[_Run]:
    found = _Run(tmp_path, [ROLLOUT])
    yield found
    found.close()


def _states(messages: Iterable[lerobot.Message]) -> list[str]:
    return [m.state for m in messages if isinstance(m, StateAssertion)]


def _started(run: _Run) -> None:
    run.write(*(text.split(" ", 4)[-1] for _, text in setup_lines()))
    run.write("DAgger continuous recording started (episode_duration=60s)", at=5)
    assert _states(run.pump(0.0))[-1] == "ai"


# -- engine -------------------------------------------------------------------------------------


def test_engine_gone_while_autonomous_is_unknown__defs_2_unknown_6_14() -> None:
    eng = engine()
    for _, text in setup_lines():
        parsed = lerobot.parse_line(text)
        if parsed is not None:
            eng.observe(0.0, parsed)
    started = lerobot.parse_line(line("Base strategy control loop started", 5))
    assert started is not None
    eng.observe(5.0, started)
    generation = eng.generation
    out = eng.process_gone(3600.0)
    assert _states(out) == ["unknown"]
    assert out[-1] == GONE
    assert not eng.following
    assert eng.generation == generation
    assert _states(eng.tick(7200.0)) == ["unknown"]
    assert eng.process_gone(7201.0) == []


def test_engine_gone_before_any_rollout_says_nothing__defs_2_unknown() -> None:
    assert engine().process_gone(1.0) == []


# -- which process ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmdline",
    [
        ("/venv/bin/lerobot-rollout", "--strategy.type=dagger"),
        (r"C:\venv\Scripts\lerobot-rollout.exe", "--fps=30"),
        ("/usr/bin/python3.12", "/venv/bin/lerobot-rollout", "--fps=30"),
        (r"C:\Python313\python.exe", r"C:\venv\Scripts\lerobot-rollout.exe", "--fps=30"),
        ("python", "-u", "-X", "dev", "-m", "lerobot.scripts.lerobot_rollout", "--fps=30"),
        ("python3", "-mlerobot.scripts.lerobot_rollout"),
        ("pythonw.exe", "src/lerobot/scripts/lerobot_rollout.py"),
        ("py.exe", "-3.12", "lerobot-rollout.py"),
    ],
)
def test_the_rollout_is_found_by_program_script_or_module(cmdline: tuple[str, ...]) -> None:
    assert is_rollout(_info(7, *cmdline))


@pytest.mark.parametrize(
    "cmdline",
    [
        ("tail", "-F", "lerobot-rollout.log"),
        ("autonometry", "lerobot", "watch", "--log", "lerobot-rollout.log"),
        ("python", "-m", "autonometry", "lerobot", "watch", "--log", "lerobot-rollout"),
        ("python", "lerobot-rollout.log"),
        ("python", "-c", "import lerobot-rollout"),
        ("python", "-m", "lerobot.scripts.lerobot_record"),
        ("python", "watch.py", "lerobot-rollout"),
        ("uv", "run", "lerobot-rollout"),
        ("python", "-X"),
        ("lerobot-rollout.log",),
    ],
)
def test_other_programs_are_not_the_rollout(cmdline: tuple[str, ...]) -> None:
    assert not is_rollout(_info(7, *cmdline))


def test_a_process_known_only_by_name_is_found() -> None:
    assert is_rollout(ProcessInfo(7, 1, "lerobot-rollout", ()))
    assert not is_rollout(ProcessInfo(7, 1, "bash", ()))


def test_a_launcher_and_its_python_are_one_rollout__watch_the_deepest() -> None:
    table = [
        _info(10, "uv", "run", "lerobot-rollout", ppid=1),
        _info(20, r"C:\venv\Scripts\lerobot-rollout.exe", "--fps=30", ppid=10),
        _info(30, r"C:\venv\Scripts\python.exe", r"C:\venv\Scripts\lerobot-rollout.exe", ppid=20),
        _info(40, r"C:\Python313\python.exe", r"C:\venv\Scripts\lerobot-rollout.exe", ppid=30),
    ]
    assert lerobot_io.rollout_pids(table, OWN) == [40]


def test_two_rollouts_stay_two_and_the_watcher_is_never_one() -> None:
    table = [ROLLOUT, _info(200, "lerobot-rollout", ppid=1), _info(OWN, "lerobot-rollout")]
    assert lerobot_io.rollout_pids(table, OWN) == [100, 200]


def test_a_parent_loop_in_the_table_ends() -> None:
    table = [_info(1, "lerobot-rollout", ppid=2), _info(2, "init", ppid=1)]
    assert lerobot_io.rollout_pids(table, OWN) == [1]


# -- watching -----------------------------------------------------------------------------------


def test_gone_while_autonomous_closes_the_task_and_is_unknown__defs_2_unknown_6_14(
    run: _Run, caplog: pytest.LogCaptureFixture
) -> None:
    _started(run)
    assert run.opened == [100]
    run.processes[100].running = False
    assert run.pump(0.5)[-1:] != [GONE]
    out = run.pump(1.0)
    assert [type(m).__name__ for m in out] == [
        "TaskEnd",
        "StateAssertion",
        "Note",
        "StateAssertion",
    ]
    assert isinstance(out[0], TaskEnd)
    assert out[0].outcome == "unknown"
    assert out[2] == GONE
    assert _states(out) == ["unknown", "unknown"]
    assert "ended without logging 'Rollout finished'" in caplog.text
    assert _states(run.pump(3600.0)) == ["unknown"]
    assert GONE not in run.pump(3601.0)


def test_gone_after_rollout_finished_adds_nothing__defs_2_unknown(
    run: _Run, caplog: pytest.LogCaptureFixture
) -> None:
    _started(run)
    run.write("Rollout finished", at=9)
    assert _states(run.pump(0.2)) == ["unknown", "unknown"]
    run.processes[100].running = False
    assert GONE not in run.pump(2.0)
    assert "without logging" not in caplog.text


def test_lines_left_in_the_log_come_before_the_exit__defs_2_unknown_6_14(run: _Run) -> None:
    _started(run)
    run.processes[100].running = False
    out: list[lerobot.Message] = []
    lerobot_io.pump(run.follower, run.engine, out.extend, 0.5, run.liveness)
    run.write("Phase transition: autonomous -> paused", at=6)
    run.liveness.check(1.0, run.follower, run.engine, out.extend)
    assert _states(out)[-2:] == ["shared", "unknown"]
    assert out[-1] == GONE


def test_lines_left_that_finish_the_rollout_need_no_note__defs_2_unknown(run: _Run) -> None:
    _started(run)
    run.processes[100].running = False
    run.write("Rollout finished", at=9)
    out: list[lerobot.Message] = []
    run.liveness.check(1.0, run.follower, run.engine, out.extend)
    assert _states(out) == ["unknown"]
    assert GONE not in out


def test_no_rollout_process_is_unwatched_and_the_log_decides__defs_6_14(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run = _Run(tmp_path, [_info(5, "tail", "-F", "lerobot-rollout.log")])
    try:
        _started(run)
        assert UNWATCHED in run.sent
        assert "no lerobot-rollout process on this computer" in caplog.text
        assert _states(run.pump(3600.0)) == ["ai"]
    finally:
        run.close()


def test_two_rollout_processes_are_unwatched__defs_6_14(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run = _Run(tmp_path, [ROLLOUT, _info(200, "lerobot-rollout")])
    try:
        _started(run)
        assert UNWATCHED in run.sent
        assert run.opened == []
        assert "2 lerobot-rollout processes here; name one with --pid" in caplog.text
    finally:
        run.close()


def test_pid_names_the_process__defs_6_14(tmp_path: Path) -> None:
    run = _Run(tmp_path, [ROLLOUT, _info(200, "lerobot-rollout")], pid=200)
    try:
        _started(run)
        assert run.opened == [200]
        assert UNWATCHED not in run.sent
        run.processes[200].running = False
        assert GONE in run.pump(1.0)
    finally:
        run.close()


def test_a_pid_with_no_running_process_is_unwatched__defs_6_14(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run = _Run(tmp_path, [], pid=999)
    try:
        _started(run)
        assert UNWATCHED in run.sent
        assert "no running process has --pid 999" in caplog.text
    finally:
        run.close()


def test_a_new_rollout_in_the_same_log_is_looked_up_again__defs_6_14(run: _Run) -> None:
    _started(run)
    run.write("Rollout finished", at=9)
    run.pump(0.5)
    run.write("Building rollout context...", at=20)
    run.pump(2.0)
    assert run.opened == [100, 100]


def test_the_process_is_checked_about_once_a_second(run: _Run) -> None:
    _started(run)
    run.processes[100].running = False
    assert GONE not in run.pump(0.9)
    assert GONE in run.pump(1.0)


def test_psutil_handles_report_this_process_alive_and_a_missing_one_absent() -> None:
    pytest.importorskip("psutil")
    handle = lerobot_io.open_process(lerobot_io.os.getpid())
    assert handle is not None
    assert handle.alive()
    assert lerobot_io.open_process(2**30) is None
    assert any(p.pid == lerobot_io.os.getpid() for p in lerobot_io.list_processes())


CHILD = """\
import pathlib, sys, time
log, stop = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
def write(message):
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(f"INFO 2026-11-14 10:00:00 _rollout.py:219 {message}\\n")
write("Building rollout context...")
write("DAgger continuous recording started (episode_duration=60s)")
while not stop.exists():
    time.sleep(0.05)
write("Phase transition: autonomous -> paused")
"""


def _wait(predicate: object, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():  # type: ignore[operator]
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_a_real_rollout_process_that_exits_unlogged_is_unknown__defs_2_unknown_6_14(
    tmp_path: Path,
) -> None:
    pytest.importorskip("psutil")
    script = tmp_path / "lerobot-rollout.py"
    script.write_text(CHILD, encoding="utf-8")
    log, stop = tmp_path / "rollout.log", tmp_path / "stop"
    child = subprocess.Popen([sys.executable, str(script), str(log), str(stop)])  # noqa: S603 -- fixed arguments
    follower = LogFollower(log, from_start=True)
    try:
        _wait(lambda: log.exists() and "started" in log.read_text(encoding="utf-8"))
        eng, sent = engine(), []
        liveness = Liveness()
        lerobot_io.pump(follower, eng, sent.extend, 0.0, liveness)
        assert UNWATCHED not in sent
        assert _states(sent)[-1] == "ai"
        stop.touch()
        child.wait(timeout=30)
        lerobot_io.pump(follower, eng, sent.extend, 2.0, liveness)
        assert _states(sent)[-3:] == ["shared", "unknown", "unknown"]
        assert GONE in sent
    finally:
        stop.touch()
        child.wait(timeout=30)
        follower.close()


# -- logs captured on Windows -------------------------------------------------------------------

ALL_SIGNALS = 21


def _signals(path: Path) -> list[str]:
    follower = LogFollower(path, from_start=True)
    try:
        lines = follower.read()
    finally:
        follower.close()
    return [type(p.signal).__name__ for text in lines if (p := lerobot.parse_line(text))]


@pytest.mark.parametrize("name", ["ps51_foreach.log", "cmd_redirect.log", "cmd_utf8.log"])
def test_the_readme_s_windows_commands_give_every_line(name: str) -> None:
    signals = _signals(FIXTURES / name)
    assert len(signals) == ALL_SIGNALS
    assert signals.count("LoopEnd") == 4
    assert signals[0] == "ProcessStart"
    assert "PolicyPath" in signals
    assert "RerecordKey" in signals


def test_plain_tee_object_loses_only_the_wrapped_line() -> None:
    signals = _signals(FIXTURES / "ps51_tee.log")
    assert len(signals) == ALL_SIGNALS - 1
    assert "PolicyPath" not in signals
    assert signals[0] == "ProcessStart"


def test_python_holds_the_printed_line_unless_unbuffered() -> None:
    assert _signals(FIXTURES / "cmd_redirect.log")[-1] == "RerecordKey"
    unbuffered = _signals(FIXTURES / "cmd_utf8.log")
    assert unbuffered.index("RerecordKey") == unbuffered.index("ResetStart") + 1


@pytest.mark.parametrize("dash", ["\u2014", "\ufffd", "\u0393\u00c7\u00f6", "\u00e2\u20ac\u201d"])
def test_the_dash_is_accepted_in_any_code_page(dash: str) -> None:
    parsed = lerobot.parse_line(line(f"DAgger continuous control loop ended {dash} pausing engine"))
    assert parsed is not None
    assert parsed.signal == lerobot.LoopEnd()


def test_powershell_s_program_prefix_is_skipped() -> None:
    parsed = lerobot.parse_line("python.exe : " + line("Rollout finished", 2))
    assert parsed is not None
    assert parsed.signal == lerobot.ProcessEnd()
    assert parsed.src_ts is not None


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")],
)
def test_a_byte_order_mark_chooses_the_encoding(tmp_path: Path, encoding: str, bom: bytes) -> None:
    log = tmp_path / "rollout.log"
    log.write_bytes(b"")
    follower = LogFollower(log)
    try:
        log.write_bytes(bom[:1])
        assert follower.read() == []
        with open(log, "ab") as stream:
            stream.write(bom[1:] + "a \u2014 b\r\nsecond\r\npart".encode(encoding))
        assert follower.read() == ["a \u2014 b", "second"]
        with open(log, "ab") as stream:
            stream.write("ial\n".encode(encoding))
        assert follower.read() == ["partial"]
    finally:
        follower.close()


def test_a_utf16_log_followed_from_its_end_still_decodes(tmp_path: Path) -> None:
    log = tmp_path / "rollout.log"
    log.write_bytes(b"\xff\xfe" + "old\r\n".encode("utf-16-le"))
    follower = LogFollower(log)
    try:
        with open(log, "ab") as stream:
            stream.write("new\r\n".encode("utf-16-le"))
        assert follower.read() == ["new"]
    finally:
        follower.close()
