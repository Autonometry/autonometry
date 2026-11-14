"""Unitree G1 input: read the robot's DDS traffic, subscribe-only (README, "Unitree G1").

The edge of :mod:`autonometry.connectors.g1`, which makes every decision. This module reads the
network, the process table and the recorder's session log, and hands over what it read as plain
observations. It never creates a DataWriter and never sends an RPC request, not even a query: a
request is a write to the robot's services (integration notes section 3). Its readers are
best-effort and volatile, so the robot's writers never wait for or resend to it.

Cyclone DDS for Python is not a declared dependency: there are no 0.10 wheels for Python 3.12,
so it is built from source (README). It is imported only inside the functions that need it, after
:func:`check_cyclonedds` accepted its version: only the 0.10 line, the robot's own, since an
observer on Cyclone DDS 11.1 crashed 0.10 publishers as soon as it subscribed (research
``unitree-g1/dds_compat``). psutil (the ``g1`` extra) names Python programs by their script.

Message types are written from the field lists of unitree_sdk2_python at commit
``814556d15970dd2ecf1c9984e845ca02ab07e206`` (``unitree_sdk2py/idl/unitree_hg``, ``unitree_go``,
``unitree_api`` and ``std_msgs``) and, for ``SportModeState_``, unitree_sdk2 at commit
``aaf69cb56c9d5ed63d8b284008ec3a8ab999bec1`` (``include/unitree/idl/hg/SportModeState_.hpp``);
both BSD-3-Clause, Copyright Unitree Robotics. A reader is created only for a topic whose writer
announces the expected type name; any other writer counts by its presence alone.
"""

import functools
import importlib.metadata
import json
import logging
import math
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

from autonometry import strictjson
from autonometry.connectors import g1
from autonometry.connectors.lerobot_io import LogFollower
from autonometry.connectors.tb4 import Source
from autonometry.recorder.store import EVENTS_FILE, session_dirs

__all__ = [
    "EXPECTED_TYPES",
    "PASS_S",
    "TICK_S",
    "Backend",
    "Discovery",
    "G1Error",
    "Observer",
    "ParticipantRecord",
    "PublicationRecord",
    "SampleRecord",
    "TaskFollower",
    "check_cyclonedds",
    "check_python",
    "cyclonedds_version",
    "discover",
    "domain_config",
    "format_discovery",
    "open_backend",
    "participant_name",
    "python_target",
    "recorder_sender",
    "resolve_script",
    "run_watch",
]

logger = logging.getLogger(__name__)

#: The Cyclone DDS line the G1 runs (unitree_sdk2 bundles 0.10.2; S11).
SUPPORTED_LINE: Final = (0, 10)
#: How often the readers are taken, and how often every scope's state is sent, seconds.
PASS_S: Final = 0.05
TICK_S: Final = 0.25
#: How often the recorder's session folder is listed for a new session, seconds.
TASK_CHECK_S: Final = 1.0
#: History of the command and request readers; ``rt/lowstate`` keeps only its latest sample.
HISTORY: Final = 64
#: Longest text kept from a sample or a property, in characters.
MAX_TEXT: Final = g1.MAX_PARAMETER_CHARS
MAX_NAME: Final = 256
#: Most participants and publications remembered (a network can hold many).
MAX_ENDPOINTS: Final = 4096
#: DDS domain ids (DDSI-RTPS port mapping).
MAX_DOMAIN: Final = 232

#: Type names (as Cyclone writes them on the wire) a reader is created for, per topic.
EXPECTED_TYPES: Final[Mapping[str, str]] = {
    g1.LOWSTATE: "unitree_hg::msg::dds_::LowState_",
    g1.SPORTSTATE: "unitree_hg::msg::dds_::SportModeState_",
    g1.LOWCMD: "unitree_hg::msg::dds_::LowCmd_",
    g1.USER_LOWCMD: "unitree_hg::msg::dds_::LowCmd_",
    g1.ARM_SDK: "unitree_hg::msg::dds_::LowCmd_",
    "rt/dex3/left/cmd": "unitree_hg::msg::dds_::HandCmd_",
    "rt/dex3/right/cmd": "unitree_hg::msg::dds_::HandCmd_",
    g1.HAND_SDK: "unitree_go::msg::dds_::MotorCmds_",
    g1.ARM_STATE: "std_msgs::msg::dds_::String_",
    **{g1.request_topic(s): "unitree_api::msg::dds_::Request_" for s in g1.SERVICES},
    **{g1.response_topic(s): "unitree_api::msg::dds_::Response_" for s in g1.SERVICES},
}
#: Every topic the watcher follows; writers of other topics are ignored (``discover`` lists all).
WATCHED: Final = frozenset(
    {
        g1.LOWSTATE,
        g1.SPORTSTATE,
        g1.ARM_STATE,
        *g1.COMMAND_TOPICS,
        *(g1.request_topic(s) for s in g1.SERVICES),
        *(g1.response_topic(s) for s in g1.SERVICES),
    }
)
#: The ``rt/arm_sdk`` joint that carries the blend weight (S4, ``kNotUsedJoint``).
WEIGHT_JOINT: Final = 29

