"""LeRobot (SO-101) engine and importer (README, "LeRobot").

Pure and standard library only: no lerobot, torch or pyarrow, no input or output, no clock. The
formats are those of LeRobot v0.6.1, commit ``7e241bd630a3719a56157a497ce5d08f244784f1``
(research/lerobot-so101); each pattern cites the file and line of that commit that writes it.
:mod:`autonometry.connectors.lerobot_io` does the reading and writing.

**Live** (``autonometry lerobot watch``). Each new line of the rollout's log goes to
:func:`parse_line`, the result to :meth:`LeRobotEngine.observe` with its read time ``t``
(seconds); :meth:`LeRobotEngine.tick` runs a few times a second; what both return is sent, in
order, through the recorder client. The recorder stamps ``ts``; the log's own local wall-clock
time (1 s resolution, time zone not recorded) travels as ``x_src_ts`` with ``x_src_clock``
``wall`` (definitions.md 8, Time). The scopes (``whole_robot``, or both arms of a bimanual
robot, dossier 14.7) are in one state, the first rule that applies (dossier 14.1 under v0.3):

1. Before a line that tells the state (the rollout's start, its control loop, a phase
   transition): no assertion, so the recorder logs ``unknown`` (section 2).
2. After ``Rollout finished``, or once the rollout's process is gone without logging it
   (:meth:`LeRobotEngine.process_gone`): ``unknown``; nothing is observed any more.
3. The rollout started, its control loop not yet: ``idle``; it connects and loads, no task
   is open and nothing commands the arm.
4. A controller that stopped (control loop ended, robot disconnected, an episodic reset with no
   teleoperator) keeps its state for ``idle_threshold_s``, then ``idle`` (6.14).
5. DAgger ``AUTONOMOUS`` and the policy loops of the other strategies: ``ai``, actor
   ``ai:<policy path>``, the policy's device. DAgger ``PAUSED``: ``shared`` with reason
   ``human_initiated``, a human pause (6.8). DAgger ``CORRECTING``: ``human_local``, or
   ``human_remote`` when the teleoperator's device is declared remote (6.10); reason
   ``human_initiated``. Human states carry the teleoperator's device and actor.
6. An episodic reset (``Reset the environment``): with a teleoperator, the person resets the
   scene: ``human_local`` or ``human_remote``, reason ``planned`` with the plan's reset item
   (6.3); without one, LeRobot drives the arm back to its start pose: ``scripted``, actor
   ``script:return_to_initial_position`` (6.9, 6.13). The rollout's teardown does the same
   (``Returning robot to initial position``, ``return_to_initial_position=True`` by default).

**Sources.** LeRobot has no ROS graph; the components the rollout names in its log are matched
against the device registry's ``match.node`` (6.10): ``policy:<--policy.path>``,
``teleop:<--teleop.type>`` and ``script:return_to_initial_position``. A source that matches no
device is sent without ``device_id``, and the loader counts it as ``human_remote``.

**Tasks.** Episodes are tasks (section 5): ``task_definition_id`` is the dataset's
``single_task`` through the task registry, ``task_id`` ``episode_<index>``. DAgger with
``record_autonomous=true`` and sentry rotate episodes: one starts with the control loop and at
each ``Episode saved``. Episodic opens one at ``Recording episode N`` and closes it at
``Reset the environment``. Outcome ``unknown`` on save (LeRobot has no success flag),
``aborted`` on re-record (dossier 14.1; ``Left arrow key pressed`` during the episode). A
re-record known only after the episode closed is recorded as ``x_lerobot_rerecorded_task`` on a
heartbeat. DAgger corrections-only episodes are correction clips, not units of work: no tasks.

**After the fact** (``autonometry lerobot import``). :func:`episode_events` turns one episode of a
LeRobotDataset v3.0 into one session: ``task_start`` at frame 0, a transition at each change of
the per-frame ``intervention`` label (true the teleoperator's state, false ``ai``), ``unknown``
for missing frames or labels, heartbeats every ``heartbeat_interval_s``, ``task_end`` with outcome
``unknown`` after the last frame. ``ts`` is an anchor plus ``frame_index / fps``; every event
carries ``x_ts_reconstructed: true`` (trust level L1 at most, section 8, 10.6).
"""

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final, Literal, cast

from autonometry.connectors.tb4 import (
    Device,
    Fields,
    Source,
    StateAssertion,
    TaskEnd,
    TaskStart,
)
from autonometry.connectors.tb4 import wire_event as _tb4_wire_event
from autonometry.timestamps import ns_to_ts

