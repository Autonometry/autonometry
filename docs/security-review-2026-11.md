# Security review, November 2026 (milestone M10)

Pre-release review of Autonometry before its first release, done as an outside audit: nothing the
comments, README or tests claimed was taken as true until an attack had tried to break it. The
threat model it tested is [`threat-model.md`](threat-model.md).

- **Date:** 2026-11-14. **Commit reviewed:** `3d9a843` (main after M9). **Branch:** `m10-security`.
- **Result:** 48 findings. 40 are fixed, each with a regression test. 6 are accepted, with the
  reason given. 1 needs the repository owner (S-13, settings). 1 is a tool's false positive.
  - The first pass (PR #17) fixed 28: 2 high, 8 medium and 18 low.
  - It left 12 findings, in 10 open points, waiting for a decision. The owner decided 9 of the 10.
    The follow-up (branch `m10b-security-decisions`) fixed 11 of those 12 findings (see "Open
    points and their decisions").
  - The owner reported S-64 during review, and the follow-up fixed it.
  - Every fix keeps the existing checks. No file under `spec/` changed and no dependency was
    added; the spec changes the decisions call for are proposals for v0.4
    ([`spec-v0.4-proposals.md`](spec-v0.4-proposals.md)).

## Scope

- **Sealing and verification:** `strictjson`, `reader`, `sealfile`, `signednote`, `merkle`,
  `sealing`, `verify`, `tsa`, `keys` and `fileio`, read against definitions.md 8, 10, 11 and 12.
- **Recorder:** `recorder/` (socket, engine, store, runtime, anchor, config, clock) and the client
  library in its three copies.
- **Analysis and the command line:** timeline, metrics and report (HTML and SVG), the witness
  reconciler, and every command of `cli.py`, `cli_g1.py` and `cli_lerobot.py`.
- **Connectors:**
  - Unitree G1: `g1`, `g1_dds`.
  - TurtleBot 4: `tb4` and the ROS 2 observer.
  - Station: `station` and the station observer.
  - LeRobot: `lerobot`, `lerobot_io`.
- **Workflows, release and history:** `.github/workflows/`, `RELEASING.md`, the repository's
  GitHub settings and the full git history.
- **Not in scope:** `research/` (reference material, never shipped), the robots' own software, and
  attackers with root on the recording computer (SECURITY.md).

## Method

1. Read CLAUDE.md, SECURITY.md, RELEASING.md and definitions.md (sealing, trust levels, report,
   witness), then every module in scope.
2. Write the threat model: assets, attackers and trust boundaries.
3. Attack each boundary with code: throwaway scripts first, then every attack that found something
   (and the main ones that did not) kept as a pytest test. The tests are:
   - `tests/test_security_verify.py`: tamper attacks through `autonometry verify` on real files;
   - `tests/test_security_parsers.py`: hostile numbers, nesting, configs, file names, the witness;
   - `tests/test_security_recorder.py`: datagrams, socket, folders and keys;
   - `tests/test_security_network.py`: the TSA client;
   - `tests/test_security_report.py`: HTML and SVG injection;
   - `tests/test_security_connectors.py`: G1, TurtleBot 4, station and LeRobot;
   - `tests/client/test_client.py`: the client library.

   Test names carry the finding ID (`__s04`).
4. Two parallel sub-reviews attacked the connectors: the G1 edge and engine, and LeRobot,
   TurtleBot 4 and the station. Every finding they reported was reproduced again before it was
   fixed or listed.
5. Run the tools below. POSIX-only tests (socket, file modes, the client) also ran on Linux in
   Docker (python:3.12-slim, as a non-root user), besides CI's Ubuntu and macOS jobs.

## Tools and versions

| Tool | Version | Run on | Result |
|---|---|---|---|
| zizmor | 1.32.0, offline, auditor persona | `.github/workflows` | 2 low findings, both fixed (S-12). Now a CI job. |
| actionlint | 1.7.7 (Docker `rhysd/actionlint:1.7.7`, shellcheck included) | `.github/workflows` | 2 SC2086 infos, fixed (S-12). |
| gitleaks | 8.24.0 (Docker `zricethezav/gitleaks:v8.24.0`) | full history, `--log-opts=--all`, 128 commits | 3 hits, all false positives (S-63). A grep of the history for private-key blocks and token patterns found nothing. No secret scan was added to CI, since nothing real was found. |
| bandit | 1.8.6 | `src`, `clients`, `connectors` | 14 B101 (asserts), 1 B704 (`Markup`). All accepted (S-60, S-61). |
| pip-audit | 2.10.1, as CI runs it | locked set and lowest set | Locked set clean. The lowest set has the 2 advisories CI already ignores; their reasoning was re-checked (S-62). |
| ruff | 0.17.3 (rules E, W, F, I, B, UP, S, SIM, RUF, PT, N, C4, PL) | everything | clean |
| mypy | 2.4.1 `--strict` | `src`, `clients/python` | clean |
| pytest, hypothesis | 9.2.0, 6.170.14 | Python 3.13.4 (Windows), Python 3.12 (Linux, Docker) | all pass. Coverage is in the PR. |
| GitHub API (`gh`) | 2.x | action pins, repository settings | Every action pin matches its tag. The settings findings are S-13. |

semgrep (optional) was not run.

## Severity

- **High:** someone without the key can make the measurement wrong (time counted as `ai` that is
  not, product rule 2) or stop the robot.
- **Medium:** someone can crash or stall a component (recorder, verifier, connector, report), make
  a connector keep a stale state, or weaken the release.
- **Low:** needs an unusual setup or local access, or slows an offline tool.
- **Info:** a tool result with no defect behind it.

## Findings

Status gives the fixing commit and the regression test, or why the finding is accepted, or a
pointer to the open point it became. Paths are relative to `src/autonometry/`.

### Sealing, verification and parsers

| ID | Sev. | Where | What | Status |
|---|---|---|---|---|
| S-01 | Medium | `signednote.py` `parse_checkpoint_body` | A checkpoint note whose tree size has more than 4300 digits made `int()` raise `ValueError`. That crashed `verify`, `report` and recorder recovery. Anyone who can add a line to a checkpoint or anchor file could stop verification. | Fixed in 4229d9f. A size is now ASCII decimal below 2^64, else `malformed_checkpoint`. Test: `test_a_checkpoint_size_of_5000_digits_is_malformed_not_a_crash__s01`. |
| S-02 | Medium | `timestamps.py` `seconds_to_ns` | Schema-valid `heartbeat_interval_s`, `dwell_s` or `clock_max_error_s` of 1e308 overflowed to infinity (`OverflowError`). That crashed `validate`, `timeline`, `metrics`, `report` and `reconcile`. | Fixed in 4229d9f. Durations saturate at 2^70 ns, longer than any two timestamps can be apart, so every comparison is unchanged. Tests: `test_extreme_schema_valid_numbers_never_crash_a_command__s02`, `test_extreme_clock_errors_never_crash_reconcile__s02`, `test_seconds_to_ns_saturates_instead_of_overflowing__s02`. |
| S-03 | Medium | `strictjson.py` | Nesting was limited only by the interpreter's stack. A line nested about 990 deep parsed or not depending on the caller's stack depth, which broke the determinism of rule 4. When it parsed, it crashed RFC 8785 hashing with `RecursionError` in `verify`. A datagram nested 1000 deep did the same inside the recorder. | Fixed in 4229d9f. The limit is now 128 levels for logs, checkpoint files, registries and datagrams, with error `invalid_json`. Tests: `test_nesting_deeper_than_the_limit_is_rejected_at_any_depth__s03`, `test_a_sealed_log_nested_to_the_limit_verifies__s03`, `test_deeply_nested_datagrams_are_rejected__s03`. See open point 3. |
| S-05 | Low | `recorder/config.py` | A config of deeply nested TOML arrays made `tomllib` raise `RecursionError`, so `autonometry record` crashed. | Fixed in 4229d9f. It is now a one-line `ConfigError`. Test: `test_a_deeply_nested_config_is_a_config_error__s05`. |
| S-14 | Medium | definitions.md 10.6, `verify.py` | A log cut back to an earlier checkpoint, with the later checkpoints and tokens removed, verifies at L2 as the shorter log. Only `no_session_end` shows it. A dishonest operator can drop the end of a session without the key. Only an anchor file kept outside the log catches this (tested). | Decided (open point 1), fixed in e2ed273. The statuses stay; `verify` warns that events after the last anchor (its time) may have been removed, and the report is not clean. The spec rule is proposed for v0.4. Tests: `test_a_session_cut_back_to_an_anchor_verifies_but_is_warned__s14`, `test_sessions_whose_end_is_anchored_carry_no_warning__s14`, `test_a_report_with_a_cut_session_is_not_clean__s14`, `test_a_tail_cut_with_its_checkpoints_is_caught_by_the_anchor__defs_10_5`. |

### Recorder, client library and network

| ID | Sev. | Where | What | Status |
|---|---|---|---|---|
| S-04 | Medium | `recorder/engine.py`, `recorder/runtime.py` | Any process that can write to the socket could stop the recorder. Two ways to do it: a client counter `n` that jumps to 2^53 − 1 twice made `x_lost_client_events` exceed I-JSON's integers (`IntegerDomainError`), and two clients each reading 1.7e308 m made the distance infinite (`FloatDomainError`). Either way the next heartbeat could not be written. The recorder loop also had no per-datagram boundary. | Fixed in 72053fb. Counts saturate at 2^53 − 1 and an overflowing reading is rejected. A datagram the engine fails on is counted as rejected and makes every scope `unknown`. Tests: `test_a_counter_jump_past_2_53_never_crashes_the_recorder__s04`, `test_odometer_readings_that_overflow_are_rejected__s04`, `test_a_datagram_the_engine_fails_on_is_counted_and_the_recorder_runs_on__s04`, `test_extreme_values_in_any_order_never_stop_the_recorder__s04` (hypothesis). |
| S-06 | Low | `recorder/service.py` `bind_socket`, `clear_stale_socket` | The socket's mode and group were set with `chmod` and `chown`, which follow symbolic links. In a socket folder others can write, a socket swapped for a link while it was being set up would change the mode of the link's target, such as the key. In such a folder another user could also bind the configured path first and receive every connector's datagrams. A stale socket that could not be removed crashed the start. | Fixed in 72053fb. The umask creates the socket with its final mode. The group is set with `follow_symlinks=False` and the file is checked with `lstat`. A socket folder every user can write is refused. Removal failure is a `StartError`. Tests: `test_a_socket_folder_every_user_can_write_is_refused__s06`, `test_a_socket_swapped_for_a_link_while_set_up_is_refused__s06`, `test_the_socket_is_created_with_its_final_mode__s06`, `test_a_stale_socket_that_cannot_be_removed_is_a_start_error__s06`. |
| S-07 | Low | `recorder/store.py`, `recorder/service.py` | An existing robot folder was used whatever its owner and mode, and logs were opened through symbolic links. A robot folder another user could write (for example made by hand under a shared `data_dir`) would let them plant a link for the next session's `events.jsonl`. The recorder would then append to any file it can write. | Fixed in 72053fb. The robot folder must belong to the recorder's user and be writable by nobody else. `data_dir` must not be world-writable unless it has the sticky bit. Logs and the lock file open with `O_NOFOLLOW`. Tests: `test_a_log_file_replaced_by_a_link_is_refused__s07`, `test_a_robot_folder_others_can_write_is_refused__s07`, `test_a_data_folder_every_user_can_write_is_refused_unless_sticky__s07`. |
| S-08 | Low | `keys.py` `load` | The key file was read, then its owner and mode were checked by path, so the file checked need not be the file read. Reads were also unbounded. | Fixed in 72053fb. The checks now use `fstat` on the opened file, and keys are limited to 64 KiB. Tests: `test_the_key_checked_is_the_key_read__s08`, `test_an_oversized_key_file_is_refused__s08`. |
| S-64 | Medium | `cli.py` `verify_summary_lines`, `events.py` `VerifyResult.to_json` | Reported by the owner during review. `verify --require L1` printed "result: verified" and returned `"verified": true` for a session whose status was `unverified`, so a reader took an L1 result for a verified log. | Fixed in e2ed273. Only L2 is verified. At L1 the line says `result: meets L1` and, separately, whether the log is verified at L2; the JSON adds `meets_required_level`, and `verified` is true only at L2. Tests: `test_require_l1_never_says_verified_below_l2__defs_10_6`, `test_require_l1_on_a_verified_set_says_both__defs_10_6`, `test_verify_reports_failed_sessions_in_the_result_line`. |
| S-11 | Low | client library (3 copies) | A signal handler that sent an event while its thread was inside a send waited for ever on a non-reentrant lock. That breaks "never blocks" (rule 1). A path or name whose `str()` raised made the constructor raise. | Fixed in d45e376. The lock is now an `RLock` and the constructor is guarded. Tests: `test_a_send_from_a_signal_handler_mid_send_never_blocks`, `test_arguments_that_cannot_be_read_never_raise` (unittest, Python 3.8). |
| S-15 | Low | recorder | `max_log_bytes` caps one file, but nothing caps the total size of `data_dir`. A misbehaving connector (64 KiB heartbeats at the rate limit, times up to 256 client names) or a flood of G1 writers notes (S-27) can fill the disk. On a robot whose own software shares that disk, this touches rule 1. | Decided (open point 2), fixed in 1ca2f09. `min_free_bytes` (default 1 GiB): below it the recorder stops writing with one error and resumes when space returns, and the gap is `unknown`. Tests: `tests/test_recorder_disk.py`. |
| — | — | `tsa.py`, `recorder/anchor.py` | The checks held: TLS is verified, timeouts and the total deadline hold, the response size is limited, redirects are refused, the nonce is checked, and requests stay off the write path. | No defect found. Kept as tests: `test_a_tsa_with_a_certificate_nobody_trusts_is_refused`, `test_a_tsa_that_never_verifies_fails_with_one_line`, `test_a_tsa_that_hangs_never_holds_up_a_checkpoint_writer`. Existing tests already cover redirects, oversize, slow drip, replay and the wall clock. |

### Command line, report and reconciler

| ID | Sev. | Where | What | Status |
|---|---|---|---|---|
| S-09 | Low | `cli.py`, `cli_g1.py`, `cli_lerobot.py` | File names (chosen by whoever wrote the log) and messages naming input were echoed raw. An escape sequence in a file name could rewrite the terminal. | Fixed in 4297475. All text the CLI prints and its one-line log formatter now go through `display.printable`, which escapes C0, C1, DEL, format and separator characters. Tests: `test_printable_escapes_control_format_and_separator_characters__s09`, `test_a_file_name_with_an_escape_sequence_prints_escaped__s09`. |
| S-10 | Low | `witness.py` `_overlaps` | Every run was matched against every episode by a linear scan. 8000 runs and 8000 episodes took 40 s; a crafted station log of short episodes could stall `reconcile` and `report --station` for hours. | Fixed in 4297475. A binary search makes it O(n log n). Tests: `test_overlap_by_bisection_matches_the_linear_definition__s10` (hypothesis, against the old definition), `test_matching_many_runs_against_many_episodes_is_not_quadratic__s10`. |
| — | — | `report/html.py`, `report/figures.py` | No injection found. Autoescape is on and every attribute is quoted. The figures draw text as paths, and their labels are fixed. Title and about refuse control characters. | Kept as tests: `test_no_input_string_reaches_the_report_as_markup__defs_11`, `test_the_figures_hold_no_text_from_the_input__defs_11`, `test_a_title_with_control_characters_or_too_long_is_refused__defs_11`. |
| — | — | tamper evidence | All held: changing, dropping, duplicating or splicing events; swapped checkpoint files; replaying an older seal; files cut mid-line; checkpoints from another key; reordered events. Reordering lines without changing `seq` is no tampering: event i is the i-th in `seq` order (10.1). | Kept as tests in `tests/test_security_verify.py`. They add to the existing ones in `test_verify.py`: hash, root, signature and timestamp-token codes and the mutation property. |

### Unitree G1

| ID | Sev. | Where | What | Status |
|---|---|---|---|---|
| S-20 | High | `connectors/g1_dds.py` `_participant`, `pump` | `str.isdigit()` accepts digits that `int()` refuses ("²"). Any participant announcing such a `__Pid` crashed `g1 discover` and every `g1 watch` pass that saw it. Each failed pass also lost the task change it had already taken from the session log. A task end lost this way left a standing robot counted as `ai`. | Fixed in 1c82f6b. Only ASCII digits are read, and task changes reach the engine before DDS is read. Tests: `test_a_participant_pid_that_is_not_ascii_digits_is_ignored__s20`, `test_a_failing_dds_read_never_drops_a_task_change__s20`. |
| S-21 | High | `connectors/g1.py` `_mode_request`, `_on_response` | A mode request ended an emergency stop. Anyone on the network can send one that the robot refuses, so a policy still writing `rt/lowcmd` then counted as `ai` while the robot stood stopped. | Fixed in 1c82f6b (a refusal puts the stop back), then decided (open point 4) and fixed in 5fd7386: the stop holds until the robot confirms, by a success response from the built-in controller or `rt/sportmodestate` leaving passive. A request alone never ends it, so the residual window is closed. Tests: `test_a_refused_mode_request_never_ends_an_emergency_stop__s21`, `test_a_stop_holds_until_the_robot_confirms_not_on_a_request__defs_6_7`, `test_stop_is_at_once_while_released_and_ends_when_the_robot_confirms__defs_6_7`. |
| S-22 | Medium | `connectors/g1.py` `_on_response`, `_on_arm_state`, `_on_lowstate` | Responses, arm action states and `rt/lowstate` are believed from any writer. A forged CheckMode answer, arm state or lowstate can push a scope towards `ai`. | Decided (open point 5), fixed in 5fd7386 for responses: they are believed only from the identity of the declared built-in controller's `rt/sportmodestate` writer, and others are noted (`x_g1_ignored_response`). Test: `test_responses_from_another_identity_are_ignored_and_noted__defs_6_10`. The arm action state and `rt/lowstate` keep the documented limit: names are claims, and L3 proves remote control. |
| S-23 | Medium | `connectors/g1.py` `_on_command` | A non-finite `rt/arm_sdk` weight (unreadable) kept the writer's previous weight instead of counting as 1, the documented open case. A person who first sent weight 0 then stayed hidden behind the built-in controller's `ai`. | Fixed in 1c82f6b. Test: `test_an_unreadable_blend_weight_counts_as_1_not_as_the_last_one__s23`. |
| S-24 | Low | `connectors/g1.py` `_goal` | Goal spans were never capped. Every participant could open one lasting its velocity's duration, and each observation scanned them all. | Fixed in 1c82f6b. Spans are capped at 1024, dropping the one that ends first. Test: `test_goal_spans_are_capped__s24`. |
| S-25 | Low | `connectors/g1.py` `_sport_request`, `_arm_request` | A request number of 1e300 made an actor id of 319 characters. The recorder rejected every state, so all scopes went `unknown` while the preset ran. | Fixed in 1c82f6b. Names now go through `_script_name`. Test: `test_a_huge_request_number_still_makes_a_valid_actor_id__s25`. |
| S-26 | Medium | `connectors/g1_dds.py`, `connectors/g1.py` | Device identity is self-declared: host, process name and pid all come from the participant itself. A participant announcing a declared program's names counts as that program. Two live participants claiming one identity are not detected. | Decided (open point 5), fixed in 5fd7386. Two writers with the same observed source commanding within `contention_window_s` are both undeclared (`human_remote`) and noted (`x_g1_duplicate_identity`). The README says names are claims, not proof, and that L3 proves remote control. Test: `test_two_writers_claiming_one_identity_at_once_are_both_undeclared__defs_6_10`. |
| S-27 | Low | `connectors/g1.py` `_writer_entry`, `_note_writers` | `x_g1_writers` carries an undeclared writer's program text (up to 256 printable characters) into the sealed log. A changing set of writers can write a note on every tick (about 10 KB every 0.25 s). | Decided (open point 6), fixed in 5fd7386. An undeclared writer's program appears as a pseudonym (first 12 hex digits of the SHA-256), like its host; `discover` prints both pseudonyms and how to compute them. Tests: `test_writers_note_names_no_host__claude_md_product_rule_5`, `test_active_writers_are_noted_on_heartbeats__notes_6`. Notes stay one per change of the writer set; the disk floor (S-15) bounds any flood. |
| S-28 | Low | `connectors/g1_dds.py` `Discovery.report` | `report()` was O(participants × writers): 16 000 of each took 22 s. | Fixed in 1c82f6b. Writers are grouped once. Test: `test_discover_reports_many_endpoints_in_linear_time__s28`. |
| S-29 | Low | `connectors/g1_dds.py` observer | 4096 junk participants evict the robot's own participant record. | Accepted. The robot's writers then count as undeclared, a person or contention, never `ai`: conservative. |
| S-30 | Low | `connectors/g1_dds.py` `convert` | The arm state text was made printable before it was parsed, so JSON containing a newline was ignored. | Fixed in 1c82f6b. The text is bounded and parsed as sent, like a request's parameter. Test: `test_an_arm_state_is_parsed_as_sent__s30`. |
| S-31 | Info | `connectors/g1_dds.py` `TaskFollower` | A symbolic link at a session's `events.jsonl` is followed. | Accepted. Planting one needs write access to the recorder's 0700 folder, which already means control of the log. |

### TurtleBot 4 and the station

| ID | Sev. | Where | What | Status |
|---|---|---|---|---|
| S-41 | Medium | `connectors/tb4.py` `relays` (also used by `connectors/station.py`) | A huge `/cmd_vel_smoothed` from the network made `math.fsum` raise inside `observe()`. The node swallowed the error and dropped every `/cmd_vel` of an unknown publisher, a person's included. The robot then read `ai` (or `unknown`), and the station read `idle`. | Fixed in 411b82c. Twists that are not finite, or whose arithmetic overflows, are no relay. Tests: `test_twists_whose_arithmetic_overflows_are_no_relay__s41`, `test_a_real_relay_is_still_recognised__s41`. |
| S-42 | Medium | `connectors/tb4.py` relay attribution | One `/cmd_vel_smoothed` sample can absorb any number of commands. A person's command pointing the same way as Nav2's counts as the AI's. The resulting `ai` carries no `x_attribution_inferred`. | Decided (open point 7), fixed in 5e4f93f. Each smoothed message accounts for one relayed command, so a person in Nav2's direction is in contention: `shared`, never `ai`. Tests: `test_a_person_driving_in_nav2s_direction_is_shared_never_ai__defs_6_12`, `test_a_relay_counts_once_per_smoothed_message__defs_6_12`. |
| S-45 | Medium | `connectors/ros2/.../station_observer.py`, `connectors/station.py` | The station counts `/joy` from any publisher as its own joystick, with the station's device and actor and no inferred flag. A stranger's `/joy` also keeps the "input lost" rule from firing. | Decided (open point 8), fixed in ce99fc9. `/joy` counts only when every publisher of the topic is a declared station device, with no shared name. Tests: `test_joy_counts_only_from_declared_station_devices__defs_12_1`, `test_two_declared_joysticks_count_without_a_device__defs_12_1_6_12`; the CI ROS witness job runs the node. |
| S-46 | Low | `connectors/tb4.py`, ROS observer | A forged `GoalStatusArray` opens a Nav2 task as clean `ai`. If Nav2 dies mid-task, the task's `ai` stays. | Decided (open point 7), fixed in 5e4f93f. Task status and feedback count only from declared Nav2 nodes. Nav2 leaving the graph, or silent for more than `gap_tolerance_s` mid-task, makes the scope `unknown`. Tests: `test_a_task_status_counts_only_from_nav2_itself__defs_5_6_10`, `test_nav2_silent_mid_task_is_unknown_until_it_is_heard_again__defs_2_6_14`, `test_feedback_from_a_stranger_does_not_keep_a_task_alive__defs_2`, `test_nav2_leaving_the_graph_mid_task_is_unknown_at_once__defs_2_6_14`; the CI ROS jobs run the observer with feedback. |
| S-54 | Low | ROS observer | On rclpy versions with `publisher_gid`, a gid matching no known publisher is still given to the topic's sole publisher. | Accepted. Jazzy, the supported distribution, has no `publisher_gid`; revisit when a newer distribution is supported. |
| S-55 | Low | ROS observer `x_publishers` | Raw node names go into the sealed log unchecked. A heartbeat over 64 KiB is dropped and not resent until the publisher set changes. | Decided (open point 8), fixed in 5e4f93f for names: a node name that is not a valid ROS 2 name of at most 255 characters appears as `pseudonym:<12 hex>`. Tests: `tests/test_connector_names.py`. Residual risk: a publisher map larger than one datagram (many publishers on many topics) is still dropped until it changes. |

### LeRobot

| ID | Sev. | Where | What | Status |
|---|---|---|---|---|
| S-40 | Medium | `connectors/lerobot_io.py`, `connectors/lerobot.py` | A tiny downloaded dataset could make the importer use unbounded memory, CPU and disk. A declared length of 30 million frames wrote 209 MB of heartbeats in 98 s, and 2^62 never finished. A 243 KiB file of 20 million rows took 768 MiB. | Fixed in d0f1ff1. Episodes over 24 h, files over 10 million rows (checked from parquet metadata) and indexes above 2^53 − 1 are refused before writing. Tests: `test_a_forged_episode_length_or_frame_index_is_refused_before_writing__s40`, `test_a_file_declaring_too_many_rows_is_refused_unread__s40`. |
| S-43 | Medium | `connectors/lerobot_io.py` `LogFollower`, `pump` | A log that vanished, was replaced or was cut while the rollout ran (log rotation) left the last state, `ai`, in place for as long as the rollout lived. That is time counted with nothing to show for it (rule 2). | Fixed in d0f1ff1. The open task is closed `unknown`, every scope goes `unknown`, and the note `x_lerobot_log: lost` is written until a line tells the state again. Test: `test_a_log_that_vanishes_or_is_replaced_is_unknown_not_its_last_state__s43`. Residual risk: a log truncated and regrown past the old offset between two reads is not seen. |
| S-44 | Low | `connectors/lerobot.py` `_dispatch` | A line flushed after the rollout's process was known gone re-attached the engine, and its state then held. | Fixed in d0f1ff1. After the process is gone only a new rollout's start is taken. Test: `test_a_late_line_from_a_dead_rollout_never_brings_its_state_back__s44`. |
| S-47 | Low | `connectors/lerobot.py` `parse_line` | Signals are accepted from bare prints and from any logger location, so any `print()` in the rollout process can flip a correction to AI. | Decided (open point 9), fixed in 3dc1d49. Logged signals need LeRobot's log prefix; an unprefixed line counts only as the exact whole line of the one printed message used. Tests: `tests/test_connector_lerobot_source.py`, against `tests/fixtures/lerobot_source/messages.json`, quoted from the pinned LeRobot commit. |
| S-48 | Low | `connectors/lerobot.py` policy actor | The policy path becomes the actor id in the sealed log, including a user name from a home-folder path. | Decided (open point 6), fixed in ed4c97d. The actor is `ai:<device_id>` for a declared policy, else `ai:<pseudonym of the path>`; the golden was updated. Tests: `test_undeclared_sources_carry_no_device__defs_6_10`, `test_a_device_registry_without_the_sources_still_works__defs_6_10`, `test_live_log_matches_the_hand_worked_events__dossier_14_1_defs_8`. |
| S-49 | Low | `connectors/lerobot_io.py` `_robot_id`, `_write_session` | On Windows a `robot_type` of `C:` from `info.json` wrote outside `--out`, and `NUL` or `so101:a` crashed the import. | Fixed in d0f1ff1. A session folder must resolve inside `--out`, and write errors are one line. Test: `test_a_robot_type_that_is_no_windows_folder_is_refused__s49` (Windows). |
| S-50 | Low | `connectors/lerobot_io.py` `_write_session` | Importing into the same `--out` silently replaced earlier session logs. | Fixed in d0f1ff1. An existing session log is never replaced. Test: `test_an_import_never_replaces_a_log_already_written__s50`. Residual risk: an error found only in a later data file's frames leaves the earlier episodes written. |
| S-51 | Low | `connectors/lerobot_io.py` | Huge indexes, impossible times, date columns and write failures escaped `LeRobotError` as generic failures. | Fixed in d0f1ff1. These are now one-line errors. Covered by the S-40 and S-49 tests and by `test_bad_episode_metadata_is_refused_in_one_line`. |
| S-52 | Low | `connectors/lerobot_io.py` `read_info` | A feature name from a dataset's `info.json` reached the error line with ESC, CR and LF. | Fixed in 4297475 (S-09). Missing columns are quoted with `ascii()`. Test: `test_a_dataset_key_with_control_characters_prints_on_one_line__s52`. |
| S-53 | Low | `connectors/lerobot.py` `_open` | Task ids were found by a linear search, so repeated episode starts were quadratic: 8000 took 20 s on the robot's computer. | Fixed in d0f1ff1. A counter per episode replaces the search. Test: `test_repeated_episode_starts_cost_linear_time__s53`. |

### Workflows, release, repository and tools

| ID | Sev. | Where | What | Status |
|---|---|---|---|---|
| S-12 | Low | `.github/workflows` | zizmor: the release workflow had no concurrency group, and its `id-token: write` was undocumented. actionlint: SC2086, an unquoted `$IGNORED` in the audit job. Checked and sound: every action is pinned by a SHA that matches its tag (verified with the GitHub API), permissions default to `contents: read`, `pull_request_target` is not used, checkout keeps no credentials, setup-uv's cache is off on tag pushes, and the build is separated from the `id-token` publish job. | Fixed in edaee95. zizmor now runs in CI (`workflows` job). |
| S-13 | Medium | GitHub repository settings | Found through the API. No `pypi` environment exists, so the first tag would create it without the required reviewers RELEASING.md asks for. `main` has no branch protection and tags have no rules. Private vulnerability reporting, the channel SECURITY.md names, is disabled. Secret scanning and push protection are off. | Open (open point 10): repository settings, an action for the owner before the first tag. |
| S-60 | Info | bandit B101 | 14 `assert` statements in `src`. | Accepted. Each one narrows a type on an invariant just checked (`noqa: S101` with the reason), never on input. |
| S-61 | Info | bandit B704, `report/html.py` | `Markup()` on the figures' SVG. | Accepted. The SVG is drawn from numbers and fixed labels with text as paths; `test_the_figures_hold_no_text_from_the_input__defs_11` checks no input reaches it. |
| S-62 | Info | pip-audit, lowest set | PYSEC-2026-3552 (cryptography < 50, PKCS#7 decryption) and PYSEC-2026-113 (pyarrow < 23.0.1, Arrow IPC files with pre-buffering). | Accepted, as CI already does. Autonometry never decrypts PKCS#7. The pyarrow advisory states the Python bindings cannot reach it, and the importer reads Parquet only. The locked set is clean. |
| S-63 | Info | gitleaks, history | 3 `generic-api-key` hits: a `key_path = "tb4_sim_01.key"` README line and two `key: Ed25519PrivateKey` annotations. | False positives. No private key, token or credential is anywhere in the history. |

## Open points and their decisions

The first pass left ten open points. The owner decided nine of them; the follow-up (branch
`m10b-security-decisions`) implemented each decision with tests. The tenth, the repository
settings, is still the owner's action.

| # | Open point | Decision | Fixed in | Tests |
|---|---|---|---|---|
| 1 | Truncation at a checkpoint (S-14) | Keep the spec's statuses. `verify` warns, naming the time of the last anchor, and the report is not clean. The threat model and README say to anchor often and keep anchor files off the robot. Requiring an anchored `session_end` is proposed for v0.4. | e2ed273 | `test_a_session_cut_back_to_an_anchor_verifies_but_is_warned__s14`, `test_a_report_with_a_cut_session_is_not_clean__s14` |
| 2 | Recorder disk budget (S-15) | Free-space floor `min_free_bytes`, default 1 GiB. Below it the recorder stops writing, logs one error and resumes when space returns; the gap is `unknown`. | 1ca2f09 | `tests/test_recorder_disk.py` |
| 3 | Nesting limit in the spec (S-03) | Keep 128 in code for v0.1 and document it. List it with the other input limits as a proposal for spec v0.4; `spec/` is unchanged. | no code change: README and [`spec-v0.4-proposals.md`](spec-v0.4-proposals.md) | `test_nesting_deeper_than_the_limit_is_rejected_at_any_depth__s03` |
| 4 | G1 emergency stop (S-21) | The stop holds until the robot confirms: a success response from the declared service, or `rt/sportmodestate` leaving passive. A request alone never ends it. | 5fd7386 | `test_a_stop_holds_until_the_robot_confirms_not_on_a_request__defs_6_7` |
| 5 | G1 identities (S-22, S-26) | Responses only from the built-in controller's identity, others ignored and noted. Duplicate identities commanding at once are both undeclared, with a note. The README says names are claims and L3 proves remote control. | 5fd7386 | `test_responses_from_another_identity_are_ignored_and_noted__defs_6_10`, `test_two_writers_claiming_one_identity_at_once_are_both_undeclared__defs_6_10` |
| 6 | Privacy of names (S-27, S-48) | Undeclared G1 programs and LeRobot policy paths become pseudonyms (first 12 hex digits of the SHA-256); a declared source appears as its `device_id`. `discover` and the README say how to compute a pseudonym; goldens updated. | 5fd7386, ed4c97d | `test_writers_note_names_no_host__claude_md_product_rule_5`, `test_live_log_matches_the_hand_worked_events__dossier_14_1_defs_8` |
| 7 | TurtleBot 4 attribution (S-42, S-46) | All three fixed. A person on the relay chain within `contention_window_s` is `shared`, never `ai`. Nav2 status and feedback count only from declared Nav2 nodes. Nav2 leaving the graph or silent for more than `gap_tolerance_s` mid-task makes the scope `unknown`. Goldens were reworked by hand: the input gained Nav2's publisher and feedback, and the expected output does not change. | 5e4f93f | the decision 7 tests in `tests/test_connector_tb4.py` |
| 8 | Station inputs (S-45, S-55) | `/joy` only from declared station devices. Node names are validated (length, ROS 2 name rules) before `x_publishers`, else they appear as a pseudonym. | ce99fc9, 5e4f93f | `test_joy_counts_only_from_declared_station_devices__defs_12_1`, `tests/test_connector_names.py` |
| 9 | LeRobot parser (S-47) | Unprefixed lines count only as exact whole-line matches of the known printed messages; every logged signal needs LeRobot's prefix. Tested against fixtures built from the pinned source. | 3dc1d49 | `tests/test_connector_lerobot_source.py` |
| 10 | Repository settings (S-13) | Still open: an action for the owner. | — | — |

**Open point 10, for the owner, before the first tag:**
- create the `pypi` environment with required reviewers, restricted to tags `v*`;
- protect `main` (pull requests, required CI checks) and add a tag ruleset for `v*`;
- enable private vulnerability reporting
  (`gh api -X PUT repos/Autonometry/autonometry/private-vulnerability-reporting`);
- enable secret scanning with push protection.

## Re-running the review

```
uv run pytest tests/test_security_*.py                       # every kept attack
uv run --no-project --python 3.8 python -m unittest discover -s tests/client
uvx zizmor@1.32.0 --offline --persona auditor .github/workflows
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7
docker run --rm -v "$PWD:/repo:ro" zricethezav/gitleaks:v8.24.0 git /repo --log-opts=--all --redact
uvx --from bandit==1.8.6 bandit -r src clients connectors
```