_INTERFACE_RE = re.compile(r"[A-Za-z0-9._:@-]{1,64}", re.ASCII)
_VERSION_RE = re.compile(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)?(?:[-.+].*)?", re.ASCII)
_PRINTABLE_RE = re.compile(r"[^\x20-\x7e]")
#: A Python interpreter's program name: ``python3`` on Linux, ``Python`` on macOS (TEST, CI).
_INTERPRETER = re.compile(r"python[0-9.]*", re.ASCII | re.IGNORECASE)
_OPTION_VALUES: Final = frozenset({"-W", "-X", "--check-hash-based-pycs"})
_REMOTE_BYTES: Final = 40


class G1Error(Exception):
    """The G1 connector cannot start; the message is one line."""


# -- Cyclone DDS --------------------------------------------------------------------------------


#: The Python the G1 watcher runs on: Cyclone DDS 0.10 for Python does not load on 3.13 (its
#: extension needs ``_Py_IsFinalizing``, gone from 3.13's ABI).
SUPPORTED_PYTHON: Final = (3, 12)


def check_python(version: Sequence[int]) -> None:
    """Accept only Python 3.12, which Cyclone DDS 0.10 for Python supports.

    Raises:
        G1Error: another Python; the message says why in one line.
    """
    found = (int(version[0]), int(version[1]))
    if found != SUPPORTED_PYTHON:
        raise G1Error(
            f"the G1 watcher needs Python 3.12: Cyclone DDS 0.10 for Python does not support "
            f"Python {found[0]}.{found[1]} (README, Unitree G1)"
        )


def cyclonedds_version() -> str | None:
    """The installed Cyclone DDS for Python's version, ``None`` when it is not installed."""
    try:
        return importlib.metadata.version("cyclonedds")
    except importlib.metadata.PackageNotFoundError:
        return None


def check_cyclonedds(version: str | None) -> str:
    """Accept only the 0.10 line (S11); return the version.

    Raises:
        G1Error: not installed, or another version; the message says why in one line.
    """
    if version is None:
        raise G1Error(
            "Cyclone DDS for Python is not installed; build 0.10.5 from source (README, Unitree G1)"
        )
    found = _VERSION_RE.fullmatch(version)
    if found is None or (int(found[1]), int(found[2])) != SUPPORTED_LINE:
        raise G1Error(
            f"Cyclone DDS {version} is not supported: the G1 runs 0.10, and an observer on "
            "Cyclone DDS 11 crashed its 0.10 publishers; install 0.10.5 (README, Unitree G1)"
        )
    return version


def domain_config(interface: str | None) -> str | None:
    """The Cyclone DDS configuration that binds to ``interface``, as Unitree's SDK writes it
    (unitree_sdk2py/core/channel_config.py), without its trace file; ``None`` for the default.

    Raises:
        G1Error: the interface name has characters an interface name never has.
    """
    if interface is None:
        return None
    if _INTERFACE_RE.fullmatch(interface) is None:
        raise G1Error("--interface must be a network interface name such as eth0")
    return (
        '<?xml version="1.0" encoding="UTF-8" ?><CycloneDDS><Domain Id="any"><General>'
        f'<Interfaces><NetworkInterface name="{interface}" priority="default" '
        'multicast="default"/></Interfaces></General></Domain></CycloneDDS>'
    )


