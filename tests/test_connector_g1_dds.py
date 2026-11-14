"""The Unitree G1 DDS edge (``autonometry.connectors.g1_dds``) without Cyclone DDS: version
guard, identities, sample conversion, the observer over a fake backend, discovery, the
session-log task follower and the watch loop. The same code over real Cyclone DDS 0.10.5 runs in
``tests/dds`` (marker ``dds``)."""

import json
import logging
import math
import os
import sys
import threading
import types
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from autonometry.connectors import g1, g1_dds
from autonometry.connectors.g1_dds import (
    G1Error,
    Observer,
    ParticipantRecord,
    PublicationRecord,
    SampleRecord,
    TaskFollower,
)
from autonometry.connectors.tb4 import Source
from g1_helpers import engine, remote_bytes

LOCAL = "robot-pc2"
LOWCMD_TYPE = "unitree_hg::msg::dds_::LowCmd_"


# -- version guard ------------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.10.5", "0.10.2", "0.10.6rc1", "0.10"])
def test_the_0_10_line_is_accepted__notes_3_s11(version: str) -> None:
    assert g1_dds.check_cyclonedds(version) == version


@pytest.mark.parametrize("version", ["11.1.0", "0.11.0", "0.9.1", "garbage", ""])
def test_any_other_cyclone_dds_is_refused_in_one_line__notes_3_s11(version: str) -> None:
    with pytest.raises(G1Error, match="is not supported") as caught:
        g1_dds.check_cyclonedds(version)
    assert "\n" not in str(caught.value)
    assert "0.10.5" in str(caught.value)


def test_no_cyclone_dds_is_refused__notes_3() -> None:
    with pytest.raises(G1Error, match="not installed"):
        g1_dds.check_cyclonedds(None)


def test_cyclonedds_version_reads_the_installed_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> str:
        raise g1_dds.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(g1_dds.importlib.metadata, "version", missing)
    assert g1_dds.cyclonedds_version() is None
    monkeypatch.setattr(g1_dds.importlib.metadata, "version", lambda name: "0.10.5")
    assert g1_dds.cyclonedds_version() == "0.10.5"


def test_domain_config_binds_an_interface_as_unitrees_sdk_does__notes_3() -> None:
    assert g1_dds.domain_config(None) is None
    xml = g1_dds.domain_config("enp3s0")
    assert xml is not None
    assert '<NetworkInterface name="enp3s0" priority="default" multicast="default"/>' in xml
    with pytest.raises(G1Error, match="interface"):
        g1_dds.domain_config('eth0"/><Tracing')


def test_open_backend_refuses_a_domain_id_out_of_range() -> None:
    with pytest.raises(G1Error, match="--domain"):
        g1_dds.open_backend(233, None)


# -- identities ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "target"),
    [
        (["teleop_hand_and_arm.py", "--arm=G1_29"], "teleop_hand_and_arm.py"),
        (["-u", "/opt/xr/teleop/teleop_hand_and_arm.py"], "teleop_hand_and_arm.py"),
        (["-W", "ignore", "eval_g1.py"], "eval_g1.py"),
        (["-m", "unitree_lerobot.eval_robot.eval_g1"], "unitree_lerobot.eval_robot.eval_g1"),
        (["-mpkg.tool"], "pkg.tool"),
        (["-m"], None),
        (["-c", "import x"], None),
        (["-"], None),
        ([], None),
        (["-B", "--", "x.py"], "x.py"),
    ],
)
def test_python_target_is_the_script_or_module__notes_4(
    argv: list[str], target: str | None
) -> None:
    assert g1_dds.python_target(argv) == target


class _FakeProcess:
    table: ClassVar[dict[int, tuple[str, list[str]]]] = {}

    def __init__(self, pid: int) -> None:
        if pid not in self.table:
            raise _FakePsutilError
        self._name, self._argv = self.table[pid]

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return self._argv


class _FakePsutilError(Exception):
    pass


