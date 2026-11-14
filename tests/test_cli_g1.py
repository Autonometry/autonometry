"""``autonometry g1``: the start-up checks of ``watch`` and ``discover`` (version guard, registry
ambiguity, scopes, platform), and ``discover``'s output over a fake backend. The commands
themselves over Cyclone DDS run in ``tests/dds`` (marker ``dds``)."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autonometry import cli, cli_g1
from autonometry.connectors.g1_dds import ParticipantRecord, PublicationRecord
from g1_helpers import CONFIG

runner = CliRunner()


def _config(tmp_path: Path, devices: Path = CONFIG / "devices.json", **extra: str) -> Path:
    lines = {
        "robot_id": '"g1_01"',
        "data_dir": f'"{(tmp_path / "data").as_posix()}"',
        "socket_path": f'"{(tmp_path / "recorder.sock").as_posix()}"',
        "scopes": '["legs", "upper_body"]',
        "simulated": "false",
        "clock_source": '"ntp"',
        "key_path": f'"{(tmp_path / "g1.key").as_posix()}"',
        "device_registry": f'"{devices.as_posix()}"',
        "task_registry": f'"{(CONFIG / "tasks.json").as_posix()}"',
        "plan": f'"{(CONFIG / "plan.json").as_posix()}"',
        **extra,
    }
    path = tmp_path / "recorder.toml"
    path.write_text("".join(f"{k} = {v}\n" for k, v in lines.items()), encoding="utf-8")
    return path


@pytest.fixture
def cyclone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend Cyclone DDS 0.10.5 is installed."""
    monkeypatch.setattr(cli_g1, "cyclonedds_version", lambda: "0.10.5")


@pytest.mark.parametrize("command", [["watch", "--config", "CONFIG"], ["discover"]])
@pytest.mark.parametrize(
    ("version", "message"),
    [
        ("11.1.0", "Cyclone DDS 11.1.0 is not supported"),
        ("0.11.0", "Cyclone DDS 0.11.0 is not supported"),
        (None, "Cyclone DDS for Python is not installed"),
    ],
)
def test_g1_commands_refuse_anything_but_cyclone_dds_0_10__notes_3_s11(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    version: str | None,
    message: str,
) -> None:
    monkeypatch.setattr(cli_g1, "cyclonedds_version", lambda: version)
    args = [str(_config(tmp_path)) if a == "CONFIG" else a for a in command]
    result = runner.invoke(cli.app, ["g1", *args])
    assert result.exit_code == 2
    assert message in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_watch_refuses_an_ambiguous_registry__notes_4(tmp_path: Path, cyclone: None) -> None:
    registry = json.loads((CONFIG / "devices.json").read_text(encoding="utf-8"))
    registry["devices"].append(
        {
            "device_id": "any_python",
            "kind": "human",
            "location": "local",
            "match": {"participant": "eval_g1.py"},
        }
    )
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps(registry), encoding="utf-8")
    result = runner.invoke(cli.app, ["g1", "watch", "--config", str(_config(tmp_path, devices))])
    assert result.exit_code == 2
    assert "is ambiguous" in result.output
    assert "'g1_policy' (ai) and 'any_python' (human local)" in result.output


def test_watch_refuses_an_invalid_registry(tmp_path: Path, cyclone: None) -> None:
    devices = tmp_path / "devices.json"
    devices.write_text('{"devices": []}', encoding="utf-8")
    result = runner.invoke(cli.app, ["g1", "watch", "--config", str(_config(tmp_path, devices))])
    assert result.exit_code == 2
    assert "is not valid" in result.output
    missing = tmp_path / "missing.json"
    result = runner.invoke(cli.app, ["g1", "watch", "--config", str(_config(tmp_path, missing))])
    assert result.exit_code == 2
    assert "cannot read" in result.output


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"scopes": '["whole_robot"]'}, "reports the scopes legs and upper_body"),
        ({"scopes": '["legs", "upper_body", "head"]'}, "reports the scopes legs and upper_body"),
        ({"role": '"station"', "station_id": '"pc"'}, "the config is a station's"),
        ({"robot_id": '"bad id"'}, "robot_id"),
    ],
)
def test_watch_refuses_a_config_it_cannot_serve(
    tmp_path: Path, cyclone: None, extra: dict[str, str], message: str
) -> None:
    result = runner.invoke(cli.app, ["g1", "watch", "--config", str(_config(tmp_path, **extra))])
    assert result.exit_code == 2
    assert message in result.output


def test_watch_refuses_a_bad_interface_name(tmp_path: Path, cyclone: None) -> None:
    args = ["g1", "watch", "--config", str(_config(tmp_path)), "--interface", "eth0;rm"]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2
    assert "--interface" in result.output


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_watch_starts_on_linux_and_macos_only(
    tmp_path: Path, cyclone: None, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    pytest.importorskip("psutil")  # imported for this OS before the platform is patched
    started: list[Any] = []
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(cli_g1, "_watch", lambda *args: started.append(args))
    config = _config(tmp_path, scopes='["legs", "upper_body", "hands"]')
    result = runner.invoke(cli.app, ["g1", "watch", "--config", str(config), "--domain", "3"])
    if platform == "win32":
        assert result.exit_code == 2
        assert "Linux and macOS" in result.output
        assert started == []
    else:
        assert result.exit_code == 0, result.output
        (args,) = started
        assert args[1].hands
        assert args[2:] == (None, 3, "g1_watch")


def test_g1_commands_need_psutil(
    tmp_path: Path, cyclone: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)
    result = runner.invoke(cli.app, ["g1", "discover"])
    assert result.exit_code == 2
    assert "pip install 'autonometry[g1]'" in result.output


class _Backend:
    participant_key = "me"

    def __init__(self, found: bool) -> None:
        self.found = found

    def participants(self) -> list[ParticipantRecord]:
        props = {"__Hostname": "g1-pc1", "__ProcessName": "ai_sport", "__Pid": "5"}
        return [ParticipantRecord("p1", props)] if self.found else []

    def publications(self) -> list[PublicationRecord]:
        return (
            [PublicationRecord("w1", 1, "p1", "rt/sportmodestate", "unitree_hg::x")]
            if self.found
            else []
        )

    def open_reader(self, topic: str, type_name: str) -> bool:
        raise AssertionError("discover opens no reader")

    def samples(self) -> list[Any]:
        return []


@pytest.mark.parametrize("found", [True, False])
@pytest.mark.parametrize("as_json", [True, False])
def test_discover_prints_participants_and_match_strings__notes_4(
    cyclone: None, monkeypatch: pytest.MonkeyPatch, found: bool, as_json: bool
) -> None:
    opened: list[tuple[int, str | None]] = []

    def open_backend(domain: int, interface: str | None) -> _Backend:
        opened.append((domain, interface))
        return _Backend(found)

    monkeypatch.setattr(cli_g1, "open_backend", open_backend)
    args = ["g1", "discover", "--seconds", "0.5", "--interface", "lo", "--domain", "7"]
    result = runner.invoke(cli.app, [*args, *(["--json"] if as_json else [])])
    assert result.exit_code == 0, result.output
    assert opened == [(7, "lo")]
    if as_json:
        report = json.loads(result.output)
        assert len(report["participants"]) == int(found)
    elif found:
        assert '"participant": "ai_sport"' in result.output
    else:
        assert "no participant found" in result.output


def test_discover_refuses_a_bad_interface_name(cyclone: None) -> None:
    result = runner.invoke(cli.app, ["g1", "discover", "--interface", "a b"])
    assert result.exit_code == 2
    assert "--interface" in result.output
