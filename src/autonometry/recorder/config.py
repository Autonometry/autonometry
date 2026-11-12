"""Recorder configuration: one TOML file, read with :mod:`tomllib` into a frozen pydantic model.

Unknown keys are rejected. Relative paths are taken relative to the folder of the config file.
Spec parameters (``heartbeat_interval_s``, ``gap_tolerance_s``, sealing intervals, limits) come
from the spec's ``rules.toml``, never from this file. :func:`prepare` then reads everything the
config points to (signing key, registries, plan, TSA roots) so that a recorder refuses to start,
with one line, before it writes anything.
"""

import hashlib
import math
import tomllib
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import rfc8785
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from autonometry import keys, strictjson, tsa
from autonometry.registry import MAX_REGISTRY_BYTES, RegistryReadError, check_registry
from autonometry.spec import Rules, SpecError, available_versions, load_rules

#: Largest config file read, in bytes.
MAX_CONFIG_BYTES = 1 << 20
#: Smallest allowed ``max_log_bytes``: room for a session's opening events and many 64 KiB lines.
MIN_LOG_BYTES = 1 << 20
_DEFAULT_LOG_BYTES = 256 << 20
#: Most scopes a config may declare.
MAX_SCOPES = 256

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
ScopeName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
GroupName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")]
Unobserved = Literal["safety_stops", "human_inputs", "tasks"]


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


def _unique[T](values: tuple[T, ...]) -> tuple[T, ...]:
    if len(set(values)) != len(values):
        raise ValueError("must not list the same value twice")
    return values


PositiveSeconds = Annotated[float, Field(gt=0), AfterValidator(_finite)]


class ConfigError(Exception):
    """The config, or a file it points to, cannot be used. The message is one line."""


class RecorderConfig(BaseModel):
    """The recorder's settings (README, "Recorder"). Frozen; unknown keys are rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    robot_id: Identifier
    data_dir: Path
    socket_path: Path
    socket_group: GroupName | None = None
    scopes: Annotated[
        tuple[ScopeName, ...], Field(min_length=1, max_length=MAX_SCOPES), AfterValidator(_unique)
    ] = ("whole_robot",)
    scope_weights: (
        dict[ScopeName, Annotated[float, Field(gt=0), AfterValidator(_finite)]] | None
    ) = None
    hint_policy: Literal["hint_as_shared", "hint_as_event"] = "hint_as_shared"
    simulated: bool
    unobserved: Annotated[tuple[Unobserved, ...], AfterValidator(_unique)] = ()
    clock_source: Literal["ntp", "ptp", "gnss"]
    key_path: Path
    device_registry: Path | None = None
    task_registry: Path
    plan: Path
    tsa_url: str | None = None
    tsa_roots: Path | None = None
    fsync_interval_s: PositiveSeconds = 1.0
    max_events_per_s: PositiveInt = 1000
    max_log_bytes: Annotated[int, Field(ge=MIN_LOG_BYTES)] = _DEFAULT_LOG_BYTES

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.scope_weights is not None:
            undeclared = sorted(set(self.scope_weights) - set(self.scopes))
            if undeclared:
                raise ValueError(
                    f"scope_weights names undeclared scope(s): {', '.join(undeclared)}"
                )
        if (self.tsa_url is None) != (self.tsa_roots is None):
            raise ValueError("tsa_url and tsa_roots must be given together")
        if self.tsa_url is not None:
            scheme = urllib.parse.urlsplit(self.tsa_url).scheme
            if scheme not in ("http", "https"):
                raise ValueError("tsa_url must start with http:// or https://")
        return self


_PATH_FIELDS = (
    "data_dir",
    "socket_path",
    "key_path",
    "device_registry",
    "task_registry",
    "plan",
    "tsa_roots",
)


def parse_config(text: str, base: Path) -> RecorderConfig:
    """Parse the TOML text of a config; relative paths are made relative to ``base``.

    Raises:
        ConfigError: the text is not TOML or does not describe a valid config.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"not valid TOML: {exc}") from None
    for name in _PATH_FIELDS:
        value = data.get(name)
        if isinstance(value, str):
            data[name] = str(base / value)
    try:
        return RecorderConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "config"
        raise ConfigError(f"{where}: {first['msg']}") from None


