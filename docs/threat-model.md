# Threat model

This document says what Autonometry protects, from whom, where the trust boundaries are, and what
each trust level proves. It covers the `autonometry` package (recorder, client library, connectors,
verifier, timeline, metrics, report, reconciler) and its release pipeline, as of spec v0.3. The
pre-release review that tested it is [`security-review-2026-11.md`](security-review-2026-11.md).

## System and data flow

```
 robot network            robot computer                                   auditor's computer
 (ROS 2 graph, DDS,  TB1  connector  TB2  recorder  TB3  <data_dir>/       TB5  verify, timeline,
  rollout log)      ───▶  + client  ───▶  (one       ───▶  <robot>/<session>/ ───▶  metrics, report,
                          library   Unix  process)       events.jsonl            reconcile
                                    socket   │ TB4       events.checkpoints.jsonl     │ TB6
                                             ▼                                         ▼
                                     RFC 3161 TSA (HTTPS)                     report.html / .json
                                                                             in a reader's browser

 operator (owns the robot computer and the recorder's key)  ── TB7 ──  auditor, buyer, insurer
 source repository ── CI (GitHub Actions) ── PyPI (Trusted Publishing) ── TB8 ── users' installs
 teleoperation station: its own recorder and key; its log is a second witness (L3)
```

A connector turns a robot's own signals into control states and sends them, fire and forget, as
datagrams to the recorder's Unix socket. The recorder alone stamps `ts` and `seq`, seals every
event into a hash chain, writes signed Merkle checkpoints (C2SP signed notes, Ed25519) and, if
configured, asks a timestamp authority (TSA) for an RFC 3161 token per checkpoint on a background
thread. An auditor verifies the files offline and turns them into metrics and a report.

## Assets

| Id | Asset | What must hold |
|---|---|---|
| A1 | Integrity of sealed logs | A change, deletion, reordering or insertion of a committed event is detected, and nothing is reported at L2 that was not proven (product rule 3: fail closed). |
| A2 | The robot's availability | Nothing in Autonometry sits in the control loop, slows or stops the robot (product rule 1). Client calls return at once and never raise; connectors only subscribe. |
| A3 | Privacy of logs | Control metadata only: no video, audio or real names; actor ids are pseudonyms (product rule 5). Reports carry counts, never actor ids. |
| A4 | Signing keys | The recorder's Ed25519 key signs checkpoints; whoever holds it can sign any log of that robot. |
| A5 | Release pipeline | What users install from PyPI is what the repository's tagged commit builds. |
| A6 | Honest numbers | Missing data becomes `unknown` time with bounds, never AI (product rule 2); the same inputs give the same report (rule 4). |

## Trust boundaries

| Id | Boundary | Untrusted side | Controls |
|---|---|---|---|
| TB1 | Robot network → connector | ROS 2 topics and graph, DDS samples and participant announcements, the LeRobot rollout log | Subscribe-only readers; every value range-checked; unknown or contradictory input becomes `unknown` or `x_attribution_inferred` (L1); bounded tables and strings. Names on the network are not identities (no DDS Security or SROS2 yet). |
| TB2 | Connector → recorder socket | Datagram content from any process that can write to the socket | Socket 0600 (or 0660 with `socket_group`) in a folder not every user can write; 64 KiB datagrams; strict I-JSON, nesting ≤ 128, schema check of every event; fields only the recorder writes are refused; per-client counter and rate limit; at most 256 clients; any rejection makes every scope `unknown`; one bad datagram never stops the recorder. |
| TB3 | Recorder → file system | Other local users on the recording computer | Folders 0700, files 0600 created with exact modes; robot folder must be the recorder user's and writable by nobody else; `data_dir` not world-writable unless sticky; logs opened `O_APPEND` and `O_NOFOLLOW`; other files written atomically; the key refused if its group or others can access it or another user owns it, checked on the opened file. |
| TB4 | Recorder → TSA | The network path and the TSA's response | Only the configured URL; TLS verified; 10 s socket timeout, 30 s deadline per attempt, 64 KiB response limit, no redirects, nonce checked, token verified against the configured roots before it is written; never on the write path. |
| TB5 | Input files → verifier and CLI | Logs, checkpoint and anchor files, registries, plans, configs, vkeys, TSA roots, LeRobot datasets, `--about` files, file names | Line length limit (64 KiB), strict UTF-8 and I-JSON, nesting ≤ 128, schema validation, every rejected line counted and failing verification; every hash and root recomputed, never trusted; sizes and counts bounded; durations saturate instead of overflowing; one-line errors, never a traceback; printed text escapes control characters. |
| TB6 | Report → browser | Every string that came from TB5 | One self-contained page, no scripts, no links; jinja2 autoescape; the only trusted markup is SVG drawn from numbers and fixed labels, with text as paths. |
| TB7 | Operator → auditor | The operator, who controls the recorder, its computer and its key | Only what the trust levels below prove. |
| TB8 | Repository → users | Dependencies, actions, the build, the publish step | Actions pinned by commit SHA; `contents: read` by default; no `pull_request_target`; no caches on tag builds; Trusted Publishing (no token) behind the `pypi` environment's reviewers, with attestations; `uv.lock` with hashes; pip-audit of the locked and the lowest sets; zizmor on every change. |

