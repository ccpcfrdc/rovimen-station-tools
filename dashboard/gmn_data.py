"""GMN meteor-trajectory data fetcher — replaces the Datasette SQL endpoint.

Background
==========
The previous integration hit ``https://explore.globalmeteornetwork.org/
gmn_data_store/-/query.json?sql=…`` (Datasette over a 12 GB SQLite). Every
query started timing out for us in May 2026 — even ``SELECT 1``. The
Datasette homepage stays 200, so it's a backend / lock-contention issue
on the table-query path, not a ban or a network problem.

The GMN's actual documented public API is a tree of static text files at
``https://globalmeteornetwork.org/data/traj_summary_data/``, listed on
the public Data page. The schema is fully documented in
``GMN_orbit_data_columns.pdf``. Files are semicolon-separated, with
``#``-prefixed header lines; column 0 is the trajectory identifier, the
last column is the comma-separated participating-station list, and the
IAU shower code is inline (no secondary lookup table needed).

This module:

1. Maintains a local on-disk cache at ``ROVIMEN_GMN_CACHE_DIR`` (default
   ``/opt/rovimen/gmn_cache/``) of recent monthly + yearly files.
2. Re-fetches with ``If-Modified-Since`` (so we get a 304 most of the
   time) up to once an hour per (year, month).
3. Parses the semicolon format into the slim event dict the dashboard
   already consumes, tagged with ``has_ro`` (RO* witness) and ``de_only``
   (only our DE001B/C/D witnesses) so the overview overlay keeps working.

The output dict shape is bit-identical to the old Datasette-based
``_gmn_fetch_multistation()`` so the rest of the dashboard is unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────

BASE_URL = "https://globalmeteornetwork.org/data/traj_summary_data"
CACHE_DIR = Path(os.environ.get("ROVIMEN_GMN_CACHE_DIR", "/opt/rovimen/gmn_cache"))
CACHE_TTL_SECONDS = 3600  # re-check upstream every hour
HTTP_TIMEOUT = (10, 60)   # (connect, read)
USER_AGENT = "rovimen-dashboard/1.0 (+https://dashboard.rovimen.org)"

# ── "Our" station codes (configured from dashboard_config.yaml at startup) ──
# An event counts as "ours" if any witnessing camera code is in
# ``_OUR_CAM_CODES``. ``_HIGHLIGHT_CODES`` is an OPTIONAL subset of those: an
# event seen ONLY by highlight stations is tagged ``de_only`` (a muted
# secondary overlay). An empty highlight set — the default for any network —
# means a single, undifferentiated "ours". These are populated once at app
# startup by ``configure_station_codes`` from the station registry, so the
# dashboard is network-agnostic (no hard-coded RO/DE prefixes).
_OUR_CAM_CODES: frozenset[str] = frozenset()
_HIGHLIGHT_CODES: frozenset[str] = frozenset()


def configure_station_codes(
    our_codes: Iterable[str], highlight_codes: Iterable[str] = ()
) -> None:
    """Set the camera codes that define 'our' events plus the optional
    highlight subset. Call once at startup from the dashboard config."""
    global _OUR_CAM_CODES, _HIGHLIGHT_CODES
    _OUR_CAM_CODES = frozenset(c.upper() for c in our_codes)
    _HIGHLIGHT_CODES = frozenset(c.upper() for c in highlight_codes) & _OUR_CAM_CODES


def our_cam_codes() -> frozenset[str]:
    """The configured set of our camera codes (upper-case)."""
    return _OUR_CAM_CODES


def tag_witness(stations_upper: set[str]) -> tuple[bool, bool, bool]:
    """Return ``(is_ours, has_primary, highlight_only)`` for an event's
    upper-cased witness set. ``has_primary`` = a non-highlight 'our' station
    witnessed it (legacy ``has_ro``); ``highlight_only`` = only highlight
    stations saw it (legacy ``de_only``)."""
    our = stations_upper & _OUR_CAM_CODES
    if not our:
        return False, False, False
    primary = our - _HIGHLIGHT_CODES
    return True, bool(primary), not primary

_fetch_locks: dict[str, threading.Lock] = {}
_fetch_locks_guard = threading.Lock()


def _lock_for_month(month_key: str) -> threading.Lock:
    with _fetch_locks_guard:
        if month_key not in _fetch_locks:
            _fetch_locks[month_key] = threading.Lock()
        return _fetch_locks[month_key]


# ── Column layout (zero-based) ─────────────────────────────────────────
# Indices verified against a live 2026-05-20 row and the header banner
# documented in ``GMN_orbit_data_columns.pdf``.

_COL_ID         = 0   # Unique trajectory identifier (e.g. 20260520084811_0vpb5)
_COL_JD         = 1   # Julian date (Beginning)
_COL_TIME_UTC   = 2   # 'YYYY-MM-DD HH:MM:SS.ffffff' (Beginning UTC)
_COL_IAU_NO     = 3
_COL_IAU_CODE   = 4   # Shower 3-letter code or "..." when unidentified
_COL_SOL_LON    = 5   # Solar longitude (deg)
_COL_RA_GEO     = 7   # Geocentric RA of radiant (deg)
_COL_DEC_GEO    = 9   # Geocentric Dec of radiant (deg)
_COL_VGEO       = 15  # Vgeo km/s
_COL_A          = 23  # Semi-major axis (AU)
_COL_E          = 25  # Eccentricity
_COL_I          = 27  # Inclination (deg)
_COL_PERI       = 29  # Argument of perihelion (deg)
_COL_NODE       = 31  # Longitude of ascending node (deg)
_COL_Q          = 37  # Perihelion distance (AU)
_COL_Q_APH      = 43  # Aphelion distance Q (AU)
_COL_TISSERAND  = 49  # Tisserand parameter w.r.t Jupiter
_COL_LAT_BEG    = 63
_COL_LON_BEG    = 65
_COL_HT_BEG     = 67  # km
_COL_LAT_END    = 69
_COL_LON_END    = 71
_COL_HT_END     = 73  # km
_COL_DURATION   = 75  # Duration (sec)
_COL_PEAK_MAG   = 76  # Peak absolute magnitude
_COL_NUM_STAT   = 84
_COL_STATIONS   = 85  # comma-separated participating station list

_EXPECTED_COL_COUNT = 86


# ── Fetch + cache ──────────────────────────────────────────────────────

def _cache_path_for_month(year: int, month: int) -> Path:
    return CACHE_DIR / f"traj_summary_monthly_{year:04d}{month:02d}.txt"


def _monthly_url(year: int, month: int) -> str:
    return f"{BASE_URL}/monthly/traj_summary_monthly_{year:04d}{month:02d}.txt"


def _is_fresh(path: Path) -> bool:
    """True if ``path`` exists and was fetched/refreshed within the TTL."""
    try:
        return path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS
    except OSError:
        return False


def ensure_monthly_cached(year: int, month: int) -> Path:
    """Return a local path to the monthly traj_summary file, fetching if
    the cached copy is missing or older than ``CACHE_TTL_SECONDS``.

    Uses ``If-Modified-Since`` so subsequent fetches that are within the
    TTL but the upstream hasn't republished only burn a HEAD-like 304 —
    much friendlier than re-downloading 45 MB every hour.

    Raises ``requests.RequestException`` on hard upstream failure (so
    callers can decide whether to surface "GMN data unavailable" to the
    UI). Returns the local file path on success.
    """
    path = _cache_path_for_month(year, month)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if _is_fresh(path):
        return path

    url = _monthly_url(year, month)
    headers = {"User-Agent": USER_AGENT}
    if path.exists():
        # Conditional GET — server returns 304 if no new meteors since.
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        headers["If-Modified-Since"] = mtime.strftime("%a, %d %b %Y %H:%M:%S GMT")

    month_key = f"{year:04d}{month:02d}"
    with _lock_for_month(month_key):
        # Recheck inside the lock so concurrent callers don't pile on.
        if _is_fresh(path):
            return path
        logger.info("GMN: fetching %s", url)
        resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, stream=True)
        if resp.status_code == 304:
            os.utime(path, None)  # bump mtime so TTL resets
            logger.info("GMN: 304 not modified — cache reused")
            return path
        resp.raise_for_status()
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
        tmp.replace(path)
        logger.info("GMN: downloaded %s (%d bytes)", path.name, path.stat().st_size)
        return path


# ── Parse ──────────────────────────────────────────────────────────────

_FLOAT_NA = (None, "", "nan", "NaN", "...")


def _maybe_float(s: str) -> float | None:
    s = (s or "").strip()
    if s in _FLOAT_NA:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _split_stations(raw: str) -> list[str]:
    if not raw:
        return []
    return [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]


def parse_traj_summary(path: Path) -> Iterator[dict[str, Any]]:
    """Yield one slim event dict per data row in a GMN traj_summary file.

    Skips comment lines (``#``-prefixed) and any row whose column count
    doesn't match the documented schema (silently — the GMN file format
    is stable, so a mismatch is almost always corrupt data).
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line or line[0] == "#":
                continue
            cells = [c.strip() for c in line.rstrip("\n").split(";")]
            if len(cells) < _EXPECTED_COL_COUNT:
                continue  # malformed row
            stations = _split_stations(cells[_COL_STATIONS])
            if len(stations) < 2:
                continue  # single-station rows aren't of dashboard interest
            iau_code = cells[_COL_IAU_CODE].strip()
            if iau_code in ("...", ""):
                iau_code = None
            yield {
                "id":             cells[_COL_ID],
                "time":           cells[_COL_TIME_UTC],
                "lat_begin":      _maybe_float(cells[_COL_LAT_BEG]),
                "lon_begin":      _maybe_float(cells[_COL_LON_BEG]),
                "lat_end":        _maybe_float(cells[_COL_LAT_END]),
                "lon_end":        _maybe_float(cells[_COL_LON_END]),
                "altitude_begin": _maybe_float(cells[_COL_HT_BEG]),
                "altitude_end":   _maybe_float(cells[_COL_HT_END]),
                "peak_mag":       _maybe_float(cells[_COL_PEAK_MAG]),
                "shower":         iau_code,
                "velocity":       _maybe_float(cells[_COL_VGEO]),
                "duration_s":     _maybe_float(cells[_COL_DURATION]),
                "sol_lon":        _maybe_float(cells[_COL_SOL_LON]),
                "ra_geo":         _maybe_float(cells[_COL_RA_GEO]),
                "dec_geo":        _maybe_float(cells[_COL_DEC_GEO]),
                # Orbital elements (heliocentric)
                "orbit_a":        _maybe_float(cells[_COL_A]),
                "orbit_e":        _maybe_float(cells[_COL_E]),
                "orbit_i":        _maybe_float(cells[_COL_I]),
                "orbit_peri":     _maybe_float(cells[_COL_PERI]),
                "orbit_node":     _maybe_float(cells[_COL_NODE]),
                "orbit_q":        _maybe_float(cells[_COL_Q]),
                "orbit_q_aph":    _maybe_float(cells[_COL_Q_APH]),
                "tisserand":      _maybe_float(cells[_COL_TISSERAND]),
                "stations":       stations,
            }


