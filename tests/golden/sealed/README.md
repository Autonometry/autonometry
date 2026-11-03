# Sealed golden log

`log.jsonl`: the raw, unsealed input -- two sessions (`s1`, `s2`) of robot `r1`, with fixed
timestamps. `tests/test_verify.py` seals it at test time with the fixed test key
`tests/sealing_helpers.py` derives from the seed `b"autonometry-golden-test-seed"` (never a
committed private key), using the real `Sealer` and signed-note code, then runs the verifier
against the result and its attacks.

`tests/fixtures/tsa/golden_checkpoint.tsr` is a real RFC 3161 token recorded once (see its
README) over session `s2`'s final checkpoint note, which this exact log and key reproduce
byte-for-byte every time.
