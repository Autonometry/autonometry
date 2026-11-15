# LeRobot

`autonometry.connectors.lerobot` measures robots run by Hugging Face
[LeRobot](https://github.com/huggingface/lerobot), such as the SO-101 arm, in two ways: **live**,
from the log `lerobot-rollout` writes as it runs, and **after the fact**, from a
LeRobotDataset's per-frame `intervention` labels. Both follow LeRobot v0.6.1 (commit
`7e241bd6`); the log formats and dataset column names are copied from that source, and the code
cites the file and line of each. Neither path opens the robot's serial port or imports anything
into the control process, so neither can slow or stop the arm. Install the extra:
`pip install 'autonometry[lerobot]'` (pyarrow, watchfiles, psutil).

**Sources.** LeRobot has no ROS graph. The connector matches the components the rollout names
against the device registry's `match.node`: `policy:<--policy.path>` for the policy,
`teleop:<--teleop.type>` for the teleoperator, and `script:return_to_initial_position` for
LeRobot's own move back to the start pose. A source no device matches is sent without
`device_id`, and the loader counts it as `human_remote` (6.10). Example registries for an SO-101
(the leader arm local, a phone or network teleoperator remote, the policy, the return motion) and
a recorder config are in [`connectors/lerobot/`](../../connectors/lerobot/).

**Live: `autonometry lerobot watch`.** Start the recorder, then the watcher, then the rollout
with its output in a file. `PYTHONUNBUFFERED=1` matters: the re-record key's line is a `print`,
which Python otherwise holds in its buffer while the output goes to a pipe or a file. Every other
signal is a logged line and counts only with LeRobot's logging prefix (level, time, location); a
line without the prefix counts only when it is, exactly and whole, the re-record key's printed
message, so a `print()` elsewhere in the rollout's process cannot pass for a signal. The messages
are checked against the pinned LeRobot source (`tests/fixtures/lerobot_source`).

```
autonometry record --config connectors/lerobot/recorder.toml &
autonometry lerobot watch --config connectors/lerobot/recorder.toml --log rollout.log \
    --task "Pick up the cube and place it in the bin." &
PYTHONUNBUFFERED=1 lerobot-rollout --strategy.type=dagger --strategy.record_autonomous=true \
    ... 2>&1 | tee rollout.log
```

The watcher, like the recorder, runs on Linux and macOS. It reads the recorder's config for its
socket, scopes and registries. It follows the log like `tail -F`: an existing log is read from
where it ended when the watcher started, unless you pass `--from-start`, and a truncated or
replaced log is read from its start. It sends the state every 0.25 s. `--task` is the rollout's
`--dataset.single_task`, matched against the task registry's `description` (or a
`task_definition_id`); it can be omitted when the registry declares one task.

**The rollout's process.** The watcher also watches the rollout's process, read-only (psutil; it
never signals or touches it), about once a second. A rollout killed without a word (kill -9,
out of memory, a native crash, its window closed) then becomes `unknown` instead of keeping its
last state. The watcher reads what is left in the log first, so the rollout's last lines still
count. Then, unless the rollout logged `Rollout finished`, it does what that line does: it closes
the open task with outcome `unknown`, makes every scope `unknown`, notes
`x_lerobot_process_exit: unlogged` on a heartbeat and logs a warning. The process is the one
`--pid` names. Without `--pid`, the watcher looks for it when it sees a rollout start, or joins
one mid-run: the process on this computer whose program is `lerobot-rollout`, its `.exe`, or a
Python running that script or `-m lerobot.scripts.lerobot_rollout`. A launcher and the Python it
starts (`uv run`, the Windows `.exe` launcher) count as one rollout, and the deepest is watched.
With none or several, the watcher warns once, notes `x_lerobot_liveness: unwatched` on a
heartbeat, and the log alone decides. A log written on another computer cannot be watched this
way. A log that vanishes, is replaced by another file or is cut shorter while a rollout is followed
(log rotation, a new `> rollout.log`) is treated the same way: the open task closes with outcome
`unknown`, every scope is `unknown` and a heartbeat notes `x_lerobot_log: lost`, until a line read
afterwards tells the state again. A line read after the rollout's process is known gone (a buffer
flushed after a kill) is ignored unless a new rollout starts.

**`--from-start`.** A rollout read from the part of the log that existed before the watcher
started may have ended long ago. Once that whole part is read, the watcher looks for its
process; if none can be found (or `--pid` names none), the rollout is taken as gone: the open task
closes with outcome `unknown`, every scope is `unknown` and a heartbeat notes
`x_lerobot_process_exit: not_found`, instead of keeping the state the log last showed. If a line
from that rollout is appended later, it was running after all: the watcher follows it again,
looks for its process again, and keeps the policy and teleoperator it learned from the earlier
part of the log; the state is `unknown` until a line tells it.

**Logs written on Windows.** These commands, tested with Windows PowerShell 5.1 and cmd on
Windows 11, give a log the watcher reads in full:

```
# PowerShell (5.1 or 7)
$env:PYTHONUNBUFFERED = "1"
lerobot-rollout ... 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath rollout.log

:: cmd
set PYTHONUNBUFFERED=1
lerobot-rollout ... > rollout.log 2>&1
```

