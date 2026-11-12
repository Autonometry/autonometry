"""Tests for autonometry.tsa (definitions.md v0.3 section 10.5; research/methods/trust.md 2.5).

The HTTP client is tested against a local ``http.server``; token verification is tested against
a real response recorded once from FreeTSA (``tests/fixtures/tsa/``). No test here uses the
internet.
"""

import datetime as datetime_module
import hashlib
import http.server
import inspect
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonometry import tsa
from autonometry.reader import read_logs
from sealing_helpers import KEY_NAME, SEED_KEY, seal_sessions

FIXTURES = Path(__file__).parent / "fixtures" / "tsa"
GOLDEN_LOG = Path(__file__).parent / "golden" / "sealed" / "log.jsonl"
GOLDEN_NOTE_SHA256 = "10a64865168cb368b725f406a6ec1985ab1971f969afc3262927e6177996a372"

# A background HTTPServer thread can close its socket after a test has already moved on (seen
# with dropped connections and the deliberate timeout below); pytest then attributes the resulting
# ResourceWarning to whatever test runs next. Harmless test-harness noise, not a product warning.
pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")


_HANG_SECONDS = 0.5
_CLIENT_TIMEOUT = 0.15

# A request's response script step is one of: bytes (200 OK with that body), _DROP (drop the
# connection at once, a fast simulated failure), None (hang past the client's timeout),
# _Redirect (answer 302) or _Drip (send the body one byte at a time).
_DROP = b""


class _Redirect:
    pass


class _Drip:
    def __init__(self, body: bytes, interval_s: float) -> None:
        self.body = body
        self.interval_s = interval_s


class _Handler(http.server.BaseHTTPRequestHandler):
    script: ClassVar[list[object]] = []
    requests: ClassVar[int] = 0

    def log_message(self, *args: object) -> None:  # quiet test output
        pass

    def do_POST(self) -> None:
        _Handler.requests += 1
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        step = _Handler.script.pop(0) if _Handler.script else b"ok"
        if step is None:
            time.sleep(_HANG_SECONDS)
        elif step == _DROP:
            self.close_connection = True
        elif isinstance(step, _Redirect):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif isinstance(step, _Drip):
            self._send_headers(len(step.body))
            for byte in step.body:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(step.interval_s)
        else:
            body = step if isinstance(step, bytes) else b""
            self._send_headers(len(body))
            self.wfile.write(body)

    def _send_headers(self, length: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/timestamp-reply")
        self.send_header("Content-Length", str(length))
        self.end_headers()


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.script = []
    _Handler.requests = 0
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/tsr"
    finally:
        httpd.shutdown()
        thread.join()


def test_post_once_round_trip(server: str) -> None:
    _Handler.script = [b"hello response"]
    assert tsa._post_once(server, b"request body") == b"hello response"


def test_post_once_rejects_oversize_response(server: str) -> None:
    _Handler.script = [b"x" * (tsa._MAX_RESPONSE_BYTES + 1)]
    with pytest.raises(tsa.TsaError, match="larger than"):
        tsa._post_once(server, b"request body")


def test_post_once_times_out(server: str) -> None:
    _Handler.script = [None]
    with pytest.raises(OSError, match=r"timed out|timeout"):
        tsa._post_once(server, b"request body", timeout=_CLIENT_TIMEOUT)
    time.sleep(_HANG_SECONDS)  # let the hung handler thread finish before the next test runs


def test_post_with_retries_succeeds_after_failures(server: str) -> None:
    _Handler.script = [_DROP, _DROP, b"third time lucky"]
    result = tsa._post_with_retries(
        server, b"request body", timeout=_CLIENT_TIMEOUT, retry_delays=(0.01, 0.01)
    )
    assert result == b"third time lucky"


def test_post_with_retries_raises_after_exhausting_attempts(server: str) -> None:
    _Handler.script = [None, None, None]
    with pytest.raises(tsa.TsaError, match="failed after 3 attempts"):
        tsa._post_with_retries(
            server, b"request body", timeout=_CLIENT_TIMEOUT, retry_delays=(0.01, 0.01)
        )
    time.sleep(_HANG_SECONDS)  # let the hung handler threads finish before the next test runs


def test_post_once_enforces_a_total_deadline_on_a_slow_drip(server: str) -> None:
    _Handler.script = [_Drip(b"x" * 40, 0.05)]  # 2 s in total, each byte well inside the timeout
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r"no complete response within 0\.3 s"):
        tsa._post_once(server, b"request body", timeout=1.0, deadline_s=0.3)
    assert time.monotonic() - started < 1.5
    time.sleep(2.2)  # let the dripping handler thread finish before the next test runs


