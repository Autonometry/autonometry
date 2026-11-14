# Golden logs

Fixed input logs with their expected `autonometry validate --json` output.

| Folder | Content |
|---|---|
| `errors/` | One log per error code of `spec/v0.3/rules.toml` `[errors]`: a clean session plus lines rejected with that code only. |
| `flags/` | One log per loader flag checked when a log is read: it raises that flag only. |
| `valid/` | Logs that give no error and no flag: several robots and sessions interleaved, and CRLF, BOM and blank lines. |
| `registry/` | Device registries (`.json`, definitions.md 6.10) with their expected `autonometry registry check --json` output: TurtleBot 4 and Unitree G1 examples, and one file per kind of problem. |
| `timeline/` | One folder per scenario of `autonometry timeline` (two humans, a six-scope humanoid, hints, safety stops, a preset, resets, unknown gaps, an undeclared device, a registry mismatch): `events.jsonl`, the registries it names (`devices.json`, `tasks.json`, `plan.json`) and `expected.json`, written by hand from the spec before the code ran. |
| `metrics/` | A small multi-session set for `autonometry metrics` (`logs/`: two robots, real and simulated sessions, tasks, approvals, a hint, unknown time, support, distance, a safety stop, a two-scope takeover and an unplanned reset, with the registries) and its `expected.json` (`--n-boot 200`, seed 0). Point estimates and bounds are worked out by hand in `test_golden_metrics_by_hand__defs_7`; intervals come from the estimators tested in `test_stats_intervals.py`. Floats are compared with a relative tolerance of 1e-4. |
| `report/` | The two golden reports of `autonometry report` (definitions.md 11): `unsealed.expected.json`, from the `metrics/logs` set as it is (trust level L1, unverified), and `sealed.expected.json`, from `report/sealed/`: the same logs sealed with the fixed-seed test key of `tests/sealing_helpers.py`, their checkpoint files, an anchor file holding every checkpoint, the vkey and copies of the registries (L2, verified). Both use `--n-boot 200`, seed 0 and `generated_at` fixed to `2026-11-13T12:00:00Z` by the test. `test_sealed_golden_files_come_from_the_seed_key` proves the sealed files are what that key gives. Floats are compared with a relative tolerance of 1e-4. |
| `lerobot/` | The LeRobot connector's goldens: `rollout.log`, a DAgger rollout log run through the watcher's engine and the recorder, and the tiny dataset `test_connector_lerobot_golden.py` builds with pyarrow; `expected.json` and its README hold the states, tasks and episodes worked out by hand. |
| `sealed/` | A small raw log, sealed at test time with a fixed-seed test key; its own README covers it and the real RFC 3161 fixture under `tests/fixtures/tsa/`. |

The `.jsonl` files are byte-exact (`.gitattributes` marks them `-text`), because some test CRLF
endings, a byte order mark and invalid UTF-8. Each `X.jsonl` has `X.expected.json` beside it.

After an intended change in output, regenerate an expected file from inside its folder, then review
the diff:

```
cd tests/golden/flags
uv run autonometry validate --json time_gap.jsonl > time_gap.expected.json
```

A timeline expected file is evidence that the code follows the spec, so it is never just
regenerated: after an intended change, work out every changed number from the spec by hand, then
regenerate it from inside its folder and compare
(`uv run autonometry timeline --json events.jsonl > expected.json`).

The metrics expected file is regenerated after an intended change from inside `metrics/logs`, after
checking the hand-worked numbers in `tests/test_metrics_cli.py`:

```
cd tests/golden/metrics/logs
uv run autonometry metrics s1.jsonl s2.jsonl s3.jsonl s4.jsonl --n-boot 200 --out ../expected.json
```

The report set is regenerated, sealed files and both expected reports, after an intended change
(including any edit to a spec file, whose hash the report carries), then the diff is reviewed:

```
uv run python tests/report_helpers.py
```
