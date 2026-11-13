"""TurtleBot 4 engine: ROS 2 observations in, recorder messages out (README, "TurtleBot 4").

Pure and standard library only: no rclpy, no input or output, no clock. The ROS 2 node in
``connectors/ros2/autonometry_ros`` turns each message into one of the observation classes below,
stamped with its receive time ``t`` (seconds), passes it to :meth:`Tb4Engine.observe`, calls
:meth:`Tb4Engine.tick` a few times a second, and sends what they return, in order, through the
recorder client. The rules are the integration notes' table B.2 (``research/turtlebot4``) under
spec v0.3; the state of the one motion scope, ``base``, is decided by the first that applies:

1. Kidnapped for at least ``kidnap_min_s``: ``human_local`` through the manual-handling device
   (notes UC-20; definitions.md 6.15, moving the robot by hand).
2. EStop from ``/function_calls`` or wheels disabled: ``safety_stop`` triggered by a person, with
   the button's device when one was pressed (6.7, UC-7).
3. A running dock, undock or wall-follow action: ``shared`` with reason ``human_initiated`` when a
   button or a person started it, ``ai`` or ``scripted`` when a declared program did (6.13, UC-3).
4. ``/cmd_vel`` from a person: ``human_local`` or ``human_remote`` by the device registry, an
   undeclared publisher ``human_remote`` (6.10); with another source within
   ``contention_window_s``, ``shared`` with reason ``human_initiated`` (6.12, UC-1). A neutral
   command or a joystick dead-man release ends it (6.11, UC-14); otherwise it lasts
   ``goal_release_s`` after the last command.
5. Motion on ``/odom`` with no command for ``gap_tolerance_s``: ``unknown`` with
   ``x_out_of_band`` (notes B.6 item 5).
6. An open Nav2 task: ``safety_stop`` while the collision monitor says STOP (automatic, no actor);
   ``shared`` while a person's re-goal or initial pose is not yet consumed (``hint_as_shared``)
   or after a person cancelled the goal; else ``ai`` (UC-8, 6.2, 6.8).
7. ``/cmd_vel`` from a declared AI or script outside a task: ``ai`` or ``scripted``.
8. No task and no command for ``idle_threshold_s``: ``idle``, docked or not (section 2).
9. After the wheels are enabled again with no commander yet: ``unknown`` (UC-7).
10. Otherwise the last state of rule 1, 3, 4, 6, 7 or 8, or the pose estimate of a person
    before a task (``human_remote`` or ``human_local``, reason ``planned``, notes UC-8 e):
    outside an open task, a scope nobody commands keeps its last controller's state until it is
    idle, and stays idle until something commands it (6.14).

Nav2 goals open and close tasks: a goal ``EXECUTING`` with no task open gives ``task_start``;
``SUCCEEDED``, ``ABORTED`` and ``CANCELED`` give ``task_end`` with outcome ``success``,
``failure`` and ``aborted``. Under ``hint_as_event``, a person's re-goal or cancel is a ``hint``
event; under ``hint_as_shared`` a ``hint`` event is sent only when a higher rule hides the shared
span, so the person's input is never lost. Attribution taken from the ROS graph instead of the
message itself carries ``x_attribution_inferred: true`` (6.12).
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal

__all__ = [
    "ABORTED",
    "ACCEPTED",
    "CANCELED",
    "CANCELING",
    "COLLISION_STOP",
    "EXECUTING",
    "NAVIGATE",
    "ROUTINE_CALLS",
    "SUCCEEDED",
    "ActionStatus",
    "ButtonEdge",
    "CmdVel",
    "CmdVelSmoothed",
    "CollisionMonitorState",
    "Device",
    "Distance",
    "DockStatus",
    "FunctionCall",
    "GoalStatus",
    "Hint",
    "InitialPose",
    "Joy",
    "KidnapStatus",
    "Message",
    "Observation",
    "Odometry",
    "Settings",
    "Source",
    "StateAssertion",
    "TaskEnd",
    "TaskStart",
    "Tb4Engine",
    "WheelStatus",
    "devices_from_registry",
    "wire_event",
]

#: ``action_msgs/msg/GoalStatus`` codes.
ACCEPTED: Final = 1
EXECUTING: Final = 2
CANCELING: Final = 3
SUCCEEDED: Final = 4
CANCELED: Final = 5
ABORTED: Final = 6
_OUTCOMES: Final = MappingProxyType({SUCCEEDED: "success", ABORTED: "failure", CANCELED: "aborted"})
_PENDING: Final = frozenset({ACCEPTED, EXECUTING})

#: ``nav2_msgs/msg/CollisionMonitorState.action_type`` for STOP.
COLLISION_STOP: Final = 1

#: The Nav2 action whose goals are tasks.
NAVIGATE: Final = "navigate_to_pose"
#: Create 3 actions a button or a program can start, and the ``/function_calls`` names of the
#: TurtleBot 4 buttons and menu that start them.
ROUTINE_CALLS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "dock": frozenset({"Dock"}),
        "undock": frozenset({"Undock"}),
        "wall_follow": frozenset({"Wall Follow Left", "Wall Follow Right"}),
    }
)
_ESTOP: Final = "EStop"
_NEUTRAL: Final = 1e-6
#: Rounding allowed on the collision monitor's scale factor, which lies in [0, 1].
_SCALE_SLACK: Final = 1e-6
#: Most commands waiting for their ``/cmd_vel_smoothed`` match.
_MAX_PENDING: Final = 256
_HINT_POLICIES: Final = frozenset({"hint_as_shared", "hint_as_event"})
_MATCH_FIELDS: Final = ("host", "node", "topic", "participant")

Role = Literal["human", "ai", "script"]


# -- identities ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Source:
    """An identity the observer saw, in the fields of a device registry ``match``
    (definitions.md 6.10): fully qualified ROS node name, topic, host or DDS participant."""

    node: str | None = None
    topic: str | None = None
    host: str | None = None
    participant: str | None = None


@dataclass(frozen=True, slots=True)
class Device:
    """One device of the registry (definitions.md 6.10); ``match`` holds (field, value) pairs."""

    device_id: str
    kind: str
    match: tuple[tuple[str, str], ...]
    location: str | None = None
    actor_id: str | None = None

    def matches(self, source: Source) -> bool:
        """True when every ``match`` field equals what was observed."""
        return all(getattr(source, name, None) == value for name, value in self.match)


def devices_from_registry(registry: Mapping[str, Any]) -> tuple[Device, ...]:
    """The devices of a registry document already valid against ``device_registry.schema.json``,
    in order, so that the first match wins (definitions.md 6.10)."""
    devices = []
    for entry in registry["devices"]:
        match = tuple(
            (name, entry["match"][name]) for name in _MATCH_FIELDS if name in entry["match"]
        )
        devices.append(
            Device(
                entry["device_id"],
                entry["kind"],
                match,
                entry.get("location"),
                entry.get("actor_id"),
            )
        )
    return tuple(devices)


# -- settings -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    """What the engine needs, seconds unless stated. The first six come from the spec's
    ``rules.toml`` (definitions.md 9); the rest describe the TurtleBot 4 and default to its stock
    names (integration notes A, B.2)."""

    devices: tuple[Device, ...]
    idle_threshold_s: float
    gap_tolerance_s: float
    goal_release_s: float
    contention_window_s: float
    hint_ttl_s: float
    simultaneity_window_s: float
    hint_policy: str = "hint_as_shared"
    scope: str = "base"
    nav_task_definition_id: str = "nav_to_pose"
    initial_pose_plan_item_id: str = "initial_pose"
    nav2_actor_id: str = "ai:nav2"
    #: The node that relays Nav2's commands to ``/cmd_vel`` (the topic-level rule's Nav2).
    nav2_source: Source = Source(node="/collision_monitor")
    #: The node whose action goals stand for the buttons and menu (``/function_calls``).
    function_caller: Source = Source(node="/turtlebot4_node")
    #: The joystick teleop node: its dead-man release ends its control; its buttons are its device.
    joy_teleop: Source = Source(node="/teleop_twist_joy_node")
    #: The identity of a person handling the robot (kidnap).
    kidnap_source: Source = Source(topic="/kidnap_status")
    unidentified_actor_id: str = "person:unidentified"
    #: Longest time from a button or ``/function_calls`` to the action it started.
    trigger_window_s: float = 2.0
    kidnap_min_s: float = 1.0
    deadman_buttons: tuple[int, ...] = (4, 5)
    #: Longest time between a ``/cmd_vel_smoothed`` message and the ``/cmd_vel`` relaying it.
    smoothed_match_s: float = 0.25
    #: Odometry speeds above these are motion (metres per second, radians per second).
    motion_linear_mps: float = 0.02
    motion_angular_rps: float = 0.1
    #: Longer odometry steps are pose resets, not travel (metres).
    max_odom_step_m: float = 1.0

    def __post_init__(self) -> None:
        if self.hint_policy not in _HINT_POLICIES:
            raise ValueError(f"hint_policy must be one of {sorted(_HINT_POLICIES)}")
        windows = {
            "idle_threshold_s": self.idle_threshold_s,
            "gap_tolerance_s": self.gap_tolerance_s,
            "goal_release_s": self.goal_release_s,
            "contention_window_s": self.contention_window_s,
            "hint_ttl_s": self.hint_ttl_s,
            "simultaneity_window_s": self.simultaneity_window_s,
            "trigger_window_s": self.trigger_window_s,
            "kidnap_min_s": self.kidnap_min_s,
            "smoothed_match_s": self.smoothed_match_s,
            "motion_linear_mps": self.motion_linear_mps,
            "motion_angular_rps": self.motion_angular_rps,
            "max_odom_step_m": self.max_odom_step_m,
        }
        for name, value in windows.items():
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a positive number")

    @classmethod
    def from_params(cls, params: Any, devices: Iterable[Device], **options: Any) -> "Settings":
        """Settings from the spec's parameter table (``autonometry.spec.Params`` or any object
        with the same attributes) and the registry's devices; ``options`` set the rest."""
        return cls(
            devices=tuple(devices),
            idle_threshold_s=float(params.idle_threshold_s),
            gap_tolerance_s=float(params.gap_tolerance_s),
            goal_release_s=float(params.goal_release_s),
            contention_window_s=float(params.contention_window_s),
            hint_ttl_s=float(params.hint_ttl_s),
            simultaneity_window_s=float(params.simultaneity_window_s),
            **options,
        )


