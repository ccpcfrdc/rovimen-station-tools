"""Tests for overlay.py -- OSD annotation utilities."""

from __future__ import annotations

import pytest

from overlay import build_drawtext_annotations, measure_text_height, measure_text_width


@pytest.fixture
def make_overlay_cfg(tmp_path):
    """Return a factory that produces a valid overlay config dict.

    Creates a temporary empty file to satisfy the font-path existence check.
    PIL font loading will fail gracefully, triggering the monospace fallback.
    """
    font_file = tmp_path / "fake_font.ttf"
    font_file.write_bytes(b"")

    def _make(**overrides) -> dict:
        base = {
            "font": str(font_file),
            "font_size": 20,
            "text_opacity": 0.4,
            "coords": "45.0N 25.0E",
            "network": "ROVIMEN",
            "style": "standard",
            "show_logo": False,
            "show_station": True,
            "show_coords": True,
            "show_pointing": True,
            "show_timestamp": True,
            "show_network": True,
            "az": 180.0,
            "alt": 45.0,
        }
        base.update(overrides)
        return base

    return _make


# ---- measure_text_width ----


class TestMeasureTextWidth:
    def test_fallback_with_nonexistent_font(self):
        """Non-existent font triggers the monospace estimate."""
        text = "Hello"
        result = measure_text_width("/no/such/font.ttf", 20, text)
        assert result == int(20 * 0.65 * len(text))

    def test_monospace_estimate_math(self):
        """Verify the exact monospace formula: font_size * 0.65 * len(text)."""
        assert measure_text_width("/no/such/font.ttf", 30, "ABC") == int(30 * 0.65 * 3)
        assert measure_text_width("/no/such/font.ttf", 10, "X") == int(10 * 0.65 * 1)
        assert measure_text_width("/no/such/font.ttf", 40, "ABCDEFGHIJ") == int(40 * 0.65 * 10)

    def test_longer_text_wider(self):
        """Longer text must produce a wider measurement."""
        short = measure_text_width("/no/such/font.ttf", 20, "Hi")
        long_ = measure_text_width("/no/such/font.ttf", 20, "Hello World")
        assert long_ > short

    def test_empty_text_returns_zero(self):
        """Empty string should measure as 0 width."""
        assert measure_text_width("/no/such/font.ttf", 20, "") == 0


# ---- measure_text_height ----


class TestMeasureTextHeight:
    def test_fallback_with_nonexistent_font(self):
        """Non-existent font falls back to font_size."""
        assert measure_text_height("/no/such/font.ttf", 24) == 24

    def test_returns_font_size_as_fallback(self):
        """Multiple font sizes all fall back correctly."""
        for size in (10, 19, 32, 64):
            assert measure_text_height("/no/such/font.ttf", size) == size


# ---- build_drawtext_annotations ----


