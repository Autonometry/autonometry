"""Verifying sealed logs (definitions.md v0.2 section 10.6).

A pure function over already-read data: :func:`verify_log` never opens a file or makes a network
call, and it never trusts a stored ``hash``, checkpoint root or timestamp without recomputing or
re-verifying it. The CLI (``autonometry verify``) is the only place that reads files and reports
exit status.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from cryptography import x509

from autonometry.events import (
    Event,
    EvidenceCode,
    GapCode,
    LogCheck,
    SealIssue,
    Session,
    SessionVerdict,
    TrustStatus,
    VerifyResult,
)
from autonometry.merkle import IncrementalTree, leaf_hash
from autonometry.sealfile import CheckpointLine, SealRecord, TimestampLine
from autonometry.sealing import event_hash, genesis_hash
from autonometry.signednote import (
    BadSignatureError,
    SignedNoteError,
    VerifierKey,
    parse_checkpoint_body,
    verify_note,
)
from autonometry.spec import Rules
from autonometry.timestamps import datetime_to_ns, seconds_to_ns, ts_to_ns
from autonometry.tsa import verify_token

_SessionKey = tuple[str, str]
# (session key, checkpoint size, Merkle root, full signed note text)
_TrustedCheckpoint = tuple[_SessionKey, int, bytes, str]


@dataclass
class _SessionWork:
    """Accumulates evidence and gaps for one session while it is being verified."""

    evidence: list[SealIssue] = field(default_factory=list)
    gaps: list[SealIssue] = field(default_factory=list)

    def fail(self, code: EvidenceCode, detail: str) -> None:
        self.evidence.append(SealIssue(code, detail))

    def gap(self, code: GapCode, detail: str) -> None:
        self.gaps.append(SealIssue(code, detail))


def _note_sha256(note: str) -> str:
    return hashlib.sha256(note.encode("utf-8")).hexdigest()


def _sealed_prefix_and_chain(
    events: tuple[Event, ...],
    chain_genesis_prefix: str,
    robot_id: str,
    session_id: str,
    work: _SessionWork,
) -> tuple[int, list[str | None]]:
    """Return (length of the sealed prefix, recomputed hash per event, ``None`` when unsealed).

    Checks, over every event regardless of position: content hashing (``hash_mismatch``), chain
    linkage and ``seq`` (``chain_break``), and that sealing forms a clean prefix rather than
    sealed events resuming after an unsealed one (also ``chain_break``, definitions.md 10.1).
    """
    recomputed: list[str | None] = []
    expected_prev = genesis_hash(chain_genesis_prefix, robot_id, session_id)
    previous_seq: int | None = None
    seen_unsealed = False
    prefix_len = 0
    for event in events:
        seq = event["seq"]
        if previous_seq is not None and seq != previous_seq + 1:
            work.fail("chain_break", f"seq jumps from {previous_seq} to {seq}")
        previous_seq = seq
        stored_hash = event.get("hash")
        stored_prev = event.get("prev_hash")
        if stored_hash is None or stored_prev is None:
            seen_unsealed = True
            recomputed.append(None)
            continue
        if seen_unsealed:
            work.fail("chain_break", f"seq {seq} is sealed after an unsealed event")
        else:
            prefix_len += 1
        if stored_prev != expected_prev:
            work.fail("chain_break", f"prev_hash at seq {seq} does not match the chain")
        this_hash = event_hash({**event, "prev_hash": stored_prev})
        if this_hash != stored_hash:
            work.fail("hash_mismatch", f"seq {seq}: stored hash does not match its content")
        recomputed.append(this_hash)
        expected_prev = this_hash
    return prefix_len, recomputed


def _roots_at_sizes(recomputed: Sequence[str | None], sizes: set[int]) -> dict[int, bytes | None]:
    """Merkle root at each of ``sizes`` (``None`` when any covered event is unsealed), computed
    in one incremental pass over ``recomputed`` rather than rebuilding the tree per size."""
    tree = IncrementalTree()
    roots: dict[int, bytes | None] = {}
    blocked = False
    for index, digest in enumerate(recomputed):
        size = index + 1
        if digest is None:
            blocked = True
        elif not blocked:
            tree.append(leaf_hash(bytes.fromhex(digest)))
        if size in sizes:
            roots[size] = None if blocked else tree.root()
    return roots


def _collect(
    records: Sequence[SealRecord],
    trusted_keys: Sequence[VerifierKey],
    work: dict[_SessionKey, _SessionWork],
) -> list[_TrustedCheckpoint]:
    """Parse and signature-check every ``CheckpointLine`` in ``records``.

    Returns the ones with a trusted, verifying signature. A note that cannot even be parsed is
    ignored (never crashes). An untrusted signature is a gap; a trusted signature that fails to
    verify is evidence; both are attributed to the session the note's own signed origin names.
    """
    out: list[_TrustedCheckpoint] = []
    for record in records:
        if not isinstance(record, CheckpointLine):
            continue
        note_body_text = record.note.partition("\n\n")[0] + "\n"
        try:
            body = parse_checkpoint_body(note_body_text)
        except SignedNoteError:
            continue
        key = (body.robot_id, body.session_id)
        try:
            verified = verify_note(record.note, trusted_keys)
        except BadSignatureError:
            work.setdefault(key, _SessionWork()).fail(
                "bad_signature", f"checkpoint size {body.size}: a trusted key's signature fails"
            )
            continue
        except SignedNoteError:
            continue
        if not verified.verified_keys:
            work.setdefault(key, _SessionWork()).gap(
                "untrusted_checkpoint",
                f"checkpoint size {body.size}: no signature from a trusted key",
            )
            continue
        out.append((key, body.size, body.root, record.note))
    return out


@dataclass(frozen=True, slots=True)
class _VerifyContext:
    """Everything one session's verification needs beyond its own events."""

    checkpoints: Sequence[_TrustedCheckpoint]
    anchors: Sequence[_TrustedCheckpoint]
    timestamps: Sequence[TimestampLine]
    trusted_tsa_roots: Sequence[x509.Certificate]
    rules: Rules


