"""Unitree G1 engine: DDS observations in, recorder messages out (README, "Unitree G1").

Pure and standard library only: no Cyclone DDS, no input or output, no clock. The edge
(:mod:`autonometry.connectors.g1_dds`) turns what it reads on the robot's network into the plain
observations below, stamped with its monotonic receive time ``t`` (seconds), passes them to
:meth:`G1Engine.observe`, calls :meth:`G1Engine.tick` a few times a second and sends what both
return, in order, through the recorder client. The rules are those of the integration notes
(``research/unitree-g1/g1_integration_notes.md``, section 6) under spec v0.3; their sources are
pinned in ``research/unitree-g1/signal_map.yaml`` (S1 to S12).

**Scopes** (notes section 5): ``legs`` (12 joints), ``upper_body`` (the waist and both arms, the
17 joints of ``rt/arm_sdk``) and, when the robot has hands and the config declares it, ``hands``.
For each scope at each instant the first rule that applies decides:

1. **Robot lost.** No ``rt/lowstate`` sample for more than ``gap_tolerance_s``: ``unknown``
   (definitions.md 2, 6.14).
2. **Emergency stop.** The remote's L2 + B, or a Damp or ZeroTorque request (7101 with data 1 or
   0) from a person or an undeclared source, followed within ``gap_tolerance_s`` by a passive
   built-in state, or at once while the built-in controller is unseen or released:
   ``safety_stop``, reason ``safety``, actor the person's, until the built-in state leaves passive
   or the next change of mode. Writers that keep writing do not end it (6.7, 6.8).
3. **Released or user control** (``legs``, ``upper_body``): the active writers of ``rt/lowcmd``
   (released) or ``rt/user_lowcmd`` (user control): one source, its device's state; several within
   ``contention_window_s``, 6.12; none, inside an open task ``unknown``, outside the last
   controller's state until ``idle`` (6.14).
4. **Built-in controller unseen** (``legs``, ``upper_body``): rule 3 when an ``rt/lowcmd`` writer
   is active, else ``unknown``.
5. **Other passive states** (``legs``, ``upper_body``): the hold of whoever caused it (6.8): a
   request from an ``ai`` or ``script`` device, ``ai`` or ``scripted``; the remote's L2 + Y,
   ``shared`` with reason ``human_initiated``; no observed cause, ``unknown``. Outside an open task,
   the last controller's state until ``idle``.
6. **upper_body, built-in controller active.** (a) An arm preset runs: ``scripted``, actor
   ``script:<preset>``, reason ``human_initiated`` when a person started it (6.9, 6.13). (b)
   Another active ``rt/arm_sdk`` writer: weight at least 0.99, its state; above 0.01, a blend
   with the built-in controller (6.12). (c) Otherwise as ``legs``.
7. **legs, built-in controller active.** (a) Open goal spans (6.11, 6.12); a posture a source
   started is a fixed motion, ``scripted`` while it moves (6.13). (b) Moving with no goal span
   for more than ``gap_tolerance_s``: ``unknown`` (the app or the voice service may drive). (c)
   Static in a position-control posture: as rule 5. (d) Static otherwise: inside an open task
   ``ai``, the built-in controller keeping balance; outside, the last controller's state until
   ``idle``.
8. **hands.** The active hand writers: one, its state; several, 6.12; none, as in rule 3.

**Where the notes leave a case open** the engine takes the more conservative state, never
``ai``; each such place says so in a comment starting ``Open case``, and the README lists them:
a person's open goal span contends (6.12) with an AI or scripted state on ``legs`` (and on
``upper_body`` where it follows the same source), so that state becomes ``shared``; every source
with an open goal span contends; while a preset runs, another ``rt/arm_sdk`` writer contends with
it; an arm motion with no observed start (sportmodestate ``task_id`` not 0, or the arm action
state holding) is ``unknown``; the arm action state's "no hold" ends a preset only after it held;
a stop that began at once ends only at a change of mode; a remote stick value that is not a
finite number counts as outside the dead zone; a writer whose weight cannot be read counts as
holding its joints (weight 1); a malformed velocity only extends a goal span; a response only
ever keeps the robot released or in user control.

**Sources.** A source is observed as ``(host, participant, topic)`` (notes section 4) and matches
the first device of the registry whose given ``match`` fields all equal it (6.10). The handheld
remote is the virtual topic ``wireless_remote`` with the host and participant of the
``rt/lowstate`` writer whose bytes carry it; Unitree's own fixed motions (arm presets, postures)
are the virtual source ``{"topic": "preset"}`` with no host or participant. A source no device
matches is sent without ``device_id`` and the loader counts it as ``human_remote``. A writer whose
identity discovery has not shown yet counts as undeclared. :func:`registry_problems` finds the
registries the watcher refuses: one observed source that could match devices of different kind
or location.

**Evidence** goes in ``x_`` fields: ``x_g1_cause`` and ``x_g1_fsm_id`` on every state,
``x_g1_weight`` on states decided by an ``rt/arm_sdk`` weight, and on heartbeat notes
``x_g1_writers`` (the active writers when that set changes: a declared one by topic and device,
an undeclared one by topic, program and a pseudonym of its host, never the host name),
``x_g1_tasks: "unreadable"`` (once, when the recorder's session log cannot be followed) and
``x_g1_robot_returned`` (the robot came back after ``idle_threshold_s`` while released or in user
control, which a restart ends without a request).
"""

import hashlib
import json
import math
import re
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal

from autonometry import strictjson
from autonometry.connectors.lerobot import Note
from autonometry.connectors.lerobot import wire_event as _wire_event
from autonometry.connectors.tb4 import Device, Fields, Source, StateAssertion

__all__ = [
    "ARM_SDK",
    "ARM_STATE",
    "BUTTONS",
    "COMMAND_TOPICS",
    "HANDS",
    "HAND_SDK",
    "HAND_TOPICS",
    "LEGS",
    "LOWCMD",
    "LOWSTATE",
    "PRESET_HOLD_S",
    "PRESET_TOPIC",
    "REMOTE_TOPIC",
    "SERVICES",
    "SPORTSTATE",
    "STICK_DEAD_ZONE",
    "UPPER_BODY",
    "USER_LOWCMD",
    "WEIGHT_MAX",
    "WEIGHT_MIN",
    "ArmState",
    "Command",
    "G1Engine",
    "LowState",
    "Message",
    "Note",
    "Observation",
    "RemoteInput",
    "Request",
    "Response",
    "Settings",
    "SportState",
    "TaskClosed",
    "TaskOpened",
    "TasksUnreadable",
    "WriterGone",
    "WriterSeen",
    "decode_remote",
    "host_pseudonym",
    "registry_problems",
    "request_topic",
    "response_topic",
    "wire_event",
]

# -- topics and constants -----------------------------------------------------------------------

LEGS: Final = "legs"
UPPER_BODY: Final = "upper_body"
HANDS: Final = "hands"