__all__ = [
    "LEROBOT_COMMIT",
    "LEROBOT_VERSION",
    "SCRIPT_RETURN",
    "UNOBSERVED",
    "CorrectionSaved",
    "Episode",
    "EpisodeRecording",
    "EpisodeSaved",
    "ExistingEpisodes",
    "ImportSettings",
    "LeRobotEngine",
    "LoopEnd",
    "LoopStart",
    "Message",
    "Note",
    "Parsed",
    "Phase",
    "PolicyPath",
    "ProcessEnd",
    "ProcessStart",
    "Rerecord",
    "RerecordKey",
    "ResetStart",
    "ReturnHome",
    "RobotDisconnect",
    "Settings",
    "Signal",
    "TeleopType",
    "controller_id",
    "episode_events",
    "frame_ns",
    "parse_line",
    "session_id",
    "task_definition",
    "wire_event",
]

#: The LeRobot release and commit whose formats this module reads.
LEROBOT_VERSION: Final = "v0.6.1"
LEROBOT_COMMIT: Final = "7e241bd630a3719a56157a497ce5d08f244784f1"
#: LeRobot's routine that drives the arm back to its start pose (rollout/strategies/core.py:141).
SCRIPT_RETURN: Final = "return_to_initial_position"
#: Signal classes a dataset cannot show: no safety function exists on the SO-101, and pauses and
#: resets are never recorded (dossier summary 2 and 4, definitions.md 8).
UNOBSERVED: Final = ("safety_stops", "human_inputs")
#: Longest line parsed, in characters; the reader drops longer ones.
MAX_LINE_CHARS: Final = 65536

_UNIDENTIFIED: Final = "person:unidentified"
_NO_POLICY: Final = "unidentified"
_ID_BAD = re.compile(r"[^A-Za-z0-9._:-]")
#: ``ai:`` plus 125 characters is the longest identifier (definitions.md 8, Identifiers).
_CONTROLLER_MAX = 125
_DIGEST_CHARS = 12
_NS_PER_S = 1_000_000_000
_MAX_COUNTER = 10**16

# -- log lines ----------------------------------------------------------------------------------

#: Windows PowerShell 5.1 puts ``<program> : `` before the first line a native program writes
#: to stderr under ``2>&1``; it is skipped.
#: ``init_logging``'s format: ``"%(levelname)s %(lerobot_pid)s%(asctime)s %(lerobot_location)15s
#: %(message)s"`` with ``datefmt="%Y-%m-%d %H:%M:%S"``; the location is the last 15 characters of
#: ``path:line`` (lerobot/utils/utils.py:62-73). The PID part is ``[PID: <pid>] `` when enabled.
_PREFIX = re.compile(
    r"(?:[^:]{1,128} : )?(?P<level>[A-Z]+) (?:\[PID: [0-9]+\] )?"
    r"(?P<time>[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}) "
    r"(?P<location>.{15}) (?P<message>.*)"
)
_COUNT = r"[0-9]{1,15}"
#: LeRobot's em dash as it reaches a log file: U+2014, or one to three other characters when a
#: shell decoded it in another code page (Windows: 0x97 in cp1252, ``ΓÇö`` for UTF-8
#: read as cp437, U+FFFD for a byte that is not UTF-8).
_DASH = r"\S{1,3}"
_PHASES = ("autonomous", "paused", "correcting")
_PHASE = "|".join(_PHASES)

LoopKind = Literal["dagger_continuous", "dagger_corrections", "sentry", "highlight", "base"]
PhaseName = Literal["autonomous", "paused", "correcting"]


@dataclass(frozen=True, slots=True)
class ProcessStart:
    """``Building rollout context...`` (lerobot/scripts/lerobot_rollout.py:219)."""


@dataclass(frozen=True, slots=True)
class PolicyPath:
    """``Loading policy from '<path>'...`` (lerobot/rollout/context.py:245)."""

    path: str


@dataclass(frozen=True, slots=True)
class TeleopType:
    """``Connecting teleoperator (<type>)...`` (lerobot/rollout/context.py:314)."""

    name: str


@dataclass(frozen=True, slots=True)
class ExistingEpisodes:
    """``Dataset ready: <repo_id> (<n> existing episodes)`` (lerobot/rollout/context.py:456)."""

    count: int


@dataclass(frozen=True, slots=True)
class LoopStart:
    """A strategy's control loop started: DAgger continuous (dagger.py:353) or corrections-only
    (dagger.py:508), sentry (sentry.py:102), highlight (highlight.py:109), base (base.py:53);
    all under lerobot/rollout/strategies/."""

    kind: LoopKind


@dataclass(frozen=True, slots=True)
class Phase:
    """``Phase transition: <old> -> <new>`` with ``DAggerPhase`` values
    (lerobot/rollout/strategies/dagger.py:86-88, 659)."""

    old: PhaseName
    new: PhaseName


