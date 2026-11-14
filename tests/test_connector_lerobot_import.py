"""The LeRobot importer (connectors/lerobot.py ``episode_events``, connectors/lerobot_io.py) and the
watch loop: one test per rule, named by spec section and dossier row (research/lerobot-so101)."""

import itertools
import json
import os
import re
import sys
import threading
from collections.abc import Iterable
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autonometry import cli, cli_lerobot
from autonometry.connectors import lerobot, lerobot_io
from autonometry.connectors.tb4 import StateAssertion
from autonometry.recorder.store import SESSION_ID_RE
from autonometry.timestamps import ns_to_ts, ts_to_ns
from lerobot_helpers import CONFIG, DEVICES, POLICY, ROOT, engine, info, line, write_dataset

pytest.importorskip("pyarrow")

HASH = "0" * 64
START = "2026-11-14T10:00:00Z"
START_NS = ts_to_ns("2026-11-14T10:00:00.000Z")


def _settings(**options: Any) -> lerobot.ImportSettings:
    values: dict[str, Any] = {
        "robot_id": "so101_01",
        "fps": Fraction(2),
        "task_registry_hash": HASH,
        "plan_hash": HASH,
        "device_registry_hash": HASH,
        "heartbeat_interval_s": 1.0,
        "devices": DEVICES,
        "policy_path": POLICY,
        "teleop": "so101_leader",
    }
    values.update(options)
    return lerobot.ImportSettings(**values)


def _episode(
    labels: list[bool | None], frames: list[int] | None = None, **options: Any
) -> lerobot.Episode:
    indices = frames if frames is not None else list(range(len(labels)))
    values: dict[str, Any] = {"index": 0, "task": "t", "length": len(labels)}
    values.update(options)
    return lerobot.Episode(frame_index=tuple(indices), intervention=tuple(labels), **values)


def _offset(event: dict[str, Any]) -> float:
    return (ts_to_ns(event["ts"]) - START_NS) / 1e9


def _brief(events: list[dict[str, Any]]) -> list[tuple[float, str, str | None]]:
    return [(_offset(e), e["type"], e.get("state")) for e in events]


# -- one episode, one session -------------------------------------------------------------------


def test_an_episode_is_one_session_with_one_task__defs_1_5_8__dossier_14_2_s1() -> None:
    events = lerobot.episode_events(
        _settings(), _episode([False, False, True, True, False, False]), START_NS, "cube"
    )
    assert _brief(events) == [
        (0.0, "session_start", None),
        (0.0, "task_start", None),
        (0.0, "transition", "ai"),
        (1.0, "transition", "human_local"),
        (1.0, "heartbeat", None),
        (2.0, "transition", "ai"),
        (2.0, "heartbeat", None),
        (3.0, "task_end", None),
        (3.0, "session_end", None),
    ]
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert all(e["x_ts_reconstructed"] is True for e in events)
    assert {e["session_id"] for e in events} == {events[0]["session_id"]}
    start, task_start, ai, human = events[:4]
    assert start["initial_states"] == {"whole_robot": "unknown"}
    assert start["unobserved"] == ["safety_stops", "human_inputs"]
    assert start["simulated"] is False
    assert task_start["task_definition_id"] == "cube"
    assert ai["actor_id"] == f"ai:{POLICY.replace('/', '_')}"
    assert ai["device_id"] == "so101_policy"
    assert ai["task_id"] == task_start["task_id"] == "episode_000000"
    assert (human["reason"], human["actor_id"], human["device_id"]) == (
        "human_initiated",
        "op_01",
        "so101_leader",
    )
    assert (human["x_src_ts"], human["x_src_clock"]) == (2, "frame_index")
    assert events[-2]["outcome"] == "unknown"
    assert events[-1]["task_id"] is None


def test_true_through_a_remote_teleoperator_is_human_remote__defs_6_10() -> None:
    events = lerobot.episode_events(_settings(teleop="phone"), _episode([True]), START_NS, "c")
    (human,) = [e for e in events if e["type"] == "transition"]
    assert (human["state"], human["device_id"]) == ("human_remote", "phone_teleop")


