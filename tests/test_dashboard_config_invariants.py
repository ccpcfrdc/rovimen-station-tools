"""Structural invariants for dashboard/dashboard_config.yaml.

These tests load the REAL config file (not a fixture) so that CI catches
config drift the moment a bad edit is committed. The stale Iasi IP
(100.64.0.5 surviving long after the physical machine was decommissioned
on 2026-06-18) is the canonical example of the class of bug these guard
against.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
# Validate the live config when present (private deployment repo); fall back to
# the shipped example (public repo, where the real config is gitignored). The
# invariants are structural, so they hold for either.
_LIVE_CONFIG = REPO_ROOT / "dashboard" / "dashboard_config.yaml"
_EXAMPLE_CONFIG = REPO_ROOT / "dashboard" / "dashboard_config.example.yaml"
CONFIG_PATH = _LIVE_CONFIG if _LIVE_CONFIG.exists() else _EXAMPLE_CONFIG

VALID_STATUSES = {"active", "commissioning", "offline"}
# Tailscale assigns IPs in 100.64.0.0/10 (CGNAT range), which maps to
# 100.64.x.x - 100.127.x.x. We accept the full 100.x.x.x prefix because
# individual station IPs have been observed across 100.67-100.126.
_TAILSCALE_RE = re.compile(r"^100\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


@pytest.fixture(scope="module")
def config() -> dict:
    """Load dashboard_config.yaml once per module."""
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict), "dashboard_config.yaml must be a YAML mapping"
    return data


@pytest.fixture(scope="module")
def stations(config: dict) -> dict:
    return config.get("stations", {})


class TestCameraCodeUniqueness:
    """Every camera code must be unique across the entire fleet."""

    def test_no_duplicate_camera_codes(self, stations: dict):
        seen: dict[str, str] = {}  # code -> station_id
        duplicates: list[str] = []
        for station_id, station in stations.items():
            for cam in station.get("cameras", []):
                code = cam.get("code", "")
                if not code:
                    continue
                if code in seen:
                    duplicates.append(
                        f"{code!r} appears in both {seen[code]!r} and {station_id!r}"
                    )
                else:
                    seen[code] = station_id
        assert not duplicates, (
            "Duplicate camera codes detected (fleet-wide codes must be unique):\n"
            + "\n".join(f"  - {d}" for d in duplicates)
        )


class TestCameraIpUniquenessWithinStation:
    """No two cameras within the same station may share a cam_ip (unless empty)."""

    def test_no_duplicate_cam_ips_within_station(self, stations: dict):
        violations: list[str] = []
        for station_id, station in stations.items():
            seen_ips: dict[str, str] = {}  # ip -> first code that claimed it
            for cam in station.get("cameras", []):
                ip = cam.get("cam_ip", "").strip()
                code = cam.get("code", "<unknown>")
                if not ip:
                    # Empty ip is allowed (camera not yet connected)
                    continue
                if ip in seen_ips:
                    violations.append(
                        f"{station_id}: {code!r} shares cam_ip {ip!r} "
                        f"with {seen_ips[ip]!r}"
                    )
                else:
                    seen_ips[ip] = code
        assert not violations, (
            "Cameras within the same station must not share cam_ip:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )


class TestStationStatusValues:
    """Every station status must be one of the accepted enum values."""

    def test_status_in_allowed_set(self, stations: dict):
        bad: list[str] = []
        for station_id, station in stations.items():
            status = station.get("status", "active")
            if status not in VALID_STATUSES:
                bad.append(
                    f"{station_id}: status={status!r} not in {sorted(VALID_STATUSES)}"
                )
        assert not bad, (
            "Station status must be one of "
            f"{sorted(VALID_STATUSES)}:\n"
            + "\n".join(f"  - {b}" for b in bad)
        )


class TestPublicStationCoordinates:
    """Every public station must have non-null lat and lon."""

    def test_public_stations_have_coordinates(self, stations: dict):
        missing: list[str] = []
        for station_id, station in stations.items():
            if not station.get("public", True):
                continue
            lat = station.get("lat")
            lon = station.get("lon")
            if lat is None or lon is None:
                missing.append(
                    f"{station_id}: public=true but lat={lat!r}, lon={lon!r}"
                )
        assert not missing, (
            "Public stations must have non-null lat and lon "
            "(needed for map rendering and public API):\n"
            + "\n".join(f"  - {m}" for m in missing)
        )


class TestStationIpFormat:
    """Every station ip must be a valid dotted-quad IPv4 address.

    Tailscale IPs use the 100.x.x.x range (CGNAT / RFC6598).  We accept any
    dotted-quad so the test remains useful if a station is ever reached via a
    non-Tailscale address, but the pattern check still catches obvious typos
    like a hostname, URL, or stale placeholder left after a migration.
    """

    def test_station_ips_are_dotted_quad(self, stations: dict):
        bad: list[str] = []
        for station_id, station in stations.items():
            ip = station.get("ip", "")
            if not _IPV4_RE.match(ip):
                bad.append(f"{station_id}: ip={ip!r} is not a dotted-quad IPv4")
        assert not bad, (
            "Station IPs must be dotted-quad IPv4 addresses:\n"
            + "\n".join(f"  - {b}" for b in bad)
        )

    def test_station_ips_are_tailscale_range(self, stations: dict):
        """Fleet IPs should be in 100.x.x.x (Tailscale CGNAT range).

        Fail with an informative message rather than silently passing a
        non-Tailscale address — a direct LAN IP in the config usually means
        the Tailscale address was not updated after a machine migration.
        """
        bad: list[str] = []
        for station_id, station in stations.items():
            ip = station.get("ip", "")
            if _IPV4_RE.match(ip) and not _TAILSCALE_RE.match(ip):
                bad.append(
                    f"{station_id}: ip={ip!r} is a valid IPv4 but not in "
                    "the 100.x.x.x Tailscale range — update after machine migration"
                )
        assert not bad, (
            "Station IPs must be Tailscale addresses (100.x.x.x):\n"
            + "\n".join(f"  - {b}" for b in bad)
        )
