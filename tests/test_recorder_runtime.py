"""Tests for the recorder writing sealed logs in-process: layout, durability, crash recovery,
rotation and anchoring (README "Recorder"; definitions.md 2, 8, 10.3, 10.4, 10.6)."""

import hashlib
import itertools
import json
import os
import re
import stat
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from autonometry import sealfile, signednote
from autonometry.reader import read_logs
from autonometry.recorder.anchor import Anchorer
from autonometry.recorder.config import Prepared, json_hash
from autonometry.recorder.engine import PLACEHOLDER_SESSION_ID
from autonometry.recorder.runtime import Recorder, tsa_anchorer
from autonometry.recorder.store import CHECKPOINTS_FILE, EVENTS_FILE
from autonometry.registry import check_registry
from recorder_helpers import (
    SPEC,
    Client,
    CrashingOps,
    FakeClock,
    RecordingOps,
    all_events,
    assert_all_verified,
    check_logs,
    events_of,
    heartbeats,
    make_prepared,
    make_recorder,
    run_until_crash,
    session_folders,
    transitions,
    with_data_dir,
)

SESSION_RE = re.compile(r"[0-9]{6}-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}")


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> Prepared:
    return make_prepared(tmp_path_factory.mktemp("recorder"))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def setup(prepared: Prepared, data_dir: Path) -> Prepared:
    return with_data_dir(prepared, data_dir)


def traffic(seconds: float, step_s: float = 0.25) -> Iterator[tuple[str, Any]]:
    """A connector's level-triggered assertions with a task, a takeover and odometry."""
    nav, arm, ui, odom = Client("nav"), Client("arm"), Client("ui"), Client("odom")
    t = 0.0
    task = 0
    while t < seconds:
        phase = int(t) % 10
        if phase < 6:
            yield "datagram", nav.state("base", "ai", actor_id="ai:nav2", device_id="nav2")
        else:
            yield (
                "datagram",
                nav.state(
                    "base",
                    "human_local",
                    reason="human_initiated",
                    actor_id="op_17",
                    device_id="joy_local",
                ),
            )
        yield "datagram", arm.state("arm", "idle")
        if t % 10 == 0:
            task += 1
            yield "datagram", ui.event("task_start", task_id=f"t{task}", task_definition_id="d")
        if t % 10 == 9:
            yield "datagram", ui.event("task_end", task_id=f"t{task}", outcome="success")
        yield "datagram", odom.distance(t * 0.3)
        yield "advance", step_s
        t += step_s


def drive(recorder: Recorder, clock: FakeClock, actions: Iterator[tuple[str, Any]]) -> None:
    for kind, value in actions:
        step(recorder, clock, kind, value)


def step(recorder: Recorder, clock: FakeClock, kind: str, value: Any) -> None:
    if kind == "datagram":
        recorder.receive(value)
    else:
        clock.advance(value)
        recorder.tick()


# -- a session from start to stop --------------------------------------------------------------


def test_a_fresh_session_is_sealed_and_verifies__defs_8_10_6(
    setup: Prepared, data_dir: Path
) -> None:
    clock = FakeClock()
    recorder = make_recorder(setup, clock)
    recorder.start()
    drive(recorder, clock, traffic(30))
    recorder.stop()

    (folder,) = session_folders(data_dir)
    assert SESSION_RE.fullmatch(folder.name)
    assert folder.name.startswith("000001-20261112T080000Z-")
    events = events_of(folder)
    assert [e["seq"] for e in events] == list(range(len(events)))
    start = events[0]
    assert start["type"] == "session_start"
    assert start["initial_states"] == {"base": "unknown", "arm": "unknown"}
    assert start["scopes"] == ["base", "arm"]
    assert start["simulated"] is True
    assert start["heartbeat_interval_s"] == 1
    assert start["hint_policy"] == "hint_as_shared"
    registry = check_registry(setup.config.device_registry, SPEC)  # type: ignore[arg-type]
    assert start["device_registry_hash"] == registry.sha256
    assert start["task_registry_hash"] == setup.task_registry_hash
    assert events[1]["type"] == "heartbeat"
    assert events[-2]["type"] == "heartbeat"
    assert (events[-1]["type"], events[-1]["x_cause"]) == ("session_end", "shutdown")
    assert len(heartbeats(events)) == 32
    assert all(h["clock_source"] == "ntp" for h in heartbeats(events))
    assert heartbeats(events)[-1]["distance_m"] == pytest.approx(29.75 * 0.3)
    base = [t["state"] for t in transitions(events, "base")]
    assert base == ["ai", "human_local", "ai", "human_local", "ai", "human_local"]
    assert_all_verified(setup, data_dir)