def _anchored_via_timestamp(
    note: str, last_event_ts_ns: int, ctx: _VerifyContext, work: _SessionWork
) -> bool:
    """Apply definitions.md 10.5(a): a granted, verified RFC 3161 token within the time window."""
    target = _note_sha256(note)
    anchored = False
    for record in ctx.timestamps:
        if record.note_sha256 != target:
            continue
        result = verify_token(record.tsr, note.encode("utf-8"), ctx.trusted_tsa_roots)
        if not result.ok or result.gen_time is None:
            work.gap("not_anchored", f"timestamp token does not verify: {result.detail}")
            continue
        gen_ns = datetime_to_ns(result.gen_time)
        if gen_ns > last_event_ts_ns + seconds_to_ns(ctx.rules.sealing.max_commit_delay_s):
            work.gap("late_anchor", "timestamp token is later than the commit delay allows")
            continue
        if gen_ns < last_event_ts_ns - seconds_to_ns(ctx.rules.sealing.clock_tolerance_s):
            work.gap("clock_skew", "timestamp token is earlier than the clock tolerance allows")
            continue
        anchored = True
    return anchored


def _anchored_via_file(size: int, root: bytes, key: _SessionKey, ctx: _VerifyContext) -> bool:
    """Apply definitions.md 10.5(b): the same checkpoint held in an anchor file."""
    return any(k == key and s == size and r == root for k, s, r, _ in ctx.anchors)


def _check_one_checkpoint(
    events: tuple[Event, ...],
    roots_at: dict[int, bytes | None],
    checkpoint: _TrustedCheckpoint,
    ctx: _VerifyContext,
    work: _SessionWork,
) -> int | None:
    """Check one checkpoint claiming to cover ``size`` events; return that size when it is valid,
    root-matching and anchored (definitions.md 10.3 to 10.5), else ``None``."""
    key, size, root, note = checkpoint
    if size > len(events):
        work.fail(
            "missing_events", f"checkpoint size {size} exceeds the {len(events)} events found"
        )
        return None
    actual_root = roots_at.get(size)
    if actual_root is None:
        work.fail("root_mismatch", f"checkpoint size {size}: covered events are not all sealed")
        return None
    if actual_root != root:
        work.fail("root_mismatch", f"checkpoint size {size}: root does not match the events")
        return None
    last_ts_ns = ts_to_ns(events[size - 1]["ts"])
    anchored = _anchored_via_timestamp(note, last_ts_ns, ctx, work) or _anchored_via_file(
        size, root, key, ctx
    )
    if not anchored:
        work.gap("not_anchored", f"checkpoint size {size} has no anchor")
        return None
    return size


def _check_anchor_roots(
    key: _SessionKey,
    events: tuple[Event, ...],
    roots_at: dict[int, bytes | None],
    ctx: _VerifyContext,
    work: _SessionWork,
) -> None:
    """An anchor's own root must also match the events it claims to cover."""
    for sess_key, size, root, _ in ctx.anchors:
        if sess_key != key or size > len(events):
            continue
        actual_root = roots_at.get(size)
        if actual_root is not None and actual_root != root:
            work.fail("root_mismatch", f"anchor size {size}: root does not match the events")


