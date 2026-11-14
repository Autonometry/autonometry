"""Trust and exclusions of a report (definitions.md 10.6, 11.2, 11.4, 12). Pure standard
library."""

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from autonometry.events import FileProblem, LineError, TrustStatus, VerifyResult

ReportStatus = Literal["verified", "not_verified", "failed"]
Level = Literal["L1", "L2", "L3"]
Key = tuple[str, str]

#: What the report says about trust level L3 (11.2, 12).
L3_TEXT: Final = "two witnesses agree: the robot's log and a teleoperation station's"
L3_UNAVAILABLE: Final = "not available: no station log was given"
#: What L3 cannot show (12.7); printed beside every L3 claim.
WITNESS_STATEMENT: Final = (
    "L3 shows that a remote operator who used the witnessed station appears in the robot's "
    "log, to within tau. It says nothing about an operator using a client the station does not "
    "witness (another station, a laptop, a direct radio link, a maintenance interface): only a "
    "witness of the control channel itself, the network witness of trust level L4, can expose "
    "that. Nor does it show that the station's logger is honest: the two keys must belong to "
    "separate software identities."
)
NO_KEY_NOTE: Final = (
    "No trusted verifier key was given: no checkpoint can be trusted, so no session can reach L2."
)
NO_TSA_ROOT_NOTE: Final = (
    "No trusted TSA root was given: a timestamp token cannot anchor a checkpoint; "
    "only an anchor file can."
)
ABSENT_REASON: Final = "absent from the logs: named only by a checkpoint or anchor"
NO_START_REASON: Final = "no session_start: no observed time and no group"
STATION_REASON: Final = (
    "a station log (role station): it witnesses a robot, it is not one; give it with --station"
)
_ORDER: Final[Mapping[Level, int]] = {"L1": 1, "L2": 2, "L3": 3}


def session_level(status: TrustStatus | None, l3: bool = False) -> Level:
    """L3 when the witnesses agree (12.6, which needs a verified session), L2 for another
    session verified up to its last event, else L1 (11.2)."""
    if status != "verified":
        return "L1"
    return "L3" if l3 else "L2"


def report_status(result: VerifyResult, stations: VerifyResult | None = None) -> ReportStatus:
    """``failed`` when any session, the robot's or a station's, is failed; ``verified`` when
    every session is verified and no line was rejected and no file has a problem (10.6, 11.2);
    else ``not_verified``."""
    results = [result] if stations is None else [result, stations]
    if any(v.status == "failed" for r in results for v in r.sessions):
        return "failed"
    return "verified" if all(r.verified("L2") for r in results) else "not_verified"


def report_level(
    result: VerifyResult, l3: Collection[Key] = (), skip: Collection[Key] = ()
) -> Level:
    """The lowest level of the report's robot sessions (those not in ``skip``); ``l3`` holds
    the sessions the witnesses make L3. L1 without a session (11.2)."""
    levels = [
        session_level(v.status, (v.robot_id, v.session_id) in l3)
        for v in result.sessions
        if (v.robot_id, v.session_id) not in skip
    ]
    return min(levels, key=_ORDER.__getitem__) if levels else "L1"


def _rejected(lines: Sequence[LineError], by_code: Mapping[str, int]) -> dict[str, Any]:
    return {
        "count": sum(by_code.values()),
        "by_code": dict(by_code),
        "listed": [
            {"file": e.file, "line": e.line, "code": e.code, "message": e.message} for e in lines
        ],
    }


def _problems(problems: Sequence[FileProblem], by_code: Mapping[str, int]) -> dict[str, Any]:
    return {
        "count": sum(by_code.values()),
        "by_code": dict(by_code),
        "listed": [
            {"file": p.file, "line": p.line, "code": p.code, "detail": p.detail} for p in problems
        ],
    }


def trust_section(  # noqa: PLR0913, PLR0917 -- every input of 11.2
    result: VerifyResult,
    trusted_keys: int,
    trusted_tsa_roots: int,
    anchor_files: int,
    stations: VerifyResult | None = None,
    l3: Collection[Key] = (),
    skip: Collection[Key] = (),
) -> dict[str, Any]:
    """Each session's status and level with its evidence and gaps, rejected lines and file
    problems of every file read, the report's status and level, and notes on missing trust
    roots (11.2). With station logs (``stations``), their sessions, rejected lines and file
    problems too, and the robot sessions in ``l3`` are L3 (12.6); ``skip`` holds sessions that
    are no robot's, which never set the level."""
    notes = []
    if trusted_keys == 0:
        notes.append(NO_KEY_NOTE)
    if trusted_tsa_roots == 0:
        notes.append(NO_TSA_ROOT_NOTE)
    reasons = result.reasons("L2")
    if stations is not None:
        reasons += [f"station {reason}" for reason in stations.reasons("L2")]
    section: dict[str, Any] = {
        "status": report_status(result, stations),
        "level": report_level(result, l3, skip),
        "reasons": reasons,
        "levels": {
            "L1": "self-reported",
            "L2": "sealed log",
            "L3": L3_UNAVAILABLE if stations is None else L3_TEXT,
        },
        "trusted_keys": trusted_keys,
        "trusted_tsa_roots": trusted_tsa_roots,
        "anchor_files": anchor_files,
        "notes": notes,
        "sessions": [
            verdict.to_json()
            | {"level": session_level(verdict.status, (verdict.robot_id, verdict.session_id) in l3)}
            for verdict in result.sessions
        ],
        "rejected_lines": _rejected(result.rejected_lines, result.rejected_by_code),
        "file_problems": _problems(result.file_problems, result.file_problems_by_code),
        "stations": None,
    }
    if stations is not None:
        section["stations"] = {
            "sessions": [
                verdict.to_json() | {"level": session_level(verdict.status)}
                for verdict in stations.sessions
            ],
            "rejected_lines": _rejected(stations.rejected_lines, stations.rejected_by_code),
            "file_problems": _problems(stations.file_problems, stations.file_problems_by_code),
        }
    return section


def exclusions(
    keys: Iterable[Key],
    in_logs: Iterable[Key],
    in_metrics: Iterable[Key],
    registry_problems: Mapping[Key, Sequence[str]],
    stations: Collection[Key] = (),
) -> list[dict[str, Any]]:
    """Every session left out of the metrics and why (11.4), sorted by robot and session:
    absent from the logs, a registry that does not match its hash, a station log given as a
    robot log (``stations``, 12.1), or no ``session_start`` (which is the reason left when a
    session in the logs has no other)."""
    present = set(in_logs)
    measured = set(in_metrics)
    out = []
    for key in sorted(set(keys) | present):
        if key in measured:
            continue
        reasons: list[str] = []
        if key not in present:
            reasons.append(ABSENT_REASON)
        if key in stations:
            reasons.append(STATION_REASON)
        reasons += registry_problems.get(key, ())
        if not reasons:
            reasons.append(NO_START_REASON)
        out.append({"robot_id": key[0], "session_id": key[1], "reasons": reasons})
    return out
