"""C2SP signed notes and checkpoints (definitions.md v0.2 section 10.3;
https://c2sp.org/signed-note, https://c2sp.org/tlog-checkpoint).

Pure functions: building, signing and verifying a checkpoint's text never touches a file or the
network. Only the Ed25519 log-signature variant (signature type ``0x01``) is implemented; the
timestamped cosignature types are out of scope for this package.
"""

import base64
import binascii
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ED25519_SIGNATURE_TYPE = 0x01
_ED25519_PUBLIC_KEY_LEN = 32
_ED25519_VKEY_BODY_LEN = 1 + _ED25519_PUBLIC_KEY_LEN  # signature type byte + public key
_SHA256_LEN = 32
_KEY_ID_LEN = 4
_VKEY_PARTS = 3
_CHECKPOINT_BODY_LINE_COUNT = 3
_MAX_SIGNATURES = 16
_EM_DASH_SPACE = "— "
_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
_KEY_ID_RE = re.compile(r"^[0-9a-f]{8}$")
# ASCII control characters other than the newline (U+000A) that terminates each line.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
# Identifiers as definitions.md section 8 defines them, so an origin is safe to print.
_ID = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_ORIGIN_RE = re.compile(rf"^autonometry/(?P<robot_id>{_ID})/(?P<session_id>{_ID})$", re.ASCII)


class SignedNoteError(ValueError):
    """A note, vkey or checkpoint body is not well formed. The message is one line."""


class BadSignatureError(SignedNoteError):
    """A signature from a trusted key's name and key ID does not verify (fail closed;
    definitions.md section 10.6 evidence code ``bad_signature``)."""


def is_valid_key_name(name: str) -> bool:
    """True when ``name`` is 1 to 256 ASCII letters, digits, ``.``, ``_``, ``:``, ``/`` or ``-``
    (definitions.md section 10.3)."""
    return bool(_KEY_NAME_RE.fullmatch(name))


def key_id(key_name: str, public_key: bytes, signature_type: int = ED25519_SIGNATURE_TYPE) -> bytes:
    """4-byte key ID: the first 4 bytes of SHA-256(key name || 0x0A || type || public key)
    (c2sp.org/signed-note, 'Signatures')."""
    digest = hashlib.sha256(
        key_name.encode("utf-8") + b"\n" + bytes([signature_type]) + public_key
    ).digest()
    return digest[:4]


def format_vkey(key_name: str, public_key: bytes) -> str:
    """Return the verifier key text for one Ed25519 public key (c2sp.org/signed-note).

    Raises:
        SignedNoteError: ``key_name`` is not a safe key name.
    """
    if not is_valid_key_name(key_name):
        raise SignedNoteError(f"key name {key_name!r} is not 1 to 256 safe characters")
    body = base64.b64encode(bytes([ED25519_SIGNATURE_TYPE]) + public_key).decode("ascii")
    return f"{key_name}+{key_id(key_name, public_key).hex()}+{body}"


@dataclass(frozen=True, slots=True)
class VerifierKey:
    """One parsed Ed25519 vkey: name, key ID and public key (c2sp.org/signed-note)."""

    name: str
    key_id: bytes
    public_key: Ed25519PublicKey


def _canonical_b64decode(text: str, what: str) -> bytes:
    """Decode standard base64 with padding, rejecting anything that is not the canonical
    re-encoding of the bytes it decodes to (definitions.md 10; 'canonical base64 only')."""
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SignedNoteError(f"{what} is not valid base64: {exc}") from None
    if base64.b64encode(raw).decode("ascii") != text:
        raise SignedNoteError(f"{what} is not canonical base64")
    return raw


def parse_vkey(text: str) -> VerifierKey:
    """Parse one Ed25519 verifier key line (c2sp.org/signed-note, 'Verifier keys').

    Raises:
        SignedNoteError: the text is not a well-formed Ed25519 (type 0x01) vkey.
    """
    parts = text.split("+", 2)
    if len(parts) != _VKEY_PARTS:
        raise SignedNoteError("vkey must have the form <name>+<key id>+<base64>")
    name, id_hex, body_b64 = parts
    if not is_valid_key_name(name):
        raise SignedNoteError(f"vkey name {name!r} is not 1 to 256 safe characters")
    if not _KEY_ID_RE.fullmatch(id_hex):
        raise SignedNoteError("vkey key id is not 8 lower-case hex digits")
    body = _canonical_b64decode(body_b64, "vkey public key material")
    if len(body) != _ED25519_VKEY_BODY_LEN or body[0] != ED25519_SIGNATURE_TYPE:
        raise SignedNoteError("vkey is not an Ed25519 (type 0x01) key of 32 bytes")
    public_key = Ed25519PublicKey.from_public_bytes(body[1:])  # 32 bytes: checked just above
    return VerifierKey(name, bytes.fromhex(id_hex), public_key)


