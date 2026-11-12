"""Tests for autonometry.verify (definitions.md v0.3 section 10.6) on the golden sealed log, its
attacks (research/demo-prototypes/seal.py) and every evidence, gap and file code."""

import copy
import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from autonometry import verify
from autonometry.events import Event, LineError, Session, SessionVerdict, VerifyResult
from autonometry.reader import read_logs
from autonometry.signednote import checkpoint_body, format_vkey, parse_vkey, sign_note
from autonometry.spec import Rules, SpecError, load_rules
from autonometry.timestamps import seconds_to_ns
from autonometry.tsa import load_tsa_roots
from autonometry.verify import timestamp_window, verify_log
from sealing_helpers import KEY_NAME, SEED_KEY, log_check_of, note_sha256, seal_file, seal_sessions

GOLDEN_LOG = Path(__file__).parent / "golden" / "sealed" / "log.jsonl"
TSA_FIXTURES = Path(__file__).parent / "fixtures" / "tsa"
RULES = load_rules("0.3")
TRUSTED_KEYS = [parse_vkey(format_vkey(KEY_NAME, SEED_KEY.public_key().public_bytes_raw()))]
GOLDEN = read_logs([GOLDEN_LOG], "0.3")
SEALED, NOTES = seal_sessions(GOLDEN.sessions)
S2_FINAL = next(n for n in NOTES if n.startswith("autonometry/r1/s2\n3\n"))
S1_FINAL = next(n for n in NOTES if n.startswith("autonometry/r1/s1\n4\n"))
TSR = (TSA_FIXTURES / "golden_checkpoint.tsr").read_bytes()
TSA_ROOTS = load_tsa_roots((TSA_FIXTURES / "freetsa_root.pem").read_bytes())


def _run(  # noqa: PLR0913, PLR0917 -- every input of verify_log, each with a default
    events: Sequence[Any] = SEALED,
    notes: Sequence[str] | None = NOTES,
    anchors: Sequence[str] | None = None,
    stamps: Sequence[tuple[str, bytes]] = (),
    keys: Sequence[Any] = TRUSTED_KEYS,
    roots: Sequence[x509.Certificate] = (),
    rules: Rules = RULES,
    errors: Sequence[LineError] = (),
) -> VerifyResult:
    checkpoint_files = [] if notes is None else [seal_file("log.checkpoints.jsonl", notes, stamps)]
    anchor_files = [] if anchors is None else [seal_file("anchor.jsonl", anchors)]
    return verify_log(
        log_check_of(events, errors), checkpoint_files, anchor_files, keys, roots, rules
    )


def _by_key(result: VerifyResult) -> dict[str, SessionVerdict]:
    return {v.session_id: v for v in result.sessions}


def _codes(verdict: SessionVerdict) -> set[str]:
    return {i.code for i in verdict.evidence} | {i.code for i in verdict.gaps}


def _raw(events: Sequence[Event]) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if k not in ("hash", "prev_hash")} for e in events]


def _resealed(raw: Sequence[dict[str, Any]]) -> tuple[list[Event], list[str]]:
    sessions = [
        Session(
            "r1",
            sid,
            tuple(sorted((e for e in raw if e["session_id"] == sid), key=lambda e: e["seq"])),
        )  # type: ignore[misc]
        for sid in ("s1", "s2")
        if any(e["session_id"] == sid for e in raw)
    ]
    return seal_sessions(sessions)


# --- The three golden outcomes --------------------------------------------------------------


def test_intact_log_with_anchor_file_is_verified__defs_10_6() -> None:
    result = _run(anchors=NOTES)
    assert result.verified("L2")
    for verdict in result.sessions:
        assert verdict.status == "verified"
        assert verdict.evidence == ()
        assert verdict.gaps == ()
    assert _by_key(result)["s1"].sealed_through_seq == 3


