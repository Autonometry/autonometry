# Open questions — Autonomy Meter v0.1

Date: 2026-10-30. Each item states what is uncertain, why it matters, and a concrete way to resolve it. Tags follow the project convention (VERIFIED / CLAIM / UNKNOWN) where a factual status is involved.

## A. Data and sources

1. **California DMV 2025-period reports (Dec 2024-Nov 2025).** UNKNOWN. The CSVs were listed on the DMV site before the 2026 restructure but are no longer served; only the 2026-02-20 press release (aggregate: >9 million test miles) remains. Resolution: public-records request to the DMV (autonomousvehicles@dmv.ca.gov) for the 2025 CSVs, or a user-supplied copy; rerun `dmv_analysis.py` unchanged.
2. **Paywalled standards read from scope pages only** (SAE J3016, IEEE 1872.2, IEEE 7001, ASTM F45/F3200, ISO 8373 beyond the sample clauses, T/CIE 298-2025 level criteria). CLAIM/UNKNOWN. Resolution: obtain the texts and re-tag the corresponding rows of `sources_all.csv`; the gap statement is unlikely to change (none of these define interval methods), but the J3016 and IEEE 1872.2 role definitions should be quoted exactly in `definitions.md`.
3. **13 CCR §227.50 definition wording.** The Phase 1 review verified the 2019 adopted text; the Phase 2 DMV analysis could not re-fetch law.cornell.edu and cites the definition as CLAIM. Resolution: quote the adopted text directly in `dmv_analysis.md`.
4. **ArmnetBench stopping rule and trimming.** UNKNOWN whether teleop episodes were end-trimmed like successful policy episodes (`success_cutoff_time`), and how failed episodes were terminated. Resolution: ask the dataset maintainers; until then the auto/teleop duration ratio (0.86-1.14) carries an unquantified trimming bias.

## B. Definitions

5. **Store-and-forward human planning.** A human who writes the plan the robot then executes is invisible to the control-state log. Proposed: add a `plan_received` event with `author` in {ai, human_local, human_remote} and a declared duration counted as human time (gaming.md section 4). Needs schema change and a rule for plan granularity.
6. **Shared control attribution.** `shared` is currently neither AI nor human in the headline share (reported as a separate variant). Alternatives: split by a declared arbitration weight; count as human. Resolution: pilot on a shared-autonomy system that logs the arbitration weight and compare the three rules.
7. **Approval dwell default.** `approval_min_dwell_s = 0` with a 5 s sensitivity variant. Field data on real approval latencies (time from `help_request` to approval) are needed to set a defensible default.
8. **Idle threshold and idle cap.** 30 s and 0.5 are placeholders. Resolution: distribution of inter-task gaps in field logs; the cap should be set so that normal operations rarely trigger dual reporting.
9. **Per-body-part scope weights for humanoids.** Default weights are declared by the reporter; no principled weighting exists. Resolution: report both any-human and scope-weighted shares (already required) and collect field cases before fixing a default.

## C. Estimators

10. **Robot-level generalisation with few robots.** Simulation showed session-level intervals fall to 0.67-0.73 coverage when the claim is about new robots and only 4 robots exist; robot-level t intervals hold but the robot-cluster GLM for the task rate does not (0.851 with 4 clusters). Resolution: evaluate small-sample cluster corrections (CR2/CR3 sandwich, wild cluster bootstrap) for the task rate; until then the standard requires a t interval with G-1 degrees of freedom and a `single_cluster` label below 2.
11. **Gap-distribution intervals under clustering.** KM median CI covers 0.81 under over-dispersion because within-session gaps are treated as independent. Resolution: frailty or cluster-bootstrap KM; keep (d) descriptive until then.
12. **NB Wald in sparse regimes.** Fails with fewer than ~3 clusters and when a handful of events sit in one cluster (DMV: Mercedes-Benz 2024, 4 events in one VIN; Waymo driverless, 22 events over 789 VINs). Resolution: a profile-likelihood or score interval for the NB rate; rule for preferring the cluster bootstrap when NB alpha is driven by very few events.
13. **Upper bounds for (c) and (e) under hidden events.** None exist; the standard says so. Resolution: an external witness (L3 station log, network witness) is the only route to an upper bound on hidden human time.
14. **Zero-event planning dispersion `a`.** Default 0.5 is a placeholder. Resolution: estimate between-deployment dispersion from the first conforming reports.
15. **Drift with sessions spanning bins.** `drift` assigns a session to the bin of its start. Resolution: split sessions at bin boundaries once sessions longer than a bin appear in practice.

## D. Gaming and trust

16. **Teleoperation detection from encrypted traffic.** No primary study (UNKNOWN); the accuracy estimate in trust.md 6.2 is an extrapolation from cloud-gaming, IoT and video-stream classifiers (CLAIM). Resolution: a controlled study with labelled sessions on public traces (teleoperation on/off, vendor-cloud routing, VPN, padding), reporting recall `r` and false-positive rate `f`; define the calibration protocol before conformance weight is attached.
17. **Clock tolerance `tau` for L3.** Default 1 s; needs calibration against measured NTP/PTP dispersion on robot fleets and teleop stations.
18. **Canonical JSON for hashing.** The chain uses SHA-256 of canonical JSON without the `hash` field; the canonicalisation (key order, number formatting, Unicode) must be fixed normatively (e.g. RFC 8785 JCS) to make chains verifiable across implementations.
19. **Checkpoint publication at L2.** Recommended, not required. Resolution: decide after assessing whether a public transparency log (Rekor-style) or witness cosigning is practical for reporters.
20. **Residual risks.** Easy-deployment selection, unobserved control paths (direct radio, local joystick) and store-and-forward planning are disclosed, not measured. Only physical audit or L4 input-path attestation addresses the second.

## E. Validation coverage

21. **Only two of six metrics tested on real data.** DMV tests metric (c) in distance units; ArmnetBench tests (b) and (e). Time share (a), gaps (d) and drift (g) are validated by simulation only. Resolution: a pilot deployment logging the full event schema (e.g. an interactive-imitation-learning setup such as Sirius/HIL-SERL, which already records interventions) for at least 20 sessions on >= 3 robots.
22. **Generator realism.** Session-level log-normal durations, Gamma frailty, Hawkes bursts and MNAR dropout are modelling choices. Resolution: refit generator parameters to the pilot logs and rerun `run_coverage.py` (seeds and scripts are in `simulation_bundle.zip`).
23. **Bootstrap replicates.** n_boot = 500 in the coverage study (1,000 in the package default). Resolution: confirm that the recommended defaults are unchanged at n_boot = 2,000 on the two worst cells.

## F. Standard process

24. Public-comment period and governance for v0.2; whether the schema `$id` domain and a registry of `task_definition_id` namespaces should be hosted by a neutral body.
25. Conformance tooling: a validator that runs the checklist C1-C16 automatically from a log plus report (the reference implementation covers C1, C5-C9 and C13 chain verification today).
