"""CLI tests for ``keys generate``, ``seal``, ``timestamp`` and ``verify``
(definitions.md v0.3 section 10)."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from autonometry import cli, tsa
from autonometry.sealfile import encode_timestamp_line
from autonometry.signednote import parse_vkey
from autonometry.spec import SpecError

runner = CliRunner()
GOLDEN_LOG = Path(__file__).parent / "golden" / "sealed" / "log.jsonl"


def test_keys_generate_creates_key_and_vkey(tmp_path: Path) -> None:
    out = tmp_path / "r1.key"
    result = runner.invoke(
        cli.app, ["keys", "generate", "--name", "autonometry/r1", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    vkey_path = out.with_name("r1.key.vkey")
    assert vkey_path.exists()
    vkey_line = vkey_path.read_text(encoding="utf-8").strip()
    assert vkey_line in result.output
    assert parse_vkey(vkey_line).name == "autonometry/r1"


def test_keys_generate_refuses_overwrite(tmp_path: Path) -> None:
    out = tmp_path / "r1.key"
    runner.invoke(cli.app, ["keys", "generate", "--name", "autonometry/r1", "--out", str(out)])
    result = runner.invoke(
        cli.app, ["keys", "generate", "--name", "autonometry/r1", "--out", str(out)]
    )
    assert result.exit_code == 1
    assert "overwrite" in result.output


def test_keys_generate_rejects_bad_name(tmp_path: Path) -> None:
    out = tmp_path / "r1.key"
    result = runner.invoke(cli.app, ["keys", "generate", "--name", "has space", "--out", str(out)])
    assert result.exit_code == 1
    assert not out.exists()


@pytest.fixture
def key(tmp_path: Path) -> Path:
    out = tmp_path / "r1.key"
    result = runner.invoke(
        cli.app, ["keys", "generate", "--name", "autonometry/r1", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    return out


@pytest.fixture
def golden_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "log.jsonl"
    shutil.copy(GOLDEN_LOG, dest)
    return dest


def test_seal_end_to_end(key: Path, golden_copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "sealed.jsonl"
    result = runner.invoke(
        cli.app, ["seal", str(golden_copy), "--key", str(key), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    checkpoints = tmp_path / "sealed.checkpoints.jsonl"
    assert checkpoints.exists()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 7
    for line in lines:
        event = json.loads(line)
        assert event["hash"]
        assert event["prev_hash"]

    vkey_file = tmp_path / "trusted.vkeys"
    vkey_file.write_text(key.with_name("r1.key.vkey").read_text(encoding="utf-8"), encoding="utf-8")

    verify_result = runner.invoke(
        cli.app, ["verify", str(out), "--vkey", str(vkey_file), "--json", "--require", "L1"]
    )
    assert verify_result.exit_code == 0, verify_result.output
    payload = json.loads(verify_result.output)
    assert {s["status"] for s in payload["sessions"]} == {"unverified"}  # sealed, not anchored

    verify_l2 = runner.invoke(cli.app, ["verify", str(out), "--vkey", str(vkey_file), "--json"])
    assert verify_l2.exit_code == 1  # default --require L2, nothing anchored yet

    anchored = runner.invoke(
        cli.app,
        [
            "verify",
            str(out),
            "--vkey",
            str(vkey_file),
            "--anchor",
            str(checkpoints),
            "--json",
        ],
    )
    assert anchored.exit_code == 0, anchored.output
    anchored_payload = json.loads(anchored.output)
    assert {s["status"] for s in anchored_payload["sessions"]} == {"verified"}


def test_seal_refuses_already_sealed_input(key: Path, golden_copy: Path, tmp_path: Path) -> None:
    sealed_once = tmp_path / "sealed.jsonl"
    runner.invoke(cli.app, ["seal", str(golden_copy), "--key", str(key), "--out", str(sealed_once)])
    result = runner.invoke(
        cli.app,
        ["seal", str(sealed_once), "--key", str(key), "--out", str(tmp_path / "again.jsonl")],
    )
    assert result.exit_code == 1
    assert "already" in result.output


def test_seal_refuses_rejected_lines(key: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    result = runner.invoke(
        cli.app, ["seal", str(bad), "--key", str(key), "--out", str(tmp_path / "out.jsonl")]
    )
    assert result.exit_code == 1
    assert "rejected" in result.output


def test_seal_refuses_existing_output(key: Path, golden_copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "sealed.jsonl"
    out.write_text("", encoding="utf-8")
    result = runner.invoke(
        cli.app, ["seal", str(golden_copy), "--key", str(key), "--out", str(out)]
    )
    assert result.exit_code == 1
    assert "overwrite" in result.output


def test_timestamp_appends_only_missing_records(
    key: Path, golden_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "sealed.jsonl"
    runner.invoke(cli.app, ["seal", str(golden_copy), "--key", str(key), "--out", str(out)])
    checkpoints = tmp_path / "sealed.checkpoints.jsonl"
    before = len(checkpoints.read_text(encoding="utf-8").splitlines())

    monkeypatch.setattr(cli.tsa, "request_timestamp", lambda note, url, roots: b"fake-tsr-bytes")
    root_pem = tmp_path / "root.pem"
    root_pem.write_text("not used, request_timestamp is mocked", encoding="utf-8")
    # request_timestamp is mocked, so load_tsa_roots must also not reject this placeholder file.
    monkeypatch.setattr(cli.tsa, "load_tsa_roots", lambda pem: [])

    result = runner.invoke(
        cli.app,
        [
            "timestamp",
            str(checkpoints),
            "--tsa-url",
            "https://example.invalid/tsr",
            "--tsa-root",
            str(root_pem),
        ],
    )
    assert result.exit_code == 0, result.output
    after_lines = checkpoints.read_text(encoding="utf-8").splitlines()
    assert len(after_lines) == before + 4  # one new timestamp record per checkpoint

    # Running again appends nothing further: every checkpoint already has a timestamp record.
    result2 = runner.invoke(
        cli.app,
        [
            "timestamp",
            str(checkpoints),
            "--tsa-url",
            "https://example.invalid/tsr",
            "--tsa-root",
            str(root_pem),
        ],
    )
    assert result2.exit_code == 0, result2.output
    assert checkpoints.read_text(encoding="utf-8").splitlines() == after_lines


def test_timestamp_reports_failures(
    key: Path, golden_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "sealed.jsonl"
    runner.invoke(cli.app, ["seal", str(golden_copy), "--key", str(key), "--out", str(out)])
    checkpoints = tmp_path / "sealed.checkpoints.jsonl"

    def failing(note: bytes, url: str, roots: object) -> bytes:
        raise tsa.TsaError("simulated failure")

    monkeypatch.setattr(cli.tsa, "request_timestamp", failing)
    monkeypatch.setattr(cli.tsa, "load_tsa_roots", lambda pem: [])
    root_pem = tmp_path / "root.pem"
    root_pem.write_text("unused", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "timestamp",
            str(checkpoints),
            "--tsa-url",
            "https://example.invalid/tsr",
            "--tsa-root",
            str(root_pem),
        ],
    )
    assert result.exit_code == 1
    assert "not timestamped" in result.output


def test_verify_bad_require_value_is_usage_error(golden_copy: Path) -> None:
    result = runner.invoke(cli.app, ["verify", str(golden_copy), "--require", "L9"])
    assert result.exit_code == 2


def test_verify_text_summary(key: Path, golden_copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "sealed.jsonl"
    runner.invoke(cli.app, ["seal", str(golden_copy), "--key", str(key), "--out", str(out)])
    checkpoints = tmp_path / "sealed.checkpoints.jsonl"
    vkey_file = tmp_path / "trusted.vkeys"
    vkey_file.write_text(key.with_name("r1.key.vkey").read_text(encoding="utf-8"), encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "verify",
            str(out),
            "--vkey",
            str(vkey_file),
            "--anchor",
            str(checkpoints),
            "--require",
            "L1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "r1/s1: verified" in result.output
    assert "r1/s2: verified" in result.output


def test_seal_fails_when_key_file_is_missing(tmp_path: Path, golden_copy: Path) -> None:
    result = runner.invoke(
        cli.app,
        [
            "seal",
            str(golden_copy),
            "--key",
            str(tmp_path / "missing.key"),
            "--out",
            str(tmp_path / "out.jsonl"),
        ],
    )
    assert result.exit_code == 1
    assert "cannot read" in result.output


def test_verify_fails_on_unreadable_vkey_file(golden_copy: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app, ["verify", str(golden_copy), "--vkey", str(tmp_path / "missing.vkeys")]
    )
    assert result.exit_code == 1
    assert "cannot read" in result.output


def test_verify_fails_on_unreadable_tsa_root_file(golden_copy: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app, ["verify", str(golden_copy), "--tsa-root", str(tmp_path / "missing.pem")]
    )
    assert result.exit_code == 1
    assert "cannot read" in result.output


def test_key_name_fails_when_vkey_has_no_lines(key: Path) -> None:
    key.with_name("r1.key.vkey").write_text("\n# only a comment\n", encoding="utf-8")
    result = runner.invoke(
        cli.app,
        [
            "seal",
            str(Path(__file__).parent / "golden" / "sealed" / "log.jsonl"),
            "--key",
            str(key),
            "--out",
            str(key.parent / "out.jsonl"),
        ],
    )
    assert result.exit_code == 1
    assert "no vkey line" in result.output


@pytest.fixture
def sealed(key: Path, golden_copy: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    """A sealed golden log, its checkpoint file and a vkey file trusting its key."""
    out = tmp_path / "sealed.jsonl"
    result = runner.invoke(
        cli.app, ["seal", str(golden_copy), "--key", str(key), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    vkeys = tmp_path / "trusted.vkeys"
    vkeys.write_text(key.with_name("r1.key.vkey").read_text(encoding="utf-8"), encoding="utf-8")
    return out, tmp_path / "sealed.checkpoints.jsonl", vkeys


def _verify(*args: str) -> Result:
    return runner.invoke(cli.app, ["verify", *args])


def test_verify_text_ends_with_result_verified(sealed: tuple[Path, Path, Path]) -> None:
    log, checkpoints, vkeys = sealed
    result = _verify(str(log), "--vkey", str(vkeys), "--anchor", str(checkpoints))
    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1] == "result: verified"


def test_verify_lists_rejected_lines_of_a_checkpoint_file(
    sealed: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """Review finding B4: a malformed checkpoint-file line made verify print every session as
    verified and exit 1 with no reason."""
    log, checkpoints, vkeys = sealed
    anchor = tmp_path / "anchor.jsonl"
    anchor.write_bytes(checkpoints.read_bytes())
    with checkpoints.open("ab") as handle:
        handle.write(b"this line is not json\n")
    result = _verify(str(log), "--vkey", str(vkeys), "--anchor", str(anchor))
    assert result.exit_code == 1
    lines = result.output.strip().splitlines()
    assert "rejected lines:" in lines
    assert "  invalid_json (1)" in lines
    assert any(line.startswith(f"    {checkpoints}:5: ") for line in lines)
    assert lines[-1] == "result: NOT verified: 1 rejected line(s)"


def test_verify_lists_file_problems(sealed: tuple[Path, Path, Path], tmp_path: Path) -> None:
    log, checkpoints, vkeys = sealed
    anchor = tmp_path / "anchor.jsonl"
    anchor.write_bytes(checkpoints.read_bytes())
    with checkpoints.open("ab") as handle:
        handle.write(encode_timestamp_line("a" * 64, b"x"))
    result = _verify(str(log), "--vkey", str(vkeys), "--anchor", str(anchor))
    assert result.exit_code == 1
    lines = result.output.strip().splitlines()
    assert "file problems:" in lines
    assert "  orphan_timestamp (1)" in lines
    assert lines[-1] == "result: NOT verified: 1 file problem(s)"


def test_verify_json_has_verified_rejected_lines_and_file_problems(
    sealed: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    log, checkpoints, vkeys = sealed
    anchor = tmp_path / "anchor.jsonl"
    anchor.write_bytes(checkpoints.read_bytes() + b"[1]\n")
    with checkpoints.open("ab") as handle:
        handle.write(encode_timestamp_line("a" * 64, b"x"))
    result = _verify(str(log), "--vkey", str(vkeys), "--anchor", str(anchor), "--json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["verified"] is False
    assert payload["require"] == "L2"
    assert [e["code"] for e in payload["rejected_lines"]] == ["not_an_object"]
    assert payload["rejected_lines"][0]["file"] == str(anchor)
    assert [p["code"] for p in payload["file_problems"]] == ["orphan_timestamp"]
    assert payload["reasons"] == ["1 rejected line(s)", "1 file problem(s)"]


def test_verify_reports_failed_sessions_in_the_result_line(
    sealed: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    log, checkpoints, vkeys = sealed
    anchor = tmp_path / "anchor.jsonl"
    anchor.write_bytes(checkpoints.read_bytes())
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")  # drop s2's last event
    result = _verify(str(log), "--vkey", str(vkeys), "--anchor", str(anchor), "--require", "L1")
    assert result.exit_code == 1
    assert result.output.strip().splitlines()[-1] == "result: NOT verified: r1/s2 failed"


def test_timestamp_refuses_a_checkpoint_file_with_rejected_lines(
    sealed: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, checkpoints, _ = sealed
    with checkpoints.open("ab") as handle:
        handle.write(b"not json\n")
    root = tmp_path / "root.pem"
    root.write_bytes((Path(__file__).parent / "fixtures" / "tsa" / "freetsa_root.pem").read_bytes())
    result = runner.invoke(
        cli.app,
        ["timestamp", str(checkpoints), "--tsa-url", "https://example.invalid/tsr",
         "--tsa-root", str(root)],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "1 rejected line(s); refusing" in result.output


def test_verify_refuses_a_spec_with_different_sealing_codes(
    sealed: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    log, _, _ = sealed

    def refuse(*_args: object) -> None:
        raise SpecError("spec 0.3: its [sealing] gap_codes differ")

    monkeypatch.setattr("autonometry.verify.check_sealing_codes", refuse)
    result = _verify(str(log))
    assert result.exit_code == 1
    assert "gap_codes differ" in result.output