# ── Date-filtered view used by the dashboard ───────────────────────────

# Trajectory IDs are stamped 'YYYYMMDD<rest>'. That lets us cheaply
# filter the monthly file by ID prefix without parsing each row's UTC
# timestamp.
_ID_DATE_PREFIX_RE = re.compile(r"^(\d{8})")


def events_for_date(date_str: str) -> dict[str, Any]:
    """Replacement for the old Datasette-based ``_gmn_fetch_multistation``.

    ``date_str`` is ``YYYY-MM-DD``. Returns the same payload shape:
    ``{date, total_upstream, events, has_ro_count, de_only_count,
    last_fetched_utc, error?}``. Never raises — on failure returns the
    payload with an ``error`` string so the dashboard renders a stub
    instead of 500-ing.
    """
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return {
            "date": date_str, "total_upstream": 0, "events": [],
            "has_ro_count": 0, "de_only_count": 0,
            "last_fetched_utc": fetched_at,
            "error": f"invalid date {date_str!r}; expected YYYY-MM-DD",
        }
    yyyymmdd = day.strftime("%Y%m%d")

    try:
        path = ensure_monthly_cached(day.year, day.month)
    except requests.RequestException as exc:
        return {
            "date": date_str, "total_upstream": 0, "events": [],
            "has_ro_count": 0, "de_only_count": 0,
            "last_fetched_utc": fetched_at,
            "error": f"GMN monthly fetch failed: {exc}",
        }

    try:
        events: list[dict[str, Any]] = []
        total = 0
        for ev in parse_traj_summary(path):
            m = _ID_DATE_PREFIX_RE.match(ev["id"] or "")
            if not m or m.group(1) != yyyymmdd:
                continue
            total += 1
            stations_upper = {s.upper() for s in ev["stations"]}
            is_ours, has_primary, highlight_only = tag_witness(stations_upper)
            if not is_ours:
                continue  # not one of ours — skip
            ev["has_ro"]  = has_primary
            ev["de_only"] = highlight_only
            events.append(ev)
    except (FileNotFoundError, OSError) as exc:
        logger.warning("GMN: failed to read cached file %s: %s", path, exc)
        return {
            "date": date_str, "total_upstream": 0, "events": [],
            "has_ro_count": 0, "de_only_count": 0,
            "last_fetched_utc": fetched_at,
        }

    return {
        "date":             date_str,
        "total_upstream":   total,
        "events":           events,
        "has_ro_count":     sum(1 for e in events if e["has_ro"]),
        "de_only_count":    sum(1 for e in events if e["de_only"]),
        "last_fetched_utc": fetched_at,
    }


