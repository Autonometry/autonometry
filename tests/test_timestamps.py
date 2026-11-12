from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from autonometry.timestamps import ns_to_seconds, ns_to_ts, seconds_to_ns, ts_to_ns


def test_epoch_and_fraction_digits__defs_3() -> None:
    assert ts_to_ns("1970-01-01T00:00:00.000Z") == 0
    assert ts_to_ns("1970-01-01T00:00:01.250Z") == 1_250_000_000
    assert ts_to_ns("1970-01-01T00:00:00.000000001Z") == 1
    assert ts_to_ns("2026-10-30T09:15:02.250Z") == 1_793_351_702_250_000_000


@pytest.mark.parametrize(
    "ts",
    [
        "2026-02-30T00:00:00.000Z",
        "2025-02-29T00:00:00.000Z",
        "2026-13-01T00:00:00.000Z",
        "2026-10-30T24:00:00.000Z",
        "2026-10-30T23:60:00.000Z",
        "0000-01-01T00:00:00.000Z",
    ],
)
def test_impossible_instants_are_rejected__defs_3(ts: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        ts_to_ns(ts)


def test_leap_second_is_invalid__defs_3() -> None:
    with pytest.raises(ValueError, match=r"."):
        ts_to_ns("2026-12-31T23:59:60.000Z")


@pytest.mark.parametrize(
    "ts",
    [
        "2026-10-30T09:00:00Z",
        "2026-10-30T09:00:00.Z",
        "2026-10-30T09:00:00.5Z",
        "2026-10-30T09:00:00.25Z",
        "2026-10-30T09:00:00.0000000001Z",
    ],
)
def test_ts_needs_fraction__defs_3(ts: str) -> None:
    with pytest.raises(ValueError, match="not YYYY"):
        ts_to_ns(ts)


@pytest.mark.parametrize(
    "ts",
    [
        "2026-10-30T09:00:00.000",
        "2026-10-30T09:00:00.000+00:00",
        "2026-10-30 09:00:00.000Z",
        "2026-10-30T09:00:00.000Z\n",
        "\u0662026-10-30T09:00:00.000Z",
    ],
)
def test_other_forms_are_rejected__defs_3(ts: str) -> None:
    with pytest.raises(ValueError, match="not YYYY"):
        ts_to_ns(ts)


@given(
    st.datetimes(min_value=datetime(1, 1, 1), max_value=datetime(9999, 12, 31)),
    st.integers(min_value=0, max_value=999),
)
def test_ns_matches_datetime_and_keeps_order__defs_3(moment: datetime, extra_ns: int) -> None:
    m = moment
    text = (
        f"{m.year:04d}-{m.month:02d}-{m.day:02d}T{m.hour:02d}:{m.minute:02d}:{m.second:02d}"
        f".{m.microsecond:06d}{extra_ns:03d}Z"
    )
    expected_us = (moment - datetime(1970, 1, 1)) // timedelta(microseconds=1)
    assert ts_to_ns(text) == expected_us * 1000 + extra_ns


def test_seconds_conversions() -> None:
    assert seconds_to_ns(3.0) == 3_000_000_000
    assert seconds_to_ns(0.1) == 100_000_000
    assert ns_to_seconds(1_500_000_000) == pytest.approx(1.5)


def test_ns_to_ts_round_trips_with_nine_fraction_digits__defs_3() -> None:
    assert ns_to_ts(0) == "1970-01-01T00:00:00.000000000Z"
    assert ns_to_ts(1_794_470_400_123_456_789) == "2026-11-12T08:00:00.123456789Z"
    assert ts_to_ns(ns_to_ts(1_794_470_400_123_456_789)) == 1_794_470_400_123_456_789
