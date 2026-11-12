"""Tests for clients/python/autonometry_client.py (README "Wire format").

Written with unittest and Python 3.8 syntax so CI can run them on Python 3.8 without pytest:
``python -m unittest discover -s tests/client``. POSIX only.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "clients", "python"))

import autonometry_client  # noqa: E402


def _short_dir() -> str:
    return tempfile.mkdtemp(prefix="amc", dir="/tmp" if os.path.isdir("/tmp") else None)


@unittest.skipIf(sys.platform == "win32", "the client needs Unix datagram sockets")
class ClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _short_dir()
        self.path = os.path.join(self.dir, "rec.sock")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def receiver(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(self.path)
        sock.settimeout(2.0)
        self.addCleanup(sock.close)
        return sock

    def client(self, name: str) -> autonometry_client.Recorder:
        rec = autonometry_client.Recorder(self.path, name)
        self.addCleanup(rec.close)
        return rec

    def receive(self, sock: socket.socket) -> Any:
        return json.loads(sock.recv(70000).decode("utf-8"))

    def test_wire_format(self) -> None:
        sock = self.receiver()
        rec = self.client("nav")
        rec.state("base", "ai", actor_id="ai:nav2", device_id="nav2", x_src_ts=1.5)
        rec.state("arm", "human_remote", "planned", "op_1", plan_item_id="reset_scene")
        rec.event("task_start", task_id="t1", task_definition_id="dock_v1")
        rec.distance(12)
        self.assertEqual(
            self.receive(sock),
            {
                "client": "nav",
                "n": 0,
                "event": {
                    "type": "transition",
                    "scope": "base",
                    "state": "ai",
                    "reason": None,
                    "actor_id": "ai:nav2",
                    "device_id": "nav2",
                    "x_src_ts": 1.5,
                },
            },
        )
        second = self.receive(sock)
        self.assertEqual(second["n"], 1)
        self.assertNotIn("device_id", second["event"])
        self.assertEqual(second["event"]["plan_item_id"], "reset_scene")
        self.assertEqual(second["event"]["reason"], "planned")
        self.assertEqual(
            self.receive(sock),
            {
                "client": "nav",
                "n": 2,
                "event": {"type": "task_start", "task_id": "t1", "task_definition_id": "dock_v1"},
            },
        )
        self.assertEqual(self.receive(sock), {"client": "nav", "n": 3, "distance_m": 12.0})
        self.assertEqual((rec.sent, rec.dropped), (4, 0))

    def test_no_recorder_drops_and_counts(self) -> None:
        rec = self.client("nav")
        rec.state("base", "ai")
        rec.event("help_request", scope="base")
        rec.distance(1.0)
        self.assertEqual((rec.sent, rec.dropped), (3, 3))

    def test_bad_values_are_dropped_but_numbered(self) -> None:
        sock = self.receiver()
        rec = self.client("nav")
        rec.state("base", "ai", x_thing=object())
        rec.state("base", "ai", x_value=float("nan"))
        rec.state("base", "ai", x_text="\ud800")
        rec.distance("far")  # type: ignore[arg-type]
        rec.distance(float("inf"))
        rec.event("help_request", scope="base", x_pad="p" * 70000)
        rec.event("help_request", scope="base")
        self.assertEqual(rec.dropped, 6)
        self.assertEqual(self.receive(sock)["n"], 6)  # the gap shows the recorder the loss

    def test_a_full_buffer_never_blocks_or_raises(self) -> None:
        self.receiver()  # never read
        rec = self.client("nav")
        started = time.monotonic()
        for _ in range(20000):
            rec.event("help_request", scope="base", x_pad="p" * 500)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertGreater(rec.dropped, 0)
        self.assertEqual(rec.sent, 20000)

    def test_threads_number_datagrams_in_send_order(self) -> None:
        sock = self.receiver()
        rec = self.client("nav")
        received: List[int] = []
        done = threading.Event()

        def read() -> None:
            sock.settimeout(0.5)
            while True:
                try:
                    received.append(self.receive(sock)["n"])
                except socket.timeout:
                    if done.is_set():
                        return

        reader = threading.Thread(target=read)
        reader.start()

        def send() -> None:
            for _ in range(500):
                rec.event("help_request", scope="base")

        senders = [threading.Thread(target=send) for _ in range(8)]
        for thread in senders:
            thread.start()
        for thread in senders:
            thread.join()
        done.set()
        reader.join()
        self.assertEqual(rec.sent, 4000)
        self.assertEqual(len(received) + rec.dropped, 4000)
        self.assertEqual(received, sorted(set(received)))

    def test_an_invalid_name_never_raises(self) -> None:
        sock = self.receiver()
        rec = self.client("bad name!")
        rec.state("base", "ai")
        self.assertEqual(self.receive(sock)["client"], "bad name!")

    def test_close_drops_later_events(self) -> None:
        self.receiver()
        rec = self.client("nav")
        rec.close()
        rec.close()
        rec.state("base", "ai")
        self.assertEqual(rec.dropped, 1)

    def test_no_socket_at_all_drops_everything(self) -> None:
        original = autonometry_client.socket.socket

        def failing(*args: Any, **kwargs: Any) -> socket.socket:
            raise OSError("too many open files (simulated)")

        autonometry_client.socket.socket = failing  # type: ignore[assignment]
        try:
            rec = autonometry_client.Recorder(self.path, "nav")
        finally:
            autonometry_client.socket.socket = original  # type: ignore[misc]
        rec.state("base", "ai")
        self.assertEqual(rec.dropped, 1)


if __name__ == "__main__":
    unittest.main()
