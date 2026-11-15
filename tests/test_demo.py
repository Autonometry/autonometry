"""``autonometry demo``: the shipped session sealed, verified (L1) and reported in a new folder."""

import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autonometry import cli, demo, sealfile
from demo_helpers import DATA, log_bytes

runner = CliRunner()
#: Inputs whose bytes depend on the throwaway key: they differ between runs, nothing else does.
KEY_DEPENDENT = {"checkpoints", "vkey"}


def _run(out: Path) -> Any:
    return runner.invoke(cli.app, ["demo", "--out", str(out)])


@pytest.fixture(scope="module")
def first(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Any]:
    out = tmp_path_factory.mktemp("demo") / "demo-out-7c1b"
    return out, _run(out)


def _report(out: Path) -> dict[str, Any]:
    return json.loads((out / demo.REPORT_JSON).read_text(encoding="utf-8"))


def _without_key_and_time(report: dict[str, Any]) -> dict[str, Any]:
    provenance = dict(report["provenance"])
    provenance.pop("generated_at")
    provenance["input_files"] = [
        f for f in provenance["input_files"] if f["role"] not in KEY_DEPENDENT
    ]
    return {**report, "provenance": provenance}


def test_the_shipped_demo_log_is_its_scenario() -> None:
    assert (DATA / "events.jsonl").read_bytes() == log_bytes()


def test_the_demo_writes_every_file_and_names_the_one_to_open(first: tuple[Path, Any]) -> None:
    out, result = first
    assert result.exit_code == 0, result.output
    for path in (
        demo.REPORT_HTML,
        demo.REPORT_JSON,
        demo.VERIFICATION,
        demo.LOG,
        demo.CHECKPOINTS,
        demo.KEY,
        demo.VKEY,
        demo.ABOUT,
        *(demo.SESSION_DIR / name for name in demo.REGISTRIES),
    ):
        assert (out / path).is_file(), path
        assert path.as_posix() in result.stdout or path.name in result.stdout
    assert "OPEN THIS" in result.stdout
    assert f"Open {out / demo.REPORT_HTML} in a browser." in result.stdout


def test_the_demo_session_is_sealed_and_meets_l1_but_not_l2__defs_10_6(
    first: tuple[Path, Any],
) -> None:
    out, result = first
    verification = json.loads((out / demo.VERIFICATION).read_text(encoding="utf-8"))
    assert verification["require"] == "L1"
    assert verification["meets_required_level"] is True
    assert verification["verified"] is False
    (session,) = verification["sessions"]
    assert session["evidence"] == []
    assert {g["code"] for g in session["gaps"]} == {"not_anchored"}
    assert "meets trust level L1, not L2" in result.stdout
    assert "no checkpoint is anchored" in result.stdout
    assert all(json.loads(line)["hash"] for line in (out / demo.LOG).read_bytes().splitlines())


def test_the_demo_report_shows_the_scenario__defs_11(first: tuple[Path, Any]) -> None:
    out, _ = first
    report = _report(out)
    verdict = report["verdict"]
    assert (verdict["status"], verdict["level"], verdict["completeness"]) == (
        "not_verified",
        "L1",
        "complete",
    )
    assert verdict["clean"] is False
    assert report["provenance"]["software"]["version"] == cli.__version__
    group = report["metrics"]["groups"]["simulated"]
    assert group["task_autonomy_rate"]["estimate"] == pytest.approx(5 / 6)
    assert group["unmeasured"]["unknown_share"] == pytest.approx(14.675 / 740, rel=1e-5)
    assert group["interventions_per_active_hour"]["estimate"] == pytest.approx(3600 / 677.421)
    html = (out / demo.REPORT_HTML).read_text(encoding="utf-8")
    assert demo.TITLE in html
    assert "<script" not in html


def test_input_paths_in_the_demo_report_are_relative_to_its_folder(
    first: tuple[Path, Any],
) -> None:
    out, _ = first
    text = (out / demo.REPORT_JSON).read_text(encoding="utf-8")
    assert str(out) not in text
    assert out.name not in text
    paths = {Path(f["path"]) for f in _report(out)["provenance"]["input_files"]}
    assert demo.LOG in paths
    assert demo.VKEY in paths


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_demo_key_is_private(first: tuple[Path, Any]) -> None:
    out, _ = first
    assert stat.S_IMODE(os.stat(out / demo.KEY.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(out / demo.KEY).st_mode) == 0o600


def test_the_demo_is_the_same_every_run_apart_from_the_key_and_time(
    first: tuple[Path, Any], tmp_path: Path
) -> None:
    out, result = first
    again = _run(tmp_path / "again")
    assert again.exit_code == 0
    assert again.stdout.replace(str(tmp_path / "again"), "OUT") == result.stdout.replace(
        str(out), "OUT"
    )
    assert _without_key_and_time(_report(tmp_path / "again")) == _without_key_and_time(_report(out))
    for path in (demo.LOG, demo.VERIFICATION):
        assert (tmp_path / "again" / path).read_bytes() == (out / path).read_bytes()
    assert (tmp_path / "again" / demo.KEY).read_bytes() != (out / demo.KEY).read_bytes()


def test_the_demo_refuses_a_folder_that_is_not_empty(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("mine", encoding="utf-8")
    result = _run(tmp_path)
    assert result.exit_code == 2
    assert result.stderr.strip().splitlines() == [
        f"error: {tmp_path} is not an empty folder; give a new one"
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]


def test_the_demo_refuses_a_file_in_place_of_its_folder(tmp_path: Path) -> None:
    target = tmp_path / "report"
    target.write_text("", encoding="utf-8")
    result = _run(target)
    assert result.exit_code == 2
    assert "is not an empty folder" in result.stderr


def test_the_demo_says_in_one_line_when_it_cannot_write(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    result = _run(blocker / "demo")
    assert result.exit_code == 2
    (line,) = result.stderr.strip().splitlines()
    assert line.startswith("error: cannot write the demo to ")


def test_the_demo_without_the_report_extra_prints_one_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "autonometry.report", None)
    result = _run(tmp_path / "demo")
    assert result.exit_code == 2
    assert result.stderr.strip().splitlines() == [
        "error: autonometry demo needs the report extra: pip install 'autonometry[report]'"
    ]
    assert not (tmp_path / "demo").exists()


def test_a_demo_session_that_fails_verification_stops_before_the_report__defs_10_6(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real: Callable[..., tuple[bytes, bytes]] = sealfile.seal_sessions

    def tampered(*args: Any) -> tuple[bytes, bytes]:
        sealed, checkpoints = real(*args)
        return sealed.replace(b'"op_7f3a"', b'"op_0000"'), checkpoints

    monkeypatch.setattr(sealfile, "seal_sessions", tampered)
    result = _run(tmp_path / "demo")
    assert result.exit_code == 1
    assert "the sealed demo session does not meet L1" in result.stderr
    assert not (tmp_path / "demo" / demo.REPORT_JSON).exists()
