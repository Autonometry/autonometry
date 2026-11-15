# Sealing and verification

```
autonometry keys generate --name autonometry/<robot_id> --out robot.key   # writes robot.key, robot.key.vkey
autonometry seal robot.jsonl --key robot.key --out robot.sealed.jsonl     # + robot.sealed.checkpoints.jsonl
autonometry timestamp robot.sealed.checkpoints.jsonl \
    --tsa-url https://freetsa.org/tsr --tsa-root freetsa.pem             # optional, needs network
autonometry verify robot.sealed.jsonl --vkey robot.key.vkey --json       # trust level per session
```

`seal` hash-chains every event (definitions.md section 8) and emits signed Merkle checkpoints
(RFC 9162, C2SP's `signed-note` and `tlog-checkpoint` formats, Ed25519) as it goes (section 10.3),
refusing input that has rejected lines, is already sealed, or would overwrite an existing output.
`timestamp` anchors a checkpoint with a real RFC 3161 token from a Time Stamping Authority; an
anchor file -- a copy of a checkpoint kept outside the log, by the customer or a witness -- does
the same without a TSA (`verify --anchor FILE`). `verify` recomputes every event's hash from its
content and every Merkle root from the events, and never trusts a stored value; every checkpoint
and anchor is checked against them, so an anchor also catches events or whole sessions deleted
later, together with their checkpoints. Each session is reported `verified`, `partial`,
`unverified` or `failed`, with evidence codes (any one means the log contradicts its own seals:
tampering or corruption) and gap codes (never raise the trust level). Records that cannot be tied to
one session (a malformed checkpoint, a timestamp for no checkpoint) are file problems. The output
ends with `result: verified` or `result: NOT verified: <reasons>`; rejected lines of any file read
and file problems make the result not verified whatever the sessions' statuses. Only L2 is
"verified": with `--require L1` the line says whether L1 was met and, separately, whether the log
is verified (`result: meets L1; NOT verified at L2: <reasons>`), and the JSON carries
`meets_required_level` beside `verified`, which is true only at L2. The codes are in
`spec/v0.3/rules.toml` `[sealing]`.

**Anchor often, and keep anchors off the robot.** A session cut back to an earlier anchored
checkpoint, its later checkpoints and tokens removed, verifies as the shorter log
(definitions.md 10.6). So `verify` warns for every session whose `session_end` no anchored
checkpoint covers -- "events after the last anchor (<its time>) may have been removed" -- and a
report with such a session is not clean. Anchor during a session, not only at its end (the
recorder timestamps every checkpoint it writes when `tsa_url` is set), and keep anchor files
somewhere the robot's operator cannot change: an auditor who holds a later anchor turns a cut
into `missing_events`.

What L2 proves: nothing was dropped, re-timed or edited after a checkpoint was anchored, and any
such change is detected even without the original log. What it does not prove: a dishonest recorder
can still log false events as they happen, and a key held in software can sign a second, fabricated
log from scratch -- only an anchor made *before* the fabrication catches that (an anchor the
fabricator can also redo does not). Sealing an old, unanchored log late is indistinguishable from a
log that was sealed on time but anchored late, and shows up as `late_anchor`. Trust level L3 adds
a second, independent witness against the first class of problem (see [Station
witness](witness.md)); L4 (hardware-rooted keys) follows in a later spec
version.
