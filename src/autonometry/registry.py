"""Registries declared before a session: devices (definitions.md 6.10), tasks (1, Task) and the
plan (3, reason ``planned``).

A registry is one JSON file valid against its schema (``device_registry.schema.json``,
``task_registry.schema.json`` or ``plan.schema.json``). Checking it is pure apart from reading the
file: the bytes are parsed as I-JSON, validated against the schema, checked for repeated ids (and,
for devices, for devices that can never be matched) and hashed (SHA-256 of the RFC 8785 canonical
form), which is the value ``session_start`` must hold in ``device_registry_hash``,
``task_registry_hash`` or ``plan_hash`` (definitions.md 8, Registries). Messages name places in
the file, never values from it.
"""

import functools
import hashlib
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

import jsonschema_rs
import rfc8785

from autonometry import strictjson
from autonometry.reader import describe_schema_error, first_schema_error
from autonometry.spec import (
    REGISTRY_SCHEMA_FILES,
    RegistryKind,
    compile_schema,
    load_registry_schema,
)

logger = logging.getLogger(__name__)

#: Largest registry file read, in bytes. A registry lists declarations, not data: 1 MiB is ample.
MAX_REGISTRY_BYTES = 1 << 20


@dataclass(frozen=True, slots=True)
class _Layout:
    """Where a kind of registry keeps its entries, and the id field that must not repeat."""

    entries: str
    id_field: str
    #: The ``session_start`` field holding the registry's hash (definitions.md 8).
    hash_field: str
    #: The name of the copy in a session folder (definitions.md 8, Registries).
    copy_name: str


LAYOUTS: Final[Mapping[RegistryKind, _Layout]] = MappingProxyType(
    {
        "devices": _Layout("devices", "device_id", "device_registry_hash", "devices.json"),
        "tasks": _Layout("tasks", "task_definition_id", "task_registry_hash", "tasks.json"),
        "plan": _Layout("items", "plan_item_id", "plan_hash", "plan.json"),
    }
)


class RegistryReadError(Exception):
    """A registry file cannot be opened or read. The message is one line."""


@dataclass(frozen=True, slots=True)
class RegistryCheck:
    """Result of checking one registry file.

    ``sha256`` is the lower-case hex SHA-256 of the registry's RFC 8785 canonical form, set only
    when the registry is valid; ``problems`` are one-line messages, empty when it is valid;
    ``document`` is the parsed registry when it is valid, else ``None``.
    """

    file: str
    spec_version: str
    kind: RegistryKind
    entries: int
    sha256: str | None
    problems: tuple[str, ...]
    document: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    @property
    def valid(self) -> bool:
        """True when the registry has no problem."""
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        """A JSON-serialisable form with sorted keys when dumped; the contents are left out."""
        return {
            "entries": self.entries,
            "file": self.file,
            "kind": self.kind,
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
def _registry_spec(version: str, kind: RegistryKind) -> _RegistrySpec:
    schema = load_registry_schema(version, kind)
    return _RegistrySpec(
        MappingProxyType(schema),
        compile_schema(version, schema, REGISTRY_SCHEMA_FILES[kind]),
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
    (definitions.md 6.10 and 8, Registries)."""
    return hashlib.sha256(rfc8785.dumps(registry)).hexdigest()


def check_registry_bytes(
    data: bytes, file: str, spec_version: str, kind: RegistryKind = "devices"
) -> RegistryCheck:
    """Check the bytes of one registry of ``kind`` (definitions.md 1, 3, 6.10 and 8).

    Problems: more than :data:`MAX_REGISTRY_BYTES` bytes, not UTF-8, not I-JSON, not valid against
    the kind's schema, an id that repeats an earlier one (``duplicate_device_id``,
    ``duplicate_task_definition_id``, ``duplicate_plan_item_id``), or a device shadowed by an
    earlier one whose ``match`` fields it all contains with the same values.
    """

    def failed(*problems: str, entries: int = 0) -> RegistryCheck:
        return RegistryCheck(file, spec_version, kind, entries, None, problems)

    if len(data) > MAX_REGISTRY_BYTES:
        return failed(f"file is larger than {MAX_REGISTRY_BYTES} bytes")
    try:
        value = strictjson.parse(data.decode("utf-8").removeprefix("﻿"))
    except UnicodeDecodeError as exc:
        return failed(f"byte {exc.start + 1} is not valid UTF-8")
    except strictjson.StrictJsonError as exc:
        return failed(f"{exc.code}: {exc.message}")
    spec = _registry_spec(spec_version, kind)
    error = first_schema_error(spec.validator, value)
    if error is not None:
        message = describe_schema_error(error, spec.schema, spec.field_names, root="registry")
        return failed(f"schema_violation: {message}")
    layout = LAYOUTS[kind]
    entries: list[dict[str, Any]] = value[layout.entries]
    problems = [*_repeated_ids(entries, layout)]
    if kind == "devices":
        problems += _shadowed_devices(entries)
    if problems:
        return failed(*problems, entries=len(entries))
    return RegistryCheck(
        file, spec_version, kind, len(entries), registry_hash(value), (), MappingProxyType(value)
    )


def _repeated_ids(entries: list[dict[str, Any]], layout: _Layout) -> Iterator[str]:
    first: dict[str, int] = {}
    where = layout.entries
    for index, entry in enumerate(entries):
        earlier = first.setdefault(entry[layout.id_field], index)
        if earlier != index:
            yield (
                f"duplicate_{layout.id_field}: /{where}/{index} repeats the {layout.id_field} "
                f"of /{where}/{earlier}"
            )


def _shadowed_devices(devices: list[dict[str, Any]]) -> Iterator[str]:
    """Devices that can never be matched: an earlier device's ``match`` fields all appear in
    theirs with the same values, and the first match wins (definitions.md 6.10)."""
    for index, device in enumerate(devices):
        fields = device["match"].items()
        earlier = next((i for i in range(index) if devices[i]["match"].items() <= fields), None)
        if earlier is not None:
            yield (
                f"shadowed_device: /devices/{index} can never be matched: the match of "
                f"/devices/{earlier} comes first and covers it"
            )


def check_registry(
    path: str | os.PathLike[str], spec_version: str, kind: RegistryKind = "devices"
) -> RegistryCheck:
    """Read and check one registry file of ``kind`` (definitions.md 1, 3, 6.10 and 8).

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
    return check_registry_bytes(data, name, spec_version, kind)


def hash_problem(check: RegistryCheck, declared: str | None) -> str | None:
    """Why a registry cannot be used for a session whose ``session_start`` declares the hash
    ``declared`` (definitions.md 8, Registries), or ``None`` when it matches. One line, naming the
    file and the ``session_start`` field, never values from the file."""
    field_name = LAYOUTS[check.kind].hash_field
    if not check.valid:
        return f"{check.file} is not a valid {check.kind} registry: {check.problems[0]}"
    if declared is None:
        return f"{check.file} cannot be matched: session_start has no {field_name}"
    if check.sha256 != declared:
        return f"{check.file} does not match session_start.{field_name}"
    return None
