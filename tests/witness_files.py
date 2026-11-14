"""Sealed robot and station logs for the witness CLI tests (definitions.md 12). Not collected.

Run as a script to rewrite ``tests/golden/witness/`` and its ``expected.json``:

    uv run python tests/witness_files.py

Two keys sign: the robot's, the fixed-seed key of ``sealing_helpers``, and the station's, derived
from its own seed (never a committed private key). Robot ``r01`` is the honest pair: a takeover
the station saw. Robot ``r02`` is research scenario S2: the robot logs ``ai`` for the whole
session while the station shows 30 s of live input; its station also reports an implausibly
small clock error without PTP.
"""

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from autonometry import cli, sealfile
from autonometry.events import Event, Session
from autonometry.signednote import format_vkey
from sealing_helpers import KEY_NAME, SEED_KEY, seal_sessions
from timeline_helpers import DEVICES, PLAN, TASKS
from witness_helpers import events

GOLDEN = Path(__file__).parent / "golden" / "witness"
STATION_KEY_NAME = "autonometry/golden-station-key"
STATION_KEY = Ed25519PrivateKey.from_private_bytes(
    hashlib.sha256(b"autonometry-golden-station-seed").digest()
)
VKEYS = "keys.vkey"
ANCHORS = "anchors.jsonl"
#: The arguments of the golden run, inside ``tests/golden/witness``.
ARGS = (
    "reconcile",
    "--robot",
    "robot/r01.jsonl",
    "--robot",
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


def scenario() -> dict[str, list[Event]]:
    """The raw events of every log, by its path relative to the golden folder."""
    return {
        "robot/r01.jsonl": events(
            [(0, "ai"), (100, "human_remote"), (130, "ai")], 300, robot_id="r01"
        ),
        "station/r01.jsonl": events(
            [(100.1, "human_remote"), (130.05, "idle")],
            301,
            start=-5,
            robot_id="r01",
            role="station",
            session_id="st01",
            station_id="pc_1",
        ),
        "robot/r02.jsonl": events([(0, "ai")], 300, robot_id="r02"),
        "station/r02.jsonl": events(
            [(100, "human_remote"), (130, "idle")],
            301,
            start=-5,
            robot_id="r02",
            role="station",
            session_id="st01",
            station_id="pc_2",
            clock_error=0.0001,
        ),
    }


def seal(found: Sequence[Event], key: Ed25519PrivateKey, key_name: str) -> tuple[bytes, bytes]:
    """One session's events sealed as ``autonometry seal`` does: the log and its checkpoints."""
    session = Session(found[0]["robot_id"], found[0]["session_id"], tuple(found))
    sealed, notes = seal_sessions([session], key, key_name)
    log = b"".join(rfc8785.dumps(dict(event)) + b"\n" for event in sealed)
    return log, b"".join(sealfile.encode_checkpoint_line(note) for note in notes)


def golden_files() -> dict[str, bytes]:
    """Every input file of the golden folder by its relative path."""
    files: dict[str, bytes] = {}
    anchors = b""
    registries = {"devices.json": DEVICES, "tasks.json": TASKS, "plan.json": PLAN}
    for path, found in scenario().items():
        station = path.startswith("station/")
        key, name = (STATION_KEY, STATION_KEY_NAME) if station else (SEED_KEY, KEY_NAME)
        log, checkpoints = seal(found, key, name)
        files[path] = log
        files[path.removesuffix(".jsonl") + ".checkpoints.jsonl"] = checkpoints
        anchors += checkpoints
        folder = path.split("/")[0]
        for registry, document in registries.items():
            files[f"{folder}/{registry}"] = json.dumps(document, indent=2).encode("utf-8") + b"\n"
    files[ANCHORS] = anchors
    vkeys = [
        format_vkey(KEY_NAME, SEED_KEY.public_key().public_bytes_raw()),
        format_vkey(STATION_KEY_NAME, STATION_KEY.public_key().public_bytes_raw()),
    ]
    files[VKEYS] = ("\n".join(vkeys) + "\n").encode("utf-8")
    return files


def write(folder: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def run_in(folder: Path, *args: str) -> tuple[int, str]:
    """``autonometry`` run inside ``folder``; returns the exit code and the output."""
    previous = Path.cwd()
    os.chdir(folder)
    try:
        result = CliRunner().invoke(cli.app, list(args))
    finally:
        os.chdir(previous)
    return result.exit_code, result.output


def main() -> None:
    write(GOLDEN, golden_files())
    _, output = run_in(GOLDEN, *ARGS)
    (GOLDEN / "expected.json").write_bytes(output.encode("utf-8"))
    parsed: Any = json.loads(output)
    print(f"wrote {GOLDEN}: {parsed['counts']}")


if __name__ == "__main__":
    main()
