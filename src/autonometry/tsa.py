"""RFC 3161 timestamp client and pure token verification (definitions.md v0.3 section 10.5;
research/methods/trust.md section 2.5).

The HTTP round trip (:func:`request_timestamp`) is the only network call this package makes
outside a witness, and it never runs on a recorder's write path (CLAUDE.md product rule 1).
Verifying a stored token (:func:`verify_token`) is pure: no clock, no network, no state but the
trusted TSA roots. Certificates are checked at the token's own ``gen_time``, never at the time
verification runs -- ``rfc3161_client`` does this itself (see ``tests/test_tsa.py`` for the
pinning test that guards it).
"""

import contextlib
import http.client
import logging
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import IO, NoReturn, override

import rfc3161_client as rfc3161
from cryptography import x509

from autonometry import __version__

logger = logging.getLogger(__name__)

_CONTENT_TYPE = "application/timestamp-query"
_ACCEPT_TYPE = "application/timestamp-reply"
_TIMEOUT_S = 10.0
_ATTEMPT_DEADLINE_S = 30.0
_MAX_RESPONSE_BYTES = 65536
_RETRY_DELAYS_S = (1.0, 2.0)
_ALLOWED_SCHEMES = ("http", "https")
_SHA256_OID = x509.ObjectIdentifier("2.16.840.1.101.3.4.2.1")


class TsaError(Exception):
    """A timestamp request or token could not be used. The message is one line."""


class _Deadline:
    """Shuts down the sockets of one attempt once its total time is up, so a server that sends
    its answer slowly cannot hold an attempt open past the deadline (each socket read alone is
    only bounded by the socket timeout)."""

    def __init__(self, seconds: float) -> None:
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self.expired = False
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def track(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.append(sock)
            if self.expired:
                _shutdown(sock)

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            for sock in self._sockets:
                _shutdown(sock)

    def __enter__(self) -> "_Deadline":
        self._timer.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._timer.cancel()


def _shutdown(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):  # already closed: nothing left to interrupt
        sock.shutdown(socket.SHUT_RDWR)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every HTTP redirect: a TSA answers at the URL the user configured or not at all."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> NoReturn:
        fp.close()
        raise TsaError(f"{req.full_url} answered with a redirect (HTTP {code}); refused")


def _opener(deadline: _Deadline) -> urllib.request.OpenerDirector:
    """An opener that refuses redirects and hands every connected socket to ``deadline``."""
    context = ssl.create_default_context()

    class _Connection(http.client.HTTPConnection):
        def connect(self) -> None:
            super().connect()
            deadline.track(self.sock)

    class _TlsConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            super().connect()
            deadline.track(self.sock)

    class _Http(urllib.request.HTTPHandler):
        def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
            return self.do_open(_Connection, req)

    class _Https(urllib.request.HTTPSHandler):
        def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
            return self.do_open(_TlsConnection, req, context=context)

    return urllib.request.build_opener(_Http(), _Https(context=context), _NoRedirect())


def _post_once(
    url: str, body: bytes, timeout: float = _TIMEOUT_S, deadline_s: float = _ATTEMPT_DEADLINE_S
) -> bytes:
    """POST ``body`` once; at most ``timeout`` seconds per socket operation and ``deadline_s`` in
    total. The scheme is checked by :func:`request_timestamp` before this is ever called."""
    request = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={
            "Content-Type": _CONTENT_TYPE,
            "Accept": _ACCEPT_TYPE,
            "User-Agent": f"autonometry/{__version__}",
        },
        method="POST",
    )
    late = f"{url} gave no complete response within {deadline_s:g} s"
    with _Deadline(deadline_s) as deadline:
        try:
            with _opener(deadline).open(request, timeout=timeout) as response:
                data: bytes = response.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            if deadline.expired:
                raise TimeoutError(late) from exc
            raise
        if deadline.expired:
            raise TimeoutError(late)
    if len(data) > _MAX_RESPONSE_BYTES:
        raise TsaError(f"{url} returned a response larger than {_MAX_RESPONSE_BYTES} bytes")
    return data


def _post_with_retries(
    url: str,
    body: bytes,
    timeout: float = _TIMEOUT_S,
    retry_delays: Sequence[float] = _RETRY_DELAYS_S,
    deadline_s: float = _ATTEMPT_DEADLINE_S,
) -> bytes:
    attempts = len(retry_delays) + 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        if attempt > 0:
            time.sleep(retry_delays[attempt - 1])
        try:
            return _post_once(url, body, timeout, deadline_s)
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
            logger.warning(
                "timestamp request to %s failed (attempt %d/%d): %s",
                url,
                attempt + 1,
                attempts,
                exc,
            )
    raise TsaError(f"timestamp request to {url} failed after {attempts} attempts: {last_error}")


