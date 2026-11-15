"""Heartbeat notes a connector records, split so that none is too large to log.

Pure and standard library only. A connector records evidence as ``x_`` fields on a client
heartbeat (docs/recorder.md, "Wire format"); the recorder rejects one whose log line would pass
64 KiB.
The ROS observer's publisher map (``x_publishers``: topic to its publishers) grows with the
publishers in the graph, so a large one is sent in parts rather than dropped (security review
S-55).
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

#: Most UTF-8 bytes of one heartbeat's ``x_`` fields as compact JSON: a 64 KiB log line less the
#: recorder's 1 KiB reserve for its own heartbeat fields, the seal (about 160 bytes) and the
#: line's other fields (ids, ``seq``, ``ts``, ``task_id``: at most about 400 bytes), with room
#: to spare.
PART_BYTES: Final = 48 * 1024
#: Width the part counter is measured at: no map splits into a million parts.
_COUNTER: Final = [999_999, 999_999]


def _size(value: Any) -> int:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(text.encode("utf-8"))


def publisher_heartbeats(
    publishers: Mapping[str, Sequence[Mapping[str, Any]]],
    extra: Mapping[str, Any],
    limit: int = PART_BYTES,
) -> list[dict[str, Any]]:
    """The heartbeats that record ``publishers`` (topic to its entries), each with ``extra``.

    One heartbeat holds the whole map as ``x_publishers`` when it fits in ``limit`` bytes of
    compact JSON. A larger map is split in topic order, a topic's list across parts if need be,
    into heartbeats that each carry ``x_publishers_part: [i, n]`` (1-based): joining the parts
    in order, extending each topic's list, gives the map back. Nothing is dropped silently: a
    topic or entry too large for a part of its own (node names are bounded, so none is in
    practice) is left out and counted in ``x_publishers_dropped`` on the last part.
    """
    whole = {"x_publishers": {topic: list(found) for topic, found in publishers.items()}}
    if _size({**whole, **extra}) <= limit:
        return [{**whole, **extra}]
    base = _size(
        {"x_publishers": {}, "x_publishers_part": _COUNTER, "x_publishers_dropped": 0, **extra}
    ) + len(str(_COUNTER[0]))
    parts: list[dict[str, list[Mapping[str, Any]]]] = [{}]
    used, dropped = base, 0
    for topic, found in publishers.items():
        key_cost = _size(topic) + 4  # "topic":[] and a comma
        if base + key_cost > limit:
            dropped += max(len(found), 1)
            continue
        if used + key_cost > limit:
            parts.append({})
            used = base
        parts[-1][topic] = []
        used += key_cost
        for entry in found:
            cost = _size(entry) + 1
            if base + key_cost + cost > limit:
                dropped += 1
                continue
            if used + cost > limit:
                parts.append({topic: []})
                used = base + key_cost
            parts[-1][topic].append(entry)
            used += cost
    count = len(parts)
    out: list[dict[str, Any]] = [
        {"x_publishers": part, "x_publishers_part": [index, count], **extra}
        for index, part in enumerate(parts, start=1)
    ]
    if dropped:
        out[-1]["x_publishers_dropped"] = dropped
    return out