@functools.cache
def _types() -> dict[str, Any]:  # pragma: no cover -- needs Cyclone DDS: the dds tests run it
    """The message types, by wire type name; imported and built on first use."""
    from dataclasses import dataclass as idl_dataclass  # noqa: PLC0415
    from dataclasses import field as idl_field  # noqa: PLC0415

    import cyclonedds.idl.annotations as annotate  # noqa: PLC0415
    from cyclonedds import idl  # noqa: PLC0415 -- not a declared dependency
    from cyclonedds.idl import types  # noqa: PLC0415

    # unitree_hg (unitree_sdk2py/idl/unitree_hg/msg/dds_/, unitree_sdk2 include/unitree/idl/hg/).
    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class MotorCmd_(idl.IdlStruct, typename="unitree_hg.msg.dds_.MotorCmd_"):  # type: ignore[misc,call-arg]  # noqa: N801
        mode: types.uint8
        q: types.float32
        dq: types.float32
        tau: types.float32
        kp: types.float32
        kd: types.float32
        reserve: types.uint32

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class LowCmd_(idl.IdlStruct, typename="unitree_hg.msg.dds_.LowCmd_"):  # type: ignore[misc,call-arg]  # noqa: N801
        mode_pr: types.uint8
        mode_machine: types.uint8
        motor_cmd: types.array[MotorCmd_, 35]
        reserve: types.array[types.uint32, 4]
        crc: types.uint32

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class IMUState_(idl.IdlStruct, typename="unitree_hg.msg.dds_.IMUState_"):  # type: ignore[misc,call-arg]  # noqa: N801
        quaternion: types.array[types.float32, 4]
        gyroscope: types.array[types.float32, 3]
        accelerometer: types.array[types.float32, 3]
        rpy: types.array[types.float32, 3]
        temperature: types.int16

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class MotorState_(idl.IdlStruct, typename="unitree_hg.msg.dds_.MotorState_"):  # type: ignore[misc,call-arg]  # noqa: N801
        mode: types.uint8
        q: types.float32
        dq: types.float32
        ddq: types.float32
        tau_est: types.float32
        temperature: types.array[types.int16, 2]
        vol: types.float32
        sensor: types.array[types.uint32, 2]
        motorstate: types.uint32
        reserve: types.array[types.uint32, 4]

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class LowState_(idl.IdlStruct, typename="unitree_hg.msg.dds_.LowState_"):  # type: ignore[misc,call-arg]  # noqa: N801
        version: types.array[types.uint32, 2]
        mode_pr: types.uint8
        mode_machine: types.uint8
        tick: types.uint32
        imu_state: IMUState_
        motor_state: types.array[MotorState_, 35]
        wireless_remote: types.array[types.uint8, 40]
        reserve: types.array[types.uint32, 4]
        crc: types.uint32

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class HandCmd_(idl.IdlStruct, typename="unitree_hg.msg.dds_.HandCmd_"):  # type: ignore[misc,call-arg]  # noqa: N801
        motor_cmd: types.sequence[MotorCmd_]
        reserve: types.array[types.uint32, 4]

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class SportModeState_(idl.IdlStruct, typename="unitree_hg.msg.dds_.SportModeState_"):  # type: ignore[misc,call-arg]  # noqa: N801
        fsm_id: types.uint32
        fsm_mode: types.uint32
        task_id: types.uint32
        task_time: types.float32

    # unitree_go (unitree_sdk2py/idl/unitree_go/msg/dds_/): the hand blend of rt/hand_sdk.
    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class GoMotorCmd_(idl.IdlStruct, typename="unitree_go.msg.dds_.MotorCmd_"):  # type: ignore[misc,call-arg]  # noqa: N801
        mode: types.uint8
        q: types.float32
        dq: types.float32
        tau: types.float32
        kp: types.float32
        kd: types.float32
        reserve: types.array[types.uint32, 3]

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class MotorCmds_(idl.IdlStruct, typename="unitree_go.msg.dds_.MotorCmds_"):  # type: ignore[misc,call-arg]  # noqa: N801
        cmds: types.sequence[GoMotorCmd_] = idl_field(default_factory=list)

    # unitree_api (unitree_sdk2py/idl/unitree_api/msg/dds_/).
    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class RequestIdentity_(idl.IdlStruct, typename="unitree_api.msg.dds_.RequestIdentity_"):  # type: ignore[misc,call-arg]  # noqa: N801
        id: types.int64
        api_id: types.int64

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class RequestLease_(idl.IdlStruct, typename="unitree_api.msg.dds_.RequestLease_"):  # type: ignore[misc,call-arg]  # noqa: N801
        id: types.int64

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class RequestPolicy_(idl.IdlStruct, typename="unitree_api.msg.dds_.RequestPolicy_"):  # type: ignore[misc,call-arg]  # noqa: N801
        priority: types.int32
        noreply: bool

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class RequestHeader_(idl.IdlStruct, typename="unitree_api.msg.dds_.RequestHeader_"):  # type: ignore[misc,call-arg]  # noqa: N801
        identity: RequestIdentity_
        lease: RequestLease_
        policy: RequestPolicy_

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class Request_(idl.IdlStruct, typename="unitree_api.msg.dds_.Request_"):  # type: ignore[misc,call-arg]  # noqa: N801
        header: RequestHeader_
        parameter: str
        binary: types.sequence[types.uint8]

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class ResponseStatus_(idl.IdlStruct, typename="unitree_api.msg.dds_.ResponseStatus_"):  # type: ignore[misc,call-arg]  # noqa: N801
        code: types.int32

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class ResponseHeader_(idl.IdlStruct, typename="unitree_api.msg.dds_.ResponseHeader_"):  # type: ignore[misc,call-arg]  # noqa: N801
        identity: RequestIdentity_
        status: ResponseStatus_

    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class Response_(idl.IdlStruct, typename="unitree_api.msg.dds_.Response_"):  # type: ignore[misc,call-arg]  # noqa: N801
        header: ResponseHeader_
        data: str
        binary: types.sequence[types.uint8]

    # std_msgs (unitree_sdk2py/idl/std_msgs/msg/dds_/_String_.py).
    @idl_dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class String_(idl.IdlStruct, typename="std_msgs.msg.dds_.String_"):  # type: ignore[misc,call-arg]  # noqa: N801
        data: str

    classes = (
        MotorCmd_,
        LowCmd_,
        IMUState_,
        MotorState_,
        LowState_,
        HandCmd_,
        SportModeState_,
        GoMotorCmd_,
        MotorCmds_,
        RequestIdentity_,
        RequestLease_,
        RequestPolicy_,
        RequestHeader_,
        Request_,
        ResponseStatus_,
        ResponseHeader_,
        Response_,
        String_,
    )
    return {_wire_type(cls.__idl_typename__): cls for cls in classes}