@pytest.fixture
def fake_psutil(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[int, tuple[str, list[str]]]]:
    module = types.ModuleType("psutil")
    module.Process = _FakeProcess  # type: ignore[attr-defined]
    module.Error = _FakePsutilError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", module)
    _FakeProcess.table = {}
    return _FakeProcess.table


def test_resolve_script_reads_the_command_line_while_the_pid_runs_that_program__notes_4(
    fake_psutil: dict[int, tuple[str, list[str]]],
) -> None:
    fake_psutil[10] = ("python3", ["/usr/bin/python3", "teleop_hand_and_arm.py"])
    fake_psutil[11] = ("bash", ["bash"])
    fake_psutil[12] = ("python3.10", ["python3", "-m", "pkg.run"])
    assert g1_dds.resolve_script(10, "python3") == "teleop_hand_and_arm.py"
    assert g1_dds.resolve_script(11, "python3") is None  # the pid now runs another program
    assert g1_dds.resolve_script(12, "python3") == "pkg.run"  # argv[0] names it
    assert g1_dds.resolve_script(99, "python3") is None  # gone


def test_participant_name_is_the_script_only_for_a_local_interpreter__notes_4() -> None:
    def resolve(pid: int, program: str) -> str | None:
        return {1: "policy.py", 2: None, 3: "bad\nname"}.get(pid)

    assert g1_dds.participant_name(LOCAL, "python3", 1, LOCAL, resolve) == "policy.py"
    assert g1_dds.participant_name("other", "python3", 1, LOCAL, resolve) == "python3"
    assert g1_dds.participant_name(LOCAL, "python3", 2, LOCAL, resolve) == "python3"
    assert g1_dds.participant_name(LOCAL, "python3", 3, LOCAL, resolve) == "bad?name"
    assert g1_dds.participant_name(LOCAL, "ai_sport", 1, LOCAL, resolve) == "ai_sport"
    assert g1_dds.participant_name(LOCAL, "python3", None, LOCAL, resolve) == "python3"
    assert g1_dds.participant_name(None, None, None, LOCAL, resolve) is None


def test_properties_are_read_from_the_participant_qos__notes_s10() -> None:
    qos = [
        SimpleNamespace(key="__Hostname", value="pc1"),
        SimpleNamespace(key="__Pid", value="42"),
        SimpleNamespace(key="other", value="x"),
        SimpleNamespace(scope="Reliability"),
    ]
    assert g1_dds._properties(qos) == {"__Hostname": "pc1", "__Pid": "42"}
    assert g1_dds._properties(42) == {}


# -- samples ------------------------------------------------------------------------------------


def _lowcmd(weight: float) -> SimpleNamespace:
    motors = [SimpleNamespace(q=0.0) for _ in range(35)]
    motors[29] = SimpleNamespace(q=weight)
    return SimpleNamespace(motor_cmd=motors)


def _request(api: int, text: Any = "{}", rid: int = 5) -> SimpleNamespace:
    identity = SimpleNamespace(id=rid, api_id=api)
    return SimpleNamespace(header=SimpleNamespace(identity=identity), parameter=text)


def _response(api: int, code: int, data: Any) -> SimpleNamespace:
    identity = SimpleNamespace(id=5, api_id=api)
    status = SimpleNamespace(code=code)
    return SimpleNamespace(header=SimpleNamespace(identity=identity, status=status), data=data)


