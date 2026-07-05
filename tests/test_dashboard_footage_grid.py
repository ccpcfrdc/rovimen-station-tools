"""Tests for the all-footage grid re-shaping logic.

The footage grid (/api/footage/grid/<date>) buckets every continuous color
chunk -- not just locked detections -- into time buckets x cameras, every clip
shown. These helpers are pure (no Flask app, no station fan-out), so they're
tested directly.
"""

from __future__ import annotations

from routes.detections import (
    _build_footage_grid,
    _cap_footage_window,
    _hhmmss_to_min,
    _in_footage_window,
    _noon_anchor,
)


class TestHhmmssToMin:
    def test_valid(self):
        assert _hhmmss_to_min("21:30:45") == 21 * 60 + 30
        assert _hhmmss_to_min("00:00:00") == 0
        assert _hhmmss_to_min("21:30") == 21 * 60 + 30  # HH:MM also accepted

    def test_none_and_garbage(self):
        assert _hhmmss_to_min(None) is None
        assert _hhmmss_to_min("") is None
        assert _hhmmss_to_min("not-a-time") is None

    def test_out_of_range(self):
        assert _hhmmss_to_min("25:00:00") is None
        assert _hhmmss_to_min("21:75:00") is None


class TestNoonAnchor:
    def test_morning_sorts_after_evening(self):
        # 22:00 evening, 02:00 next morning -- morning must rank later.
        assert _noon_anchor(22 * 60) < _noon_anchor(2 * 60)

    def test_noon_is_zero(self):
        assert _noon_anchor(12 * 60) == 0


class TestInFootageWindow:
    def test_no_bounds_always_true(self):
        assert _in_footage_window(600, None, None) is True

    def test_simple_inclusive_range(self):
        assert _in_footage_window(21 * 60, 21 * 60, 23 * 60) is True
        assert _in_footage_window(23 * 60, 21 * 60, 23 * 60) is True
        assert _in_footage_window(20 * 60, 21 * 60, 23 * 60) is False
        assert _in_footage_window(23 * 60 + 1, 21 * 60, 23 * 60) is False

    def test_window_crossing_midnight(self):
        # 22:00 -> 02:00 wraps; 23:30 in, 00:30 in, 12:00 out.
        f, t = 22 * 60, 2 * 60
        assert _in_footage_window(23 * 60 + 30, f, t) is True
        assert _in_footage_window(30, f, t) is True
        assert _in_footage_window(12 * 60, f, t) is False

    def test_one_sided_bounds(self):
        assert _in_footage_window(21 * 60, 20 * 60, None) is True
        assert _in_footage_window(19 * 60, 20 * 60, None) is False
        assert _in_footage_window(19 * 60, None, 20 * 60) is True
        assert _in_footage_window(21 * 60, None, 20 * 60) is False


class TestCapFootageWindow:
    def test_within_cap_unchanged(self):
        assert _cap_footage_window(22 * 60, 22 * 60 + 3, 5) == (22 * 60, 22 * 60 + 3)

    def test_over_cap_clamped(self):
        # 22:00 -> 22:30 (30 min) clamps the end to 22:05.
        assert _cap_footage_window(22 * 60, 22 * 60 + 30, 5) == (22 * 60, 22 * 60 + 5)

    def test_single_bound_expands_to_full_window(self):
        assert _cap_footage_window(22 * 60, None, 5) == (22 * 60, 22 * 60 + 5)
        assert _cap_footage_window(None, 22 * 60, 5) == (22 * 60 - 5, 22 * 60)

    def test_zero_span_clamped(self):
        assert _cap_footage_window(22 * 60, 22 * 60, 5) == (22 * 60, 22 * 60 + 5)

    def test_crossing_midnight_within_cap_unchanged(self):
        # 23:58 -> 00:02 is 4 min, under the cap.
        assert _cap_footage_window(23 * 60 + 58, 2, 5) == (23 * 60 + 58, 2)

    def test_crossing_midnight_over_cap_clamped(self):
        # 23:58 -> 00:10 is 12 min; clamp end to 00:03.
        assert _cap_footage_window(23 * 60 + 58, 10, 5) == (23 * 60 + 58, 3)

    def test_both_none_untouched(self):
        assert _cap_footage_window(None, None, 5) == (None, None)


_COLUMNS = [
    {"cam": "RO000A", "host_key": "gmnro01", "station_label": "Ghirdoveni"},
    {"cam": "RO000M", "host_key": "gmnro02", "station_label": "Vaslui"},
]


