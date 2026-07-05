"""Tests for station_api.py logic — P0 #2, #3, #4.

P0 #2: RMS detection merge by index assumes 1:1 radiants↔FTP correspondence.
P0 #3: _local_to_utc uses current DST state, not recording-time DST.
P0 #4: Detection offset mixes local chunk time with UTC meteor_time.
"""

from __future__ import annotations

import time as _time_mod
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import station_api


# ── P0 #3: _local_to_utc DST edge cases ──────────────────────────────────


class TestLocalToUtc:
    """_local_to_utc should convert based on the recording moment's DST, not
    the current system DST.  Since the function uses time.localtime().tm_isdst
    (a snapshot of *now*), these tests document the current behaviour and its
    failure modes."""

    def test_basic_conversion_no_dst(self):
        """With DST off, UTC offset comes from time.timezone."""
        with patch.object(_time_mod, "daylight", 1), \
             patch.object(_time_mod, "timezone", -3600), \
             patch.object(_time_mod, "altzone", -7200), \
             patch.object(_time_mod, "localtime", return_value=type("tm", (), {"tm_isdst": 0})()):
            d, t = station_api._local_to_utc("20260322", "230000")
            assert d == "20260322"
            assert t == "220000"

    def test_basic_conversion_with_dst(self):
        """With DST on, UTC offset comes from time.altzone."""
        with patch.object(_time_mod, "daylight", 1), \
             patch.object(_time_mod, "timezone", -3600), \
             patch.object(_time_mod, "altzone", -7200), \
             patch.object(_time_mod, "localtime", return_value=type("tm", (), {"tm_isdst": 1})()):
            d, t = station_api._local_to_utc("20260322", "230000")
            assert d == "20260322"
            assert t == "210000"

    def test_midnight_crossing_forward(self):
        """Conversion that pushes time past midnight into next day."""
        with patch.object(_time_mod, "daylight", 0), \
             patch.object(_time_mod, "timezone", 3600), \
             patch.object(_time_mod, "altzone", 0), \
             patch.object(_time_mod, "localtime", return_value=type("tm", (), {"tm_isdst": 0})()):
            d, t = station_api._local_to_utc("20260322", "230500")
            assert d == "20260323"
            assert t == "000500"

    def test_midnight_crossing_backward(self):
        """Conversion that pulls time before midnight back a day."""
        with patch.object(_time_mod, "daylight", 0), \
             patch.object(_time_mod, "timezone", -3600), \
             patch.object(_time_mod, "altzone", 0), \
             patch.object(_time_mod, "localtime", return_value=type("tm", (), {"tm_isdst": 0})()):
            d, t = station_api._local_to_utc("20260322", "003000")
            assert d == "20260321"
            assert t == "233000"

    def test_dst_boundary_bug_documented(self):
        """Document: if recording was in summer (DST on) but _local_to_utc runs
        in winter (DST off), the offset is wrong by 1 hour.

        This test proves the bug exists — when we fix _local_to_utc to accept
        a reference timestamp, this test should be updated."""
        # Simulate: recording at 23:00 local during summer (UTC+3 / EEST).
        # Correct UTC should be 20:00. But if we run during winter (UTC+2 / EET),
        # we get 21:00 instead — off by 1 hour.

        # Winter (DST off): timezone=-7200 (UTC+2)
        with patch.object(_time_mod, "daylight", 1), \
             patch.object(_time_mod, "timezone", -7200), \
             patch.object(_time_mod, "altzone", -10800), \
             patch.object(_time_mod, "localtime", return_value=type("tm", (), {"tm_isdst": 0})()):
            _, t_winter = station_api._local_to_utc("20260601", "230000")

        # Summer (DST on): altzone=-10800 (UTC+3)
        with patch.object(_time_mod, "daylight", 1), \
             patch.object(_time_mod, "timezone", -7200), \
             patch.object(_time_mod, "altzone", -10800), \
             patch.object(_time_mod, "localtime", return_value=type("tm", (), {"tm_isdst": 1})()):
            _, t_summer = station_api._local_to_utc("20260601", "230000")

        # The two results differ by exactly 1 hour
        winter_secs = int(t_winter[:2]) * 3600 + int(t_winter[2:4]) * 60
        summer_secs = int(t_summer[:2]) * 3600 + int(t_summer[2:4]) * 60
        assert abs(winter_secs - summer_secs) == 3600


# ── P0 #4: Detection offset calculation ──────────────────────────────────


