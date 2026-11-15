# Autonometry specification

**Status: draft.** v0.3 is a working draft, published with Autonometry 0.1.0 (alpha) as the
version that software implements. It may still change before it is declared stable, and
reports name the spec version they were computed under.

**Versioning.** Each version lives in its own folder (`v0.3/`). A released version folder never
changes; fixes and new rules go into a new version, whose `definitions.md` lists its changes. The
software ships every version folder inside the package (`autonometry/spec/`), and
`autonometry spec show <version>` prints the SHA-256 of each file, so a report can be checked
against the exact rules it used. Changes planned for the next version are collected in
[`docs/spec-v0.4-proposals.md`](../docs/spec-v0.4-proposals.md).

**Feedback.** Open an issue with the **Spec change** template
(<https://github.com/Autonometry/autonometry/issues/new/choose>): the section, the case the rules
get wrong or leave open, and an example log if you can. [`CONTRIBUTING.md`](../CONTRIBUTING.md)
says how a proposal becomes part of the next version.

Current draft: **v0.3**.

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
