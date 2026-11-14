"""Teleoperation station engine: the station's own input in, station-log states out
(definitions.md 12.1; README, "Station witness").

Pure and standard library only: no rclpy, no input or output, no clock. The ROS 2 node
``station_observer`` in ``connectors/ros2/autonometry_ros`` runs on the operator's PC beside the
station's own recorder (``role = "station"``). It turns ``/cmd_vel``, ``/cmd_vel_smoothed`` and
``/joy`` into the observations below, stamped with their receive time ``t`` (seconds), passes
them to :meth:`StationEngine.observe`, calls :meth:`StationEngine.tick` a few times a second and
sends what both return through the recorder client.

Each witnessed scope is, by the first rule that applies:

1. ``unknown`` while the station's joystick, once seen, has sent nothing on ``/joy`` for
   ``gap_tolerance_s``: the station lost its own input (6.14).
2. ``human_remote`` while an input of the station is live: its latest command, at most
   ``goal_release_s`` old, is outside the dead zone (6.11). The inputs are ``/cmd_vel`` from a
   station device (``teleop_twist_keyboard`` or ``teleop_twist_joy`` on this PC) and ``/joy``
   with a dead-man button held and a stick outside the dead zone. A neutral command, centred
   sticks or a released dead-man ends it at once.
3. ``idle`` otherwise.

A station device is a ``human`` device of the station's registry (definitions.md 6.10). A
``/cmd_vel`` message counts when its publisher, known from the message or as the topic's only
publisher, is a station device. When the publisher cannot be told, it counts when a station
device is among the topic's publishers and the command does not relay Nav2's latest
``/cmd_vel_smoothed``; the state then carries ``x_attribution_inferred: true``, which makes the
station log trust level L1 (6.12) and so gives no L3: a witness that guesses cannot vouch.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from autonometry.connectors.tb4 import (
    CmdVel,
    CmdVelSmoothed,
    Device,
    Source,
    StateAssertion,
    neutral,
    relays,
)

__all__ = ["JOY_NODE", "StationEngine", "StationJoy", "StationObservation", "StationSettings"]

_HUMAN: Final = "human"
_REASON: Final = "human_initiated"
#: The node that publishes ``/joy`` from a joystick on the station (ROS 2 ``joy`` package).
JOY_NODE: Final = Source(node="/joy_node")


@dataclass(frozen=True, slots=True)
class StationSettings:
    """What the station engine needs, seconds unless stated. ``goal_release_s`` and
    ``gap_tolerance_s`` come from the spec's ``rules.toml`` (definitions.md 9); ``devices`` from
    the station's device registry."""

    devices: tuple[Device, ...]
    goal_release_s: float
    gap_tolerance_s: float
    scopes: tuple[str, ...] = ("base",)
    #: Sticks within this distance of centre are neutral (unit of the joystick's axes).
    dead_zone: float = 0.05
    deadman_buttons: tuple[int, ...] = (4, 5)
    #: The node that publishes the station's ``/joy``.
    joy_source: Source = JOY_NODE
    #: Longest time between a ``/cmd_vel_smoothed`` message and the ``/cmd_vel`` relaying it.
    smoothed_match_s: float = 0.25
    unidentified_actor_id: str = "person:unidentified"

    def __post_init__(self) -> None:
        if not self.scopes:
            raise ValueError("scopes must name at least one scope")
        windows = {
            "goal_release_s": self.goal_release_s,
            "gap_tolerance_s": self.gap_tolerance_s,
            "dead_zone": self.dead_zone,
            "smoothed_match_s": self.smoothed_match_s,
        }
        for name, value in windows.items():
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a positive number")

    @classmethod
    def from_params(
        cls, params: Any, devices: Iterable[Device], **options: Any
    ) -> "StationSettings":
        """Settings from the spec's parameter table (``autonometry.spec.Params`` or any object
        with the same attributes) and the station registry's devices."""
        return cls(
            devices=tuple(devices),
            goal_release_s=float(params.goal_release_s),
            gap_tolerance_s=float(params.gap_tolerance_s),
            **options,
        )


@dataclass(frozen=True, slots=True)
class StationJoy:
    """``/joy`` on the station: its axes and buttons (1 pressed, 0 not)."""

    t: float
    axes: tuple[float, ...]
    buttons: tuple[int, ...]


StationObservation = CmdVel | CmdVelSmoothed | StationJoy


