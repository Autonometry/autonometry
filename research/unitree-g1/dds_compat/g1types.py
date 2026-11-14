"""Unitree hg/api message types written from unitree_sdk2_python's IDL field lists (test only)."""
from dataclasses import dataclass
import cyclonedds.idl as idl
import cyclonedds.idl.annotations as annotate
import cyclonedds.idl.types as types

@dataclass
@annotate.final
@annotate.autoid("sequential")
class MotorCmd_(idl.IdlStruct, typename="unitree_hg.msg.dds_.MotorCmd_"):
    mode: types.uint8
    q: types.float32
    dq: types.float32
    tau: types.float32
    kp: types.float32
    kd: types.float32
    reserve: types.uint32

@dataclass
@annotate.final
@annotate.autoid("sequential")
class LowCmd_(idl.IdlStruct, typename="unitree_hg.msg.dds_.LowCmd_"):
    mode_pr: types.uint8
    mode_machine: types.uint8
    motor_cmd: types.array[MotorCmd_, 35]
    reserve: types.array[types.uint32, 4]
    crc: types.uint32

@dataclass
@annotate.final
@annotate.autoid("sequential")
class RequestIdentity_(idl.IdlStruct, typename="unitree_api.msg.dds_.RequestIdentity_"):
    id: types.int64
    api_id: types.int64

@dataclass
@annotate.final
@annotate.autoid("sequential")
class RequestLease_(idl.IdlStruct, typename="unitree_api.msg.dds_.RequestLease_"):
    id: types.int64

@dataclass
@annotate.final
@annotate.autoid("sequential")
class RequestPolicy_(idl.IdlStruct, typename="unitree_api.msg.dds_.RequestPolicy_"):
    priority: types.int32
    noreply: bool

@dataclass
@annotate.final
@annotate.autoid("sequential")
class RequestHeader_(idl.IdlStruct, typename="unitree_api.msg.dds_.RequestHeader_"):
    identity: RequestIdentity_
    lease: RequestLease_
    policy: RequestPolicy_

@dataclass
@annotate.final
@annotate.autoid("sequential")
class Request_(idl.IdlStruct, typename="unitree_api.msg.dds_.Request_"):
    header: RequestHeader_
    parameter: str
    binary: types.sequence[types.uint8]
