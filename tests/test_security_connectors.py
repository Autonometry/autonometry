"""Security review M10 (docs/security-review-2026-11.md): the connectors against what anyone on
the robot's network, or whoever wrote a dataset or a rollout log, can send them. A hostile input
never stops a connector and never makes time count as ``ai`` that is not (product rule 2)."""

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autonometry.connectors import g1, g1_dds, lerobot, lerobot_io, tb4
from autonometry.connectors.g1_dds import SampleRecord, convert
from autonometry.connectors.tb4 import Source, StateAssertion
from g1_helpers import (
    POLICY_LOWCMD,
    XR_SWITCH,
    Sim,
    cause,
    command,
    remote_bytes,
    request,
    velocity,
)
from lerobot_helpers import CONFIG, engine, feed, line, setup_lines, write_dataset

# -- Unitree G1 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("pid", [chr(0xB2), chr(0x663), chr(0xFF11), "12345678901", "-1", ""])
def test_a_participant_pid_that_is_not_ascii_digits_is_ignored__s20(pid: str) -> None:
    """S-20: str.isdigit() accepts "²", which int() refuses: one participant property crashed
    discover and every watch pass that saw it."""
    found = g1_dds._participant({"__Pid": pid, "__ProcessName": "python3"}, "h", lambda p, n: None)
    assert found.pid is None


def test_a_failing_dds_read_never_drops_a_task_change__s20() -> None:
    """S-20: the pass took the task follower's changes, then lost them when reading DDS
    raised."""

    class Tasks:
        def poll(self, now: float) -> list[g1.Observation]:
            return [g1.TaskClosed(now, "t1")]

    class Broken:
        def poll(self, now: float) -> list[g1.Observation]:
            raise ValueError("a hostile participant")

    seen: list[g1.Observation] = []

    class Recording:
        def observe(self, observation: g1.Observation) -> list[g1.Message]:
            seen.append(observation)
            return []

        def tick(self, now: float) -> list[g1.Message]:
            return []

    with pytest.raises(ValueError, match="hostile"):
        g1_dds.pump(Broken(), Tasks(), Recording(), lambda m: None, 1.0, tick=True)  # type: ignore[arg-type]
    assert seen == [g1.TaskClosed(1.0, "t1")]


def _stopped_sim() -> Sim:
    """Released by the operator, the policy writing rt/lowcmd, then L2 + B on the remote."""
    sim = Sim()
    sim.step(request(0, XR_SWITCH, "motion_switcher", 1003, {}, request_id=1))
    sim.run(2.0, command(0, POLICY_LOWCMD))
    sim.remote = remote_bytes("L2", "B")
    sim.step(command(0, POLICY_LOWCMD))
    sim.remote = remote_bytes()
    sim.run(3.0, command(0, POLICY_LOWCMD))
    assert sim.state().state == "safety_stop"
    return sim


def test_a_refused_mode_request_never_ends_an_emergency_stop__s21() -> None:
    """S-21: any participant could send a SelectMode the robot refuses; the request ended the
    stop and the policy that kept writing counted as ai while the robot stood stopped."""
    sim = _stopped_sim()
    sim.feed(
        g1.WriterSeen(
            0.0,
            "w-evil",
            Source(host="laptop", participant="python3", topic=g1.request_topic("motion_switcher")),
            "",
        )
    )
    sim.step(
        command(0, POLICY_LOWCMD),
        request(0, "w-evil", "motion_switcher", 1002, {"name": "no_such_mode"}, request_id=77),
    )
    sim.step(
        command(0, POLICY_LOWCMD),
        g1.Response(0, "w-robot", "motion_switcher", 77, 1002, 3104, ""),
    )
    sim.run(10.0, command(0, POLICY_LOWCMD))
    assert sim.state().state == "safety_stop"


def _arm_sample(q: float) -> SampleRecord:
    motors = [SimpleNamespace(q=0.0) for _ in range(35)]
    motors[29] = SimpleNamespace(q=q)
    return SampleRecord(g1.ARM_SDK, 5, SimpleNamespace(motor_cmd=motors))


@pytest.mark.parametrize("first", [None, 0.0])
def test_an_unreadable_blend_weight_counts_as_1_not_as_the_last_one__s23(
    first: float | None,
) -> None:
    """S-23: weight 0, then a non-finite weight, kept 0: a person driving the arms through
    rt/arm_sdk stayed hidden behind the built-in controller's ai."""
    sim = Sim()
    sim.feed(
        g1.WriterSeen(
            0.0, "w-arm", Source(host="laptop", participant="python3", topic=g1.ARM_SDK), ""
        )
    )
    sim.run(1.0)
    if first is not None:
        sim.run(1.5, convert(_arm_sample(first), "w-arm", 0.0))
    sim.run(8.0, convert(_arm_sample(float("inf")), "w-arm", 0.0))
    upper = sim.state(g1.UPPER_BODY)
    assert upper.state != "ai", cause(upper)


