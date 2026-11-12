"""Verifying sealed logs (definitions.md v0.2 section 10.6).

A pure function over already-read data: :func:`verify_log` never opens a file or makes a network
call, and it never trusts a stored value. Every event's hash is recomputed from its content,
sealed or not, so a Merkle root exists at every size; every signed checkpoint, from checkpoint
files and anchor files alike, is then checked against those roots. The CLI (``autonometry
verify``) is the only place that reads files and reports exit status.
"""

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, cast, get_args

from cryptography import x509

from autonometry.events import (
    Event,
    EvidenceCode,
    FileCode,
    FileProblem,
    GapCode,
    LineError,
    LogCheck,
    SealIssue,
    SessionVerdict,
    TrustStatus,
    VerifyResult,
)
from autonometry.merkle import IncrementalTree, leaf_hash
from autonometry.sealfile import CheckpointLine, SealFile, TimestampLine
from autonometry.sealing import event_hash, genesis_hash
from autonometry.signednote import (
    BadSignatureError,
    SignedNoteError,
    VerifierKey,
    parse_signed_checkpoint,
    trusted_signers,
)
from autonometry.spec import Rules, SpecError
from autonometry.timestamps import datetime_to_ns, seconds_to_ns, ts_to_ns
from autonometry.tsa import check_imprint, verify_token

_SessionKey = tuple[str, str]
TimestampTiming = Literal["inside", "late", "early"]


def timestamp_window(
    gen_time_ns: int, last_event_ts_ns: int, max_commit_delay_s: float, clock_tolerance_s: float
) -> TimestampTiming:
    """Place a verified token's time against the window of definitions.md 10.5: at most
    ``max_commit_delay_s`` seconds after and at most ``clock_tolerance_s`` seconds before the
    ``ts`` of the last event the checkpoint covers (both bounds included; times in integer
    nanoseconds since the Unix epoch)."""
    if gen_time_ns > last_event_ts_ns + seconds_to_ns(max_commit_delay_s):
        return "late"
    if gen_time_ns < last_event_ts_ns - seconds_to_ns(clock_tolerance_s):
        return "early"
    return "inside"


def check_sealing_codes(rules: Rules) -> None:
    """Refuse a spec whose ``[sealing]`` code lists differ from the codes this verifier reports.

    Raises:
        SpecError: one of evidence_codes, gap_codes or file_codes differs.
    """
    tables: tuple[tuple[str, Any, tuple[str, ...]], ...] = (
        ("evidence_codes", EvidenceCode, rules.sealing.evidence_codes),
        ("gap_codes", GapCode, rules.sealing.gap_codes),
        ("file_codes", FileCode, rules.sealing.file_codes),
    )
    for name, literal, codes in tables:
        if set(get_args(literal)) != set(codes):
            raise SpecError(
                f"spec {rules.spec_version}: its [sealing] {name} differ from the codes this "
                "verifier reports"
            )


# Details kept per code and session: a long tampered log still gives a short report.
_DETAILS_PER_CODE = 5


@dataclass
class _SessionWork:
    """Evidence and gaps of one session: per code, the first distinct details and a total."""

    evidence: dict[EvidenceCode, tuple[list[str], int]] = field(default_factory=dict)
    gaps: dict[GapCode, tuple[list[str], int]] = field(default_factory=dict)

    def fail(self, code: EvidenceCode, detail: str) -> None:
        _note(self.evidence, code, detail)

    def gap(self, code: GapCode, detail: str) -> None:
        _note(self.gaps, code, detail)


def _note[Code: (EvidenceCode, GapCode)](
    table: dict[Code, tuple[list[str], int]], code: Code, detail: str
) -> None:
    details, count = table.get(code, ([], 0))
    if detail not in details and len(details) < _DETAILS_PER_CODE:
        details.append(detail)
    table[code] = (details, count + 1)


