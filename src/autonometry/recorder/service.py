"""The recorder process (POSIX): one loop owns the socket and every file write
(README, "Recorder").

Connectors send fire-and-forget datagrams to a Unix datagram socket; the loop (``selectors``)
reads them, runs timers and writes the log through :class:`~autonometry.recorder.runtime.Recorder`.
The only other thread fetches RFC 3161 tokens and wakes the loop when one is ready. SIGTERM or
SIGINT stops the recorder cleanly. A lock file keeps a second recorder away from the same robot's
folder.
"""

import contextlib
import fcntl
import grp
import logging
import os
import selectors
import signal
import socket
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autonometry.recorder.anchor import Anchorer
from autonometry.recorder.clock import ClockStatus
from autonometry.recorder.config import Prepared
from autonometry.recorder.engine import Clock
from autonometry.recorder.runtime import Recorder, SystemClock, tsa_anchorer
from autonometry.recorder.store import FileOps, robot_dir

logger = logging.getLogger(__name__)

#: Most datagrams handled before timers run again.
_BATCH = 256
#: Receive buffer asked for (the system may grant less).
_RECEIVE_BUFFER_BYTES = 4 << 20
#: Seconds to wait at shutdown for the final checkpoint's timestamp token.
STOP_TOKEN_WAIT_S = 10.0
_LOCK_FILE = ".lock"

AnchorerFactory = Callable[[Prepared, Callable[[], None]], Anchorer | None]


class StartError(Exception):
    """The recorder cannot start: the socket or the robot's folder is in use or unusable. The
    message is one line."""


def lock_robot_dir(robot: Path) -> int:
    """Take an exclusive lock on ``<robot>/.lock``; return its descriptor, held until closed.

    Raises:
        StartError: another recorder holds the lock.
    """
    fd = os.open(robot / _LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise StartError(f"another recorder is already writing to {robot}") from None
    return fd


def clear_stale_socket(path: Path) -> None:
    """Remove a socket left behind by a recorder that died; refuse a live one or another file.

    Raises:
        StartError: something listens on ``path``, or ``path`` is not a socket.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise StartError(f"{path} exists and is not a socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        probe.connect(str(path))
    except ConnectionRefusedError:
        os.unlink(path)
        return
    except OSError as exc:
        raise StartError(f"cannot check {path}: {exc.strerror or exc}") from None
    finally:
        probe.close()
    raise StartError(f"another process is receiving on {path}")


def bind_socket(path: Path, group: str | None) -> socket.socket:
    """Bind a non-blocking Unix datagram socket at ``path``: mode 0600, or 0660 owned by
    ``group`` when one is given. The folder must exist.

    Raises:
        StartError: the folder is missing, the group is unknown, or binding fails.
    """
    if not path.parent.is_dir():
        raise StartError(f"the folder of socket_path does not exist: {path.parent}")
    gid = None
    if group is not None:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError:
            raise StartError(f"socket_group {group!r} is not a group on this system") from None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    old_umask = os.umask(0o177)  # no window in which the socket is open to others
    try:
        sock.bind(str(path))
        if gid is not None:
            os.chown(path, -1, gid)
        os.chmod(path, 0o660 if gid is not None else 0o600)
    except OSError as exc:
        sock.close()
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise StartError(f"cannot bind {path}: {exc.strerror or exc}") from None
    finally:
        os.umask(old_umask)
    sock.setblocking(False)
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RECEIVE_BUFFER_BYTES)
    return sock


class Service:
    """The recorder process: lock, recover or start a session, bind, loop, stop."""

    def __init__(
        self,
        prepared: Prepared,
        clock: Clock | None = None,
        ops: FileOps | None = None,
        read_clock_status: Callable[[], ClockStatus] | None = None,
        anchorer_factory: AnchorerFactory = tsa_anchorer,
    ) -> None:
        self._prepared = prepared
        self._clock = clock or SystemClock()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._stopping = False
        self._recorder = Recorder(
            prepared, self._clock, ops, read_clock_status, anchorer_factory(prepared, self._wake)
        )

    def _wake(self) -> None:
        with contextlib.suppress(OSError):  # full: the loop is waking up anyway
            self._wake_w.send(b"\0")

    def request_stop(self) -> None:
        """Ask the loop to stop; safe from a signal handler or another thread."""
        self._stopping = True
        self._wake()

    def run(self, install_signals: bool = True) -> None:
        """Run until :meth:`request_stop` (or SIGTERM/SIGINT when ``install_signals``).

        Raises:
            StartError: the recorder could not start; nothing was written unless the socket
                could not be bound after the session started, in which case it is closed.
        """
        try:
            lock_fd = lock_robot_dir(robot_dir(self._prepared))
            try:
                self._run_locked(install_signals)
            finally:
                os.close(lock_fd)
        finally:
            self._wake_r.close()
            self._wake_w.close()

    def _run_locked(self, install_signals: bool) -> None:
        config = self._prepared.config
        clear_stale_socket(config.socket_path)
        self._recorder.start()
        try:
            sock = bind_socket(config.socket_path, config.socket_group)
        except StartError:
            self._recorder.stop(0.0)
            raise
        logger.info("receiving on %s", config.socket_path)
        if install_signals:
            self._install_signals()
        try:
            self._loop(sock)
        finally:
            sock.close()
            with contextlib.suppress(OSError):
                os.unlink(config.socket_path)

    def _install_signals(self) -> None:
        def handler(signum: int, frame: Any) -> None:
            self.request_stop()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def _loop(self, sock: socket.socket) -> None:
        limit = self._prepared.rules.errors.max_line_bytes + 1  # one more shows a datagram too long
        with selectors.DefaultSelector() as selector:
            selector.register(sock, selectors.EVENT_READ, "socket")
            selector.register(self._wake_r, selectors.EVENT_READ, "wake")
            while not self._stopping:
                timeout = max(0.0, self._recorder.next_deadline() - self._clock.monotonic())
                for key, _ in selector.select(timeout):
                    if key.data == "socket":
                        self._drain(sock, limit)
                    else:
                        self._drain_wake()
                self._recorder.tick()
        self._recorder.stop(STOP_TOKEN_WAIT_S)

    def _drain(self, sock: socket.socket, limit: int) -> None:
        for _ in range(_BATCH):
            try:
                data = sock.recv(limit)
            except BlockingIOError:
                return
            self._recorder.receive(data)

    def _drain_wake(self) -> None:
        with contextlib.suppress(BlockingIOError):
            while self._wake_r.recv(4096):
                pass


def serve(prepared: Prepared) -> int:
    """Run the recorder until SIGTERM or SIGINT; return the exit status: 0 after a clean stop,
    2 when it could not start."""
    try:
        Service(prepared).run()
    except StartError as exc:
        logger.error("%s", exc)
        return 2
    return 0
