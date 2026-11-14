"""Tests for clients/python/station_example.py (definitions.md 12.1, 6.11, 6.14).

unittest and Python 3.8 syntax, like test_client.py, so CI runs them on Python 3.8 too.
"""

import os
import sys
import unittest
from typing import Any, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "clients", "python"))

from station_example import StationWitness  # noqa: E402

Call = Tuple[str, str, Optional[str], Optional[str], Optional[str]]


class FakeRecorder:
    def __init__(self) -> None:
        self.calls = []  # type: List[Call]

    def state(
        self,
        scope: str,
        state: str,
        reason: Optional[str] = None,
        actor_id: Optional[str] = None,
        device_id: Optional[str] = None,
        **fields: Any,
    ) -> None:
        self.calls.append((scope, state, reason, actor_id, device_id))


class StationWitnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = FakeRecorder()
        self.witness = StationWitness(self.recorder, ["base", "arm"], "joy_pc_1", "op_01")

    def last(self) -> List[Call]:
        return self.recorder.calls[-2:]

    def test_unknown_before_the_first_reading(self) -> None:
        self.witness.tick(0.0)
        self.assertEqual([c[1] for c in self.last()], ["unknown", "unknown"])

    def test_live_input_is_human_remote_until_goal_release(self) -> None:
        self.witness.report(10.0, [0.5, 0.0])
        self.assertEqual(
            self.last(),
            [
                ("base", "human_remote", "human_initiated", "op_01", "joy_pc_1"),
                ("arm", "human_remote", "human_initiated", "op_01", "joy_pc_1"),
            ],
        )
        self.assertEqual(self.witness.state(11.0), "human_remote")
        self.assertEqual(self.witness.state(11.1), "idle")

    def test_neutral_reading_or_released_control_ends_at_once(self) -> None:
        self.witness.report(10.0, [0.5])
        self.witness.report(10.1, [0.01])
        self.assertEqual(self.witness.state(10.1), "idle")
        self.witness.report(10.2, [0.5], holds_control=False)
        self.assertEqual(self.witness.state(10.2), "idle")

    def test_silence_or_a_lost_device_is_unknown(self) -> None:
        self.witness.report(10.0, [0.0])
        self.assertEqual(self.witness.state(12.0), "idle")
        self.assertEqual(self.witness.state(12.1), "unknown")
        self.witness.report(13.0, [0.0])
        self.witness.lost(13.5)
        self.assertEqual([c[1] for c in self.last()], ["unknown", "unknown"])

    def test_bad_reading_never_raises(self) -> None:
        self.witness.report(10.0, ["not a number"])  # type: ignore[list-item]
        self.assertEqual(self.witness.state(10.0), "idle")

    def test_no_actor_is_unidentified(self) -> None:
        recorder = FakeRecorder()
        StationWitness(recorder, ["base"], "kbd").report(1.0, [1.0])
        self.assertEqual(recorder.calls[-1][3], "person:unidentified")


if __name__ == "__main__":
    unittest.main()
