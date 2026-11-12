"""Tests for autonometry.signednote (definitions.md v0.2 section 10.3;
https://c2sp.org/signed-note, https://c2sp.org/tlog-checkpoint)."""

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonometry.signednote import (
    BadSignatureError,
    CheckpointBody,
    SignedNoteError,
    checkpoint_body,
    checkpoint_origin,
    format_vkey,
    is_valid_key_name,
    key_id,
    parse_checkpoint_body,
    parse_note,
    parse_signed_checkpoint,
    parse_vkey,
    sign_note,
    verify_note,
)

# The published example from c2sp.org/signed-note, "Verifier keys".
_EXAMPLE_VKEY = "example.com/foo+530d903a+AekyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k"
_EXAMPLE_SIGNATURE = (
    "Uw2QOkn8srV1yJGh2VYRlL1Tnagv1YEq6TfXppzi2ONncAlTgK7Ztg1ERYNZXsYjOBH3mFXmRKuwHjG1Yu72IneyaQM="
)
_EXAMPLE_NOTE = f"This is an example message.\n\n— example.com/foo {_EXAMPLE_SIGNATURE}\n"


def test_example_vkey_and_note_verify__c2sp_signed_note() -> None:
    vkey = parse_vkey(_EXAMPLE_VKEY)
    assert vkey.name == "example.com/foo"
    assert vkey.key_id.hex() == "530d903a"
    verified = verify_note(_EXAMPLE_NOTE, [vkey])
    assert verified.verified_keys == ("example.com/foo",)
    assert verified.text == "This is an example message.\n"


def test_key_id_matches_the_published_example() -> None:
    vkey = parse_vkey(_EXAMPLE_VKEY)
    computed = key_id(vkey.name, vkey.public_key.public_bytes_raw())
    assert computed == vkey.key_id


def test_sign_and_verify_round_trip() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw()
    vkey_text = format_vkey("robot/r1", public_key)
    vkey = parse_vkey(vkey_text)
    note = sign_note("hello\nworld\n", "robot/r1", private_key)
    verified = verify_note(note, [vkey])
    assert verified.verified_keys == ("robot/r1",)
    assert verified.text == "hello\nworld\n"


def test_unknown_key_is_ignored_not_rejected() -> None:
    private_key = Ed25519PrivateKey.generate()
    other_key = Ed25519PrivateKey.generate()
    vkey = parse_vkey(format_vkey("robot/r1", other_key.public_key().public_bytes_raw()))
    note = sign_note("hello\n", "robot/r1", private_key)
    verified = verify_note(note, [vkey])
    assert verified.verified_keys == ()


def test_tampered_signature_from_trusted_key_raises_bad_signature() -> None:
    private_key = Ed25519PrivateKey.generate()
    vkey = parse_vkey(format_vkey("robot/r1", private_key.public_key().public_bytes_raw()))
    note = sign_note("hello\n", "robot/r1", private_key)
    tampered = note.replace("hello", "jacked")
    with pytest.raises(BadSignatureError):
        verify_note(tampered, [vkey])


def test_at_most_16_signature_lines() -> None:
    private_key = Ed25519PrivateKey.generate()
    vkey = parse_vkey(format_vkey("robot/r1", private_key.public_key().public_bytes_raw()))
    note = "hello\n"
    text = note + "\n"
    for i in range(17):
        line = sign_note(note, f"other/{i}", private_key).splitlines()[-1]
        text += line + "\n"
    with pytest.raises(SignedNoteError, match="more than 16 signature lines"):
        verify_note(text, [vkey])


def test_canonical_base64_required_in_signature() -> None:
    private_key = Ed25519PrivateKey.generate()
    vkey = parse_vkey(format_vkey("robot/r1", private_key.public_key().public_bytes_raw()))
    note = sign_note("hello\n", "robot/r1", private_key)
    # Flip the final padding to a non-canonical (but decodable) base64 tail.
    bad = note[:-2] + ("A" if note[-2] != "A" else "B") + note[-1]
    with pytest.raises(SignedNoteError):
        verify_note(bad, [vkey])


def test_control_character_in_note_text_is_rejected() -> None:
    private_key = Ed25519PrivateKey.generate()
    with pytest.raises(SignedNoteError, match="control character"):
        sign_note("hello\x07\n", "robot/r1", private_key)


@pytest.mark.parametrize(
    "name",
    ["", "a" * 257, "has space", "has+plus", "ok/robot-1.name_2:3"],
)
def test_key_name_validity(name: str) -> None:
    expected = name == "ok/robot-1.name_2:3"
    assert is_valid_key_name(name) == expected


def test_vkey_must_have_three_parts() -> None:
    with pytest.raises(SignedNoteError, match="must have the form"):
        parse_vkey("name+onlytwo")


def test_vkey_rejects_wrong_key_length() -> None:
    too_short = base64.b64encode(bytes([1]) + b"\x00" * 10).decode("ascii")
    with pytest.raises(SignedNoteError, match="Ed25519"):
        parse_vkey(f"robot/r1+deadbeef+{too_short}")