def _issues[Code: (EvidenceCode, GapCode)](
    table: Mapping[Code, tuple[list[str], int]],
) -> tuple[SealIssue, ...]:
    """One issue per kept detail, sorted by code; the last one of a code says how many more
    occurrences were not listed."""
    issues = []
    for code in sorted(table):
        details, count = table[code]
        for detail in details[:-1]:
            issues.append(SealIssue(code, detail))
        more = count - len(details)
        issues.append(SealIssue(code, details[-1] + (f" (and {more} more)" if more else "")))
    return tuple(issues)


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    """A checkpoint or anchor with a verifying signature from a trusted key, and the timestamp
    records of its file that point to it and pass the imprint check."""

    key: _SessionKey
    size: int
    root: bytes
    note: str
    is_anchor: bool
    where: str
    timestamps: tuple[TimestampLine, ...]

    @property
    def what(self) -> str:
        return f"{'anchor' if self.is_anchor else 'checkpoint'} of size {self.size} ({self.where})"


def _note_sha256(note: str) -> str:
    return hashlib.sha256(note.encode("utf-8")).hexdigest()


@dataclass
class _Collector:
    """Reads every signed checkpoint and timestamp record of the given files once."""

    trusted_keys: Sequence[VerifierKey]
    work: dict[_SessionKey, _SessionWork] = field(default_factory=dict)
    problems: list[FileProblem] = field(default_factory=list)
    checkpoints: list[_Checkpoint] = field(default_factory=list)

    def session(self, key: _SessionKey) -> _SessionWork:
        return self.work.setdefault(key, _SessionWork())

    def add_file(self, seal_file: SealFile, is_anchor: bool) -> None:
        notes = [r for r in seal_file.records if isinstance(r, CheckpointLine)]
        known = {_note_sha256(r.note) for r in notes}
        stamps: dict[str, list[TimestampLine]] = {}
        for record in seal_file.records:
            if not isinstance(record, TimestampLine):
                continue
            if record.note_sha256 in known:
                stamps.setdefault(record.note_sha256, []).append(record)
            else:
                self._problem(
                    seal_file,
                    record.line,
                    "orphan_timestamp",
                    "timestamp record matches no checkpoint record in this file",
                )
        for record in notes:
            self._add_checkpoint(
                seal_file, record, stamps.get(_note_sha256(record.note), []), is_anchor
            )

    def _problem(self, seal_file: SealFile, line: int, code: FileCode, detail: str) -> None:
        self.problems.append(FileProblem(seal_file.name, line, code, detail))

    def _add_checkpoint(
        self,
        seal_file: SealFile,
        record: CheckpointLine,
        stamps: Iterable[TimestampLine],
        is_anchor: bool,
    ) -> None:
        where = f"{seal_file.name}:{record.line}"
        try:
            body, note = parse_signed_checkpoint(record.note)
        except SignedNoteError as exc:
            self._problem(seal_file, record.line, "malformed_checkpoint", str(exc))
            return
        key = (body.robot_id, body.session_id)
        work = self.session(key)
        kind = "anchor" if is_anchor else "checkpoint"
        passed = []
        for stamp in stamps:
            if reason := check_imprint(stamp.tsr, stamp.note_sha256):
                work.fail("bad_timestamp_token", f"{seal_file.name}:{stamp.line}: {reason}")
            else:
                passed.append(stamp)
        try:
            signers = trusted_signers(note, self.trusted_keys)
        except BadSignatureError as exc:
            work.fail("bad_signature", f"{kind} of size {body.size} ({where}): {exc}")
            return
        if not signers:
            work.gap("untrusted_checkpoint", f"{kind} of size {body.size} ({where}) is ignored")
            return
        self.checkpoints.append(
            _Checkpoint(key, body.size, body.root, record.note, is_anchor, where, tuple(passed))
        )


def _is_sealed(event: Event) -> bool:
    return event.get("hash") is not None and event.get("prev_hash") is not None


