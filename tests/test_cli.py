import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from autonometry import __version__, cli, reader, spec

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"autonometry {__version__}"


def test_spec_list() -> None:
    result = runner.invoke(cli.app, ["spec", "list"])
    assert result.exit_code == 0
    assert result.output.split() == ["0.3"]


def test_spec_show_prints_names_with_hashes__defs_8() -> None:
    result = runner.invoke(cli.app, ["spec", "show", "0.3"])
    assert result.exit_code == 0
    lines = [line.split("  ") for line in result.output.strip().splitlines()]
    assert [name for _, name in lines] == [
        "checkpoint.schema.json",
        "definitions.md",
        "device_registry.schema.json",
        "event.schema.json",
        "plan.schema.json",
        "rules.toml",
        "task_registry.schema.json",
    ]
    assert all(len(digest) == 64 for digest, _ in lines)


def test_spec_show_unknown_version_is_one_line_exit_1() -> None:
    result = runner.invoke(cli.app, ["spec", "show", "9.9"])
    assert result.exit_code == 1
    assert result.output.strip().splitlines() == [
        "error: unknown spec version '9.9'; available: 0.3"
    ]


def test_spec_show_path_like_version_is_exit_1() -> None:
    result = runner.invoke(cli.app, ["spec", "show", "../.."])
    assert result.exit_code == 1
    assert result.output.startswith("error: invalid spec version")


def test_spec_list_failure_is_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> list[str]:
        raise spec.SpecError("specification files not found")

    monkeypatch.setattr(cli, "available_versions", broken)
    result = runner.invoke(cli.app, ["spec", "list"])
    assert result.exit_code == 1
    assert result.output.strip() == "error: specification files not found"


@pytest.mark.parametrize("args", [["spec", "show"], ["nonsense"], ["--no-such-flag"]])
def test_bad_usage_is_exit_2(args: list[str]) -> None:
    assert runner.invoke(cli.app, args).exit_code == 2


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(cli.app, [])
    assert "spec" in result.output


def test_main_turns_unexpected_failure_into_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert capsys.readouterr().err.strip() == "error: unexpected failure (RuntimeError)"


def test_main_passes_through_normal_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["autonometry", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0


GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _validate(*args: str) -> Result:
    return runner.invoke(cli.app, ["validate", *args])


def test_validate_clean_log_exits_0__defs_8() -> None:
    result = _validate(str(GOLDEN / "valid" / "multi_robot.jsonl"))
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "spec 0.3: 1 file(s), 30 line(s), 30 event(s) in 3 session(s), "
        "0 blank, 0 rejected, 0 flag(s)"
    ]


def test_validate_flags_are_warnings_exit_0__defs_8() -> None:
    result = _validate(str(GOLDEN / "flags" / "seq_gap.jsonl"))
    assert result.exit_code == 0
    assert result.output.splitlines()[1:] == [
        "flags (warnings):",
        "  seq_gap (1)",
        "    robot r01 session s01 seq 4: seq jumps from 1 to 4",
    ]


