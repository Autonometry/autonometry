"""Security review M10 (docs/security-review-2026-11.md): tamper attacks on sealed logs, run
through ``autonometry verify`` on real files (definitions.md 10.6). Every attack must leave the
result not verified, and the verifier must never crash: fail closed."""

import json
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from autonometry import cli
from autonometry.signednote import format_vkey
from report_helpers import ANCHORS, LOGS, VKEY, seal_log, sealed_golden_files

runner = CliRunner()
CHECKPOINTS = {name: name.removesuffix(".jsonl") + ".checkpoints.jsonl" for name in LOGS}


@pytest.fixture
def sealed(tmp_path: Path) -> Path:
    """The golden sealed report set (four sessions, checkpoints, an anchor file, the vkey)."""
    for name, data in sealed_golden_files().items():
        (tmp_path / name).write_bytes(data)
    return tmp_path


def _verify(folder: Path, *logs: str, anchor: bool = True) -> tuple[int, dict]:
    args = ["verify", *(str(folder / log) for log in logs), "--vkey", str(folder / VKEY)]
    if anchor:
        args += ["--anchor", str(folder / ANCHORS)]
    result = runner.invoke(cli.app, [*args, "--json"])
    assert "unexpected failure" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    return result.exit_code, json.loads(result.stdout) if result.stdout.strip() else {}


def _lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines(keepends=True)


def _statuses(report: dict) -> dict[str, str]:
    return {s["session_id"]: s["status"] for s in report["sessions"]}


def test_the_untouched_set_verifies(sealed: Path) -> None:
    code, report = _verify(sealed, *LOGS)
    assert code == 0
    assert report["verified"]


def test_a_duplicated_event_line_is_never_verified__defs_10_6(sealed: Path) -> None:
    lines = _lines(sealed / "s1.jsonl")
    (sealed / "s1.jsonl").write_bytes(b"".join([*lines[:3], lines[2], *lines[3:]]))
    code, report = _verify(sealed, *LOGS)
    assert code == 1
    assert report["counts"]["rejected_lines_by_code"] == {"duplicate_seq": 1}


def test_a_dropped_event_line_fails__defs_10_6(sealed: Path) -> None:
    lines = _lines(sealed / "s2.jsonl")
    (sealed / "s2.jsonl").write_bytes(b"".join(lines[:4] + lines[5:]))
    code, report = _verify(sealed, *LOGS)
    assert code == 1
    assert _statuses(report)["s2"] == "failed"


def test_reordered_lines_are_the_same_record_but_reordered_events_fail__defs_10_1(
    sealed: Path,
) -> None:
    """Event i is the i-th in seq order (10.1): moving a line changes nothing that is sealed;
    giving two events each other's seq breaks the chain."""
    lines = _lines(sealed / "s3.jsonl")
    (sealed / "s3.jsonl").write_bytes(b"".join(reversed(lines)))
    assert _verify(sealed, *LOGS)[0] == 0
    events = [json.loads(line) for line in lines]
    events[2]["seq"], events[3]["seq"] = events[3]["seq"], events[2]["seq"]
    (sealed / "s3.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    code, report = _verify(sealed, *LOGS)
    assert code == 1
    assert _statuses(report)["s3"] == "failed"


def test_an_event_spliced_into_another_session_fails__defs_10_6(sealed: Path) -> None:
    moved = json.loads(_lines(sealed / "s2.jsonl")[3])
    moved["session_id"] = "s1"
    moved["seq"] = len(_lines(sealed / "s1.jsonl"))
    with (sealed / "s1.jsonl").open("a") as stream:
        stream.write(json.dumps(moved) + "\n")
    code, report = _verify(sealed, *LOGS)
    assert code == 1
    assert _statuses(report)["s1"] == "failed"


def test_events_copied_into_a_second_file_are_rejected__defs_8(sealed: Path) -> None:
    (sealed / "s1.jsonl").write_bytes(
        (sealed / "s1.jsonl").read_bytes() + (sealed / "s2.jsonl").read_bytes()
    )
    code, report = _verify(sealed, *LOGS)
    assert code == 1
    assert report["counts"]["rejected_lines_by_code"]["duplicate_seq"] > 0


def test_swapped_checkpoint_files_fail__defs_10_6(sealed: Path) -> None:
    one, two = sealed / CHECKPOINTS["s1.jsonl"], sealed / CHECKPOINTS["s2.jsonl"]
    first = one.read_bytes()
    one.write_bytes(two.read_bytes())
    two.write_bytes(first)
    code, _ = _verify(sealed, "s1.jsonl", anchor=False)
    assert code == 1


def test_a_replayed_older_checkpoint_instead_of_the_last_is_not_l2__defs_10_6(
    sealed: Path,
) -> None:
    """Dropping the final checkpoint from both the log's file and the anchor file leaves an
    older seal: the events after it are only L1 (partial)."""
    path = sealed / CHECKPOINTS["s1.jsonl"]
    final = _lines(path)[-1]
    path.write_bytes(b"".join(_lines(path)[:-1]))
    anchors = sealed / ANCHORS
    anchors.write_bytes(b"".join(line for line in _lines(anchors) if line != final))
    code, report = _verify(sealed, *LOGS)
    assert code == 1
    assert _statuses(report)["s1"] == "partial"


def test_a_tail_cut_with_its_checkpoints_is_caught_by_the_anchor__defs_10_5(
    sealed: Path,
) -> None:
    log, path = sealed / "s4.jsonl", sealed / CHECKPOINTS["s4.jsonl"]
    log.write_bytes(b"".join(_lines(log)[:1]))
    path.write_bytes(b"".join(_lines(path)[:1]))
    code, report = _verify(sealed, *LOGS)
    assert code == 1
    assert _statuses(report)["s4"] == "failed"
    # Without a copy kept outside the log, the cut prefix is all a verifier can see: never L2
    # here, because no timestamp token anchors it either.
    code, report = _verify(sealed, *LOGS, anchor=False)
    assert code == 1
    assert _statuses(report)["s4"] == "unverified"


@pytest.mark.parametrize("target", ["log", "checkpoints"])
def test_a_file_cut_mid_line_is_never_verified__defs_10_6(sealed: Path, target: str) -> None:
    path = sealed / ("s2.jsonl" if target == "log" else CHECKPOINTS["s2.jsonl"])
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 7])
    code, report = _verify(sealed, *LOGS)
    assert code == 1
    assert not report["verified"]


