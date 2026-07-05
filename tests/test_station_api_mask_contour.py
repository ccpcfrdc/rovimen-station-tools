"""Tests for station_api mask-contour derivation used by FOV polygon rendering.

The dashboard overview map walks the camera mask boundary (when present) so the
rendered FOV reflects the actual unmasked sky area. The station API derives that
boundary from the RMS ``mask.bmp`` (0 = masked, >0 = active sky).
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

import station_api


def _write_mask(path, arr: np.ndarray) -> None:
    Image.fromarray(arr.astype(np.uint8), "L").save(path)


class TestMaskContour:
    def test_full_mask_returns_none(self, tmp_path):
        """An all-active mask has no occlusions → fall back to sensor edges."""
        mask = tmp_path / "mask.bmp"
        _write_mask(mask, np.full((720, 1280), 255, dtype=np.uint8))
        assert station_api._mask_contour(mask) is None

    def test_horizon_mask_produces_contour(self, tmp_path):
        """Bottom horizon band masked → contour exists and clips the bottom."""
        arr = np.full((720, 1280), 255, dtype=np.uint8)
        arr[600:, :] = 0  # bottom 120 rows masked (horizon / trees)
        mask = tmp_path / "mask.bmp"
        _write_mask(mask, arr)

        poly = station_api._mask_contour(mask)
        assert poly is not None
        assert len(poly) >= 6
        # All points are normalised into [-1, 1] for both axes.
        for nx, ny in poly:
            assert -1.0 <= nx <= 1.0
            assert -1.0 <= ny <= 1.0
        # Bottom boundary should sit near the mask edge (row ~600 → ny ~0.667),
        # well above the full-sensor bottom edge (ny = +1.0).
        max_ny = max(ny for _, ny in poly)
        assert max_ny < 0.9

    def test_contour_point_budget_respected(self, tmp_path):
        """Polygon size is bounded by max_points (cheap / downsampled)."""
        arr = np.full((720, 1280), 255, dtype=np.uint8)
        arr[650:, :] = 0
        mask = tmp_path / "mask.bmp"
        _write_mask(mask, arr)

        poly = station_api._mask_contour(mask, max_points=20)
        assert poly is not None
        assert len(poly) <= 20

    def test_fully_masked_returns_none(self, tmp_path):
        """A degenerate all-masked bitmap yields no usable contour."""
        mask = tmp_path / "mask.bmp"
        _write_mask(mask, np.zeros((720, 1280), dtype=np.uint8))
        assert station_api._mask_contour(mask) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert station_api._mask_contour(tmp_path / "does_not_exist.bmp") is None


class TestFindLatestMask:
    def test_returns_none_when_absent(self, tmp_path):
        cfg = {"stations": {"RO000H": {"rms_data_path": str(tmp_path / "rms")}}}
        assert station_api._find_latest_mask("RO000H", cfg) is None

    def test_finds_session_mask(self, tmp_path):
        rms = tmp_path / "rms"
        session = rms / "CapturedFiles" / "RO000H_20260607_180000"
        session.mkdir(parents=True)
        mask = session / "mask.bmp"
        arr = np.full((720, 1280), 255, dtype=np.uint8)
        arr[600:, :] = 0
        _write_mask(mask, arr)

        cfg = {"stations": {"RO000H": {"rms_data_path": str(rms)}}}
        found = station_api._find_latest_mask("RO000H", cfg)
        assert found == mask
