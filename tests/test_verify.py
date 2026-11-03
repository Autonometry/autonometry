"""Tests for autonometry.verify (definitions.md v0.2 section 10.6) against the golden sealed log
and the attacks of research/demo-prototypes/seal.py.
"""

import copy
import hashlib
from collections.abc import Sequence
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from autonometry.events import Event, LogCheck, Session, SessionVerdict
from autonometry.reader import read_logs
from autonometry.sealfile import CheckpointLine, SealRecord, TimestampLine
from autonometry.signednote import format_vkey, parse_vkey
from autonometry.spec import load_rules
from autonometry.tsa import load_tsa_roots
from autonometry.verify import verify_log
from sealing_helpers import KEY_NAME, SEED_KEY, checkpoint_records, resealed_sessions, seal_sessions

GOLDEN_LOG = Path(__file__).parent / "golden" / "sealed" / "log.jsonl"
TSA_FIXTURES = Path(__file__).parent / "fixtures" / "tsa"
RULES = load_rules("0.2")
TRUSTED_KEYS = [parse_vkey(format_vkey(KEY_NAME, SEED_KEY.public_key().public_bytes_raw()))]
GOLDEN = read_logs([GOLDEN_LOG], "0.2")


def _seal_golden() -> tuple[list[Event], list[str]]:
    return seal_sessions(GOLDEN.sessions)


def _verify(
    sealed_events: list[Event],
    notes: list[str],
    anchors: list[SealRecord] | None = None,
    roots: Sequence[x509.Certificate] = (),
    checkpoints: list[SealRecord] | None = None,
) -> dict[tuple[str, str], SessionVerdict]:
    log_check = LogCheck("0.2", tuple(resealed_sessions(sealed_events)), (), (), GOLDEN.counts)
    records = checkpoints if checkpoints is not None else checkpoint_records(notes)
    result = verify_log(log_check, records, anchors or [], TRUSTED_KEYS, roots, RULES)
    return {(v.robot_id, v.session_id): v for v in result.sessions}


def test_fully_sealed_without_any_anchor_is_unverified__defs_10_6() -> None:
    """'No anchors': a valid, trusted, root-matching checkpoint with no anchor is a gap, not a
    failure (definitions.md 10.5, 10.6 gap code not_anchored)."""
    sealed_events, notes = _seal_golden()
    by_key = _verify(sealed_events, notes)
    for verdict in by_key.values():
        assert verdict.status == "unverified"
        assert verdict.evidence == ()
        assert any(g.code == "not_anchored" for g in verdict.gaps)


def test_fully_sealed_with_anchor_file_is_verified__defs_10_6() -> None:
    """'Intact': every checkpoint anchored by a copy held outside the log (definitions.md
    10.5(b))."""
    sealed_events, notes = _seal_golden()
    by_key = _verify(sealed_events, notes, anchors=checkpoint_records(notes))
    for (robot_id, session_id), verdict in by_key.items():
        assert verdict.status == "verified", verdict
        assert verdict.evidence == ()
        last_seq = max(
            e["seq"]
            for e in sealed_events
            if (e["robot_id"], e["session_id"]) == (robot_id, session_id)
        )
        assert verdict.sealed_through_seq == last_seq


def test_fully_sealed_with_real_tsa_timestamp_is_verified__defs_10_5() -> None:
    """A real RFC 3161 token, recorded once from FreeTSA, anchors session s2 (definitions.md
    10.5(a))."""
    sealed_events, notes = _seal_golden()
    s2_note = next(n for n in notes if n.startswith("autonometry/r1/s2\n3\n"))
    note_sha256 = hashlib.sha256(s2_note.encode("utf-8")).hexdigest()
    assert note_sha256 == "10a64865168cb368b725f406a6ec1985ab1971f969afc3262927e6177996a372", (
        "the golden log or sealing code changed: the committed TSA fixture no longer matches; "
        "see tests/fixtures/tsa/README.md"
    )
    tsr = (TSA_FIXTURES / "golden_checkpoint.tsr").read_bytes()
    roots = load_tsa_roots((TSA_FIXTURES / "freetsa_root.pem").read_bytes())
    records: list[SealRecord] = [*checkpoint_records(notes), TimestampLine(note_sha256, tsr)]
    by_key = _verify(sealed_events, notes, checkpoints=records, roots=roots)
    assert by_key[("r1", "s2")].status == "verified"
    assert by_key[("r1", "s1")].status == "unverified"  # s1's checkpoints are still unanchored