# ── Orbit count aggregation ──────────────────────────────────────────

# Per-month cache: key = "YYYYMM", value = (file_mtime, {date_str: count})
_daily_count_cache: dict[str, tuple[float, dict[str, int]]] = {}
_daily_count_lock = threading.Lock()


def _daily_counts_for_month(year: int, month: int) -> dict[str, int]:
    """Return {YYYYMMDD: orbit_count} for RO-station orbits, mtime-cached."""
    key = f"{year:04d}{month:02d}"
    path = _cache_path_for_month(year, month)
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    with _daily_count_lock:
        cached = _daily_count_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]

    counts: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line or line[0] == "#":
                    continue
                cells = line.rstrip("\n").split(";")
                if len(cells) < _EXPECTED_COL_COUNT:
                    continue
                stations = _split_stations(cells[_COL_STATIONS])
                if len(stations) < 2:
                    continue
                if not any(s.upper().startswith("RO") for s in stations):
                    continue
                m = _ID_DATE_PREFIX_RE.match(cells[_COL_ID].strip())
                if m:
                    counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    except (FileNotFoundError, OSError):
        return {}

    with _daily_count_lock:
        _daily_count_cache[key] = (mtime, counts)
    return counts


_our_daily_count_cache: dict[str, tuple[float, dict[str, int]]] = {}
_our_daily_count_lock = threading.Lock()


