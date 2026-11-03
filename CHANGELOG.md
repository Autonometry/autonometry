# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project skeleton: `src/autonometry` package, uv lockfile, hatchling build, Apache-2.0 licence.
- `autonometry.spec`: `available_versions`, `load_schema`, `load_rules` (frozen pydantic model of `rules.toml`) and `check_consistency` for the specification shipped inside the wheel as `autonometry/spec/`.
- `autonometry` command line: `--version`, `spec list`, `spec show <version>`.
- Tooling: ruff, mypy strict, pytest with coverage gate (90 %) and a `slow` marker.
- GitHub automation: CI (Python 3.12 and 3.13, x86-64 and arm64, pip-audit), release workflow with PyPI Trusted Publishing, Dependabot.
- `SECURITY.md`, `RELEASING.md`.
- CI job that installs the lowest allowed versions of the direct dependencies (`uv --resolution lowest-direct`, Python 3.12) and runs the tests, so the minimum versions in `pyproject.toml` are proven.
- `autonometry validate PATH... [--json] [--spec VERSION]`: reads JSON Lines event logs safely and reports rejected lines and structural flags. Exit status 0 when nothing was rejected (flags are warnings), 1 when any line was rejected, 2 on bad usage. Event contents are never printed.
- `autonometry.reader`: streaming reader for one or many log files. Lines are read as bytes with a length limit (a longer line is skipped without being held in memory), decoded as UTF-8 (a BOM only at the start of a file; LF and CRLF), parsed as I-JSON (RFC 7493: no duplicate keys, no NaN or Infinity, integers within ±(2^53−1)), validated against the event schema, and their `ts` checked for impossible dates. Valid events are grouped by `(robot_id, session_id)` and ordered by `seq`; a repeated `seq` is rejected. Memory grows with the events kept, never with rejected data.
- `autonometry.checks`: per-session loader flags `no_session_start`, `no_session_end`, `missing_initial_state`, `undeclared_scope`, `seq_gap`, `ts_regression`, `time_gap`, `nested_task`, `task_end_without_start`, `task_not_closed`, `distance_regression`, `ts_reconstructed`.
- `autonometry.events`: immutable, JSON-serialisable results (`LogCheck`, `Session`, `LineError`, `Flag`, `Counts`) and an `Event` TypedDict.
- `autonometry.strictjson` (I-JSON parsing) and `autonometry.timestamps` (RFC 3339 UTC timestamps as integer nanoseconds).
- Spec v0.2: `rules.toml` `[errors]` table (error codes for rejected lines, `max_line_bytes = 65536`) and the **Rejected lines** paragraph in `definitions.md` section 8. `Rules` loads and checks the new table.
- Command line option `--verbose` (`-v`): debug output and full tracebacks.
- Dependency `jsonschema-rs`: compiled Draft 2020-12 validator, about 70 times faster than `jsonschema` on the event schema, with ECMA-262 regular expressions as JSON Schema requires. `jsonschema` stays for loading and checking the schema itself.
- CI: test jobs on Windows and macOS (Python 3.12), and a dependency audit of both the locked and the lowest-version sets.
- `.gitattributes`: LF line endings in the repository on every OS, so spec file hashes match everywhere.

### Changed

- Minimum dependency versions lowered to the oldest releases that provide the features we use and carry no known advisory for them (`uv.lock` keeps exact pins). The tooling run by `uv run pytest` now also excludes `research/` from mypy and from `pytest .`.
- Slow statistical tests run with `uv run pytest -m slow --no-cov`.
- Logging is configured once by the console entry point: warnings and errors print as one clean line without exception text; an unexpected crash prints only `error: unexpected failure (<type>)` unless `--verbose` is given.
- `Rules.states` is a read-only mapping, so loaded rules can be shared and cached.
- Project URLs, `SECURITY.md` and `RELEASING.md` use the exact GitHub owner name `Autonometry` (PyPI Trusted Publishing compares it); release tags are annotated (`git tag -a`).

### Removed

- The `License :: OSI Approved` classifier: the SPDX `license` field replaces it (PEP 639).
