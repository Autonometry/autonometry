"""Property tests of autonometry.stats (definitions.md 7): random timelines never crash the
report, its JSON has no NaN, and the bounds bracket the point estimate of the time share and the
task rates (``unknown_as_human <= point <= unknown_as_ai``)."""

import dataclasses
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from autonometry.stats.metrics import Options
from autonometry.stats.report import metrics_report, to_json
from stats_helpers import build
from test_timeline_properties import REGISTRIES, logs
from timeline_helpers import RULES

OPTIONS = Options(n_boot=20)
EPS = 1e-12


def _ordered(block: dict[str, Any]) -> None:
    point = block["estimate"]
    lower = block["unknown_as_human"]["estimate"]
    upper = block["unknown_as_ai"]["estimate"]
    if point is not None:
        assert lower is not None
        assert upper is not None
        assert lower <= point + EPS
        assert point <= upper + EPS


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_random_timelines_never_crash_and_bounds_bracket__defs_7(data: st.DataObject) -> None:
    timelines = []
    for index in range(data.draw(st.integers(1, 3))):
        log = data.draw(logs())
        if data.draw(st.booleans()):
            log.events[0] = log.events[0] | {"simulated": True}
        robot = data.draw(st.sampled_from(["r01", "r02", "r03"]))
        timeline = build(log, robot, f"s{index}", registries=data.draw(REGISTRIES))
        timelines.append(timeline)
    options = dataclasses.replace(
        OPTIONS, cluster=data.draw(st.sampled_from(["auto", "session", "robot"]))
    )
    report = metrics_report(timelines, RULES, options)
    text = to_json(report)
    assert "NaN" not in text
    assert "Infinity" not in text
    for group in report["groups"].values():
        _ordered(group["time_autonomy_share"])
        _ordered(group["task_autonomy_rate"])
        _ordered(group["autonomous_success_rate"])
        lower = group["human_minutes_per_task"]["unknown_as_human"]["estimate"]
        point = group["human_minutes_per_task"]["estimate"]
        if point is not None:
            assert lower >= point - EPS
