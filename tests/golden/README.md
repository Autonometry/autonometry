# Golden logs

Fixed input logs with their expected `autonometry validate --json` output.

| Folder | Content |
|---|---|
| `errors/` | One log per error code of `spec/v0.3/rules.toml` `[errors]`: a clean session plus lines rejected with that code only. |
| `flags/` | One log per loader flag checked when a log is read: it raises that flag only. |
| `valid/` | Logs that give no error and no flag: several robots and sessions interleaved, and CRLF, BOM and blank lines. |
| `registry/` | Device registries (`.json`, definitions.md 6.10) with their expected `autonometry registry check --json` output: TurtleBot 4 and Unitree G1 examples, and one file per kind of problem. |
| `timeline/` | One folder per scenario of `autonometry timeline` (two humans, a six-scope humanoid, hints, safety stops, a preset, resets, unknown gaps, an undeclared device, a registry mismatch): `events.jsonl`, the registries it names (`devices.json`, `tasks.json`, `plan.json`) and `expected.json`, written by hand from the spec before the code ran. |
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