PowerShell 5.1 writes the file as UTF-16 with a byte-order mark, and Python writes a redirected
stream in the Windows code page. The watcher honours a byte-order mark and accepts LeRobot's dash
in any code page. Without `ForEach-Object { "$_" }`, PowerShell 5.1 turns each line of stderr
into an error record and wraps the long ones at the console width. That cuts the line that names
the policy. The captures are in `tests/fixtures/lerobot_windows/`. The recorder, and so the
watcher, does not run on Windows yet. PowerShell 7 was not tested.

Rules (dossier 14.1 under spec v0.3; the first that applies decides):

| Observed in the log | State or event |
|---|---|
| nothing that tells the state yet | no assertion: the recorder logs `unknown` |
| `Rollout finished`, or the rollout's process gone | `unknown`: nothing is observed any more |
| the rollout started (`Building rollout context...`), its loop not yet | `idle` |
| DAgger `AUTONOMOUS`, and the policy loops of `base`, `sentry`, `highlight` and `episodic` | `ai`, actor `ai:<device_id>` when the registry declares the policy, else `ai:<pseudonym>`: the first 12 hex digits of the SHA-256 of the policy path, never the path, which can hold a user name (`python3 -c "import hashlib; print(hashlib.sha256(b'PATH').hexdigest()[:12])"` traces it) |
| DAgger `PAUSED` (`Phase transition: autonomous -> paused`) | `shared`, `human_initiated`: a human pause (6.8) |
| DAgger `CORRECTING` | `human_local`, or `human_remote` when the teleoperator's device is declared remote (6.10) |
| episodic `Reset the environment` with a teleoperator | `human_local` or `human_remote`, `planned` with the plan's `reset` item (6.3) |
| `Returning robot to initial position`, or an episodic reset without a teleoperator | `scripted`, actor `script:return_to_initial_position` (6.9, 6.13) |
| a control loop ended, the robot disconnected | the last controller's state, then `idle` after `idle_threshold_s` (6.14) |

Episodes are tasks (5): DAgger with `record_autonomous=true` and sentry open one with the control
loop and at each `Episode saved`; episodic opens one at `Recording episode N` and closes it at
`Reset the environment`. The outcome is `unknown` (LeRobot records no success), or `aborted` when
the re-record key was pressed during the episode. A re-record seen only after the episode closed
is noted as `x_lerobot_rerecorded_task` on a heartbeat. DAgger's corrections-only episodes are
clips of corrections, not units of work, so they open no task; declare `unobserved = ["tasks",
...]` for such runs. The log's own local time (1 s resolution, zone not recorded) travels as
`x_src_ts` with `x_src_clock` `wall`; `ts` is the recorder's. When the watcher stops it sends
`unknown`. When a rollout is unwatched and dies without a word, its last state stays until the
watcher stops, so stop the watcher, or the recorder, when such a rollout ends.

**After the fact: `autonometry lerobot import`.**

```
autonometry lerobot import DATASET_DIR --out logs --registry connectors/lerobot \
    --policy outputs/train/act_so101/checkpoints/last/pretrained_model --teleop so101_leader \
    [--robot-id so101_01] [--start 2026-10-19T06:10:24Z] [--simulated]
autonometry report logs/*/*/events.jsonl --out report
```

It reads a LeRobotDataset v3.0 recorded by DAgger (`meta/info.json`, `meta/episodes/`,
`data/chunk-*/file-*.parquet`) and writes one log per episode, `logs/<robot_id>/<session_id>/
events.jsonl`, with the registries copied beside it. Each episode is its own session, since the
time between episodes was never observed. The log holds `task_start` at frame 0, a transition at
each change of `intervention` (true: the teleoperator's state; false: `ai`), `unknown` for
missing frames or labels, and `task_end` with outcome `unknown`. Timestamps are rebuilt as an
anchor plus `frame_index / fps`: `--start` for the first episode, with episodes back to back, or
by default each data file's modification time as the end of its last episode. Every event
carries `x_ts_reconstructed: true`, so the logs are trust level L1 at most (8, 10.6). The
sessions declare `unobserved: ["safety_stops", "human_inputs"]`: the SO-101 has no safety
function, and pauses and resets are never recorded. Episodes listed in the metadata whose data
file is missing are named and skipped. A task text not in the task registry gets an id
`lerobot_task_<hash>`, and its tasks are flagged `unknown_task_definition`. A dataset is untrusted
input: an episode longer than 24 hours (its declared length or last frame over the frame rate), a
parquet file of more than 10 million rows, or an index above 2^53 − 1 stops the import with one
line, checked from the metadata before anything is written; an import never replaces a session log
already in `--out`, and a robot id that is no folder name on the system (`C:` or `NUL` on Windows)
is refused.

**Demo.** [`demo/lerobot/run_demo.py`](../../demo/lerobot/README.md) downloads the metadata and first
data files of a public DAgger dataset from the Hugging Face Hub, using the standard library only.
It prints the size first and stops above 100 MB. Then it imports and reports: a time autonomy
share of 0.779, 95 % CI [0.668, 0.891] over 18 episodes, and 20 interventions, as in the dossier.

**Deferred.** Bus taps on real hardware: a UART receiver on the leader and follower buses for
leader polling, torque writes and follower commands (integration notes part B). Capture of
asynchronous inference (`policy_server` over gRPC on port 8080). `lerobot-replay`, which would be
`scripted` replay of recorded actions. The Foxglove WebSocket tap. HIL-SERL's `is_intervention`,
which no dataset keeps. `lerobot-record` teleoperation logs.
