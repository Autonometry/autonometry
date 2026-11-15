# Recorder

`autonometry record --config FILE` runs on the robot's computer (Linux; macOS for development). It
receives control events from *connectors* (small adapters next to the robot's software) over a
local Unix datagram socket and writes a sealed, crash-safe log that `autonometry verify` accepts at
L2. It never sits in a control loop: connectors send fire-and-forget datagrams and carry on, and a
recorder that is down or slow only costs events, which the log then shows as `unknown` time.

What it writes, per robot and session: `<data_dir>/<robot_id>/<session_id>/events.jsonl` and
`events.checkpoints.jsonl` (folders 0700, files 0600), where `session_id` is a per-robot counter,
the UTC start time and a random suffix, such as `000042-20261112T083000Z-7f3a`. The next session
takes the largest counter on disk plus 1, and the newest session is the one with the largest
counter, never decided by time: a computer without a real-time clock (a Raspberry Pi, as in the
TurtleBot 4) can boot with an old time. Each session folder also gets private copies of the device
registry, task registry and plan the session used, as `devices.json`, `tasks.json` and `plan.json`,
so the folder is complete for an auditor; their hashes are in `session_start`, and `autonometry
timeline` checks the copies against them. The recorder alone writes `session_start`,
`session_end`, heartbeats, `seq` and `ts` (its receive time from the system UTC clock, never earlier
than the previous event's). `session_start` carries the config's scopes, weights, hint policy,
`simulated` and `unobserved`, the scopes' current states, and the SHA-256 of the device registry
(checked first), the task registry and the plan, each in RFC 8785 form; a registry or plan that is
not valid against its schema stops the start. Heartbeats every second carry the clock status: on Linux it is read from the kernel
with `adjtimex(2)` (unsynchronised gives `none`, else the configured `clock_source` and the kernel's
maximum error), elsewhere `unknown`.

**Level-triggered states.** A connector re-sends the state of each scope it observes on every
change and at least once a second; the recorder writes a transition only when the state, reason,
actor or device changes. A scope with no assertion for `gap_tolerance_s` (2 s) becomes `unknown`
(definitions.md 6.14). A scope belongs to the first connector that asserts it until that connector
stops asserting it for `gap_tolerance_s`; another connector's assertions meanwhile are dropped and
counted (`x_foreign_assertions` on the next heartbeat). Discrete events (`task_start`, `task_end`,
`hint`, `approval`, `help_request`, `supervision`, `support_start`, `support_end`) are written as
received; transitions and heartbeats carry the id of the open task.

**Loss is visible.** A gap in a connector's counter `n`, a datagram that is not a valid event, or
one over the rate limit (`max_events_per_s` per connector) makes every scope `unknown` at once,
because nothing says which scope it touched. Assertions restore the scopes only once a whole
heartbeat interval (1 s) has passed without another of these, so a flood writes at most one
`unknown` per scope per interval. A connector first seen with `n` above 0 kept running while the
recorder restarted: every scope is `unknown` too, but it is counted as `x_resumed_clients`, not as
lost events (the previous run mostly recorded them). The next heartbeat carries
`x_lost_client_events`, `x_rejected_client_events`, `x_rate_limited_events` and
`x_resumed_clients`; a step of the system clock backwards is clamped and reported as
`x_clock_stepped_back_s`.

**Durability.** Each event is one `write` on an `O_APPEND` descriptor. Events are fsynced at most
every `fsync_interval_s` and always before a checkpoint that covers them; the checkpoint file is
fsynced after every record. So a checkpoint, and any timestamp of it, never covers an event that a
power cut can lose. With `tsa_url` and `tsa_roots`, a background thread fetches an RFC 3161 token
for each new checkpoint; failures are retried with backoff and never block the writer.

**Crash recovery.** On start, if the newest session has no `session_end`, the recorder cuts a final
partial line (never complete, so no checkpoint covers it), replays the session verifying its hash
chain and checking its checkpoint file against the events, and continues it: a heartbeat with
`x_recovered: true` and `x_truncated_bytes`, every scope `unknown`, and a checkpoint. The downtime
is a gap and so `unknown` time. A session whose chain does not verify, or whose checkpoints
contradict its events (a checkpoint covering more events than the log holds, a different root, a
bad signature by the recorder's key), is left as it is and a new one starts: the recorder never
appends to a log that contradicts its own seals. A session whose config fields changed is closed
(`x_cause: "config_changed"`) and a new one starts. SIGTERM or SIGINT writes a final heartbeat,
`session_end`, the final checkpoint, waits up to 10 s for its token and exits 0. A log about to
exceed `max_log_bytes` ends its session (`x_cause: "rotation"`) and a new one starts with the
current states, followed by one transition per scope that is not `unknown` repeating its full value
(state, reason, actor, device); an open task carries over and the new `session_start` names it in
`x_carried_task`. Exit status: 0 after a clean stop, 1 on a failure while recording (a restart
continues the same session), 2 when it cannot start (config, key, registry, socket or folder in
use). Operational messages go to stderr and never contain event contents.

**It never fills the robot's disk.** Below `min_free_bytes` free on the data folder's disk
(default 1 GiB) the recorder stops writing and logs one error; datagrams are dropped unread, so
each connector's counter shows the loss. When space returns it writes a heartbeat with
`x_disk_full_s` (how long it stopped) and `unknown` on every scope: the time in between, with no
event and no heartbeat, is `unknown` like any lost data. It looks at the free space once a
second.

**Folders it trusts.** The recorder refuses to start (exit 2) when `<data_dir>/<robot_id>` belongs
to another user or its group or others can write it, when `data_dir` can be written by every user
without the sticky bit, or when the folder of `socket_path` can be written by every user (another
user could bind the path first and receive the connectors' datagrams). It creates the socket with
its final mode, never follows a symbolic link in place of a log file, and keeps running whatever a
datagram holds: one it cannot handle is counted in `x_rejected_client_events` and makes every
scope `unknown`. The threat model is in [`docs/threat-model.md`](threat-model.md).

## Install

On the robot (Python 3.12 or newer):

```
sudo useradd --system --home-dir /var/lib/autonometry --shell /usr/sbin/nologin autonometry
sudo python3 -m venv /opt/autonometry && sudo /opt/autonometry/bin/pip install autonometry
sudo install -d -m 0750 -o autonometry -g autonometry /etc/autonometry
sudo -u autonometry /opt/autonometry/bin/autonometry keys generate \
    --name autonometry/tb4_sim_01 --out /etc/autonometry/tb4_sim_01.key
/opt/autonometry/bin/autonometry registry check /etc/autonometry/devices.json
```

Add the connectors' users to the `autonometry` group so they can reach the socket, and give
auditors `tb4_sim_01.key.vkey` (never the key).

## A config for the TurtleBot 4 simulator

`/etc/autonometry/recorder.toml` (relative paths are relative to this file; unknown keys are an
error; spec parameters such as `gap_tolerance_s` come from the spec, never from here):

```toml
robot_id = "tb4_sim_01"
data_dir = "/var/lib/autonometry"
socket_path = "/run/autonometry/recorder.sock"
socket_group = "autonometry"          # socket 0660 for this group; without it, 0600
scopes = ["whole_robot"]              # the default; add scope_weights = { ... } for several
hint_policy = "hint_as_shared"        # the default
simulated = true                      # Gazebo: reports never pool it with real runs
unobserved = []                       # signal classes the connectors cannot see on the platform
clock_source = "ntp"                  # ntp, ptp or gnss: what keeps this computer's clock in sync
key_path = "tb4_sim_01.key"
device_registry = "devices.json"      # required: every source that can command the robot (6.10)
task_registry = "tasks.json"          # required: the tasks declared before the session (section 1)
plan = "plan.json"                    # required: pre-registered human actions, may be empty (3)
# tsa_url = "https://freetsa.org/tsr" # optional RFC 3161 anchoring, together with tsa_roots
# tsa_roots = "freetsa.pem"
# role = "station"                    # a teleoperation station's recorder, with station_id (12.1)
# station_id = "operator_pc_1"
# Defaults: fsync_interval_s = 1.0, max_events_per_s = 1000, max_log_bytes = 268435456,
#           min_free_bytes = 1073741824 (1 GiB left free on the data folder's disk; 0 turns it off)
```

## systemd

`/etc/systemd/system/autonometry-recorder.service`:

```ini
[Unit]
Description=Autonometry recorder
After=time-sync.target network-online.target
Wants=network-online.target

[Service]
User=autonometry
Group=autonometry
ExecStart=/opt/autonometry/bin/autonometry record --config /etc/autonometry/recorder.toml
Restart=always
RestartSec=2
# The final checkpoint waits up to 10 s for its timestamp token.
TimeoutStopSec=20
RuntimeDirectory=autonometry
RuntimeDirectoryMode=0750
StateDirectory=autonometry
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/autonometry
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=yes
LockPersonality=yes
# No ProtectClock= and no SystemCallFilter= without @clock: they block the read-only adjtimex(2)
# call that reports the clock status, and every heartbeat would then say "unknown".

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now autonometry-recorder`. For bursts of many small events, raise
`net.unix.max_dgram_qlen` (sysctl) so the socket queue holds more datagrams.

## Wire format

Connectors in any language can implement it: one UTF-8 JSON object per Unix datagram (`SOCK_DGRAM`)
sent to `socket_path`, at most 65536 bytes:

```json
{"client": "nav_connector", "n": 41, "event": {"type": "transition", "scope": "whole_robot",
 "state": "ai", "reason": null, "actor_id": "ai:nav2", "device_id": "nav2"}}
{"client": "nav_connector", "n": 42, "distance_m": 1234.5}
```

- `client`: the connector's name, an identifier (1 to 128 ASCII letters, digits, `.`, `_`, `:`,
  `-`). One name per connector process; a scope belongs to one connector at a time.
- `n`: counts every datagram the connector *tried* to send, from 0, including those it could not
  send; it restarts at 0 when the connector restarts. A gap tells the recorder what it lost.
- `event`: an event of `spec/v0.3/event.schema.json` without `robot_id`, `session_id`, `seq`, `ts`,
  `prev_hash` or `hash`, of type `transition` (a level-triggered assertion: send it on every change
  and at least once a second) or one of the discrete types above. `task_id` is optional: the
  recorder fills in the open task's id. Types `session_start` and `session_end`, and scopes the
  config does not declare, are rejected. A `heartbeat` may carry only `x_` fields that the
  recorder does not write itself (for example a connector's `x_publishers`): the recorder writes a
  heartbeat of its own at once, with its clock status and distance, and those fields added.
- `distance_m`: instead of `event`, an odometer reading in metres that never decreases while the
  connector runs; heartbeats carry the distance travelled since the session started. A reading
  that would make that distance overflow a 64-bit float is rejected.

Arrays and objects may nest at most 128 deep, in a datagram as in a log line: anything deeper is
rejected as `invalid_json`, the same way whatever reads it.

Send without blocking and never wait for an answer: there is none. For Python 3.8 or newer, copy
[`clients/python/autonometry_client.py`](../clients/python/autonometry_client.py) (standard library
only; every call returns at once and never raises):

```python
from autonometry_client import Recorder

rec = Recorder("/run/autonometry/recorder.sock", "nav_connector")
rec.state("whole_robot", "ai", actor_id="ai:nav2", device_id="nav2")  # on change and every second
rec.event("task_start", task_id="t17", task_definition_id="dock_v1")
rec.distance(odometer_metres)
rec.dropped  # events dropped: recorder down, socket full, or a value not serialisable
```

## Verify the logs

Copy the session folders off the robot and verify them with the vkey. Keep a copy of the
checkpoint files somewhere the robot cannot change (or configure a TSA) so that they anchor the log:

```
autonometry validate /var/lib/autonometry/tb4_sim_01/*/events.jsonl
autonometry verify /var/lib/autonometry/tb4_sim_01/*/events.jsonl --vkey tb4_sim_01.key.vkey \
    --anchor copy/000042-20261112T083000Z-7f3a/events.checkpoints.jsonl     # or --tsa-root freetsa.pem
```