def test_validate_flags_nothing_on_a_clean_session__defs_8(setup: Prepared, data_dir: Path) -> None:
    clock = FakeClock()
    recorder = make_recorder(setup, clock)
    recorder.start()
    drive(recorder, clock, traffic(10))
    recorder.stop()
    (folder,) = session_folders(data_dir)
    result = read_logs([folder / EVENTS_FILE], SPEC)
    assert result.counts.rejected_lines == 0
    assert [f.code for f in result.flags] == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
def test_folders_are_0700_and_files_0600(setup: Prepared, data_dir: Path) -> None:
    recorder = make_recorder(setup, FakeClock())
    recorder.start()
    recorder.stop()
    (folder,) = session_folders(data_dir)
    for path in (data_dir, data_dir / "r1", folder):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
    for path in (folder / EVENTS_FILE, folder / CHECKPOINTS_FILE):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_an_event_of_the_longest_sealed_line_passes_validate__defs_8(
    setup: Prepared, data_dir: Path
) -> None:
    clock = FakeClock()
    recorder = make_recorder(setup, clock)
    recorder.start()
    probe = Client("probe")
    recorder.receive(probe.event("help_request", scope="base", x_pad=""))
    (folder,) = session_folders(data_dir)
    line = (folder / EVENTS_FILE).read_bytes().splitlines()[-1]
    # The check assumes the longest session id and seq: the real line is that much shorter.
    slack = (16 - len(str(json.loads(line)["seq"]))) + (
        len(PLACEHOLDER_SESSION_ID) - len(folder.name)
    )
    room = 65536 - len(line) - slack
    recorder.receive(probe.event("help_request", scope="base", x_pad="p" * room))
    recorder.stop()
    longest = max(len(raw) for raw in (folder / EVENTS_FILE).read_bytes().splitlines())
    assert longest == 65536 - slack
    assert_all_verified(setup, data_dir)


# -- durability --------------------------------------------------------------------------------


def test_no_checkpoint_covers_an_event_not_yet_fsynced__defs_10_3(
    setup: Prepared, data_dir: Path
) -> None:
    ops = RecordingOps()
    clock = FakeClock()
    recorder = make_recorder(setup, clock, ops)
    recorder.start()
    nav = Client("nav")
    for index in range(2500):
        recorder.receive(nav.event("help_request", scope="base", x_i=index))
        if index % 100 == 0:
            clock.advance(0.3)
            recorder.tick()
    recorder.stop()

    (folder,) = session_folders(data_dir)
    offsets = [0]
    for raw in (folder / EVENTS_FILE).read_bytes().splitlines(keepends=True):
        offsets.append(offsets[-1] + len(raw))
    events_synced = 0
    checkpoints = 0
    previous: tuple[str, Path] | None = None
    for op, path, size, data in ops.log:
        if path.name == EVENTS_FILE and op == "fsync":
            events_synced = size
        if previous is not None and previous[1].name == CHECKPOINTS_FILE and previous[0] == "write":
            assert (op, path) == ("fsync", previous[1]), "a checkpoint record is fsynced at once"
        if path.name == CHECKPOINTS_FILE and op == "write":
            record = json.loads(data)
            body = signednote.parse_checkpoint_body(record["note"].split("\n\n")[0] + "\n")
            assert offsets[body.size] <= events_synced
            checkpoints += 1
        previous = (op, path)
    assert checkpoints >= 4  # first event, 1000, 2000 and the final one


