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
    assert _code('{"note": "\\ud800"}') == "invalid_unicode"
    assert _code('["ok", ["\\udfff"]]') == "invalid_unicode"
    assert _code('{"\\udc00": 1}') == "invalid_unicode"
    assert strictjson.parse('"\\ud83d\\ude00"') == "\N{GRINNING FACE}"


_NONCHARACTERS = ["\ufdd0", "\ufdef", "\ufffe", "\uffff", "\U0001fffe", "\U0010ffff"]


@pytest.mark.parametrize("char", _NONCHARACTERS)
def test_noncharacter_rejected__defs_8(char: str) -> None:
    escaped = json.dumps(char)
    assert "\\u" in escaped
    texts = [f'{{"note": "{char}"}}', f'{{"{char}": 1}}', f'["a", {escaped}]', f"{{{escaped}: 1}}"]
    for text in texts:
        with pytest.raises(StrictJsonError, match="noncharacter") as info:
            strictjson.parse(text)
        assert info.value.code == "invalid_unicode"


@pytest.mark.parametrize("char", ["\ufdcf", "\ufdf0", "\ufffd", "\U0001fffd", "\u00e9"])
def test_neighbours_of_noncharacters_are_accepted__defs_8(char: str) -> None:
    assert strictjson.parse(json.dumps({char: char})) == {char: char}
    assert strictjson.parse(json.dumps({char: char}, ensure_ascii=False)) == {char: char}


def test_ascii_text_without_escapes_is_not_scanned(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(value: Any) -> None:
        raise AssertionError("scanned")

    monkeypatch.setattr(strictjson, "_bad_code_point", fail)
    assert strictjson.parse('{"note": "plain"}') == {"note": "plain"}


@pytest.mark.parametrize("text", ["", "{", "{'a': 1}", "[1,]", "1 2", "\ufeff{}"])
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
        "invalid_unicode",
    }


def _forbidden(char: str) -> bool:
    """A surrogate or a noncharacter, which I-JSON strings must not hold."""
    code = ord(char)
    return 0xD800 <= code <= 0xDFFF or 0xFDD0 <= code <= 0xFDEF or code & 0xFFFE == 0xFFFE


# Code points drawn as integers (cheap, unlike a st.characters() table); forbidden: U+FFFD.
_i_json_text = st.text(
    st.integers(0, 0x10FFFF).map(chr).map(lambda c: chr(0xFFFD) if _forbidden(c) else c)
)


_json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER)
    | st.floats(allow_nan=False, allow_infinity=False)
    | _i_json_text,
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(_i_json_text, inner, max_size=4),
    max_leaves=20,
)


@given(_json_values, st.booleans())
def test_i_json_values_round_trip__defs_8(value: Any, ensure_ascii: bool) -> None:
    assert strictjson.parse(json.dumps(value, ensure_ascii=ensure_ascii)) == value


@given(st.characters(), st.booleans())
def test_every_code_point_is_accepted_or_invalid_unicode__defs_8(
    char: str, ensure_ascii: bool
) -> None:
    text = json.dumps(char, ensure_ascii=ensure_ascii)
    if _forbidden(char):
        assert _code(text) == "invalid_unicode"
    else:
        assert strictjson.parse(text) == char


@given(_json_values, st.integers(min_value=0, max_value=50))
def test_truncated_json_never_crashes(value: Any, cut: int) -> None:
    text = json.dumps(value)
    with contextlib.suppress(StrictJsonError):
        strictjson.parse(text[: len(text) - cut])