def test_request_timestamp_refuses_a_scheme_other_than_http_or_https(server: str) -> None:
    url = server.replace("http://", "ftp://")
    with pytest.raises(tsa.TsaError, match="only http"):
        tsa.request_timestamp(b"note", url, [])
    assert _Handler.requests == 0


def test_request_timestamp_refuses_a_redirect(server: str) -> None:
    _Handler.script = [_Redirect()]
    with pytest.raises(tsa.TsaError, match=r"redirect \(HTTP 302\); refused"):
        tsa.request_timestamp(b"note", server, [])
    assert _Handler.requests == 1  # refused at once, not retried


def test_request_timestamp_refuses_a_replayed_response(server: str, golden_tsr: bytes) -> None:
    _Handler.script = [golden_tsr]  # a real token, but for another request: the nonce differs
    with pytest.raises(tsa.TsaError, match="wrong nonce"):
        tsa.request_timestamp(b"note", server, [])


def test_request_timestamp_refuses_an_undecodable_response(server: str) -> None:
    _Handler.script = [b"not a timestamp response"]
    with pytest.raises(tsa.TsaError, match="cannot be decoded"):
        tsa.request_timestamp(b"note", server, [])


def test_check_imprint_accepts_the_matching_sha256(golden_tsr: bytes) -> None:
    assert tsa.check_imprint(golden_tsr, GOLDEN_NOTE_SHA256) is None


def test_check_imprint_rejects_another_sha256(golden_tsr: bytes) -> None:
    assert tsa.check_imprint(golden_tsr, "0" * 64) == (
        "timestamp token's message imprint is not its note_sha256"
    )


def test_check_imprint_rejects_an_undecodable_token() -> None:
    assert (tsa.check_imprint(b"junk", "0" * 64) or "").startswith("timestamp token cannot be")