def test_unsealed_tail_is_partial__defs_10_6() -> None:
    """A session still being recorded: a sealed prefix, then an unsealed tail."""

    s1, s2 = GOLDEN.sessions
    s2_sealed_events, s2_notes = seal_sessions(
        [Session(s2.robot_id, s2.session_id, s2.events[:-1])]
    )
    tail = dict(s2.events[-1])
    s1_sealed_events, s1_notes = seal_sessions([s1])
    sealed_events = [*s1_sealed_events, *s2_sealed_events, tail]
    notes = s1_notes + s2_notes

    by_key = _verify(sealed_events, notes, anchors=checkpoint_records(notes))
    assert by_key[("r1", "s1")].status == "verified"
    s2_verdict = by_key[("r1", "s2")]
    assert s2_verdict.status == "partial"
    assert s2_verdict.sealed_through_seq == s2.events[-2]["seq"]
    assert any(g.code == "no_checkpoint" for g in s2_verdict.gaps)


# --- Attacks (research/demo-prototypes/seal.py; none may end "verified") -------------------------


def test_attack_edit_without_rehashing__hash_mismatch() -> None:
    sealed_events, notes = _seal_golden()
    edited = copy.deepcopy(sealed_events)
    i = next(i for i, e in enumerate(edited) if e.get("state") == "human_remote")
    edited[i]["state"] = "ai"  # type: ignore[index]
    by_key = _verify(edited, notes, anchors=checkpoint_records(notes))
    assert by_key[("r1", "s1")].status != "verified"
    assert any(e.code == "hash_mismatch" for e in by_key[("r1", "s1")].evidence)


def _stripped_and_edited(sealed_events: list[Event]) -> tuple[list[dict], list[dict]]:
    """Strip hash/prev_hash and flip the one human_remote transition to 'ai'."""
    raw = [{k: v for k, v in e.items() if k not in ("hash", "prev_hash")} for e in sealed_events]
    i = next(i for i, e in enumerate(raw) if e.get("state") == "human_remote")
    raw[i]["state"] = "ai"
    s1_raw = [e for e in raw if e["session_id"] == "s1"]
    s2_raw = [e for e in raw if e["session_id"] == "s2"]
    return s1_raw, s2_raw


def test_attack_edit_and_rehash_the_chain__root_mismatch() -> None:

    sealed_events, notes = _seal_golden()
    s1_raw, s2_raw = _stripped_and_edited(sealed_events)
    s1 = Session("r1", "s1", tuple(s1_raw))
    s2 = Session("r1", "s2", tuple(s2_raw))
    re_sealed, _ = seal_sessions([s1, s2])  # rehashed, but the OLD checkpoints/anchor are kept
    by_key = _verify(re_sealed, notes, anchors=checkpoint_records(notes))
    assert by_key[("r1", "s1")].status != "verified"
    assert any(e.code == "root_mismatch" for e in by_key[("r1", "s1")].evidence)