def test_events_are_fsynced_at_most_every_fsync_interval(
    prepared: Prepared, data_dir: Path
) -> None:
    setup = with_data_dir(prepared, data_dir)
    ops = RecordingOps()
    clock = FakeClock()
    recorder = make_recorder(setup, clock, ops)
    recorder.start()
    nav = Client("nav")
    for _ in range(200):
        recorder.receive(nav.event("help_request", scope="base"))
        clock.advance(0.05)
        recorder.tick()
    fsyncs = [op for op, path, *_ in ops.log if op == "fsync" and path.name == EVENTS_FILE]
    recorder.stop()
    # 10 s of traffic: one fsync when the first checkpoint is written, then at most one a second.
    assert 9 <= len(fsyncs) <= 13


# -- crash recovery ----------------------------------------------------------------------------


def crash_and_restart(
    prepared: Prepared,
    data_dir: Path,
    ops: CrashingOps,
    power_cut: bool,
    seconds: float = 20,
) -> Recorder:
    clock = FakeClock()
    recorder = make_recorder(prepared, clock, ops)
    crashed = run_until_crash(
        [("start", None), *traffic(seconds), ("stop", None)],
        lambda a: (
            recorder.start()
            if a[0] == "start"
            else recorder.stop()
            if a[0] == "stop"
            else step(recorder, clock, *a)
        ),
    )
    assert crashed
    if power_cut:
        ops.power_cut()
    else:
        ops.close_all()
    clock.advance(30)
    restarted = make_recorder(prepared, clock)
    restarted.start()
    drive(restarted, clock, traffic(5))
    restarted.stop()
    return restarted


def nth_op(prepared: Prepared, probe_dir: Path, op: str, which: int) -> int:
    """The 1-based index of the ``which``-th ``op`` on the events file in the scenario that
    :func:`crash_and_restart` runs, found by running it once without crashing."""
    ops = RecordingOps()
    clock = FakeClock()
    recorder = make_recorder(with_data_dir(prepared, probe_dir), clock, ops)
    recorder.start()
    drive(recorder, clock, traffic(20))
    recorder.stop()
    found = [
        index
        for index, (kind, path, *_) in enumerate(ops.log, start=1)
        if kind == op and path.name == EVENTS_FILE
    ]
    return found[which]


def recovery_heartbeat(data_dir: Path) -> dict[str, Any]:
    return next(h for h in heartbeats(all_events(data_dir)) if h.get("x_recovered"))


def test_crash_mid_line_cuts_the_partial_line_and_continues__defs_10_1(
    setup: Prepared, data_dir: Path, tmp_path: Path
) -> None:
    crash_at = nth_op(setup, tmp_path / "probe", "write", 20)
    crash_and_restart(setup, data_dir, CrashingOps(crash_at, torn=True), power_cut=False)
    (folder,) = session_folders(data_dir)
    heartbeat = recovery_heartbeat(data_dir)
    assert heartbeat["x_truncated_bytes"] > 0
    events = events_of(folder)
    after = events[events.index(heartbeat) + 1 :]
    assert {e["state"] for e in transitions(after[:2])} == {"unknown"}
    assert {e["x_cause"] for e in transitions(after[:2])} == {"recovered"}
    assert_all_verified(setup, data_dir)


@pytest.mark.parametrize("power_cut", [False, True])
def test_crash_between_a_write_and_its_fsync_recovers__defs_10_3(
    setup: Prepared, data_dir: Path, tmp_path: Path, power_cut: bool
) -> None:
    crash_at = nth_op(setup, tmp_path / "probe", "fsync", 4)
    crash_and_restart(setup, data_dir, CrashingOps(crash_at), power_cut=power_cut)
    assert recovery_heartbeat(data_dir)["x_truncated_bytes"] == 0
    assert_all_verified(setup, data_dir)
    gaps = [f for f in read_logs(_logs(data_dir), SPEC).flags if f.code == "time_gap"]
    assert len(gaps) == 1  # the downtime is a gap, unknown by definitions.md 2 rule 2


def _logs(data_dir: Path) -> list[Path]:
    return [folder / EVENTS_FILE for folder in session_folders(data_dir)]


class FakeTsa:
    """Returns a recognisable token for each note and records the notes it saw."""

    def __init__(self) -> None:
        self.notes: list[bytes] = []
        self.lock = threading.Lock()

    def __call__(self, note: bytes) -> bytes:
        with self.lock:
            self.notes.append(note)
        return b"token:" + hashlib.sha256(note).digest()


def unreachable_tsa(note: bytes) -> bytes:
    raise OSError("TSA down (simulated)")


