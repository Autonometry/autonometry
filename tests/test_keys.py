"""Tests for autonometry.keys (definitions.md v0.3 section 10.3; CLAUDE.md security rules)."""

import os
import stat
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonometry import keys
from autonometry.signednote import SignedNoteError, parse_vkey

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission checks only")


def test_generate_then_load_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "robot.key"
    vkey_path = keys.generate("autonometry/r1", out)
    assert vkey_path == out.with_name("robot.key.vkey")
    private_key = keys.load(out)
    assert isinstance(private_key, Ed25519PrivateKey)
    vkey_text = vkey_path.read_text(encoding="utf-8").strip()
    vkey = parse_vkey(vkey_text)
    assert vkey.name == "autonometry/r1"
    assert vkey.public_key.public_bytes_raw() == private_key.public_key().public_bytes_raw()


@posix_only
def test_generate_sets_key_0600_vkey_0644_and_created_folders_0700(tmp_path: Path) -> None:
    out = tmp_path / "a" / "b" / "robot.key"
    keys.generate("autonometry/r1", out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.with_name("robot.key.vkey").stat().st_mode) == 0o644
    assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "a" / "b").stat().st_mode) == 0o700


@posix_only
def test_generate_never_changes_an_existing_folder(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    keys.generate("autonometry/r1", shared / "robot.key")
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


def test_generate_creates_missing_folders(tmp_path: Path) -> None:
    out = tmp_path / "x" / "y" / "robot.key"
    keys.generate("autonometry/r1", out)
    assert out.exists()


def test_generate_never_overwrites(tmp_path: Path) -> None:
    out = tmp_path / "robot.key"
    keys.generate("autonometry/r1", out)
    with pytest.raises(keys.SigningKeyError, match="overwrite"):
        keys.generate("autonometry/r1", out)


def test_generate_rejects_bad_key_name(tmp_path: Path) -> None:
    with pytest.raises(SignedNoteError):
        keys.generate("has space", tmp_path / "robot.key")
    assert not (tmp_path / "robot.key").exists()


def test_generate_leaves_no_partial_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "robot.key"
    real_replace = os.replace

    def failing_replace(src: object, dst: object) -> None:
        if str(dst) == str(out):
            raise OSError("disk full (simulated)")
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        keys.generate("autonometry/r1", out)
    assert not out.exists()
    assert list(tmp_path.glob("*.tmp-*")) == []


@posix_only
def test_load_refuses_group_readable_key(tmp_path: Path) -> None:
    out = tmp_path / "robot.key"
    keys.generate("autonometry/r1", out)
    out.chmod(0o640)
    with pytest.raises(keys.SigningKeyError, match="group or others"):
        keys.load(out)


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(keys.SigningKeyError, match="cannot read"):
        keys.load(tmp_path / "missing.key")


def test_load_rejects_non_ed25519_key(tmp_path: Path) -> None:
    out = tmp_path / "robot.key"
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    out.write_bytes(pem)
    out.chmod(0o600)
    with pytest.raises(keys.SigningKeyError, match="Ed25519"):
        keys.load(out)


def test_load_rejects_garbage_file(tmp_path: Path) -> None:
    out = tmp_path / "robot.key"
    out.write_bytes(b"not a pem file")
    out.chmod(0o600)
    with pytest.raises(keys.SigningKeyError, match="usable private key"):
        keys.load(out)


def test_load_vkeys_skips_blanks_and_comments(tmp_path: Path) -> None:
    out = tmp_path / "robot.key"
    vkey_path = keys.generate("autonometry/r1", out)
    original = vkey_path.read_text(encoding="utf-8").strip()
    vkey_path.write_text(f"\n# a comment\n{original}\n\n  \n", encoding="utf-8")
    assert keys.load_vkeys(vkey_path) == [original]


def test_load_vkeys_missing_file(tmp_path: Path) -> None:
    with pytest.raises(keys.SigningKeyError, match="cannot read"):
        keys.load_vkeys(tmp_path / "missing.vkey")
