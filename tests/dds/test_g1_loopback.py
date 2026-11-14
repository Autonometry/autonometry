"""The Unitree G1 watcher over real Cyclone DDS 0.10.5, on loopback (marker ``dds``; CI job
``dds``; research/unitree-g1/dds_compat shows the set-up). Five programs in separate processes
play the robot's built-in controller, its lowstate with the remote's bytes, xr_teleoperate, a
policy and the arm action service. The observer must name each writer by host and script and
give the expected states; a second participant confirms through DCPSPublication that the
observer created no writer; every program must still be running when its timeline ends (the
observer harmed none). Then ``autonometry g1 discover``, and ``autonometry g1 watch`` with a real
recorder, end to end.

The timeline, seconds after START: the left stick 3 to 5 (a person's goal, ``shared``);
xr_teleoperate's arms 6 to 8 (``human_remote`` on upper_body until 10); L2 + B and damping 9 to 11
(``safety_stop``); the policy's SetVelocity 13 to 15 (``ai``, its device); preset 18 run by the
arm action service 16 to 18 (``scripted`` on upper_body)."""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from autonometry.connectors import g1
from autonometry.connectors.tb4 import devices_from_registry
from autonometry.spec import load_rules

pytestmark = pytest.mark.dds

if os.environ.get("AUTONOMETRY_REQUIRE_DDS") != "1":
    pytest.importorskip("cyclonedds")

from autonometry.connectors import g1_dds  # noqa: E402 -- after the skip

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FAKES = (
    "fake_builtin_controller.py",
    "fake_robot_lowstate.py",
    "fake_xr_teleoperate.py",
    "fake_policy.py",
    "fake_arm_action_service.py",
)
#: Loopback only, no multicast (research/unitree-g1/dds_compat/README.md); room for many
#: participants on one computer.
LOOPBACK = (
    '<CycloneDDS><Domain Id="any"><General><Interfaces><NetworkInterface address="127.0.0.1"/>'
    "</Interfaces><AllowMulticast>false</AllowMulticast></General><Discovery><Peers>"
    '<Peer address="127.0.0.1"/></Peers><ParticipantIndex>auto</ParticipantIndex>'
    "<MaxAutoParticipantIndex>60</MaxAutoParticipantIndex></Discovery></Domain></CycloneDDS>"
)
DURATION = 21.0
LEAD_S = 5.0
HOST = socket.gethostname()


def _devices() -> dict[str, Any]:
    def device(device_id: str, kind: str, participant: str, **extra: Any) -> dict[str, Any]:
        match = {"host": HOST, "participant": participant, **extra.pop("match", {})}
        return {"device_id": device_id, "kind": kind, "match": match, **extra}

    return {
        "devices": [
            device(
                "builtin",
                "ai",
                "fake_builtin_controller.py",
                match={"topic": "rt/sportmodestate"},
            ),
            device(
                "remote",
                "human",
                "fake_robot_lowstate.py",
                match={"topic": "wireless_remote"},
                location="local",
                actor_id="op_01",
            ),
            {"device_id": "presets", "kind": "script", "match": {"topic": "preset"}},
            device("arm_service", "script", "fake_arm_action_service.py"),
            device("xr", "human", "fake_xr_teleoperate.py", location="remote", actor_id="op_02"),
            device("policy", "ai", "fake_policy.py"),
        ]
    }


def _env() -> dict[str, str]:
    return {**os.environ, "CYCLONEDDS_URI": LOOPBACK, "PYTHONUNBUFFERED": "1"}


def _start_fakes(domain: int, start: float) -> list[subprocess.Popen[bytes]]:
    return [
        subprocess.Popen(  # noqa: S603 -- fixed arguments, our own scripts
            [sys.executable, str(HERE / name), str(domain), repr(start)], env=_env(), cwd=ROOT
        )
        for name in FAKES
    ]


def _autonometry(*args: str, **kwargs: Any) -> subprocess.Popen[bytes]:
    code = "from autonometry.cli import main; main()"
    return subprocess.Popen(  # noqa: S603 -- fixed arguments
        [sys.executable, "-c", code, *args], env=_env(), cwd=ROOT, **kwargs
    )


def _finish(processes: Sequence[subprocess.Popen[bytes]]) -> list[int | None]:
    """Wait for the fakes to end their timeline; their exit codes (0: unharmed)."""
    codes = []
    for process in processes:
        try:
            codes.append(process.wait(timeout=30))
        except subprocess.TimeoutExpired:
            process.kill()
            codes.append(None)
    return codes