def our_daily_counts_for_month(
    year: int, month: int, our_cam_codes: frozenset[str]
) -> dict[str, int]:
    """Return {YYYYMMDD: count} for orbits witnessed by at least one camera in our_cam_codes.

    Mtime-cached so repeated calls within the same process hit memory, not disk.
    The cache key includes a hash of our_cam_codes so adding/removing cameras
    from the public config invalidates the cache.
    """
    path = _cache_path_for_month(year, month)
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    codes_hash = hash(tuple(sorted(our_cam_codes)))
    cache_key = f"{year:04d}{month:02d}_{codes_hash}"
    with _our_daily_count_lock:
        cached = _our_daily_count_cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]

    counts: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line or line[0] == "#":
                    continue
                cells = line.rstrip("\n").split(";")
                if len(cells) < _EXPECTED_COL_COUNT:
                    continue
                stations = _split_stations(cells[_COL_STATIONS])
                if len(stations) < 2:
                    continue
                if not any(s.upper() in our_cam_codes for s in stations):
                    continue
                m = _ID_DATE_PREFIX_RE.match(cells[_COL_ID].strip())
                if m:
                    counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    except (FileNotFoundError, OSError):
        return {}

    with _our_daily_count_lock:
        _our_daily_count_cache[cache_key] = (mtime, counts)
    return counts


_orbit_result: dict[str, Any] | None = None
_orbit_computing = False


def _compute_orbit_counts() -> None:
    global _orbit_result, _orbit_computing
    from datetime import timedelta
    _orbit_computing = True
    try:
        now = datetime.now(timezone.utc)
        today = now.date()

        months_needed: set[tuple[int, int]] = set()
        d = today
        for _ in range(366):
            months_needed.add((d.year, d.month))
            d -= timedelta(days=1)

        for year, month in months_needed:
            try:
                ensure_monthly_cached(year, month)
            except Exception:
                pass

        cutoff_30d = (today - timedelta(days=30)).strftime("%Y%m%d")
        cutoff_12m = (today - timedelta(days=365)).strftime("%Y%m%d")

        total_12m = 0
        total_30d = 0
        latest_date = ""

        for year, month in sorted(months_needed):
            daily = _daily_counts_for_month(year, month)
            for date_str, count in daily.items():
                if date_str >= cutoff_12m:
                    total_12m += count
                if date_str >= cutoff_30d:
                    total_30d += count
                if date_str > latest_date:
                    latest_date = date_str

        last_night_count = 0
        last_night_date = ""
        if latest_date:
            ym = (int(latest_date[:4]), int(latest_date[4:6]))
            daily = _daily_counts_for_month(*ym)
            last_night_count = daily.get(latest_date, 0)
            last_night_date = f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:8]}"

        _orbit_result = {
            "last_12_months": total_12m,
            "last_30_days": total_30d,
            "last_night": last_night_count,
            "last_night_date": last_night_date,
        }
        logger.info("orbit_counts: computed (12m=%d, 30d=%d, last=%d on %s)",
                     total_12m, total_30d, last_night_count, last_night_date)
    except Exception:
        logger.exception("orbit_counts: computation failed")
    finally:
        _orbit_computing = False


def orbit_counts() -> dict[str, Any] | None:
    """Return cached orbit counts, or None if not yet computed.

    On first call, kicks off background computation. Subsequent calls
    return the cached result instantly.
    """
    global _orbit_computing
    if _orbit_result is not None:
        return _orbit_result
    if not _orbit_computing:
        threading.Thread(target=_compute_orbit_counts, daemon=True, name="orbit-counts").start()
    return None
