"""``autonometry reconcile`` and ``autonometry report --station`` (definitions.md 11.2, 11.7,
12): the golden pair, scenario S2, a tampered station log, an unsynced station clock, and logs
given on the wrong side."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from witness_files import (
    ANCHORS,
    ARGS,
    GOLDEN,
    STATION_KEY,
    STATION_KEY_NAME,
    VKEYS,
    run_in,
    seal,
)
from witness_helpers import events

HONEST = (
    "reconcile",
    "--robot",
    "robot/r01.jsonl",
    "--station",
    "station/r01.jsonl",
    "--vkey",
    VKEYS,
    "--anchor",
    "robot/r01.checkpoints.jsonl",
    "--anchor",
    "station/r01.checkpoints.jsonl",
)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """A copy of the golden folder that a test may change."""
    copy = tmp_path / "witness"
    shutil.copytree(GOLDEN, copy)
    return copy


def reconciled(folder: Path, *args: str) -> tuple[int, dict[str, Any]]:
    code, output = run_in(folder, *args)
    return code, json.loads(output)


def session(body: dict[str, Any], robot_id: str) -> dict[str, Any]:
    (found,) = [s for s in body["sessions"] if s["robot_id"] == robot_id]
    return found


def test_golden_reconcile__defs_12() -> None:
    code, output = run_in(GOLDEN, *ARGS)
    assert code == 1
    assert output == (GOLDEN / "expected.json").read_text(encoding="utf-8")


def test_honest_pair_reaches_l3_and_exits_0__defs_12_6(folder: Path) -> None:
    code, body = reconciled(folder, *HONEST)
    assert code == 0
    assert body["all_l3"] is True
    assert session(body, "r01")["level"] == "L3"


def test_scenario_s2_station_only_time_gives_no_l3__defs_12_6() -> None:
    _, body = reconciled(GOLDEN, *ARGS)
    hidden = session(body, "r02")
    assert hidden["level"] == "L2"
    assert hidden["times"]["station_only_s"] == 30.0
    assert [r["code"] for r in hidden["reasons"]] == [
        "station_only_above_max",
        "disputed_above_max",
    ]
    assert hidden["interventions"] == {"robot": 0, "upper_bound": 1}
    assert [f["code"] for f in hidden["flags"]] == ["implausible_clock_error"]


def test_tampered_station_log_is_failed_and_gives_no_l3__defs_10_6_12_6(folder: Path) -> None:
    log = folder / "station" / "r01.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if b'"human_remote"' in line)
    lines[index] = lines[index].replace(b'"human_remote"', b'"idle"', 1)
    log.write_bytes(b"".join(lines))
    code, body = reconciled(folder, *HONEST)
    assert code == 1
    honest = session(body, "r01")
    assert honest["stations"] == [{"session_id": "st01", "station_id": "pc_1", "status": "failed"}]
    # The tampered log no longer shows the takeover, so the robot's run is unmatched too.
    assert [r["code"] for r in honest["reasons"]] == ["station_not_verified", "disputed_above_max"]
    assert honest["reasons"][0]["detail"] == "st01 (pc_1) failed"
    assert body["verification"]["station"]["verified"] is False


def test_unsynced_station_clock_gives_no_l3__defs_12_3(folder: Path) -> None:
    raw = events(
        [(100.1, "human_remote"), (130.05, "idle")],
        301,
        start=-5,
        robot_id="r01",
        role="station",
        session_id="st01",
        station_id="pc_1",
        clock_source="none",
    )
    log, checkpoints = seal(raw, STATION_KEY, STATION_KEY_NAME)
    (folder / "station" / "r01.jsonl").write_bytes(log)
    (folder / "station" / "r01.checkpoints.jsonl").write_bytes(checkpoints)
    code, body = reconciled(folder, *HONEST)
    assert code == 1
    assert [r["code"] for r in session(body, "r01")["reasons"]] == ["station_clock_unsynced"]


def test_logs_on_the_wrong_side_are_ignored__defs_12_1(folder: Path) -> None:
    code, body = reconciled(
        folder, "reconcile", "--robot", "station/r01.jsonl", "--station", "robot/r01.jsonl"
    )
    assert code == 1
    assert body["sessions"] == []
    assert [(i["session_id"], i["given_as"]) for i in body["ignored"]] == [
        ("s01", "station"),
        ("st01", "robot"),
    ]


def test_registry_not_matching_its_hash_is_listed__defs_8(folder: Path) -> None:
    (folder / "station" / "devices.json").write_text('{"devices": []}', encoding="utf-8")
    code, body = reconciled(folder, *HONEST)
    assert code == 1
    assert [(e["role"], e["session_id"]) for e in body["registry_errors"]] == [("station", "st01")]
    assert [r["code"] for r in session(body, "r01")["reasons"]] == ["no_station"]


def test_out_writes_the_json_and_prints_a_summary(folder: Path) -> None:
    code, output = run_in(folder, *HONEST, "--out", "witness.json")
    assert code == 0
    assert output == "reconciled 1 robot session(s) with 1 station session(s): 1 at L3\n"
    assert json.loads((folder / "witness.json").read_text(encoding="utf-8"))["all_l3"] is True


def test_out_that_cannot_be_written_exits_2(folder: Path) -> None:
    code, output = run_in(folder, *HONEST, "--out", "missing/witness.json")
    assert code == 2
    assert output.startswith("error: cannot write")


def test_unreadable_input_exits_2(folder: Path) -> None:
    (folder / "bad.vkey").write_text("not a vkey\n", encoding="utf-8")
    code, output = run_in(folder, *HONEST, "--vkey", "bad.vkey")
    assert code == 2
    assert "error:" in output
    code, _ = run_in(folder, "reconcile", "--robot", "robot/r01.jsonl")
    assert code == 2


# -- report --station ---------------------------------------------------------------------------

pytest.importorskip("matplotlib")


def report(folder: Path, *args: str) -> tuple[int, dict[str, Any], str]:
    code, output = run_in(folder, "report", *args, "--out", "out", "--n-boot", "50")
    assert "Traceback" not in output
    body = json.loads((folder / "out" / "report.json").read_text(encoding="utf-8"))
    return code, body, (folder / "out" / "report.html").read_text(encoding="utf-8")


def test_report_shows_l3_and_the_witness_section__defs_11_2_12(folder: Path) -> None:
    code, body, page = report(
        folder,
        "robot/r01.jsonl",
        "robot/r02.jsonl",
        "--station",
        "station/r01.jsonl",
        "--station",
        "station/r02.jsonl",
        "--vkey",
        VKEYS,
        "--anchor",
        ANCHORS,
    )
    assert code == 1
    trust = body["trust"]
    assert {s["robot_id"]: s["level"] for s in trust["sessions"]} == {"r01": "L3", "r02": "L2"}
    assert trust["level"] == "L2"
    assert trust["status"] == "verified"
    assert [s["status"] for s in trust["stations"]["sessions"]] == ["verified", "verified"]
    witness = body["witness"]
    assert witness["available"] is True
    assert "network witness" in witness["statement"]
    assert [s["level"] for s in witness["sessions"]] == ["L3", "L2"]
    invariant = body["invariants"]["witnesses"]
    assert invariant["status"] == "fail"
    assert invariant["failures"][0].startswith("r02/s01: (station-only + unwitnessed)")
    assert "invariant witnesses failed" in body["verdict"]["reasons"]
    roles = {f["role"] for f in body["provenance"]["input_files"]}
    assert {"station_log", "station_checkpoints", "station_registry_devices"} <= roles
    assert 'id="witness"' in page
    assert "network witness" in page
    assert "op_a" not in json.dumps(body)


def test_report_of_an_honest_pair_is_l3__defs_11_2_11_7(folder: Path) -> None:
    code, body, _ = report(
        folder,
        "robot/r01.jsonl",
        "--station",
        "station/r01.jsonl",
        "--vkey",
        VKEYS,
        "--anchor",
        "robot/r01.checkpoints.jsonl",
        "--anchor",
        "station/r01.checkpoints.jsonl",
    )
    assert body["verdict"]["level"] == "L3"
    assert body["invariants"]["witnesses"]["status"] == "pass"
    assert body["verdict"]["clean"] is True
    assert code == 0


def test_report_leaves_a_station_log_given_as_robot_log_out__defs_11_4(folder: Path) -> None:
    _, body, _ = report(folder, "robot/r01.jsonl", "station/r01.jsonl", "--vkey", VKEYS)
    (left_out,) = body["exclusions"]
    assert left_out["session_id"] == "st01"
    assert left_out["reasons"][0].startswith("a station log (role station)")
    assert body["census"]["robots"][0]["sessions_not_checked"] == ["s01"]
    assert body["witness"]["available"] is False
    assert body["trust"]["levels"]["L3"] == "not available: no station log was given"