def test_convert_reads_what_the_rules_need_from_each_topic__notes_s1_to_s9() -> None:
    remote = list(remote_bytes("L2", "B", lx=0.5))
    cases: list[tuple[str, Any, g1.Observation | None]] = [
        (
            g1.LOWSTATE,
            SimpleNamespace(wireless_remote=remote),
            g1.LowState(1.0, "k", bytes(remote)),
        ),
        (g1.LOWSTATE, SimpleNamespace(wireless_remote=None), g1.LowState(1.0, "k", b"")),
        (
            g1.SPORTSTATE,
            SimpleNamespace(fsm_id=500, fsm_mode=1, task_id=True),
            g1.SportState(1.0, "k", 500, 1, 0),
        ),
        (g1.ARM_SDK, _lowcmd(0.75), g1.Command(1.0, "k", g1.ARM_SDK, 0.75)),
        (g1.ARM_SDK, _lowcmd(math.nan), g1.Command(1.0, "k", g1.ARM_SDK, None)),
        (g1.ARM_SDK, SimpleNamespace(motor_cmd=[]), g1.Command(1.0, "k", g1.ARM_SDK, None)),
        (g1.LOWCMD, _lowcmd(0.75), g1.Command(1.0, "k", g1.LOWCMD, None)),
        (
            g1.HAND_SDK,
            SimpleNamespace(cmds=[SimpleNamespace(mode=60)]),
            g1.Command(1.0, "k", g1.HAND_SDK, 0.6),
        ),
        (g1.HAND_SDK, SimpleNamespace(cmds=[]), g1.Command(1.0, "k", g1.HAND_SDK, None)),
        (
            g1.ARM_STATE,
            SimpleNamespace(data='{"holding": true}'),
            g1.ArmState(1.0, "k", '{"holding": true}'),
        ),
        (
            g1.request_topic("sport"),
            _request(7105, '{"velocity": [0.3, 0, 0]}'),
            g1.Request(1.0, "k", "sport", 5, 7105, '{"velocity": [0.3, 0, 0]}'),
        ),
        (
            g1.request_topic("arm"),
            _request(7106, None),
            g1.Request(1.0, "k", "arm", 5, 7106, ""),
        ),
        (
            g1.response_topic("motion_switcher"),
            _response(1001, 0, '{"name": ""}'),
            g1.Response(1.0, "k", "motion_switcher", 5, 1001, 0, '{"name": ""}'),
        ),
        (
            g1.response_topic("sport"),
            _response(7105, 3, b"x"),
            g1.Response(1.0, "k", "sport", 5, 7105, 3, ""),
        ),
        (g1.SPORTSTATE, SimpleNamespace(), None),
        ("rt/other", SimpleNamespace(), None),
    ]
    for topic, value, want in cases:
        assert g1_dds.convert(SampleRecord(topic, 7, value), "k", 1.0) == want, topic


# -- the observer -------------------------------------------------------------------------------


class FakeBackend:
    """Records to hand out per call, and the readers asked for."""

    def __init__(self) -> None:
        self.parts: list[ParticipantRecord] = []
        self.pubs: list[PublicationRecord] = []
        self.data: list[SampleRecord] = []
        self.opened: list[tuple[str, str]] = []
        self.known = {LOWCMD_TYPE, "unitree_hg::msg::dds_::LowState_"}

    @property
    def participant_key(self) -> str:
        return "me"

    def participants(self) -> list[ParticipantRecord]:
        out, self.parts = self.parts, []
        return out

    def publications(self) -> list[PublicationRecord]:
        out, self.pubs = self.pubs, []
        return out

    def open_reader(self, topic: str, type_name: str) -> bool:
        self.opened.append((topic, type_name))
        return type_name in self.known

    def samples(self) -> list[SampleRecord]:
        out, self.data = self.data, []
        return out


def _props(host: str, program: str, pid: int) -> dict[str, str]:
    return {"__Hostname": host, "__ProcessName": program, "__Pid": str(pid)}


def _observer(backend: FakeBackend) -> Observer:
    scripts = {7: "teleop_hand_and_arm.py"}
    return Observer(backend, LOCAL, lambda pid, program: scripts.get(pid))


