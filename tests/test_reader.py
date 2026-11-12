import io
import json
import shutil
import tracemalloc
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator  # reference implementation, tests only

from autonometry import reader, spec
from autonometry.events import Event, LogCheck
from autonometry.reader import LogReadError, iter_lines, read_logs, read_streams
from autonometry.strictjson import MAX_SAFE_INTEGER

GOLDEN = Path(__file__).parent / "golden"
REPO_SPEC = Path(__file__).resolve().parents[1] / "spec"
SPEC = "0.3"
ZERO_HASH = "0" * 64
BOM = b"\xef\xbb\xbf"
LIMIT = spec.load_rules(SPEC).errors.max_line_bytes


def _ts(second: float) -> str:
    """Timestamp ``second`` seconds after 2026-10-30T09:00:00Z, in milliseconds."""
    moment = datetime(2026, 10, 30, 9) + timedelta(milliseconds=round(second * 1000))
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _event(
    seq: int, second: float = 0.0, type_: str = "heartbeat", **fields: Any
) -> dict[str, Any]:
    event = {
        "robot_id": "r01",
        "session_id": "s01",
        "task_id": None,
        "seq": seq,
        "ts": _ts(second),
        "type": type_,
    }
    if type_ == "heartbeat":
        event |= {"clock_source": "ntp", "clock_max_error_s": 0.001}
    return event | fields


def _start(seq: int = 0, second: float = 0.0, **fields: Any) -> dict[str, Any]:
    return (
        _event(
            seq,
            second,
            "session_start",
            scopes=["whole_robot"],
            initial_states={"whole_robot": "idle"},
            heartbeat_interval_s=1.0,
            hint_policy="hint_as_shared",
            task_registry_hash=ZERO_HASH,
            plan_hash=ZERO_HASH,
            simulated=False,
        )
        | fields
    )


def _jsonl(*items: dict[str, Any] | bytes) -> bytes:
    return b"".join(
        (item if isinstance(item, bytes) else json.dumps(item).encode()) + b"\n" for item in items
    )


def _read(data: bytes) -> LogCheck:
    return read_streams([("log.jsonl", io.BytesIO(data))], SPEC)


def _expected_lines(data: bytes, limit: int) -> list[bytes | None]:
    """Reference splitter for iter_lines: LF ends a line, a CR before it is dropped."""
    parts = data.removeprefix(BOM).split(b"\n")
    if parts[-1] == b"":
        parts.pop()
    lines = [part.removesuffix(b"\r") for part in parts]
    return [line if len(line) <= limit else None for line in lines]


@pytest.fixture
def fresh_spec_cache() -> Iterator[None]:
    reader._load_spec.cache_clear()
    yield
    reader._load_spec.cache_clear()


@pytest.fixture
def spec_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_spec_cache: None) -> Path:
    copy = tmp_path / "spec"
    shutil.copytree(REPO_SPEC, copy)
    monkeypatch.setattr(spec, "spec_root", lambda: copy)
    return copy / "v0.3"


# --- lines -------------------------------------------------------------------------------------


def test_lf_and_crlf_endings__defs_8() -> None:
    lines = list(iter_lines(io.BytesIO(b"a\nb\r\nc\r\n\nlast"), 10))
    assert lines == [b"a", b"b", b"c", b"", b"last"]


def test_bom_is_removed_only_at_the_start__defs_8() -> None:
    lines = list(iter_lines(io.BytesIO(BOM + b"a\n" + BOM + b"b\n"), 10))
    assert lines == [b"a", BOM + b"b"]


def test_line_limit_applies_to_content_without_ending__defs_8() -> None:
    data = b"abcd\nabcd\r\nabcde\n" + BOM + b"x\nabcd"
    assert list(iter_lines(io.BytesIO(data), 4)) == [b"abcd", b"abcd", None, BOM + b"x", b"abcd"]
    assert list(iter_lines(io.BytesIO(BOM + b"abcd\r\n"), 4)) == [b"abcd"]


