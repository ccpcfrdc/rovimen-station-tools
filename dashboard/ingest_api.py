"""Reversed-HTTP push ingest API for the ROVIMEN dashboard (Phase A).

Stations POST their own telemetry to the VPS over HTTPS instead of the VPS
polling into :7779. See ``docs/reversed_http_push_design.md`` §2. This module is
the VPS-side ingest surface: a thin Flask blueprint-style set of routes mounted
in the same process as the dashboard (exactly like ``public_api.py``), sharing
``detection_db``, the in-process ``StationCache``, and the durable
``station_state`` mirror without any IPC.

Phase A is **additive**: pushed data lands in the *same* caches and tables the
existing pollers (``station_client.py``, ``index_poller.py``) write, so the
dashboard reads either source transparently. No poller is modified and no
station-side code ships in this phase.

Endpoints (all POST, all under ``/api/ingest/v1/<station>/``)::

    heartbeat    status/heartbeat  → StationCache.set_status + station_state
    vitals       vitals            → StationCache.set_vitals + station_state
    detections   detection batch   → detection_db.upsert_detections(source="push")
    media        timelapse/stack/clip pointers → station_state.media_pointers
    live_thumb   newest-FF maxpixel WebP (base64) → StationCache.set_live_thumb

Auth (§2.2): ``X-Station-Key`` (or ``Authorization: Bearer``). A key authorises
exactly one ``host_key``; a request whose path ``<station>`` differs from the
key's station is 403. No/invalid key is 401. Idempotency (§2.3): every body
carries a monotonic ``seq``; ``seq <= last_seq`` for that station is 200-and-drop.
"""

from __future__ import annotations

import base64
import binascii
import functools
import logging
import re
from typing import Any, Callable

from flask import Flask, Response, g, jsonify, request
from pydantic import BaseModel, Field, ValidationError

import detection_db
import station_keys
import station_state as station_state_mod

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"

# Path-segment validation. host_key is lowercase alnum (gmn0002, gmnro10).
_STATION_RE = re.compile(r"^[a-z0-9]{1,16}$")
# Camera codes are uppercase alnum (RO000M, DE001E) — matches station_api.
_CAM_RE = re.compile(r"^[A-Z0-9]{1,16}$")

# Live-thumbnail bytes cap. A BW maxpixel WebP is a few KB; 512 KB is a generous
# ceiling that still bounds a misbehaving station's per-POST body.
_MAX_LIVE_THUMB_BYTES = 512 * 1024
_ALLOWED_THUMB_TYPES = {"image/webp", "image/jpeg", "image/png"}

# Per-key + per-IP rate budgets, consistent with public_api.py. Heartbeat/vitals
# fire every 30-60 s per station; detections/media are bursty on dawn-process, so
# the default is generous. A looping or compromised station is throttled in
# isolation (§7 backpressure) — it can never starve other stations.
_INGEST_RATE = "120/minute;3000/hour"
_DETECTIONS_RATE = "60/minute;1000/hour"

# Cap on a single detection batch — a runaway station can't push an unbounded
# body that pins the SQLite writer.
_MAX_DETECTIONS = 5000
_MAX_MEDIA_POINTERS = 5000


# ── Envelope + payload schemas (§2.4) ────────────────────────────────────


class Envelope(BaseModel):
    """Common wrapper for every ingest body (§2.4).

    ``payload`` is validated per-endpoint. ``seq`` drives idempotency; ``sent_at``
    is advisory (display / TTL) — ordering is by ``seq``, never wall-clock.
    """

    model_config = {"extra": "ignore"}

    seq: int = Field(ge=0)
    sent_at: str | None = None
    agent_version: str | None = None
    payload: dict[str, Any]


class HeartbeatPayload(BaseModel):
    """§2.4.1 — mirrors GET /api/status diff fields. Extra keys tolerated so a
    newer agent can add fields without a VPS bump."""

    model_config = {"extra": "allow"}

    online: bool = True
    services: dict[str, str] = Field(default_factory=dict)
    cameras: list[dict[str, Any]] = Field(default_factory=list)
    disk: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)
    extra_disks: list[dict[str, Any]] = Field(default_factory=list)
    rms_running: bool | None = None
    color_capture_running: bool | None = None
    network_name: str | None = None


class VitalsPayload(BaseModel):
    """§2.4.2 — mirrors GET /api/vitals."""

    model_config = {"extra": "allow"}

    cpu_pct: float | None = None
    ram_pct: float | None = None
    ram_used_mb: float | None = None
    ram_total_mb: float | None = None
    temp_c: float | None = None
    total_cores: int | None = None
    cores: list[dict[str, Any]] = Field(default_factory=list)
    disk_write_mbps: float | None = None
    top_procs: list[dict[str, Any]] = Field(default_factory=list)


