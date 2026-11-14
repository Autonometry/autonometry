"""Security review M10 (docs/security-review-2026-11.md): parsers and the command line under
hostile input. Whatever a log, config or file name holds, every command ends with a result or a
one-line error, never a crash, and never prints a control character."""

import json
import sys
import time
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from autonometry import cli, witness
from autonometry.display import printable
from autonometry.recorder.config import ConfigError, parse_config
from autonometry.timestamps import MAX_DURATION_NS, seconds_to_ns

runner = CliRunner()

_START = {
    "robot_id": "r1", "session_id": "s1", "task_id": None, "seq": 0,
    "ts": "2026-01-01T00:00:00.000Z", "type": "session_start", "scopes": ["whole_robot"],
    "initial_states": {"whole_robot": "ai"}, "heartbeat_interval_s": 1.0,
    "hint_policy": "hint_as_event", "task_registry_hash": "0" * 64, "plan_hash": "0" * 64,
    "simulated": False,
}  # fmt: skip


def _log(**overrides: object) -> list[dict[str, object]]:
    common = {"robot_id": "r1", "session_id": "s1", "task_id": None}
    events: list[dict[str, object]] = [
        dict(_START),
        {**common, "seq": 1, "ts": "2026-01-01T00:00:01.000Z", "type": "heartbeat",
         "clock_source": "ntp", "clock_max_error_s": 0.01, "distance_m": 1.0},
        {**common, "seq": 2, "ts": "2026-01-01T00:00:02.000Z", "type": "approval",
         "scope": "whole_robot", "actor_id": "op", "reason": "ai_requested_help", "dwell_s": 0.5},
        {**common, "seq": 3, "ts": "2026-01-01T00:00:03.000Z", "type": "hint",
         "scope": "whole_robot", "actor_id": "op", "ttl_s": 5.0, "dwell_s": 0.5},
        {**common, "seq": 4, "ts": "2026-01-01T00:00:04.000Z", "type": "session_end"},
    ]  # fmt: skip
    for key, value in overrides.items():
        index, field = key.split("__")
        events[int(index)][field] = value
    return events


_EXTREMES = {
    "heartbeat_interval": {"0__heartbeat_interval_s": 1e308},
    "tiny_interval": {"0__heartbeat_interval_s": 5e-324},
    "clock_error": {"1__clock_max_error_s": 1e308},
    "distance": {"1__distance_m": 1.7976931348623157e308},
    "approval_dwell": {"2__dwell_s": 1e308},
    "hint_dwell": {"3__dwell_s": 1e308},
    "hint_ttl": {"3__ttl_s": 1e308},
    "scope_weight": {"0__scope_weights": {"whole_robot": 1e308}},
}


@pytest.mark.parametrize("case", sorted(_EXTREMES))
def test_extreme_schema_valid_numbers_never_crash_a_command__s02(tmp_path: Path, case: str) -> None:
    """S-02: a schema-valid heartbeat_interval_s or dwell_s of 1e308 overflowed converting to
    nanoseconds (OverflowError) in validate, timeline, metrics and report."""
    log = tmp_path / "log.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in _log(**_EXTREMES[case])))
    for command in (
        ["validate"],
        ["timeline", "--json"],
        ["metrics", "--n-boot", "20"],
        ["report", "--n-boot", "20", "--out", str(tmp_path / "report")],
    ):
        result = runner.invoke(cli.app, [*command, str(log)])
        assert "unexpected failure" not in result.output, command
        assert result.exception is None or isinstance(result.exception, SystemExit), command


def test_extreme_clock_errors_never_crash_reconcile__s02(tmp_path: Path) -> None:
    robot, station = tmp_path / "robot.jsonl", tmp_path / "station.jsonl"
    robot.write_text("".join(json.dumps(e) + "\n" for e in _log(**{"1__clock_max_error_s": 1e308})))
    station_events = _log(**{"1__clock_max_error_s": 1e308})
    station_events[0] |= {"role": "station", "station_id": "st1", "session_id": "s2"}
    for event in station_events:
        event["session_id"] = "s2"
    station.write_text("".join(json.dumps(e) + "\n" for e in station_events))
    result = runner.invoke(cli.app, ["reconcile", str(robot), "--station", str(station)])
    assert "unexpected failure" not in result.output


