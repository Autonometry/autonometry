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

# The rules.toml [flags] codes checked when a log is read. help_reason_recoded, idle_in_task and
# undeclared_device need the state timeline and are added there.
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

TrustStatus = Literal["verified", "partial", "unverified", "failed"]


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
    code: FlagCode
    detail: str


@dataclass(frozen=True, slots=True)
class Session:
    """The valid events of one ``(robot_id, session_id)``, ordered by ``seq``; never empty."""

    robot_id: str
    session_id: str
    events: tuple[Event, ...]


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
class VerifyResult:
    """Result of verifying one or more logs: one verdict per session, sorted by
    ``(robot_id, session_id)`` (definitions.md section 10.6).
    """

    log_check: LogCheck
    sessions: tuple[SessionVerdict, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "log": self.log_check.to_json(),
            "sessions": [session.to_json() for session in self.sessions],
        }
