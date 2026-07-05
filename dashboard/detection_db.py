"""detection_db.py — VPS-side detection index (SQLite).

Single source of truth for the fleet-wide detection metadata index.
Written by index_poller.py (station pulls) and bootstrap_index.py
(storagebox historical scan). Read by dashboard routes (social report,
public API stats) instead of parsing SSHFS files at request time.

Identity is (cam, date, ff_file, meteor_no): the real FF filename plus the
1-based meteor number within that FF. Both producers derive this the same way
(see rms_parse.parse_session_detections), so the same physical detection lands
on one row regardless of source — no cross-source double counting.

Schema versioning: SCHEMA_VERSION is stored in PRAGMA user_version. open_db()
compares the on-disk version to the current one and applies ALTER TABLE
migrations for any missing columns before the caller runs any queries. This
ensures deploy-preserved DBs (excluded from rsync) survive schema additions.
"""

from __future__ import annotations

import logging
import os
import pwd
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("ROVIMEN_DETECTIONS_DB", "/opt/rovimen/detections.db"))

# Columns written for each detection, in a single canonical order shared by
# every INSERT path so producers cannot drift out of sync.
DETECTION_COLS = (
    "cam", "date", "ff_file", "meteor_no",
    "time_utc", "jd", "solar_lon", "shower",
    "mag_apparent", "mag_absolute", "duration_s",
    "ra_beg", "dec_beg", "ra_end", "dec_end",
    "ra_radiant", "dec_radiant", "radiant_elev",
    "angular_velocity", "num_segments", "fps",
    "azim_beg", "elev_beg", "azim_end", "elev_end",
    "chunk_file",
    "source",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    cam              TEXT NOT NULL,
    date             TEXT NOT NULL,
    ff_file          TEXT NOT NULL,
    meteor_no        INTEGER NOT NULL DEFAULT 1,
    time_utc         TEXT,
    jd               REAL,
    solar_lon        REAL,
    shower           TEXT,
    mag_apparent     REAL,
    mag_absolute     REAL,
    duration_s       REAL,
    ra_beg           REAL,
    dec_beg          REAL,
    ra_end           REAL,
    dec_end          REAL,
    ra_radiant       REAL,
    dec_radiant      REAL,
    radiant_elev     REAL,
    angular_velocity REAL,
    num_segments     INTEGER,
    fps              REAL,
    azim_beg         REAL,
    elev_beg         REAL,
    azim_end         REAL,
    elev_end         REAL,
    chunk_file       TEXT,
    source           TEXT DEFAULT 'station',
    PRIMARY KEY (cam, date, ff_file, meteor_no)
);