def test_sealed_log_without_anchor_is_unverified_not_anchored__defs_10_5() -> None:
    result = _run()
    for verdict in result.sessions:
        assert verdict.status == "unverified"
        assert verdict.evidence == ()
        assert _codes(verdict) == {"not_anchored"}
    assert result.reasons("L2") == ["r1/s1 unverified, below L2", "r1/s2 unverified, below L2"]
    assert result.verified("L1")


def test_valid_anchor_alone_counts_as_anchored_checkpoint__defs_10_6() -> None:
    result = _run(notes=None, anchors=NOTES)
    assert {v.status for v in result.sessions} == {"verified"}


def test_sealed_events_after_last_checkpoint_are_partial__defs_10_6() -> None:
    covered = [n for n in NOTES if n != S2_FINAL]
    verdict = _by_key(_run(notes=covered, anchors=covered))["s2"]
    assert verdict.status == "partial"
    assert verdict.sealed_through_seq == 0
    assert _codes(verdict) == {"no_checkpoint"}


def test_real_tsa_timestamp_anchors_its_checkpoint__defs_10_5() -> None:
    by_key = _by_key(_run(stamps=[(note_sha256(S2_FINAL), TSR)], roots=TSA_ROOTS))
    assert by_key["s2"].status == "verified"
    assert by_key["s1"].status == "unverified"


# --- Review findings B1: anchors catch deletion --------------------------------------------


def test_deleting_tail_and_its_checkpoint_fails_with_anchor__missing_events() -> None:
    events = [e for e in SEALED if not (e["session_id"] == "s2" and e["seq"] == 2)]
    notes = [n for n in NOTES if n != S2_FINAL]
    verdict = _by_key(_run(events, notes=notes, anchors=NOTES))["s2"]
    assert verdict.status == "failed"
    assert "missing_events" in _codes(verdict)


def test_deleting_a_session_and_its_checkpoints_fails_with_anchor__missing_events() -> None:
    events = [e for e in SEALED if e["session_id"] != "s2"]
    notes = [n for n in NOTES if "/s2\n" not in n]
    verdict = _by_key(_run(events, notes=notes, anchors=NOTES))["s2"]
    assert verdict.status == "failed"
    assert "missing_events" in _codes(verdict)


def test_stripping_hashes_and_checkpoints_fails_with_anchor__root_mismatch() -> None:
    result = _run(_raw(SEALED), notes=None, anchors=NOTES)
    for verdict in result.sessions:
        assert verdict.status == "failed"
        assert {i.code for i in verdict.evidence} == {"root_mismatch"}


# --- B2: honest logs are not failed -----------------------------------------------------------


def test_unsealed_log_with_a_lost_line_is_only_unsealed__defs_10_6() -> None:
    raw = [e for e in _raw(SEALED) if not (e["session_id"] == "s1" and e["seq"] == 1)]
    lost = LineError("log.jsonl", 2, "invalid_json", "Expecting value at column 1")
    result = _run(raw, notes=None, errors=[lost])
    for verdict in result.sessions:
        assert verdict.status == "unverified"
        assert _codes(verdict) == {"unsealed"}
    assert result.reasons("L1") == ["1 rejected line(s)"]


def test_timestamped_log_without_vkey_is_unverified_not_failed__defs_10_6() -> None:
    result = _run(stamps=[(note_sha256(S2_FINAL), TSR)], keys=[], roots=TSA_ROOTS)
    assert result.file_problems == ()
    for verdict in result.sessions:
        assert verdict.status == "unverified"
        assert _codes(verdict) == {"untrusted_checkpoint", "no_checkpoint"}


# --- B3: spec deviations ----------------------------------------------------------------------


def test_undecodable_token_is_bad_timestamp_token__defs_10_6() -> None:
    verdict = _by_key(_run(stamps=[(note_sha256(S2_FINAL), TSR[:40])], anchors=NOTES))["s2"]
    assert verdict.status == "failed"
    assert {i.code for i in verdict.evidence} == {"bad_timestamp_token"}


