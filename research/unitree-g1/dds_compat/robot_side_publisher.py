"""Robot side: Cyclone DDS 0.10.x (as in Unitree's SDK) publishes like xr_teleoperate would."""
import json, sys, time
from cyclonedds.domain import DomainParticipant
from cyclonedds.topic import Topic
from cyclonedds.pub import DataWriter
from g1types import *
weight = float(sys.argv[1])
dp = DomainParticipant(7)
w = DataWriter(dp, Topic(dp, "rt/arm_sdk", LowCmd_))
rw = DataWriter(dp, Topic(dp, "rt/api/sport/request", Request_))
for i in range(250):
    cmds = [MotorCmd_(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0) for _ in range(35)]
    cmds[29] = MotorCmd_(0, weight, 0.0, 0.0, 0.0, 0.0, 0)
    w.write(LowCmd_(0, 0, cmds, [0, 0, 0, 0], 0))
    if i % 25 == 0:
        hdr = RequestHeader_(RequestIdentity_(i, 7105), RequestLease_(0), RequestPolicy_(0, False))
        rw.write(Request_(hdr, json.dumps({"velocity": [0.3, 0.0, 0.0], "duration": 1.0}), []))
    time.sleep(0.02)
