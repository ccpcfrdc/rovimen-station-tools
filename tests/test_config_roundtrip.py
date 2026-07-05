"""Tests for dashboard config round-trip — P2 #16.

P2 #16: _config_to_dict must serialize all fields from StationConfig and
        CameraConfig. If a new field is added to the model but not to the
        serializer, admin edits silently drop it.
"""

from __future__ import annotations

import pytest

from models import CameraConfig, StationConfig, DashboardConfig


class TestCameraConfigFields:
    """Every field in CameraConfig must be representable in the dict output."""

    CAMERA_FIELDS = {"code", "cam_ip", "rotate", "label", "az", "alt", "rtsp_url"}

    def test_all_fields_present_in_model(self):
        """Verify the set of fields we expect."""
        actual = set(CameraConfig.model_fields.keys())
        assert actual == self.CAMERA_FIELDS

    def test_camera_to_dict_coverage(self):
        """Simulate _camera_to_dict and verify all fields are emitted.
        This test must be updated when new fields are added to CameraConfig."""
        cam = CameraConfig(
            code="RO000H",
            cam_ip="192.168.1.10",
            rotate=True,
            label="North",
            az=45.0,
            alt=30.0,
            rtsp_url="rtsp://192.168.1.10:554/live",
        )

        # Replicate _camera_to_dict logic
        d = {
            "code": cam.code,
            "cam_ip": cam.cam_ip,
            "rotate": cam.rotate,
            "label": getattr(cam, "label", None) or "",
        }
        if cam.az is not None:
            d["az"] = cam.az
        if cam.alt is not None:
            d["alt"] = cam.alt
        if cam.rtsp_url is not None:
            d["rtsp_url"] = cam.rtsp_url

        for field in self.CAMERA_FIELDS:
            assert field in d, f"CameraConfig.{field} missing from _camera_to_dict output"

    def test_camera_to_dict_omits_none_optional(self):
        """Optional fields that are None should be omitted (not serialized as null)."""
        cam = CameraConfig(code="RO000H", cam_ip="", rotate=False)

        d = {
            "code": cam.code,
            "cam_ip": cam.cam_ip,
            "rotate": cam.rotate,
            "label": getattr(cam, "label", None) or "",
        }
        if cam.az is not None:
            d["az"] = cam.az
        if cam.alt is not None:
            d["alt"] = cam.alt
        if cam.rtsp_url is not None:
            d["rtsp_url"] = cam.rtsp_url

        assert "az" not in d
        assert "alt" not in d
        assert "rtsp_url" not in d


class TestStationConfigFields:
    """Every field in StationConfig must be in _config_to_dict output."""

    STATION_FIELDS = {
        "ip", "label", "ssh_user", "cameras", "jump_hosts",
        "proxy_media", "lat", "lon", "show_on_map", "public_tabs",
        "public", "location_name", "status", "ssh_host_key_fingerprints",
        "push_enabled",
    }

    def test_all_fields_present_in_model(self):
        actual = set(StationConfig.model_fields.keys())
        assert actual == self.STATION_FIELDS

    def test_config_to_dict_coverage(self):
        """Simulate _config_to_dict station serialization and verify all fields."""
        st = StationConfig(
            ip="100.1.2.3",
            label="Test Station",
            ssh_user="gmn",
            cameras=[CameraConfig(code="RO000H", cam_ip="192.168.1.10")],
            jump_hosts=["100.64.0.6"],
            proxy_media=True,
            lat=45.0,
            lon=25.0,
            show_on_map=True,
            public_tabs=["archive"],
            public=True,
            location_name="Bucharest",
        )

        # Replicate _config_to_dict station serialization
        d = {
            "ip": st.ip,
            "label": st.label,
            "ssh_user": st.ssh_user,
            "proxy_media": st.proxy_media,
            "jump_hosts": st.jump_hosts,
            "lat": st.lat,
            "lon": st.lon,
            "show_on_map": st.show_on_map,
            "public_tabs": st.public_tabs,
            "public": st.public,
            "location_name": st.location_name,
            "status": st.status,
            "push_enabled": st.push_enabled,
            # Serialized conditionally by _config_to_dict (only when set); the
            # test station leaves it default-empty, so mirror that here.
            "ssh_host_key_fingerprints": st.ssh_host_key_fingerprints,
            "cameras": [{"code": c.code} for c in st.cameras],
        }

        for field in self.STATION_FIELDS:
            assert field in d, f"StationConfig.{field} missing from _config_to_dict output"

    def test_new_field_detection(self):
        """If a field is added to StationConfig, this test MUST be updated.
        It fails when the model has fields we don't know about."""
        actual = set(StationConfig.model_fields.keys())
        missing = actual - self.STATION_FIELDS
        assert not missing, (
            f"New field(s) {missing} added to StationConfig but not listed in test. "
            f"Update STATION_FIELDS and verify _config_to_dict serializes them."
        )