def test_the_observer_names_each_writer_by_host_participant_and_topic__notes_4() -> None:
    backend = FakeBackend()
    observer = _observer(backend)
    backend.parts = [
        ParticipantRecord("p-xr", _props(LOCAL, "python3", 7)),
        ParticipantRecord("p-robot", _props("g1-pc1", "hg_service", 100)),
        ParticipantRecord("me", _props(LOCAL, "python3", 8)),
    ]
    backend.pubs = [
        PublicationRecord("w1", 11, "p-xr", g1.ARM_SDK, "unitree_hg.msg.dds_.LowCmd_"),
        PublicationRecord("w2", 12, "p-robot", g1.LOWSTATE, "unitree_hg::msg::dds_::LowState_"),
        PublicationRecord("w3", 13, "p-xr", "rt/dex3/left/cmd", "unitree_hg::msg::dds_::HandCmd_"),
        PublicationRecord("w4", 14, "p-xr", "rt/inspire/cmd", "unitree_go::msg::dds_::MotorCmds_"),
        PublicationRecord("w5", 15, "p-xr", "rt/camera", "sensor_msgs::Image"),
        PublicationRecord("w6", 16, "me", g1.ARM_SDK, LOWCMD_TYPE),
    ]
    seen = observer.poll(1.0)
    assert seen == [
        g1.WriterSeen(
            1.0,
            "w1",
            Source(host=LOCAL, participant="teleop_hand_and_arm.py", topic=g1.ARM_SDK),
            "unitree_hg.msg.dds_.LowCmd_",
            True,
        ),
        g1.WriterSeen(
            1.0,
            "w2",
            Source(host="g1-pc1", participant="hg_service", topic=g1.LOWSTATE),
            "unitree_hg::msg::dds_::LowState_",
            True,
        ),
        g1.WriterSeen(
            1.0,
            "w3",
            Source(host=LOCAL, participant="teleop_hand_and_arm.py", topic="rt/dex3/left/cmd"),
            "unitree_hg::msg::dds_::HandCmd_",
            False,  # the backend could not open that reader
        ),
        g1.WriterSeen(
            1.0,
            "w4",
            Source(host=LOCAL, participant="teleop_hand_and_arm.py", topic="rt/inspire/cmd"),
            "unitree_go::msg::dds_::MotorCmds_",
            False,  # an unconfirmed type: presence only
        ),
    ]
    assert backend.opened == [
        (g1.ARM_SDK, LOWCMD_TYPE),
        (g1.LOWSTATE, "unitree_hg::msg::dds_::LowState_"),
        ("rt/dex3/left/cmd", "unitree_hg::msg::dds_::HandCmd_"),
    ]


def test_samples_map_to_their_writer_by_publication_handle__notes_s10() -> None:
    backend = FakeBackend()
    observer = _observer(backend)
    backend.pubs = [
        PublicationRecord("w1", 11, "p-xr", g1.ARM_SDK, LOWCMD_TYPE),
        PublicationRecord("w2", 12, "p-pol", g1.LOWCMD, LOWCMD_TYPE),
        PublicationRecord("w9", 19, "p-robot", g1.SPORTSTATE, "x"),
    ]
    observer.poll(0.5)
    backend.data = [
        SampleRecord(g1.ARM_SDK, 11, _lowcmd(0.2)),
        SampleRecord(g1.ARM_SDK, 11, _lowcmd(0.9)),
        SampleRecord(g1.LOWCMD, 12, _lowcmd(0.0)),
        SampleRecord(g1.LOWCMD, 77, _lowcmd(0.0)),
        SampleRecord(g1.SPORTSTATE, 19, SimpleNamespace(fsm_id=500, fsm_mode=0, task_id=0)),
        SampleRecord(g1.SPORTSTATE, 19, SimpleNamespace(fsm_id=500, fsm_mode=0, task_id=0)),
        SampleRecord(g1.SPORTSTATE, 19, SimpleNamespace(fsm_id=1, fsm_mode=0, task_id=0)),
        SampleRecord(g1.LOWSTATE, 30, SimpleNamespace(wireless_remote=[0] * 40)),
        SampleRecord(g1.LOWSTATE, 30, SimpleNamespace(wireless_remote=[1] * 40)),
        SampleRecord(g1.request_topic("sport"), 40, _request(7105)),
        SampleRecord(g1.request_topic("sport"), 40, _request(7105)),
    ]
    out = observer.poll(1.0)
    assert out == [
        g1.Command(1.0, "w1", g1.ARM_SDK, 0.9),
        g1.Command(1.0, "w2", g1.LOWCMD, None),
        g1.Command(1.0, "handle:77", g1.LOWCMD, None),
        g1.SportState(1.0, "w9", 500, 0, 0),
        g1.SportState(1.0, "w9", 1, 0, 0),
        g1.LowState(1.0, "handle:30", bytes([1] * 40)),
        g1.Request(1.0, "handle:40", "sport", 5, 7105, "{}"),
        g1.Request(1.0, "handle:40", "sport", 5, 7105, "{}"),
    ]


