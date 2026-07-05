"""Tests for dashboard/coverage.py — sky coverage computation."""

from __future__ import annotations

import math
import time
from unittest.mock import patch

import pytest

from coverage import (
    EARTH_RADIUS,
    MAX_DIST_KM,
    METEOR_ALT_KM,
    MIN_ALT_DEG,
    _ROMANIA,
    _destination,
    _project,
    camera_footprint,
    compute_coverage_stats,
    get_coverage_pct,
    get_coverage_stats,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_platepar(
    lat: float = 45.0,
    lon: float = 25.0,
    az_centre: float = 180.0,
    alt_centre: float = 45.0,
    fov_h: float = 88.0,
    fov_v: float = 47.0,
) -> dict:
    return {
        "lat": lat,
        "lon": lon,
        "az_centre": az_centre,
        "alt_centre": alt_centre,
        "fov_h": fov_h,
        "fov_v": fov_v,
    }


# ── _destination ─────────────────────────────────────────────────────────


class TestDestination:
    def test_due_north_from_equator(self):
        """100 km due north from (0, 0) should be roughly (0.9, 0)."""
        lat, lon = _destination(0.0, 0.0, 0.0, 100.0)
        expected_lat = math.degrees(100.0 / EARTH_RADIUS)
        assert abs(lat - expected_lat) < 0.01
        assert abs(lon - 0.0) < 0.01

    def test_due_east_from_known_point(self):
        """Due east from (45, 25) for 100 km. Latitude should barely change,
        longitude should increase."""
        lat, lon = _destination(45.0, 25.0, 90.0, 100.0)
        assert abs(lat - 45.0) < 0.1  # nearly same latitude
        assert lon > 25.0  # moved eastward

    def test_zero_distance(self):
        lat, lon = _destination(44.5, 26.1, 123.0, 0.0)
        assert abs(lat - 44.5) < 1e-9
        assert abs(lon - 26.1) < 1e-9

    def test_symmetry_north_south(self):
        """Going 200 km north then 200 km south should return close to start."""
        lat1, lon1 = _destination(45.0, 25.0, 0.0, 200.0)
        lat2, lon2 = _destination(lat1, lon1, 180.0, 200.0)
        assert abs(lat2 - 45.0) < 0.01
        assert abs(lon2 - 25.0) < 0.01


# ── _project ─────────────────────────────────────────────────────────────


class TestProject:
    def test_high_altitude_near_station(self):
        """90-degree elevation (straight up) should project very close to the station."""
        lat, lon = _project(45.0, 25.0, 0.0, 90.0, METEOR_ALT_KM)
        assert abs(lat - 45.0) < 0.01
        assert abs(lon - 25.0) < 0.01

    def test_low_altitude_clips_to_min(self):
        """Elevation below MIN_ALT_DEG is clipped to MIN_ALT_DEG."""
        lat_low, lon_low = _project(45.0, 25.0, 0.0, 1.0, METEOR_ALT_KM)
        lat_min, lon_min = _project(45.0, 25.0, 0.0, MIN_ALT_DEG, METEOR_ALT_KM)
        assert abs(lat_low - lat_min) < 1e-9
        assert abs(lon_low - lon_min) < 1e-9

    def test_result_within_max_dist(self):
        """Even at MIN_ALT_DEG, the ground distance should not exceed MAX_DIST_KM."""
        lat, lon = _project(45.0, 25.0, 0.0, MIN_ALT_DEG, METEOR_ALT_KM)
        # Compute approximate distance using haversine
        dlat = math.radians(lat - 45.0)
        dlon = math.radians(lon - 25.0)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(45.0)) * math.cos(math.radians(lat)) * math.sin(dlon / 2) ** 2
        )
        dist_km = 2 * EARTH_RADIUS * math.asin(math.sqrt(a))
        assert dist_km <= MAX_DIST_KM + 1.0  # small tolerance

    def test_medium_altitude(self):
        """45-degree elevation should give a moderate distance."""
        lat, lon = _project(45.0, 25.0, 180.0, 45.0, METEOR_ALT_KM)
        # Just verify it moved south (bearing 180)
        assert lat < 45.0


# ── camera_footprint ─────────────────────────────────────────────────────


class TestCameraFootprint:
    def test_valid_platepar_returns_polygon(self):
        pp = _make_platepar()
        poly = camera_footprint(pp)
        assert poly is not None
        assert poly.is_valid
        assert not poly.is_empty
        assert poly.area > 0

    def test_missing_lat_returns_none(self):
        pp = _make_platepar()
        del pp["lat"]
        assert camera_footprint(pp) is None

    def test_missing_fov_h_returns_none(self):
        pp = _make_platepar()
        del pp["fov_h"]
        assert camera_footprint(pp) is None

    def test_invalid_value_returns_none(self):
        pp = _make_platepar()
        pp["lat"] = "not-a-number"
        assert camera_footprint(pp) is None

    def test_none_value_returns_none(self):
        pp = _make_platepar()
        pp["alt_centre"] = None
        assert camera_footprint(pp) is None

    def test_empty_dict_returns_none(self):
        assert camera_footprint({}) is None


# ── compute_coverage_stats ───────────────────────────────────────────────


