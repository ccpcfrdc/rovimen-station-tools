"""command_api.py — server->station command channel (reversed-HTTP push §3).

Server-side of the outbound long-poll command channel. In the push model a
station holds **no inbound port**; server->station actions ride a queue the
station polls. This blueprint exposes three routes mounted in the dashboard
process (mirroring ``ingest_api.py``):

    POST /api/fleet/<station>/commands            admin session  -> enqueue (signed)
    GET  /api/fleet/<station>/commands            station key    -> poll pending
    POST /api/fleet/<station>/commands/<id>/ack   station key    -> report result

Security (non-negotiable, §3.2 / §6):

  * **Enqueue is admin-only** (``@require_admin`` — the existing role gate). A
    ``host`` account cannot enqueue.
  * **Every command is ed25519-signed** by a VPS-held private key over the
    canonical ``(id|station|type|args|issued_at|not_after)`` message. The station
    verifies against the bundled public key and refuses anything that does not
    verify — so even someone who reaches the queue cannot forge a command. If the
    signing key is missing the enqueue **fails closed** (503), never emitting an
    unsigned command.
  * **Strict type allowlist** (:data:`command_store.ALLOWED_TYPES`): restart_service,
    reboot, patch_settings, lock_clip, trigger_upload, run_updater,
    restart_services. No ``exec``/shell type.
  * **Poll/ack authenticate with the same per-station key as ingest** — a key for
    ``gmn0002`` can only read/ack ``gmn0002``'s commands.
  * **Every enqueue + ack is audit-logged** via ``security.audit``.
  * Rate-limited like ingest.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from flask import Flask, Response, g, jsonify, request
from pydantic import BaseModel, Field, ValidationError

import command_signing
import command_store
import station_keys
from auth import require_admin

logger = logging.getLogger(__name__)

# Path-segment validation for <station> (same rule as ingest).
import re

_STATION_RE = re.compile(r"^[a-z0-9]{1,16}$")

# Rate budgets. The poll route is long-poll so it is naturally paced; keep the
# budget generous. Enqueue/ack are low-volume operator/station events.
_POLL_RATE = "120/minute;3000/hour"
_MUTATE_RATE = "60/minute;600/hour"

# Long-poll bounds (§3.1): server holds the connection up to this many seconds
# waiting for work, then returns an empty list so the station re-polls.
_MAX_WAIT_SEC = 25
_POLL_INTERVAL_SEC = 0.5

# Default command TTL if the caller does not specify not_after (§3.2 — minutes).
_DEFAULT_TTL_SEC = 300


class EnqueueBody(BaseModel):
    """Admin enqueue request. ``type`` is validated against the allowlist in the
    handler (not here) so a bad type yields a clear 400, not a schema 422."""

    model_config = {"extra": "ignore"}

    type: str
    args: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int | None = Field(default=None, ge=1, le=86400)
    not_after: str | None = None


class AckBody(BaseModel):
    model_config = {"extra": "ignore"}

    status: str = "ok"           # ok | error
    detail: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def register_command_routes(
    app: Flask,
    *,
    known_stations: Callable[[], set[str]] | None = None,
    command_db_path: Any = None,
    station_keys_path: Any = None,
    signing_key_path: Any = None,
    limiter: Any = None,
) -> None:
    """Mount the command-channel routes onto an existing Flask app.

    Args mirror ``ingest_api.register_ingest_routes``. ``signing_key_path`` points
    at the PEM ed25519 private key; a missing key makes enqueue fail closed (503).
    """
    cmd_path = command_db_path if command_db_path is not None else command_store.DB_PATH
    keys_path = station_keys_path if station_keys_path is not None else station_keys.DEFAULT_KEYS_PATH
    sign_path = signing_key_path if signing_key_path is not None else command_signing.DEFAULT_SIGNING_KEY_PATH

    try:
        command_store.ensure_schema(cmd_path)
    except Exception:
        logger.exception("command_api: could not initialise commands DB at %s", cmd_path)

    def _limiter_key() -> str:
        key = getattr(g, "station_key", None)
        if key is not None:
            return f"station:{key.id}"
        from security import _client_ip

        return _client_ip()

    def _rate_limited(rate: str) -> Callable:
        if limiter is None:
            return lambda fn: fn
        return limiter.limit(rate, key_func=_limiter_key)

    def _err(code: int, error: str, detail: str) -> Response:
        resp = jsonify({"error": error, "detail": detail})
        resp.status_code = code
        return resp

    def _validate_station(station: str) -> Response | None:
        if not _STATION_RE.match(station):
            return _err(404, "unknown_station", f"invalid station id: {station!r}")
        if known_stations is not None and station not in known_stations():
            return _err(404, "unknown_station", f"no such station: {station!r}")
        return None

    def _authorize_station_key(station: str) -> Response | None:
        """Auth gate for the station-key routes (poll/ack). Same rules as ingest:
        reject ?key=, accept X-Station-Key / Bearer, 401 unknown, 403 wrong
        station. Attaches the key to ``g`` for the rate limiter."""
        if "key" in request.args:
            return _err(
                400, "url_key_param_disabled",
                "station keys must be passed via the X-Station-Key header or "
                "Authorization: Bearer; the ?key= URL parameter is not accepted.",
            )
        secret = (
            request.headers.get("X-Station-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or ""
        ).strip()

        if not station_keys.is_required():
            key = station_keys.validate(secret, path=keys_path) if secret else None
            if key is not None:
                g.station_key = key
            return None

        key = station_keys.validate(secret, path=keys_path) if secret else None
        if key is None:
            resp = _err(
                401, "missing_or_invalid_station_key",
                "supply your station key via the X-Station-Key header or "
                "Authorization: Bearer <key>",
            )
            resp.headers["WWW-Authenticate"] = 'StationKey realm="rovimen-commands"'
            return resp
        g.station_key = key
        if key.station != station:
            return _err(
                403, "station_mismatch",
                f"this key authorises {key.station!r}, not {station!r}",
            )
        return None

    # ── JSON 429 for the command surface ──────────────────────────────
    # A 429 handler may already be registered (e.g. by ingest_api). Flask keeps
    # only one handler per code, so chain to the previous one for non-fleet paths
    # rather than clobbering its JSON body.
    _prev_429 = app.error_handler_spec.get(None, {}).get(429, {}).get(
        __import__("werkzeug").exceptions.TooManyRequests
    )

    @app.errorhandler(429)
    def _command_429(exc):  # noqa: ANN001
        path = request.path or ""
        if not path.startswith("/api/fleet/"):
            if _prev_429 is not None:
                return _prev_429(exc)
            return exc.get_response()
        retry_after = 60
        try:
            reset_at = getattr(exc, "reset_at", None) or getattr(
                getattr(exc, "limit", None), "reset_at", None
            )
            if reset_at:
                retry_after = max(1, int(reset_at - time.time()))
        except Exception:
            pass
        resp = jsonify({
            "error": "rate_limit_exceeded",
            "detail": str(getattr(exc, "description", "Too many requests")),
            "retry_after_seconds": retry_after,
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    # ── Enqueue (admin session) ───────────────────────────────────────
    @app.route("/api/fleet/<station>/commands", methods=["POST"])
    @require_admin
    @_rate_limited(_MUTATE_RATE)
    def fleet_enqueue_command(station: str):
        bad = _validate_station(station)
        if bad is not None:
            return bad
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return _err(422, "invalid_body", "body must be a JSON object")
        try:
            req = EnqueueBody.model_validate(body)
        except ValidationError as exc:
            return _err(422, "invalid_body", exc.errors(include_url=False).__str__())

        if req.type not in command_store.ALLOWED_TYPES:
            return _err(
                400, "disallowed_command_type",
                f"type {req.type!r} is not allowed; permitted: "
                f"{sorted(command_store.ALLOWED_TYPES)}",
            )

        now = datetime.now(timezone.utc)
        issued_at = _iso(now)
        if req.not_after:
            from command_store import _parse_ts

            na_dt = _parse_ts(req.not_after)
            if na_dt is None:
                return _err(400, "invalid_not_after", "not_after must be RFC3339")
            not_after = _iso(na_dt)
        else:
            ttl = req.ttl_seconds or _DEFAULT_TTL_SEC
            not_after = _iso(now + timedelta(seconds=ttl))

        # Fail closed: no signing key -> refuse to enqueue an unsigned command.
        try:
            priv = command_signing.load_private_key(sign_path)
        except FileNotFoundError:
            logger.error("command_api: signing key missing at %s — refusing enqueue", sign_path)
            return _err(
                503, "signing_unavailable",
                "command signing key is not configured on this server; "
                "commands cannot be issued (fail-closed)",
            )
        except Exception as exc:
            logger.exception("command_api: signing key load failed")
            return _err(503, "signing_unavailable", f"signing key error: {exc}")

        cmd_id = command_store.new_command_id()
        issued_by = getattr(g, "user", None) or (
            request.headers.get("X-Remote-User") or ""
        )
        try:
            from flask import session

            issued_by = session.get("user") or issued_by
        except Exception:
            pass

        msg = command_signing.canonical_message(
            id=cmd_id, station=station, type=req.type, args=req.args,
            issued_at=issued_at, not_after=not_after,
        )
        sig = command_signing.sign(msg, priv)

        command_store.enqueue(
            id=cmd_id, host_key=station, type=req.type, args=req.args,
            created_by=issued_by or "unknown", issued_at=issued_at,
            not_after=not_after, sig=sig, path=cmd_path,
        )

        from security import audit_request

        audit_request(
            "command_enqueue", station=station, command_id=cmd_id,
            type=req.type, issued_by=issued_by or "unknown", not_after=not_after,
        )

        resp = jsonify({
            "id": cmd_id, "station": station, "type": req.type,
            "issued_at": issued_at, "not_after": not_after, "status": "pending",
        })
        resp.status_code = 201
        return resp

    # ── Poll (station key, long-poll) ─────────────────────────────────
    @app.route("/api/fleet/<station>/commands", methods=["GET"])
    @_rate_limited(_POLL_RATE)
    def fleet_poll_commands(station: str):
        bad = _validate_station(station) or _authorize_station_key(station)
        if bad is not None:
            return bad

        wait = 0
        try:
            wait = max(0, min(_MAX_WAIT_SEC, int(request.args.get("wait", "0"))))
        except (TypeError, ValueError):
            wait = 0

        deadline = time.monotonic() + wait
        while True:
            pending = command_store.pending_for_station(station, path=cmd_path)
            if pending or time.monotonic() >= deadline:
                return jsonify({"station": station, "commands": pending})
            time.sleep(_POLL_INTERVAL_SEC)

    # ── Ack (station key) ─────────────────────────────────────────────
    @app.route("/api/fleet/<station>/commands/<cmd_id>/ack", methods=["POST"])
    @_rate_limited(_MUTATE_RATE)
    def fleet_ack_command(station: str, cmd_id: str):
        bad = _validate_station(station) or _authorize_station_key(station)
        if bad is not None:
            return bad
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return _err(422, "invalid_body", "body must be a JSON object")
        try:
            ack = AckBody.model_validate(body)
        except ValidationError as exc:
            return _err(422, "invalid_body", exc.errors(include_url=False).__str__())

        existing = command_store.get_command(cmd_id, path=cmd_path)
        if existing is None or existing.get("host_key") != station:
            return _err(404, "unknown_command", f"no command {cmd_id!r} for {station!r}")

        result = {"status": ack.status, "detail": ack.detail, **(ack.result or {})}
        updated = command_store.ack(cmd_id, station, result, path=cmd_path)

        from security import audit_request

        audit_request(
            "command_ack", station=station, command_id=cmd_id,
            result_status=ack.status, redelivery=not updated,
        )

        return jsonify({
            "ok": True, "id": cmd_id, "station": station,
            "acked": updated, "redelivery": not updated,
        })
