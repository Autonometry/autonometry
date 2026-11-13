"""Tests for the recorder's engine: level triggering, loss, ownership, rate limit, clock
(README "Recorder"; definitions.md 2, 3, 6.14, 8)."""

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from autonometry.recorder.clock import ClockStatus
from autonometry.recorder.engine import (
    MAX_CLIENTS,
    PLACEHOLDER_SESSION_ID,
    SEAL_OVERHEAD_BYTES,
    Engine,
    Restored,
    ScopeValue,
    Settings,
)
from autonometry.timestamps import ts_to_ns
from recorder_helpers import SCOPES, SPEC, START_NS, SYNCED, Client, FakeClock

DECLARATION = {
    "scopes": list(SCOPES),
    "heartbeat_interval_s": 1.0,
    "hint_policy": "hint_as_shared",
    "task_registry_hash": "0" * 64,
    "plan_hash": "0" * 64,
    "simulated": True,
}


def make_engine(
    clock: FakeClock | None = None, max_events_per_s: int = 1000, status: ClockStatus = SYNCED
) -> tuple[Engine, FakeClock]:
    clock = clock or FakeClock()
    settings_ = Settings(
        robot_id="r1",
        scopes=SCOPES,
        declaration=DECLARATION,
        spec_version=SPEC,
        heartbeat_interval_s=1.0,
        gap_tolerance_s=2.0,
        max_events_per_s=max_events_per_s,
        max_line_bytes=65536,
    )
    return Engine(settings_, clock, lambda: status), clock


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [
        f"{e['type']}:{e['scope']}:{e['state']}" if e["type"] == "transition" else e["type"]
        for e in events
    ]


# -- level triggering --------------------------------------------------------------------------


def test_repeated_assertions_write_one_transition__defs_2() -> None:
    engine, clock = make_engine()
    nav = Client("nav")
    written = []
    for _ in range(5):
        written += engine.receive(nav.state("base", "ai", actor_id="ai:nav2", x_src_ts=1.0))
        clock.advance(0.2)
    assert kinds(written) == ["transition:base:ai"]
    assert written[0]["actor_id"] == "ai:nav2"
    assert written[0]["task_id"] is None


def test_a_change_of_reason_actor_or_device_writes_a_transition__defs_3() -> None:
    engine, _ = make_engine()
    op = Client("teleop")
    base = {"reason": "human_initiated", "actor_id": "op_1", "device_id": "joy_local"}
    changes = [
        base,
        {**base, "reason": "ai_requested_help"},
        {**base, "reason": "ai_requested_help", "actor_id": "op_2"},
        {**base, "reason": "ai_requested_help", "actor_id": "op_2", "device_id": "station"},
    ]
    written = []
    for fields in changes:
        written += engine.receive(op.state("base", "human_remote", **fields))
        written += engine.receive(op.state("base", "human_remote", **fields))
    assert len(written) == 4
    assert [e["device_id"] for e in written] == ["joy_local", "joy_local", "joy_local", "station"]


def test_silent_scope_goes_unknown_after_gap_tolerance__defs_6_14() -> None:
    engine, clock = make_engine()
    engine.receive(Client("nav").state("base", "ai", actor_id="ai:nav2"))
    clock.advance(1.0)
    engine.heartbeat()
    clock.advance(0.99)
    assert transitions_of(engine.tick()) == []
    clock.advance(0.01)
    due = engine.tick()
    assert kinds(transitions_of(due)) == ["transition:base:unknown"]
    assert due[0]["x_cause"] == "silent"
    assert ts_to_ns(due[0]["ts"]) == START_NS + 2_000_000_000


def transitions_of(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == "transition"]


def test_next_deadline_is_the_earliest_silence_or_heartbeat__defs_6_14() -> None:
    engine, clock = make_engine()
    assert engine.next_deadline() == pytest.approx(1001.0)
    clock.advance(0.5)
    engine.receive(Client("nav").state("base", "ai", actor_id="ai:nav2"))
    assert engine.next_deadline() == pytest.approx(1001.0)  # the heartbeat comes first
    clock.advance(1.1)
    engine.heartbeat()
    assert engine.next_deadline() == pytest.approx(1002.5)  # the silence comes first


def test_explicit_unknown_from_a_client_is_a_transition__defs_2() -> None:
    engine, _ = make_engine()
    nav = Client("nav")
    engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))
    assert kinds(engine.receive(nav.state("base", "unknown"))) == ["transition:base:unknown"]
    assert engine.receive(nav.state("base", "unknown")) == []