def test_undeclared_sources_carry_no_device__defs_6_10() -> None:
    events = lerobot.episode_events(
        _settings(policy_path=None, teleop=None), _episode([False, True]), START_NS, "c"
    )
    ai, human = [e for e in events if e["type"] == "transition"]
    assert "device_id" not in ai
    assert "device_id" not in human
    assert (ai["actor_id"], human["actor_id"]) == ("ai:unidentified", "person:unidentified")


def test_missing_frames_and_labels_are_unknown__defs_2_7() -> None:
    episode = _episode([False, None, True, False], frames=[0, 1, 2, 5], length=8)
    events = lerobot.episode_events(_settings(), episode, START_NS, "c")
    changes = [(e["x_src_ts"], e["state"]) for e in events if e["type"] == "transition"]
    assert changes == [
        (0, "ai"),
        (1, "unknown"),
        (2, "human_local"),
        (3, "unknown"),
        (5, "ai"),
        (6, "unknown"),
    ]
    assert _offset(events[-1]) == 4.0


def test_an_episode_whose_first_frames_are_missing_starts_unknown__defs_2_rule_1() -> None:
    episode = _episode([True, True], frames=[3, 4], length=5)
    events = lerobot.episode_events(_settings(), episode, START_NS, "c")
    changes = [(e["x_src_ts"], e["state"]) for e in events if e["type"] == "transition"]
    assert changes == [(3, "human_local")]


def test_a_repeated_frame_is_read_once() -> None:
    episode = _episode([False, True, False], frames=[0, 0, 1], length=2)
    events = lerobot.episode_events(_settings(), episode, START_NS, "c")
    assert [e["state"] for e in events if e["type"] == "transition"] == ["ai"]


def test_heartbeats_keep_every_gap_under_the_silence_limit__defs_2_rule_2() -> None:
    events = lerobot.episode_events(
        _settings(fps=Fraction(30)), _episode([False] * 300), START_NS, "c"
    )
    times = [ts_to_ns(e["ts"]) for e in events]
    assert max(b - a for a, b in itertools.pairwise(times)) <= 1_000_000_000
    beats = [e for e in events if e["type"] == "heartbeat"]
    assert len(beats) == 9
    assert all(b["clock_source"] == "unknown" for b in beats)


def test_reconstructed_time_is_start_plus_frame_over_fps__defs_8_time() -> None:
    fps = Fraction(2997, 100)
    assert lerobot.frame_ns(0, 2997, fps) == 100_000_000_000
    assert lerobot.frame_ns(5, 1, fps) == 5 + round(Fraction(100_000_000_000, 2997))
    events = lerobot.episode_events(_settings(fps=fps), _episode([False] * 10), START_NS, "c")
    assert events[-1]["ts"] == ns_to_ts(lerobot.frame_ns(START_NS, 10, fps))


def test_session_ids_have_the_recorder_form_with_the_episode_as_counter__defs_11_3() -> None:
    sid = lerobot.session_id("so101_01", 17, START_NS)
    assert SESSION_ID_RE.fullmatch(sid)
    assert sid.startswith("000017-20261114T100000Z-")
    assert sid == lerobot.session_id("so101_01", 17, START_NS)
    assert sid != lerobot.session_id("so101_02", 17, START_NS)


def test_import_settings_and_episodes_are_checked() -> None:
    with pytest.raises(ValueError, match="positive"):
        _settings(fps=Fraction(0))
    with pytest.raises(ValueError, match="scopes"):
        _settings(scopes=())
    with pytest.raises(ValueError, match="same length"):
        lerobot.Episode(0, None, 1, (0,), ())
    with pytest.raises(ValueError, match="index"):
        lerobot.Episode(-1, None, 0, (), ())


def test_an_empty_episode_is_an_empty_session() -> None:
    events = lerobot.episode_events(_settings(), _episode([]), START_NS, "c")
    assert [e["type"] for e in events] == ["session_start", "task_start", "task_end", "session_end"]


# -- dataset files ------------------------------------------------------------------------------