def _no_control_chars(text: str, what: str) -> None:
    if _CONTROL_CHAR_RE.search(text):
        raise SignedNoteError(f"{what} contains a control character other than newline")


@dataclass(frozen=True, slots=True)
class SignatureLine:
    """One parsed signature line of a signed note (c2sp.org/signed-note)."""

    key_name: str
    key_id: bytes
    signature: bytes


def sign_note(note_text: str, key_name: str, private_key: Ed25519PrivateKey) -> str:
    """Return ``note_text`` with one Ed25519 log signature line appended (c2sp.org/signed-note,
    'Ed25519 signatures'). ``note_text`` MUST already end in a newline.

    Raises:
        SignedNoteError: ``note_text`` does not end in a newline, holds a disallowed control
            character, or ``key_name`` is not a safe key name.
    """
    if not note_text.endswith("\n"):
        raise SignedNoteError("note text must end with a newline")
    _no_control_chars(note_text, "note text")
    if not is_valid_key_name(key_name):
        raise SignedNoteError(f"key name {key_name!r} is not 1 to 256 safe characters")
    public_key = private_key.public_key().public_bytes_raw()
    signature = private_key.sign(note_text.encode("utf-8"))
    body = base64.b64encode(key_id(key_name, public_key) + signature).decode("ascii")
    return f"{note_text}\n{_EM_DASH_SPACE}{key_name} {body}\n"


@dataclass(frozen=True, slots=True)
class SignedNote:
    """A signed note split into the text its signatures cover and its signature lines."""

    text: str
    signatures: tuple[SignatureLine, ...]


def parse_note(text: str) -> SignedNote:
    """Parse a signed note (c2sp.org/signed-note, 'Format'). The signed text is everything before
    the last blank line, including its final newline; every line after it is a signature line.

    Raises:
        SignedNoteError: no final newline, a control character other than newline, no blank line
            or empty signed text, no or more than 16 signature lines, or a malformed signature
            line (including non-canonical base64).
    """
    if not text.endswith("\n"):
        raise SignedNoteError("note does not end with a newline")
    _no_control_chars(text, "note")
    lines = text.split("\n")[:-1]
    blanks = [i for i, line in enumerate(lines) if line == ""]
    if not blanks:
        raise SignedNoteError("no blank line between the note text and its signatures")
    sep = blanks[-1]
    if sep == 0:
        raise SignedNoteError("the signed note text is empty")
    sig_lines = lines[sep + 1 :]
    if not sig_lines:
        raise SignedNoteError("no signature lines")
    if len(sig_lines) > _MAX_SIGNATURES:
        raise SignedNoteError(f"more than {_MAX_SIGNATURES} signature lines")
    signatures = tuple(_parse_signature_line(line) for line in sig_lines)
    return SignedNote("\n".join(lines[:sep]) + "\n", signatures)


def _parse_signature_line(line: str) -> SignatureLine:
    if not line.startswith(_EM_DASH_SPACE):
        raise SignedNoteError("signature line does not start with '— '")
    name, _, body_b64 = line.removeprefix(_EM_DASH_SPACE).partition(" ")
    if not is_valid_key_name(name) or not body_b64:
        raise SignedNoteError("malformed signature line")
    body = _canonical_b64decode(body_b64, "signature")
    if len(body) < _KEY_ID_LEN:
        raise SignedNoteError("signature is shorter than its 4-byte key id")
    return SignatureLine(name, body[:_KEY_ID_LEN], body[_KEY_ID_LEN:])


@dataclass(frozen=True, slots=True)
class VerifiedNote:
    """A signed note whose text verified against at least one trusted key."""

    text: str
    verified_keys: tuple[str, ...]


