"""Tests for autonometry.sealfile (definitions.md v0.2 section 10.4)."""

import base64
import io
from pathlib import Path

from autonometry.sealfile import (
    CheckpointLine,
    TimestampLine,
    checkpoints_path,
    encode_checkpoint_line,
    encode_timestamp_line,
    read_seal_file,
)

SPEC = "0.2"
MAX_LINE = 65536


def _read(data: bytes) -> tuple[list, list]:
    return read_seal_file("f.jsonl", io.BytesIO(data), MAX_LINE, SPEC)


def test_checkpoints_path_replaces_jsonl_suffix() -> None:
    assert checkpoints_path(Path("robot.jsonl")) == Path("robot.checkpoints.jsonl")
    assert checkpoints_path(Path("a/b/robot.jsonl")) == Path("a/b/robot.checkpoints.jsonl")


def test_encode_and_read_checkpoint_line_round_trips() -> None:
    line = encode_checkpoint_line("note text\n\n— k deadbeef=\n")
    records, errors = _read(line)
    assert errors == []
    assert records == [CheckpointLine("note text\n\n— k deadbeef=\n")]


def test_encode_and_read_timestamp_line_round_trips() -> None:
    line = encode_timestamp_line("a" * 64, b"der-bytes-here")
    records, errors = _read(line)
    assert errors == []
    assert records == [TimestampLine("a" * 64, b"der-bytes-here")]


def test_blank_lines_are_skipped() -> None:
    records, errors = _read(b"\n   \n")
    assert records == []
    assert errors == []


def test_invalid_json_is_rejected_not_raised() -> None:
    records, errors = _read(b"not json\n")
    assert records == []
    assert len(errors) == 1
    assert errors[0].code == "invalid_json"


def test_non_object_is_rejected() -> None:
    records, errors = _read(b"[1, 2]\n")
    assert records == []
    assert errors[0].code == "not_an_object"


def test_schema_violation_for_unknown_type() -> None:
    records, errors = _read(b'{"type": "mystery"}\n')
    assert records == []
    assert errors[0].code == "schema_violation"


def test_missing_required_field_is_rejected() -> None:
    records, errors = _read(b'{"type": "checkpoint"}\n')
    assert records == []
    assert errors[0].code == "schema_violation"


def test_bad_base64_tsr_is_rejected() -> None:
    line = b'{"type": "timestamp", "note_sha256": "' + b"a" * 64 + b'", "tsr": "not-base64!!"}\n'
    records, errors = _read(line)
    assert records == []
    assert errors[0].code == "schema_violation"
    assert "tsr" in errors[0].message


def test_oversize_line_is_rejected() -> None:
    line = encode_checkpoint_line("x" * (MAX_LINE + 10))
    records, errors = read_seal_file("f.jsonl", io.BytesIO(line), MAX_LINE, SPEC)
    assert records == []
    assert errors[0].code == "line_too_long"


def test_invalid_utf8_is_rejected() -> None:
    records, errors = _read(b'{"type": "checkpoint", "note": "\xff"}\n')
    assert records == []
    assert errors[0].code == "invalid_utf8"


def test_additional_properties_are_rejected() -> None:
    records, errors = _read(b'{"type": "checkpoint", "note": "x\\n", "extra": 1}\n')
    assert records == []
    assert errors[0].code == "schema_violation"


def test_encode_timestamp_line_base64_round_trip() -> None:
    line = encode_timestamp_line("b" * 64, b"\x00\x01\x02binary")
    decoded = base64.b64decode(line.split(b'"tsr": "')[1].split(b'"')[0])
    assert decoded == b"\x00\x01\x02binary"
