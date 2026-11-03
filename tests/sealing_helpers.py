"""Shared sealing helpers for M2 tests. Not collected by pytest (no ``test_`` prefix)."""

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonometry.events import Event, Session
from autonometry.sealfile import CheckpointLine, SealRecord
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
    """Group already-sealed events back into :class:`Session` objects, by ``seq`` order."""
    by_key: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for event in sealed_events:
        by_key.setdefault((event["robot_id"], event["session_id"]), []).append(event)
    return [
        Session(robot_id, session_id, tuple(sorted(events, key=lambda e: e["seq"])))  # type: ignore[arg-type]
        for (robot_id, session_id), events in by_key.items()
    ]


def checkpoint_records(notes: Sequence[str]) -> list[SealRecord]:
    return [CheckpointLine(note) for note in notes]
