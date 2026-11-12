"""Slow performance check: seal and verify 100k events (run with ``pytest -m slow --no-cov``).

Reports timing and peak memory to stdout (``pytest -s`` to see it). Timing is measured without
``tracemalloc`` (which can slow allocation-heavy code down by an order of magnitude on its own);
peak memory is measured in a separate pass, so neither figure distorts the other. The assertions
are loose bounds so this does not become flaky on a slower machine, not a benchmark gate.
"""

import hashlib
import time
import tracemalloc
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonometry.events import Counts, Event, LogCheck, Session
from autonometry.sealfile import CheckpointLine, SealFile
from autonometry.sealing import Checkpoint, Sealer
from autonometry.signednote import checkpoint_body, format_vkey, parse_vkey, sign_note
from autonometry.spec import load_rules
from autonometry.timestamps import ns_to_seconds, seconds_to_ns
from autonometry.verify import verify_log

MiB = 1024 * 1024
N_EVENTS = 100_000
RULES = load_rules("0.2")
KEY_NAME = "autonometry/perf-test-key"
_SEED = hashlib.sha256(b"autonometry-perf-test-seed").digest()
SEED_KEY = Ed25519PrivateKey.from_private_bytes(_SEED)


def _events(n: int) -> list[Event]:
    start_ns = seconds_to_ns(0.0)
    step_ns = seconds_to_ns(0.5)
    events: list[Event] = []
    for i in range(n):
        ts_s = ns_to_seconds(start_ns + i * step_ns)
        hours, remainder = divmod(ts_s, 3600)
        minutes, seconds = divmod(remainder, 60)
        events.append(
            {  # type: ignore[typeddict-item]
                "robot_id": "perf",
                "session_id": "s1",
                "task_id": None,
                "seq": i,
                "ts": f"2026-01-01T{int(hours) % 24:02d}:{int(minutes):02d}:{seconds:06.3f}Z",
                "type": "heartbeat",
            }
        )
    return events


def _seal(events: list[Event]) -> tuple[list[Event], list[Checkpoint]]:
    sealer = Sealer(
        RULES.chain_genesis_prefix,
        "perf",
        "s1",
        RULES.sealing.checkpoint_every_events,
        RULES.sealing.checkpoint_every_s,
    )
    sealed_events: list[Event] = []
    checkpoints: list[Checkpoint] = []
    for event in events:
        sealed, checkpoint = sealer.add(event)
        sealed_events.append(sealed)
        if checkpoint is not None:
            checkpoints.append(checkpoint)
    final = sealer.finish()
    if final is not None:
        checkpoints.append(final)
    return sealed_events, checkpoints


def _verify(sealed_events: list[Event], checkpoints: list[Checkpoint]) -> str:
    notes = [
        sign_note(checkpoint_body("perf", "s1", cp.size, cp.root), KEY_NAME, SEED_KEY)
        for cp in checkpoints
    ]
    session = Session("perf", "s1", tuple(sealed_events))
    counts = Counts(
        files=1,
        lines=N_EVENTS,
        blank_lines=0,
        events=N_EVENTS,
        rejected_lines=0,
        sessions=1,
        errors_by_code={},
        flags_by_code={},
    )
    log_check = LogCheck("0.2", (session,), (), (), counts)
    records = tuple(CheckpointLine(i, n) for i, n in enumerate(notes, 1))
    files = [SealFile("perf.checkpoints.jsonl", records, (), {})]
    trusted_keys = [parse_vkey(format_vkey(KEY_NAME, SEED_KEY.public_key().public_bytes_raw()))]
    result = verify_log(log_check, files, files, trusted_keys, [], RULES)
    return result.sessions[0].status


def _timed[T](func: Callable[[], T]) -> tuple[float, T]:
    start = time.perf_counter()
    result = func()
    return time.perf_counter() - start, result


def _peak_memory(func: Callable[[], None]) -> int:
    tracemalloc.start()
    try:
        func()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.slow
def test_seal_100k_events_time_and_memory() -> None:
    events = _events(N_EVENTS)

    seal_seconds, (sealed_events, checkpoints) = _timed(lambda: _seal(events))
    verify_seconds, status = _timed(lambda: _verify(sealed_events, checkpoints))
    assert status == "verified"

    seal_peak = _peak_memory(lambda: _seal(events))
    verify_peak = _peak_memory(lambda: _verify(sealed_events, checkpoints))

    print(
        f"\nseal {N_EVENTS} events, {len(checkpoints)} checkpoints: "
        f"{seal_seconds:.2f}s, {seal_peak / MiB:.1f} MiB peak\n"
        f"verify: {verify_seconds:.2f}s, {verify_peak / MiB:.1f} MiB peak"
    )
    assert seal_seconds < 60
    assert verify_seconds < 60
    assert seal_peak < 512 * MiB
    assert verify_peak < 512 * MiB