def test_attack_edit_rehash_and_resign__caught_only_by_the_anchor() -> None:
    """Assert both outcomes: a fabricated-but-self-consistent log passes with no outside copy,
    and is caught once an anchor (or timestamp) from before the attack exists."""

    sealed_events, notes = _seal_golden()
    s1_raw, s2_raw = _stripped_and_edited(sealed_events)
    s1 = Session("r1", "s1", tuple(s1_raw))
    s2 = Session("r1", "s2", tuple(s2_raw))
    re_sealed, re_notes = seal_sessions([s1, s2])  # full re-fabrication, same key

    # The attacker also re-anchors their fabrication (an anchor only helps when it predates the
    # attack): L2 alone cannot then tell the fabrication from the original.
    self_anchored = _verify(re_sealed, re_notes, anchors=checkpoint_records(re_notes))
    assert self_anchored[("r1", "s1")].status == "verified"  # the documented limitation

    with_anchor = _verify(re_sealed, re_notes, anchors=checkpoint_records(notes))
    assert with_anchor[("r1", "s1")].status != "verified"
    assert any(e.code == "root_mismatch" for e in with_anchor[("r1", "s1")].evidence)


def test_attack_truncate_after_a_checkpoint__missing_events() -> None:
    sealed_events, notes = _seal_golden()
    kept = [e for e in sealed_events if not (e["session_id"] == "s2" and e["seq"] >= 1)]
    by_key = _verify(kept, notes, anchors=checkpoint_records(notes))
    assert by_key[("r1", "s2")].status != "verified"
    assert any(e.code == "missing_events" for e in by_key[("r1", "s2")].evidence)


def test_attack_delete_tail_add_session_end_and_resign__caught_only_by_the_anchor() -> None:

    sealed_events, notes = _seal_golden()
    raw = [
        {k: v for k, v in e.items() if k not in ("hash", "prev_hash")}
        for e in sealed_events
        if not (e["session_id"] == "s2" and e["type"] == "session_end")
    ]
    raw.append(
        {
            "robot_id": "r1",
            "session_id": "s2",
            "task_id": None,
            "seq": 2,
            "ts": "2026-11-03T15:02:10.000Z",
            "type": "session_end",
        }
    )
    s1 = Session("r1", "s1", tuple(e for e in raw if e["session_id"] == "s1"))
    s2_raw = sorted((e for e in raw if e["session_id"] == "s2"), key=lambda e: e["seq"])
    s2 = Session("r1", "s2", tuple(s2_raw))
    re_sealed, re_notes = seal_sessions([s1, s2])

    # As above: only an anchor from before the attack catches a fully re-fabricated session.
    self_anchored = _verify(re_sealed, re_notes, anchors=checkpoint_records(re_notes))
    assert self_anchored[("r1", "s2")].status == "verified"

    with_anchor = _verify(re_sealed, re_notes, anchors=checkpoint_records(notes))
    assert with_anchor[("r1", "s2")].status != "verified"


def test_attack_strip_all_hashes__unsealed() -> None:
    sealed_events, _notes = _seal_golden()
    stripped = [
        {k: v for k, v in e.items() if k not in ("hash", "prev_hash")} for e in sealed_events
    ]
    # A session that was never sealed also never had a legitimate checkpoint.
    by_key = _verify(stripped, [])
    for verdict in by_key.values():
        assert verdict.status != "verified"
        assert verdict.evidence == ()
        assert any(g.code == "unsealed" for g in verdict.gaps)


def test_attack_delete_a_whole_session__missing_events() -> None:
    sealed_events, notes = _seal_golden()
    remaining = [e for e in sealed_events if e["session_id"] != "s2"]
    by_key = _verify(remaining, notes, anchors=checkpoint_records(notes))
    assert by_key[("r1", "s2")].status == "failed"
    assert any(e.code == "missing_events" for e in by_key[("r1", "s2")].evidence)


def test_attack_swap_two_events__chain_break() -> None:
    # Swap only the seq labels (not the hash-chained content): reordering by seq then puts each
    # event's content where its stored prev_hash/hash no longer matches its new neighbours.
    sealed_events, notes = _seal_golden()
    swapped = copy.deepcopy(sealed_events)
    i = next(i for i, e in enumerate(swapped) if e["session_id"] == "s1" and e["seq"] == 1)
    j = next(j for j, e in enumerate(swapped) if e["session_id"] == "s1" and e["seq"] == 2)
    swapped[i]["seq"], swapped[j]["seq"] = swapped[j]["seq"], swapped[i]["seq"]
    by_key = _verify(swapped, notes, anchors=checkpoint_records(notes))
    assert by_key[("r1", "s1")].status != "verified"


