"""``autonometry.connectors.heartbeats``: a publisher map is never too large to log (S-55)."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from autonometry.connectors.heartbeats import PART_BYTES, publisher_heartbeats
from autonometry.connectors.names import MAX_NODE_NAME
from autonometry.recorder.engine import Engine, Settings
from recorder_helpers import SPEC, SYNCED, Client, FakeClock

EXTRA = {"x_rmw": "rmw_fastrtps_cpp", "x_ros_domain_id": 0}


def _size(value: Any) -> int:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(text.encode("utf-8"))


def _joined(parts: Sequence[Mapping[str, Any]]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for part in parts:
        for topic, found in part["x_publishers"].items():
            out.setdefault(topic, []).extend(found)
    return out


def _map(topics: int, per_topic: int) -> dict[str, list[dict[str, str]]]:
    return {
        f"/robot/topic_{t}": [
            {"node": f"/robot/node_{t}_{i}_" + "x" * 200, "gid": f"{t:04x}{i:04x}" * 4}
            for i in range(per_topic)
        ]
        for t in range(topics)
    }


def test_a_small_map_is_one_heartbeat_as_before__s55() -> None:
    publishers = _map(3, 2)
    assert publisher_heartbeats(publishers, EXTRA) == [{"x_publishers": publishers, **EXTRA}]


def test_a_map_larger_than_a_datagram_is_split_into_numbered_parts__s55() -> None:
    publishers = _map(8, 64)
    assert _size(publishers) > PART_BYTES
    parts = publisher_heartbeats(publishers, EXTRA)
    assert len(parts) > 1
    assert [p["x_publishers_part"] for p in parts] == [
        [i, len(parts)] for i in range(1, len(parts) + 1)
    ]
    assert all(_size(p) <= PART_BYTES for p in parts)
    assert all(p["x_rmw"] == EXTRA["x_rmw"] for p in parts)
    assert _joined(parts) == publishers
    assert all("x_publishers_dropped" not in p for p in parts)


def test_one_topic_s_list_is_split_across_parts__s55() -> None:
    publishers = _map(1, 64) | {"/empty": []}
    parts = publisher_heartbeats(publishers, EXTRA, limit=4096)
    assert len(parts) > 1
    assert _joined(parts) == publishers


def test_an_entry_too_large_for_any_part_is_counted_not_dropped_silently__s55() -> None:
    publishers = {"/t": [{"node": "/n", "gid": "a" * 5000}, {"node": "/m", "gid": "b"}]}
    parts = publisher_heartbeats(publishers, EXTRA, limit=1024)
    assert parts[-1]["x_publishers_dropped"] == 1
    assert _joined(parts) == {"/t": [{"node": "/m", "gid": "b"}]}
    huge_topic = {"/" + "t" * 2000: [{"node": "/n"}], "/ok": [{"node": "/m"}] * 40}
    parts = publisher_heartbeats(huge_topic, EXTRA, limit=1024)
    assert parts[-1]["x_publishers_dropped"] == 1
    assert _joined(parts) == {"/ok": [{"node": "/m"}] * 40}


_NAMES = st.from_regex(r"/[a-z_][a-z0-9_]{0,40}", fullmatch=True)
_ENTRIES = st.lists(
    st.fixed_dictionaries(
        {
            "node": st.text(min_size=1, max_size=MAX_NODE_NAME),
            "gid": st.text(alphabet="0123456789abcdef", max_size=48),
        }
    ),
    max_size=40,
)


@settings(max_examples=60, deadline=None)
@given(
    publishers=st.dictionaries(_NAMES, _ENTRIES, max_size=12),
    limit=st.integers(min_value=2048, max_value=PART_BYTES),
)
def test_parts_always_fit_and_rebuild_the_map__s55(
    publishers: dict[str, list[dict[str, str]]], limit: int
) -> None:
    parts = publisher_heartbeats(publishers, EXTRA, limit)
    assert all(_size(p) <= limit for p in parts)
    assert _joined(parts) == publishers
    if len(parts) > 1:
        assert [p["x_publishers_part"] for p in parts] == [
            [i, len(parts)] for i in range(1, len(parts) + 1)
        ]


def test_every_part_is_logged_by_the_recorder_with_its_longest_ids__s55() -> None:
    """Each part passes the recorder's own check (a 64 KiB line once sealed, with its reserve for
    the recorder's fields) with a 128-character robot id and open task id, the longest an
    identifier may be."""
    long_id = "r" * 128
    settings_ = Settings(
        robot_id=long_id,
        scopes=("base",),
        declaration={"scopes": ["base"], "heartbeat_interval_s": 1.0, "simulated": True},
        spec_version=SPEC,
        heartbeat_interval_s=1.0,
        gap_tolerance_s=2.0,
        max_events_per_s=1000,
        max_line_bytes=65536,
    )
    engine = Engine(settings_, FakeClock(), lambda: SYNCED)
    observer = Client("tb4_observer")
    engine.receive(observer.event("task_start", task_id="t" * 128, task_definition_id="nav"))
    parts = publisher_heartbeats(_map(8, 64), EXTRA)
    assert len(parts) > 1
    for part in parts:
        datagram = observer.event("heartbeat", **part)
        assert len(datagram) <= 65536
        (heartbeat,) = engine.receive(datagram)
        assert heartbeat["x_publishers_part"] == part["x_publishers_part"]
    assert "x_rejected_client_events" not in engine.heartbeat()
