# Rollout logs captured on Windows

Captured on Windows 11 (console code page 437, ANSI 1252) with Windows PowerShell 5.1 and cmd.
`fake_rollout.py` writes LeRobot v0.6.1's log lines to stderr in `init_logging`'s format
(lerobot/utils/utils.py:62-73), plus the re-record key line as a print on stdout. PowerShell 7 was
not installed. The files are byte-exact (`.gitattributes`: `-text`).

| File | Command | What the shell did |
|---|---|---|
| `ps51_tee.log` | `python fake_rollout.py 2>&1 \| Tee-Object -FilePath ps51_tee.log` | UTF-16 LE with a byte-order mark; `python.exe : ` before the first line, then PowerShell's error record; long lines wrapped at the console width (the policy path is cut) |
| `ps51_foreach.log` | `$env:PYTHONUNBUFFERED='1'; $env:PYTHONIOENCODING='utf-8'; python fake_rollout.py 2>&1 \| ForEach-Object { "$_" } \| Tee-Object -FilePath ps51_foreach.log` | UTF-16 LE with a byte-order mark; no prefix, no wrapping; the em dash, UTF-8 read as code page 437, arrives as three characters |
| `cmd_redirect.log` | `python fake_rollout.py > cmd_redirect.log 2>&1` | code page 1252: the em dash is byte 0x97, not UTF-8; the printed line comes last, held in Python's buffer |
| `cmd_utf8.log` | `set PYTHONUNBUFFERED=1` and `set PYTHONIOENCODING=utf-8`, then the same | UTF-8, every line in order |
