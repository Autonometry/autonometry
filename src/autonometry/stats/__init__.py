"""Statistics for the metrics of the standard (needs the ``report`` extra).

The core package never imports this package or its dependencies (NumPy, SciPy, statsmodels):
``autonometry metrics`` imports it only when it runs. Everything here is deterministic: the same
timelines, options and seed give the same numbers.

- :mod:`~autonometry.stats.intervals`: estimators and confidence intervals.
- :mod:`~autonometry.stats.facts`: the per-session numbers the metrics need, read from timelines.
- :mod:`~autonometry.stats.metrics`: metrics (a) to (h) with bounds (definitions.md 7).
- :mod:`~autonometry.stats.report`: the JSON report, one block per group (definitions.md 8).
- :mod:`~autonometry.stats.planning`: test-length planning and two-period power.
"""
