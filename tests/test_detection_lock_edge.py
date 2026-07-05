"""Tests for detection_lock.py edge cases — P1 #6, #7.

P1 #6: Chunk matching when chunks have time gaps > segment_secs + 5.
P1 #7: Manual locks survive process_night (confirmed_chunks doesn't include them).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import detection_lock
import flags_manager


STATION = "RO000H"
DATE_STR = "20260322"


@pytest.fixture
def chunk_dir(tmp_path, sample_config):
    """Create chunk directory structure."""
    cap = Path(sample_config["videocapture_path"])
    d = cap / STATION / DATE_STR
    d.mkdir(parents=True)
    return d


def _touch_chunk(chunk_dir: Path, time_str: str) -> Path:
    p = chunk_dir / f"{STATION}_{DATE_STR}_{time_str}_color.mkv"
    p.write_bytes(b"")
    return p


# ── P1 #6: Chunk matching with time gaps ──────────────────────────────────


class TestChunkMatchingWithGaps:
    """_find_chunks_for_time uses next chunk's timestamp as the boundary.
    When there's a gap, it falls back to ts + segment_secs + 5."""

    def test_contiguous_chunks_match(self, chunk_dir):
        """No gaps: detection at 21:00:05 matches chunk starting at 21:00:00.
        FF_DURATION is 10s, so detection at 21:00:05 spans 21:00:05-21:00:15,
        which fits within the 210000 chunk (ends at 210020)."""
        _touch_chunk(chunk_dir, "210000")
        _touch_chunk(chunk_dir, "210020")

        ff_time = datetime(2026, 3, 22, 21, 0, 5)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1
        assert "210000" in result[0].name

    def test_gap_within_fallback_window_still_matches(self, chunk_dir):
        """Gap of segment_secs + 4 (< segment_secs + 5): detection still matches."""
        _touch_chunk(chunk_dir, "210000")
        # Next chunk at 210044 (gap of 24s where segment=20 → fallback window is 25s)
        _touch_chunk(chunk_dir, "210044")

        ff_time = datetime(2026, 3, 22, 21, 0, 10)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1
        assert "210000" in result[0].name

    def test_gap_with_next_chunk_extends_boundary(self, chunk_dir):
        """When a next chunk exists, the boundary extends to next_ts regardless
        of segment_secs. A detection in a gap between two chunks is attributed
        to the earlier chunk — even though the video doesn't cover that time.
        This documents the incorrect-attribution behaviour."""
        _touch_chunk(chunk_dir, "210000")
        # Next chunk at 210200 (120s gap, segment=20)
        _touch_chunk(chunk_dir, "210200")

        # Detection at 21:00:40 — 40s after chunk 1 started, but chunk 1 is
        # only 20s long. Still matches because next_ts = 210200.
        ff_time = datetime(2026, 3, 22, 21, 0, 40)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1
        assert "210000" in result[0].name

    def test_gap_beyond_last_chunk_fallback_misses(self, chunk_dir):
        """The last chunk uses fallback ts + segment_secs + 5. A detection
        beyond that window is not matched."""
        _touch_chunk(chunk_dir, "210000")
        # No more chunks after this one

        # Detection at 21:00:30 — beyond fallback (210000 + 25s = 210025)
        ff_time = datetime(2026, 3, 22, 21, 0, 30)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 0

    def test_detection_at_last_chunk_uses_fallback(self, chunk_dir):
        """Last chunk has no successor: uses ts + segment_secs + 5."""
        _touch_chunk(chunk_dir, "210000")
        _touch_chunk(chunk_dir, "210020")

        # Detection at 21:00:30 — within last chunk's fallback (210020 + 25 = 210045)
        ff_time = datetime(2026, 3, 22, 21, 0, 30)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1
        assert "210020" in result[0].name

    def test_ff_spanning_two_chunks(self, chunk_dir):
        """FF block spanning chunk boundary returns both chunks."""
        _touch_chunk(chunk_dir, "210000")
        _touch_chunk(chunk_dir, "210020")

        # FF at 21:00:18 with FF_DURATION=10s spans into 210020 chunk
        ff_time = datetime(2026, 3, 22, 21, 0, 18)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 2

    def test_no_chunks_returns_empty(self, tmp_path):
        ff_time = datetime(2026, 3, 22, 21, 0, 10)
        result = detection_lock._find_chunks_for_time(ff_time, tmp_path / "nonexistent", segment_secs=20)
        assert result == []

    def test_nonmatching_files_ignored(self, chunk_dir):
        """Files that don't match _CHUNK_RE are silently skipped."""
        (chunk_dir / "README.txt").write_text("ignore me")
        (chunk_dir / "some_other.mkv").write_bytes(b"")
        _touch_chunk(chunk_dir, "210000")

        ff_time = datetime(2026, 3, 22, 21, 0, 10)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1


