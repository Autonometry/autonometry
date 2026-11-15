# Messages of the pinned LeRobot source

`messages.json` lists every message `autonometry.connectors.lerobot` reads from a `lerobot-rollout`
log, plus the other lines the rollout prints instead of logging. Each entry quotes the call at
LeRobot commit `7e241bd630a3719a56157a497ce5d08f244784f1` (v0.6.1): its file and line, the call
(`logger.info`, `log_say`, which calls `logging.info` at `lerobot/utils/utils.py:148`, or
`print`), its source text, the format string, sample arguments and the message they render.

It was built once by fetching those files at that commit (`gh api -H "Accept:
application/vnd.github.raw" repos/huggingface/lerobot/contents/<path>?ref=<commit>`) and copying
each call verbatim. `tests/test_connector_lerobot_source.py` checks that every logged message is
read only with LeRobot's logging prefix, and every printed one only as its exact whole line
(security review M10, open point 9). Rebuild it when the pinned commit changes.
