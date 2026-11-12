"""Event timestamps (definitions.md section 3, field ``ts``) as exact integers."""

import re
from datetime import UTC, datetime, timedelta

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NS_PER_S = 1_000_000_000
_FRACTION_DIGITS = 9
_TS = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)\.(\d{3,9})Z", re.ASCII)


def ts_to_ns(ts: str) -> int:
    """Return an RFC 3339 UTC timestamp as integer nanoseconds since 1970-01-01T00:00:00Z.

    Accepts ``YYYY-MM-DDTHH:MM:SS.fffZ`` with 3 to 9 fraction digits, as the event schema does.
    Integers keep comparisons exact; floats would lose nanoseconds.

    Raises:
        ValueError: the text is not in that form or names an impossible instant, such as
            February 30, hour 24, year 0 or a leap second (``:60``), which ``datetime`` cannot
            represent; definitions.md section 3 makes leap seconds invalid.
    """
    match = _TS.fullmatch(ts)
    if match is None:
        raise ValueError("timestamp is not YYYY-MM-DDTHH:MM:SS.fffZ")
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    instant = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    seconds = (instant - _EPOCH) // timedelta(seconds=1)
    return seconds * _NS_PER_S + int(match.group(7).ljust(_FRACTION_DIGITS, "0"))


def seconds_to_ns(seconds: float) -> int:
    """Return a duration in seconds as integer nanoseconds, rounded to the nearest."""
    return round(seconds * _NS_PER_S)


def ns_to_seconds(ns: int) -> float:
    """Return integer nanoseconds as float seconds (for display and reports only)."""
    return ns / _NS_PER_S


def datetime_to_ns(instant: datetime) -> int:
    """Return a timezone-aware ``datetime`` as integer nanoseconds since the Unix epoch.

    Exact: ``datetime`` has microsecond resolution, so the conversion never rounds.
    """
    return ((instant.astimezone(UTC) - _EPOCH) // timedelta(microseconds=1)) * 1000


def ns_to_ts(ns: int) -> str:
    """Return integer nanoseconds since the Unix epoch as an RFC 3339 UTC timestamp with nine
    fraction digits, the form a recorder writes in ``ts`` (definitions.md section 3)."""
    seconds, fraction = divmod(ns, _NS_PER_S)
    instant = _EPOCH + timedelta(seconds=seconds)
    return f"{instant:%Y-%m-%dT%H:%M:%S}.{fraction:09d}Z"
