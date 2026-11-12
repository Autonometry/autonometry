"""Tests for autonometry.sealfile (definitions.md v0.2 section 10.4)."""

import io
from pathlib import Path

from autonometry.sealfile import (
    CheckpointLine,
    SealFile,
    TimestampLine,
    checkpoints_path,
    encode_checkpoint_line,
    encode_timestamp_line,
    read_seal_file,
)

SPEC = "0.2"
MAX_LINE = 65536
LIST_MAX = 100


def _read(data: bytes, list_max: int = LIST_MAX) -> SealFile:
    return read_seal_file("f.jsonl", io.BytesIO(data), MAX_LINE, list_max, SPEC)


def _codes(seal_file: SealFile) -> list[str]:
    return [e.code for e in seal_file.errors]


def test_checkpoints_path_replaces_jsonl_suffix__defs_10_4() -> None:
    assert checkpoints_path(Path("robot.jsonl")) == Path("robot.checkpoints.jsonl")
    assert checkpoints_path(Path("a/b/robot.jsonl")) == Path("a/b/robot.checkpoints.jsonl")


def test_checkpoint_line_round_trips_with_its_line_number__defs_10_4() -> None:
    note = "note text\n\n— k deadbeef=\n"
    seal_file = _read(b"\n" + encode_checkpoint_line(note))
    assert seal_file.name == "f.jsonl"
    assert seal_file.records == (CheckpointLine(2, note),)
    assert seal_file.errors == ()
    assert seal_file.errors_by_code == {}


def test_timestamp_line_round_trips__defs_10_4() -> None:
    seal_file = _read(encode_timestamp_line("a" * 64, b"\x00\x01der"))
    assert seal_file.records == (TimestampLine(1, "a" * 64, b"\x00\x01der"),)


def test_blank_lines_are_skipped() -> None:
    assert _read(b"\n   \n") == SealFile("f.jsonl", (), (), {})


def test_invalid_json_is_rejected_not_raised() -> None:
    assert _codes(_read(b"not json\n")) == ["invalid_json"]


def test_non_object_is_rejected() -> None:
    assert _codes(_read(b"[1, 2]\n")) == ["not_an_object"]


def test_unknown_record_type_is_a_schema_violation() -> None:
    seal_file = _read(b'{"type": "mystery"}\n')
    assert _codes(seal_file) == ["schema_violation"]
    assert seal_file.errors[0].message == "line: is not a checkpoint or timestamp record"


def test_missing_required_field_is_a_schema_violation() -> None:
    assert _codes(_read(b'{"type": "checkpoint"}\n')) == ["schema_violation"]


def test_additional_property_is_a_schema_violation() -> None:
    assert _codes(_read(b'{"type": "checkpoint", "note": "x\\n", "extra": 1}\n')) == [
        "schema_violation"
    ]


def test_bad_base64_tsr_is_rejected() -> None:
    line = b'{"type": "timestamp", "note_sha256": "' + b"a" * 64 + b'", "tsr": "not-base64!!"}\n'
    seal_file = _read(line)
    assert _codes(seal_file) == ["schema_violation"]
    assert seal_file.errors[0].message == "/tsr: is not valid base64"


def test_oversize_line_is_rejected() -> None:
    assert _codes(_read(encode_checkpoint_line("x" * (MAX_LINE + 10)))) == ["line_too_long"]


def test_invalid_utf8_is_rejected() -> None:
    assert _codes(_read(b'{"type": "checkpoint", "note": "\xff"}\n')) == ["invalid_utf8"]


def test_rejected_lines_are_listed_up_to_the_limit_and_counted_exactly__defs_8() -> None:
    seal_file = _read(b"x\n" * 5, list_max=2)
    assert [e.line for e in seal_file.errors] == [1, 2]
    assert seal_file.errors_by_code == {"invalid_json": 5}