class _RecordingStream(io.BytesIO):
    """A stream that remembers the largest piece any read returned."""

    largest = 0

    def readline(self, size: int | None = -1, /) -> bytes:
        piece = super().readline(size)
        self.largest = max(self.largest, len(piece))
        return piece


def test_long_line_is_never_held_whole__defs_8() -> None:
    stream = _RecordingStream(b"x" * 1_000_000 + b"\nok\n")
    assert list(iter_lines(stream, 100)) == [None, b"ok"]
    assert stream.largest <= 105


@given(st.binary(max_size=300), st.integers(min_value=1, max_value=20))
def test_iter_lines_matches_reference_splitter__defs_8(data: bytes, limit: int) -> None:
    assert list(iter_lines(io.BytesIO(data), limit)) == _expected_lines(data, limit)


# --- golden fixtures ---------------------------------------------------------------------------

FIXTURES = sorted(
    p for p in GOLDEN.glob("*/*.jsonl") if p.parent.name != "sealed"
)  # tests/golden/sealed/ is a raw input for M2 sealing tests, not a validate fixture


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_golden_fixture__defs_8(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(path.parent)
    result = read_logs([path.name], SPEC)
    expected = path.with_name(f"{path.stem}.expected.json").read_text(encoding="utf-8")
    assert json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n" == expected
    kind = path.parent.name
    assert set(result.counts.errors_by_code) == ({path.stem} if kind == "errors" else set())
    assert set(result.counts.flags_by_code) == ({path.stem} if kind == "flags" else set())


def test_every_code_has_a_golden_fixture__defs_8() -> None:
    rules = spec.load_rules(SPEC)
    assert {p.stem for p in GOLDEN.glob("errors/*.jsonl")} == set(rules.errors.codes)
    timeline_flags = {"help_reason_recoded", "idle_in_task", "undeclared_device"}
    assert {p.stem for p in GOLDEN.glob("flags/*.jsonl")} == set(rules.flags.codes) - timeline_flags


def test_valid_multi_robot_log_is_grouped_and_ordered__defs_1() -> None:
    result = read_logs([GOLDEN / "valid" / "multi_robot.jsonl"], SPEC)
    assert not result.errors
    assert not result.flags
    keys = [(s.robot_id, s.session_id) for s in result.sessions]
    assert keys == [("r01", "s01"), ("r01", "s02"), ("r02", "s01")]
    for session in result.sessions:
        seqs = [event["seq"] for event in session.events]
        assert seqs == sorted(seqs)
        assert {e["robot_id"] for e in session.events} == {session.robot_id}


# --- reader rules ------------------------------------------------------------------------------


def test_rejected_line_leaves_a_seq_gap__defs_8() -> None:
    data = _jsonl(_start(), b'{"seq": 1, "broken"', _event(2, 1), _event(3, 2, "session_end"))
    result = _read(data)
    assert [(e.line, e.code) for e in result.errors] == [(2, "invalid_json")]
    assert [(f.seq, f.code) for f in result.flags] == [(2, "seq_gap")]


def test_later_duplicate_seq_is_rejected_in_file_order__defs_8() -> None:
    first = _jsonl(_start(), _event(1, 1, distance_m=1.0))
    second = _jsonl(_event(1, 1, distance_m=2.0), _event(2, 2, "session_end"))
    result = read_streams([("a.jsonl", io.BytesIO(first)), ("b.jsonl", io.BytesIO(second))], SPEC)
    assert [(e.file, e.line, e.code) for e in result.errors] == [("b.jsonl", 1, "duplicate_seq")]
    assert result.sessions[0].events[1].get("distance_m") == 1.0


def test_same_seq_in_another_session_is_accepted__defs_1() -> None:
    result = _read(_jsonl(_start(), _start(session_id="s02"), _event(0, robot_id="r02")))
    assert not result.errors
    assert result.counts.sessions == 3


def test_errors_are_sorted_by_file_then_line() -> None:
    bad = b"[]\n" * 3
    result = read_streams([("b.jsonl", io.BytesIO(bad)), ("a.jsonl", io.BytesIO(bad))], SPEC)
    assert [(e.file, e.line) for e in result.errors] == [
        ("a.jsonl", 1),
        ("a.jsonl", 2),
        ("a.jsonl", 3),
        ("b.jsonl", 1),
        ("b.jsonl", 2),
        ("b.jsonl", 3),
    ]


def test_line_at_the_limit_is_accepted_and_one_byte_more_is_not__defs_8() -> None:
    event = json.dumps(_event(1, 1, x_pad="")).encode()
    padded = event.replace(b'"x_pad": ""', b'"x_pad": "' + b"x" * (LIMIT - len(event)) + b'"')
    assert len(padded) == LIMIT
    longer = padded.replace(b'"x_pad": "', b'"x_pad": "x')
    result = _read(_jsonl(_start(), padded, longer, _event(2, 2, "session_end")))
    assert [(e.line, e.code) for e in result.errors] == [(3, "line_too_long")]
    assert result.counts.events == 3


def test_ts_without_z_or_with_newline_is_a_schema_violation__defs_3() -> None:
    lines = [_event(1) | {"ts": "2026-10-30T09:00:00.000Z\n"}, _event(1) | {"ts": "2026-10-30T09"}]
    result = _read(_jsonl(*lines))
    assert [e.code for e in result.errors] == ["schema_violation", "schema_violation"]


def test_event_typed_dict_matches_the_schema__defs_8() -> None:
    schema = spec.load_schema(SPEC)
    assert set(Event.__annotations__) == set(schema["properties"])
    assert Event.__required_keys__ == frozenset(schema["required"])


def test_messages_never_quote_event_values__defs_8() -> None:
    pseudonym = "op_pseudonym_name"
    lines = [
        _event(1, actor_id=pseudonym, x_extra=pseudonym, **{pseudonym: 1}),
        _event(1, 0, "transition", scope=pseudonym + "!", state="ai", actor_id=pseudonym),
        _event(1, 0, "transition", scope="whole_robot", state=pseudonym, actor_id=pseudonym),
        _start(initial_states={pseudonym: pseudonym}),
        _event(1) | {"ts": pseudonym},
        _event(1) | {"robot_id": 7, "session_id": [pseudonym]},
        {"seq": pseudonym, pseudonym: pseudonym},
    ]
    result = _read(_jsonl(*lines))
    assert len(result.errors) == len(lines)
    for error in result.errors:
        assert pseudonym not in error.message


def test_schema_errors_name_path_and_rule__defs_8() -> None:
    result = _read(
        _jsonl(
            _start(initial_states={"left_arm": "zz"}),
            _event(1, note="x" * 2001),
            _start(scopes=["a", "a"]),
            _event(1, task_id=5),
            _event(1, 0, "approval")
            | {"scope": "whole_robot", "actor_id": "op", "reason": "safety"},
        )
    )
    assert [e.message for e in result.errors] == [
        '/initial_states/*: must be one of "ai", "human_local", "human_remote", "shared", '
        '"scripted", "safety_stop", "idle", "unknown"',
        "/note: must be a string of length <= 2000",
        "/scopes: must not contain the same item twice",
        "/task_id: must be of type string or null",
        '/reason: must be one of "ai_requested_help", "planned"',
    ]


def test_other_schema_rules_are_named__defs_8() -> None:
    result = _read(_jsonl(_event(1, 0, "transition", scope="whole_robot", state="ai", reason=5)))
    assert [e.message for e in result.errors] == ["/reason: fails the schema rule oneOf"]


def test_schema_values_are_found_through_local_references__defs_8() -> None:
    schema = spec.load_schema(SPEC)
    pattern = schema["$defs"]["ts"]["pattern"]
    assert reader._schema_value(schema, ["properties", "ts", "$ref", "pattern"]) == pattern
    assert reader._schema_value(schema, ["$defs", "ts", "pattern"]) == pattern
    assert reader._schema_value(schema, ["allOf", 99, "then"]) is None
    assert reader._schema_value(schema, ["properties", "nope"]) is None
    assert reader._schema_value(schema, ["required", "x"]) is None


def test_counts_add_up_and_are_read_only__defs_8() -> None:
    result = _read(_jsonl(_start(), b"", b"[]", _event(1, 1)) + b"\n")
    counts = result.counts
    assert (counts.lines, counts.blank_lines, counts.rejected_lines, counts.events) == (5, 2, 1, 2)
    assert counts.errors_by_code == {"not_an_object": 1}
    with pytest.raises(TypeError):
        counts.errors_by_code["x"] = 1  # type: ignore[index]


def test_spec_version_is_normalised() -> None:
    assert read_streams([], "v0.3").spec_version == "0.3"


def test_unreadable_path_is_a_log_read_error(tmp_path: Path) -> None:
    with pytest.raises(LogReadError, match="cannot read"):
        read_logs([tmp_path / "missing.jsonl"], SPEC)
    with pytest.raises(LogReadError, match="cannot read"):
        read_logs([tmp_path], SPEC)


def test_unknown_spec_version_is_a_spec_error() -> None:
    with pytest.raises(spec.SpecError, match="unknown spec version"):
        read_streams([], "9.9")


def test_spec_without_a_reader_error_code_is_refused__defs_8(spec_copy: Path) -> None:
    text = (spec_copy / "rules.toml").read_text(encoding="utf-8")
    (spec_copy / "rules.toml").write_text(text.replace(', "duplicate_seq"]', "]"), "utf-8")
    with pytest.raises(spec.SpecError, match=r"\[errors\] codes differ"):
        read_streams([], SPEC)


def test_spec_without_a_reader_flag_code_is_refused__defs_8(spec_copy: Path) -> None:
    text = (spec_copy / "rules.toml").read_text(encoding="utf-8")
    (spec_copy / "rules.toml").write_text(text.replace('"seq_gap", ', ""), "utf-8")
    with pytest.raises(spec.SpecError, match=r"\[flags\] codes lack"):
        read_streams([], SPEC)


def test_inconsistent_spec_is_refused(spec_copy: Path) -> None:
    text = (spec_copy / "rules.toml").read_text(encoding="utf-8")
    (spec_copy / "rules.toml").write_text(
        text.replace('spec_version = "0.3"', 'spec_version = "0.4"'), "utf-8"
    )
    with pytest.raises(spec.SpecError, match="differs from folder"):
        read_streams([], SPEC)


def test_schema_with_a_remote_reference_is_refused_without_fetching(spec_copy: Path) -> None:
    path = spec_copy / "event.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["properties"]["note"] = {"$ref": "https://example.invalid/note.json"}
    path.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(spec.SpecError, match="not fetched"):
        read_streams([], SPEC)


def test_loaded_spec_is_cached_and_read_only() -> None:
    first = reader._load_spec(SPEC)
    assert reader._load_spec(SPEC) is first
    with pytest.raises(TypeError):
        first.schema["type"] = "array"  # type: ignore[index]


# --- identifiers, integers and listing limits ---------------------------------------------------

ID_PATTERN = spec.load_schema(SPEC)["$defs"]["id"]["pattern"]
SCOPE_PATTERN = spec.load_schema(SPEC)["$defs"]["scope"]["pattern"]


@pytest.mark.parametrize(
    "fields",
    [
        {"robot_id": "r\x1b[2J\x07"},
        {"session_id": ".s01"},
        {"robot_id": ""},
        {"robot_id": "r" * 129},
        {"robot_id": None},
        {"task_id": "t 1"},
        {"actor_id": "op\u00e9"},
        {"device_id": "-vr"},
        {"type": "transition", "scope": "a" * 65, "state": "ai"},
    ],
)
def test_identifier_outside_the_safe_set_is_rejected__defs_8(fields: dict[str, Any]) -> None:
    result = _read(_jsonl(_event(1) | fields))
    assert [e.code for e in result.errors] == ["schema_violation"]


def test_identifiers_at_the_limits_are_accepted__defs_8() -> None:
    longest = "r" * 128
    event = _event(1, robot_id=longest, task_id="t.1:x-2_y", actor_id="ai:policy_v3")
    start = _start(robot_id=longest, scopes=["s" * 64], initial_states={"s" * 64: "idle"})
    result = _read(_jsonl(start, event))
    assert result.errors == ()
    assert result.sessions[0].robot_id == longest


def test_identifier_errors_name_the_pattern__defs_8() -> None:
    result = _read(_jsonl(_event(1, robot_id="r\x1b"), _event(1, robot_id=None)))
    assert [e.message for e in result.errors] == [
        f"/robot_id: does not match the pattern {ID_PATTERN}",
        "/robot_id: must be of type string",
    ]


@pytest.mark.parametrize("field", ["initial_states", "scope_weights"])
def test_scope_map_keys_follow_the_scope_rule__defs_8(field: str) -> None:
    value = {"left arm": "idle"} if field == "initial_states" else {"left arm": 1.0}
    result = _read(_jsonl(_start(**{field: value})))
    assert [e.message for e in result.errors] == [
        f"/{field}: has a key that does not match the pattern {SCOPE_PATTERN}"
    ]


@pytest.mark.parametrize(
    ("field", "text"),
    [("seq", "5.0"), ("seq", "5e0"), ("related_seq", "1.0"), ("n_supervisors", "2E+0")],
)
def test_integer_literal__defs_8(field: str, text: str) -> None:
    event = _event(5, 0, "supervision", level="none", n_supervisors=1, related_seq=None)
    line = json.dumps(event | {field: 7}).replace(f'"{field}": 7', f'"{field}": {text}')
    result = _read(_jsonl(line.encode()))
    assert [(e.code, e.message) for e in result.errors] == [
        ("schema_violation", f"/{field}: must be written as an integer")
    ]


def test_integer_fields_come_from_the_schema__defs_8() -> None:
    schema = spec.load_schema(SPEC)
    assert reader._integer_fields(schema) == ("n_supervisors", "related_seq", "seq")
    changed = json.loads(json.dumps(schema))
    changed["$defs"]["nonneg"]["type"] = "integer"
    assert "dwell_s" in reader._integer_fields(changed)


def test_errors_listed_up_to_list_max_per_code_with_exact_counts__defs_8() -> None:
    limit = spec.load_rules(SPEC).errors.list_max_per_code
    data = b"[]\n" * (limit + 50) + b"x\n" * 3
    result = _read(data)
    listed = [(e.line, e.code) for e in result.errors]
    assert listed == [(n, "not_an_object") for n in range(1, limit + 1)] + [
        (n, "invalid_json") for n in range(limit + 51, limit + 54)
    ]
    assert result.counts.rejected_lines == limit + 53
    assert result.counts.errors_by_code == {"invalid_json": 3, "not_an_object": limit + 50}


def test_ts_without_fraction_is_a_schema_violation__defs_3() -> None:
    result = _read(_jsonl(_event(1) | {"ts": "2026-10-30T09:00:00Z"}))
    assert [e.message for e in result.errors] == [
        "/ts: does not match the pattern " + spec.load_schema(SPEC)["$defs"]["ts"]["pattern"]
    ]


def test_noncharacter_rejected__defs_8() -> None:
    lines = [
        _jsonl(_event(1, note="a\ufffeb")).replace(b"\\ufffe", "\ufffe".encode()),
        _jsonl(_event(2, note="\U0010ffff")),
        _jsonl(_event(3, note="\ud800")),
        b'{"note": "\xed\xa0\x80"}\n',
    ]
    result = _read(b"".join(lines))
    assert [e.code for e in result.errors] == [
        "invalid_unicode",
        "invalid_unicode",
        "invalid_unicode",
        "invalid_utf8",
    ]


# --- property tests ----------------------------------------------------------------------------


@given(st.binary(max_size=2000))
def test_arbitrary_bytes_never_crash_and_errors_have_lines__defs_8(data: bytes) -> None:
    result = _read(data)
    counts = result.counts
    assert counts.lines == len(_expected_lines(data, LIMIT))
    assert counts.blank_lines + counts.rejected_lines + counts.events == counts.lines
    assert all(1 <= error.line <= counts.lines for error in result.errors)


VALID_LOG = (GOLDEN / "valid" / "multi_robot.jsonl").read_bytes()


@given(st.integers(min_value=0, max_value=len(VALID_LOG)))
def test_truncated_log_never_crashes__defs_8(cut: int) -> None:
    original = {
        (e["robot_id"], e["session_id"], e["seq"]): e
        for session in _read(VALID_LOG).sessions
        for e in session.events
    }
    result = _read(VALID_LOG[:cut])
    assert len(result.errors) <= 1
    assert all(error.line >= 1 for error in result.errors)
    for session in result.sessions:
        for event in session.events:
            assert original[(event["robot_id"], event["session_id"], event["seq"])] == event


def _json_values(
    integers: st.SearchStrategy[int],
    floats: st.SearchStrategy[float],
    characters: st.SearchStrategy[str],
) -> Any:
    return st.recursive(
        st.none()
        | st.booleans()
        | integers
        | floats
        | st.text(characters, max_size=10)
        | st.sampled_from(["r01", "s01", "heartbeat", "transition", "whole_robot", _ts(1)]),
        lambda inner: (
            st.lists(inner, max_size=3)
            | st.dictionaries(
                st.sampled_from(["robot_id", "session_id", "task_id", "seq", "ts", "type", "scope"])
                | st.text(characters, max_size=5),
                inner,
                max_size=7,
            )
        ),
        max_leaves=12,
    )


def _i_json_character(code: int) -> bool:
    """Not a surrogate or a noncharacter (definitions.md 8, JSON)."""
    return not (0xD800 <= code <= 0xDFFF or 0xFDD0 <= code <= 0xFDEF or code & 0xFFFE == 0xFFFE)


# Code points from U+0020 drawn as integers (cheap, unlike a st.characters() table).
_i_json_characters = st.integers(0x20, 0x10FFFF).map(
    lambda c: chr(c if _i_json_character(c) else 0xFFFD)
)
_any_json = _json_values(st.integers(), st.floats(), st.characters())
_json = _json_values(
    st.integers(-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER),
    st.floats(allow_nan=False, allow_infinity=False),
    _i_json_characters,
)


@given(st.lists(_any_json, max_size=8))
def test_random_json_lines_never_crash__defs_8(values: list[Any]) -> None:
    data = b"".join(json.dumps(value).encode() + b"\n" for value in values)
    result = _read(data)
    assert result.counts.lines == len(values)
    assert all(1 <= error.line <= len(values) for error in result.errors)


_STATES = [
    "ai",
    "human_local",
    "human_remote",
    "shared",
    "scripted",
    "safety_stop",
    "idle",
    "unknown",
]
_HUMAN = {"human_local", "human_remote", "shared"}
_REASONS = ["ai_requested_help", "human_initiated", "safety", "planned"]
_scope = st.text("abcXYZ019_.-", min_size=1, max_size=8)
_ID_FIRST = "abcXYZ019"
_id = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from(_ID_FIRST),
    st.text(_ID_FIRST + "._:-", max_size=8),
)
_nonneg = st.floats(min_value=0, max_value=1e9, allow_nan=False, allow_infinity=False)
_sha = st.binary(min_size=32, max_size=32).map(bytes.hex)