#: The robot's state with the handheld remote's bytes (S2).
LOWSTATE: Final = "rt/lowstate"
#: The built-in controller's state (S1).
SPORTSTATE: Final = "rt/sportmodestate"
#: Joint commands while the built-in program is released (S3) and in user control (S5).
LOWCMD: Final = "rt/lowcmd"
USER_LOWCMD: Final = "rt/user_lowcmd"
#: The upper-body blend; its weight is ``motor_cmd[29].q`` (S4).
ARM_SDK: Final = "rt/arm_sdk"
#: The arm action service's state (S9): JSON text ``{"holding", "id", "name"}``.
ARM_STATE: Final = "rt/arm/action/state"
#: The hand blend; its weight is ``cmds[0].mode / 100`` (unitree_sdk2 aaf69cb,
#: example/g1/high_level/g1_hand_sdk_example.cpp:34-40).
HAND_SDK: Final = "rt/hand_sdk"
#: Hand command topics (S6).
HAND_TOPICS: Final = (
    "rt/dex3/left/cmd",
    "rt/dex3/right/cmd",
    "rt/inspire/cmd",
    "rt/inspire_hand/ctrl/l",
    "rt/inspire_hand/ctrl/r",
    "rt/brainco/left/cmd",
    "rt/brainco/right/cmd",
    "rt/dex1/left/cmd",
    "rt/dex1/right/cmd",
    HAND_SDK,
)
#: Every topic whose samples are commands to a scope.
COMMAND_TOPICS: Final = (LOWCMD, USER_LOWCMD, ARM_SDK, *HAND_TOPICS)
#: Unitree's RPC services the rules use (S7, S8, S9).
SERVICES: Final = ("sport", "motion_switcher", "arm")
#: Virtual topics: the handheld remote inside ``rt/lowstate`` (notes section 4), and Unitree's own
#: fixed motions (arm presets, postures), which no participant of their own announces.
REMOTE_TOPIC: Final = "wireless_remote"
PRESET_TOPIC: Final = "preset"

#: The connector's own constants (notes section 7): Unitree's stick dead zone (S2, gamepad.hpp),
#: the weight limits of a blend, and the longest a preset holds its last pose, seconds (S9).
STICK_DEAD_ZONE: Final = 0.01
WEIGHT_MIN: Final = 0.01
WEIGHT_MAX: Final = 0.99
PRESET_HOLD_S: Final = 20.0

#: Built-in states (S1): passive (no balance control), position-control postures, every posture
#: a person can start, and ``fsm_mode`` while moving.
PASSIVE: Final = frozenset({0, 1})
POSITION_POSTURES: Final = frozenset({2, 3, 4})
POSTURES: Final = frozenset({2, 3, 4, 702, 706})
MOVING: Final = 1

#: API ids (S7, S8, S9).
SET_FSM_ID: Final = 7101
SET_VELOCITY: Final = 7105
SET_ARM_TASK: Final = 7106
USER_CONTROL: Final = 7110
INTERNAL_CONTROL: Final = 7111
SPORT_QUERIES: Final = frozenset(range(7001, 7007))
CHECK_MODE: Final = 1001
SELECT_MODE: Final = 1002
RELEASE_MODE: Final = 1003
ARM_EXECUTE: Final = 7106
ARM_CUSTOM: Final = 7108
ARM_STOP_CUSTOM: Final = 7113
ARM_RELEASE_ID: Final = 99

#: Unitree's continuous Move duration, seconds (S7): the longest duration a request may carry.
MAX_DURATION_S: Final = 864000.0
#: Longest request parameter parsed, in characters; a longer one counts as malformed.
MAX_PARAMETER_CHARS: Final = 65536
#: Most writers, goal spans and pending requests remembered; most writers on a note.
MAX_WRITERS: Final = 1024
MAX_PENDING: Final = 64
MAX_NOTED_WRITERS: Final = 32
_NEUTRAL: Final = 1e-6
_NON_CONTROL: Final = frozenset({"unknown", "idle", "safety_stop"})
_ID_BAD = re.compile(r"[^A-Za-z0-9._:-]")
#: ``script:`` plus 121 characters is the longest identifier (definitions.md 8, Identifiers).
_NAME_MAX = 121
_DIGEST_CHARS = 12

Role = Literal["person", "ai", "script"]

# -- the handheld remote ------------------------------------------------------------------------

#: Bits of the remote's button word (unitree_sdk2 aaf69cb, example/g1/low_level/gamepad.hpp,
#: ``xKeySwitchUnion``: R1 is the lowest bit of byte 2 of ``wireless_remote``).
BUTTONS: Final[Mapping[str, int]] = {
    name: 1 << bit
    for bit, name in enumerate(
        (
            "R1", "L1", "START", "SELECT", "R2", "L2", "F1", "F2",
            "A", "B", "X", "Y", "UP", "RIGHT", "DOWN", "LEFT",
        )
    )
}  # fmt: skip
#: ``xRockerBtnDataStruct``: head[2], the button word, then float32 lx, rx, ry, L2, ly.
_REMOTE_LAYOUT: Final = struct.Struct("<2xH5f")


@dataclass(frozen=True, slots=True)
class RemoteInput:
    """The handheld remote's buttons (a mask of :data:`BUTTONS`) and sticks in [-1, 1]."""

    buttons: int
    lx: float
    rx: float
    ry: float
    ly: float

    def centred(self, dead_zone: float) -> bool:
        """True when every stick is within the dead zone. Open case: a value that is not a
        finite number counts as outside it (the bytes of a remote that is off are not
        documented, notes section 9)."""
        return all(
            math.isfinite(v) and abs(v) <= dead_zone for v in (self.lx, self.rx, self.ry, self.ly)
        )


def decode_remote(data: bytes) -> RemoteInput | None:
    """The remote's input from the 40 bytes of ``LowState_.wireless_remote`` (S2), ``None`` when
    fewer than its first 24 bytes are given."""
    if len(data) < _REMOTE_LAYOUT.size:
        return None
    buttons, lx, rx, ry, _l2, ly = _REMOTE_LAYOUT.unpack_from(data)
    return RemoteInput(buttons, lx, rx, ry, ly)


ComboKind = Literal["stop", "release", "zero_torque", "posture", "preset", "mode"]


@dataclass(frozen=True, slots=True)
class _Combo:
    name: str
    mask: int
    kind: ComboKind
    #: The fsm ids a posture or a passive combination leads to; the preset's name.
    targets: frozenset[int] = frozenset()
    preset: str = ""


def _mask(*names: str) -> int:
    value = 0
    for name in names:
        value |= BUTTONS[name]
    return value


#: The remote's combinations (S2), in the order they are recognised.
_COMBOS: Final = (
    _Combo("L2+B", _mask("L2", "B"), "stop", frozenset({1})),
    _Combo("L2+R2", _mask("L2", "R2"), "release"),
    _Combo("L2+Y", _mask("L2", "Y"), "zero_torque", frozenset({0})),
    _Combo("L2+UP", _mask("L2", "UP"), "posture", frozenset({4})),
    _Combo("L2+LEFT", _mask("L2", "LEFT"), "posture", frozenset({3})),
    _Combo("L2+X", _mask("L2", "X"), "posture", frozenset({702})),
    _Combo("L2+A", _mask("L2", "A"), "posture", frozenset({2, 706})),
    _Combo("SELECT+Y", _mask("SELECT", "Y"), "preset", preset="wave_hand"),
    _Combo("SELECT+A", _mask("SELECT", "A"), "preset", preset="handshake"),
    _Combo("SELECT+X", _mask("SELECT", "X"), "preset", preset="turn_and_wave"),
    _Combo("R1+X", _mask("R1", "X"), "mode"),
    _Combo("R1+Y", _mask("R1", "Y"), "mode"),
    _Combo("R2+A", _mask("R2", "A"), "mode"),
)
#: Combinations named in rules 2, 3, 5 and 6: not goal input (notes section 6, goal span).
_NOT_GOAL: Final = frozenset({"stop", "release", "zero_torque", "preset"})
#: Combinations that change the robot's mode, ending an emergency stop (rule 2).
_MODE_CHANGES: Final = frozenset({"release", "posture", "mode"})


def _combo(buttons: int) -> _Combo | None:
    return next((c for c in _COMBOS if buttons & c.mask == c.mask), None)


# -- observations -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriterSeen:
    """A DDS writer appeared (DCPSPublication): its key, its observed source (host and
    participant of its participant, the topic it writes) and type name. ``decoded`` is false when
    its samples cannot be read (a type the sources do not confirm): it then counts as active while
    it exists (notes section 3)."""

    t: float
    key: str
    source: Source
    type_name: str = ""
    decoded: bool = True


