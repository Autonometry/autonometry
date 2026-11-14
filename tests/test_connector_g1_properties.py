"""Hypothesis properties of the Unitree G1 engine (integration notes section 10): random
observation sequences never crash and give only valid states; ``legs`` is never ``ai`` while a
person's goal span is open; every scope is ``unknown`` once the robot is lost; the same
observations give the same messages."""

import json
import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from autonometry.connectors import g1
from autonometry.connectors.tb4 import Source, StateAssertion
from g1_helpers import (
    ARM_SERVICE,
    LOWSTATE,
    POLICY_ARM,
    POLICY_ARM_REQ,
    POLICY_LOWCMD,
    POLICY_SPORT,
    REPLAY_LOWCMD,
    RULES,
    SPORT,
    STRANGER_LOWCMD,
    STRANGER_SPORT,
    XR_ARM,
    XR_DEX3,
    XR_LOWCMD,
    XR_SPORT,
    XR_SWITCH,
    engine,
    remote_bytes,
    seen,
)

GAP = RULES.params.gap_tolerance_s
RELEASE = RULES.params.goal_release_s
STATES = {
    "ai",
    "human_local",
    "human_remote",
    "shared",
    "scripted",
    "safety_stop",
    "idle",
    "unknown",
}
NEEDS_REASON = {"human_local", "human_remote", "shared", "safety_stop"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
#: Writers the strategy may make appear and go; the robot's and the persons' stay.
VOLATILE = (ARM_SERVICE, XR_ARM, XR_LOWCMD, XR_DEX3, POLICY_LOWCMD, POLICY_ARM, REPLAY_LOWCMD)
COMMANDS = (*VOLATILE, STRANGER_LOWCMD, "w-unknown")
TOPICS = {
    ARM_SERVICE: g1.ARM_SDK,
    XR_ARM: g1.ARM_SDK,
    XR_LOWCMD: g1.LOWCMD,
    XR_DEX3: "rt/dex3/left/cmd",
    POLICY_LOWCMD: g1.LOWCMD,
    POLICY_ARM: g1.ARM_SDK,
    REPLAY_LOWCMD: g1.LOWCMD,
    STRANGER_LOWCMD: g1.LOWCMD,
    "w-unknown": g1.HAND_SDK,
}
REQUESTERS = (XR_SPORT, XR_SWITCH, POLICY_SPORT, POLICY_ARM_REQ, STRANGER_SPORT)
#: (service, api id) of requests other than SetVelocity.
APIS = (
    ("sport", 7101),
    ("sport", 7106),
    ("sport", 7110),
    ("sport", 7111),
    ("sport", 7001),
    ("sport", 7102),
    ("motion_switcher", 1001),
    ("motion_switcher", 1002),
    ("motion_switcher", 1003),
    ("arm", 7106),
    ("arm", 7108),
    ("arm", 7113),
)
PARAMS = st.sampled_from(
    [
        "{}",
        '{"data": 1}',
        '{"data": 0}',
        '{"data": 3}',
        '{"data": 702}',
        '{"data": 500}',
        '{"action_id": 99}',
        '{"action_id": 18}',
        '{"action_name": "bow"}',
        '{"name": ""}',
        '{"name": "ai"}',
        "not json",
        "[1, 2]",
        '{"velocity": "fast", "duration": 0.2}',
    ]
)
COMBOS = (
    (),
    ("L2", "B"),
    ("L2", "R2"),
    ("L2", "Y"),
    ("L2", "UP"),
    ("L2", "LEFT"),
    ("SELECT", "Y"),
    ("R1", "X"),
    ("L2",),
)
STICK = st.sampled_from([0.0, 0.0, 0.005, -0.3, 0.8, math.nan, math.inf])


@st.composite
def remote(draw: st.DrawFn) -> bytes:
    if draw(st.integers(0, 9)) == 0:
        return draw(st.binary(min_size=0, max_size=40))
    buttons = draw(st.sampled_from(COMBOS))
    return remote_bytes(*buttons, lx=draw(STICK), ly=draw(STICK))


STEP = st.one_of(
    st.tuples(st.just("lowstate"), remote()),
    st.tuples(st.just("lowstate_other"), remote()),
    st.tuples(
        st.just("sport"),
        st.tuples(
            st.sampled_from([0, 1, 2, 3, 4, 500, 501, 702, 706, 801]),
            st.sampled_from([0, 1]),
            st.sampled_from([0, 0, 0, 2]),
        ),
    ),
    st.tuples(
        st.just("command"),
        st.tuples(
            st.sampled_from(COMMANDS),
            st.one_of(st.none(), st.floats(-0.5, 1.5), st.just(math.nan)),
        ),
    ),
    st.tuples(
        st.just("request"),
        st.tuples(st.sampled_from(REQUESTERS), st.sampled_from(APIS), PARAMS),
    ),
    st.tuples(
        st.just("velocity"),
        st.tuples(
            st.sampled_from([XR_SPORT, STRANGER_SPORT, POLICY_SPORT]),
            st.sampled_from([0.0, 0.3]),
            st.sampled_from([0.2, 1.0, 3.0]),
        ),
    ),
    st.tuples(
        st.just("response"),
        st.tuples(st.sampled_from([1001, 1002, 1003, 7110]), st.integers(0, 3), st.integers(0, 2)),
    ),
    st.tuples(
        st.just("arm_state"),
        st.sampled_from(
            [
                '{"holding": true, "id": 1, "name": "wave"}',
                '{"holding": false, "id": 1}',
                '{"holding": false, "id": 99}',
                "garbage",
            ]
        ),
    ),
    st.tuples(st.just("writer"), st.tuples(st.sampled_from(VOLATILE), st.booleans())),
    st.tuples(st.just("task"), st.sampled_from(["open", "close", "unreadable"])),
    st.tuples(st.just("tick"), st.none()),
)


@dataclass
class Oracle:
    """What the properties assert, worked out from the observations alone: the persons' goal
    spans (the remote's sticks and SetVelocity from the persons' sources) and the robot's
    liveness."""

    last_lowstate: float | None = None
    spans: dict[str, tuple[float, bool]] = field(default_factory=dict)

    def person_open(self, now: float) -> bool:
        return any(not closed and now < until - 1e-9 for until, closed in self.spans.values())

    def lost(self, now: float) -> bool:
        return self.last_lowstate is None or now - self.last_lowstate > GAP + 1e-9


def _sticks_out(data: bytes) -> bool | None:
    if len(data) < 24:
        return None
    lx, _rx, _ry, _l2, ly = struct.unpack_from("<5f", data, 4)
    return not all(math.isfinite(v) and abs(v) <= g1.STICK_DEAD_ZONE for v in (lx, ly))


def _observation(kind: str, payload: Any, t: float, oracle: Oracle) -> g1.Observation | None:  # noqa: PLR0911, PLR0912 -- one branch per kind
    match kind:
        case "lowstate":
            oracle.last_lowstate = t
            out = _sticks_out(payload)
            if out:
                oracle.spans["remote"] = (t + RELEASE, False)
            elif out is False and "remote" in oracle.spans:
                oracle.spans["remote"] = (oracle.spans["remote"][0], True)
            return g1.LowState(t, LOWSTATE, payload)
        case "lowstate_other":
            oracle.last_lowstate = t
            return g1.LowState(t, "w-other-lowstate", payload)
        case "sport":
            return g1.SportState(t, SPORT, *payload)
        case "command":
            key, weight = payload
            return g1.Command(t, key, TOPICS[key], weight)
        case "request":
            key, (service, api), params = payload
            return g1.Request(t, key, service, int(t * 1000) % 7, api, params)
        case "velocity":
            key, vx, duration = payload
            if key != POLICY_SPORT:
                if vx == 0.0:
                    until = oracle.spans.get(key, (t, True))[0]
                    oracle.spans[key] = (until, True)
                else:
                    oracle.spans[key] = (t + max(RELEASE, duration), False)
            text = json.dumps({"velocity": [vx, 0.0, 0.0], "duration": duration})
            return g1.Request(t, key, "sport", 1, 7105, text)
        case "response":
            api, request_id, code = payload
            service = "sport" if api == 7110 else "motion_switcher"
            data = '{"name": ""}' if request_id == 0 else '{"name": "ai"}'
            return g1.Response(t, "w-resp", service, request_id, api, code, data)
        case "arm_state":
            return g1.ArmState(t, "w-arm-state", payload)
        case "writer":
            key, appear = payload
            return seen(t, key, decoded=bool(int(t * 10) % 3)) if appear else g1.WriterGone(t, key)
        case "task":
            if payload == "open":
                return g1.TaskOpened(t, "t1")
            return g1.TaskClosed(t) if payload == "close" else g1.TasksUnreadable(t)
    return None


def _check(message: g1.Message, now: float, oracle: Oracle, hands: bool) -> None:
    if not isinstance(message, StateAssertion):
        assert isinstance(message, g1.Note)
        json.dumps(dict(message.fields), allow_nan=False)
        return
    assert message.scope in (
        (g1.LEGS, g1.UPPER_BODY, g1.HANDS) if hands else (g1.LEGS, g1.UPPER_BODY)
    )
    assert message.state in STATES
    assert (message.reason is not None) or message.state not in NEEDS_REASON
    assert message.actor_id is None or ID.fullmatch(message.actor_id)
    if message.state in NEEDS_REASON - {"safety_stop"}:
        assert message.actor_id is not None
    json.dumps(dict(message.fields), allow_nan=False)
    if oracle.lost(now):
        assert message.state == "unknown", message
    if message.scope == g1.LEGS and oracle.person_open(now):
        assert message.state != "ai", message


def _run(steps: list[tuple[float, str, Any]], hands: bool) -> list[g1.Message]:
    eng = engine(hands=hands)
    oracle = Oracle()
    out: list[g1.Message] = []
    for key in (LOWSTATE, SPORT, XR_SPORT, XR_SWITCH, POLICY_SPORT, POLICY_ARM_REQ, STRANGER_SPORT):
        out += eng.observe(seen(0.0, key))
    eng.observe(g1.WriterSeen(0.0, "w-other-lowstate", Source(host="sim", topic=g1.LOWSTATE)))
    t = 0.0
    for dt, kind, payload in steps:
        t = round(t + dt, 6)
        if kind == "tick":
            messages = eng.tick(t)
        else:
            observation = _observation(kind, payload, t, oracle)
            assert observation is not None
            messages = eng.observe(observation)
        for message in messages:
            _check(message, t, oracle, hands)
        out += messages
    out += eng.stop()
    return out


SEQUENCES = st.lists(
    st.tuples(st.sampled_from([0.0, 0.05, 0.1, 0.5, 1.5, 3.0]), STEP).map(
        lambda item: (item[0], item[1][0], item[1][1])
    ),
    max_size=120,
)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(SEQUENCES, st.booleans())
def test_random_observations_never_crash_and_keep_the_properties__notes_10(
    steps: list[tuple[float, str, Any]], hands: bool
) -> None:
    _run(steps, hands)


@settings(max_examples=50, deadline=None)
@given(SEQUENCES)
def test_the_same_observations_give_the_same_messages__claude_md_deterministic(
    steps: list[tuple[float, str, Any]],
) -> None:
    assert _run(steps, True) == _run(steps, True)
