"""RFC 3161 timestamp client and pure token verification (definitions.md v0.2 section 10.5;
research/methods/trust.md section 2.5).

The HTTP round trip (:func:`request_timestamp`) is the only network call this package makes
outside a witness, and it never runs on a recorder's write path (CLAUDE.md product rule 1).
Verifying a stored token (:func:`verify_token`) is pure: no clock, no network, no state but the
trusted TSA roots. Certificates are checked at the token's own ``gen_time``, never at the time
verification runs -- ``rfc3161_client`` does this itself (see ``tests/test_tsa.py`` for the
pinning test that guards it).
"""

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import rfc3161_client as rfc3161
from cryptography import x509

from autonometry import __version__

logger = logging.getLogger(__name__)

_CONTENT_TYPE = "application/timestamp-query"
_ACCEPT_TYPE = "application/timestamp-reply"
_TIMEOUT_S = 10.0
_MAX_RESPONSE_BYTES = 65536
_RETRY_DELAYS_S = (1.0, 2.0)
_ALLOWED_SCHEMES = ("http", "https")


class TsaError(Exception):
    """A timestamp request or token could not be used. The message is one line."""


def _post_once(url: str, body: bytes, timeout: float = _TIMEOUT_S) -> bytes:
    # Scheme is checked by request_timestamp before this is ever called.
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
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        data: bytes = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(data) > _MAX_RESPONSE_BYTES:
        raise TsaError(f"{url} returned a response larger than {_MAX_RESPONSE_BYTES} bytes")
    return data


def _post_with_retries(
    url: str,
    body: bytes,
    timeout: float = _TIMEOUT_S,
    retry_delays: Sequence[float] = _RETRY_DELAYS_S,
) -> bytes:
    attempts = len(retry_delays) + 1
    last_error: OSError | None = None
    for attempt in range(attempts):
        if attempt > 0:
            time.sleep(retry_delays[attempt - 1])
        try:
            return _post_once(url, body, timeout)
        except OSError as exc:
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