CREATE TABLE IF NOT EXISTS poll_state (
    host_key         TEXT PRIMARY KEY,
    last_polled      REAL,
    since_date       TEXT,
    error_count      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ingested_files (
    cam         TEXT NOT NULL,
    date        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    file_mtime  REAL NOT NULL,
    row_count   INTEGER NOT NULL,
    ingested_at REAL NOT NULL,
    PRIMARY KEY (cam, date, filename)
);

CREATE INDEX IF NOT EXISTS idx_det_date   ON detections(date);
CREATE INDEX IF NOT EXISTS idx_det_cam    ON detections(cam);
CREATE INDEX IF NOT EXISTS idx_det_shower ON detections(shower);
CREATE INDEX IF NOT EXISTS idx_det_mag    ON detections(mag_apparent);
"""

# Bump this integer whenever a new column is added or a column type changes.
# open_db() reads PRAGMA user_version from an existing DB and applies
# ALTER TABLE migrations for every version gap before the caller runs any query.
SCHEMA_VERSION = 2

# Column definitions added in each schema revision, keyed by the version at
# which they were introduced. Entries here drive the ALTER TABLE migration loop.
# Format: {version_introduced: [(table, col, sql_type_and_default), ...]}
_MIGRATIONS: dict[int, list[tuple[str, str, str]]] = {
    # Version 1 is the baseline — the CREATE TABLE above covers it.
    2: [("detections", "chunk_file", "TEXT")],
}

# SQLite allows a single writer; give concurrent connections time to wait for
# the lock instead of failing immediately with "database is locked".
_BUSY_TIMEOUT_MS = 30_000


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=_BUSY_TIMEOUT_MS / 1000)
    con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    if not read_only:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    return con


def _apply_migrations(con: sqlite3.Connection) -> None:
    """Apply any ALTER TABLE migrations needed to reach SCHEMA_VERSION.

    Uses PRAGMA user_version as the on-disk version stamp. Also detects column
    drift via PRAGMA table_info so a DB with columns missing at the table level
    (rather than tracked via user_version) is healed without data loss.
    """
    on_disk: int = con.execute("PRAGMA user_version").fetchone()[0]

    # Detect and heal column-level drift against the current DETECTION_COLS,
    # regardless of user_version (guards DBs that predated version tracking).
    existing_cols = {
        row[1]
        for row in con.execute("PRAGMA table_info(detections)").fetchall()
    }
    all_schema_cols = set(DETECTION_COLS)
    missing = all_schema_cols - existing_cols
    if missing:
        logger.info(
            "detection_db: healing %d missing column(s) on detections table: %s",
            len(missing), sorted(missing),
        )
        for col in sorted(missing):
            # Derive a safe default: TEXT columns default to NULL, numeric to NULL.
            if col == "source":
                col_type, default = "TEXT", "DEFAULT 'station'"
            elif col == "chunk_file":
                col_type, default = "TEXT", ""
            else:
                col_type, default = "REAL", ""
            con.execute(
                f"ALTER TABLE detections ADD COLUMN {col} {col_type} {default}".strip()
            )

    # Apply version-keyed migrations for any versions the DB has not yet seen.
    if on_disk < SCHEMA_VERSION:
        for version in range(on_disk + 1, SCHEMA_VERSION + 1):
            for table, col, col_def in _MIGRATIONS.get(version, []):
                existing = {
                    row[1]
                    for row in con.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if col not in existing:
                    logger.info(
                        "detection_db: migration v%d: ALTER TABLE %s ADD COLUMN %s %s",
                        version, table, col, col_def,
                    )
                    con.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {col_def}"
                    )
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.commit()
        logger.info("detection_db: schema migrated to version %d", SCHEMA_VERSION)


def _chown_db_to_rovimen(path: Path) -> None:
    """If running as root, transfer DB ownership to the rovimen service user.

    Covers the DB file itself and any WAL/SHM sidecars so the long-running
    poller (which runs as rovimen) can open it for writing after bootstrap.
    """
    try:
        pw = pwd.getpwnam("rovimen")
    except KeyError:
        return  # no rovimen user on this host (dev/test env)
    for suffix in ("", "-wal", "-shm"):
        p = path.with_name(path.name + suffix)
        if p.exists():
            try:
                os.chown(p, pw.pw_uid, pw.pw_gid)
                logger.debug("detection_db: chowned %s to rovimen", p)
            except OSError as exc:
                logger.warning("detection_db: could not chown %s: %s", p, exc)


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open (or create) the detection index DB, applying schema migrations.

    If called as root the DB is created root-owned and then immediately
    chowned to the rovimen user so the long-running poller can write to it.
    If there is no rovimen user (dev/test environment) ownership is left as-is.
    """
    if os.geteuid() == 0:
        logger.warning(
            "detection_db: open_db called as root — DB will be created then "
            "chowned to rovimen. Run the poller as rovimen to avoid this."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path)
    con.executescript(_SCHEMA)
    _apply_migrations(con)
    if os.geteuid() == 0:
        _chown_db_to_rovimen(path)
    return con


def is_ready(path: Path = DB_PATH) -> bool:
    """True if the DB exists and holds at least one detection row."""
    if not path.exists():
        return False
    try:
        with closing(_connect(path, read_only=True)) as con:
            return con.execute("SELECT 1 FROM detections LIMIT 1").fetchone() is not None
    except Exception:
        return False


def covers_dates(
    dates: list[str],
    path: Path = DB_PATH,
    poll_interval: int | None = None,
) -> bool:
    """True if the index can authoritatively answer for the requested range.

    Guards the dashboard fast path. Three conditions must all hold:

    (a) The indexed history reaches back far enough to cover the oldest
        requested date (mid-bootstrap safety: a partially-built index fails).
    (b) The newest indexed date is within one day of the newest requested date.
        The slack matters because the newest requested date is almost always
        "tonight", whose meteor night is still in progress, so the index
        legitimately has no rows for it yet.
    (c) At least one station has polled successfully within 2× the configured
        poll interval. A wedged poller whose ``MAX(date)`` still looks current
        because older nights happen to cover the range would otherwise pass (a)
        and (b) while silently serving stale zeros for completed nights.

    ``poll_interval`` overrides the module default (``POLL_INTERVAL`` from
    ``index_poller``). If ``None`` the environment variable
    ``ROVIMEN_INDEX_POLL_INTERVAL`` is read, falling back to 300 s.
    """
    import time as _time
    if not dates or not path.exists():
        return False

    effective_interval = poll_interval
    if effective_interval is None:
        try:
            effective_interval = int(os.environ.get("ROVIMEN_INDEX_POLL_INTERVAL", "300"))
        except (ValueError, TypeError):
            effective_interval = 300

    try:
        with closing(_connect(path, read_only=True)) as con:
            row = con.execute("SELECT MIN(date), MAX(date) FROM detections").fetchone()
            # Freshness: take the most recent successful poll across all stations.
            poll_row = con.execute(
                "SELECT MAX(last_polled) FROM poll_state WHERE last_polled IS NOT NULL"
            ).fetchone()
    except Exception:
        return False
    if not row or row[0] is None:
        return False

    db_lo, db_hi = row[0], row[1]
    req_lo, req_hi = min(dates), max(dates)
    try:
        req_hi_floor = (datetime.strptime(req_hi, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    except ValueError:
        req_hi_floor = req_hi

    if not (db_lo <= req_lo and db_hi >= req_hi_floor):
        return False

    # Freshness gate: if the poller has been silent for more than 2× the
    # normal interval, the index may be stale even though date coverage looks
    # correct. Fall back to SSHFS rather than serve stale zeros.
    last_polled: float | None = poll_row[0] if poll_row else None
    if last_polled is None:
        # No poll record at all — bootstrap-only DB, no freshness guarantee.
        return False
    staleness_threshold = effective_interval * 2
    if (_time.time() - last_polled) > staleness_threshold:
        logger.debug(
            "covers_dates: index stale — last_polled %.0fs ago, threshold %.0fs",
            _time.time() - last_polled,
            staleness_threshold,
        )
        return False

    return True


def aggregate_stats(
    dates: list[str],
    cam_filter: set[str] | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Aggregate detection stats for a list of YYYYMMDD dates.

    Returns: total, per_day, per_cam, per_shower, brightest, longest.
    cam_filter semantics: None = all cameras; a set = only those cameras;
    an *empty* set = no cameras (returns zeroes — never "all").
    """
    empty = {"total": 0, "per_day": {}, "per_cam": {}, "per_shower": [],
             "brightest": None, "longest": None}
    if not dates:
        return empty
    if cam_filter is not None and len(cam_filter) == 0:
        return empty

    placeholders = ",".join("?" * len(dates))
    cam_clause = ""
    cam_params: list[Any] = []
    if cam_filter:
        cam_clause = f" AND cam IN ({','.join('?' * len(cam_filter))})"
        cam_params = list(cam_filter)
    params = list(dates) + cam_params

    with closing(_connect(path, read_only=True)) as con:
        con.row_factory = sqlite3.Row
        total = con.execute(
            f"SELECT COUNT(*) FROM detections WHERE date IN ({placeholders}){cam_clause}",
            params,
        ).fetchone()[0]

        per_day = {
            r["date"]: r["cnt"] for r in con.execute(
                f"""SELECT date, COUNT(*) AS cnt FROM detections
                    WHERE date IN ({placeholders}){cam_clause} GROUP BY date""",
                params,
            ).fetchall()
        }

        per_cam = {
            r["cam"]: r["cnt"] for r in con.execute(
                f"""SELECT cam, COUNT(*) AS cnt FROM detections
                    WHERE date IN ({placeholders}){cam_clause} GROUP BY cam""",
                params,
            ).fetchall()
        }

        per_shower = [
            (r["shower"], r["cnt"]) for r in con.execute(
                f"""SELECT shower, COUNT(*) AS cnt FROM detections
                    WHERE date IN ({placeholders}){cam_clause} AND shower IS NOT NULL
                    GROUP BY shower ORDER BY cnt DESC LIMIT 20""",
                params,
            ).fetchall()
        ]

        brightest_row = con.execute(
            f"""SELECT cam, date, time_utc, shower, mag_apparent, mag_absolute, ff_file
                FROM detections
                WHERE date IN ({placeholders}){cam_clause}
                  AND (mag_apparent IS NOT NULL OR mag_absolute IS NOT NULL)
                ORDER BY COALESCE(mag_apparent, mag_absolute) ASC LIMIT 1""",
            params,
        ).fetchone()
        brightest = dict(brightest_row) if brightest_row else None

        longest_row = con.execute(
            f"""SELECT cam, date, time_utc, shower, duration_s, ff_file
                FROM detections
                WHERE date IN ({placeholders}){cam_clause} AND duration_s IS NOT NULL
                ORDER BY duration_s DESC LIMIT 1""",
            params,
        ).fetchone()
        longest = dict(longest_row) if longest_row else None

    return {
        "total": total,
        "per_day": per_day,
        "per_cam": per_cam,
        "per_shower": per_shower,
        "brightest": brightest,
        "longest": longest,
    }


def query_detections(
    dates: list[str],
    cam_filter: set[str] | None = None,
    min_mag: float | None = None,
    shower: str | None = None,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return filtered detection rows for the given dates."""
    if not dates:
        return []
    if cam_filter is not None and len(cam_filter) == 0:
        return []

    placeholders = ",".join("?" * len(dates))
    clauses = [f"date IN ({placeholders})"]
    params: list[Any] = list(dates)
    if cam_filter:
        clauses.append(f"cam IN ({','.join('?' * len(cam_filter))})")
        params.extend(cam_filter)
    if min_mag is not None:
        clauses.append("COALESCE(mag_apparent, mag_absolute) <= ?")
        params.append(min_mag)
    if shower:
        clauses.append("shower = ?")
        params.append(shower.upper())

    with closing(_connect(path, read_only=True)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"SELECT * FROM detections WHERE {' AND '.join(clauses)} ORDER BY date, time_utc",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


_INSERT_SQL = (
    f"INSERT OR REPLACE INTO detections ({', '.join(DETECTION_COLS)}) "
    f"VALUES ({', '.join(':' + c for c in DETECTION_COLS)})"
)


def upsert_detections(
    rows: list[dict[str, Any]],
    source: str = "station",
    path: Path = DB_PATH,
    con: sqlite3.Connection | None = None,
) -> int:
    """Bulk-upsert detection rows. Returns count inserted/replaced.

    Pass ``con`` to write on an existing connection/transaction (lets a caller
    keep all of a session's writes on one connection instead of opening a second
    writer and contending for the single-writer lock).
    """
    if not rows:
        return 0

    def _write(c: sqlite3.Connection) -> int:
        n = 0
        for r in rows:
            payload = {col: r.get(col) for col in DETECTION_COLS}
            payload["source"] = source
            if payload.get("meteor_no") is None:
                payload["meteor_no"] = 1
            c.execute(_INSERT_SQL, payload)
            n += 1
        return n

    if con is not None:
        return _write(con)
    with closing(_connect(path)) as own:
        with own:
            return _write(own)


def get_poll_state(host_key: str, path: Path = DB_PATH) -> dict[str, Any]:
    with closing(_connect(path)) as con:
        row = con.execute(
            "SELECT since_date, last_polled, error_count FROM poll_state WHERE host_key=?",
            (host_key,),
        ).fetchone()
    if row:
        return {"since_date": row[0], "last_polled": row[1], "error_count": row[2]}
    return {"since_date": None, "last_polled": None, "error_count": 0}


def set_poll_state(host_key: str, since_date: str, error_count: int = 0, path: Path = DB_PATH) -> None:
    import time
    with closing(_connect(path)) as con:
        with con:
            con.execute(
                """INSERT OR REPLACE INTO poll_state (host_key, last_polled, since_date, error_count)
                   VALUES (?, ?, ?, ?)""",
                (host_key, time.time(), since_date, error_count),
            )