# -- loss is visible ---------------------------------------------------------------------------


def both_ai(engine: Engine, client: Client) -> None:
    engine.receive(client.state("base", "ai", actor_id="ai:nav2"))
    engine.receive(client.state("arm", "ai", actor_id="ai:arm"))


def test_gap_in_n_makes_every_scope_unknown_and_is_counted__defs_2() -> None:
    engine, clock = make_engine()
    nav = Client("nav")
    both_ai(engine, nav)
    nav.n += 2  # two datagrams lost
    written = engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))
    assert kinds(written) == ["transition:base:unknown", "transition:arm:unknown"]
    assert {e.get("x_cause") for e in written} == {"lost"}
    heartbeat = engine.heartbeat()
    assert heartbeat["x_lost_client_events"] == 2
    assert "x_lost_client_events" not in engine.heartbeat()
    clock.advance(1.0)  # one quiet interval: the next assertion restores the scope
    assert kinds(engine.receive(nav.state("base", "ai", actor_id="ai:nav2")))[-1:] == [
        "transition:base:ai"
    ]


def test_client_restart_with_n_zero_is_not_a_loss() -> None:
    engine, _ = make_engine()
    nav = Client("nav")
    both_ai(engine, nav)
    restarted = Client("nav")
    assert engine.receive(restarted.state("base", "ai", actor_id="ai:nav2")) == []
    assert "x_lost_client_events" not in engine.heartbeat()


def test_restart_whose_first_datagrams_were_lost_counts_them() -> None:
    engine, _ = make_engine()
    nav = Client("nav")
    for _ in range(10):
        engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))
    restarted = Client("nav")
    restarted.n = 3
    written = engine.receive(restarted.state("base", "ai", actor_id="ai:nav2"))
    assert kinds(written) == ["transition:base:unknown"]
    assert engine.heartbeat()["x_lost_client_events"] == 3


def test_a_client_that_kept_running_is_counted_as_resumed_not_lost() -> None:
    engine, _ = make_engine()
    nav = Client("nav")
    both_ai(engine, nav)
    late = Client("late")
    late.n = 7  # the recorder restarted while this client kept sending
    written = engine.receive(late.event("help_request", scope="base"))
    assert kinds(written) == [
        "transition:base:unknown",
        "transition:arm:unknown",
        "help_request",
    ]
    assert {e.get("x_cause") for e in written[:2]} == {"resumed"}
    heartbeat = engine.heartbeat()
    assert heartbeat["x_resumed_clients"] == 1
    assert "x_lost_client_events" not in heartbeat
    # Not a loss: assertions restore the scopes at once.
    assert kinds(engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))) == [
        "transition:base:ai"
    ]


def test_rejected_datagrams_make_every_scope_unknown__defs_8() -> None:
    engine, clock = make_engine()
    nav, odd = Client("nav"), Client("odd")  # odd's datagrams are sent in the order built
    bad = [
        b"not json",
        b'["an array"]',
        json.dumps({"client": "bad name!", "n": 0, "event": {}}).encode(),
        odd.event("session_start"),
        odd.event("heartbeat"),
        odd.event("help_request", scope="base", seq=4),
        odd.state("base", "human_remote"),  # no reason or actor
        odd.state("gripper", "ai"),  # undeclared scope
        json.dumps({"client": "c1", "n": 0, "event": {}, "distance_m": 1}).encode(),
        json.dumps({"client": "c2", "n": 0, "distance_m": -1}).encode(),
        b'{"client": "c3", "n": 0, "n": 1, "event": {}}',
        b"\xff\xfe",
        b"x" * 70000,
    ]
    for data in bad:
        clock.advance(1.0)  # a quiet interval, so the assertions restore the scopes
        both_ai(engine, nav)
        written = engine.receive(data)
        transitions = [e for e in written if e["type"] == "transition"]
        assert kinds(transitions)[-2:] == [
            "transition:base:unknown",
            "transition:arm:unknown",
        ], data
        assert {e["x_cause"] for e in transitions} == {"rejected"}


def test_rejection_keeps_following_the_client_counter() -> None:
    engine, _ = make_engine()
    nav = Client("nav")
    both_ai(engine, nav)
    engine.receive(nav.state("base", "human_remote"))  # rejected, n still followed
    engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))
    heartbeat = engine.heartbeat()
    assert heartbeat["x_rejected_client_events"] == 1
    assert "x_lost_client_events" not in heartbeat


