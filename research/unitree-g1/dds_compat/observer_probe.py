"""Observer side: best-effort readers; maps each sample to its writer and its writer to a process."""
import time
from cyclonedds.domain import DomainParticipant
from cyclonedds.topic import Topic
from cyclonedds.sub import DataReader
from cyclonedds.qos import Qos, Policy
from cyclonedds.builtin import BuiltinDataReader, BuiltinTopicDcpsParticipant, BuiltinTopicDcpsPublication
from g1types import *
dp = DomainParticipant(7)
parts = BuiltinDataReader(dp, BuiltinTopicDcpsParticipant)
pubs = BuiltinDataReader(dp, BuiltinTopicDcpsPublication)
qos = Qos(Policy.Reliability.BestEffort, Policy.History.KeepLast(64))
arm = DataReader(dp, Topic(dp, "rt/arm_sdk", LowCmd_), qos=qos)
req = DataReader(dp, Topic(dp, "rt/api/sport/request", Request_), qos=qos)
participants, writers, handles = {}, {}, {}
counts = {}
end = time.time() + 6
while time.time() < end:
    for p in parts.take(N=64):
        participants[str(p.key)] = repr(p.qos)[:400]
    for s in pubs.take(N=64):
        writers[str(s.key)] = (s.topic_name, s.type_name, str(s.participant_key))
        handles[s.sample_info.instance_handle] = str(s.key)
    for s in arm.take(N=64):
        h = s.sample_info.publication_handle
        counts.setdefault(("arm", h), [0, s.motor_cmd[29].q])[0] += 1
    for s in req.take(N=64):
        h = s.sample_info.publication_handle
        counts.setdefault(("req", h, s.header.identity.api_id, s.parameter), [0])[0] += 1
    time.sleep(0.05)
print("participants:")
for k, v in participants.items():
    print("  ", k, v)
print("writers (topic, type, participant):")
for k, v in writers.items():
    if v[0].startswith("rt/"):
        print("  ", k, v)
print("samples by publication_handle -> writer key:")
for k, v in counts.items():
    print("  ", k[0], k[2:] if len(k) > 2 else "", "count", v[0], "weight" if k[0] == "arm" else "", v[1] if k[0] == "arm" else "",
          "->", handles.get(k[1], "NOT FOUND in DCPSPublication handles"))
