import contextlib
import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from autonometry import strictjson
from autonometry.strictjson import MAX_SAFE_INTEGER, StrictJsonError


def _code(text: str) -> str:
    with pytest.raises(StrictJsonError) as info:
        strictjson.parse(text)
    return info.value.code


def test_plain_json_parses() -> None:
    assert strictjson.parse('{"a": [1, 2.5, "x", null, true], "b": {}}') == {
        "a": [1, 2.5, "x", None, True],
        "b": {},
    }


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":1,"\\u0061":2}', '[{"k":1,"k":1}]'])
def test_duplicate_keys_are_rejected__defs_8(text: str) -> None:
    assert _code(text) == "duplicate_key"


@pytest.mark.parametrize("text", ["NaN", "[Infinity]", '{"x": -Infinity}', "1e400", "-1e309"])
def test_non_finite_numbers_are_rejected__defs_8(text: str) -> None:
    assert _code(text) == "non_finite_number"


def test_safe_integer_bounds__defs_8() -> None:
    assert strictjson.parse(str(MAX_SAFE_INTEGER)) == MAX_SAFE_INTEGER
    assert strictjson.parse(str(-MAX_SAFE_INTEGER)) == -MAX_SAFE_INTEGER
    assert _code(str(MAX_SAFE_INTEGER + 1)) == "integer_out_of_range"
    assert _code(str(-MAX_SAFE_INTEGER - 1)) == "integer_out_of_range"


def test_huge_integer_is_rejected_without_conversion__defs_8() -> None:
    # 60 000 digits is beyond Python's int-from-string limit; it must not raise ValueError.
    assert _code("9" * 60_000) == "integer_out_of_range"


def test_lone_surrogate_escape_is_rejected__defs_8() -> None:
    assert _code('{"note": "\\ud800"}') == "invalid_utf8"
    assert _code('["ok", ["\\udfff"]]') == "invalid_utf8"
    assert strictjson.parse('"\\ud83d\\ude00"') == "\N{GRINNING FACE}"


@pytest.mark.parametrize("text", ["", "{", "{'a': 1}", "[1,]", "1 2", "﻿{}"])
def test_syntax_errors_are_invalid_json__defs_8(text: str) -> None:
    assert _code(text) == "invalid_json"


def test_deep_nesting_is_invalid_json_not_a_crash__defs_8() -> None:
    assert _code("[" * 100_000 + "]" * 100_000) == "invalid_json"


def test_error_message_does_not_quote_the_text() -> None:
    with pytest.raises(StrictJsonError) as info:
        strictjson.parse('{"actor_id": "op_secret", "actor_id": "op_secret"}')
    assert "op_secret" not in info.value.message


@given(st.text(max_size=200))
def test_any_text_parses_or_is_rejected_with_a_code__defs_8(text: str) -> None:
    try:
        strictjson.parse(text)
    except StrictJsonError as exc:
        code = exc.code
    else:
        return
    assert code in {
        "invalid_json",
        "duplicate_key",
        "non_finite_number",
        "integer_out_of_range",
        "invalid_utf8",
    }


_json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=20,
)


@given(_json_values)
def test_i_json_values_round_trip__defs_8(value: Any) -> None:
    assert strictjson.parse(json.dumps(value)) == value


@given(_json_values, st.integers(min_value=0, max_value=50))
def test_truncated_json_never_crashes(value: Any, cut: int) -> None:
    text = json.dumps(value)
    with contextlib.suppress(StrictJsonError):
        strictjson.parse(text[: len(text) - cut])