@st.composite
def _timestamp(draw: st.DrawFn) -> str:
    moment = draw(st.datetimes(min_value=datetime(1, 1, 1)))
    digits = draw(st.integers(min_value=3, max_value=9))
    fraction = draw(st.integers(min_value=0, max_value=10**digits - 1))
    m = moment
    return (
        f"{m.year:04d}-{m.month:02d}-{m.day:02d}T{m.hour:02d}:{m.minute:02d}:{m.second:02d}"
        f".{fraction:0{digits}d}Z"
    )


@st.composite
def _transition(draw: st.DrawFn) -> dict[str, Any]:
    state = draw(st.sampled_from(_STATES))
    fields: dict[str, Any] = {"scope": draw(_scope), "state": state}
    if state in _HUMAN:
        fields |= {"reason": draw(st.sampled_from(_REASONS)), "actor_id": draw(_id)}
    elif state == "safety_stop":
        fields["reason"] = "safety"
    else:
        fields |= {"reason": None, "actor_id": draw(st.none() | _id)}
    return fields


def _human_input(reasons: list[str]) -> st.SearchStrategy[dict[str, Any]]:
    return st.fixed_dictionaries(
        {"scope": _scope, "actor_id": _id},
        optional={"dwell_s": _nonneg, "device_id": _id}
        | ({"reason": st.sampled_from(reasons)} if reasons else {}),
    )