def _check_chain(
    events: tuple[Event, ...], hashes: Sequence[str], genesis: str, work: _SessionWork
) -> bool:
    """Apply definitions.md 10.1 and the chain codes of 10.6; return True when every event is
    sealed. A session with no sealed event is only ``unsealed``: chain rules do not apply."""
    sealed = [_is_sealed(event) for event in events]
    if not any(sealed):
        work.gap("unsealed", "no event is sealed")
        return False
    expected_prev = genesis
    previous_seq: int | None = None
    for event, digest, is_sealed in zip(events, hashes, sealed, strict=True):
        seq = event["seq"]
        if previous_seq is not None and seq != previous_seq + 1:
            work.fail("chain_break", f"seq jumps from {previous_seq} to {seq}")
        if not is_sealed:
            work.fail("chain_break", f"seq {seq} is not sealed")
        else:
            if event.get("prev_hash") != expected_prev:
                work.fail("chain_break", f"prev_hash of seq {seq} does not follow the chain")
            if event.get("hash") != digest:
                work.fail("hash_mismatch", f"stored hash of seq {seq} differs from its content")
        expected_prev = digest
        previous_seq = seq
    return all(sealed)


def _roots_at(hashes: Sequence[str], sizes: set[int]) -> dict[int, bytes]:
    """Merkle root at each of ``sizes`` (each at most ``len(hashes)``) in one pass; size 0 is
    the root of the empty tree, SHA-256 of nothing (RFC 9162 section 2.1.1)."""
    roots = {0: hashlib.sha256(b"").digest()} if 0 in sizes else {}
    tree = IncrementalTree()
    for index, digest in enumerate(hashes):
        tree.append(leaf_hash(bytes.fromhex(digest)))
        if index + 1 in sizes:
            roots[index + 1] = tree.root()
    return roots


@dataclass(frozen=True, slots=True)
class _Context:
    trusted_tsa_roots: Sequence[x509.Certificate]
    rules: Rules


def _anchored_by_timestamp(
    checkpoint: _Checkpoint, events: tuple[Event, ...], ctx: _Context, work: _SessionWork
) -> bool:
    """Apply definitions.md 10.5(a) to the timestamp records of one checkpoint."""
    last_ts_ns = ts_to_ns(events[checkpoint.size - 1]["ts"])
    anchored = False
    for stamp in checkpoint.timestamps:
        result = verify_token(stamp.tsr, checkpoint.note.encode("utf-8"), ctx.trusted_tsa_roots)
        if not result.ok or result.gen_time is None:
            work.gap("not_anchored", f"{checkpoint.what}: {result.detail}")
            continue
        timing = timestamp_window(
            datetime_to_ns(result.gen_time),
            last_ts_ns,
            ctx.rules.sealing.max_commit_delay_s,
            ctx.rules.sealing.clock_tolerance_s,
        )
        if timing == "late":
            work.gap("late_anchor", f"{checkpoint.what}: token later than max_commit_delay_s")
        elif timing == "early":
            work.gap("clock_skew", f"{checkpoint.what}: token earlier than clock_tolerance_s")
        else:
            anchored = True
    return anchored


def _status(
    work: _SessionWork, events: tuple[Event, ...], usable: bool, best_anchored: int
) -> tuple[TrustStatus, int | None]:
    if work.evidence:
        return "failed", None
    if not usable or best_anchored == 0:
        return "unverified", None
    sealed_through = events[best_anchored - 1]["seq"]
    return ("verified" if best_anchored == len(events) else "partial"), sealed_through


