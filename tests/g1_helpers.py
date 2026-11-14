"""Shared helpers for the Unitree G1 connector tests. Not collected by pytest (no ``test_``)."""

import json
import struct
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from autonometry.connectors import g1
from autonometry.connectors.tb4 import Source, StateAssertion, devices_from_registry
from autonometry.spec import load_rules
from recorder_helpers import Client

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "connectors" / "g1"
GOLDEN = Path(__file__).resolve().parent / "golden" / "g1"
RULES = load_rules("0.3")
REGISTRY = json.loads((CONFIG / "devices.json").read_text(encoding="utf-8"))
DEVICES = devices_from_registry(REGISTRY)

PC1, PC2, TELEOP = "g1-pc1", "g1-pc2", "teleop-pc"
#: Writer keys of the example registry's sources.
LOWSTATE = "w-lowstate"
SPORT = "w-sport"
ARM_SERVICE = "w-arm-service"
XR_ARM, XR_LOWCMD, XR_SPORT, XR_SWITCH, XR_DEX3 = (
    "w-xr-arm",
    "w-xr-low",
    "w-xr-sport",
    "w-xr-ms",
    "w-xr-dex3",
)
POLICY_LOWCMD, POLICY_ARM, POLICY_SPORT, POLICY_ARM_REQ = (
    "w-pol-low",
    "w-pol-arm",
    "w-pol-sport",
    "w-pol-armreq",
)
REPLAY_LOWCMD = "w-replay-low"
STRANGER_LOWCMD, STRANGER_SPORT = "w-stranger-low", "w-stranger-sport"

_WRITERS: dict[str, tuple[str | None, str | None, str]] = {
    LOWSTATE: (PC1, "hg_lowstate", g1.LOWSTATE),
    SPORT: (PC1, "ai_sport", g1.SPORTSTATE),
    ARM_SERVICE: (PC1, "g1_arm_action", g1.ARM_SDK),
    XR_ARM: (TELEOP, "teleop_hand_and_arm.py", g1.ARM_SDK),
    XR_LOWCMD: (TELEOP, "teleop_hand_and_arm.py", g1.LOWCMD),
    XR_SPORT: (TELEOP, "teleop_hand_and_arm.py", g1.request_topic("sport")),
    XR_SWITCH: (TELEOP, "teleop_hand_and_arm.py", g1.request_topic("motion_switcher")),
    XR_DEX3: (TELEOP, "teleop_hand_and_arm.py", "rt/dex3/left/cmd"),
    POLICY_LOWCMD: (PC2, "eval_g1.py", g1.LOWCMD),
    POLICY_ARM: (PC2, "eval_g1.py", g1.ARM_SDK),
    POLICY_SPORT: (PC2, "eval_g1.py", g1.request_topic("sport")),
    POLICY_ARM_REQ: (PC2, "eval_g1.py", g1.request_topic("arm")),
    REPLAY_LOWCMD: (PC2, "replay_robot.py", g1.LOWCMD),
    STRANGER_LOWCMD: ("laptop", "python3", g1.LOWCMD),
    STRANGER_SPORT: ("laptop", "python3", g1.request_topic("sport")),
}


def settings(**options: Any) -> g1.Settings:
    """Settings from the spec's parameters and the example G1 registry."""
    devices = options.pop("devices", DEVICES)
    return g1.Settings.from_params(RULES.params, devices, **options)


def engine(**options: Any) -> g1.G1Engine:
    return g1.G1Engine(settings(**options))


def seen(t: float, key: str, *, decoded: bool = True) -> g1.WriterSeen:
    """The discovery of one of the writers above."""
    host, participant, topic = _WRITERS[key]
    return g1.WriterSeen(
        t, key, Source(host=host, participant=participant, topic=topic), "", decoded
    )


