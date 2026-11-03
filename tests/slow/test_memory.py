"""Slow memory checks (run with ``uv run pytest -m slow --no-cov``)."""

import tracemalloc
from pathlib import Path

import pytest

from autonometry import spec
from autonometry.reader import read_logs

MiB = 1024 * 1024
SPEC = "0.2"


@pytest.mark.slow
def test_a_million_rejected_lines_stay_within_a_fixed_memory_budget__defs_8(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_bytes(b"x\n1\n" * 500_000)
    tracemalloc.start()
    try:
        result = read_logs([path], SPEC)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    limit = spec.load_rules(SPEC).errors.list_max_per_code
    assert result.counts.lines == result.counts.rejected_lines == 1_000_000
    assert result.counts.errors_by_code == {"invalid_json": 500_000, "not_an_object": 500_000}
    assert len(result.errors) == 2 * limit
    assert peak < 16 * MiB, f"peak {peak / MiB:.1f} MiB"
