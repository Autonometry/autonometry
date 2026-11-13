"""Trust and exclusions of a report (definitions.md 10.6, 11.2, 11.4). Pure standard library."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from autonometry.events import TrustStatus, VerifyResult

ReportStatus = Literal["verified", "not_verified", "failed"]
Level = Literal["L1", "L2"]

#: What the report says about trust levels it cannot reach yet (11.2).
L3_TEXT: Final = "not available yet"
NO_KEY_NOTE: Final = (
    "No trusted verifier key was given: no checkpoint can be trusted, so no session can reach L2."
)
NO_TSA_ROOT_NOTE: Final = (
    "No trusted TSA root was given: a timestamp token cannot anchor a checkpoint; "
    "only an anchor file can."
)
ABSENT_REASON: Final = "absent from the logs: named only by a checkpoint or anchor"
NO_START_REASON: Final = "no session_start: no observed time and no group"


def session_level(status: TrustStatus) -> Level:
    """L2 only for a session verified up to its last event (11.2)."""
    return "L2" if status == "verified" else "L1"


def report_status(result: VerifyResult) -> ReportStatus:
    """``failed`` when any session is failed; ``verified`` when every session is verified and
    no line was rejected and no file has a problem (10.6, 11.2); else ``not_verified``."""
    if any(v.status == "failed" for v in result.sessions):
        return "failed"
    return "verified" if result.verified("L2") else "not_verified"


def report_level(result: VerifyResult) -> Level:
    """The lowest level of the report's sessions; L1 without a session (11.2)."""
    levels = [session_level(v.status) for v in result.sessions]
    return "L2" if levels and all(level == "L2" for level in levels) else "L1"


def trust_section(
    result: VerifyResult, trusted_keys: int, trusted_tsa_roots: int, anchor_files: int
) -> dict[str, Any]:
    """Each session's status and level with its evidence and gaps, rejected lines and file
    problems of every file read, the report's status and level, and notes on missing trust
    roots (11.2)."""
    notes = []
    if trusted_keys == 0:
        notes.append(NO_KEY_NOTE)
    if trusted_tsa_roots == 0:
        notes.append(NO_TSA_ROOT_NOTE)
    return {
        "status": report_status(result),
        "level": report_level(result),
        "reasons": result.reasons("L2"),
        "levels": {"L1": "self-reported", "L2": "sealed log", "L3": L3_TEXT},
        "trusted_keys": trusted_keys,
        "trusted_tsa_roots": trusted_tsa_roots,
        "anchor_files": anchor_files,
        "notes": notes,
        "sessions": [
            verdict.to_json() | {"level": session_level(verdict.status)}
            for verdict in result.sessions
        ],
        "rejected_lines": {
            "count": sum(result.rejected_by_code.values()),
            "by_code": dict(result.rejected_by_code),
            "listed": [
                {"file": e.file, "line": e.line, "code": e.code, "message": e.message}
                for e in result.rejected_lines
            ],
        },
        "file_problems": {
            "count": sum(result.file_problems_by_code.values()),
            "by_code": dict(result.file_problems_by_code),
            "listed": [
                {"file": p.file, "line": p.line, "code": p.code, "detail": p.detail}
                for p in result.file_problems
            ],
        },
    }


def exclusions(
    keys: Iterable[tuple[str, str]],
    in_logs: Iterable[tuple[str, str]],
    in_metrics: Iterable[tuple[str, str]],
    registry_problems: Mapping[tuple[str, str], Sequence[str]],
) -> list[dict[str, Any]]:
    """Every session left out of the metrics and why (11.4), sorted by robot and session:
    absent from the logs, a registry that does not match its hash, or no ``session_start``
    (which is the reason left when a session in the logs has neither metrics nor a registry
    problem)."""
    present = set(in_logs)
    measured = set(in_metrics)
    out = []
    for key in sorted(set(keys) | present):
        if key in measured:
            continue
        reasons: list[str] = []
        if key not in present:
            reasons.append(ABSENT_REASON)
        reasons += registry_problems.get(key, ())
        if not reasons:
            reasons.append(NO_START_REASON)
        out.append({"robot_id": key[0], "session_id": key[1], "reasons": reasons})
    return out