def test_token_imprint_not_its_note_sha256_is_bad_timestamp_token__defs_10_6() -> None:
    # The real token is over s2's note; tied by note_sha256 to s1's checkpoint instead.
    by_key = _by_key(_run(stamps=[(note_sha256(S1_FINAL), TSR)], anchors=NOTES))
    assert {i.code for i in by_key["s1"].evidence} == {"bad_timestamp_token"}
    assert by_key["s2"].status == "verified"


def test_token_not_verifying_against_trusted_roots_is_not_anchored__defs_10_5() -> None:
    verdict = _by_key(_run(stamps=[(note_sha256(S2_FINAL), TSR)]))["s2"]
    assert verdict.status == "unverified"
    assert verdict.evidence == ()
    assert _codes(verdict) == {"not_anchored"}
    assert any("no trusted TSA root" in gap.detail for gap in verdict.gaps)


def test_unsealed_events_after_sealed_ones_are_chain_break__defs_10_6() -> None:
    events = [*SEALED[:-1], _raw(SEALED[-1:])[0]]
    verdict = _by_key(_run(events, anchors=NOTES))["s2"]
    assert verdict.status == "failed"
    assert "chain_break" in {i.code for i in verdict.evidence}


def test_note_with_lines_after_a_blank_line_is_malformed_checkpoint__defs_10_6() -> None:
    body = checkpoint_body("r1", "s2", 3, bytes(32))
    forged = sign_note(body + "\nextra line\n", KEY_NAME, SEED_KEY)  # body, blank, extra: signed
    result = _run(notes=[*NOTES, forged], anchors=NOTES)
    assert [p.code for p in result.file_problems] == ["malformed_checkpoint"]
    assert result.file_problems[0].line == len(NOTES) + 1
    assert {v.status for v in result.sessions} == {"verified"}
    assert not result.verified("L2")
    assert result.reasons("L2") == ["1 file problem(s)"]


def test_unparseable_note_is_malformed_checkpoint__defs_10_6() -> None:
    result = _run(notes=[*NOTES, "not a signed note at all"], anchors=NOTES)
    assert [p.code for p in result.file_problems] == ["malformed_checkpoint"]


def test_timestamp_matching_no_checkpoint_is_orphan_timestamp__defs_10_6() -> None:
    result = _run(stamps=[("a" * 64, TSR)], anchors=NOTES)
    assert [p.code for p in result.file_problems] == ["orphan_timestamp"]
    assert {v.status for v in result.sessions} == {"verified"}
    assert not result.verified("L2")


def test_timestamp_for_a_malformed_checkpoint_is_not_an_orphan__defs_10_6() -> None:
    result = _run(notes=[*NOTES, "garbage"], stamps=[(note_sha256("garbage"), b"x")], anchors=NOTES)
    assert [p.code for p in result.file_problems] == ["malformed_checkpoint"]


# --- Every other code -----------------------------------------------------------------------


def test_edit_without_rehash__hash_mismatch() -> None:
    edited = copy.deepcopy(SEALED)
    edited[1]["state"] = "ai"  # type: ignore[typeddict-unknown-key]
    verdict = _by_key(_run(edited, anchors=NOTES))["s1"]
    assert verdict.status == "failed"
    assert {"hash_mismatch", "root_mismatch"} <= {i.code for i in verdict.evidence}


def test_edit_and_rehash_with_old_anchor__root_mismatch() -> None:
    raw = _raw(SEALED)
    raw[1]["state"] = "ai"
    resealed, _ = _resealed(raw)
    verdict = _by_key(_run(resealed, anchors=NOTES))["s1"]
    assert verdict.status == "failed"
    assert {i.code for i in verdict.evidence} == {"root_mismatch"}


def test_edit_rehash_and_resign__caught_only_by_an_older_anchor() -> None:
    """Both outcomes: a fabrication re-anchored by its author passes; an anchor made before it
    catches it."""
    raw = _raw(SEALED)
    raw[1]["state"] = "ai"
    resealed, new_notes = _resealed(raw)
    assert _by_key(_run(resealed, new_notes, anchors=new_notes))["s1"].status == "verified"
    assert _by_key(_run(resealed, new_notes, anchors=NOTES))["s1"].status == "failed"


