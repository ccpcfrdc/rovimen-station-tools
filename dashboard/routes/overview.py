"""Overview and status routes -- config/all, overview stacks, GMN data,
station status/vitals, SSE event stream.

Extracted from rovimen_dashboard.py.  All routes preserved verbatim --
same URLs, same behaviour, same decorators.
Wired in from ``create_app()`` via ``register_overview_routes``.

Closure state (overview stacks cache, status/vitals last-seen dicts,
kick/record helpers, GMN cache access, morning_done) is passed in from
the caller so that the main module and this module share the same
mutable instances.
"""

from __future__ import annotations

import logging
import os
import queue
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from flask import Flask, Response, abort, jsonify, request, session

from auth import (
    require_auth,
    require_admin,
    require_station,
    _session_role,
    _has_station_access,
    is_anonymous,
)
from gmn_poller import (
    _gmn_cache,
    _gmn_cache_lock,
    _gmn_fetch_multistation,
    _gmn_get_cached,
    _gmn_monthly_count,
)
from http_caching import _json_cached
from route_helpers import lookup_station
from security import public_route
from station_client import station_get_raw

logger = logging.getLogger(__name__)

_STATION_POLL_EXECUTOR = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="overview-poll"
)

_SSE_DEADLINE_S = 300  # 5 min — auto-close SSE to free the WSGI worker