EPISODES: list[tuple[int, str, list[bool | None]]] = [
    (0, "Pick up the cube and place it in the bin.", [False] * 4 + [True] * 2 + [False] * 4),
    (1, "Pick up the cube and place it in the bin.", [False] * 6),
]


def _import(dataset: Path, out: Path, *extra: str) -> Any:
    args = ["lerobot", "import", str(dataset), "--out", str(out), "--registry", str(CONFIG)]
    return CliRunner().invoke(cli.app, [*args, *extra])


def _logs(out: Path) -> list[Path]:
    return sorted(out.glob("*/*/events.jsonl"))


def _timeline(logs: Iterable[Path]) -> list[dict[str, Any]]:
    result = CliRunner().invoke(cli.app, ["timeline", "--json", *map(str, logs)])
    assert result.exit_code == 0, result.output
    return list(json.loads(result.output)["sessions"])


def test_import_writes_one_checked_log_per_episode__defs_8_registries(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "ds", EPISODES, info_doc=info(fps=2))
    out = tmp_path / "out"
    result = _import(dataset, out, "--start", START, "--policy", POLICY, "--teleop", "so101_leader")
    assert result.exit_code == 0, result.output
    assert "wrote 2 session log(s)" in result.output
    logs = _logs(out)
    assert [p.parent.parent.name for p in logs] == ["so_follower", "so_follower"]
    for log in logs:
        for name in ("devices.json", "tasks.json", "plan.json"):
            assert (log.parent / name).read_bytes() == (CONFIG / name).read_bytes()
    validated = CliRunner().invoke(cli.app, ["validate", *map(str, logs)])
    assert validated.exit_code == 0, validated.output
    first, second = _timeline(logs)
    whole = first["time"]["whole_robot"]
    assert whole["states"]["ai"] == pytest.approx(4.0)
    assert whole["states"]["human_local"] == pytest.approx(1.0)
    assert first["interventions"]["whole_robot"] == 1
    assert [t["task_definition_id"] for t in first["tasks"]] == ["pick_place_cube"]
    assert first["tasks"][0]["autonomous"] is False
    assert second["tasks"][0]["autonomous"] is True
    assert _offset(json.loads(logs[1].read_text(encoding="utf-8").splitlines()[0])) == 5.0


def test_import_is_deterministic__claude_md_rule_4(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "ds", EPISODES)
    for out in ("a", "b"):
        assert _import(dataset, tmp_path / out, "--start", START).exit_code == 0
    first, second = _logs(tmp_path / "a"), _logs(tmp_path / "b")
    assert [p.read_bytes() for p in first] == [p.read_bytes() for p in second]


def test_imported_logs_are_capped_at_l1__defs_8_10_6(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "ds", EPISODES)
    assert _import(dataset, tmp_path / "out", "--start", START).exit_code == 0
    result = CliRunner().invoke(cli.app, ["verify", "--json", *map(str, _logs(tmp_path / "out"))])
    sessions = json.loads(result.output)["sessions"]
    assert all("reconstructed" in {g["code"] for g in s["gaps"]} for s in sessions)
    assert {s["status"] for s in sessions} == {"unverified"}


def test_an_intervention_stored_as_a_one_element_list_is_read__dossier_5b(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "ds", EPISODES, as_list=True, info_doc=info(fps=2))
    options = ("--start", START, "--policy", POLICY, "--teleop", "so101_leader")
    assert _import(dataset, tmp_path / "out", *options).exit_code == 0
    first, _ = _timeline(_logs(tmp_path / "out"))
    assert first["interventions"]["whole_robot"] == 1