def anchorer(tsa: Callable[[bytes], bytes]) -> Anchorer:
    return Anchorer(tsa, lambda: None, give_up_after_s=3600.0)


def test_crash_between_a_checkpoint_and_its_token_anchors_on_restart__defs_10_5(
    setup: Prepared, data_dir: Path
) -> None:
    clock = FakeClock()
    tsa = FakeTsa()
    first = anchorer(unreachable_tsa)
    ops = CrashingOps(crash_at=10_000)
    recorder = make_recorder(setup, clock, ops, anchorer=first)
    recorder.start()  # the first checkpoint is written; its token never arrives
    drive(recorder, clock, traffic(3))
    first.close()
    ops.close_all()
    clock.advance(10)
    second = anchorer(tsa)
    restarted = make_recorder(setup, clock, anchorer=second)
    restarted.start()
    drive(restarted, clock, traffic(2))
    restarted.stop()

    (folder,) = session_folders(data_dir)
    records = [json.loads(line) for line in (folder / CHECKPOINTS_FILE).read_bytes().splitlines()]
    notes = {
        hashlib.sha256(r["note"].encode()).hexdigest(): r["note"]
        for r in records
        if r["type"] == "checkpoint"
    }
    stamps = [r for r in records if r["type"] == "timestamp"]
    assert stamps, "the recovery or final checkpoint was timestamped"
    for stamp in stamps:
        assert stamp["note_sha256"] in notes
    assert_all_verified(setup, data_dir, drop_timestamps=True)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    actions=st.lists(
        st.one_of(
            st.tuples(
                st.just("state"),
                st.integers(0, 2),
                st.sampled_from(["base", "arm"]),
                st.sampled_from(["ai", "idle", "human_remote", "scripted", "unknown"]),
            ),
            st.tuples(st.just("help"), st.integers(0, 2)),
            st.tuples(st.just("lose"), st.integers(0, 2)),
            st.tuples(st.just("garbage"), st.binary(max_size=40)),
            st.tuples(st.just("advance"), st.floats(0.0, 3.0)),
        ),
        max_size=60,
    ),
    crash_at=st.integers(1, 120),
    torn=st.booleans(),
    power_cut=st.booleans(),
)
def test_random_traffic_and_crash_points_always_verify__defs_10_6(
    prepared: Prepared,
    actions: list[tuple[Any, ...]],
    crash_at: int,
    torn: bool,
    power_cut: bool,
) -> None:
    with tempfile.TemporaryDirectory() as folder:
        data_dir = Path(folder) / "data"
        setup = with_data_dir(prepared, data_dir)
        clock = FakeClock()
        clients = [Client("a"), Client("b"), Client("c")]

        def act(recorder: Recorder, action: tuple[Any, ...]) -> None:
            kind = action[0]
            if kind == "state":
                _, who, scope, state = action
                fields = {"reason": "human_initiated", "actor_id": "op_1"}
                extra = fields if state == "human_remote" else {}
                recorder.receive(clients[who].state(scope, state, **extra))
            elif kind == "help":
                recorder.receive(clients[action[1]].event("help_request", scope="base"))
            elif kind == "lose":
                clients[action[1]].n += 1
            elif kind == "garbage":
                recorder.receive(action[1])
            else:
                clock.advance(action[1])
                recorder.tick()

        ops = CrashingOps(crash_at, torn)
        recorder = make_recorder(setup, clock, ops)

        def first_run(action: tuple[Any, ...]) -> None:
            if action[0] == "start":
                recorder.start()
            elif action[0] == "stop":
                recorder.stop()
            else:
                act(recorder, action)

        crashed = run_until_crash([("start",), *actions, ("stop",)], first_run)
        if crashed:
            if power_cut:
                ops.power_cut()
            else:
                ops.close_all()
            clock.advance(5)
            restarted = make_recorder(setup, clock)
            restarted.start()
            for action in actions[:10]:
                act(restarted, action)
            restarted.stop()
        assert_all_verified(setup, data_dir)


