#!/usr/bin/env python3
"""detection_indexer.py — Station-side detection metadata indexer.

Polls RMS session directories for new/changed FTPdetectinfo and radiants
files, parses them, and upserts results into a local SQLite index at
~/rovimen_scripts/detections_index.db.

The station API reads this DB to serve /api/detections-index, which the
VPS polls every few minutes instead of scanning the storagebox directly.

The parse+join here mirrors dashboard/rms_parse.parse_session_detections so a
detection gets the SAME identity (real FF filename + meteor number) whether it
is indexed here or by the VPS bootstrap — that is what stops the two index
sources from double-counting. The two implementations are kept in lockstep by
tests/test_detection_index.py. (Station scripts ship separately from the
dashboard, so they cannot import dashboard modules.)

Run as: detection-indexer.service (systemd)
"""

from __future__ import annotations

import json
import logging
import math
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

BASE = Path.home()
CONFIG_PATH = next(
    (p for p in [
        BASE / "rovimen_scripts" / "config.json",
        BASE / "meteor_detector" / "config.json",
    ] if p.exists()),
    BASE / "rovimen_scripts" / "config.json",
)
DB_PATH = BASE / "rovimen_scripts" / "detections_index.db"

# Scan interval for the hot window (recent sessions that may still change)
HOT_POLL_INTERVAL = 60       # seconds
HOT_DAYS = 14                # days considered "hot"
_stop = False

# Canonical column order shared with the station API endpoint and (minus the
# VPS-only ``source`` column) with dashboard/detection_db.DETECTION_COLS.
COLS = (
    "cam", "date", "ff_file", "meteor_no",
    "time_utc", "jd", "solar_lon", "shower",
    "mag_apparent", "mag_absolute", "duration_s",
    "ra_beg", "dec_beg", "ra_end", "dec_end",
    "ra_radiant", "dec_radiant", "radiant_elev",
    "angular_velocity", "num_segments", "fps",
    "azim_beg", "elev_beg", "azim_end", "elev_end",
    "chunk_file",
)


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


# ── RMS session discovery (mirrors station_api logic, no Flask dependency) ────

_ARC_RE = re.compile(r"^([A-Z0-9]+)_(\d{8})_(\d{6})_")
_FF_RE = re.compile(r"^FF_([A-Z0-9]+)_(\d{8})_(\d{6})_(\d{3})_")
_JOIN_TOLERANCE_S = 2.0


def _night_date(date_str: str, time_str: str) -> str:
    if int(time_str[:2]) < 12:
        d = (datetime(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:]),
                      tzinfo=timezone.utc) - timedelta(days=1))
        return d.strftime("%Y%m%d")
    return date_str


def _rms_camera_dirs(cfg: dict) -> list[tuple[str, Path]]:
    """Return (cam_code, rms_data_path) pairs from config."""
    results: list[tuple[str, Path]] = []
    for cam_code, info in cfg.get("stations", {}).items():
        rms_path = info.get("rms_data_path")
        if rms_path:
            p = Path(rms_path)
            if p.exists():
                results.append((cam_code, p))
    if not results:
        rms_base = BASE / "RMS_data"
        if rms_base.exists():
            for d in sorted(rms_base.iterdir()):
                if d.is_dir() and (
                    (d / "ArchivedFiles").exists() or (d / "CapturedFiles").exists()
                ):
                    results.append((d.name, d))
    return results


def _all_sessions(rms_dir: Path, since_date: str | None = None) -> list[tuple[str, str, Path]]:
    """Return (cam_code, night_date, session_path) for sessions in rms_dir."""
    sessions: list[tuple[str, str, Path]] = []
    for sub in ("ArchivedFiles", "CapturedFiles"):
        sub_dir = rms_dir / sub
        if not sub_dir.exists():
            continue
        try:
            for d in sub_dir.iterdir():
                if not d.is_dir():
                    continue
                m = _ARC_RE.match(d.name)
                if not m:
                    continue
                cam = m.group(1)
                try:
                    night = _night_date(m.group(2), m.group(3))
                except (ValueError, IndexError):
                    continue
                if since_date and night < since_date:
                    continue
                sessions.append((cam, night, d))
        except OSError:
            pass
    return sessions


# ── Parsers + join (mirror of dashboard/rms_parse) ────────────────────────────

