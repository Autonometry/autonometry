# Autonometry

Autonometry measures how much of a robot's work is done by its AI and how much by humans, local or remote.
It records every change of control in a sealed, tamper-evident log, verifies logs, reconciles a robot's log with a teleoperation station's log, and reports autonomy metrics with honest confidence intervals.
It is open source and the reference implementation of an open standard; it only observes and never sits in a robot's control loop.

**Status: pre-alpha.** Version 0.0.1.dev0 reads and checks event logs (`autonometry validate`). There are no autonomy metrics yet, and nothing is published on PyPI yet.

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
autonometry validate robot.jsonl station.jsonl         # summary; exit 1 if any line is rejected
autonometry validate --json --spec 0.2 robot.jsonl     # the same as stable JSON
```

`validate` reads each file line by line. A line that is not one valid event (bad UTF-8, longer than
64 KiB, not I-JSON, against the schema, an impossible `ts`, or a `seq` already used in its session)
is rejected with an error code and its line number. Each session is then checked for structural
problems (missing `session_start` or `session_end`, gaps in `seq`, silences longer than the heartbeat
plus tolerance, `ts` going backwards, tasks that overlap or never close, and more), reported as
warning flags. The codes are listed in [`spec/v0.2/rules.toml`](spec/v0.2/rules.toml). Event contents,
such as actor ids, are never printed. Add `-v` before the command for debug output.

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