def test_attack_replay_a_checkpoint_from_another_session() -> None:
    sealed_events, notes = _seal_golden()
    s2_notes = [n for n in notes if n.startswith("autonometry/r1/s2\n")]
    by_key = _verify(sealed_events, s2_notes, anchors=checkpoint_records(s2_notes))
    assert by_key[("r1", "s1")].status != "verified"


# --- Property tests --------------------------------------------------------------------------


@given(st.integers(min_value=1, max_value=6))
@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_seal_then_verify_with_anchor_is_always_verified__defs_10_6(n: int) -> None:

    key = Ed25519PrivateKey.generate()
    key_name = "autonometry/prop-test"
    events = tuple(
        {
            "robot_id": "r1",
            "session_id": "sX",
            "task_id": None,
            "seq": i,
            "ts": f"2026-01-01T00:00:{i:02d}.000Z",
            "type": "session_start" if i == 0 else ("session_end" if i == n - 1 else "heartbeat"),
            **(
                {
                    "scopes": ["whole_robot"],
                    "initial_states": {"whole_robot": "ai"},
                    "heartbeat_interval_s": 1.0,
                    "hint_policy": "hint_as_shared",
                    "task_registry_hash": "0" * 64,
                    "plan_hash": "0" * 64,
                }
                if i == 0
                else {}
            ),
        }
        for i in range(n)
    )
    session = Session("r1", "sX", events)
    sealed_events, notes = seal_sessions([session], key=key, key_name=key_name)
    keys = [parse_vkey(format_vkey(key_name, key.public_key().public_bytes_raw()))]
    log_check = LogCheck("0.2", tuple(resealed_sessions(sealed_events)), (), (), GOLDEN.counts)
    result = verify_log(
        log_check, checkpoint_records(notes), checkpoint_records(notes), keys, [], RULES
    )
    assert len(result.sessions) == 1
    assert result.sessions[0].status == "verified"


def _mutate(
    events: list[Event], notes: list[str], rng_choice: int
) -> tuple[list[Event], list[str]]:
    """Apply one of a few structural mutations, chosen by ``rng_choice % 4``."""
    choice = rng_choice % 4
    events = copy.deepcopy(events)
    notes = list(notes)
    if choice == 0 and events:
        events[0]["hash"] = "0" * 64  # type: ignore[index]
    elif choice == 1 and len(events) > 1:
        events.pop()
    elif choice == 2 and notes:
        # Flip a character in the last (full-coverage) checkpoint's signature: still parseable,
        # still the same trusted key name and ID, but the signature itself no longer verifies.
        last = notes[-1]
        sig_start = last.rindex(" ") + 1
        flipped = "A" if last[sig_start] != "A" else "B"
        notes[-1] = last[:sig_start] + flipped + last[sig_start + 1 :]
    elif choice == 3 and events:
        events[-1]["seq"] = events[-1]["seq"] + 5  # type: ignore[index]
    return events, notes


@given(st.integers(min_value=0, max_value=1000))
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_any_single_mutation_is_never_verified__defs_10_6(choice: int) -> None:
    sealed_events, notes = _seal_golden()
    anchors = checkpoint_records(notes)
    mutated_events, mutated_notes = _mutate(sealed_events, notes, choice)
    by_key = _verify(mutated_events, mutated_notes, anchors=anchors)
    # A mutation may target only one session; the log as a whole must never come back clean.
    assert not all(v.status == "verified" for v in by_key.values())


@given(st.binary(min_size=0, max_size=200))
@settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_random_bytes_as_checkpoint_note_never_crash(data: bytes) -> None:
    sealed_events, notes = _seal_golden()
    garbage_note = data.decode("utf-8", errors="replace")
    records: list[SealRecord] = [*checkpoint_records(notes), CheckpointLine(garbage_note)]
    by_key = _verify(sealed_events, [], anchors=records)
    assert by_key  # must not raise
