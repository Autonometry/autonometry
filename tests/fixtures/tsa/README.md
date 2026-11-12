# RFC 3161 test fixtures

Source: [FreeTSA](https://freetsa.org), a public, free Time Stamping Authority.
Root retrieved: 2026-11-03. Token retrieved: 2026-11-12.

- `freetsa_root.pem`: FreeTSA's root CA certificate, fetched from
  `https://freetsa.org/files/cacert.pem`. Valid 2016-03-13 to 2041-03-07.
- `golden_checkpoint.tsr`: a real RFC 3161 `TimeStampResp`, requested live from
  `https://freetsa.org/tsr`, over the exact bytes of the signed checkpoint note that
  `tests/golden/sealed/log.jsonl` session `s2` produces at size 3 (its last event) when sealed
  with the fixed test key `tests/golden/sealed/__init__.py` derives from the seed
  `b"autonometry-golden-test-seed"`. `tst_info.gen_time` is `2026-11-12T07:55:15Z`. (Spec v0.3 made
  `session_start.simulated` required, which changed the log and so the note; the token was
  requested again.)

The golden log's own event timestamps (`tests/golden/sealed/log.jsonl`, session `s2`, last event
at `2026-11-12T07:52:40.000Z`) were chosen a few minutes before this token was requested, so the
token falls inside the definitions.md v0.3 section 10.5 window (`max_commit_delay_s` = 3600,
`clock_tolerance_s` = 1) relative to that fixed timestamp, forever -- this does not depend on
when the tests are run.

`tests/test_tsa.py` pins that certificates are checked against the token's own `gen_time`, not
wall-clock time, so this fixture never expires as its own certificate chain ages.