def request_timestamp(
    note: bytes, tsa_url: str, trusted_roots: Sequence[x509.Certificate]
) -> bytes:
    """Request, check and return a fresh RFC 3161 token over ``note`` (definitions.md 10.4, 10.5).

    Checks the response's nonce against the request (replay detection) and verifies it against
    ``trusted_roots`` with :func:`verify_token` before returning, so a caller never writes a
    token that does not hold up.

    Raises:
        TsaError: ``tsa_url`` is not ``http://`` or ``https://``, every attempt failed, the
            response is oversize, or the token does not verify.
    """
    scheme = urllib.parse.urlsplit(tsa_url).scheme
    if scheme not in _ALLOWED_SCHEMES:
        raise TsaError(f"refusing to contact {tsa_url!r}: only http:// and https:// are allowed")
    request = (
        rfc3161.TimestampRequestBuilder()
        .data(note)
        .hash_algorithm(rfc3161.HashAlgorithm.SHA256)
        .nonce(nonce=True)
        .cert_request(cert_request=True)
        .build()
    )
    body = _post_with_retries(tsa_url, request.as_bytes())
    try:
        response = rfc3161.decode_timestamp_response(body)
    except ValueError as exc:
        raise TsaError(f"{tsa_url} returned a token that cannot be decoded: {exc}") from None
    if response.tst_info.nonce != request.nonce:
        raise TsaError(f"{tsa_url} returned a token with the wrong nonce (possible replay)")
    verification = verify_token(body, note, trusted_roots)
    if not verification.ok:
        raise TsaError(f"{tsa_url} returned a token that does not verify: {verification.detail}")
    return body


@dataclass(frozen=True, slots=True)
class TsaVerification:
    """Result of verifying one RFC 3161 token (definitions.md section 10.5, 10.6)."""

    ok: bool
    gen_time: datetime | None
    detail: str


def verify_token(
    tsr: bytes, note: bytes, trusted_roots: Sequence[x509.Certificate]
) -> TsaVerification:
    """Verify an RFC 3161 token over ``note`` against trusted TSA root certificates
    (definitions.md 10.5, evidence code ``bad_timestamp_token``).

    Pure: never touches the network or the wall clock. The certificate chain is checked at the
    token's own ``gen_time`` (what the underlying library does), so this never depends on when
    verification happens to run.
    """
    try:
        response = rfc3161.decode_timestamp_response(tsr)
    except ValueError as exc:
        return TsaVerification(False, None, f"cannot decode timestamp token: {exc}")
    if not trusted_roots:
        return TsaVerification(False, None, "no trusted TSA root certificates")
    builder = rfc3161.VerifierBuilder()
    for root in trusted_roots:
        builder = builder.add_root_certificate(root)
    try:
        ok = builder.build().verify_message(response, note)
    except rfc3161.errors.VerificationError as exc:
        return TsaVerification(False, None, f"timestamp token does not verify: {exc}")
    return TsaVerification(ok, response.tst_info.gen_time, "")


def check_imprint(tsr: bytes, note_sha256: str) -> str | None:
    """Check that ``tsr`` decodes and that its token's message imprint is the SHA-256 given in
    ``note_sha256`` (definitions.md 10.6, evidence code ``bad_timestamp_token``).

    Pure, and independent of any trusted root: a token that fails this check is evidence, while
    one that passes it but does not verify against the trusted roots is only a gap.

    Returns:
        ``None`` when the token passes, else a one-line reason.
    """
    try:
        imprint = rfc3161.decode_timestamp_response(tsr).tst_info.message_imprint
    except ValueError as exc:
        return f"timestamp token cannot be decoded: {exc}"
    if imprint.hash_algorithm != _SHA256_OID:
        return "timestamp token's message imprint is not SHA-256"
    if imprint.message.hex() != note_sha256:
        return "timestamp token's message imprint is not its note_sha256"
    return None


def load_tsa_roots(pem: bytes) -> list[x509.Certificate]:
    """Parse one or more concatenated PEM certificates (a ``--tsa-root`` file).

    Raises:
        TsaError: the data holds no valid PEM certificate.
    """
    try:
        certs = x509.load_pem_x509_certificates(pem)
    except ValueError as exc:
        raise TsaError(f"not a valid PEM certificate file: {exc}") from None
    if not certs:
        raise TsaError("no certificates found")
    return certs