def test_episodes_without_their_data_file_are_listed_not_imported__defs_11_3(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dataset = write_dataset(tmp_path / "ds", EPISODES, files=[0, 1], write_files=[0])
    result = _import(dataset, tmp_path / "out", "--start", START)
    assert result.exit_code == 0, result.output
    assert "1 episode(s) have no data file and were not imported: 1" in caplog.text
    assert len(_logs(tmp_path / "out")) == 1


def test_a_task_the_registry_does_not_declare_is_flagged__defs_5(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dataset = write_dataset(tmp_path / "ds", [(0, "Fold the cloth.", [False, True])])
    result = _import(dataset, tmp_path / "out", "--start", START)
    assert result.exit_code == 0
    assert "1 task text(s) are not in the task registry" in caplog.text
    (session,) = _timeline(_logs(tmp_path / "out"))
    assert "unknown_task_definition" in {f["code"] for f in session["flags"]}


def test_without_start_episodes_end_at_their_data_file_time__dossier_14_4(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "ds", EPISODES, info_doc=info(fps=2))
    data_file = dataset / "data" / "chunk-000" / "file-000.parquet"
    os.utime(data_file, ns=(START_NS + 8_000_000_000, START_NS + 8_000_000_000))
    assert _import(dataset, tmp_path / "out", "--robot-id", "arm_7").exit_code == 0
    first, second = (
        json.loads(p.read_text(encoding="utf-8").splitlines()[0]) for p in _logs(tmp_path / "out")
    )
    assert (_offset(first), _offset(second)) == (0.0, 5.0)
    assert first["x_ts_anchor"] == "file_mtime"
    assert first["robot_id"] == "arm_7"


def test_simulated_episodes_are_declared__defs_8(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "ds", EPISODES)
    assert _import(dataset, tmp_path / "out", "--start", START, "--simulated").exit_code == 0
    first = json.loads(_logs(tmp_path / "out")[0].read_text(encoding="utf-8").splitlines()[0])
    assert first["simulated"] is True


def _bad_info(root: Path, document: Any) -> Path:
    write_dataset(root, EPISODES)
    (root / "meta" / "info.json").write_text(
        document if isinstance(document, str) else json.dumps(document), encoding="utf-8"
    )
    return root


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("{not json", "is not valid JSON"),
        (info(codebase_version="v2.1"), "codebase_version must be v3.0"),
        (info(data_path="data/{episode_index}.parquet"), "data_path is not LeRobot's default"),
        (info(features={"frame_index": {"dtype": "int64"}}), "no boolean intervention feature"),
        (info(fps=0), "fps: Input should be greater than 0"),
        ({"fps": 30}, "codebase_version: Field required"),
    ],
)
def test_a_dataset_the_importer_cannot_read_is_refused_in_one_line(
    tmp_path: Path, document: Any, message: str
) -> None:
    result = _import(_bad_info(tmp_path / "ds", document), tmp_path / "out")
    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output


def test_a_dataset_without_files_is_refused(tmp_path: Path) -> None:
    (tmp_path / "ds").mkdir()
    result = _import(tmp_path / "ds", tmp_path / "out")
    assert result.exit_code == 1
    assert "cannot read" in result.output
    dataset = write_dataset(tmp_path / "ds2", EPISODES)
    (dataset / "meta" / "episodes" / "chunk-000" / "file-000.parquet").unlink()
    result = _import(dataset, tmp_path / "out")
    assert "holds no chunk-*/file-*.parquet" in result.output


def _rewrite_meta(dataset: Path, **columns: list[Any]) -> None:
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    path = dataset / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    table = pq.read_table(path).to_pydict()
    table.update(columns)
    pq.write_table(pa.table({k: v for k, v in table.items() if v is not None}), path)


def test_bad_episode_metadata_is_refused_in_one_line(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "ds", EPISODES)
    _rewrite_meta(dataset, episode_index=[0, 0])
    assert "episode 0 is listed twice" in _import(dataset, tmp_path / "o").output
    _rewrite_meta(dataset, episode_index=[0, -1])
    assert "episode_index must be a non-negative integer" in _import(dataset, tmp_path / "o").output
    _rewrite_meta(dataset, episode_index=[0, 1], length=None)
    assert "no column 'length'" in _import(dataset, tmp_path / "o").output


def test_a_file_that_is_not_parquet_or_too_large_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = write_dataset(tmp_path / "ds", EPISODES)
    data_file = dataset / "data" / "chunk-000" / "file-000.parquet"
    monkeypatch.setattr(lerobot_io, "MAX_DATA_BYTES", 10)
    assert "is larger than 10 bytes" in _import(dataset, tmp_path / "o").output
    data_file.write_bytes(b"not parquet at all")
    monkeypatch.setattr(lerobot_io, "MAX_DATA_BYTES", 1 << 30)
    assert "is not a readable parquet file" in _import(dataset, tmp_path / "o").output