@dataclass(frozen=True, slots=True)
class WriterGone:
    """A writer was disposed or its participant left."""

    t: float
    key: str


@dataclass(frozen=True, slots=True)
class Command:
    """A command sample of ``topic`` (one of :data:`COMMAND_TOPICS`) from the writer ``key``; the
    blend weight in [0, 1] of ``rt/arm_sdk`` and ``rt/hand_sdk``, ``None`` elsewhere."""

    t: float
    key: str
    topic: str
    weight: float | None = None


@dataclass(frozen=True, slots=True)
class LowState:
    """An ``rt/lowstate`` sample: the robot is there, and the remote's 40 bytes (S2)."""

    t: float
    key: str
    wireless_remote: bytes


@dataclass(frozen=True, slots=True)
class SportState:
    """An ``rt/sportmodestate`` sample (S1)."""

    t: float
    key: str
    fsm_id: int
    fsm_mode: int
    task_id: int


@dataclass(frozen=True, slots=True)
class Request:
    """A request on ``rt/api/<service>/request`` (S7): its id, API id and JSON parameter."""

    t: float
    key: str
    service: str
    request_id: int
    api_id: int
    parameter: str


@dataclass(frozen=True, slots=True)
class Response:
    """A response on ``rt/api/<service>/response``: the status code and JSON data (S7)."""

    t: float
    key: str
    service: str
    request_id: int
    api_id: int
    code: int
    data: str


@dataclass(frozen=True, slots=True)
class ArmState:
    """An ``rt/arm/action/state`` sample: its JSON text (S9)."""

    t: float
    key: str
    text: str


@dataclass(frozen=True, slots=True)
class TaskOpened:
    """The recorder's session log shows a ``task_start`` (notes section 8)."""

    t: float
    task_id: str


@dataclass(frozen=True, slots=True)
class TaskClosed:
    """The log shows a ``task_end``; ``task_id`` ``None``: no task is open."""

    t: float
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class TasksUnreadable:
    """The recorder's session log cannot be followed: time counts as outside any task."""

    t: float


Observation = (
    WriterSeen
    | WriterGone
    | Command
    | LowState
    | SportState
    | Request
    | Response
    | ArmState
    | TaskOpened
    | TaskClosed
    | TasksUnreadable
)
Message = StateAssertion | Note


def wire_event(message: Message) -> dict[str, Any]:
    """The event a message stands for, as a connector sends it (README, "Wire format")."""
    return _wire_event(message)


def request_topic(service: str) -> str:
    """``rt/api/<service>/request`` (S7)."""
    return f"rt/api/{service}/request"


def response_topic(service: str) -> str:
    """``rt/api/<service>/response`` (S7)."""
    return f"rt/api/{service}/response"


# -- settings and the registry ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    """What the engine needs, seconds unless stated. The first four come from the spec's
    ``rules.toml`` (definitions.md 9); the constants of notes section 7 have their defaults."""

    devices: tuple[Device, ...]
    gap_tolerance_s: float
    goal_release_s: float
    contention_window_s: float
    idle_threshold_s: float
    hands: bool = False
    unidentified_actor_id: str = "person:unidentified"
    stick_dead_zone: float = STICK_DEAD_ZONE
    weight_min: float = WEIGHT_MIN
    weight_max: float = WEIGHT_MAX
    preset_hold_s: float = PRESET_HOLD_S

    def __post_init__(self) -> None:
        for name in (
            "gap_tolerance_s",
            "goal_release_s",
            "contention_window_s",
            "idle_threshold_s",
            "preset_hold_s",
        ):
            value = getattr(self, name)
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a positive number")
        if not 0 <= self.stick_dead_zone < 1 or not 0 <= self.weight_min < self.weight_max <= 1:
            raise ValueError("dead zone and weight limits must lie in [0, 1] and be ordered")

    @classmethod
    def from_params(cls, params: Any, devices: Iterable[Device], **options: Any) -> "Settings":
        """Settings from the spec's parameter table (``autonometry.spec.Params``) and the
        registry's devices; ``options`` set the rest."""
        return cls(
            devices=tuple(devices),
            gap_tolerance_s=float(params.gap_tolerance_s),
            goal_release_s=float(params.goal_release_s),
            contention_window_s=float(params.contention_window_s),
            idle_threshold_s=float(params.idle_threshold_s),
            **options,
        )

    @property
    def scopes(self) -> tuple[str, ...]:
        """The scopes the engine reports, in order."""
        return (LEGS, UPPER_BODY, HANDS) if self.hands else (LEGS, UPPER_BODY)


def _realizable(match: Mapping[str, str]) -> bool:
    """Whether some observed source carries all these fields: never a ROS node; the preset
    source has no host or participant."""
    if "node" in match:
        return False
    return not (match.get("topic") == PRESET_TOPIC and ("host" in match or "participant" in match))


def registry_problems(devices: Sequence[Device]) -> list[str]:
    """Why a registry cannot be used here, one line per problem (notes section 4): two devices of
    different kind or location that one observed source could match, so that which one counts
    would rest on their order (definitions.md 6.10)."""
    problems = []
    for i, first in enumerate(devices):
        for second in devices[i + 1 :]:
            if (first.kind, first.location) == (second.kind, second.location):
                continue
            a, b = dict(first.match), dict(second.match)
            if any(a[name] != b[name] for name in a.keys() & b.keys()):
                continue
            if _realizable(a | b):
                problems.append(
                    f"devices {first.device_id!r} ({_kind(first)}) and {second.device_id!r} "
                    f"({_kind(second)}) can match the same source; give them a host, participant "
                    "or topic that tells them apart"
                )
    return problems


def _kind(device: Device) -> str:
    return f"{device.kind} {device.location}" if device.location else device.kind


def _role(device: Device | None) -> Role:
    """A support device or an undeclared one counts as a person (definitions.md 6.10)."""
    if device is not None and device.kind == "ai":
        return "ai"
    if device is not None and device.kind == "script":
        return "script"
    return "person"


def _script_name(text: str) -> str:
    """A preset's name for ``script:<name>``: characters outside an identifier's set become
    ``_``; a longer name keeps a 12-hex SHA-256 of the whole and its last characters."""
    name = _ID_BAD.sub("_", text.strip()) or "unnamed"
    if len(name) <= _NAME_MAX:
        return name
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:_DIGEST_CHARS]
    return f"{digest}_{name[-(_NAME_MAX - _DIGEST_CHARS - 1) :]}"


def _parameters(text: str) -> Mapping[str, Any]:
    """A request's JSON parameter as an object; empty when it is not one (never raises)."""
    if len(text) > MAX_PARAMETER_CHARS:
        return {}
    try:
        value = strictjson.parse(text)
    except strictjson.StrictJsonError:
        return {}
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _velocity(params: Mapping[str, Any]) -> tuple[float, ...] | None:
    """SetVelocity's ``velocity`` [vx, vy, omega], ``None`` when malformed."""
    value = params.get("velocity")
    if not isinstance(value, list) or len(value) != 3:  # noqa: PLR2004 -- three components
        return None
    numbers = tuple(_number(v) for v in value)
    return None if any(n is None for n in numbers) else tuple(n or 0.0 for n in numbers)


def _duration(params: Mapping[str, Any]) -> float | None:
    """SetVelocity's ``duration`` in seconds, at most :data:`MAX_DURATION_S`."""
    value = _number(params.get("duration"))
    return None if value is None or value < 0 else min(value, MAX_DURATION_S)


# -- engine state -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Origin:
    """Who did something: a key per source, its device (None when undeclared), its role."""

    key: str
    device: Device | None
    role: Role