class TestBuildDrawtextAnnotations:
    STATION_ID = "RO000H"
    CHUNK_EPOCH = 1717200000

    def test_missing_font_returns_empty(self):
        """When the font file does not exist, return ([], 0)."""
        cfg = {"font": "/no/such/font.ttf", "font_size": 20, "style": "standard"}
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert filters == []
        assert bar_h == 0

    def test_standard_style_returns_filters_bar_zero(self, make_overlay_cfg):
        """Standard style produces drawtext filters with bar_h == 0."""
        cfg = make_overlay_cfg(style="standard")
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert len(filters) > 0
        assert bar_h == 0

    def test_cinema_style_returns_filters_bar_positive(self, make_overlay_cfg):
        """Cinema style produces drawtext filters with bar_h > 0."""
        cfg = make_overlay_cfg(style="cinema")
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert len(filters) > 0
        assert bar_h > 0

    def test_cinema_bar_h_aligned_to_16px(self, make_overlay_cfg):
        """Cinema bar height is always aligned to 16px boundary."""
        for font_size in (10, 19, 24, 32, 48):
            cfg = make_overlay_cfg(style="cinema", font_size=font_size)
            _, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
            assert bar_h % 16 == 0, f"bar_h={bar_h} not aligned to 16px for font_size={font_size}"

    def test_show_network_false_excludes_network(self, make_overlay_cfg):
        """Disabling show_network removes network text from filters."""
        cfg = make_overlay_cfg(show_network=False)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "ROVIMEN" not in combined

    def test_show_timestamp_false_excludes_timestamp(self, make_overlay_cfg):
        """Disabling show_timestamp removes strftime expansion."""
        cfg = make_overlay_cfg(show_timestamp=False)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "basetime" not in combined
        assert "strftime" not in combined

    def test_show_station_false_excludes_station_id(self, make_overlay_cfg):
        """Disabling show_station removes the station ID from filters."""
        cfg = make_overlay_cfg(show_station=False, show_coords=False)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert self.STATION_ID not in combined

    def test_show_coords_false_excludes_coordinates(self, make_overlay_cfg):
        """Disabling show_coords removes coordinate text from filters."""
        cfg = make_overlay_cfg(show_coords=False, show_station=False)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "45.0N 25.0E" not in combined

    def test_show_pointing_false_excludes_az_alt(self, make_overlay_cfg):
        """Disabling show_pointing removes az/alt text from filters."""
        cfg = make_overlay_cfg(show_pointing=False)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "ALT" not in combined
        assert "AZ" not in combined

    def test_all_show_flags_false_returns_empty(self, make_overlay_cfg):
        """With all show flags disabled, no filters are generated."""
        cfg = make_overlay_cfg(
            show_network=False,
            show_station=False,
            show_coords=False,
            show_pointing=False,
            show_timestamp=False,
        )
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert filters == []

    def test_filters_contain_font_path_and_size(self, make_overlay_cfg):
        """Every filter references the configured font path and font size."""
        cfg = make_overlay_cfg(font_size=24)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        font_path = cfg["font"]
        for f in filters:
            assert f"fontfile={font_path}" in f
            assert "fontsize=24" in f

    def test_station_cfg_az_alt_overrides_overlay(self, make_overlay_cfg):
        """station_cfg az/alt take precedence over overlay_cfg values."""
        cfg = make_overlay_cfg(az=100.0, alt=30.0)
        station_cfg = {"az": 270.5, "alt": 55.3}
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, station_cfg, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "ALT 55.3" in combined
        assert "AZ 270.5" in combined
        # Verify the overlay_cfg values are NOT present
        assert "ALT 30.0" not in combined
        assert "AZ 100.0" not in combined

    def test_chunk_epoch_in_timestamp_filter(self, make_overlay_cfg):
        """The chunk_epoch appears as basetime (microseconds) in the timestamp filter."""
        epoch = 1717200000
        cfg = make_overlay_cfg()
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, epoch)
        combined = " ".join(filters)
        expected_basetime = str(epoch * 1000000)
        assert f"basetime={expected_basetime}" in combined

    def test_text_opacity_in_fontcolor(self, make_overlay_cfg):
        """The text_opacity value appears as alpha in fontcolor."""
        cfg = make_overlay_cfg(text_opacity=0.7)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        for f in filters:
            assert "fontcolor=white@0.70" in f

    # -- Cinema-specific tests --

    def test_cinema_show_network_false(self, make_overlay_cfg):
        """Cinema style with show_network=False excludes network text."""
        cfg = make_overlay_cfg(style="cinema", show_network=False)
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "ROVIMEN" not in combined
        assert bar_h > 0

    def test_cinema_all_flags_false_returns_empty(self, make_overlay_cfg):
        """Cinema style with all show flags disabled returns empty filters."""
        cfg = make_overlay_cfg(
            style="cinema",
            show_network=False,
            show_station=False,
            show_coords=False,
            show_pointing=False,
            show_timestamp=False,
        )
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert filters == []

    def test_standard_network_at_top_left(self, make_overlay_cfg):
        """In standard style, network drawtext is positioned at top-left (x=MARGIN, y=MARGIN)."""
        cfg = make_overlay_cfg(style="standard")
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        # The network filter should have x=14:y=14 (MARGIN=14)
        network_filters = [f for f in filters if "ROVIMEN" in f]
        assert len(network_filters) == 1
        assert ":x=14:" in network_filters[0]
        assert ":y=14" in network_filters[0]

    def test_standard_timestamp_at_bottom_right(self, make_overlay_cfg):
        """In standard style, timestamp is positioned at bottom-right."""
        cfg = make_overlay_cfg(style="standard")
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        ts_filters = [f for f in filters if "basetime" in f]
        assert len(ts_filters) == 1
        # Bottom-right: x uses w-MARGIN-tw, y uses h-MARGIN-th
        assert "w-14-tw" in ts_filters[0]
        assert "h-14-th" in ts_filters[0]