def _verify_session(
    key: _SessionKey,
    events: tuple[Event, ...],
    checkpoints: Sequence[_Checkpoint],
    ctx: _Context,
    work: _SessionWork,
) -> SessionVerdict:
    robot_id, session_id = key
    hashes = [event_hash(cast(Mapping[str, Any], event)) for event in events]
    fully_sealed = _check_chain(
        events, hashes, genesis_hash(ctx.rules.chain_genesis_prefix, robot_id, session_id), work
    )
    reconstructed = any(
        cast(Mapping[str, Any], event).get("x_ts_reconstructed") is True for event in events
    )
    if reconstructed:
        work.gap("reconstructed", "events carry x_ts_reconstructed: true; capped at L1")

    roots = _roots_at(hashes, {c.size for c in checkpoints if c.size <= len(events)})
    valid: list[_Checkpoint] = []
    for checkpoint in checkpoints:
        if checkpoint.size > len(events):
            work.fail(
                "missing_events",
                f"{checkpoint.what} covers more than the {len(events)} event(s) in the log",
            )
        elif roots[checkpoint.size] != checkpoint.root:
            work.fail("root_mismatch", f"{checkpoint.what}: root differs from the events")
        else:
            valid.append(checkpoint)

    anchors = {(c.size, c.root) for c in valid if c.is_anchor}
    anchored_sizes = [c.size for c in valid if c.is_anchor]
    for checkpoint in valid:
        if checkpoint.is_anchor or checkpoint.size == 0:
            continue
        by_timestamp = _anchored_by_timestamp(checkpoint, events, ctx, work)
        if by_timestamp or (checkpoint.size, checkpoint.root) in anchors:
            anchored_sizes.append(checkpoint.size)
        else:
            work.gap("not_anchored", f"{checkpoint.what} has no anchor")

    best_valid = max((c.size for c in valid), default=0)
    if fully_sealed and best_valid < len(events):
        work.gap(
            "no_checkpoint", f"events from seq {events[best_valid]['seq']} on have no checkpoint"
        )
    usable = fully_sealed and not reconstructed
    status, sealed_through = _status(work, events, usable, max(anchored_sizes, default=0))
    return SessionVerdict(
        robot_id, session_id, status, sealed_through, _issues(work.evidence), _issues(work.gaps)
    )


def _bounded(
    problems: Sequence[FileProblem], list_max_per_code: int
) -> tuple[tuple[FileProblem, ...], Mapping[str, int]]:
    ordered = sorted(problems, key=lambda p: (p.file, p.line, p.code))
    counts: Counter[str] = Counter()
    listed = []
    for problem in ordered:
        counts[problem.code] += 1
        if counts[problem.code] <= list_max_per_code:
            listed.append(problem)
    return tuple(listed), MappingProxyType(dict(sorted(counts.items())))


def verify_log(  # noqa: PLR0913, PLR0917 -- matches the verifier's spec signature exactly
    log_check: LogCheck,
    checkpoint_files: Sequence[SealFile],
    anchor_files: Sequence[SealFile],
    trusted_keys: Sequence[VerifierKey],
    trusted_tsa_roots: Sequence[x509.Certificate],
    rules: Rules,
) -> VerifyResult:
    """Verify every session of ``log_check`` (definitions.md section 10.6).

    ``checkpoint_files`` are the logs' ``.checkpoints.jsonl`` files and ``anchor_files`` copies
    kept outside the log. A checkpoint's own signed origin, not its file, decides which session it
    covers, and a session named by a checkpoint or anchor but absent from the log is reported
    too. Timestamp records are tied to checkpoint records of their own file.

    Raises:
        SpecError: the spec's ``[sealing]`` code lists differ from the codes reported here.
    """
    check_sealing_codes(rules)
    collector = _Collector(trusted_keys)
    for seal_file in checkpoint_files:
        collector.add_file(seal_file, is_anchor=False)
    for seal_file in anchor_files:
        collector.add_file(seal_file, is_anchor=True)

    events_by_key = {(s.robot_id, s.session_id): s.events for s in log_check.sessions}
    keys = sorted(set(events_by_key) | set(collector.work))
    ctx = _Context(trusted_tsa_roots, rules)
    verdicts = tuple(
        _verify_session(
            key,
            events_by_key.get(key, ()),
            [c for c in collector.checkpoints if c.key == key],
            ctx,
            collector.session(key),
        )
        for key in keys
    )

    seal_files = (*checkpoint_files, *anchor_files)
    rejected: list[LineError] = [*log_check.errors]
    rejected_counts: Counter[str] = Counter(log_check.counts.errors_by_code)
    for seal_file in seal_files:
        rejected.extend(seal_file.errors)
        rejected_counts.update(seal_file.errors_by_code)
    problems, problem_counts = _bounded(collector.problems, rules.errors.list_max_per_code)
    return VerifyResult(
        log_check.spec_version,
        verdicts,
        tuple(sorted(rejected, key=lambda e: (e.file, e.line))),
        MappingProxyType(dict(sorted(rejected_counts.items()))),
        problems,
        problem_counts,
    )
