"""Read event logs safely (definitions.md section 8, Rejected lines).

A log is one or more JSON Lines files. Each line is read as bytes with a length limit, decoded as
UTF-8, parsed as I-JSON, validated against the spec's event schema, and its ``ts`` parsed. A line
that fails any step is rejected with an error code from ``rules.toml`` ``[errors]`` and its line
number. Valid events are grouped by ``(robot_id, session_id)`` and ordered by ``seq``, and each
session gets the loader flags of :func:`autonometry.checks.check_session`.

Memory grows with the number of valid events kept, never with the size or number of rejected
lines: a line longer than the limit is skipped in chunks without being held whole, and only the
first ``list_max_per_code`` rejected lines of each code are kept, while every count stays exact.
"""

import functools
import json
import os
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import IO, Any, cast, get_args

import jsonschema_rs

from autonometry import strictjson
from autonometry.checks import check_session
from autonometry.events import Counts, ErrorCode, Event, FlagCode, LineError, LogCheck, Session
from autonometry.spec import (
    Rules,
    SpecError,
    compile_schema,
    find_inconsistencies,
    load_rules,
    load_schema,
)
from autonometry.timestamps import ts_to_ns

_BOM = b"\xef\xbb\xbf"
_LINE_END = b"\r\n"
_JSON_SPACE = b" \t\r"


class LogReadError(Exception):
    """A log file cannot be opened or read. The message is one line."""