def test_checkpoints_signed_by_another_key_only_are_untrusted__defs_10_6(sealed: Path) -> None:
    """A vkey with the trusted key's name but another public key: its key ID differs, so no
    signature is from a trusted key and nothing reaches L2."""
    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    attacker = sealed / "attacker.vkey"
    attacker.write_text(format_vkey("autonometry/golden-test-key", other) + "\n", "utf-8")
    result = runner.invoke(
        cli.app,
        ["verify", *(str(sealed / log) for log in LOGS), "--vkey", str(attacker), "--json"],
    )
    assert "unexpected failure" not in result.output
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert set(_statuses(report).values()) == {"unverified"}
    gaps = {g["code"] for s in report["sessions"] for g in s["gaps"]}
    assert "untrusted_checkpoint" in gaps


def test_a_checkpoint_size_of_5000_digits_is_malformed_not_a_crash__s01(sealed: Path) -> None:
    """S-01: a tree size beyond Python's 4300-digit int() limit crashed the verifier."""
    note = (
        "autonometry/r01/s1\n" + "9" * 5000 + "\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
        "\n— autonometry/golden-test-key AAAAAAAAAAAAAAAAAAAAAA==\n"
    )
    with (sealed / CHECKPOINTS["s1.jsonl"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "checkpoint", "note": note}) + "\n")
    code, report = _verify(sealed, *LOGS)
    assert code == 1
    assert report["counts"]["file_problems_by_code"] == {"malformed_checkpoint": 1}


def _nested_log(depth: int) -> bytes:
    start = {
        "robot_id": "r1", "session_id": "s1", "task_id": None, "seq": 0,
        "ts": "2026-01-01T00:00:00.000Z", "type": "session_start", "scopes": ["whole_robot"],
        "initial_states": {"whole_robot": "idle"}, "heartbeat_interval_s": 1.0,
        "hint_policy": "hint_as_event", "task_registry_hash": "0" * 64,
        "plan_hash": "0" * 64, "simulated": False,
    }  # fmt: skip
    beat = (
        '{"robot_id":"r1","session_id":"s1","task_id":null,"seq":1,'
        '"ts":"2026-01-01T00:00:01.000Z","type":"heartbeat","x_deep":'
        + "[" * depth
        + "]" * depth
        + "}"
    )
    return (json.dumps(start) + "\n" + beat + "\n").encode()


@pytest.mark.parametrize("depth", [128, 500, 990, 5000])
def test_nesting_deeper_than_the_limit_is_rejected_at_any_depth__s03(
    tmp_path: Path, depth: int
) -> None:
    """S-03: a line nested about 990 deep parsed, then crashed hashing with RecursionError;
    whether it parsed depended on the caller's stack. Now every line deeper than 128 is
    rejected the same way, by every command."""
    log = tmp_path / "deep.jsonl"
    log.write_bytes(_nested_log(depth))
    for command in (["validate", "--json"], ["verify", "--json"], ["timeline", "--json"]):
        result = runner.invoke(cli.app, [*command, str(log)])
        assert "unexpected failure" not in result.output, command
        assert "invalid_json" in result.stdout, command


def test_a_sealed_log_nested_to_the_limit_verifies__s03(tmp_path: Path) -> None:
    """The event object is the first level: an x_ value nested 127 deep reaches 128."""
    log, checkpoints = seal_log(_nested_log(127), "deep.jsonl")
    (tmp_path / "deep.jsonl").write_bytes(log)
    (tmp_path / "deep.checkpoints.jsonl").write_bytes(checkpoints)
    shutil.copyfile(tmp_path / "deep.checkpoints.jsonl", tmp_path / ANCHORS)
    for name, data in sealed_golden_files().items():
        if name == VKEY:
            (tmp_path / name).write_bytes(data)
    code, report = _verify(tmp_path, "deep.jsonl")
    assert code == 0, report