@pytest.mark.parametrize(
    ("service", "api", "params"),
    [("arm", g1.ARM_EXECUTE, {"action_id": 1e300}), ("sport", g1.SET_ARM_TASK, {"data": 1e200})],
)
def test_a_huge_request_number_still_makes_a_valid_actor_id__s25(
    service: str, api: int, params: dict[str, Any]
) -> None:
    """S-25: a 301-digit action number made an actor id of 319 characters; the recorder rejected
    every state and all scopes went unknown while the preset ran."""
    sim = Sim()
    sim.run(1.0)
    sim.step(request(0, XR_SWITCH, service, api, params))
    sim.run(2.0)
    actors = [value.actor_id for _, value in sim.history if value.actor_id is not None]
    assert any(actor.startswith("script:") for actor in actors)
    assert all(len(actor) <= 128 for actor in actors)


def test_goal_spans_are_capped__s24() -> None:
    """S-24: every participant could open a goal span that lived for its velocity's duration;
    spans were never capped and each observation scanned them all."""
    sim = Sim()
    for i in range(g1.MAX_SPANS + 50):
        key = f"w-{i}"
        sim.feed(
            g1.WriterSeen(
                0.0,
                key,
                Source(host="h", participant=f"p{i}", topic=g1.request_topic("sport")),
                "",
            )
        )
        sim.feed(velocity(0.0, key, 0.5, duration=864000))
    assert len(sim.engine._spans) <= g1.MAX_SPANS


def test_discover_reports_many_endpoints_in_linear_time__s28() -> None:
    """S-28: discover matched every writer against every participant: 16k of each took 22 s,
    and anyone on the network can announce endpoints."""
    n = 30_000

    class Backend:
        participant_key = "self"

        def participants(self) -> list[g1_dds.ParticipantRecord]:
            return [g1_dds.ParticipantRecord(f"p{i}", {"__ProcessName": f"x{i}"}) for i in range(n)]

        def publications(self) -> list[g1_dds.PublicationRecord]:
            return [g1_dds.PublicationRecord(f"w{i}", i, f"p{i}", g1.LOWCMD, "t") for i in range(n)]

    found = g1_dds.Discovery()
    found.take(Backend(), "h", lambda pid, name: None)  # type: ignore[arg-type]
    began = time.perf_counter()
    report = found.report()
    assert time.perf_counter() - began < 10.0
    assert len(report["participants"]) == n


def test_an_arm_state_is_parsed_as_sent__s30() -> None:
    """S-30: the arm state's text was made printable before parsing, so JSON with a newline
    became ``{?`` and was ignored."""
    record = SampleRecord(
        g1.ARM_STATE, 0, SimpleNamespace(data='{\n "holding": true,\n "id": 3\n}')
    )
    observation = convert(record, "k", 0.0)
    assert isinstance(observation, g1.ArmState)
    assert json.loads(observation.text) == {"holding": True, "id": 3}


# -- TurtleBot 4 and the station ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("command_", "smoothed"),
    [
        ((0.3, 0.0, 0.0), (1.2e154, 1.2e154, 0.0)),
        ((-1e200, 1e200, 0.0), (1e200, 1e200, 0.0)),
        ((float("inf"), 0.0, 0.0), (0.3, 0.0, 0.0)),
        ((0.3, 0.0, 0.0), (float("nan"), 0.0, 0.0)),
    ],
)
def test_twists_whose_arithmetic_overflows_are_no_relay__s41(
    command_: tuple[float, float, float], smoothed: tuple[float, float, float]
) -> None:
    """S-41: a finite but huge /cmd_vel_smoothed made relays() raise inside observe(); the node
    swallowed it and dropped every /cmd_vel of an unknown publisher, a person's included."""
    assert tb4.relays(command_, smoothed) is False


def test_a_real_relay_is_still_recognised__s41() -> None:
    assert tb4.relays((0.15, 0.0, 0.1), (0.3, 0.0, 0.2))


# -- LeRobot ------------------------------------------------------------------------------------


class _Watch:
    def __init__(self, tmp_path: Path) -> None:
        self.log = tmp_path / "rollout.log"
        self.log.write_text("", encoding="utf-8")
        self.follower = lerobot_io.LogFollower(self.log)
        self.engine = engine()
        self.sent: list[lerobot.Message] = []

    def write(self, *messages: str, at: float = 0.0) -> None:
        with open(self.log, "a", encoding="utf-8") as stream:
            stream.writelines(line(m, at) + "\n" for m in messages)

    def pump(self, now: float) -> list[str]:
        start = len(self.sent)
        lerobot_io.pump(self.follower, self.engine, self.sent.extend, now)
        return [m.state for m in self.sent[start:] if isinstance(m, StateAssertion)]

    def started(self) -> None:
        self.write(*(text.split(" ", 4)[-1] for _, text in setup_lines()))
        self.write("DAgger continuous recording started (episode_duration=60s)", at=5)
        assert self.pump(0.0)[-1] == "ai"


