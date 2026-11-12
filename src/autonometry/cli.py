"""Command line interface: ``autonometry``."""

import hashlib
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, NoReturn, cast

import rfc8785
import typer
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonometry import __version__, keys, sealfile, sealing, signednote, tsa
from autonometry import verify as verify_
from autonometry.events import LogCheck, Session, TrustLevel, VerifyResult
from autonometry.fileio import append_durably, atomic_write
from autonometry.reader import LogReadError, read_logs
from autonometry.registry import RegistryReadError, check_registry
from autonometry.sealfile import SealFile
from autonometry.signednote import SignedNoteError, VerifierKey
from autonometry.spec import (
    Rules,
    SpecError,
    available_versions,
    known_version,
    load_rules,
    spec_file_hashes,
)

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="autonometry",
    help="Measure how much of a robot's work is done by its AI and how much by humans.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
spec_app = typer.Typer(
    help="Inspect the specification shipped with this package.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(spec_app, name="spec")
registry_app = typer.Typer(
    help="Check device registries (definitions.md 6.10).",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(registry_app, name="registry")

_SHOWN_PER_CODE = 20
_VERBOSE_FORMAT = "%(levelname)s %(name)s: %(message)s"


class _OneLineFormatter(logging.Formatter):
    """``warning: message``: the first line of the message only, never exception text."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage().splitlines() or [""]
        return f"{record.levelname.lower()}: {message[0]}"


def _configure_logging() -> None:
    """Send warnings and errors to stderr as one clean line each; called once, by ``main``."""
    handler = logging.StreamHandler()
    handler.setFormatter(_OneLineFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)


def _set_verbose(value: bool) -> None:
    """``--verbose``: debug messages and full exception text on the handlers ``main`` set up."""
    if not value:
        return
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in root.handlers:
        if isinstance(handler.formatter, _OneLineFormatter):
            handler.setFormatter(logging.Formatter(_VERBOSE_FORMAT))


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"autonometry {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show the version."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose", "-v", callback=_set_verbose, help="Show debug output and full errors."
        ),
    ] = False,
) -> None:
    """Measure how much of a robot's work is done by its AI and how much by humans."""


@spec_app.command("list")
def spec_list() -> None:
    """List the spec versions shipped with this package."""
    try:
        versions = available_versions()
    except SpecError as exc:
        _fail(str(exc))
    for version in versions:
        typer.echo(version)


@spec_app.command("show")
def spec_show(
    version: Annotated[str, typer.Argument(help="Spec version, for example 0.3.")],
) -> None:
    """Show the files of one spec version with their SHA-256 hashes."""
    try:
        hashes = spec_file_hashes(version)
    except SpecError as exc:
        _fail(str(exc))
    for name, digest in hashes.items():
        typer.echo(f"{digest}  {name}")


def _spec_version(spec: str | None) -> str:
    """Resolve ``--spec`` to a concrete, shipped spec version; the newest one when ``None``."""
    try:
        return known_version(spec) if spec is not None else available_versions()[-1]
    except SpecError as exc:
        raise typer.BadParameter(str(exc), param_hint="'--spec'") from None
    except IndexError:
        _fail("no specification is shipped with this package")


@app.command("validate")
def validate(
    paths: Annotated[
        list[Path],
        typer.Argument(
            help="JSON Lines event log files.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON instead of a summary.")
    ] = False,
    spec: Annotated[
        str | None,
        typer.Option(
            "--spec", help="Spec version, for example 0.3. Default: the newest one shipped."
        ),
    ] = None,
) -> None:
    """Check event logs: reject unreadable lines and flag structural problems.

    Exit status: 0 when no line was rejected (flags are warnings), 1 when any line was rejected,
    2 on bad usage. Event contents are never printed.
    """
    version = _spec_version(spec)
    try:
        result = read_logs(paths, version)
    except (SpecError, LogReadError) as exc:
        _fail(str(exc))
    if as_json:
        typer.echo(json.dumps(result.to_json(), indent=2, sort_keys=True))
    else:
        for line in summary_lines(result):
            typer.echo(line)
    if result.errors:
        raise typer.Exit(1)


@registry_app.command("check")
def registry_check(
    path: Annotated[
        Path,
        typer.Argument(
            help="Device registry JSON file.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON instead of a summary.")
    ] = False,
    spec: Annotated[
        str | None,
        typer.Option(
            "--spec", help="Spec version, for example 0.3. Default: the newest one shipped."
        ),
    ] = None,
) -> None:
    """Check a device registry and print its hash for session_start.device_registry_hash.

    Exit status: 0 when the registry is valid, 1 when it is not (or cannot be read), 2 on bad
    usage. Values from the file are never printed.
    """
    version = _spec_version(spec)
    try:
        result = check_registry(path, version)
    except (SpecError, RegistryReadError) as exc:
        _fail(str(exc))
    if as_json:
        typer.echo(json.dumps(result.to_json(), indent=2, sort_keys=True))
    elif result.sha256 is not None:
        typer.echo(f"spec {version}: {result.devices} device(s), valid")
        typer.echo(f"sha256 {result.sha256}")
    else:
        typer.echo(f"spec {version}: NOT valid")
        for problem in result.problems:
            typer.echo(f"  {problem}")
    if not result.valid:
        raise typer.Exit(1)


def summary_lines(result: LogCheck) -> list[str]:
    """Return the human summary of a check: totals, then errors and flags grouped by code.

    Each group shows its exact count, the first 20 items and how many more there are, also when
    the result lists only some of the rejected lines.
    """
    counts = result.counts
    lines = [
        f"spec {result.spec_version}: {counts.files} file(s), {counts.lines} line(s), "
        f"{counts.events} event(s) in {counts.sessions} session(s), "
        f"{counts.blank_lines} blank, {counts.rejected_lines} rejected, {len(result.flags)} flag(s)"
    ]
    lines += _grouped(
        "rejected lines",
        result.errors,
        counts.errors_by_code,
        code=lambda e: e.code,
        describe=lambda e: f"{e.file}:{e.line}: {e.message}",
    )
    lines += _grouped(
        "flags (warnings)",
        result.flags,
        counts.flags_by_code,
        code=lambda f: f.code,
        describe=lambda f: f"robot {f.robot_id} session {f.session_id} seq {f.seq}: {f.detail}",
    )
    return lines


def _grouped[Item](
    title: str,
    items: Sequence[Item],
    totals: Mapping[str, int],
    code: Callable[[Item], str],
    describe: Callable[[Item], str],
) -> list[str]:
    if not items:
        return []
    by_code: dict[str, list[Item]] = {}
    for item in items:
        by_code.setdefault(code(item), []).append(item)
    lines = [f"{title}:"]
    for name in sorted(by_code):
        shown = by_code[name][:_SHOWN_PER_CODE]
        total = totals[name]
        lines.append(f"  {name} ({total})")
        lines += [f"    {describe(item)}" for item in shown]
        if total > len(shown):
            lines.append(f"    ... and {total - len(shown)} more")
    return lines


keys_app = typer.Typer(
    help="Manage Ed25519 signing keys for sealing.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(keys_app, name="keys")


@keys_app.command("generate")
def keys_generate(
    name: Annotated[str, typer.Option("--name", help="Key name, e.g. autonometry/<robot_id>.")],
    out: Annotated[
        Path, typer.Option("--out", help="Path for the private key; its vkey is written beside it.")
    ],
) -> None:
    """Generate an Ed25519 signing key and its vkey (definitions.md section 10.3)."""
    try:
        vkey_path = keys.generate(name, out)
    except (SignedNoteError, keys.SigningKeyError) as exc:
        _fail(str(exc))
    typer.echo(f"private key: {out}")
    typer.echo(f"vkey file:   {vkey_path}")
    typer.echo(vkey_path.read_text(encoding="utf-8").strip())


def _key_name(key_path: Path) -> str:
    try:
        return keys.load_key_name(key_path)
    except keys.SigningKeyError as exc:
        _fail(str(exc))


def _sign_checkpoint(
    checkpoint: sealing.Checkpoint,
    session: Session,
    key_name: str,
    private_key: Ed25519PrivateKey,
) -> bytes:
    body = signednote.checkpoint_body(
        session.robot_id, session.session_id, checkpoint.size, checkpoint.root
    )
    signed = signednote.sign_note(body, key_name, private_key)
    return sealfile.encode_checkpoint_line(signed)


@app.command("seal")
def seal(
    log: Annotated[
        Path,
        typer.Argument(
            help="Input JSON Lines event log.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    key: Annotated[
        Path,
        typer.Option(
            "--key", help="Ed25519 private key (PKCS8 PEM); its name is read from <key>.vkey."
        ),
    ],
    out: Annotated[Path, typer.Option("--out", help="Output sealed log.")],
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Seal an event log: hash chain, Merkle checkpoints, Ed25519 signatures (definitions.md 10).

    Refuses input with rejected lines, input that is already sealed, and existing outputs.
    """
    version = _spec_version(spec)
    try:
        log_check = read_logs([log], version)
    except (SpecError, LogReadError) as exc:
        _fail(str(exc))
    if log_check.errors:
        _fail(f"{log} has {log_check.counts.rejected_lines} rejected line(s); refusing to seal it")
    if any(
        event.get("hash") is not None or event.get("prev_hash") is not None
        for session in log_check.sessions
        for event in session.events
    ):
        _fail(
            f"{log} already carries hash/prev_hash fields; refusing to seal an already-sealed log"
        )
    checkpoints_out = sealfile.checkpoints_path(out)
    if out.exists() or checkpoints_out.exists():
        _fail(f"refusing to overwrite an existing output: {out} or {checkpoints_out}")

    try:
        private_key = keys.load(key)
    except keys.SigningKeyError as exc:
        _fail(str(exc))
    key_name = _key_name(key)
    rules = load_rules(version)

    sealed_lines: list[bytes] = []
    checkpoint_lines: list[bytes] = []
    for session in log_check.sessions:
        sealer = sealing.Sealer(
            rules.chain_genesis_prefix,
            session.robot_id,
            session.session_id,
            rules.sealing.checkpoint_every_events,
            rules.sealing.checkpoint_every_s,
        )
        for event in session.events:
            sealed_event, checkpoint = sealer.add(event)
            sealed_lines.append(rfc8785.dumps(sealed_event) + b"\n")
            if checkpoint is not None:
                checkpoint_lines.append(
                    _sign_checkpoint(checkpoint, session, key_name, private_key)
                )
        final = sealer.finish()
        if final is not None:
            checkpoint_lines.append(_sign_checkpoint(final, session, key_name, private_key))

    atomic_write(out, b"".join(sealed_lines))
    atomic_write(checkpoints_out, b"".join(checkpoint_lines))
    total_events = sum(len(session.events) for session in log_check.sessions)
    typer.echo(f"sealed {total_events} event(s) in {len(log_check.sessions)} session(s)")
    typer.echo(f"wrote {out} and {checkpoints_out}")


@app.command("timestamp")
def timestamp(
    checkpoints_file: Annotated[
        Path,
        typer.Argument(
            help="A <name>.checkpoints.jsonl file.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    tsa_url: Annotated[
        str, typer.Option("--tsa-url", help="RFC 3161 TSA URL (http:// or https://).")
    ],
    tsa_root: Annotated[
        Path, typer.Option("--tsa-root", help="Trusted TSA root certificate(s) (PEM).")
    ],
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Append a verified RFC 3161 timestamp record for each checkpoint that lacks one."""
    version = _spec_version(spec)
    rules = load_rules(version)
    roots = _load_tsa_roots([tsa_root])
    (seal_file,) = _read_seal_files([checkpoints_file], version, rules)
    if rejected := sum(seal_file.errors_by_code.values()):
        _fail(f"{checkpoints_file} has {rejected} rejected line(s); refusing")

    records = seal_file.records
    have = {r.note_sha256 for r in records if isinstance(r, sealfile.TimestampLine)}
    checkpoints = [r for r in records if isinstance(r, sealfile.CheckpointLine)]
    appended = failures = 0
    for checkpoint in checkpoints:
        note_bytes = checkpoint.note.encode("utf-8")
        note_sha256 = hashlib.sha256(note_bytes).hexdigest()
        if note_sha256 in have:
            continue
        try:
            tsr = tsa.request_timestamp(note_bytes, tsa_url, roots)
        except tsa.TsaError as exc:
            logger.warning("could not timestamp a checkpoint: %s", exc)
            failures += 1
            continue
        append_durably(checkpoints_file, sealfile.encode_timestamp_line(note_sha256, tsr))
        have.add(note_sha256)
        appended += 1
    typer.echo(f"appended {appended} timestamp record(s); {failures} checkpoint(s) not timestamped")
    if failures:
        raise typer.Exit(1)


def _load_vkeys(paths: Sequence[Path]) -> list[VerifierKey]:
    out: list[VerifierKey] = []
    for path in paths:
        try:
            lines = keys.load_vkeys(path)
        except keys.SigningKeyError as exc:
            _fail(str(exc))
        for line in lines:
            try:
                out.append(signednote.parse_vkey(line))
            except SignedNoteError as exc:
                _fail(f"{path}: {exc}")
    return out


def _load_tsa_roots(paths: Sequence[Path]) -> list[x509.Certificate]:
    out: list[x509.Certificate] = []
    for path in paths:
        try:
            pem = path.read_bytes()
        except OSError as exc:
            _fail(f"cannot read {path}: {exc.strerror or exc}")
        try:
            out.extend(tsa.load_tsa_roots(pem))
        except tsa.TsaError as exc:
            _fail(f"{path}: {exc}")
    return out


def _read_seal_files(paths: Sequence[Path], version: str, rules: Rules) -> list[SealFile]:
    seal_files: list[SealFile] = []
    for path in paths:
        try:
            with open(path, "rb") as stream:
                seal_files.append(
                    sealfile.read_seal_file(
                        str(path),
                        stream,
                        rules.errors.max_line_bytes,
                        rules.errors.list_max_per_code,
                        version,
                    )
                )
        except OSError as exc:
            _fail(f"cannot read {path}: {exc.strerror or exc}")
    return seal_files


def verify_summary_lines(result: VerifyResult, require: TrustLevel) -> list[str]:
    """Return the human summary of a verification: one line per session with its evidence and
    gaps, then rejected lines and file problems grouped by code (the first 20 of each), and a
    final ``result:`` line."""
    lines = [f"spec {result.spec_version}: {len(result.sessions)} session(s)"]
    for verdict in result.sessions:
        through = (
            f" (sealed through seq {verdict.sealed_through_seq})"
            if verdict.sealed_through_seq is not None
            else ""
        )
        lines.append(f"  {verdict.robot_id}/{verdict.session_id}: {verdict.status}{through}")
        lines += [f"    evidence {i.code}: {i.detail}" for i in verdict.evidence]
        lines += [f"    gap {i.code}: {i.detail}" for i in verdict.gaps]
    lines += _grouped(
        "rejected lines",
        result.rejected_lines,
        result.rejected_by_code,
        code=lambda e: e.code,
        describe=lambda e: f"{e.file}:{e.line}: {e.message}",
    )
    lines += _grouped(
        "file problems",
        result.file_problems,
        result.file_problems_by_code,
        code=lambda p: p.code,
        describe=lambda p: f"{p.file}:{p.line}: {p.detail}",
    )
    reasons = result.reasons(require)
    lines.append(
        "result: verified" if not reasons else f"result: NOT verified: {'; '.join(reasons)}"
    )
    return lines


@app.command("verify")
def verify_cmd(  # noqa: PLR0913, PLR0917 -- matches the verify CLI's documented options
    logs: Annotated[
        list[Path],
        typer.Argument(
            help="JSON Lines event log files.",
            exists=True,
            dir_okay=False,
            readable=True,
            show_default=False,
        ),
    ],
    vkey: Annotated[
        list[Path] | None,
        typer.Option("--vkey", help="File of trusted vkeys, one per line; repeatable."),
    ] = None,
    tsa_root: Annotated[
        list[Path] | None,
        typer.Option("--tsa-root", help="Trusted TSA root certificate (PEM); repeatable."),
    ] = None,
    anchor: Annotated[
        list[Path] | None,
        typer.Option(
            "--anchor", help="Anchor file, same format as a checkpoints file; repeatable."
        ),
    ] = None,
    require: Annotated[
        str,
        typer.Option("--require", help="Minimum trust level every session must meet: L1 or L2."),
    ] = "L2",
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON instead of a summary.")
    ] = False,
    spec: Annotated[
        str | None, typer.Option("--spec", help="Spec version, for example 0.3.")
    ] = None,
) -> None:
    """Verify sealed logs (definitions.md section 10.6): recompute, never trust a stored value.

    Exit status: 0 only when every session meets ``--require`` (L1: not failed; L2: verified),
    no line of any file read (logs, checkpoint and anchor files) was rejected and no file has a
    problem; 1 otherwise; 2 on bad usage. Event contents are never printed.
    """
    if require not in ("L1", "L2"):
        raise typer.BadParameter("must be 'L1' or 'L2'", param_hint="'--require'")
    version = _spec_version(spec)
    rules = load_rules(version)
    try:
        log_check = read_logs(logs, version)
    except (SpecError, LogReadError) as exc:
        _fail(str(exc))

    trusted_keys = _load_vkeys(vkey or [])
    trusted_roots = _load_tsa_roots(tsa_root or [])
    checkpoint_paths = [
        p for log_path in logs if (p := sealfile.checkpoints_path(log_path)).exists()
    ]
    checkpoint_files = _read_seal_files(checkpoint_paths, version, rules)
    anchor_files = _read_seal_files(anchor or [], version, rules)
    level = cast(TrustLevel, require)
    try:
        result = verify_.verify_log(
            log_check, checkpoint_files, anchor_files, trusted_keys, trusted_roots, rules
        )
    except SpecError as exc:
        _fail(str(exc))

    if as_json:
        typer.echo(json.dumps(result.to_json(level), indent=2, sort_keys=True))
    else:
        for line in verify_summary_lines(result, level):
            typer.echo(line)
    if not result.verified(level):
        raise typer.Exit(1)


@app.command("record")
def record(
    config: Annotated[
        Path,
        typer.Option(
            "--config", help="Recorder config (TOML).", dir_okay=False, show_default=False
        ),
    ],
) -> None:
    """Record a robot's control events into a sealed, crash-safe log (Linux and macOS).

    Receives events from connectors on a local Unix datagram socket until SIGTERM or SIGINT.
    Exit status: 0 after a clean stop, 1 on a failure while recording (restarting continues the
    same session), 2 when it cannot start (bad config, key or registry; socket or folder in use).
    Operational messages go to stderr and never contain event contents.
    """
    if sys.platform == "win32":
        typer.echo("error: the recorder runs on Linux and macOS only", err=True)
        raise typer.Exit(2)
    from autonometry.recorder.config import ConfigError, load_config, prepare  # noqa: PLC0415
    from autonometry.recorder.service import serve  # noqa: PLC0415 -- POSIX-only modules

    try:
        prepared = prepare(load_config(config))
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    recorder_logger = logging.getLogger("autonometry.recorder")
    if recorder_logger.getEffectiveLevel() > logging.INFO:
        recorder_logger.setLevel(logging.INFO)
    raise typer.Exit(serve(prepared))


def main() -> None:
    """Console entry point: configure logging, run the CLI, turn any crash into one line, exit 1."""
    _configure_logging()
    try:
        app()
    except Exception as exc:  # process boundary: never show a traceback unless --verbose
        logger.debug("unexpected failure", exc_info=True)
        typer.echo(f"error: unexpected failure ({type(exc).__name__})", err=True)
        sys.exit(1)
