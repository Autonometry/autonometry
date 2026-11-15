"""One recorder: the engine's events written to sealed session logs (docs/recorder.md).

No sockets here: the service feeds datagrams and timer ticks in, so the whole recorder runs and
is tested on any platform with fake clocks and fake file operations. One thread calls every
method; only the anchorer's thread runs beside it, and it never touches a file.
"""

import functools
import logging
import math
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from autonometry import sealfile, tsa
from autonometry.recorder.anchor import Anchorer
from autonometry.recorder.clock import ClockReader, ClockStatus
from autonometry.recorder.config import Prepared
from autonometry.recorder.engine import Clock, Engine, Restored, Settings
from autonometry.recorder.store import (
    DECLARED_FIELDS,
    FileOps,
    OsFileOps,
    Recovered,
    RecoveryError,
    SessionLog,
    SignedCheckpoint,
    append_record,
    is_closed,
    is_committed,
    new_session_dir,
    recover,
    repair_checkpoint_files,
    robot_dir,
    session_dirs,
)

logger = logging.getLogger(__name__)

#: Seconds between two looks at the free space of the data folder's disk.
DISK_CHECK_S = 1.0

FreeBytes = Callable[[Path], int]


def free_bytes(path: Path) -> int:
    """Bytes an unprivileged user may still write on the disk holding ``path``."""
    return shutil.disk_usage(path).free


#: Bytes kept free per event a batch may write besides one client event (recorder-made
#: transitions and heartbeats are far shorter), so a session never outgrows ``max_log_bytes``.
_RECORDER_EVENT_BYTES = 1024


class SystemClock:
    """The system's monotonic and UTC clocks."""

    def monotonic(self) -> float:
        """``time.monotonic``, seconds."""
        return time.monotonic()

    def wall_ns(self) -> int:
        """``time.time_ns``, nanoseconds since the Unix epoch."""
        return time.time_ns()


def tsa_anchorer(prepared: Prepared, notify: Callable[[], None]) -> Anchorer | None:
    """The anchorer for the config's TSA, or None when no TSA is configured."""
    url = prepared.config.tsa_url
    if url is None:
        return None
    request = functools.partial(
        tsa.request_timestamp, tsa_url=url, trusted_roots=prepared.tsa_roots
    )
    return Anchorer(request, notify, prepared.rules.sealing.max_commit_delay_s)


