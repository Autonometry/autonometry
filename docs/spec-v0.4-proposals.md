# Proposals for spec v0.4

Changes to the specification that the November 2026 security review
([`security-review-2026-11.md`](security-review-2026-11.md)) and its follow-up proposed. None of
them is in `spec/v0.3/`. Version 0.1 of the software implements each as described below, and
says so where it goes beyond v0.3.

## 1. A verified session ends under an anchor

**Problem (S-14).** definitions.md 10.6 gives `verified` to a session that is L2 up to its last
event. Someone who can edit the log's files can cut a session back to an earlier anchored
checkpoint and drop the later checkpoints and tokens: what remains is L2 up to its last event, so
it is `verified`, and only the missing `session_end` shows the cut.

**Proposal.** Add to 10.6: "A session is `verified` only when an anchored checkpoint covers its
`session_end`; a session that is L2 up to its last event but has no anchored `session_end` is
`partial`." Add to 11.7: "A report is clean only when every session's `session_end`, the robot's
and the stations', is covered by an anchored checkpoint." A crash also leaves a session without
`session_end`: such a session would no longer be `verified`, which is right, since nothing then
proves that the session ended where its log ends.

**v0.1 meanwhile.** The statuses of 10.6 stay. Every session verdict carries `last_anchored_ts`
and `end_anchored`; `verify` prints a warning for a session whose `session_end` no anchored
checkpoint covers ("events after the last anchor (<time>) may have been removed"), and the
report lists the same among the reasons it is not clean.

## 2. Only L2 is "verified"

**Proposal.** Add to 10.6: "A verifier asked for trust level L1 reports whether L1 was met; it
never calls a session or a result `verified` unless it is verified at L2."

**v0.1.** `verify --require L1` prints `result: meets L1` (and whether the log is verified at L2);
the JSON has `meets_required_level` beside `verified`, which is true only at L2.

## 3. Input limits in `rules.toml`

v0.3 puts one input limit in `rules.toml` (`[errors] max_line_bytes = 65536`) and a few in the
schemas (`maxLength` of a checkpoint note and of a timestamp token). Version 0.1 enforces the
limits below in code; they bound what every implementation must accept and what it may refuse, so
they belong in the specification. Proposed as a new `[limits]` table, with the error or file code
each one gives.

| Limit | v0.1 value | Applies to | When exceeded |
|---|---|---|---|
| `max_nesting_depth` | 128 | arrays and objects in a log line, checkpoint or anchor record, registry, plan, recorder datagram (the line's own object is level 1) | `invalid_json` |
| `max_tree_size` | 2^64 − 1 (at most 20 decimal digits) | a checkpoint's tree size | `malformed_checkpoint` |
| `max_signature_lines` | 16 | signature lines of one signed note | `malformed_checkpoint` |
| `max_registry_bytes` | 1 MiB | a device registry, task registry or plan | the file is refused |
| `max_config_bytes` | 1 MiB | a recorder config | the recorder does not start |
| `max_clients` | 256 | distinct client names a recorder follows in one run | the datagram is rejected (`unknown`) |
| `max_scopes` | 256 | scopes a recorder config declares | the recorder does not start |
| `max_tsa_response_bytes` | 64 KiB | an RFC 3161 response | the token is not used |
| `max_key_bytes` | 64 KiB | a private key file | the key is refused |

Durations from a log (`heartbeat_interval_s`, `dwell_s`, `ttl_s`, `clock_max_error_s`) need no
upper limit: v0.1 saturates any duration longer than any two timestamps can be apart (2^70 ns),
which leaves every comparison unchanged. A sentence in section 8 could say so: "a duration longer
than the span of the timestamp format compares as longer than any interval".

The recorder's free-space floor (`min_free_bytes`, default 1 GiB) and the LeRobot importer's
bounds (24 h per episode, 10 million rows per parquet file) are properties of those programs, not
of the log format, and stay out of the specification.
