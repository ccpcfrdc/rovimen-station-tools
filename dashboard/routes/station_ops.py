"""Station operations -- reboot, probe, updater, services, RMS plots, encoding.

Extracted from rovimen_dashboard.py as part of issue #328.  All routes
preserved verbatim -- same URLs, same behaviour, same decorators.
Wired in from ``create_app()`` via ``register_station_ops_routes``.

Closure state (``_rms_plots_list_cache``, ``_RMS_PLOTS_LIST_TTL``,
``_proxy_cache``, ``_proxy_cache_lock``) is passed in from the caller
so that the main module and this module share the same cache instances.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

import requests
from flask import Flask, Response, abort, jsonify, request, send_file

import command_dispatch
from cache_store import THUMB_CACHE_DIR, _thumb_cache_has_space, _rms_plots_list_cache_path

from auth import require_station, station_is_public_for_request
from http_caching import _json_cached
from security import public_route
from route_helpers import (
    COLOR_METEOR_STACK_FILENAME,
    lookup_station,
    invalidate_proxy_cache,
    proxy_error_response,
    proxy_get,
    media_url,
)
from station_client import station_url, station_get_raw, _session_for_url
from tunnels import _TunnelDown

logger = logging.getLogger(__name__)

# Sanitisation regex for systemd-style service names.  Shared with the
# main module (defined there as _SERVICE_NAME_RE); kept as a local constant
# so this module is self-contained.
_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def register_station_ops_routes(
    app: Flask,
    config,
    tunnels,
    cache,
    *,
    rms_plots_list_cache: dict[tuple[str, str, str], tuple[float, Any, bool | None]],
    rms_plots_list_cache_lock: threading.Lock,
    rms_plots_list_ttl: float,
    proxy_cache: dict[tuple[str, str], tuple[float, Any]],
    proxy_cache_lock: threading.Lock,
) -> None:
    _require_station_impl = lookup_station
    _invalidate_proxy_cache_impl = invalidate_proxy_cache
    _proxy_error_response_impl = proxy_error_response
    _proxy_get_impl = proxy_get
    def _media_url(host_key: str, *path_parts: str) -> str:
        return media_url(config, tunnels, host_key, *path_parts)

    # ── RMS cameras ──────────────────────────────────────────────────────

    @app.route("/api/rms_cameras/<host_key>")
    @require_station
    def api_rms_cameras_proxy(host_key: str):
        _require_station_impl(config, host_key)
        try:
            return jsonify(station_get_raw(config, tunnels, host_key, "/api/rms_cameras"))
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Reboot ───────────────────────────────────────────────────────────

    @app.route("/api/reboot/<host_key>", methods=["POST"])
    @require_station
    def api_reboot(host_key: str):
        _require_station_impl(config, host_key)
        if command_dispatch.should_push(config, host_key):
            try:
                cmd_id = command_dispatch.enqueue_signed_command(
                    host_key=host_key, type="reboot",
                )
            except command_dispatch.SigningUnavailable:
                return command_dispatch.signing_unavailable_response()
            return command_dispatch.queued_response(host_key, "reboot", cmd_id)
        try:
            url = station_url(config, tunnels, host_key, "/api/reboot")
            resp = _session_for_url(url).post(url, timeout=10)
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Probe ────────────────────────────────────────────────────────────

    @app.route("/api/probe/<host_key>", methods=["POST"])
    @require_station
    def api_probe(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/probe")
            resp = _session_for_url(url).post(url, timeout=60)
            return jsonify(resp.json()), resp.status_code
        except (_TunnelDown, requests.RequestException, Exception) as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Encoding stats ───────────────────────────────────────────────────

    @app.route("/api/encoding/stats/<host_key>")
    @require_station
    def api_encoding_stats(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/encoding/stats", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, cache_ttl=5)

    # ── Detection stats ──────────────────────────────────────────────────

    @app.route("/api/detection-stats/<host_key>")
    @require_station
    def api_detection_stats(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/detection-stats", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, cache_ttl=10)

    # ── Overlay preview ──────────────────────────────────────────────────

    @app.route("/api/overlay_preview/<host_key>", methods=["POST"])
    @require_station
    def api_overlay_preview(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/overlay_preview")
            resp = _session_for_url(url).post(
                url, json=request.get_json(silent=True), timeout=20,
            )
            resp.raise_for_status()
            return Response(resp.content, mimetype="image/png")
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Archive test ─────────────────────────────────────────────────────

    @app.route("/api/archive/test/<host_key>", methods=["POST"])
    @require_station
    def api_archive_test(host_key: str):
        _require_station_impl(config, host_key)
        if command_dispatch.should_push(config, host_key):
            try:
                cmd_id = command_dispatch.enqueue_signed_command(
                    host_key=host_key, type="trigger_upload",
                )
            except command_dispatch.SigningUnavailable:
                return command_dispatch.signing_unavailable_response()
            return command_dispatch.queued_response(host_key, "trigger_upload", cmd_id)
        try:
            url = station_url(config, tunnels, host_key, "/api/archive/test")
            resp = _session_for_url(url).post(url, timeout=20)
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Updater ──────────────────────────────────────────────────────────

    @app.route("/api/updater/status/<host_key>")
    @require_station
    def api_updater_status(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/updater/status", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, cache_ttl=5)

    @app.route("/api/updater/check/<host_key>", methods=["POST"])
    @require_station
    def api_updater_check(host_key: str):
        _require_station_impl(config, host_key)
        if command_dispatch.should_push(config, host_key):
            try:
                cmd_id = command_dispatch.enqueue_signed_command(
                    host_key=host_key, type="run_updater", args={"check": True},
                )
            except command_dispatch.SigningUnavailable:
                return command_dispatch.signing_unavailable_response()
            return command_dispatch.queued_response(host_key, "run_updater", cmd_id)
        try:
            url = station_url(config, tunnels, host_key, "/api/updater/check")
            resp = _session_for_url(url).post(url, timeout=40)
            _invalidate_proxy_cache_impl(proxy_cache, proxy_cache_lock, host_key, "/api/updater/status")
            _invalidate_proxy_cache_impl(proxy_cache, proxy_cache_lock, host_key, "/api/updater/log")
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    @app.route("/api/updater/run/<host_key>", methods=["POST"])
    @require_station
    def api_updater_run(host_key: str):
        _require_station_impl(config, host_key)
        if command_dispatch.should_push(config, host_key):
            try:
                cmd_id = command_dispatch.enqueue_signed_command(
                    host_key=host_key, type="run_updater",
                )
            except command_dispatch.SigningUnavailable:
                return command_dispatch.signing_unavailable_response()
            return command_dispatch.queued_response(host_key, "run_updater", cmd_id)
        try:
            url = station_url(config, tunnels, host_key, "/api/updater/run")
            resp = _session_for_url(url).post(url, timeout=15)
            _invalidate_proxy_cache_impl(proxy_cache, proxy_cache_lock, host_key, "/api/updater/status")
            _invalidate_proxy_cache_impl(proxy_cache, proxy_cache_lock, host_key, "/api/updater/log")
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    @app.route("/api/updater/log/<host_key>")
    @require_station
    def api_updater_log(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/updater/log", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, cache_ttl=2)

    # ── Services restart ─────────────────────────────────────────────────

    @app.route("/api/services/restart/<host_key>", methods=["POST"])
    @require_station
    def api_services_restart(host_key: str):
        _require_station_impl(config, host_key)
        if command_dispatch.should_push(config, host_key):
            try:
                cmd_id = command_dispatch.enqueue_signed_command(
                    host_key=host_key, type="restart_services",
                )
            except command_dispatch.SigningUnavailable:
                return command_dispatch.signing_unavailable_response()
            return command_dispatch.queued_response(host_key, "restart_services", cmd_id)
        try:
            url = station_url(config, tunnels, host_key, "/api/services/restart")
            resp = _session_for_url(url).post(url, timeout=20)
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Rovimen control ──────────────────────────────────────────────────

    @app.route("/api/rovimen/<host_key>", methods=["POST"])
    @require_station
    def api_rovimen_toggle(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/rovimen")
            resp = _session_for_url(url).post(
                url, json=request.get_json(silent=True), timeout=30,
            )
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    @app.route("/api/rovimen/restart/<host_key>", methods=["POST"])
    @require_station
    def api_rovimen_restart(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/rovimen/restart")
            resp = _session_for_url(url).post(url, json={}, timeout=30)
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    @app.route("/api/rovimen/status/<host_key>")
    @require_station
    def api_rovimen_status(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/rovimen/status")
            resp = _session_for_url(url).get(url, timeout=20)
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Color capture ────────────────────────────────────────────────────

    @app.route("/api/color-capture/<host_key>", methods=["POST"])
    @require_station
    def api_color_capture_toggle(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/color-capture")
            resp = _session_for_url(url).post(
                url, json=request.get_json(silent=True), timeout=15,
            )
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Dawn pipeline ────────────────────────────────────────────────────

    @app.route("/api/dawn/run/<host_key>", methods=["POST"])
    @require_station
    def api_dawn_run(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/dawn/run")
            resp = _session_for_url(url).post(
                url, json=request.get_json(silent=True), timeout=15,
            )
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    @app.route("/api/dawn/progress/<host_key>")
    @require_station
    def api_dawn_progress(host_key: str):
        _require_station_impl(config, host_key)
        qs = request.query_string.decode()
        path = "/api/dawn/progress"
        if qs:
            path += f"?{qs}"
        try:
            return jsonify(station_get_raw(config, tunnels, host_key, path))
        except (_TunnelDown, requests.RequestException, Exception) as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Crons ────────────────────────────────────────────────────────────

    @app.route("/api/crons/<host_key>")
    @require_station
    def api_crons(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/crons", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, status_on_error=502, cache_ttl=30)

    # ── RMS status ───────────────────────────────────────────────────────

    @app.route("/api/rms/status/<host_key>")
    @require_station
    def api_rms_status(host_key: str):
        _require_station_impl(config, host_key)
        qs = request.query_string.decode()
        path = "/api/rms/status"
        if qs:
            path += f"?{qs}"
        try:
            return jsonify(station_get_raw(config, tunnels, host_key, path))
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── RMS plots ────────────────────────────────────────────────────────

    @app.route("/api/rms/plots/<host_key>/<cam>/<date>")
    @public_route(page="overview")
    def api_rms_plots_proxy(host_key: str, cam: str, date: str):
        """Cached + offline-tolerant.

        Memory cache (5 min) for fresh hits; disk cache (`_plots.json` next to
        the cached plot images) for serving when the station is offline or the
        process restarted. The disk cache is written by both this route and
        the prefetch loop.

        Public under the "overview" page toggle: the landing/overview page's
        per-camera "Latest Plots and Data" grid lists each camera's RMS plot
        images via this route. The payload carries only ``filename`` / ``label``
        / ``order`` per plot (no ip / cam_ip / host paths) — the synthetic
        color-meteor entry added below is the same shape — so it exposes
        nothing sensitive. A GET is a pure read. Anonymous callers may only
        list plots for ``public: true`` stations: a private/commissioning
        station 404s before any station round-trip, matching
        ``station_is_public_for_request``'s unknown-key fail-closed rule.
        Logged-in operators keep the full fleet.
        """
        _require_station_impl(config, host_key)
        if not station_is_public_for_request(config, host_key):
            abort(404)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        key = (host_key, cam, date)
        now_mono = time.monotonic()
        cached = rms_plots_list_cache.get(key)
        # Only short-circuit when the color-meteor HEAD result is already known
        # (cached[2] is True or False). If it's None the prefetch populated the
        # list without probing the station, so fall through to do the HEAD check.
        if cached and now_mono < cached[0] and cached[2] is not None:
            return _json_cached(cached[1], max_age=300)
        status = cache.get_status(host_key)
        online = (status is None) or bool((status or {}).get("online", False))
        plots: Any = None
        if online:
            try:
                plots = station_get_raw(
                    config, tunnels, host_key, f"/api/rms/plots/{cam}/{date}"
                )
            except Exception:
                plots = None
            # Inject color meteor stack if the station has it. The HEAD
            # check below costs one full station RTT (~70 ms from Romania),
            # which doubles the wall-clock of every plots-list cache miss.
            # Cache the HEAD result alongside the plots list so the next
            # call within the 5-min TTL skips it. `cached[2]` carries the
            # previous outcome — None means "not yet probed" so we still
            # need a fresh HEAD on first hit.
            has_color_cached: bool | None = (
                cached[2] if cached and len(cached) >= 3 else None
            )
            has_color = has_color_cached
            if isinstance(plots, list):
                if has_color is None:
                    try:
                        color_url = station_url(
                            config, tunnels, host_key,
                            f"/api/color-meteor-stack/{cam}/{date}"
                        )
                        head = _session_for_url(color_url).head(
                            color_url, timeout=6, allow_redirects=True,
                        )
                        has_color = head.status_code == 200
                    except Exception:
                        has_color = False
                if has_color:
                    plots.append({
                        "filename": COLOR_METEOR_STACK_FILENAME,
                        "label":    "Meteor stack (color)",
                        "order":    2,
                    })
        # Persist a successful live fetch to disk + memory.
        if isinstance(plots, list):
            with rms_plots_list_cache_lock:
                rms_plots_list_cache[key] = (
                    now_mono + rms_plots_list_ttl, plots, bool(has_color),
                )
                if len(rms_plots_list_cache) > 2000:
                    evict_mono = time.monotonic()
                    expired = [k for k, v in rms_plots_list_cache.items()
                               if v[0] < evict_mono]
                    for k in expired:
                        rms_plots_list_cache.pop(k, None)
                    if len(rms_plots_list_cache) > 2000:
                        by_age = sorted(rms_plots_list_cache.items(),
                                        key=lambda x: x[1][0])
                        for k, _ in by_age[:500]:
                            rms_plots_list_cache.pop(k, None)
            try:
                p = _rms_plots_list_cache_path(host_key, cam, date)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(plots))
            except Exception:
                pass
            return _json_cached(plots, max_age=300)
        # Live fetch failed (or offline) — try disk fallback.
        try:
            disk = _rms_plots_list_cache_path(host_key, cam, date)
            if disk.exists():
                disk_plots = json.loads(disk.read_text())
                if isinstance(disk_plots, list):
                    # Infer has_color from the disk payload itself so the
                    # cache entry stays self-consistent — the synthetic
                    # COLOR_METEOR_STACK_FILENAME entry is added before
                    # the list is written to disk.
                    disk_has_color = any(
                        e.get("filename") == COLOR_METEOR_STACK_FILENAME
                        for e in disk_plots if isinstance(e, dict)
                    )
                    with rms_plots_list_cache_lock:
                        rms_plots_list_cache[key] = (
                            now_mono + rms_plots_list_ttl, disk_plots, disk_has_color,
                        )
                        if len(rms_plots_list_cache) > 2000:
                            evict_mono = time.monotonic()
                            expired = [k for k, v in rms_plots_list_cache.items()
                                       if v[0] < evict_mono]
                            for k in expired:
                                rms_plots_list_cache.pop(k, None)
                            if len(rms_plots_list_cache) > 2000:
                                by_age = sorted(rms_plots_list_cache.items(),
                                                key=lambda x: x[1][0])
                                for k, _ in by_age[:500]:
                                    rms_plots_list_cache.pop(k, None)
                    return _json_cached(disk_plots, max_age=300)
        except Exception:
            pass
        # In-memory stale (expired but present) — better than nothing.
        if cached:
            return _json_cached(cached[1], max_age=300)
        return jsonify({"error": "station unreachable, no cached plots list"}), 503

    @app.route("/api/rms/plot_image/<host_key>/<cam>/<date>/<filename>")
    @public_route(page="overview")
    def api_rms_plot_image_proxy(host_key: str, cam: str, date: str, filename: str):
        """Serve RMS plot images (FF-based) -- raw upstream bytes, no rotation.

        FF frames are always in native sensor orientation. Cameras flagged
        ``rotate: true`` in dashboard_config.yaml render upside-down here;
        the client applies a 180-degree CSS transform (``.thumb-rotated``)
        on the ``<img>`` instead. Avoids a PIL decode/re-encode per cache
        miss and lets the compositor flip after first paint.

        The colour meteor stack is already rotated station-side -- we route
        to a different station endpoint for it. Either way the dashboard
        no longer rotates bytes; the client knows to skip the CSS class
        for the colour meteor stack filename.

        Public under the "overview" page toggle: these JPG/PNG/WEBP plots are
        non-sensitive science imagery (captured-star stacks, photometry /
        detection plots) shown in the overview "Latest Plots and Data" grid.
        Anonymous callers may only fetch plots for ``public: true`` stations:
        a private/commissioning station 404s before any file read or station
        round-trip, matching ``station_is_public_for_request``'s unknown-key
        fail-closed rule. Logged-in operators keep the full fleet.
        """
        _require_station_impl(config, host_key)
        if not station_is_public_for_request(config, host_key):
            abort(404)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        if not re.match(r"^[\w._-]+\.(jpg|png|webp)$", filename):
            abort(400)
        is_color_meteor = filename == COLOR_METEOR_STACK_FILENAME
        cache_file = THUMB_CACHE_DIR / host_key / "rms_plots" / cam / date / filename
        if cache_file.exists():
            return send_file(cache_file, conditional=True)
        try:
            if is_color_meteor:
                url = _media_url(host_key, "api", "color-meteor-stack", cam, date)
            else:
                url = _media_url(host_key, "api", "rms", "plot_image", cam, date, filename)
            resp = _session_for_url(url).get(url, timeout=15)
            if resp.status_code == 200:
                ct = resp.headers.get(
                    "Content-Type",
                    "image/webp" if is_color_meteor else "image/png",
                )
                data = resp.content
                # Rotation moved client-side: the rotate flag is exposed in
                # /api/stations and the .thumb-rotated CSS class on the <img>
                # handles 180 rotation in the compositor.
                if _thumb_cache_has_space():
                    try:
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        cache_file.write_bytes(data)
                    except Exception:
                        pass
                return Response(data, status=200, headers={"Content-Type": ct})
            return Response(resp.content, status=resp.status_code)
        except Exception:
            abort(502)

    # ── Logo management ──────────────────────────────────────────────────

    @app.route("/api/logo/list/<host_key>")
    @require_station
    def api_logo_list_proxy(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/logo/list", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, status_on_error=502, cache_ttl=60)

    @app.route("/api/logo/upload/<host_key>", methods=["POST"])
    @require_station
    def api_logo_upload_proxy(host_key: str):
        _require_station_impl(config, host_key)
        if "file" not in request.files:
            abort(400)
        f = request.files["file"]
        try:
            url = station_url(config, tunnels, host_key, "/api/logo/upload")
            resp = _session_for_url(url).post(
                url, files={"file": (f.filename, f.stream, f.mimetype)}, timeout=30,
            )
            _invalidate_proxy_cache_impl(proxy_cache, proxy_cache_lock, host_key, "/api/logo/list")
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Storage watch ────────────────────────────────────────────────────

    @app.route("/api/storagewatch/<host_key>")
    @require_station
    def api_storagewatch(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(
            config, tunnels, host_key, "/api/storagewatch/status",
            proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock,
            json_cached=_json_cached, status_on_error=502, cache_ttl=5,
        )

    # ── Encode chunk ─────────────────────────────────────────────────────

    @app.route("/api/encode_chunk/<host_key>", methods=["POST"])
    @require_station
    def api_encode_chunk(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/encode_chunk")
            resp = _session_for_url(url).post(
                url, json=request.get_json(silent=True), timeout=120,
            )
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Service restart proxy ────────────────────────────────────────────

    @app.route("/api/restart/<host_key>/<service>", methods=["POST"])
    @require_station
    def api_restart_service(host_key: str, service: str):
        _require_station_impl(config, host_key)
        if not _SERVICE_NAME_RE.match(service):
            return jsonify({"error": "invalid_service_name"}), 400
        if command_dispatch.should_push(config, host_key):
            try:
                cmd_id = command_dispatch.enqueue_signed_command(
                    host_key=host_key, type="restart_service",
                    args={"service": service},
                )
            except command_dispatch.SigningUnavailable:
                return command_dispatch.signing_unavailable_response()
            return command_dispatch.queued_response(host_key, "restart_service", cmd_id)
        try:
            url = station_url(config, tunnels, host_key, f"/api/restart/{service}")
            resp = _session_for_url(url).post(url, timeout=30)
            resp.raise_for_status()
            return jsonify(resp.json())
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Log viewer proxy ─────────────────────────────────────────────────

    @app.route("/api/logs/<host_key>/<service>")
    @require_station
    def api_logs(host_key: str, service: str):
        _require_station_impl(config, host_key)
        if not _SERVICE_NAME_RE.match(service):
            return jsonify({"error": "invalid_service_name"}), 400
        lines = request.args.get("lines", "200")
        if not lines.isdigit() or int(lines) > 5000:
            lines = "200"
        date = request.args.get("date", "")
        if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            date = ""
        qs = f"lines={lines}" + (f"&date={date}" if date else "")
        try:
            url = station_url(config, tunnels, host_key, f"/api/logs/{service}?{qs}")
            resp = _session_for_url(url).get(url, timeout=15)
            resp.raise_for_status()
            return jsonify(resp.json())
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)
