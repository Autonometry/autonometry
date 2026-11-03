"""Produce report_example.json from example_log.jsonl.

Usage: python run_example_report.py [log.jsonl] [out.json]
The example log carries heartbeats every 10 s (to stay small), so the loader is
run with gap_tolerance_s=30; the single deliberate 45-50 s silence in session
s002 therefore yields ~20 s of `unknown`, visible in report['unmeasured'].
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import autonomy_meter as am  # noqa: E402


def main(log: str | None = None, out: str | None = None) -> dict:
    here = Path(__file__).resolve().parent
    log_path = Path(log) if log else here / "example_log.jsonl"
    out_path = Path(out) if out else here / "report_example.json"
    events = am.read_jsonl(log_path)
    errors = am.validate_events(events)
    chain_ok, chain_problems = am.verify_chain(events)
    tables = am.build_tables(events, am.Params(gap_tolerance_s=30.0))
    report = am.full_report(tables, alpha=0.05, n_boot=1000, seed=0, drift_freq="D")
    report["input"] = {"path": log_path.name, "n_events": len(events), "schema_errors": len(errors),
                       "chain_ok": chain_ok, "chain_problems": chain_problems[:10]}
    report["planning_examples"] = {
        "hours_zero_interventions_for_rate_below_0.1_per_h_95": am.planning.hours_for_zero_events(0.1, 0.05),
        "same_with_nb_alpha_0.5": am.planning.hours_for_zero_events_nb(0.1, 0.5, 0.05),
        "tasks_zero_failures_for_p_below_0.05_95": am.planning.tasks_for_zero_failures(0.05, 0.05),
        "hours_to_show_rate_below_0.1_if_true_0.05": am.planning.hours_to_demonstrate_rate(0.1, 0.05, 0.05),
        "power_halving_rate_100h_each": am.planning.power_two_periods(1.0, 0.5, 100, 100, 0.05, 0.3),
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    print(f"wrote {out_path}")
    ts = report["time_autonomy_share"]
    print(f"time autonomy share {ts['estimate']:.3f} bootstrap CI {ts['default']['ci_bootstrap']} outer {ts['outer_interval']}")
    ta = report["task_autonomy_rate"]["point"]
    print(f"task autonomy rate {ta['k']}/{ta['n']} = {ta['estimate']:.3f} Wilson {ta['ci_wilson']}")
    ip = report["interventions_per_active_hour"]
    print(f"interventions/active hour {ip['estimate']:.2f} Garwood {ip['ci_poisson_exact']} ({ip['interval_method']})")
    return report


if __name__ == "__main__":
    main(*(sys.argv[1:3]))
