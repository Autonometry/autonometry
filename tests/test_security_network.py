"""Security review M10 (docs/security-review-2026-11.md): the timestamp authority client against a
server it should not trust. TLS is verified (a self-signed certificate is refused), and the
recorder never waits for the network on its write path."""

import datetime
import http.server
import ipaddress
import ssl
import threading
import time
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from autonometry import tsa
from autonometry.recorder.anchor import Anchorer


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


class _TlsServer(http.server.ThreadingHTTPServer):
    """Does each TLS handshake itself, closing a connection whose handshake fails (the client
    refusing the certificate), so the test server leaks no socket."""

    context: ssl.SSLContext

    def get_request(self) -> tuple[ssl.SSLSocket, object]:  # type: ignore[override]
        sock, address = self.socket.accept()
        try:
            return self.context.wrap_socket(sock, server_side=True), address
        except OSError:
            sock.close()
            raise


def _self_signed(folder: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def https_server(tmp_path: Path) -> Iterator[str]:
    cert, key = _self_signed(tmp_path)
    httpd = _TlsServer(("127.0.0.1", 0), _Handler)
    httpd.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    httpd.context.load_cert_chain(cert, key)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{httpd.server_port}/tsr"
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def test_a_tsa_with_a_certificate_nobody_trusts_is_refused(https_server: str) -> None:
    """TLS verification is on: the system's trust store does not hold a self-signed certificate,
    so no response is read from it."""
    with pytest.raises(urllib.error.URLError, match="CERTIFICATE_VERIFY_FAILED"):
        tsa._post_once(https_server, b"request", timeout=5.0, deadline_s=10.0)


def test_a_tsa_that_never_verifies_fails_with_one_line(https_server: str) -> None:
    with pytest.raises(tsa.TsaError, match="failed after 2 attempts"):
        tsa._post_with_retries(https_server, b"request", timeout=5.0, retry_delays=(0.0,))


def test_a_tsa_that_hangs_never_holds_up_a_checkpoint_writer() -> None:
    """The writer only submits; a request that never returns is the anchorer thread's alone."""
    release = threading.Event()

    def hanging(note: bytes) -> bytes:
        release.wait(30.0)
        raise TimeoutError("released")

    anchorer = Anchorer(hanging, lambda: None, give_up_after_s=3600.0)
    anchorer.start()
    began = time.perf_counter()
    for size in range(1000):
        anchorer.submit(Path("events.checkpoints.jsonl"), f"note {size}\n")
    assert time.perf_counter() - began < 1.0
    assert anchorer.take_results() == []
    release.set()
    anchorer.close()
