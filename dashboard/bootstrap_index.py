#!/usr/bin/env python3
"""bootstrap_index.py — One-time historical detection index builder.

Scans the storagebox archive for all historical sessions and populates
detections.db using the SAME parse+join as the live station indexer
(rms_parse.parse_session_detections), so historical rows carry the exact same
identity (cam/date/ff_file/meteor_no) as rows the poller later pulls from the
stations — no double counting between the two sources.

Uses a per-session fingerprint (ingested_files table) so it is safe to re-run:
only changed sessions are re-parsed.

Usage (on VPS):
    cd /opt/rovimen
    python3 bootstrap_index.py [--archive /srv/rovimen/archive] [--db /opt/rovimen/detections.db] [--days 0]

--days 0  scans everything (default). --days 30 rescans only the last 30 days.

This script is separate from index_poller.py so historical data can be loaded
independently without requiring all stations to have detection_indexer running.
Once the DB is populated, index_poller.py keeps it current via station APIs.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import detection_db
from rms_parse import parse_session_detections

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_SKIP_DIRS = {"compilations", "skyfit", ".thumb_cache", ".thumb_cache_dev"}
_CAM_RE = re.compile(r"^[A-Z0-9]{4,8}$")
_DATE_RE = re.compile(r"^\d{8}$")
_SESSION_KEY = "__session__"


def _session_fingerprint(rms_dir: Path) -> float | None:
    """Largest mtime across radiants + filtered FTPdetectinfo files (or None)."""
    latest: float | None = None
    try:
        for f in rms_dir.iterdir():
            n = f.name
            if not f.is_file():
                continue
            if n.endswith("_radiants.txt") or (
                n.startswith("FTPdetectinfo")
                and "_unfiltered" not in n and "_backup" not in n
            ):
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if latest is None or m > latest:
                    latest = m
    except OSError:
        return None
    return latest


def _scan_session(con, cam: str, date: str, rms_dir: Path, db_path: Path, force: bool = False) -> int:
    """Scan one rms/ directory. Returns number of rows inserted/replaced."""
    fingerprint = _session_fingerprint(rms_dir)
    if fingerprint is None:
        return 0

    if not force:
        row = con.execute(
            "SELECT file_mtime FROM ingested_files WHERE cam=? AND date=? AND filename=?",
            (cam, date, _SESSION_KEY),
        ).fetchone()
        if row and row[0] == fingerprint:
            return 0  # unchanged, skip

    detections = parse_session_detections(rms_dir)
    for d in detections:
        d["cam"], d["date"] = cam, date

    with con:
        if detections:
            detection_db.upsert_detections(detections, source="storagebox", path=db_path, con=con)
        con.execute(
            """INSERT OR REPLACE INTO ingested_files (cam, date, filename, file_mtime, row_count, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cam, date, _SESSION_KEY, fingerprint, len(detections), time.time()),
        )
    return len(detections)


def _scan_camera(archive: Path, cam: str, since_date: str | None, db_path: Path) -> tuple[str, int, int]:
    """Scan all nights for one camera on its own DB connection (thread-safe)."""
    cam_dir = archive / cam
    nights_scanned = 0
    total_rows = 0
    try:
        with os.scandir(cam_dir) as it:
            date_entries = [
                e for e in it
                if e.is_dir(follow_symlinks=False) and _DATE_RE.match(e.name)
                and (since_date is None or e.name >= since_date)
            ]
    except OSError as exc:
        logger.warning("%s: cannot scan cam dir: %s", cam, exc)
        return cam, 0, 0

    with closing(detection_db.open_db(db_path)) as con:
        for entry in sorted(date_entries, key=lambda e: e.name):
            rms_dir = Path(entry.path) / "rms"
            try:
                if not rms_dir.is_dir():
                    continue
            except OSError:
                continue
            try:
                n = _scan_session(con, cam, entry.name, rms_dir, db_path)
                if n:
                    logger.debug("  %s/%s: %d rows", cam, entry.name, n)
                    total_rows += n
                nights_scanned += 1
            except Exception as exc:
                logger.warning("%s/%s: error: %s", cam, entry.name, exc)

    return cam, nights_scanned, total_rows


def bootstrap(archive: Path, db_path: Path, days: int) -> None:
    since_date: str | None = None
    if days > 0:
        since_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
        logger.info("scanning last %d days (since %s)", days, since_date)
    else:
        logger.info("scanning full archive (all history)")

    logger.info("archive: %s", archive)
    logger.info("DB:      %s", db_path)

    # Create the schema once up front; workers open their own connections.
    detection_db.open_db(db_path).close()
    t0 = time.perf_counter()

    try:
        with os.scandir(archive) as it:
            cams = [
                e.name for e in it
                if e.is_dir(follow_symlinks=False)
                and _CAM_RE.match(e.name)
                and e.name not in _SKIP_DIRS
            ]
    except OSError as exc:
        logger.error("cannot scan archive: %s", exc)
        sys.exit(1)

    logger.info("found %d camera directories", len(cams))

    total_nights = 0
    total_rows = 0

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_scan_camera, archive, cam, since_date, db_path): cam
            for cam in sorted(cams)
        }
        for future in as_completed(futures):
            cam = futures[future]
            try:
                cam, nights, rows = future.result()
            except Exception as exc:
                logger.warning("%s: worker failed: %s", cam, exc)
                continue
            if nights or rows:
                logger.info("%s: %d nights, %d rows", cam, nights, rows)
            total_nights += nights
            total_rows += rows

    elapsed = time.perf_counter() - t0
    logger.info(
        "bootstrap complete: %d cameras, %d nights, %d rows in %.1fs",
        len(cams), total_nights, total_rows, elapsed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the VPS detection index from storagebox")
    parser.add_argument(
        "--archive",
        type=Path,
        default=Path(os.environ.get("ROVIMEN_ARCHIVE_PATH", "/srv/rovimen/archive")),
    )
    parser.add_argument("--db", type=Path, default=detection_db.DB_PATH)
    parser.add_argument(
        "--days", type=int, default=0,
        help="Scan only the last N days (0 = all history)",
    )
    args = parser.parse_args()
    bootstrap(args.archive, args.db, args.days)


if __name__ == "__main__":
    main()
