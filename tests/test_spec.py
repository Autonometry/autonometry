import contextlib
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from autonometry import spec

REPO_SPEC = Path(__file__).resolve().parents[1] / "spec"


@pytest.fixture
def spec_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A writable copy of the spec folder that the spec module reads instead of the real one."""
    copy = tmp_path / "spec"
    shutil.copytree(REPO_SPEC, copy)
    monkeypatch.setattr(spec, "spec_root", lambda: copy)
    return copy / "v0.2"


def _edit_schema(folder: Path, edit: Any) -> None:
    path = folder / "event.schema.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def _edit_rules(folder: Path, old: str, new: str) -> None:
    path = folder / "rules.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_available_versions_lists_v0_2() -> None:
    assert spec.available_versions() == ["0.2"]


def test_schema_is_valid_draft_2020_12__defs_8() -> None:
    schema = spec.load_schema("0.2")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_every_schema_example_validates__defs_8() -> None:
    schema = spec.load_schema("0.2")
    validator = Draft202012Validator(schema)
    examples = schema["examples"]
    assert len(examples) > 5
    for example in examples:
        validator.validate(example)


def test_schema_rejects_human_transition_without_actor__defs_3() -> None:
    schema = spec.load_schema("0.2")
    bad = {
        "robot_id": "r01",
        "session_id": "s001",
        "task_id": None,
        "seq": 1,
        "ts": "2026-10-30T09:00:00.000Z",
        "type": "transition",
        "scope": "whole_robot",
        "state": "human_remote",
        "reason": "human_initiated",
    }
    assert not Draft202012Validator(schema).is_valid(bad)


def test_rules_load_as_frozen_model__defs_9() -> None:
    rules = spec.load_rules("0.2")
    assert rules.spec_version == "0.2"
    assert rules.params.idle_threshold_s == 30.0
    assert rules.params.unknown_share_max == 0.05
    assert rules.states["shared"].category == "human"
    with pytest.raises(ValidationError):
        rules.spec_version = "9.9"  # type: ignore[misc]


def test_rules_reject_unknown_keys__defs_9(spec_copy: Path) -> None:
    _edit_rules(spec_copy, "[gaps]", "surprise = 1\n[gaps]")
    with pytest.raises(spec.SpecError, match=r"rules\.toml .* invalid"):
        spec.load_rules("0.2")


def test_check_consistency_passes_for_v0_2__defs_2() -> None:
    spec.check_consistency("0.2")


def test_state_removed_from_schema_fails_consistency__defs_2(spec_copy: Path) -> None:
    _edit_schema(spec_copy, lambda d: d["$defs"]["state"]["enum"].remove("scripted"))
    with pytest.raises(spec.SpecConsistencyError, match="'scripted' is in rules but not"):
        spec.check_consistency("0.2")


def test_reason_added_to_schema_fails_consistency__defs_3(spec_copy: Path) -> None:
    _edit_schema(spec_copy, lambda d: d["$defs"]["reason"]["enum"].append("curiosity"))
    with pytest.raises(spec.SpecConsistencyError, match="'curiosity' is in the schema but not"):
        spec.check_consistency("0.2")


def test_unknown_state_in_rules_fails_consistency__defs_6_6(spec_copy: Path) -> None:
    _edit_rules(
        spec_copy,
        '"human_remote", "human_local", "shared"',
        '"human_remoet", "human_local", "shared"',
    )
    with pytest.raises(spec.SpecConsistencyError, match="unknown state 'human_remoet'"):
        spec.check_consistency("0.2")


def test_duplicate_flag_code_fails_consistency__defs_8(spec_copy: Path) -> None:
    _edit_rules(spec_copy, '"time_gap", "seq_gap"', '"time_gap", "time_gap"')
    with pytest.raises(
        spec.SpecConsistencyError, match="flag code 'time_gap' appears more than once"
    ):
        spec.check_consistency("0.2")


def test_wrong_spec_version_in_rules_fails_consistency(spec_copy: Path) -> None:
    _edit_rules(spec_copy, 'spec_version = "0.2"', 'spec_version = "0.3"')
    with pytest.raises(spec.SpecConsistencyError, match="differs from folder"):
        spec.check_consistency("0.2")


def test_invalid_schema_is_a_spec_error(spec_copy: Path) -> None:
    _edit_schema(spec_copy, lambda d: d.update(type=12))
    with pytest.raises(spec.SpecError, match="is invalid"):
        spec.load_schema("0.2")


def test_broken_json_and_toml_are_spec_errors(spec_copy: Path) -> None:
    (spec_copy / "event.schema.json").write_text("{not json", encoding="utf-8")
    (spec_copy / "rules.toml").write_text("not = [toml", encoding="utf-8")
    with pytest.raises(spec.SpecError, match="not valid JSON"):
        spec.load_schema("0.2")
    with pytest.raises(spec.SpecError, match="not valid TOML"):
        spec.load_rules("0.2")


def test_non_object_schema_is_a_spec_error(spec_copy: Path) -> None:
    (spec_copy / "event.schema.json").write_text("[1]", encoding="utf-8")
    with pytest.raises(spec.SpecError, match="not a JSON object"):
        spec.load_schema("0.2")


def test_missing_enum_is_a_spec_error() -> None:
    with pytest.raises(spec.SpecError, match=r"no \$defs/state/enum"):
        spec.find_inconsistencies("0.2", {}, spec.load_rules("0.2"))


def test_missing_file_is_a_spec_error(spec_copy: Path) -> None:
    (spec_copy / "rules.toml").unlink()
    with pytest.raises(spec.SpecError, match=r"cannot read rules\.toml"):
        spec.load_rules("0.2")


def test_missing_spec_root_is_a_spec_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spec.resources, "files", lambda _name: tmp_path)
    monkeypatch.setattr(spec, "__file__", str(tmp_path / "a" / "b" / "spec.py"))
    with pytest.raises(spec.SpecError, match="not found"):
        spec.spec_root()


def test_file_hashes_are_sha256_of_the_files() -> None:
    hashes = spec.spec_file_hashes("0.2")
    assert list(hashes) == ["definitions.md", "event.schema.json", "rules.toml"]
    expected = hashlib.sha256((REPO_SPEC / "v0.2" / "rules.toml").read_bytes()).hexdigest()
    assert hashes["rules.toml"] == expected


def test_version_with_leading_v_is_accepted() -> None:
    assert spec.spec_file_hashes("v0.2") == spec.spec_file_hashes("0.2")


@pytest.mark.parametrize("bad", ["", "../x", "0.2/../..", "latest", "0.2.1", "v", "0.2\n"])
def test_invalid_version_is_rejected(bad: str) -> None:
    with pytest.raises(spec.SpecError, match="invalid spec version"):
        spec.spec_file_hashes(bad)


def test_unknown_version_is_rejected() -> None:
    with pytest.raises(spec.SpecError, match=r"unknown spec version '9\.9'; available: 0\.2"):
        spec.load_schema("9.9")


@given(st.text())
def test_any_version_text_gives_hashes_or_spec_error(text: str) -> None:
    with contextlib.suppress(spec.SpecError):
        spec.spec_file_hashes(text)


def test_spec_files_have_no_carriage_return__defs_8() -> None:
    """Spec hashes must match on every OS, so no file under spec/ may contain CR."""
    files = [path for path in REPO_SPEC.rglob("*") if path.is_file()]
    assert files
    for path in files:
        assert b"\r" not in path.read_bytes(), path


def test_rules_state_table_is_read_only__defs_2() -> None:
    rules = spec.load_rules("0.2")
    with pytest.raises(TypeError):
        rules.states["ai"] = rules.states["idle"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        rules.states.clear()  # type: ignore[attr-defined]
    assert spec.Rules.model_validate(rules.model_dump()) == rules


def test_rules_error_table__defs_8() -> None:
    errors = spec.load_rules("0.2").errors
    assert errors.max_line_bytes == 65536
    assert errors.codes[0] == "invalid_utf8"
    assert len(errors.codes) == 10


def test_non_positive_line_limit_is_a_spec_error__defs_8(spec_copy: Path) -> None:
    _edit_rules(spec_copy, "max_line_bytes = 65536", "max_line_bytes = 0")
    with pytest.raises(spec.SpecError, match=r"errors\.max_line_bytes"):
        spec.load_rules("0.2")


def test_duplicate_error_code_fails_consistency__defs_8(spec_copy: Path) -> None:
    _edit_rules(spec_copy, '"invalid_utf8", "line_too_long"', '"invalid_utf8", "invalid_utf8"')
    with pytest.raises(spec.SpecConsistencyError, match="error code 'invalid_utf8' appears more"):
        spec.check_consistency("0.2")


def test_code_in_flags_and_errors_fails_consistency__defs_8(spec_copy: Path) -> None:
    _edit_rules(spec_copy, '"duplicate_seq"]', '"duplicate_seq", "seq_gap"]')
    with pytest.raises(spec.SpecConsistencyError, match="'seq_gap' is both a flag and an error"):
        spec.check_consistency("0.2")


def test_known_version_normalises() -> None:
    assert spec.known_version("v0.2") == "0.2"
    with pytest.raises(spec.SpecError, match="unknown spec version"):
        spec.known_version("9.9")