class RejectedEventError(Exception):
    """A line or value is not one valid event; ``code`` is a ``rules.toml`` ``[errors]`` code and
    ``message`` names the place, never the contents."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message


@dataclass(frozen=True, slots=True)
class _Spec:
    rules: Rules
    schema: Mapping[str, Any]
    validator: jsonschema_rs.Draft202012Validator
    field_names: frozenset[str]
    integer_fields: tuple[str, ...]


@functools.cache
def _load_spec(version: str) -> _Spec:
    """Load, check and compile one spec version once; the result is read-only and shared."""
    rules = load_rules(version)
    schema = load_schema(version)
    problems = find_inconsistencies(version, schema, rules)
    if set(get_args(ErrorCode)) != set(rules.errors.codes):
        problems.append("its [errors] codes differ from the ones this reader reports")
    if not set(get_args(FlagCode)) <= set(rules.flags.codes):
        problems.append("its [flags] codes lack ones this reader reports")
    if problems:
        raise SpecError(f"spec {version} cannot be used: " + "; ".join(problems))
    # Patterns follow ECMA-262, as JSON Schema requires, so "$" does not accept a trailing newline.
    return _Spec(
        rules,
        MappingProxyType(schema),
        compile_schema(version, schema),
        frozenset(schema.get("properties", ())),
        _integer_fields(schema),
    )


def _integer_fields(schema: Mapping[str, Any]) -> tuple[str, ...]:
    """Names of the top-level fields the schema types as ``integer``, directly or via ``$ref``."""
    names: list[str] = []
    for name, rule in schema.get("properties", {}).items():
        target = _schema_value(schema, ["properties", name, "$ref"]) or {}
        if "integer" in _as_list(rule.get("type")) + _as_list(target.get("type")):
            names.append(name)
    return tuple(sorted(names))


def iter_lines(stream: IO[bytes], max_line_bytes: int) -> Iterator[bytes | None]:
    """Yield each line of a binary stream without its LF or CRLF ending (definitions.md 8).

    A UTF-8 byte order mark is removed from the very start of the stream only. A line whose
    content is longer than ``max_line_bytes`` bytes is yielded as ``None``; it is read and dropped
    in pieces of at most ``max_line_bytes + 5`` bytes, so it is never held in memory whole.
    """
    piece = max_line_bytes + len(_LINE_END) + len(_BOM)
    first = True
    while raw := stream.readline(piece):
        truncated = len(raw) == piece and not raw.endswith(b"\n")
        if first:
            raw = raw.removeprefix(_BOM)
            first = False
            if not raw:
                continue
        if truncated:
            while (rest := stream.readline(piece)) and not rest.endswith(b"\n"):
                pass
            yield None
            continue
        content = raw.removesuffix(b"\n").removesuffix(b"\r")
        yield None if len(content) > max_line_bytes else content


def first_schema_error(
    validator: jsonschema_rs.Draft202012Validator, value: Any
) -> jsonschema_rs.ValidationError | None:
    """Return the schema error to report for ``value``, or None when it is valid.

    The choice does not depend on the validator's own order (see :func:`_error_rank`).
    """
    if validator.is_valid(value):
        return None
    return min(validator.iter_errors(value), key=_error_rank)


def describe_schema_error(
    error: jsonschema_rs.ValidationError,
    schema: Mapping[str, Any],
    field_names: frozenset[str],
    root: str = "event",
) -> str:
    """Describe a schema error by location and rule, without quoting any value from the input.

    The rule is the keyword at the end of the error's schema path and its value in the schema.
    Path parts that are not schema field names (for example scope names used as keys) print as
    ``*`` and unexpected field names are not repeated, so no input contents reach the output.
    An error at the top level is placed at ``root``.
    """
    parts = [
        str(part) if isinstance(part, int) or part in field_names else "*"
        for part in error.instance_path
    ]
    where = "/" + "/".join(parts) if parts else root
    keyword = str(error.schema_path[-1]) if error.schema_path else ""
    expected = _schema_value(schema, error.schema_path)
    if keyword == "required" and isinstance(expected, list):
        present = error.instance if isinstance(error.instance, dict) else {}
        rule = f"missing field {', '.join(n for n in expected if n not in present)}"
    elif keyword == "additionalProperties":
        rule = "has a field the schema does not define (extensions start with x_)"
    elif keyword == "type":
        rule = f"must be of type {' or '.join(_as_list(expected))}"
    elif keyword in ("enum", "const"):
        allowed = expected if keyword == "enum" else [expected]
        rule = f"must be one of {', '.join(json.dumps(v) for v in allowed)}"
    elif isinstance(error.instance, dict) and keyword in _KEY_RULES:
        # A propertyNames rule: the object is the instance and the failing key is not repeated.
        rule = f"has a key {_KEY_RULES[keyword]} {expected}"
    elif keyword == "uniqueItems":
        rule = "must not contain the same item twice"
    elif keyword == "pattern":
        rule = f"does not match the pattern {expected}"
    elif keyword in _BOUNDS:
        rule = f"must be {_BOUNDS[keyword]} {expected}"
    else:
        rule = f"fails the schema rule {keyword}"
    return f"{where}: {rule}"


_KEY_RULES = {"pattern": "that does not match the pattern", "minLength": "of length <"}

_BOUNDS = {
    "minimum": ">=",
    "exclusiveMinimum": ">",
    "minLength": "a string of length >=",
    "maxLength": "a string of length <=",
    "minItems": "a list of length >=",
    "minProperties": "an object with a number of fields >=",
}


def _error_rank(error: jsonschema_rs.ValidationError) -> tuple[int, str, str]:
    """Order schema errors so the reported one does not depend on the validator's own order:
    the error highest in the schema first (it usually explains the others), then by path text."""
    return len(error.schema_path), str(error.schema_path), str(error.instance_path)


def _schema_value(schema: Mapping[str, Any], path: Sequence[str | int]) -> Any:
    """Return the schema value at ``path``, following local ``$ref`` hops; None if absent."""
    node: Any = schema
    for part in path:
        if part == "$ref" and isinstance(node, Mapping) and str(node.get("$ref")).startswith("#/"):
            node = _schema_value(schema, node["$ref"][2:].split("/"))
            continue
        try:
            node = node[part]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def _as_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else [str(value)]


def _json_kind(value: object) -> str:
    if isinstance(value, list):
        return "an array"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, bool):
        return "a boolean"
    if value is None:
        return "null"
    return "a number"


@dataclass
class _Reader:
    """Accumulates valid events and rejected lines across files; the only mutable state."""

    spec: _Spec
    files: int = 0
    lines: int = 0
    blank_lines: int = 0
    errors: dict[ErrorCode, list[LineError]] = field(default_factory=dict)
    error_counts: Counter[ErrorCode] = field(default_factory=Counter)
    sessions: dict[tuple[str, str], dict[int, Event]] = field(default_factory=dict)
    field_names: dict[str, str] = field(default_factory=dict)

    def read(self, name: str, stream: IO[bytes]) -> None:
        self.files += 1
        limit = self.spec.rules.errors.max_line_bytes
        for number, content in enumerate(iter_lines(stream, limit), start=1):
            self.lines += 1
            if content is None:
                self._reject(name, number, "line_too_long", f"line is longer than {limit} bytes")
            elif not content.strip(_JSON_SPACE):
                self.blank_lines += 1
            else:
                try:
                    self._keep(self._event(content))
                except RejectedEventError as exc:
                    self._reject(name, number, exc.code, exc.message)

    def _reject(self, name: str, number: int, code: ErrorCode, message: str) -> None:
        """Count a rejected line; keep it only while its code has fewer than the listed maximum."""
        self.error_counts[code] += 1
        listed = self.errors.setdefault(code, [])
        if len(listed) < self.spec.rules.errors.list_max_per_code:
            listed.append(LineError(name, number, code, message))

    def _event(self, content: bytes) -> Event:
        """Return the line as a validated event (definitions.md 8)."""
        try:
            value = strictjson.parse(content.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise RejectedEventError(
                "invalid_utf8", f"byte {exc.start + 1} is not valid UTF-8"
            ) from None
        except strictjson.StrictJsonError as exc:
            raise RejectedEventError(cast(ErrorCode, exc.code), exc.message) from None
        return _checked(value, self.spec)

    def _keep(self, event: Event) -> None:
        """Add the event to its session; a seq already used there is rejected."""
        events = self.sessions.setdefault((event["robot_id"], event["session_id"]), {})
        if event["seq"] in events:
            message = f"seq {event['seq']} is already used in this session"
            raise RejectedEventError("duplicate_seq", message)
        previous = next(reversed(events.values()), None)
        events[event["seq"]] = self._shared(event, previous)

    def _shared(self, event: Event, previous: Event | None) -> Event:
        """Return an equal event that reuses field names of earlier events and string values of
        the previous one in its session, so a long log keeps one copy of each repeated string."""
        before: Mapping[str, Any] = previous or {}
        shared: dict[str, Any] = {}
        for name, value in event.items():
            same = before.get(name)
            shared[self.field_names.setdefault(name, name)] = (
                same if isinstance(value, str) and same == value else value
            )
        return cast(Event, shared)

    def finish(self) -> LogCheck:
        sessions = tuple(
            Session(robot_id, session_id, tuple(events[seq] for seq in sorted(events)))
            for (robot_id, session_id), events in sorted(self.sessions.items())
        )
        flags = sorted(
            (flag for session in sessions for flag in check_session(session, self.spec.rules)),
            key=lambda f: (f.robot_id, f.session_id, f.seq, f.code, f.detail),
        )
        errors = sorted(
            (error for listed in self.errors.values() for error in listed),
            key=lambda e: (e.file, e.line),
        )
        counts = Counts(
            files=self.files,
            lines=self.lines,
            blank_lines=self.blank_lines,
            events=sum(len(session.events) for session in sessions),
            rejected_lines=self.error_counts.total(),
            sessions=len(sessions),
            errors_by_code=MappingProxyType(dict(sorted(self.error_counts.items()))),
            flags_by_code=_tally(f.code for f in flags),
        )
        return LogCheck(self.spec.rules.spec_version, sessions, tuple(errors), tuple(flags), counts)


def _checked(value: Any, spec: _Spec) -> Event:
    """Return an already parsed I-JSON value as a validated event (definitions.md 8): an object,
    valid against the event schema, integer fields written as integers, a possible ``ts``."""
    if not isinstance(value, dict):
        raise RejectedEventError("not_an_object", f"line is {_json_kind(value)}, not a JSON object")
    problem = first_schema_error(spec.validator, value)
    if problem is not None:
        message = describe_schema_error(problem, spec.schema, spec.field_names)
        raise RejectedEventError("schema_violation", message)
    for name in spec.integer_fields:
        if isinstance(value.get(name), float):
            raise RejectedEventError("schema_violation", f"/{name}: must be written as an integer")
    try:
        ts_to_ns(value["ts"])
    except ValueError:
        raise RejectedEventError(
            "invalid_timestamp", "ts is not a possible UTC date and time"
        ) from None
    return cast(Event, value)


def check_event(value: Any, spec_version: str) -> Event:
    """Check one parsed I-JSON value exactly as :func:`read_logs` checks a line once parsed
    (definitions.md 8): a JSON object valid against the event schema, integer fields written as
    integers and a possible ``ts``. The recorder uses it so every event it writes passes
    ``validate``.

    Raises:
        RejectedEventError: with code ``not_an_object``, ``schema_violation`` or
            ``invalid_timestamp``.
        SpecError: the spec version is unknown or unusable.
    """
    return _checked(value, _load_spec(spec_version))


def _tally(codes: Iterable[str]) -> Mapping[str, int]:
    return MappingProxyType(dict(sorted(Counter(codes).items())))


def read_streams(sources: Iterable[tuple[str, IO[bytes]]], spec_version: str) -> LogCheck:
    """Read and check named binary streams in order (definitions.md 2, 3 and 8).

    When the same ``seq`` appears twice in a session, the later line in that order is rejected.

    Raises:
        SpecError: the spec version is unknown or unusable.
    """
    reader = _Reader(_load_spec(spec_version))
    for name, stream in sources:
        reader.read(name, stream)
    return reader.finish()


def read_logs(paths: Sequence[str | os.PathLike[str]], spec_version: str) -> LogCheck:
    """Read and check JSON Lines files in the order given (definitions.md 2, 3 and 8).

    Each file is named in errors as given. When the same ``seq`` appears twice in a session, the
    later line in that order is rejected.

    Raises:
        SpecError: the spec version is unknown or unusable.
        LogReadError: a file cannot be opened or read.
    """
    reader = _Reader(_load_spec(spec_version))
    for path in paths:
        name = os.fspath(path)
        try:
            with open(path, "rb") as stream:
                reader.read(name, stream)
        except OSError as exc:
            raise LogReadError(f"cannot read {name}: {exc.strerror or exc}") from exc
    return reader.finish()
