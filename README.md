# Autonometry

Autonometry measures how much of a robot's work is done by its AI and how much by humans, local
or remote. When a company says its robot is autonomous, a buyer, insurer or auditor cannot easily
check how often a person stepped in. Autonometry records every change of control in a sealed,
tamper-evident log, verifies that log, compares it with the teleoperation station's own log, and
reports autonomy metrics with honest confidence intervals. Time it cannot see is reported as
unknown, never counted as AI.

It only observes: nothing it ships sits in a robot's control loop, and it cannot slow or stop a
robot. It is open source (Apache-2.0) and the reference implementation of an
[open specification](https://github.com/Autonometry/autonometry/tree/main/spec).

**Status: alpha (0.1.0).** The recorder, verifier, metrics and report work and are tested in CI;
the connectors are tested against simulated robots and recorded logs, not yet on real hardware.
The specification is a draft (v0.3) and may still change.

## Try it in 60 seconds

No robot, no network and no source checkout needed:

```
pip install "autonometry[report]"
autonometry demo --out autonometry-demo
```

The demo takes one twelve-minute shift of a warehouse robot shipped with the package (synthetic
data: six tasks, a remote operator taking over once, an automatic safety stop and a gap the
recorder could not see). It generates a throwaway signing key, seals the log, verifies it and
writes a report. It explains why the result is trust level L1 and not L2: there is no timestamp
authority, because the demo makes no network call. Then open `autonometry-demo/report.html` in
a browser. The demo prints what each file is and the commands that redo each step by hand.

## Install

Python 3.12 or newer:

```
pip install autonometry               # recorder, verifier, timeline
pip install "autonometry[report]"     # + metrics and the report (NumPy, SciPy, statsmodels, matplotlib)
pip install "autonometry[lerobot]"    # + the LeRobot connector
```

Live recording (the recorder, `autonometry lerobot watch`, `autonometry g1 watch`) runs on Linux
and macOS. Everything else (verify, timeline, metrics, report, reconcile, the demo, LeRobot dataset
import) also runs on Windows. Binary wheels of every dependency exist for Linux x86-64 and ARM64
(glibc or musl), macOS and Windows; on other platforms, such as 32-bit ARM, `jsonschema-rs` needs
a Rust toolchain to build. To work on the code, see
[CONTRIBUTING.md](https://github.com/Autonometry/autonometry/blob/main/CONTRIBUTING.md).

## How it works

1. A **connector** next to the robot's software watches who commands the robot (the AI stack, a
   person with a joystick, a remote operator, a script) and sends each change to the recorder. It
   never writes to the robot.
2. The **recorder** (`autonometry record`) writes a sealed, crash-safe log: every event
   hash-chained, signed Merkle checkpoints, optional RFC 3161 timestamps. Lost data becomes
   `unknown` time.
3. **`autonometry verify`** recomputes every hash and signature and says which trust level the log
   reaches. **`autonometry reconcile`** compares it with a teleoperation station's own log.
4. **`autonometry report`** turns the logs into the metrics of the standard (time autonomy share,
   task autonomy rate, interventions per active hour, human minutes per task, and more) with
   confidence intervals and bounds for unknown time, as `report.json` and a self-contained
   `report.html`.

## Connectors

| Robot or source | What it observes | Status |
|---|---|---|
| [TurtleBot 4](https://github.com/Autonometry/autonometry/blob/main/docs/connectors/turtlebot4.md) (ROS 2 Jazzy, Nav2) | Nav2 tasks, `/cmd_vel` and Nav2's relay chain, buttons, joystick, docking, safety stops, kidnap; a read-only ROS 2 node | Alpha: CI runs it in a `ros:jazzy` container against fake TurtleBot 4 nodes; a Gazebo demo exists but is untested; not yet run on a real robot |
| [LeRobot](https://github.com/Autonometry/autonometry/blob/main/docs/connectors/lerobot.md) (SO-101 and other LeRobot arms) | live, from the `lerobot-rollout` log (policy, DAgger pauses and corrections, resets); after the fact, from a LeRobotDataset's intervention labels | Alpha: log formats checked against the pinned LeRobot v0.6.1 source and logs captured on Windows; dataset import tested on a public DAgger dataset; not yet run beside a real arm |
| [Unitree G1](https://github.com/Autonometry/autonometry/blob/main/docs/connectors/unitree-g1.md) (EDU, 29 DoF) | who writes the robot's DDS command topics (built-in controller, handheld remote, teleoperation, policies, presets); subscribe-only | Alpha: CI runs five fake G1 programs over Cyclone DDS 0.10.5 on Linux and macOS; not yet run against a real G1 |
| [Teleoperation station](https://github.com/Autonometry/autonometry/blob/main/docs/witness.md) (witness for L3) | when the operator really gave input, logged under the station's own key; a ROS 2 node for the TurtleBot 4, or a few lines of Python for any teleop stack | Alpha: CI runs an honest TurtleBot 4 session with a station and requires L3 |

Any other robot can send events over the recorder's documented
[wire format](https://github.com/Autonometry/autonometry/blob/main/docs/recorder.md#wire-format),
with a one-file, standard-library Python client.

## Trust levels in plain words

- **L1, consistent.** Nothing in the log contradicts itself: in a sealed log every hash and
  signed checkpoint matches the events (an unsealed log can be L1 too). It shows no sign of
  editing, but whoever wrote it, or holds its signing key, could have rewritten all of it. The
  demo is L1.
- **L2, sealed and anchored.** Every event is covered by a signed checkpoint, and the checkpoints
  are anchored outside the operator's control: by an RFC 3161 timestamp authority or by an anchor
  file someone else keeps. Nothing was dropped, re-timed or edited after anchoring. A dishonest
  recorder can still log false events as they happen.
- **L3, witnessed.** A second, independent log at the teleoperation station agrees with the
  robot's about when a person was in control, within stated tolerances. A hidden remote operator
  using that station would show.
- **L4, hardware-rooted** (planned for a later spec version): keys in hardware and a witness of the
  control channel itself.

The full rules are in the
[specification](https://github.com/Autonometry/autonometry/blob/main/spec/v0.3/definitions.md)
(sections 10 and 12); what each level proves against whom is in the
[threat model](https://github.com/Autonometry/autonometry/blob/main/docs/threat-model.md).

## Documentation

- [Command line](https://github.com/Autonometry/autonometry/blob/main/docs/cli.md): every command,
  the demo, checking logs and registries
- [Recorder](https://github.com/Autonometry/autonometry/blob/main/docs/recorder.md): installing it
  on a robot, its config, systemd unit and wire format
- [Sealing and verification](https://github.com/Autonometry/autonometry/blob/main/docs/verification.md)
- [Timelines, metrics and the report](https://github.com/Autonometry/autonometry/blob/main/docs/metrics-and-report.md)
- [Station witness (L3)](https://github.com/Autonometry/autonometry/blob/main/docs/witness.md)
- Connectors: [TurtleBot 4](https://github.com/Autonometry/autonometry/blob/main/docs/connectors/turtlebot4.md),
  [LeRobot](https://github.com/Autonometry/autonometry/blob/main/docs/connectors/lerobot.md),
  [Unitree G1](https://github.com/Autonometry/autonometry/blob/main/docs/connectors/unitree-g1.md)
- [Specification](https://github.com/Autonometry/autonometry/tree/main/spec) (draft v0.3), shipped
  in the package as `autonometry/spec/`; changes are proposed in
  [spec-v0.4-proposals.md](https://github.com/Autonometry/autonometry/blob/main/docs/spec-v0.4-proposals.md)
- [Changelog](https://github.com/Autonometry/autonometry/blob/main/CHANGELOG.md),
  [releasing](https://github.com/Autonometry/autonometry/blob/main/RELEASING.md),
  [how to cite](https://github.com/Autonometry/autonometry/blob/main/CITATION.cff)

## Contributing, questions and security

Contributions are welcome: see
[CONTRIBUTING.md](https://github.com/Autonometry/autonometry/blob/main/CONTRIBUTING.md) and the
[code of conduct](https://github.com/Autonometry/autonometry/blob/main/CODE_OF_CONDUCT.md). Bugs
and ideas go in [issues](https://github.com/Autonometry/autonometry/issues). For general
questions, write to aaryan.solanki@thestatsigma.com.

Report vulnerabilities only through GitHub private vulnerability reporting, as
[SECURITY.md](https://github.com/Autonometry/autonometry/blob/main/SECURITY.md) says, never in a
public issue or by email. The pre-release
[security review](https://github.com/Autonometry/autonometry/blob/main/docs/security-review-2026-11.md)
lists its findings and what was fixed.

## Licence

Apache-2.0. See [LICENSE](https://github.com/Autonometry/autonometry/blob/main/LICENSE).