def _session_status(
    work: _SessionWork,
    events: tuple[Event, ...],
    prefix_len: int,
    best_anchored_size: int,
    capped: bool,
) -> tuple[TrustStatus, int | None]:
    if work.evidence:
        return "failed", None
    if best_anchored_size and not capped:
        if best_anchored_size == len(events):
            return "verified", events[best_anchored_size - 1]["seq"]
        work.gap("no_checkpoint", "sealed events follow the last anchored checkpoint")
        return "partial", events[best_anchored_size - 1]["seq"]
    if prefix_len == 0:
        work.gap("unsealed", "no event is sealed")
    elif not any(g.code == "not_anchored" for g in work.gaps):
        work.gap("no_checkpoint", "sealed events have no covering checkpoint")
    return "unverified", None


def _verify_session(
    key: _SessionKey, events: tuple[Event, ...], ctx: _VerifyContext, work: _SessionWork
) -> SessionVerdict:
    robot_id, session_id = key
    prefix_len, recomputed = _sealed_prefix_and_chain(
        events, ctx.rules.chain_genesis_prefix, robot_id, session_id, work
    )
    reconstructed = any(bool(e.get("x_ts_reconstructed")) for e in events)
    if reconstructed:
        work.gap("reconstructed", "session holds events with x_ts_reconstructed: true")

    needed_sizes = {
        size
        for sess_key, size, _, _ in (*ctx.checkpoints, *ctx.anchors)
        if sess_key == key and size <= len(events)
    }
    roots_at = _roots_at_sizes(recomputed, needed_sizes)

    sizes = [
        _check_one_checkpoint(events, roots_at, checkpoint, ctx, work)
        for checkpoint in ctx.checkpoints
        if checkpoint[0] == key
    ]
    best_anchored_size = max((s for s in sizes if s is not None), default=0)
    _check_anchor_roots(key, events, roots_at, ctx, work)

    status, sealed_through = _session_status(
        work, events, prefix_len, best_anchored_size, reconstructed
    )
    return SessionVerdict(
        robot_id, session_id, status, sealed_through, tuple(work.evidence), tuple(work.gaps)
    )


def verify_log(  # noqa: PLR0913, PLR0917 -- matches the verifier's spec signature exactly
    log_check: LogCheck,
    checkpoint_records: Sequence[SealRecord],
    anchor_records: Sequence[SealRecord],
    trusted_keys: Sequence[VerifierKey],
    trusted_tsa_roots: Sequence[x509.Certificate],
    rules: Rules,
) -> VerifyResult:
    """Verify every session of ``log_check`` (definitions.md section 10.6).

    ``checkpoint_records`` and ``anchor_records`` are every ``SealRecord`` read from, respectively,
    the logs' ``.checkpoints.jsonl`` files and the ``--anchor`` files given, pooled together: a
    checkpoint's own signed origin, not which file it came from, decides which session it covers.
    A session named by a checkpoint or anchor but absent from the log is still reported (evidence
    code ``missing_events``).
    """
    sessions_by_key: dict[_SessionKey, Session] = {
        (s.robot_id, s.session_id): s for s in log_check.sessions
    }
    work: dict[_SessionKey, _SessionWork] = {}
    checkpoints = _collect(checkpoint_records, trusted_keys, work)
    anchors = _collect(anchor_records, trusted_keys, work)
    timestamps = [t for t in checkpoint_records if isinstance(t, TimestampLine)]

    all_keys = set(sessions_by_key) | {k for k, *_ in checkpoints} | {k for k, *_ in anchors}
    for key in all_keys:
        work.setdefault(key, _SessionWork())

    known_notes = {_note_sha256(note) for _, _, _, note in checkpoints}
    for record in timestamps:
        if record.note_sha256 not in known_notes:
            for session_work in work.values():
                session_work.fail(
                    "bad_timestamp_token", "a timestamp record matches no checkpoint note"
                )

    ctx = _VerifyContext(checkpoints, anchors, timestamps, trusted_tsa_roots, rules)
    verdicts = [
        _verify_session(
            key, sessions_by_key[key].events if key in sessions_by_key else (), ctx, work[key]
        )
        for key in sorted(all_keys)
    ]
    return VerifyResult(log_check, tuple(verdicts))
