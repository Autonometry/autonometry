"""Golden test: a Unitree G1 session (``tests/golden/g1/session.json``) through the engine, the
real recorder (fake clock), ``autonometry timeline`` and ``autonometry report``. The expected
log, states, interventions, task and report numbers in ``tests/golden/g1/expected.json`` were
worked out by hand under spec v0.3 before the code ran; ``tests/golden/g1/README.md`` shows the
arithmetic."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autonometry import cli, keys
from autonometry.connectors import g1
from autonometry.recorder.store import CHECKPOINTS_FILE, EVENTS_FILE
from autonometry.timestamps import ts_to_ns
from g1_helpers import (
    _WRITERS,
    CONFIG,
    GOLDEN,
    LOWSTATE,
    SPORT,
    datagrams,
    engine,
    remote_bytes,
    seen,
)
from recorder_helpers import Client, FakeClock, make_prepared, make_recorder

SESSION = json.loads((GOLDEN / "session.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))
T0_NS = ts_to_ns(SESSION["start"])
#: Steps of 0.1 s: times in the session file are whole tenths.
TENTHS = 10


def _tenth(t: float) -> int:
    return round(t * TENTHS)


def _from(table: list[list[Any]], k: int) -> list[Any]:
    """The row of a "from this time on" table in force at step ``k``."""
    return [row for row in table if _tenth(row[0]) <= k][-1][1:]


def _events(k: int, t: float) -> list[dict[str, Any]]:
    out = []
    for event in SESSION["events"]:
        first, every = _tenth(event["t"]), _tenth(event.get("every", 0.0))
        last = _tenth(event.get("until", event["t"]))
        if first <= k <= last and (k == first or (every and (k - first) % every == 0)):
            out.append(event)
    return out


def _observations(k: int, t: float) -> list[g1.Observation]:
    fsm = _from(SESSION["robot"], k)
    buttons, ly = _from(SESSION["remote"], k)
    out: list[g1.Observation] = [
        g1.LowState(t, LOWSTATE, remote_bytes(*buttons, ly=ly)),
        g1.SportState(t, SPORT, *fsm),
    ]
    for stream in SESSION["streams"]:
        if _tenth(stream["from"]) <= k <= _tenth(stream["to"]):
            key = stream["key"]
            out.append(g1.Command(t, key, _WRITERS[key][2], stream["weight"]))
    return out


def record_golden(tmp_path: Path) -> Path:
    """Run the session; return the folder of its sealed log."""
    prepared = make_prepared(
        tmp_path,
        robot_id="g1_01",
        scopes=["legs", "upper_body"],
        scope_weights={"legs": 12, "upper_body": 17},
        simulated=False,
        unobserved=["safety_stops", "human_inputs"],
        device_registry=str(CONFIG / "devices.json"),
        task_registry=str(CONFIG / "tasks.json"),
        plan=str(CONFIG / "plan.json"),
    )
    clock = FakeClock(wall_ns=T0_NS)
    recorder = make_recorder(prepared, clock)
    watcher = engine()
    client, program = Client("g1_watch"), Client("g1_program")
    recorder.start()
    for key in _WRITERS:
        for data in datagrams(client, watcher.observe(seen(0.0, key))):
            recorder.receive(data)
    for k in range(_tenth(SESSION["end_s"]) + 1):
        t = k / TENTHS
        clock.wall, clock.mono = T0_NS + k * 100_000_000, 1000.0 + t
        recorder.tick()
        messages: list[g1.Message] = []
        for observation in _observations(k, t):
            messages += watcher.observe(observation)
        for event in _events(k, t):
            messages += _event(event, t, watcher, recorder, client, program, messages)
        for data in datagrams(client, [*messages, *watcher.tick(t)]):
            recorder.receive(data)
    recorder.tick()
    recorder.stop()
    robot = prepared.config.data_dir / prepared.config.robot_id
    (folder,) = [p for p in robot.iterdir() if p.is_dir()]
    vkey = keys.vkey_path(prepared.config.key_path)
    (folder / "recorder.vkey").write_bytes(vkey.read_bytes())
    return folder


def _event(  # noqa: PLR0913, PLR0917 -- the golden loop's collaborators
    event: dict[str, Any],
    t: float,
    watcher: g1.G1Engine,
    recorder: Any,
    client: Client,
    program: Client,
    pending: list[g1.Message],
) -> list[g1.Message]:
    """One event: a request to the engine, or a task the robot program sends to the recorder
    (the watcher's messages so far go first) and the follower then reports to the engine."""
    if event["kind"] == "request":
        parameter = json.dumps(event["parameter"])
        request = g1.Request(t, event["key"], event["service"], 1, event["api_id"], parameter)
        return watcher.observe(request)
    for data in datagrams(client, pending):
        recorder.receive(data)
    pending.clear()
    if event["kind"] == "task_start":
        recorder.receive(
            program.event(
                "task_start",
                task_id=event["task_id"],
                task_definition_id=event["task_definition_id"],
            )
        )
        return watcher.observe(g1.TaskOpened(t, event["task_id"]))
    recorder.receive(program.event("task_end", task_id=event["task_id"], outcome=event["outcome"]))
    return watcher.observe(g1.TaskClosed(t, event["task_id"]))


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[dict[str, Any]]]:
    folder = record_golden(tmp_path_factory.mktemp("g1"))
    events = [json.loads(line) for line in (folder / EVENTS_FILE).read_bytes().splitlines()]
    return folder, events


