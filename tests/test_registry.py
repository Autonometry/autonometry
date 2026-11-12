"""Tests for autonometry.registry and ``autonometry registry check`` (definitions.md 6.10)."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from autonometry import cli, spec
from autonometry.registry import (
    MAX_REGISTRY_BYTES,
    RegistryReadError,
    check_registry,
    check_registry_bytes,
    registry_hash,
)

SPEC = "0.3"
GOLDEN = Path(__file__).parent / "golden" / "registry"
FIXTURES = sorted(p for p in GOLDEN.glob("*.json") if not p.name.endswith(".expected.json"))
runner = CliRunner()


def _device(**fields: Any) -> dict[str, Any]:
    return {"device_id": "nav2", "kind": "ai", "match": {"node": "/bt_navigator"}} | fields


def _check(registry: Any) -> list[str]:
    return list(check_registry_bytes(json.dumps(registry).encode(), "r.json", SPEC).problems)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_golden_registry__defs_6_10(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(path.parent)
    result = check_registry(path.name, SPEC)
    expected = path.with_name(f"{path.stem}.expected.json").read_text(encoding="utf-8")
    assert json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n" == expected


def test_registry_hash__defs_6_10() -> None:
    """The hash is SHA-256 of the RFC 8785 form: key order and whitespace do not change it."""
    data = json.loads((GOLDEN / "turtlebot4.json").read_text(encoding="utf-8"))
    compact = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    assert registry_hash(data) == hashlib.sha256(compact).hexdigest()
    reordered = json.dumps(dict(reversed(list(data.items()))), indent=7).encode()
    assert check_registry_bytes(reordered, "r.json", SPEC).sha256 == registry_hash(data)
    assert check_registry(GOLDEN / "turtlebot4.json", SPEC).sha256 == registry_hash(data)


def test_registry_hash_is_lower_case_hex__defs_6_10() -> None:
    digest = check_registry(GOLDEN / "unitree_g1.json", SPEC).sha256
    assert digest is not None
    assert len(digest) == 64
    assert digest == digest.lower()


def test_duplicate_device_id_is_an_error__defs_6_10() -> None:
    result = check_registry_bytes(
        json.dumps({"devices": [_device(), _device(match={"node": "/other"})]}).encode(),
        "r.json",
        SPEC,
    )
    assert not result.valid
    assert result.sha256 is None
    assert result.devices == 2
    assert result.problems == (
        "duplicate_device_id: /devices/1 repeats the device_id of /devices/0",
    )


def test_shadowed_device_is_an_error__defs_6_10() -> None:
    """First match wins, so a device whose match holds every field of an earlier device's match,
    with the same values, can never be matched; identical matches included."""
    first = _device(device_id="a", match={"node": "/n"})
    assert _check({"devices": [first, _device(device_id="b", match={"node": "/n"})]}) == [
        "shadowed_device: /devices/1 can never be matched: the match of /devices/0 comes first "
        "and covers it"
    ]
    wider = _device(device_id="b", match={"node": "/n", "host": "h"})
    assert len(_check({"devices": [first, wider]})) == 1
    assert _check({"devices": [wider, first]}) == []
    other = _device(device_id="b", match={"node": "/m", "host": "h"})
    assert _check({"devices": [first, other]}) == []
    result = check_registry_bytes(json.dumps({"devices": [first, wider]}).encode(), "r.json", SPEC)
    assert (result.valid, result.sha256) == (False, None)


def test_human_device_needs_a_location__defs_6_10() -> None:
    assert _check({"devices": [_device(kind="human")]}) == [
        "schema_violation: /devices/0: missing field location"
    ]
    assert _check({"devices": [_device(kind="human", location="remote")]}) == []


@pytest.mark.parametrize("kind", ["human", "ai", "script", "support"])
def test_every_device_kind_is_accepted__defs_6_10(kind: str) -> None:
    assert _check({"devices": [_device(kind=kind, location="local")]}) == []


def test_unknown_kind_and_location_are_rejected__defs_6_10() -> None:
    assert _check({"devices": [_device(kind="robot")]}) == [
        'schema_violation: /devices/0/kind: must be one of "human", "ai", "script", "support"'
    ]
    assert _check({"devices": [_device(kind="human", location="nearby")]}) == [
        'schema_violation: /devices/0/location: must be one of "local", "remote"'
    ]


def test_match_needs_a_known_field__defs_6_10() -> None:
    assert _check({"devices": [_device(match={})]}) == [
        "schema_violation: /devices/0/match: must be an object with a number of fields >= 1"
    ]
    assert _check({"devices": [_device(match={"pid": "1"})]}) == [
        "schema_violation: /devices/0/match: has a field the schema does not define "
        "(extensions start with x_)"
    ]
    fields = {"host": "h", "node": "/n", "topic": "/t", "participant": "p"}
    assert _check({"devices": [_device(match=fields)]}) == []


def test_match_values_are_1_to_256_printable_ascii__defs_6_10() -> None:
    assert _check({"devices": [_device(match={"host": "~" * 256})]}) == []
    assert _check({"devices": [_device(match={"host": " "})]}) == []
    for bad in ("", "x" * 257, "tab\there", "café"):
        (problem,) = _check({"devices": [_device(match={"host": bad})]})
        assert problem.startswith("schema_violation: /devices/0/match/host: does not match")


def test_identifiers_follow_the_identifier_rule__defs_6_10() -> None:
    assert _check({"devices": [_device(actor_id="op_17")]}) == []
    for field in ("device_id", "actor_id"):
        (problem,) = _check({"devices": [_device(**{field: "-bad id"})]})
        assert problem.startswith(f"schema_violation: /devices/0/{field}: does not match")


def test_extensions_on_registry_and_device_but_not_in_match__defs_6_10() -> None:
    assert _check({"devices": [_device(x_note="ok")], "x_site": "lab"}) == []
    assert _check({"devices": [_device()], "site": "lab"}) == [
        "schema_violation: registry: has a field the schema does not define "
        "(extensions start with x_)"
    ]
    assert _check({"devices": [_device(match={"x_pid": "1"})]}) != []


def test_registry_needs_devices__defs_6_10() -> None:
    assert _check({"devices": []}) == ["schema_violation: /devices: must be a list of length >= 1"]
    assert _check({}) == ["schema_violation: registry: missing field devices"]
    assert _check([]) == ["schema_violation: registry: must be of type object"]


def test_problems_never_quote_values_from_the_file__defs_6_10() -> None:
    pseudonym = "Alice-Example"
    registries = [
        {"devices": [_device(device_id=pseudonym), _device(device_id=pseudonym)]},
        {"devices": [_device(match={"host": pseudonym + "\t"})]},
        {"devices": [_device(**{"Alice": pseudonym})]},
    ]
    for registry in registries:
        problems = _check(registry)
        assert problems
        assert all(pseudonym not in p and "Alice" not in p for p in problems)


def test_bad_bytes_give_one_problem__defs_6_10() -> None:
    def problems(data: bytes) -> tuple[str, ...]:
        return check_registry_bytes(data, "r.json", SPEC).problems

    assert problems(b'{"devices": "\xff"}') == ("byte 14 is not valid UTF-8",)
    assert problems(b"{") == ("invalid_json: Expecting property name enclosed in double quotes "
                              "at column 2",)  # fmt: skip
    assert problems(b'{"a": 1, "a": 2}') == ("duplicate_key: an object has the same key twice",)
    assert problems(b'{"a": NaN}') == ("non_finite_number: NaN is not a JSON number",)


def test_byte_order_mark_is_accepted__defs_6_10() -> None:
    data = (GOLDEN / "turtlebot4.json").read_bytes()
    plain = check_registry_bytes(data, "r.json", SPEC)
    with_bom = check_registry_bytes("﻿".encode() + data, "r.json", SPEC)
    assert with_bom.valid
    assert with_bom.sha256 == plain.sha256


def test_oversize_registry_is_refused_without_reading_it_all(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b" " * (MAX_REGISTRY_BYTES + 10))
    assert check_registry(path, SPEC).problems == (
        f"file is larger than {MAX_REGISTRY_BYTES} bytes",
    )


def test_unreadable_registry_raises_one_line_error(tmp_path: Path) -> None:
    with pytest.raises(RegistryReadError, match=r"^cannot read .*missing\.json"):
        check_registry(tmp_path / "missing.json", SPEC)


@given(st.binary(max_size=300))
def test_any_bytes_give_a_result_never_a_crash__defs_6_10(data: bytes) -> None:
    result = check_registry_bytes(data, "r.json", SPEC)
    assert result.valid == (result.sha256 is not None)


_observed = st.text(st.characters(min_codepoint=0x20, max_codepoint=0x7E), min_size=1, max_size=20)
_ident = st.from_regex(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,10}\Z")


@st.composite
def _registries(draw: st.DrawFn) -> dict[str, Any]:
    devices = []
    for device_id in draw(st.lists(_ident, min_size=1, max_size=4, unique=True)):
        kind = draw(st.sampled_from(["human", "ai", "script", "support"]))
        # A node unique to each device, so no device shadows another.
        others = draw(st.dictionaries(st.sampled_from(["host", "topic", "participant"]), _observed))
        match = others | {"node": f"/{device_id}"}
        device: dict[str, Any] = {"device_id": device_id, "kind": kind, "match": match}
        if kind == "human":
            device["location"] = draw(st.sampled_from(["local", "remote"]))
        devices.append(device)
    return {"devices": devices}


_PYTHON_VALIDATOR = Draft202012Validator(spec.load_registry_schema(SPEC))


@given(_registries())
def test_valid_registries_pass_both_validators__defs_6_10(registry: dict[str, Any]) -> None:
    """jsonschema-rs and the reference Python validator agree on valid registries."""
    assert _PYTHON_VALIDATOR.is_valid(registry)
    result = check_registry_bytes(json.dumps(registry).encode(), "r.json", SPEC)
    assert result.valid
    assert result.sha256 == registry_hash(registry)


def test_registry_schema_examples_are_valid__defs_6_10() -> None:
    schema = spec.load_registry_schema(SPEC)
    assert schema["$id"] == "https://autonometry.github.io/spec/v0.3/device_registry.schema.json"
    for example in schema["examples"]:
        assert _check(example) == []
        assert _PYTHON_VALIDATOR.is_valid(example)


# --- command line ------------------------------------------------------------------------------


def test_cli_registry_check_valid_prints_hash__defs_6_10() -> None:
    result = runner.invoke(cli.app, ["registry", "check", str(GOLDEN / "turtlebot4.json")])
    assert result.exit_code == 0
    digest = check_registry(GOLDEN / "turtlebot4.json", SPEC).sha256
    assert result.output.splitlines() == ["spec 0.3: 5 device(s), valid", f"sha256 {digest}"]


def test_cli_registry_check_invalid_exits_1__defs_6_10() -> None:
    path = GOLDEN / "duplicate_device_id.json"
    result = runner.invoke(cli.app, ["registry", "check", str(path)])
    assert result.exit_code == 1
    assert result.output.splitlines() == [
        "spec 0.3: NOT valid",
        "  duplicate_device_id: /devices/2 repeats the device_id of /devices/0",
        "  duplicate_device_id: /devices/3 repeats the device_id of /devices/1",
    ]


def test_cli_registry_check_json_matches_golden__defs_6_10(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(GOLDEN)
    for name, code in (("unitree_g1", 0), ("empty_match", 1)):
        result = runner.invoke(cli.app, ["registry", "check", "--json", f"{name}.json"])
        assert result.exit_code == code
        assert result.output == (GOLDEN / f"{name}.expected.json").read_text(encoding="utf-8")


def test_cli_registry_check_bad_usage_exits_2(tmp_path: Path) -> None:
    missing = runner.invoke(cli.app, ["registry", "check", str(tmp_path / "nope.json")])
    assert missing.exit_code == 2
    bad_spec = runner.invoke(
        cli.app, ["registry", "check", "--spec", "9.9", str(GOLDEN / "turtlebot4.json")]
    )
    assert bad_spec.exit_code == 2


def test_cli_registry_check_unreadable_file_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "r.json"
    path.write_text("{}", encoding="utf-8")

    def refuse(*_: Any) -> None:
        raise RegistryReadError(f"cannot read {path}: Permission denied")

    monkeypatch.setattr(cli, "check_registry", refuse)
    result = runner.invoke(cli.app, ["registry", "check", str(path)])
    assert result.exit_code == 1
    assert "error: cannot read" in result.output
