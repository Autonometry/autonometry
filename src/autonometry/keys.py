"""Ed25519 signing keys for sealing checkpoints (definitions.md v0.2 section 10.3).

Keys are stored as unencrypted PKCS8 PEM. The private key file and its directory are made as
restrictive as the platform allows (CLAUDE.md Security rules: 0600 / 0700, refuse a key others can
read). On Windows, where POSIX permission bits do not apply, the checks are skipped and one
warning is logged instead.
"""

import logging
import os
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonometry.signednote import format_vkey

logger = logging.getLogger(__name__)

_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700
_WINDOWS = sys.platform == "win32"


class SigningKeyError(Exception):
    """A signing key file is missing, unreadable, insecurely stored or already exists."""


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` without ever leaving a partial file: a temporary file in the
    same directory, created with ``mode`` and never overwritten, then renamed into place."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if not _WINDOWS:
            os.chmod(tmp, mode)  # os.open's mode is subject to umask; force the exact bits.
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def generate(key_name: str, out_path: Path) -> Path:
    """Generate one Ed25519 signing key at ``out_path`` and its vkey at ``out_path`` + ``.vkey``.

    Never overwrites an existing key or vkey file.

    Returns:
        The vkey file's path.

    Raises:
        SignedNoteError: ``key_name`` is not a safe key name.
        SigningKeyError: ``out_path`` or its vkey already exists.
    """
    vkey_path = out_path.with_name(f"{out_path.name}.vkey")
    if out_path.exists() or vkey_path.exists():
        raise SigningKeyError(f"refusing to overwrite an existing key at {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not _WINDOWS:
        os.chmod(out_path.parent, _PRIVATE_DIR_MODE)
    else:
        logger.warning("key directory permissions are not checked on Windows: %s", out_path.parent)
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    vkey = format_vkey(key_name, private_key.public_key().public_bytes_raw())
    _atomic_write(out_path, pem, _PRIVATE_FILE_MODE)
    _atomic_write(vkey_path, (vkey + "\n").encode("ascii"), stat.S_IRUSR | stat.S_IWUSR)
    return vkey_path


def load(path: Path) -> Ed25519PrivateKey:
    """Load an Ed25519 PKCS8 PEM private key, refusing one that is not kept private.

    Raises:
        SigningKeyError: the file cannot be read or is not a usable Ed25519 private key, or
            (POSIX only) it is accessible to its group or others, or owned by another user.
    """
    try:
        pem = path.read_bytes()
    except OSError as exc:
        raise SigningKeyError(f"cannot read key {path}: {exc.strerror or exc}") from None
    if not _WINDOWS:
        info = path.stat()
        if info.st_uid != os.getuid():
            raise SigningKeyError(f"refusing to load {path}: it is owned by another user")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SigningKeyError(f"refusing to load {path}: it is accessible by group or others")
    else:
        logger.warning("key file permissions are not checked on Windows: %s", path)
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except ValueError as exc:
        raise SigningKeyError(f"{path} is not a usable private key: {exc}") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningKeyError(f"{path} is not an Ed25519 private key")
    return key


def load_vkeys(path: Path) -> list[str]:
    """Read a vkey file: one vkey per line, blank lines and ``#`` comments ignored.

    Raises:
        SigningKeyError: the file cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SigningKeyError(f"cannot read vkey file {path}: {exc.strerror or exc}") from None
    except UnicodeDecodeError as exc:
        raise SigningKeyError(f"vkey file {path} is not valid UTF-8: {exc}") from None
    return [
        stripped
        for line in text.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]
