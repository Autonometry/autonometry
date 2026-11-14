"""Shared code of the fake G1 programs in this folder, which ``test_g1_loopback.py`` runs as
separate processes. Each program is its own script, so the observer names it by its file name,
as it names xr_teleoperate or a policy on a real robot (notes section 4). They use the message
types of :mod:`autonometry.connectors.g1_dds`, written from Unitree's IDL files.

Every program takes ``DOMAIN START`` (the scenario's wall-clock start, Unix seconds) and runs
its part of the timeline in ``test_g1_loopback.py`` relative to START. Not collected by pytest."""

import json
import sys
import time
from collections.abc import Callable
from typing import Any

from cyclonedds.domain import DomainParticipant
from cyclonedds.pub import DataWriter
from cyclonedds.topic import Topic

from autonometry.connectors.g1_dds import _types

#: How long every program runs after START, seconds.
DURATION = 21.0
T = _types()
HG, GO, API = "unitree_hg::msg::dds_::", "unitree_go::msg::dds_::", "unitree_api::msg::dds_::"


def args() -> tuple[DomainParticipant, float]:
    """The participant on DOMAIN, and START."""
    return DomainParticipant(int(sys.argv[1])), float(sys.argv[2])


def writer(dp: DomainParticipant, topic: str, type_name: str) -> DataWriter:
    return DataWriter(dp, Topic(dp, topic, T[type_name]))


def run(start: float, period: float, step: Callable[[float], None]) -> None:
    """Call ``step(elapsed)`` every ``period`` seconds from START until START + DURATION."""
    while time.time() < start:
        time.sleep(0.01)
    while (elapsed := time.time() - start) < DURATION:
        step(elapsed)
        time.sleep(period)


def lowcmd(weight: float = 0.0) -> Any:
    """A ``LowCmd_`` with the blend weight in ``motor_cmd[29].q`` (S4)."""
    motor = T[HG + "MotorCmd_"]
    motors = [motor(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0) for _ in range(35)]
    motors[29] = motor(0, weight, 0.0, 0.0, 0.0, 0.0, 0)
    return T[HG + "LowCmd_"](0, 0, motors, [0, 0, 0, 0], 0)


def request(request_id: int, api_id: int, parameter: dict[str, Any]) -> Any:
    """A ``Request_`` as Unitree's RPC client sends it (S7)."""
    header = T[API + "RequestHeader_"](
        T[API + "RequestIdentity_"](request_id, api_id),
        T[API + "RequestLease_"](0),
        T[API + "RequestPolicy_"](0, False),
    )
    return T[API + "Request_"](header, json.dumps(parameter), [])


def remote(*, ly: float = 0.0, buttons: int = 0) -> list[int]:
    """``wireless_remote``: head, the button word, sticks lx rx ry L2 ly (gamepad.hpp)."""
    import struct  # noqa: PLC0415

    return list(struct.pack("<2xH5f", buttons, 0.0, 0.0, 0.0, 0.0, ly) + bytes(16))


def lowstate(wireless_remote: list[int]) -> Any:
    imu = T[HG + "IMUState_"]([1.0, 0.0, 0.0, 0.0], [0.0] * 3, [0.0] * 3, [0.0] * 3, 30)
    motor = T[HG + "MotorState_"]
    motors = [motor(0, 0.0, 0.0, 0.0, 0.0, [30, 30], 48.0, [0, 0], 0, [0] * 4) for _ in range(35)]
    return T[HG + "LowState_"]([1, 0], 0, 0, 0, imu, motors, wireless_remote, [0] * 4, 0)
