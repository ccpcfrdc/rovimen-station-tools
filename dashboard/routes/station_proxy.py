"""Station proxy routes -- timelapses, settings, hardware, platepar.

Extracted from rovimen_dashboard.py as part of the route-splitting effort.
All routes preserved verbatim -- same URLs, same behaviour, same decorators.
Wired in from ``create_app()`` via ``register_station_proxy_routes``.

Closure state (``_proxy_cache``, ``_proxy_cache_lock``, SWR caches,
platepar cache) is passed in from the caller so that the main module
and this module share the same cache instances.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from typing import Any, Callable

from flask import Flask, abort, jsonify, request

import command_dispatch
from auth import require_station, station_is_public_for_request
from http_caching import _json_cached
from route_helpers import (
    lookup_station,
    invalidate_proxy_cache,
    proxy_error_response,
    proxy_get,
)
from security import public_route
from station_client import station_url, station_get_raw, _session_for_url
from tunnels import _TunnelDown

logger = logging.getLogger(__name__)

_STATION_POLL_EXECUTOR = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="timelapse-poll"
)


def register_station_proxy_routes(
    app: Flask,
    config,
    tunnels,
    cache,
    *,
    timelapses_swr_cache: dict[str, tuple[float, dict[str, Any]]],
    timelapses_swr_inflight: set[str],
    timelapses_swr_lock: threading.Lock,
    timelapses_fresh_ttl: float,
    platepar_cache: dict[str, tuple[float, Any]],
    platepar_ttl: float,
    proxy_cache: dict[tuple[str, str], tuple[float, Any]],
    proxy_cache_lock: threading.Lock,
    prefetch_executor: ThreadPoolExecutor,
    archive_idx,
) -> Callable[[str], dict[str, Any]]:
    """Register station proxy routes and return ``_get_timelapses_swr``.

    The returned accessor is the SWR-cached timelapses lookup, needed by
    ``public_api.register_public_routes`` as ``get_timelapses_payload``.
    """
    _require_station_impl = lookup_station
    _invalidate_proxy_cache_impl = invalidate_proxy_cache
    _proxy_error_response_impl = proxy_error_response
    _proxy_get_impl = proxy_get

    # ── Timelapses (SWR-cached, archive-merged) ─────────────────────────

    def _walk_archive_timelapses(cam_code: str) -> list[dict]:
        out: list[dict] = []
        for date in archive_idx.nights(cam_code):
            ni = archive_idx.night_files(cam_code, date)
            if ni is None:
                continue
            mp4s = sorted(f for f in ni.timelapse_files if f.endswith(".mp4"))
            if not mp4s:
                continue
            webp_names = {f for f in ni.timelapse_files if f.endswith(".webp")}
            mp4 = mp4s[0]
            stack_name = mp4.replace("_timelapse.mp4", "_night_stack.webp")
            out.append({
                "date": date,
                "filename": mp4,
                "night_stack": stack_name if stack_name in webp_names else None,
                "source": "archive",
            })
        return out

    def _compute_timelapses_payload(host_key: str) -> dict[str, Any]:
        status = cache.get_status(host_key)
        online = (status is None) or bool((status or {}).get("online", False))
        try:
            if online:
                data = station_get_raw(config, tunnels, host_key, "/api/timelapses")
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
        except Exception:
            data = {}
        # Deep-copy before we mutate. Even though station_get_raw returns a
        # fresh dict from resp.json(), the very next caller (_refresh_timelapses
        # / _get_timelapses_swr) stores this object in timelapses_swr_cache,
        # and _json_cached caches the serialized body by id(payload). Mutating
        # the dict in place would let a subsequent request hit the stale
        # serialized body under the same id — same ETag, wrong content. A
        # fresh dict here keeps the in-place mutations local to this call.
        data = copy.deepcopy(data)
        station_obj = config.stations.get(host_key)
        cam_codes = [c.code for c in station_obj.cameras] if station_obj else []
        # Walks for different cameras hit independent SSHFS subtrees and
        # are otherwise independent, so fan them out in parallel. A 5-cam
        # station (Raul) used to take up to 5 × 30 s of serial SSHFS
        # walking on a cold mount; now bounded by the slowest single walk.
        archive_results: dict[str, list[dict]] = {}
        if cam_codes:
            future_to_cam = {
                _STATION_POLL_EXECUTOR.submit(_walk_archive_timelapses, c): c
                for c in cam_codes
            }
            try:
                for fut in as_completed(future_to_cam, timeout=15):
                    cam_code = future_to_cam[fut]
                    try:
                        archive_results[cam_code] = fut.result() or []
                    except (Exception, CancelledError):
                        archive_results[cam_code] = []
            except TimeoutError:
                logger.warning("timelapse archive walk timed out for %s, using partial results", host_key)
                for fut, cam_code in future_to_cam.items():
                    if cam_code not in archive_results:
                        fut.cancel()
                        archive_results[cam_code] = []
        for cam_code in cam_codes:
            entries = data.get(cam_code, [])
            for e in entries:
                e.setdefault("source", "station")
            station_dates = {e["date"] for e in entries}
            for ae in archive_results.get(cam_code, []):
                if ae["date"] not in station_dates:
                    entries.append(ae)
            if entries:
                data[cam_code] = entries
        if cam_codes:
            data = {k: v for k, v in data.items() if k in cam_codes}
        return data

    def _refresh_timelapses(host_key: str) -> None:
        # Coalesced background refresh. On failure (exception OR empty
        # result that wasn't empty before — though we don't currently
        # distinguish), keep the previous cache rather than poison it.
        with timelapses_swr_lock:
            if host_key in timelapses_swr_inflight:
                return
            timelapses_swr_inflight.add(host_key)
        try:
            data = _compute_timelapses_payload(host_key)
            with timelapses_swr_lock:
                timelapses_swr_cache[host_key] = (time.monotonic(), data)
        except Exception:
            logger.exception("background timelapses refresh failed for %s", host_key)
        finally:
            with timelapses_swr_lock:
                timelapses_swr_inflight.discard(host_key)

    def _get_timelapses_swr(host_key: str) -> dict[str, Any]:
        """Stale-while-revalidate accessor for the per-host timelapses payload.

        Returns the cached dict immediately on any hit (fresh or stale),
        kicking a background refresh on stale; falls through to one
        synchronous compute on a truly cold cache. Used by both the
        authenticated dashboard route and the public API — neither should
        ever pay the full per-cam SSHFS walk on the request thread when a
        prior payload exists.
        """
        now_mono = time.monotonic()
        with timelapses_swr_lock:
            cached = timelapses_swr_cache.get(host_key)
            if cached is None and host_key in timelapses_swr_inflight:
                return {}
        if cached is not None:
            age = now_mono - cached[0]
            if age >= timelapses_fresh_ttl:
                prefetch_executor.submit(_refresh_timelapses, host_key)
            return cached[1]
        with timelapses_swr_lock:
            timelapses_swr_inflight.add(host_key)
        try:
            data = _compute_timelapses_payload(host_key)
        finally:
            with timelapses_swr_lock:
                timelapses_swr_inflight.discard(host_key)
        with timelapses_swr_lock:
            timelapses_swr_cache[host_key] = (time.monotonic(), data)
        return data

    @app.route("/api/timelapses/<host_key>")
    @require_station
    def api_timelapses(host_key: str):
        """SWR + offline-tolerant. See ``_get_timelapses_swr``.

        Merges archive timelapses for any date the station doesn't have,
        so the response stays useful even when the station is offline."""
        _require_station_impl(config, host_key)
        return _json_cached(_get_timelapses_swr(host_key), max_age=60)

    # ── Settings / hardware / platepar ───────────────────────────────────

    @app.route("/api/settings/<host_key>")
    @require_station
    def api_settings(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/settings", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, cache_ttl=10)

    @app.route("/api/settings/<host_key>", methods=["PATCH"])
    @require_station
    def api_settings_patch(host_key: str):
        _require_station_impl(config, host_key)
        if command_dispatch.should_push(config, host_key):
            patch = request.get_json(silent=True) or {}
            try:
                cmd_id = command_dispatch.enqueue_signed_command(
                    host_key=host_key, type="patch_settings",
                    args={"settings": patch},
                )
            except command_dispatch.SigningUnavailable:
                return command_dispatch.signing_unavailable_response()
            return command_dispatch.queued_response(host_key, "patch_settings", cmd_id)
        try:
            url = station_url(config, tunnels, host_key, "/api/settings")
            resp = _session_for_url(url).patch(
                url, json=request.get_json(silent=True), timeout=10,
            )
            resp.raise_for_status()
            _invalidate_proxy_cache_impl(proxy_cache, proxy_cache_lock, host_key, "/api/settings")
            return jsonify(resp.json())
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    @app.route("/api/hardware/<host_key>")
    @require_station
    def api_hardware(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/hardware", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, cache_ttl=60)

    @app.route("/api/platepar/<host_key>")
    @public_route(page="events")
    def api_platepar_proxy(host_key: str):
        """Cached server-side (5 min via platepar_cache) + offline-tolerant.
        Client-side `Cache-Control: max-age=5` only — platepars are tuned
        interactively when calibrating cameras, and a 5-minute browser cache
        meant operator edits weren't visible until the cache expired even
        though the server had the new value. Keep the server cache long
        (round-trips to the station are expensive) but let the browser
        re-validate on every interaction; ETag makes the re-validation a
        304 in the unchanged case.

        Public under the "events" page toggle: the Detections page draws the
        FOV / radiant overlay from each camera's platepar (az_centre etc.).
        The payload carries only astrometric calibration (RA/Dec centre,
        rotation, scale) — no IP / cam_ip / host paths — so exposing it adds
        nothing sensitive. A GET is a pure read; the write path (recalibration)
        never touches this route. Anonymous callers may only read platepars for
        ``public: true`` stations: a private/commissioning station 404s before
        any station round-trip, matching ``station_is_public_for_request``'s
        unknown-key fail-closed rule. Logged-in operators keep the full fleet."""
        _require_station_impl(config, host_key)
        if not station_is_public_for_request(config, host_key):
            abort(404)
        cached = platepar_cache.get(host_key)
        now_mono = time.monotonic()
        status = cache.get_status(host_key)
        online = (status is None) or bool((status or {}).get("online", False))
        # Serve fresh cache hit
        if cached and now_mono < cached[0]:
            return _json_cached(cached[1], max_age=5)
        # Try live fetch when online; fall back to stale on failure.
        if online:
            try:
                payload = station_get_raw(config, tunnels, host_key, "/api/platepar")
                platepar_cache[host_key] = (now_mono + platepar_ttl, payload)
                import platepar_store; platepar_store.update_host(payload)
                return _json_cached(payload, max_age=5)
            except Exception:
                pass
        # Offline or live fetch failed — serve stale if we have any.
        if cached:
            return _json_cached(cached[1], max_age=5)
        return jsonify({"error": "station unreachable, no cached platepar"}), 503

    return _get_timelapses_swr
