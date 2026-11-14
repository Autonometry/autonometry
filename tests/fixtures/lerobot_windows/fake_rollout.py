"""A fake ``lerobot-rollout`` that made the logs in this folder (README.md beside it): LeRobot
v0.6.1's log lines on stderr in ``init_logging``'s format (lerobot/utils/utils.py:62-73), and the
re-record key line as a print on stdout (lerobot/utils/keyboard_input.py:161)."""

import logging

BEFORE = (
    "Building rollout context...",
    "Loading policy from 'outputs/train/act_so101/checkpoints/last/pretrained_model'...",
    "Connecting teleoperator (so101_leader)...",
    "Dataset ready: op/rollout_cube_20261114_100000 (0 existing episodes)",
    "DAgger continuous recording started (episode_duration=60s)",
    "Phase transition: autonomous -> paused",
    "Phase transition: paused -> correcting",
    "Phase transition: correcting -> paused",
    "Phase transition: paused -> autonomous",
    "Episode saved (total: 1, elapsed: 60.0s)",
    "Recording episode 1",
    "Reset the environment",
)
AFTER = (
    "Re-record episode",
    "DAgger continuous control loop ended — pausing engine",
    "Episodic control loop ended — saving any in-progress episode",
    "Sentry control loop ended — saving final episode",
    "Stopping inference engine...",
    "Returning robot to initial position before shutdown...",
    "Disconnecting robot...",
    "Rollout finished",
)


class LeRobotFormatter(logging.Formatter):
    """LeRobot's formatter: the last 15 characters of ``path:line`` as the location."""

    def format(self, record: logging.LogRecord) -> str:
        record.lerobot_location = f"{record.pathname}:{record.lineno}"[-15:]
        record.lerobot_pid = ""
        return super().format(record)


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        LeRobotFormatter(
            "%(levelname)s %(lerobot_pid)s%(asctime)s %(lerobot_location)15s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    log = logging.getLogger("lerobot")
    for message in BEFORE:
        log.info(message)
    print("Left arrow key pressed. Exiting loop and rerecord the last episode...")
    for message in AFTER:
        log.info(message)


if __name__ == "__main__":
    main()