@dataclass(frozen=True, slots=True)
class EpisodeSaved:
    """``Episode saved (total: <n>, elapsed: <s>s)`` when a DAgger continuous or sentry episode
    rotates (dagger.py:443, sentry.py:146; ``rotation`` true), or ``Episode saved (total: <n>)``
    for a highlight clip (highlight.py:152). ``total`` counts the dataset's episodes after the
    save, so it is the index of the next one."""

    total: int
    rotation: bool


@dataclass(frozen=True, slots=True)
class CorrectionSaved:
    """``Correction <n>/<m> saved``: a DAgger corrections-only episode (dagger.py:546)."""


@dataclass(frozen=True, slots=True)
class EpisodeRecording:
    """``Recording episode <n>``: an episodic episode starts (``log_say``,
    lerobot/rollout/strategies/episodic.py:122, lerobot/utils/utils.py:147)."""

    index: int


@dataclass(frozen=True, slots=True)
class ResetStart:
    """``Reset the environment``: the episodic policy loop ended, the reset phase starts
    (episodic.py:138)."""


@dataclass(frozen=True, slots=True)
class RerecordKey:
    """``Left arrow key pressed. Exiting loop and rerecord the last episode...``: printed, not
    logged, when the re-record key is pressed (lerobot/utils/keyboard_input.py:161)."""


@dataclass(frozen=True, slots=True)
class Rerecord:
    """``Re-record episode``: the episode is discarded (episodic.py:183)."""


@dataclass(frozen=True, slots=True)
class LoopEnd:
    """A control loop ended: DAgger (dagger.py:465, 614), episodic (episodic.py:200), sentry
    (sentry.py:168), highlight (highlight.py:179), or ``Stopping inference engine...`` at teardown,
    the only sign for the base strategy (lerobot/rollout/strategies/core.py:122)."""


@dataclass(frozen=True, slots=True)
class ReturnHome:
    """``Returning robot to initial position before shutdown...`` (core.py:127)."""


@dataclass(frozen=True, slots=True)
class RobotDisconnect:
    """``Disconnecting robot...``: the arm is released, torque off by default (core.py:133)."""


@dataclass(frozen=True, slots=True)
class ProcessEnd:
    """``Rollout finished`` (lerobot/scripts/lerobot_rollout.py:242)."""


Signal = (
    ProcessStart
    | PolicyPath
    | TeleopType
    | ExistingEpisodes
    | LoopStart
    | Phase
    | EpisodeSaved
    | CorrectionSaved
    | EpisodeRecording
    | ResetStart
    | RerecordKey
    | Rerecord
    | LoopEnd
    | ReturnHome
    | RobotDisconnect
    | ProcessEnd
)


@dataclass(frozen=True, slots=True)
class Parsed:
    """A recognised line: its signal and the local wall-clock time LeRobot printed on it
    (``YYYY-MM-DD HH:MM:SS``), ``None`` for a printed line without the logging prefix."""

    signal: Signal
    src_ts: str | None


def _phase(match: re.Match[str]) -> Signal:
    return Phase(cast(PhaseName, match.group("old")), cast(PhaseName, match.group("new")))


