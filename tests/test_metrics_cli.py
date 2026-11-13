"""``autonometry metrics`` and the golden metrics set (definitions.md 3 to 9)."""

import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autonometry import cli

GOLDEN = Path(__file__).parent / "golden" / "metrics" / "logs"
EXPECTED = GOLDEN.parent / "expected.json"
LOGS = ["s1.jsonl", "s2.jsonl", "s3.jsonl", "s4.jsonl"]
#: Floats may differ in late digits between platforms and library versions (optimisers, BLAS).
REL = 1e-4
ABS = 1e-8
runner = CliRunner()


def _close(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Differences between two JSON values: floats within the tolerance, everything else equal."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected) != set(actual):
            return [f"{path}: keys {sorted(set(expected) ^ set(actual))}"]
        return [d for key in expected for d in _close(expected[key], actual[key], f"{path}.{key}")]
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(expected)} != {len(actual)}"]
        return [
            d
            for i, pair in enumerate(zip(expected, actual, strict=True))
            for d in _close(*pair, f"{path}[{i}]")
        ]
    if (
        isinstance(expected, float)
        and isinstance(actual, float | int)
        and not isinstance(actual, bool)
    ):
        ok = math.isclose(expected, actual, rel_tol=REL, abs_tol=ABS)
        return [] if ok else [f"{path}: {expected} != {actual}"]
    return (
        []
        if expected == actual and type(expected) is type(actual)
        else [f"{path}: {expected!r} != {actual!r}"]
    )


def _run(folder: Path, *args: str) -> tuple[int, dict[str, Any], str]:
    out = folder / "out.json"
    result = runner.invoke(cli.app, ["metrics", "--n-boot", "200", "--out", str(out), *args])
    report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    return result.exit_code, report, result.stderr