def test_bad_options_are_refused_in_one_line(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "ds", EPISODES)
    assert (
        "--start must be an RFC 3339" in _import(dataset, tmp_path / "o", "--start", "noon").output
    )
    bad_day = _import(dataset, tmp_path / "o", "--start", "2026-02-30T00:00:00Z")
    assert "--start is not a valid time" in bad_day.output
    assert "robot id must be" in _import(dataset, tmp_path / "o", "--robot-id", "a b").output
    registry = tmp_path / "reg"
    registry.mkdir()
    result = CliRunner().invoke(
        cli.app,
        [
            "lerobot",
            "import",
            str(dataset),
            "--out",
            str(tmp_path / "o"),
            "--registry",
            str(registry),
        ],
    )
    assert result.exit_code == 1
    assert "cannot read" in result.output
    (registry / "devices.json").write_text('{"devices": []}', encoding="utf-8")
    for name in ("tasks.json", "plan.json"):
        (registry / name).write_bytes((CONFIG / name).read_bytes())
    result = CliRunner().invoke(
        cli.app,
        [
            "lerobot",
            "import",
            str(dataset),
            "--out",
            str(tmp_path / "o"),
            "--registry",
            str(registry),
        ],
    )
    assert "is not a valid devices registry" in result.output
    assert "Traceback" not in result.output


def test_import_without_the_lerobot_extra_says_how_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    result = _import(tmp_path, tmp_path / "out")
    assert result.exit_code == 1
    assert "autonometry[lerobot]" in result.output


def test_robot_id_defaults_to_the_robot_type() -> None:
    assert lerobot_io._robot_id("so_follower") == "so_follower"
    assert lerobot_io._robot_id(None) == "lerobot_robot"
    assert lerobot_io._robot_id("_odd") == "r_odd"


def test_frames_of_other_types_are_unknown__defs_2() -> None:
    assert lerobot_io._label(True) is True
    assert lerobot_io._label([False]) is False
    assert lerobot_io._label([True, False]) is None
    assert lerobot_io._label(1) is None
    assert lerobot_io._task_text(["a", "b"]) == "a"
    assert lerobot_io._task_text("a") == "a"
    assert lerobot_io._task_text([]) is None


def test_registries_name_their_reset_plan_item() -> None:
    registries = lerobot_io.load_registries(
        CONFIG / "devices.json", CONFIG / "tasks.json", CONFIG / "plan.json", "0.3"
    )
    assert registries.reset_plan_item() == "reset"
    empty = lerobot_io.Registries({}, {}, {"items": []}, HASH, HASH, HASH, {})
    assert empty.reset_plan_item() is None


# -- following the log --------------------------------------------------------------------------


def _append(path: Path, data: bytes) -> None:
    with open(path, "ab") as stream:
        stream.write(data)


def test_follower_reads_new_lines_only_unless_from_start(tmp_path: Path) -> None:
    log = tmp_path / "rollout.log"
    log.write_bytes(b"old line\n")
    tail = lerobot_io.LogFollower(log)
    assert tail.read() == []
    _append(log, b"new line\r\npart")
    assert tail.read() == ["new line"]
    _append(log, b"ial\n\xff\n")
    assert tail.read() == ["partial", "�"]
    whole = lerobot_io.LogFollower(log, from_start=True)
    assert whole.read() == ["old line", "new line", "partial", "�"]
    whole.close()
    tail.close()


def test_follower_waits_for_the_file_and_rereads_a_truncated_one(tmp_path: Path) -> None:
    log = tmp_path / "rollout.log"
    tail = lerobot_io.LogFollower(log)
    assert tail.read() == []
    log.write_bytes(b"first\n")
    assert tail.read() == ["first"]
    with open(log, "r+b") as stream:
        stream.truncate(0)
    assert tail.read() == []
    _append(log, b"again\n")
    assert tail.read() == ["again"]
    tail.close()
    log.unlink()
    assert tail.read() == []