## Attackers

### Someone on the robot's network

Can publish any ROS 2 topic or DDS sample, announce participants with any names and properties,
and flood the graph. **Names on the robot network are claims, not proof**: a DDS writer that
announces a declared program's host and process name, or a ROS node started under a declared
node's name, looks like that program. Without authenticated transport (DDS Security, SROS2), L1
and L2 logs assume the robot's network is not hostile. What the connectors still guarantee: they
never write to the robot (rule 1); never crash or stall on any sample (bounded tables, range
checks, linear-time processing); resolve contradictions conservatively (a person or `unknown`,
never `ai`, where the rules leave a choice); treat two live writers or publishers claiming one
identity as an undeclared person, with a note; believe the Unitree G1's RPC responses only from
the declared built-in controller's identity and Nav2's task status and feedback only from declared
Nav2 nodes; make an open Nav2 task whose Nav2 falls silent or leaves the graph `unknown`; and let
an emergency stop end only when the robot confirms a change of mode. A remote operator hiding
behind a forged identity is what L3 exposes (below). They cannot reach the recorder: its socket is
local.

### A local user without root on the recording computer

Cannot read or write the logs, the key or the socket (0700/0600, socket 0600 or group 0660), cannot
plant links in the recorder's folders, and cannot squat the socket path: the recorder refuses a
robot folder others can write and a socket folder or `data_dir` every user can write. If the
socket's group includes them, they are a connector (next attacker but one). They can fill the disk
or exhaust memory like any local user; the recorder itself never fills the disk: below
`min_free_bytes` (default 1 GiB) free on the data folder's disk it stops writing, logs one error
and resumes when space returns, and the gap is `unknown` time.

### Anyone who supplies a log, registry, config, plan, dataset or report input

Typically an operator handing files to an auditor, or a dataset downloaded from the internet.
Every file is parsed under limits and validated; a malformed line is rejected and makes the result
not verified; no input makes a command crash, hang or run in super-linear time; nothing from the
input reaches the report as markup or the terminal as control codes. A forged or edited sealed log
fails verification unless re-signed with the robot's key (see the dishonest operator). A LeRobot
dataset is bounded before anything is written, and an import never leaves `--out` or replaces a
log.

### A misbehaving connector on the recorder socket

A buggy or compromised process that can write to the socket. It **can assert any state for a
scope it owns** (the first client to assert a scope owns it until silent for 2 s), including `ai`
while a person drives: the recorder records what connectors say. It cannot write fields only the
recorder writes (`seq`, `ts`, hashes, the recorder's counts), cannot hide loss (a counter gap,
rejection or rate-limit drop makes every scope `unknown`), cannot crash the recorder or write a
line the verifier would reject, and can never make the recorder block the robot. Flooding is
limited per client name (`max_events_per_s`) and to 256 names per run; a client that uses many
names multiplies the limit, and exhausting the 256 names makes later clients' events rejected,
which is visible as `unknown` time. Whatever it floods, the recorder stops writing before the disk
falls below `min_free_bytes`.