class Recorder:
    """Writes the sealed, crash-safe log of one robot (docs/recorder.md)."""

    def __init__(  # noqa: PLR0913 -- the recorder's collaborators, each replaceable in tests
        self,
        prepared: Prepared,
        clock: Clock,
        ops: FileOps | None = None,
        read_clock_status: Callable[[], ClockStatus] | None = None,
        anchorer: Anchorer | None = None,
        *,
        disk_free: FreeBytes = free_bytes,
    ) -> None:
        config, rules = prepared.config, prepared.rules
        self._prepared = prepared
        self._clock = clock
        self._ops = ops or OsFileOps()
        self._anchorer = anchorer
        self._disk_free = disk_free
        #: Monotonic time recording stopped for want of disk space; None while recording.
        self._full_since: float | None = None
        self._next_disk_check = -math.inf
        settings = Settings(
            robot_id=config.robot_id,
            scopes=config.scopes,
            declaration=prepared.declaration(),
            spec_version=prepared.spec_version,
            heartbeat_interval_s=rules.params.heartbeat_interval_s,
            gap_tolerance_s=rules.params.gap_tolerance_s,
            max_events_per_s=config.max_events_per_s,
            max_line_bytes=rules.errors.max_line_bytes,
        )
        reader = read_clock_status or ClockReader(config.clock_source).read
        self._engine = Engine(settings, clock, reader)
        reserve = rules.errors.max_line_bytes + _RECORDER_EVENT_BYTES * (len(config.scopes) + 4)
        self._rotate_at = config.max_log_bytes - reserve
        self._log: SessionLog | None = None

    @property
    def session(self) -> SessionLog:
        """The open session log."""
        if self._log is None:
            raise RuntimeError("the recorder has no open session")
        return self._log

    # -- start and stop -----------------------------------------------------------------------

    def start(self) -> None:
        """Continue the newest session if it has no ``session_end`` and its chain verifies,
        else open a new session (docs/recorder.md, "Crash recovery")."""
        if self._anchorer is not None:
            self._anchorer.start()
        robot = robot_dir(self._prepared)
        repair_checkpoint_files(robot)
        sessions = session_dirs(robot)
        newest = sessions[-1] if sessions else None
        if newest is not None and is_closed(newest):
            if not is_committed(newest, self._prepared):
                self._commit_closed(newest)
        elif newest is not None:
            try:
                recovered = recover(newest, self._prepared)
            except RecoveryError as exc:
                logger.warning(
                    "session %s cannot be continued (%s); it is left as it is", newest.name, exc
                )
            else:
                self._continue(recovered)
                if recovered.declaration() == self._declared():
                    return
                logger.warning("the config changed; closing session %s", newest.name)
                self._close("config_changed")
        self._open()

    def _commit_closed(self, directory: Path) -> None:
        """Write the final checkpoint a crash prevented after a session's ``session_end``."""
        try:
            recovered = recover(directory, self._prepared)
        except RecoveryError as exc:
            logger.warning("session %s cannot be committed (%s)", directory.name, exc)
            return
        log = SessionLog(
            directory, self._prepared.config.robot_id, self._prepared, self._ops, recovered.sealer
        )
        self._anchor(log.checkpoint_now())
        log.close()
        logger.info("wrote the final checkpoint of session %s", directory.name)

    def _declared(self) -> dict[str, Any]:
        declaration = self._prepared.declaration()
        return {name: declaration.get(name) for name in DECLARED_FIELDS}

    def _continue(self, recovered: Recovered) -> None:
        self._log = SessionLog(
            recovered.directory,
            self._prepared.config.robot_id,
            self._prepared,
            self._ops,
            recovered.sealer,
        )
        self._engine.restore(
            Restored(
                recovered.values,
                recovered.open_task,
                recovered.distance_m,
                recovered.last_ts_ns,
            )
        )
        heartbeat = self._engine.heartbeat(
            x_recovered=True, x_truncated_bytes=recovered.truncated_bytes
        )
        self._write([heartbeat, *self._engine.all_unknown("recovered")])
        self._anchor(self.session.checkpoint_now())
        logger.info(
            "continued session %s after %d event(s); cut %d byte(s) of a partial line",
            recovered.directory.name,
            recovered.events,
            recovered.truncated_bytes,
        )

    def _open(self) -> None:
        robot = robot_dir(self._prepared)
        directory = new_session_dir(robot, self._engine.now_ns(), self._prepared.inputs)
        self._log = SessionLog(directory, self._prepared.config.robot_id, self._prepared, self._ops)
        start = self._engine.session_start()
        self._write([start, *self._engine.carried_states(), self._engine.heartbeat()])
        logger.info("started session %s", directory.name)

    def _close(self, cause: str) -> None:
        log = self.session
        self._write([self._engine.heartbeat(), self._engine.session_end(cause)])
        self._anchor(log.finish())
        log.close()
        self._log = None
        logger.info("closed session %s (%s)", log.session_id, cause)

    def stop(self, token_wait_s: float = 10.0) -> None:
        """Final heartbeat, ``session_end``, fsync and final checkpoint; then wait up to
        ``token_wait_s`` seconds for outstanding timestamp tokens and append them."""
        self._close("shutdown")
        if self._anchorer is not None:
            self._anchorer.wait_idle(token_wait_s)
            self.deliver_tokens()
            self._anchorer.close()

    # -- running ------------------------------------------------------------------------------

    def receive(self, data: bytes) -> None:
        """Handle one datagram. Whatever a datagram holds, the recorder keeps running: one the
        engine fails on is counted as rejected and makes every scope ``unknown``. While the disk
        is below its free-space floor the datagram is dropped unread (:meth:`_room_to_write`)."""
        if not self._room_to_write():
            return
        try:
            events = self._engine.receive(data)
        except Exception as exc:  # process boundary: one datagram never stops the recorder
            logger.warning(
                "could not handle a datagram (%s); counted as rejected", type(exc).__name__
            )
            logger.debug("datagram failure", exc_info=True)
            events = self._engine.reject(f"internal error ({type(exc).__name__})")
        self._emit(events)

    def tick(self) -> None:
        """Do what is due: append fetched tokens, write due events, fsync if due. Nothing new is
        written while the disk is below its free-space floor."""
        if self._room_to_write():
            self.deliver_tokens()
            self._emit(self._engine.tick())
        interval = self._prepared.config.fsync_interval_s
        deadline = self.session.sync_deadline(interval)
        now = self._clock.monotonic()
        if deadline is not None and now >= deadline:
            self.session.sync(now)

    def next_deadline(self) -> float:
        """Monotonic time at which :meth:`tick` next has work."""
        sync = self.session.sync_deadline(self._prepared.config.fsync_interval_s)
        engine = math.inf if self._full_since is not None else self._engine.next_deadline()
        return min(engine, self._next_disk_check, math.inf if sync is None else sync)

    # -- disk space ---------------------------------------------------------------------------

    def _room_to_write(self) -> bool:
        """Whether the data folder's disk has at least ``min_free_bytes`` free, looked at once
        every :data:`DISK_CHECK_S` seconds (product rule 1: the recorder never fills the robot's
        disk). Below the floor recording stops with one error; datagrams are dropped unread, so
        each connector's counter shows the loss. When space returns, a heartbeat with
        ``x_disk_full_s`` and ``unknown`` on every scope resume the log: the time in between,
        with no event and no heartbeat, is ``unknown`` (definitions.md 2)."""
        now = self._clock.monotonic()
        if now < self._next_disk_check:
            return self._full_since is None
        self._next_disk_check = now + DISK_CHECK_S
        floor = self._prepared.config.min_free_bytes
        data_dir = self._prepared.config.data_dir
        try:
            free = self._disk_free(data_dir) if floor else floor
        except OSError as exc:
            logger.debug("cannot read the free space of %s: %s", data_dir, exc)
            return self._full_since is None
        if free < floor and self._full_since is None:
            self._full_since = now
            logger.error(
                "only %d bytes free on the disk of %s, below min_free_bytes (%d): recording "
                "stops until there is room again; the time in between will be unknown",
                free,
                data_dir,
                floor,
            )
        elif free >= floor and self._full_since is not None:
            stopped_s, self._full_since = now - self._full_since, None
            logger.warning("disk space is back after %.0f s; recording continues", stopped_s)
            heartbeat = self._engine.heartbeat(x_disk_full_s=stopped_s)
            self._emit([heartbeat, *self._engine.all_unknown("disk_full")])
        return self._full_since is None

    def deliver_tokens(self) -> None:
        """Append every token the anchorer fetched as a timestamp record (definitions.md 10.4)."""
        if self._anchorer is None:
            return
        for token in self._anchorer.take_results():
            line = sealfile.encode_timestamp_line(token.note_sha256, token.tsr)
            if self._log is not None and token.checkpoints_path == self._log.checkpoints_path:
                self._log.append_timestamp(line)
            else:
                append_record(token.checkpoints_path, line, self._ops)

    # -- writing ------------------------------------------------------------------------------

    def _write(self, events: Iterable[Mapping[str, Any]]) -> None:
        for event in events:
            self._anchor(self.session.append(event))

    def _emit(self, events: list[dict[str, Any]]) -> None:
        """Write one batch; rotate after it when the log is near ``max_log_bytes``, keeping
        the batch, whose events share a time, in one session."""
        if not events:
            return
        self._write(events)
        if self.session.size_bytes > self._rotate_at:
            self._close("rotation")
            self._open()

    def _anchor(self, checkpoint: SignedCheckpoint | None) -> None:
        if checkpoint is not None and self._anchorer is not None:
            self._anchorer.submit(checkpoint.checkpoints_path, checkpoint.note)
