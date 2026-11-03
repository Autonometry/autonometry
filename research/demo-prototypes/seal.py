"""Sealing prototype (trust level L2): hash chain + signed, externally anchored checkpoints.

* Canonical JSON is RFC 8785 (package `rfc8785`), so any language can re-verify.
* The chain starts from a domain-separated genesis bound to robot and session.
* Every `every` events (and at the end) the logger signs a checkpoint {origin, size, root, ts}
  with Ed25519 and sends a copy out at once (buyer, auditor, RFC 3161 TSA or transparency log).
  That outside copy is what stops the key holder from quietly rewriting history.
* Verification fails closed: missing hashes, broken links, bad signatures, a log shorter than or
  different from any anchored checkpoint, or a missing session_end are all failures.
"""
from __future__ import annotations

import hashlib

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DOMAIN = b"autonomy-meter/v0.1"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def genesis(robot_id: str, session_id: str) -> str:
    return _sha(DOMAIN + b"|" + robot_id.encode() + b"|" + session_id.encode())


def _body(e: dict) -> dict:
    return {k: v for k, v in e.items() if k != "hash"}


def seal(events: list[dict], key: Ed25519PrivateKey, every: int = 100) -> tuple[list[dict], list[dict]]:
    prev = genesis(events[0]["robot_id"], events[0]["session_id"])
    sealed, checkpoints = [], []
    for i, ev in enumerate(events, 1):
        e = {k: v for k, v in ev.items() if k not in ("hash", "prev_hash")} | {"prev_hash": prev}
        e["hash"] = prev = _sha(rfc8785.dumps(e))
        sealed.append(e)
        if i % every == 0 or i == len(events):
            cp = {"origin": f"{e['robot_id']}/{e['session_id']}", "size": i, "root": prev, "ts": e["ts"]}
            checkpoints.append(cp | {"sig": key.sign(rfc8785.dumps(cp)).hex()})
    return sealed, checkpoints


def verify(events: list[dict], pub: Ed25519PublicKey, anchored: list[dict]) -> tuple[bool, list[str]]:
    if not events:
        return False, ["empty log"]
    if any("hash" not in e or "prev_hash" not in e for e in events):
        return False, ["unsealed events present (fail closed)"]
    problems, roots = [], []
    prev = genesis(events[0]["robot_id"], events[0]["session_id"])
    for e in events:
        if e["prev_hash"] != prev:
            problems.append(f"seq {e['seq']}: broken link")
        h = _sha(rfc8785.dumps(_body(e)))
        if h != e["hash"]:
            problems.append(f"seq {e['seq']}: content does not match its hash")
        roots.append(h)
        prev = e["hash"]
    for cp in anchored:
        body = {k: v for k, v in cp.items() if k != "sig"}
        try:
            pub.verify(bytes.fromhex(cp["sig"]), rfc8785.dumps(body))
        except InvalidSignature:
            problems.append(f"checkpoint size {cp['size']}: bad signature")
        if cp["size"] > len(roots):
            problems.append(f"log has {len(roots)} events but an anchored checkpoint covers {cp['size']} (cut off)")
        elif roots[cp["size"] - 1] != cp["root"]:
            problems.append(f"history differs from anchored checkpoint at size {cp['size']} (rewritten)")
    if events[-1]["type"] != "session_end":
        problems.append("no session_end")
    return not problems, problems[:3] + ([f"... {len(problems) - 3} more"] if len(problems) > 3 else [])


def reference_chain(events: list[dict], event_hash) -> list[dict]:
    """The research package's plain chain (first prev_hash = None), for side-by-side attacks."""
    out, prev = [], None
    for ev in events:
        e = {k: v for k, v in ev.items() if k not in ("hash", "prev_hash")} | {"prev_hash": prev}
        e["hash"] = prev = event_hash(e)
        out.append(e)
    return out