@pytest.mark.skipif(sys.platform == "win32", reason="an open file cannot be replaced on Windows")
def test_follower_rereads_a_replaced_file(tmp_path: Path) -> None:
    log = tmp_path / "rollout.log"
    log.write_bytes(b"")
    tail = lerobot_io.LogFollower(log)
    _append(log, b"one\n")
    assert tail.read() == ["one"]
    other = tmp_path / "other.log"
    other.write_bytes(b"two\n")
    os.replace(other, log)
    assert tail.read() == ["two"]
    tail.close()


def test_follower_drops_lines_longer_than_the_limit__claude_md_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lerobot_io, "MAX_LINE_BYTES", 8)
    log = tmp_path / "rollout.log"
    tail = lerobot_io.LogFollower(log)
    log.write_bytes(b"0123456789")
    assert tail.read() == []
    _append(log, b"abc\nshort\n")
    assert tail.read() == ["short"]
    tail.close()


def test_pump_sends_lines_then_the_state(tmp_path: Path) -> None:
    log = tmp_path / "rollout.log"
    tail = lerobot_io.LogFollower(log)
    log.write_text(line("Base strategy control loop started") + "\nnoise\n", encoding="utf-8")
    sent: list[lerobot.Message] = []
    lerobot_io.pump(tail, engine(), sent.extend, 1.0)
    states = [m.state for m in sent if isinstance(m, StateAssertion)]
    assert states == ["ai", "ai"]
    tail.close()


class _FakeRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, type: str, **fields: Any) -> None:
        self.events.append((type, fields))


def test_recorder_sender_sends_wire_events__readme_wire_format() -> None:
    recorder = _FakeRecorder()
    send = lerobot_io.recorder_sender(recorder)
    send([lerobot.Note((("x_lerobot_rerecorded_task", "episode_000001"),))])
    assert recorder.events == [("heartbeat", {"x_lerobot_rerecorded_task": "episode_000001"})]


def test_watch_log_follows_until_stopped_and_ends_unknown__defs_6_14(tmp_path: Path) -> None:
    log = tmp_path / "rollout.log"
    seen_ai = threading.Event()
    stop = threading.Event()
    sent: list[lerobot.Message] = []
    failed_once: list[bool] = []

    def send(messages: Iterable[lerobot.Message]) -> None:
        batch = list(messages)
        if batch and not failed_once:
            failed_once.append(True)
            raise RuntimeError("the first pass fails")
        sent.extend(batch)
        if any(isinstance(m, StateAssertion) and m.state == "ai" for m in batch):
            seen_ai.set()

    thread = threading.Thread(
        target=lerobot_io.watch_log,
        args=(log, engine(), send, stop),
        kwargs={"from_start": True},
        daemon=True,
    )
    thread.start()
    log.write_text(line("Base strategy control loop started") + "\n", encoding="utf-8")
    assert seen_ai.wait(20), "the watcher never sent the ai state"
    stop.set()
    thread.join(20)
    assert not thread.is_alive()
    last = sent[-1]
    assert isinstance(last, StateAssertion)
    assert last.state == "unknown"


# -- watch CLI ----------------------------------------------------------------------------------


def _recorder_toml(folder: Path, **overrides: str) -> Path:
    values = {
        "robot_id": '"so101_01"',
        "data_dir": '"data"',
        "socket_path": '"recorder.sock"',
        "simulated": "false",
        "clock_source": '"ntp"',
        "key_path": '"so101_01.key"',
        "device_registry": json.dumps(str(CONFIG / "devices.json")),
        "task_registry": json.dumps(str(CONFIG / "tasks.json")),
        "plan": json.dumps(str(CONFIG / "plan.json")),
    }
    values.update(overrides)
    path = folder / "recorder.toml"
    path.write_text("".join(f"{k} = {v}\n" for k, v in values.items()), encoding="utf-8")
    return path


