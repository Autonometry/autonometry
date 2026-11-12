"""Typed results of reading an event log (definitions.md sections 1 to 3 and 8).

Everything here is immutable data; :mod:`autonometry.reader` produces it and
:mod:`autonometry.checks` adds the loader flags.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, Required, TypedDict

# Codes of rules.toml [errors]. The reader refuses a spec whose table differs.
ErrorCode = Literal[
    "invalid_utf8",
    "invalid_unicode",
    "line_too_long",
    "invalid_json",
    "not_an_object",
    "duplicate_key",
    "non_finite_number",
    "integer_out_of_range",
    "schema_violation",
    "invalid_timestamp",
    "duplicate_seq",
]

# The rules.toml [flags] codes checked when a log is read. The others need the state timeline and
# the registries, and are raised by autonometry.timeline (TimelineFlagCode).
FlagCode = Literal[
    "no_session_start",
    "no_session_end",
    "missing_initial_state",
    "undeclared_scope",
    "seq_gap",
    "ts_regression",
    "time_gap",
    "nested_task",
    "task_end_without_start",
    "task_not_closed",
    "distance_regression",
    "ts_reconstructed",
    "repeated_session_marker",
    "event_outside_session",
    "clock_unsynced",
    "attribution_inferred",
]

# The rules.toml [flags] codes raised while building a timeline (definitions.md 3, 5, 6.8, 6.10, 8).
TimelineFlagCode = Literal[
    "help_reason_recoded",
    "idle_in_task",
    "undeclared_device",
    "registry_mismatch",
    "registry_missing",
    "unknown_task_definition",
    "unplanned",
    "approval_wait_recoded",
]

# Codes of rules.toml [sealing] evidence_codes and gap_codes (definitions.md section 10.6).
EvidenceCode = Literal[
    "hash_mismatch",
    "chain_break",
    "bad_signature",
    "root_mismatch",
    "missing_events",
    "bad_timestamp_token",
]

GapCode = Literal[
    "unsealed",
    "no_checkpoint",
    "untrusted_checkpoint",
    "not_anchored",
    "late_anchor",
    "clock_skew",
    "reconstructed",
]

# Codes of rules.toml [sealing] file_codes: records that cannot be tied to one session.
FileCode = Literal["malformed_checkpoint", "orphan_timestamp"]

TrustStatus = Literal["verified", "partial", "unverified", "failed"]
TrustLevel = Literal["L1", "L2"]


class Event(TypedDict, total=False):
    """One validated event, exactly as read (definitions.md 8). For typing only.

    The schema is the model: this lists its fields so code can be type-checked, and a test keeps
    it equal to the schema's ``properties``. ``x_`` extension fields are present at run time too.
    """

    robot_id: Required[str]
    session_id: Required[str]
    task_id: Required[str | None]
    seq: Required[int]
    ts: Required[str]
    type: Required[str]
    scope: NotRequired[str]
    state: NotRequired[str]
    reason: NotRequired[str | None]
    actor_id: NotRequired[str | None]
    device_id: NotRequired[str | None]
    plan_item_id: NotRequired[str | None]
    related_seq: NotRequired[int | None]
    dwell_s: NotRequired[float]
    ttl_s: NotRequired[float]
    distance_m: NotRequired[float]
    task_definition_id: NotRequired[str]
    outcome: NotRequired[str]
    level: NotRequired[str]
    n_supervisors: NotRequired[int]
    scopes: NotRequired[list[str]]
    initial_states: NotRequired[dict[str, str]]
    scope_weights: NotRequired[dict[str, float]]
    heartbeat_interval_s: NotRequired[float]
    hint_policy: NotRequired[str]
    task_registry_hash: NotRequired[str]
    plan_hash: NotRequired[str]
    device_registry_hash: NotRequired[str]
    simulated: NotRequired[bool]
    unobserved: NotRequired[list[str]]
    clock_source: NotRequired[str]
    clock_max_error_s: NotRequired[float]
    prev_hash: NotRequired[str | None]
    hash: NotRequired[str]
    note: NotRequired[str]


@dataclass(frozen=True, slots=True)
class LineError:
    """A rejected line: file as given, 1-based line number, error code, message without contents."""

    file: str
    line: int
    code: ErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class Flag:
    """A loader flag on one session; ``seq`` is the event where the problem shows."""

    robot_id: str
    session_id: str
    seq: int
    code: FlagCode | TimelineFlagCode
    detail: str


@dataclass(frozen=True, slots=True)
class Session:
    """The valid events of one ``(robot_id, session_id)``, ordered by ``seq``; never empty.

    ``file`` names, as given, the file that holds the session's first ``session_start`` (else its
    first event read); its folder is the session folder (definitions.md 8, Registries). ``None``
    when the session was not read from a named file.
    """

    robot_id: str
    session_id: str
    events: tuple[Event, ...]
    file: str | None = None


@dataclass(frozen=True, slots=True)
class Counts:
    """Totals of one read. ``lines`` counts every line, blank and rejected ones included.

    Every count is exact, including ``rejected_lines`` and ``errors_by_code`` when the result lists
    only some of the rejected lines (definitions.md 8, Rejected lines).
    """

    files: int
    lines: int
    blank_lines: int
    events: int
    rejected_lines: int
    sessions: int
    errors_by_code: Mapping[str, int]
    flags_by_code: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class LogCheck:
    """Result of reading and checking a log: sessions, rejected lines, flags and counts.

    ``errors`` lists at most ``list_max_per_code`` rejected lines of each code, the first ones
    read; ``counts`` has the exact totals. Sessions are sorted by ``(robot_id, session_id)``,
    errors by ``(file, line)`` and flags by ``(robot_id, session_id, seq, code, detail)``.
    """

    spec_version: str
    sessions: tuple[Session, ...]
    errors: tuple[LineError, ...]
    flags: tuple[Flag, ...]
    counts: Counts

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary in stable order. Event contents are left out."""
        return {
            "spec_version": self.spec_version,
            "counts": {
                "files": self.counts.files,
                "lines": self.counts.lines,
                "blank_lines": self.counts.blank_lines,
                "events": self.counts.events,
                "rejected_lines": self.counts.rejected_lines,
                "sessions": self.counts.sessions,
                "flags": len(self.flags),
                "errors_by_code": dict(self.counts.errors_by_code),
                "flags_by_code": dict(self.counts.flags_by_code),
            },
            "sessions": [
                {
                    "robot_id": session.robot_id,
                    "session_id": session.session_id,
                    "events": len(session.events),
                    "first_seq": session.events[0]["seq"],
                    "last_seq": session.events[-1]["seq"],
                }
                for session in self.sessions
            ],
            "errors": [
                {"file": e.file, "line": e.line, "code": e.code, "message": e.message}
                for e in self.errors
            ],
            "flags": [
                {
                    "robot_id": f.robot_id,
                    "session_id": f.session_id,
                    "seq": f.seq,
                    "code": f.code,
                    "detail": f.detail,
                }
                for f in self.flags
            ],
        }


