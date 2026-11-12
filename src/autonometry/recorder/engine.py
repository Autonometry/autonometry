"""The recorder's decisions: which events to write, and when (README, "Recorder").

Pure apart from reading the injected clocks: the engine takes datagrams and the passing of time
and returns events that lack only ``robot_id``, ``session_id``, ``seq`` and the seal, which the
session log adds. Its rules:

- Level-triggered states. A connector re-sends the state of each scope it observes on every
  change and at least every second; a transition is written only when ``state``, ``reason``,
  ``actor_id`` or ``device_id`` differ from the scope's current value. A scope belongs to the
  first client that asserts it until that client has not asserted it for ``gap_tolerance_s``;
  then the scope is ``unknown`` (definitions.md 6.14) and the next client to assert it owns it.
  Assertions from another client meanwhile are dropped and counted.
- Loss is visible. A gap in a client's counter ``n``, a rejected datagram or one dropped by the
  rate limit makes every scope ``unknown`` at once (nothing says which scope it touched); the next
  assertions restore them. The next heartbeat carries the counts.
- ``ts`` is the receive time from the system UTC clock, never earlier than the previous event's;
  a backward step is clamped and the next heartbeat carries its size (``x_clock_stepped_back_s``).
- Heartbeats every ``heartbeat_interval_s`` carry the clock status and the distance travelled.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import rfc8785

from autonometry.reader import RejectedEventError, check_event
from autonometry.recorder.clock import ClockStatus
from autonometry.recorder.wire import STATE_TYPE, Message, WireError, parse_datagram
from autonometry.strictjson import MAX_SAFE_INTEGER
from autonometry.timestamps import ns_to_seconds, ns_to_ts

logger = logging.getLogger(__name__)

#: Most distinct client names followed in one run; datagrams from more are rejected.
MAX_CLIENTS = 256
#: Bytes the seal adds to a canonical event line: ``,"hash":"<64 hex>"`` and
#: ``,"prev_hash":"<64 hex>"`` (definitions.md 8).
SEAL_OVERHEAD_BYTES = len(rfc8785.dumps({"a": 0, "hash": "0" * 64, "prev_hash": "0" * 64})) - len(
    rfc8785.dumps({"a": 0})
)
#: Stands in for the real session id while a client event is checked; session ids always have
#: this length (``YYYYMMDDTHHMMSSZ-xxxx``), so the length check is exact.
PLACEHOLDER_SESSION_ID = "00000000T000000Z-0000"


class Clock(Protocol):
    """The two clocks a recorder reads: monotonic seconds for timers, UTC nanoseconds for ts."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, never going backwards."""
        ...

    def wall_ns(self) -> int:
        """Nanoseconds since the Unix epoch, UTC, from the system clock."""
        ...


@dataclass(frozen=True, slots=True)
class ScopeValue:
    """What a level-triggered assertion sets for one scope; a change writes a transition."""

    state: str
    reason: str | None = None
    actor_id: str | None = None
    device_id: str | None = None


UNKNOWN = ScopeValue("unknown")


@dataclass(frozen=True, slots=True)
class Settings:
    """What the engine needs from the config and the spec rules (seconds, bytes)."""

    robot_id: str
    scopes: tuple[str, ...]
    declaration: Mapping[str, Any]
    spec_version: str
    heartbeat_interval_s: float
    gap_tolerance_s: float
    max_events_per_s: int
    max_line_bytes: int


@dataclass(frozen=True, slots=True)
class Restored:
    """State rebuilt from a session's events on disk after a crash (README, "Crash recovery")."""

    values: Mapping[str, ScopeValue]
    open_task: str | None
    distance_m: float | None
    last_ts_ns: int


@dataclass(slots=True)
class _Client:
    last_n: int
    tokens: float
    refilled_at: float


@dataclass(slots=True)
class _Owner:
    client: str
    last_assertion: float


@dataclass(slots=True)
class _Counters:
    """Counts since the last heartbeat, written on it as ``x_`` fields when not zero."""

    lost: int = 0
    rejected: int = 0
    rate_limited: int = 0
    foreign: int = 0
    stepped_back_ns: int = 0
    first_rejection: str | None = None

    def fields(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "x_lost_client_events": self.lost,
            "x_rejected_client_events": self.rejected,
            "x_rate_limited_events": self.rate_limited,
            "x_foreign_assertions": self.foreign,
        }
        out = {name: value for name, value in found.items() if value}
        if self.stepped_back_ns:
            out["x_clock_stepped_back_s"] = ns_to_seconds(self.stepped_back_ns)
        return out


class _RejectedError(Exception):
    pass


@dataclass(slots=True)
class _Distance:
    """Distance travelled in the session, metres: the sum of increases of each client's
    odometer readings, so it starts at 0 with the session and never falls."""

    total: float | None = None
    readings: dict[str, float] = field(default_factory=dict)

    def add(self, client: str, reading: float) -> None:
        last = self.readings.get(client)
        self.readings[client] = reading
        self.total = self.total or 0.0
        if last is not None and reading > last:
            self.total += reading - last