def _subsequence(seen: Sequence[Any], wanted: Sequence[Any]) -> bool:
    items = iter(seen)
    return all(any(item == want for item in items) for want in wanted)


def _changes(values: Sequence[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for value in values:
        if not out or out[-1] != value:
            out.append(value)
    return out


# -- the observer in this process ---------------------------------------------------------------


@pytest.fixture(scope="module")
def observed() -> dict[str, Any]:
    domain = 17
    os.environ["CYCLONEDDS_URI"] = LOOPBACK
    start = time.time() + LEAD_S
    fakes = _start_fakes(domain, start)
    checker = subprocess.Popen(  # noqa: S603 -- fixed arguments
        [sys.executable, str(HERE / "check_no_writer.py"), str(domain), str(LEAD_S + DURATION)],
        env=_env(),
        cwd=ROOT,
        stdout=subprocess.PIPE,
    )
    discover = _autonometry(
        "g1", "discover", "--json", "--domain", str(domain), "--seconds", str(LEAD_S + 4),
        stdout=subprocess.PIPE,
    )  # fmt: skip
    rules = load_rules("0.3")
    settings = g1.Settings.from_params(rules.params, devices_from_registry(_devices()))
    engine = g1.G1Engine(settings)
    backend = g1_dds.open_backend(domain, None)
    observer = g1_dds.Observer(backend)
    seen: list[g1.WriterSeen] = []
    states: list[tuple[float, g1.StateAssertion]] = []
    engine.observe(g1.TaskOpened(time.monotonic(), "t1"))
    next_tick = 0.0
    try:
        while time.time() < start + DURATION:
            now = time.monotonic()
            for observation in observer.poll(now):
                if isinstance(observation, g1.WriterSeen):
                    seen.append(observation)
                for message in engine.observe(observation):
                    if isinstance(message, g1.StateAssertion):
                        states.append((time.time() - start, message))
            if now >= next_tick:
                next_tick = now + g1_dds.TICK_S
                for message in engine.tick(now):
                    if isinstance(message, g1.StateAssertion):
                        states.append((time.time() - start, message))
            time.sleep(g1_dds.PASS_S)
        codes = _finish(fakes)
        publications = [
            json.loads(line) for line in checker.communicate(timeout=30)[0].splitlines()
        ]
        report = json.loads(discover.communicate(timeout=30)[0])
    finally:
        for process in [*fakes, checker, discover]:
            if process.poll() is None:
                process.kill()
    return {
        "seen": seen,
        "states": states,
        "codes": codes,
        "publications": publications,
        "own": backend.participant_key,
        "discover": report,
    }


def test_the_observer_names_each_writer_by_host_and_script__notes_4(
    observed: dict[str, Any],
) -> None:
    named = {(w.source.participant, w.source.topic) for w in observed["seen"]}
    assert {
        ("fake_builtin_controller.py", "rt/sportmodestate"),
        ("fake_robot_lowstate.py", "rt/lowstate"),
        ("fake_xr_teleoperate.py", "rt/arm_sdk"),
        ("fake_policy.py", "rt/api/sport/request"),
        ("fake_policy.py", "rt/api/arm/request"),
        ("fake_arm_action_service.py", "rt/arm_sdk"),
        ("fake_arm_action_service.py", "rt/arm/action/state"),
    } <= named
    assert {w.source.host for w in observed["seen"]} == {HOST}
    assert all(w.decoded for w in observed["seen"])


def test_the_observer_gives_the_expected_states__notes_6(observed: dict[str, Any]) -> None:
    legs = _changes([(m.state, m.device_id) for _, m in observed["states"] if m.scope == "legs"])
    upper = _changes(
        [(m.state, m.device_id) for _, m in observed["states"] if m.scope == "upper_body"]
    )
    assert _subsequence(
        legs,
        [
            ("ai", "builtin"),
            ("shared", "remote"),
            ("ai", "builtin"),
            ("safety_stop", "remote"),
            ("ai", "builtin"),
            ("ai", "policy"),
            ("ai", "builtin"),
        ],
    ), legs
    assert _subsequence(
        upper,
        [
            ("shared", "remote"),
            ("human_remote", "xr"),
            ("safety_stop", "remote"),
            ("ai", "policy"),
            ("scripted", "presets"),
            ("ai", "builtin"),
        ],
    ), upper
    preset = {m.actor_id for _, m in observed["states"] if m.device_id == "presets"}
    assert preset == {"script:arm_action_18"}


def test_a_second_participant_sees_no_writer_of_the_observer__notes_3(
    observed: dict[str, Any],
) -> None:
    publications = observed["publications"]
    topics = {p["topic"] for p in publications}
    assert {"rt/sportmodestate", "rt/lowstate", "rt/arm_sdk", "rt/api/sport/request"} <= topics
    assert observed["own"] not in {p["participant"] for p in publications}


def test_the_observer_harms_no_publisher__notes_3_s11(observed: dict[str, Any]) -> None:
    assert observed["codes"] == [0] * len(FAKES)


def test_discover_lists_the_programs_with_their_match_strings__notes_4(
    observed: dict[str, Any],
) -> None:
    participants = {p["participant"]: p for p in observed["discover"]["participants"]}
    assert set(FAKES) <= set(participants)
    robot = participants["fake_robot_lowstate.py"]
    (lowstate,) = robot["writers"]
    assert lowstate["type"] == "unitree_hg::msg::dds_::LowState_"
    assert lowstate["remote_match"] == {
        "host": HOST,
        "participant": "fake_robot_lowstate.py",
        "topic": "wireless_remote",
    }
    assert participants["fake_policy.py"]["program"].lower().startswith("python")  # macOS: Python


# -- autonometry g1 watch with a recorder -------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="the recorder needs POSIX sockets")
def test_watch_feeds_the_recorder_end_to_end__notes_6_8(tmp_path: Path) -> None:
    domain = 23
    # A Unix socket path has at most about 104 bytes; pytest's folders on macOS are longer.
    short = Path(tempfile.mkdtemp(prefix="g1", dir="/tmp"))
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps(_devices()), encoding="utf-8")
    config = tmp_path / "recorder.toml"
    config.write_text(
        "\n".join(
            [
                'robot_id = "g1_test"',
                f'data_dir = "{tmp_path / "data"}"',
                f'socket_path = "{short / "recorder.sock"}"',
                'scopes = ["legs", "upper_body"]',
                "simulated = true",
                'clock_source = "ntp"',
                f'key_path = "{tmp_path / "g1.key"}"',
                f'device_registry = "{devices}"',
                f'task_registry = "{ROOT / "connectors" / "g1" / "tasks.json"}"',
                f'plan = "{ROOT / "connectors" / "g1" / "plan.json"}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    keygen = _autonometry(
        "keys", "generate", "--name", "autonometry/g1_test", "--out", str(tmp_path / "g1.key")
    )
    assert keygen.wait(timeout=30) == 0
    recorder = _autonometry("record", "--config", str(config))
    sock = short / "recorder.sock"
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.1)
    watch = _autonometry("g1", "watch", "--config", str(config), "--domain", str(domain))
    from autonometry.connectors.autonometry_client import Recorder  # noqa: PLC0415

    start = time.time() + LEAD_S
    fakes = _start_fakes(domain, start)
    try:
        with Recorder(str(sock), "g1_program") as program:
            while time.time() < start + 1.0:
                time.sleep(0.05)
            program.event("task_start", task_id="t1", task_definition_id="g1_walk_to_station")
            while time.time() < start + DURATION - 1.0:
                time.sleep(0.05)
            program.event("task_end", task_id="t1", outcome="success")
        assert _finish(fakes) == [0] * len(FAKES)
    finally:
        for process in (watch, recorder):
            process.send_signal(signal.SIGTERM)
        assert watch.wait(timeout=30) == 0
        assert recorder.wait(timeout=30) == 0
        for process in fakes:
            if process.poll() is None:
                process.kill()
        shutil.rmtree(short, ignore_errors=True)
    (folder,) = [p for p in (tmp_path / "data" / "g1_test").iterdir() if p.is_dir()]
    events = [json.loads(line) for line in (folder / "events.jsonl").read_bytes().splitlines()]
    legs = _changes(
        [
            (e["state"], e.get("device_id"))
            for e in events
            if e["type"] == "transition" and e["scope"] == "legs"
        ]
    )
    assert _subsequence(
        legs,
        [("shared", "remote"), ("safety_stop", "remote"), ("ai", "policy"), ("ai", "builtin")],
    ), legs
    assert any(e.get("x_g1_writers") for e in events if e["type"] == "heartbeat")
    started = next(e for e in events if e["type"] == "task_start")
    after = [e for e in events if e["type"] == "transition" and e["seq"] > started["seq"]]
    assert any(e["task_id"] == "t1" and e["state"] == "ai" for e in after)
    vkey = Path(str(tmp_path / "g1.key") + ".vkey")
    verify = _autonometry(
        "verify", str(folder / "events.jsonl"), "--vkey", str(vkey),
        "--anchor", str(folder / "events.checkpoints.jsonl"), "--require", "L2",
    )  # fmt: skip
    assert verify.wait(timeout=60) == 0