# ── P1 #7: Manual locks removed by process_night ─────────────────────────


class TestManualLockPreservation:
    """process_night removes locks that aren't in confirmed_chunks.
    Manual locks (lock_type='manual') are also removed.
    This documents the behaviour."""

    def test_manual_lock_removed_as_false_positive(self, chunk_dir, sample_config):
        """A manually locked chunk with no FTPdetectinfo confirmation
        is removed by the false-positive sweep."""
        mkv = _touch_chunk(chunk_dir, "210000")
        _touch_chunk(chunk_dir, "210020")

        # Manually lock chunk via flags_manager
        flags_manager.lock_chunk(
            STATION, DATE_STR, mkv.name,
            {"lock_type": "manual", "reason": "interesting"},
            sample_config,
        )

        state = flags_manager.load(STATION, DATE_STR, sample_config)
        assert state["chunks"][mkv.name]["lock"] is not None

        # process_night reads FTPdetectinfo from ArchivedFiles/<session>/
        rms_path = Path(sample_config["stations"][STATION]["rms_data_path"])
        archived = rms_path / "ArchivedFiles"
        session_dir = archived / f"{STATION}_{DATE_STR}_210000_000000"
        session_dir.mkdir(parents=True)
        ftp = session_dir / f"FTPdetectinfo_{STATION}_{DATE_STR}_210000_000000.txt"
        ftp.write_text(
            "------------------------------\n"
            "Meteor Count = 0\n"
            "------------------------------\n"
        )

        with patch.object(detection_lock, "_stitch_clips", return_value=None):
            detection_lock.process_night(STATION, DATE_STR, sample_config)

        state_after = flags_manager.load(STATION, DATE_STR, sample_config)
        # Manual lock should have been removed (it's not in confirmed_chunks)
        chunk_entry = state_after.get("chunks", {}).get(mkv.name, {})
        assert chunk_entry.get("lock") is None

    def test_detection_lock_preserved(self, chunk_dir, sample_config):
        """A detection-locked chunk that IS in FTPdetectinfo survives."""
        mkv = _touch_chunk(chunk_dir, "213045")

        # process_night reads from ArchivedFiles/<session>/
        rms_path = Path(sample_config["stations"][STATION]["rms_data_path"])
        archived = rms_path / "ArchivedFiles"
        session_dir = archived / f"{STATION}_{DATE_STR}_210000_000000"
        session_dir.mkdir(parents=True)
        ftp = session_dir / f"FTPdetectinfo_{STATION}_{DATE_STR}_210000_000000.txt"
        ftp.write_text(
            "------------------------------\n"
            "Meteor Count = 1\n"
            "------------------------------\n"
            f"FF_{STATION}_{DATE_STR}_213045_000\n"
            "720  576 25.00  fps\n"
            "   1.50  320.5  240.2  1000  5.5  32.1  123.4  45.6\n"
            "------------------------------\n"
        )

        with patch.object(detection_lock, "_stitch_clips", return_value=None):
            detection_lock.process_night(STATION, DATE_STR, sample_config)

        state = flags_manager.load(STATION, DATE_STR, sample_config)
        chunk_entry = state.get("chunks", {}).get(mkv.name, {})
        assert chunk_entry.get("lock") is not None
        assert chunk_entry["lock"]["lock_type"] == "detection"
