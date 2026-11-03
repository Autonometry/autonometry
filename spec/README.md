# Autonometry specification

A released version folder never changes; fixes go into a new version. Current draft: **v0.2**.

| File | Role |
|---|---|
| `v0.2/definitions.md` | Normative rules: control states, transitions, interventions, holds, scripted motion, device location, unmeasured time, event format, hashing |
| `v0.2/event.schema.json` | JSON Schema (draft 2020-12) for one event line |
| `v0.2/rules.toml` | Machine-readable tables derived from `definitions.md`; code reads these instead of hard-coding rules |

The reporting standard (required report fields, interval methods, trust levels L1-L4) is still the
design-phase draft in `research/methods/standard_v0.1.md`. It will move here as `standard.md`.
