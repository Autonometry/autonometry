# Autonometry

Autonometry measures how much of a robot's work is done by its AI and how much by humans, local or remote.
It records every change of control in a sealed, tamper-evident log, verifies logs, reconciles a robot's log with a teleoperation station's log, and reports autonomy metrics with honest confidence intervals.
It is open source and the reference implementation of an open standard; it only observes and never sits in a robot's control loop.

**Status: pre-alpha.** Version 0.0.1.dev0 reads and checks event logs (`autonometry validate`) and
seals and verifies them (`autonometry seal`, `timestamp`, `verify`; trust levels L1 and L2). There
are no autonomy metrics yet, and nothing is published on PyPI yet.

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
64 KiB, not I-JSON, a lone surrogate or Unicode noncharacter, against the schema, an impossible
`ts`, or a `seq` already used in its session) is rejected with an error code and its line number.
Every rejected line is counted; the first 100 of each code are listed. Each session is then checked for structural
problems (missing `session_start` or `session_end`, gaps in `seq`, silences longer than the heartbeat
plus tolerance, `ts` going backwards, tasks that overlap or never close, and more), reported as
warning flags. The codes are listed in [`spec/v0.2/rules.toml`](spec/v0.2/rules.toml). Event contents,
such as actor ids, are never printed; the robot and session ids it does print are restricted by
the schema to letters, digits and `._:-`, so a log cannot send control codes to your terminal. Add
`-v` before the command for debug output.

## Sealing and verification

```
autonometry keys generate --name autonometry/<robot_id> --out robot.key   # writes robot.key, robot.key.vkey
autonometry seal robot.jsonl --key robot.key --out robot.sealed.jsonl     # + robot.sealed.checkpoints.jsonl
autonometry timestamp robot.sealed.checkpoints.jsonl \
    --tsa-url https://freetsa.org/tsr --tsa-root freetsa.pem             # optional, needs network
autonometry verify robot.sealed.jsonl --vkey robot.key.vkey --json       # trust level per session
```

`seal` hash-chains every event (definitions.md section 8) and emits signed Merkle checkpoints
(RFC 9162, C2SP's `signed-note` and `tlog-checkpoint` formats, Ed25519) as it goes (section 10.3),
refusing input that has rejected lines, is already sealed, or would overwrite an existing output.
`timestamp` anchors a checkpoint with a real RFC 3161 token from a Time Stamping Authority; an
anchor file -- a copy of a checkpoint kept outside the log, by the customer or a witness -- does
the same without a TSA (`verify --anchor FILE`). `verify` recomputes every event's hash from its
content and every Merkle root from the events, and never trusts a stored value; every checkpoint
and anchor is checked against them, so an anchor also catches events or whole sessions deleted
later, together with their checkpoints. Each session is reported `verified`, `partial`,
`unverified` or `failed`, with evidence codes (any one means the log contradicts its own seals:
tampering or corruption) and gap codes (never raise the trust level). Records that cannot be tied to
one session (a malformed checkpoint, a timestamp for no checkpoint) are file problems. The output
ends with `result: verified` or `result: NOT verified: <reasons>`; rejected lines of any file read
and file problems make the result not verified whatever the sessions' statuses. The codes are in
`spec/v0.2/rules.toml` `[sealing]`.

What L2 proves: nothing was dropped, re-timed or edited after a checkpoint was anchored, and any
such change is detected even without the original log. What it does not prove: a dishonest recorder
can still log false events as they happen, and a key held in software can sign a second, fabricated
log from scratch -- only an anchor made *before* the fabrication catches that (an anchor the
fabricator can also redo does not). Sealing an old, unanchored log late is indistinguishable from a
log that was sealed on time but anchored late, and shows up as `late_anchor`. Trust levels L3 (a
second, independent witness) and L4 (hardware-rooted keys) follow in later spec versions.

## Supported platforms

Python 3.12 or newer on:

- Linux x86-64 and ARM64, with glibc or musl
- macOS
- Windows

`jsonschema-rs` ships binary wheels for these. On other platforms, such as 32-bit ARM, installing it
needs a Rust toolchain to build it from source.

## Links

- [`spec/`](spec/): the normative definitions, the event JSON Schema and the machine-readable rules (current draft: v0.2). The wheel ships this folder as `autonometry/spec/`.
- [`research/`](research/): design-phase material, for reference only. It is not part of the package.
- [`CHANGELOG.md`](CHANGELOG.md), [`SECURITY.md`](SECURITY.md), [`RELEASING.md`](RELEASING.md)

## Development

```
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
uv run pytest -m slow --no-cov    # slow checks, such as memory use with a million rejected lines
```

## Licence

Apache-2.0. See [`LICENSE`](LICENSE).
