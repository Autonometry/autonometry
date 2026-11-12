"""Slow timing of the timeline (run with ``uv run pytest -m slow --no-cov -s``)."""

import sys
import time

import pytest

from autonometry.reader import check_event
from autonometry.timeline import build_timeline
from timeline_helpers import FULL, RULES, SPEC, Log

SCOPES = ("left_arm", "right_arm", "left_leg", "right_leg", "torso", "head")
EVENTS = 100_000


def _session() -> Log:
    """100k events over 1000 s on six scopes: tasks, takeovers, approvals, heartbeats."""
    log = Log(SCOPES, initial="idle", heartbeat_s=1.0)
    while log.seq < EVENTS - 1:
        cycle = log.seq % 100
        t = log.seq / 100
        if cycle == 0:
            log.add(t, "task_start", task=f"t{log.seq}")
        elif cycle <= len(SCOPES):
            log.ai(t, SCOPES[cycle - 1])
        elif cycle == 40:
            log.human(t, scope=SCOPES[(log.seq // 100) % len(SCOPES)])
        elif cycle == 60:
            log.ai(t, SCOPES[(log.seq // 100) % len(SCOPES)])
        elif cycle == 70:
            log.add(t, "approval", scope="head", actor_id="op_b", reason="planned",
                    plan_item_id="reset_scene", dwell_s=0.05)  # fmt: skip
        elif cycle == 99 and log.task is not None:
            log.add(t, "task_end")
        else:
            log.add(t, "heartbeat", clock_source="ntp", clock_max_error_s=0.001)
    return log.end(log.seq / 100)


@pytest.mark.slow
def test_timeline_of_100k_events_is_fast__defs_2_to_8(capsys: pytest.CaptureFixture[str]) -> None:
    log = _session()
    session = log.session()
    assert len(session.events) == EVENTS
    for event in session.events:
        check_event(event, SPEC)
    started = time.perf_counter()
    timeline = build_timeline(session, FULL, RULES)
    elapsed = time.perf_counter() - started
    with capsys.disabled():
        sys.stdout.write(
            f"\ntimeline of {EVENTS} events on {len(SCOPES)} scopes: {elapsed:.2f} s\n"
        )
    assert len(timeline.tasks) == 999  # seq 0 is the session_start, not a task_start
    assert len(timeline.episodes) == 1000
    assert len(timeline.discrete) == 1000
    assert sum(timeline.whole_robot.states.values()) == timeline.observed_ns
    assert elapsed < 20, f"{elapsed:.1f} s"
