"""Config-driven public-page toggles + custom public front page.

Stacks on the explicit-allow auth model from PR #628
(``tests/test_public_dashboard_authz.py``). Where that suite proves a
``@public_route`` view is anon-reachable, this suite proves the *runtime*
``public_pages`` toggle can flip an eligible page off (re-gating it for anon
while leaving logged-in access intact) and can NEVER expose a sensitive /
ungated route — and that the public front page renders for anon with only
the currently-enabled pages linked.

Driven through a real ``create_app`` so the genuine gate + decorator +
config-model path is exercised, not a re-implementation.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml


# ── App factory: real create_app with a configurable public_pages set ─────


def _build_app(public_pages):
    """Build a real dashboard app in test mode with the given ``public_pages``
    (pass ``None`` to omit the key entirely and exercise the default)."""
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_toggle_"))

    (tmp / "users.yaml").write_text(
        yaml.safe_dump(
            {
                "admin": {
                    "display_name": "Admin",
                    "password_hash": "",
                    "role": "admin",
                    "require_totp": False,
                },
                "viewer": {
                    "display_name": "Viewer",
                    "password_hash": "",
                    "role": "visitor",
                    "require_totp": False,
                },
            }
        )
    )

    cfg = {
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "pub_station": {
                "ip": "127.0.0.1",
                "label": "Public Station",
                "ssh_user": "test",
                "public": True,
                "lat": 52.5,
                "lon": 13.4,
                "cameras": [
                    {"code": "PUB001", "cam_ip": "127.0.0.1",
                     "label": "N", "rotate": False},
                ],
            },
        },
    }
    if public_pages is not None:
        cfg["public_pages"] = public_pages

    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))

    os.environ["ROVIMEN_USERS_PATH"] = str(tmp / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "toggle-test-secret"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")
    os.environ["ROVIMEN_STATION_STATE_DB"] = str(tmp / "station_state.db")
    os.environ["ROVIMEN_DETECTIONS_DB"] = str(tmp / "detections.db")
    os.environ["ROVIMEN_SHOWER_COUNTS_PATH"] = str(tmp / "shower_counts.json")

    import security

    _saved_threshold = security._LOGIN_THRESHOLD
    _saved_lockout = security._LOGIN_LOCKOUT_SECONDS
    _saved_init_limiter = security.init_limiter

    security._LOGIN_THRESHOLD = 10_000
    security._LOGIN_LOCKOUT_SECONDS = 0

    def _noop_limiter(app):
        lim = _saved_init_limiter(app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter

    from rovimen_dashboard import load_config, create_app

    config = load_config(config_path)
    app = create_app(config, config_path)
    app.config["TESTING"] = True

    # restore process-global knobs immediately; the gate captured public_pages
    # by value at install time so it's safe to restore now.
    security._LOGIN_THRESHOLD = _saved_threshold
    security._LOGIN_LOCKOUT_SECONDS = _saved_lockout
    security.init_limiter = _saved_init_limiter
    return app


def _logged_in(app, user="admin", role="admin"):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = user
        sess["role"] = role
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


# ── 1. A page in public_pages is anon-reachable ───────────────────────────


def test_enabled_page_is_anon_reachable():
    app = _build_app(["overview", "station", "events", "live", "showers"])
    anon = app.test_client()
    assert anon.get("/events").status_code == 200
    assert anon.get("/live").status_code == 200
    assert anon.get("/showers").status_code == 200


# ── 2. Removing a page from the list re-gates it for anon, not for users ──


def test_disabled_page_is_gated_for_anon_but_ok_for_user():
    # "events" intentionally dropped from the toggle set.
    app = _build_app(["overview", "station", "live", "showers"])
    anon = app.test_client()
    resp = anon.get("/events")
    assert resp.status_code in (302, 401), (
        f"disabled page must be gated for anon, got {resp.status_code}"
    )
    # A still-enabled sibling remains reachable — proves it's a per-page flip.
    assert anon.get("/live").status_code == 200
    # Logged-in operators are unaffected by the public toggle.
    assert _logged_in(app).get("/events").status_code == 200


def test_disabled_overview_api_regated_for_anon():
    """The fleet-map *data* endpoint /api/overview carries page='overview';
    toggling overview off re-gates it for anon. The landing page ``/`` now
    IS the fleet map (page='overview'), so it re-gates in lockstep — an
    operator who hides the map hides the anon landing too (fail-closed).
    /api/stations is decoupled (plain @public_route) — see
    ``test_stations_metadata_public_without_overview`` — so it stays reachable."""
    app = _build_app(["events", "live", "showers"])  # no "overview"
    anon = app.test_client()
    assert anon.get("/api/overview").status_code in (302, 401)
    # Landing page now carries page='overview' → gated when overview is off.
    assert anon.get("/").status_code in (302, 401)
    # /api/stations is page-less, so it stays reachable for the other pages.
    assert anon.get("/api/stations").status_code == 200
    # Logged-in still fine on both the data endpoint and the landing.
    assert _logged_in(app).get("/api/overview").status_code == 200
    assert _logged_in(app).get("/").status_code == 200


def test_stations_metadata_public_without_overview():
    """/api/stations is static station metadata (label/coords/camera codes)
    that events/showers/highlights need to render — it's a plain @public_route,
    reachable by anon regardless of the fleet-map 'overview' toggle so those
    pages can be exposed without turning on the live map."""
    # Every public page enabled EXCEPT overview.
    app = _build_app(["events", "showers", "highlights"])
    anon = app.test_client()
    assert anon.get("/api/stations").status_code == 200
    # Even with NOTHING enabled it stays reachable — it has no page key.
    empty = _build_app([]).test_client()
    assert empty.get("/api/stations").status_code == 200
    # But /api/overview (page='overview') is still gated in that empty case.
    assert empty.get("/api/overview").status_code in (302, 401)


# ── 3. Empty / unknown lists fail closed ──────────────────────────────────


def test_empty_public_pages_gates_everything_for_anon():
    app = _build_app([])
    anon = app.test_client()
    # The landing "/" now carries page='overview', so with nothing enabled it
    # is gated too — there is no anon surface at all in the empty config.
    for path in ("/", "/events", "/live", "/showers", "/api/overview"):
        assert anon.get(path).status_code in (302, 401), path
    # Page-less shower reference API stays reachable (no page key).
    assert anon.get("/api/showers").status_code == 200


def test_unknown_page_keys_are_dropped_not_exposed():
    """An unknown / typo'd key can never expose anything — it's dropped by the
    model validator and resolves to 'not enabled'. admin/config are not page
    keys at all, so even listing them never exposes the routes."""
    from models import DashboardConfig

    cfg = DashboardConfig(public_pages=["events", "admin", "config", "bogus"])
    assert cfg.public_pages == ["events"], cfg.public_pages

    # And end-to-end: adding "admin"/"config" to the list must not expose them
    # (neither is a page key — no @page= tag — so they can never go public).
    app = _build_app(["overview", "admin", "config"])
    anon = app.test_client()
    for path in ("/admin", "/config"):
        assert anon.get(path).status_code in (302, 401, 403), path


# ── 4. Sensitive routes can never be exposed by config ────────────────────


_SENSITIVE = [
    "/admin",
    "/config",
    "/api/admin/users",
    "/api/config/all",
    "/api/settings/pub_station",
    "/social",
]


@pytest.mark.parametrize("path", _SENSITIVE)
def test_sensitive_routes_never_public_even_if_key_added(path):
    # Every plausible page key jammed into the toggle set, including the now-
    # eligible "highlights" plus non-keys (admin/config/settings/users). None
    # of these routes carry a @public_route tag, so no config entry can expose
    # them — the fail-closed default holds.
    app = _build_app(
        ["overview", "station", "events", "live", "showers", "highlights",
         "admin", "config", "settings", "users", "social", "network"]
    )
    anon = app.test_client()
    assert anon.get(path).status_code in (302, 401, 403, 404, 405), (
        f"{path} must never be anon-reachable, got {anon.get(path).status_code}"
    )


# ── 5. Public front page renders for anon and shows only enabled pages ────


def test_anon_root_is_map_with_hero_when_overview_on():
    """With "overview" enabled, the anon landing at "/" is the fleet map (has a
    Leaflet map element) plus the hero/intro block."""
    app = _build_app(["overview", "live"])
    resp = app.test_client().get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="map"' in html                       # the fleet map itself
    assert "GLOBAL METEOR NETWORK" in html          # hero title
    assert "Explore the public data below" in html  # hero body
    # Top-nav tabs are gated on ``public_pages`` for anon: "overview" is on so
    # the Stations tab renders, but events/showers/highlights are NOT public in
    # this build, so those sibling tabs must be absent — a visible-but-gated tab
    # would just bounce an anonymous visitor to /login.
    assert 'href="/map"' in html
    assert 'href="/events"' not in html
    assert 'href="/showers"' not in html
    assert 'href="/highlights"' not in html


def test_anon_nav_shows_only_public_tabs():
    """The anon top-nav renders a sibling public-page tab only when that page's
    key is in ``public_pages``; logged-in operators always see every tab."""
    app = _build_app(["overview", "events", "showers"])  # highlights OFF
    anon_html = app.test_client().get("/").get_data(as_text=True)
    assert 'href="/events"' in anon_html        # events public → tab shown
    assert 'href="/showers"' in anon_html       # showers public → tab shown
    assert 'href="/highlights"' not in anon_html  # highlights off → tab hidden
    # A logged-in operator sees the Highlights tab regardless of the toggle.
    assert 'href="/highlights"' in _logged_in(app).get("/").get_data(as_text=True)


def test_anon_root_gated_when_overview_off():
    """With "overview" NOT enabled, the map-landing "/" is login-gated for anon
    (fail-closed) — no map, no hero."""
    app = _build_app(["events", "live", "showers"])  # no "overview"
    assert app.test_client().get("/").status_code in (302, 401)


def test_logged_in_root_serves_operator_overview():
    """Operators land on the operator overview dashboard (the map shell) — and
    never see the anon hero/intro block."""
    app = _build_app(["overview"])
    html = _logged_in(app).get("/").get_data(as_text=True)
    assert 'id="map"' in html                        # operator overview shell
    assert "GLOBAL METEOR NETWORK" not in html       # hero is anon-only


# ── 6. Default (key omitted) preserves the #628 public surface ────────────


def test_default_public_pages_preserves_all_eligible():
    from models import PUBLIC_PAGE_KEYS, DashboardConfig

    cfg = DashboardConfig()
    assert set(cfg.public_pages) == set(PUBLIC_PAGE_KEYS)

    app = _build_app(None)  # omit the key entirely
    anon = app.test_client()
    for path in ("/events", "/live", "/showers"):
        assert anon.get(path).status_code == 200, path


# ── 7. Operator's final public surface: [events, showers, highlights] ─────
#
# The exact set the operator will ship: Detections (/events), Meteor showers
# (/showers), Highlights (/highlights) and the About modal — and NOTHING
# else. overview/live/station are deliberately OFF. This block pins the whole
# boundary in one place so a future edit can't silently widen or narrow it.

_FINAL_PUBLIC_PAGES = ["events", "showers", "highlights"]


def _final_app():
    return _build_app(_FINAL_PUBLIC_PAGES)


@pytest.mark.parametrize("path", ["/events", "/showers", "/highlights"])
def test_final_surface_anon_pages_reachable(path):
    """The three enabled pages render 200 for anon under the operator's final
    toggle set. ``/`` is NOT here: it is now the fleet map (page='overview'),
    which is OFF in this set, so it is gated — see
    ``test_final_surface_anon_gated_routes``."""
    assert _final_app().test_client().get(path).status_code == 200, path


@pytest.mark.parametrize(
    "path",
    [
        "/api/stations",             # plain public — page labels/coords
        "/api/showers",              # IAU reference (page-less public)
        "/api/shower-year-counts",   # per-shower yearly counts (page-less)
        "/api/highlights/data?start=2026-01-01&end=2026-01-02",
        "/api/network-stats",        # About-modal aggregate stats
        "/api/auth/status",          # login/logout state for the header
        # Detections page data feed (page="events") — now anon-public so the
        # /events spinner actually resolves for a visitor.
        "/api/detections/nights",
        "/api/detections/20260101",
        "/api/detections/range?from=20260101&to=20260102",
        "/api/twilight/pub_station/20260101",  # twilight slider (page-less)
    ],
)
def test_final_surface_anon_data_apis_reachable(path):
    """Every data API the enabled pages + About modal fetch must be anon-
    reachable (200) — never a 302/401 — under the final toggle set."""
    resp = _final_app().test_client().get(path)
    assert resp.status_code == 200, f"{path} got {resp.status_code}"


def test_final_surface_anon_platepar_passes_gate():
    """The FOV-overlay platepar route is anon-public under the events toggle.
    With no live station in the test it 503s (station unreachable), which is a
    passed-gate state — the important thing is it is NOT a 302/401/403."""
    resp = _final_app().test_client().get("/api/platepar/pub_station")
    assert resp.status_code not in (301, 302, 401, 403), (
        f"/api/platepar/pub_station should pass the gate for anon, got {resp.status_code}"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/",                         # landing == fleet map (overview OFF)
        "/live",                     # live page OFF
        "/station/pub_station",      # station detail OFF
        "/map",                      # fleet-map page (overview OFF)
        "/stations",                 # fleet-map page alias (overview OFF)
        "/admin",
        "/config",
        "/social",
        "/api/overview",             # fleet-map data (overview OFF)
        "/api/live-feed",
        "/api/settings/pub_station",
        "/api/status/pub_station",
    ],
)
def test_final_surface_anon_gated_routes(path):
    """With overview/live/station OFF and only curated pages on, anon must be
    redirected/401/403 on the fleet-map page + data, live feeds, station detail,
    and all admin/config/social surfaces. The detection feed is NOT here — it
    rides the "events" toggle, which is on in this set."""
    resp = _final_app().test_client().get(path)
    assert resp.status_code in (302, 401, 403, 404), (
        f"{path} must be gated for anon, got {resp.status_code}"
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/admin/users"),
        ("POST", "/api/reboot/pub_station"),
        ("POST", "/api/rovimen/restart/pub_station"),
        ("PATCH", "/api/settings/pub_station"),
        ("POST", "/api/probe/pub_station"),
        ("POST", "/api/compilation"),
    ],
)
def test_final_surface_anon_mutations_blocked(method, path):
    """No mutating route is ever 2xx for anon, regardless of page toggles —
    the mutation gates are independent of public_pages (PR #628)."""
    resp = _final_app().test_client().open(path, method=method, json={})
    assert not (200 <= resp.status_code < 300), (
        f"{method} {path} must never succeed for anon, got {resp.status_code}"
    )
    assert resp.status_code in (302, 401, 403, 404, 405), (
        f"{method} {path} unexpected status for anon: {resp.status_code}"
    )


def test_final_surface_stations_hides_admin_fields_and_private_stations():
    """Anon /api/stations under the final set: no cam_ip / ip admin fields,
    and only public stations (the fixture has a single public station, so we
    assert the redaction directly on the camera rows)."""
    data = _final_app().test_client().get("/api/stations").get_json()
    assert "pub_station" in data
    for st in data.values():
        assert "ip" not in st, "station ip must not leak to anon"
        for cam in st.get("cameras", []):
            assert "cam_ip" not in cam, "cam_ip must not leak to anon"


def test_final_surface_network_stats_no_admin_fields():
    """The About-modal network-stats payload for anon carries only aggregate
    science + a public-station breakdown: no IP / cam_ip / hostname fields."""
    import json as _json

    raw = _json.dumps(
        _final_app().test_client().get("/api/network-stats").get_json()
    )
    for banned in ("cam_ip", '"ip"', "ssh_user", "jump_hosts", "127.0.0.1"):
        assert banned not in raw, f"network-stats leaked {banned}"


def test_highlights_off_regates_page_and_data_for_anon():
    """Dropping 'highlights' from the toggle set re-gates BOTH the page and
    its data route for anon, while a logged-in user keeps access."""
    app = _build_app(["events", "showers"])  # highlights OFF
    anon = app.test_client()
    assert anon.get("/highlights").status_code in (302, 401)
    assert anon.get(
        "/api/highlights/data?start=2026-01-01&end=2026-01-02"
    ).status_code in (302, 401)
    # Logged-in operator is unaffected by the public toggle.
    user = _logged_in(app)
    assert user.get("/highlights").status_code == 200
    assert user.get(
        "/api/highlights/data?start=2026-01-01&end=2026-01-02"
    ).status_code == 200


# ── 8. Detections-fix + public fleet-map surface: [events,showers,highlights,
# overview]. This is the set that both un-hangs the Detections page (events)
# AND exposes the fleet map (overview). Pins the whole widened boundary. ─────

_MAP_PLUS_PUBLIC_PAGES = ["events", "showers", "highlights", "overview"]


def _map_app():
    return _build_app(_MAP_PLUS_PUBLIC_PAGES)


@pytest.mark.parametrize(
    "path",
    [
        "/", "/map", "/stations", "/events", "/showers", "/highlights",
    ],
)
def test_map_surface_anon_pages_reachable(path):
    """With overview on alongside the curated pages, the fleet-map page (/map,
    /stations) plus the always-on landing render 200 for anon."""
    assert _map_app().test_client().get(path).status_code == 200, path


@pytest.mark.parametrize(
    "path",
    [
        "/api/overview",
        "/api/stations",
        "/api/detections/nights",
        "/api/detections/20260101",
        "/api/detections/range?from=20260101&to=20260102",
        "/api/twilight/pub_station/20260101",
    ],
)
def test_map_surface_anon_data_apis_reachable(path):
    """Under [events,showers,highlights,overview] anon must reach the fleet-map
    data (/api/overview) and the whole Detections feed — never 302/401/403."""
    resp = _map_app().test_client().get(path)
    assert resp.status_code == 200, f"{path} got {resp.status_code}"


def test_map_surface_anon_platepar_passes_gate():
    """Platepar (FOV overlay) passes the gate for anon under this set; 503 in
    the test is a passed-gate 'station unreachable', not a gate rejection."""
    resp = _map_app().test_client().get("/api/platepar/pub_station")
    assert resp.status_code not in (301, 302, 401, 403), (
        f"platepar should pass the gate for anon, got {resp.status_code}"
    )


def test_map_surface_anon_payloads_have_no_ip_or_cam_ip():
    """The anon fleet-map + detection payloads carry no ip / cam_ip / ssh_user
    / jump_hosts / raw host address anywhere in the body."""
    import json as _json

    client = _map_app().test_client()
    for path in (
        "/api/overview",
        "/api/stations",
        "/api/detections/20260101",
        "/api/detections/range?from=20260101&to=20260102",
    ):
        raw = _json.dumps(client.get(path).get_json())
        for banned in ("cam_ip", '"ip"', "ssh_user", "jump_hosts", "127.0.0.1"):
            assert banned not in raw, f"{path} leaked {banned}"


def test_map_surface_root_is_map_with_hero_for_anon():
    """The fleet map is the anon landing at '/' when overview is enabled: it
    serves the Leaflet map shell plus the hero/intro block. '/map' is the same
    shell (no hero)."""
    root = _map_app().test_client().get("/").get_data(as_text=True)
    assert "leaflet" in root.lower(), "'/' must serve the fleet map for anon"
    assert "GLOBAL METEOR NETWORK" in root, "'/' must carry the anon hero"
    # /map is the same shell, reachable, but without the hero (it's landing-only).
    map_html = _map_app().test_client().get("/map").get_data(as_text=True)
    assert "leaflet" in map_html.lower()
    assert "GLOBAL METEOR NETWORK" not in map_html


def test_map_surface_logged_in_root_is_overview():
    """Logged-in operators keep the operator overview at '/' (unchanged)."""
    html = _logged_in(_map_app()).get("/").get_data(as_text=True)
    assert "leaflet" in html.lower(), "operator '/' should still be the map"


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/reboot/pub_station"),
        ("PATCH", "/api/settings/pub_station"),
        ("POST", "/api/probe/pub_station"),
        ("POST", "/api/admin/users"),
    ],
)
def test_map_surface_mutations_still_blocked(method, path):
    """Widening the read surface never opens a mutating route for anon."""
    resp = _map_app().test_client().open(path, method=method, json={})
    assert not (200 <= resp.status_code < 300), (
        f"{method} {path} must never succeed for anon, got {resp.status_code}"
    )