@dataclass(slots=True)
class _Writer:
    key: str
    topic: str
    source: Source
    device: Device | None
    identified: bool
    decoded: bool = True
    present: bool = True
    last_t: float | None = None
    weight: float | None = None

    def active(self, now: float, window: float) -> bool:
        """A writer whose latest sample is at most ``window`` old; one whose samples cannot be
        read is active while it exists."""
        if not self.decoded:
            return self.present
        return self.last_t is not None and 0 <= now - self.last_t <= window

    def latest(self, now: float) -> float:
        return now if not self.decoded or self.last_t is None else self.last_t


@dataclass(slots=True)
class _Span:
    """A goal span (notes section 6): open from a goal input until ``until``, unless a neutral
    input from the same source closed it."""

    origin: _Origin
    label: str
    last_input: float
    until: float
    closed_at: float | None = None

    def open_at(self, now: float) -> bool:
        return self.closed_at is None and now < self.until

    def end(self) -> float:
        return self.until if self.closed_at is None else min(self.closed_at, self.until)


@dataclass(frozen=True, slots=True)
class _Trigger:
    """An input that may change the built-in state to one of ``targets``."""

    t: float
    targets: frozenset[int]
    origin: _Origin
    label: str


@dataclass(frozen=True, slots=True)
class _Cause:
    """Who put the built-in controller into ``fsm_id`` (``origin`` None: no observed cause)."""

    fsm_id: int
    origin: _Origin | None
    label: str
    t: float


@dataclass(slots=True)
class _Builtin:
    t: float
    fsm_id: int
    fsm_mode: int
    task_id: int
    device: Device
    moving_since: float | None


@dataclass(slots=True)
class _Stop:
    """An emergency stop, pending until a passive state follows unless ``active`` at once;
    ``passive`` once the built-in state was seen passive during it."""

    t: float
    origin: _Origin
    label: str
    active: bool
    passive: bool = False


@dataclass(slots=True)
class _Preset:
    name: str
    origin: _Origin
    label: str
    custom: bool
    last_sign: float
    held: bool = False
    task_seen: bool = False


@dataclass(frozen=True, slots=True)
class _Pending:
    """A mode request waiting for its response: the mode version it made and the flags before."""

    version: int
    released: bool
    user_control: bool


def host_pseudonym(host: str | None) -> str | None:
    """A host name as it may appear in a log: the first 12 hex digits of its SHA-256, never the
    name itself, which often holds a person's name (CLAUDE.md product rule 5). ``autonometry g1
    discover`` prints it beside each host, so a log entry can be traced to a machine."""
    if host is None:
        return None
    return hashlib.sha256(host.encode("utf-8", "replace")).hexdigest()[:_DIGEST_CHARS]


def _writer_entry(writer: "_Writer") -> dict[str, Any]:
    """A writer on ``x_g1_writers``: a declared one by its topic and device; an undeclared one by
    its topic, program and host pseudonym (product rule 5)."""
    if writer.device is not None:
        return {"topic": writer.topic, "device_id": writer.device.device_id}
    identified = writer.identified
    return {
        "topic": writer.topic,
        "program": writer.source.participant if identified else None,
        "host": host_pseudonym(writer.source.host) if identified else None,
    }


def _value_key(value: StateAssertion) -> tuple[str, str | None, str | None, str | None]:
    return value.state, value.reason, value.actor_id, value.device_id


