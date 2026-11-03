"""Strict JSON for event lines: I-JSON (RFC 7493), which RFC 8785 canonical hashing needs.

Rejected: syntax errors, duplicate keys, ``NaN`` and ``Infinity``, numbers that overflow a 64-bit
float, integers outside +/-(2**53 - 1), and, in keys or values, lone UTF-16 surrogates (written as
``\\u`` escapes; they cannot be encoded as UTF-8, so the event could not be hashed) and Unicode
noncharacters (definitions.md section 8, JSON). Error messages describe where the problem is, never
the text itself.
"""

import json
import math
import re
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1
_MAX_SAFE_DIGITS = len(str(MAX_SAFE_INTEGER))
_SURROGATES = ("\ud800", "\udfff")
# Noncharacters: U+FDD0 to U+FDEF and the last two code points of each of the 17 planes
# (RFC 7493 section 2.1; Unicode section 23.7).
_NONCHARACTERS = "\ufdd0-\ufdef" + "".join(
    chr(plane << 16 | 0xFFFE) + chr(plane << 16 | 0xFFFF) for plane in range(17)
)
_BAD_CODE_POINT = re.compile(f"[{_SURROGATES[0]}-{_SURROGATES[1]}{_NONCHARACTERS}]")


class StrictJsonError(ValueError):
    """The text is not acceptable I-JSON; ``code`` is a ``rules.toml`` ``[errors]`` code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise StrictJsonError("duplicate_key", "an object has the same key twice")
    return value


def _integer(text: str) -> int:
    digits = text.removeprefix("-")
    # Checking the length first keeps a 64 KiB run of digits from being converted at all.
    if len(digits) > _MAX_SAFE_DIGITS or int(digits) > MAX_SAFE_INTEGER:
        raise StrictJsonError("integer_out_of_range", "an integer is outside +/-(2^53 - 1)")
    return int(text)


def _float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise StrictJsonError("non_finite_number", "a number is too large for a 64-bit float")
    return value


def _constant(name: str) -> float:
    raise StrictJsonError("non_finite_number", f"{name} is not a JSON number")


def _bad_code_point(value: Any) -> str | None:
    """Return the first lone surrogate or noncharacter in any key or string value, else None."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if found := _BAD_CODE_POINT.search(item):
                return found.group()
        elif isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return None


_DECODER = json.JSONDecoder(
    object_pairs_hook=_object, parse_int=_integer, parse_float=_float, parse_constant=_constant
)


def parse(text: str) -> Any:
    """Parse one JSON text under the I-JSON rules (RFC 7493 sections 2.1 to 2.3).

    Raises:
        StrictJsonError: with code ``invalid_json``, ``duplicate_key``, ``non_finite_number``,
            ``integer_out_of_range`` or ``invalid_unicode``.
    """
    try:
        value = _DECODER.decode(text)
    except json.JSONDecodeError as exc:
        raise StrictJsonError("invalid_json", f"{exc.msg} at column {exc.colno}") from None
    except RecursionError:
        raise StrictJsonError("invalid_json", "values are nested too deeply") from None
    # Only a \u escape or non-ASCII text can hold a bad code point, so ASCII logs skip the scan.
    if "\\u" in text or not text.isascii():
        found = _bad_code_point(value)
        if found is not None and _SURROGATES[0] <= found <= _SURROGATES[1]:
            raise StrictJsonError("invalid_unicode", "a string holds a lone UTF-16 surrogate")
        if found is not None:
            raise StrictJsonError("invalid_unicode", "a string holds a Unicode noncharacter")
    return value
