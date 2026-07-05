"""Ed25519 signing for the server->station command channel.

The command channel (``docs/reversed_http_push_design.md`` §3) lets an admin
enqueue an action for a station; the station long-polls the queue, **verifies the
signature**, and only then dispatches an allowlisted action. This module is the
VPS-side signer.

Threat model (§3.2, §6): the VPS holds an ed25519 **command-signing private
key**; every station ships the matching **public** key baked into its signed
deploy bundle. The station refuses to act on any command whose ``sig`` does not
verify, whose ``type``/``args`` were tampered, or whose ``not_after`` has passed.
Crucially the private key **signs, it does not connect** — a VPS compromise lets
an attacker sign admin-equivalent commands (already an admin power) but yields no
shell on any station and cannot exceed the allowlisted command types.

The signed message is a **canonical** serialisation of the command's identity
fields so the station recomputes exactly the same bytes:

    canonical(id, station, type, args, issued_at, not_after)

``args`` is canonicalised with ``json.dumps(sort_keys=True, separators=(",",":"))``
so key ordering can never change the signed bytes. The station verifier
(``rovimen-scripts/command_verify.py``) implements byte-identical canonicalisation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

# Path to the PEM-encoded ed25519 private signing key on the VPS (0600,
# rovimen:rovimen). Never leaves the VPS. Only the public half ships to stations.
DEFAULT_SIGNING_KEY_PATH = Path(
    os.environ.get("ROVIMEN_COMMAND_SIGNING_KEY", "/opt/rovimen/command_signing_key.pem")
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
    """Deterministic bytes signed/verified for a command.

    The station recomputes this identically before verifying ``sig``. ``args`` is
    serialised with sorted keys + compact separators so ordering is irrelevant.
    """
    args_canon = json.dumps(args or {}, sort_keys=True, separators=(",", ":"))
    parts = [id, station, type, args_canon, issued_at, not_after]
    # Newline-join with a field count prefix so no field boundary is ambiguous.
    return ("v1\n" + "\n".join(parts)).encode("utf-8")


def load_private_key(path: Path = DEFAULT_SIGNING_KEY_PATH) -> Ed25519PrivateKey:
    """Load the PEM ed25519 private key from ``path``.

    Raises FileNotFoundError if absent — the enqueue endpoint treats a missing
    signing key as a hard 503 (fail closed: never enqueue an unsigned command)."""
    data = path.read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an ed25519 private key")
    return key


def sign(message: bytes, private_key: Ed25519PrivateKey) -> str:
    """Return base64(ed25519 signature) of ``message``."""
    return base64.b64encode(private_key.sign(message)).decode("ascii")


def verify(message: bytes, sig_b64: str, public_key: Ed25519PublicKey) -> bool:
    """True iff ``sig_b64`` is a valid ed25519 signature of ``message`` (server-side
    self-check / tests). The station uses its own vendored verifier."""
    from cryptography.exceptions import InvalidSignature

    try:
        public_key.verify(base64.b64decode(sig_b64), message)
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


def generate_keypair(private_path: Path, public_path: Path) -> None:
    """Generate a fresh ed25519 keypair, writing PEM private (0600) + public.

    Used by ``tools/gen_command_key.py``. The public PEM is what ships in the
    deploy bundle to every station; the private PEM never leaves the VPS.
    """
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(priv_pem)
    try:
        os.chmod(private_path, 0o600)
    except OSError:
        logger.warning("could not chmod 0600 on %s", private_path)
    public_path.write_bytes(pub_pem)


def public_key_pem(private_key: Ed25519PrivateKey) -> str:
    """Return the PEM public key text for a loaded private key (bundle export)."""
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
