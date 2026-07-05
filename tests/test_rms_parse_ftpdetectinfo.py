"""Robustness tests for rms_parse.parse_ftpdetectinfo.

Regression guard: a malformed/variant FTPdetectinfo block (a free-text note
line where the numeric "STATION meteor_no segs fps" header is expected) must
NOT crash the parse on float('on:'). Before the fix this raised ValueError and
took down the whole storage-box janitor mid-scan. Shared parser, so this also
protects the dashboard's duration display.

Also covers parse_session_detections time-based join: FTP and radiants lists
with different counts must be paired by begin-time, not list index.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rms_parse

# A valid two-detection file. Frame data lines need >=9 columns; col[0]=frame,
# col[8]=mag. fps is header[3].
_VALID = """Meteor Count = 000002
-----------------------------------------------------
FF_RO000H_20260314_023422_123_0123456.fits
RO000H 0001 0002 25.00
0010.0 100.0 100.0 10.0 5.0 0.0 0.0 0.0 4.50
0035.0 110.0 110.0 10.0 5.0 0.0 0.0 0.0 4.20
-----------------------------------------------------
FF_RO000H_20260314_030000_000_0123457.fits
RO000H 0002 0002 25.00
0005.0 100.0 100.0 10.0 5.0 0.0 0.0 0.0 3.10
0030.0 110.0 110.0 10.0 5.0 0.0 0.0 0.0 3.00
"""

# Second block carries a free-text note line instead of the numeric header.
_WITH_NOTE = """Meteor Count = 000002
-----------------------------------------------------
FF_RO000H_20260314_023422_123_0123456.fits
RO000H 0001 0002 25.00
0010.0 100.0 100.0 10.0 5.0 0.0 0.0 0.0 4.50
0035.0 110.0 110.0 10.0 5.0 0.0 0.0 0.0 4.20
-----------------------------------------------------
FF_RO000H_20260314_030000_000_0123457.fits
Detection estimated based on: platepar_recalibrated
0005.0 100.0 100.0 10.0 5.0 0.0 0.0 0.0 3.10
"""


def test_parses_valid_blocks(tmp_path: Path):
    f = tmp_path / "FTPdetectinfo_RO000H.txt"
    f.write_text(_VALID)
    out = rms_parse.parse_ftpdetectinfo(f)
    assert len(out) == 2
    assert out[0]["ff_file"].startswith("FF_RO000H_20260314_023422")
    # (35-10)/25 = 1.0 s
    assert out[0]["duration_s"] == 1.0
    assert out[0]["fps"] == 25.0


def test_note_line_block_is_skipped_not_crashed(tmp_path: Path):
    f = tmp_path / "FTPdetectinfo_RO000H.txt"
    f.write_text(_WITH_NOTE)
    # Must not raise; the well-formed block is returned, the note block skipped.
    out = rms_parse.parse_ftpdetectinfo(f)
    assert len(out) == 1
    assert out[0]["ff_file"].startswith("FF_RO000H_20260314_023422")


# ── parse_session_detections: time-join with mismatched counts ────────────────
#
# Scenario: 3 meteors in FTPdetectinfo but only 2 in radiants.
# The first FTP meteor (FF_*_200000_*) was not calibrated and was filtered out
# of the radiants file.  The two radiant rows (PER at 20:00:05, SPO at 20:05:10)
# correspond to the second and third FTP entries.
#
# Under the old index-join, radiant[0]/PER was incorrectly paired with ftp[0]
# (the uncalibrated detection), giving the wrong shower to that FF file.
# The time-join must produce:
#   ftp[0] -> no radiant match  (shower=None, mag=FTP peak_mag)
#   ftp[1] -> radiant PER       (shower="PER")
#   ftp[2] -> radiant SPO       (shower="SPO")

_FTP_3_ENTRIES = """\
Meteor Count = 000003
-----------------------------------------------------
FF_RO000H_20260314_200000_000_0000000.fits
RO000H 0001 0002 25.00
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 5.50 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 5.20 0 10 0
-----------------------------------------------------
FF_RO000H_20260314_200005_000_0000000.fits
RO000H 0001 0002 25.00
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 -2.00 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 -1.50 0 10 0
-----------------------------------------------------
FF_RO000H_20260314_200510_000_0000000.fits
RO000H 0001 0002 25.00
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 1.80 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 2.00 0 10 0
"""

# Radiants file has only 2 entries corresponding to the 2nd and 3rd FTP meteors.
# begin times: PER at 20:00:05.000, SPO at 20:05:10.000
_RAD_2_ENTRIES = """\
# RMS single station association
20260314 20:00:05.000000, 2461100.000000, 95.0, PER, 10.0, 20.0, 11.0, 21.0, None, None, 0,0,0,0, -2.00, -3.50, 45.0
20260314 20:05:10.000000, 2461100.100000, 95.1, SPO, 12.0, 22.0, 13.0, 23.0, None, None, 0,0,0,0,  1.80, None, 40.0
"""


@pytest.fixture
def session_dir_mismatch(tmp_path: Path) -> Path:
    d = tmp_path / "RO000H_20260314_180000_000000"
    d.mkdir()
    (d / "FTPdetectinfo_RO000H_20260314.txt").write_text(_FTP_3_ENTRIES)
    (d / "RO000H_20260314_180000_000000_radiants.txt").write_text(_RAD_2_ENTRIES)
    return d


def test_time_join_mismatched_counts_correct_pairing(session_dir_mismatch: Path):
    """When FTP has more entries than radiants (a meteor was filtered from
    radiants but not from FTPdetectinfo), parse_session_detections must pair
    by begin-time, not by list position.

    Regression: the old index-join attached radiant[0]/PER to ftp[0], giving
    the uncalibrated meteor a PER shower and leaving ftp[1] unmatched.
    """
    rows = rms_parse.parse_session_detections(session_dir_mismatch)
    # All 3 FTP entries produce rows (no entries silently dropped).
    assert len(rows) == 3

    by_ff = {r["ff_file"]: r for r in rows}

    # First FTP entry has no radiant partner — shower must be None.
    uncal = by_ff["FF_RO000H_20260314_200000_000_0000000.fits"]
    assert uncal["shower"] is None, "uncalibrated meteor must not inherit a shower"
    assert uncal["mag_apparent"] == pytest.approx(5.20), "should use FTP peak_mag"

    # Second FTP entry (begin 20:00:05) must match the PER radiant.
    per = by_ff["FF_RO000H_20260314_200005_000_0000000.fits"]
    assert per["shower"] == "PER"
    assert per["mag_apparent"] == pytest.approx(-2.00)
    assert per["mag_absolute"] == pytest.approx(-3.50)

    # Third FTP entry (begin 20:05:10) must match the SPO radiant.
    spo = by_ff["FF_RO000H_20260314_200510_000_0000000.fits"]
    assert spo["shower"] == "SPO"
    assert spo["mag_apparent"] == pytest.approx(1.80)