def test_checkpoint_body_round_trip() -> None:
    root = bytes(range(32))
    text = checkpoint_body("r1", "s1", 42, root)
    assert text == f"{checkpoint_origin('r1', 's1')}\n42\n{base64.b64encode(root).decode()}\n"
    parsed = parse_checkpoint_body(text)
    assert parsed == CheckpointBody("r1", "s1", 42, root)


def test_checkpoint_body_rejects_leading_zero_size() -> None:
    root_b64 = base64.b64encode(bytes(32)).decode()
    with pytest.raises(SignedNoteError, match="ASCII decimal integer"):
        parse_checkpoint_body(f"autonometry/r1/s1\n007\n{root_b64}\n")


def test_checkpoint_body_rejects_extension_lines() -> None:
    root_b64 = base64.b64encode(bytes(32)).decode()
    with pytest.raises(SignedNoteError, match="exactly 3 non-empty lines"):
        parse_checkpoint_body(f"autonometry/r1/s1\n1\n{root_b64}\nextra\n")


def test_checkpoint_body_rejects_bad_origin() -> None:
    root_b64 = base64.b64encode(bytes(32)).decode()
    with pytest.raises(SignedNoteError, match="autonometry/<robot_id>/<session_id>"):
        parse_checkpoint_body(f"not-autonometry/r1/s1\n1\n{root_b64}\n")


def test_checkpoint_body_rejects_bad_root_length() -> None:
    short_b64 = base64.b64encode(bytes(10)).decode()
    with pytest.raises(SignedNoteError, match="32 bytes"):
        parse_checkpoint_body(f"autonometry/r1/s1\n1\n{short_b64}\n")


def test_sign_note_requires_trailing_newline() -> None:
    private_key = Ed25519PrivateKey.generate()
    with pytest.raises(SignedNoteError, match="newline"):
        sign_note("no newline", "robot/r1", private_key)


def test_verify_note_requires_trailing_newline() -> None:
    public_key = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    vkey = parse_vkey(format_vkey("robot/r1", public_key))
    with pytest.raises(SignedNoteError, match="newline"):
        verify_note("no blank line or signature", [vkey])


# --- Parse errors: every malformed input is a one-line SignedNoteError ---------------------

_KEY = Ed25519PrivateKey.generate()
_VKEY_BODY = base64.b64encode(b"\x01" + _KEY.public_key().public_bytes_raw()).decode()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"bad name+deadbeef+{_VKEY_BODY}", "vkey name"),
        (f"a/b+DEADBEEF+{_VKEY_BODY}", "8 lower-case hex digits"),
        ("a/b+deadbeef+!!!!", "not valid base64"),
        ("a/b+deadbeef+AR==", "not canonical base64"),  # decodes to 0x01, re-encodes as AQ==
    ],
)
def test_malformed_vkey_is_rejected__c2sp_signed_note(text: str, message: str) -> None:
    with pytest.raises(SignedNoteError, match=message):
        parse_vkey(text)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a\nb\n", "no blank line"),
        ("\n— k AAAAAA==\n", "signed note text is empty"),
        ("a\n\n", "no signature lines"),
        ("a\n\nxx k AAAAAA==\n", "does not start with"),
        ("a\n\n— k\n", "malformed signature line"),
        ("a\n\n— k AAA=\n", "shorter than its 4-byte key id"),
        ("a\n\n— k AAAAAB==\n", "not canonical base64"),
        ("a\x1b\n\n— k AAAAAA==\n", "control character"),
    ],
)
def test_malformed_note_is_rejected__c2sp_signed_note(text: str, message: str) -> None:
    with pytest.raises(SignedNoteError, match=message):
        parse_note(text)


def test_sign_note_rejects_bad_key_name() -> None:
    with pytest.raises(SignedNoteError, match="key name"):
        sign_note("hello\n", "bad name", _KEY)


def test_checkpoint_body_rejects_negative_size_and_short_root() -> None:
    with pytest.raises(SignedNoteError, match="negative"):
        checkpoint_body("r1", "s1", -1, bytes(32))
    with pytest.raises(SignedNoteError, match="32 bytes"):
        checkpoint_body("r1", "s1", 1, bytes(10))


def test_parse_signed_checkpoint_reads_the_body_from_the_signed_text__defs_10_3() -> None:
    note = sign_note(checkpoint_body("r1", "s1", 2, bytes(32)), "k/1", _KEY)
    body, signed = parse_signed_checkpoint(note)
    assert body == CheckpointBody("r1", "s1", 2, bytes(32))
    assert signed.text == checkpoint_body("r1", "s1", 2, bytes(32))


def test_parse_signed_checkpoint_rejects_lines_after_a_blank_line__defs_10_3() -> None:
    body = checkpoint_body("r1", "s1", 2, bytes(32))
    with pytest.raises(SignedNoteError, match="exactly 3"):
        parse_signed_checkpoint(sign_note(body + "\nextra\n", "k/1", _KEY))


def test_checkpoint_origin_must_use_safe_identifiers__defs_8() -> None:
    root = base64.b64encode(bytes(32)).decode()
    with pytest.raises(SignedNoteError, match="origin"):
        parse_checkpoint_body(f"autonometry/r 1/s1\n1\n{root}\n")