_Factory = Callable[[re.Match[str]], Signal]
_MESSAGES: Final[tuple[tuple[re.Pattern[str], _Factory], ...]] = (
    (re.compile(r"Phase transition: (?P<old>" + _PHASE + ") -> (?P<new>" + _PHASE + ")"), _phase),
    (
        re.compile(r"Episode saved \(total: (?P<n>" + _COUNT + r")(?P<e>, elapsed: \S+s)?\)"),
        lambda m: EpisodeSaved(int(m.group("n")), m.group("e") is not None),
    ),
    (re.compile(r"Correction " + _COUNT + "/" + _COUNT + " saved"), lambda m: CorrectionSaved()),
    (
        re.compile(r"Recording episode (?P<n>" + _COUNT + ")"),
        lambda m: EpisodeRecording(int(m.group("n"))),
    ),
    (re.compile(r"Reset the environment"), lambda m: ResetStart()),
    (re.compile(r"Re-record episode"), lambda m: Rerecord()),
    (
        re.compile(r"Left arrow key pressed\. Exiting loop and rerecord the last episode\.\.\."),
        lambda m: RerecordKey(),
    ),
    (
        re.compile(r"DAgger continuous recording started \(.*\)"),
        lambda m: LoopStart("dagger_continuous"),
    ),
    (
        re.compile(r"DAgger corrections-only recording started \(.*\)"),
        lambda m: LoopStart("dagger_corrections"),
    ),
    (re.compile(r"Sentry recording started \(.*\)"), lambda m: LoopStart("sentry")),
    (re.compile(r"Highlight strategy recording started \(.*\)"), lambda m: LoopStart("highlight")),
    (re.compile(r"Base strategy control loop started"), lambda m: LoopStart("base")),
    (
        re.compile(
            r"(?:DAgger continuous control loop ended|DAgger corrections-only loop ended)"
            r" " + _DASH + r" pausing engine"
            r"|Episodic control loop ended " + _DASH + r" saving any in-progress episode"
            r"|Sentry control loop ended " + _DASH + r" saving final episode"
            r"|Highlight control loop ended"
            r"|Stopping inference engine\.\.\."
        ),
        lambda m: LoopEnd(),
    ),
    (
        re.compile(r"Returning robot to initial position before shutdown\.\.\."),
        lambda m: ReturnHome(),
    ),
    (re.compile(r"Disconnecting robot\.\.\."), lambda m: RobotDisconnect()),
    (re.compile(r"Rollout finished"), lambda m: ProcessEnd()),
    (re.compile(r"Building rollout context\.\.\."), lambda m: ProcessStart()),
    (re.compile(r"Loading policy from '(?P<p>.+)'\.\.\."), lambda m: PolicyPath(m.group("p"))),
    (
        re.compile(r"Connecting teleoperator \((?P<t>.+)\)\.\.\."),
        lambda m: TeleopType(m.group("t")),
    ),
    (
        re.compile(r"Dataset ready: .* \((?P<n>" + _COUNT + r") existing episodes\)"),
        lambda m: ExistingEpisodes(int(m.group("n"))),
    ),
)


def parse_line(text: str) -> Parsed | None:
    """Recognise one line of a ``lerobot-rollout`` log, ``None`` when it says nothing this
    connector uses. Lines LeRobot's logging formatted keep their printed time; other lines (the
    keyboard listener prints, it does not log) are matched as they are, without a time. Never
    raises."""
    line = text.rstrip("\r\n")
    if len(line) > MAX_LINE_CHARS:
        return None
    prefix = _PREFIX.fullmatch(line)
    message, src_ts = (prefix["message"], prefix["time"]) if prefix else (line.strip(), None)
    for pattern, factory in _MESSAGES:
        found = pattern.fullmatch(message)
        if found is not None:
            return Parsed(factory(found), src_ts)
    return None


# -- identities ---------------------------------------------------------------------------------


def controller_id(path: str) -> str:
    """The ``<controller_id>`` of the actor ``ai:<controller_id>`` for a policy path (definitions.md
    1, Actor; 8, Identifiers): characters outside an identifier's set become ``_``; a path longer
    than 125 characters keeps a 12-hex SHA-256 of the whole, ``_`` and its last characters."""
    text = _ID_BAD.sub("_", path.strip()) or _NO_POLICY
    if len(text) <= _CONTROLLER_MAX:
        return text
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return f"{digest}_{text[-(_CONTROLLER_MAX - _DIGEST_CHARS - 1) :]}"


def _match(devices: Iterable[Device], node: str) -> Device | None:
    """The first device whose ``match`` fields all equal the observed identity (6.10)."""
    source = Source(node=node)
    return next((d for d in devices if d.matches(source)), None)


