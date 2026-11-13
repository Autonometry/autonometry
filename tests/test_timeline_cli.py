"""Golden timelines and ``autonometry timeline`` (definitions.md 1 to 8, Registries in 8)."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autonometry import cli
from autonometry.reader import LogReadError
from autonometry.registry import RegistryReadError

GOLDEN = Path(__file__).parent / "golden" / "timeline"
CASES = sorted(p for p in GOLDEN.iterdir() if p.is_dir())
runner = CliRunner()


@pytest.mark.parametrize("folder", CASES, ids=lambda p: p.name)
def test_golden_timeline__defs_2_to_8(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expected files were written by hand from the spec (tests/golden/README.md)."""
    monkeypatch.chdir(folder)
    result = runner.invoke(cli.app, ["timeline", "--json", "events.jsonl"])
    assert result.exit_code == 0, result.output
    assert result.output == (folder / "expected.json").read_text(encoding="utf-8")


def _copy(case: str, tmp_path: Path, *registries: str) -> Path:
    for name in ("events.jsonl", *registries):
        shutil.copyfile(GOLDEN / case / name, tmp_path / name)
    return tmp_path / "events.jsonl"


def _json(args: list[str]) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(cli.app, ["timeline", "--json", *args])
    return result.exit_code, json.loads(result.output)


def test_missing_registry_copies_give_flags__defs_8(tmp_path: Path) -> None:
    log = _copy("person_stop", tmp_path)
    code, report = _json([str(log)])
    assert code == 0
    (session,) = report["sessions"]
    assert session["registries"] == dict.fromkeys(("devices", "tasks", "plan"), "missing")
    flag_codes = {f["code"] for f in session["flags"]}
    assert {"registry_missing", "undeclared_device", "unknown_task_definition"} <= flag_codes
    assert session["time"]["whole_robot"]["states"]["ai"] == 0.0


def test_registry_that_does_not_match_its_hash_is_an_error__defs_8(tmp_path: Path) -> None:
    log = _copy("preset", tmp_path, "tasks.json", "plan.json")
    other = tmp_path / "devices.json"
    other.write_text(
        json.dumps({"devices": [{"device_id": "x", "kind": "ai", "match": {"node": "/x"}}]})
    )
    code, report = _json([str(log)])
    assert code == 1
    assert report["sessions"] == []
    assert report["counts"]["registry_errors"] == 1
    (error,) = report["registry_errors"]
    assert error["problem"].endswith("does not match session_start.device_registry_hash")


def test_named_registry_files_replace_the_folder_copies__defs_8(tmp_path: Path) -> None:
    log = _copy("preset", tmp_path)
    folder = GOLDEN / "preset"
    args = ["--devices", str(folder / "devices.json"), "--tasks", str(folder / "tasks.json")]
    code, report = _json([*args, "--plan", str(folder / "plan.json"), str(log)])
    assert code == 0
    assert report["sessions"][0]["registries"]["plan"] == "matched"


def test_invalid_registry_is_an_error__defs_8(tmp_path: Path) -> None:
    log = _copy("preset", tmp_path, "devices.json", "plan.json")
    (tmp_path / "tasks.json").write_text('{"tasks": []}', encoding="utf-8")
    code, report = _json([str(log)])
    assert code == 1
    assert (
        "is not a valid tasks registry: schema_violation" in report["registry_errors"][0]["problem"]
    )


def test_registry_without_a_declared_hash_cannot_be_matched__defs_8(tmp_path: Path) -> None:
    log = _copy("preset", tmp_path, "devices.json", "tasks.json", "plan.json")
    lines = log.read_text(encoding="utf-8").splitlines()
    start = json.loads(lines[0])
    del start["device_registry_hash"]
    log.write_text("\n".join([json.dumps(start), *lines[1:]]) + "\n", encoding="utf-8")
    code, report = _json([str(log)])
    assert code == 1
    assert "session_start has no device_registry_hash" in report["registry_errors"][0]["problem"]


def test_unreadable_registry_is_an_error__defs_8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _copy("preset", tmp_path, "devices.json", "tasks.json", "plan.json")

    def unreadable(path: object, version: str, kind: str) -> None:
        raise RegistryReadError(f"cannot read {path}: denied")

    monkeypatch.setattr(cli, "check_registry", unreadable)
    code, report = _json([str(log)])
    assert code == 1
    assert len(report["registry_errors"]) == 3


def test_rejected_lines_exit_1_and_are_listed__defs_8(tmp_path: Path) -> None:
    log = _copy("preset", tmp_path, "devices.json", "tasks.json", "plan.json")
    with open(log, "a", encoding="utf-8") as out:
        out.write("not json\n")
    code, report = _json([str(log)])
    assert code == 1
    assert report["counts"]["rejected_lines"] == 1
    assert len(report["sessions"]) == 1


