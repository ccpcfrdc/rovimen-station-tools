"""Tests for detection_lock.py — end-of-night detection locking and reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import detection_lock


# -- Fixtures ----------------------------------------------------------------

STATION = "RO000H"
DATE_STR = "20260322"


@pytest.fixture
def chunk_dir(tmp_path):
    """Create a temporary chunk directory with sample MKV files."""
    d = tmp_path / STATION / DATE_STR
    d.mkdir(parents=True)
    return d


def _touch_chunk(chunk_dir: Path, name: str) -> Path:
    p = chunk_dir / name
    p.write_bytes(b"")
    return p


@pytest.fixture
def sample_chunks(chunk_dir):
    """Create a sequence of 20-second chunks starting at 21:00:00."""
    names = [
        f"{STATION}_20260322_210000_color.mkv",
        f"{STATION}_20260322_210020_color.mkv",
        f"{STATION}_20260322_210040_color.mkv",
        f"{STATION}_20260322_210100_color.mkv",
    ]
    return [_touch_chunk(chunk_dir, n) for n in names]


@pytest.fixture
def ftpdetect_file(tmp_path):
    """Create a sample FTPdetectinfo file and return its path.

    Real FTPdetectinfo format: FF filename on its own line, then a camera
    header line (resolution + fps), then detection rows.
    """
    content = (
        "------------------------------\n"
        "Meteor Count = 2\n"
        "------------------------------\n"
        "FF_RO000H_20260322_213045_000\n"
        "720  576 25.00  fps\n"
        "   1.50  320.5  240.2  1000  5.5  32.1  123.4  45.6\n"
        "   2.00  321.0  241.0  1100  5.6  32.2  123.5  45.7\n"
        "------------------------------\n"
        "FF_RO000H_20260322_221530_000\n"
        "720  576 25.00  fps\n"
        "   3.00  400.0  300.0  800  4.2  28.5  130.0  50.0\n"
    )
    p = tmp_path / "FTPdetectinfo_RO000H.txt"
    p.write_text(content)
    return p


# -- _parse_dt ---------------------------------------------------------------

class TestParseDt:
    def test_normal_datetime(self):
        result = detection_lock._parse_dt("20260322", "213045")
        assert result == datetime(2026, 3, 22, 21, 30, 45)

    def test_format_yyyymmdd_hhmmss(self):
        result = detection_lock._parse_dt("20260101", "000000")
        assert result == datetime(2026, 1, 1, 0, 0, 0)


# -- _night_date --------------------------------------------------------------

class TestNightDate:
    def test_evening_returns_same_date(self):
        assert detection_lock._night_date("20260322", "210000") == "20260322"

    def test_morning_returns_previous_day(self):
        assert detection_lock._night_date("20260323", "030000") == "20260322"

    def test_noon_returns_same_date(self):
        assert detection_lock._night_date("20260322", "120000") == "20260322"

    def test_midnight_returns_previous_day(self):
        assert detection_lock._night_date("20260323", "000000") == "20260322"

    def test_1159_returns_previous_day(self):
        assert detection_lock._night_date("20260323", "115959") == "20260322"

    def test_month_boundary_rollback(self):
        # March 1 at 03:00 belongs to Feb 28 night
        assert detection_lock._night_date("20260301", "030000") == "20260228"


# -- _chunk_time ---------------------------------------------------------------

class TestChunkTime:
    def test_valid_chunk_filename(self):
        mkv = Path(f"{STATION}_20260322_210000_color.mkv")
        result = detection_lock._chunk_time(mkv)
        assert result == datetime(2026, 3, 22, 21, 0, 0)

    def test_invalid_filename_returns_none(self):
        mkv = Path("not_a_chunk.mkv")
        assert detection_lock._chunk_time(mkv) is None

    def test_stitched_filename_returns_none(self):
        # Stitched filenames have two time components: {station}_{date}_{Tstart}_{Tend}_color.mkv
        mkv = Path(f"{STATION}_20260322_210010_210030_color.mkv")
        assert detection_lock._chunk_time(mkv) is None

    def test_non_mkv_returns_none(self):
        mkv = Path(f"{STATION}_20260322_210000_color.txt")
        assert detection_lock._chunk_time(mkv) is None

    def test_lowercase_station_returns_none(self):
        # Regex requires uppercase
        mkv = Path("ro000h_20260322_210000_color.mkv")
        assert detection_lock._chunk_time(mkv) is None


# -- _find_chunks_for_time -----------------------------------------------------

class TestFindChunksForTime:
    def test_returns_chunk_containing_ff_time(self, chunk_dir, sample_chunks):
        # 21:00:05 falls in the first chunk (21:00:00 - 21:00:20)
        ff_time = datetime(2026, 3, 22, 21, 0, 5)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1
        assert result[0].name == f"{STATION}_20260322_210000_color.mkv"

    def test_returns_two_chunks_when_ff_spans_boundary(self, chunk_dir, sample_chunks):
        # FF_DURATION is ~10.24s. If ff_time is at 21:00:15, the FF block
        # ends at ~21:00:25.24 which spills into the next chunk (21:00:20).
        ff_time = datetime(2026, 3, 22, 21, 0, 15)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 2
        assert result[0].name == f"{STATION}_20260322_210000_color.mkv"
        assert result[1].name == f"{STATION}_20260322_210020_color.mkv"

    def test_returns_empty_for_nonexistent_directory(self, tmp_path):
        ff_time = datetime(2026, 3, 22, 21, 0, 5)
        result = detection_lock._find_chunks_for_time(
            ff_time, tmp_path / "does_not_exist", segment_secs=20
        )
        assert result == []

    def test_returns_empty_when_no_chunks_match(self, chunk_dir, sample_chunks):
        # Time before any chunk exists
        ff_time = datetime(2026, 3, 22, 20, 0, 0)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert result == []

    def test_returns_empty_for_empty_directory(self, chunk_dir):
        ff_time = datetime(2026, 3, 22, 21, 0, 5)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert result == []

    def test_last_chunk_still_matches(self, chunk_dir, sample_chunks):
        # 21:01:05 falls in the last chunk (21:01:00 onward)
        ff_time = datetime(2026, 3, 22, 21, 1, 5)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1
        assert result[0].name == f"{STATION}_20260322_210100_color.mkv"

    def test_exact_chunk_start_time_matches(self, chunk_dir, sample_chunks):
        # Exactly at chunk boundary
        ff_time = datetime(2026, 3, 22, 21, 0, 20)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) >= 1
        assert result[0].name == f"{STATION}_20260322_210020_color.mkv"


# -- _parse_ftpdetectinfo -----------------------------------------------------

class TestParseFtpdetectinfo:
    def test_parse_realistic_file(self, ftpdetect_file):
        times = detection_lock._parse_ftpdetectinfo(ftpdetect_file)
        assert len(times) == 2

    def test_first_detection_time(self, ftpdetect_file):
        times = detection_lock._parse_ftpdetectinfo(ftpdetect_file)
        # FF_RO000H_20260322_213045_000: base=21:30:45, ms=0,
        # fps=25, first frame=1.50 => offset=1.50/25=0.06s
        expected = datetime(2026, 3, 22, 21, 30, 45) + timedelta(seconds=1.50 / 25.0)
        assert abs((times[0] - expected).total_seconds()) < 0.01

    def test_second_detection_time(self, ftpdetect_file):
        times = detection_lock._parse_ftpdetectinfo(ftpdetect_file)
        # FF_RO000H_20260322_221530_000: base=22:15:30, ms=0,
        # fps=25, first frame=3.00 => offset=3.00/25=0.12s
        expected = datetime(2026, 3, 22, 22, 15, 30) + timedelta(seconds=3.00 / 25.0)
        assert abs((times[1] - expected).total_seconds()) < 0.01

    def test_multiple_detections(self, tmp_path):
        content = (
            "------------------------------\n"
            "Meteor Count = 3\n"
            "------------------------------\n"
            "FF_RO000H_20260322_200000_000\n"
            "720  576 25.00  fps\n"
            "   1.00  100.0  100.0  500  3.0  20.0  100.0  40.0\n"
            "------------------------------\n"
            "FF_RO000H_20260322_210000_000\n"
            "720  576 25.00  fps\n"
            "   2.00  200.0  200.0  600  4.0  25.0  110.0  42.0\n"
            "------------------------------\n"
            "FF_RO000H_20260322_220000_000\n"
            "720  576 25.00  fps\n"
            "   5.00  300.0  300.0  700  5.0  30.0  120.0  44.0\n"
        )
        p = tmp_path / "FTPdetectinfo_multi.txt"
        p.write_text(content)
        times = detection_lock._parse_ftpdetectinfo(p)
        assert len(times) == 3

    def test_empty_file_returns_empty(self, tmp_path):
        p = tmp_path / "FTPdetectinfo_empty.txt"
        p.write_text("")
        assert detection_lock._parse_ftpdetectinfo(p) == []

    def test_nonexistent_file_returns_empty(self, tmp_path):
        p = tmp_path / "no_such_file.txt"
        assert detection_lock._parse_ftpdetectinfo(p) == []

    def test_malformed_lines_skipped(self, tmp_path):
        content = (
            "------------------------------\n"
            "Meteor Count = 1\n"
            "------------------------------\n"
            "FF_RO000H_BADDATE_213045_000\n"
            "720  576 25.00  fps\n"
            "   1.50  320.5  240.2  1000  5.5  32.1  123.4  45.6\n"
            "------------------------------\n"
            "FF_RO000H_20260322_221530_000\n"
            "720  576 25.00  fps\n"
            "   3.00  400.0  300.0  800  4.2  28.5  130.0  50.0\n"
        )
        p = tmp_path / "FTPdetectinfo_malformed.txt"
        p.write_text(content)
        times = detection_lock._parse_ftpdetectinfo(p)
        # The malformed FF line is skipped, valid one parses
        assert len(times) == 1

    def test_file_with_only_separators(self, tmp_path):
        content = (
            "------------------------------\n"
            "Meteor Count = 0\n"
            "------------------------------\n"
        )
        p = tmp_path / "FTPdetectinfo_nosep.txt"
        p.write_text(content)
        assert detection_lock._parse_ftpdetectinfo(p) == []

    def test_non_default_fps(self, tmp_path):
        # Camera header: seg[0]=station, seg[1]=width, seg[2]=height, seg[3]=fps
        # The parser reads fps from seg[3] when seg[0] is non-numeric.
        content = (
            "------------------------------\n"
            "FF_RO000H_20260322_213045_000\n"
            "RO000H 720 576 30.00\n"
            "   3.00  320.5  240.2  1000  5.5  32.1  123.4  45.6\n"
        )
        p = tmp_path / "FTPdetectinfo_fps.txt"
        p.write_text(content)
        times = detection_lock._parse_ftpdetectinfo(p)
        assert len(times) == 1
        expected = datetime(2026, 3, 22, 21, 30, 45) + timedelta(seconds=3.0 / 30.0)
        assert abs((times[0] - expected).total_seconds()) < 0.01

    def test_ff_with_nonzero_milliseconds(self, tmp_path):
        content = (
            "------------------------------\n"
            "FF_RO000H_20260322_213045_500\n"
            "720  576 25.00  fps\n"
            "   1.00  320.5  240.2  1000  5.5  32.1  123.4  45.6\n"
        )
        p = tmp_path / "FTPdetectinfo_ms.txt"
        p.write_text(content)
        times = detection_lock._parse_ftpdetectinfo(p)
        assert len(times) == 1
        # base = 21:30:45 + 0.5s ms, then frame 1.0/25 = 0.04s offset
        expected = datetime(2026, 3, 22, 21, 30, 45) + timedelta(seconds=0.5 + 1.0 / 25.0)
        assert abs((times[0] - expected).total_seconds()) < 0.01