def test_delete_tail_add_session_end_and_resign__caught_only_by_an_older_anchor() -> None:
    raw = [e for e in _raw(SEALED) if not (e["session_id"] == "s2" and e["seq"] == 2)]
    raw.append(
        {"robot_id": "r1", "session_id": "s2", "task_id": None, "seq": 2,
         "ts": "2026-11-12T07:52:10.000Z", "type": "session_end"}
    )  # fmt: skip
    resealed, new_notes = _resealed(raw)
    assert _by_key(_run(resealed, new_notes, anchors=new_notes))["s2"].status == "verified"
    assert _by_key(_run(resealed, new_notes, anchors=NOTES))["s2"].status == "failed"


def test_truncate_after_a_checkpoint__missing_events() -> None:
    events = [e for e in SEALED if not (e["session_id"] == "s2" and e["seq"] >= 1)]
    verdict = _by_key(_run(events, anchors=NOTES))["s2"]
    assert verdict.status == "failed"
    assert "missing_events" in _codes(verdict)


def test_swap_two_events__chain_break() -> None:
    swapped = copy.deepcopy(SEALED)
    swapped[1]["seq"], swapped[2]["seq"] = 2, 1
    verdict = _by_key(_run(swapped, anchors=NOTES))["s1"]
    assert verdict.status == "failed"
    assert "chain_break" in {i.code for i in verdict.evidence}


def test_seq_jump__chain_break() -> None:
    jumped = copy.deepcopy(SEALED)
    jumped[3]["seq"] = 7
    assert "chain_break" in _codes(_by_key(_run(jumped))["s1"])


def test_replay_a_checkpoint_from_another_session_is_not_verified() -> None:
    s2_notes = [n for n in NOTES if "/s2\n" in n]
    verdict = _by_key(_run(notes=s2_notes, anchors=s2_notes))["s1"]
    assert verdict.status == "unverified"
    assert _codes(verdict) == {"no_checkpoint"}


def test_tampered_trusted_signature__bad_signature() -> None:
    # Past the first 8 base64 characters, which hold the 4-byte key ID: same key, bad signature.
    at = S2_FINAL.rindex(" ") + 12
    flipped = "A" if S2_FINAL[at] != "A" else "B"
    tampered = S2_FINAL[:at] + flipped + S2_FINAL[at + 1 :]
    notes = [tampered if n == S2_FINAL else n for n in NOTES]
    verdict = _by_key(_run(notes=notes, anchors=NOTES))["s2"]
    assert verdict.status == "failed"
    assert {i.code for i in verdict.evidence} == {"bad_signature"}


def test_checkpoint_from_an_unknown_key__untrusted_checkpoint() -> None:
    other = Ed25519PrivateKey.generate()
    _, foreign = seal_sessions(GOLDEN.sessions, key=other, key_name="someone/else")
    verdict = _by_key(_run(notes=foreign))["s1"]
    assert verdict.status == "unverified"
    assert _codes(verdict) == {"untrusted_checkpoint", "no_checkpoint"}


@pytest.mark.parametrize("marker", ["x_ts_reconstructed", "x_attribution_inferred"])
def test_marked_events_are_capped_at_l1__defs_10_6(marker: str) -> None:
    """One marked event caps its session at L1 with gap code reconstructed (8, 6.12, 10.6)."""
    raw = _raw(SEALED)
    raw[1][marker] = True
    resealed, new_notes = _resealed(raw)
    verdicts = _by_key(_run(resealed, new_notes, anchors=new_notes))
    assert verdicts["s1"].status == "unverified"
    assert _codes(verdicts["s1"]) == {"reconstructed"}
    (gap,) = verdicts["s1"].gaps
    assert marker in gap.detail
    assert verdicts["s2"].status == "verified"


