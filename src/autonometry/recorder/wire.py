"""The recorder's wire format: one UTF-8 JSON object per Unix datagram (README, "Wire format").

``{"client": <name>, "n": <counter>, "event": {...}}`` carries one event (a heartbeat from a
client carries only ``x_`` fields), and
``{"client": <name>, "n": <counter>, "distance_m": <metres>}`` one odometer reading. ``client`` is
an identifier (definitions.md 8, Identifiers); ``n`` counts every datagram the client tried to
send, from 0, so a gap shows the recorder what was lost. The event never carries the fields the
recorder alone writes. Parsing is pure and never quotes the datagram in an error.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from autonometry import strictjson

#: Event types a client sends as level-triggered state assertions (README, "Level triggering").
STATE_TYPE = "transition"
#: Event types written as received.
DISCRETE_TYPES = frozenset(
    {
        "task_start",
        "task_end",
        "hint",
        "approval",
        "help_request",
        "supervision",
        "support_start",
        "support_end",
    }
)
#: A client heartbeat carries only ``x_`` fields, such as a connector's ``x_publishers``; the
#: recorder writes them on a heartbeat of its own, with the clock status and distance.
NOTE_TYPE = "heartbeat"
#: ``x_`` fields the recorder itself writes; a client heartbeat may not carry them, so a connector
#: can neither hide nor fake the recorder's own counts.
RECORDER_X_FIELDS = frozenset(
    {
        "x_lost_client_events",
        "x_rejected_client_events",
        "x_rate_limited_events",
        "x_foreign_assertions",
        "x_resumed_clients",
        "x_clock_stepped_back_s",
        "x_recovered",
        "x_truncated_bytes",
        "x_cause",
        "x_carried_task",
    }
)
#: Fields only the recorder writes (definitions.md 8 common fields, section 10 sealing).
RECORDER_FIELDS = frozenset({"robot_id", "session_id", "seq", "ts", "prev_hash", "hash"})

_CLIENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_ENVELOPE_KEYS = frozenset({"client", "n", "event", "distance_m"})


class WireError(Exception):
    """A datagram is not a valid message. ``client`` and ``n`` are set when they could be read,
    so the recorder can still follow that client's counter. The message is one line."""

    def __init__(self, message: str, client: str | None = None, n: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.client = client
        self.n = n


@dataclass(frozen=True, slots=True)
class Message:
    """One parsed datagram: an event or an odometer reading (metres), never both."""

    client: str
    n: int
    event: Mapping[str, Any] | None
    distance_m: float | None


def _client_and_n(value: dict[str, Any]) -> tuple[str, int]:
    client, n = value.get("client"), value.get("n")
    if not isinstance(client, str) or not _CLIENT_RE.fullmatch(client):
        raise WireError("client is not an identifier of 1 to 128 safe characters")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise WireError("n is not a non-negative integer", client)
    return client, n


def _check_event(event: Any, client: str, n: int) -> Mapping[str, Any]:
    if not isinstance(event, dict):
        raise WireError("event is not a JSON object", client, n)
    if RECORDER_FIELDS & event.keys():
        raise WireError("event carries a field only the recorder writes", client, n)
    kind = event.get("type")
    if kind not in (STATE_TYPE, NOTE_TYPE) and kind not in DISCRETE_TYPES:
        raise WireError("event type is not one a client may send", client, n)
    if kind == NOTE_TYPE and (
        len(event) == 1
        or any(k != "type" and (not k.startswith("x_") or k in RECORDER_X_FIELDS) for k in event)
    ):
        raise WireError(
            "a client heartbeat must carry x_ fields, and only ones the recorder does not write",
            client,
            n,
        )
    return event


def parse_datagram(data: bytes, max_bytes: int) -> Message:
    """Parse one datagram of at most ``max_bytes`` bytes.

    Raises:
        WireError: the datagram is too long, not UTF-8 I-JSON, or not a valid envelope.
    """
    if len(data) > max_bytes:
        raise WireError(f"datagram is longer than {max_bytes} bytes")
    try:
        value = strictjson.parse(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise WireError("datagram is not valid UTF-8") from None
    except strictjson.StrictJsonError as exc:
        raise WireError(f"{exc.code}: {exc.message}") from None
    if not isinstance(value, dict):
        raise WireError("datagram is not a JSON object")
    client, n = _client_and_n(value)
    if value.keys() - _ENVELOPE_KEYS:
        raise WireError("datagram has a field the wire format does not define", client, n)
    if ("event" in value) == ("distance_m" in value):
        raise WireError("datagram must carry exactly one of event and distance_m", client, n)
    if "event" in value:
        return Message(client, n, _check_event(value["event"], client, n), None)
    distance = value["distance_m"]
    if (
        not isinstance(distance, int | float)
        or isinstance(distance, bool)
        or not math.isfinite(distance)
        or distance < 0
    ):
        raise WireError("distance_m is not a non-negative number", client, n)
    return Message(client, n, None, float(distance))
