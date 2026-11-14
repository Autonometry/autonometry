"""A second participant: prints every writer DCPSPublication shows during SECONDS, one JSON line
each ({"participant", "topic"}), so the test can confirm the observer created none."""

import json
import sys
import time

from cyclonedds.builtin import BuiltinDataReader, BuiltinTopicDcpsPublication
from cyclonedds.core import InstanceState
from cyclonedds.domain import DomainParticipant

dp = DomainParticipant(int(sys.argv[1]))
reader = BuiltinDataReader(dp, BuiltinTopicDcpsPublication)
seen: set[tuple[str, str]] = set()
end = time.time() + float(sys.argv[2])
while time.time() < end:
    for sample in reader.take(N=64):
        if sample.sample_info.instance_state == InstanceState.Alive:
            seen.add((str(sample.participant_key), str(sample.topic_name)))
    time.sleep(0.05)
for participant, topic in sorted(seen):
    print(json.dumps({"participant": participant, "topic": topic}))
