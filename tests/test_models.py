"""Tests for dashboard/models.py — Pydantic configuration models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models import CameraConfig, DashboardConfig, StationConfig, UserConfig


# ── CameraConfig ─────────────────────────────────────────────────────────


class TestCameraConfig:
    def test_valid_ip(self):
        cam = CameraConfig(code="RO000A", cam_ip="192.168.1.100")
        assert cam.cam_ip == "192.168.1.100"
        assert cam.code == "RO000A"

    def test_invalid_ip_raises(self):
        with pytest.raises(ValidationError, match="dotted-quad"):
            CameraConfig(code="RO000A", cam_ip="not-an-ip")

    def test_empty_ip_allowed(self):
        cam = CameraConfig(code="RO000A", cam_ip="")
        assert cam.cam_ip == ""

    def test_whitespace_only_ip_treated_as_empty(self):
        cam = CameraConfig(code="RO000A", cam_ip="   ")
        assert cam.cam_ip == ""

    def test_ip_with_out_of_range_octets_passes_regex(self):
        # The validator only checks dotted-quad format, not 0-255 range
        cam = CameraConfig(code="X", cam_ip="999.999.999.999")
        assert cam.cam_ip == "999.999.999.999"

    def test_defaults(self):
        cam = CameraConfig(code="DE001B", cam_ip="10.0.0.1")
        assert cam.rotate is False
        assert cam.label == ""
        assert cam.az is None
        assert cam.alt is None
        assert cam.rtsp_url is None

    def test_all_fields(self):
        cam = CameraConfig(
            code="RO000H",
            cam_ip="192.168.1.50",
            rotate=True,
            label="North cam",
            az=45.0,
            alt=30.0,
            rtsp_url="rtsp://192.168.1.50:554/stream",
        )
        assert cam.rotate is True
        assert cam.label == "North cam"
        assert cam.az == 45.0
        assert cam.alt == 30.0
        assert cam.rtsp_url == "rtsp://192.168.1.50:554/stream"

    def test_hostname_rejected(self):
        with pytest.raises(ValidationError, match="dotted-quad"):
            CameraConfig(code="X", cam_ip="camera.local")

    def test_ipv6_rejected(self):
        with pytest.raises(ValidationError, match="dotted-quad"):
            CameraConfig(code="X", cam_ip="::1")


# ── StationConfig ────────────────────────────────────────────────────────


class TestStationConfig:
    def test_defaults(self):
        st = StationConfig(ip="100.64.0.3", label="Berlin")
        assert st.ssh_user == "gmn"
        assert st.cameras == []
        assert st.jump_hosts == []
        assert st.proxy_media is False
        assert st.lat is None
        assert st.lon is None
        assert st.show_on_map is True
        assert st.public_tabs == []
        assert st.public is True
        assert st.location_name == ""
        assert st.push_enabled is False

    def test_with_cameras(self):
        cam = CameraConfig(code="RO000A", cam_ip="192.168.1.10")
        st = StationConfig(
            ip="100.64.0.7",
            label="Ghirdoveni",
            cameras=[cam],
        )
        assert len(st.cameras) == 1
        assert st.cameras[0].code == "RO000A"

    def test_nested_cameras_from_dict(self):
        st = StationConfig(
            ip="100.64.0.6",
            label="Vaslui",
            cameras=[
                {"code": "RO000M", "cam_ip": "192.168.1.10"},
                {"code": "RO000N", "cam_ip": "192.168.1.11"},
            ],
        )
        assert len(st.cameras) == 2
        assert st.cameras[1].code == "RO000N"

    def test_jump_hosts(self):
        st = StationConfig(
            ip="100.64.0.8",
            label="Bucharest",
            jump_hosts=["100.64.0.6"],
        )
        assert st.jump_hosts == ["100.64.0.6"]

    def test_public_station(self):
        st = StationConfig(
            ip="100.64.0.9",
            label="Bistrita",
            public=True,
            location_name="Bistrita, Bistrita-Nasaud",
        )
        assert st.public is True
        assert st.location_name == "Bistrita, Bistrita-Nasaud"


# ── DashboardConfig ──────────────────────────────────────────────────────


class TestDashboardConfig:
    def test_defaults(self):
        dc = DashboardConfig()
        assert dc.station_api_port == 7779
        assert dc.correlation_window_s == 1
        assert dc.stations == {}

    def test_with_stations(self):
        dc = DashboardConfig(
            stations={
                "gmn0000": StationConfig(ip="100.64.0.3", label="Berlin"),
            }
        )
        assert "gmn0000" in dc.stations
        assert dc.stations["gmn0000"].label == "Berlin"

    def test_custom_port(self):
        dc = DashboardConfig(station_api_port=8080, correlation_window_s=5)
        assert dc.station_api_port == 8080
        assert dc.correlation_window_s == 5


# ── UserConfig ───────────────────────────────────────────────────────────


class TestUserConfig:
    def test_all_fields(self):
        user = UserConfig(
            display_name="Alex",
            password_hash="pbkdf2:sha256:600000$hash",
            role="admin",
            stations=["gmn0000", "gmn0001"],
            reset_token="tok_hash",
            reset_token_expiry="2026-06-01T00:00:00+00:00",
            email="alex@example.com",
            totp_secret="JBSWY3DPEHPK3PXP",
            require_totp=True,
            magic_token="magic_hash",
            expires_at="2026-12-31T23:59:59+00:00",
        )
        assert user.display_name == "Alex"
        assert user.role == "admin"
        assert len(user.stations) == 2
        assert user.email == "alex@example.com"
        assert user.totp_secret == "JBSWY3DPEHPK3PXP"
        assert user.magic_token == "magic_hash"
        assert user.expires_at == "2026-12-31T23:59:59+00:00"

    def test_require_totp_defaults_true(self):
        user = UserConfig(role="host")
        assert user.require_totp is True

    def test_minimal_user(self):
        user = UserConfig(role="host")
        assert user.display_name == ""
        assert user.password_hash == ""
        assert user.stations == []
        assert user.reset_token is None
        assert user.reset_token_expiry is None
        assert user.email is None
        assert user.totp_secret is None
        assert user.magic_token is None
        assert user.expires_at is None

    def test_session_epoch_defaults_zero(self):
        # Legacy rows without the field inherit 0 (audit H3) so existing
        # sessions never spuriously revalidate on first deploy.
        assert UserConfig(role="host").session_epoch == 0

    def test_session_epoch_roundtrips(self):
        user = UserConfig(role="host", session_epoch=7)
        assert user.session_epoch == 7
        assert user.model_dump()["session_epoch"] == 7

    def test_role_is_required(self):
        with pytest.raises(ValidationError):
            UserConfig()  # type: ignore[call-arg]
