"""Tests for autonometry.sealing (definitions.md v0.3 section 8 hash/prev_hash; section 10)."""

import hashlib

import rfc8785

from autonometry.merkle import leaf_hash, root_of
from autonometry.sealing import Sealer, event_hash, genesis_hash, seal_event

_PREFIX = "autonometry/chain-v1"


def _event(seq: int, **extra: object) -> dict[str, object]:
    return {
        "robot_id": "r1",
        "session_id": "s1",
        "task_id": None,
        "seq": seq,
        "ts": f"2026-10-30T09:00:{seq:02d}.000Z",
        "type": "heartbeat",
        **extra,
    }


def test_genesis_hash_matches_definitions_md_formula__defs_8() -> None:
    expected = hashlib.sha256(f"{_PREFIX}|r1|s1".encode()).hexdigest()
    assert genesis_hash(_PREFIX, "r1", "s1") == expected


def test_event_hash_is_rfc8785_sha256_without_hash_field__defs_8() -> None:
    event = _event(0, prev_hash="abc")
    expected = hashlib.sha256(rfc8785.dumps(event)).hexdigest()
    assert event_hash(event) == expected
    # Adding a stale "hash" field must not change the recomputed hash.
    assert event_hash({**event, "hash": "stale"}) == expected


def test_seal_event_sets_prev_hash_and_hash_and_does_not_mutate_input() -> None:
    event = _event(0)
    sealed = seal_event(event, "genesis")
    assert event.get("hash") is None
    assert event.get("prev_hash") is None
    assert sealed["prev_hash"] == "genesis"
    assert sealed["hash"] == event_hash(sealed)


def test_seal_event_discards_stale_hash_fields() -> None:
    event = _event(0, hash="stale-hash", prev_hash="stale-prev")
    sealed = seal_event(event, "genesis")
    assert sealed["prev_hash"] == "genesis"
    assert sealed["hash"] == event_hash({**event, "prev_hash": "genesis"})


def test_sealer_chains_events_and_sets_sequential_hashes() -> None:
    sealer = Sealer(_PREFIX, "r1", "s1", checkpoint_every_events=1000, checkpoint_every_s=600)
    sealed_events = []
    prev = genesis_hash(_PREFIX, "r1", "s1")
    for seq in range(5):
        sealed, _ = sealer.add(_event(seq))
        assert sealed["prev_hash"] == prev
        assert sealed["hash"] == event_hash(sealed)
        prev = sealed["hash"]
        sealed_events.append(sealed)
    assert sealer.size == 5


def test_checkpoint_after_first_event__defs_10_3() -> None:
    sealer = Sealer(_PREFIX, "r1", "s1", checkpoint_every_events=1000, checkpoint_every_s=600)
    _, checkpoint = sealer.add(_event(0))
    assert checkpoint is not None
    assert checkpoint.size == 1


def test_checkpoint_every_n_events__defs_10_3() -> None:
    sealer = Sealer(_PREFIX, "r1", "s1", checkpoint_every_events=3, checkpoint_every_s=10_000)
    checkpoints = []
    for seq in range(7):
        _, checkpoint = sealer.add(_event(seq))
        if checkpoint is not None:
            checkpoints.append(checkpoint.size)
    assert checkpoints == [1, 4, 7]


def test_checkpoint_after_time_elapsed__defs_10_3() -> None:
    sealer = Sealer(_PREFIX, "r1", "s1", checkpoint_every_events=1000, checkpoint_every_s=5)
    _, first = sealer.add(_event(0))
    assert first is not None
    _, second = sealer.add({**_event(1), "ts": "2026-10-30T09:00:02.000Z"})
    assert second is None
    _, third = sealer.add({**_event(2), "ts": "2026-10-30T09:00:06.000Z"})
    assert third is not None
    assert third.size == 3


def test_finish_checkpoints_the_last_event_once__defs_10_3() -> None:
    sealer = Sealer(_PREFIX, "r1", "s1", checkpoint_every_events=1000, checkpoint_every_s=600)
    sealer.add(_event(0))
    sealer.add(_event(1))
    final = sealer.finish()
    assert final is not None
    assert final.size == 2


def test_finish_is_none_when_last_event_already_checkpointed__defs_10_3() -> None:
    sealer = Sealer(_PREFIX, "r1", "s1", checkpoint_every_events=1, checkpoint_every_s=600)
    sealer.add(_event(0))
    assert sealer.finish() is None


def test_finish_with_no_events_is_none() -> None:
    sealer = Sealer(_PREFIX, "r1", "s1", checkpoint_every_events=1000, checkpoint_every_s=600)
    assert sealer.finish() is None


def test_sealer_root_matches_merkle_root_of_recomputed_hashes__defs_10_2() -> None:
    sealer = Sealer(_PREFIX, "r1", "s1", checkpoint_every_events=1000, checkpoint_every_s=600)
    hashes = []
    checkpoint = None
    for seq in range(4):
        sealed, cp = sealer.add(_event(seq))
        hashes.append(sealed["hash"])
        checkpoint = cp or checkpoint
    final = sealer.finish()
    assert final is not None
    expected_root = root_of(leaf_hash(bytes.fromhex(h)) for h in hashes)
    assert final.root == expected_root


def test_sealer_is_rebuildable_by_replaying_sealed_events__defs_10_3() -> None:
    """A crashed Sealer rebuilds its state by replaying the events already sealed on disk."""
    sealer = Sealer(_PREFIX, "r1", "s1", checkpoint_every_events=2, checkpoint_every_s=600)
    sealed_so_far = []
    original_checkpoints = []
    for seq in range(5):
        sealed, checkpoint = sealer.add(_event(seq))
        sealed_so_far.append(sealed)
        if checkpoint is not None:
            original_checkpoints.append(checkpoint)
    final = sealer.finish()
    if final is not None:
        original_checkpoints.append(final)

    rebuilt = Sealer(_PREFIX, "r1", "s1", checkpoint_every_events=2, checkpoint_every_s=600)
    rebuilt_checkpoints = []
    for sealed in sealed_so_far:
        # Replay from the already-sealed event, as read back from disk (hash/prev_hash kept).
        _, checkpoint = rebuilt.add(sealed)
        if checkpoint is not None:
            rebuilt_checkpoints.append(checkpoint)
    rebuilt_final = rebuilt.finish()
    if rebuilt_final is not None:
        rebuilt_checkpoints.append(rebuilt_final)

    assert rebuilt.size == sealer.size
    assert len(original_checkpoints) >= 2
    assert rebuilt_checkpoints == original_checkpoints