def test_rate_limit_drops_makes_every_scope_unknown_and_counts() -> None:
    engine, clock = make_engine(max_events_per_s=5)
    nav = Client("nav")
    both_ai(engine, nav)
    for _ in range(3):
        assert engine.receive(nav.event("help_request", scope="base"))[0]["type"] == "help_request"
    written = engine.receive(nav.event("help_request", scope="base"))
    assert kinds(written) == ["transition:base:unknown", "transition:arm:unknown"]
    assert written[0]["x_cause"] == "rate_limited"
    clock.advance(0.2)  # one token back, but not yet a quiet interval
    assert engine.receive(nav.state("base", "ai", actor_id="ai:nav2")) == []
    heartbeat = engine.heartbeat()
    assert heartbeat["x_rate_limited_events"] == 1
    assert "x_lost_client_events" not in heartbeat
    clock.advance(0.8)
    assert kinds(engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))) == [
        "transition:base:ai"
    ]


@pytest.mark.parametrize("disturbance", ["rejected", "rate_limited", "lost"])
def test_a_flood_writes_at_most_one_unknown_per_scope_per_interval(disturbance: str) -> None:
    engine, clock = make_engine(max_events_per_s=20)
    nav, noisy = Client("nav"), Client("noisy")
    both_ai(engine, nav)
    engine.receive(noisy.event("help_request", scope="base"))  # known from n = 0
    written = []
    for _ in range(300):  # 3 s of a flood, one disturbance every 10 ms
        if disturbance == "rejected":
            written += engine.receive(b"garbage")
        elif disturbance == "rate_limited":
            written += engine.receive(noisy.event("help_request", scope="base"))
        else:
            noisy.n += 1
            written += engine.receive(noisy.event("help_request", scope="base"))
        written += engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))
        written += engine.receive(nav.state("arm", "ai", actor_id="ai:arm"))
        clock.advance(0.01)
    unknowns = [e for e in written if e["type"] == "transition" and e["state"] == "unknown"]
    assert len(unknowns) == 2  # once per scope: the flood never left a quiet interval
    restored = [e for e in written if e["type"] == "transition" and e["state"] == "ai"]
    assert restored == []
    clock.advance(0.5)
    assert transitions_of(engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))) == []
    clock.advance(0.5)  # a whole interval without a disturbance
    assert kinds(transitions_of(engine.receive(nav.state("base", "ai", actor_id="ai:nav2")))) == [
        "transition:base:ai"
    ]


def test_quiet_assertions_keep_ownership_alive__defs_6_14() -> None:
    engine, clock = make_engine()
    nav = Client("nav")
    both_ai(engine, nav)
    for _ in range(30):  # 3 s of rejections; nav keeps asserting
        engine.receive(b"garbage")
        engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))
        clock.advance(0.1)
    assert all(e.get("x_cause") != "silent" for e in engine.tick())


def test_more_than_max_clients_are_rejected() -> None:
    engine, _ = make_engine()
    for index in range(MAX_CLIENTS):
        engine.receive(Client(f"c{index}").event("help_request", scope="base"))
    engine.receive(Client("one_too_many").event("help_request", scope="base"))
    engine.receive(b'{"client": "another", "n": 0}')
    assert engine.heartbeat()["x_rejected_client_events"] == 2


# -- ownership ---------------------------------------------------------------------------------


def test_assertions_from_a_second_client_are_dropped_and_counted__defs_6_14() -> None:
    engine, clock = make_engine()
    nav, teleop = Client("nav"), Client("teleop")
    engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))
    foreign = teleop.state("base", "human_remote", reason="human_initiated", actor_id="op_1")
    assert engine.receive(foreign) == []
    assert engine.receive(teleop.state("arm", "ai", actor_id="ai:arm"))  # an unowned scope
    assert engine.heartbeat()["x_foreign_assertions"] == 1
    clock.advance(2.0)
    assert kinds(engine.tick()) == [
        "transition:base:unknown",
        "transition:arm:unknown",
        "heartbeat",
    ]
    takeover = teleop.state("base", "human_remote", reason="human_initiated", actor_id="op_1")
    assert kinds(engine.receive(takeover)) == ["transition:base:human_remote"]
    assert engine.receive(nav.state("base", "ai", actor_id="ai:nav2")) == []