def test_a_session_that_does_not_verify_is_left_as_it_is(setup: Prepared, data_dir: Path) -> None:
    clock = FakeClock()
    ops = CrashingOps(crash_at=10_000)
    recorder = make_recorder(setup, clock, ops)
    recorder.start()
    drive(recorder, clock, traffic(3))
    ops.close_all()
    (folder,) = session_folders(data_dir)
    path = folder / EVENTS_FILE
    tampered = path.read_bytes().replace(b'"state":"ai"', b'"state":"human_local"', 1)
    path.write_bytes(tampered)
    clock.advance(5)
    restarted = make_recorder(setup, clock)
    restarted.start()
    restarted.stop()
    old, new = session_folders(data_dir)
    assert old == folder
    assert path.read_bytes() == tampered
    assert events_of(new)[0]["type"] == "session_start"


def test_a_closed_newest_session_starts_a_new_one(setup: Prepared, data_dir: Path) -> None:
    clock = FakeClock()
    first = make_recorder(setup, clock)
    first.start()
    first.stop()
    clock.advance(2)
    second = make_recorder(setup, clock)
    second.start()
    second.stop()
    assert len(session_folders(data_dir)) == 2
    assert_all_verified(setup, data_dir)


def test_a_session_without_a_complete_event_is_left_and_a_new_one_starts(
    setup: Prepared, data_dir: Path
) -> None:
    clock = FakeClock()
    ops = CrashingOps(crash_at=3, torn=True)  # in the write of session_start
    recorder = make_recorder(setup, clock, ops)
    assert run_until_crash([None], lambda _: recorder.start())
    ops.close_all()
    clock.advance(2)
    restarted = make_recorder(setup, clock)
    restarted.start()
    restarted.stop()
    empty, new = session_folders(data_dir)
    assert (empty / EVENTS_FILE).read_bytes() == b""
    assert events_of(new)[0]["type"] == "session_start"
    assert_all_verified(setup, data_dir)


def test_a_changed_config_closes_the_recovered_session(
    prepared: Prepared, data_dir: Path, tmp_path: Path
) -> None:
    setup = with_data_dir(prepared, data_dir)
    clock = FakeClock()
    ops = CrashingOps(crash_at=10_000)
    recorder = make_recorder(setup, clock, ops)
    recorder.start()
    drive(recorder, clock, traffic(3))
    ops.close_all()
    changed = replace(setup, config=setup.config.model_copy(update={"simulated": False}))
    clock.advance(5)
    restarted = make_recorder(changed, clock)
    restarted.start()
    restarted.stop()
    old, new = session_folders(data_dir)
    old_events = events_of(old)
    assert old_events[-1]["x_cause"] == "config_changed"
    assert any(h.get("x_recovered") for h in heartbeats(old_events))
    assert events_of(new)[0]["simulated"] is False
    assert_all_verified(setup, data_dir)


def test_partial_lines_of_older_checkpoint_files_are_cut(setup: Prepared, data_dir: Path) -> None:
    clock = FakeClock()
    first = make_recorder(setup, clock)
    first.start()
    first.stop()
    (folder,) = session_folders(data_dir)
    with open(folder / CHECKPOINTS_FILE, "ab") as stream:
        stream.write(b'{"type": "timest')
    clock.advance(2)
    second = make_recorder(setup, clock)
    second.start()
    second.stop()
    assert (folder / CHECKPOINTS_FILE).read_bytes().endswith(b"\n")
    assert_all_verified(setup, data_dir)


# -- rotation and anchoring --------------------------------------------------------------------