def _wire_type(name: str) -> str:
    """A type name as Cyclone puts it on the wire: ``::`` between the scopes."""
    return name.replace(".", "::")


# -- records ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParticipantRecord:
    """A DCPSParticipant sample: its key and properties, or that it left."""

    key: str
    properties: Mapping[str, str]
    alive: bool = True


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """A DCPSPublication sample: a writer's key, the instance handle its samples carry, its
    participant, topic and type name; or that it went."""

    key: str
    handle: int
    participant_key: str
    topic: str
    type_name: str
    alive: bool = True


@dataclass(frozen=True, slots=True)
class SampleRecord:
    """A data sample of ``topic`` from the writer whose instance handle is ``handle``."""

    topic: str
    handle: int
    value: Any


class Backend(Protocol):
    """The DDS side of the observer: built-in topics, readers, nothing that writes."""

    def participants(self) -> list[ParticipantRecord]:
        """DCPSParticipant samples since the last call."""
        ...

    def publications(self) -> list[PublicationRecord]:
        """DCPSPublication samples since the last call."""
        ...

    def open_reader(self, topic: str, type_name: str) -> bool:
        """Read ``topic`` with the type ``type_name``; False when that type is not known."""
        ...

    def samples(self) -> list[SampleRecord]:
        """The data samples of every open reader since the last call."""
        ...

    @property
    def participant_key(self) -> str:
        """This observer's own participant key."""
        ...


# -- identities ---------------------------------------------------------------------------------


def _text(value: Any, limit: int = MAX_NAME) -> str | None:
    """A string from the network made safe to match and print: printable ASCII only (anything
    else becomes ``?``), at most ``limit`` characters; ``None`` when empty or not a string."""
    if not isinstance(value, str) or not value:
        return None
    return _PRINTABLE_RE.sub("?", value[:limit])


def python_target(argv: Sequence[str]) -> str | None:
    """The script file name or ``-m`` module a Python interpreter's arguments run; ``None`` for
    ``-c``, an interactive interpreter or anything else."""
    args = iter(argv)
    for arg in args:
        if arg == "-m":
            return next(args, None) or None
        if arg.startswith("-m") and not arg.startswith("--"):
            return arg[2:] or None
        if arg.startswith("-c") and not arg.startswith("--"):
            return None
        if arg in _OPTION_VALUES:
            next(args, None)
        elif not arg.startswith("-"):
            return PurePosixPath(arg).name or None
        elif arg == "-":
            return None
    return None


def resolve_script(pid: int, program: str) -> str | None:
    """The script or module of process ``pid`` on this computer, read with psutil, but only
    while that process still runs ``program`` (a reused pid runs something else)."""
    import psutil  # noqa: PLC0415 -- the g1 extra

    try:
        process = psutil.Process(pid)
        argv = [str(arg) for arg in process.cmdline()]
        name = str(process.name())
    except (psutil.Error, OSError, ValueError):
        return None
    running = {name, PurePosixPath(argv[0]).name if argv else ""}
    if program not in running:
        return None
    return python_target(argv[1:])


Resolver = Callable[[int, str], str | None]


def participant_name(
    host: str | None, program: str | None, pid: int | None, local_host: str, resolve: Resolver
) -> str | None:
    """The ``participant`` of notes section 4: for a Python interpreter on this computer, its
    script or module; otherwise the program's name (``__ProcessName``)."""
    if program and _INTERPRETER.fullmatch(program) and pid is not None and host == local_host:
        script = _text(resolve(pid, program))
        if script is not None:
            return script
    return program


@dataclass(frozen=True, slots=True)
class _Participant:
    host: str | None
    program: str | None
    pid: int | None
    name: str | None


def _participant(properties: Mapping[str, str], local_host: str, resolve: Resolver) -> _Participant:
    host = _text(properties.get("__Hostname"))
    program = _text(properties.get("__ProcessName"))
    pid_text = properties.get("__Pid", "")
    pid = int(pid_text) if pid_text.isdigit() and len(pid_text) <= 10 else None  # noqa: PLR2004
    return _Participant(
        host, program, pid, participant_name(host, program, pid, local_host, resolve)
    )


# -- samples ------------------------------------------------------------------------------------


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _weight(topic: str, value: Any) -> float | None:
    """The blend weight of ``rt/arm_sdk`` (``motor_cmd[29].q``, S4) or ``rt/hand_sdk``
    (``cmds[0].mode / 100``); ``None`` elsewhere or when it cannot be read."""
    try:
        if topic == g1.ARM_SDK:
            weight = _float(value.motor_cmd[WEIGHT_JOINT].q)
        elif topic == g1.HAND_SDK:
            weight = _float(value.cmds[0].mode)
            weight = None if weight is None else weight / 100
        else:
            return None
    except (AttributeError, IndexError, TypeError):
        return None
    return weight if weight is not None and math.isfinite(weight) else None