def test_text_summary_names_every_part(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(GOLDEN / "humanoid")
    result = runner.invoke(cli.app, ["timeline", "events.jsonl"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert (
        lines[0] == "spec 0.3: 1 session(s), 20 event(s), 0 rejected line(s), 0 registry error(s)"
    )
    assert (
        lines[1] == "r01/humanoid: observed 60.000 s from 2026-11-12T08:00:00.000000000Z (closed)"
    )
    assert lines[2] == (
        "  whole robot: active 58.000 s, idle 2.000 s, unknown 0.000 s; "
        "ai 48.000 s, human_remote 10.000 s, idle 2.000 s"
    )
    assert any(line.startswith("  scope head: ") for line in lines)
    assert (
        "  interventions: 4 per scope, 3 whole robot; by kind: takeover 3; by reason: "
        "human_initiated 3"
    ) in lines
    assert "    w1 (walk_v1): success, not autonomous, human 10.000 s, 4 intervention(s)" in lines
    assert "  supervision: ratio 0.000, supervisor-hours per robot-hour 0.000" in lines


def test_text_summary_lists_flags_and_registry_errors(tmp_path: Path) -> None:
    log = _copy("unknown_gaps", tmp_path, "devices.json", "tasks.json", "plan.json")
    shutil.copytree(GOLDEN / "preset", tmp_path / "other")
    (tmp_path / "other" / "devices.json").write_text('{"devices": "x"}', encoding="utf-8")
    result = runner.invoke(
        cli.app, ["timeline", str(log), str(tmp_path / "other" / "events.jsonl")]
    )
    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert "  time_gap (1)" in lines
    assert "registry errors:" in lines
    assert any(line.startswith("  r01/preset: ") for line in lines)


def test_text_summary_of_a_session_without_start(tmp_path: Path) -> None:
    log = _copy("preset", tmp_path)
    log.write_text("".join(log.read_text(encoding="utf-8").splitlines(True)[1:]), "utf-8")
    result = runner.invoke(cli.app, ["timeline", str(log)])
    assert result.exit_code == 0
    assert "r01/preset: no session_start, nothing observed" in result.output.splitlines()


def test_many_tasks_are_shortened_in_the_text_summary(tmp_path: Path) -> None:
    lines = [
        {"robot_id": "r", "session_id": "s", "task_id": None, "seq": 0,
         "ts": "2026-11-12T08:00:00.000Z", "type": "session_start", "scopes": ["whole_robot"],
         "initial_states": {"whole_robot": "unknown"}, "heartbeat_interval_s": 1000.0,
         "hint_policy": "hint_as_event", "task_registry_hash": "0" * 64, "plan_hash": "0" * 64,
         "simulated": True},
    ]  # fmt: skip
    for index in range(25):
        lines.append({"robot_id": "r", "session_id": "s", "task_id": f"t{index}",
                      "seq": 1 + index, "ts": f"2026-11-12T08:00:{index:02d}.000Z",
                      "type": "task_start", "task_definition_id": "d"})  # fmt: skip
    log = tmp_path / "events.jsonl"
    log.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    result = runner.invoke(cli.app, ["timeline", str(log)])
    assert result.exit_code == 0
    assert "    ... and 5 more" in result.output.splitlines()


def test_timeline_bad_usage_exits_2(tmp_path: Path) -> None:
    assert runner.invoke(cli.app, ["timeline", str(tmp_path / "none.jsonl")]).exit_code == 2
    log = _copy("preset", tmp_path)
    missing = ["timeline", str(log), "--devices", str(tmp_path / "none.json")]
    assert runner.invoke(cli.app, missing).exit_code == 2
    assert runner.invoke(cli.app, ["timeline", str(log), "--spec", "9.9"]).exit_code == 2


@pytest.mark.parametrize(
    ("kind", "file", "line"),
    [
        ("tasks", "tasks.json", "spec 0.3: 2 task(s), valid"),
        ("plan", "plan.json", "spec 0.3: 2 plan item(s), valid"),
        ("devices", "devices.json", "spec 0.3: 5 device(s), valid"),
    ],
)
def test_registry_check_kind__defs_8(kind: str, file: str, line: str) -> None:
    path = GOLDEN / "preset" / file
    result = runner.invoke(cli.app, ["registry", "check", "--kind", kind, str(path)])
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == line


def test_registry_check_unknown_kind_exits_2() -> None:
    path = GOLDEN / "preset" / "plan.json"
    result = runner.invoke(cli.app, ["registry", "check", "--kind", "robots", str(path)])
    assert result.exit_code == 2


def test_unreadable_log_is_one_line_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _copy("preset", tmp_path)

    def unreadable(paths: object, version: str) -> None:
        raise LogReadError(f"cannot read {log}: denied")

    monkeypatch.setattr(cli, "read_logs", unreadable)
    result = runner.invoke(cli.app, ["timeline", str(log)])
    assert result.exit_code == 1
    assert result.output.splitlines() == [f"error: cannot read {log}: denied"]