class DetectionRow(BaseModel):
    """§2.4.3 — one detection. Columns match detection_db.DETECTION_COLS so
    ingest is a pass-through to upsert_detections. ``source`` is set by the
    handler, not the station."""

    model_config = {"extra": "ignore"}

    cam: str
    date: str
    ff_file: str
    meteor_no: int = 1
    time_utc: str | None = None
    jd: float | None = None
    solar_lon: float | None = None
    shower: str | None = None
    mag_apparent: float | None = None
    mag_absolute: float | None = None
    duration_s: float | None = None
    ra_beg: float | None = None
    dec_beg: float | None = None
    ra_end: float | None = None
    dec_end: float | None = None
    ra_radiant: float | None = None
    dec_radiant: float | None = None
    radiant_elev: float | None = None
    angular_velocity: float | None = None
    num_segments: int | None = None
    fps: float | None = None
    azim_beg: float | None = None
    elev_beg: float | None = None
    azim_end: float | None = None
    elev_end: float | None = None
    chunk_file: str | None = None


class DetectionsPayload(BaseModel):
    model_config = {"extra": "ignore"}

    window_since: str | None = None
    detections: list[DetectionRow] = Field(default_factory=list, max_length=_MAX_DETECTIONS)


class TimelapsePointer(BaseModel):
    model_config = {"extra": "allow"}

    cam: str
    date: str
    filename: str
    night_stack: str | None = None


class NightstackPointer(BaseModel):
    model_config = {"extra": "allow"}

    cam: str
    date: str
    filename: str


class ClipPointer(BaseModel):
    model_config = {"extra": "allow"}

    cam: str
    date: str
    filename: str
    locked: bool | None = None
    meteor_time: str | None = None


class MediaPayload(BaseModel):
    model_config = {"extra": "ignore"}

    timelapses: list[TimelapsePointer] = Field(default_factory=list, max_length=_MAX_MEDIA_POINTERS)
    nightstacks: list[NightstackPointer] = Field(default_factory=list, max_length=_MAX_MEDIA_POINTERS)
    clips: list[ClipPointer] = Field(default_factory=list, max_length=_MAX_MEDIA_POINTERS)


class LiveThumbPayload(BaseModel):
    """§9 — a single newest-FF BW maxpixel WebP, base64 in the envelope.

    The image is tiny and ephemeral (newest FF only, no archival value), so
    unlike archival media (bytes→box + pointer→VPS) the bytes ride the JSON body
    directly and are cached latest-per-(station,cam) in memory on the VPS."""

    model_config = {"extra": "ignore"}

    content_type: str = "image/webp"
    ff_timestamp: str | None = None
    data_b64: str


# ── Registration ─────────────────────────────────────────────────────────


