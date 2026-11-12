"""Shared sealing helpers for M2 tests. Not collected by pytest (no ``test_`` prefix)."""

import hashlib
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonometry.events import Counts, Event, LineError, LogCheck, Session
from autonometry.sealfile import CheckpointLine, SealFile, SealRecord, TimestampLine
from autonometry.sealing import Sealer
from autonometry.signednote import checkpoint_body, sign_note

CHAIN_GENESIS_PREFIX = "autonometry/chain-v1"
KEY_NAME = "autonometry/golden-test-key"

#: Derived from a fixed seed, never a committed private key (CLAUDE.md security rules).
SEED_KEY = Ed25519PrivateKey.from_private_bytes(
    hashlib.sha256(b"autonometry-golden-test-seed").digest()
)


def seal_sessions(
    sessions: Sequence[Session],
    key: Ed25519PrivateKey = SEED_KEY,
    key_name: str = KEY_NAME,
    checkpoint_every_events: int = 1000,
    checkpoint_every_s: float = 600.0,
) -> tuple[list[Event], list[str]]:
    """Seal every session's events in order; return (sealed events, signed checkpoint notes)."""
    sealed_events: list[Event] = []
    checkpoint_notes: list[str] = []
    for session in sessions:
        sealer = Sealer(
            CHAIN_GENESIS_PREFIX,
            session.robot_id,
            session.session_id,
            checkpoint_every_events,
            checkpoint_every_s,
        )
        for event in session.events:
            sealed, checkpoint = sealer.add(event)
            sealed_events.append(sealed)  # type: ignore[arg-type]
            if checkpoint is not None:
                note_text = checkpoint_body(
                    session.robot_id, session.session_id, checkpoint.size, checkpoint.root
                )
                checkpoint_notes.append(sign_note(note_text, key_name, key))
        final = sealer.finish()
        if final is not None:
            note_text = checkpoint_body(
                session.robot_id, session.session_id, final.size, final.root
            )
            checkpoint_notes.append(sign_note(note_text, key_name, key))
    return sealed_events, checkpoint_notes


def resealed_sessions(sealed_events: Sequence[Event]) -> list[Session]:
    """Group events back into :class:`Session` objects, sorted by key and by ``seq``."""
    by_key: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for event in sealed_events:
        by_key.setdefault((event["robot_id"], event["session_id"]), []).append(event)
    return [
        Session(robot_id, session_id, tuple(sorted(events, key=lambda e: e["seq"])))  # type: ignore[arg-type]
        for (robot_id, session_id), events in sorted(by_key.items())
    ]


def log_check_of(events: Sequence[Event], errors: Sequence[LineError] = ()) -> LogCheck:
    """A LogCheck holding ``events`` grouped into sessions, as the reader would return it."""
    sessions = tuple(resealed_sessions(events))
    by_code: dict[str, int] = {}
    for error in errors:
        by_code[error.code] = by_code.get(error.code, 0) + 1
    counts = Counts(
        files=1,
        lines=len(events) + len(errors),
        blank_lines=0,
        events=len(events),
        rejected_lines=len(errors),
        sessions=len(sessions),
        errors_by_code=MappingProxyType(by_code),
        flags_by_code=MappingProxyType({}),
    )
    return LogCheck("0.2", sessions, tuple(errors), (), counts)


def seal_file(
    name: str,
    notes: Sequence[str] = (),
    stamps: Sequence[tuple[str, bytes]] = (),
    errors: Sequence[LineError] = (),
) -> SealFile:
    """A checkpoint or anchor file holding ``notes`` then ``(note_sha256, tsr)`` stamps."""
    records: list[SealRecord] = [CheckpointLine(i, note) for i, note in enumerate(notes, 1)]
    records += [TimestampLine(len(notes) + i, sha, tsr) for i, (sha, tsr) in enumerate(stamps, 1)]
    by_code: dict[str, int] = {}
    for error in errors:
        by_code[error.code] = by_code.get(error.code, 0) + 1
    return SealFile(name, tuple(records), tuple(errors), MappingProxyType(by_code))


def note_sha256(note: str) -> str:
    return hashlib.sha256(note.encode("utf-8")).hexdigest()
