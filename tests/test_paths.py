"""Paths in reports and JSON outputs are the same on every system (CLAUDE.md product rule 4).

On Windows a path given with backslashes and the same path given with forward slashes must give
byte-identical outputs, written with forward slashes; on Linux and macOS both spellings are the
same string, and the outputs must hold no backslash either.
"""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

import pytest
from typer.testing import CliRunner

from autonometry import cli
from autonometry.paths import path_text
from report_helpers import ANCHORS, GENERATED_AT, LOGS, SEALED, VKEY

runner = CliRunner()
FOLDER = ("data", "sealed")


def test_a_windows_path_reads_as_its_posix_form__rule_4() -> None:
    windows = PureWindowsPath(r"logs\demo_amr_01\events.jsonl")
    posix = PurePosixPath("logs/demo_amr_01/events.jsonl")
    assert path_text(windows) == path_text(posix) == "logs/demo_amr_01/events.jsonl"
    assert path_text(PureWindowsPath(r"C:\data\events.jsonl")) == "C:/data/events.jsonl"
    assert path_text("events.jsonl") == "events.jsonl"
    assert path_text(Path("a") / "b.jsonl") == "a/b.jsonl"


@pytest.fixture
def workdir(tmp_path: Path) -> Iterator[Path]:
    """The sealed golden logs two folders down, and a log with one rejected line, as cwd."""
    target = tmp_path.joinpath(*FOLDER)
    shutil.copytree(SEALED, target)
    (target / "bad.jsonl").write_bytes(b"not json\n")
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(previous)


def _names(separator: str, *files: str) -> list[str]:
    return [separator.join((*FOLDER, name)) for name in files]


def _run(*args: str) -> str:
    with mock.patch.object(cli, "_utc_now", return_value=GENERATED_AT):
        result = runner.invoke(cli.app, list(args))
    assert result.exit_code in (0, 1), result.output
    return result.stdout


@pytest.mark.parametrize(
    "command",
    [
        lambda s: ["verify", "--json", *_names(s, *LOGS, "bad.jsonl"), "--vkey", *_names(s, VKEY),
                   "--anchor", *_names(s, ANCHORS)],
        lambda s: ["timeline", "--json", *_names(s, *LOGS)],
        lambda s: ["metrics", "--n-boot", "50", *_names(s, *LOGS)],
        lambda s: ["registry", "check", "--json", *_names(s, "devices.json")],
    ],
    ids=["verify", "timeline", "metrics", "registry"],
)  # fmt: skip
def test_json_outputs_are_the_same_with_either_separator__rule_4(
    workdir: Path, command: object
) -> None:
    build = command if callable(command) else None
    assert build is not None
    native, posix = _run(*build(os.sep)), _run(*build("/"))
    assert native == posix
    assert "\\" not in posix


def test_the_report_is_the_same_with_either_separator__rule_4_defs_11(workdir: Path) -> None:
    outputs = {}
    for name, separator in (("native", os.sep), ("posix", "/")):
        _run(
            "report",
            *_names(separator, *LOGS),
            "--vkey",
            *_names(separator, VKEY),
            "--anchor",
            *_names(separator, ANCHORS),
            "--n-boot",
            "50",
            "--out",
            f"out-{name}",
        )
        outputs[name] = {
            file: (workdir / f"out-{name}" / file).read_bytes()
            for file in ("report.json", "report.html")
        }
    assert outputs["native"] == outputs["posix"]
    text = outputs["posix"]["report.json"].decode("utf-8")
    assert "\\\\" not in text
    assert '"path": "data/sealed/s1.jsonl"' in text
    assert '"path": "data/sealed/golden.vkey"' in text
