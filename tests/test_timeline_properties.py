"""Property tests of autonometry.timeline (definitions.md 2, 4, 6.6): for any valid session the
state times sum exactly to observed time, spans tile the session, episodes never overlap within
a scope, the order of a file's lines changes nothing, and nothing crashes."""

import io
import json
from fractions import Fraction
from itertools import pairwise
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from autonometry.reader import check_event, read_streams
from autonometry.timeline import Registries, Timeline, build_timeline, dwell_shift
from timeline_helpers import DEVICES, FULL, PLAN, RULES, SPEC, TASKS, Log

STATES = [
    "ai",
    "human_local",
    "human_remote",
    "shared",
    "scripted",
    "safety_stop",
    "idle",
    "unknown",
]
REASONS = [None, "ai_requested_help", "human_initiated", "safety", "planned"]
DEVICE_IDS = [None, "policy", "replay", "joy_local", "vr_remote", "hoist", "rogue"]
REGISTRIES = st.sampled_from(
    [
        FULL,
        Registries(),
        Registries.from_documents(None, TASKS, PLAN),
        Registries.from_documents(DEVICES, None, None),
    ]
)


def _transition(draw: Any, log: Log, t: float, scope: str) -> None:
    state = draw(st.sampled_from(STATES))
    if state == "safety_stop":
        reason: str | None = "safety"
    elif state in ("human_local", "human_remote", "shared"):
        reason = draw(st.sampled_from(REASONS[1:]))
    else:
        reason = draw(st.sampled_from(REASONS))
    actor = draw(st.sampled_from([None, "op_a", "op_b", "ai:policy"]))
    if state in ("human_local", "human_remote", "shared") and actor is None:
        actor = "op_a"
    extra = {}
    if reason == "planned":
        extra["plan_item_id"] = draw(st.sampled_from(["reset_scene", "other"]))
    log.tr(t, state, reason, actor, draw(st.sampled_from(DEVICE_IDS)), scope, **extra)


def _event(draw: Any, log: Log, t: float, scopes: list[str]) -> None:
    kind = draw(
        st.sampled_from(
            ["transition"] * 6
            + ["approval", "hint", "help_request", "task_start", "task_end", "supervision"]
            + ["heartbeat", "support_start", "support_end", "session_start"]
        )
    )
    scope = draw(st.sampled_from([*scopes, "ghost"]))
    dwell = draw(st.sampled_from([None, 0.0, 1.5, 30.0]))
    if kind == "transition":
        _transition(draw, log, t, scope)
    elif kind == "approval":
        fields: dict[str, Any] = {"scope": scope, "actor_id": "op_c"}
        fields["reason"] = draw(st.sampled_from(["ai_requested_help", "planned"]))
        if fields["reason"] == "planned":
            fields["plan_item_id"] = "reset_scene"
        log.add(t, "approval", **fields, **({} if dwell is None else {"dwell_s": dwell}))
    elif kind == "hint":
        log.add(
            t, "hint", scope=scope, actor_id="op_d", **({} if dwell is None else {"dwell_s": dwell})
        )
    elif kind == "help_request":
        log.add(t, "help_request", scope=scope)
    elif kind == "task_start":
        log.add(t, "task_start", task=draw(st.sampled_from(["t1", "t2"])),
                task_definition_id=draw(st.sampled_from(["pick_v1", "other"])))  # fmt: skip
    elif kind == "task_end":
        task = log.task or "t9"
        log.add(t, "task_end", task_id=task, outcome=draw(st.sampled_from(["success", "unknown"])))
    elif kind == "supervision":
        level = draw(st.sampled_from(["none", "remote_monitoring", "ready_to_takeover"]))
        log.add(t, "supervision", level=level, n_supervisors=draw(st.integers(0, 3)))
    elif kind == "heartbeat":
        log.add(t, "heartbeat", distance_m=draw(st.sampled_from([0.0, 1.0, 2.5])))
    elif kind in ("support_start", "support_end"):
        log.add(t, kind, **({"scope": scope} if draw(st.booleans()) else {}))
    else:
        common = ("robot_id", "session_id", "task_id", "seq", "ts", "type")
        log.add(t, "session_start", **{k: v for k, v in log.events[0].items() if k not in common})