def test_golden_metrics__defs_3_to_9(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point estimates and bounds are checked by hand in the next test; intervals come from the
    estimators of test_stats_intervals (tests/golden/README.md)."""
    monkeypatch.chdir(GOLDEN)
    code, report, _ = _run(tmp_path, *LOGS)
    assert code == 0
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    assert _close(expected, report) == []


def test_golden_metrics_by_hand__defs_7(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(GOLDEN)
    _, report, _ = _run(tmp_path, *LOGS)
    real, simulated = report["groups"]["real"], report["groups"]["simulated"]
    assert real["n_sessions"] == 3
    assert real["n_robots"] == 2
    # AI minus moved dwell over active time: s1 (140 - 2) / 150, s2 (104 - 1) / 109, s3 77 / 90.
    share = real["time_autonomy_share"]
    assert share["estimate"] == pytest.approx(318 / 349)
    assert share["unknown_as_human"]["estimate"] == pytest.approx(318 / 370)
    assert share["unknown_as_ai"]["estimate"] == pytest.approx(339 / 370)
    assert share["supported_time_s"] == 20.0
    # Whole-robot interventions 2 + 1 + 2; s2 has two unknown runs between AI spans.
    rate = real["interventions_per_active_hour"]
    assert rate["count"] == 5
    assert rate["per_scope"]["count"] == 6
    assert rate["estimate"] == pytest.approx(5 / (349 / 3600))
    assert rate["unknown_as_human"]["count"] == 7
    assert real["safety_stops_per_active_hour"]["automatic"]["count"] == 1
    # No closed task is autonomous; t4 is unclosed.
    tasks = real["task_autonomy_rate"]
    assert (tasks["k"], tasks["n"], tasks["unknown_as_human"]["n"]) == (0, 5, 6)
    # Human seconds per task: 2, 10, 1, 0, 10, 3; t3 holds 21 s of unknown time.
    minutes = real["human_minutes_per_task"]
    assert minutes["estimate"] == pytest.approx(26 / 6 / 60)
    assert minutes["unknown_as_human"]["estimate"] == pytest.approx(47 / 6 / 60)
    assert real["gaps_between_interventions"]["metres"]["estimate"] == 3.5
    assert real["unmeasured"]["unknown_share"] == pytest.approx(21 / 370)
    assert real["unmeasured"]["conformance"] == "incomplete"
    assert simulated["time_autonomy_share"]["estimate"] == pytest.approx(47 / 50)
    assert simulated["unmeasured"]["conformance"] == "complete"


def test_same_logs_and_options_give_byte_identical_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(GOLDEN)
    outputs = []
    for order in (LOGS, list(reversed(LOGS))):
        result = runner.invoke(cli.app, ["metrics", "--n-boot", "50", *order])
        assert result.exit_code == 0
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    seeded = runner.invoke(cli.app, ["metrics", "--n-boot", "50", "--seed", "1", *LOGS]).stdout
    assert seeded != outputs[0]


def test_options_are_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(GOLDEN)
    args = ["--alpha", "0.1", "--seed", "3", "--cluster", "robot", "--drift-bin", "week", *LOGS]
    code, report, _ = _run(tmp_path, *args)
    assert code == 0
    assert report["options"] == {
        "alpha": 0.1,
        "cluster": "robot",
        "drift_bin": "week",
        "n_boot": 200,
        "seed": 3,
    }
    assert report["groups"]["real"]["interventions_per_active_hour"]["cluster"] == "robot"
    assert report["groups"]["real"]["drift"]["bin"] == "week"


def test_session_without_timeline_is_left_out_and_named__defs_8(tmp_path: Path) -> None:
    for name in [*LOGS, "tasks.json", "plan.json"]:
        shutil.copyfile(GOLDEN / name, tmp_path / name)
    other = tmp_path / "other"
    other.mkdir()
    shutil.move(tmp_path / "s3.jsonl", other / "s3.jsonl")
    shutil.copyfile(GOLDEN / "devices.json", tmp_path / "devices.json")
    (other / "devices.json").write_text(
        json.dumps({"devices": [{"device_id": "x", "kind": "ai", "match": {"node": "/x"}}]})
    )
    logs = [str(tmp_path / n) for n in ("s1.jsonl", "s2.jsonl", "s4.jsonl")]
    code, report, stderr = _run(tmp_path, *logs, str(other / "s3.jsonl"))
    assert code == 1
    assert "r02/s3 left out" in stderr
    assert report["counts"]["sessions_left_out"] == 1
    assert report["sessions_left_out"][0]["session_id"] == "s3"
    assert report["groups"]["real"]["n_sessions"] == 2


def test_rejected_lines_exit_1(tmp_path: Path) -> None:
    for name in ["s4.jsonl", "devices.json", "tasks.json", "plan.json"]:
        shutil.copyfile(GOLDEN / name, tmp_path / name)
    with open(tmp_path / "s4.jsonl", "a", encoding="utf-8") as handle:
        handle.write("not json\n")
    code, report, stderr = _run(tmp_path, str(tmp_path / "s4.jsonl"))
    assert code == 1
    assert report["counts"]["rejected_lines"] == 1
    assert "1 rejected line(s)" in stderr


@pytest.mark.parametrize(
    "args",
    [["--alpha", "0"], ["--alpha", "1.5"], ["--cluster", "site"], ["--n-boot", "1"]],
)
def test_bad_options_exit_2(args: list[str]) -> None:
    result = runner.invoke(cli.app, ["metrics", *args, str(GOLDEN / "s4.jsonl")])
    assert result.exit_code == 2


def test_without_the_report_extra_one_line_and_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [m for m in sys.modules if m.startswith("autonometry.stats")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "numpy", None)
    result = runner.invoke(cli.app, ["metrics", str(GOLDEN / "s4.jsonl")])
    assert result.exit_code == 2
    assert result.stderr.strip().splitlines() == [
        "error: autonometry metrics needs the report extra: pip install 'autonometry[report]'"
    ]


def test_unknown_spec_is_bad_usage() -> None:
    result = runner.invoke(cli.app, ["metrics", "--spec", "9.9", str(GOLDEN / "s4.jsonl")])
    assert result.exit_code == 2
    assert "unknown spec version" in result.stderr
