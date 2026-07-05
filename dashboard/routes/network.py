"""Network map, coverage grid, version, and admin network config routes."""

import logging
from pathlib import Path

from flask import Flask, jsonify, redirect, request, url_for

from auth import require_auth, require_admin, is_anonymous, \
    station_is_public_for_request
from http_caching import _json_cached
from security import public_route
import gmn_data

logger = logging.getLogger(__name__)


def register_network_routes(
    app: Flask,
    config,
    *,
    save_config,
) -> None:
    """Attach network-related routes to *app*.

    Parameters
    ----------
    config:
        The live ``DashboardConfig`` instance (shared with the rest of the
        dashboard — mutations here are visible everywhere).
    save_config:
        Closure that atomically persists ``config`` to
        ``dashboard_config.yaml`` (defined in ``create_app()``).
    """
    import network_stats as _net_stats_mod

    # ── Pre-computation warmup ───────────────────────────────────────────
    _net_stats_mod.register_precompute(config.model_dump())

    import threading as _th
    _th.Thread(target=gmn_data.orbit_counts, daemon=True, name="orbit-warmup").start()

    # ── Network stats ────────────────────────────────────────────────────

    @app.route("/api/network-stats")
    @public_route
    def api_network_stats():
        # Plain @public_route (no page key): powers the "Network Statistics"
        # block of the About modal, which every public page can open. The
        # payload is aggregate network science — covered area, double-station
        # coverage by altitude, atmospheric volume, expected flux, and total
        # station/camera/platepar counts. No IPs, cam_ip, hostnames, host
        # paths, or per-user data. For anonymous visitors the per-station
        # breakdown is filtered to ``public: true`` stations so a
        # commissioning/opted-out site's existence isn't disclosed (same
        # per-station rule as /api/stations, PR #628). Logged-in accounts get
        # the full fleet breakdown.
        import platepar_store
        platepars = platepar_store.get_all()
        cfg_raw = config.model_dump()
        result = _net_stats_mod.get_network_stats(platepars, cfg_raw)
        try:
            result["orbit_counts"] = gmn_data.orbit_counts()
        except Exception:
            logger.exception("orbit_counts failed")
        if is_anonymous():
            per_station = result.get("per_station")
            if isinstance(per_station, dict):
                result = dict(result)  # don't mutate the cached compute result
                result["per_station"] = {
                    hk: row
                    for hk, row in per_station.items()
                    if station_is_public_for_request(config, hk)
                }
        return jsonify(result)

    # ── Coverage grid ────────────────────────────────────────────────────

    @app.route("/api/coverage-grid")
    @require_auth
    def api_coverage_grid():
        import platepar_store
        import network_stats
        alt = request.args.get("alt", 100.0, type=float)
        alt = max(10.0, min(alt, 200.0))
        platepars = platepar_store.get_all()
        cfg_raw = config.model_dump()
        cells = network_stats.get_coverage_grid(platepars, cfg_raw, alt)
        return _json_cached({"altitude_km": alt, "cells": cells}, max_age=300)

    # ── Version endpoint ─────────────────────────────────────────────────

    @app.route("/api/version")
    def api_version():
        version_file = Path("/opt/rovimen/station-bundle/.version")
        version = "unknown"
        if version_file.exists():
            version = version_file.read_text().strip()
        return jsonify({"version": version})

    # ── Global config PATCH ──────────────────────────────────────────────

    @app.route("/api/admin/network-config/global", methods=["PATCH"])
    @require_admin
    def api_admin_network_config_global():
        body = request.get_json(force=True) or {}
        if "correlation_window_s" in body:
            v = body["correlation_window_s"]
            if isinstance(v, int) and 1 <= v <= 3600:
                config.correlation_window_s = v
        save_config()
        return jsonify({"ok": True, "correlation_window_s": config.correlation_window_s})

    # ── Network status page ──────────────────────────────────────────────

    @app.route("/network")
    def network_page():
        # No auth -- public redirect; network status now lives on the overview page
        return redirect(url_for("index"))