def _remote(value: Any) -> bytes:
    """The 40 bytes of ``LowState_.wireless_remote`` (S2)."""
    try:
        return bytes(int(b) & 0xFF for b in list(value.wireless_remote)[:_REMOTE_BYTES])
    except (AttributeError, TypeError, ValueError):
        return b""


def convert(record: SampleRecord, key: str, t: float) -> g1.Observation | None:  # noqa: PLR0911 -- one return per topic
    """The observation a sample stands for (``key``: its writer), ``None`` when it says nothing
    the rules use."""
    topic, value = record.topic, record.value
    try:
        if topic == g1.LOWSTATE:
            return g1.LowState(t, key, _remote(value))
        if topic == g1.SPORTSTATE:
            return g1.SportState(
                t, key, _int(value.fsm_id), _int(value.fsm_mode), _int(value.task_id)
            )
        if topic in g1.COMMAND_TOPICS:
            return g1.Command(t, key, topic, _weight(topic, value))
        if topic == g1.ARM_STATE:
            return g1.ArmState(t, key, _text(value.data, MAX_TEXT) or "")
        for service in g1.SERVICES:
            if topic == g1.request_topic(service):
                identity = value.header.identity
                parameter = value.parameter if isinstance(value.parameter, str) else ""
                return g1.Request(
                    t, key, service, _int(identity.id), _int(identity.api_id), parameter[:MAX_TEXT]
                )
            if topic == g1.response_topic(service):
                header = value.header
                data = value.data if isinstance(value.data, str) else ""
                return g1.Response(
                    t,
                    key,
                    service,
                    _int(header.identity.id),
                    _int(header.identity.api_id),
                    _int(header.status.code),
                    data[:MAX_TEXT],
                )
    except AttributeError:
        return None
    return None


# -- the observer -------------------------------------------------------------------------------


class Observer:
    """Turns what a :class:`Backend` reads into observations of
    :mod:`autonometry.connectors.g1`: writers appearing and going with their observed source
    (notes section 4), and samples mapped to their writer by the publication handle (TEST in
    research/unitree-g1/dds_compat). A reader opens only for a writer of the expected type."""

    def __init__(
        self,
        backend: Backend,
        local_host: str | None = None,
        resolve: Resolver = resolve_script,
    ) -> None:
        self._backend = backend
        self._local = socket.gethostname() if local_host is None else local_host
        self._resolve = resolve
        self._participants: dict[str, _Participant] = {}
        self._writers: dict[str, PublicationRecord] = {}
        self._handles: dict[int, str] = {}
        self._opened: set[str] = set()

    def poll(self, now: float) -> list[g1.Observation]:
        """Everything read since the last call, stamped ``now`` (seconds)."""
        out: list[g1.Observation] = []
        own = self._backend.participant_key
        for participant in self._backend.participants():
            if participant.key != own:
                out += self._on_participant(participant, now)
        for publication in self._backend.publications():
            if publication.participant_key != own:
                out += self._on_publication(publication, now)
        return out + _thin(self._converted(now))

    def _on_participant(self, record: ParticipantRecord, now: float) -> list[g1.Observation]:
        writers = [w for w in self._writers.values() if w.participant_key == record.key]
        if not record.alive:
            self._participants.pop(record.key, None)
            for writer in writers:
                self._forget(writer)
            return [g1.WriterGone(now, w.key) for w in writers]
        info = _participant(record.properties, self._local, self._resolve)
        if self._participants.get(record.key) == info:
            return []
        if record.key not in self._participants and len(self._participants) >= MAX_ENDPOINTS:
            del self._participants[next(iter(self._participants))]
        self._participants[record.key] = info
        return [self._seen(w, now) for w in writers]

    def _on_publication(self, record: PublicationRecord, now: float) -> list[g1.Observation]:
        if not record.alive:
            known = self._writers.get(record.key)
            if known is None:
                return []
            self._forget(known)
            return [g1.WriterGone(now, record.key)]
        if record.topic not in WATCHED:
            return []
        if record.key not in self._writers and len(self._writers) >= MAX_ENDPOINTS:
            self._forget(next(iter(self._writers.values())))
        self._writers[record.key] = record
        self._handles[record.handle] = record.key
        return [self._seen(record, now)]

    def _forget(self, record: PublicationRecord) -> None:
        self._writers.pop(record.key, None)
        if self._handles.get(record.handle) == record.key:
            del self._handles[record.handle]

    def _seen(self, record: PublicationRecord, now: float) -> g1.WriterSeen:
        info = self._participants.get(record.participant_key)
        source = Source(
            host=None if info is None else info.host,
            participant=None if info is None else info.name,
            topic=record.topic,
        )
        type_name = _text(record.type_name) or ""
        expected = EXPECTED_TYPES.get(record.topic)
        matches = expected is not None and _wire_type(type_name) == expected
        if (
            matches
            and expected is not None
            and record.topic not in self._opened
            and self._backend.open_reader(record.topic, expected)
        ):
            self._opened.add(record.topic)
        decoded = matches and record.topic in self._opened
        return g1.WriterSeen(now, record.key, source, type_name, decoded)

    def _converted(self, now: float) -> list[g1.Observation]:
        out = []
        for sample in self._backend.samples():
            key = self._handles.get(sample.handle, f"handle:{sample.handle}")
            observation = convert(sample, key, now)
            if observation is not None:
                out.append(observation)
        return out