@pytest.fixture
def watch_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Pretend to run on Linux, record what the CLI would follow instead of following it."""
    calls: list[dict[str, Any]] = []

    def fake_watch(path: Path, eng: Any, send: Any, stop: Any, *, from_start: bool) -> None:
        calls.append({"path": path, "engine": eng, "from_start": from_start})

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cli_lerobot, "watch_log", fake_watch)
    monkeypatch.setattr(cli_lerobot.signal, "signal", lambda *_: None)
    return calls


def _watch(*args: str) -> Any:
    return CliRunner().invoke(cli.app, ["lerobot", "watch", *args])


def test_watch_follows_the_log_with_the_recorder_s_registries__readme_lerobot(
    tmp_path: Path, watch_calls: list[dict[str, Any]]
) -> None:
    config = _recorder_toml(tmp_path)
    log = tmp_path / "rollout.log"
    task = "Pick up the cube and place it in the bin."
    result = _watch("--log", str(log), "--config", str(config), "--task", task, "--from-start")
    assert result.exit_code == 0, result.output
    (call,) = watch_calls
    assert call["path"] == log
    assert call["from_start"] is True
    settings = call["engine"]._s
    assert settings.task_definition_id == "pick_place_cube"
    assert settings.reset_plan_item_id == "reset"
    assert settings.scopes == ("whole_robot",)
    assert settings.idle_threshold_s == 30.0


def test_watch_needs_the_task_when_the_registry_declares_several(
    tmp_path: Path, watch_calls: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    config = _recorder_toml(tmp_path)
    log = str(tmp_path / "rollout.log")
    result = _watch("--log", log, "--config", str(config))
    assert result.exit_code == 2
    assert "name the rollout's one with --task" in result.output
    one = tmp_path / "one.json"
    one.write_text('{"tasks": [{"task_definition_id": "only"}]}', encoding="utf-8")
    config = _recorder_toml(tmp_path, task_registry=json.dumps(str(one)))
    assert _watch("--log", log, "--config", str(config)).exit_code == 0
    assert watch_calls[-1]["engine"]._s.task_definition_id == "only"
    result = _watch("--log", log, "--config", str(config), "--task", "Fold the cloth.")
    assert result.exit_code == 0
    assert "--task is not in the task registry" in caplog.text


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"role": '"station"', "station_id": '"pc_1"'}, "the config is a station's"),
        ({"clock_source": '"sundial"'}, "clock_source"),
        ({"device_registry": '"missing.json"'}, "cannot read"),
    ],
)
def test_watch_refuses_a_config_it_cannot_use(
    tmp_path: Path, watch_calls: list[dict[str, Any]], overrides: dict[str, str], message: str
) -> None:
    config = _recorder_toml(tmp_path, **overrides)
    result = _watch("--log", str(tmp_path / "r.log"), "--config", str(config))
    assert result.exit_code == 2
    assert message in result.output
    assert watch_calls == []


def test_watch_needs_the_log_s_folder(tmp_path: Path, watch_calls: list[dict[str, Any]]) -> None:
    config = _recorder_toml(tmp_path)
    result = _watch("--log", str(tmp_path / "nowhere" / "r.log"), "--config", str(config))
    assert result.exit_code == 2
    assert "is not a folder" in result.output


def test_watch_without_watchfiles_says_how_to_install_it(
    tmp_path: Path, watch_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "watchfiles", None)
    result = _watch("--log", str(tmp_path / "r.log"), "--config", str(_recorder_toml(tmp_path)))
    assert result.exit_code == 2
    assert "autonometry[lerobot]" in result.output


def test_watch_refuses_windows_where_the_recorder_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    result = _watch("--log", str(tmp_path / "r.log"), "--config", str(_recorder_toml(tmp_path)))
    assert result.exit_code == 2
    assert "Linux and macOS only" in result.output


def test_the_vendored_client_is_the_published_one__readme_wire_format() -> None:
    original = ROOT / "clients" / "python" / "autonometry_client.py"
    vendored = ROOT / "src" / "autonometry" / "connectors" / "autonometry_client.py"
    assert vendored.read_bytes() == original.read_bytes()


def test_example_config_is_a_valid_recorder_config__readme_lerobot() -> None:
    from autonometry.recorder.config import load_config  # noqa: PLC0415

    config = load_config(CONFIG / "recorder.toml")
    assert config.unobserved == ("safety_stops", "human_inputs")
    assert re.fullmatch(r"so101_\d+", config.robot_id)