def _chunk(t, *, filename=None, stack="s.webp", locked=False, offset=None, mt=None):
    return {
        "filename": filename or f"c_{t.replace(':', '')}_color.mkv",
        "time": t,
        "stack": stack,
        "locked": locked,
        "meteor_time": mt,
        "detection_offset_s": offset,
    }


def _footage(*, a=None, m=None):
    """Build a footage_by_cam dict for RO000A (gmnro01) and RO000M (gmnro02)."""
    out: dict[str, dict] = {}
    if a is not None:
        out["RO000A"] = {"host_key": "gmnro01", "station_label": "Ghirdoveni", "chunks": a}
    if m is not None:
        out["RO000M"] = {"host_key": "gmnro02", "station_label": "Vaslui", "chunks": m}
    return out


class TestBuildFootageGrid:
    def test_buckets_by_time_and_camera(self):
        fb = _footage(
            a=[_chunk("21:32:00"), _chunk("21:33:20")],   # same 5-min bucket
            m=[_chunk("21:33:30")],                        # same bucket, other cam
        )
        rows = _build_footage_grid(fb, _COLUMNS, 5, None, None)
        assert len(rows) == 1
        row = rows[0]
        assert row["label"] == "21:30"
        assert row["total"] == 3
        assert row["station_count"] == 2
        assert set(row["cells"].keys()) == {"RO000A", "RO000M"}
        assert row["cells"]["RO000A"]["count"] == 2
        # Cell carries the full (time-sorted) chunk list — every clip is shown.
        assert len(row["cells"]["RO000A"]["chunks"]) == 2
        assert [c["time"] for c in row["cells"]["RO000A"]["chunks"]] == ["21:32:00", "21:33:20"]

    def test_rows_chronological_earliest_first_across_midnight(self):
        fb = _footage(
            a=[_chunk("22:05:00"), _chunk("23:50:00"), _chunk("00:30:00")],
        )
        rows = _build_footage_grid(fb, _COLUMNS, 5, None, None)
        # noon->noon night: evening first, post-midnight last.
        assert [r["label"] for r in rows] == ["22:05", "23:50", "00:30"]

    def test_detection_flag_set_when_bucket_has_locked_clip(self):
        fb = _footage(
            a=[_chunk("21:32:00"), _chunk("21:33:00", filename="DET", locked=True)],
        )
        rows = _build_footage_grid(fb, _COLUMNS, 5, None, None)
        cell = rows[0]["cells"]["RO000A"]
        assert cell["has_detection"] is True
        assert rows[0]["has_detection"] is True

    def test_no_detection_flag_for_plain_footage(self):
        fb = _footage(a=[_chunk("21:32:00")])
        rows = _build_footage_grid(fb, _COLUMNS, 5, None, None)
        assert rows[0]["has_detection"] is False
        assert rows[0]["cells"]["RO000A"]["has_detection"] is False

    def test_time_window_filters(self):
        fb = _footage(a=[_chunk("20:00:00"), _chunk("22:00:00"), _chunk("23:30:00")])
        rows = _build_footage_grid(fb, _COLUMNS, 5, 21 * 60, 23 * 60)
        assert [r["label"] for r in rows] == ["22:00"]

    def test_window_crossing_midnight(self):
        fb = _footage(a=[_chunk("22:30:00"), _chunk("12:00:00"), _chunk("00:30:00")])
        rows = _build_footage_grid(fb, _COLUMNS, 5, 22 * 60, 2 * 60)
        assert [r["label"] for r in rows] == ["22:30", "00:30"]

    def test_unknown_camera_dropped(self):
        fb = {"RO999X": {"host_key": "x", "station_label": "x", "chunks": [_chunk("21:32:00")]}}
        assert _build_footage_grid(fb, _COLUMNS, 5, None, None) == []

    def test_malformed_time_skipped_not_crashed(self):
        fb = _footage(a=[_chunk("not-a-time"), _chunk("21:32:00")])
        rows = _build_footage_grid(fb, _COLUMNS, 5, None, None)
        assert len(rows) == 1
        assert rows[0]["total"] == 1

    def test_bucket_width_one_minute(self):
        fb = _footage(a=[_chunk("21:32:10"), _chunk("21:33:10")])
        rows = _build_footage_grid(fb, _COLUMNS, 1, None, None)
        assert len(rows) == 2

    def test_empty_input(self):
        assert _build_footage_grid({}, _COLUMNS, 5, None, None) == []
