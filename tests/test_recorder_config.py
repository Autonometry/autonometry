"""Tests for the recorder config (README "Recorder config")."""

import hashlib
import json
from pathlib import Path

import pytest

from autonometry.recorder.config import (
    MAX_CONFIG_BYTES,
    ConfigError,
    json_file_hash,
    load_config,
    parse_config,
    prepare,
)
from recorder_helpers import make_config, write_inputs

TSA_ROOT = Path(__file__).parent / "fixtures" / "tsa" / "freetsa_root.pem"

MINIMAL = """
robot_id = "tb4_01"
data_dir = "data"
socket_path = "/run/autonometry/recorder.sock"
simulated = true
clock_source = "ntp"
key_path = "etc/robot.key"
task_registry = "etc/tasks.json"
plan = "etc/plan.json"
"""


def test_minimal_config_has_spec_defaults_and_relative_paths(tmp_path: Path) -> None:
    config = parse_config(MINIMAL, tmp_path)
    assert config.scopes == ("whole_robot",)
    assert config.hint_policy == "hint_as_shared"
    assert config.unobserved == ()
    assert config.fsync_interval_s == 1.0
    assert config.max_events_per_s == 1000
    assert config.max_log_bytes == 256 << 20
    assert config.data_dir == tmp_path / "data"
    assert config.key_path == tmp_path / "etc" / "robot.key"
    assert str(config.socket_path).replace("\\", "/").endswith("/run/autonometry/recorder.sock")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("heartbeat_interval_s = 2.0\n", "heartbeat_interval_s: Extra inputs are not permitted"),
        ('robot_id = "has space"\n', "robot_id"),
        ('scopes = ["a", "a"]\n', "same value twice"),
        ("scopes = []\n", "scopes"),
        ("scope_weights = { b = 1.0 }\n", "undeclared scope(s): b"),
        ("scope_weights = { whole_robot = 0.0 }\n", "scope_weights"),
        ("scope_weights = { whole_robot = inf }\n", "finite"),
        ('tsa_url = "https://tsa.example"\n', "given together"),
        ('tsa_url = "ftp://tsa.example"\ntsa_roots = "r.pem"\n', "http:// or https://"),
        ('unobserved = ["tasks", "tasks"]\n', "same value twice"),
        ('unobserved = ["video"]\n', "unobserved"),
        ('clock_source = "none"\n', "clock_source"),
        ("fsync_interval_s = 0\n", "fsync_interval_s"),
        ("max_log_bytes = 1000\n", "max_log_bytes"),
        ('socket_group = "bad group"\n', "socket_group"),
    ],
)
def test_bad_config_is_one_line_error(tmp_path: Path, extra: str, message: str) -> None:
    text = "\n".join(line for line in MINIMAL.splitlines() if line.split(" =")[0] not in extra)
    with pytest.raises(ConfigError) as caught:
        parse_config(text + "\n" + extra, tmp_path)
    assert message in str(caught.value)
    assert "\n" not in str(caught.value)


def test_missing_required_field(tmp_path: Path) -> None:
    text = MINIMAL.replace("simulated = true\n", "")
    with pytest.raises(ConfigError, match="simulated: Field required"):
        parse_config(text, tmp_path)


def test_load_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.toml")
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text("robot_id = ", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(bad_toml)
    latin1 = tmp_path / "latin1.toml"
    latin1.write_bytes(b'robot_id = "\xe9"')
    with pytest.raises(ConfigError, match="not valid UTF-8"):
        load_config(latin1)
    huge = tmp_path / "huge.toml"
    huge.write_bytes(b"#" * (MAX_CONFIG_BYTES + 1))
    with pytest.raises(ConfigError, match="larger than"):
        load_config(huge)
    good = tmp_path / "recorder.toml"
    good.write_text(MINIMAL, encoding="utf-8")
    assert load_config(good).data_dir == tmp_path / "data"


def test_prepare_loads_key_registries_and_hashes(tmp_path: Path) -> None:
    prepared = prepare(make_config(tmp_path, scope_weights={"base": 2.0}, unobserved=["tasks"]))
    assert prepared.key_name == "autonometry/r1"
    assert prepared.spec_version == "0.3"
    declaration = prepared.declaration()
    assert declaration["scope_weights"] == {"base": 2.0}
    assert declaration["unobserved"] == ["tasks"]
    assert declaration["heartbeat_interval_s"] == 1.0
    assert declaration["device_registry_hash"] == prepared.device_registry_hash
    assert prepared.tsa_roots == ()


def test_prepare_without_optional_inputs(tmp_path: Path) -> None:
    prepared = prepare(make_config(tmp_path, device_registry=None))
    declaration = prepared.declaration()
    assert "device_registry_hash" not in declaration
    assert "scope_weights" not in declaration
    assert "unobserved" not in declaration


def test_prepare_loads_tsa_roots(tmp_path: Path) -> None:
    config = make_config(tmp_path, tsa_url="https://freetsa.org/tsr", tsa_roots=str(TSA_ROOT))
    assert len(prepare(config).tsa_roots) >= 1


def test_task_registry_hash_is_sha256_of_rfc8785_form(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    path.write_text('﻿{ "b": 1, "a": [1.0, "x"] }', encoding="utf-8")
    assert json_file_hash(path) == hashlib.sha256(b'{"a":[1,"x"],"b":1}').hexdigest()


def test_prepare_refuses_unusable_inputs(tmp_path: Path) -> None:
    inputs = write_inputs(tmp_path / "other")
    cases = []
    duplicate = tmp_path / "dup.json"
    duplicate.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    cases.append(({"task_registry": str(duplicate)}, "duplicate_key"))
    latin1 = tmp_path / "latin1.json"
    latin1.write_bytes(b'{"a": "\xe9"}')
    cases.append(({"plan": str(latin1)}, "not valid UTF-8"))
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (1 << 20) + b"{}")
    cases.append(({"plan": str(big)}, "larger than"))
    cases.append(({"plan": str(tmp_path / "missing.json")}, "cannot read"))
    registry = tmp_path / "devices.json"
    registry.write_text(json.dumps({"devices": []}), encoding="utf-8")
    cases.append(({"device_registry": str(registry)}, "is not valid"))
    cases.append(({"device_registry": str(tmp_path / "nope.json")}, "cannot read"))
    cases.append(({"key_path": str(tmp_path / "nokey")}, "cannot read key"))
    no_vkey = tmp_path / "copy.key"
    no_vkey.write_bytes(inputs["key_path"].read_bytes())
    no_vkey.chmod(0o600)
    cases.append(({"key_path": str(no_vkey)}, "cannot read vkey file"))
    not_pem = tmp_path / "roots.pem"
    not_pem.write_text("not a certificate", encoding="utf-8")
    cases.append(({"tsa_url": "https://t.example", "tsa_roots": str(not_pem)}, "roots.pem"))
    cases.append(
        ({"tsa_url": "https://t.example", "tsa_roots": str(tmp_path / "x.pem")}, "cannot read")
    )
    for index, (overrides, message) in enumerate(cases):
        with pytest.raises(ConfigError, match=message):
            prepare(make_config(tmp_path / f"case{index}", **overrides))
