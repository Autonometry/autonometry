"""Builders for timeline tests: sessions as event lists, with registries matching their hashes."""

from collections.abc import Iterable
from typing import Any, cast

from autonometry import spec
from autonometry.events import Event, Flag, Session
from autonometry.registry import registry_hash
from autonometry.timeline import Registries, Timeline, build_timeline

SPEC = "0.3"
RULES = spec.load_rules(SPEC)
NS = 1_000_000_000

DEVICES: dict[str, Any] = {
    "devices": [
        {"device_id": "policy", "kind": "ai", "match": {"node": "/policy"}},
        {"device_id": "replay", "kind": "script", "match": {"node": "/replay"}},
        {"device_id": "joy_local", "kind": "human", "location": "local", "match": {"node": "/j"}},
        {"device_id": "vr_remote", "kind": "human", "location": "remote", "match": {"node": "/v"}},
        {"device_id": "hoist", "kind": "support", "match": {"topic": "/hoist"}},
    ]
}
TASKS: dict[str, Any] = {"tasks": [{"task_definition_id": "pick_v1"}]}
PLAN: dict[str, Any] = {"items": [{"plan_item_id": "reset_scene", "kind": "reset"}]}
FULL = Registries.from_documents(DEVICES, TASKS, PLAN)


def ts(seconds: float) -> str:
    """``2026-11-12T08:MM:SS.fffffffffZ`` for ``seconds`` after 08:00 (below one hour)."""
    ns = round(seconds * NS)
    whole, fraction = divmod(ns, NS)
    return f"2026-11-12T08:{whole // 60:02d}:{whole % 60:02d}.{fraction:09d}Z"


class Log:
    """A session under construction; ``seq`` rises by one per event unless skipped."""

    def __init__(
        self,
        scopes: Iterable[str] = ("whole_robot",),
        initial: str | dict[str, str] = "idle",
        heartbeat_s: float = 30.0,
        **start: Any,
    ) -> None:
        names = list(scopes)
        self.events: list[dict[str, Any]] = []
        self.seq = 0
        self.task: str | None = None
        states = dict.fromkeys(names, initial) if isinstance(initial, str) else initial
        fields = {
            "scopes": names,
            "initial_states": states,
            "heartbeat_interval_s": heartbeat_s,
            "hint_policy": "hint_as_shared",
            "task_registry_hash": registry_hash(TASKS),
            "plan_hash": registry_hash(PLAN),
            "device_registry_hash": registry_hash(DEVICES),
            "simulated": False,
        }
        self.add(0, "session_start", **(fields | start))

    def add(self, t: float, kind: str, **fields: Any) -> "Log":
        if kind == "task_start":
            self.task = fields.pop("task", "t1")
            fields.setdefault("task_definition_id", "pick_v1")
        event = {
            "robot_id": "r01",
            "session_id": "s01",
            "task_id": self.task,
            "seq": self.seq,
            "ts": ts(t),
            "type": kind,
        }
        event |= fields
        if kind == "task_end":
            event.setdefault("outcome", "success")
            self.task = None
        self.events.append(event)
        self.seq += 1
        return self

    def skip(self) -> "Log":
        self.seq += 1
        return self

    def tr(  # noqa: PLR0913, PLR0917 -- the fields of a transition
        self,
        t: float,
        state: str,
        reason: str | None = None,
        actor: str | None = None,
        device: str | None = None,
        scope: str = "whole_robot",
        **extra: Any,
    ) -> "Log":
        fields: dict[str, Any] = {"scope": scope, "state": state, "reason": reason}
        fields["actor_id"] = actor
        if device is not None:
            fields["device_id"] = device
        return self.add(t, "transition", **fields, **extra)

    def ai(self, t: float, scope: str = "whole_robot") -> "Log":
        return self.tr(t, "ai", actor="ai:policy", device="policy", scope=scope)

    def human(
        self,
        t: float,
        state: str = "human_remote",
        reason: str = "human_initiated",
        actor: str = "op_a",
        scope: str = "whole_robot",
        **extra: Any,
    ) -> "Log":
        device = "joy_local" if state == "human_local" else "vr_remote"
        return self.tr(t, state, reason, actor, device, scope, **extra)

    def end(self, t: float) -> "Log":
        return self.add(t, "session_end")

    def session(self) -> Session:
        return Session("r01", "s01", tuple(cast(Event, dict(e)) for e in self.events))

    def build(self, registries: Registries = FULL, flags: Iterable[Flag] = ()) -> Timeline:
        return build_timeline(self.session(), registries, RULES, flags)


def seconds(ns: int) -> float:
    return ns / NS


def codes(timeline: Timeline) -> list[tuple[int, str]]:
    return [(f.seq, f.code) for f in timeline.flags]


def at(timeline: Timeline, ns: int) -> float:
    """Seconds from the session start to the instant ``ns``."""
    assert timeline.start_ns is not None
    return (ns - timeline.start_ns) / NS
