"""Property tests of the TurtleBot 4 engine: random observation streams never crash, every state
is in the vocabulary (definitions.md 2), and every message is an event the recorder accepts
(event.schema.json, definitions.md 3 and 8)."""

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from autonometry.connectors import tb4
from autonometry.reader import check_event
from autonometry.recorder.engine import PLACEHOLDER_SESSION_ID
from autonometry.strictjson import MAX_SAFE_INTEGER
from tb4_helpers import BUTTON_2, JOY, KBD, MISSION, NAV2, POWER, RULES, RVIZ, STRANGER, TB4_NODE
from tb4_helpers import engine as make_engine

STATES = frozenset(RULES.states)
HUMAN = frozenset({"human_local", "human_remote", "shared"})
SOURCES = st.sampled_from([NAV2, KBD, JOY, RVIZ, MISSION, STRANGER, TB4_NODE, BUTTON_2, POWER])
GOALS = st.sampled_from(["a" * 32, "b" * 32, "c" * 32])
TIMES = st.floats(min_value=-5.0, max_value=200.0, allow_nan=False)
SPEEDS = st.floats(min_value=-2.0, max_value=2.0, allow_nan=False) | st.just(0.0)
STATUS = st.integers(min_value=0, max_value=6)
ACTIONS = st.sampled_from([tb4.NAVIGATE, "dock", "undock", "wall_follow", "spin"])
CALLS = st.sampled_from(["Dock", "Undock", "Wall Follow Left", "EStop", "Power", "Select"])
COORDS = st.floats(min_value=-1e3, max_value=1e3, allow_nan=False)

OBSERVATIONS = st.one_of(
    st.builds(
        tb4.CmdVel,
        TIMES,
        SPEEDS,
        SPEEDS,
        SPEEDS,
        st.none() | SOURCES,
        st.lists(SOURCES, max_size=3).map(tuple),
    ),
    st.builds(tb4.CmdVelSmoothed, TIMES, SPEEDS, SPEEDS, SPEEDS),
    st.builds(
        tb4.ActionStatus,
        TIMES,
        ACTIONS,
        st.lists(st.builds(tb4.GoalStatus, GOALS, STATUS), max_size=3).map(tuple),
        st.lists(SOURCES, max_size=3).map(tuple),
    ),
    st.builds(tb4.CollisionMonitorState, TIMES, st.integers(min_value=0, max_value=4)),
    st.builds(tb4.FunctionCall, TIMES, CALLS),
    st.builds(tb4.ButtonEdge, TIMES, SOURCES, st.booleans()),
    st.builds(tb4.Joy, TIMES, st.lists(st.integers(0, 1), max_size=8).map(tuple)),
    st.builds(tb4.WheelStatus, TIMES, st.booleans()),
    st.builds(tb4.DockStatus, TIMES, st.booleans()),
    st.builds(tb4.KidnapStatus, TIMES, st.booleans()),
    st.builds(
        tb4.InitialPose, TIMES, st.none() | SOURCES, st.lists(SOURCES, max_size=3).map(tuple)
    ),
    st.builds(tb4.Odometry, TIMES, COORDS, COORDS, SPEEDS, SPEEDS),
)
STEPS = st.lists(st.one_of(OBSERVATIONS, TIMES.map(lambda t: ("tick", t))), max_size=60)


def _accepted(message: tb4.Message) -> None:
    """The message as the recorder would write it passes the event schema and reader checks."""
    if isinstance(message, tb4.Distance):
        assert message.metres >= 0
        return
    event: dict[str, Any] = tb4.wire_event(message)
    event.setdefault("task_id", "t1" if event["type"] in ("task_start", "task_end") else None)
    event.update(
        robot_id="tb4_sim_01",
        session_id=PLACEHOLDER_SESSION_ID,
        seq=MAX_SAFE_INTEGER,
        ts="2026-11-13T10:00:00.000Z",
    )
    check_event(event, "0.3")


@settings(max_examples=300, deadline=None)
@given(STEPS, st.sampled_from(["hint_as_shared", "hint_as_event"]))
def test_random_streams_never_crash_and_stay_in_the_vocabulary__defs_2_3_8(
    steps: list[Any], policy: str
) -> None:
    engine = make_engine(hint_policy=policy)
    open_tasks: set[str] = set()
    distance = 0.0
    for step in steps:
        messages = engine.tick(step[1]) if isinstance(step, tuple) else engine.observe(step)
        for message in messages:
            _accepted(message)
            if isinstance(message, tb4.StateAssertion):
                assert message.state in STATES
                assert message.scope == "base"
                if message.state in HUMAN:
                    assert message.reason is not None
                    assert message.actor_id
                if message.state == "safety_stop":
                    assert message.reason == "safety"
            elif isinstance(message, tb4.TaskStart):
                assert not open_tasks, "tasks never nest"
                open_tasks.add(message.task_id)
            elif isinstance(message, tb4.TaskEnd):
                assert open_tasks == {message.task_id}
                open_tasks.clear()
            elif isinstance(message, tb4.Distance):
                assert message.metres >= distance
                distance = message.metres
