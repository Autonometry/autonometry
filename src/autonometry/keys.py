"""Ed25519 signing keys for sealing checkpoints (definitions.md v0.3 section 10.3).

Keys are stored as unencrypted PKCS8 PEM, the private key 0600 and its vkey 0644. Folders that
``generate`` creates are 0700; an existing folder is never changed (CLAUDE.md Security rules).
Loading refuses a key that others can access. On Windows, where POSIX permission bits do not
apply, the checks are skipped and one warning is logged instead.
"""

import logging
import os
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonometry.fileio import PRIVATE_FILE_MODE, PUBLIC_FILE_MODE, atomic_write, fsync_directory
from autonometry.signednote import format_vkey

logger = logging.getLogger(__name__)

_PRIVATE_DIR_MODE = 0o700
_WINDOWS = sys.platform == "win32"


class SigningKeyError(Exception):
    """A signing key file is missing, unreadable, insecurely stored or already exists."""


def _make_missing_folders(folder: Path) -> None:
    """Create ``folder`` and any missing parents, each 0700; existing folders are left as they
    are (changing, say, /tmp to 0700 would lock other users out)."""
    missing: list[Path] = []
    current = folder
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        os.mkdir(path, _PRIVATE_DIR_MODE)
        if not _WINDOWS:
            os.chmod(path, _PRIVATE_DIR_MODE)  # mkdir's mode is subject to umask.
            fsync_directory(path.parent)


def generate(key_name: str, out_path: Path) -> Path:
    """Generate one Ed25519 signing key at ``out_path`` and its vkey at ``out_path`` + ``.vkey``.

    Never overwrites an existing key or vkey file and never leaves a partial one.

    Returns:
        The vkey file's path.

    Raises:
        SignedNoteError: ``key_name`` is not a safe key name.
        SigningKeyError: ``out_path`` or its vkey already exists.
    """
    vkey_path = out_path.with_name(f"{out_path.name}.vkey")
    if out_path.exists() or vkey_path.exists():
        raise SigningKeyError(f"refusing to overwrite an existing key at {out_path}")
    if _WINDOWS:
        logger.warning("key file permissions are not checked on Windows: %s", out_path)
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    vkey = format_vkey(key_name, private_key.public_key().public_bytes_raw())
    _make_missing_folders(out_path.parent)
    atomic_write(out_path, pem, PRIVATE_FILE_MODE)
    atomic_write(vkey_path, (vkey + "\n").encode("ascii"), PUBLIC_FILE_MODE)
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
