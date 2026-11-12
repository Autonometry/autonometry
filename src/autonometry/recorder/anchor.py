"""RFC 3161 timestamps for new checkpoints, fetched on one background thread
(definitions.md 10.4, 10.5).

The writer submits each checkpoint it writes and never waits: the thread requests a token and
hands it back through a queue, and the writer appends the timestamp record. A failed request is
logged and retried with backoff; a newer checkpoint of the same checkpoint file replaces one still
waiting, because it covers every event the older one covers. A checkpoint still without a token
``max_commit_delay_s`` after it was submitted is given up: a later token would be a
``late_anchor`` anyway, and ``autonometry timestamp`` can still add one.
"""

import hashlib
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Seconds to wait before each retry of a failed checkpoint; the last value repeats.
RETRY_DELAYS_S = (5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0)


@dataclass(frozen=True, slots=True)
class Token:
    """A token to append to ``checkpoints_path`` as a timestamp record (definitions.md 10.4)."""

    checkpoints_path: Path
    note_sha256: str
    tsr: bytes


@dataclass(slots=True)
class _Pending:
    note: str
    submitted: float
    due: float
    failures: int = 0


class Anchorer:
    """The token thread. ``request`` takes a signed note's bytes and returns a verified token
    (:func:`autonometry.tsa.request_timestamp` with the configured URL and roots) or raises;
    ``notify`` is called, from the thread, after each token is queued."""

    def __init__(
        self,
        request: Callable[[bytes], bytes],
        notify: Callable[[], None],
        give_up_after_s: float,
        clock: Callable[[], float] = time.monotonic,
        retry_delays_s: Sequence[float] = RETRY_DELAYS_S,
    ) -> None:
        self._request = request
        self._notify = notify
        self._give_up_after_s = give_up_after_s
        self._clock = clock
        self._retry_delays_s = tuple(retry_delays_s)
        self._pending: dict[Path, _Pending] = {}
        self._in_flight = False
        self._closed = False
        self._condition = threading.Condition()
        self._results: queue.SimpleQueue[Token] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="autonometry-tsa", daemon=True)

    def start(self) -> None:
        """Start the thread."""
        self._thread.start()

    def submit(self, checkpoints_path: Path, note: str) -> None:
        """Timestamp ``note``, replacing any checkpoint of the same file still waiting."""
        now = self._clock()
        with self._condition:
            self._pending[checkpoints_path] = _Pending(note, now, now)
            self._condition.notify_all()

    def take_results(self) -> list[Token]:
        """Every token fetched since the last call, in the order fetched."""
        tokens = []
        while True:
            try:
                tokens.append(self._results.get_nowait())
            except queue.Empty:
                return tokens

    def wait_idle(self, timeout_s: float) -> bool:
        """Wait up to ``timeout_s`` seconds until nothing is waiting or in flight."""
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._pending and not self._in_flight, timeout_s
            )

    def close(self) -> None:
        """Stop the thread after its current request; waiting checkpoints are dropped."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _next(self) -> tuple[Path, _Pending] | None:
        """Wait for the waiting checkpoint due first and take it; None once closed."""
        with self._condition:
            while not self._closed:
                now = self._clock()
                due = [(item.due, path) for path, item in self._pending.items()]
                if due and min(due)[0] <= now:
                    path = min(due)[1]
                    self._in_flight = True
                    return path, self._pending.pop(path)
                self._condition.wait(min(due)[0] - now if due else None)
            return None

    def _run(self) -> None:
        while (taken := self._next()) is not None:
            path, item = taken
            note = item.note.encode("utf-8")
            try:
                tsr = self._request(note)
            except Exception as exc:  # thread boundary: log, retry later, keep running
                logger.warning("could not timestamp a checkpoint of %s: %s", path, exc)
                self._retry(path, item)
                continue
            token = Token(path, hashlib.sha256(note).hexdigest(), tsr)
            self._results.put(token)
            with self._condition:
                self._in_flight = False
                self._condition.notify_all()
            self._notify()

    def _retry(self, path: Path, item: _Pending) -> None:
        now = self._clock()
        with self._condition:
            self._in_flight = False
            if path not in self._pending:  # not replaced by a newer checkpoint meanwhile
                if now - item.submitted >= self._give_up_after_s:
                    logger.warning(
                        "giving up timestamping a checkpoint of %s after %d attempts",
                        path,
                        item.failures + 1,
                    )
                else:
                    delay = self._retry_delays_s[min(item.failures, len(self._retry_delays_s) - 1)]
                    item.failures += 1
                    item.due = now + delay
                    self._pending[path] = item
            self._condition.notify_all()