# -- observations -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CmdVel:
    """A velocity command on ``/cmd_vel`` (metres per second, radians per second). ``source`` is
    its publisher when known from the message or because it is the topic's only publisher;
    otherwise ``candidates`` lists the topic's publishers in the ROS graph."""

    t: float
    linear_x: float
    linear_y: float
    angular_z: float
    source: Source | None = None
    candidates: tuple[Source, ...] = ()


@dataclass(frozen=True, slots=True)
class CmdVelSmoothed:
    """Nav2's smoothed command on ``/cmd_vel_smoothed``, which the collision monitor relays."""

    t: float
    linear_x: float
    linear_y: float
    angular_z: float


@dataclass(frozen=True, slots=True)
class GoalStatus:
    """One goal of an action's status array: its id (hex) and ``action_msgs`` status code."""

    goal_id: str
    status: int


@dataclass(frozen=True, slots=True)
class ActionStatus:
    """``/<action>/_action/status``; ``requesters`` are the action's clients in the ROS graph
    (the observer excluded): who may have sent a goal or cancel."""

    t: float
    action: str
    goals: tuple[GoalStatus, ...]
    requesters: tuple[Source, ...] = ()


@dataclass(frozen=True, slots=True)
class CollisionMonitorState:
    """``/collision_monitor_state``: the action the collision monitor applies."""

    t: float
    action_type: int


