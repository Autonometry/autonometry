# Autonomy Meter demo (dummy data)

Six simulated scenarios run through the research reference package (`autonomy_meter` v0.1.1).
The prototypes here exist only for the demo; the real build replaces them.

## Run

```
pip install numpy pandas scipy statsmodels lifelines jsonschema cryptography rfc8785
unzip reference.zip              # from the Claude Science bundle -> reference/autonomy_meter
python run_demo.py reference     # about 1 minute
```

## Files

| File | What it is |
|---|---|
| `gen.py` | Simulated pilot: robot event logs (schema v0.1), teleop-station logs, hidden ground truth |
| `seal.py` | L2 prototype: RFC 8785 hash chain, Ed25519 signed checkpoints, verifier that fails closed |
| `reconcile.py` | L3 prototype: robot log vs station log, following trust.md section 4.3 |
| `run_demo.py` | Runs S1-S6 and writes `out/` |
| `out/results.json` | Every number from the six scenarios |
| `out/report_S*.md` | Sample one-page reports with suggestions |
| `out/s*_*.jsonl, *.json` | Fixture logs to reuse as test data in the build |

## Scenarios

S1 honest pilot · S2 hidden remote operator · S3 logger crash · S4 log tampering · S5 "zero takeovers" claim · S6 LeRobot DAgger mapping.
