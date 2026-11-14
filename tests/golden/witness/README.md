# Witness golden set (definitions.md 12)

Robot and station logs sealed with two fixed-seed test keys (`tests/witness_files.py`; never a
committed private key), and the expected output of

```
autonometry reconcile --robot robot/r01.jsonl --robot robot/r02.jsonl \
    --station station/r01.jsonl --station station/r02.jsonl --vkey keys.vkey --anchor anchors.jsonl
```

run inside this folder.

| Robot | Robot log | Station log | Expected |
|---|---|---|---|
| `r01` | `ai`, a takeover `human_remote` at 100 s to 130 s, `ai`; 300 s | live input 100.1 s to 130.05 s (`pc_1`) | L3: 30.05 s both-human, of which 0.15 s tolerated; tau 0.52 s |
| `r02` | `ai` for 300 s (scenario S2, the hidden remote operator) | live input 100 s to 130 s (`pc_2`), clock error 0.1 ms without PTP | L2: 30 s station-only, share 0.1 > 0.005; one unmatched station episode; the small station error narrows tau to 0.5101 s |

Both clocks report 0.01 s errors except station `pc_2`. Every log has a heartbeat each second.
The anchor file holds the checkpoints of all four logs, so every session is L2 before the
witnesses are weighed. Regenerate after an intended change, then review the diff:

```
uv run python tests/witness_files.py
```
