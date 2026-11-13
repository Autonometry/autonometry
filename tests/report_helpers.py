"""Shared helpers for the report tests (definitions.md 11). Not collected by pytest.

Run as a script to rewrite ``tests/golden/report/sealed/`` from the metrics golden logs and
both expected reports (``unsealed.expected.json``, ``sealed.expected.json``):

    uv run python tests/report_helpers.py
"""

import io
import os
import tempfile
from pathlib import Path
from unittest import mock

import rfc8785
from typer.testing import CliRunner

from autonometry import cli, sealfile
from autonometry.reader import read_streams
from autonometry.signednote import format_vkey
from sealing_helpers import KEY_NAME, SEED_KEY, seal_sessions

GOLDEN = Path(__file__).parent / "golden"
METRICS_LOGS = GOLDEN / "metrics" / "logs"
REPORT = GOLDEN / "report"
SEALED = REPORT / "sealed"
LOGS = ("s1.jsonl", "s2.jsonl", "s3.jsonl", "s4.jsonl")
REGISTRIES = ("devices.json", "tasks.json", "plan.json")
VKEY = "golden.vkey"
ANCHORS = "anchors.jsonl"
#: The generated-at time every report test fixes (definitions.md 11.1).
GENERATED_AT = "2026-11-13T12:00:00Z"
#: Pseudonymous actor ids in the golden logs; a report never shows them (11).
ACTOR_IDS = ("op_a", "op_b", "op_c")
#: The two golden reports: folder the command runs in, and its arguments besides --out.
GOLDEN_RUNS: dict[str, tuple[Path, tuple[str, ...]]] = {
    "unsealed": (METRICS_LOGS, (*LOGS, "--n-boot", "200")),
    "sealed": (SEALED, (*LOGS, "--vkey", VKEY, "--anchor", ANCHORS, "--n-boot", "200")),
}


def vkey_line() -> str:
    """The vkey of the fixed-seed test key, as ``autonometry keys generate`` writes it."""
    public = SEED_KEY.public_key().public_bytes_raw()
    return format_vkey(KEY_NAME, public)


def seal_log(raw: bytes, name: str) -> tuple[bytes, bytes]:
    """A raw log sealed as ``autonometry seal`` does, with the fixed-seed key: the sealed log
    and its checkpoints file."""
    sessions = read_streams([(name, io.BytesIO(raw))], "0.3").sessions
    events, notes = seal_sessions(sessions)
    log = b"".join(rfc8785.dumps(dict(event)) + b"\n" for event in events)
    checkpoints = b"".join(sealfile.encode_checkpoint_line(note) for note in notes)
    return log, checkpoints


def sealed_golden_files() -> dict[str, bytes]:
    """Every file of ``tests/golden/report/sealed/`` by name: the metrics golden logs sealed
    with the fixed-seed key, their checkpoints, an anchor file holding every checkpoint
    (definitions.md 10.5 b), the vkey and copies of the registries."""
    files: dict[str, bytes] = {}
    anchors = b""
    for name in LOGS:
        log, checkpoints = seal_log((METRICS_LOGS / name).read_bytes(), name)
        files[name] = log
        files[name.removesuffix(".jsonl") + ".checkpoints.jsonl"] = checkpoints
        anchors += checkpoints
    files[ANCHORS] = anchors
    files[VKEY] = (vkey_line() + "\n").encode("utf-8")
    for name in REGISTRIES:
        files[name] = (METRICS_LOGS / name).read_bytes()
    return files


def run_report(folder: Path, args: tuple[str, ...], out: Path) -> tuple[int, str]:
    """``autonometry report`` run inside ``folder`` with the generated-at time fixed; returns
    the exit code and the output."""
    previous = Path.cwd()
    os.chdir(folder)
    try:
        with mock.patch.object(cli, "_utc_now", return_value=GENERATED_AT):
            result = CliRunner().invoke(cli.app, ["report", *args, "--out", str(out)])
    finally:
        os.chdir(previous)
    return result.exit_code, result.output


def write_golden() -> None:
    """Rewrite the sealed golden files, then both expected reports."""
    SEALED.mkdir(parents=True, exist_ok=True)
    for file_name, data in sealed_golden_files().items():
        (SEALED / file_name).write_bytes(data)
    for name, (folder, args) in GOLDEN_RUNS.items():
        with tempfile.TemporaryDirectory() as out:
            run_report(folder, args, Path(out))
            data = (Path(out) / "report.json").read_bytes()
        (REPORT / f"{name}.expected.json").write_bytes(data)


if __name__ == "__main__":
    write_golden()