def test_validate_rejected_line_exits_1__defs_8(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both files hold session r01/s01, so the second file's copies are duplicates."""
    monkeypatch.chdir(GOLDEN / "errors")
    result = _validate("not_an_object.jsonl", "duplicate_seq.jsonl")
    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert lines[0].endswith("9 rejected, 0 flag(s)")
    assert lines[1:8] == [
        "rejected lines:",
        "  duplicate_seq (4)",
        "    duplicate_seq.jsonl:1: seq 0 is already used in this session",
        "    duplicate_seq.jsonl:2: seq 1 is already used in this session",
        "    duplicate_seq.jsonl:3: seq 1 is already used in this session",
        "    duplicate_seq.jsonl:7: seq 2 is already used in this session",
        "  not_an_object (5)",
    ]


@pytest.mark.parametrize(
    "fixture", ["errors/schema_violation", "flags/time_gap", "valid/crlf_bom_blank"]
)
def test_validate_json_output_matches_golden__defs_8(
    fixture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = GOLDEN / f"{fixture}.jsonl"
    monkeypatch.chdir(path.parent)
    result = _validate("--json", path.name)
    expected = path.with_name(f"{path.stem}.expected.json").read_text(encoding="utf-8")
    assert result.output == expected
    assert result.exit_code == (1 if fixture.startswith("errors") else 0)


def test_validate_shows_twenty_per_code_then_a_count(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    log.write_bytes(b"[]\n" * 25 + b"42\n")
    result = _validate(str(log))
    lines = result.output.splitlines()
    assert lines[2] == "  not_an_object (26)"
    assert len([line for line in lines if line.startswith(f"    {log}:")]) == 20
    assert lines[-1] == "    ... and 6 more"


def test_validate_more_uses_exact_counts_beyond_the_listed_lines__defs_8(tmp_path: Path) -> None:
    limit = spec.load_rules("0.3").errors.list_max_per_code
    log = tmp_path / "log.jsonl"
    log.write_bytes(b"[]\n" * (limit + 30))
    lines = _validate(str(log)).output.splitlines()
    assert lines[0].endswith(f"{limit + 30} rejected, 0 flag(s)")
    assert lines[2] == f"  not_an_object ({limit + 30})"
    assert lines[-1] == f"    ... and {limit + 10} more"


def test_validate_output_has_no_control_characters__defs_8(tmp_path: Path) -> None:
    """A hostile robot_id cannot reach the terminal: it is rejected and never echoed."""
    hostile = "r01\x1b[2J\x1b[H\x07\x1b]0;owned\x07"
    event = {"robot_id": "r01", "session_id": "s01", "task_id": None, "seq": 0}
    event |= {"ts": "2026-10-30T09:00:00.000Z", "type": "heartbeat"}
    lines = [event | {"robot_id": hostile}, event | {"session_id": hostile}]
    log = tmp_path / "log.jsonl"
    log.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    text = _validate(str(log))
    as_json = _validate("--json", str(log))
    assert text.exit_code == as_json.exit_code == 1
    assert "  schema_violation (2)" in text.output.splitlines()
    assert json.loads(as_json.output)["counts"]["errors_by_code"] == {"schema_violation": 2}
    for output in (text.output, as_json.output):
        assert all(char == "\n" or ord(char) >= 0x20 for char in output)


def test_validate_never_prints_event_contents__defs_8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(GOLDEN)
    paths = ["valid/multi_robot.jsonl", "errors/schema_violation.jsonl", "flags/time_gap.jsonl"]
    for args in (paths, ["--json", *paths]):
        output = _validate(*args).output
        for value in ("op_17", "op_03", "tech_02", "operator_name", "operator wrote", "asleep"):
            assert value not in output


@pytest.mark.parametrize(
    "args",
    [[], ["no-such-file.jsonl"], ["--spec", "9.9", "README.md"], ["--spec", "../x", "README.md"]],
)
def test_validate_bad_usage_exits_2(args: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(GOLDEN.parents[1])
    assert _validate(*args).exit_code == 2


def test_validate_directory_is_bad_usage(tmp_path: Path) -> None:
    assert _validate(str(tmp_path)).exit_code == 2


def test_validate_accepts_an_explicit_spec_version() -> None:
    result = _validate("--spec", "v0.3", str(GOLDEN / "valid" / "multi_robot.jsonl"))
    assert result.exit_code == 0
    assert result.output.startswith("spec 0.3:")


def test_validate_read_failure_is_one_line_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(paths: object, version: str) -> None:
        raise reader.LogReadError("cannot read log.jsonl: Input/output error")

    monkeypatch.setattr(cli, "read_logs", failing)
    result = _validate(str(GOLDEN / "valid" / "multi_robot.jsonl"))
    assert result.exit_code == 1
    assert result.output.strip() == "error: cannot read log.jsonl: Input/output error"


def test_validate_without_shipped_spec_is_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "available_versions", list)
    result = _validate(str(GOLDEN / "valid" / "multi_robot.jsonl"))
    assert result.exit_code == 1
    assert result.output.strip() == "error: no specification is shipped with this package"


def _crash_validate(monkeypatch: pytest.MonkeyPatch, *options: str) -> None:
    def crash(paths: object, version: str) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(cli, "read_logs", crash)
    log = str(GOLDEN / "valid" / "multi_robot.jsonl")
    monkeypatch.setattr("sys.argv", ["autonometry", *options, "validate", log])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1


def test_crash_prints_only_one_line_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _crash_validate(monkeypatch)
    assert capsys.readouterr().err == "error: unexpected failure (RuntimeError)\n"


def test_crash_with_verbose_shows_the_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _crash_validate(monkeypatch, "-v")
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "RuntimeError: secret detail" in err
    assert err.endswith("error: unexpected failure (RuntimeError)\n")


def test_warnings_are_one_clean_line(capsys: pytest.CaptureFixture[str]) -> None:
    cli._configure_logging()
    log = logging.getLogger("autonometry.test")
    log.debug("hidden")
    try:
        raise ValueError("boom")
    except ValueError:
        log.warning("first line\nsecond line", exc_info=True)
    assert capsys.readouterr().err == "warning: first line\n"


def test_verbose_shows_debug_messages(capsys: pytest.CaptureFixture[str]) -> None:
    cli._configure_logging()
    cli._set_verbose(True)
    logging.getLogger("autonometry.test").debug("shown")
    assert capsys.readouterr().err == "DEBUG autonometry.test: shown\n"