def _thin(observations: Sequence[g1.Observation]) -> list[g1.Observation]:
    """One pass's samples, keeping what the rules need: every request, response and arm state,
    each change of the built-in state, and the latest command and lowstate of each writer."""
    latest: dict[tuple[str, str], int] = {}
    for index, observation in enumerate(observations):
        if isinstance(observation, g1.Command):
            latest[(observation.topic, observation.key)] = index
        elif isinstance(observation, g1.LowState):
            latest[(g1.LOWSTATE, observation.key)] = index
    out: list[g1.Observation] = []
    last_state: dict[str, tuple[int, int, int]] = {}
    for index, observation in enumerate(observations):
        if isinstance(observation, g1.Command):
            if latest[(observation.topic, observation.key)] != index:
                continue
        elif isinstance(observation, g1.LowState):
            if latest[(g1.LOWSTATE, observation.key)] != index:
                continue
        elif isinstance(observation, g1.SportState):
            state = (observation.fsm_id, observation.fsm_mode, observation.task_id)
            if last_state.get(observation.key) == state:
                continue
            last_state[observation.key] = state
        out.append(observation)
    return out


# -- Cyclone DDS backend ------------------------------------------------------------------------


def _properties(qos: Any) -> dict[str, str]:
    """The ``__`` properties a Cyclone DDS participant announces (S10)."""
    found: dict[str, str] = {}
    try:
        policies = list(qos)
    except TypeError:
        return found
    for policy in policies:
        key, value = getattr(policy, "key", None), getattr(policy, "value", None)
        if isinstance(key, str) and isinstance(value, str) and key.startswith("__"):
            found[key] = value
    return found


class _CycloneBackend:  # pragma: no cover -- needs Cyclone DDS: the dds tests run it
    """A Cyclone DDS 0.10 participant with the built-in readers and best-effort, volatile
    readers. It creates no DataWriter and sends nothing."""

    def __init__(self, domain: int, interface: str | None) -> None:
        from cyclonedds.builtin import (  # noqa: PLC0415 -- not a declared dependency
            BuiltinDataReader,
            BuiltinTopicDcpsParticipant,
            BuiltinTopicDcpsPublication,
        )
        from cyclonedds.domain import Domain, DomainParticipant  # noqa: PLC0415

        config = domain_config(interface)
        self._domain = None if config is None else Domain(domain, config)
        self._dp = DomainParticipant(domain)
        self._participant_reader = BuiltinDataReader(self._dp, BuiltinTopicDcpsParticipant)
        self._publication_reader = BuiltinDataReader(self._dp, BuiltinTopicDcpsPublication)
        self._readers: dict[str, Any] = {}
        self._participant_keys: dict[int, str] = {}
        self._publication_keys: dict[int, tuple[str, str, str, str]] = {}

    @property
    def participant_key(self) -> str:
        return str(self._dp.guid)

    def participants(self) -> list[ParticipantRecord]:
        out = []
        for sample in self._participant_reader.take(N=HISTORY):
            info = sample.sample_info
            handle = int(info.instance_handle)
            if _alive(info):
                key = str(sample.key)
                self._participant_keys[handle] = key
                out.append(ParticipantRecord(key, _properties(sample.qos)))
            elif handle in self._participant_keys:
                out.append(ParticipantRecord(self._participant_keys.pop(handle), {}, alive=False))
        return out

    def publications(self) -> list[PublicationRecord]:
        out = []
        for sample in self._publication_reader.take(N=HISTORY):
            info = sample.sample_info
            handle = int(info.instance_handle)
            if _alive(info):
                fields = (
                    str(sample.key),
                    str(sample.participant_key),
                    str(sample.topic_name),
                    str(sample.type_name),
                )
                self._publication_keys[handle] = fields
                out.append(PublicationRecord(fields[0], handle, *fields[1:]))
            elif handle in self._publication_keys:
                key, participant, topic, type_name = self._publication_keys.pop(handle)
                out.append(PublicationRecord(key, handle, participant, topic, type_name, False))
        return out

    def open_reader(self, topic: str, type_name: str) -> bool:
        message = _types().get(type_name)
        if message is None:
            return False
        from cyclonedds.qos import Policy, Qos  # noqa: PLC0415 -- not a declared dependency
        from cyclonedds.sub import DataReader  # noqa: PLC0415
        from cyclonedds.topic import Topic  # noqa: PLC0415

        depth = 1 if topic == g1.LOWSTATE else HISTORY
        qos = Qos(
            Policy.Reliability.BestEffort,
            Policy.Durability.Volatile,
            Policy.History.KeepLast(depth),
        )
        self._readers[topic] = DataReader(self._dp, Topic(self._dp, topic, message), qos=qos)
        return True

    def samples(self) -> list[SampleRecord]:
        out = []
        for topic, reader in self._readers.items():
            for sample in reader.take(N=HISTORY):
                info = sample.sample_info
                if getattr(info, "valid_data", True):
                    out.append(SampleRecord(topic, int(info.publication_handle), sample))
        return out


