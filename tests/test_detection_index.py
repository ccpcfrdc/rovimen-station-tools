"""Tests for the detection SQLite index pipeline.

Covers the shared parse+join (rms_parse), the station indexer staying in
lockstep with it, the VPS DB aggregation/coverage/dedup semantics, schema
migration hardening, poller freshness gating, and an end-to-end bootstrap
over a synthetic archive.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

import detection_db
import detection_indexer
import rms_parse


# ── Synthetic RMS session fixture ─────────────────────────────────────────────
#
# 5 FTP detections across 4 FF files; FF "203000" carries TWO meteors.
# 4 radiants entries (one per FF detection except the FTP-only "202000" one),
# whose begin times line up with FTP begin = FF_block_start + frame0/fps.

def _ftp_block(ff: str, meteor_no: int, frame0: float, frame1: float, mags: tuple[float, float]) -> str:
    hdr = f"{ff}\nRO000M {meteor_no:04d} 0002 0025.00 0 0 0 0 0 0"
    f0 = f"{frame0:09.4f} 100.0 100.0 10.000000 20.000000 130.000000 45.000000 100 {mags[0]:+.2f} 0 10 0"
    f1 = f"{frame1:09.4f} 110.0 110.0 11.000000 21.000000 131.000000 46.000000 100 {mags[1]:+.2f} 0 10 0"
    return f"{hdr}\n{f0}\n{f1}"


FF1 = "FF_RO000M_20260623_200000_500_0000000.fits"  # begin 20:00:01.500 (frame0=25)
FF2 = "FF_RO000M_20260623_201000_000_0000000.fits"  # begin 20:10:00.000, longest (3s)
FF3 = "FF_RO000M_20260623_202000_000_0000000.fits"  # FTP-only (no radiants)
FF4 = "FF_RO000M_20260623_203000_000_0000000.fits"  # two meteors


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    d = tmp_path / "RO000M_20260623_180000_000000"
    d.mkdir()

    ftp = "Meteor Count = 000005\n-----\nCam# Meteor# #Segments fps\n-----\n"
    ftp += _ftp_block(FF1, 1, 25, 50, (-2.00, -1.50)) + "\n"     # 1.0 s, peak -2.00
    ftp += _ftp_block(FF2, 1, 0, 75, (-1.00, -0.50)) + "\n"      # 3.0 s
    ftp += _ftp_block(FF3, 1, 0, 25, (3.00, 3.50)) + "\n"        # FTP-only, peak 3.00
    ftp += _ftp_block(FF4, 1, 0, 25, (0.50, 0.80)) + "\n"        # begin 20:30:00.000
    ftp += _ftp_block(FF4, 2, 50, 75, (1.00, 1.20)) + "\n"       # begin 20:30:02.000
    (d / "FTPdetectinfo_RO000M_20260623_180000_000000.txt").write_text(ftp)

    rad_lines = [
        "# RMS single station association",
        "# Date And Time, Beg JD, La Sun, Shower, RAb, Db, RAe, De, RAr, Dr, t0, p0, bp, ep, AppMag, AbsMag, RadElev",
        "20260623 20:00:01.500000, 2461100.000000, 100.0, PER, 10.0, 20.0, 11.0, 21.0, None, None, 0,0,0,0, -2.00, None, 45.0",
        "20260623 20:10:00.000000, 2461100.100000, 100.1, SPO, 12.0, 22.0, 13.0, 23.0, None, None, 0,0,0,0, -1.00, None, 40.0",
        "20260623 20:30:00.000000, 2461100.200000, 100.2, GEM, 14.0, 24.0, 15.0, 25.0, None, None, 0,0,0,0,  0.50, None, 50.0",
        "20260623 20:30:02.000000, 2461100.300000, 100.3, GEM, 16.0, 26.0, 17.0, 27.0, None, None, 0,0,0,0,  1.00, None, 50.0",
    ]
    (d / "RO000M_20260623_180000_000000_radiants.txt").write_text("\n".join(rad_lines))
    return d


# ── Parse + join correctness ──────────────────────────────────────────────────

def test_join_keys_and_count(session_dir):
    rows = rms_parse.parse_session_detections(session_dir)
    # 5 FTP detections -> 5 rows (radiants count mismatch must NOT drop/merge).
    assert len(rows) == 5
    keys = {(r["ff_file"], r["meteor_no"]) for r in rows}
    assert keys == {(FF1, 1), (FF2, 1), (FF3, 1), (FF4, 1), (FF4, 2)}


def test_join_attaches_radiants_by_time_not_position(session_dir):
    rows = {(r["ff_file"], r["meteor_no"]): r for r in rms_parse.parse_session_detections(session_dir)}
    # FF1 begin 20:00:01.5 matches the -2.00/PER radiant.
    assert rows[(FF1, 1)]["shower"] == "PER"
    assert rows[(FF1, 1)]["mag_apparent"] == -2.00
    # FF4 holds two meteors 2 s apart; each gets its own radiant, not swapped.
    assert rows[(FF4, 1)]["mag_apparent"] == 0.50
    assert rows[(FF4, 2)]["mag_apparent"] == 1.00
    # Kinematics come from FTP regardless of the radiants match.
    assert rows[(FF1, 1)]["duration_s"] == pytest.approx(1.0)
    assert rows[(FF1, 1)]["azim_beg"] == pytest.approx(130.0)


def test_ftp_only_detection_kept(session_dir):
    rows = {(r["ff_file"], r["meteor_no"]): r for r in rms_parse.parse_session_detections(session_dir)}
    ftp_only = rows[(FF3, 1)]
    assert ftp_only["shower"] is None          # no radiants partner
    assert ftp_only["mag_apparent"] == 3.00     # falls back to FTP peak mag
    assert ftp_only["duration_s"] == pytest.approx(1.0)


def test_station_indexer_in_lockstep_with_rms_parse(session_dir):
    """The station indexer must emit byte-identical rows to rms_parse so both
    index sources produce the same identity for the same detection."""
    a = sorted(detection_indexer._session_detections(session_dir),
               key=lambda r: (r["ff_file"], r["meteor_no"]))
    b = sorted(rms_parse.parse_session_detections(session_dir),
               key=lambda r: (r["ff_file"], r["meteor_no"]))
    assert a == b


# ── VPS DB aggregation ────────────────────────────────────────────────────────

@pytest.fixture
def populated_db(tmp_path, session_dir):
    db = tmp_path / "detections.db"
    detection_db.open_db(db).close()
    rows = rms_parse.parse_session_detections(session_dir)
    for r in rows:
        r["cam"], r["date"] = "RO000M", "20260623"
    detection_db.upsert_detections(rows, source="station", path=db)
    return db


def test_aggregate_stats_basic(populated_db):
    stats = detection_db.aggregate_stats(["20260623"], path=populated_db)
    assert stats["total"] == 5
    assert stats["per_day"] == {"20260623": 5}
    assert stats["per_cam"] == {"RO000M": 5}
    shower = dict(stats["per_shower"])
    assert shower == {"PER": 1, "SPO": 1, "GEM": 2}   # None excluded
    assert stats["brightest"]["mag_apparent"] == -2.00
    assert stats["longest"]["duration_s"] == pytest.approx(3.0)   # FF2


def test_empty_cam_filter_returns_zero_not_all(populated_db):
    """An empty cam_filter must mean 'no cameras', never silently 'all' — that
    would leak/count non-public stations on the public API."""
    stats = detection_db.aggregate_stats(["20260623"], cam_filter=set(), path=populated_db)
    assert stats["total"] == 0
    assert detection_db.aggregate_stats(["20260623"], cam_filter={"RO000M"}, path=populated_db)["total"] == 5
    assert detection_db.aggregate_stats(["20260623"], cam_filter={"NOPE"}, path=populated_db)["total"] == 0


def test_covers_dates(populated_db):
    # Seed a fresh poll record so the freshness gate does not block coverage checks.
    interval = 300
    _seed_poll_state(populated_db, time.time() - 10)

    # DB holds exactly 20260623.
    assert detection_db.covers_dates(["20260623"], path=populated_db, poll_interval=interval) is True
    # One day past the newest indexed night = "tonight" in progress -> still OK
    # (the index legitimately has no rows for it yet).
    assert detection_db.covers_dates(["20260624"], path=populated_db, poll_interval=interval) is True
    # A range whose old end is covered and whose new end is "tonight" -> fast path.
    assert detection_db.covers_dates(["20260623", "20260624"], path=populated_db, poll_interval=interval) is True
    # Old end reaches before the indexed history -> fall back.
    assert detection_db.covers_dates(["20260620", "20260623", "20260624"], path=populated_db, poll_interval=interval) is False
    # >1 day past the newest indexed night -> poller is stale, fall back.
    assert detection_db.covers_dates(["20260625"], path=populated_db, poll_interval=interval) is False
    # Older than the indexed history -> fall back (mid-bootstrap safety).
    assert detection_db.covers_dates(["20260622"], path=populated_db, poll_interval=interval) is False
    assert detection_db.covers_dates([], path=populated_db, poll_interval=interval) is False


def test_no_double_count_across_sources(populated_db, session_dir):
    """Re-ingesting the same detections from the 'storagebox' source must not
    create duplicate rows — the whole point of the shared identity scheme."""
    rows = rms_parse.parse_session_detections(session_dir)
    for r in rows:
        r["cam"], r["date"] = "RO000M", "20260623"
    detection_db.upsert_detections(rows, source="storagebox", path=populated_db)
    assert detection_db.aggregate_stats(["20260623"], path=populated_db)["total"] == 5


def test_multiple_meteors_same_ff_are_distinct_rows(populated_db):
    rows = detection_db.query_detections(["20260623"], path=populated_db)
    ff4 = [r for r in rows if r["ff_file"] == FF4]
    assert len(ff4) == 2
    assert {r["meteor_no"] for r in ff4} == {1, 2}


# ── Schema migration hardening ────────────────────────────────────────────────

def test_open_db_adds_missing_column_without_data_loss(tmp_path):
    """Opening an old-schema DB with a column missing must add the column and
    preserve existing rows — no truncation, no failure."""
    db = tmp_path / "old.db"

    # Build a v0 DB manually: create the table without the 'source' column,
    # insert a row, then call open_db() which must heal the schema.
    with sqlite3.connect(str(db)) as con:
        # Subset of columns that existed before 'source' was introduced.
        con.execute("""
            CREATE TABLE detections (
                cam TEXT NOT NULL,
                date TEXT NOT NULL,
                ff_file TEXT NOT NULL,
                meteor_no INTEGER NOT NULL DEFAULT 1,
                time_utc TEXT,
                jd REAL,
                solar_lon REAL,
                shower TEXT,
                mag_apparent REAL,
                mag_absolute REAL,
                duration_s REAL,
                ra_beg REAL, dec_beg REAL, ra_end REAL, dec_end REAL,
                ra_radiant REAL, dec_radiant REAL, radiant_elev REAL,
                angular_velocity REAL, num_segments INTEGER, fps REAL,
                azim_beg REAL, elev_beg REAL, azim_end REAL, elev_end REAL,
                PRIMARY KEY (cam, date, ff_file, meteor_no)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS poll_state (
                host_key TEXT PRIMARY KEY, last_polled REAL,
                since_date TEXT, error_count INTEGER DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS ingested_files (
                cam TEXT NOT NULL, date TEXT NOT NULL, filename TEXT NOT NULL,
                file_mtime REAL NOT NULL, row_count INTEGER NOT NULL,
                ingested_at REAL NOT NULL,
                PRIMARY KEY (cam, date, filename)
            )
        """)
        # Insert a row using only the old columns.
        con.execute(
            "INSERT INTO detections (cam, date, ff_file, meteor_no, shower) "
            "VALUES (?, ?, ?, ?, ?)",
            ("RO000M", "20260623", "FF_RO000M_20260623_200000_500_0000000.fits", 1, "PER"),
        )
        # Leave user_version at 0 to simulate a pre-versioned DB.
        con.execute("PRAGMA user_version=0")

    # Now open via the current open_db — must not raise and must heal the schema.
    healed = detection_db.open_db(db)
    healed.close()

    with sqlite3.connect(str(db)) as con:
        cols = {row[1] for row in con.execute("PRAGMA table_info(detections)").fetchall()}
        assert "source" in cols, "'source' column missing after migration"

        # Existing data must survive.
        row = con.execute(
            "SELECT shower FROM detections WHERE cam='RO000M' AND date='20260623'"
        ).fetchone()
        assert row is not None, "pre-existing row was lost during migration"
        assert row[0] == "PER"

        # user_version bumped.
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        assert ver == detection_db.SCHEMA_VERSION


def test_open_db_idempotent_on_current_schema(tmp_path):
    """Calling open_db twice on a current-schema DB must not raise."""
    db = tmp_path / "current.db"
    detection_db.open_db(db).close()
    detection_db.open_db(db).close()  # must be a no-op, not an error


# ── Poller freshness gate ─────────────────────────────────────────────────────

def _seed_poll_state(db: Path, last_polled: float) -> None:
    """Insert a poll_state row with the given last_polled timestamp."""
    with sqlite3.connect(str(db)) as con:
        con.execute(
            "INSERT OR REPLACE INTO poll_state (host_key, last_polled, since_date, error_count) "
            "VALUES (?, ?, ?, ?)",
            ("gmnro02", last_polled, "20260623", 0),
        )


def test_covers_dates_fails_when_poller_stale(populated_db):
    """covers_dates must return False when last_polled is beyond 2× the poll
    interval, even if date coverage is otherwise adequate."""
    interval = 300  # seconds
    stale_ts = time.time() - (interval * 2 + 60)   # just past the threshold
    _seed_poll_state(populated_db, stale_ts)

    result = detection_db.covers_dates(["20260623"], path=populated_db, poll_interval=interval)
    assert result is False, "covers_dates should fail when poller is stale"


def test_covers_dates_passes_when_poller_fresh(populated_db):
    """covers_dates must return True when last_polled is within 2× the poll
    interval and date coverage is adequate."""
    interval = 300
    fresh_ts = time.time() - (interval // 2)   # well within threshold
    _seed_poll_state(populated_db, fresh_ts)

    result = detection_db.covers_dates(["20260623"], path=populated_db, poll_interval=interval)
    assert result is True, "covers_dates should pass when poller is fresh and dates covered"


def test_covers_dates_fails_with_no_poll_state(populated_db):
    """covers_dates must return False when there is no poll_state record at all
    (bootstrap-only DB with no live poller running yet)."""
    # populated_db fixture has no poll_state rows by default.
    result = detection_db.covers_dates(["20260623"], path=populated_db, poll_interval=300)
    assert result is False, "covers_dates should fail with no poll_state (bootstrap-only DB)"


# ── Bootstrap end-to-end ──────────────────────────────────────────────────────

def test_bootstrap_scans_archive(tmp_path, session_dir, monkeypatch):
    import bootstrap_index
    archive = tmp_path / "archive"
    rms_dir = archive / "RO000M" / "20260623" / "rms"
    rms_dir.mkdir(parents=True)
    for f in session_dir.iterdir():
        (rms_dir / f.name).write_text(f.read_text())

    db = tmp_path / "boot.db"
    bootstrap_index.bootstrap(archive, db, days=0)

    stats = detection_db.aggregate_stats(["20260623"], path=db)
    assert stats["total"] == 5
    assert dict(stats["per_shower"]) == {"PER": 1, "SPO": 1, "GEM": 2}

    # Re-run is a no-op (fingerprint unchanged) and must not duplicate rows.
    bootstrap_index.bootstrap(archive, db, days=0)
    assert detection_db.aggregate_stats(["20260623"], path=db)["total"] == 5
