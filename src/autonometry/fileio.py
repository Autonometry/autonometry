"""Durable file writes (CLAUDE.md Security rules): one atomic-write helper and an append helper.

Files this package creates are private (0600) unless a caller asks otherwise, for example 0644 for
a public vkey. On Windows, POSIX modes and directory fsync do not apply; the writes are still
atomic.
"""

import os
import sys
from pathlib import Path

PRIVATE_FILE_MODE = 0o600
PUBLIC_FILE_MODE = 0o644
PRIVATE_DIR_MODE = 0o700
_POSIX = sys.platform != "win32"
# Without O_BINARY, os.open on Windows translates "\n" into "\r\n".
_BINARY = getattr(os, "O_BINARY", 0)


def fsync_directory(path: Path) -> None:
    """Make a rename or file creation in ``path`` durable (POSIX; a no-op on Windows)."""
    if not _POSIX:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make_private_dirs(folder: Path) -> None:
    """Create ``folder`` and any missing parents, each 0700, fsyncing each new entry into its
    parent; existing folders are left as they are (changing, say, /tmp to 0700 would lock other
    users out)."""
    missing: list[Path] = []
    current = folder
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        os.mkdir(path, PRIVATE_DIR_MODE)
        if _POSIX:
            os.chmod(path, PRIVATE_DIR_MODE)  # mkdir's mode is subject to umask.
        fsync_directory(path.parent)


def atomic_write(path: Path, data: bytes, mode: int = PRIVATE_FILE_MODE) -> None:
    """Write ``data`` to ``path`` so that a reader sees the old file or the whole new one, never
    part of it: a temporary file in the same folder created with ``mode``, written, fsynced,
    renamed over ``path``, then the folder fsynced. Nothing is left behind on failure."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if _POSIX:
            os.chmod(tmp, mode)  # os.open's mode is subject to umask; set the exact bits.
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


def append_durably(path: Path, data: bytes) -> None:
    """Append ``data`` to ``path`` with ``O_APPEND`` and fsync it; a new file is created 0600."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _BINARY, PRIVATE_FILE_MODE)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
