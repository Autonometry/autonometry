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
- `autonometry.reader`: streaming reader for one or many log files. Lines are read as bytes with a length limit (a longer line is skipped without being held in memory), decoded as UTF-8 (a BOM only at the start of a file; LF and CRLF), parsed as I-JSON (RFC 7493: no duplicate keys, no NaN or Infinity, integers within ±(2^53−1), no lone surrogates or Unicode noncharacters), validated against the event schema (integer fields must be written as integers, not `5.0`), and their `ts` checked for impossible dates. Valid events are grouped by `(robot_id, session_id)` and ordered by `seq`; a repeated `seq` is rejected. Every rejected line is counted, and the first `list_max_per_code` of each code are kept, so memory grows with the events kept, never with the size or number of rejected lines.
- `autonometry.checks`: per-session loader flags `no_session_start`, `no_session_end`, `missing_initial_state`, `undeclared_scope`, `seq_gap`, `ts_regression`, `time_gap`, `nested_task`, `task_end_without_start`, `task_not_closed`, `distance_regression`, `ts_reconstructed`, `repeated_session_marker`, `event_outside_session`.
- `autonometry.events`: immutable, JSON-serialisable results (`LogCheck`, `Session`, `LineError`, `Flag`, `Counts`) and an `Event` TypedDict.
- `autonometry.strictjson` (I-JSON parsing) and `autonometry.timestamps` (RFC 3339 UTC timestamps as integer nanoseconds).
- Spec v0.3: `rules.toml` `[errors]` table (error codes for rejected lines, `max_line_bytes = 65536`, `list_max_per_code = 100`) and the **Rejected lines** paragraph in `definitions.md` section 8. `Rules` loads and checks the new table.
- Spec v0.3, stricter log format: `ts` needs 3 to 9 fraction digits and a leap second is invalid (section 3); identifiers and scope names have a fixed character set and length (`$defs/id`, `$defs/scope`, `propertyNames` on scope maps; section 8, **Identifiers**); the **JSON** paragraph of section 8 (I-JSON, integer fields written as integers); flags `repeated_session_marker` and `event_outside_session` (section 1); error code `invalid_unicode`.
- Command line option `--verbose` (`-v`): debug output and full tracebacks.
- Dependency `jsonschema-rs`: compiled Draft 2020-12 validator, about 70 times faster than `jsonschema` on the event schema, with ECMA-262 regular expressions as JSON Schema requires. It also checks the schema itself against the meta-schema, so `jsonschema` is no longer a core dependency; it stays in the dev group as the reference in a differential test, and `types-jsonschema` is dropped.
- CI: test jobs on Windows and macOS (Python 3.12), and a dependency audit of both the locked and the lowest-version sets.
- `.gitattributes`: LF line endings in the repository on every OS, so spec file hashes match everywhere.
- Spec v0.3 section 10, "Sealing and verification": hash chain, Merkle checkpoints (RFC 9162), C2SP signed notes, RFC 3161 anchors, and the trust-level L1/L2 verification rules, with evidence, gap and file codes (`malformed_checkpoint`, `orphan_timestamp`: records that cannot be tied to one session, which like rejected lines make a result not verified). Anchors are checked against the events exactly like checkpoints, and a valid anchor counts as an anchored checkpoint. `rules.toml` `[sealing]` table (`checkpoint_every_events`, `checkpoint_every_s`, `max_commit_delay_s`, `clock_tolerance_s`, `evidence_codes`, `gap_codes`, `file_codes`); `Rules.sealing` and a consistency check that flag, error, evidence, gap and file codes are each unique and never shared. New `spec/v0.3/checkpoint.schema.json` for checkpoint and timestamp records.
- `autonometry.merkle`: RFC 9162 / RFC 6962 leaf and node hashing, and an `IncrementalTree` that computes a Merkle tree hash one leaf at a time in O(log n) space.
- `autonometry.sealing`: the event hash and chain genesis of definitions.md section 8, and an incremental `Sealer` that seals a session's events in order and decides when to checkpoint (section 10.3); rebuildable after a crash by replaying already-sealed events.
- `autonometry.signednote`: C2SP signed notes and checkpoints (Ed25519 log signatures only) -- key IDs, vkey parsing and formatting, signing, fail-closed verification (an untrusted key is ignored; a trusted key's bad signature rejects the note), and checkpoint body encoding/decoding.
- `autonometry.keys`: Ed25519 signing keys as unencrypted PKCS8 PEM, generated without overwriting and without ever leaving a partial file; private key 0600, vkey 0644; folders it creates are 0700 and existing folders are never changed; loading refuses a key that is group/other-accessible or owned by another user (POSIX; skipped with a warning on Windows).
- `autonometry.tsa`: an RFC 3161 timestamp client (the only network call in the package outside a witness, never on a recorder's write path) with a 10 s socket timeout, a 30 s total deadline per attempt, a 64 KiB response limit, no redirects, a nonce check against replays and 3 attempts with 1 s/2 s backoff; a pure `check_imprint` (decodes the token and checks its SHA-256 message imprint) and `verify_token` (checks it against trusted TSA roots at the token's own `gen_time`).
- `autonometry.sealfile`: reading and writing `<name>.checkpoints.jsonl` and anchor files under the same line rules as event logs, checked against `checkpoint.schema.json`; each record keeps its file and line, and rejected lines are listed per code like the event reader's.
- `autonometry.fileio`: one atomic-write helper (temporary file, fsync, rename, fsync of the folder on POSIX) for keys, sealed logs and checkpoint files, all created 0600, and a durable append for timestamp records. Both open files in binary mode, so Windows no longer turns `
` into `
` in appended records.
- `autonometry.verify`: `verify_log`, the definitions.md section 10.6 verifier. It recomputes every event's hash from its content, sealed or not, so a root exists at every size, and runs every signed checkpoint, from checkpoint and anchor files alike, through one check: a size beyond the events present is `missing_events` (including a session absent from the log), a differing root `root_mismatch`. Chain rules apply only to sessions with a sealed event. Each session gets `verified` / `partial` / `unverified` / `failed` with its evidence and gaps (per code, up to five details and a count); the result also lists rejected lines of every file read and file problems, and says whether it is verified at L1 or L2 and why not.
- Command line: `autonometry keys generate --name NAME --out PATH`; `autonometry seal IN.jsonl --key PATH --out OUT.jsonl`; `autonometry timestamp FILE.checkpoints.jsonl --tsa-url URL --tsa-root PEM`; `autonometry verify LOG.jsonl... [--vkey FILE]... [--tsa-root PEM]... [--anchor FILE]... [--require L1|L2] [--json]`. The text output ends with `result: verified` or `result: NOT verified: <reasons>` and lists rejected lines and file problems grouped by code; the JSON output carries `verified`, `reasons`, every listed rejected line and `file_problems`.
- CI: a job (Ubuntu 24.04, Python 3.12) runs the slow checks, `uv run pytest -m slow --no-cov`.
- Golden fixtures under `tests/golden/sealed/` and a real RFC 3161 response recorded once from FreeTSA under `tests/fixtures/tsa/`, covering the sealing attacks of `research/demo-prototypes/seal.py`.
- Spec v0.3, the rules for the cases the TurtleBot 4 and Unitree G1 research found uncovered (definitions.md "Changes in v0.3"): a `safety_stop` a person triggered and a `scripted` preset a person started are interventions; `actor_id` on `safety_stop` names who triggered it; damping modes are `safety_stop` only when a safety function or an emergency-stop control caused them; pre-recorded references run by a controller are `scripted`; the device registry declares every commanding source; new sections 6.11 to 6.15 (continuous goal input, several sources at once, presets started by a person, hand-overs and stale input, physical support); parameters `goal_release_s` and `contention_window_s`. The timeline rules themselves come with the metrics (M4).
- Event schema v0.3: `support_start` and `support_end` events (optional `scope`, `device_id`); `session_start.unobserved` (signal classes the recorder cannot see); heartbeat `clock_source` (`ntp`, `ptp`, `gnss`, `none`, `unknown`) and `clock_max_error_s`.
- `spec/v0.3/device_registry.schema.json` and `autonometry registry check FILE [--json] [--spec VERSION]`: validates a device registry (I-JSON, schema, no repeated `device_id`, no `shadowed_device` whose `match` holds every field of an earlier device's `match` with the same values, since the first match wins; at most 1 MiB) and prints the SHA-256 of its RFC 8785 form, the value of `session_start.device_registry_hash`. Exit status 0 valid, 1 invalid or unreadable, 2 bad usage. Values from the file are never printed. `autonometry.spec.load_registry_schema`.
- Loader flags `clock_unsynced` (a heartbeat reports `clock_source` `none` or `unknown`, reports none, or reports `clock_max_error_s` above `clock_tolerance_s` or not at all; or no heartbeat reports the clock) and `attribution_inferred` (events with `x_attribution_inferred: true`), each once per session with a count.
- `rules.toml`: `[params]` `goal_release_s`, `contention_window_s`; `[interventions]` `person_started_states`, checked to name known states.

### Changed

- The spec draft v0.2 became v0.3 in place (`spec/v0.3/`, schema `$id`s, `spec_version = "0.3"`); nothing had been released, so no v0.2 is kept.
- `session_start` must carry `simulated` (boolean); a `session_start` without it is rejected with `schema_violation`. Reports must not pool simulated and real sessions.
- `verify` caps a session holding events with `x_attribution_inferred: true` at trust level L1 with gap code `reconstructed`, as for `x_ts_reconstructed`.
- The golden sealed log moved to 2026-11-12 timestamps and its RFC 3161 fixture was recorded again, because its notes changed with `simulated`.
- Minimum dependency versions lowered to the oldest releases that provide the features we use and carry no known advisory for them (`uv.lock` keeps exact pins). The tooling run by `uv run pytest` now also excludes `research/` from mypy and from `pytest .`.
- Slow statistical tests run with `uv run pytest -m slow --no-cov`.
- Logging is configured once by the console entry point: warnings and errors print as one clean line without exception text; an unexpected crash prints only `error: unexpected failure (<type>)` unless `--verbose` is given.
- `Rules.states` is a read-only mapping, so loaded rules can be shared and cached.
- Project URLs, `SECURITY.md` and `RELEASING.md` use the exact GitHub owner name `Autonometry` (PyPI Trusted Publishing compares it); release tags are annotated (`git tag -a`).

### Security

- `validate` output cannot carry terminal control codes from a log: robot and session ids, the only event values it prints, must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`, so a line with escape codes in them is rejected (`schema_violation`) and never echoed.

### Removed

- The `License :: OSI Approved` classifier: the SPDX `license` field replaces it (PEP 639).
