"""Strict JSON for event lines: I-JSON (RFC 7493), which RFC 8785 canonical hashing needs.

Rejected: syntax errors, duplicate keys, ``NaN`` and ``Infinity``, numbers that overflow a 64-bit
float, integers outside +/-(2**53 - 1), and lone UTF-16 surrogates written as ``\\u`` escapes
(they cannot be encoded as UTF-8, so the event could not be hashed). Error messages describe where
the problem is, never the text itself.
"""

import json
import math
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1
_MAX_SAFE_DIGITS = len(str(MAX_SAFE_INTEGER))


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


def _has_lone_surrogate(value: Any) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


_DECODER = json.JSONDecoder(
    object_pairs_hook=_object, parse_int=_integer, parse_float=_float, parse_constant=_constant
)


def parse(text: str) -> Any:
    """Parse one JSON text under the I-JSON rules (RFC 7493 sections 2.1 to 2.3).

    Raises:
        StrictJsonError: with code ``invalid_json``, ``duplicate_key``, ``non_finite_number``,
            ``integer_out_of_range`` or ``invalid_utf8``.
    """
    try:
        value = _DECODER.decode(text)
    except json.JSONDecodeError as exc:
        raise StrictJsonError("invalid_json", f"{exc.msg} at column {exc.colno}") from None
    except RecursionError:
        raise StrictJsonError("invalid_json", "values are nested too deeply") from None
    if "\\u" in text and _has_lone_surrogate(value):
        raise StrictJsonError("invalid_utf8", "a \\u escape is a lone UTF-16 surrogate")
    return value