def test_writers_and_participants_that_go_are_reported_gone__notes_3() -> None:
    backend = FakeBackend()
    observer = _observer(backend)
    backend.pubs = [
        PublicationRecord("w1", 11, "p-xr", g1.ARM_SDK, LOWCMD_TYPE),
        PublicationRecord("w2", 12, "p-xr", g1.LOWCMD, LOWCMD_TYPE),
        PublicationRecord("w3", 13, "p-pol", g1.LOWCMD, LOWCMD_TYPE),
    ]
    observer.poll(0.0)
    backend.pubs = [
        PublicationRecord("w1", 11, "p-xr", g1.ARM_SDK, LOWCMD_TYPE, alive=False),
        PublicationRecord("w8", 18, "p-xr", g1.ARM_SDK, LOWCMD_TYPE, alive=False),
    ]
    assert observer.poll(1.0) == [g1.WriterGone(1.0, "w1")]
    backend.parts = [ParticipantRecord("p-xr", {}, alive=False)]
    assert observer.poll(2.0) == [g1.WriterGone(2.0, "w2")]
    backend.data = [SampleRecord(g1.LOWCMD, 12, _lowcmd(0.0))]
    assert observer.poll(3.0) == [g1.Command(3.0, "handle:12", g1.LOWCMD, None)]


def test_a_participant_seen_after_its_writers_names_them_again__notes_4() -> None:
    backend = FakeBackend()
    observer = _observer(backend)
    backend.pubs = [PublicationRecord("w1", 11, "p-pol", g1.LOWCMD, LOWCMD_TYPE)]
    (first,) = observer.poll(0.0)
    assert isinstance(first, g1.WriterSeen)
    assert first.source == Source(topic=g1.LOWCMD)
    backend.parts = [ParticipantRecord("p-pol", _props("g1-pc2", "eval_g1", 9))]
    (again,) = observer.poll(1.0)
    assert isinstance(again, g1.WriterSeen)
    assert again.source == Source(host="g1-pc2", participant="eval_g1", topic=g1.LOWCMD)
    backend.parts = [ParticipantRecord("p-pol", _props("g1-pc2", "eval_g1", 9))]
    assert observer.poll(2.0) == []  # nothing changed


def test_the_observer_forgets_the_oldest_endpoints_beyond_its_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(g1_dds, "MAX_ENDPOINTS", 2)
    backend = FakeBackend()
    observer = _observer(backend)
    backend.parts = [ParticipantRecord(f"p{k}", _props("h", "x", k)) for k in range(3)]
    backend.pubs = [
        PublicationRecord(f"w{k}", k, f"p{k}", g1.LOWCMD, LOWCMD_TYPE) for k in range(3)
    ]
    observer.poll(0.0)
    assert len(observer._participants) == 2
    assert len(observer._writers) == 2