class G1Engine:
    """Decides a Unitree G1's control states; see the module docstring."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._start: float | None = None
        self._now = 0.0
        self._writers: dict[str, _Writer] = {}
        self._last_lowstate: float | None = None
        self._remote_buttons = 0
        self._builtin: _Builtin | None = None
        self._released = False
        self._user_control = False
        self._mode_version = 0
        self._pending: dict[tuple[str, int], _Pending] = {}
        self._spans: dict[str, _Span] = {}
        self._triggers: list[_Trigger] = []
        self._cause: _Cause | None = None
        self._posture_end: float | None = None
        self._stop: _Stop | None = None
        self._preset: _Preset | None = None
        self._arm_hold: float | None = None
        self._task: str | None = None
        self._tasks_readable = True
        self._last: dict[str, StateAssertion] = {}
        self._last_command: dict[str, float] = {}
        self._sent: dict[str, tuple[str, str | None, str | None, str | None]] = {}
        self._notes: list[Note] = []
        self._noted_writers: str | None = None
        self._preset_device = self._match(Source(topic=PRESET_TOPIC))

    # -- public -----------------------------------------------------------------------------

    def observe(self, observation: Observation) -> list[Message]:
        """Take one observation; return the states that changed and any heartbeat notes, in the
        order they must be sent."""
        self._advance(observation.t)
        self._dispatch(observation)
        self._settle()
        return self._flush(everything=False)

    def tick(self, now: float) -> list[Message]:
        """Let time pass to ``now`` (seconds, the clock of the observations); return the state of
        every scope, which the recorder needs at least every second, and any notes."""
        self._advance(now)
        self._settle()
        self._note_writers()
        return self._flush(everything=True)

    def stop(self) -> list[Message]:
        """The watcher stops: every scope ``unknown``, since nothing is observed any more."""
        values = [self._plain(scope, "unknown", "watcher_stopped") for scope in self._s.scopes]
        self._sent = {value.scope: _value_key(value) for value in values}
        return list(values)

    @property
    def task_open(self) -> bool:
        """True while the recorder's log shows an open task."""
        return self._task is not None

    # -- plumbing ---------------------------------------------------------------------------

    def _advance(self, t: float) -> None:
        if self._start is None:
            self._start = t
            self._now = t
        self._now = max(self._now, t)

    def _flush(self, everything: bool) -> list[Message]:
        out: list[Message] = []
        for scope, value in self._decide().items():
            key = _value_key(value)
            if everything or self._sent.get(scope) != key:
                self._sent[scope] = key
                out.append(value)
        out.extend(self._notes)
        self._notes = []
        return out

    def _settle(self) -> None:
        """Time-driven changes: a pending stop lapses, a preset ends after its hold limit, old
        triggers, spans and writers are forgotten (memory stays bounded)."""
        now, s = self._now, self._s
        if (
            self._stop is not None
            and not self._stop.active
            and now - self._stop.t > s.gap_tolerance_s
        ):
            self._stop = None
        if self._preset is not None and now - self._preset.last_sign > s.preset_hold_s:
            self._preset = None
        if self._arm_hold is not None and now - self._arm_hold > s.preset_hold_s:
            self._arm_hold = None
        self._triggers = [t for t in self._triggers if now - t.t <= s.gap_tolerance_s]
        stale = [k for k, span in self._spans.items() if now - span.end() > s.idle_threshold_s]
        for key in stale:
            del self._spans[key]
        gone = [
            key
            for key, w in self._writers.items()
            if not w.present and (w.last_t is None or now - w.last_t > s.gap_tolerance_s)
        ]
        for key in gone:
            del self._writers[key]

    def _dispatch(self, observation: Observation) -> None:  # noqa: PLR0912 -- one branch per kind
        match observation:
            case WriterSeen():
                self._on_writer(observation)
            case WriterGone(key=key):
                if key in self._writers:
                    self._writers[key].present = False
            case Command():
                self._on_command(observation)
            case LowState():
                self._on_lowstate(observation)
            case SportState():
                self._on_sport(observation)
            case Request():
                self._on_request(observation)
            case Response():
                self._on_response(observation)
            case ArmState():
                self._on_arm_state(observation)
            case TaskOpened(task_id=task_id):
                self._task, self._tasks_readable = task_id, True
                self._touch(self._s.scopes, observation.t)
            case TaskClosed(task_id=task_id):
                if task_id is None or task_id == self._task:
                    if self._task is not None:
                        self._touch(self._s.scopes, observation.t)
                    self._task = None
                self._tasks_readable = True
            case TasksUnreadable():
                self._task = None
                if self._tasks_readable:
                    self._notes.append(Note((("x_g1_tasks", "unreadable"),)))
                self._tasks_readable = False

    def _touch(self, scopes: Iterable[str], t: float) -> None:
        """A command reached these scopes: their idle clock restarts (definitions.md 2, idle)."""
        for scope in scopes:
            self._last_command[scope] = max(self._last_command.get(scope, t), t)

    # -- identities -------------------------------------------------------------------------

    def _match(self, source: Source) -> Device | None:
        return next((d for d in self._s.devices if d.matches(source)), None)

    def _writer(self, key: str, topic: str) -> _Writer:
        """The writer ``key``; one not yet seen in discovery is undeclared until it is."""
        writer = self._writers.get(key)
        if writer is None:
            writer = _Writer(key, topic, Source(topic=topic), None, identified=False)
            self._add_writer(writer)
        return writer

    def _add_writer(self, writer: _Writer) -> None:
        if len(self._writers) >= MAX_WRITERS:
            oldest = min(
                self._writers.values(), key=lambda w: (w.present, w.last_t or -math.inf, w.key)
            )
            del self._writers[oldest.key]
        self._writers[writer.key] = writer

    def _on_writer(self, observation: WriterSeen) -> None:
        source = observation.source
        topic = source.topic or ""
        writer = self._writers.get(observation.key)
        if writer is None:
            writer = _Writer(observation.key, topic, source, None, identified=True)
            self._add_writer(writer)
        writer.topic, writer.source, writer.identified = topic, source, True
        writer.device = self._match(source)
        writer.decoded, writer.present = observation.decoded, True

    def _origin(self, writer: _Writer) -> _Origin:
        device = writer.device if writer.identified else None
        if device is not None:
            return _Origin(f"device:{device.device_id}", device, _role(device))
        if writer.identified:
            key = f"undeclared:{writer.source.host}|{writer.source.participant}"
        else:
            key = f"unidentified:{writer.key}"
        return _Origin(key, None, "person")

    def _remote_origin(self, writer: _Writer) -> _Origin | None:
        """The handheld remote, when the ``rt/lowstate`` writer is the one its device names."""
        if not writer.identified:
            return None
        source = Source(
            host=writer.source.host, participant=writer.source.participant, topic=REMOTE_TOPIC
        )
        device = self._match(source)
        if device is None or device.kind != "human":
            return None
        return _Origin(f"device:{device.device_id}", device, "person")

    # -- observations -----------------------------------------------------------------------

    def _on_command(self, observation: Command) -> None:
        topic, t = observation.topic, observation.t
        if topic not in COMMAND_TOPICS:
            return
        writer = self._writer(observation.key, topic)
        writer.last_t = t if writer.last_t is None else max(writer.last_t, t)
        if observation.weight is not None:
            weight = observation.weight
            writer.weight = min(max(weight, 0.0), 1.0) if math.isfinite(weight) else None
        if topic in (LOWCMD, USER_LOWCMD):
            self._touch((LEGS, UPPER_BODY), t)
        elif topic == ARM_SDK and self._weight(writer) > self._s.weight_min:
            self._touch((UPPER_BODY,), t)
            if self._preset is not None and _role(writer.device) == "script":
                self._preset.last_sign = t  # the arm action service's writes are the preset
        elif topic in HAND_TOPICS and self._weight(writer) > self._s.weight_min:
            self._touch((HANDS,), t)

    @staticmethod
    def _weight(writer: _Writer) -> float:
        """Open case: a weight that cannot be read counts as 1, the writer holding its joints."""
        return 1.0 if writer.weight is None else writer.weight

    def _on_lowstate(self, observation: LowState) -> None:
        t = observation.t
        writer = self._writer(observation.key, LOWSTATE)
        writer.last_t = t
        last = self._last_lowstate
        if (
            last is not None
            and t - last > self._s.idle_threshold_s
            and (self._released or self._user_control)
        ):
            # The robot was away long enough to have restarted, which ends a release or user
            # control without a request: the mode stands (it never moves towards ai), and the
            # log says the robot returned in it (README, restart the watcher too).
            mode = "released" if self._released else "user_control"
            self._notes.append(Note((("x_g1_robot_returned", mode),)))
        self._last_lowstate = t if last is None else max(last, t)
        origin = self._remote_origin(writer)
        remote = decode_remote(observation.wireless_remote)
        if origin is None or remote is None:
            return
        previous, self._remote_buttons = self._remote_buttons, remote.buttons
        combo = _combo(remote.buttons)
        rising = combo is not None and previous & combo.mask != combo.mask
        goal_buttons = remote.buttons
        if combo is not None:
            if combo.kind in _NOT_GOAL:
                goal_buttons &= ~combo.mask
            self._on_combo(combo, rising, origin, t)
            self._touch((LEGS, UPPER_BODY), t)
        if goal_buttons or not remote.centred(self._s.stick_dead_zone):
            label = "remote" if combo is None else f"remote:{combo.name}"
            self._goal(origin, t, label, None)
            self._touch((LEGS, UPPER_BODY), t)
        else:
            self._neutral(origin, t)

    def _on_combo(self, combo: _Combo, rising: bool, origin: _Origin, t: float) -> None:
        label = f"remote:{combo.name}"
        if combo.targets:
            self._add_trigger(_Trigger(t, combo.targets, origin, label))
        if rising and combo.kind in _MODE_CHANGES:
            self._mode_change()
        if combo.kind == "stop":
            self._stop_trigger(origin, label, t)
        elif combo.kind == "release" and rising:
            self._released = True
        elif combo.kind == "preset" and rising:
            self._start_preset(combo.preset, origin, label, t, custom=False)

    def _on_sport(self, observation: SportState) -> None:
        t, s = observation.t, self._s
        writer = self._writer(observation.key, SPORTSTATE)
        writer.last_t = t
        device = writer.device if writer.identified else None
        if device is None or device.kind != "ai":
            return  # only the declared built-in controller's state counts (S1)
        previous = self._builtin
        fresh = previous is not None and t - previous.t <= s.gap_tolerance_s
        posture_before = self._posture_moving()
        since = None
        if observation.fsm_mode == MOVING:
            kept = previous.moving_since if previous is not None else None
            since = kept if fresh and previous is not None and kept is not None else t
        self._builtin = _Builtin(
            t, observation.fsm_id, observation.fsm_mode, observation.task_id, device, since
        )
        if previous is None or not fresh or previous.fsm_id != observation.fsm_id:
            self._cause = self._find_cause(observation.fsm_id, t)
        if posture_before and not self._posture_moving():
            self._posture_end = t
        passive = observation.fsm_id in PASSIVE
        stop = self._stop
        if stop is not None and stop.active and stop.passive and not passive:
            # The built-in state left passive. Open case: a stop that began at once (released,
            # or the controller unseen) and never saw a passive state ends only at a change of
            # mode, so a built-in state that never stopped does not end it.
            self._stop = None
        elif stop is not None and passive and (stop.active or 0 <= t - stop.t <= s.gap_tolerance_s):
            stop.active = stop.passive = True
        preset = self._preset
        if preset is not None and observation.task_id != 0:
            preset.last_sign, preset.task_seen = t, True
        elif preset is not None and preset.task_seen:
            self._preset = None
        if observation.fsm_mode == MOVING:
            self._touch((LEGS, UPPER_BODY), t)

    def _on_request(self, observation: Request) -> None:
        service = observation.service
        if service not in SERVICES:
            return
        writer = self._writer(observation.key, request_topic(service))
        writer.last_t = observation.t
        origin = self._origin(writer)
        params = _parameters(observation.parameter)
        if service == "sport":
            self._sport_request(observation, origin, params)
        elif service == "motion_switcher":
            if observation.api_id in (SELECT_MODE, RELEASE_MODE):
                released = observation.api_id == RELEASE_MODE
                self._mode_request(observation, released, self._user_control)
                self._touch((LEGS, UPPER_BODY), observation.t)
        else:
            self._arm_request(observation, origin, params)

    def _sport_request(
        self, observation: Request, origin: _Origin, params: Mapping[str, Any]
    ) -> None:
        api, t = observation.api_id, observation.t
        if api in SPORT_QUERIES:
            return
        label = f"request:sport:{api}"
        self._touch((LEGS, UPPER_BODY), t)
        if api == SET_VELOCITY:
            velocity = _velocity(params)
            if velocity is None:
                # Open case: a malformed velocity is still input, and its duration may only
                # extend a span: the robot keeps the command it had.
                self._goal(origin, t, label, None)
            elif all(abs(v) < _NEUTRAL for v in velocity):
                self._neutral(origin, t)
            else:
                self._goal(origin, t, label, _duration(params))
        else:
            self._goal(origin, t, label, None)
        if api == SET_FSM_ID:
            fsm_id = _int(params.get("data"))
            if fsm_id is not None:
                self._add_trigger(_Trigger(t, frozenset({fsm_id}), origin, label))
                if fsm_id in PASSIVE and origin.role == "person":
                    self._stop_trigger(origin, label, t)
        elif api == SET_ARM_TASK:
            task = _int(params.get("data"))
            name = "arm_task" if task is None else f"arm_task_{task}"
            self._start_preset(name, origin, label, t, custom=False)
        elif api in (USER_CONTROL, INTERNAL_CONTROL):
            self._mode_request(observation, self._released, api == USER_CONTROL)

    def _arm_request(
        self, observation: Request, origin: _Origin, params: Mapping[str, Any]
    ) -> None:
        api, t = observation.api_id, observation.t
        label = f"request:arm:{api}"
        if api == ARM_EXECUTE:
            action = _int(params.get("action_id"))
            if action == ARM_RELEASE_ID:
                self._preset = None
            else:
                name = "arm_action" if action is None else f"arm_action_{action}"
                self._start_preset(name, origin, label, t, custom=False)
        elif api == ARM_CUSTOM:
            text = params.get("action_name")
            name = f"custom_{text}" if isinstance(text, str) else "custom"
            self._start_preset(_script_name(name), origin, label, t, custom=True)
        elif api == ARM_STOP_CUSTOM and self._preset is not None and self._preset.custom:
            self._preset = None
        else:
            return
        self._touch((UPPER_BODY,), t)

    def _mode_request(self, observation: Request, released: bool, user_control: bool) -> None:
        """A mode change by request (notes section 6, released and user control); its response
        is awaited (:meth:`_on_response`)."""
        prior = (self._released, self._user_control)
        self._mode_change()
        self._released, self._user_control = released, user_control
        key = (observation.service, observation.request_id)
        self._pending.pop(key, None)
        if len(self._pending) >= MAX_PENDING:
            del self._pending[next(iter(self._pending))]
        self._pending[key] = _Pending(self._mode_version, *prior)

    def _on_response(self, observation: Response) -> None:
        """Open case: the notes define released and user control by requests alone. A response
        only ever keeps the robot in them (rule 3), never takes it out: a refused SelectMode or
        SwitchToInternalCtrl leaves it released or in user control, and a CheckMode answer with
        no mode (S8) marks it released. A refused ReleaseMode is not undone."""
        pending = self._pending.pop((observation.service, observation.request_id), None)
        if pending is not None and observation.code != 0 and pending.version == self._mode_version:
            self._released = self._released or pending.released
            self._user_control = self._user_control or pending.user_control
        if (
            observation.service == "motion_switcher"
            and observation.api_id == CHECK_MODE
            and observation.code == 0
            and _parameters(observation.data).get("name") == ""
        ):
            self._released = True

    def _on_arm_state(self, observation: ArmState) -> None:
        params = _parameters(observation.text)
        holding = params.get("holding")
        action = _int(params.get("id"))
        preset, t = self._preset, observation.t
        if preset is not None:
            # Open case: "no hold" ends a preset only after it held, so a state published before
            # the preset began cannot end it at once (a premature end would count it as ai).
            if action == ARM_RELEASE_ID or (holding is False and preset.held):
                self._preset = None
            elif holding is True:
                preset.last_sign, preset.held = t, True
        elif holding is True and action != ARM_RELEASE_ID:
            self._arm_hold = t
        elif holding is False or action == ARM_RELEASE_ID:
            self._arm_hold = None
        if holding is True:
            self._touch((UPPER_BODY,), t)

    # -- inputs -----------------------------------------------------------------------------

    def _goal(self, origin: _Origin, t: float, label: str, duration: float | None) -> None:
        """A goal input (6.11): the span lasts ``goal_release_s`` after it, or to the end of the
        duration a velocity command carries, whichever is later."""
        release_end = t + self._s.goal_release_s
        span = self._spans.get(origin.key)
        if duration is not None:
            until = max(release_end, t + duration)
        elif span is not None and span.open_at(t):
            until = max(span.until, release_end)
        else:
            until = release_end
        self._spans[origin.key] = _Span(origin, label, t, until)

    def _neutral(self, origin: _Origin, t: float) -> None:
        """A neutral input ends its source's span at once and never opens one (6.11)."""
        span = self._spans.get(origin.key)
        if span is not None and span.open_at(t):
            span.closed_at = t

    def _add_trigger(self, trigger: _Trigger) -> None:
        """Remember an input that may change the built-in state; it also names the cause of a
        change seen just before it (the readers' order within one pass)."""
        self._triggers.append(trigger)
        cause, builtin = self._cause, self._builtin
        if (
            cause is not None
            and cause.origin is None
            and builtin is not None
            and builtin.fsm_id == cause.fsm_id
            and cause.fsm_id in trigger.targets
            and 0 <= trigger.t - cause.t <= self._s.gap_tolerance_s
        ):
            self._cause = _Cause(cause.fsm_id, trigger.origin, trigger.label, cause.t)

    def _find_cause(self, fsm_id: int, t: float) -> _Cause:
        found = [
            trigger
            for trigger in self._triggers
            if fsm_id in trigger.targets and abs(t - trigger.t) <= self._s.gap_tolerance_s
        ]
        if not found:
            return _Cause(fsm_id, None, "unobserved", t)
        latest = max(found, key=lambda trigger: trigger.t)
        return _Cause(fsm_id, latest.origin, latest.label, t)

    def _stop_trigger(self, origin: _Origin, label: str, t: float) -> None:
        """An emergency-stop control (rule 2): a stop at once while the built-in controller is
        unseen or released, else once a passive state follows within ``gap_tolerance_s``."""
        if self._stop is not None and self._stop.active:
            return
        immediate = self._released or self._builtin_unseen(t)
        self._stop = _Stop(t, origin, label, immediate)

    def _mode_change(self) -> None:
        """A change of mode ends an emergency stop (rule 2)."""
        self._stop = None
        self._mode_version += 1

    def _start_preset(
        self, name: str, origin: _Origin, label: str, t: float, *, custom: bool
    ) -> None:
        self._preset = _Preset(name, origin, label, custom, t)
        self._arm_hold = None
        self._touch((UPPER_BODY,), t)

    # -- the decision -----------------------------------------------------------------------

    def _lost(self, now: float) -> bool:
        last = self._last_lowstate
        return last is None or now - last > self._s.gap_tolerance_s

    def _builtin_unseen(self, now: float) -> bool:
        builtin = self._builtin
        return builtin is None or now - builtin.t > self._s.gap_tolerance_s

    def _decide(self) -> dict[str, StateAssertion]:
        now, scopes = self._now, self._s.scopes
        if self._lost(now):
            return {scope: self._plain(scope, "unknown", "robot_lost") for scope in scopes}
        if self._stop is not None and self._stop.active:
            return {scope: self._stop_value(scope) for scope in scopes}
        legs, upper = self._body(now)
        out = {LEGS: legs, UPPER_BODY: upper}
        if self._s.hands:
            out[HANDS] = self._hands(now)
        for scope, value in out.items():
            if value.state not in _NON_CONTROL:
                self._last[scope] = value
        return out

    def _body(self, now: float) -> tuple[StateAssertion, StateAssertion]:
        builtin = self._builtin
        if self._released or self._user_control:
            topics = ((LOWCMD,) if self._released else ()) + (
                (USER_LOWCMD,) if self._user_control else ()
            )
            return self._follow(now, topics, "released" if self._released else "user_control")
        if builtin is None or self._builtin_unseen(now):
            if self._active(now, (LOWCMD,)):
                return self._follow(now, (LOWCMD,), "builtin_unseen")
            return self._plain(LEGS, "unknown", "builtin_unseen"), self._plain(
                UPPER_BODY, "unknown", "builtin_unseen"
            )
        if builtin.fsm_id in PASSIVE:
            return self._passive(LEGS, now), self._passive(UPPER_BODY, now)
        legs = self._legs(now, builtin)
        return legs, self._upper(now, builtin, legs)

    def _follow(
        self, now: float, topics: tuple[str, ...], mode: str
    ) -> tuple[StateAssertion, StateAssertion]:
        """Rule 3: the scopes follow the active writers of ``topics``."""
        writers = self._active(now, topics)
        values = []
        for scope in (LEGS, UPPER_BODY):
            if writers:
                value = self._commanders(scope, writers, now, f"writer:{mode}")
            elif self._task is not None:
                value = self._plain(scope, "unknown", f"no_writer:{mode}")
            else:
                value = self._idle_or_last(scope, now, f"no_writer:{mode}")
            values.append(self._with_goals(value, now))
        return values[0], values[1]

    def _passive(self, scope: str, now: float) -> StateAssertion:
        """Rule 5 (and 7c): the hold of whoever caused the state; outside a task the last
        controller's state until idle."""
        if self._task is None:
            return self._with_goals(self._idle_or_last(scope, now, "passive"), now)
        cause = self._cause
        if cause is None or cause.origin is None:
            return self._plain(scope, "unknown", "unobserved")
        value = self._origin_value(scope, cause.origin, cause.label, shared=True)
        return self._with_goals(value, now)

    def _legs(self, now: float, builtin: _Builtin) -> StateAssertion:  # noqa: PLR0911 -- one per rule
        """Rule 7."""
        spans = self._open_spans(now)
        if spans:
            return self._goal_value(LEGS, spans)
        posture = self._posture_value(LEGS)
        if posture is not None:
            return posture
        if builtin.fsm_mode == MOVING:
            explained = max(
                builtin.moving_since if builtin.moving_since is not None else now,
                self._last_goal_end(now),
                self._posture_end if self._posture_end is not None else -math.inf,
            )
            if now - explained > self._s.gap_tolerance_s:
                return self._plain(LEGS, "unknown", "unobserved")
            last = self._last.get(LEGS)
            return last if last is not None else self._plain(LEGS, "unknown", "unobserved")
        if builtin.fsm_id in POSITION_POSTURES:
            return self._passive(LEGS, now)
        if self._task is not None:
            return self._builtin_value(LEGS, builtin)
        return self._idle_or_last(LEGS, now, "builtin")

    def _upper(self, now: float, builtin: _Builtin, legs: StateAssertion) -> StateAssertion:
        """Rule 6."""
        arm = [w for w in self._active(now, (ARM_SDK,)) if self._weight(w) > self._s.weight_min]
        preset = self._preset
        if preset is not None:
            value = self._preset_value(preset)
            # Open case: a source other than a script writing rt/arm_sdk while a preset runs
            # contends with it (6.12), so a person taking the arms never stays scripted.
            others = [w for w in arm if _role(w.device) != "script"]
            if others:
                runner = _Origin(PRESET_TOPIC, self._preset_device, "script")
                origins = [runner, *self._by_origin(others, now)]
                return self._contention(UPPER_BODY, origins, "contention:preset", value)
            return value
        if self._unobserved_arm(now, builtin):
            # Open case: an arm motion with no observed start (the app, the voice service).
            return self._plain(UPPER_BODY, "unknown", "unobserved")
        if arm:
            return self._blend(now, arm, builtin)
        return replace(legs, scope=UPPER_BODY)

    def _hands(self, now: float) -> StateAssertion:
        """Rule 8."""
        writers = [
            w for w in self._active(now, HAND_TOPICS) if self._weight(w) > self._s.weight_min
        ]
        if any(not w.decoded for w in writers):
            self._touch((HANDS,), now)
        if writers:
            return self._commanders(HANDS, writers, now, "writer")
        if self._task is not None:
            return self._plain(HANDS, "unknown", "no_writer")
        return self._idle_or_last(HANDS, now, "no_writer")

    # -- parts of the decision --------------------------------------------------------------

    def _active(self, now: float, topics: Iterable[str]) -> list[_Writer]:
        wanted = set(topics)
        return sorted(
            (
                w
                for w in self._writers.values()
                if w.topic in wanted and w.active(now, self._s.gap_tolerance_s)
            ),
            key=lambda w: (w.latest(now), w.key),
        )

    def _by_origin(self, writers: Iterable[_Writer], now: float) -> list[_Origin]:
        """The sources of these writers, least recent first; one per source."""
        latest: dict[str, tuple[float, _Origin]] = {}
        for writer in writers:
            origin = self._origin(writer)
            t = writer.latest(now)
            if origin.key not in latest or latest[origin.key][0] < t:
                latest[origin.key] = (t, origin)
        return [origin for _, origin in sorted(latest.values(), key=lambda i: (i[0], i[1].key))]

    def _commanders(
        self, scope: str, writers: Sequence[_Writer], now: float, cause: str
    ) -> StateAssertion:
        """One source: its device's state; several within ``contention_window_s``: 6.12."""
        timed: dict[str, tuple[float, _Origin]] = {}
        for writer in writers:
            origin = self._origin(writer)
            t = writer.latest(now)
            if origin.key not in timed or timed[origin.key][0] < t:
                timed[origin.key] = (t, origin)
        ordered = sorted(timed.values(), key=lambda i: (i[0], i[1].key))
        lead_t = ordered[-1][0]
        rivals = [o for t, o in ordered if lead_t - t <= self._s.contention_window_s]
        if len(rivals) == 1:
            return self._origin_value(scope, rivals[0], cause, shared=False)
        return self._contention(scope, rivals, "contention", None)

    def _blend(self, now: float, writers: Sequence[_Writer], builtin: _Builtin) -> StateAssertion:
        """Rule 6b: the writers of ``rt/arm_sdk``; below ``weight_max`` the built-in controller
        is blended in (6.12)."""
        origins = self._by_origin(writers, now)
        lead_t = max(w.latest(now) for w in writers)
        rivals_keys = {
            self._origin(w).key
            for w in writers
            if lead_t - w.latest(now) <= self._s.contention_window_s
        }
        rivals = [o for o in origins if o.key in rivals_keys]
        weights = [self._weight(w) for w in writers if self._origin(w).key in rivals_keys]
        weight = min(weights)
        extra: Fields = (("x_g1_weight", round(weight, 4)),)
        if len(rivals) == 1 and weight >= self._s.weight_max:
            return self._origin_value(UPPER_BODY, rivals[0], "writer", shared=False, extra=extra)
        if weight < self._s.weight_max:
            device = builtin.device
            rivals = [_Origin(f"device:{device.device_id}", device, "ai"), *rivals]
        return self._contention(UPPER_BODY, rivals, "blend", None, extra)

    def _open_spans(self, now: float) -> list[_Span]:
        return sorted(
            (s for s in self._spans.values() if s.open_at(now)),
            key=lambda s: (s.last_input, s.origin.key),
        )

    def _last_goal_end(self, now: float) -> float:
        ends = [span.end() for span in self._spans.values() if span.end() <= now]
        return max(ends, default=-math.inf)

    def _goal_value(self, scope: str, spans: Sequence[_Span]) -> StateAssertion:
        """Rule 7a: one source with an open goal span: a person ``shared`` (6.11), an undeclared
        one ``human_remote``, a program its state; several: 6.12."""
        lead = spans[-1]
        cause = f"goal:{lead.label}"
        if len(spans) == 1:
            origin = lead.origin
            return self._origin_value(scope, origin, cause, shared=origin.device is not None)
        # Open case: every source with an open span contends, however far apart their last
        # inputs: a span open now is a source commanding now (a SetVelocity runs its duration).
        return self._contention(scope, [s.origin for s in spans], "contention:goal", None)

    def _posture_moving(self) -> bool:
        builtin, cause = self._builtin, self._cause
        return (
            builtin is not None
            and cause is not None
            and cause.origin is not None
            and builtin.fsm_id in POSTURES
            and builtin.fsm_id == cause.fsm_id
            and builtin.fsm_mode == MOVING
        )

    def _posture_value(self, scope: str) -> StateAssertion | None:
        """A posture a source started, while it moves: a fixed motion (6.13)."""
        if not self._posture_moving():
            return None
        cause = self._cause
        assert cause is not None  # noqa: S101 -- checked above
        assert cause.origin is not None  # noqa: S101 -- checked above
        reason = "human_initiated" if cause.origin.role == "person" else None
        device = self._preset_device
        return StateAssertion(
            scope,
            "scripted",
            reason,
            f"script:fsm_{cause.fsm_id}",
            None if device is None else device.device_id,
            self._fields(cause.label),
        )

    def _preset_value(self, preset: _Preset) -> StateAssertion:
        reason = "human_initiated" if preset.origin.role == "person" else None
        device = self._preset_device
        return StateAssertion(
            UPPER_BODY,
            "scripted",
            reason,
            f"script:{preset.name}",
            None if device is None else device.device_id,
            self._fields(f"preset:{preset.label}"),
        )

    def _unobserved_arm(self, now: float, builtin: _Builtin) -> bool:
        return builtin.task_id != 0 or (
            self._arm_hold is not None and now - self._arm_hold <= self._s.preset_hold_s
        )

    def _builtin_value(self, scope: str, builtin: _Builtin) -> StateAssertion:
        device = builtin.device
        return StateAssertion(
            scope, "ai", None, f"ai:{device.device_id}", device.device_id, self._fields("builtin")
        )

    def _with_goals(self, value: StateAssertion, now: float) -> StateAssertion:
        """Open case: a person's open goal span contends (6.12) with an AI or scripted state on
        a scope that follows it, so a person's live input is never counted as the AI's."""
        if value.state not in ("ai", "scripted"):
            return value
        persons = [s for s in self._open_spans(now) if s.origin.role == "person"]
        if not persons:
            return value
        return self._origin_value(
            value.scope, persons[-1].origin, f"contention:goal:{persons[-1].label}", shared=True
        )

    def _idle_or_last(self, scope: str, now: float, cause: str) -> StateAssertion:
        """Outside an open task: the last controller's state until ``idle`` (6.14)."""
        start = self._start if self._start is not None else now
        if now - self._last_command.get(scope, start) >= self._s.idle_threshold_s:
            return self._plain(scope, "idle", "idle")
        last = self._last.get(scope)
        return last if last is not None else self._plain(scope, "unknown", cause)

    # -- values -----------------------------------------------------------------------------

    def _fields(self, cause: str, extra: Fields = ()) -> Fields:
        fields: Fields = (("x_g1_cause", cause),)
        if self._builtin is not None:
            fields += (("x_g1_fsm_id", self._builtin.fsm_id),)
        return fields + extra

    def _plain(self, scope: str, state: str, cause: str) -> StateAssertion:
        return StateAssertion(scope, state, fields=self._fields(cause))

    def _stop_value(self, scope: str) -> StateAssertion:
        stop = self._stop
        assert stop is not None  # noqa: S101 -- only called during a stop
        device = stop.origin.device
        actor = device.actor_id if device is not None and device.actor_id else None
        return StateAssertion(
            scope,
            "safety_stop",
            "safety",
            actor or self._s.unidentified_actor_id,
            None if device is None else device.device_id,
            self._fields(stop.label),
        )

    def _origin_value(
        self, scope: str, origin: _Origin, cause: str, *, shared: bool, extra: Fields = ()
    ) -> StateAssertion:
        """One source's state: a person ``shared`` when ``shared``, else by the device's
        location; undeclared or support ``human_remote`` (6.10); a program ``ai`` or
        ``scripted``."""
        device = origin.device
        if origin.role == "person":
            if device is None or device.kind == "support":
                state = "human_remote"
            elif shared:
                state = "shared"
            else:
                state = "human_local" if device.location == "local" else "human_remote"
            actor = device.actor_id if device is not None and device.actor_id else None
            return StateAssertion(
                scope,
                state,
                "human_initiated",
                actor or self._s.unidentified_actor_id,
                None if device is None else device.device_id,
                self._fields(cause, extra),
            )
        assert device is not None  # noqa: S101 -- a program's role comes from its device
        state, prefix = ("ai", "ai") if origin.role == "ai" else ("scripted", "script")
        return StateAssertion(
            scope,
            state,
            None,
            f"{prefix}:{device.device_id}",
            device.device_id,
            self._fields(cause, extra),
        )

    def _contention(
        self,
        scope: str,
        origins: Sequence[_Origin],
        cause: str,
        fallback: StateAssertion | None,
        extra: Fields = (),
    ) -> StateAssertion:
        """Several sources at once (6.12), least recent first: ``shared`` with reason
        ``human_initiated`` when any is a person; else ``ai`` when any is an AI controller; else
        ``scripted`` (``fallback`` when the latest script has no device)."""
        persons = [o for o in origins if o.role == "person"]
        if persons:
            return self._origin_value(scope, persons[-1], cause, shared=True, extra=extra)
        machines = [o for o in origins if o.role == "ai"] or [
            o for o in origins if o.device is not None
        ]
        if machines:
            return self._origin_value(scope, machines[-1], cause, shared=False, extra=extra)
        assert fallback is not None  # noqa: S101 -- only the preset has no device
        return fallback

    # -- notes ------------------------------------------------------------------------------

    def _note_writers(self) -> None:
        """``x_g1_writers`` on a heartbeat when the set of active writers changed (notes 6)."""
        now = self._now
        active = [w for w in self._writers.values() if w.active(now, self._s.gap_tolerance_s)]
        entries = sorted(
            (_writer_entry(w) for w in active), key=lambda e: json.dumps(e, sort_keys=True)
        )
        text = json.dumps(entries, sort_keys=True)
        if text == self._noted_writers:
            return
        self._noted_writers = text
        fields: Fields = (("x_g1_writers", entries[:MAX_NOTED_WRITERS]),)
        if len(entries) > MAX_NOTED_WRITERS:
            fields += (("x_g1_writers_more", len(entries) - MAX_NOTED_WRITERS),)
        self._notes.append(Note(fields))