@dataclass(frozen=True, slots=True)
class FunctionCall:
    """``/function_calls``: a TurtleBot 4 function a button or the menu started."""

    t: float
    name: str


@dataclass(frozen=True, slots=True)
class ButtonEdge:
    """A button pressed or released; ``source`` is its identity in the registry, for example
    ``Source(topic="/interface_buttons/button_2")``."""

    t: float
    source: Source
    pressed: bool


@dataclass(frozen=True, slots=True)
class Joy:
    """``/joy``: the controller's buttons (1 pressed, 0 not)."""

    t: float
    buttons: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WheelStatus:
    """``/wheel_status.wheels_enabled``."""

    t: float
    wheels_enabled: bool


@dataclass(frozen=True, slots=True)
class DockStatus:
    """``/dock_status.is_docked``."""

    t: float
    is_docked: bool


@dataclass(frozen=True, slots=True)
class KidnapStatus:
    """``/kidnap_status.is_kidnapped``."""

    t: float
    is_kidnapped: bool


@dataclass(frozen=True, slots=True)
class InitialPose:
    """A pose estimate on ``/initialpose``; ``source`` and ``candidates`` as for :class:`CmdVel`."""

    t: float
    source: Source | None = None
    candidates: tuple[Source, ...] = ()


@dataclass(frozen=True, slots=True)
class Odometry:
    """``/odom``: position (metres) and speeds (metres per second, radians per second)."""

    t: float
    x: float
    y: float
    linear_speed: float
    angular_speed: float


Observation = (
    CmdVel
    | CmdVelSmoothed
    | ActionStatus
    | CollisionMonitorState
    | FunctionCall
    | ButtonEdge
    | Joy
    | WheelStatus
    | DockStatus
    | KidnapStatus
    | InitialPose
    | Odometry
)


# -- messages -----------------------------------------------------------------------------------

Fields = tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class StateAssertion:
    """The current state of a scope, for ``Recorder.state`` (definitions.md 2, 3). ``fields``
    holds ``plan_item_id`` and ``x_`` extensions as (name, value) pairs."""

    scope: str
    state: str
    reason: str | None = None
    actor_id: str | None = None
    device_id: str | None = None
    fields: Fields = ()


@dataclass(frozen=True, slots=True)
class TaskStart:
    """A ``task_start`` event (definitions.md 1, 5)."""

    task_id: str
    task_definition_id: str
    fields: Fields = ()


@dataclass(frozen=True, slots=True)
class TaskEnd:
    """A ``task_end`` event with its outcome (definitions.md 5)."""

    task_id: str
    outcome: str
    fields: Fields = ()


@dataclass(frozen=True, slots=True)
class Hint:
    """A discrete ``hint`` event (definitions.md 6.2)."""

    scope: str
    actor_id: str
    device_id: str | None
    fields: Fields = ()


@dataclass(frozen=True, slots=True)
class Distance:
    """The odometer: metres travelled since the engine started, never decreasing."""

    metres: float


Message = StateAssertion | TaskStart | TaskEnd | Hint | Distance


def wire_event(message: StateAssertion | TaskStart | TaskEnd | Hint) -> dict[str, Any]:
    """The event a message stands for, as a connector sends it (README, "Wire format"): no
    ``ts``, ``seq`` or ids the recorder writes. A :class:`Distance` goes to
    ``Recorder.distance`` instead."""
    event: dict[str, Any]
    match message:
        case StateAssertion():
            event = {
                "type": "transition",
                "scope": message.scope,
                "state": message.state,
                "reason": message.reason,
                "actor_id": message.actor_id,
            }
            if message.device_id is not None:
                event["device_id"] = message.device_id
        case TaskStart():
            event = {
                "type": "task_start",
                "task_id": message.task_id,
                "task_definition_id": message.task_definition_id,
            }
        case TaskEnd():
            event = {"type": "task_end", "task_id": message.task_id, "outcome": message.outcome}
        case Hint():
            event = {"type": "hint", "scope": message.scope, "actor_id": message.actor_id}
            if message.device_id is not None:
                event["device_id"] = message.device_id
    event.update(message.fields)
    return event


# -- engine state -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Person:
    """Who did something: a device (None when undeclared), the actor and whether the identity
    was inferred from the ROS graph rather than seen on the message."""

    device: Device | None
    actor_id: str
    inferred: bool