def verify_note(text: str, trusted_keys: Sequence[VerifierKey]) -> VerifiedNote:
    """Verify a signed note against trusted Ed25519 keys (c2sp.org/signed-note).

    Unknown keys (no name and ID match) are ignored: ``verified_keys`` is empty when no signature
    from a trusted key is present at all, which callers treat as a gap (``untrusted_checkpoint``),
    not a failure. A signature from a known key that fails to verify rejects the whole note (fail
    closed, definitions.md 10.6 evidence code ``bad_signature``).

    Raises:
        SignedNoteError: the note is malformed.
        BadSignatureError: a signature from a trusted key's name and key ID does not verify.
    """
    note = parse_note(text)
    return VerifiedNote(note.text, trusted_signers(note, trusted_keys))


def trusted_signers(note: SignedNote, trusted_keys: Sequence[VerifierKey]) -> tuple[str, ...]:
    """Return the names of the trusted keys whose signatures on ``note`` verify; empty when no
    signature line has a trusted key's name and key ID (c2sp.org/signed-note, 'Signatures').

    Raises:
        BadSignatureError: a signature from a trusted key's name and key ID does not verify.
    """
    by_name_id = {(key.name, key.key_id): key for key in trusted_keys}
    message = note.text.encode("utf-8")
    verified: list[str] = []
    for sig in note.signatures:
        key = by_name_id.get((sig.key_name, sig.key_id))
        if key is None:
            continue
        try:
            key.public_key.verify(sig.signature, message)
        except InvalidSignature:
            raise BadSignatureError(
                f"signature from trusted key {sig.key_name!r} does not verify"
            ) from None
        verified.append(sig.key_name)
    return tuple(verified)


def checkpoint_origin(robot_id: str, session_id: str) -> str:
    """Return a checkpoint's origin line: ``autonometry/<robot_id>/<session_id>``."""
    return f"autonometry/{robot_id}/{session_id}"


def checkpoint_body(robot_id: str, session_id: str, size: int, root: bytes) -> str:
    """Return the 3-line checkpoint note text, with no extension lines (definitions.md 10.3;
    c2sp.org/tlog-checkpoint).

    Raises:
        SignedNoteError: ``size`` is negative or ``root`` is not 32 bytes.
    """
    if size < 0:
        raise SignedNoteError("checkpoint size must not be negative")
    if len(root) != _SHA256_LEN:
        raise SignedNoteError("checkpoint root hash must be 32 bytes")
    root_b64 = base64.b64encode(root).decode("ascii")
    return f"{checkpoint_origin(robot_id, session_id)}\n{size}\n{root_b64}\n"


@dataclass(frozen=True, slots=True)
class CheckpointBody:
    """A parsed checkpoint note body: origin, tree size and Merkle root (definitions.md 10.3)."""

    robot_id: str
    session_id: str
    size: int
    root: bytes


def parse_checkpoint_body(note_text: str) -> CheckpointBody:
    """Parse a checkpoint's 3-line note text (definitions.md 10.3; c2sp.org/tlog-checkpoint).

    Raises:
        SignedNoteError: the text is not exactly 3 non-empty lines (no extension lines), the size
            is not an ASCII decimal integer with no leading zero, the origin is not
            ``autonometry/<robot_id>/<session_id>``, or the root hash is not canonical base64 of
            32 bytes.
    """
    lines = note_text.split("\n")
    body_lines = _CHECKPOINT_BODY_LINE_COUNT
    if len(lines) != body_lines + 1 or lines[-1] != "" or not all(lines[:body_lines]):
        raise SignedNoteError("checkpoint note text must be exactly 3 non-empty lines")
    origin, size_text, root_b64 = lines[0], lines[1], lines[2]
    match = _ORIGIN_RE.fullmatch(origin)
    if match is None:
        raise SignedNoteError("checkpoint origin is not autonometry/<robot_id>/<session_id>")
    if not size_text.isascii() or not size_text.isdigit() or size_text != str(int(size_text)):
        raise SignedNoteError("checkpoint size is not an ASCII decimal integer")
    root = _canonical_b64decode(root_b64, "checkpoint root hash")
    if len(root) != _SHA256_LEN:
        raise SignedNoteError("checkpoint root hash is not 32 bytes")
    return CheckpointBody(match.group("robot_id"), match.group("session_id"), int(size_text), root)


def parse_signed_checkpoint(text: str) -> tuple[CheckpointBody, SignedNote]:
    """Parse a signed checkpoint: the body is read from exactly the text the signatures cover,
    which must be the three checkpoint lines and nothing else (definitions.md 10.3).

    Raises:
        SignedNoteError: the note or its checkpoint body is not well formed.
    """
    note = parse_note(text)
    return parse_checkpoint_body(note.text), note