def remote_bytes(*buttons: str, lx: float = 0.0, ly: float = 0.0, rx: float = 0.0) -> bytes:
    """``wireless_remote`` with these buttons held and sticks (gamepad.hpp layout)."""
    mask = 0
    for name in buttons:
        mask |= g1.BUTTONS[name]
    return struct.pack("<2xH5f", mask, lx, rx, 0.0, 0.0, ly) + bytes(16)


def request(  # noqa: PLR0913, PLR0917 -- the fields of a request
    t: float, key: str, service: str, api_id: int, params: Any = None, request_id: int = 0
) -> g1.Request:
    text = params if isinstance(params, str) else json.dumps(params or {})
    return g1.Request(t, key, service, request_id, api_id, text)


def velocity(t: float, key: str, vx: float, duration: float = 1.0) -> g1.Request:
    """SetVelocity (7105), as Unitree's Move sends it."""
    return request(t, key, "sport", 7105, {"velocity": [vx, 0.0, 0.0], "duration": duration})


class Sim:
    """The robot publishing ``rt/lowstate`` (with the remote's bytes) and ``rt/sportmodestate``
    every ``dt`` seconds, the engine ticking at the same rate; the latest assertion per scope
    and every message are kept. The writers of the example registry are discovered at t = 0."""

    def __init__(self, *, task: str | None = "t1", dt: float = 0.05, **options: Any) -> None:
        self.engine = engine(**options)
        self.t = 0.0
        self.dt = dt
        self.fsm = (500, 0, 0)
        self.remote = remote_bytes()
        self.robot = True
        self.builtin = True
        self.latest: dict[str, StateAssertion] = {}
        self.history: list[tuple[float, StateAssertion]] = []
        self.notes: list[g1.Note] = []
        self.feed(*(seen(0.0, key) for key in _WRITERS))
        if task is not None:
            self.feed(g1.TaskOpened(0.0, task))

    def feed(self, *observations: g1.Observation) -> None:
        for observation in observations:
            self._keep(observation.t, self.engine.observe(observation))

    def _keep(self, t: float, messages: Iterable[g1.Message]) -> None:
        for message in messages:
            if isinstance(message, StateAssertion):
                if self.latest.get(message.scope) != message:
                    self.history.append((t, message))
                self.latest[message.scope] = message
            else:
                self.notes.append(message)

    def step(self, *observations: g1.Observation) -> None:
        """One period: the robot's samples, then ``observations`` (stamped now), then a tick."""
        self.t = round(self.t + self.dt, 6)
        if self.robot:
            self.feed(g1.LowState(self.t, LOWSTATE, self.remote))
            if self.builtin:
                self.feed(g1.SportState(self.t, SPORT, *self.fsm))
        self.feed(*(_stamp(o, self.t) for o in observations))
        self._keep(self.t, self.engine.tick(self.t))

    def run(self, until: float, *every: g1.Observation) -> None:
        """Step until ``until``, with ``every`` observation repeated at each step."""
        while self.t < until - 1e-9:
            self.step(*every)

    def state(self, scope: str = g1.LEGS) -> StateAssertion:
        return self.latest[scope]

    def states(self, scope: str = g1.LEGS) -> list[str]:
        """The distinct states the scope went through, in order."""
        out: list[str] = []
        for _, value in self.history:
            if value.scope == scope and (not out or out[-1] != value.state):
                out.append(value.state)
        return out


def _stamp(observation: g1.Observation, t: float) -> g1.Observation:
    return replace(observation, t=t)


def cause(value: StateAssertion) -> Any:
    return dict(value.fields).get("x_g1_cause")


def command(t: float, key: str, weight: float | None = None) -> g1.Command:
    return g1.Command(t, key, _WRITERS[key][2], weight)


def datagrams(client: Client, messages: Iterable[g1.Message]) -> list[bytes]:
    """The datagrams the watcher sends for ``messages``, in order."""
    out = []
    for message in messages:
        event = g1.wire_event(message)
        out.append(client.event(event.pop("type"), **event))
    return out