@st.composite
def logs(draw: Any) -> Log:
    scopes = draw(st.lists(st.sampled_from(["a", "b", "c"]), min_size=1, max_size=3, unique=True))
    initial = {s: draw(st.sampled_from(STATES)) for s in scopes if draw(st.booleans())}
    weights = draw(st.sampled_from([None, {s: 1 + i for i, s in enumerate(scopes)}, {"a": 2}]))
    extra = {} if weights is None else {"scope_weights": weights}
    log = Log(scopes, initial=initial, heartbeat_s=draw(st.sampled_from([0.5, 1.0, 30.0])), **extra)
    t = 0.0
    for _ in range(draw(st.integers(0, 30))):
        t = max(0.0, t + draw(st.integers(-1000, 5000)) / 1000)
        if draw(st.integers(0, 9)) == 0:
            log.skip()
        _event(draw, log, t, scopes)
    if draw(st.booleans()):
        log.end(t + draw(st.integers(0, 3000)) / 1000)
    for event in log.events:
        check_event(event, SPEC)
    return log


def _tiles(spans: Any, start: int, end: int) -> bool:
    edges = [(s.start_ns, s.end_ns) for s in spans]
    if start == end:
        return not edges
    return (
        edges[0][0] == start
        and edges[-1][1] == end
        and all(a[1] == b[0] for a, b in pairwise(edges))
        and all(a < b for a, b in edges)
    )


def _check(timeline: Timeline) -> None:
    observed = timeline.observed_ns
    for scope in timeline.scopes:
        totals = timeline.per_scope[scope]
        assert sum(totals.states.values()) == totals.observed_ns == observed
        assert totals.active_ns + totals.idle_ns + totals.unknown_ns == observed
        scope_spans = [s for s in timeline.spans if s.scope == scope]
        assert timeline.start_ns is not None
        assert timeline.end_ns is not None
        assert _tiles(scope_spans, timeline.start_ns, timeline.end_ns)
    assert sum(timeline.whole_robot.states.values()) == observed
    if timeline.scopes:
        assert sum(timeline.weights.values()) == 1
        assert sum(timeline.scope_weighted.states.values(), Fraction(0)) == observed
        assert timeline.start_ns is not None
        assert timeline.end_ns is not None
        assert _tiles(timeline.whole_spans, timeline.start_ns, timeline.end_ns)
    for scope in timeline.scopes:
        episodes = sorted((e.start_ns, e.end_ns) for e in timeline.episodes if e.scope == scope)
        assert all(a[1] <= b[0] for a, b in pairwise(episodes))
        assert all(start <= end for start, end in episodes)
    for task in timeline.tasks:
        assert task.start_ns <= task.end_ns
    json.dumps(timeline.to_json())


@settings(max_examples=300, deadline=None)
@given(logs(), REGISTRIES)
def test_state_times_sum_to_observed_and_episodes_never_overlap__defs_2_4_6_6(
    log: Log, registries: Registries
) -> None:
    _check(log.build(registries))


@settings(max_examples=300, deadline=None)
@given(logs(), REGISTRIES, st.sampled_from([0.0, 2.0, 30.0]))
def test_dwell_never_moves_more_than_the_ai_time__defs_6_1(
    log: Log, registries: Registries, minimum: float
) -> None:
    timeline = log.build(registries)
    shift = dwell_shift(timeline, minimum, minimum)
    assert shift.whole_robot_moved_ns <= timeline.whole_robot.states["ai"]
    for scope, moved in shift.moved_ns_by_scope.items():
        assert moved <= timeline.per_scope[scope].states["ai"]
    assert shift.scope_weighted_moved_ns <= timeline.scope_weighted.states["ai"]
    for task in timeline.tasks:
        # the task's human time never exceeds its duration plus the reset attributed to it
        human = task.human_ns + task.dwell_moved_ns + task.unplanned_reset_ns
        assert human <= task.end_ns - task.start_ns + task.unplanned_reset_ns


@settings(max_examples=150, deadline=None)
@given(logs(), REGISTRIES, st.randoms(use_true_random=False))
def test_shuffling_the_lines_changes_nothing__defs_3(
    log: Log, registries: Registries, random: Any
) -> None:
    lines = [json.dumps(event).encode() + b"\n" for event in log.events]
    shuffled = lines[:]
    random.shuffle(shuffled)

    def report(chunks: list[bytes]) -> list[dict[str, Any]]:
        result = read_streams([("events.jsonl", io.BytesIO(b"".join(chunks)))], SPEC)
        return [
            build_timeline(session, registries, RULES, result.flags).to_json()
            for session in result.sessions
        ]

    assert report(shuffled) == report(lines)
