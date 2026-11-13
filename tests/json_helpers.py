"""Tolerant comparison of JSON values for golden tests. Not collected by pytest."""

import math
from typing import Any

#: Floats may differ in late digits between platforms and library versions (optimisers, BLAS).
REL = 1e-4
ABS = 1e-8


def close(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Differences between two JSON values: floats within the tolerance, everything else equal."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected) != set(actual):
            return [f"{path}: keys {sorted(set(expected) ^ set(actual))}"]
        return [d for key in expected for d in close(expected[key], actual[key], f"{path}.{key}")]
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(expected)} != {len(actual)}"]
        return [
            d
            for i, pair in enumerate(zip(expected, actual, strict=True))
            for d in close(*pair, f"{path}[{i}]")
        ]
    if (
        isinstance(expected, float)
        and isinstance(actual, float | int)
        and not isinstance(actual, bool)
    ):
        ok = math.isclose(expected, actual, rel_tol=REL, abs_tol=ABS)
        return [] if ok else [f"{path}: {expected} != {actual}"]
    return (
        []
        if expected == actual and type(expected) is type(actual)
        else [f"{path}: {expected!r} != {actual!r}"]
    )
