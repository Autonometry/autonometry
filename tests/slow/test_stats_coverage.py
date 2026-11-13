"""Coverage of the default intervals on the baseline scenario of the simulation study
(research/simulation, scenario ``i_baseline_poisson``): 20 sessions, 400 replicates, 500
bootstrap replicates. Each default interval of (a), (b), (c), (e) and the Kaplan-Meier median of
(d) must cover the population truth in 0.91 to 0.985 of the replicates.

The generator is a port of ``simulate.simulate_session`` for the baseline scenario (no frailty,
no Hawkes bursts, no unknown time): sessions of LogNormal(1200 s, 0.4) active time on 4 robots,
candidate interventions at 8 per active hour thinned by three task types, takeovers (dwell
LogNormal(40 s, 0.7), clipped at the task end), approvals (LogNormal(3 s, 0.5)) and hints
(LogNormal(2 s, 0.5)). Each session is rendered as events and goes through the real pipeline:
timeline, session facts, metric functions. The truths are the study's population values
(research/simulation/README.md, Section 2, 40000 sessions): time share including approval and
hint dwell 0.9349, 7.345 interventions per active hour, task autonomy rate 0.5747, 0.3159 human
minutes per task, median gap 0.0935 active hours. Their Monte Carlo errors (at most 0.0024,
0.18, 0.0074, 0.012) are small beside the interval widths.
"""

import math
from collections.abc import Iterator
from typing import Any, cast

import numpy as np
import pytest

from autonometry.events import Event, Session
from autonometry.registry import registry_hash
from autonometry.stats import intervals as iv
from autonometry.stats import metrics as m
from autonometry.stats.facts import SessionFacts, session_facts
from autonometry.timeline import Registries, build_timeline
from autonometry.timestamps import ns_to_ts, ts_to_ns
from timeline_helpers import DEVICES, RULES

pytestmark = pytest.mark.slow

REPLICATES = 400
SESSIONS = 20
N_BOOT = 500
LOW, HIGH = 0.91, 0.985
SEED = 20261113
TRUTH = {
    "time_share": 0.9349,
    "task_rate": 0.5747,
    "rate": 7.345,
    "human_minutes": 0.3159,
    "gap_median": 0.0935,
}

LAMBDA0 = 8.0
TYPE_PROBS = (0.5, 0.35, 0.15)
TYPE_MULT = (0.7, 1.0, 2.0)
KIND_PROBS = (0.7, 0.2, 0.1)
ROBOTS = 4
SESSIONS_PER_DAY = 4
TASKS = {"tasks": [{"task_definition_id": f"type_{k}"} for k in range(3)]}
PLAN = {"items": [{"plan_item_id": "gate", "kind": "approval_gate"}]}
REGISTRIES = Registries.from_documents(DEVICES, TASKS, PLAN)
START_NS = ts_to_ns("2026-10-01T08:00:00.000Z")


class _Events:
    """Events of one session in time order, ``seq`` rising by one."""

    def __init__(self, robot: str, session: str, start_ns: int) -> None:
        self.robot, self.session, self.start_ns = robot, session, start_ns
        self.events: list[dict[str, Any]] = []
        self.task: str | None = None

    def add(self, t: float, kind: str, **fields: Any) -> None:
        event = {
            "robot_id": self.robot,
            "session_id": self.session,
            "task_id": self.task,
            "seq": len(self.events),
            "ts": ns_to_ts(self.start_ns + round(t * 1e9)),
            "type": kind,
        }
        self.events.append(event | fields)