def test_rotation_ends_the_session_and_carries_states_over(
    prepared: Prepared, data_dir: Path
) -> None:
    limit = 1 << 20
    setup = with_data_dir(prepared, data_dir)
    setup = replace(setup, config=setup.config.model_copy(update={"max_log_bytes": limit}))
    clock = FakeClock()
    tsa = FakeTsa()
    tokens = anchorer(tsa)
    recorder = make_recorder(setup, clock, anchorer=tokens)
    recorder.start()
    nav, reset = Client("nav"), Client("reset")
    planned = {"reason": "planned", "actor_id": "tech_02", "plan_item_id": "reset_scene"}
    recorder.receive(reset.event("task_start", task_id="t1", task_definition_id="dock_v1"))
    for index in range(40):
        recorder.receive(nav.event("help_request", scope="base", x_pad="p" * 60000, x_i=index))
        recorder.receive(nav.state("base", "ai", actor_id="ai:nav2", device_id="nav2"))
        recorder.receive(reset.state("arm", "human_local", device_id="joy_local", **planned))
        clock.advance(0.1)
        recorder.tick()
    recorder.stop()
    folders = session_folders(data_dir)
    assert len(folders) >= 2
    for older, newer in itertools.pairwise(folders):
        assert (older / EVENTS_FILE).stat().st_size <= limit
        assert events_of(older)[-1]["x_cause"] == "rotation"
        start, *carried = events_of(newer)[:3]
        assert start["initial_states"] == {"base": "ai", "arm": "human_local"}
        assert start["x_carried_task"] == "t1"
        assert [
            {k: e.get(k) for k in ("scope", "state", "reason", "actor_id", "device_id")}
            for e in carried
        ] == [
            {
                "scope": "base",
                "state": "ai",
                "reason": None,
                "actor_id": "ai:nav2",
                "device_id": "nav2",
            },
            {
                "scope": "arm",
                "state": "human_local",
                "reason": "planned",
                "actor_id": "tech_02",
                "device_id": "joy_local",
            },
        ]
        assert carried[1]["plan_item_id"] == "reset_scene"
        assert {e["x_cause"] for e in carried} == {"rotation"}
        assert {e["task_id"] for e in carried} == {"t1"}
    stamped = {
        folder.name
        for folder in folders
        if b'"timestamp"' in (folder / CHECKPOINTS_FILE).read_bytes()
    }
    assert stamped == {folder.name for folder in folders}
    assert_all_verified(setup, data_dir, drop_timestamps=True)


def test_tokens_are_appended_as_timestamp_records__defs_10_4(
    setup: Prepared, data_dir: Path
) -> None:
    clock = FakeClock()
    tsa = FakeTsa()
    tokens = anchorer(tsa)
    recorder = make_recorder(setup, clock, anchorer=tokens)
    recorder.start()
    assert tokens.wait_idle(5.0)
    recorder.tick()
    recorder.stop()
    (folder,) = session_folders(data_dir)
    with open(folder / CHECKPOINTS_FILE, "rb") as stream:
        records = sealfile.read_seal_file("c", stream, 65536, 100, SPEC).records
    kinds = [type(r).__name__ for r in records]
    assert kinds == ["CheckpointLine", "TimestampLine", "CheckpointLine", "TimestampLine"]
    for checkpoint, stamp in zip(records[::2], records[1::2], strict=True):
        assert isinstance(checkpoint, sealfile.CheckpointLine)
        assert isinstance(stamp, sealfile.TimestampLine)
        note = checkpoint.note.encode()
        assert stamp.note_sha256 == hashlib.sha256(note).hexdigest()
        assert stamp.tsr == b"token:" + hashlib.sha256(note).digest()


def test_the_recorder_writes_through_os_ops_by_default(setup: Prepared, data_dir: Path) -> None:
    recorder = Recorder(setup, FakeClock())
    recorder.start()
    recorder.tick()
    assert recorder.next_deadline() < float("inf")
    recorder.stop()
    assert os.path.getsize(session_folders(data_dir)[0] / EVENTS_FILE) > 0
    with pytest.raises(RuntimeError, match="no open session"):
        recorder.receive(Client("late").event("help_request", scope="base"))


def test_a_configured_tsa_gets_an_anchorer(prepared: Prepared) -> None:
    assert tsa_anchorer(prepared, lambda: None) is None
    root = Path(__file__).parent / "fixtures" / "tsa" / "freetsa_root.pem"
    config = prepared.config.model_copy(
        update={"tsa_url": "https://tsa.example/tsr", "tsa_roots": root}
    )
    assert isinstance(tsa_anchorer(replace(prepared, config=config), lambda: None), Anchorer)


@pytest.mark.skipif(sys.platform != "win32", reason="the refusal is for Windows")
def test_record_refuses_to_run_on_windows(tmp_path: Path) -> None:
    from typer.testing import CliRunner  # noqa: PLC0415

    from autonometry import cli  # noqa: PLC0415

    result = CliRunner().invoke(cli.app, ["record", "--config", str(tmp_path / "r.toml")])
    assert result.exit_code == 2
    assert "Linux and macOS only" in result.output


