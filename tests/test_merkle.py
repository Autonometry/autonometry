"""Tests for autonometry.merkle (definitions.md v0.2 section 10.2; RFC 9162 / RFC 6962)."""

import pytest

from autonometry.merkle import IncrementalTree, leaf_hash, node_hash, root_of

# Known-answer test vectors from the original C++ reference implementation
# (github.com/google/certificate-transparency, cpp/merkletree/merkle_tree_test.cc), reused
# verbatim by transparency-dev/merkle. Inputs are hex-encoded; roots are SHA-256 hex.
_INPUTS_HEX = [
    "",
    "00",
    "10",
    "2021",
    "3031",
    "40414243",
    "5051525354555657",
    "606162636465666768696a6b6c6d6e6f",
]

_ROOTS_HEX = [
    "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
    "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
    "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
    "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
    "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
    "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
    "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328",
]

_EMPTY_LEAF_HASH = "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"


def _leaves() -> list[bytes]:
    return [bytes.fromhex(h) for h in _INPUTS_HEX]


def test_leaf_hash_of_empty_string__defs_10_2() -> None:
    assert leaf_hash(b"").hex() == _EMPTY_LEAF_HASH


def test_root_of_each_prefix_matches_rfc6962_vectors__defs_10_2() -> None:
    leaves = _leaves()
    for size in range(1, len(leaves) + 1):
        root = root_of(leaf_hash(leaf) for leaf in leaves[:size])
        assert root.hex() == _ROOTS_HEX[size - 1]


def test_incremental_tree_matches_bulk_root_at_every_size__defs_10_2() -> None:
    tree = IncrementalTree()
    for i, leaf in enumerate(_leaves(), start=1):
        tree.append(leaf_hash(leaf))
        assert tree.size == i
        assert tree.root().hex() == _ROOTS_HEX[i - 1]


def test_node_hash_is_order_sensitive() -> None:
    a, b = leaf_hash(b"a"), leaf_hash(b"b")
    assert node_hash(a, b) != node_hash(b, a)


def test_node_hash_differs_from_leaf_hash_of_concatenation() -> None:
    # second-preimage resistance: an interior node must not collide with a leaf of its children.
    a, b = leaf_hash(b"Hello"), leaf_hash(b"World")
    assert node_hash(a, b) != leaf_hash(a + b)


def test_root_of_empty_raises() -> None:
    with pytest.raises(ValueError, match="no leaves"):
        root_of([])


def test_incremental_tree_root_before_any_append_raises() -> None:
    with pytest.raises(ValueError, match="no leaves"):
        IncrementalTree().root()
