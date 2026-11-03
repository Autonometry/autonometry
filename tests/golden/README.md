# Golden logs

Fixed input logs with their expected `autonometry validate --json` output.

| Folder | Content |
|---|---|
| `errors/` | One log per error code of `spec/v0.2/rules.toml` `[errors]`: a clean session plus lines rejected with that code only. |
| `flags/` | One log per loader flag checked when a log is read: it raises that flag only. |
| `valid/` | Logs that give no error and no flag: several robots and sessions interleaved, and CRLF, BOM and blank lines. |

The `.jsonl` files are byte-exact (`.gitattributes` marks them `-text`), because some test CRLF
endings, a byte order mark and invalid UTF-8. Each `X.jsonl` has `X.expected.json` beside it.

After an intended change in output, regenerate an expected file from inside its folder, then review
the diff:

```
cd tests/golden/flags
uv run autonometry validate --json time_gap.jsonl > time_gap.expected.json
```