def _ff_block_start(ff_file: str) -> datetime | None:
    m = _FF_RE.match(ff_file)
    if not m:
        return None
    date_s, time_s, ms_s = m.group(2), m.group(3), m.group(4)
    try:
        return datetime(
            int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8]),
            int(time_s[:2]), int(time_s[2:4]), int(time_s[4:6]),
            int(ms_s) * 1000,
        )
    except ValueError:
        return None


def _opt(s: str) -> float | None:
    s = s.strip()
    return None if s in ("None", "") else float(s)


def _radiant_rows(path: Path) -> list[dict]:
    results: list[dict] = []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return results
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 17:
            continue
        try:
            begin_dt = datetime.strptime(parts[0].strip(), "%Y%m%d %H:%M:%S.%f")
        except ValueError:
            try:
                begin_dt = datetime.strptime(parts[0].strip().split(".")[0], "%Y%m%d %H:%M:%S")
            except ValueError:
                continue
        try:
            shower = parts[3].strip() or "SPO"
            if shower == "...":
                shower = "SPO"
            results.append({
                "_begin_dt": begin_dt,
                "time_utc": begin_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "jd": float(parts[1]),
                "solar_lon": _opt(parts[2]),
                "shower": shower,
                "ra_beg": float(parts[4]),
                "dec_beg": float(parts[5]),
                "ra_end": float(parts[6]),
                "dec_end": float(parts[7]),
                "ra_radiant": _opt(parts[8]),
                "dec_radiant": _opt(parts[9]),
                "mag_apparent": _opt(parts[14]),
                "mag_absolute": _opt(parts[15]),
                "radiant_elev": _opt(parts[16]),
            })
        except (ValueError, IndexError):
            continue
    return results


def _ftp_rows(path: Path) -> list[dict]:
    results: list[dict] = []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return results
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("FF_") and line.endswith(".fits"):
            ff_file = line
            i += 1
            if i < len(lines) and "Recalibrated" in lines[i]:
                i += 1
            if i >= len(lines):
                break
            header = lines[i].strip().split()
            if len(header) < 4:
                i += 1
                continue
            try:
                meteor_no = int(header[1])
                num_segments = int(header[2])
                fps = float(header[3])
            except (ValueError, IndexError):
                i += 1
                continue
            i += 1
            frames: list[dict] = []
            while i < len(lines):
                dl = lines[i].strip()
                if not dl or dl.startswith("-") or dl.startswith("FF_"):
                    break
                fp = dl.split()
                if len(fp) >= 9:
                    try:
                        frames.append({
                            "frame": float(fp[0]),
                            "ra": float(fp[3]), "dec": float(fp[4]),
                            "azim": float(fp[5]), "elev": float(fp[6]),
                            "mag": float(fp[8]),
                        })
                    except ValueError:
                        break
                else:
                    break
                i += 1
            if frames:
                duration_s = (frames[-1]["frame"] - frames[0]["frame"]) / fps if fps > 0 else 0.0
                ang_vel = None
                if duration_s > 0 and len(frames) >= 2:
                    ra0, dec0 = math.radians(frames[0]["ra"]), math.radians(frames[0]["dec"])
                    ra1, dec1 = math.radians(frames[-1]["ra"]), math.radians(frames[-1]["dec"])
                    dlat, dlon = dec1 - dec0, ra1 - ra0
                    a = (math.sin(dlat / 2) ** 2
                         + math.cos(dec0) * math.cos(dec1) * math.sin(dlon / 2) ** 2)
                    ang_dist = math.degrees(2 * math.asin(min(1.0, math.sqrt(a))))
                    ang_vel = round(ang_dist / duration_s, 2)
                begin_dt = _ff_block_start(ff_file)
                if begin_dt is not None and fps > 0:
                    begin_dt = begin_dt + timedelta(seconds=frames[0]["frame"] / fps)
                results.append({
                    "_begin_dt": begin_dt,
                    "ff_file": ff_file,
                    "meteor_no": meteor_no,
                    "duration_s": round(duration_s, 3),
                    "angular_velocity": ang_vel,
                    "num_segments": num_segments,
                    "fps": fps,
                    "peak_mag": min(f["mag"] for f in frames),
                    "azim_beg": frames[0]["azim"],
                    "elev_beg": frames[0]["elev"],
                    "azim_end": frames[-1]["azim"],
                    "elev_end": frames[-1]["elev"],
                })
            continue
        i += 1
    return results