def load_config(path: Path) -> RecorderConfig:
    """Read and parse a config file of at most :data:`MAX_CONFIG_BYTES` bytes.

    Raises:
        ConfigError: the file cannot be read, is not UTF-8 TOML or is not a valid config.
    """
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{path} is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: byte {exc.start + 1} is not valid UTF-8") from None
    try:
        return parse_config(text, path.resolve().parent)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def json_file_hash(path: Path) -> str:
    """Return the lower-case hex SHA-256 of a JSON file's RFC 8785 canonical form, the value of
    ``session_start.task_registry_hash`` and ``plan_hash`` (definitions.md 1 and 3). The file must
    be I-JSON of at most 1 MiB.

    Raises:
        ConfigError: the file cannot be read or is not I-JSON.
    """
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_REGISTRY_BYTES + 1)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    if len(data) > MAX_REGISTRY_BYTES:
        raise ConfigError(f"{path} is larger than {MAX_REGISTRY_BYTES} bytes")
    try:
        value = strictjson.parse(data.decode("utf-8").removeprefix("﻿"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: byte {exc.start + 1} is not valid UTF-8") from None
    except strictjson.StrictJsonError as exc:
        raise ConfigError(f"{path}: {exc.code}: {exc.message}") from None
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class Prepared:
    """A config with everything it points to loaded and checked: the spec rules, the signing key
    and its name, the hashes for ``session_start`` and the trusted TSA roots."""

    config: RecorderConfig
    spec_version: str
    rules: Rules
    key: Ed25519PrivateKey
    key_name: str
    task_registry_hash: str
    plan_hash: str
    device_registry_hash: str | None
    tsa_roots: tuple[x509.Certificate, ...]

    def declaration(self) -> Mapping[str, Any]:
        """The ``session_start`` fields that the config decides (definitions.md 8), except
        ``initial_states``, which is the recorder's state when the session opens."""
        config = self.config
        fields: dict[str, Any] = {
            "scopes": list(config.scopes),
            "heartbeat_interval_s": self.rules.params.heartbeat_interval_s,
            "hint_policy": config.hint_policy,
            "task_registry_hash": self.task_registry_hash,
            "plan_hash": self.plan_hash,
            "simulated": config.simulated,
        }
        if config.scope_weights is not None:
            fields["scope_weights"] = dict(config.scope_weights)
        if self.device_registry_hash is not None:
            fields["device_registry_hash"] = self.device_registry_hash
        if config.unobserved:
            fields["unobserved"] = list(config.unobserved)
        return fields


def _newest_spec() -> str:
    versions = available_versions()
    if not versions:
        raise ConfigError("no specification is shipped with this package")
    return versions[-1]


def _device_registry_hash(path: Path | None, spec_version: str) -> str | None:
    if path is None:
        return None
    try:
        check = check_registry(path, spec_version)
    except (RegistryReadError, SpecError) as exc:
        raise ConfigError(str(exc)) from None
    if check.sha256 is None:
        raise ConfigError(f"device registry {path} is not valid: {check.problems[0]}")
    return check.sha256


def _tsa_roots(path: Path | None) -> tuple[x509.Certificate, ...]:
    if path is None:
        return ()
    try:
        return tuple(tsa.load_tsa_roots(path.read_bytes()))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    except tsa.TsaError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def prepare(config: RecorderConfig) -> Prepared:
    """Load everything ``config`` points to, for the newest spec version shipped.

    Raises:
        ConfigError: the spec, key, a registry, the plan or the TSA roots cannot be used.
    """
    try:
        version = _newest_spec()
        rules = load_rules(version)
    except SpecError as exc:
        raise ConfigError(str(exc)) from None
    try:
        key = keys.load(config.key_path)
        key_name = keys.load_key_name(config.key_path)
    except keys.SigningKeyError as exc:
        raise ConfigError(str(exc)) from None
    return Prepared(
        config=config,
        spec_version=version,
        rules=rules,
        key=key,
        key_name=key_name,
        task_registry_hash=json_file_hash(config.task_registry),
        plan_hash=json_file_hash(config.plan),
        device_registry_hash=_device_registry_hash(config.device_registry, version),
        tsa_roots=_tsa_roots(config.tsa_roots),
    )