def test_owner_keeps_its_scope_after_a_loss() -> None:
    engine, clock = make_engine()
    nav, teleop = Client("nav"), Client("teleop")
    engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))
    engine.receive(b"garbage")
    clock.advance(1.0)
    assert transitions_of(engine.receive(teleop.state("base", "ai", actor_id="ai:other"))) == []
    assert kinds(engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))) == [
        "transition:base:ai"
    ]


# -- discrete events, tasks, distance, clock ---------------------------------------------------


def test_discrete_events_are_written_as_received_and_tasks_tracked__defs_8() -> None:
    engine, _ = make_engine()
    nav, ui = Client("nav"), Client("ui")
    start = engine.receive(ui.event("task_start", task_id="t1", task_definition_id="dock_v1"))
    assert start == [
        {
            "type": "task_start",
            "ts": start[0]["ts"],
            "task_id": "t1",
            "task_definition_id": "dock_v1",
        }
    ]
    assert engine.receive(nav.state("base", "ai", actor_id="ai:nav2"))[0]["task_id"] == "t1"
    assert engine.receive(ui.event("help_request", scope="base"))[0]["task_id"] == "t1"
    assert engine.heartbeat()["task_id"] == "t1"
    engine.receive(ui.event("task_end", task_id="t2", outcome="success"))  # another task
    assert engine.heartbeat()["task_id"] == "t1"
    engine.receive(ui.event("task_end", task_id="t1", outcome="success"))
    assert engine.heartbeat()["task_id"] is None
    for kind, fields in [
        ("hint", {"scope": "base", "actor_id": "op_1"}),
        (
            "approval",
            {"scope": "base", "actor_id": "op_1", "reason": "planned", "plan_item_id": "g"},
        ),
        ("supervision", {"level": "remote_monitoring", "n_supervisors": 1}),
        ("support_start", {"device_id": "gantry_hoist"}),
        ("support_end", {}),
    ]:
        (event,) = engine.receive(ui.event(kind, **fields))
        assert event["type"] == kind


def test_heartbeat_carries_clock_status_and_distance__defs_8() -> None:
    engine, _ = make_engine(status=ClockStatus("ptp", 0.0002))
    heartbeat = engine.heartbeat()
    assert heartbeat["clock_source"] == "ptp"
    assert heartbeat["clock_max_error_s"] == 0.0002
    assert "distance_m" not in heartbeat
    odometry = Client("odom")
    engine.receive(odometry.distance(100.0))
    assert engine.heartbeat()["distance_m"] == 0.0
    engine.receive(odometry.distance(102.5))
    engine.receive(odometry.distance(101.0))  # odometer reset: never counted backwards
    engine.receive(odometry.distance(103.0))
    assert engine.heartbeat()["distance_m"] == 4.5
    engine.session_start()
    assert engine.heartbeat()["distance_m"] == 0.0


def test_heartbeat_without_a_clock_error_omits_it__defs_8() -> None:
    engine, _ = make_engine(status=ClockStatus("unknown", None))
    heartbeat = engine.heartbeat()
    assert heartbeat["clock_source"] == "unknown"
    assert "clock_max_error_s" not in heartbeat


def test_heartbeats_are_due_every_interval__defs_2() -> None:
    engine, clock = make_engine()
    assert engine.tick() == []
    clock.advance(1.0)
    assert kinds(engine.tick()) == ["heartbeat"]
    clock.advance(0.5)
    assert engine.tick() == []
    clock.advance(5.0)  # late: one heartbeat, not a burst
    assert kinds(engine.tick()) == ["heartbeat"]


def test_backward_clock_step_is_clamped_and_reported__defs_3() -> None:
    engine, clock = make_engine()
    first = engine.receive(Client("ui").event("help_request", scope="base"))[0]
    clock.wall -= 5_000_000_000
    clock.advance(0.5)
    second = engine.receive(Client("ui2").event("help_request", scope="base"))[0]
    assert second["ts"] == first["ts"]
    clock.wall -= 1_000_000_000
    heartbeat = engine.heartbeat()
    assert heartbeat["ts"] == first["ts"]
    assert heartbeat["x_clock_stepped_back_s"] == pytest.approx(5.5)
    clock.wall += 6_000_000_000  # the clock catches up: nothing more to report
    assert "x_clock_stepped_back_s" not in engine.heartbeat()