def _session_detections(session: Path) -> list[dict]:
    ftp_by_key: dict[tuple[str, int], dict] = {}
    for f in sorted(session.glob("FTPdetectinfo_*.txt")):
        if "_unfiltered" in f.name or "_backup" in f.name:
            continue
        for row in _ftp_rows(f):
            ftp_by_key[(row["ff_file"], row["meteor_no"])] = row
    ftps = list(ftp_by_key.values())

    radiants: list[dict] = []
    for f in sorted(session.glob("*_radiants.txt")):
        radiants.extend(_radiant_rows(f))

    used = [False] * len(radiants)
    rows: list[dict] = []
    for ftp in ftps:
        match: dict | None = None
        if ftp["_begin_dt"] is not None:
            best_idx, best_dt = -1, _JOIN_TOLERANCE_S + 1.0
            for idx, rad in enumerate(radiants):
                if used[idx]:
                    continue
                diff = abs((ftp["_begin_dt"] - rad["_begin_dt"]).total_seconds())
                if diff < best_dt:
                    best_dt, best_idx = diff, idx
            if best_idx >= 0 and best_dt <= _JOIN_TOLERANCE_S:
                used[best_idx] = True
                match = radiants[best_idx]
        rows.append(_merge_row(ftp, match))

    for idx, rad in enumerate(radiants):
        if not used[idx]:
            rows.append(_radiant_only_row(rad))
    return rows


def _merge_row(ftp: dict, rad: dict | None) -> dict:
    if rad is not None:
        time_utc = rad.get("time_utc")
        mag_apparent = rad.get("mag_apparent")
    else:
        bdt = ftp.get("_begin_dt")
        time_utc = bdt.strftime("%Y-%m-%dT%H:%M:%S") if bdt is not None else None
        mag_apparent = ftp.get("peak_mag")
    rad = rad or {}
    return {
        "ff_file": ftp["ff_file"], "meteor_no": ftp["meteor_no"],
        "time_utc": time_utc, "jd": rad.get("jd"), "solar_lon": rad.get("solar_lon"),
        "shower": rad.get("shower"), "mag_apparent": mag_apparent,
        "mag_absolute": rad.get("mag_absolute"), "duration_s": ftp.get("duration_s"),
        "ra_beg": rad.get("ra_beg"), "dec_beg": rad.get("dec_beg"),
        "ra_end": rad.get("ra_end"), "dec_end": rad.get("dec_end"),
        "ra_radiant": rad.get("ra_radiant"), "dec_radiant": rad.get("dec_radiant"),
        "radiant_elev": rad.get("radiant_elev"),
        "angular_velocity": ftp.get("angular_velocity"),
        "num_segments": ftp.get("num_segments"), "fps": ftp.get("fps"),
        "azim_beg": ftp.get("azim_beg"), "elev_beg": ftp.get("elev_beg"),
        "azim_end": ftp.get("azim_end"), "elev_end": ftp.get("elev_end"),
    }


def _radiant_only_row(rad: dict) -> dict:
    bdt = rad["_begin_dt"]
    return {
        "ff_file": f"RAD_{bdt.strftime('%Y%m%d_%H%M%S_%f')}", "meteor_no": 1,
        "time_utc": rad.get("time_utc"), "jd": rad.get("jd"), "solar_lon": rad.get("solar_lon"),
        "shower": rad.get("shower"), "mag_apparent": rad.get("mag_apparent"),
        "mag_absolute": rad.get("mag_absolute"), "duration_s": None,
        "ra_beg": rad.get("ra_beg"), "dec_beg": rad.get("dec_beg"),
        "ra_end": rad.get("ra_end"), "dec_end": rad.get("dec_end"),
        "ra_radiant": rad.get("ra_radiant"), "dec_radiant": rad.get("dec_radiant"),
        "radiant_elev": rad.get("radiant_elev"),
        "angular_velocity": None, "num_segments": None, "fps": None,
        "azim_beg": None, "elev_beg": None, "azim_end": None, "elev_end": None,
    }