### A dishonest operator who controls the recorder

They own the robot computer, the recorder's key, the TSA account and the logs. They can run a
modified recorder or connector, and sign anything with the key. The trust levels prove this much
against them:

| Level | What it proves | What it does not prove |
|---|---|---|
| **L1** self-reported | Nothing about honesty. The log is well formed and the numbers follow the spec from it. | That any event happened, or that the log is complete. |
| **L2** sealed log | No event committed by a checkpoint was changed, removed, reordered or inserted afterwards, **provided** the checkpoint is anchored outside the operator's control: an RFC 3161 token from a TSA the auditor trusts, within `max_commit_delay_s` (1 h) of the events, or a copy of the checkpoint held by the customer or a witness. A token's time bounds when the record existed, so a rewrite later than 1 h after the events is detected. | That the record was true when written: a modified recorder or connector can seal false states from the start. A rewrite within `max_commit_delay_s` re-anchored with a fresh token. Completeness: a log cut back to an earlier anchored checkpoint verifies as that shorter log (definitions.md 10.6), unless an anchor kept outside the log covers the cut part. `verify` warns for every session whose `session_end` no anchored checkpoint covers, naming the time of its last anchor, and a report with such a session is not clean; the report's session census catches whole sessions only. An anchor file the operator supplies is only as independent as its keeper. |
| **L3** two witnesses | A remote operator who used the witnessed teleoperation station appears in the robot's log, to within tau (both clocks' reported errors plus `react_s`): the station's own sealed log under a separate key agrees with the robot's (definitions.md 12). | Control through anything the station does not witness: another station, a laptop, a direct radio link, a maintenance interface (only a network witness, L4, could). That the station's logger is honest: the two keys must belong to separate software identities, and the station logger should be installed by the customer or auditor. |

L2 and L3 therefore protect the record against later tampering and against a hidden remote
operator; they do not make a dishonest operator's original record true. Independent anchors (a
TSA the auditor chooses, checkpoint copies sent to the customer as they are made) and an
independently installed station logger are what give the levels their strength.

**Anchor often during a session, and keep anchor files off the robot.** The recorder writes a
checkpoint after the first event, every 1000 events, every 600 s and after the last event
(definitions.md 10.3), and with `tsa_url` it timestamps each one: everything before the last
anchor is then fixed, and only the time since can be cut without a trace in the session's own
files. An anchor file kept where the operator cannot change it, such as checkpoint copies the
auditor or customer receives as they are made, turns a later cut into `missing_events`. Requiring
an anchored `session_end` for `verified` is proposed for spec v0.4
([`spec-v0.4-proposals.md`](spec-v0.4-proposals.md)).

### The supply chain

Dependencies are few and locked with hashes (`uv.lock`); the audit job fails on any known advisory
except two documented ones that cannot reach our code (lowest versions only). Workflows pin every
action by commit SHA, read the repository with `contents: read`, never run untrusted code with
secrets (`pull_request_target` is not used; checkout does not keep credentials), and the release
job publishes with PyPI Trusted Publishing behind a required-reviewer environment, with attestations.
Repository settings that complete this (branch and tag protection, the `pypi` environment's
reviewers, private vulnerability reporting, secret scanning) are owner actions listed in the
review.

## Assumptions and out of scope

- The recording computer's kernel and root account are trusted; an attacker with root owns the
  key and the recorder (SECURITY.md: out of scope).
- The auditor obtains the verifier key (vkey) and TSA roots through a channel the operator cannot
  alter, and keeps their own copies of checkpoints when they can.
- The robot's own software and its safety functions are out of scope; Autonometry only observes.
- Denial of service against the robot's network or computer by means unrelated to Autonometry.
