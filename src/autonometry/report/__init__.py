"""The report (definitions.md 11; needs the ``report`` extra): one JSON document and one
self-contained HTML file that say what was measured, how far to trust it, and the numbers with
their uncertainty.

- :mod:`~autonometry.report.build`: the report as a dict, from pure functions.
- :mod:`~autonometry.report.trust`, :mod:`~autonometry.report.census`,
  :mod:`~autonometry.report.statements`, :mod:`~autonometry.report.invariants`: its sections.
- :mod:`~autonometry.report.figures`: inline SVG figures drawn with matplotlib.
- :mod:`~autonometry.report.html`: the HTML page, rendered with jinja2 (autoescape on).
"""