# ── SQLite schema ─────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    cam             TEXT NOT NULL,
    date            TEXT NOT NULL,
    ff_file         TEXT NOT NULL,
    meteor_no       INTEGER NOT NULL DEFAULT 1,
    time_utc        TEXT,
    jd              REAL,
    solar_lon       REAL,
    shower          TEXT,
    mag_apparent    REAL,
    mag_absolute    REAL,
    duration_s      REAL,
    ra_beg          REAL,
    dec_beg         REAL,
    ra_end          REAL,
    dec_end         REAL,
    ra_radiant      REAL,
    dec_radiant     REAL,
    radiant_elev    REAL,
    angular_velocity REAL,
    num_segments    INTEGER,
    fps             REAL,
    azim_beg        REAL,
    elev_beg        REAL,
    azim_end        REAL,
    elev_end        REAL,
    chunk_file      TEXT,
    PRIMARY KEY (cam, date, ff_file, meteor_no)
);

CREATE TABLE IF NOT EXISTS indexed_sessions (
    session_path    TEXT PRIMARY KEY,
    fingerprint     REAL,
    row_count       INTEGER,
    indexed_at      REAL
);

CREATE INDEX IF NOT EXISTS idx_det_date    ON detections(date);
CREATE INDEX IF NOT EXISTS idx_det_cam     ON detections(cam);
CREATE INDEX IF NOT EXISTS idx_det_shower  ON detections(shower);
CREATE INDEX IF NOT EXISTS idx_det_mag     ON detections(mag_apparent);
"""

# Bump when adding columns. _open_db() applies ALTER TABLE for any missing
# columns detected via PRAGMA table_info, keeping the DB backward-compatible.
_SCHEMA_VERSION = 2

# Version-keyed migrations: {version: [(table, col, col_def), ...]}
_MIGRATIONS: dict[int, list[tuple[str, str, str]]] = {
    # Version 1 is the baseline (covered by CREATE TABLE above).
    2: [("detections", "chunk_file", "TEXT")],
}

_INSERT_SQL = (
    f"INSERT OR REPLACE INTO detections ({', '.join(COLS)}) "
    f"VALUES ({', '.join(':' + c for c in COLS)})"
)


def _apply_schema_migrations(con: sqlite3.Connection) -> None:
    """Heal any column-level drift and apply version-keyed migrations."""
    existing_cols = {
        row[1]
        for row in con.execute("PRAGMA table_info(detections)").fetchall()
    }
    missing = set(COLS) - existing_cols
    if missing:
        logger.info("detection_indexer: healing missing column(s): %s", sorted(missing))
        for col in sorted(missing):
            con.execute(f"ALTER TABLE detections ADD COLUMN {col} REAL")

    on_disk: int = con.execute("PRAGMA user_version").fetchone()[0]
    if on_disk < _SCHEMA_VERSION:
        for version in range(on_disk + 1, _SCHEMA_VERSION + 1):
            for table, col, col_def in _MIGRATIONS.get(version, []):
                existing = {
                    row[1]
                    for row in con.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if col not in existing:
                    logger.info(
                        "detection_indexer: migration v%d: ALTER TABLE %s ADD COLUMN %s %s",
                        version, table, col, col_def,
                    )
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        con.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        con.commit()
        logger.info("detection_indexer: schema at version %d", _SCHEMA_VERSION)


def _open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA)
    _apply_schema_migrations(con)
    return con


# ── Chunk lookup ─────────────────────────────────────────────────────────────

_CHUNK_RE = re.compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_color\.mkv$')
_CHUNK_DURATION_S = 20


def _capture_base(cfg: dict) -> Path:
    return Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(BASE / 'color_capture')
    )


def _build_chunk_index(cam: str, night: str, cfg: dict) -> list[tuple[datetime, str]]:
    """Return sorted [(chunk_start_utc, filename)] for cam/night."""
    cap_dir = _capture_base(cfg) / cam / night
    if not cap_dir.exists():
        return []
    index: list[tuple[datetime, str]] = []
    for f in cap_dir.glob('*_color.mkv'):
        m = _CHUNK_RE.match(f.name)
        if not m:
            continue
        ds, ts = m.group(2), m.group(3)
        try:
            chunk_dt = datetime(
                int(ds[:4]), int(ds[4:6]), int(ds[6:8]),
                int(ts[:2]), int(ts[2:4]), int(ts[4:6]),
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue
        index.append((chunk_dt, f.name))
    index.sort(key=lambda x: x[0])
    return index


def _find_chunk(
    chunk_index: list[tuple[datetime, str]],
    time_utc: str,
) -> str | None:
    """Binary search: return chunk filename that contains time_utc, or None."""
    if not chunk_index or not time_utc:
        return None
    try:
        det_dt = datetime.fromisoformat(time_utc).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    lo, hi = 0, len(chunk_index) - 1
    idx = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if chunk_index[mid][0] <= det_dt:
            idx = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if idx < 0:
        return None
    chunk_start, filename = chunk_index[idx]
    if det_dt < chunk_start + timedelta(seconds=_CHUNK_DURATION_S):
        return filename
    return None


# ── Indexing logic ────────────────────────────────────────────────────────────

def _session_fingerprint(session: Path) -> float | None:
    """Largest mtime across all radiants + filtered FTPdetectinfo files.

    Any edit to any relevant file moves the fingerprint, so a re-index is
    triggered even when a session has several matching files.
    """
    latest: float | None = None
    try:
        for f in session.iterdir():
            n = f.name
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


def _index_session(
    con: sqlite3.Connection,
    cam: str,
    night: str,
    session: Path,
    cfg: dict | None = None,
) -> int:
    fingerprint = _session_fingerprint(session)
    if fingerprint is None:
        return 0

    row = con.execute(
        "SELECT fingerprint FROM indexed_sessions WHERE session_path=?",
        (str(session),),
    ).fetchone()
    if row and row[0] == fingerprint:
        return 0  # nothing changed

    detections = _session_detections(session)
    if not detections:
        # Still record the fingerprint so we don't re-parse an empty session.
        with con:
            con.execute(
                "INSERT OR REPLACE INTO indexed_sessions VALUES (?,?,?,?)",
                (str(session), fingerprint, 0, time.time()),
            )
        return 0

    chunk_idx = _build_chunk_index(cam, night, cfg or {})

    # No blanket delete: keying on the real FF filename + meteor number means
    # re-runs and overlapping Captured/Archived dirs INSERT OR REPLACE the same
    # rows, while genuinely distinct detections coexist.
    with con:
        for det in detections:
            payload = {c: det.get(c) for c in COLS}
            payload["cam"], payload["date"] = cam, night
            payload["chunk_file"] = _find_chunk(chunk_idx, det.get("time_utc") or "")
            con.execute(_INSERT_SQL, payload)
        con.execute(
            "INSERT OR REPLACE INTO indexed_sessions VALUES (?,?,?,?)",
            (str(session), fingerprint, len(detections), time.time()),
        )
    return len(detections)


def _scan(con: sqlite3.Connection, cfg: dict, since_date: str | None = None) -> int:
    total = 0
    for cam, rms_dir in _rms_camera_dirs(cfg):
        for session_cam, night, session in _all_sessions(rms_dir, since_date):
            try:
                n = _index_session(con, session_cam or cam, night, session, cfg)
                if n:
                    logger.info("indexed %s/%s: %d rows", session_cam or cam, night, n)
                    total += n
            except Exception as exc:
                logger.warning("failed to index %s: %s", session, exc)
    return total


# ── Main loop ─────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _stop
    logger.info("received signal %s, stopping", sig)
    _stop = True


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("detection_indexer starting, DB: %s", DB_PATH)
    con = _open_db()
    cfg = _load_config()

    # Startup: full scan — catches anything missed while service was down.
    logger.info("startup: full scan of all sessions")
    try:
        n = _scan(con, cfg, since_date=None)
        logger.info("startup scan complete: %d rows indexed", n)
    except Exception as exc:
        logger.error("startup scan failed: %s", exc)

    while not _stop:
        start = time.monotonic()
        cfg = _load_config()
        since = (datetime.now(timezone.utc) - timedelta(days=HOT_DAYS)).strftime("%Y%m%d")
        try:
            _scan(con, cfg, since_date=since)
        except Exception as exc:
            logger.error("scan error: %s", exc)
        elapsed = time.monotonic() - start
        sleep_for = max(0, HOT_POLL_INTERVAL - elapsed)
        # Sleep in 1s increments so SIGTERM is handled promptly
        for _ in range(int(sleep_for)):
            if _stop:
                break
            time.sleep(1)

    con.close()
    logger.info("detection_indexer stopped")


if __name__ == "__main__":
    main()
