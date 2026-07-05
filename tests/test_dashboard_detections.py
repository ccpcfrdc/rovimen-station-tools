"""Tests for dashboard detection payload clustering — P0 #5.

P0 #5: A malformed meteor_time from any station crashes
       _compute_detections_payload for ALL users (datetime.fromisoformat
       at line 7084 is not wrapped in try/except).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


class TestDetectionClustering:
    """Test the greedy clustering logic from _compute_detections_payload
    (lines 7076-7111) in isolation."""

    @staticmethod
    def _cluster(all_det: list[dict], window: float = 1.0) -> list[dict]:
        """Replicate the clustering logic from rovimen_dashboard.py."""
        all_det.sort(key=lambda x: x["meteor_time"])
        used: set[int] = set()
        events: list[dict] = []
        for i, det in enumerate(all_det):
            if i in used:
                continue
            group = [det]
            used.add(i)
            t0 = datetime.fromisoformat(det["meteor_time"])
            for j in range(i + 1, len(all_det)):
                if j in used:
                    continue
                t1 = datetime.fromisoformat(all_det[j]["meteor_time"])
                if abs((t1 - t0).total_seconds()) <= window:
                    group.append(all_det[j])
                    used.add(j)
            seen: dict[tuple, dict] = {}
            for w in group:
                key = (w["host_key"], w["cam"])
                if key not in seen or (w.get("detection_offset_s") or 0) > (seen[key].get("detection_offset_s") or 0):
                    seen[key] = w
            witnesses = list(seen.values())
            station_set = {w["host_key"] for w in witnesses}
            if len(station_set) >= 2:
                events.append({
                    "event_time": det["meteor_time"],
                    "witness_count": len(witnesses),
                    "station_count": len(station_set),
                    "witnesses": witnesses,
                })
        return events

    def test_two_stations_within_window_creates_event(self):
        dets = [
            {"host_key": "gmnro01", "cam": "RO000A", "meteor_time": "2026-04-01T21:30:05.123",
             "detection_offset_s": 5.0, "stack": None, "time": "21:30:05"},
            {"host_key": "gmnro02", "cam": "RO000M", "meteor_time": "2026-04-01T21:30:05.800",
             "detection_offset_s": 3.0, "stack": None, "time": "21:30:05"},
        ]
        events = self._cluster(dets, window=1.0)
        assert len(events) == 1
        assert events[0]["station_count"] == 2

    def test_single_station_no_event(self):
        dets = [
            {"host_key": "gmnro01", "cam": "RO000A", "meteor_time": "2026-04-01T21:30:05.123",
             "detection_offset_s": 5.0, "stack": None, "time": "21:30:05"},
            {"host_key": "gmnro01", "cam": "RO000B", "meteor_time": "2026-04-01T21:30:05.800",
             "detection_offset_s": 3.0, "stack": None, "time": "21:30:05"},
        ]
        events = self._cluster(dets, window=1.0)
        assert len(events) == 0

    def test_outside_window_separate(self):
        dets = [
            {"host_key": "gmnro01", "cam": "RO000A", "meteor_time": "2026-04-01T21:30:05.000",
             "detection_offset_s": 5.0, "stack": None, "time": "21:30:05"},
            {"host_key": "gmnro02", "cam": "RO000M", "meteor_time": "2026-04-01T21:30:10.000",
             "detection_offset_s": 3.0, "stack": None, "time": "21:30:10"},
        ]
        events = self._cluster(dets, window=1.0)
        assert len(events) == 0

    def test_dedup_same_camera_keeps_larger_offset(self):
        """Same (host_key, cam) in one cluster keeps the one with larger offset."""
        dets = [
            {"host_key": "gmnro01", "cam": "RO000A", "meteor_time": "2026-04-01T21:30:05.100",
             "detection_offset_s": 3.0, "stack": None, "time": "21:30:05"},
            {"host_key": "gmnro01", "cam": "RO000A", "meteor_time": "2026-04-01T21:30:05.200",
             "detection_offset_s": 8.0, "stack": None, "time": "21:30:05"},
            {"host_key": "gmnro02", "cam": "RO000M", "meteor_time": "2026-04-01T21:30:05.300",
             "detection_offset_s": 5.0, "stack": None, "time": "21:30:05"},
        ]
        events = self._cluster(dets, window=1.0)
        assert len(events) == 1
        ro000a_witness = [w for w in events[0]["witnesses"] if w["cam"] == "RO000A"][0]
        assert ro000a_witness["detection_offset_s"] == 8.0

    def test_malformed_meteor_time_crashes(self):
        """Document: a malformed meteor_time crashes the entire clustering.
        This is the P0 bug — datetime.fromisoformat raises ValueError."""
        dets = [
            {"host_key": "gmnro01", "cam": "RO000A", "meteor_time": "not-a-date",
             "detection_offset_s": 5.0, "stack": None, "time": "21:30:05"},
            {"host_key": "gmnro02", "cam": "RO000M", "meteor_time": "2026-04-01T21:30:05.800",
             "detection_offset_s": 3.0, "stack": None, "time": "21:30:05"},
        ]
        with pytest.raises(ValueError, match="Invalid isoformat"):
            self._cluster(dets, window=1.0)

    def test_none_detection_offset_dedup(self):
        """Both offsets None: (None or 0) > (None or 0) → False → first wins."""
        dets = [
            {"host_key": "gmnro01", "cam": "RO000A", "meteor_time": "2026-04-01T21:30:05.100",
             "detection_offset_s": None, "stack": "a", "time": "21:30:05"},
            {"host_key": "gmnro01", "cam": "RO000A", "meteor_time": "2026-04-01T21:30:05.200",
             "detection_offset_s": None, "stack": "b", "time": "21:30:05"},
            {"host_key": "gmnro02", "cam": "RO000M", "meteor_time": "2026-04-01T21:30:05.300",
             "detection_offset_s": 5.0, "stack": None, "time": "21:30:05"},
        ]
        events = self._cluster(dets, window=1.0)
        assert len(events) == 1
        ro000a = [w for w in events[0]["witnesses"] if w["cam"] == "RO000A"][0]
        assert ro000a["stack"] == "a"

    def test_greedy_order_dependence(self):
        """Greedy clustering claims detections in sort order. A detection
        between two potential events is claimed by the first one."""
        dets = [
            {"host_key": "gmnro01", "cam": "RO000A", "meteor_time": "2026-04-01T21:30:00.000",
             "detection_offset_s": 5.0, "stack": None, "time": "21:30:00"},
            {"host_key": "gmnro02", "cam": "RO000M", "meteor_time": "2026-04-01T21:30:00.500",
             "detection_offset_s": 3.0, "stack": None, "time": "21:30:00"},
            {"host_key": "gmnro03", "cam": "RO000H", "meteor_time": "2026-04-01T21:30:01.000",
             "detection_offset_s": 2.0, "stack": None, "time": "21:30:01"},
        ]
        events = self._cluster(dets, window=1.0)
        assert len(events) == 1
        assert events[0]["station_count"] == 3

    def test_empty_input(self):
        assert self._cluster([], window=1.0) == []

    def test_aware_and_naive_mixed_crashes(self):
        """Mixing aware and naive datetimes in meteor_time raises TypeError."""
        dets = [
            {"host_key": "gmnro01", "cam": "RO000A",
             "meteor_time": "2026-04-01T21:30:05+00:00",
             "detection_offset_s": 5.0, "stack": None, "time": "21:30:05"},
            {"host_key": "gmnro02", "cam": "RO000M",
             "meteor_time": "2026-04-01T21:30:05.800",
             "detection_offset_s": 3.0, "stack": None, "time": "21:30:05"},
        ]
        with pytest.raises(TypeError):
            self._cluster(dets, window=1.0)