def _alive(info: Any) -> bool:  # pragma: no cover -- needs Cyclone DDS: the dds tests run it
    from cyclonedds.core import InstanceState  # noqa: PLC0415 -- not a declared dependency

    return bool(getattr(info, "valid_data", True)) and info.instance_state == InstanceState.Alive


def open_backend(domain: int, interface: str | None) -> Backend:
    """A subscribe-only Cyclone DDS backend on ``domain``, bound to ``interface`` if given.

    Raises:
        G1Error: the domain id or interface name is not valid.
    """
    if not 0 <= domain <= MAX_DOMAIN:
        raise G1Error(f"--domain must be 0 to {MAX_DOMAIN}")
    domain_config(interface)
    try:
        return _CycloneBackend(domain, interface)
    except ImportError as exc:
        raise G1Error(
            f"Cyclone DDS for Python cannot be loaded ({type(exc).__name__}): the G1 watcher "
            "needs Python 3.12 and Cyclone DDS 0.10.5 built from source (README, Unitree G1)"
        ) from None


# -- discover -----------------------------------------------------------------------------------


@dataclass(slots=True)
class Discovery:
    """What ``autonometry g1 discover`` found: participants and the writers each has."""

    participants: dict[str, _Participant] = field(default_factory=dict)
    writers: dict[str, PublicationRecord] = field(default_factory=dict)

    def take(self, backend: Backend, local_host: str, resolve: Resolver) -> None:
        """Add what the backend read since the last call."""
        own = backend.participant_key
        for record in backend.participants():
            if record.key == own:
                continue
            if record.alive:
                self.participants[record.key] = _participant(record.properties, local_host, resolve)
            else:
                self.participants.pop(record.key, None)
        for publication in backend.publications():
            if publication.participant_key == own:
                continue
            if publication.alive:
                self.writers[publication.key] = publication
            else:
                self.writers.pop(publication.key, None)

    def report(self) -> dict[str, Any]:
        """Participants with host, program, pid and the topics they write, each with the exact
        ``match`` for the device registry (notes section 4); sorted, so the same network gives
        the same text."""
        keys = set(self.participants) | {w.participant_key for w in self.writers.values()}
        participants = []
        for key in sorted(keys):
            info = self.participants.get(key, _Participant(None, None, None, None))
            writers = []
            for writer in sorted(
                (w for w in self.writers.values() if w.participant_key == key),
                key=lambda w: (w.topic, w.key),
            ):
                topic = _text(writer.topic) or ""
                entry: dict[str, Any] = {
                    "topic": topic,
                    "type": _text(writer.type_name) or "",
                    "match": _match(info, topic),
                }
                if topic == g1.LOWSTATE:
                    entry["remote_match"] = _match(info, g1.REMOTE_TOPIC)
                writers.append(entry)
            participants.append(
                {
                    "key": key,
                    "host": info.host,
                    "host_pseudonym": g1.host_pseudonym(info.host),
                    "program": info.program,
                    "pid": info.pid,
                    "participant": info.name,
                    "writers": writers,
                }
            )
        return {"participants": participants, "preset_match": {"topic": g1.PRESET_TOPIC}}


def _match(info: _Participant, topic: str) -> dict[str, str]:
    match = {"host": info.host, "participant": info.name, "topic": topic}
    return {name: value for name, value in match.items() if value is not None}


