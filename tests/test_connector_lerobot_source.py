"""The LeRobot log parser against the messages of the pinned LeRobot source
(``tests/fixtures/lerobot_source``; security review M10, open point 9): a logged signal counts
only with LeRobot's logging prefix, a printed one only as its exact whole line, so a ``print()``
anywhere in the rollout's process cannot pass for a signal."""

import json
from pathlib import Path
from typing import Any

import pytest

from autonometry.connectors import lerobot
from lerobot_helpers import line, src

FIXTURE = Path(__file__).parent / "fixtures" / "lerobot_source" / "messages.json"
SOURCE: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
LOGGED = [m for m in SOURCE["messages"] if m["kind"] == "log"]
PRINTED = [m for m in SOURCE["messages"] if m["kind"] == "print"]
REREAD = "Left arrow key pressed. Exiting loop and rerecord the last episode..."


def _id(message: dict[str, Any]) -> str:
    return f"{message['file'].rsplit('/', 1)[-1]}:{message['line']}"


def test_the_fixture_is_the_pinned_commit_and_renders_its_formats() -> None:
    assert SOURCE["lerobot_commit"] == "7e241bd630a3719a56157a497ce5d08f244784f1"
    for message in SOURCE["messages"]:
        assert message["format"] in message["source"] or message["call"] == "log_say"
        rendered = (
            message["format"] % tuple(message["args"]) if message["args"] else message["format"]
        )
        assert rendered == message["message"]
    assert [m["message"] for m in PRINTED].count(REREAD) == 1


@pytest.mark.parametrize("message", LOGGED, ids=_id)
def test_a_logged_message_counts_with_the_logging_prefix(message: dict[str, Any]) -> None:
    parsed = lerobot.parse_line(line(message["message"], 3))
    assert parsed is not None
    assert parsed.src_ts == src(3)


@pytest.mark.parametrize("message", LOGGED, ids=_id)
def test_a_logged_message_without_the_prefix_counts_for_nothing(message: dict[str, Any]) -> None:
    """A bare print of the same text, indented or not, is not the rollout's logging."""
    assert lerobot.parse_line(message["message"]) is None
    assert lerobot.parse_line("    " + message["message"]) is None


@pytest.mark.parametrize("message", PRINTED, ids=_id)
def test_a_printed_message_counts_only_as_its_exact_whole_line(message: dict[str, Any]) -> None:
    text = message["message"]
    expected = lerobot.Parsed(lerobot.RerecordKey(), None) if text == REREAD else None
    assert lerobot.parse_line(text) == expected
    assert lerobot.parse_line(text + "\r\n") == expected
    for variant in (" " + text, text + " ", text + " now", line(text, 3)):
        assert lerobot.parse_line(variant) is None