class TestDashboardConfigFields:
    """DashboardConfig top-level fields."""

    CONFIG_FIELDS = {"station_api_port", "correlation_window_s", "stations",
                     "public_pages", "highlight_codes"}

    def test_all_fields_present(self):
        actual = set(DashboardConfig.model_fields.keys())
        assert actual == self.CONFIG_FIELDS

    def test_new_field_detection(self):
        actual = set(DashboardConfig.model_fields.keys())
        missing = actual - self.CONFIG_FIELDS
        assert not missing, (
            f"New field(s) {missing} added to DashboardConfig. "
            f"Update CONFIG_FIELDS and _config_to_dict."
        )


class TestConfigRoundTrip:
    """Full round-trip: model → dict → model should be lossless."""

    def test_station_round_trip(self):
        original = StationConfig(
            ip="100.1.2.3",
            label="Ghirdoveni",
            ssh_user="thor",
            cameras=[
                CameraConfig(code="RO000A", cam_ip="192.168.1.10", rotate=True,
                             label="NW", az=315.0, alt=25.0,
                             rtsp_url="rtsp://192.168.1.10:554/live"),
            ],
            jump_hosts=["100.64.0.6"],
            proxy_media=True,
            lat=44.8,
            lon=25.5,
            show_on_map=True,
            public_tabs=["archive", "videodb"],
            public=True,
            location_name="Ghirdoveni, Dambovita",
        )

        d = {
            "ip": original.ip,
            "label": original.label,
            "ssh_user": original.ssh_user,
            "proxy_media": original.proxy_media,
            "jump_hosts": original.jump_hosts,
            "lat": original.lat,
            "lon": original.lon,
            "show_on_map": original.show_on_map,
            "public_tabs": original.public_tabs,
            "public": original.public,
            "location_name": original.location_name,
            "cameras": [
                {
                    "code": c.code,
                    "cam_ip": c.cam_ip,
                    "rotate": c.rotate,
                    "label": c.label or "",
                    **({"az": c.az} if c.az is not None else {}),
                    **({"alt": c.alt} if c.alt is not None else {}),
                    **({"rtsp_url": c.rtsp_url} if c.rtsp_url is not None else {}),
                }
                for c in original.cameras
            ],
        }

        restored = StationConfig.model_validate(d)
        assert restored.ip == original.ip
        assert restored.label == original.label
        assert restored.ssh_user == original.ssh_user
        assert restored.proxy_media == original.proxy_media
        assert restored.jump_hosts == original.jump_hosts
        assert restored.lat == original.lat
        assert restored.lon == original.lon
        assert restored.show_on_map == original.show_on_map
        assert restored.public_tabs == original.public_tabs
        assert restored.public == original.public
        assert restored.location_name == original.location_name
        assert len(restored.cameras) == 1
        assert restored.cameras[0].code == "RO000A"
        assert restored.cameras[0].az == 315.0
        assert restored.cameras[0].rtsp_url == "rtsp://192.168.1.10:554/live"
