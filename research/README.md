# Research (reference only)

Material from the design phase (October 2026, working name "Autonomy Meter"). It explains why the
rules and methods are what they are. Nothing here is imported or shipped. Where this folder and
`spec/` disagree, `spec/` wins (for example, v0.2 replaced the genesis string, the PAUSED mapping
and the replay mapping used in some of these files).

| Folder | What it holds |
|---|---|
| `methods/` | Methods note (103 sources), draft reporting standard v0.1, trust review (sealing, two witnesses, network witness), gaming review, prior art, open questions |
| `reference-impl/` | `autonomy_meter` v0.1.1: the reference statistics package (44 tests). Port its estimators; known defects are listed in CLAUDE.md |
| `simulation/` | Coverage study that chose the default interval methods (scripts and summary tables) |
| `lerobot-so101/` | SO-101 / LeRobot v0.6.1 dossier, signal map, integration notes, computed human shares of public datasets |
| `demo-prototypes/` | Dummy-pilot generator, sealing and reconciliation prototypes, and results of six demo scenarios |

Known defects in `reference-impl/` (fix them when porting):
1. `verify_chain` passes logs whose hashes were stripped, rewritten or cut off; it has no signed checkpoints.
2. `full_report` crashes on logs with zero interventions (drift GLM).
3. 20 of 44 tests fail when NumPy deprecation warnings become errors (timedelta units).
4. Hashing uses `json.dumps`, not RFC 8785.
5. A declared `idle` inside an open task is accepted silently.
6. No reconciliation (L3), invariants, provenance or trust level in the report.
7. Uses `lifelines`, which blocks pandas 3; replace with statsmodels `SurvfuncRight` and SciPy `CensoredData`.