@dataclass(slots=True)
class _Input:
    """The latest command of one station input: when, whether outside the dead zone, and who."""

    t: float
    live: bool
    device: Device | None
    inferred: bool


class StationEngine:
    """Decides the station's state for each witnessed scope; see the module docstring."""

    def __init__(self, settings: StationSettings) -> None:
        self._s = settings
        self._now = 0.0
        self._inputs: dict[str, _Input] = {}
        self._joy_seen: float | None = None
        self._smoothed: tuple[float, tuple[float, float, float]] | None = None
        self._sent: StateAssertion | None = None

    def observe(self, observation: StationObservation) -> list[StateAssertion]:
        """Take one observation; return the state of every scope when it changed."""
        self._now = max(self._now, observation.t)
        match observation:
            case CmdVel():
                self._on_cmd_vel(observation)
            case CmdVelSmoothed():
                self._smoothed = (
                    observation.t,
                    (observation.linear_x, observation.linear_y, observation.angular_z),
                )
            case StationJoy():
                self._on_joy(observation)
        value = self._decide()
        if value == self._sent:
            return []
        self._sent = value
        return self._each_scope(value)

    def tick(self, now: float) -> list[StateAssertion]:
        """Let time pass to ``now`` (the clock of the observations); return the state of every
        scope, which the recorder needs at least every second."""
        self._now = max(self._now, now)
        self._sent = self._decide()
        return self._each_scope(self._sent)

    # -- inputs -----------------------------------------------------------------------------

    def _device(self, source: Source) -> Device | None:
        """The station device ``source`` is: a ``human`` device of the registry, else None."""
        found = next((d for d in self._s.devices if d.matches(source)), None)
        return found if found is not None and found.kind == _HUMAN else None

    def _on_cmd_vel(self, observation: CmdVel) -> None:
        values = (observation.linear_x, observation.linear_y, observation.angular_z)
        if observation.source is not None:
            device = self._device(observation.source)
            if device is not None:
                self._inputs[device.device_id] = _Input(
                    observation.t, not neutral(*values), device, inferred=False
                )
            return
        mine = {d.device_id: d for c in observation.candidates if (d := self._device(c))}
        if not mine or self._relayed(observation.t, values):
            return
        device = next(iter(mine.values())) if len(mine) == 1 else None
        key = device.device_id if device is not None else "cmd_vel:inferred"
        self._inputs[key] = _Input(observation.t, not neutral(*values), device, inferred=True)

    def _relayed(self, t: float, values: tuple[float, float, float]) -> bool:
        smoothed = self._smoothed
        return (
            smoothed is not None
            and abs(t - smoothed[0]) <= self._s.smoothed_match_s
            and relays(values, smoothed[1])
        )

    def _on_joy(self, observation: StationJoy) -> None:
        self._joy_seen = observation.t
        buttons = observation.buttons
        held = any(0 <= i < len(buttons) and buttons[i] == 1 for i in self._s.deadman_buttons)
        moved = any(abs(a) > self._s.dead_zone for a in observation.axes if math.isfinite(a))
        device = self._device(self._s.joy_source)
        self._inputs["joy"] = _Input(observation.t, held and moved, device, inferred=False)

    # -- state ------------------------------------------------------------------------------

    def _decide(self) -> StateAssertion:
        scope = self._s.scopes[0]
        if self._joy_seen is not None and self._now - self._joy_seen > self._s.gap_tolerance_s:
            return StateAssertion(scope, "unknown", fields=(("x_input_lost", "joy"),))
        live = [
            i for i in self._inputs.values() if i.live and self._now - i.t <= self._s.goal_release_s
        ]
        if not live:
            return StateAssertion(scope, "idle")
        latest = max(live, key=lambda i: (i.t, i.device.device_id if i.device else ""))
        device = latest.device
        actor = device.actor_id if device is not None and device.actor_id else None
        inferred = any(i.inferred for i in live)
        return StateAssertion(
            scope,
            "human_remote",
            _REASON,
            actor or self._s.unidentified_actor_id,
            None if device is None else device.device_id,
            (("x_attribution_inferred", True),) if inferred else (),
        )

    def _each_scope(self, value: StateAssertion) -> list[StateAssertion]:
        return [
            StateAssertion(
                scope, value.state, value.reason, value.actor_id, value.device_id, value.fields
            )
            for scope in self._s.scopes
        ]
