"""Property tests of the LeRobot connector: random log lines and random intervention columns never
crash; every message is an event the recorder accepts (event.schema.json, definitions.md 3 and 8);
an imported episode's AI and human time are its labelled frames over fps (definitions.md 2, 4)."""

import io
import json
from fractions import Fraction
from typing import Any

import rfc8785
from hypothesis import given, settings
from hypothesis import strategies as st

from autonometry.connectors import lerobot
from autonometry.connectors.tb4 import StateAssertion, TaskEnd, TaskStart
from autonometry.reader import check_event, read_streams
from autonometry.recorder.engine import PLACEHOLDER_SESSION_ID
from autonometry.strictjson import MAX_SAFE_INTEGER
from autonometry.timeline import Registries, build_timeline
from lerobot_helpers import CONFIG, DEVICES, POLICY, RULES, SETUP, engine, line

STATES = frozenset(RULES.states)
HUMAN = frozenset({"human_local", "human_remote", "shared"})
MESSAGES = [
    *SETUP,
    "Connecting teleoperator (phone)...",
    "DAgger continuous recording started (episode_duration=600s)",
    "DAgger corrections-only recording started (target: 5 episodes)",
    "Sentry recording started (episode_duration=600s)",
    "Highlight strategy recording started (press 's' to save)",
    "Base strategy control loop started",
    *(f"Phase transition: {a} -> {b}" for a in lerobot._PHASES for b in lerobot._PHASES),
    "Episode saved (total: 3, elapsed: 60.0s)",
    "Episode saved (total: 3)",
    "Correction 1/5 saved",
    "Recording episode 2",
    "Recording episode 0",
    "Reset the environment",
    "Re-record episode",
    "DAgger continuous control loop ended — pausing engine",
    "Stopping inference engine...",
    "Returning robot to initial position before shutdown...",
    "Disconnecting robot...",
    "Rollout finished",
]
TIMES = st.floats(min_value=0.0, max_value=500.0, allow_nan=False)
LINES = st.one_of(
    st.tuples(TIMES, st.sampled_from(MESSAGES)).map(lambda p: ("line", p[0], line(p[1], p[0]))),
    st.tuples(
        TIMES, st.just("Left arrow key pressed. Exiting loop and rerecord the last episode...")
    ).map(lambda p: ("line", p[0], p[1])),
    st.tuples(TIMES, st.text(max_size=200)).map(lambda p: ("line", p[0], p[1])),
    TIMES.map(lambda t: ("tick", t, "")),
)


def _accepted(message: lerobot.Message) -> None:
    """The message as the recorder would write it passes the event schema and reader checks."""
    event: dict[str, Any] = lerobot.wire_event(message)
    event.setdefault("task_id", None)
    event.update(
        robot_id="so101_01",
        session_id=PLACEHOLDER_SESSION_ID,
        seq=MAX_SAFE_INTEGER,
        ts="2026-11-14T10:00:00.000Z",
    )
    check_event(event, "0.3")


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=400))
def test_random_text_never_crashes_the_parser__claude_md_property_tests(text: str) -> None:
    parsed = lerobot.parse_line(text)
    assert parsed is None or isinstance(parsed, lerobot.Parsed)


@settings(max_examples=300, deadline=None)
@given(st.lists(LINES, max_size=60), st.sampled_from([("whole_robot",), ("left", "right")]))
def test_random_logs_never_crash_and_stay_in_the_vocabulary__defs_2_3_5_8(
    steps: list[tuple[str, float, str]], scopes: tuple[str, ...]
) -> None:
    eng = engine(scopes=scopes)
    open_task: str | None = None
    task_ids: set[str] = set()
    for kind, t, text in steps:
        if kind == "tick":
            messages = eng.tick(t)
            assert len(messages) in (0, len(scopes))
        else:
            parsed = lerobot.parse_line(text)
            messages = [] if parsed is None else eng.observe(t, parsed)
        for message in messages:
            _accepted(message)
            if isinstance(message, StateAssertion):
                assert message.state in STATES
                assert message.scope in scopes
                if message.state in HUMAN:
                    assert message.reason is not None
                    assert message.actor_id
            elif isinstance(message, TaskStart):
                assert open_task is None, "tasks never nest"
                assert message.task_id not in task_ids, "task ids never repeat"
                open_task = message.task_id
                task_ids.add(message.task_id)
            elif isinstance(message, TaskEnd):
                assert message.task_id == open_task
                open_task = None


LABELS = st.lists(st.sampled_from([True, False, None]), max_size=120)


@settings(max_examples=150, deadline=None)
@given(
    LABELS,
    st.lists(st.integers(min_value=0, max_value=3), max_size=120),
    st.integers(min_value=0, max_value=20),
    st.sampled_from([Fraction(30), Fraction(2997, 100), Fraction(1, 3)]),
)
def test_random_intervention_columns_never_crash_and_add_up__defs_2_4_8(
    labels: list[bool | None], skips: list[int], extra: int, fps: Fraction
) -> None:
    frames, frame = [], 0
    for index in range(len(labels)):
        frame += skips[index] if index < len(skips) else 0
        frames.append(frame)
        frame += 1
    episode = lerobot.Episode(4, "t", len(labels) + extra, tuple(frames), tuple(labels))
    settings_ = lerobot.ImportSettings(
        robot_id="so101_01",
        fps=fps,
        task_registry_hash="0" * 64,
        plan_hash="0" * 64,
        device_registry_hash="0" * 64,
        heartbeat_interval_s=RULES.params.heartbeat_interval_s,
        devices=DEVICES,
        policy_path=POLICY,
        teleop="so101_leader",
    )
    events = lerobot.episode_events(settings_, episode, 1_700_000_000_000_000_000, "pick")
    for event in events:
        check_event(json.loads(rfc8785.dumps(event)), "0.3")
    data = b"".join(rfc8785.dumps(e) + b"\n" for e in events)
    check = read_streams([("episode.jsonl", io.BytesIO(data))], "0.3")
    assert check.errors == ()
    assert {f.code for f in check.flags} <= {"clock_unsynced", "ts_reconstructed"}
    (session,) = check.sessions
    devices = json.loads((CONFIG / "devices.json").read_text(encoding="utf-8"))
    timeline = build_timeline(session, Registries.from_documents(devices, None, None), RULES)
    states = timeline.whole_robot.states
    frame_s = Fraction(1) / fps
    ai_frames = sum(1 for v in labels if v is False)
    human_frames = sum(1 for v in labels if v is True)
    tolerance = 2 * len(labels) + 2
    assert abs(states["ai"] - round(ai_frames * frame_s * 10**9)) <= tolerance
    assert abs(states["human_local"] - round(human_frames * frame_s * 10**9)) <= tolerance
    assert timeline.whole_robot.observed_ns == sum(states.values())
    span = max(len(labels) + extra, frames[-1] + 1 if frames else 0)
    assert abs(timeline.whole_robot.observed_ns - round(span * frame_s * 10**9)) <= 1