def register_ingest_routes(
    app: Flask,
    *,
    cache_set_status: Callable[[str, dict[str, Any]], None],
    cache_set_vitals: Callable[[str, dict[str, Any]], None],
    cache_set_live_thumb: Callable[..., None] | None = None,
    known_stations: Callable[[], set[str]] | None = None,
    detections_db_path: Any = None,
    station_state_path: Any = None,
    station_keys_path: Any = None,
    limiter: Any = None,
) -> None:
    """Mount the ingest API onto an existing Flask app.

    Args:
        app: The Flask application to attach routes to.
        cache_set_status: ``StationCache.set_status`` — the *same* writer the
            status poll thread uses, so a pushed heartbeat fans out over the
            existing SSE channel unchanged (coexistence).
        cache_set_vitals: ``StationCache.set_vitals``.
        cache_set_live_thumb: ``StationCache.set_live_thumb`` — stores the latest
            pushed newest-FF maxpixel WebP per (station, cam) in memory (§9).
            When None, the live_thumb ingest route is not mounted (tests may pass
            a stub); the on-demand :7779 pull remains the fallback either way.
        known_stations: Optional callable returning the set of valid host_keys
            (from ``dashboard_config.yaml``). When provided, a POST to an
            unknown station 404s before touching the key store. When None, any
            path that passes the regex is accepted (tests / standalone).
        detections_db_path / station_state_path / station_keys_path: path
            overrides for the respective stores (tests). Default to each
            module's configured path.
        limiter: Flask-Limiter instance from ``security.init_limiter``. When
            None (tests / standalone), rate limiting is a no-op.
    """
    det_path = detections_db_path if detections_db_path is not None else detection_db.DB_PATH
    st_path = station_state_path if station_state_path is not None else station_state_mod.DB_PATH
    keys_path = station_keys_path if station_keys_path is not None else station_keys.DEFAULT_KEYS_PATH

    # Ensure both schemas exist so a pushed batch never hits a missing table on
    # a fresh install. In production the index poller / bootstrap already create
    # the detections table; this is idempotent and just guarantees ordering.
    try:
        detection_db.open_db(det_path).close()
    except Exception:
        logger.exception("ingest_api: could not initialise detections DB at %s", det_path)
    try:
        station_state_mod.ensure_schema(st_path)
    except Exception:
        logger.exception("ingest_api: could not initialise station_state DB at %s", st_path)

    def _limiter_key() -> str:
        """Rate-limit identity: authenticated station key id, else client IP."""
        key = getattr(g, "station_key", None)
        if key is not None:
            return f"station:{key.id}"
        from security import _client_ip

        return _client_ip()

    def _rate_limited(rate: str) -> Callable:
        if limiter is None:
            return lambda fn: fn

        def _effective_limit() -> str:
            key = getattr(g, "station_key", None)
            if key is not None and key.rate_limit_override:
                return key.rate_limit_override
            return rate

        return limiter.limit(_effective_limit, key_func=_limiter_key)

    def _err(code: int, error: str, detail: str) -> Response:
        resp = jsonify({"error": error, "detail": detail})
        resp.status_code = code
        return resp

    def _authorize(station: str) -> Response | None:
        """Auth gate for an ingest route (§2.2).

        Returns an error Response to short-circuit, or None on success (having
        attached the matching :class:`station_keys.StationKey` to ``g`` for the
        rate limiter). ``?key=`` in the query string is rejected outright — it
        leaks into access logs / referers, same rule as the public API.
        """
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
            # Soft-mode (dev/test): attach the key if supplied so per-key rate
            # limiting still works, but do not reject.
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
            resp.headers["WWW-Authenticate"] = 'StationKey realm="rovimen-ingest"'
            return resp
        g.station_key = key
        if key.station != station:
            # Authenticated but wrong station: a valid key can only write its
            # own host_key's state. This is the core §2.2 property.
            return _err(
                403, "station_mismatch",
                f"this key authorises {key.station!r}, not {station!r}",
            )
        return None

    def _validate_station(station: str) -> Response | None:
        if not _STATION_RE.match(station):
            return _err(404, "unknown_station", f"invalid station id: {station!r}")
        if known_stations is not None and station not in known_stations():
            return _err(404, "unknown_station", f"no such station: {station!r}")
        return None

    def _parse_envelope() -> tuple[Envelope | None, Response | None]:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return None, _err(422, "invalid_body", "body must be a JSON object")
        try:
            env = Envelope.model_validate(body)
        except ValidationError as exc:
            return None, _err(422, "invalid_envelope", exc.errors(include_url=False).__str__())
        return env, None

    def _accepted(seq: int, *, dropped: bool = False, **extra: Any) -> Response:
        resp = jsonify({"ok": True, "seq": seq, "dropped": dropped, **extra})
        resp.status_code = 200
        return resp

    # ── JSON 429 for the ingest surface ───────────────────────────────
    @app.errorhandler(429)
    def _ingest_429(exc):  # noqa: ANN001
        path = request.path or ""
        if not path.startswith("/api/ingest/v1/"):
            return exc.get_response()
        retry_after = 60
        try:
            reset_at = getattr(exc, "reset_at", None) or getattr(
                getattr(exc, "limit", None), "reset_at", None
            )
            if reset_at:
                import time as _time

                retry_after = max(1, int(reset_at - _time.time()))
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

    # ── Index (unauthenticated discovery) ─────────────────────────────
    @app.route("/api/ingest/v1", methods=["GET"])
    @app.route("/api/ingest/v1/", methods=["GET"])
    def ingest_index():
        return jsonify({
            "service": "rovimen-ingest-api",
            "schema_version": SCHEMA_VERSION,
            "endpoints": {
                "heartbeat":  "/api/ingest/v1/<station>/heartbeat",
                "vitals":     "/api/ingest/v1/<station>/vitals",
                "detections": "/api/ingest/v1/<station>/detections",
                "media":      "/api/ingest/v1/<station>/media",
                "live_thumb": "/api/ingest/v1/<station>/live_thumb/<cam>",
            },
            "auth": "X-Station-Key header (or Authorization: Bearer)",
        })

    # ── Heartbeat / status ────────────────────────────────────────────
    @app.route("/api/ingest/v1/<station>/heartbeat", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_heartbeat(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = HeartbeatPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", exc.errors(include_url=False).__str__())

        # Atomic seq check-and-bump (§2.3). On the Redis path this is a single
        # Lua op — the per-push seq write never touches the single-writer SQLite
        # DB. Returns False for a replay/older seq, which we 200-and-drop.
        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay")

        status = payload.model_dump()
        # Stamp last_seen so downstream (staleness badge, public API) has it.
        if env.sent_at:
            status.setdefault("last_updated", env.sent_at)
        # Same writer the poll thread uses — preserves the SSE diff/fan-out.
        cache_set_status(station, status)
        station_state_mod.record_status(station, env.seq, status, env.sent_at, path=st_path)
        return _accepted(env.seq)

    # ── Vitals ─────────────────────────────────────────────────────────
    @app.route("/api/ingest/v1/<station>/vitals", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_vitals(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = VitalsPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", exc.errors(include_url=False).__str__())

        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay")

        vitals = payload.model_dump()
        cache_set_vitals(station, vitals)
        station_state_mod.record_vitals(station, env.seq, vitals, env.sent_at, path=st_path)
        return _accepted(env.seq)

    # ── Detections ─────────────────────────────────────────────────────
    @app.route("/api/ingest/v1/<station>/detections", methods=["POST"])
    @_rate_limited(_DETECTIONS_RATE)
    def ingest_detections(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = DetectionsPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", exc.errors(include_url=False).__str__())

        # Detections are PK-idempotent (cam,date,ff_file,meteor_no) regardless of
        # seq, so a stale-seq replay is still safe — but honour the envelope for
        # consistency and to spare the writer a no-op batch. Atomic decide+bump.
        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay", written=0)

        rows = [d.model_dump() for d in payload.detections]
        written = detection_db.upsert_detections(rows, source="push", path=det_path)
        return _accepted(env.seq, written=written)

    # ── Media pointers ─────────────────────────────────────────────────
    @app.route("/api/ingest/v1/<station>/media", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_media(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = MediaPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", exc.errors(include_url=False).__str__())

        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay", written=0)

        pointers: list[dict[str, Any]] = []
        for tl in payload.timelapses:
            pointers.append({"kind": "timelapse", **tl.model_dump()})
        for ns in payload.nightstacks:
            pointers.append({"kind": "nightstack", **ns.model_dump()})
        for clip in payload.clips:
            pointers.append({"kind": "clip", **clip.model_dump()})

        written = station_state_mod.upsert_media_pointers(station, pointers, path=st_path)
        return _accepted(env.seq, written=written)

    # ── Live thumbnail (newest-FF maxpixel WebP) ───────────────────────
    if cache_set_live_thumb is not None:

        @app.route("/api/ingest/v1/<station>/live_thumb/<cam>", methods=["POST"])
        @_rate_limited(_INGEST_RATE)
        def ingest_live_thumb(station: str, cam: str):
            bad = _validate_station(station) or _authorize(station)
            if bad is not None:
                return bad
            if not _CAM_RE.match(cam):
                return _err(404, "invalid_cam", f"invalid camera code: {cam!r}")
            env, err = _parse_envelope()
            if err is not None:
                return err
            try:
                payload = LiveThumbPayload.model_validate(env.payload)
            except ValidationError as exc:
                return _err(422, "invalid_payload", exc.errors(include_url=False).__str__())

            if payload.content_type not in _ALLOWED_THUMB_TYPES:
                return _err(
                    422, "invalid_content_type",
                    f"content_type must be one of {sorted(_ALLOWED_THUMB_TYPES)}",
                )
            try:
                raw = base64.b64decode(payload.data_b64, validate=True)
            except (binascii.Error, ValueError):
                return _err(422, "invalid_base64", "data_b64 is not valid base64")
            if not raw:
                return _err(422, "empty_thumb", "decoded thumbnail is empty")
            if len(raw) > _MAX_LIVE_THUMB_BYTES:
                return _err(
                    413, "thumb_too_large",
                    f"thumbnail exceeds {_MAX_LIVE_THUMB_BYTES} bytes",
                )

            # Seq-guarded like every ingest body (§2.3): a stale/replayed thumb is
            # 200-and-dropped so an out-of-order POST can't overwrite a newer one.
            # Atomic decide+bump so a race can't let a stale frame win.
            if not station_state_mod.accept_seq(station, env.seq, path=st_path):
                return _accepted(env.seq, dropped=True, reason="replay")

            cache_set_live_thumb(
                station, cam, raw,
                content_type=payload.content_type,
                ff_timestamp=payload.ff_timestamp,
            )
            return _accepted(env.seq, bytes=len(raw))
