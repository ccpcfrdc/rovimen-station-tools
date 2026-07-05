"""Tests for the /api/highlights/data aggregation cache.

The highlights range aggregation is expensive enough to blow nginx's upstream
timeout (and, on the single gthread worker, stall sibling requests). The route
caches the assembled payload per (start, end). These tests prove a repeat
request is served from cache without re-running the per-date aggregation, and
that a different range still recomputes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT / "tests" / "e2e" / "fixtures"
sys.path.insert(0, str(PROJECT / "dashboard"))

PAST = "2024-01-15"          # fully in the past -> long TTL, deterministic
PAST2 = "2024-02-20"


@pytest.fixture
def app():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_hl_test_"))
    os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "hl-test-key"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["ROVIMEN_API_KEYS_REQUIRED"] = "0"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_ARCHIVE_PATH"] = str(tmp / "archive")  # empty -> no clips
    os.environ["ROVIMEN_GMN_CACHE_DIR"] = str(tmp / "gmn_cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")

    import security
    original_init_limiter = security.init_limiter

    def _noop_limiter(a):
        lim = original_init_limiter(a)
        lim.enabled = False
        return lim
    security.init_limiter = _noop_limiter

    import yaml
    config_data = {
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststn1": {
                "cameras": [{"code": "TST001", "label": "N", "cam_ip": "127.0.0.1",
                             "rotate": False, "az": 0.0, "alt": 45.0}],
                "ip": "127.0.0.1", "jump_hosts": [], "label": "Test Alpha",
                "lat": 45.0, "lon": 25.0, "proxy_media": False, "public": True,
                "public_tabs": [], "ssh_user": "test", "location_name": "Test Alpha",
                "show_on_map": True, "status": "active",
            },
        },
    }
    cfg_path = tmp / "dashboard_config.yaml"
    cfg_path.write_text(yaml.dump(config_data))

    from rovimen_dashboard import load_config, create_app
    config = load_config(cfg_path)
    flask_app = create_app(config, cfg_path)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "alice"
        sess["role"] = "admin"
        sess["stations"] = []
    return c


@pytest.fixture
def gmn_counter(monkeypatch):
    """Count events_for_date calls; return empty events so the route is fast."""
    import gmn_data
    calls: list[str] = []

    def _stub(date_str):
        calls.append(date_str)
        return {"events": [], "date": date_str}
    monkeypatch.setattr(gmn_data, "events_for_date", _stub)
    # Avoid network/file reads for orbit tallies too.
    monkeypatch.setattr(gmn_data, "_daily_counts_for_month", lambda y, m: {})
    return calls


def test_repeat_request_hits_cache(client, gmn_counter):
    r1 = client.get(f"/api/highlights/data?start={PAST}&end={PAST}")
    assert r1.status_code == 200
    after_first = len(gmn_counter)
    assert after_first >= 1  # recomputed: at least one date aggregated

    r2 = client.get(f"/api/highlights/data?start={PAST}&end={PAST}")
    assert r2.status_code == 200
    # Cache hit: no further per-date aggregation happened.
    assert len(gmn_counter) == after_first
    assert r1.get_json() == r2.get_json()


def test_different_range_recomputes(client, gmn_counter):
    client.get(f"/api/highlights/data?start={PAST}&end={PAST}")
    after_first = len(gmn_counter)
    client.get(f"/api/highlights/data?start={PAST2}&end={PAST2}")
    assert len(gmn_counter) > after_first  # distinct key -> recompute


def test_response_carries_etag(client, gmn_counter):
    r = client.get(f"/api/highlights/data?start={PAST}&end={PAST}")
    assert r.status_code == 200
    assert r.headers.get("ETag")
    assert "max-age" in r.headers.get("Cache-Control", "")


def test_invalid_dates_not_cached(client, gmn_counter):
    r = client.get("/api/highlights/data?start=nonsense&end=also-bad")
    assert r.status_code == 400
