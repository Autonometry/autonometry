"""Autonometry recorder client: send control events to ``autonometry record``.

One file, standard library only, Python 3.8 or newer: copy it into a connector. Every call
returns at once and never raises. Events go out as fire-and-forget Unix datagrams; when the
recorder is down, its socket buffer is full, or a value cannot be serialised, the event is dropped
and counted in ``Recorder.dropped``. The recorder still sees every drop as a gap in the counter
``n`` and marks the time ``unknown``.

Wire format (one UTF-8 JSON object per datagram, at most 65536 bytes)::

    {"client": "<name>", "n": <counter>, "event": {"type": "transition", ...}}
    {"client": "<name>", "n": <counter>, "distance_m": <odometer reading in metres>}

``n`` counts every call that sends a datagram, from 0 for each client object, including the
calls whose datagram was dropped.

Example::

    from autonometry_client import Recorder

    rec = Recorder("/run/autonometry/recorder.sock", "nav_connector")
    rec.state("base", "ai", actor_id="ai:nav2", device_id="nav2")    # on change and every second
    rec.event("task_start", task_id="t17", task_definition_id="dock_v1")
    rec.distance(12.5)

Licensed under the Apache License, Version 2.0.
"""

import contextlib
import json
import logging
import re
import socket
import threading
from typing import Any, Dict, Optional

__all__ = ["MAX_DATAGRAM_BYTES", "Recorder"]

#: Largest datagram the recorder accepts (rules.toml ``[errors] max_line_bytes``).
MAX_DATAGRAM_BYTES = 65536
_SEND_BUFFER_BYTES = 4 * MAX_DATAGRAM_BYTES
_CLIENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

logger = logging.getLogger("autonometry_client")


class Recorder:
    """A connection to an Autonometry recorder. Thread-safe; no call ever blocks or raises.

    Args:
        socket_path: the recorder's Unix datagram socket (its config's ``socket_path``).
        client_name: this connector's name: 1 to 128 ASCII letters, digits, ``.``, ``_``, ``:``
            or ``-``, starting with a letter or digit. Use one name per connector process.
    """

    def __init__(self, socket_path: str, client_name: str) -> None:
        self._path = str(socket_path)
        self._lock = threading.Lock()
        self._n = 0
        self._dropped = 0
        self._sock = None  # type: Optional[socket.socket]
        name = str(client_name)
        if not _CLIENT_RE.match(name):
            logger.warning("client name is not a valid identifier; the recorder will reject it")
        self._prefix = '{"client":' + json.dumps(name) + ',"n":'
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.setblocking(False)
        except Exception:  # never raise into the robot's code
            logger.warning("cannot create a Unix datagram socket; every event will be dropped")
            return
        # macOS limits Unix datagrams to the send buffer, 2048 bytes by default.
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SEND_BUFFER_BYTES)
        self._sock = sock

    @property
    def dropped(self) -> int:
        """Events dropped so far: recorder down, buffer full, or a value not serialisable."""
        return self._dropped

    @property
    def sent(self) -> int:
        """Datagrams this client tried to send so far (the next ``n``)."""
        return self._n

    def state(
        self,
        scope: str,
        state: str,
        reason: Optional[str] = None,
        actor_id: Optional[str] = None,
        device_id: Optional[str] = None,
        **fields: Any,
    ) -> None:
        """Assert the current control state of one scope (definitions.md section 2).

        Call it on every change and at least once a second while the state holds: the recorder
        writes a transition only when the state, reason, actor or device changes, and marks a
        scope ``unknown`` when no assertion arrives for ``gap_tolerance_s`` (2 s). Other
        transition fields (``plan_item_id``, ``related_seq``, ``x_...`` extensions) go in
        ``fields``.
        """
        event = {
            "type": "transition",
            "scope": scope,
            "state": state,
            "reason": reason,
            "actor_id": actor_id,
        }  # type: Dict[str, Any]
        if device_id is not None:
            event["device_id"] = device_id
        event.update(fields)
        self._send_event(event)

    def event(self, type: str, **fields: Any) -> None:
        """Send a discrete event as received: ``task_start``, ``task_end``, ``hint``,
        ``approval``, ``help_request``, ``supervision``, ``support_start`` or ``support_end``,
        with its fields (definitions.md section 8)."""
        event = {"type": type}  # type: Dict[str, Any]
        event.update(fields)
        self._send_event(event)

    def distance(self, metres: float) -> None:
        """Report the odometer: cumulative metres travelled, never decreasing while this client
        runs. The recorder adds up the increases into each heartbeat's ``distance_m``."""
        try:
            body = '"distance_m":' + json.dumps(float(metres), allow_nan=False) + "}"  # type: Optional[str]
        except Exception:  # never raise into the robot's code
            body = None
        self._send(body)

    def close(self) -> None:
        """Close the socket; later calls drop their events."""
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _send_event(self, event: Dict[str, Any]) -> None:
        try:
            text = json.dumps(event, allow_nan=False, separators=(",", ":"), ensure_ascii=False)
            body = '"event":' + text + "}"  # type: Optional[str]
        except Exception:  # never raise into the robot's code
            body = None
        self._send(body)

    def _send(self, body: Optional[str]) -> None:
        """Number and send one datagram; ``body`` None means it could not be serialised."""
        with self._lock:
            n = self._n
            self._n += 1
            if not self._sent(n, body):
                self._dropped += 1

    def _sent(self, n: int, body: Optional[str]) -> bool:
        if body is None or self._sock is None:
            return False
        try:
            data = f"{self._prefix}{n},{body}".encode()
            if len(data) > MAX_DATAGRAM_BYTES:
                return False
            self._sock.sendto(data, self._path)
        except Exception:  # no recorder, buffer full, refused, or text that is not UTF-8
            return False
        return True
