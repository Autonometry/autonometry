"""Golden test: the integration notes' 17-event example (research/turtlebot4, B.5) rebuilt as
TurtleBot 4 observations, run through the engine, the real recorder (fake clock) and
``autonometry timeline``. The expected log, states, episodes and tasks in
``tests/golden/tb4/expected.json`` were worked out by hand under spec v0.3 before the code ran;
``tests/golden/tb4/README.md`` explains where they differ from the notes' v0.2 expectation."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autonometry import cli
from autonometry.connectors import tb4
from autonometry.recorder.store import EVENTS_FILE
from autonometry.timestamps import ts_to_ns
from recorder_helpers import Client, FakeClock, make_prepared, make_recorder
from tb4_helpers import CONFIG, GOLDEN, datagrams, engine, load_observations

EXPECTED = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))
T0_NS = ts_to_ns(EXPECTED["start"])
END_S: float = EXPECTED["end_offset_s"]
#: The node's timer runs every 0.1 s here (0.25 s on a robot).
TICKS_PER_S = 10


def _set(clock: FakeClock, t: float) -> None:
    clock.wall = T0_NS + round(t * 1e9)
    clock.mono = 1000.0 + t


def _schedule(observations: list[tb4.Observation]) -> list[tuple[float, int, Any]]:
    """Observations and timer ticks in time order; at one instant, observations first."""
    first = min(o.t for o in observations)
    ticks = range(int(first * TICKS_PER_S), round(END_S * TICKS_PER_S))
    items = [(o.t, 0, o) for o in observations] + [(k / TICKS_PER_S, 1, None) for k in ticks]
    return sorted(items, key=lambda item: (item[0], item[1]))


def record_golden(tmp_path: Path) -> Path:
    """Run the session and return the folder of its sealed log."""
    prepared = make_prepared(
        tmp_path,
        robot_id="tb4_sim_01",
        scopes=["base"],
        unobserved=["safety_stops", "human_inputs"],
        device_registry=str(CONFIG / "devices.json"),
        task_registry=str(CONFIG / "tasks.json"),
        plan=str(CONFIG / "plan.json"),
    )
    clock = FakeClock(wall_ns=T0_NS)
    recorder = make_recorder(prepared, clock)
    observer = engine()
    client = Client("tb4_observer")
    started = False
    for t, kind, item in _schedule(load_observations(GOLDEN / "observations.jsonl")):
        if t >= 0 and not started:
            _set(clock, 0.0)
            recorder.start()
            started = True
        if started:
            _set(clock, t)
            recorder.tick()
        messages = observer.observe(item) if kind == 0 else observer.tick(t)
        if started:
            for data in datagrams(client, messages):
                recorder.receive(data)
    _set(clock, END_S)
    recorder.tick()
    recorder.stop()
    robot = prepared.config.data_dir / prepared.config.robot_id
    (folder,) = [p for p in robot.iterdir() if p.is_dir()]
    return folder


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    folder = record_golden(tmp_path_factory.mktemp("tb4"))
    log = folder / EVENTS_FILE
    events = [json.loads(line) for line in log.read_bytes().splitlines()]
    result = CliRunner().invoke(cli.app, ["timeline", "--json", str(log)])
    assert result.exit_code == 0, result.output
    (session,) = json.loads(result.output)["sessions"]
    return events, session


def _offset(event: dict[str, Any]) -> float:
    return round((ts_to_ns(event["ts"]) - T0_NS) / 1e9, 3)


def test_log_matches_the_hand_worked_events__notes_b5_defs_8(
    golden: tuple[list[dict[str, Any]], dict[str, Any]],
) -> None:
    events, _ = golden
    written = [e for e in events if e["type"] != "heartbeat"]
    expected = EXPECTED["log"]
    assert len(written) == len(expected), [(_offset(e), e["type"], e.get("state")) for e in written]
    for event, want in zip(written, expected, strict=True):
        got = {"t": _offset(event)} | {k: event.get(k) for k in want if k != "t"}
        assert got == want


def test_timeline_matches_the_hand_worked_states_episodes_and_tasks__notes_b5_defs_3_5_6_7(
    golden: tuple[list[dict[str, Any]], dict[str, Any]],
) -> None:
    _, session = golden
    want = EXPECTED["timeline"]
    whole = session["time"]["whole_robot"]
    assert whole["observed_s"] == pytest.approx(want["observed_s"])
    assert whole["active_s"] == pytest.approx(want["active_s"])
    assert whole["states"] == pytest.approx(want["states_s"])
    interventions = session["interventions"]
    assert interventions["whole_robot"] == want["interventions_whole_robot"]
    assert interventions["by_kind"] == want["interventions_by_kind"]
    assert [{"in_episode": h["in_episode"]} for h in interventions["hints"]] == want["hints"]
    stops = session["safety_stops"]
    assert {k: stops[k] for k in want["safety_stops"]} == want["safety_stops"]
    tasks = [{k: task[k] for k in want["tasks"][0]} for task in session["tasks"]]
    assert tasks == [pytest.approx(t) for t in want["tasks"]]
    assert session["resets"] == pytest.approx(want["resets"])
    assert session["humans"]["distinct_human_actors"] == want["distinct_human_actors"]
    assert session["distance_m"] == pytest.approx(want["distance_m"])
    assert sorted({f["code"] for f in session["flags"]}) == want["flag_codes"]


def test_the_log_verifies_and_inferred_attribution_caps_it_at_l1__defs_10_6(
    golden: tuple[list[dict[str, Any]], dict[str, Any]],
) -> None:
    events, _ = golden
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert all(e["prev_hash"] and e["hash"] for e in events)
    assert sum(1 for e in events if e.get("x_attribution_inferred")) == 3
