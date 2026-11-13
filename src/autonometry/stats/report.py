"""The metrics report: one block of metrics per group of sessions, real and simulated, which are
never pooled (definitions.md 8). Deterministic: the same timelines and options give the same JSON,
with sorted keys and lists in a fixed order; there is no generated-at time."""

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

import numpy as np

from autonometry.spec import Rules
from autonometry.stats.facts import session_facts
from autonometry.stats.metrics import Json, Options, group_metrics
from autonometry.timeline import Timeline

#: Group names by ``session_start.simulated``.
GROUPS: Final[Mapping[bool, str]] = {False: "real", True: "simulated"}


def metrics_report(timelines: Sequence[Timeline], rules: Rules, options: Options) -> Json:
    """Every metric for every group present. A session without ``session_start`` has no
    observed time and no declared ``simulated``; it is listed, never assigned to a group."""
    groups: dict[str, list[Timeline]] = {}
    without_start: list[Timeline] = []
    for timeline in timelines:
        if timeline.start_ns is None or timeline.simulated is None:
            without_start.append(timeline)
        else:
            groups.setdefault(GROUPS[timeline.simulated], []).append(timeline)
    return {
        "options": {
            "alpha": options.alpha,
            "n_boot": options.n_boot,
            "seed": options.seed,
            "cluster": options.cluster,
            "drift_bin": options.drift_bin,
        },
        "groups": {
            name: group_metrics([session_facts(t, rules) for t in members], options, rules)
            for name, members in sorted(groups.items())
        },
        "sessions_without_session_start": sorted(
            ({"robot_id": t.robot_id, "session_id": t.session_id} for t in without_start),
            key=lambda item: (item["robot_id"], item["session_id"]),
        ),
    }


def clean(value: Any) -> Any:
    """A JSON-ready copy: NumPy scalars as Python numbers, non-finite floats as ``None``."""
    if isinstance(value, Mapping):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [clean(item) for item in value]
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, float | np.floating):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def to_json(report: Json) -> str:
    """The report as JSON text with sorted keys, two-space indent and a final newline."""
    return json.dumps(clean(report), indent=2, sort_keys=True, allow_nan=False) + "\n"
