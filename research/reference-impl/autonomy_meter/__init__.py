"""autonomy_meter — reference implementation of the Autonomy Meter metrics.

Modules
-------
events     load/validate JSON-Lines event logs, build state intervals, handle unknown gaps
metrics    time autonomy share, task autonomy rate, interventions per hour, censored gaps,
           human minutes per task, breakdowns, drift, unmeasured time, full_report
intervals  interval estimators (Wilson, Clopper-Pearson, Garwood, NB2, cluster bootstrap,
           cluster-robust GLM, ratio linearisation)
planning   test-length planning (zero-event rule, binomial, NB-inflated, two-period power)
"""
from .events import Params, Tables, build_tables, read_jsonl, validate_events, verify_chain, load_schema
from .metrics import (time_autonomy_share, task_autonomy_rate, interventions_per_hour,
                      gaps_between_interventions, human_minutes_per_task, breakdowns, drift,
                      unmeasured, full_report)
from . import intervals, planning

__version__ = "0.1.1"
__all__ = ["Params", "Tables", "build_tables", "read_jsonl", "validate_events", "verify_chain", "load_schema",
           "time_autonomy_share", "task_autonomy_rate", "interventions_per_hour", "gaps_between_interventions",
           "human_minutes_per_task", "breakdowns", "drift", "unmeasured", "full_report", "intervals", "planning"]