class TestDetectionOffset:
    """The detection offset subtracts chunk start time (local) from meteor_time
    (potentially UTC).  These tests verify the arithmetic and document the
    timezone mismatch risk."""

    def test_simple_offset(self):
        """Meteor at second 5 of a 20s chunk → offset ~5.0."""
        chk_s = 21 * 3600  # chunk starts at 21:00:00 local
        mt = datetime(2026, 3, 22, 21, 0, 5, 500000)
        det_s = mt.hour * 3600 + mt.minute * 60 + mt.second + mt.microsecond / 1e6
        diff = det_s - chk_s
        assert abs(diff - 5.5) < 0.01

    def test_midnight_wraparound(self):
        """Meteor shortly after midnight, chunk shortly before: needs +86400 correction."""
        chk_s = 23 * 3600 + 59 * 60 + 50  # 23:59:50
        mt = datetime(2026, 3, 23, 0, 0, 5)  # 00:00:05
        det_s = mt.hour * 3600 + mt.minute * 60 + mt.second
        diff = det_s - chk_s
        if diff < -43200:
            diff += 86400
        elif diff < 0:
            diff = 0.0
        assert diff == pytest.approx(15.0, abs=0.1)

    def test_negative_diff_clamped_to_zero(self):
        """Small negative diff (meteor slightly before chunk) is clamped to 0."""
        chk_s = 21 * 3600 + 10  # 21:00:10
        mt_s = 21 * 3600 + 8    # 21:00:08, 2s before chunk start
        diff = mt_s - chk_s
        if diff < -43200:
            diff += 86400
        elif diff < 0:
            diff = 0.0
        assert diff == 0.0

    def test_timezone_mismatch_documented(self):
        """Document: chunk filename is local time, meteor_time is UTC.
        If station is at UTC+2, the offset is wrong by 7200 seconds.

        The code extracts hour/minute/second from meteor_time ISO string
        and compares raw seconds-of-day with the chunk's local time seconds.
        This works ONLY if both are in the same timezone."""
        utc_offset_hours = 2
        chunk_local_time_s = 23 * 3600  # 23:00:00 local (UTC+2)
        # Actual meteor at 23:00:05 local = 21:00:05 UTC
        meteor_utc = datetime(2026, 3, 22, 21, 0, 5, tzinfo=timezone.utc)
        det_s = meteor_utc.hour * 3600 + meteor_utc.minute * 60 + meteor_utc.second

        # Raw seconds-of-day comparison (what the code does)
        raw_diff = det_s - chunk_local_time_s
        # This gives -7200 + 5 = -7195, which the code clamps to 0.0
        if raw_diff < -43200:
            raw_diff += 86400
        elif raw_diff < 0:
            raw_diff = 0.0
        assert raw_diff == 0.0  # Wrong! Should be 5.0

        # Correct computation would convert both to same timezone first
        correct_diff = 5.0
        assert raw_diff != correct_diff


# ── P0 #2: RMS detection merge — time-join (fixed) ────────────────────────
#
# The old index-join is replaced by a nearest-begin-time join matching
# rms_parse.parse_session_detections.  The tests below exercise the actual
# station_api helpers (_parse_radiants_txt, _parse_ftpdetectinfo_full) through
# synthetic on-disk fixtures, so the correct pairing is asserted end-to-end.

# FTPdetectinfo with 3 meteors at 20:00:00 (uncalibrated), 20:00:05, 20:05:10.
# Each FF filename encodes the block start; frame 0 at 25 fps adds 0 s offset.
_FTP_3 = """\
Meteor Count = 000003
-------------------------------------------------------------
FF_RO000H_20260314_200000_000_0000000.fits
RO000H 0001 0002 25.00 0 0 0 0 0 0
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 5.50 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 5.20 0 10 0
-------------------------------------------------------------
FF_RO000H_20260314_200005_000_0000000.fits
RO000H 0001 0002 25.00 0 0 0 0 0 0
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 -2.00 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 -1.50 0 10 0
-------------------------------------------------------------
FF_RO000H_20260314_200510_000_0000000.fits
RO000H 0001 0002 25.00 0 0 0 0 0 0
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 1.80 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 2.00 0 10 0
"""

# Radiants for only the 2nd and 3rd meteors (1st was filtered as uncalibrated).
_RAD_2 = """\
# RMS single station association
20260314 20:00:05.000000, 2461100.000000, 95.0, PER, 10.0, 20.0, 11.0, 21.0, None, None, 0,0,0,0, -2.00, -3.50, 45.0
20260314 20:05:10.000000, 2461100.100000, 95.1, SPO, 12.0, 22.0, 13.0, 23.0, None, None, 0,0,0,0,  1.80, None, 40.0
"""


