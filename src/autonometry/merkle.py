"""Merkle tree hashing (definitions.md v0.2 section 10.2; RFC 9162 section 2.1.1 / RFC 6962
section 2.1): a leaf's data is hashed with a ``0x00`` prefix and an interior node with a ``0x01``
prefix, the tree splitting at the largest power of two below the leaf count.

Pure functions and a pure incremental builder: no I/O, no randomness.
"""

import hashlib
from collections.abc import Iterable

_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"


def leaf_hash(data: bytes) -> bytes:
    """Hash of one Merkle tree leaf: SHA-256(0x00 || data)."""
    return hashlib.sha256(_LEAF_PREFIX + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    """Hash of one interior node: SHA-256(0x01 || left || right)."""
    return hashlib.sha256(_NODE_PREFIX + left + right).digest()


class IncrementalTree:
    """Builds a Merkle tree hash one leaf at a time in O(log n) space and O(1) amortised time.

    Equivalent to the recursive definition that splits a list of leaves at the largest power of
    two below its length, but keeps only the roots of the complete subtrees seen so far (one per
    set bit of the current size): a crashed :class:`~autonometry.sealing.Sealer` rebuilds this
    state by replaying the events already sealed on disk, never the whole tree.
    """

    __slots__ = ("_levels", "_size")

    def __init__(self) -> None:
        self._levels: list[bytes | None] = []
        self._size = 0

    @property
    def size(self) -> int:
        """Number of leaves appended so far."""
        return self._size

    def append(self, leaf: bytes) -> None:
        """Add one more leaf hash (already hashed with :func:`leaf_hash`)."""
        node = leaf
        level = 0
        while level < len(self._levels):
            existing = self._levels[level]
            if existing is None:
                break
            node = node_hash(existing, node)
            self._levels[level] = None
            level += 1
        if level == len(self._levels):
            self._levels.append(node)
        else:
            self._levels[level] = node
        self._size += 1

    def root(self) -> bytes:
        """Return the tree hash of every leaf appended so far.

        Raises:
            ValueError: no leaf has been appended yet.
        """
        acc: bytes | None = None
        for level_hash in self._levels:
            if level_hash is None:
                continue
            acc = level_hash if acc is None else node_hash(level_hash, acc)
        if acc is None:
            raise ValueError("tree has no leaves")
        return acc


def root_of(leaves: Iterable[bytes]) -> bytes:
    """Return the Merkle tree hash of already-hashed leaves, in order (definitions.md 10.2).

    Raises:
        ValueError: ``leaves`` is empty.
    """
    tree = IncrementalTree()
    for leaf in leaves:
        tree.append(leaf)
    return tree.root()