def test_a_crash_before_the_final_checkpoint_commits_the_closed_session__defs_10_3(
    setup: Prepared, data_dir: Path, tmp_path: Path
) -> None:
    probe = RecordingOps()
    recorder = make_recorder(with_data_dir(setup, tmp_path / "probe"), FakeClock(), probe)
    recorder.start()
    recorder.stop()
    final = max(
        index
        for index, (op, path, *_) in enumerate(probe.log, start=1)
        if op == "write" and path.name == CHECKPOINTS_FILE
    )
    ops = CrashingOps(crash_at=final - 1)  # the fsync of the events before it
    clock = FakeClock()
    crashing = make_recorder(setup, clock, ops)
    assert run_until_crash([None], lambda _: (crashing.start(), crashing.stop()))
    ops.close_all()
    clock.advance(5)
    restarted = make_recorder(setup, clock)
    restarted.start()
    restarted.stop()
    closed, new = session_folders(data_dir)
    assert events_of(closed)[-1]["type"] == "session_end"
    assert events_of(new)[0]["type"] == "session_start"
    assert_all_verified(setup, data_dir)


def test_every_session_folder_holds_its_registries_and_plan__defs_6_10(
    setup: Prepared, data_dir: Path
) -> None:
    recorder = make_recorder(setup, FakeClock())
    recorder.start()
    recorder.stop()
    (folder,) = session_folders(data_dir)
    for name, source in [
        ("devices.json", setup.config.device_registry),
        ("tasks.json", setup.config.task_registry),
        ("plan.json", setup.config.plan),
    ]:
        assert (folder / name).read_bytes() == source.read_bytes()
        if sys.platform != "win32":
            assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600
    start = events_of(folder)[0]
    assert start["device_registry_hash"] == check_registry(folder / "devices.json", SPEC).sha256
    assert start["task_registry_hash"] == json_hash((folder / "tasks.json").read_bytes(), folder)
    assert start["plan_hash"] == json_hash((folder / "plan.json").read_bytes(), folder)


def test_the_newest_session_is_the_largest_counter_whatever_the_clock(
    setup: Prepared, data_dir: Path
) -> None:
    clock = FakeClock()
    first = make_recorder(setup, clock)
    first.start()
    first.stop()
    clock.wall -= 6 * 365 * 24 * 3600 * 10**9  # booted with no real-time clock: back to 2020
    ops = CrashingOps(crash_at=10_000)
    second = make_recorder(setup, clock, ops)
    second.start()
    drive(second, clock, traffic(3))
    ops.close_all()  # crashed
    clock.wall += 6 * 365 * 24 * 3600 * 10**9  # the clock is synchronised again
    third = make_recorder(setup, clock)
    third.start()
    third.stop()
    first_dir, second_dir = session_folders(data_dir)
    assert first_dir.name.startswith("000001-2026")
    assert second_dir.name.startswith("000002-2020")
    assert any(h.get("x_recovered") for h in heartbeats(events_of(second_dir)))
    assert events_of(second_dir)[-1]["x_cause"] == "shutdown"
    assert_all_verified(setup, data_dir)


def test_a_log_shorter_than_its_checkpoints_is_left_and_fails_verification__defs_10_6(
    setup: Prepared, data_dir: Path
) -> None:
    clock = FakeClock()
    ops = CrashingOps(crash_at=10_000)
    recorder = make_recorder(setup, clock, ops)
    recorder.start()
    nav = Client("nav")
    for index in range(1100):  # a checkpoint at 1000 events
        recorder.receive(nav.event("help_request", scope="base", x_i=index))
    ops.close_all()  # crashed
    (folder,) = session_folders(data_dir)
    path = folder / EVENTS_FILE
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:900]))  # the chain of what is left still verifies
    clock.advance(5)
    restarted = make_recorder(setup, clock)
    restarted.start()
    restarted.stop()
    old, new = session_folders(data_dir)
    assert old == folder
    assert len(path.read_bytes().splitlines()) == 900  # never appended to
    _, result = check_logs(setup, data_dir)
    verdicts = {v.session_id: v for v in result.sessions}
    assert verdicts[old.name].status == "failed"
    assert {i.code for i in verdicts[old.name].evidence} == {"missing_events"}
    assert verdicts[new.name].status == "verified"