@given(st.floats(allow_nan=False))
def test_seconds_to_ns_saturates_instead_of_overflowing__s02(seconds: float) -> None:
    ns = seconds_to_ns(seconds)
    assert -MAX_DURATION_NS <= ns <= MAX_DURATION_NS
    if abs(seconds) < 1e9:
        assert ns == round(seconds * 1_000_000_000)


def test_a_deeply_nested_config_is_a_config_error__s05() -> None:
    """S-05: tomllib recurses; a config of nested arrays crashed ``autonometry record``."""
    with pytest.raises(ConfigError, match="nested too deeply"):
        parse_config("a = " + "[" * 5000 + "]" * 5000, Path("."))


def test_printable_escapes_control_format_and_separator_characters__s09() -> None:
    assert printable("ok\tline\n") == "ok\tline\n"
    assert printable("\x1b]0;owned\x07red") == "\\x1b]0;owned\\x07red"
    assert printable("a\x9bb\x7f") == "a\\x9bb\\x7f"
    bidi = "evil" + chr(0x202E) + "txt.jsonl" + chr(0x2028)
    assert printable(bidi) == r"evil\u202etxt.jsonl\u2028"
    assert printable("café — résumé") == "café — résumé"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot hold ESC")
def test_a_file_name_with_an_escape_sequence_prints_escaped__s09(tmp_path: Path) -> None:
    """S-09: a log named by whoever wrote it reached the terminal raw in validate's output."""
    log = tmp_path / "\x1b[2J\x1b]0;owned\x07.jsonl"
    log.write_text("not json\n")
    result = runner.invoke(cli.app, ["validate", str(log)])
    assert "\x1b" not in result.output
    assert "\\x1b[2J" in result.output


def test_a_dataset_key_with_control_characters_prints_on_one_line__s52(tmp_path: Path) -> None:
    """S-52: a feature name from a downloaded dataset's info.json reached the error message."""
    meta = tmp_path / "dataset" / "meta"
    meta.mkdir(parents=True)
    hostile = "\x1b[2K\r\x1b[32mwrote 18 session log(s)\n" + "X" * 30
    (meta / "info.json").write_text(
        json.dumps({"codebase_version": "v3.0", "fps": 30, "features": {hostile: 5}})
    )
    registry = Path(__file__).parent.parent / "connectors" / "lerobot"
    args = ["lerobot", "import", str(tmp_path / "dataset"), "--out", str(tmp_path / "o")]
    result = runner.invoke(cli.app, [*args, "--registry", str(registry)])
    assert "\x1b" not in result.output
    assert "\r" not in result.output
    errors = [line for line in result.output.splitlines() if line.startswith("error")]
    assert len(errors) == 1


def _reference_overlaps(interval: tuple[int, int], others: list[tuple[int, int]]) -> bool:
    return bool(witness._intersect([interval], others))


@given(
    st.lists(st.integers(0, 200), max_size=30, unique=True),
    st.integers(-5, 205),
    st.integers(-5, 205),
)
def test_overlap_by_bisection_matches_the_linear_definition__s10(
    points: list[int], start: int, end: int
) -> None:
    ordered = sorted(points)
    others = list(zip(ordered[::2], ordered[1::2], strict=False))
    assert witness._overlaps((start, end), others) == _reference_overlaps((start, end), others)


def test_matching_many_runs_against_many_episodes_is_not_quadratic__s10() -> None:
    """S-10: 8000 robot runs against 8000 station episodes took 40 s (O(n^2)); a crafted
    station log of short episodes could stall reconcile and report."""
    n = 50_000
    human = [(10 * i, 10 * i + 3) for i in range(n)]
    runs = [(10 * i + 5, 10 * i + 7) for i in reversed(range(n))]
    began = time.perf_counter()
    assert not any(witness._overlaps(run, human) for run in runs)
    assert time.perf_counter() - began < 10.0