def test_the_observer_feeds_the_engine_end_to_end__notes_6() -> None:
    backend = FakeBackend()
    observer = _observer(backend)
    backend.parts = [
        ParticipantRecord("p-robot", _props("g1-pc1", "hg_lowstate", 100)),
        ParticipantRecord("p-sport", _props("g1-pc1", "ai_sport", 101)),
    ]
    backend.known.add("unitree_hg::msg::dds_::SportModeState_")
    backend.pubs = [
        PublicationRecord("ls", 1, "p-robot", g1.LOWSTATE, "unitree_hg::msg::dds_::LowState_"),
        PublicationRecord(
            "ss", 2, "p-sport", g1.SPORTSTATE, "unitree_hg::msg::dds_::SportModeState_"
        ),
    ]
    eng = engine()
    sent: list[g1.Message] = []
    tasks = SimpleNamespace(poll=lambda now: [g1.TaskOpened(now, "t1")] if now == 0 else [])
    for k in range(10):
        backend.data = [
            SampleRecord(g1.LOWSTATE, 1, SimpleNamespace(wireless_remote=list(remote_bytes()))),
            SampleRecord(g1.SPORTSTATE, 2, SimpleNamespace(fsm_id=500, fsm_mode=0, task_id=0)),
        ]
        g1_dds.pump(observer, tasks, eng, sent.extend, k * 0.05, tick=True)
    states = [m for m in sent if isinstance(m, g1.StateAssertion)]
    assert (states[-1].state, states[-1].device_id) == ("ai", "g1_builtin")


# -- discovery ----------------------------------------------------------------------------------


def test_discovery_lists_participants_and_the_exact_match_strings__notes_4() -> None:
    backend = FakeBackend()
    backend.parts = [
        ParticipantRecord("b", _props(LOCAL, "python3", 7)),
        ParticipantRecord("a", _props("g1-pc1", "hg_lowstate", 100)),
        ParticipantRecord("me", _props(LOCAL, "python3", 8)),
        ParticipantRecord("c", {}),
        ParticipantRecord("gone", {}, alive=False),
    ]
    backend.pubs = [
        PublicationRecord("w2", 2, "b", g1.ARM_SDK, LOWCMD_TYPE),
        PublicationRecord("w1", 1, "a", g1.LOWSTATE, "unitree_hg::msg::dds_::LowState_"),
        PublicationRecord("w3", 3, "b", "rt/camera", "x"),
        PublicationRecord("w4", 4, "me", g1.LOWCMD, LOWCMD_TYPE),
        PublicationRecord("w5", 5, "b", g1.LOWCMD, LOWCMD_TYPE, alive=False),
    ]
    clock = iter([0.0, 0.0, 0.1, 9.9])
    report = g1_dds.discover(
        backend,
        5.0,
        local_host=LOCAL,
        resolve=lambda pid, program: "teleop_hand_and_arm.py" if pid == 7 else None,
        clock=lambda: next(clock),
        sleep=lambda s: None,
    )
    assert [p["key"] for p in report["participants"]] == ["a", "b", "c"]
    robot, teleop, other = report["participants"]
    assert robot["writers"] == [
        {
            "topic": "rt/lowstate",
            "type": "unitree_hg::msg::dds_::LowState_",
            "match": {"host": "g1-pc1", "participant": "hg_lowstate", "topic": "rt/lowstate"},
            "remote_match": {
                "host": "g1-pc1",
                "participant": "hg_lowstate",
                "topic": "wireless_remote",
            },
        }
    ]
    assert (teleop["program"], teleop["pid"], teleop["participant"]) == (
        "python3",
        7,
        "teleop_hand_and_arm.py",
    )
    assert [w["topic"] for w in teleop["writers"]] == ["rt/arm_sdk", "rt/camera"]
    assert other == {
        "key": "c",
        "host": None,
        "program": None,
        "pid": None,
        "participant": None,
        "writers": [],
    }
    text = g1_dds.format_discovery(report)
    assert text[0] == "participant a: host g1-pc1, program hg_lowstate (pid 100)"
    assert any("handheld remote match" in line for line in text)
    assert "participant b: host robot-pc2, program python3 (pid 7), script " in "\n".join(text)
    assert "  writes nothing" in text
    assert text[-1] == 'Unitree\'s presets: match {"topic": "preset"}'
    json.dumps(report)


# -- tasks --------------------------------------------------------------------------------------


def _session(robot: Path, name: str, *events: dict[str, Any]) -> Path:
    folder = robot / name
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    return folder