_FIELDS_BY_TYPE: dict[str, st.SearchStrategy[dict[str, Any]]] = {
    "heartbeat": st.fixed_dictionaries(
        {},
        optional={
            "clock_source": st.sampled_from(["ntp", "ptp", "gnss", "none", "unknown"]),
            "clock_max_error_s": _nonneg,
        },
    ),
    "support_start": st.fixed_dictionaries({}, optional={"scope": _scope, "device_id": _id}),
    "support_end": st.fixed_dictionaries({}, optional={"scope": _scope, "device_id": _id}),
    "session_end": st.just({}),
    "help_request": st.fixed_dictionaries({"scope": _scope}),
    "transition": _transition(),
    "approval": st.builds(
        lambda fields, reason: fields | {"reason": reason},
        _human_input([]),
        st.sampled_from(["ai_requested_help", "planned"]),
    ),
    "hint": _human_input(_REASONS),
    "task_start": st.fixed_dictionaries({"task_id": _id, "task_definition_id": _id}),
    "task_end": st.fixed_dictionaries(
        {"task_id": _id, "outcome": st.sampled_from(["success", "failure", "aborted", "unknown"])}
    ),
    "supervision": st.fixed_dictionaries(
        {
            "level": st.sampled_from(
                ["none", "remote_monitoring", "local_monitoring", "ready_to_takeover"]
            ),
            "n_supervisors": st.integers(min_value=0, max_value=MAX_SAFE_INTEGER),
        }
    ),
    "session_start": st.fixed_dictionaries(
        {
            "scopes": st.lists(_scope, min_size=1, max_size=3, unique=True),
            "initial_states": st.dictionaries(_scope, st.sampled_from(_STATES), max_size=3),
            "heartbeat_interval_s": st.floats(min_value=0.001, max_value=60),
            "hint_policy": st.sampled_from(["hint_as_shared", "hint_as_event"]),
            "task_registry_hash": _sha,
            "plan_hash": _sha,
            "simulated": st.booleans(),
        },
        optional={
            "unobserved": st.lists(
                st.sampled_from(["safety_stops", "human_inputs", "tasks"]), unique=True
            )
        },
    ),
}


