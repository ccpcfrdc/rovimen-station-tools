#!/usr/bin/env python3
"""command_verify.py — station-side ed25519 verification for the command channel.

The server->station command channel (``docs/reversed_http_push_design.md`` §3)
hands each station signed commands via an outbound long-poll. Before dispatching
*any* action the station must verify the ed25519 signature against the server's
**public** key, which ships baked into the signed deploy bundle (never fetched
from the VPS — the chicken-and-egg avoidance in §8). This module is that
verifier plus the canonical-message reconstruction.

**Fail closed.** If no public key is configured, :func:`load_public_key` returns
None and the worker refuses to act on any command. If the ``cryptography`` library
is present it is used; otherwise a small, self-contained RFC 8032 ed25519 verify
implementation (vendored below, verify-only, no secrets) is used so a station
with a minimal Python still verifies signatures rather than failing open.

The canonical message MUST be byte-identical to the server's
``command_signing.canonical_message`` — that shared contract is what makes a
tampered ``type``/``args``/``not_after`` fail verification.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("rovimen_pusher.command_verify")

# Public key path, baked into the deploy bundle. Overridable by env for tests.
DEFAULT_PUBKEY_PATH = Path(
    os.environ.get(
        "ROVIMEN_COMMAND_PUBKEY",
        str(Path.home() / "rovimen_scripts" / "command_pubkey.pem"),
    )
)


def canonical_message(
    *,
    id: str,
    station: str,
    type: str,
    args: dict[str, Any],
    issued_at: str,
    not_after: str,
) -> bytes:
    """Byte-identical to the server signer. See ``dashboard/command_signing.py``."""
    args_canon = json.dumps(args or {}, sort_keys=True, separators=(",", ":"))
    parts = [id, station, type, args_canon, issued_at, not_after]
    return ("v1\n" + "\n".join(parts)).encode("utf-8")


# ── ed25519 public-key loading (PEM SubjectPublicKeyInfo) ──────────────────


def _spki_pem_to_raw(pem_text: str) -> bytes | None:
    """Extract the 32-byte raw ed25519 public key from a PEM SPKI block.

    ed25519 SPKI DER is a fixed 44-byte structure whose last 32 bytes are the
    raw key: ``30 2a 30 05 06 03 2b 65 70 03 21 00 <32 bytes>``."""
    try:
        b64 = "".join(
            line.strip()
            for line in pem_text.splitlines()
            if line.strip() and not line.startswith("-----")
        )
        der = base64.b64decode(b64)
    except Exception:
        return None
    if len(der) == 44 and der[:12] == bytes.fromhex("302a300506032b6570032100"):
        return der[12:]
    # Fallback: raw 32-byte key on disk.
    if len(der) == 32:
        return der
    return None


def load_public_key_raw(path: Path = DEFAULT_PUBKEY_PATH) -> bytes | None:
    """Return the 32-byte raw ed25519 public key, or None (fail-closed)."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        logger.warning("no command public key at %s — command channel disabled", path)
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not read command public key %s: %s", path, exc)
        return None
    raw = _spki_pem_to_raw(text)
    if raw is None or len(raw) != 32:
        logger.error("command public key at %s is not a valid ed25519 SPKI key", path)
        return None
    return raw


def verify(message: bytes, sig_b64: str, pubkey_raw: bytes) -> bool:
    """True iff ``sig_b64`` is a valid ed25519 signature of ``message``.

    Prefers ``cryptography`` (constant-time, audited); falls back to the vendored
    verify-only implementation. Any error -> False (fail closed)."""
    if pubkey_raw is None or len(pubkey_raw) != 32:
        return False
    try:
        sig = base64.b64decode(sig_b64)
    except Exception:
        return False
    if len(sig) != 64:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature

        pk = Ed25519PublicKey.from_public_bytes(pubkey_raw)
        try:
            pk.verify(sig, message)
            return True
        except InvalidSignature:
            return False
    except Exception:
        # cryptography unavailable — use the vendored verifier.
        try:
            return _ed25519_verify(pubkey_raw, message, sig)
        except Exception:
            return False


def not_expired(not_after: str, now: datetime | None = None) -> bool:
    """True iff ``not_after`` (RFC3339) is in the future on the station clock."""
    now = now or datetime.now(timezone.utc)
    try:
        s = not_after.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return False
    return dt >= now


# ── Vendored RFC 8032 ed25519 verify (verify-only, no secret material) ─────
# Pure-Python fallback so a station without `cryptography` still verifies rather
# than failing open. Adapted from the reference implementation in RFC 8032.

_p = 2 ** 255 - 19
_d = (-121665 * pow(121666, _p - 2, _p)) % _p
_I = pow(2, (_p - 1) // 4, _p)
_L = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _p - 2, _p)


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = (x * _I) % _p
    if x % 2 != 0:
        x = _p - x
    return x


_By = (4 * _inv(5)) % _p
_Bx = _x_recover(_By)
_B = (_Bx % _p, _By % _p, 1, (_Bx * _By) % _p)


def _edwards_add(P, Q):  # type: ignore[no-untyped-def]
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = ((y1 - x1) * (y2 - x2)) % _p
    b = ((y1 + x1) * (y2 + x2)) % _p
    c = (t1 * 2 * _d * t2) % _p
    dd = (z1 * 2 * z2) % _p
    e = b - a
    f = dd - c
    g = dd + c
    h = b + a
    return ((e * f) % _p, (g * h) % _p, (f * g) % _p, (e * h) % _p)


def _scalarmult(P, e):  # type: ignore[no-untyped-def]
    if e == 0:
        return (0, 1, 1, 0)
    Q = _scalarmult(P, e // 2)
    Q = _edwards_add(Q, Q)
    if e & 1:
        Q = _edwards_add(Q, P)
    return Q


def _to_affine(P):  # type: ignore[no-untyped-def]
    x, y, z, _t = P
    zi = _inv(z)
    return ((x * zi) % _p, (y * zi) % _p)


def _decode_int(s: bytes) -> int:
    return int.from_bytes(s, "little")


def _decode_point(s: bytes):  # type: ignore[no-untyped-def]
    y = _decode_int(s) & ((1 << 255) - 1)
    if y >= _p:
        raise ValueError("bad point")
    x = _x_recover(y)
    if x & 1 != (s[31] >> 7) & 1:
        x = _p - x
    return (x % _p, y % _p, 1, (x * y) % _p)


def _ed25519_verify(pubkey: bytes, message: bytes, signature: bytes) -> bool:
    import hashlib

    if len(signature) != 64 or len(pubkey) != 32:
        return False
    R = _decode_point(signature[:32])
    A = _decode_point(pubkey)
    s = _decode_int(signature[32:])
    if s >= _L:
        return False
    h = _decode_int(hashlib.sha512(signature[:32] + pubkey + message).digest()) % _L
    sB = _to_affine(_scalarmult(_B, s))
    RhA = _to_affine(_edwards_add(R, _scalarmult(A, h)))
    return sB == RhA
