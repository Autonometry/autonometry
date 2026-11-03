"""Reading and writing ``<name>.checkpoints.jsonl`` and anchor files (definitions.md v0.2
section 10.4).

Same line rules as event logs (section 8): a length limit, strict UTF-8, I-JSON, each line
checked against ``checkpoint.schema.json``. A malformed line is rejected and returned as a
:class:`~autonometry.events.LineError`, never raised, so the verifier can fail closed on it
instead of crashing (definitions.md 10.6: "MUST report a log with rejected lines as not
verified").
"""

import base64
import binascii
import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

import jsonschema_rs

from autonometry import strictjson
from autonometry.events import ErrorCode, LineError
from autonometry.reader import iter_lines
from autonometry.spec import compile_schema, load_checkpoint_schema

_JSON_SPACE = b" \t\r"
_LOG_SUFFIX = ".jsonl"
_CHECKPOINTS_SUFFIX = ".checkpoints.jsonl"


def checkpoints_path(log_path: Path) -> Path:
    """Return the checkpoint file of a log ``<name>.jsonl``: ``<name>.checkpoints.jsonl``
    (definitions.md section 10.4)."""
    return log_path.with_name(log_path.name.removesuffix(_LOG_SUFFIX) + _CHECKPOINTS_SUFFIX)


@functools.cache
def _validator(spec_version: str) -> jsonschema_rs.Draft202012Validator:
    return compile_schema(spec_version, load_checkpoint_schema(spec_version))


@dataclass(frozen=True, slots=True)
class CheckpointLine:
    """A ``{"type": "checkpoint", ...}`` line: one signed C2SP checkpoint (definitions.md 10.4)."""

    note: str


@dataclass(frozen=True, slots=True)
class TimestampLine:
    """A ``{"type": "timestamp", ...}`` line: one RFC 3161 token over a checkpoint's note
    (definitions.md 10.4). ``tsr`` is the decoded DER ``TimeStampResp``."""

    note_sha256: str
    tsr: bytes


SealRecord = CheckpointLine | TimestampLine


def encode_checkpoint_line(note: str) -> bytes:
    """Return one ``checkpoint`` line, ready to append to a ``.checkpoints.jsonl`` file."""
    return json.dumps({"type": "checkpoint", "note": note}, sort_keys=True).encode("utf-8") + b"\n"


def encode_timestamp_line(note_sha256: str, tsr: bytes) -> bytes:
    """Return one ``timestamp`` line, ready to append to a ``.checkpoints.jsonl`` file."""
    record = {
        "type": "timestamp",
        "note_sha256": note_sha256,
        "tsr": base64.b64encode(tsr).decode("ascii"),
    }
    return json.dumps(record, sort_keys=True).encode("utf-8") + b"\n"


_RULE_MESSAGES = {
    "required": "missing a required field",
    "additionalProperties": "has a field the schema does not define",
    "type": "has the wrong type",
    "pattern": "does not match the expected pattern",
    "oneOf": "is not a checkpoint or timestamp record",
}


def _describe(error: jsonschema_rs.ValidationError) -> str:
    """Describe a schema error by location and rule, without quoting any record content."""
    parts = [str(part) for part in error.instance_path]
    where = "/" + "/".join(parts) if parts else "line"
    keyword = str(error.schema_path[-1]) if error.schema_path else ""
    message = _RULE_MESSAGES.get(keyword, f"fails the schema rule {keyword}")
    return f"{where}: {message}"


def _record(value: dict[str, Any], name: str, number: int) -> SealRecord | LineError:
    if value.get("type") == "checkpoint":
        return CheckpointLine(cast(str, value["note"]))
    try:
        tsr = base64.b64decode(cast(str, value["tsr"]), validate=True)
    except (ValueError, binascii.Error):
        return LineError(name, number, "schema_violation", "/tsr: is not valid base64")
    return TimestampLine(cast(str, value["note_sha256"]), tsr)


def _read_one(name: str, number: int, content: bytes, spec_version: str) -> SealRecord | LineError:
    try:
        value = strictjson.parse(content.decode("utf-8"))
    except UnicodeDecodeError as exc:
        return LineError(name, number, "invalid_utf8", f"byte {exc.start + 1} is not valid UTF-8")
    except strictjson.StrictJsonError as exc:
        return LineError(name, number, cast(ErrorCode, exc.code), exc.message)
    if not isinstance(value, dict):
        return LineError(name, number, "not_an_object", "line is not a JSON object")
    validator = _validator(spec_version)
    if not validator.is_valid(value):
        problem = min(
            validator.iter_errors(value), key=lambda e: (len(e.schema_path), str(e.schema_path))
        )
        return LineError(name, number, "schema_violation", _describe(problem))
    return _record(value, name, number)


def read_seal_file(
    name: str, stream: IO[bytes], max_line_bytes: int, spec_version: str
) -> tuple[list[SealRecord], list[LineError]]:
    """Read one checkpoint or anchor file. Blank lines are skipped; a malformed line is rejected,
    never raised."""
    records: list[SealRecord] = []
    errors: list[LineError] = []
    for number, content in enumerate(iter_lines(stream, max_line_bytes), start=1):
        if content is None:
            errors.append(
                LineError(
                    name, number, "line_too_long", f"line is longer than {max_line_bytes} bytes"
                )
            )
            continue
        if not content.strip(_JSON_SPACE):
            continue
        parsed = _read_one(name, number, content, spec_version)
        if isinstance(parsed, LineError):
            errors.append(parsed)
        else:
            records.append(parsed)
    return records, errors