@pytest.mark.parametrize("change", ["deleted", "replaced", "cut"])
def test_a_log_that_vanishes_or_is_replaced_is_unknown_not_its_last_state__s43(
    tmp_path: Path, change: str
) -> None:
    """S-43: after the rollout log was moved away (log rotation) the watcher kept the last
    state, ai, for as long as the rollout ran, with nothing to show for it."""
    watch = _Watch(tmp_path)
    watch.started()
    watch.follower.close()  # Windows renames no file a process holds open
    if change in ("deleted", "replaced"):
        os.replace(watch.log, tmp_path / "rollout.log.1")
    if change in ("replaced", "cut"):
        watch.log.write_text("", encoding="utf-8")
    assert watch.pump(1.0)[-1] == "unknown"
    assert watch.pump(3600.0) == ["unknown"]
    assert lerobot.Note((("x_lerobot_log", "lost"),)) in watch.sent
    watch.follower.close()


def test_a_late_line_from_a_dead_rollout_never_brings_its_state_back__s44() -> None:
    """S-44: a line flushed after the rollout's process was known gone re-attached the engine,
    and its ai state then held for good."""
    eng = engine()
    feed(eng, [*setup_lines(), (5.0, line("Phase transition: correcting -> autonomous", 5))])
    assert {a.state for a in eng.tick(5.5)} == {"ai"}
    eng.process_gone(6.0)
    late = lerobot.parse_line(line("Phase transition: correcting -> autonomous", 7))
    assert late is not None
    eng.observe(7.0, late)
    assert {a.state for a in eng.tick(3600.0)} == {"unknown"}


def test_repeated_episode_starts_cost_linear_time__s53() -> None:
    """S-53: each repeat of an episode's start searched every earlier task id: 8000 repeats took
    20 s, pinning the watcher on the robot's computer."""
    eng = engine()
    feed(eng, setup_lines())
    parsed = lerobot.parse_line(line("Recording episode 0", 1))
    assert parsed is not None
    began = time.perf_counter()
    for i in range(20_000):
        eng.observe(1.0 + i * 1e-3, parsed)
    assert time.perf_counter() - began < 10.0


_EPISODES = [(0, "pick", [False, True, False]), (1, "pick", [False, False])]


def _import(dataset: Path, out: Path) -> lerobot_io.ImportResult:
    registries = lerobot_io.load_registries(
        CONFIG / "devices.json", CONFIG / "tasks.json", CONFIG / "plan.json", "0.3"
    )
    return lerobot_io.import_dataset(
        dataset, out, registries, heartbeat_interval_s=1.0, start_ns=1_800_000_000 * 10**9
    )


@pytest.mark.parametrize("forged", [{"lengths": {0: 30_000_000}}, {"frames": {0: [0, 1, 2**40]}}])
def test_a_forged_episode_length_or_frame_index_is_refused_before_writing__s40(
    tmp_path: Path, forged: dict[str, Any]
) -> None:
    """S-40: a 2 KB dataset declaring a 30-million-frame episode made the importer build and
    write 209 MB of heartbeats; a frame index of 2^62 never finished."""
    dataset = write_dataset(tmp_path / "ds", _EPISODES, **forged)
    began = time.perf_counter()
    with pytest.raises(lerobot_io.LeRobotError, match="more than 86400 s"):
        _import(dataset, tmp_path / "out")
    assert time.perf_counter() - began < 10.0
    if "lengths" in forged:  # refused from the metadata alone: nothing was written
        assert not list((tmp_path / "out").glob("*/*/events.jsonl"))


def test_a_file_declaring_too_many_rows_is_refused_unread__s40(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = write_dataset(tmp_path / "ds", _EPISODES)
    monkeypatch.setattr(lerobot_io, "MAX_ROWS", 4)
    with pytest.raises(lerobot_io.LeRobotError, match="rows, more than 4"):
        _import(dataset, tmp_path / "out")


def test_an_import_never_replaces_a_log_already_written__s50(tmp_path: Path) -> None:
    """S-50: importing a second dataset into the same folder silently replaced the first
    dataset's session logs."""
    dataset = write_dataset(tmp_path / "ds", _EPISODES)
    first = _import(dataset, tmp_path / "out")
    logs = [folder / "events.jsonl" for folder in first.sessions]
    before = [log.read_bytes() for log in logs]
    with pytest.raises(lerobot_io.LeRobotError, match="already holds a session log"):
        _import(dataset, tmp_path / "out")
    assert [log.read_bytes() for log in logs] == before


@pytest.mark.skipif(sys.platform != "win32", reason="':' and device names are Windows paths")
@pytest.mark.parametrize("robot_type", ["C:", "NUL", "so101:a"])
def test_a_robot_type_that_is_no_windows_folder_is_refused__s49(
    tmp_path: Path, robot_type: str
) -> None:
    """S-49: robot_type "C:" from info.json wrote outside --out (drive C's current folder);
    "NUL" and "so101:a" crashed the import."""
    from lerobot_helpers import info  # noqa: PLC0415

    dataset = write_dataset(tmp_path / "ds", _EPISODES, info_doc=info(robot_type=robot_type))
    with pytest.raises(lerobot_io.LeRobotError):
        _import(dataset, tmp_path / "out")