@st.composite
def _type_fields(draw: st.DrawFn) -> dict[str, Any]:
    kind = draw(st.sampled_from(sorted(_FIELDS_BY_TYPE)))
    fields = {"type": kind} | draw(_FIELDS_BY_TYPE[kind])
    if fields.get("reason") == "planned":
        fields["plan_item_id"] = draw(_id)
    return fields


@st.composite
def _valid_event(draw: st.DrawFn, seq: int = 0) -> dict[str, Any]:
    event = {
        "robot_id": draw(st.sampled_from(["r01", "r02", "robot.2"])),
        "session_id": draw(st.sampled_from(["s01", "s:02"])),
        "task_id": draw(st.none() | _id),
        "seq": seq,
        "ts": draw(_timestamp()),
    }
    event |= draw(_type_fields())
    optional = {
        "distance_m": _nonneg,
        "note": st.text(_i_json_characters, max_size=20),
        "x_note": _json,
        "prev_hash": st.none() | _sha,
    }
    return event | draw(st.fixed_dictionaries({}, optional=optional))


@st.composite
def _valid_events(draw: st.DrawFn) -> list[dict[str, Any]]:
    count = draw(st.integers(min_value=0, max_value=8))
    return [draw(_valid_event(seq)) for seq in range(count)]


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


