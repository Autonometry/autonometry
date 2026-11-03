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

### Changed

- Minimum dependency versions lowered to the oldest releases that provide the features we use and carry no known advisory for them (`uv.lock` keeps exact pins). The tooling run by `uv run pytest` now also excludes `research/` from mypy and from `pytest .`.
- Slow statistical tests run with `uv run pytest -m slow --no-cov`.