def discover(  # noqa: PLR0913 -- the listening loop's collaborators
    backend: Backend,
    seconds: float,
    *,
    local_host: str | None = None,
    resolve: Resolver = resolve_script,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """Listen for ``seconds`` and report every participant and writer (read-only)."""
    found = Discovery()
    local = socket.gethostname() if local_host is None else local_host
    end = clock() + seconds
    while True:
        found.take(backend, local, resolve)
        if clock() >= end:
            return found.report()
        sleep(PASS_S)


def format_discovery(report: Mapping[str, Any]) -> list[str]:
    """The discovery as text lines, one block per participant."""
    lines = []
    for participant in report["participants"]:
        pid, name = participant["pid"], participant["participant"]
        host = participant["host"]
        shown = "?" if host is None else f"{host} (in logs {participant['host_pseudonym']})"
        head = f"participant {participant['key']}: host {shown}, "
        head += f"program {participant['program'] or '?'}"
        head += f" (pid {pid})" if pid is not None else ""
        head += f", script {name}" if name != participant["program"] else ""
        lines.append(head)
        for writer in participant["writers"]:
            lines.append(f"  writes {writer['topic']}  [{writer['type']}]")
            lines.append(f"    match: {_json(writer['match'])}")
            if "remote_match" in writer:
                lines.append(f"    handheld remote match: {_json(writer['remote_match'])}")
        if not participant["writers"]:
            lines.append("  writes nothing")
    lines.append(f"Unitree's presets: match {_json(report['preset_match'])}")
    return lines


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


# -- tasks --------------------------------------------------------------------------------------


class TaskFollower:
    """Follows the recorder's current session log, read-only, for ``task_start`` and
    ``task_end`` (notes section 8): the newest session folder by its counter, from its first
    line, switching when the recorder rotates. A log it cannot read makes every moment outside a
    task; it warns once, and the engine notes it once on a heartbeat."""

    def __init__(self, robot_dir: Path) -> None:
        self._robot = robot_dir
        self._session: Path | None = None
        self._follower: LogFollower | None = None
        self._open: str | None = None
        self._readable = False
        self._checked: float | None = None
        self._reported: tuple[bool, str | None] | None = None

    def poll(self, now: float) -> list[g1.Observation]:
        """What changed since the last call: the open task, or that the log is unreadable."""
        if self._checked is None or now - self._checked >= TASK_CHECK_S:
            self._checked = now
            self._find()
        if self._readable and self._follower is not None:
            for line in self._follower.read():
                self._line(line)
        report = (self._readable, self._open if self._readable else None)
        if report == self._reported:
            return []
        if not self._readable and (self._reported is None or self._reported[0]):
            logger.warning(
                "cannot read the recorder's session log under %s: every moment counts as "
                "outside a task",
                self._robot,
            )
        self._reported = report
        if not self._readable:
            return [g1.TasksUnreadable(now)]
        if self._open is not None:
            return [g1.TaskOpened(now, self._open)]
        return [g1.TaskClosed(now)]

    def _find(self) -> None:
        try:
            sessions = session_dirs(self._robot)
        except OSError:
            sessions = []
        newest = sessions[-1] if sessions else None
        events = None if newest is None else newest / EVENTS_FILE
        self._readable = events is not None and os.access(events, os.R_OK)
        if newest != self._session:
            if self._follower is not None:
                self._follower.close()
            self._session, self._open = newest, None
            self._follower = None if events is None else LogFollower(events, from_start=True)

    def close(self) -> None:
        """Close the log."""
        if self._follower is not None:
            self._follower.close()

    def _line(self, line: str) -> None:
        try:
            event = strictjson.parse(line)
        except strictjson.StrictJsonError:
            return
        if not isinstance(event, dict):
            return
        kind, task_id = event.get("type"), event.get("task_id")
        if kind == "task_start" and isinstance(task_id, str):
            self._open = task_id
        elif kind == "task_end" and task_id == self._open:
            self._open = None
        elif kind == "session_start":
            carried = event.get("x_carried_task")
            self._open = carried if isinstance(carried, str) else None


# -- the watch loop -----------------------------------------------------------------------------

Sender = Callable[[Iterable[g1.Message]], None]


def recorder_sender(recorder: Any) -> Sender:
    """Send messages through the recorder client (``autonometry_client.Recorder``), which never
    blocks or raises."""

    def send(messages: Iterable[g1.Message]) -> None:
        for message in messages:
            event = g1.wire_event(message)
            recorder.event(event.pop("type"), **event)

    return send


class Polled(Protocol):
    """Something read once per pass."""

    def poll(self, now: float) -> list[g1.Observation]:
        """What changed since the last call."""
        ...


def pump(  # noqa: PLR0913 -- the pass's collaborators
    observer: Polled,
    tasks: Polled,
    engine: g1.G1Engine,
    send: Sender,
    now: float,
    *,
    tick: bool,
) -> None:
    """One pass: the tasks, then everything read from DDS, to the engine; then, when due, the
    state of every scope (seconds, ``now``)."""
    for observation in [*tasks.poll(now), *observer.poll(now)]:
        send(_warned(engine.observe(observation)))
    if tick:
        send(_warned(engine.tick(now)))


def _warned(messages: list[g1.Message]) -> list[g1.Message]:
    """Messages unchanged; one warning when they say the robot returned while released or in
    user control (a restart ends those modes without a request the watcher could see)."""
    for message in messages:
        if isinstance(message, g1.Note) and "x_g1_robot_returned" in dict(message.fields):
            logger.warning(
                "the robot's lowstate returned while it counts as %s; if the robot restarted, "
                "restart the watcher too",
                dict(message.fields)["x_g1_robot_returned"].replace("_", " "),
            )
    return messages


def run_watch(  # noqa: PLR0913 -- the watch loop's collaborators
    observer: Polled,
    tasks: Polled,
    engine: g1.G1Engine,
    send: Sender,
    stop: threading.Event,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Pass every :data:`PASS_S` seconds until ``stop`` is set, the state every :data:`TICK_S`.
    A failing pass is logged and the next one runs (the process boundary of CLAUDE.md). On the
    way out every scope is sent ``unknown``."""
    next_tick = -math.inf
    try:
        while not stop.is_set():
            now = clock()
            due = now >= next_tick
            try:
                pump(observer, tasks, engine, send, now, tick=due)
            except Exception:  # process boundary: keep observing
                logger.warning(
                    "a pass over the robot's DDS traffic failed; continuing", exc_info=True
                )
            if due:
                next_tick = now + TICK_S
            stop.wait(PASS_S)
    finally:
        send(engine.stop())