def test_session_start_declares_current_states__defs_2_rule_1() -> None:
    engine, _ = make_engine()
    start = engine.session_start()
    assert start["initial_states"] == {"base": "unknown", "arm": "unknown"}
    assert {k: start[k] for k in DECLARATION} == DECLARATION
    engine.receive(Client("nav").state("base", "ai", actor_id="ai:nav2"))
    assert engine.session_start()["initial_states"] == {"base": "ai", "arm": "unknown"}
    end = engine.session_end("rotation")
    assert (end["type"], end["x_cause"], end["task_id"]) == ("session_end", "rotation", None)


def test_restore_continues_from_a_recovered_session() -> None:
    engine, _ = make_engine()
    engine.restore(
        Restored(
            {"base": ScopeValue("ai", None, "ai:nav2"), "arm": ScopeValue("idle")},
            "t9",
            12.5,
            START_NS + 60_000_000_000,
        )
    )
    heartbeat = engine.heartbeat()
    assert (heartbeat["task_id"], heartbeat["distance_m"]) == ("t9", 12.5)
    assert ts_to_ns(heartbeat["ts"]) == START_NS + 60_000_000_000
    assert "x_clock_stepped_back_s" in heartbeat
    assert kinds(engine.all_unknown("recovered")) == [
        "transition:base:unknown",
        "transition:arm:unknown",
    ]


def test_an_event_longer_than_a_line_once_sealed_is_rejected__defs_8() -> None:
    engine, _ = make_engine()
    probe = Client("probe")
    (event,) = engine.receive(probe.event("help_request", scope="base", x_pad=""))
    trial = {**event, "robot_id": "r1", "session_id": PLACEHOLDER_SESSION_ID, "seq": 2**53 - 1}
    room = 65536 - SEAL_OVERHEAD_BYTES - len(json.dumps(trial, separators=(",", ":")))
    assert engine.receive(probe.event("help_request", scope="base", x_pad="p" * room))
    engine.receive(probe.event("help_request", scope="base", x_pad="p" * (room + 1)))
    assert engine.heartbeat()["x_rejected_client_events"] == 1


@settings(max_examples=300, deadline=None)
@given(st.lists(st.binary(max_size=300) | st.from_regex(rb"\{.{0,200}\}", fullmatch=True)))
def test_random_datagrams_never_crash_the_engine(datagrams: list[bytes]) -> None:
    engine, clock = make_engine()
    for data in datagrams:
        for event in engine.receive(data):
            assert event["type"] in ("transition", "heartbeat")
        clock.advance(0.3)
        engine.tick()


# -- client heartbeats -------------------------------------------------------------------------


def test_a_client_heartbeat_adds_its_x_fields_to_a_recorder_heartbeat__defs_8() -> None:
    engine, _ = make_engine()
    observer = Client("tb4_observer")
    engine.receive(observer.distance(2.0))
    publishers = {"/cmd_vel": [{"node": "/collision_monitor", "gid": "01.0f"}]}
    (heartbeat,) = engine.receive(observer.event("heartbeat", x_publishers=publishers))
    assert heartbeat["type"] == "heartbeat"
    assert heartbeat["x_publishers"] == publishers
    assert heartbeat["clock_source"] == "ntp"
    assert heartbeat["distance_m"] == 0.0
    assert heartbeat["task_id"] is None


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"distance_m": 1.0},
        {"clock_source": "ntp"},
        {"x_lost_client_events": 0},
        {"x_cause": "shutdown", "x_note": "hidden"},
    ],
)
def test_a_client_heartbeat_without_x_fields_or_with_recorder_fields_is_rejected__defs_8(
    fields: dict[str, Any],
) -> None:
    engine, _ = make_engine()
    nav = Client("nav")
    both_ai(engine, nav)
    written = engine.receive(nav.event("heartbeat", **fields))
    assert kinds(written) == ["transition:base:unknown", "transition:arm:unknown"]
    assert engine.heartbeat()["x_rejected_client_events"] == 1


def test_a_client_heartbeat_too_long_with_the_recorder_fields_is_rejected__defs_8() -> None:
    engine, _ = make_engine()
    nav = Client("nav")
    both_ai(engine, nav)
    written = engine.receive(nav.event("heartbeat", x_blob="a" * 64800))
    assert {e["x_cause"] for e in written} == {"rejected"}
