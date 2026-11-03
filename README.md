# Autonometry

Autonometry measures how much of a robot's work is done by its AI and how much by humans, local or remote.
It records every change of control in a sealed, tamper-evident log, verifies logs, reconciles a robot's log with a teleoperation station's log, and reports autonomy metrics with honest confidence intervals.
It is open source and the reference implementation of an open standard; it only observes and never sits in a robot's control loop.

**Status: pre-alpha.** Version 0.0.1.dev0 is the project skeleton: packaging, CI and the shipped specification. There are no measurement features yet, and nothing is published on PyPI yet.

## Install

From a source checkout (Python 3.12 or newer, [uv](https://docs.astral.sh/uv/)):

```
uv sync --all-extras --dev
uv run autonometry --help
```

Once released: `pip install autonometry` (extras: `report`, `lerobot`).

## Try it

```
autonometry --version
autonometry spec list          # spec versions shipped with the package
autonometry spec show 0.2      # its files with SHA-256 hashes
```

## Links

- [`spec/`](spec/): the normative definitions, the event JSON Schema and the machine-readable rules (current draft: v0.2). The wheel ships this folder as `autonometry/spec/`.
- [`research/`](research/): design-phase material, for reference only. It is not part of the package.
- [`CHANGELOG.md`](CHANGELOG.md), [`SECURITY.md`](SECURITY.md), [`RELEASING.md`](RELEASING.md)

## Development

```
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
```

## Licence

Apache-2.0. See [`LICENSE`](LICENSE).