def test_reconstructed_events_are_capped_at_l1__reconstructed() -> None:
    raw = _raw(SEALED)
    for event in raw:
        event["x_ts_reconstructed"] = True
    resealed, new_notes = _resealed(raw)
    for verdict in _run(resealed, new_notes, anchors=new_notes).sessions:
        assert verdict.status == "unverified"
        assert _codes(verdict) == {"reconstructed"}


def test_late_token__late_anchor_end_to_end() -> None:
    sealing = RULES.sealing.model_copy(update={"max_commit_delay_s": 1.0})
    rules = RULES.model_copy(update={"sealing": sealing})
    verdict = _by_key(_run(stamps=[(note_sha256(S2_FINAL), TSR)], roots=TSA_ROOTS, rules=rules))[
        "s2"
    ]
    assert verdict.status == "unverified"
    assert _codes(verdict) == {"late_anchor", "not_anchored"}


def test_early_token__clock_skew_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "timestamp_window", lambda *_: "early")
    verdict = _by_key(_run(stamps=[(note_sha256(S2_FINAL), TSR)], roots=TSA_ROOTS))["s2"]
    assert _codes(verdict) == {"clock_skew", "not_anchored"}


def test_session_absent_from_log_with_no_checkpoint_is_unsealed() -> None:
    zero = sign_note(
        checkpoint_body("r1", "s9", 0, hashlib.sha256(b"").digest()), KEY_NAME, SEED_KEY
    )
    verdict = _by_key(_run(notes=[*NOTES, zero], anchors=NOTES))["s9"]
    assert verdict.status == "unverified"
    assert _codes(verdict) == {"unsealed"}


def test_size_zero_checkpoint_matches_the_empty_tree_and_covers_nothing() -> None:
    zero = sign_note(
        checkpoint_body("r1", "s1", 0, hashlib.sha256(b"").digest()), KEY_NAME, SEED_KEY
    )
    result = _run(notes=[*NOTES, zero], anchors=[*NOTES, zero])
    assert {v.status for v in result.sessions} == {"verified"}
    assert all(v.gaps == () for v in result.sessions)


def _heartbeats(n: int, session_id: str = "sX") -> tuple[dict[str, Any], ...]:
    return tuple(
        {"robot_id": "r1", "session_id": session_id, "task_id": None, "seq": i,
         "ts": f"2026-01-01T00:00:{i:02d}.000Z", "type": "heartbeat"}
        for i in range(n)
    )  # fmt: skip


def test_repeated_codes_list_five_details_and_count_the_rest() -> None:
    sealed, notes = seal_sessions([Session("r1", "sX", _heartbeats(8))])  # type: ignore[arg-type]
    for event in sealed:
        event["type"] = "help_request"
    issues = [i for i in _run(sealed, notes).sessions[0].evidence if i.code == "hash_mismatch"]
    assert len(issues) == 5
    assert issues[-1].detail.endswith("(and 3 more)")


def test_rejected_lines_of_seal_files_make_the_result_not_verified() -> None:
    bad = LineError("anchor.jsonl", 9, "invalid_json", "Expecting value at column 1")
    files = [seal_file("anchor.jsonl", NOTES, errors=[bad])]
    result = verify_log(log_check_of(SEALED), [], files, TRUSTED_KEYS, [], RULES)
    assert {v.status for v in result.sessions} == {"verified"}
    assert result.rejected_lines == (bad,)
    assert result.reasons("L2") == ["1 rejected line(s)"]
    assert result.to_json("L2")["verified"] is False


def test_empty_log_is_not_verified() -> None:
    assert _run([], notes=None).reasons("L1") == ["no session to verify"]


def test_file_problems_are_listed_up_to_the_limit_and_counted_exactly() -> None:
    errors = RULES.errors.model_copy(update={"list_max_per_code": 2})
    rules = RULES.model_copy(update={"errors": errors})
    result = _run(stamps=[("a" * 64, b"x")] * 3, anchors=NOTES, rules=rules)
    assert len(result.file_problems) == 2
    assert result.file_problems_by_code == {"orphan_timestamp": 3}


