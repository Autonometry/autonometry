"""Example: a teleoperation station witness (Autonometry definitions.md section 12.1).

Run this beside the station's own recorder: ``autonometry record`` with ``role = "station"``,
``station_id`` and the ``robot_id`` of the robot the station commands, under the station's own
key. The station then keeps its own sealed log, which ``autonometry reconcile`` compares with the
robot's (trust level L3).

For each witnessed scope it asserts:

- ``human_remote`` while the operator holds control and their input is live: the latest reading,
  at most ``goal_release_s`` (1 s) old, is outside the dead zone. A neutral reading or giving up
  control ends it at once (section 6.11);
- ``unknown`` while the input device has not reported for ``lost_after_s`` (section 6.14);
- ``idle`` otherwise.

Standard library only, Python 3.8 or newer: copy it next to ``autonometry_client.py``. Every
call returns at once and never raises, like the client. Replace ``read_input`` in ``main`` with
your teleoperation stack's input (joystick axes, a VR controller's pose deltas, ...).

Licensed under the Apache License, Version 2.0.
"""

import time
from typing import Any, Callable, Optional, Sequence, Tuple

from autonometry_client import Recorder

#: The reason and the state a live operator gives (definitions.md 3, 12.1).
REASON = "human_initiated"


class StationWitness:
    """Turns an operator's input readings into the station's states.

    Args:
        recorder: an ``autonometry_client.Recorder`` connected to the station's recorder.
        scopes: the robot scopes this station witnesses (the station recorder's ``scopes``).
        device_id: the input device, as declared in the station's device registry.
        actor_id: the operator's pseudonymous id, or None.
        dead_zone: readings within this distance of centre are neutral.
        goal_release_s: how long a live reading counts, seconds.
        lost_after_s: silence after which the device counts as lost, seconds.
    """

    def __init__(  # noqa: PLR0913, PLR0917 -- the knobs of a station
        self,
        recorder: Any,
        scopes: Sequence[str],
        device_id: str,
        actor_id: Optional[str] = None,
        dead_zone: float = 0.05,
        goal_release_s: float = 1.0,
        lost_after_s: float = 2.0,
    ) -> None:
        self._recorder = recorder
        self._scopes = tuple(scopes)
        self._device_id = device_id
        self._actor_id = actor_id
        self._dead_zone = dead_zone
        self._goal_release_s = goal_release_s
        self._lost_after_s = lost_after_s
        self._last_report = None  # type: Optional[float]
        self._last_live = None  # type: Optional[float]

    def report(self, t: float, values: Sequence[float], holds_control: bool = True) -> None:
        """Take one reading of the input device at ``t`` (monotonic seconds) and assert."""
        try:
            self._last_report = t
            live = holds_control and any(abs(float(v)) > self._dead_zone for v in values)
            self._last_live = t if live else None
        except Exception:  # never raise into the station's code
            self._last_live = None
        self.tick(t)

    def lost(self, t: float) -> None:
        """The input device is gone (unplugged, tracking lost): ``unknown`` from now on."""
        self._last_report = None
        self._last_live = None
        self.tick(t)

    def state(self, t: float) -> str:
        """The state of every witnessed scope at ``t``."""
        if self._last_report is None or t - self._last_report > self._lost_after_s:
            return "unknown"
        if self._last_live is not None and t - self._last_live <= self._goal_release_s:
            return "human_remote"
        return "idle"

    def tick(self, t: float) -> None:
        """Assert the current state of every scope; call it at least once a second."""
        state = self.state(t)
        for scope in self._scopes:
            if state == "human_remote":
                self._recorder.state(
                    scope, state, REASON, self._actor_id or "person:unidentified", self._device_id
                )
            else:
                self._recorder.state(scope, state)


def main(
    read_input: Callable[[], Optional[Tuple[Sequence[float], bool]]],
    socket_path: str = "/run/autonometry/station.sock",
    period_s: float = 0.05,
) -> None:
    """Poll the input every ``period_s`` seconds until interrupted. ``read_input`` returns the
    readings and whether the operator holds control, or None when the device is gone."""
    with Recorder(socket_path, "station_example") as recorder:
        witness = StationWitness(recorder, ["base"], "joystick_pc_1", "op_01")
        try:
            while True:
                reading = read_input()
                now = time.monotonic()
                if reading is None:
                    witness.lost(now)
                else:
                    witness.report(now, reading[0], reading[1])
                time.sleep(period_s)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    # A stand-in device: centred sticks, control held. Replace with your input.
    main(lambda: ((0.0, 0.0), True))
