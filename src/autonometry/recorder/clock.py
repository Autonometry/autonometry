"""The recorder's clock status for heartbeats (definitions.md section 8, Time).

On Linux the kernel's NTP state is read with ``adjtimex(2)`` in read-only mode (``modes = 0``,
allowed without privileges): ``STA_UNSYNC`` or a ``TIME_ERROR`` clock state means the clock is not
synchronised (``none``); otherwise the clock is synchronised by the source the config names and
``maxerror`` (microseconds) is its estimated maximum error. Elsewhere the status is ``unknown``.
"""

import ctypes
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: ``status`` bit: the clock is not synchronised (linux/timex.h).
STA_UNSYNC = 0x0040
#: ``adjtimex`` return value: the clock is not synchronised (linux/timex.h).
TIME_ERROR = 5
_US_PER_S = 1_000_000


@dataclass(frozen=True, slots=True)
class ClockStatus:
    """``clock_source`` and ``clock_max_error_s`` (seconds; None when not known) of a heartbeat."""

    source: str
    max_error_s: float | None


UNKNOWN = ClockStatus("unknown", None)


def status_from_timex(state: int, status: int, maxerror_us: int, configured: str) -> ClockStatus:
    """Turn the result of ``adjtimex(2)`` into a heartbeat's clock status.

    Args:
        state: the return value, the clock state (``TIME_OK`` .. ``TIME_ERROR``; -1 on failure).
        status: the ``status`` field (``STA_*`` bits).
        maxerror_us: the ``maxerror`` field, microseconds.
        configured: the source the config names: ``ntp``, ``ptp`` or ``gnss``.
    """
    if state < 0:
        return UNKNOWN
    max_error_s = maxerror_us / _US_PER_S if maxerror_us >= 0 else None
    if state == TIME_ERROR or status & STA_UNSYNC:
        return ClockStatus("none", max_error_s)
    return ClockStatus(configured, max_error_s)


class _Timex(ctypes.Structure):
    """``struct timex`` of glibc and musl up to ``tai``, then generous padding: only ``modes``,
    ``maxerror`` and ``status`` are read, and the kernel may write the rest."""

    _fields_ = (
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long),
        ("time_usec", ctypes.c_long),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("padding", ctypes.c_byte * 256),
    )


Adjtimex = Callable[["ctypes._Pointer[_Timex]"], int]


def _libc_adjtimex() -> Adjtimex | None:
    try:
        function = ctypes.CDLL(None, use_errno=True).adjtimex
    except (OSError, AttributeError):
        logger.warning("adjtimex is not available; clock status is reported as unknown")
        return None
    function.argtypes = (ctypes.POINTER(_Timex),)
    function.restype = ctypes.c_int
    return function


class ClockReader:
    """Reads the clock status on each heartbeat (definitions.md 8, Time)."""

    def __init__(self, configured: str, adjtimex: Adjtimex | None = None) -> None:
        self._configured = configured
        if adjtimex is None and sys.platform == "linux":
            adjtimex = _libc_adjtimex()
        self._adjtimex = adjtimex

    def read(self) -> ClockStatus:
        """Return the current status; ``unknown`` when it cannot be read on this platform."""
        if self._adjtimex is None:
            return UNKNOWN
        timex = _Timex()  # modes = 0: read only
        state = self._adjtimex(ctypes.pointer(timex))
        return status_from_timex(state, timex.status, timex.maxerror, self._configured)
