"""Names connectors read from a robot's network or files, made fit for a sealed log
(``autonometry.connectors.names``; security review M10, decisions 6 and 8)."""

import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from autonometry.connectors.names import MAX_NODE_NAME, node_name, pseudonym


def test_a_pseudonym_is_the_first_12_hex_of_the_sha256_of_the_name__claude_md_rule_5() -> None:
    assert pseudonym("python3") == hashlib.sha256(b"python3").hexdigest()[:12] == "c1cc69e61c0f"
    assert (
        pseudonym("/home/alice.smith/models/act")
        == hashlib.sha256(b"/home/alice.smith/models/act").hexdigest()[:12]
    )
    assert pseudonym(None) is None


@pytest.mark.parametrize(
    "name", ["/bt_navigator", "bt_navigator", "/robot1/teleop_twist_keyboard", "/_hidden/x_2"]
)
def test_a_valid_ros_2_node_name_is_kept__defs_6_10(name: str) -> None:
    assert node_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "/joy\x1b[31m",
        "/two\nlines",
        "/1starts_with_a_digit",
        "/ends/",
        "/alice smith's laptop",
        "/" + "a" * MAX_NODE_NAME,
    ],
)
def test_any_other_name_becomes_a_pseudonym__decision_8(name: str) -> None:
    assert node_name(name) == f"pseudonym:{pseudonym(name)}"


@given(st.text(max_size=300))
def test_whatever_the_network_says_the_logged_name_is_short_and_printable(name: str) -> None:
    logged = node_name(name)
    assert len(logged) <= MAX_NODE_NAME
    assert logged.isascii()
    assert logged.isprintable()