@dataclass(slots=True)
class _Commander:
    device: Device | None
    source: Source | None
    inferred: bool
    last_nonzero: float | None = None
    ended: bool = True


@dataclass(slots=True)
class _Hint:
    kind: str
    person: _Person
    t: float
    until_task_end: bool


@dataclass(slots=True)
class _Nav:
    task_id: str
    goal_id: str
    executing_since: float | None
    hint: _Hint | None = None


@dataclass(slots=True)
class _Routine:
    goal_id: str
    value: StateAssertion


@dataclass(slots=True)
class _Buttons:
    """The latest edge of each button: (pressed, time)."""

    edges: dict[Source, tuple[bool, float]] = field(default_factory=dict)


def _neutral(*values: float) -> bool:
    return all(abs(v) < _NEUTRAL for v in values)


def _relays(command: Sequence[float], smoothed: Sequence[float]) -> bool:
    """True when ``command`` is ``smoothed`` scaled by a factor in [0, 1], as the collision
    monitor's slowdown and stop produce (the notes' topic-level rule, section A)."""
    norm = math.fsum(s * s for s in smoothed)
    if norm < _NEUTRAL * _NEUTRAL:
        return _neutral(*command)
    factor = math.fsum(c * s for c, s in zip(command, smoothed, strict=True)) / norm
    if not -_SCALE_SLACK <= factor <= 1 + _SCALE_SLACK:
        return False
    error = math.sqrt(
        math.fsum((c - factor * s) ** 2 for c, s in zip(command, smoothed, strict=True))
    )
    return error <= 1e-3 + 1e-3 * math.sqrt(norm)


