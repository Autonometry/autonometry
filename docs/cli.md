# Command line

`autonometry --help` lists the commands and `autonometry <command> --help` their options. Every
command validates its input at the boundary: a bad file or option gives one line on stderr, never
a traceback (add `-v` before the command for debug output and full errors). Commands that read
logs never print event contents such as actor ids unless the page of that command says so.

| Command | What it does | Runs on | Details |
|---|---|---|---|
| `demo --out DIR` | seals, verifies and reports a session shipped with the package; no robot, no network (needs the `report` extra) | all | [below](#the-demo) |
| `spec list`, `spec show VERSION` | the spec versions shipped in the package, and their files with SHA-256 hashes | all | [below](#checking-logs-and-registries) |
| `validate LOG...` | reads event logs line by line: rejected lines and structural flags | all | [below](#checking-logs-and-registries) |
| `registry check FILE` | checks a device registry, task registry or plan and prints its hash | all | [below](#checking-logs-and-registries) |
| `keys generate`, `seal`, `timestamp`, `verify` | signing keys, sealing, RFC 3161 anchoring, verification (L1, L2) | all (`timestamp` needs network) | [verification](verification.md) |
| `timeline LOG...` | each session's exact timeline: time per state, interventions, tasks, flags | all | [metrics and report](metrics-and-report.md#read-a-timeline) |
| `metrics LOG...` | the metrics of the standard with confidence intervals, as JSON (needs the `report` extra) | all | [metrics and report](metrics-and-report.md#compute-metrics) |
| `report LOG... --out DIR` | `report.json` and a self-contained `report.html` (needs the `report` extra) | all | [metrics and report](metrics-and-report.md#write-a-report) |
| `reconcile --robot LOG... --station LOG...` | compares a robot's log with a teleoperation station's log (L3) | all | [witness](witness.md) |
| `record --config FILE` | the recorder: receives control events from connectors and writes a sealed log | Linux, macOS | [recorder](recorder.md) |
| `lerobot watch`, `lerobot import` | LeRobot: follow a rollout log live, or import a LeRobotDataset (extra `lerobot`) | `watch`: Linux, macOS; `import`: all | [LeRobot](connectors/lerobot.md) |
| `g1 discover`, `g1 watch` | Unitree G1: list the robot's DDS participants, or watch who commands it (Cyclone DDS 0.10, Python 3.12) | Linux, macOS | [Unitree G1](connectors/unitree-g1.md) |

The TurtleBot 4 observer and the station observer are ROS 2 nodes, started with `ros2 launch`
rather than this command ([TurtleBot 4](connectors/turtlebot4.md), [witness](witness.md)).

Exit status, for every command: 0 when the result is what was asked for (a log with no rejected
line, a verified log, a clean report), 1 when the command ran but the result is not that (each
command's page says exactly when), 2 on bad usage, unreadable input or a missing extra.

## The demo

```
pip install "autonometry[report]"
autonometry demo --out autonometry-demo
```

`demo` runs the whole chain on a session shipped inside the package, so it needs no robot, no
network and no source checkout: one twelve-minute shift of a warehouse robot (synthetic data,
marked simulated) with six tasks, a remote operator taking over once, an automatic safety stop and
a connector crash that leaves 15 s unknown. Into a new or empty folder it writes:

- `key/demo.key` and `key/demo.key.vkey`: a throwaway Ed25519 key, generated for this run (folder
  0700, key 0600 on Linux and macOS); it signs only the demo;
- `logs/demo_amr_01/<session>/`: the sealed log, its signed checkpoints and the three registries
  the session declares;
- `verification.json`: what `autonometry verify --json --require L1` says. The session meets trust
  level L1 and not L2, because no checkpoint is anchored by a timestamp authority or an anchor
  file someone else keeps; the demo makes no network call, so it asks no timestamp authority;
- `report.json` and `report.html`: the report. Open `report.html` in a browser.

It prints what each file is and the commands that redo each step by hand. Everything is the same
every run, on every system, apart from the key, the signatures it makes and the report's
generated-at time. It
refuses a folder that is not empty, and without the `report` extra it prints one line and exits
2. Exit status 1 means the sealed session did not meet L1, which should never happen.

## Checking logs and registries

```
autonometry --version
autonometry spec list          # spec versions shipped with the package
autonometry spec show 0.3      # its files with SHA-256 hashes
autonometry validate robot.jsonl station.jsonl         # summary; exit 1 if any line is rejected
autonometry validate --json --spec 0.3 robot.jsonl     # the same as stable JSON
autonometry registry check devices.json                # device registry: valid? and its hash
autonometry registry check --kind tasks tasks.json     # the same for a task registry or a plan
```

`validate` reads each file line by line. A line that is not one valid event (bad UTF-8, longer than
64 KiB, not I-JSON, a lone surrogate or Unicode noncharacter, against the schema, an impossible
`ts`, or a `seq` already used in its session) is rejected with an error code and its line number.
Every rejected line is counted; the first 100 of each code are listed. Each session is then checked for structural
problems (missing `session_start` or `session_end`, gaps in `seq`, silences longer than the heartbeat
plus tolerance, `ts` going backwards, tasks that overlap or never close, and more), reported as
warning flags. The codes are listed in [`spec/v0.3/rules.toml`](../spec/v0.3/rules.toml). Event contents,
such as actor ids, are never printed; the robot and session ids it does print are restricted by
the schema to letters, digits and `._:-`, so a log cannot send control codes to your terminal. Add
`-v` before the command for debug output.

Every `session_start` says whether the session ran in a simulator (`simulated`), and heartbeats
report the recorder's clock (`clock_source`, `clock_max_error_s`). A session whose clock is not
reported as synchronised within `clock_tolerance_s` is flagged `clock_unsynced`.

`registry check` validates a device registry, the JSON file that declares every source that can
command the robot (people's input devices with their location, AI controllers, scripts, hoists;
definitions.md 6.10), and prints its SHA-256, which goes into `session_start.device_registry_hash`.
It exits 1 for an invalid registry, including a repeated `device_id` or a device that an earlier
one shadows (the first match wins), and never prints values from the file. `--kind tasks` checks a
task registry (`{"tasks": [{"task_definition_id": ...}]}`, section 1) and `--kind plan` a plan
of pre-registered human actions (`{"items": [{"plan_item_id": ..., "kind": "reset"}]}`, section 3)
the same way; their hashes go into `task_registry_hash` and `plan_hash`.
