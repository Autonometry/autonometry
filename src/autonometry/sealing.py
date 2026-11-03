"""Hash-chain sealing of event logs (definitions.md v0.2 section 8 ``hash``/``prev_hash``;
section 10, sealing and verification).

Pure functions plus an incremental, crash-rebuildable builder: no file or network I/O happens
here, only in the CLI (``autonometry seal``) and, later, the M3 recorder.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import rfc8785

from autonometry.events import Event
from autonometry.merkle import IncrementalTree, leaf_hash
from autonometry.timestamps import seconds_to_ns, ts_to_ns


def genesis_hash(chain_genesis_prefix: str, robot_id: str, session_id: str) -> str:
    """Return a session chain's genesis ``prev_hash`` (definitions.md section 8): the lower-case
    hex SHA-256 of ``"<chain_genesis_prefix>|<robot_id>|<session_id>"``."""
    text = f"{chain_genesis_prefix}|{robot_id}|{session_id}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def event_hash(event_with_prev_hash: Mapping[str, Any]) -> str:
    """Return the lower-case hex SHA-256 ``hash`` of an event that already carries ``prev_hash``
    (definitions.md section 8): RFC 8785 canonical JSON of the event without its own ``hash``."""
    body = {k: v for k, v in event_with_prev_hash.items() if k != "hash"}
    return hashlib.sha256(rfc8785.dumps(body)).hexdigest()


def seal_event(event: Mapping[str, Any], prev_hash: str) -> dict[str, Any]:
    """Return a copy of ``event`` with ``prev_hash`` and ``hash`` set (definitions.md section 8).

    Any ``hash``/``prev_hash`` already on ``event`` are discarded first, so sealing an unsealed
    event is idempotent in the fields that matter.
    """
    sealed = {k: v for k, v in event.items() if k not in ("hash", "prev_hash")}
    sealed["prev_hash"] = prev_hash
    sealed["hash"] = event_hash(sealed)
    return sealed


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A checkpoint due by the policy of definitions.md section 10.3: tree size, Merkle root
    (32 raw bytes) and the last event it covers."""

    size: int
    root: bytes
    last_event: Event


class Sealer:
    """Seals one session's events in ``seq`` order and decides when to checkpoint
    (definitions.md 10.1, 10.2, 10.3).

    Keeps O(log n) state: the chain tip and the Merkle tree's right-edge frontier
    (:class:`~autonometry.merkle.IncrementalTree`). Rebuildable after a crash: replaying every
    already-sealed event of a session through :meth:`add`, in order, reaches exactly the state a
    Sealer that had never stopped would be in, because every step recomputes its hash instead of
    trusting the stored one.
    """

    def __init__(
        self,
        chain_genesis_prefix: str,
        robot_id: str,
        session_id: str,
        checkpoint_every_events: int,
        checkpoint_every_s: float,
    ) -> None:
        self._prev_hash = genesis_hash(chain_genesis_prefix, robot_id, session_id)
        self._tree = IncrementalTree()
        self._checkpoint_every_events = checkpoint_every_events
        self._checkpoint_every_s_ns = seconds_to_ns(checkpoint_every_s)
        self._last_checkpoint_size = 0
        self._last_checkpoint_ts_ns: int | None = None
        self._last_event: Event | None = None

    @property
    def size(self) -> int:
        """Number of events sealed so far."""
        return self._tree.size

    def add(self, event: Mapping[str, Any]) -> tuple[dict[str, Any], Checkpoint | None]:
        """Seal one more event; return it and a checkpoint when the policy of 10.3 fires."""
        sealed = seal_event(event, self._prev_hash)
        self._prev_hash = sealed["hash"]
        self._tree.append(leaf_hash(bytes.fromhex(sealed["hash"])))
        self._last_event = cast(Event, sealed)
        checkpoint = self._checkpoint() if self._due(sealed) else None
        return sealed, checkpoint

    def _due(self, sealed: Mapping[str, Any]) -> bool:
        size = self._tree.size
        if size == 1 or size - self._last_checkpoint_size >= self._checkpoint_every_events:
            return True
        if self._last_checkpoint_ts_ns is None:
            return False
        return ts_to_ns(sealed["ts"]) - self._last_checkpoint_ts_ns >= self._checkpoint_every_s_ns

    def _checkpoint(self) -> Checkpoint:
        last_event = cast(Event, self._last_event)  # callers only reach here after add()
        self._last_checkpoint_size = self._tree.size
        self._last_checkpoint_ts_ns = ts_to_ns(last_event["ts"])
        return Checkpoint(self._tree.size, self._tree.root(), last_event)

    def finish(self) -> Checkpoint | None:
        """Return the final checkpoint after the session's last event (definitions.md 10.3),
        unless one already covers it, or no event was ever added."""
        if self._last_event is None or self._last_checkpoint_size == self._tree.size:
            return None
        return self._checkpoint()
