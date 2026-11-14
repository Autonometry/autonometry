"""Golden tests of the LeRobot connector. Live: the rollout log ``tests/golden/lerobot/rollout.log``
(a DAgger run with ``record_autonomous=true``: a pause and correction, a pause alone, two episode
rotations, teardown) run through the engine, the real recorder (fake clock) and
``autonometry timeline``. Import: a tiny LeRobotDataset built here with pyarrow, imported with
``autonometry lerobot import`` and read back with ``autonometry timeline``. The expected log,
states, episodes and tasks in ``tests/golden/lerobot/expected.json`` were worked out by hand from
the spec before the code ran; ``tests/golden/lerobot/README.md`` shows the arithmetic."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autonometry import cli
from autonometry.connectors import lerobot
from autonometry.recorder.store import EVENTS_FILE
from autonometry.timestamps import datetime_to_ns, ts_to_ns
from lerobot_helpers import CONFIG, GOLDEN, POLICY, settings, write_dataset
from recorder_helpers import Client, FakeClock, make_prepared, make_recorder

EXPECTED = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))
LIVE = EXPECTED["live"]
IMPORT = EXPECTED["import"]
T0_NS = ts_to_ns(LIVE["start"])
END_S: float = LIVE["end_offset_s"]
#: The watcher re-asserts the state every 0.25 s on a robot; 0.1 s here.
TICKS_PER_S = 10
_PRINTED = re.compile(r"[A-Z]+ (?:\[PID: [0-9]+\] )?([0-9-]{10} [0-9:]{8}) ")


def _log_lines() -> list[tuple[float, str]]:
    """(t, line) of the fixture, t the line's printed time in seconds after its first line's;
    a line without LeRobot's prefix takes the time of the line before it."""
    lines: list[tuple[float, str]] = []
    first: datetime | None = None
    t = 0.0
    for text in (GOLDEN / "rollout.log").read_text(encoding="utf-8").splitlines():
        found = _PRINTED.match(text)
        if found is not None:
            moment = datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            first = first or moment
            t = (datetime_to_ns(moment) - datetime_to_ns(first)) / 1e9
        lines.append((t, text))
    return lines


def _schedule() -> list[tuple[float, int, str]]:
    """Log lines and watcher ticks in time order; at one instant, lines first."""
    ticks = [(k / TICKS_PER_S, 1, "") for k in range(round(END_S * TICKS_PER_S))]
    items = [(t, 0, text) for t, text in _log_lines()] + ticks
    return sorted(items, key=lambda item: (item[0], item[1]))


def _datagrams(client: Client, messages: list[lerobot.Message]) -> list[bytes]:
    out = []
    for message in messages:
        event = lerobot.wire_event(message)
        out.append(client.event(event.pop("type"), **event))
    return out


def record_golden(tmp_path: Path) -> Path:
    """Run the rollout log through the watcher's engine and the recorder; the log's folder."""
    prepared = make_prepared(
        tmp_path,
        robot_id="so101_01",
        scopes=["whole_robot"],
        simulated=False,
        unobserved=["safety_stops", "human_inputs"],
        device_registry=str(CONFIG / "devices.json"),
        task_registry=str(CONFIG / "tasks.json"),
        plan=str(CONFIG / "plan.json"),
    )
    clock = FakeClock(wall_ns=T0_NS)
    recorder = make_recorder(prepared, clock)
    watcher = lerobot.LeRobotEngine(settings(task_definition_id="pick_place_cube"))
    client = Client("lerobot_watch")
    recorder.start()
    for t, kind, text in _schedule():
        clock.wall = T0_NS + round(t * 1e9)
        clock.mono = 1000.0 + t
        recorder.tick()
        if kind == 0:
            parsed = lerobot.parse_line(text)
            messages = [] if parsed is None else watcher.observe(t, parsed)
        else:
            messages = watcher.tick(t)
        for data in _datagrams(client, messages):
            recorder.receive(data)
    clock.wall = T0_NS + round(END_S * 1e9)
    clock.mono = 1000.0 + END_S
    recorder.tick()
    recorder.stop()
    robot = prepared.config.data_dir / prepared.config.robot_id
    (folder,) = [p for p in robot.iterdir() if p.is_dir()]
    return folder


def _timeline(paths: list[Path]) -> list[dict[str, Any]]:
    result = CliRunner().invoke(cli.app, ["timeline", "--json", *map(str, paths)])
    assert result.exit_code == 0, result.output
    return list(json.loads(result.output)["sessions"])


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    log = record_golden(tmp_path_factory.mktemp("lerobot_live")) / EVENTS_FILE
    events = [json.loads(line) for line in log.read_bytes().splitlines()]
    (session,) = _timeline([log])
    return events, session