def _session(rng: np.random.Generator, index: int) -> Session:
    """One baseline session (simulate.simulate_session with every effect switched off)."""
    duration = float(rng.lognormal(math.log(1200.0), 0.4))
    rate_s = LAMBDA0 * max(TYPE_MULT) / 3600.0
    times = np.sort(rng.uniform(0.0, duration, rng.poisson(rate_s * duration)))
    starts: list[float] = []
    t = 0.0
    while t < duration:
        starts.append(t)
        t += float(rng.lognormal(math.log(300.0), 0.5))
    ends = [*starts[1:], duration]
    types = rng.choice(3, size=len(starts), p=TYPE_PROBS)
    robot, day = f"r{index % ROBOTS:02d}", index // SESSIONS_PER_DAY
    out = _Events(robot, f"s{index:03d}", START_NS + day * 86_400 * 10**9)
    out.add(
        0.0,
        "session_start",
        scopes=["whole_robot"],
        initial_states={"whole_robot": "idle"},
        heartbeat_interval_s=86_400.0,
        hint_policy="hint_as_event",
        task_registry_hash=registry_hash(TASKS),
        plan_hash=registry_hash(PLAN),
        device_registry_hash=registry_hash(DEVICES),
        simulated=True,
    )
    ai = {"scope": "whole_robot", "state": "ai", "reason": None, "actor_id": "ai:policy"}
    candidates = iter(times)
    pending = next(candidates, None)
    for k, (a, b) in enumerate(zip(starts, ends, strict=True)):
        out.task = f"t{k}"
        out.add(a, "task_start", task_definition_id=f"type_{types[k]}")
        out.add(a, "transition", device_id="policy", **ai)
        human_end = -1.0
        while pending is not None and pending < b:
            te, pending = float(pending), next(candidates, None)
            if rng.random() > TYPE_MULT[types[k]] / max(TYPE_MULT) or te < human_end:
                continue
            u = rng.random()
            if u < KIND_PROBS[0]:
                end = min(te + float(rng.lognormal(math.log(40.0), 0.7)), b)
                out.add(
                    te,
                    "transition",
                    scope="whole_robot",
                    state="human_remote",
                    reason="human_initiated",
                    actor_id="op_01",
                    device_id="vr_remote",
                )
                human_end = end
                if end < b:
                    out.add(end, "transition", device_id="policy", **ai)
            elif u < KIND_PROBS[0] + KIND_PROBS[1]:
                dwell = float(rng.lognormal(math.log(3.0), 0.5))
                out.add(
                    te,
                    "approval",
                    scope="whole_robot",
                    actor_id="op_01",
                    reason="planned",
                    plan_item_id="gate",
                    dwell_s=dwell,
                )
            else:
                dwell = float(rng.lognormal(math.log(2.0), 0.5))
                out.add(te, "hint", scope="whole_robot", actor_id="op_01", dwell_s=dwell)
        out.add(b, "task_end", outcome="success")
        out.task = None
    out.add(duration, "session_end")
    return Session(out.robot, out.session, tuple(cast(Event, e) for e in out.events))


def _replicate(rep: int) -> list[SessionFacts]:
    rng = np.random.default_rng([SEED, rep])
    sessions = [_session(rng, i) for i in range(SESSIONS)]
    return [session_facts(build_timeline(s, REGISTRIES, RULES), RULES) for s in sessions]


def _covers(ci: list[float | None] | None, truth: float) -> bool:
    return ci is not None and ci[0] is not None and ci[1] is not None and ci[0] <= truth <= ci[1]


def _intervals(rep: int) -> Iterator[tuple[str, bool]]:
    sessions = _replicate(rep)
    options = m.Options(n_boot=N_BOOT, seed=rep)
    share = m.time_autonomy_share(sessions, options, RULES)
    yield "time_share", _covers(share["ci"], TRUTH["time_share"])
    task_rate, _ = m.task_rates(sessions, options)
    yield "task_rate", _covers(task_rate["ci"], TRUTH["task_rate"])
    rate = m.interventions_rate(sessions, options)
    yield "rate", _covers(rate["ci"], TRUTH["rate"])
    minutes = m.human_minutes(sessions, options)
    yield "human_minutes", _covers(minutes["ci"], TRUTH["human_minutes"])
    rows, _ = m.gap_table(sessions, "session", "active_hours")
    km = iv.kaplan_meier([g.duration for g in rows], [g.observed for g in rows], 0.05, ())
    yield "gap_median", _covers(km.median_interval.as_list(), TRUTH["gap_median"])


@pytest.fixture(scope="module")
def coverage() -> dict[str, float]:
    hits: dict[str, int] = dict.fromkeys(TRUTH, 0)
    for rep in range(REPLICATES):
        for name, covered in _intervals(rep):
            hits[name] += covered
    return {name: count / REPLICATES for name, count in hits.items()}


def test_generator_matches_the_baseline_scenario() -> None:
    sessions = [s for rep in range(5) for s in _replicate(rep)]
    active_h = sum(s.active_s for s in sessions) / 3600
    assert sum(s.interventions for s in sessions) / active_h == pytest.approx(7.345, rel=0.15)
    assert sum(len(s.tasks) for s in sessions) / len(sessions) == pytest.approx(4.47, rel=0.1)
    assert all(s.unknown_s == 0 for s in sessions)


@pytest.mark.parametrize("name", list(TRUTH))
def test_default_interval_coverage_on_the_baseline(coverage: dict[str, float], name: str) -> None:
    assert LOW <= coverage[name] <= HIGH, coverage