def _offset(event: dict[str, Any]) -> float:
    return round((ts_to_ns(event["ts"]) - T0_NS) / 1e9, 3)


def test_log_matches_the_hand_worked_events__notes_6_defs_8(
    golden: tuple[Path, list[dict[str, Any]]],
) -> None:
    _, events = golden
    written = [e for e in events if e["type"] != "heartbeat"]
    expected = EXPECTED["log"]
    assert len(written) == len(expected), [
        (_offset(e), e["type"], e.get("scope"), e.get("state")) for e in written
    ]
    for event, want in zip(written, expected, strict=True):
        got = {"t": _offset(event)} | {k: event.get(k) for k in want if k != "t"}
        assert got == want


def test_active_writers_are_noted_on_heartbeats__notes_6(
    golden: tuple[Path, list[dict[str, Any]]],
) -> None:
    folder, events = golden
    noted = [e["x_g1_writers"] for e in events if "x_g1_writers" in e]
    assert any({"topic": "rt/arm_sdk", "device_id": "xr_teleop"} in writers for writers in noted)
    assert any(
        {"topic": "rt/api/sport/request", "program": "python3", "host": "5eec0dc419aa"} in writers
        for writers in noted
    )  # the undeclared laptop, its host as a pseudonym (product rule 5)
    text = (folder / EVENTS_FILE).read_text(encoding="utf-8")
    assert "laptop" not in text
    assert "teleop-pc" not in text


def test_timeline_matches_the_hand_worked_states_and_interventions__defs_3_5_6_6_6_7(
    golden: tuple[Path, list[dict[str, Any]]],
) -> None:
    folder, _ = golden
    result = CliRunner().invoke(cli.app, ["timeline", "--json", str(folder / EVENTS_FILE)])
    assert result.exit_code == 0, result.output
    (session,) = json.loads(result.output)["sessions"]
    want = EXPECTED["timeline"]
    time = session["time"]
    assert time["whole_robot"]["observed_s"] == pytest.approx(want["observed_s"])
    assert time["whole_robot"]["active_s"] == pytest.approx(want["whole_robot"]["active_s"])
    assert time["whole_robot"]["states"] == pytest.approx(want["whole_robot"]["states_s"])
    for scope in ("legs", "upper_body"):
        assert time["per_scope"][scope]["states"] == pytest.approx(want[scope])
    weighted = time["scope_weighted"]  # (12 * 75.1 + 17 * 58.0) / 29 over 107.6 (6.6)
    assert weighted["states"]["ai"] == pytest.approx((12 * 75.1 + 17 * 58.0) / 29)
    interventions = session["interventions"]
    assert interventions["whole_robot"] == want["interventions_whole_robot"]
    assert interventions["per_scope"] == want["interventions_per_scope"]
    stops = session["safety_stops"]
    assert {k: stops[k] for k in want["safety_stops"]} == want["safety_stops"]
    tasks = [{k: task[k] for k in want["tasks"][0]} for task in session["tasks"]]
    assert tasks == [pytest.approx(t) for t in want["tasks"]]
    assert session["humans"]["distinct_human_actors"] == want["distinct_human_actors"]
    assert sorted({f["code"] for f in session["flags"]}) == want["flag_codes"]


def test_report_matches_the_hand_worked_metrics__defs_7_11(
    golden: tuple[Path, list[dict[str, Any]]], tmp_path: Path
) -> None:
    pytest.importorskip("scipy")
    folder, _ = golden
    out = tmp_path / "report"
    args = [
        "report",
        str(folder / EVENTS_FILE),
        "--out",
        str(out),
        "--vkey",
        str(folder / "recorder.vkey"),
        "--anchor",
        str(folder / CHECKPOINTS_FILE),
        "--n-boot",
        "200",
    ]
    result = CliRunner().invoke(cli.app, args)
    want = EXPECTED["report"]
    assert result.exit_code == 0, result.output
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    verdict = report["verdict"]
    assert {k: verdict[k] for k in ("status", "level", "completeness", "census", "clean")} == {
        k: want[k] for k in ("status", "level", "completeness", "census", "clean")
    }
    real = report["metrics"]["groups"]["real"]
    share = real["time_autonomy_share"]
    assert share["estimate"] == pytest.approx(want["time_autonomy_share"])
    assert share["unknown_as_human"]["estimate"] == pytest.approx(want["unknown_as_human"])
    assert share["unknown_as_ai"]["estimate"] == pytest.approx(want["unknown_as_ai"])
    assert share["scripted_share"]["estimate"] == pytest.approx(want["scripted_share"])
    assert share["safety_stop_share"]["estimate"] == pytest.approx(want["safety_stop_share"])
    rate = real["interventions_per_active_hour"]
    assert rate["count"] == want["interventions"]
    assert rate["estimate"] == pytest.approx(want["interventions_per_active_hour"])
    assert real["safety_stops_per_active_hour"]["count"] == want["safety_stops"]
    tasks = real["task_autonomy_rate"]
    assert {"k": tasks["k"], "n": tasks["n"]} == want["task_autonomy_rate"]
    assert "op_01" not in (out / "report.json").read_text(encoding="utf-8")