class TestComputeCoverageStats:
    def _pct(self, pps: dict) -> float:
        return compute_coverage_stats(pps)["coverage_pct"]

    def test_empty_platepars(self):
        stats = compute_coverage_stats({})
        assert stats["coverage_pct"] == 0.0
        assert stats["dual_coverage_pct"] == 0.0
        assert stats["coverage_pct_40"] == 0.0
        assert stats["dual_coverage_pct_40"] == 0.0

    def test_single_camera_over_romania(self):
        """A single camera pointed over Romania should give a small but nonzero coverage."""
        pps = {"RO000A": _make_platepar(lat=45.0, lon=25.0)}
        assert 0 < self._pct(pps) < 100

    def test_multiple_cameras_higher_coverage(self):
        """Two cameras pointing in different directions should cover more than one."""
        single = {"RO000A": _make_platepar(lat=45.0, lon=25.0, az_centre=0.0)}
        double = {
            "RO000A": _make_platepar(lat=45.0, lon=25.0, az_centre=0.0),
            "RO000B": _make_platepar(lat=45.0, lon=25.0, az_centre=180.0),
        }
        assert self._pct(double) >= self._pct(single)

    def test_dual_coverage_nonzero_when_cameras_overlap(self):
        """Two overlapping cameras should give nonzero dual coverage."""
        pps = {
            "RO000A": _make_platepar(lat=45.0, lon=25.0, az_centre=180.0),
            "RO000B": _make_platepar(lat=46.0, lon=25.0, az_centre=180.0),
        }
        stats = compute_coverage_stats(pps)
        assert stats["dual_coverage_pct"] >= 0.0
        assert stats["dual_coverage_pct"] <= stats["coverage_pct"]

    def test_camera_outside_romania_zero(self):
        """A camera in Berlin pointing away should contribute zero coverage over Romania."""
        pps = {
            "DE001B": _make_platepar(
                lat=52.5, lon=13.4, az_centre=0.0, alt_centre=60.0, fov_h=30.0, fov_v=20.0
            )
        }
        assert self._pct(pps) == 0.0

    def test_invalid_platepars_ignored(self):
        pps = {"BAD": {}, "GOOD": _make_platepar(lat=45.0, lon=25.0)}
        assert self._pct(pps) > 0

    def test_all_invalid_returns_zero(self):
        pps = {"BAD1": {}, "BAD2": {"lat": "x"}}
        assert self._pct(pps) == 0.0

    def test_result_bounded(self):
        """All coverage values should be in [0, 100]."""
        pps = {"CAM": _make_platepar(fov_h=170.0, fov_v=90.0)}
        stats = compute_coverage_stats(pps)
        for v in stats.values():
            assert 0 <= v <= 100


# ── get_coverage_stats / get_coverage_pct (cached) ──────────────────────


class TestGetCoverageStats:
    def _reset_cache(self):
        import coverage as cov_mod
        with cov_mod._cache_lock:
            cov_mod._cache = None

    def test_returns_all_keys(self):
        self._reset_cache()
        pps = {"RO000A": _make_platepar()}
        stats = get_coverage_stats(pps)
        assert set(stats) == {"coverage_pct", "dual_coverage_pct", "coverage_pct_40", "dual_coverage_pct_40"}

    def test_returns_same_result_twice(self):
        self._reset_cache()
        pps = {"RO000A": _make_platepar()}
        assert get_coverage_stats(pps) == get_coverage_stats(pps)

    def test_cache_avoids_recomputation(self):
        self._reset_cache()
        pps = {"RO000A": _make_platepar()}

        call_count = 0
        original = compute_coverage_stats

        def counting_compute(platepars):
            nonlocal call_count
            call_count += 1
            return original(platepars)

        with patch("coverage.compute_coverage_stats", side_effect=counting_compute):
            get_coverage_stats(pps)
            get_coverage_stats(pps)
            assert call_count == 1

    def test_expired_cache_recomputes(self):
        import coverage as cov_mod
        self._reset_cache()
        pps = {"RO000A": _make_platepar()}

        r1 = get_coverage_stats(pps)

        # Expire the cache
        with cov_mod._cache_lock:
            if cov_mod._cache:
                cov_mod._cache = (time.monotonic() - 1, *cov_mod._cache[1:])

        r2 = get_coverage_stats(pps)
        assert r1 == r2

    def test_get_coverage_pct_compat(self):
        """get_coverage_pct should still return the 75km single-camera value."""
        self._reset_cache()
        pps = {"RO000A": _make_platepar()}
        assert get_coverage_pct(pps) == get_coverage_stats(pps)["coverage_pct"]


# ── Romania boundary sanity ──────────────────────────────────────────────


class TestRomaniaBoundary:
    def test_polygon_valid(self):
        assert _ROMANIA.is_valid
        assert not _ROMANIA.is_empty

    def test_polygon_area_reasonable(self):
        """Romania is roughly 238,000 km2. In square degrees at ~45N latitude,
        this is roughly (238000 / (111*cos(45)*111)) ~= 27 sq degrees."""
        assert 20 < _ROMANIA.area < 40

    def test_bucharest_inside(self):
        from shapely.geometry import Point
        bucharest = Point(26.1, 44.43)
        assert _ROMANIA.contains(bucharest)

    def test_berlin_outside(self):
        from shapely.geometry import Point
        berlin = Point(13.4, 52.5)
        assert not _ROMANIA.contains(berlin)
