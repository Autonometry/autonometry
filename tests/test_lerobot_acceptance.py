"""Acceptance: the LeRobot demo (demo/lerobot/run_demo.py) downloads the public DAgger dataset of
dossier 14.6 (research/lerobot-so101), imports it and writes a report whose time autonomy share
and interval match the dossier's. Needs the network; skipped by default (``-m network``)."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.network

DEMO = Path(__file__).resolve().parent.parent / "demo" / "lerobot" / "run_demo.py"


def _demo() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lerobot_demo", DEMO)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_dataset_gives_the_dossier_share_and_interval__dossier_14_6_defs_7_11(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("statsmodels")
    assert _demo().main(["--out", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "report" / "report.json").read_text(encoding="utf-8"))
    real = report["metrics"]["groups"]["real"]
    share = real["time_autonomy_share"]
    assert round(share["estimate"], 3) == 0.779
    assert [round(limit, 3) for limit in share["ci"]] == [0.668, 0.891]
    assert share["interval_method"] == "ratio_linearised_t(session)"
    assert share["n_clusters"] == 18
    assert round(share["human_local_share"]["estimate"], 3) == 0.221
    assert real["interventions_per_active_hour"]["count"] == 20
    assert report["trust"]["level"] == "L1"
    assert len(list((tmp_path / "logs").glob("*/*/events.jsonl"))) == 18