def task_definition(task: str | None, registry: Mapping[str, Any] | None) -> tuple[str, bool]:
    """The ``task_definition_id`` for a dataset's task text (``single_task``) and whether the task
    registry declares it (definitions.md 1, Task; 5): the first task whose ``description`` or
    ``task_definition_id`` equals the text. Otherwise ``lerobot_task_<12 hex of its SHA-256>``,
    which the loader flags ``unknown_task_definition``."""
    if registry is not None and task is not None:
        for entry in registry.get("tasks", ()):
            if task in (entry.get("description"), entry.get("task_definition_id")):
                return str(entry["task_definition_id"]), True
    digest = hashlib.sha256((task or "").encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return f"lerobot_task_{digest}", False


# -- control values -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Control:
    """A scope's state with what the transition carries; ``src_ts`` is the log time of the line
    that set it."""

    state: str
    reason: str | None = None
    actor_id: str | None = None
    device_id: str | None = None
    plan_item_id: str | None = None
    src_ts: str | None = None


def _device_id(device: Device | None) -> str | None:
    return None if device is None else device.device_id


def _ai(devices: Sequence[Device], policy: str | None, src_ts: str | None = None) -> _Control:
    device = None if policy is None else _match(devices, f"policy:{policy}")
    actor = f"ai:{controller_id(policy) if policy is not None else _NO_POLICY}"
    return _Control("ai", None, actor, _device_id(device), None, src_ts)


def _human(  # noqa: PLR0913, PLR0917 -- the fields of a human transition
    devices: Sequence[Device],
    teleop: str | None,
    shared: bool,
    reason: str,
    plan_item_id: str | None = None,
    src_ts: str | None = None,
) -> _Control:
    """The teleoperator's state: ``shared`` for a pause, else ``human_remote`` when its device is
    declared remote and ``human_local`` otherwise (6.8, 6.10)."""
    device = None if teleop is None else _match(devices, f"teleop:{teleop}")
    if shared:
        state = "shared"
    elif device is not None and device.location == "remote":
        state = "human_remote"
    else:
        state = "human_local"
    actor = (device.actor_id if device is not None else None) or _UNIDENTIFIED
    return _Control(state, reason, actor, _device_id(device), plan_item_id, src_ts)


def _script(devices: Sequence[Device], src_ts: str | None) -> _Control:
    device = _match(devices, f"script:{SCRIPT_RETURN}")
    actor = f"script:{SCRIPT_RETURN}"
    return _Control("scripted", None, actor, _device_id(device), None, src_ts)


def _src_fields(src_ts: str | None, clock: str = "wall") -> Fields:
    return () if src_ts is None else (("x_src_ts", src_ts), ("x_src_clock", clock))


def _assertion(scope: str, control: _Control) -> StateAssertion:
    fields: Fields = ()
    if control.plan_item_id is not None:
        fields += (("plan_item_id", control.plan_item_id),)
    fields += _src_fields(control.src_ts)
    return StateAssertion(
        scope, control.state, control.reason, control.actor_id, control.device_id, fields
    )


# -- live engine --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    """What the live engine needs. ``idle_threshold_s`` (seconds) comes from the spec's
    ``rules.toml`` (definitions.md 9); ``devices`` from the device registry; ``policy_path`` names
    the policy when the log does not (a watcher started mid-run); ``reset_plan_item_id`` is the
    plan item of an episodic reset, ``None`` when the plan has none (reason ``human_initiated``)."""

    devices: tuple[Device, ...]
    idle_threshold_s: float
    task_definition_id: str
    scopes: tuple[str, ...] = ("whole_robot",)
    policy_path: str | None = None
    reset_plan_item_id: str | None = "reset"

    def __post_init__(self) -> None:
        if not self.scopes:
            raise ValueError("scopes must name at least one scope")
        if not self.idle_threshold_s > 0:
            raise ValueError("idle_threshold_s must be a positive number")


@dataclass(frozen=True, slots=True)
class Note:
    """``x_`` fields for a heartbeat the recorder writes at once (README, "Wire format")."""

    fields: Fields


Message = StateAssertion | TaskStart | TaskEnd | Note

_ROTATING: Final = frozenset({"dagger_continuous", "sentry"})


def wire_event(message: Message) -> dict[str, Any]:
    """The event a message stands for, as a connector sends it (README, "Wire format")."""
    if isinstance(message, Note):
        return {"type": "heartbeat", **dict(message.fields)}
    return _tb4_wire_event(message)


class LeRobotEngine:
    """Decides a LeRobot robot's control state and tasks from its rollout log; see the module
    docstring."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._now = 0.0
        self._sent: tuple[StateAssertion, ...] = ()
        self._task: str | None = None
        self._task_ids: set[str] = set()
        self._closed_unknown: str | None = None
        self._attached = False
        self._ended: str | None = None
        self._end_src: str | None = None
        self._process_src: str | None = None
        self._policy = settings.policy_path
        self._teleop: str | None = None
        self._existing = 0
        self._mode: str | None = None
        self._control: _Control | None = None
        self._released: float | None = None
        self._rerecord = False
        self._generation = 0

    # -- public -----------------------------------------------------------------------------

    def observe(self, t: float, parsed: Parsed) -> list[Message]:
        """Take one recognised line read at ``t`` (seconds, the clock of :meth:`tick`); return
        the events it causes and, when the state changed, the new state, in sending order."""
        self._now = max(self._now, t)
        out: list[Message] = []
        self._dispatch(parsed.signal, parsed.src_ts, out)
        self._flush(out)
        return out

    def tick(self, now: float) -> list[Message]:
        """Let time pass to ``now``; return the current state of every scope (empty until a line
        told the state), which the recorder needs at least every second."""
        self._now = max(self._now, now)
        self._sent = self._decide()
        return list(self._sent)

    @property
    def generation(self) -> int:
        """How many rollouts this engine has followed: a new one starts at each rollout start,
        or when a line shows a rollout the engine joined mid-run."""
        return self._generation

    @property
    def following(self) -> bool:
        """True while a rollout is followed and has not ended."""
        return self._attached and self._ended is None

    def process_gone(self, t: float) -> list[Message]:
        """The rollout's process ended at or before ``t`` without logging ``Rollout finished``
        (killed, crashed, its window closed): what that line does (the open task closed with
        outcome ``unknown``, every scope ``unknown``, section 2 and 6.14), then a heartbeat note
        ``x_lerobot_process_exit: unlogged``. Nothing when no rollout is followed."""
        self._now = max(self._now, t)
        if not self.following:
            return []
        out: list[Message] = []
        self._close("unknown", None, out)
        self._ended, self._end_src = "gone", None
        self._flush(out)
        out.append(Note((("x_lerobot_process_exit", "unlogged"),)))
        return out

    def stop(self) -> list[Message]:
        """The watcher stops: every scope ``unknown``, since nothing is observed any more."""
        self._sent = tuple(_assertion(scope, _Control("unknown")) for scope in self._s.scopes)
        return list(self._sent)

    # -- state ------------------------------------------------------------------------------

    def _new_process(self, src_ts: str | None) -> None:
        self._generation += 1
        self._attached = True
        self._ended = None
        self._end_src = None
        self._process_src = src_ts
        self._policy = self._s.policy_path
        self._teleop = None
        self._existing = 0
        self._mode = None
        self._control = None
        self._released = None
        self._rerecord = False

    def _attach(self, src_ts: str | None) -> None:
        """A line that only a running rollout writes: start following one if none is."""
        if not self._attached or self._ended is not None:
            self._new_process(src_ts)

    def _take(self, control: _Control) -> None:
        self._control = control
        self._released = None

    def _release(self) -> None:
        if self._released is None and self._control is not None:
            self._released = self._now

    def _decide(self) -> tuple[StateAssertion, ...]:
        if not self._attached:
            return ()
        if self._ended is not None:
            control = _Control("unknown", src_ts=self._end_src)
        elif self._control is None:
            control = _Control("idle", src_ts=self._process_src)
        elif self._released is not None and self._now - self._released >= self._s.idle_threshold_s:
            control = _Control("idle")
        else:
            control = self._control
        return tuple(_assertion(scope, control) for scope in self._s.scopes)

    def _flush(self, out: list[Message]) -> None:
        value = self._decide()
        if value and value != self._sent:
            self._sent = value
            out.extend(value)

    # -- tasks ------------------------------------------------------------------------------

    def _open(self, index: int, src_ts: str | None, out: list[Message]) -> None:
        """Open the task of episode ``index``; one still open is closed first (tasks never
        nest, definitions.md 5)."""
        self._close("unknown", src_ts, out)
        self._flush(out)
        base = f"episode_{index:06d}"
        task_id, attempt = base, 1
        while task_id in self._task_ids:
            attempt += 1
            task_id = f"{base}_{attempt}"
        self._task_ids.add(task_id)
        self._task = task_id
        self._closed_unknown = None
        fields = (("x_episode_index", index), *_src_fields(src_ts))
        out.append(TaskStart(task_id, self._s.task_definition_id, fields))

    def _close(self, outcome: str, src_ts: str | None, out: list[Message]) -> None:
        if self._task is None:
            return
        out.append(TaskEnd(self._task, outcome, _src_fields(src_ts)))
        self._closed_unknown = self._task if outcome == "unknown" else None
        self._task = None

    # -- signals ----------------------------------------------------------------------------

    def _policy_control(self, src_ts: str | None) -> _Control:
        return _ai(self._s.devices, self._policy, src_ts)

    def _phase_control(self, phase: PhaseName, src_ts: str | None) -> _Control:
        if phase == "autonomous":
            return self._policy_control(src_ts)
        shared = phase == "paused"
        return _human(self._s.devices, self._teleop, shared, "human_initiated", None, src_ts)

    def _dispatch(self, signal: Signal, src: str | None, out: list[Message]) -> None:  # noqa: PLR0912 -- one branch per signal
        match signal:
            case ProcessStart() | PolicyPath() | TeleopType() | ExistingEpisodes():
                self._on_setup(signal, src, out)
            case LoopStart(kind=kind):
                self._attach(src)
                self._mode = kind
                self._take(self._policy_control(src))
                if kind in _ROTATING:
                    self._open(self._existing, src, out)
            case Phase(new=new):
                self._attach(src)
                self._mode = self._mode or "dagger"
                self._take(self._phase_control(new, src))
            case EpisodeSaved(total=total, rotation=rotation):
                self._on_saved(total, rotation, src, out)
            case CorrectionSaved():
                if self._mode == "dagger":
                    self._mode = "dagger_corrections"
            case EpisodeRecording(index=index):
                self._attach(src)
                self._mode = "episodic"
                self._rerecord = False
                self._take(self._policy_control(src))
                self._open(index, src, out)
            case ResetStart():
                self._on_reset(src, out)
            case RerecordKey():
                self._rerecord = self._task is not None
            case Rerecord():
                if self._closed_unknown is not None:
                    out.append(Note((("x_lerobot_rerecorded_task", self._closed_unknown),)))
                    self._closed_unknown = None
            case LoopEnd():
                self._close("unknown", src, out)
                self._release()
            case ReturnHome():
                self._attach(src)
                self._take(_script(self._s.devices, src))
            case RobotDisconnect():
                self._release()
            case ProcessEnd():
                if self._attached and self._ended is None:
                    self._close("unknown", src, out)
                    self._ended, self._end_src = "finished", src

    def _on_setup(
        self,
        signal: ProcessStart | PolicyPath | TeleopType | ExistingEpisodes,
        src: str | None,
        out: list[Message],
    ) -> None:
        """The rollout starts and names its policy, teleoperator and existing episodes."""
        if isinstance(signal, ProcessStart):
            self._close("unknown", src, out)
            self._new_process(src)
            return
        self._attach(src)
        match signal:
            case PolicyPath(path=path):
                self._policy = path
            case TeleopType(name=name):
                self._teleop = name
            case ExistingEpisodes(count=count):
                self._existing = count

    def _on_saved(self, total: int, rotation: bool, src: str | None, out: list[Message]) -> None:
        """A rotating episode was saved: close it and open the next (DAgger continuous, sentry;
        a DAgger loop joined mid-run rotates only once a save shows it records continuously)."""
        if not rotation or not self._attached or self._ended is not None:
            return
        if self._mode == "dagger":
            self._mode = "dagger_continuous"
        if self._mode in _ROTATING and self._control is not None and self._released is None:
            self._close("unknown", src, out)
            self._open(total, src, out)

    def _on_reset(self, src: str | None, out: list[Message]) -> None:
        """An episodic reset starts; only the episodic strategy writes this line."""
        if not self._attached or self._ended is not None:
            return
        self._mode = "episodic"
        self._close("aborted" if self._rerecord else "unknown", src, out)
        self._rerecord = False
        if self._teleop is None:
            self._take(_script(self._s.devices, src))
            self._release()
        else:
            plan_item = self._s.reset_plan_item_id
            reason = "planned" if plan_item is not None else "human_initiated"
            self._take(_human(self._s.devices, self._teleop, False, reason, plan_item, src))


# -- importer -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Episode:
    """One episode of a LeRobotDataset: its index, task text, the frame count ``meta/episodes``
    declares (``length``), and per frame, in ``frame_index`` order, the ``intervention`` label
    (``None`` when missing or not a boolean)."""

    index: int
    task: str | None
    length: int
    frame_index: tuple[int, ...]
    intervention: tuple[bool | None, ...]

    def __post_init__(self) -> None:
        if len(self.frame_index) != len(self.intervention):
            raise ValueError("frame_index and intervention must have the same length")
        if not 0 <= self.index < _MAX_COUNTER or self.length < 0:
            raise ValueError("index must be 0 to 10**16 - 1 and length not negative")

    @property
    def span(self) -> int:
        """Frames the episode covers: its declared length or past its last frame, the larger."""
        return max(self.length, self.frame_index[-1] + 1 if self.frame_index else 0)


@dataclass(frozen=True, slots=True)
class ImportSettings:
    """What :func:`episode_events` needs: the robot, the frame rate (frames per second), the
    hashes of the registries copied beside each log, ``heartbeat_interval_s`` from the spec's
    ``rules.toml``, the devices and the observed policy and teleoperator (6.10)."""

    robot_id: str
    fps: Fraction
    task_registry_hash: str
    plan_hash: str
    device_registry_hash: str
    heartbeat_interval_s: float
    devices: tuple[Device, ...] = ()
    policy_path: str | None = None
    teleop: str | None = None
    simulated: bool = False
    scopes: tuple[str, ...] = ("whole_robot",)
    anchor: str = "start"

    def __post_init__(self) -> None:
        if not self.fps > 0 or not self.heartbeat_interval_s > 0:
            raise ValueError("fps and heartbeat_interval_s must be positive")
        if not self.scopes:
            raise ValueError("scopes must name at least one scope")


def frame_ns(start_ns: int, frames: int, fps: Fraction) -> int:
    """The instant of frame ``frames`` after ``start_ns``: ``start + frames / fps`` seconds, in
    nanoseconds rounded to the nearest (definitions.md 8: a dataset's ``timestamp`` is
    ``frame_index / fps``)."""
    return start_ns + round(Fraction(frames * _NS_PER_S) / fps)


def session_id(robot_id: str, episode_index: int, start_ns: int) -> str:
    """A session id of the recorder's form ``<counter>-<YYYYMMDD>T<hhmmss>Z-<4 hex>`` (11.3) with
    the episode index as counter, so a missing episode shows in the census; the hex digits are
    the start of the SHA-256 of the robot, episode and start."""
    stamp = ns_to_ts(start_ns)
    compact = stamp[:19].replace("-", "").replace(":", "") + "Z"
    digest = hashlib.sha256(f"{robot_id}|{episode_index}|{start_ns}".encode()).hexdigest()[:4]
    return f"{episode_index:06d}-{compact}-{digest}"


def _labels(settings: ImportSettings, episode: Episode) -> list[tuple[int, _Control]]:
    """(frame, control) at each change of the label, with ``unknown`` for missing frames and
    labels; the scope starts ``unknown``, so a change into it at frame 0 is not listed."""
    ai = _ai(settings.devices, settings.policy_path)
    human = _human(settings.devices, settings.teleop, False, "human_initiated")
    unknown = _Control("unknown")
    changes: list[tuple[int, _Control]] = []
    current, expected = unknown, 0
    for frame, label in zip(episode.frame_index, episode.intervention, strict=True):
        if frame < expected:
            continue
        if frame > expected and current != unknown:
            changes.append((expected, unknown))
            current = unknown
        value = unknown if label is None else human if label else ai
        if value != current:
            changes.append((frame, value))
            current = value
        expected = frame + 1
    if episode.length > expected and current != unknown:
        changes.append((expected, unknown))
    return changes


def episode_events(
    settings: ImportSettings, episode: Episode, start_ns: int, task_definition_id: str
) -> list[dict[str, Any]]:
    """One episode as the events of one session, in ``seq`` order (definitions.md 8): its time
    between ``start_ns`` and ``start_ns + span / fps`` is all that was observed; nothing before or
    after it was. Every event carries ``x_ts_reconstructed: true`` (8, 10.6)."""
    sid = session_id(settings.robot_id, episode.index, start_ns)
    task_id = f"episode_{episode.index:06d}"
    end_ns = frame_ns(start_ns, episode.span, settings.fps)
    frame_clock = "frame_index"
    items: list[tuple[int, int, dict[str, Any]]] = []
    start: dict[str, Any] = {
        "type": "session_start",
        "task_id": None,
        "scopes": list(settings.scopes),
        "initial_states": dict.fromkeys(settings.scopes, "unknown"),
        "heartbeat_interval_s": settings.heartbeat_interval_s,
        "hint_policy": "hint_as_shared",
        "task_registry_hash": settings.task_registry_hash,
        "plan_hash": settings.plan_hash,
        "device_registry_hash": settings.device_registry_hash,
        "simulated": settings.simulated,
        "unobserved": list(UNOBSERVED),
        "x_episode_index": episode.index,
        "x_fps": float(settings.fps),
        "x_ts_anchor": settings.anchor,
        "x_source_format": "LeRobotDataset v3.0",
    }
    items.append((start_ns, 0, start))
    items.append(
        (
            start_ns,
            1,
            {
                "type": "task_start",
                "task_id": task_id,
                "task_definition_id": task_definition_id,
                "x_episode_index": episode.index,
                "x_src_ts": 0,
                "x_src_clock": frame_clock,
            },
        )
    )
    for frame, control in _labels(settings, episode):
        for scope in settings.scopes:
            event = wire_event(_assertion(scope, control))
            event.update(task_id=task_id, x_src_ts=frame, x_src_clock=frame_clock)
            items.append((frame_ns(start_ns, frame, settings.fps), 2, event))
    step = round(settings.heartbeat_interval_s * _NS_PER_S)
    beat = start_ns + step
    while beat < end_ns:
        items.append(
            (beat, 3, {"type": "heartbeat", "task_id": task_id, "clock_source": "unknown"})
        )
        beat += step
    end_fields = {"x_src_ts": episode.span, "x_src_clock": frame_clock}
    task_end = {"type": "task_end", "task_id": task_id, "outcome": "unknown", **end_fields}
    items.append((end_ns, 4, task_end))
    items.append((end_ns, 5, {"type": "session_end", "task_id": None, **end_fields}))
    items.sort(key=lambda item: (item[0], item[1]))
    events = []
    for seq, (ns, _, event) in enumerate(items):
        common = {"robot_id": settings.robot_id, "session_id": sid, "seq": seq, "ts": ns_to_ts(ns)}
        events.append({**common, **event, "x_ts_reconstructed": True})
    return events