def register_overview_routes(
    app: Flask,
    config,
    cache,
    tunnels,
    *,
    # Overview stacks cache
    overview_stacks_cache: dict[tuple[str, str], dict[str, Any]],
    overview_stacks_cache_lock: threading.Lock,
    overview_stacks_full_ts: list[float],  # mutable container: [monotonic_ts]
    overview_stacks_ttl: float,
    # Status / vitals last-seen
    status_last_seen: dict[str, tuple[float, dict[str, Any]]],
    vitals_last_seen: dict[str, tuple[float, dict[str, Any]]],
    status_last_seen_lock: threading.Lock,
    vitals_last_seen_lock: threading.Lock,
    last_seen_max_age: float,
    # Kick / record helpers
    kick_status_refresh,
    kick_vitals_refresh,
    record_status_last_seen,
    record_vitals_last_seen,
) -> None:
    limiter = app._limiter  # type: ignore[attr-defined]

    _require_station_impl = lookup_station

    # ── Config-all endpoint (parallel fetch from all stations) ───────────

    @app.route("/api/config/all")
    @require_admin
    def api_config_all():
        def fetch_one(host_key: str):
            try:
                cfg = station_get_raw(config, tunnels, host_key, "/api/settings", timeout=10)
                return host_key, {"ok": True, "config": cfg}
            except Exception as exc:
                return host_key, {"ok": False, "error": str(exc)}

        result: dict[str, Any] = {}
        futures = {
            _STATION_POLL_EXECUTOR.submit(fetch_one, hk): hk
            for hk in config.stations
        }
        for fut in as_completed(futures):
            hk, data = fut.result()
            result[hk] = data
        return jsonify(result)

    # ── Overview captured stacks (latest night per camera) ──────────────

    def _fetch_cam_stack(host_key: str, cam_code: str) -> dict | None:
        from cache_store import _drop_future_dates

        try:
            nights = station_get_raw(
                config, tunnels, host_key,
                f"/api/nights/{cam_code}", timeout=10,
            )
            if not isinstance(nights, list):
                nights = []
            nights = _drop_future_dates(nights)
            if not nights:
                return None
            for date in nights[:3]:
                try:
                    plots = station_get_raw(
                        config, tunnels, host_key,
                        f"/api/rms/plots/{cam_code}/{date}", timeout=10,
                    )
                    for p in (plots or []):
                        if "captured_stack" in p.get("filename", ""):
                            return {
                                "host": host_key,
                                "cam": cam_code,
                                "date": date,
                                "filename": p["filename"],
                                "label": p.get("label", "Captured stack"),
                            }
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _refresh_overview_stacks() -> None:
        """Background fan-out updater for overview_stacks_cache. NEVER blocks
        an HTTP request; called from a daemon thread by api_overview_stacks
        and at dashboard startup."""
        tasks = []
        for host_key, station in config.stations.items():
            # Skip ONLY if we explicitly know the station is offline.
            # Status=None means the status poller hasn't run yet (race
            # with startup). Attempt the fetch -- it'll fail fast if the
            # station really is unreachable, and the inner try/except
            # in _fetch_cam_stack handles that silently.
            status = cache.get_status(host_key)
            if status is not None and not status.get("online", False):
                continue  # keep prior cache entry for offline stations
            for cam in station.cameras:
                tasks.append(
                    (host_key, cam.code,
                     _STATION_POLL_EXECUTOR.submit(
                         _fetch_cam_stack, host_key, cam.code))
                )
        for host_key, cam_code, future in tasks:
            try:
                r = future.result()
            except Exception:
                r = None
            if r:
                with overview_stacks_cache_lock:
                    overview_stacks_cache[(host_key, cam_code)] = r
            # None -> keep last-known-good entry untouched

    # Stash on app so the prefetch loop in rovimen_dashboard.py can call it.
    app._refresh_overview_stacks = _refresh_overview_stacks  # type: ignore[attr-defined]

    @app.route("/api/overview/stacks")
    @public_route(page="overview")
    def api_overview_stacks():
        """Always returns cached entries instantly. If the cache is older
        than overview_stacks_ttl, spawns a background refresh thread (does
        NOT block the user's request). Subsequent calls within ~5-30 s
        will see the updated cache.

        Trade-off: first page-load after a fresh dashboard restart returns
        an empty list (cache empty), and the user has to retry to get
        populated data. The frontend handles this with a JS-side retry
        on empty response. The startup hook also kicks off one prefetch
        iteration synchronously in a thread so the cold window is short.

        Public under the "overview" page toggle (the fleet map's latest-night
        thumbnail strip needs it). Anonymous callers only ever get stacks from
        ``public: true`` stations; the shared cache stays full-fleet, so we
        drop non-public host_keys per request. Each cached entry carries only
        host_key/cam/date/filename/label — no ip/cam_ip/host-path — so the
        redaction is entirely a public-station filter.
        """
        now_mono = time.monotonic()
        if now_mono - overview_stacks_full_ts[0] >= overview_stacks_ttl:
            overview_stacks_full_ts[0] = now_mono
            threading.Thread(target=_refresh_overview_stacks, daemon=True).start()
        # Snapshot under the lock -- the refresh thread mutates the dict
        # concurrently, and an un-guarded list() can hit a "dictionary
        # changed size during iteration" RuntimeError mid-poll.
        with overview_stacks_cache_lock:
            snapshot = list(overview_stacks_cache.values())
        if is_anonymous():
            pub_hosts = {hk for hk, st in config.stations.items() if st.public}
            snapshot = [s for s in snapshot if s.get("host") in pub_hosts]
        # Heavy payload (~50-200 KB) that rarely changes minute-to-minute --
        # ETag lets the browser short-circuit to 304 on repeat polls.
        return _json_cached(snapshot, max_age=60)

    # ── GMN multi-station trajectories -- served from background cache ────

    @app.route("/api/gmn/multistation/<date>")
    def api_gmn_multistation(date: str):
        """Return GMN multi-station events for ``date``. Tries the in-memory
        cache first; on a miss, fetches on-demand via gmn_data so arbitrary
        historical dates work without waiting for the background poller."""
        payload = _gmn_get_cached(date)
        if not payload.get("events") and not payload.get("error"):
            try:
                payload = _gmn_fetch_multistation(date)
                with _gmn_cache_lock:
                    _gmn_cache[date] = payload
            except Exception:
                pass
        return _json_cached(payload, max_age=300)

    @app.route("/api/gmn/monthly_count")
    def api_gmn_monthly_count():
        """Sum has_ro_count across cached entries for a given month.
        Accepts ?month=YYYY-MM; defaults to current UTC month."""
        month_param = request.args.get("month", "")
        target: datetime | None = None
        if month_param:
            try:
                target = datetime.strptime(month_param + "-15", "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
        return _json_cached(_gmn_monthly_count(target), max_age=300)

    @app.route("/api/_debug/gmn/inject", methods=["POST"])
    def api_debug_gmn_inject():
        """Debug-only: inject a mock GMN event into the cache so the
        frontend can be exercised while the real upstream is rate-limited
        or otherwise unavailable. Body: { "date": "YYYY-MM-DD", "event": {...} }.
        Requires ROVIMEN_DEBUG_TOKEN env var; always 403 if unset."""
        expected = os.environ.get("ROVIMEN_DEBUG_TOKEN", "")
        token = request.headers.get("X-Debug-Token", "")
        if not expected or not secrets.compare_digest(expected, token):
            return jsonify({"error": "forbidden"}), 403
        body = request.get_json(force=True, silent=True) or {}
        date = body.get("date")
        event = body.get("event")
        if not date or not isinstance(event, dict):
            return jsonify({"error": "need {date, event}"}), 400
        with _gmn_cache_lock:
            entry = _gmn_cache.get(date) or {
                "date": date, "total_upstream": 0, "events": [],
                "has_ro_count": 0, "de_only_count": 0,
                "last_fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            # If the cache has an error stub, clear it so the frontend doesn't
            # paint "GMN data unavailable" over our mock data.
            entry.pop("error", None)
            entry["events"].append(event)
            if event.get("has_ro"):  entry["has_ro_count"]  += 1
            if event.get("de_only"): entry["de_only_count"] += 1
            entry["total_upstream"] = max(entry["total_upstream"], len(entry["events"]))
            # Survive the next poll cycle -- without this flag the backfill
            # would refetch this date and trample the mock with an error stub.
            entry["pinned"] = True
            _gmn_cache[date] = entry
        return jsonify({"ok": True, "events_for_date": len(entry["events"]), "pinned": True})

    # ── Overview endpoint (all stations health at a glance) ─────────────

    @app.route("/api/overview")
    @public_route(page="overview")
    def api_overview():
        is_admin = _session_role() == "admin"
        # Anonymous public visitors only ever see ``public: true`` stations —
        # commissioning / opted-out sites never appear in the public overview.
        # Logged-in accounts keep the full fleet (fleet-wide read rule).
        anon = is_anonymous()
        result = {}
        for key, station in config.stations.items():
            if anon and not station.public:
                continue
            status = cache.get_status(key) or {}
            vitals = cache.get_vitals(key) or {}
            result[key] = {
                "label": station.label,
                **({"ip": station.ip} if is_admin else {}),
                "cameras": [
                    {"code": c.code, **({"cam_ip": c.cam_ip} if is_admin else {}),
                     "rotate": c.rotate, "az": c.az, "alt": c.alt}
                    for c in station.cameras
                ],
                "online": status.get("online", False),
                "services": status.get("services", {}),
                "disk": status.get("disk"),
                "cpu_pct": vitals.get("cpu_pct"),
                "ram_pct": vitals.get("ram_pct"),
                "temp_c": vitals.get("temp_c"),
                "last_updated": status.get("last_updated"),
                "lat": station.lat,
                "lon": station.lon,
                "show_on_map": station.show_on_map,
                "status": station.status,
            }
        return _json_cached(result, max_age=30)

    # ── Config endpoint (single source of truth for JS) ───────────────────

    @app.route("/api/stations")
    @public_route
    def api_stations():
        # Plain @public_route (no page key): this is the static station
        # metadata registry — label, camera codes/pointing, coords, public
        # tabs — that EVERY public page (events, showers, station, highlights)
        # needs to label + place detections, independent of whether the
        # fleet-map "overview" toggle is on. Decoupling it from page="overview"
        # lets an operator expose events/showers/highlights WITHOUT enabling
        # the live fleet map. It still applies the same anon safeguards as the
        # rest of the surface: the sensitive cam_ip stays admin-only, and
        # anonymous visitors see ONLY ``public: true`` stations — the same
        # opt-in set the versioned /api/public/v1 surface exposes.
        is_admin = _session_role() == "admin"
        anon = is_anonymous()
        result = {}
        for key, station in config.stations.items():
            if anon and not station.public:
                continue
            result[key] = {
                "label": station.label,
                "cameras": [
                    {"code": c.code, **({"cam_ip": c.cam_ip} if is_admin else {}),
                     "rotate": c.rotate, "az": c.az, "alt": c.alt}
                    for c in station.cameras
                ],
                "lat": station.lat,
                "lon": station.lon,
                "show_on_map": station.show_on_map,
                "public_tabs": station.public_tabs,
            }
        # The non-admin response is identical for everyone, so it's cacheable;
        # the admin variant carries cam_ip, so don't let it be cached/shared.
        if is_admin:
            return jsonify(result)
        return _json_cached(result, max_age=300)

    # ── Cached proxy endpoints ────────────────────────────────────────────

    @app.route("/api/status/<host_key>")
    @require_station
    def api_status(host_key: str):
        _require_station_impl(config, host_key)
        data = cache.get_status(host_key)
        if data is not None:
            record_status_last_seen(host_key, data)
            return jsonify(data)
        # Cold miss: never hold the request worker for a 15 s SSH-tunneled
        # fetch. Kick a coalesced background refresh and reply with the
        # most recent known-good snapshot (flagged stale) or a cold-start
        # stub the frontend already handles.
        kick_status_refresh(host_key)
        with status_last_seen_lock:
            stale = status_last_seen.get(host_key)
        if stale is not None and (time.time() - stale[0]) < last_seen_max_age:
            payload = dict(stale[1])
            payload["stale"] = True
            return jsonify(payload)
        return jsonify({"online": False, "stale": True, "reason": "cold-start"})

    @app.route("/api/vitals/<host_key>")
    @require_station
    def api_vitals(host_key: str):
        _require_station_impl(config, host_key)
        data = cache.get_vitals(host_key)
        if data is not None:
            record_vitals_last_seen(host_key, data)
            return jsonify(data)
        kick_vitals_refresh(host_key)
        with vitals_last_seen_lock:
            stale = vitals_last_seen.get(host_key)
        if stale is not None and (time.time() - stale[0]) < last_seen_max_age:
            payload = dict(stale[1])
            payload["stale"] = True
            return jsonify(payload)
        return jsonify({"online": False, "stale": True, "reason": "cold-start"})

    # ── Aggregator endpoints: collapse N per-station calls into one ───────
    # Browser-side fan-out across stations is a hot path for users on
    # high-latency links (e.g. Romania -> Falkenstein, ~70 ms RTT). One
    # aggregator call saves N x RTT plus N x TCP/TLS connection overhead.
    # Backed by the same StationCache the per-host routes use -- no extra load.

    @app.route("/api/status/all")
    @public_route(page="overview")
    def api_status_all():
        # Anonymous public visitors on the fleet map only ever see the online
        # flag (plus label/coords, which are already public metadata) for
        # ``public: true`` stations — never the raw station status dict, which
        # can carry host-internal service names, disk mounts, and per-poll
        # timing that must not leak. Logged-in accounts keep the full-fleet,
        # full-fidelity status they rely on for ops.
        if is_anonymous():
            result: dict[str, Any] = {}
            for key, station in config.stations.items():
                if not station.public:
                    continue
                status = cache.get_status(key) or {}
                result[key] = {
                    "online": bool(status.get("online", False)),
                    "label": station.label,
                    "lat": station.lat,
                    "lon": station.lon,
                    "show_on_map": station.show_on_map,
                }
            return _json_cached(result, max_age=30)
        keys = [k for k in config.stations if _has_station_access(k)]
        result = {key: (cache.get_status(key) or {}) for key in keys}
        return _json_cached(result, max_age=30)

    @app.route("/api/vitals/all")
    @require_auth
    def api_vitals_all():
        keys = [k for k in config.stations if _has_station_access(k)]
        result = {key: (cache.get_vitals(key) or {}) for key in keys}
        return _json_cached(result, max_age=15)

    # ── Server-Sent Events: push station status diffs instead of polling ───
    # The aggregator above gets re-fetched by every connected dashboard every
    # 60 s. Station status is mostly stationary (services up/down, disk pct,
    # online flag), so 95%+ of those round-trips return an identical body --
    # they're polling waste. This endpoint flips the model: the client opens
    # one long-lived stream, gets a `snapshot` event on connect, then receives
    # a `status` event only when a station's state actually changes.
    #
    # Trade-offs:
    #   - Each SSE connection holds a WSGI worker for up to _SSE_DEADLINE_S
    #     seconds before auto-closing (issue #329). The browser reconnects
    #     transparently via EventSource's built-in retry logic.
    #     Next step if needed: ``gunicorn --worker-class=gevent`` turns each
    #     connection into a greenlet (~4 KB) instead of a thread (~8 MB stack).
    #     If we ever move to gunicorn with multiple sync workers, a single
    #     in-process pub/sub set won't fan out across workers -- we'd need
    #     an external bus (redis pub/sub, etc.).
    #   - If a client disconnects mid-yield, Flask raises GeneratorExit on the
    #     next yield, not immediately. The 25-s heartbeat ensures we hit a
    #     yield boundary within at most 25 s of disconnect, so the listener is
    #     unregistered promptly. The ``finally`` block is the safety net.
    #   - Slow consumers are dropped at the queue level (set_status never
    #     blocks). If a listener queue stays full, the affected client misses
    #     events between drops -- they'll resync via the client-side
    #     fetchAllStatus() fallback or the next legitimate change.
    @app.route("/api/events/status")
    def api_events_status_stream():
        # Cap-check up front: we still need the cap to gate the 503 response,
        # but we DO NOT hold the registration across the Response construction.
        # If we did and Flask never started iterating gen() (e.g. client
        # disconnected before the first byte, or a middleware short-circuited
        # the response), the finally inside gen() would never run and the
        # listener would leak. Instead we register at the very top of gen() --
        # by the time we're inside the generator's try, iteration has begun
        # and the finally clause is guaranteed to execute on close.
        if not cache.can_register_status_listener():
            # Listener cap hit -- tell the client to back off. EventSource's
            # default reconnect (3 s) will keep retrying; once an existing
            # listener disconnects, the next attempt gets through.
            return Response(
                "listener cap reached",
                status=503,
                mimetype="text/plain",
                headers={"Retry-After": "10"},
            )

        def gen():
            listener = cache.register_status_listener()
            if listener is None:
                # Racy cap hit between can_register_status_listener() and
                # the actual register call. End the stream cleanly so the
                # client retries.
                return
            try:
                # Initial snapshot so the client doesn't need a separate
                # /api/status/all fetch on top of subscribing. Encoded once
                # and shared across listeners that arrive at the same cache
                # version (reconnect-storm protection).
                yield cache.snapshot_bytes(tuple(config.stations))
                deadline = time.monotonic() + _SSE_DEADLINE_S
                while time.monotonic() < deadline:
                    try:
                        # 25 s < typical proxy idle timeout (nginx 60 s default,
                        # cloudflare 100 s). A `: ping` comment keeps the TCP
                        # connection warm without producing a parseable event.
                        yield listener.get(timeout=25)
                    except queue.Empty:
                        yield b": ping\n\n"
                # Deadline reached -- close with a retry hint so the browser
                # reconnects in 3 s (short enough to be imperceptible).
                yield b"retry: 3000\nevent: reconnect\ndata: deadline\n\n"
            except GeneratorExit:
                # Client disconnected -- fall through to finally.
                pass
            finally:
                cache.unregister_status_listener(listener)

        return Response(
            gen(),
            mimetype="text/event-stream",
            headers={
                # No caching for an infinite stream; intermediate proxies
                # MUST NOT buffer (nginx-specific header but harmless
                # elsewhere). Connection: keep-alive is implicit for HTTP/1.1
                # but spelled out for clarity.
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