def test_spec_with_different_sealing_codes_is_refused() -> None:
    sealing = RULES.sealing.model_copy(update={"gap_codes": ("unsealed",)})
    with pytest.raises(SpecError, match="gap_codes differ"):
        _run(rules=RULES.model_copy(update={"sealing": sealing}))


def test_json_summary_carries_verdict_rejects_and_problems() -> None:
    payload = _run(stamps=[("a" * 64, b"x")], anchors=NOTES).to_json("L2")
    assert payload["verified"] is False
    assert payload["reasons"] == ["1 file problem(s)"]
    assert payload["file_problems"][0]["code"] == "orphan_timestamp"
    assert payload["counts"]["file_problems_by_code"] == {"orphan_timestamp": 1}
    assert [s["status"] for s in payload["sessions"]] == ["verified", "verified"]


# --- The 10.5 window ---------------------------------------------------------------------------

LAST = seconds_to_ns(1_000_000.0)


@pytest.mark.parametrize(
    ("offset_s", "expected"),
    [
        (0.0, "inside"),
        (3600.0, "inside"),
        (3600.001, "late"),
        (-1.0, "inside"),
        (-1.001, "early"),
    ],
)
def test_timestamp_window__defs_10_5(offset_s: float, expected: str) -> None:
    assert timestamp_window(LAST + seconds_to_ns(offset_s), LAST, 3600.0, 1.0) == expected


# --- Properties ------------------------------------------------------------------------------


@given(st.integers(min_value=1, max_value=6))
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_seal_then_verify_with_anchor_is_always_verified__defs_10_6(n: int) -> None:
    key = Ed25519PrivateKey.generate()
    events = tuple(
        {"robot_id": "r1", "session_id": "sX", "task_id": None, "seq": i,
         "ts": f"2026-01-01T00:00:{i:02d}.000Z", "type": "heartbeat"}
        for i in range(n)
    )  # fmt: skip
    sealed, notes = seal_sessions([Session("r1", "sX", events)], key=key, key_name="k/p")  # type: ignore[arg-type]
    keys = [parse_vkey(format_vkey("k/p", key.public_key().public_bytes_raw()))]
    result = _run(sealed, notes, anchors=notes, keys=keys)
    assert result.verified("L2")


def _mutate(choice: int) -> tuple[list[Any], list[str], list[str]]:
    """One mutation of the sealed log, its checkpoint file or its anchor file."""
    events: list[Any] = copy.deepcopy(SEALED)
    notes, anchors = list(NOTES), list(NOTES)
    index = choice // 8 % len(events)
    match choice % 8:
        case 0:
            events[index]["hash"] = "0" * 64
        case 1:
            del events[index]
        case 2:
            events[index]["ts"] = "2026-11-12T07:50:01.000Z"
        case 3:
            events[index]["seq"] += 50
        case 4:
            del events[index]["prev_hash"]
        case 5:
            notes[index % len(notes)] = notes[index % len(notes)].replace("\n", "\n\n", 1)
        case 6:
            anchors[index % len(anchors)] = anchors[index % len(anchors)][:-3] + "AA\n"
        case _:
            events[index]["x_note"] = "added"
    return events, notes, anchors


@given(st.integers(min_value=0, max_value=10_000))
@settings(max_examples=60, deadline=None)
def test_any_single_mutation_is_never_verified__defs_10_6(choice: int) -> None:
    events, notes, anchors = _mutate(choice)
    assert not _run(events, notes, anchors=anchors).verified("L2")


@given(st.binary(max_size=300))
@settings(max_examples=40, deadline=None)
def test_random_bytes_in_checkpoint_and_anchor_files_never_crash(data: bytes) -> None:
    garbage = data.decode("utf-8", errors="replace")
    result = _run(notes=[*NOTES, garbage], stamps=[(garbage[:64], data)], anchors=[garbage])
    assert len(result.sessions) >= 2
