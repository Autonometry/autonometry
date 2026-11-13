# Autonometry specification

A released version folder never changes; fixes go into a new version. Current draft: **v0.3**.

| File | Role |
|---|---|
| `v0.3/definitions.md` | Normative rules: control states, transitions, interventions, holds, scripted motion, device registry, goal input, several sources, presets, hand-overs, physical support, unmeasured time, event format, hashing, sealing and verification |
| `v0.3/event.schema.json` | JSON Schema (draft 2020-12) for one event line |
| `v0.3/device_registry.schema.json` | JSON Schema (draft 2020-12) for the device registry (section 6.10) |
| `v0.3/task_registry.schema.json` | JSON Schema (draft 2020-12) for the task registry (section 1, Task) |
| `v0.3/plan.schema.json` | JSON Schema (draft 2020-12) for the plan of pre-registered human actions (section 3, `planned`) |
| `v0.3/checkpoint.schema.json` | JSON Schema (draft 2020-12) for one checkpoint or timestamp record (section 10.4) |
| `v0.3/rules.toml` | Machine-readable tables derived from `definitions.md`; code reads these instead of hard-coding rules |

The reporting standard (required report fields, interval methods, trust levels L1-L4) is still the
design-phase draft in `research/methods/standard_v0.1.md`. It will move here as `standard.md`.
