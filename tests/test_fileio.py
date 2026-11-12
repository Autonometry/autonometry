"""Tests for autonometry.fileio (CLAUDE.md Security rules: atomic writes, private files)."""

import os
import stat
import sys
from pathlib import Path

import pytest

from autonometry import fileio

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")


def test_atomic_write_writes_exact_bytes(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    fileio.atomic_write(path, b"a\nb\n")
    assert path.read_bytes() == b"a\nb\n"  # no newline translation on Windows
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_write_replaces_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    path.write_bytes(b"old contents that are longer")
    fileio.atomic_write(path, b"new")
    assert path.read_bytes() == b"new"


def test_atomic_write_leaves_nothing_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        fileio.atomic_write(tmp_path / "out.jsonl", b"data")
    assert list(tmp_path.iterdir()) == []


@posix_only
def test_atomic_write_modes(tmp_path: Path) -> None:
    private, public = tmp_path / "private", tmp_path / "public"
    fileio.atomic_write(private, b"x")
    fileio.atomic_write(public, b"x", fileio.PUBLIC_FILE_MODE)
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(public.stat().st_mode) == 0o644


def test_append_durably_appends_exact_bytes(tmp_path: Path) -> None:
    path = tmp_path / "log.checkpoints.jsonl"
    fileio.append_durably(path, b"one\n")
    fileio.append_durably(path, b"two\n")
    assert path.read_bytes() == b"one\ntwo\n"  # no newline translation on Windows


@posix_only
def test_append_durably_creates_a_private_file(tmp_path: Path) -> None:
    path = tmp_path / "new.jsonl"
    fileio.append_durably(path, b"x\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_fsync_directory_does_not_raise(tmp_path: Path) -> None:
    fileio.fsync_directory(tmp_path)