# Building whole events is slow under coverage tracing; generation speed is not under test.
_COMPOSITE = settings(suppress_health_check=[HealthCheck.too_slow])


@_COMPOSITE
@given(_valid_events(), st.booleans())
def test_valid_events_round_trip_unchanged__defs_8(
    events: list[dict[str, Any]], ascii_only: bool
) -> None:
    data = _jsonl(*[json.dumps(e, ensure_ascii=ascii_only).encode() for e in events])
    result = _read(data)
    assert result.errors == ()
    read_back = [event for session in result.sessions for event in session.events]
    key = lambda e: (e["robot_id"], e["session_id"], e["seq"])  # noqa: E731
    assert sorted(read_back, key=key) == sorted(events, key=key)


_SCHEMA = spec.load_schema(SPEC)
_PYTHON_VALIDATOR = Draft202012Validator(_SCHEMA)
_RUST_VALIDATOR = spec.compile_schema(SPEC, _SCHEMA)


@_COMPOSITE
@given(
    _valid_event(),
    st.sampled_from([*_SCHEMA["properties"], "x_ok", "other"]),
    st.none() | _json,
    st.booleans(),
)
def test_rust_and_python_validators_agree__defs_8(
    valid: dict[str, Any], field: str, value: Any, delete: bool
) -> None:
    """jsonschema-rs decides; the reference implementation must agree except for one known case:
    Python's ``re.search`` lets ``$`` match before a trailing newline, ECMA-262 does not."""
    event = dict(valid)
    if delete:
        event.pop(field, None)
    else:
        event[field] = value
    assume(not any(text.endswith("\n") for text in _strings(event)))
    assert _RUST_VALIDATOR.is_valid(event) == _PYTHON_VALIDATOR.is_valid(event)