@dataclass(frozen=True, slots=True)
class SealIssue:
    """One evidence or gap code found while verifying a session (definitions.md section 10.6)."""

    code: EvidenceCode | GapCode
    detail: str


@dataclass(frozen=True, slots=True)
class SessionVerdict:
    """The trust status of one session (definitions.md section 10.6).

    ``sealed_through_seq`` is the last ``seq`` proven trust level L2, or ``None`` when no span of
    the session is L2. Evidence codes made the status ``failed``; gaps never raise the level.
    """

    robot_id: str
    session_id: str
    status: TrustStatus
    sealed_through_seq: int | None
    evidence: tuple[SealIssue, ...]
    gaps: tuple[SealIssue, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "session_id": self.session_id,
            "status": self.status,
            "sealed_through_seq": self.sealed_through_seq,
            "evidence": [{"code": i.code, "detail": i.detail} for i in self.evidence],
            "gaps": [{"code": i.code, "detail": i.detail} for i in self.gaps],
        }


@dataclass(frozen=True, slots=True)
class FileProblem:
    """A checkpoint or timestamp record that cannot be tied to one session, reported for its file
    (definitions.md section 10.6, File problems). ``line`` is 1-based."""

    file: str
    line: int
    code: FileCode
    detail: str


def meets_level(status: TrustStatus, require: TrustLevel) -> bool:
    """True when a session with ``status`` meets trust level ``require``: L1 for any status but
    ``failed``, L2 only when the session is ``verified`` up to its last event (definitions.md
    10.6)."""
    return status != "failed" if require == "L1" else status == "verified"


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Result of verifying one or more logs (definitions.md section 10.6).

    ``sessions`` are sorted by ``(robot_id, session_id)``. ``rejected_lines`` holds the listed
    rejected lines of every file read (logs, checkpoint and anchor files), sorted by
    ``(file, line)``, and ``file_problems`` the listed file problems; both list at most
    ``list_max_per_code`` per code, while the ``*_by_code`` totals are exact.
    """

    spec_version: str
    sessions: tuple[SessionVerdict, ...]
    rejected_lines: tuple[LineError, ...]
    rejected_by_code: Mapping[str, int]
    file_problems: tuple[FileProblem, ...]
    file_problems_by_code: Mapping[str, int]

    def reasons(self, require: TrustLevel) -> list[str]:
        """Why the result is not verified at level ``require``; empty when it is. Rejected lines
        and file problems count whatever the sessions' statuses."""
        reasons: list[str] = []
        if rejected := sum(self.rejected_by_code.values()):
            reasons.append(f"{rejected} rejected line(s)")
        if problems := sum(self.file_problems_by_code.values()):
            reasons.append(f"{problems} file problem(s)")
        if not self.sessions:
            reasons.append("no session to verify")
        for verdict in self.sessions:
            if not meets_level(verdict.status, require):
                below = "" if verdict.status == "failed" else f", below {require}"
                reasons.append(f"{verdict.robot_id}/{verdict.session_id} {verdict.status}{below}")
        return reasons

    def verified(self, require: TrustLevel) -> bool:
        """True when every session meets ``require`` and nothing was rejected or malformed."""
        return not self.reasons(require)

    def to_json(self, require: TrustLevel) -> dict[str, Any]:
        """Return a JSON-serialisable summary in stable order. Event contents are left out."""
        return {
            "spec_version": self.spec_version,
            "require": require,
            "verified": self.verified(require),
            "reasons": self.reasons(require),
            "counts": {
                "sessions": len(self.sessions),
                "rejected_lines": sum(self.rejected_by_code.values()),
                "rejected_lines_by_code": dict(self.rejected_by_code),
                "file_problems": sum(self.file_problems_by_code.values()),
                "file_problems_by_code": dict(self.file_problems_by_code),
            },
            "rejected_lines": [
                {"file": e.file, "line": e.line, "code": e.code, "message": e.message}
                for e in self.rejected_lines
            ],
            "file_problems": [
                {"file": p.file, "line": p.line, "code": p.code, "detail": p.detail}
                for p in self.file_problems
            ],
            "sessions": [session.to_json() for session in self.sessions],
        }