def test_check_imprint_rejects_a_non_sha256_imprint(
    golden_tsr: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha512 = x509.ObjectIdentifier("2.16.840.1.101.3.4.2.3")
    imprint = SimpleNamespace(hash_algorithm=sha512, message=bytes(64))
    fake = SimpleNamespace(tst_info=SimpleNamespace(message_imprint=imprint))
    monkeypatch.setattr(tsa.rfc3161, "decode_timestamp_response", lambda _data: fake)
    assert tsa.check_imprint(golden_tsr, "0" * 64) == (
        "timestamp token's message imprint is not SHA-256"
    )


# --- Verification against a real, recorded FreeTSA response --------------------------------------


@pytest.fixture(scope="module")
def freetsa_root() -> list[x509.Certificate]:
    return tsa.load_tsa_roots((FIXTURES / "freetsa_root.pem").read_bytes())


@pytest.fixture(scope="module")
def golden_tsr() -> bytes:
    return (FIXTURES / "golden_checkpoint.tsr").read_bytes()


@pytest.fixture(scope="module")
def golden_note() -> bytes:
    log_check = read_logs([GOLDEN_LOG], "0.3")
    _, notes = seal_sessions(log_check.sessions, key=SEED_KEY, key_name=KEY_NAME)
    note = next(n for n in notes if n.startswith("autonometry/r1/s2\n3\n"))
    assert hashlib.sha256(note.encode("utf-8")).hexdigest() == GOLDEN_NOTE_SHA256, (
        "the golden log or sealing code changed: the committed TSA fixture no longer matches"
    )
    return note.encode("utf-8")


def test_verify_token_against_real_freetsa_response(
    golden_tsr: bytes, golden_note: bytes, freetsa_root: list[x509.Certificate]
) -> None:
    result = tsa.verify_token(golden_tsr, golden_note, freetsa_root)
    assert result.ok
    assert result.gen_time == datetime(2026, 11, 3, 15, 11, 28, tzinfo=UTC)


def test_verify_token_rejects_flipped_byte(
    golden_tsr: bytes, golden_note: bytes, freetsa_root: list[x509.Certificate]
) -> None:
    flipped = bytearray(golden_tsr)
    flipped[-10] ^= 0xFF
    result = tsa.verify_token(bytes(flipped), golden_note, freetsa_root)
    assert not result.ok


def test_verify_token_rejects_mismatched_message(
    golden_tsr: bytes, freetsa_root: list[x509.Certificate]
) -> None:
    result = tsa.verify_token(golden_tsr, b"this is not the timestamped note", freetsa_root)
    assert not result.ok


def test_verify_token_rejects_wrong_root(golden_tsr: bytes, golden_note: bytes) -> None:
    unrelated = _self_signed_cert()
    result = tsa.verify_token(golden_tsr, golden_note, [unrelated])
    assert not result.ok


def test_verify_token_rejects_when_no_root_is_trusted(
    golden_tsr: bytes, golden_note: bytes
) -> None:
    result = tsa.verify_token(golden_tsr, golden_note, [])
    assert not result.ok
    assert "no trusted TSA root" in result.detail


def test_verify_token_rejects_undecodable_token(
    golden_note: bytes, freetsa_root: list[x509.Certificate]
) -> None:
    result = tsa.verify_token(b"not a timestamp token", golden_note, freetsa_root)
    assert not result.ok
    assert "cannot decode" in result.detail


@pytest.mark.parametrize("data", [b"", b"\x00\x01\x02random garbage bytes that is not DER at all"])
def test_verify_token_never_crashes_on_garbage(
    data: bytes, freetsa_root: list[x509.Certificate]
) -> None:
    result = tsa.verify_token(data, b"note", freetsa_root)
    assert not result.ok


def _self_signed_cert() -> x509.Certificate:
    key = Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "unrelated test root")])
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime_module.timedelta(days=1))
        .not_valid_after(now + datetime_module.timedelta(days=1))
        .sign(key, None)
    )


def test_verify_token_never_reads_the_wall_clock() -> None:
    """Pin: ``rfc3161_client`` checks a token's certificate chain at the token's own ``gen_time``,
    never at the time verification runs (confirmed by reading its source: it passes
    ``tst_info.gen_time`` as the PKCS7 verification time, not ``datetime.now()``). That is why
    this fixture (``test_verify_token_against_real_freetsa_response``) never expires as its own
    certificates age. A static check pins our side of that contract: :func:`tsa.verify_token`
    must never call a wall-clock function itself, so this cannot regress unnoticed.
    """
    source = inspect.getsource(tsa)
    assert "datetime.now(" not in source
    assert "time.time()" not in source
    assert "utcnow(" not in source


def test_fixture_certificates_would_otherwise_be_judged_by_gen_time(
    freetsa_root: list[x509.Certificate],
) -> None:
    """The fixture's own token was generated in the past relative to its (long-lived) root, so a
    verifier that checked wall-clock time instead of ``gen_time`` would currently agree with one
    that checks ``gen_time`` -- this only documents that the two certificates span today, which
    is why the real regression guard is :func:`test_verify_token_never_reads_the_wall_clock`."""
    root = freetsa_root[0]
    assert root.not_valid_before_utc <= datetime.now(UTC) <= root.not_valid_after_utc
