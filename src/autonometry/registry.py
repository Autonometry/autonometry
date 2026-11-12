"""Device registries: the declared sources that can command a robot (definitions.md 6.10).

A registry is one JSON file valid against ``device_registry.schema.json``. Checking it is pure
apart from reading the file: the bytes are parsed as I-JSON, validated against the schema, checked
for repeated ``device_id`` values and hashed (SHA-256 of the RFC 8785 canonical form), which is the
value ``session_start.device_registry_hash`` must hold. Messages name places in the file, never
values from it.
"""

import functools
import hashlib
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import jsonschema_rs
import rfc8785

from autonometry import strictjson
from autonometry.reader import describe_schema_error, first_schema_error
from autonometry.spec import compile_schema, load_registry_schema

logger = logging.getLogger(__name__)

#: Largest registry file read, in bytes. A registry lists devices, not data: 1 MiB is ample.
MAX_REGISTRY_BYTES = 1 << 20


class RegistryReadError(Exception):
    """A registry file cannot be opened or read. The message is one line."""


@dataclass(frozen=True, slots=True)
class RegistryCheck:
    """Result of checking one device registry file.

    ``sha256`` is the lower-case hex SHA-256 of the registry's RFC 8785 canonical form, set only
    when the registry is valid; ``problems`` are one-line messages, empty when it is valid.
    """

    file: str
    spec_version: str
    devices: int
    sha256: str | None
    problems: tuple[str, ...]

    @property
    def valid(self) -> bool:
        """True when the registry has no problem."""
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        """A JSON-serialisable form with sorted keys when dumped."""
        return {
            "devices": self.devices,
            "file": self.file,
            "problems": list(self.problems),
            "sha256": self.sha256,
            "spec_version": self.spec_version,
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class _RegistrySpec:
    schema: Mapping[str, Any]
    validator: jsonschema_rs.Draft202012Validator
    field_names: frozenset[str]


@functools.cache
def _registry_spec(version: str) -> _RegistrySpec:
    schema = load_registry_schema(version)
    return _RegistrySpec(
        MappingProxyType(schema),
        compile_schema(version, schema, "device_registry.schema.json"),
        frozenset(_property_names(schema)),
    )


def _property_names(node: Any) -> Iterator[str]:
    """Every name declared under a ``properties`` keyword anywhere in the schema."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                yield from value
            yield from _property_names(value)
    elif isinstance(node, list):
        for item in node:
            yield from _property_names(item)


def registry_hash(registry: Any) -> str:
    """Return the lower-case hex SHA-256 of a registry's RFC 8785 canonical form
    (definitions.md 6.10, ``session_start.device_registry_hash``)."""
    return hashlib.sha256(rfc8785.dumps(registry)).hexdigest()


def check_registry_bytes(data: bytes, file: str, spec_version: str) -> RegistryCheck:
    """Check the bytes of one device registry against definitions.md 6.10.

    Problems: more than :data:`MAX_REGISTRY_BYTES` bytes, not UTF-8, not I-JSON, not valid against
    ``device_registry.schema.json``, or a ``device_id`` that repeats an earlier one.
    """

    def failed(*problems: str, devices: int = 0) -> RegistryCheck:
        return RegistryCheck(file, spec_version, devices, None, problems)

    if len(data) > MAX_REGISTRY_BYTES:
        return failed(f"file is larger than {MAX_REGISTRY_BYTES} bytes")
    try:
        value = strictjson.parse(data.decode("utf-8").removeprefix("﻿"))
    except UnicodeDecodeError as exc:
        return failed(f"byte {exc.start + 1} is not valid UTF-8")
    except strictjson.StrictJsonError as exc:
        return failed(f"{exc.code}: {exc.message}")
    spec = _registry_spec(spec_version)
    error = first_schema_error(spec.validator, value)
    if error is not None:
        message = describe_schema_error(error, spec.schema, spec.field_names, root="registry")
        return failed(f"schema_violation: {message}")
    devices: list[dict[str, Any]] = value["devices"]
    repeats = tuple(_repeated_ids(devices))
    if repeats:
        return failed(*repeats, devices=len(devices))
    return RegistryCheck(file, spec_version, len(devices), registry_hash(value), ())


def _repeated_ids(devices: list[dict[str, Any]]) -> Iterator[str]:
    first: dict[str, int] = {}
    for index, device in enumerate(devices):
        earlier = first.setdefault(device["device_id"], index)
        if earlier != index:
            yield (
                f"duplicate_device_id: /devices/{index} repeats the device_id of /devices/{earlier}"
            )


def check_registry(path: str | os.PathLike[str], spec_version: str) -> RegistryCheck:
    """Read and check one device registry file (definitions.md 6.10).

    Reads at most :data:`MAX_REGISTRY_BYTES` + 1 bytes, so a huge file is refused without being
    held in memory.

    Raises:
        RegistryReadError: the file cannot be opened or read.
    """
    name = os.fspath(path)
    try:
        with open(name, "rb") as stream:
            data = stream.read(MAX_REGISTRY_BYTES + 1)
    except OSError as exc:
        logger.debug("cannot read registry %s", name, exc_info=True)
        raise RegistryReadError(f"cannot read {name}: {exc.strerror or exc}") from None
    return check_registry_bytes(data, name, spec_version)