class Tb4Engine:
    """Decides the TurtleBot 4's control state and events; see the module docstring."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._start: float | None = None
        self._now = 0.0
        self._sent: StateAssertion | None = None
        self._last_control: StateAssertion | None = None
        self._last_command: float | None = None
        self._last_motion: float | None = None
        self._released = False
        self._commanders: dict[str, _Commander] = {}
        self._smoothed: tuple[float, tuple[float, float, float]] | None = None
        self._pending: list[CmdVel] = []
        self._goals: dict[str, dict[str, int]] = {}
        self._nav: _Nav | None = None
        self._routine: _Routine | None = None
        self._collision_stop = False
        self._estop: _Person | None = None
        self._estop_trigger = ""
        self._estop_since = 0.0
        self._wheels_enabled: bool | None = None
        self._kidnap_since: float | None = None
        self._docked: bool | None = None
        self._buttons = _Buttons()
        self._joy_held = False
        self._calls: dict[str, tuple[float, _Person]] = {}
        self._odom: tuple[float, float] | None = None
        self._speed: tuple[float, float, float] | None = None
        self._distance = 0.0

    # -- public -----------------------------------------------------------------------------

    def observe(self, observation: Observation) -> list[Message]:
        """Take one observation; return the events it causes and, when the state changed, the
        new state assertion, in the order they must be sent."""
        self._advance(observation.t)
        out: list[Message] = []
        self._dispatch(observation, out)
        self._flush(out)
        return out

    def tick(self, now: float) -> list[Message]:
        """Let time pass to ``now`` (seconds, the clock of the observations); return the current
        state assertion and, once odometry was seen, the odometer: both always, since the
        recorder needs a state at least every second and counts distance from the first reading
        it receives. Publishers silent for ``idle_threshold_s`` are forgotten, so memory stays
        bounded however many come and go."""
        self._advance(now)
        stale = [
            key
            for key, c in self._commanders.items()
            if c.last_nonzero is None or self._now - c.last_nonzero > self._s.idle_threshold_s
        ]
        for key in stale:
            del self._commanders[key]
        value = self._decide()
        self._sent = value
        out: list[Message] = [value]
        if self._odom is not None:
            out.append(Distance(self._distance))
        return out

    # -- plumbing ---------------------------------------------------------------------------

    def _advance(self, t: float) -> None:
        if self._start is None:
            self._start = t
            self._now = t
        self._now = max(self._now, t)
        self._settle()

    def _flush(self, out: list[Message]) -> None:
        value = self._decide()
        if value != self._sent:
            self._sent = value
            out.append(value)

    def _dispatch(self, observation: Observation, out: list[Message]) -> None:
        match observation:
            case CmdVel():
                self._on_cmd_vel(observation)
            case CmdVelSmoothed():
                self._on_smoothed(observation)
            case ActionStatus():
                self._on_action(observation, out)
            case CollisionMonitorState():
                self._collision_stop = observation.action_type == COLLISION_STOP
            case FunctionCall():
                self._on_function_call(observation)
            case ButtonEdge():
                self._buttons.edges[observation.source] = (observation.pressed, observation.t)
            case Joy():
                self._on_joy(observation)
            case WheelStatus():
                self._on_wheels(observation)
            case DockStatus():
                self._docked = observation.is_docked
            case KidnapStatus():
                self._on_kidnap(observation)
            case InitialPose():
                self._on_initial_pose(observation, out)
            case Odometry():
                self._on_odometry(observation)

    def _touch(self, t: float) -> None:
        """A command or action: the idle clock restarts and a released stop is over."""
        self._last_command = t if self._last_command is None else max(self._last_command, t)
        self._released = False

    def _moved(self, t: float) -> None:
        self._last_motion = t if self._last_motion is None else max(self._last_motion, t)

    # -- identities -------------------------------------------------------------------------

    def _device(self, source: Source | None) -> Device | None:
        if source is None:
            return None
        return next((d for d in self._s.devices if d.matches(source)), None)

    @staticmethod
    def _role(device: Device | None) -> Role:
        """A support device or an undeclared one counts as a person (definitions.md 6.10)."""
        if device is not None and device.kind == "ai":
            return "ai"
        if device is not None and device.kind == "script":
            return "script"
        return "human"

    def _person(self, device: Device | None, inferred: bool) -> _Person:
        actor = device.actor_id if device is not None and device.actor_id else None
        return _Person(device, actor or self._s.unidentified_actor_id, inferred)

    def _one_device(self, sources: Iterable[Source]) -> tuple[Device | None, bool]:
        """The device of several candidate sources when they all resolve to the same one, else
        undeclared; a device found this way is inferred."""
        found = {self._device(source) for source in sources}
        if len(found) == 1:
            return found.pop(), True
        return None, True

    def _requester(self, requesters: Iterable[Source]) -> _Person | None:
        """The person who sent a goal or cancel, from the action's clients in the ROS graph;
        None when it was a declared AI or script. Several or no clients: an undeclared person."""
        device, _ = self._one_device(requesters)
        if self._role(device) != "human":
            return None
        return self._person(device, inferred=True)

    def _human_value(
        self, person: _Person, shared: bool, reason: str, extra: Fields = ()
    ) -> StateAssertion:
        """A person's state: ``shared`` or by the device's location; ``human_remote`` for an
        undeclared or support device (definitions.md 6.10)."""
        device = person.device
        if device is None or device.kind != "human":
            state = "human_remote"
        elif shared:
            state = "shared"
        else:
            state = "human_local" if device.location == "local" else "human_remote"
        fields = extra + ((("x_attribution_inferred", True),) if person.inferred else ())
        return StateAssertion(
            self._s.scope,
            state,
            reason,
            person.actor_id,
            None if device is None else device.device_id,
            fields,
        )

    def _machine_value(self, device: Device, inferred: bool, extra: Fields = ()) -> StateAssertion:
        state, prefix = ("ai", "ai") if self._role(device) == "ai" else ("scripted", "script")
        fields = extra + ((("x_attribution_inferred", True),) if inferred else ())
        return StateAssertion(
            self._s.scope, state, None, f"{prefix}:{device.device_id}", device.device_id, fields
        )

    # -- topics -----------------------------------------------------------------------------

    def _relayed(self, observation: CmdVel) -> bool:
        """True when the command relays the latest ``/cmd_vel_smoothed`` (topic-level rule)."""
        smoothed = self._smoothed
        command = (observation.linear_x, observation.linear_y, observation.angular_z)
        return (
            smoothed is not None
            and abs(observation.t - smoothed[0]) <= self._s.smoothed_match_s
            and _relays(command, smoothed[1])
        )

    def _other(self, observation: CmdVel) -> Source | None:
        """The publisher of a command that relays nothing: the one candidate besides Nav2's
        relay, or undeclared when there are several."""
        nav2 = self._s.nav2_source
        others = {c for c in observation.candidates if c != nav2}
        return others.pop() if len(others) == 1 else None

    def _on_cmd_vel(self, observation: CmdVel) -> None:
        """A command with a known publisher counts at once. Otherwise the topic-level rule
        decides; since the relay and its ``/cmd_vel_smoothed`` may arrive in either order, a
        command that relays nothing yet waits up to ``smoothed_match_s`` for one (inferred)."""
        if observation.source is not None:
            self._command(observation, observation.source, inferred=False)
        elif self._relayed(observation):
            self._command(observation, self._s.nav2_source, inferred=True)
        else:
            self._pending.append(observation)
            if len(self._pending) > _MAX_PENDING:
                oldest = self._pending.pop(0)
                self._command(oldest, self._other(oldest), inferred=True)

    def _on_smoothed(self, observation: CmdVelSmoothed) -> None:
        self._smoothed = (
            observation.t,
            (observation.linear_x, observation.linear_y, observation.angular_z),
        )
        waiting, self._pending = self._pending, []
        for command in waiting:
            if self._relayed(command):
                self._command(command, self._s.nav2_source, inferred=True)
            else:
                self._pending.append(command)

    def _settle(self) -> None:
        """Commands that waited ``smoothed_match_s`` without a matching relay are the other
        publisher's."""
        limit = self._now - self._s.smoothed_match_s
        while self._pending and self._pending[0].t < limit:
            command = self._pending.pop(0)
            self._command(command, self._other(command), inferred=True)

    def _command(self, observation: CmdVel, source: Source | None, inferred: bool) -> None:
        device = self._device(source)
        key = device.device_id if device is not None else f"undeclared:{source!r}"
        commander = self._commanders.get(key)
        if commander is None:
            commander = self._commanders[key] = _Commander(device, source, inferred)
        commander.device, commander.source, commander.inferred = device, source, inferred
        if _neutral(observation.linear_x, observation.linear_y, observation.angular_z):
            commander.ended = True
            return
        commander.ended = False
        commander.last_nonzero = observation.t
        self._touch(observation.t)
        self._moved(observation.t)

    def _on_joy(self, observation: Joy) -> None:
        buttons = observation.buttons
        held = any(0 <= i < len(buttons) and buttons[i] == 1 for i in self._s.deadman_buttons)
        source = self._s.joy_teleop
        previous = self._buttons.edges.get(source)
        pressed = any(b == 1 for b in buttons)
        if previous is None or previous[0] != pressed:
            self._buttons.edges[source] = (pressed, observation.t)
        if self._joy_held and not held:
            for commander in self._commanders.values():
                if commander.source == source:
                    commander.ended = True
        self._joy_held = held

    def _presser(self, t: float) -> _Person:
        """The person behind a button-started function: the button held, or released last,
        within ``trigger_window_s``; an undeclared person when no button was seen."""
        recent = [
            (edge_t, source)
            for source, (pressed, edge_t) in self._buttons.edges.items()
            if pressed or 0 <= t - edge_t <= self._s.trigger_window_s
        ]
        if not recent:
            return self._person(None, inferred=False)
        recent.sort(key=lambda item: (item[0], repr(item[1])))
        return self._person(self._device(recent[-1][1]), inferred=False)

    def _on_function_call(self, observation: FunctionCall) -> None:
        name, t = observation.name, observation.t
        if name != _ESTOP and not any(name in names for names in ROUTINE_CALLS.values()):
            return  # menu navigation, sensors, power: not control of the base
        person = self._presser(t)
        self._calls[name] = (t, person)
        if name != _ESTOP:
            self._touch(t)
            return
        if self._wheels_enabled is None:  # no wheel status: the call toggles the stop
            if self._estop is None:
                self._start_stop(person, "/function_calls:EStop", t)
            else:
                self._estop = None
                self._released = True
        elif (
            self._estop is not None
            and self._estop.device is None
            and 0 <= t - self._estop_since <= self._s.trigger_window_s
        ):  # the wheels stopped just before the call arrived: name the button now
            self._calls.pop(name)
            self._start_stop(person, "/function_calls:EStop", self._estop_since)

    def _start_stop(self, person: _Person, trigger: str, t: float) -> None:
        self._estop, self._estop_trigger, self._estop_since = person, trigger, t

    def _recent_call(self, names: Iterable[str], t: float) -> _Person | None:
        """The person of the latest call among ``names`` within ``trigger_window_s`` before
        ``t``; the call is used up."""
        found = [
            (call_t, name)
            for name in names
            if (call := self._calls.get(name)) is not None
            and 0 <= t - (call_t := call[0]) <= self._s.trigger_window_s
        ]
        if not found:
            return None
        _, name = max(found)
        return self._calls.pop(name)[1]

    def _on_wheels(self, observation: WheelStatus) -> None:
        enabled = observation.wheels_enabled
        if self._wheels_enabled == enabled:
            return
        self._wheels_enabled = enabled
        if not enabled and self._estop is None:
            person = self._recent_call((_ESTOP,), observation.t)
            trigger = "/function_calls:EStop" if person else "/wheel_status"
            self._start_stop(person or self._person(None, inferred=False), trigger, observation.t)
        elif enabled and self._estop is not None:
            self._estop = None
            self._released = True

    def _on_kidnap(self, observation: KidnapStatus) -> None:
        if observation.is_kidnapped:
            if self._kidnap_since is None:
                self._kidnap_since = observation.t
            return
        if self._kidnap_since is None:
            return
        if observation.t - self._kidnap_since >= self._s.kidnap_min_s:
            self._last_control = self._kidnap_value()
            self._touch(observation.t)
            self._moved(observation.t)
        self._kidnap_since = None

    def _kidnap_value(self) -> StateAssertion:
        person = self._person(self._device(self._s.kidnap_source), inferred=False)
        return self._human_value(
            person, False, "human_initiated", (("x_trigger", "/kidnap_status"),)
        )

    def _on_initial_pose(self, observation: InitialPose, out: list[Message]) -> None:
        if observation.source is not None:
            device, inferred = self._device(observation.source), False
        else:
            device, inferred = self._one_device(observation.candidates)
        if self._role(device) != "human":
            return
        person = self._person(device, inferred)
        self._touch(observation.t)
        if self._nav is not None:
            self._hint("initial_pose", person, observation.t, out, until_task_end=False)
            return
        self._last_control = self._human_value(
            person,
            False,
            "planned",
            (("plan_item_id", self._s.initial_pose_plan_item_id), ("x_trigger", "/initialpose")),
        )

    def _on_odometry(self, observation: Odometry) -> None:
        if self._odom is not None:
            step = math.hypot(observation.x - self._odom[0], observation.y - self._odom[1])
            if math.isfinite(step) and step <= self._s.max_odom_step_m:
                self._distance += step
        self._odom = (observation.x, observation.y)
        self._speed = (observation.t, observation.linear_speed, observation.angular_speed)

    # -- actions ----------------------------------------------------------------------------

    def _on_action(self, observation: ActionStatus, out: list[Message]) -> None:
        known = self._goals.get(observation.action, {})
        changes = [
            (goal, known.get(goal.goal_id))
            for goal in observation.goals
            if known.get(goal.goal_id) != goal.status
        ]
        # Execution first: Nav2 aborts a preempted goal before its successor runs.
        changes.sort(key=lambda change: change[0].status != EXECUTING)
        self._goals[observation.action] = {g.goal_id: g.status for g in observation.goals}
        for goal, _ in changes:
            if observation.action == NAVIGATE:
                self._nav_change(goal, observation, out)
            elif observation.action in ROUTINE_CALLS:
                self._routine_change(goal, observation)

    def _nav_change(self, goal: GoalStatus, observation: ActionStatus, out: list[Message]) -> None:
        nav, t = self._nav, observation.t
        if goal.status == EXECUTING:
            if nav is None:
                self._start_task(goal.goal_id, t, out)
            elif goal.goal_id != nav.goal_id:
                self._regoal(goal.goal_id, observation, out)
                nav.executing_since = t
            else:
                nav.executing_since = t
            return
        if nav is None or goal.goal_id != nav.goal_id:
            return
        if goal.status == CANCELING:
            self._cancel(observation, out)
        elif goal.status in _OUTCOMES:
            successor = next(
                (
                    g.goal_id
                    for g in observation.goals
                    if g.goal_id != goal.goal_id and g.status in _PENDING
                ),
                None,
            )
            if successor is not None:
                self._regoal(successor, observation, out)
                nav.executing_since = None
                return
            if goal.status == CANCELED and (nav.hint is None or nav.hint.kind != "cancel"):
                self._cancel(observation, out)
            self._end_task(_OUTCOMES[goal.status], goal.status, t, out)

    def _start_task(self, goal_id: str, t: float, out: list[Message]) -> None:
        task_id = f"nav-{goal_id}"[:128]
        out.append(
            TaskStart(
                task_id,
                self._s.nav_task_definition_id,
                (("x_trigger", f"/{NAVIGATE}/_action/status:EXECUTING"),),
            )
        )
        self._nav = _Nav(task_id, goal_id, t)
        self._touch(t)
        self._flush(out)

    def _end_task(self, outcome: str, status: int, t: float, out: list[Message]) -> None:
        assert self._nav is not None  # noqa: S101 -- only called with a task open
        name = {SUCCEEDED: "SUCCEEDED", ABORTED: "ABORTED", CANCELED: "CANCELED"}[status]
        out.append(
            TaskEnd(
                self._nav.task_id, outcome, (("x_trigger", f"/{NAVIGATE}/_action/status:{name}"),)
            )
        )
        self._nav = None
        self._touch(t)

    def _regoal(self, goal_id: str, observation: ActionStatus, out: list[Message]) -> None:
        assert self._nav is not None  # noqa: S101 -- only called with a task open
        self._nav.goal_id = goal_id
        person = self._requester(observation.requesters)
        if person is not None:
            self._hint("regoal", person, observation.t, out, until_task_end=False)

    def _cancel(self, observation: ActionStatus, out: list[Message]) -> None:
        person = self._requester(observation.requesters)
        if person is not None:
            self._hint("cancel", person, observation.t, out, until_task_end=True)

    def _hint(
        self, kind: str, person: _Person, t: float, out: list[Message], until_task_end: bool
    ) -> None:
        """A person's input to Nav2 during a task (definitions.md 6.2, 6.8): a shared span, a
        ``hint`` event, or both when a higher rule hides the span."""
        assert self._nav is not None  # noqa: S101 -- only called with a task open
        self._nav.hint = _Hint(kind, person, t, until_task_end)
        self._flush(out)
        shown = self._sent is not None and dict(self._sent.fields).get("x_hint") == kind
        if self._s.hint_policy == "hint_as_event" or not shown:
            fields: Fields = (("x_kind", kind),)
            if person.inferred:
                fields += (("x_attribution_inferred", True),)
            device = person.device
            out.append(
                Hint(
                    self._s.scope,
                    person.actor_id,
                    None if device is None else device.device_id,
                    fields,
                )
            )

    def _routine_change(self, goal: GoalStatus, observation: ActionStatus) -> None:
        routine, t = self._routine, observation.t
        if goal.status == EXECUTING and (routine is None or routine.goal_id != goal.goal_id):
            value = self._routine_value(observation)
            self._routine = _Routine(goal.goal_id, value)
            self._touch(t)
            self._moved(t)
        elif goal.status in _OUTCOMES and routine is not None and routine.goal_id == goal.goal_id:
            self._last_control = routine.value
            self._routine = None
            self._touch(t)
            self._moved(t)

    def _routine_value(self, observation: ActionStatus) -> StateAssertion:
        """Who started a dock, undock or wall-follow (definitions.md 6.13, notes UC-3)."""
        action = observation.action
        extra: Fields = (
            ("x_trigger", f"/{action}/_action/status:EXECUTING"),
            ("x_routine", action),
        )
        person = self._recent_call(ROUTINE_CALLS[action], observation.t)
        if person is not None:
            return self._human_value(person, True, "human_initiated", extra)
        sources = [r for r in observation.requesters if r != self._s.function_caller]
        device, inferred = self._one_device(sources)
        if device is not None and self._role(device) != "human":
            return self._machine_value(device, inferred, extra)
        return self._human_value(self._person(device, inferred), True, "human_initiated", extra)

    # -- the decision -----------------------------------------------------------------------

    def _active(self, now: float, window: float) -> list[_Commander]:
        return [
            c
            for c in self._commanders.values()
            if not c.ended and c.last_nonzero is not None and 0 <= now - c.last_nonzero < window
        ]

    def _control(self, value: StateAssertion) -> StateAssertion:
        self._last_control = value
        return value

    def _decide(self) -> StateAssertion:  # noqa: PLR0911 -- one return per rule, in order
        now, s = self._now, self._s
        if self._kidnap_since is not None and now - self._kidnap_since >= s.kidnap_min_s:
            return self._control(self._kidnap_value())
        if self._estop is not None:
            return self._stop_value()
        if self._routine is not None:
            return self._control(self._routine.value)
        commanded = self._commanded(now)
        if commanded is not None:
            return self._control(commanded)
        if self._out_of_band(now):
            return StateAssertion(s.scope, "unknown", fields=(("x_out_of_band", True),))
        if self._nav is not None:
            return self._task_value(now)
        machine = self._machine(now)
        if machine is not None:
            return self._control(machine)
        start = self._last_command if self._last_command is not None else self._start
        if self._kidnap_since is None and start is not None and now - start >= s.idle_threshold_s:
            self._released = False
            docked = () if self._docked is None else (("x_docked", self._docked),)
            return self._control(StateAssertion(s.scope, "idle", fields=docked))
        if self._released:
            return StateAssertion(s.scope, "unknown", fields=(("x_cause", "stop_released"),))
        if self._last_control is not None:
            return self._last_control
        return StateAssertion(s.scope, "unknown", fields=(("x_cause", "not_yet_observed"),))

    def _stop_value(self) -> StateAssertion:
        assert self._estop is not None  # noqa: S101 -- only called during a stop
        person = self._estop
        fields: Fields = (("x_trigger", self._estop_trigger),)
        if person.device is not None:
            fields += (("x_trigger_kind", "human_button"),)
        device_id = None if person.device is None else person.device.device_id
        return StateAssertion(
            self._s.scope, "safety_stop", "safety", person.actor_id, device_id, fields
        )

    def _commanded(self, now: float) -> StateAssertion | None:
        """A person's ``/cmd_vel`` (rule 4), shared when another source is in contention."""
        s = self._s
        humans = [c for c in self._active(now, s.goal_release_s) if self._role(c.device) == "human"]
        if not humans:
            return None
        lead = max(humans, key=lambda c: c.last_nonzero or 0.0)
        rivals = [
            c
            for c in self._active(now, s.contention_window_s)
            if c is not lead and (self._role(c.device) != "human" or c in humans)
        ]
        person = self._person(lead.device, lead.inferred)
        if rivals:
            extra: Fields = (("x_contention", True), ("x_trigger", "/cmd_vel"))
            if any(c.inferred for c in rivals):
                person = _Person(person.device, person.actor_id, True)
            return self._human_value(person, True, "human_initiated", extra)
        return self._human_value(person, False, "human_initiated", (("x_trigger", "/cmd_vel"),))

    def _machine(self, now: float) -> StateAssertion | None:
        """A declared AI or script on ``/cmd_vel`` outside a task (rule 7)."""
        machines = [c for c in self._active(now, self._s.goal_release_s) if c.device is not None]
        if not machines:
            return None
        ai = [c for c in machines if self._role(c.device) == "ai"]
        lead = max(ai or machines, key=lambda c: c.last_nonzero or 0.0)
        assert lead.device is not None  # noqa: S101 -- filtered above
        return self._machine_value(lead.device, lead.inferred, (("x_trigger", "/cmd_vel"),))

    def _out_of_band(self, now: float) -> bool:
        """Motion on ``/odom`` with no command for ``gap_tolerance_s`` (notes B.6 item 5)."""
        if self._speed is None:
            return False
        t, linear, angular = self._speed
        s = self._s
        fresh = 0 <= now - t < s.gap_tolerance_s
        moving = abs(linear) > s.motion_linear_mps or abs(angular) > s.motion_angular_rps
        quiet = self._last_motion is None or now - self._last_motion > s.gap_tolerance_s
        return fresh and moving and quiet

    def _task_value(self, now: float) -> StateAssertion:
        """Rule 6: an open Nav2 task."""
        nav, s = self._nav, self._s
        assert nav is not None  # noqa: S101 -- only called with a task open
        if self._collision_stop:
            return StateAssertion(
                s.scope,
                "safety_stop",
                "safety",
                fields=(("x_trigger", "/collision_monitor_state:STOP"),),
            )
        hint = self._live_hint(now)
        if hint is not None:
            extra: Fields = (("x_hint", hint.kind), ("x_trigger", f"/{NAVIGATE}"))
            return self._control(self._human_value(hint.person, True, "human_initiated", extra))
        nav2 = self._device(s.nav2_source)
        return self._control(
            StateAssertion(
                s.scope,
                "ai",
                None,
                s.nav2_actor_id,
                None if nav2 is None else nav2.device_id,
                (("x_trigger", f"/{NAVIGATE}/_action/status:EXECUTING"),),
            )
        )

    def _live_hint(self, now: float) -> _Hint | None:
        """The person's input still shown as shared: a cancel until the task ends; under
        ``hint_as_shared`` a re-goal or pose until Nav2 follows the new goal or ``hint_ttl_s``."""
        nav = self._nav
        hint = None if nav is None else nav.hint
        if nav is None or hint is None:
            return None
        if hint.until_task_end:
            return hint
        if self._s.hint_policy != "hint_as_shared" or now - hint.t >= self._s.hint_ttl_s:
            return None
        nav2 = self._device(self._s.nav2_source)
        following = nav.executing_since is not None and any(
            c.device == nav2
            and c.last_nonzero is not None
            and c.last_nonzero > max(hint.t, nav.executing_since)
            for c in self._commanders.values()
            if c.device is not None
        )
        return None if following else hint
