"""Access to the specification files that ship with the package.

The spec lives once in the repository (``spec/``). The wheel carries it as ``autonometry/spec/``;
from a source checkout the repository folder is used. Spec versions are named without the leading
``v`` (``"0.2"`` is the folder ``spec/v0.2``); a leading ``v`` is accepted on input.
"""

import hashlib
import json
import logging
import re
import tomllib
from collections.abc import Mapping
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

import jsonschema_rs
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    PositiveInt,
    SerializerFunctionWrapHandler,
    ValidationError,
    WrapSerializer,
)

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"v?([0-9]{1,4})\.([0-9]{1,4})")
_FOLDER_RE = re.compile(r"v([0-9]{1,4})\.([0-9]{1,4})")
_SCHEMA_FILE = "event.schema.json"
_RULES_FILE = "rules.toml"


class SpecError(Exception):
    """A specification file is missing, unreadable or invalid. The message is one line."""


class SpecConsistencyError(SpecError):
    """The rule tables and the event schema of one spec version disagree."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StateRule(_Frozen):
    """How one control state counts (definitions.md sections 2 and 4)."""

    category: Literal["ai", "human", "neutral", "inactive", "unmeasured"]
    active: bool


class WholeRobotRule(_Frozen):
    """Whole-robot state from several scopes (definitions.md 6.6)."""

    precedence: tuple[str, ...]
    idle_requires_all_scopes: bool
    fallback: str


class InterventionRules(_Frozen):
    """Interventions (definitions.md 3, 6.1, 6.2, 6.7)."""

    from_states: tuple[str, ...]
    into_states: tuple[str, ...]
    episode_ends_when_scope_returns_to: tuple[str, ...]
    discrete_event_types: tuple[str, ...]


class HoldRules(_Frozen):
    """Holds (definitions.md 6.8)."""

    idle_in_open_task_becomes: str


class DeviceLocationRules(_Frozen):
    """Device location (definitions.md 6.10)."""

    undeclared_device_state: str


class ReasonRules(_Frozen):
    """Transition reasons (definitions.md 3)."""

    values: tuple[str, ...]
    required_for_states: tuple[str, ...]
    safety_stop_reason: str


class GapRules(_Frozen):
    """Gap handling (definitions.md 2, rule 2)."""

    rule: str


class Params(_Frozen):
    """Parameter defaults, seconds except ``unknown_share_max`` (definitions.md 9)."""

    idle_threshold_s: float
    gap_tolerance_s: float
    heartbeat_interval_s: float
    help_latency_max_s: float
    approval_min_dwell_s: float
    approval_dwell_sensitivity_s: float
    approval_wait_max_s: float
    hint_ttl_s: float
    hint_min_dwell_s: float
    takeover_window_s: float
    simultaneity_window_s: float
    unknown_share_max: float


class FlagRules(_Frozen):
    """Loader flag codes (definitions.md 6.8, 6.10, 8)."""

    codes: tuple[str, ...]


class ErrorRules(_Frozen):
    """Codes for rejected lines, the line length limit in bytes and how many rejected lines of
    each code a reader lists (definitions.md 8)."""

    codes: tuple[str, ...]
    max_line_bytes: PositiveInt
    list_max_per_code: PositiveInt


class TrustRules(_Frozen):
    """Trust thresholds (definitions.md 8)."""

    reconstructed_events_max_level: int
    l3_station_only_max_share: float
    l3_disputed_max_share: float
    default_tau_s: float


def _read_only(value: Mapping[str, StateRule]) -> Mapping[str, StateRule]:
    return MappingProxyType(dict(value))


def _as_dict(value: Mapping[str, StateRule], handler: SerializerFunctionWrapHandler) -> Any:
    return handler(dict(value))


# Cannot be changed after loading, so a loaded Rules object can be shared and cached.
_StateTable = Annotated[
    Mapping[str, StateRule], AfterValidator(_read_only), WrapSerializer(_as_dict)
]


class Rules(_Frozen):
    """Typed, read-only form of ``rules.toml``. Unknown keys are rejected."""

    spec_version: str
    chain_genesis_prefix: str
    states: _StateTable
    whole_robot: WholeRobotRule
    interventions: InterventionRules
    holds: HoldRules
    device_location: DeviceLocationRules
    reasons: ReasonRules
    gaps: GapRules
    params: Params
    flags: FlagRules
    errors: ErrorRules
    trust: TrustRules


def spec_root() -> Traversable:
    """Return the folder that holds the ``v<version>`` spec folders.

    Prefers the copy packaged inside ``autonometry`` (installed wheel) and falls back to the
    repository's ``spec/`` folder (source tree during development).
    """
    packaged = resources.files("autonometry") / "spec"
    if packaged.is_dir():
        return packaged
    source_tree = Path(__file__).resolve().parents[2] / "spec"
    if source_tree.is_dir():
        return source_tree
    raise SpecError("specification files not found")


def available_versions() -> list[str]:
    """Return the spec versions present, oldest first, without the ``v``, such as ``["0.2"]``."""
    found: list[tuple[int, int]] = []
    for entry in spec_root().iterdir():
        match = _FOLDER_RE.fullmatch(entry.name)
        if match and entry.is_dir():
            found.append((int(match.group(1)), int(match.group(2))))
    return [f"{major}.{minor}" for major, minor in sorted(found)]


def _normalise(version: str) -> str:
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        raise SpecError(f"invalid spec version {version!r}; expected a form like '0.2'")
    return f"{int(match.group(1))}.{int(match.group(2))}"


def _version_dir(version: str) -> Traversable:
    """Return the folder of one spec version; reject anything that is not ``[v]N.N``."""
    folder = spec_root() / f"v{_normalise(version)}"
    if not folder.is_dir():
        known = ", ".join(available_versions()) or "none"
        raise SpecError(f"unknown spec version {version!r}; available: {known}")
    return folder


def known_version(version: str) -> str:
    """Return a shipped spec version in normal form (``"v0.2"`` gives ``"0.2"``).

    Raises:
        SpecError: the version is malformed or not shipped with this package.
    """
    _version_dir(version)
    return _normalise(version)


def _read(version: str, name: str) -> bytes:
    try:
        return (_version_dir(version) / name).read_bytes()
    except OSError as exc:
        raise SpecError(f"cannot read {name} of spec {version}: {exc.strerror or exc}") from exc


def spec_file_hashes(version: str) -> dict[str, str]:
    """Return ``{file name: SHA-256 hex}`` for every file of a spec version, sorted by name."""
    hashes = {
        entry.name: hashlib.sha256(entry.read_bytes()).hexdigest()
        for entry in _version_dir(version).iterdir()
        if entry.is_file()
    }
    return dict(sorted(hashes.items()))


def load_schema(version: str) -> dict[str, Any]:
    """Load ``event.schema.json`` (definitions.md section 8) and check it is draft 2020-12."""
    try:
        schema = json.loads(_read(version, _SCHEMA_FILE))
    except ValueError as exc:
        raise SpecError(f"{_SCHEMA_FILE} of spec {version} is not valid JSON: {exc}") from exc
    if not isinstance(schema, dict):
        raise SpecError(f"{_SCHEMA_FILE} of spec {version} is not a JSON object")
    compile_schema(version, schema)
    return schema


def _no_retrieval(uri: str) -> Any:
    """Refuse to fetch a schema reference: loading and validation never touch the network."""
    raise SpecError(f"the event schema refers to {uri}, which is not fetched")


def compile_schema(version: str, schema: dict[str, Any]) -> jsonschema_rs.Draft202012Validator:
    """Compile the event schema of a spec version into a validator (definitions.md section 8).

    Compiling checks the schema against the draft 2020-12 meta-schema and compiles every
    ``pattern`` as ECMA-262, the engine that then validates events. Formats are not checked
    (``ts`` is parsed separately) and references outside the schema are refused, never fetched.

    Raises:
        SpecError: the schema is invalid; the message is one line.
    """
    try:
        return jsonschema_rs.Draft202012Validator(
            schema, validate_formats=False, retriever=_no_retrieval
        )
    except ValueError as exc:
        first_line = (str(exc).splitlines() or [type(exc).__name__])[0]
        raise SpecError(f"{_SCHEMA_FILE} of spec {version} is invalid: {first_line}") from exc


def load_rules(version: str) -> Rules:
    """Load ``rules.toml`` of a spec version as a frozen model; unknown keys are an error."""
    try:
        data = tomllib.loads(_read(version, _RULES_FILE).decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SpecError(f"{_RULES_FILE} of spec {version} is not valid TOML: {exc}") from exc
    try:
        return Rules.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "<root>"
        raise SpecError(
            f"{_RULES_FILE} of spec {version} is invalid at {where}: {first['msg']}"
        ) from exc


def _schema_enum(schema: dict[str, Any], name: str) -> set[str]:
    try:
        values = schema["$defs"][name]["enum"]
    except (KeyError, TypeError) as exc:
        raise SpecError(f"schema has no $defs/{name}/enum") from exc
    return {str(value) for value in values}


def _referenced_states(rules: Rules) -> dict[str, tuple[str, ...]]:
    """State names used outside the ``[states]`` table, keyed by the rule that uses them."""
    return {
        "whole_robot.precedence": rules.whole_robot.precedence,
        "whole_robot.fallback": (rules.whole_robot.fallback,),
        "interventions.from_states": rules.interventions.from_states,
        "interventions.into_states": rules.interventions.into_states,
        "interventions.episode_ends_when_scope_returns_to": (
            rules.interventions.episode_ends_when_scope_returns_to
        ),
        "holds.idle_in_open_task_becomes": (rules.holds.idle_in_open_task_becomes,),
        "device_location.undeclared_device_state": (rules.device_location.undeclared_device_state,),
        "reasons.required_for_states": rules.reasons.required_for_states,
    }


def _compare(kind: str, in_rules: set[str], in_schema: set[str]) -> list[str]:
    return [
        f"{kind} {name!r} is in rules but not in the schema"
        for name in sorted(in_rules - in_schema)
    ] + [
        f"{kind} {name!r} is in the schema but not in rules"
        for name in sorted(in_schema - in_rules)
    ]


def find_inconsistencies(version: str, schema: dict[str, Any], rules: Rules) -> list[str]:
    """Return every disagreement between rules and schema of one version (empty when consistent)."""
    problems: list[str] = []
    if rules.spec_version != _normalise(version):
        problems.append(
            f"rules spec_version {rules.spec_version!r} differs from folder {version!r}"
        )

    rule_states = set(rules.states)
    problems += _compare("state", rule_states, _schema_enum(schema, "state"))

    rule_reasons = set(rules.reasons.values)
    problems += _compare("reason", rule_reasons, _schema_enum(schema, "reason"))
    if rules.reasons.safety_stop_reason not in rule_reasons:
        problems.append(f"safety_stop_reason {rules.reasons.safety_stop_reason!r} is not a reason")

    for where, names in _referenced_states(rules).items():
        problems += [
            f"{where} names unknown state {name!r}" for name in names if name not in rule_states
        ]

    problems += _duplicates("flag code", rules.flags.codes)
    problems += _duplicates("error code", rules.errors.codes)
    problems += [
        f"code {code!r} is both a flag and an error code"
        for code in sorted(set(rules.flags.codes) & set(rules.errors.codes))
    ]
    return problems


def _duplicates(kind: str, codes: tuple[str, ...]) -> list[str]:
    return [
        f"{kind} {code!r} appears more than once"
        for code in sorted({c for c in codes if codes.count(c) > 1})
    ]


def check_consistency(version: str) -> None:
    """Check that rules.toml and the schema of a version agree (definitions.md sections 2, 3, 8).

    Rule states equal the schema state enum, rule reasons equal the schema reason enum, every state
    named in the rules exists, flag and error codes are unique and no code is both.

    Raises:
        SpecConsistencyError: all problems, on one line.
        SpecError: when a file cannot be loaded.
    """
    problems = find_inconsistencies(version, load_schema(version), load_rules(version))
    if problems:
        logger.debug("spec %s inconsistencies: %s", version, problems)
        raise SpecConsistencyError(f"spec {version} is inconsistent: " + "; ".join(problems))