def _offset(event: dict[str, Any], origin_ns: int = T0_NS) -> float:
    return round((ts_to_ns(event["ts"]) - origin_ns) / 1e9, 3)


def test_the_fixture_is_timed_by_its_printed_clock() -> None:
    lines = _log_lines()
    assert lines[0][0] == 0.0
    assert lines[-1] == (149.0, lines[-1][1])


def test_live_log_matches_the_hand_worked_events__dossier_14_1_defs_8(
    live: tuple[list[dict[str, Any]], dict[str, Any]],
) -> None:
    events, _ = live
    written = [e for e in events if e["type"] != "heartbeat"]
    expected = LIVE["log"]
    assert len(written) == len(expected), [(_offset(e), e["type"], e.get("state")) for e in written]
    for event, want in zip(written, expected, strict=True):
        got = {"t": _offset(event)} | {k: event.get(k) for k in want if k != "t"}
        assert got == want


def test_live_timeline_matches_the_hand_worked_states_and_tasks__defs_2_3_5_6_8_6_9_6_14(
    live: tuple[list[dict[str, Any]], dict[str, Any]],
) -> None:
    _, session = live
    want = LIVE["timeline"]
    whole = session["time"]["whole_robot"]
    assert whole["observed_s"] == pytest.approx(want["observed_s"])
    assert whole["active_s"] == pytest.approx(want["active_s"])
    assert whole["states"] == pytest.approx(want["states_s"])
    assert session["interventions"]["whole_robot"] == want["interventions_whole_robot"]
    tasks = [{k: task[k] for k in want["tasks"][0]} for task in session["tasks"]]
    assert tasks == [pytest.approx(t) for t in want["tasks"]]
    assert session["humans"]["distinct_human_actors"] == want["distinct_human_actors"]
    assert sorted({f["code"] for f in session["flags"]}) == want["flag_codes"]


def test_the_live_log_is_sealed__defs_10_1(
    live: tuple[list[dict[str, Any]], dict[str, Any]],
) -> None:
    events, _ = live
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert all(e["prev_hash"] and e["hash"] for e in events)


# -- import -------------------------------------------------------------------------------------

CUBE = "Pick up the cube and place it in the bin."
DATASET: list[tuple[int, str, list[bool | None]]] = [
    (0, CUBE, [False] * 20 + [True] * 10 + [False] * 20),
    (1, CUBE, [False] * 25),
    (2, "Fold the cloth.", [True] * 5 + [False] * 5),
    (3, CUBE, [False] * 10),
]


@pytest.fixture(scope="module")
def imported(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    pytest.importorskip("pyarrow")
    from lerobot_helpers import info  # noqa: PLC0415

    root = tmp_path_factory.mktemp("lerobot_import")
    dataset = write_dataset(
        root / "dataset",
        DATASET,
        files=[0, 0, 0, 1],
        write_files=[0],
        lengths={1: 30},
        frames={1: [*range(10), *range(15, 30)]},
        info_doc=info(fps=IMPORT["fps"]),
    )
    out = root / "logs"
    args = [
        "lerobot", "import", str(dataset), "--out", str(out), "--registry", str(CONFIG),
        "--start", IMPORT["start"], "--policy", POLICY, "--teleop", "so101_leader",
    ]  # fmt: skip
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    return _timeline(sorted(out.glob("*/*/events.jsonl")))


def test_import_matches_the_hand_worked_sessions_and_tasks__defs_2_5_7_8_11_3(
    imported: list[dict[str, Any]],
) -> None:
    origin = ts_to_ns(IMPORT["start"].replace("Z", ".000Z"))
    assert len(imported) == len(IMPORT["sessions"])
    for session, want in zip(imported, IMPORT["sessions"], strict=True):
        assert session["session_id"].split("-")[0] == want["counter"]
        whole = session["time"]["whole_robot"]
        assert whole["observed_s"] == pytest.approx(want["observed_s"])
        states = {k: whole["states"][k] for k in want["states_s"]}
        assert states == pytest.approx(want["states_s"])
        assert session["interventions"]["whole_robot"] == want["interventions_whole_robot"]
        (task,) = session["tasks"]
        assert {k: task[k] for k in want["task"]} == pytest.approx(want["task"])
        assert sorted({f["code"] for f in session["flags"]}) == want["flag_codes"]
        start = session["session_id"].split("-")[1]
        stamp = datetime.strptime(start, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        assert (datetime_to_ns(stamp) - origin) / 1e9 == want["start_offset_s"]
