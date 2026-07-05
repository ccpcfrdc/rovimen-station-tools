"""IAU Meteor Data Center shower data fetcher and background poller.

Fetches the MDC full shower list weekly, parses the pipe-delimited format,
and caches a slim dict keyed by IAU code. Routes call get_shower_data() —
they never hit upstream directly.

MDC file format: one record per observation/publication, multiple records
per shower. Records with 'M' in the Flags field are mean/averaged values
and are preferred. Solar longitudes are converted to approximate calendar
dates using the vernal-equinox anchor (λ☉=0° ≈ March 20).
"""

import datetime
import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

# ZHR at peak — from IMO Meteor Shower Calendar (not in MDC file).
# Variable showers noted with their typical range; value here is the
# typical/average peak, not storm-year maxima.
_ZHR: dict[str, int] = {
    "QUA": 120, "ACE": 6,  "GNO": 4,   "LYR": 18,  "ELY": 3,
    "ETA": 50,  "XHE": 3,  "ARI": 54,  "ZPE": 40,  "BTA": 25,
    "JBO": 2,   "JPE": 3,  "JXA": 5,   "PAU": 5,   "CAP": 5,
    "SDA": 25,  "NDA": 3,  "PER": 100, "KCG": 3,   "MIC": 10,
    "AUR": 6,   "SPE": 5,  "DSX": 14,  "SSG": 3,   "DRA": 5,
    "ORI": 20,  "EGE": 3,  "STA": 5,   "NTA": 5,   "TAU": 5,
    "LMI": 2,   "LEO": 15, "AMO": 5,   "AND": 3,   "NOO": 3,
    "NPI": 3,   "MON": 3,  "HYD": 3,   "GEM": 150, "COM": 5,
    "URS": 10,  "PHO": 3,  "ICE": 3,   "ANT": 2,   "SPO": 8,
}

_MDC_URL_TPL = (
    "https://www.ta3.sk/IAUC22DB/MDC2022/Etc/streamfulldata{year}.txt"
)
_POLL_INTERVAL_S = 7 * 24 * 3600   # refresh weekly
_HTTP_TIMEOUT    = 30

# Module-level state — sole writer is the poller thread.
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


# ── Solar longitude → calendar date ───────────────────────────────────────

def _sol_lon_to_date(lon: float) -> str:
    """Convert solar longitude (degrees, J2000) to approximate calendar date.

    Uses the vernal-equinox anchor: λ☉=0° ≈ March 20 (day 79 of the year).
    Accurate to ±1–2 days, which is sufficient for display purposes.
    """
    doy = (lon * 365.25 / 360.0 + 79.0) % 365.25
    dt  = datetime.date(2023, 1, 1) + datetime.timedelta(days=round(doy) - 1)
    return f"{dt.strftime('%b')} {dt.day}"


# ── MDC fetch ──────────────────────────────────────────────────────────────

def _fetch_raw() -> str | None:
    """Try current year then previous year for the MDC full data file."""
    year = datetime.date.today().year
    for y in (year, year - 1):
        url = _MDC_URL_TPL.format(year=y)
        try:
            r = requests.get(url, timeout=_HTTP_TIMEOUT)
            if r.status_code == 200:
                logger.info("MDC poller: fetched %s (%d bytes)", url, len(r.content))
                return r.text
            logger.warning("MDC poller: HTTP %s for %s", r.status_code, url)
        except Exception as exc:
            logger.warning("MDC poller: request failed for %s: %s", url, exc)
    return None


# ── MDC parse ─────────────────────────────────────────────────────────────

def _pf(s: str) -> float | None:
    """Parse a stripped string to float, returning None if blank/invalid."""
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse(raw: str) -> dict[str, dict]:
    """Parse MDC pipe-delimited text → {iau_code: shower_info}.

    Column indices (0-based, after splitting on '"|"' and stripping quotes):
      3  code       IAU 3-letter code
      4  s          status (negative = removed/rejected)
      5  subdate    submission date (YYYY-mm-dd)
      6  name       full shower name
      7  activity   activity type string
      8  LoSb       solar longitude at activity begin
      9  LoSe       solar longitude at activity end
     10  LoS        solar longitude at peak
     15  Vg         geocentric velocity (km/s)
     21  Flags      'M' present → mean/averaged record
     31  Origin     parent body
    """
    candidates: dict[str, list[dict]] = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line or line[0] in (':', '+'):
            continue

        parts = [p.strip().strip('"') for p in line.split('"|"')]
        if len(parts) < 32:
            continue

        code   = parts[3].strip()
        status = parts[4].strip()
        if not code or len(code) > 5:
            continue
        try:
            if int(status) < 0:
                continue
        except ValueError:
            continue

        flags   = parts[21].strip() if len(parts) > 21 else ''
        is_mean = 'M' in flags

        candidates.setdefault(code, []).append({
            'name':      parts[6].strip(),
            'subdate':   parts[5].strip(),
            'activity':  parts[7].strip(),
            'sol_begin': _pf(parts[8]),
            'sol_end':   _pf(parts[9]),
            'sol_peak':  _pf(parts[10]),
            'vg':        _pf(parts[15]),
            'parent':    parts[31].strip() if len(parts) > 31 else '',
            'is_mean':   is_mean,
        })

    result: dict[str, dict] = {}
    for code, recs in candidates.items():
        # Prefer mean records; within that, most recent submission date.
        pool = sorted(
            [r for r in recs if r['is_mean']] or recs,
            key=lambda r: r['subdate'],
            reverse=True,
        )
        b = pool[0]

        entry: dict[str, object] = {'name': b['name']}
        if b['vg'] is not None:
            entry['vg'] = round(b['vg'], 1)
        if b['parent']:
            entry['parent'] = b['parent']
        if b['sol_begin'] is not None:
            entry['activity_begin'] = _sol_lon_to_date(b['sol_begin'])
        if b['sol_end'] is not None:
            entry['activity_end']   = _sol_lon_to_date(b['sol_end'])
        if b['sol_peak'] is not None:
            entry['peak'] = _sol_lon_to_date(b['sol_peak'])
        if code in _ZHR:
            entry['zhr'] = _ZHR[code]

        result[code] = entry

    # Inject SPO manually — not present in MDC as a parseable shower.
    result.setdefault('SPO', {'name': 'Sporadic', 'zhr': _ZHR['SPO']})

    return result


# ── Public API ─────────────────────────────────────────────────────────────

def get_shower_data() -> dict[str, dict]:
    """Return a snapshot of the cached shower data. Empty dict before first fetch."""
    with _cache_lock:
        return dict(_cache)


# ── Background poller ──────────────────────────────────────────────────────

def _poll_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        raw = _fetch_raw()
        if raw:
            parsed = _parse(raw)
            with _cache_lock:
                global _cache
                _cache = parsed
            logger.info("MDC poller: cached %d showers", len(parsed))
        else:
            logger.warning("MDC poller: all fetch attempts failed — keeping existing cache")
        stop.wait(_POLL_INTERVAL_S)


def start_mdc_poller() -> threading.Event:
    """Spawn the MDC background poller daemon thread. Returns a stop event."""
    stop = threading.Event()
    threading.Thread(
        target=_poll_loop, args=(stop,),
        name="mdc-poller",
        daemon=True,
    ).start()
    return stop