def test_trailing_newline_is_the_known_validator_difference__defs_8() -> None:
    event = _event(1) | {"ts": "2026-10-30T09:00:00.000Z\n"}
    assert _PYTHON_VALIDATOR.is_valid(event)
    assert not _RUST_VALIDATOR.is_valid(event)


# --- memory ------------------------------------------------------------------------------------

MiB = 1024 * 1024


def test_100k_events_stream_within_a_fixed_memory_budget__defs_8(tmp_path: Path) -> None:
    path = tmp_path / "big.jsonl"
    with path.open("wb") as out:
        out.write(_jsonl(_start()))
        for seq in range(1, 100_000):
            out.write(_jsonl(_event(seq, seq / 2, distance_m=seq / 100)))
    tracemalloc.start()
    try:
        result = read_logs([path], SPEC)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.counts.events == 100_000
    assert not result.errors
    assert path.stat().st_size > 14 * MiB
    assert peak < 64 * MiB, f"peak {peak / MiB:.1f} MiB"


def test_rejected_data_does_not_grow_memory__defs_8(tmp_path: Path) -> None:
    path = tmp_path / "junk.jsonl"
    with path.open("wb") as out:
        for _ in range(16):
            out.write(b"x" * (2 * MiB) + b"\n")
        for _ in range(200):
            out.write(b'{"a": ' + b"1" * 60_000 + b"}\n")
            out.write(b"[" * 60_000 + b"\n")
    tracemalloc.start()
    try:
        result = read_logs([path], SPEC)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.counts.rejected_lines == 416
    assert path.stat().st_size > 50 * MiB
    assert peak < 2 * MiB, f"peak {peak / MiB:.1f} MiB"


def test_many_rejected_lines_do_not_grow_memory__defs_8() -> None:
    data = b"x\n1\n" * 5_000
    tracemalloc.start()
    try:
        result = _read(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.counts.errors_by_code == {"invalid_json": 5_000, "not_an_object": 5_000}
    assert len(result.errors) == 2 * spec.load_rules(SPEC).errors.list_max_per_code
    assert peak < MiB, f"peak {peak / MiB:.1f} MiB"
