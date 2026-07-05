"""Simple page renders and utility routes."""

import json
import logging
import os
from pathlib import Path
from flask import Flask, Response, abort, jsonify, render_template, session

from auth import require_admin, station_is_public_for_request
from mdc_poller import get_shower_data
from route_helpers import lookup_station
from security import public_route

logger = logging.getLogger(__name__)


_SHOWER_COUNTS_PATH = Path(
    os.environ.get("ROVIMEN_SHOWER_COUNTS_PATH", "/opt/rovimen/shower_year_counts.json")
)


def register_misc_routes(app: Flask, config, build_version: str) -> None:
    _require_station_impl = lookup_station

    # ── Service Worker ────────────────────────────────────────────────────

    # The SW file is physically stored under dashboard/static/sw.js, but we
    # also expose it at the origin root.  A SW served from /static/sw.js
    # would only control the /static/ scope, which is useless to us -- we
    # need full origin scope so navigation requests and other static
    # requests both flow through it.
    @app.route("/sw.js")
    def service_worker():
        """Serve the Service Worker from the origin root so its scope is '/'.

        The CACHE_VERSION placeholder is replaced with BUILD_VERSION so
        every deploy automatically invalidates the SW cache without
        manually bumping a version constant.
        """
        sw_path = Path(app.static_folder) / "sw.js"
        body = sw_path.read_text().replace(
            "const CACHE_VERSION = 'v20';",
            f"const CACHE_VERSION = '{build_version}';",
        )
        return Response(body, mimetype="application/javascript",
                        headers={"Cache-Control": "max-age=300",
                                 "Service-Worker-Allowed": "/"})

    # ── Pages ─────────────────────────────────────────────────────────────

    @app.route("/")
    @public_route(page="overview")
    def index():
        # The live fleet map is now the landing page for EVERYONE — anonymous
        # visitors and logged-in operators both get the ``overview.html`` shell
        # at ``/``. Anonymous visitors additionally see a hero/intro block
        # (rendered by the template when ``show_hero`` is truthy) explaining
        # what ROVIMEN is; logged-in operators get the plain operator dashboard
        # they already had. The map's data (``/api/overview`` + ``/api/stations``
        # + ``/api/status/all`` + ``/api/overview/stacks``, all page="overview")
        # applies the per-station public filter + admin-field redaction for anon,
        # so no private station or IP reaches an anonymous caller.
        #
        # ``/`` carries page="overview": if an operator toggles the fleet-map
        # page off, an anonymous visitor falls through to the login gate rather
        # than landing on a map they weren't meant to see — fail-closed, and
        # symmetric with ``/map``. ``public_home.html`` is retired (unused).
        return render_template(
            "overview.html",
            show_hero=not session.get("user"),
        )

    @app.route("/map")
    @app.route("/stations")
    @public_route(page="overview")
    def fleet_map():
        # Alias of ``/`` — the fleet map is the landing page for everyone now,
        # and the "Stations" nav link points here. Kept as a stable, explicit
        # URL for the map (bookmarks / the nav) even though ``/`` renders the
        # same shell. No hero here: the intro block belongs on the ``/`` entry
        # point only, so navigating within the app doesn't re-show it. Reachable
        # by anon only when "overview" is in ``public_pages``; the map's data
        # (``/api/overview`` + ``/api/stations`` + ``/api/status/all`` +
        # ``/api/overview/stacks``) applies the same per-station public filter +
        # admin-field redaction, so no private station or IP reaches an anon
        # caller. Logged-in operators get the identical overview shell.
        return render_template("overview.html", show_hero=False)

    @app.route("/station/<host_key>")
    @app.route("/station/<host_key>/<tab>")
    @public_route(page="station")
    def station_detail(host_key: str, tab: str = ""):
        _require_station_impl(config, host_key)
        # A commissioning / opted-out (public: false) station must not be
        # viewable by anonymous visitors even by direct deep-link; 404 keeps
        # its very existence undisclosed. Logged-in users keep full access.
        if not station_is_public_for_request(config, host_key):
            abort(404)
        return render_template("dashboard.html", default_station=host_key)

    @app.route("/config")
    def config_page():
        return render_template("config.html")

    @app.route("/admin")
    @require_admin
    def admin_page():
        return render_template("admin.html")

    # ── Events page ──────────────────────────────────────────────────────

    @app.route("/events")
    @public_route(page="events")
    def events_page():
        return render_template("events.html")

    # ── Highlights page ──────────────────────────────────────────────────

    @app.route("/highlights")
    @public_route(page="highlights")
    def highlights_page():
        # Anon-reachable only when "highlights" is in ``public_pages``; the
        # curated top-10 data route carries the same page key so the page and
        # its data flip together. The page itself renders no admin fields.
        return render_template("highlights.html", build_version=build_version)

    # ── Live view page ───────────────────────────────────────────────────

    @app.route("/live")
    @public_route(page="live")
    def live_view():
        return render_template("live_view.html")

    # ── Meteor showers page ──────────────────────────────────────────────

    @app.route("/showers")
    @public_route(page="showers")
    def showers_page():
        return render_template("showers.html")

    # ── Meteor shower reference data (public — IAU reference, not sensitive) ─

    @app.route("/api/showers")
    @public_route
    def api_showers():
        """Return MDC shower data keyed by IAU code."""
        return jsonify(get_shower_data())

    # ── Per-shower ROVIMEN detection counts for the current year ──────────

    @app.route("/api/shower-year-counts")
    @public_route
    def api_shower_year_counts():
        """Return {IAU_CODE: count} for all ROVIMEN detections in the current year.

        Written by scan_shower_counts.py (daily cron on the VPS).
        Returns {} if the file hasn't been generated yet.
        """
        if not _SHOWER_COUNTS_PATH.exists():
            return jsonify({})
        try:
            payload = json.loads(_SHOWER_COUNTS_PATH.read_text())
            return jsonify(payload.get("counts", {}))
        except Exception:
            logger.exception("failed to read %s", _SHOWER_COUNTS_PATH)
            return jsonify({})