class Engine:
    """Decides the events of a recorder; see the module docstring for the rules."""

    def __init__(
        self, settings: Settings, clock: Clock, read_clock_status: Callable[[], ClockStatus]
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._read_clock_status = read_clock_status
        self._scopes = frozenset(settings.scopes)
        self._values: dict[str, ScopeValue] = dict.fromkeys(settings.scopes, UNKNOWN)
        self._owners: dict[str, _Owner] = {}
        self._clients: dict[str, _Client] = {}
        self._counters = _Counters()
        self._distance = _Distance()
        self._open_task: str | None = None
        self._last_ts_ns = 0
        self._next_heartbeat = clock.monotonic() + settings.heartbeat_interval_s

    # -- time ---------------------------------------------------------------------------------

    def now_ns(self) -> int:
        """The UTC time the next event gets, clamped to never fall below the previous event's;
        a backward step of the system clock is remembered for the next heartbeat."""
        now = self._clock.wall_ns()
        if now < self._last_ts_ns:
            self._counters.stepped_back_ns = max(
                self._counters.stepped_back_ns, self._last_ts_ns - now
            )
            now = self._last_ts_ns
        self._last_ts_ns = now
        return now

    def _base(self, kind: str) -> dict[str, Any]:
        return {"type": kind, "ts": ns_to_ts(self.now_ns()), "task_id": self._open_task}

    def next_deadline(self) -> float:
        """Monotonic time of the next heartbeat or scope silence, whichever comes first."""
        silences = [
            o.last_assertion + self._settings.gap_tolerance_s for o in self._owners.values()
        ]
        return min([self._next_heartbeat, *silences])

    # -- session markers and heartbeats -------------------------------------------------------

    def session_start(self) -> dict[str, Any]:
        """A ``session_start`` whose ``initial_states`` are the current states (all ``unknown``
        at a fresh start); the distance travelled starts again at 0 (definitions.md 2 rule 1, 8)."""
        if self._distance.total is not None:
            self._distance.total = 0.0
        event = self._base("session_start")
        event["task_id"] = None
        event.update(self._settings.declaration)
        event["initial_states"] = {scope: value.state for scope, value in self._values.items()}
        return event

    def session_end(self, cause: str) -> dict[str, Any]:
        """A ``session_end`` with ``x_cause`` saying why the recorder closed the session."""
        event = self._base("session_end")
        event["task_id"] = None
        event["x_cause"] = cause
        return event

    def heartbeat(self, **extra: Any) -> dict[str, Any]:
        """A heartbeat now: clock status, distance, and the counts since the last heartbeat."""
        self._next_heartbeat = self._clock.monotonic() + self._settings.heartbeat_interval_s
        status = self._read_clock_status()
        event = self._base("heartbeat")
        event["clock_source"] = status.source
        if status.max_error_s is not None:
            event["clock_max_error_s"] = status.max_error_s
        if self._distance.total is not None:
            event["distance_m"] = self._distance.total
        counts = self._counters.fields()
        if counts:
            logger.warning(
                "since the last heartbeat: %s%s",
                ", ".join(f"{name.removeprefix('x_')} {value:g}" for name, value in counts.items()),
                f" (first rejection: {self._counters.first_rejection})"
                if self._counters.first_rejection
                else "",
            )
        event.update(counts)
        event.update(extra)
        self._counters = _Counters()
        return event

    def all_unknown(self, cause: str) -> list[dict[str, Any]]:
        """``unknown`` transitions for every scope not already ``unknown``; owners keep their
        scopes, so their next assertions restore them."""
        events = []
        for scope in self._settings.scopes:
            if self._values[scope] != UNKNOWN:
                events.append(self._unknown(scope, cause))
        return events

    def _unknown(self, scope: str, cause: str) -> dict[str, Any]:
        self._values[scope] = UNKNOWN
        event = self._base(STATE_TYPE)
        event.update(scope=scope, state="unknown", reason=None, actor_id=None, x_cause=cause)
        return event

    def restore(self, restored: Restored) -> None:
        """Continue from a session rebuilt after a crash: its states, open task, distance and
        last ``ts``. No client owns a scope until it asserts it again."""
        self._values.update(restored.values)
        self._open_task = restored.open_task
        self._distance.total = restored.distance_m
        self._last_ts_ns = max(self._last_ts_ns, restored.last_ts_ns)

    # -- timers -------------------------------------------------------------------------------

    def tick(self) -> list[dict[str, Any]]:
        """Events due by now: ``unknown`` for scopes silent for ``gap_tolerance_s``
        (definitions.md 6.14), then a heartbeat if one is due."""
        now = self._clock.monotonic()
        events: list[dict[str, Any]] = []
        for scope in self._settings.scopes:
            owner = self._owners.get(scope)
            if owner is not None and now - owner.last_assertion >= self._settings.gap_tolerance_s:
                del self._owners[scope]
                if self._values[scope] != UNKNOWN:
                    events.append(self._unknown(scope, "silent"))
        if now >= self._next_heartbeat:
            events.append(self.heartbeat())
        return events

    # -- datagrams ----------------------------------------------------------------------------

    def receive(self, data: bytes) -> list[dict[str, Any]]:
        """Events caused by one datagram, after any that were already due."""
        events = self.tick()
        try:
            message = parse_datagram(data, self._settings.max_line_bytes)
        except WireError as exc:
            if exc.client is not None and exc.n is not None and self._can_follow(exc.client):
                events += self._follow(exc.client, exc.n)
            return events + self._reject(exc.message)
        if not self._can_follow(message.client):
            return events + self._reject(f"more than {MAX_CLIENTS} clients")
        events += self._follow(message.client, message.n)
        if not self._take_token(message.client):
            self._counters.rate_limited += 1
            return events + self.all_unknown("rate_limited")
        return events + self._handle(message)

    def _reject(self, reason: str) -> list[dict[str, Any]]:
        self._counters.rejected += 1
        if self._counters.first_rejection is None:
            self._counters.first_rejection = reason
        return self.all_unknown("rejected")

    def _can_follow(self, client: str) -> bool:
        return client in self._clients or len(self._clients) < MAX_CLIENTS

    def _follow(self, client: str, n: int) -> list[dict[str, Any]]:
        """Track a client's counter: ``n`` should be one more than the last, or 0 after the
        client restarted. A first datagram with ``n`` above 0, or a jump, means datagrams
        were lost; a smaller ``n`` above 0 means it restarted and its first ``n`` were lost."""
        now = self._clock.monotonic()
        known = self._clients.get(client)
        if known is None:
            limit = float(self._settings.max_events_per_s)
            self._clients[client] = _Client(n, limit, now)
            lost = n
        else:
            last, known.last_n = known.last_n, n
            lost = n - last - 1 if n > last else n
        if lost <= 0:
            return []
        self._counters.lost += lost
        return self.all_unknown("lost")

    def _take_token(self, client: str) -> bool:
        """Token bucket of ``max_events_per_s`` per client, refilled continuously."""
        state = self._clients[client]
        now = self._clock.monotonic()
        limit = float(self._settings.max_events_per_s)
        state.tokens = min(limit, state.tokens + (now - state.refilled_at) * limit)
        state.refilled_at = now
        if state.tokens < 1.0:
            return False
        state.tokens -= 1.0
        return True

    def _handle(self, message: Message) -> list[dict[str, Any]]:
        if message.distance_m is not None:
            self._distance.add(message.client, message.distance_m)
            return []
        assert message.event is not None  # noqa: S101 -- parse_datagram returns one of the two
        try:
            event = self._checked(message.event)
        except _RejectedError as exc:
            return self._reject(str(exc))
        if event["type"] == STATE_TYPE:
            return self._assert(message.client, event)
        if event["type"] == "task_start":
            self._open_task = event["task_id"]
        elif event["type"] == "task_end" and event["task_id"] == self._open_task:
            self._open_task = None
        return [event]

    def _checked(self, received: Mapping[str, Any]) -> dict[str, Any]:
        """The client's event with ``ts`` and, unless it gives one, the open task's id; checked
        exactly as ``validate`` checks a line, including its length once sealed."""
        event = dict(received)
        event["ts"] = ns_to_ts(self.now_ns())
        event.setdefault("task_id", self._open_task)
        scope = event.get("scope")
        if scope is not None and scope not in self._scopes:
            raise _RejectedError("event names a scope the config does not declare")
        trial = {
            **event,
            "robot_id": self._settings.robot_id,
            "session_id": PLACEHOLDER_SESSION_ID,
            "seq": MAX_SAFE_INTEGER,
        }
        try:
            check_event(trial, self._settings.spec_version)
        except RejectedEventError as exc:
            raise _RejectedError(f"{exc.code}: {exc.message}") from None
        if len(rfc8785.dumps(trial)) + SEAL_OVERHEAD_BYTES > self._settings.max_line_bytes:
            raise _RejectedError(
                f"event is longer than {self._settings.max_line_bytes} bytes once sealed"
            )
        return event

    def _assert(self, client: str, event: dict[str, Any]) -> list[dict[str, Any]]:
        scope = event["scope"]
        now = self._clock.monotonic()
        owner = self._owners.get(scope)
        if owner is None:
            owner = self._owners[scope] = _Owner(client, now)
        elif owner.client != client:
            self._counters.foreign += 1
            return []
        owner.last_assertion = now
        value = ScopeValue(
            event["state"], event.get("reason"), event.get("actor_id"), event.get("device_id")
        )
        if value == self._values[scope]:
            return []
        self._values[scope] = value
        return [event]
