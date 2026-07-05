"""Tests for the detection-grid re-shaping logic (issue #313).

The grid view (/api/detections/grid/<date>) buckets the cached flat detections
list into time-bucket x camera rows. These helpers are pure -- no Flask app, no
station fan-out -- so they're tested directly.
"""

from __future__ import annotations

from types import SimpleNamespace

from routes.detections import (
    _build_detection_grid,
    _grid_columns,
    _parse_hhmm,
)


class TestParseHHMM:
    def test_valid(self):
        assert _parse_hhmm("21:30") == 21 * 60 + 30
        assert _parse_hhmm("00:00") == 0
        assert _parse_hhmm("24:00") == 24 * 60
        assert _parse_hhmm("3:05") == 3 * 60 + 5

    def test_trims_whitespace(self):
        assert _parse_hhmm("  21:30 ") == 21 * 60 + 30

    def test_none_and_empty(self):
        assert _parse_hhmm(None) is None
        assert _parse_hhmm("") is None
        assert _parse_hhmm("   ") is None

    def test_out_of_range(self):
        assert _parse_hhmm("25:00") is None
        assert _parse_hhmm("21:60") is None
        assert _parse_hhmm("24:30") is None

    def test_garbage(self):
        assert _parse_hhmm("abc") is None
        assert _parse_hhmm("2130") is None  # only HH:MM accepted server-side


def _fake_config():
    """Two stations, sorted by host_key, with one and two cameras."""
    return SimpleNamespace(
        stations={
            "gmnro02": SimpleNamespace(
                label="Vaslui",
                cameras=[SimpleNamespace(code="RO000M")],
            ),
            "gmnro01": SimpleNamespace(
                label="Ghirdoveni",
                cameras=[
                    SimpleNamespace(code="RO000A"),
                    SimpleNamespace(code="RO000B"),
                ],
            ),
        }
    )


class TestGridColumns:
    def test_sorted_by_host_key_then_camera_order(self):
        cols = _grid_columns(_fake_config())
        assert [c["cam"] for c in cols] == ["RO000A", "RO000B", "RO000M"]
        assert cols[0] == {
            "cam": "RO000A",
            "host_key": "gmnro01",
            "station_label": "Ghirdoveni",
        }
        assert cols[2]["station_label"] == "Vaslui"


def _det(host, cam, t, *, filename="f.mkv", stack="s.jpg", offset=2.0):
    return {
        "host_key": host,
        "cam": cam,
        "station_label": host,
        "filename": filename,
        "meteor_time": t,
        "detection_offset_s": offset,
        "stack": stack,
        "rms": None,
    }


_COLUMNS = [
    {"cam": "RO000A", "host_key": "gmnro01", "station_label": "Ghirdoveni"},
    {"cam": "RO000M", "host_key": "gmnro02", "station_label": "Vaslui"},
]


class TestBuildDetectionGrid:
    def test_buckets_by_time_and_camera(self):
        dets = [
            _det("gmnro01", "RO000A", "2026-04-01T21:32:00"),
            _det("gmnro02", "RO000M", "2026-04-01T21:33:30"),  # same 5-min bucket
            _det("gmnro01", "RO000A", "2026-04-01T21:41:00"),  # next bucket
        ]
        rows = _build_detection_grid(dets, [], _COLUMNS, 5, None, None)
        assert len(rows) == 2
        # Newest first
        assert rows[0]["label"] == "21:40"
        assert rows[1]["label"] == "21:30"
        # 21:30 bucket has both cameras
        b = rows[1]
        assert b["total"] == 2
        assert b["station_count"] == 2
        assert set(b["cells"].keys()) == {"RO000A", "RO000M"}

    def test_only_populated_buckets_returned(self):
        dets = [_det("gmnro01", "RO000A", "2026-04-01T21:32:00")]
        rows = _build_detection_grid(dets, [], _COLUMNS, 5, None, None)
        assert len(rows) == 1

    def test_time_window_filters(self):
        dets = [
            _det("gmnro01", "RO000A", "2026-04-01T20:00:00"),
            _det("gmnro01", "RO000A", "2026-04-01T22:00:00"),
            _det("gmnro01", "RO000A", "2026-04-01T23:30:00"),
        ]
        rows = _build_detection_grid(
            dets, [], _COLUMNS, 5, 21 * 60, 23 * 60
        )
        labels = [r["label"] for r in rows]
        assert labels == ["22:00"]

    def test_unknown_camera_dropped(self):
        dets = [_det("gmnro09", "RO999X", "2026-04-01T21:32:00")]
        rows = _build_detection_grid(dets, [], _COLUMNS, 5, None, None)
        assert rows == []

    def test_malformed_meteor_time_skipped_not_crashed(self):
        dets = [
            _det("gmnro01", "RO000A", "not-a-date"),
            _det("gmnro02", "RO000M", "2026-04-01T21:32:00"),
        ]
        rows = _build_detection_grid(dets, [], _COLUMNS, 5, None, None)
        assert len(rows) == 1
        assert rows[0]["total"] == 1

    def test_multi_flag_from_events(self):
        dets = [
            _det("gmnro01", "RO000A", "2026-04-01T21:32:00", filename="a.mkv"),
            _det("gmnro02", "RO000M", "2026-04-01T21:32:01", filename="m.mkv"),
        ]
        events = [{
            "witness_count": 2,
            "witnesses": [
                {"filename": "a.mkv", "cam": "RO000A"},
                {"filename": "m.mkv", "cam": "RO000M"},
            ],
        }]
        rows = _build_detection_grid(dets, events, _COLUMNS, 5, None, None)
        assert rows[0]["has_multi"] is True
        assert rows[0]["cells"]["RO000A"][0]["multi"] is True

    def test_single_witness_event_not_multi(self):
        dets = [_det("gmnro01", "RO000A", "2026-04-01T21:32:00", filename="a.mkv")]
        events = [{
            "witness_count": 1,
            "witnesses": [{"filename": "a.mkv", "cam": "RO000A"}],
        }]
        rows = _build_detection_grid(dets, events, _COLUMNS, 5, None, None)
        assert rows[0]["has_multi"] is False
        assert rows[0]["cells"]["RO000A"][0]["multi"] is False

    def test_bucket_width_one_minute(self):
        dets = [
            _det("gmnro01", "RO000A", "2026-04-01T21:32:10"),
            _det("gmnro01", "RO000A", "2026-04-01T21:33:10"),
        ]
        rows = _build_detection_grid(dets, [], _COLUMNS, 1, None, None)
        assert len(rows) == 2

    def test_empty_input(self):
        assert _build_detection_grid([], [], _COLUMNS, 5, None, None) == []