def test_the_task_follower_reads_tasks_from_the_current_session_log__notes_8(
    tmp_path: Path,
) -> None:
    robot = tmp_path / "data" / "g1_01"
    first = _session(robot, "000001-20261114T100000Z-aaaa", {"type": "session_start"})
    follower = TaskFollower(robot)
    assert follower.poll(0.0) == [g1.TaskClosed(0.0)]
    assert follower.poll(0.1) == []
    with open(first / "events.jsonl", "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "task_start", "task_id": "t1"}) + "\n")
        stream.write("not json\n[1]\n")
    assert follower.poll(0.2) == [g1.TaskOpened(0.2, "t1")]
    with open(first / "events.jsonl", "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "task_end", "task_id": "other"}) + "\n")
    assert follower.poll(0.3) == []
    _session(
        robot,
        "000002-20261114T101000Z-bbbb",
        {"type": "session_start", "x_carried_task": "t1"},
    )
    assert follower.poll(1.5) == []  # the rotated session carries the open task
    second = robot / "000002-20261114T101000Z-bbbb" / "events.jsonl"
    with open(second, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "task_end", "task_id": "t1"}) + "\n")
    assert follower.poll(1.6) == [g1.TaskClosed(1.6)]
    follower.close()


def test_an_unreadable_session_log_counts_as_outside_and_warns_once__notes_8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    robot = tmp_path / "data" / "g1_01"
    follower = TaskFollower(robot)
    with caplog.at_level(logging.WARNING):
        assert follower.poll(0.0) == [g1.TasksUnreadable(0.0)]
        assert follower.poll(2.0) == []
    assert len([r for r in caplog.records if "cannot read" in r.message]) == 1
    _session(robot, "000001-20261114T100000Z-aaaa", {"type": "task_start", "task_id": "t9"})
    assert follower.poll(3.0) == [g1.TaskOpened(3.0, "t9")]
    monkeypatch.setattr(g1_dds.os, "access", lambda path, mode: False)
    assert follower.poll(4.5) == [g1.TasksUnreadable(4.5)]
    follower.close()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX, not root")
def test_a_session_folder_of_another_user_is_unreadable__notes_8(tmp_path: Path) -> None:
    robot = tmp_path / "data" / "g1_01"
    _session(robot, "000001-20261114T100000Z-aaaa", {"type": "task_start", "task_id": "t1"})
    robot.chmod(0o000)
    try:
        assert TaskFollower(robot).poll(0.0) == [g1.TasksUnreadable(0.0)]
    finally:
        robot.chmod(0o700)


# -- the watch loop -----------------------------------------------------------------------------


def test_recorder_sender_sends_events_and_notes_through_the_client() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    recorder = SimpleNamespace(event=lambda kind, **fields: calls.append((kind, fields)))
    send = g1_dds.recorder_sender(recorder)
    send([*engine().stop()[:1], g1.Note((("x_g1_tasks", "unreadable"),))])
    assert calls[0][0] == "transition"
    assert calls[0][1]["state"] == "unknown"
    assert calls[1] == ("heartbeat", {"x_g1_tasks": "unreadable"})


def test_run_watch_survives_a_failing_pass_and_ends_unknown__claude_md_process_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = threading.Event()
    passes = {"n": 0}

    def poll(now: float) -> list[g1.Observation]:
        passes["n"] += 1
        if passes["n"] == 2:
            raise RuntimeError("boom")
        if passes["n"] >= 4:
            stop.set()
        return []

    sent: list[g1.Message] = []
    times = iter(k * 0.3 for k in range(100))
    with caplog.at_level(logging.WARNING):
        g1_dds.run_watch(
            SimpleNamespace(poll=poll),
            SimpleNamespace(poll=lambda now: []),
            engine(),
            sent.extend,
            stop,
            clock=lambda: next(times),
        )
    assert passes["n"] == 4
    assert any("failed; continuing" in r.message for r in caplog.records)
    states = [m for m in sent if isinstance(m, g1.StateAssertion)]
    assert [m.state for m in states[-2:]] == ["unknown", "unknown"]
    assert {m.scope for m in states[-2:]} == {"legs", "upper_body"}
