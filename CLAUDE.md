# CLAUDE.md — Autonometry

Read this file fully at the start of every session. It overrides your defaults.

## What we are building

Autonometry measures how much of a robot's work is done by its AI and how much by humans, local or
remote. It records every change of control in a sealed, tamper-evident log, verifies logs, reconciles
a robot's log with a teleoperation station's log, and reports autonomy metrics with honest confidence
intervals. It is open source (Apache-2.0) and the reference implementation of an open standard.

Product rules that override everything else:
1. **Observer only.** Nothing we ship may sit in a robot's control loop, slow it or stop it. Every call
   from robot code into our client library returns at once and never raises.
2. **Fail visible.** Missing data becomes `unknown` time with bounds. It is never counted as AI.
3. **Fail closed.** A verifier that cannot prove a log intact says so. It never assumes.
4. **Deterministic.** The same log and parameters always give the same numbers and the same JSON
   report (apart from its generated-at time): seeded resampling, sorted output. No AI models and no
   network calls inside measurement or reporting.
5. **Privacy.** Control metadata only: no video, audio or real names. Actor ids are pseudonyms.

## Sources of truth

- `spec/<version>/definitions.md`: the normative rules.
- `spec/<version>/event.schema.json`: the event format (JSON Schema 2020-12).
- `spec/<version>/rules.toml`: machine-readable tables derived from definitions.md (state categories,
  precedence, parameters, flag codes, trust thresholds). Code reads these; never hard-code them.
- `research/`: design-phase material. Reference only: never import from it, never ship it. Its
  README lists known defects of the reference statistics package that must be fixed when porting.

If code and spec disagree, the spec wins. Never edit files under `spec/` unless the task says so.
If the spec is ambiguous or silent, stop and ask instead of inventing a rule; list open points at the
end of your reply.

## Stack

- Python >= 3.12. uv for environments, lockfile and builds (hatchling backend). ruff for lint and
  format, mypy --strict on `src/`, pytest with hypothesis and pytest-cov.
- Core dependencies (installed on robots, keep light): typer, pydantic, jsonschema, rfc8785,
  cryptography, rfc3161-client. Use the standard library for HTTP (`urllib.request`) and TOML (`tomllib`).
- Extra `report`: numpy, pandas, scipy, statsmodels, jinja2, matplotlib.
- Extra `lerobot`: pyarrow, watchfiles.
- Do not add a dependency without explaining why. Prefer a maintained package over custom code when it
  does the job in a few lines; prefer the standard library over a new package for small jobs. Avoid
  packages that look abandoned (no release for about 2 years, unanswered security reports), except ones
  that implement a frozen standard, such as rfc8785.
- Do not use lifelines (it blocks pandas 3). Kaplan-Meier: statsmodels `SurvfuncRight`. Censored
  Weibull and exponential fits: SciPy `stats.CensoredData`.

## Code rules

- Small, typed functions with clear names. Public functions have docstrings that state units
  (seconds, metres) and which spec section they implement.
- No placeholder code, TODO stubs, commented-out code, dead code or print debugging.
- Measurement logic is pure functions. Input and output happen only at the edges: CLI, recorder,
  adapters.
- Validate all external input (files, sockets, CLI arguments, configs) at the boundary with jsonschema
  or pydantic. Bad input produces a flag or a clear one-line error, never a crash or traceback for the
  user. Catch broad exceptions only at process boundaries (recorder loop, CLI entry point), log them,
  then keep running or exit non-zero.
- Time: UTC everywhere, timezone-aware datetimes, RFC 3339 strings ending in `Z`, durations as float
  seconds. Never compare floats for equality in logic. Statistics take explicit seeds.
- Logging: stdlib `logging`, one logger per module, never secrets or personal data.

## Security rules

- Never use `eval`, `exec`, `pickle`, unsafe YAML loading, `shell=True`, or subprocess calls with
  arguments that come from input.
- Logs are append-only: open with `O_APPEND`, flush, `os.fsync` according to the configured policy.
  Other files are written atomically (temporary file, then `os.replace`). Private files are created with
  mode 0600 and directories with 0700. Refuse to load a signing key that others can read.
- Paths taken from input are resolved and must stay inside the configured data directory.
- Network access only to URLs the user configured (timestamp authority, witness): TLS verification on,
  timeouts always, retries with backoff, and never on the recorder's write path.
- Limits on everything read from outside: maximum event size 64 KiB, maximum line length, maximum
  events per second per client, maximum file size before rotation.
- Never commit keys, tokens, real logs or personal data. `.gitignore` covers `*.key`, `*.pem` and data
  directories.
- Cryptography only through `cryptography` (Ed25519), `rfc8785` (canonical JSON), `hashlib` (SHA-256)
  and `rfc3161-client` (timestamps). Never write your own cryptographic primitive.

## Definition of done (every task)

- `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest` all pass.
- Coverage of `src/autonometry` stays at or above 90 %.
- Every spec rule the task touches has a test whose name cites the section, for example
  `test_human_pause_is_shared__defs_6_8`.
- Property tests (hypothesis) for parsers, the hash chain and the timeline: random or corrupted input
  never crashes; seal-then-verify always passes; any change to a sealed log fails verification.
- Golden tests: fixed input logs with expected outputs under `tests/golden/`.
- Slow statistical checks live in `tests/slow/` behind the `slow` marker, skipped by default.
- `CHANGELOG.md` (Keep a Changelog format) and the README are updated when behaviour changes.

## Workflow

- One milestone per branch and pull request. Small commits with conventional messages
  (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).
- Before coding, read the spec sections the task names and state your plan in a few lines.
- End every task with: what was built, how it was tested (commands and results), open questions, and
  anything skipped or deferred.

## Commands

```
uv sync --all-extras --dev     # set up
uv run pytest                  # fast tests
uv run pytest -m slow --no-cov  # statistical validation
uv run ruff check . && uv run ruff format .
uv run mypy src
uv run autonometry --help
```