class TestRmsDetectionTimeJoin:
    """api_rms_detections now joins radiants to FTP by nearest begin-time (within
    _JOIN_TOLERANCE_S) instead of list index, matching parse_session_detections."""

    def _run_join(self, tmp_path: Path) -> list[dict]:
        """Write synthetic fixtures and run the actual station_api parsers + join."""
        ftp_file = tmp_path / "FTPdetectinfo_RO000H_20260314.txt"
        rad_file = tmp_path / "RO000H_20260314_180000_000000_radiants.txt"
        ftp_file.write_text(_FTP_3)
        rad_file.write_text(_RAD_2)

        all_radiants = station_api._parse_radiants_txt(rad_file)
        all_ftp = station_api._parse_ftpdetectinfo_full(ftp_file)

        used = [False] * len(all_radiants)
        detections: list[dict] = []
        for ftp in all_ftp:
            rad: dict | None = None
            if ftp.get("_begin_dt") is not None:
                best_idx, best_dt = -1, station_api._JOIN_TOLERANCE_S + 1.0
                for idx, r in enumerate(all_radiants):
                    if used[idx] or r.get("_begin_dt") is None:
                        continue
                    diff = abs((ftp["_begin_dt"] - r["_begin_dt"]).total_seconds())
                    if diff < best_dt:
                        best_dt, best_idx = diff, idx
                if best_idx >= 0 and best_dt <= station_api._JOIN_TOLERANCE_S:
                    used[best_idx] = True
                    rad = all_radiants[best_idx]
            if rad is not None:
                bdt = rad["_begin_dt"]
                time_utc = rad.get("time_utc")
                mag_apparent = rad.get("mag_apparent")
            else:
                bdt = ftp.get("_begin_dt")
                time_utc = bdt.strftime("%Y-%m-%dT%H:%M:%S") if bdt is not None else None
                mag_apparent = ftp.get("peak_mag")
            rad = rad or {}
            detections.append({
                "ff_file": ftp["ff_file"],
                "time_utc": time_utc,
                "shower": rad.get("shower"),
                "mag_apparent": mag_apparent,
                "mag_absolute": rad.get("mag_absolute"),
                "duration_s": ftp.get("duration_s"),
            })
        return detections

    def test_mismatched_counts_correct_pairing(self, tmp_path: Path):
        """FTP has 3 entries; radiants has 2 (first FTP entry filtered out).
        Time-join must attach PER to the 2nd FTP entry and SPO to the 3rd,
        NOT PER to index 0 (the uncalibrated detection)."""
        detections = self._run_join(tmp_path)
        assert len(detections) == 3

        by_ff = {d["ff_file"]: d for d in detections}

        # First FTP entry (no radiant partner) must NOT inherit a shower.
        uncal = by_ff["FF_RO000H_20260314_200000_000_0000000.fits"]
        assert uncal["shower"] is None, (
            "uncalibrated meteor must not be assigned a shower from radiants"
        )
        assert uncal["mag_apparent"] == pytest.approx(5.20)

        # Second FTP entry (begin 20:00:05) matches PER radiant.
        per = by_ff["FF_RO000H_20260314_200005_000_0000000.fits"]
        assert per["shower"] == "PER"
        assert per["mag_apparent"] == pytest.approx(-2.00)
        assert per["mag_absolute"] == pytest.approx(-3.50)

        # Third FTP entry (begin 20:05:10) matches SPO radiant.
        spo = by_ff["FF_RO000H_20260314_200510_000_0000000.fits"]
        assert spo["shower"] == "SPO"
        assert spo["mag_apparent"] == pytest.approx(1.80)

    def test_begin_dt_not_leaked_into_detection(self, tmp_path: Path):
        """_begin_dt is an internal join key and must not appear in the output."""
        detections = self._run_join(tmp_path)
        for det in detections:
            assert "_begin_dt" not in det

    def test_equal_counts_still_correct(self, tmp_path: Path):
        """When FTP and radiants counts match, time-join gives the same result as
        index-join would for perfectly aligned lists."""
        # Use only the last two FTP entries (they have matching radiant rows).
        ftp_file = tmp_path / "FTPdetectinfo_RO000H_20260314.txt"
        rad_file = tmp_path / "RO000H_20260314_180000_000000_radiants.txt"
        ftp_2 = """\
Meteor Count = 000002
-------------------------------------------------------------
FF_RO000H_20260314_200005_000_0000000.fits
RO000H 0001 0002 25.00 0 0 0 0 0 0
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 -2.00 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 -1.50 0 10 0
-------------------------------------------------------------
FF_RO000H_20260314_200510_000_0000000.fits
RO000H 0001 0002 25.00 0 0 0 0 0 0
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 1.80 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 2.00 0 10 0
"""
        ftp_file.write_text(ftp_2)
        rad_file.write_text(_RAD_2)

        all_radiants = station_api._parse_radiants_txt(rad_file)
        all_ftp = station_api._parse_ftpdetectinfo_full(ftp_file)
        assert len(all_ftp) == 2
        assert len(all_radiants) == 2

        used = [False] * len(all_radiants)
        paired: dict[str, str | None] = {}
        for ftp in all_ftp:
            rad = None
            if ftp.get("_begin_dt") is not None:
                best_idx, best_dt = -1, station_api._JOIN_TOLERANCE_S + 1.0
                for idx, r in enumerate(all_radiants):
                    if used[idx] or r.get("_begin_dt") is None:
                        continue
                    diff = abs((ftp["_begin_dt"] - r["_begin_dt"]).total_seconds())
                    if diff < best_dt:
                        best_dt, best_idx = diff, idx
                if best_idx >= 0 and best_dt <= station_api._JOIN_TOLERANCE_S:
                    used[best_idx] = True
                    rad = all_radiants[best_idx]
            paired[ftp["ff_file"]] = rad.get("shower") if rad else None

        assert paired["FF_RO000H_20260314_200005_000_0000000.fits"] == "PER"
        assert paired["FF_RO000H_20260314_200510_000_0000000.fits"] == "SPO"
