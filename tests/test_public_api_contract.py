"""Contract tests for the public API surface.

Lock down response schemas so that field renames, type changes, or
accidental removals break CI before they break astromania.org.
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


@pytest.fixture(scope="module")
def app():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_api_test_"))

    os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "contract-test-key"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["ROVIMEN_API_KEYS_REQUIRED"] = "0"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
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
                "cameras": [
                    {"code": "TST001", "label": "North", "cam_ip": "127.0.0.1",
                     "rotate": False, "az": 0.0, "alt": 45.0},
                    {"code": "TST002", "label": "East", "cam_ip": "127.0.0.1",
                     "rotate": False, "az": 90.0, "alt": 45.0},
                ],
                "ip": "127.0.0.1", "jump_hosts": [], "label": "Test Alpha",
                "lat": 45.0, "lon": 25.0, "proxy_media": False,
                "public": True, "public_tabs": [], "ssh_user": "test",
                "location_name": "Test Alpha", "show_on_map": True,
                "status": "active",
            },
            "teststn2": {
                "cameras": [
                    {"code": "TST003", "label": "South", "cam_ip": "127.0.0.1",
                     "rotate": False, "az": 180.0, "alt": 45.0},
                ],
                "ip": "127.0.0.1", "jump_hosts": [], "label": "Test Beta",
                "lat": 44.43, "lon": 26.1, "proxy_media": False,
                "public": True, "public_tabs": [], "ssh_user": "test",
                "location_name": "Test Beta", "show_on_map": True,
                "status": "active",
            },
        },
    }

    cfg_path = tmp / "dashboard_config.yaml"
    cfg_path.write_text(yaml.dump(config_data))

    from rovimen_dashboard import load_config, create_app
    config = load_config(cfg_path)
    flask_app = create_app(config, cfg_path)
    flask_app.config["TESTING"] = True
    yield flask_app


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


# ── Index ────────────────────────────────────────────────────────────────


class TestIndex:
    def test_returns_200(self, client):
        resp = client.get("/api/public/v1")
        assert resp.status_code == 200

    def test_schema_fields(self, client):
        data = client.get("/api/public/v1").get_json()
        assert data["service"] == "rovimen-public-api"
        assert "schema_version" in data
        assert "endpoints" in data
        assert "media" in data
        assert "now_utc" in data

    def test_endpoints_listing(self, client):
        endpoints = client.get("/api/public/v1").get_json()["endpoints"]
        required = [
            "stations", "station", "detections", "detection",
            "events", "event", "timelapses", "nightstacks",
            "stats", "orbits",
        ]
        for name in required:
            assert name in endpoints, f"missing endpoint: {name}"

    def test_media_listing(self, client):
        media = client.get("/api/public/v1").get_json()["media"]
        for kind in ("clip", "stack", "timelapse", "nightstack"):
            assert kind in media, f"missing media type: {kind}"


# ── CORS ─────────────────────────────────────────────────────────────────


class TestCORS:
    def test_cors_on_json(self, client):
        resp = client.get("/api/public/v1/stations")
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    def test_cors_methods(self, client):
        resp = client.options("/api/public/v1/stations")
        allow = resp.headers.get("Access-Control-Allow-Methods", "")
        for m in ("GET", "HEAD", "OPTIONS"):
            assert m in allow

    def test_cors_headers_exposed(self, client):
        resp = client.get("/api/public/v1/stations")
        exposed = resp.headers.get("Access-Control-Expose-Headers", "")
        assert "ETag" in exposed


# ── Stations ─────────────────────────────────────────────────────────────


class TestStations:
    def test_returns_200(self, client):
        resp = client.get("/api/public/v1/stations")
        assert resp.status_code == 200

    def test_response_shape(self, client):
        data = client.get("/api/public/v1/stations").get_json()
        assert "stations" in data
        assert "count" in data
        assert isinstance(data["stations"], list)
        assert isinstance(data["count"], int)
        assert data["count"] == len(data["stations"])

    def test_station_fields(self, client):
        data = client.get("/api/public/v1/stations").get_json()
        assert data["count"] > 0, "no public stations in test config"
        st = data["stations"][0]
        required_fields = [
            "id", "label", "location_name", "latitude", "longitude",
            "country", "online", "cameras",
        ]
        for f in required_fields:
            assert f in st, f"station missing field: {f}"

    def test_station_field_types(self, client):
        st = client.get("/api/public/v1/stations").get_json()["stations"][0]
        assert isinstance(st["id"], str)
        assert isinstance(st["label"], str)
        assert isinstance(st["latitude"], (int, float))
        assert isinstance(st["longitude"], (int, float))
        assert isinstance(st["online"], bool)
        assert isinstance(st["cameras"], list)

    def test_camera_fields(self, client):
        st = client.get("/api/public/v1/stations").get_json()["stations"][0]
        assert len(st["cameras"]) > 0
        cam = st["cameras"][0]
        for f in ("code", "label", "azimuth", "elevation"):
            assert f in cam, f"camera missing field: {f}"

    def test_camera_field_types(self, client):
        cam = client.get("/api/public/v1/stations").get_json()["stations"][0]["cameras"][0]
        assert isinstance(cam["code"], str)
        assert isinstance(cam["azimuth"], (int, float))
        assert isinstance(cam["elevation"], (int, float))


# ── Station Detail ───────────────────────────────────────────────────────


class TestStationDetail:
    def test_valid_station(self, client):
        stations = client.get("/api/public/v1/stations").get_json()["stations"]
        sid = stations[0]["id"]
        resp = client.get(f"/api/public/v1/stations/{sid}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == sid

    def test_nonexistent_station_404(self, client):
        resp = client.get("/api/public/v1/stations/doesnotexist")
        assert resp.status_code == 404


# ── Detections ───────────────────────────────────────────────────────────


class TestDetections:
    def test_returns_200(self, client):
        resp = client.get("/api/public/v1/detections")
        assert resp.status_code == 200

    def test_response_shape(self, client):
        data = client.get("/api/public/v1/detections").get_json()
        required = ["date", "count", "total", "offset", "limit", "detections"]
        for f in required:
            assert f in data, f"detections response missing: {f}"
        assert isinstance(data["detections"], list)
        assert isinstance(data["count"], int)
        assert isinstance(data["total"], int)
        assert isinstance(data["offset"], int)
        assert isinstance(data["limit"], int)

    def test_date_filter(self, client):
        resp = client.get("/api/public/v1/detections?date=2026-01-01")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["date"] == "2026-01-01"

    def test_invalid_date_400(self, client):
        resp = client.get("/api/public/v1/detections?date=not-a-date")
        assert resp.status_code == 400

    def test_limit_over_max_400(self, client):
        resp = client.get("/api/public/v1/detections?limit=9999")
        assert resp.status_code == 400

    def test_order_param(self, client):
        for order in ("time", "time_desc", "mag"):
            resp = client.get(f"/api/public/v1/detections?order={order}")
            assert resp.status_code == 200


# ── Detection Detail ─────────────────────────────────────────────────────


class TestDetectionDetail:
    def test_nonexistent_404(self, client):
        resp = client.get("/api/public/v1/detections/2026-01-01:fake:TST001:none.mkv")
        assert resp.status_code == 404


# ── Events ───────────────────────────────────────────────────────────────


class TestEvents:
    def test_returns_200(self, client):
        resp = client.get("/api/public/v1/events")
        assert resp.status_code == 200

    def test_response_shape(self, client):
        data = client.get("/api/public/v1/events").get_json()
        required = ["date", "count", "total", "offset", "limit", "events"]
        for f in required:
            assert f in data, f"events response missing: {f}"
        assert isinstance(data["events"], list)

    def test_has_trajectory_filter(self, client):
        resp = client.get("/api/public/v1/events?has_trajectory=true")
        assert resp.status_code == 200

    def test_invalid_date_400(self, client):
        resp = client.get("/api/public/v1/events?date=nope")
        assert resp.status_code == 400


# ── Event Detail ─────────────────────────────────────────────────────────


class TestEventDetail:
    def test_nonexistent_404(self, client):
        resp = client.get("/api/public/v1/events/gmn:fake_id_12345")
        assert resp.status_code == 404


# ── Timelapses ───────────────────────────────────────────────────────────


class TestTimelapses:
    def test_returns_200(self, client):
        resp = client.get("/api/public/v1/timelapses")
        assert resp.status_code == 200

    def test_response_shape(self, client):
        data = client.get("/api/public/v1/timelapses").get_json()
        required = ["count", "total", "offset", "limit", "timelapses"]
        for f in required:
            assert f in data, f"timelapses response missing: {f}"
        assert isinstance(data["timelapses"], list)


# ── Night Stacks ─────────────────────────────────────────────────────────


class TestNightstacks:
    def test_returns_200(self, client):
        resp = client.get("/api/public/v1/nightstacks")
        assert resp.status_code == 200

    def test_response_shape(self, client):
        data = client.get("/api/public/v1/nightstacks").get_json()
        required = ["count", "total", "offset", "limit", "nightstacks"]
        for f in required:
            assert f in data, f"nightstacks response missing: {f}"
        assert isinstance(data["nightstacks"], list)


# ── Stats ────────────────────────────────────────────────────────────────


class TestStats:
    def test_returns_200(self, client):
        resp = client.get("/api/public/v1/stats")
        assert resp.status_code == 200

    def test_response_shape(self, client):
        data = client.get("/api/public/v1/stats").get_json()
        required = [
            "period", "anchor_date", "online_stations", "total_stations",
            "detection_count", "per_station", "top_showers", "brightest",
            "coverage_pct",
        ]
        for f in required:
            assert f in data, f"stats response missing: {f}"

    def test_coverage_fields(self, client):
        data = client.get("/api/public/v1/stats").get_json()
        for f in ("coverage_pct", "dual_coverage_pct",
                  "coverage_pct_40", "dual_coverage_pct_40"):
            assert f in data, f"stats missing coverage field: {f}"

    def test_field_types(self, client):
        data = client.get("/api/public/v1/stats").get_json()
        assert isinstance(data["period"], str)
        assert isinstance(data["anchor_date"], str)
        assert isinstance(data["online_stations"], int)
        assert isinstance(data["total_stations"], int)
        assert isinstance(data["detection_count"], int)
        assert isinstance(data["per_station"], dict)
        assert isinstance(data["top_showers"], list)

    def test_period_param(self, client):
        for period in ("tonight", "day", "month", "all"):
            resp = client.get(f"/api/public/v1/stats?period={period}")
            assert resp.status_code == 200
            assert resp.get_json()["period"] == period


# ── FOV ──────────────────────────────────────────────────────────────────


class TestFOV:
    def test_returns_200(self, client):
        resp = client.get("/api/public/v1/fov")
        assert resp.status_code == 200

    def test_response_is_geojson(self, client):
        data = client.get("/api/public/v1/fov").get_json()
        assert "type" in data or "features" in data

    def test_alt_param(self, client):
        for alt in ("90", "40"):
            resp = client.get(f"/api/public/v1/fov?alt={alt}")
            assert resp.status_code == 200


# ── Orbits ───────────────────────────────────────────────────────────────


class TestOrbits:
    """Orbits endpoint contract.

    The production endpoint calls ``gmn_data.ensure_monthly_cached()``, which
    downloads a ~45 MB monthly ``traj_summary`` file from
    globalmeteornetwork.org (HTTP read timeout of 60s). Left unmocked the
    contract tests do real network I/O — non-deterministic, and a slow/hung
    upstream blows past the global 60s pytest timeout. We stub the fetch with
    a tiny on-disk fixture so the real parser (``parse_traj_summary``) and the
    endpoint's witness-matching logic still run, but no network is touched.
    """

    @pytest.fixture(autouse=True)
    def _stub_gmn_fetch(self, monkeypatch, tmp_path):
        import gmn_data

        # One valid 86-column semicolon row witnessed by our test cameras
        # (TST001/TST002 are defined on teststn1 in the `app` fixture), so the
        # endpoint yields a real orbit rather than an empty list.
        cells = ["" for _ in range(86)]
        cells[0] = "20260115084811_test0"       # id (YYYYMMDD-prefixed)
        cells[2] = "2026-01-15 08:48:11.000000"  # begin UTC
        cells[4] = "PER"                          # shower code
        cells[5] = "294.5"                        # solar longitude
        cells[7] = "45.1"                         # RA geo
        cells[9] = "58.0"                         # Dec geo
        cells[15] = "59.2"                        # Vgeo
        cells[23] = "2.7"                         # a
        cells[25] = "0.62"                        # e
        cells[27] = "113.0"                       # i
        cells[29] = "150.0"                       # peri
        cells[31] = "294.5"                       # node
        cells[37] = "0.95"                        # q
        cells[43] = "4.5"                         # q_aph
        cells[49] = "1.9"                         # tisserand
        cells[63] = "45.0"                        # lat begin
        cells[65] = "25.0"                        # lon begin
        cells[67] = "102.0"                       # ht begin
        cells[69] = "44.8"                        # lat end
        cells[71] = "25.2"                        # lon end
        cells[73] = "88.0"                        # ht end
        cells[75] = "0.42"                        # duration
        cells[76] = "-2.1"                        # peak mag
        cells[84] = "2"                           # num stations
        cells[85] = "TST001,TST002"               # participating stations

        fixture = tmp_path / "traj_summary_fixture.txt"
        fixture.write_text("# header comment line\n" + ";".join(cells) + "\n")

        monkeypatch.setattr(
            gmn_data, "ensure_monthly_cached",
            lambda year, month: fixture,
        )
        yield

    def test_returns_200(self, client):
        resp = client.get("/api/public/v1/orbits")
        assert resp.status_code == 200

    def test_response_shape(self, client):
        data = client.get("/api/public/v1/orbits").get_json()
        required = ["month", "count", "total", "offset", "limit", "orbits"]
        for f in required:
            assert f in data, f"orbits response missing: {f}"
        assert isinstance(data["orbits"], list)

    def test_month_param(self, client):
        resp = client.get("/api/public/v1/orbits?month=2026-01")
        assert resp.status_code == 200
        assert resp.get_json()["month"] == "2026-01"


# ── Auth (API key enforcement) ───────────────────────────────────────────


class TestAuth:
    """Test auth behavior when keys ARE required."""

    @pytest.fixture(autouse=True)
    def _require_keys(self, monkeypatch):
        monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "1")
        yield
        monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "0")

    def test_index_no_key_ok(self, client):
        resp = client.get("/api/public/v1")
        assert resp.status_code == 200

    def test_stations_no_key_401(self, client):
        resp = client.get("/api/public/v1/stations")
        assert resp.status_code == 401

    def test_401_body_shape(self, client):
        data = client.get("/api/public/v1/stations").get_json()
        assert data["error"] == "missing_or_invalid_api_key"
        assert "detail" in data

    def test_401_www_authenticate(self, client):
        resp = client.get("/api/public/v1/stations")
        assert "ApiKey" in resp.headers.get("WWW-Authenticate", "")

    def test_url_key_param_rejected(self, client):
        resp = client.get("/api/public/v1/stations?key=somekey")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["error"] == "url_key_param_disabled"


# ── Media path validation ────────────────────────────────────────────────


class TestMediaValidation:
    def test_invalid_camera_format(self, client):
        resp = client.get("/media/v1/clip/bad!cam/2026-01-01/x.mkv")
        assert resp.status_code == 404

    def test_invalid_date_format(self, client):
        resp = client.get("/media/v1/clip/RO000A/not-a-date/x.mkv")
        assert resp.status_code == 404

    def test_invalid_extension(self, client):
        resp = client.get("/media/v1/clip/RO000A/2026-01-01/x.exe")
        assert resp.status_code == 404

    def test_missing_file_404(self, client):
        resp = client.get("/media/v1/clip/TST001/2026-01-01/clip.mkv")
        assert resp.status_code == 404
