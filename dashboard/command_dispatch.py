"""command_dispatch.py — route station mutations to the signed command channel.

Bridges the dashboard's existing station-op routes (reboot, restart, settings
PATCH, lock, trigger-upload) to the reversed-HTTP command channel
(``docs/reversed_http_push_design.md`` §3). A station carrying
``push_enabled: true`` in ``dashboard_config.yaml`` holds no inbound port, so the
dashboard can no longer POST/PATCH its :7779 API directly. Instead it enqueues a
**signed** command the station's CommandWorker long-polls, verifies, and
dispatches to its own loopback API.

Design:

  * ``should_push(config, host_key)`` — True iff the station is marked
    ``push_enabled``. Every route uses this to branch; when False the route keeps
    its existing direct-HTTP behaviour byte-for-byte.
  * ``enqueue_signed_command(...)`` — signs the canonical message with the
    VPS-held ed25519 private key and persists a pending row via ``command_store``.
    Fail-closed: a missing signing key raises so the route surfaces a 503 rather
    than silently dropping the action.
  * ``queued_response(...)`` — the uniform 202 JSON body the routes return when a
    command was queued, carrying the command id so the caller can poll status.

The type allowlist and canonical message are shared with ``command_api.py`` /
``command_store.py`` — there is deliberately no exec/shell path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Response, jsonify

import command_signing
import command_store

logger = logging.getLogger(__name__)

# Default TTL for a dashboard-issued command if the caller does not set one.
# Mirrors command_api._DEFAULT_TTL_SEC — operator actions are short-lived.
DEFAULT_TTL_SEC = 300


class SigningUnavailable(RuntimeError):
    """Raised when the command signing key is missing/unloadable.

    Routes map this to a 503 (fail-closed): never enqueue an unsigned command."""


def should_push(config: Any, host_key: str) -> bool:
    """True iff ``host_key`` is a known station with ``push_enabled`` set.

    Falsy (default) for every station today, so callers keep the direct-HTTP
    path unless a station has been explicitly cut over to push."""
    station = config.stations.get(host_key)
    return bool(station is not None and getattr(station, "push_enabled", False))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def enqueue_signed_command(
    *,
    host_key: str,
    type: str,
    args: dict[str, Any] | None = None,
    issued_by: str = "dashboard",
    ttl_seconds: int = DEFAULT_TTL_SEC,
    signing_key_path: Path | None = None,
    command_db_path: Path | None = None,
) -> str:
    """Sign and enqueue a command for ``host_key``; return the new command id.

    ``type`` must be in :data:`command_store.ALLOWED_TYPES` (the caller has
    already chosen an allowlisted type). Raises :class:`ValueError` on a bad type
    and :class:`SigningUnavailable` if the signing key cannot be loaded."""
    if type not in command_store.ALLOWED_TYPES:
        raise ValueError(f"command type {type!r} is not allowed")

    args = args or {}
    sign_path = signing_key_path or command_signing.DEFAULT_SIGNING_KEY_PATH
    cmd_path = command_db_path or command_store.DB_PATH

    try:
        priv = command_signing.load_private_key(sign_path)
    except FileNotFoundError as exc:
        raise SigningUnavailable(
            f"command signing key missing at {sign_path}"
        ) from exc
    except Exception as exc:  # malformed key etc.
        raise SigningUnavailable(f"signing key error: {exc}") from exc

    now = datetime.now(timezone.utc)
    issued_at = _iso(now)
    not_after = _iso(now + timedelta(seconds=max(1, ttl_seconds)))
    cmd_id = command_store.new_command_id()

    msg = command_signing.canonical_message(
        id=cmd_id, station=host_key, type=type, args=args,
        issued_at=issued_at, not_after=not_after,
    )
    sig = command_signing.sign(msg, priv)

    command_store.enqueue(
        id=cmd_id, host_key=host_key, type=type, args=args,
        created_by=issued_by or "dashboard", issued_at=issued_at,
        not_after=not_after, sig=sig, path=cmd_path,
    )
    logger.info("queued signed command %s (%s) for %s", cmd_id, type, host_key)
    return cmd_id


def queued_response(host_key: str, type: str, cmd_id: str) -> tuple[Response, int]:
    """Uniform 202 body for a queued command: ``(jsonify(...), 202)``."""
    body = jsonify({
        "queued": True,
        "command_id": cmd_id,
        "station": host_key,
        "type": type,
        "detail": "action queued on the signed command channel; the station will "
                  "execute it on its next command poll",
    })
    return body, 202


def signing_unavailable_response() -> tuple[Response, int]:
    """Uniform 503 body when signing is unavailable (fail-closed)."""
    body = jsonify({
        "error": "signing_unavailable",
        "detail": "command signing key is not configured on this server; "
                  "commands cannot be issued (fail-closed)",
    })
    return body, 503
