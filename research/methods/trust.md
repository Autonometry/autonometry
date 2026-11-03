# Trust: tamper-evident logging and the outside witness for Autonomy Meter

Retrieval date for all sources: 2026-10-30. Source ids (S01..S25, T01..T26) refer to `trust_sources.csv`. Tags: VERIFIED = primary text read by this review; CLAIM = abstract or bibliographic record only; UNKNOWN = not verifiable from retrieved material. Design statements (how Autonomy Meter should work) carry no tag; they are proposals, not facts.

## 1. Threat model

The quantity being protected is the control-state timeline: a sequence of events `(t, state, reason)` with `state in {ai, human_local, human_remote, shared, safety_stop, idle, unknown}` from which time autonomy share, intervention rate and human-minutes-per-task are computed. Three parties matter.

1. The reporter (robot vendor or operator) produces the log and benefits from a high autonomy share.
2. The verifier (customer, auditor, regulator) consumes the report and may hold a copy of the log.
3. The teleoperation station (possibly run by the reporter, possibly by a contractor) is the second place where human control is observable.

Attacks fall in two classes. Post hoc attacks alter the record after the fact: deleting human_remote segments, re-timing events, replacing whole sessions. Real-time attacks make the record wrong at write time: a logger that labels a remotely driven interval `ai`, a clock set wrong on purpose, an operator path the logger does not see. Tamper-evident logging closes the first class; only independent witnesses close the second. This distinction is the one Schneier and Kelsey drew in 1999: a forward-secure log guarantees that entries written before the machine is compromised cannot be undetectably modified or destroyed, and says nothing about entries written by a compromised (or dishonest) logger afterward (S06, CLAIM, https://doi.org/10.1145/317087.317089).

## 2. Primitives reviewed

### 2.1 Hash chains and forward-secure logs

The classic construction stores `h_i = H(h_{i-1} || e_i)` with the per-entry authentication key evolved one-way after each entry, so an attacker who captures the machine learns nothing useful about earlier keys (S06, CLAIM). Ma and Tsudik replaced the trusted third party in such schemes with forward-secure sequential aggregate signatures, giving space-efficient publicly verifiable logs with no online trusted server or secure hardware (S10, CLAIM, https://doi.org/10.1145/1502777.1502779). The limitation of a bare chain is proof cost: Crosby and Wallach note that proving one event is present in an 80-million-event hash-chain log can require an 800 MB trace, whereas their tree returns a 3 KB proof (S07, VERIFIED, https://www.usenix.org/legacy/event/sec09/tech/full_papers/crosby.pdf).

### 2.2 Merkle trees, history trees and the audit semantics

Merkle hash trees originate in Merkle's 1987 signature construction (S08, CLAIM, https://doi.org/10.1007/3-540-48184-2_32), and linked timestamping of documents in Haber and Stornetta (S09, CLAIM, https://doi.org/10.1007/bf00196791). Crosby and Wallach define tamper-evident logging operationally: an untrusted logger serves clients and is kept honest by auditors who challenge it; the logger must prove that individual events are still present and that the log as seen now is consistent with how it was seen in the past; a secure system must both detect tampering within a signed snapshot and detect when different snapshots make inconsistent claims (S07, VERIFIED). Their history tree produces logarithmic-size membership and consistency proofs and ran at 1,750 events/s on one core with per-commitment signatures, 10,500 events/s with signatures offloaded (S07, VERIFIED). Robot control-state logs are orders of magnitude smaller than that (a state transition every few seconds at most), so cost is not a constraint.

### 2.3 Certificate Transparency: the deployed template

RFC 6962 specifies publicly auditable, append-only, untrusted logs; the logs do not prevent misissuance but let interested parties detect it; a log must incorporate an entry within a Maximum Merge Delay and periodically appends new entries to the tree and signs the tree head (S02, VERIFIED, https://www.rfc-editor.org/rfc/rfc6962.txt). RFC 9162 (CT 2.0) obsoletes 6962 and defines Merkle inclusion proofs and consistency proofs (S03, VERIFIED, https://www.rfc-editor.org/rfc/rfc9162.txt). The two proof types map directly onto Autonomy Meter needs: an inclusion proof shows a specific event is in the committed log; a consistency proof shows a later checkpoint extends an earlier one without rewriting it. The Go module checksum database applies the same structure outside certificates: clients verify a record is in the log and that the server never removes records, auditors iterate the log, and clients and auditors compare observations so the server is not trusted to store the log or to show everyone the same view (S18, VERIFIED, https://raw.githubusercontent.com/golang/proposal/master/design/25530-sumdb.md). Sigstore's Rekor provides an immutable tamper-resistant ledger of signed metadata with inclusion proofs and integrity verification; Rekor v1 is in maintenance mode and v2 is tile-based (S13, VERIFIED, https://raw.githubusercontent.com/sigstore/rekor/main/README.md; S12, CLAIM, https://doi.org/10.1145/3548606.3560596). Trillian, the general-purpose log server behind many deployments, is in maintenance mode with Tessera and tiled APIs recommended for new logs (S19, VERIFIED, https://raw.githubusercontent.com/google/trillian/master/README.md).

### 2.4 Signed checkpoints and witness cosigning

The C2SP checkpoint format is a signed note whose body carries exactly three items: log origin, tree size, root hash (S15, VERIFIED, https://raw.githubusercontent.com/C2SP/C2SP/main/tlog-checkpoint.md). A witness verifies that a log evolves append-only and countersigns checkpoints that extend previously witnessed ones; clients use countersignatures to ensure they are not shown a different view than others (split-view attack) (S14, VERIFIED, https://raw.githubusercontent.com/transparency-dev/witness/main/README.md). A cosignature is a statement that the cosigner verified consistency of a tree hash, and a quorum of cosignatures lets clients rule out split views before trusting an inclusion proof (S16, VERIFIED, https://raw.githubusercontent.com/C2SP/C2SP/main/tlog-cosignature.md; protocol S17, VERIFIED). The motivation is documented: most transparency designs rely on gossip to detect split views, but gossip protocols have "next to no deployment in the wild" (S21, VERIFIED, quoting the paper's own characterisation of prior work, https://arxiv.org/abs/2011.04551); witness cosigning as in CoSi has a statement validated and publicly logged by a diverse witness group before clients accept it (S20, CLAIM, https://doi.org/10.1109/sp.2016.38).

### 2.5 Trusted timestamps

RFC 3161 defines the request/response format for a Time Stamping Authority and requires the TSA to use a trustworthy source of time, include a trustworthy time value and a unique integer in each token; the request's messageImprint should contain the hash of the datum, and a client nonce supports replay detection (S01, VERIFIED, https://www.rfc-editor.org/rfc/rfc3161.txt). A timestamp token over a checkpoint hash establishes that the checkpoint (and therefore every event under it) existed no later than the TSA time. It does not establish that the events' own `t` fields are correct; that is the clock problem handled in section 4.

### 2.6 Hardware roots of trust and remote attestation

The TCG TPM 2.0 Library Specification is current at Version 185 (March 2026) with Errata 1.7 (Oct 2026), in four parts (Architecture, Structures, Commands) (S22, VERIFIED for existence and version; spec body not read, so any statement about specific commands is CLAIM). RFC 9334 defines the RATS roles: an Attester (typically a device) produces Evidence, a set of Claims that may include configuration data, measurements or telemetry; a Verifier appraises Evidence against policy and emits Attestation Results for a Relying Party; freshness is provided by nonces, epoch IDs, or synchronized clocks (S04, VERIFIED, https://www.rfc-editor.org/rfc/rfc9334.txt). Surveys of bootstrapping trust in commodity computers (S23, CLAIM, https://doi.org/10.1109/sp.2010.32) and of attestation principles (S24, CLAIM, https://doi.org/10.1007/s10207-011-0124-7) frame what attestation can and cannot show: it binds a key and a software measurement to a platform; it does not vouch for inputs to that software.

## 3. Level L-A: single sealed log

Design.

1. Canonical event encoding: each event is serialised deterministically (fixed key order, UTF-8, no insignificant whitespace) before hashing. The exact canonicalisation is specified in `event.schema.json` (sibling deliverable).
2. Chain: `h_0 = H("autonomy-meter/v0.1" || robot_id || session_id)`, `h_i = H(h_{i-1} || enc(e_i))`. Every event record stores `h_{i-1}` so a verifier can recompute.
3. Tree: events are also leaves of a Merkle tree built per RFC 9162 section 2.1 (S03, VERIFIED), so proofs of inclusion for a single disputed interval are logarithmic (S07, VERIFIED).
4. Checkpoint: at every session start and end, every N = 1,000 events, and at least every 10 minutes while active, the logger emits a C2SP-format checkpoint (origin = `robot_id/log_id`, size, root hash) signed with the logger key (S15, VERIFIED). The checkpoint body also carries, as an extension line, the current clock-sync status (section 4.2).
5. Timestamp: each checkpoint hash is submitted to an RFC 3161 TSA and the token is stored next to the checkpoint (S01, VERIFIED). A missing or late token (TSA time minus logger time greater than the tolerance in section 4) marks the interval as `clock_unverified`.
6. Publication (optional but recommended): checkpoints are appended to a transparency log operated by the reporter or a third party (Rekor-style, S13, VERIFIED) and may be cosigned by witnesses (S16, VERIFIED). This is what makes split views (one log shown to the customer, another to the regulator) detectable.

What it exposes. Any post hoc deletion, insertion, re-ordering or re-timing of events after a checkpoint has been timestamped (S06 CLAIM, S07 VERIFIED). Dropped segments appear as chain discontinuities or as gaps between consecutive checkpoints; the gap duration is reported as `unknown` time and enters the bounds (gaps counted as human versus as AI) required in Part 3 of the brief.

What it does not expose. A logger that writes `ai` while a human drives. A key held in software can be copied, so a second "clean" log can be fabricated from scratch and signed; only the TSA tokens then constrain it (a fabricated log cannot carry tokens older than its fabrication). Therefore L-A is the floor for the post hoc class only.

## 4. Level L-B: two witnesses reconciled

### 4.1 Witnesses

Witness A is the robot log (L-A). Witness B is the teleoperation station log: its own L-A chain, under a distinct key, recording `session_connect`, `session_disconnect`, `control_grant`, `control_release`, `input_active` (operator input observed in the last 1 s window), `hint_sent`, `approval_sent`. For L3 the two keys must be held by distinct software identities; for independence the station logger should be installable by the customer or auditor rather than shipped only inside the vendor's teleop client.

Cross-stamping. Every station checkpoint embeds the latest robot checkpoint hash it has seen, and vice versa, carried in the control channel. This is the pairwise analogue of witness cosigning (S14, S16, VERIFIED): it fixes a happens-before relation between the two logs that does not depend on either clock, so a party cannot later insert a station event "before" a robot checkpoint it had already acknowledged.

### 4.2 Clock-drift tolerance

Both witnesses record, at every checkpoint, their time source and its error estimate: for NTP, the local offset and root dispersion from the NTP daemon; for PTP, the offset from master. RFC 5905 states that NTPv4 servers and clients are precise within a few tens of milliseconds with poll intervals up to 36 hours, and that improved algorithms extend potential accuracy to tens of microseconds on fast LANs; accuracy degrades with stratum, network path and clock stability (S05, VERIFIED, https://www.rfc-editor.org/rfc/rfc5905.txt). IEEE 1588 defines a precision clock synchronisation protocol for LAN measurement and control systems (S25, CLAIM, record only).

Tolerance rule. The matching tolerance for one boundary is

    tau = (eps_A + eps_B) + d_react

where `eps_X` is witness X's self-reported synchronisation error at the nearest checkpoint (root dispersion for NTP; if absent, 0.1 s is assumed for Internet NTP and the interval is flagged) and `d_react` is the maximum emission latency of a control-state transition in the logger (declared by the implementer, default 0.5 s). Defaults: `tau = 1.0 s` for two Internet-synchronised witnesses, `tau = 0.1 s` when both report PTP or LAN NTP with dispersion below 10 ms. Because a teleop episode is seconds to minutes long, a 1 s boundary tolerance costs at most a few percent of a short episode and is negligible for the time-share metric; the sensitive quantity is the count of short episodes, which is why section 4.3 reports disputed time separately rather than silently absorbing it.

Sanity bound. If an implementer's self-reported `eps` is implausibly small (below 1 ms without PTP) the verifier replaces it with the RFC 5905 Internet figure (tens of ms; 0.05 s is used) and flags the log.

### 4.3 Reconciliation algorithm

Input: robot intervals `R = {[s_i, e_i]}` with state `human_remote` or `shared`; station intervals `B = {[s_j, e_j]}` where `control_grant..control_release` and `input_active` are true; session ids on both sides.

1. Offset estimate. For each session id present on both sides, compute `d_k = t_robot(session_connect) - t_station(session_connect)`. Set `delta = median(d_k)` and `spread = MAD(d_k)`. If `spread > tau`, the pair fails L-B for that reporting period (clocks inconsistent beyond declared error) and the period is reported at L2.
2. Shift. Replace every station time by `t + delta`.
3. Classify every instant of active time into one of four cells: both-human (in R and in B), neither, robot-only (in R, not in B), station-only (in B, not in R). Boundary regions within `tau` of any interval edge are excluded from the disputed cells.
4. Disputed time `D = robot-only + station-only` beyond tolerance. Station-only time is the critical cell: a human was issuing input that the robot log did not attribute to a human. Robot-only time is benign for anti-gaming (the robot over-reports human time) but still signals a logger fault.
5. Report. The human-remote share is reported as an interval: lower bound uses both-human only; upper bound adds all disputed time to human. The report must state `D / active_time`. L3 conformance requires `station-only / active_time <= 0.5 %` and `D / active_time <= 2 %`; otherwise the period is downgraded to L2 with the discrepancy published.
6. Episode matching for counts. Interventions per active hour are recomputed from matched episodes: a robot episode and a station episode match if both boundaries agree within `tau` after shift; unmatched station episodes are added to the count (upper bound), unmatched robot episodes are kept (they are already in the robot's count).

The algorithm is deterministic and uses only sorted interval sweeps, so a reference implementation in pandas is straightforward (the sibling `reference/` package should expose `reconcile(robot_events, station_events, tau)`).

What L-B exposes beyond L-A: real-time mislabelling of remote control as `ai` or `idle`, provided the operator's input passes through the instrumented station; hidden operators using an uninstrumented client are exposed only if the control channel itself is witnessed (section 6).

## 5. Level L-C: hardware-rooted keys

Design. Logger signing keys are generated inside a TPM 2.0 or equivalent secure element and are non-exportable (S22, CLAIM for the non-exportability mechanics; the spec body was not read). At each checkpoint the robot acts as RATS Attester and produces Evidence: a signed platform measurement (boot chain plus logger binary hash) with the checkpoint hash supplied as the freshness nonce (S04, VERIFIED). The auditor runs or contracts a Verifier whose appraisal policy lists accepted logger binaries; Attestation Results are stored with the checkpoint. The same applies to the teleop station at L4.

What it exposes beyond L-B: key cloning (fabricating a parallel log that validates under the same identity), silent replacement of the logger software with one that mislabels, and replay of old Evidence (freshness). What it does not expose: a control path that bypasses the attested software entirely, for example a human driving through a maintenance interface that the logger never observes. Attestation certifies the software, not the world.

## 6. Optional add-on: the outside network witness

Question posed in the brief: can live teleoperation be detected from encrypted traffic patterns on the customer's own network?

### 6.1 What the literature supports

Encryption hides payloads but not packet sizes, timing and direction, and the protocol designers say so: RFC 9000 states that QUIC header protection "does not prevent other properties of packets, such as timing and size, from being used to correlate activity" (T01, VERIFIED, https://www.rfc-editor.org/rfc/rfc9000.txt). Real-time media rides on RTP (T03, VERIFIED) or WebRTC/SRTP, whose packets have a characteristic header (T02, VERIFIED, https://www.rfc-editor.org/rfc/rfc8826.txt).

Flow-type and application identification from these side channels is mature. Website fingerprinting reaches 98.3 % closed-world accuracy on Tor and 0.99 precision / 0.94 recall in an open world of 20,000 unmonitored sites (T05, VERIFIED, https://arxiv.org/abs/1801.02265), above 96 % for 100 sites and 94 % for 900 (T06, VERIFIED). Encrypted video streams are identified from burst patterns with near-zero false positives (YouTube detector: 0 false positives at 0.988 recall; Netflix: FPR 0.0005 at 0.93 recall) (T09, VERIFIED, https://www.usenix.org/system/files/conference/usenixsecurity17/sec17-schuster.pdf; T10, CLAIM). Encrypted VoIP leaks spoken phrases through packet lengths (T11, CLAIM). IoT devices are identified from traffic characteristics at over 99 % accuracy (T14, CLAIM; T15, CLAIM). The closest analogues to teleoperation are the following.

1. Activity inference from rates: a passive observer infers in-home activity from send/receive rates of encrypted IoT devices; a camera's traffic rate is orders of magnitude higher while its live feed is being viewed than in motion-detection mode, so an adversary can tell when the feed is actively watched (T16, VERIFIED, https://arxiv.org/abs/1812.00955). A robot whose uplink video is being watched by an operator is the same signal.
2. Cloud gaming, the closest bidirectional low-latency control analogue: Stadia and GeForce Now use RTP/WebRTC at up to 45 Mbit/s (T17, VERIFIED), with a common temporal traffic-generation pattern shared with other WebRTC applications (T18, VERIFIED), and a decision tree on flow features detects cloud-gaming among other high-bitrate UDP applications at 98.5 % accuracy (T19, CLAIM, https://doi.org/10.1109/noms56928.2023.10154417).
3. Video conferencing, the bidirectional live-media analogue: 0.8 to 1.9 Mbit/s average utilisation on an unconstrained link (T12, VERIFIED), and proprietary encrypted streams can be grouped into meetings and measured from traces (T13, CLAIM).
4. Remote desktop: activity detection from encrypted RDP traffic has been studied (T24, CLAIM, title only). Haptic teleoperation command traffic has been characterised over 5G (T25, CLAIM) and bilateral teleoperation is defined by its ultra-low-latency requirement (T26, CLAIM).

Limits are also documented. Open-world methods do not scale to Internet-scale universes (T04, CLAIM); real-traffic QUIC service classification over one month reached only 88 % with measurable degradation under drift (T23, CLAIM); and VPN or tunnel multiplexing offers protection that is "inconsistent and difficult to quantify" because individual devices keep sparse, distinctive patterns (T16, VERIFIED). Conversely, padding defences are weak against coarse features: nine countermeasures including those standardised in TLS, SSH and IPsec were defeated using total time and bandwidth alone (T07, CLAIM), and adaptive padding (WTF-PAD) reduced a deep-learning attack only to 90 % closed-world accuracy (T05, VERIFIED; T08, CLAIM).

No retrieved primary source measures detection of robot teleoperation sessions from a customer-side vantage point. That specific accuracy is UNKNOWN in the literature as retrieved.

### 6.2 Extrapolated accuracy estimate (CLAIM, this review's extrapolation)

The witness task is a binary, per-time-window decision ("is a live bidirectional control session to this robot active now?") with a small, known set of flows (the robot's own addresses), which is strictly easier than the many-class open-world problems above. The discriminating features are: a persistent uplink media flow at hundreds of kbit/s to several Mbit/s with video-like burst cadence (T09, T12, T17 analogues), concurrent with a downlink flow of small packets at a stable command cadence (tens of Hz) whose inter-arrival variance drops when a human is driving (T18, T25 analogues), and uplink rate that steps up when the feed is viewed (T16). From the 98 to 99 % figures for cloud-gaming and IoT flow typing and the 88 % figure under real drift, this review estimates, for a witness calibrated on the vendor's own stack at the deployment site:

1. Session-level detection of continuous teleoperation lasting 60 s or more: recall about 0.90 to 0.95, false-positive rate about 0.02 to 0.05 per minute of AI-only operation against the hardest negative (video monitoring with no control). Tag: CLAIM.
2. Minute-level time attribution: 10 to 20 % relative error on human_remote share once a session is detected. Tag: CLAIM.
3. Sparse interventions (single approval clicks, text hints, sub-second nudges): near chance. The command-channel signature is absent and a single small packet is indistinguishable from telemetry acknowledgements. Tag: CLAIM.

Direction of error. With per-window recall `r` and false-positive rate `f`, the witness's estimated human share is `r*s + f*(1-s)`. For true share `s = 0.10`, `r = 0.95`, `f = 0.03` the estimate is 0.122 (bias +0.022); for `s = 0.02` it is 0.048 (bias +0.028); for `s = 0.30` the bias is +0.006 (computed in this review). The witness therefore overstates human time at low true shares. That is the safe direction for anti-gaming (it cannot be used to inflate autonomy) but it means the witness is a cross-check on the two logs, not a replacement for them; if `r` and `f` are estimated on a calibration set the standard Rogan-Gladen inversion `(est - f)/(r - f)` recovers `s`.

### 6.3 Failure cases

1. VPN or tunnel multiplexing: when the robot's control, telemetry and unrelated traffic share one encrypted tunnel, per-flow features collapse into aggregate rate; protection is inconsistent but the witness's precision drops to whatever aggregate rate changes reveal (T16, VERIFIED for the inconsistency; magnitude for robots UNKNOWN).
2. Store-and-forward or batched hints: a human reviews a clip and sends a plan or waypoint list minutes later. No live bidirectional flow exists; the witness sees nothing. Only the log (with `reason = human_initiated`, `state = human_remote` or a `hint` event) can capture it.
3. Low-rate approval clicks: one packet per decision is invisible at flow level; see 6.2 item 3.
4. Co-located operator on the LAN or via a direct radio link: traffic never crosses the customer's monitored uplink. A LAN tap or switch-port mirror is required, and a direct joystick or radio link bypasses IP entirely.
5. Padding and shaping: constant-bitrate shaping of both directions removes rate and cadence features; literature shows coarse features survive many defences (T07, CLAIM; T05, VERIFIED) but a vendor who controls both endpoints can pad perfectly at bandwidth cost. Padding is itself detectable (a constant-rate downlink to a robot is unusual) and should be declared.
6. Operator traffic routed through the vendor cloud: if the robot always maintains a media and command channel to the vendor cloud (for monitoring, data collection, or AI inference offload) and the operator attaches on the cloud side, the customer-side flow may look identical whether a human is attached or not. Only a step-up in uplink rate or a change in downlink cadence (T16 analogue) is visible; if the vendor streams at constant quality regardless, the witness fails. This is the single most likely failure mode for a vendor-hosted teleop fleet.
7. Shared or assistive autonomy where the human sends a low-rate goal and the AI closes the loop: the downlink cadence stays AI-like; the witness classifies as AI.
8. Multiple robots behind one NAT: flows are attributable to the site, not to a robot, unless the witness is inside the NAT.

## 7. Mapping to the standard's trust levels

| Standard level | Content | Minimum machinery (this document) | Closes |
|---|---|---|---|
| L1 | Self-reported, unsigned event log and report | None; schema conformance only | Nothing; honest-reporter assumption |
| L2 | Sealed log | L-A: hash chain + Merkle checkpoints (C2SP format) + RFC 3161 timestamps; publication to a transparency log recommended | Post hoc editing, dropping, re-timing after checkpoint; gaps become bounded `unknown` time |
| L3 | Two witnesses | L-B: robot log and teleop-station log, each L-A, distinct keys, cross-stamped, reconciled with `tau` rule and disputed-time report | Real-time mislabelling of remote control passing through the instrumented station; clock manipulation beyond declared sync error |
| L4 | Hardware-rooted plus external witness | L-C on both witnesses (TPM/secure-element keys, RATS attestation of logger binary) plus at least one of: network witness (section 6) run by the customer or auditor, or independent witness cosigning of all checkpoints | Key cloning, logger replacement, split views; network witness adds an independent lower bound on live teleoperation time |

Each level requires the ones below it. Downgrade rules: an L3 period with disputed time above threshold is published at L2 with the discrepancy; an L2 period with a chain break or missing timestamp is published at L1 for the affected span.

## 8. Recommendation: what V1 ships

V1 ships L-A as mandatory and the L-B station logger and reconciler as optional software, and defers hardware roots and the network witness to an experimental annex.

Reasons.

1. L-A is pure software built from standardised, deployed primitives (RFC 9162 trees, C2SP checkpoints, RFC 3161 tokens; S01, S03, S15 VERIFIED), is cheap at robot event rates (S07 VERIFIED), and closes the entire post hoc class of attacks in `gaming.md`. Nothing in the metric layer (Part 3) is meaningful unless the record it is computed from is fixed; this is the minimum credible floor.
2. The L-B reconciler should ship in V1 even though many reporters will not run a station logger at first, because publishing the algorithm and `tau` rule pre-registers how disputes will be adjudicated and lets customers demand it contractually. The disputed-time report is also the only metric in the suite that directly measures the hidden-operator problem.
3. Hardware attestation is deferred, not rejected: the primitives are standardised (S04 VERIFIED; S22 VERIFIED for existence) but fleet heterogeneity (embedded controllers without TPMs, vendor-hosted teleop stations) makes a mandatory requirement unrealistic in a v0.1 standard, and the attack it closes (key cloning) presupposes a vendor already committing fraud at L3.
4. The network witness is shipped as advisory only. The literature supports feasibility by analogy (T09, T16, T17, T18, T19) but no primary measurement of teleoperation detection exists (UNKNOWN), the extrapolated error rates in 6.2 are untested, and failure case 6.3.6 (vendor-cloud routing) is the common deployment pattern. A V1 reporter may include witness-derived human_remote lower bounds as an additional, clearly labelled row; the standard should define the calibration protocol (labelled sessions, reported `r` and `f`) before any conformance weight is attached.

Open items carried to `open_questions.md`: canonicalisation choice for events; whether checkpoint publication should be mandatory at L2; the `tau` defaults, to be calibrated against real NTP dispersion on robot fleets; a controlled study of teleoperation traffic detection with public traces.
