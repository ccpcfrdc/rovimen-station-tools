"""Astronomy helpers — twilight, moon phase, moon rise/set."""

import functools
import math
import subprocess
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Static-asset cache-busting
# ---------------------------------------------------------------------------
# Nginx in front of this app sets `Cache-Control: public, max-age=604800` on
# /static/ so that browsers reuse dashboard.js / dashboard.css for a week
# without round-tripping. That speed-up is great *until* we deploy a new
# dashboard.js that calls a renamed/removed endpoint: returning users keep
# executing the cached old JS against the new backend and get silently broken
# behaviour with no recovery short of a hard-refresh. To defeat that we append
# a `?v=<build>` query string to every same-origin <script>/<link> reference.
# Browsers treat the full URL (path + query) as the cache key, so any change
# to <build> forces a fresh fetch while letting unchanged builds stay cached.

def _compute_build_version() -> str:
    """Return a short Git SHA used as a cache-busting query for static assets.

    Falls back to a file-mtime tag from one of the deployed JS bundles when
    git metadata is not available (e.g. a stripped-down Docker image).
    Picks the newest mtime across the known bundles so the version flips
    whenever the deploy rsync touches any of them. Computed once per
    process so request handling stays free of subprocess overhead.
    """
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        if sha:
            return sha
    except Exception:
        pass
    # mtime fallback: any of the shipped bundles will do; pick the newest
    # so a deploy that only updates one of them still bumps the tag.
    static_dir = Path(__file__).parent / "static"
    candidates = [
        static_dir / "dashboard-station.js",
        static_dir / "dashboard-common.js",
        static_dir / "dashboard-overview.js",
        static_dir / "dashboard-rms.js",
        static_dir / "dashboard-vdb.js",
        static_dir / "dashboard-archive.js",
        static_dir / "dashboard-sysadmin.js",
        static_dir / "dashboard-highlights.js",
        static_dir / "dashboard-events.js",
        static_dir / "dashboard-video-modal.js",
        static_dir / "dashboard-multi-det-modal.js",
        static_dir / "dashboard-dome-webgl.js",
        static_dir / "twilight-slider.js",
        static_dir / "dashboard-showers.js",
        static_dir / "dashboard-showers.css",
        static_dir / "dashboard.css",
        static_dir / "dashboard-overview.css",
        static_dir / "dashboard-common.js",
        static_dir / "dashboard-video-modal.css",
        static_dir / "dashboard-multi-det-modal.css",
    ]
    mtimes = [int(p.stat().st_mtime) for p in candidates if p.exists()]
    if mtimes:
        return str(max(mtimes))
    return "dev"


BUILD_VERSION: str = _compute_build_version()


# ---------------------------------------------------------------------------
# Twilight computation (pure-Python, no ephem dependency)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=4096)
def _compute_twilight(lat: float, lon: float, date_str: str, horizon: float = -18.0) -> dict:
    """Compute sunset/sunrise UTC times for a given lat/lon and date (YYYYMMDD).

    Uses the NOAA solar calculator algorithm.  *horizon* is the sun altitude
    in degrees that defines the boundary (default -18 = astronomical twilight).
    Returns dict with sunset_utc, sunrise_utc (HH:MM strings) and
    duration_hours.  On failure (polar night / midnight sun) returns sensible
    defaults (18:00->06:00).
    """
    try:
        year = int(date_str[:4])
        month = int(date_str[4:6])
        day = int(date_str[6:8])

        # Julian day number
        if month <= 2:
            year -= 1
            month += 12
        A = int(year / 100)
        B = 2 - A + int(A / 4)
        jd = int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + B - 1524.5

        # Julian century
        jc = (jd - 2451545.0) / 36525.0

        # Geometric mean longitude of the sun (degrees)
        L0 = (280.46646 + jc * (36000.76983 + 0.0003032 * jc)) % 360
        # Mean anomaly (degrees)
        M = (357.52911 + jc * (35999.05029 - 0.0001537 * jc)) % 360
        Mr = math.radians(M)
        # Sun equation of center
        C = (math.sin(Mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
             + math.sin(2 * Mr) * (0.019993 - 0.000101 * jc)
             + math.sin(3 * Mr) * 0.000289)
        # Sun true longitude and apparent longitude
        sun_lon = L0 + C
        omega = 125.04 - 1934.136 * jc
        sun_app = sun_lon - 0.00569 - 0.00478 * math.sin(math.radians(omega))
        # Mean obliquity of the ecliptic
        obliq0 = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
        obliq = obliq0 + 0.00256 * math.cos(math.radians(omega))
        # Sun declination
        sin_dec = math.sin(math.radians(obliq)) * math.sin(math.radians(sun_app))
        dec = math.asin(sin_dec)
        # Equation of time (minutes)
        y = math.tan(math.radians(obliq / 2)) ** 2
        L0r = math.radians(L0)
        eot = 4 * math.degrees(
            y * math.sin(2 * L0r)
            - 2 * 0.016709 * math.sin(Mr)  # eccentricity approx
            + 4 * 0.016709 * y * math.sin(Mr) * math.cos(2 * L0r)
            - 0.5 * y * y * math.sin(4 * L0r)
            - 1.25 * 0.016709 ** 2 * math.sin(2 * Mr)
        )
        # Hour angle for the given horizon
        lat_r = math.radians(lat)
        cos_ha = (math.sin(math.radians(horizon)) - math.sin(lat_r) * math.sin(dec)) / (
            math.cos(lat_r) * math.cos(dec)
        )
        if cos_ha > 1 or cos_ha < -1:
            return {"sunset_utc": "18:00", "sunrise_utc": "06:00",
                    "sunset_min": 0, "sunrise_min": 0, "duration_hours": 12.0}
        ha = math.degrees(math.acos(cos_ha))

        # Solar noon (minutes from midnight UTC)
        noon_min = 720 - 4 * lon - eot

        sunset_min = noon_min + ha * 4
        sunrise_min = noon_min - ha * 4

        def _fmt(minutes: float) -> str:
            m = minutes % 1440
            h = int(m // 60)
            mi = int(m % 60)
            return f"{h:02d}:{mi:02d}"

        duration = (sunset_min - sunrise_min) / 60.0  # negative means night spans midnight
        # For "night duration" we want sunrise(next day) - sunset
        # Approximate: 24 - duration gives the dark period
        night_hours = 24.0 - abs(duration)

        return {
            "sunset_utc": _fmt(sunset_min),
            "sunrise_utc": _fmt(sunrise_min),
            "sunset_min": round(sunset_min % 1440),
            "sunrise_min": round(sunrise_min % 1440),
            "duration_hours": round(night_hours, 2),
        }
    except Exception:
        return {"sunset_utc": "18:00", "sunrise_utc": "06:00",
                "sunset_min": 1080, "sunrise_min": 360, "duration_hours": 12.0}


@functools.lru_cache(maxsize=1024)
def _compute_moon_phase(date_str: str) -> dict:
    """Return moon illumination fraction, phase name and emoji for a given date (YYYYMMDD)."""
    try:
        d = datetime.strptime(date_str, "%Y%m%d")
        y, mo, day = d.year, d.month, d.day
        if mo <= 2:
            y -= 1
            mo += 12
        A = int(y / 100)
        B = 2 - A + int(A / 4)
        jd = int(365.25 * (y + 4716)) + int(30.6001 * (mo + 1)) + day + 0.5 + B - 1524.5

        # Synodic phase — reference new moon 2000-01-06 18:14 UTC (JD 2451549.76)
        KNOWN_NEW_MOON = 2451549.76
        SYNODIC = 29.53059
        phase = ((jd - KNOWN_NEW_MOON) % SYNODIC) / SYNODIC
        illumination = (1 - math.cos(2 * math.pi * phase)) / 2

        if   phase < 0.034: emoji, name = "\U0001f311", "New Moon"
        elif phase < 0.250: emoji, name = "\U0001f312", "Waxing Crescent"
        elif phase < 0.266: emoji, name = "\U0001f313", "First Quarter"
        elif phase < 0.500: emoji, name = "\U0001f314", "Waxing Gibbous"
        elif phase < 0.534: emoji, name = "\U0001f315", "Full Moon"
        elif phase < 0.750: emoji, name = "\U0001f316", "Waning Gibbous"
        elif phase < 0.766: emoji, name = "\U0001f317", "Last Quarter"
        else:               emoji, name = "\U0001f318", "Waning Crescent"

        return {"illumination": round(illumination, 3), "phase_name": name, "phase_emoji": emoji}
    except Exception:
        return {"illumination": 0.5, "phase_name": "Unknown", "phase_emoji": "\U0001f319"}


@functools.lru_cache(maxsize=4096)
def _compute_moon_rise_set(lat: float, lon: float, date_str: str) -> dict:
    """Approximate moon rise/set as UTC minutes from midnight for a given date (YYYYMMDD).

    Uses simplified Meeus orbital elements — accuracy +-20-40 minutes.
    Returns None for each value when the moon is circumpolar or never rises.
    """
    try:
        d = datetime.strptime(date_str, "%Y%m%d")
        y, mo, day = d.year, d.month, d.day
        if mo <= 2:
            y -= 1
            mo += 12
        A = int(y / 100)
        B = 2 - A + int(A / 4)
        jd = int(365.25 * (y + 4716)) + int(30.6001 * (mo + 1)) + day + 0.5 + B - 1524.5

        D = jd - 2451545.0  # days since J2000.0

        # Simplified ecliptic position (Meeus ch. 47, low-precision)
        L   = (218.316 + 13.176396 * D) % 360
        M   = math.radians((134.963 + 13.064993 * D) % 360)
        F   = math.radians((93.272  + 13.229350 * D) % 360)
        lam = L + 6.289 * math.sin(M)   # ecliptic longitude (deg)
        bet = 5.128 * math.sin(F)        # ecliptic latitude  (deg)

        # Ecliptic -> equatorial
        eps   = math.radians(23.439 - 0.00013 * D / 36525)
        lr, br = math.radians(lam), math.radians(bet)
        x  = math.cos(br) * math.cos(lr)
        y_ = math.cos(eps) * math.cos(br) * math.sin(lr) - math.sin(eps) * math.sin(br)
        z  = math.sin(eps) * math.cos(br) * math.sin(lr) + math.cos(eps) * math.sin(br)
        ra  = math.degrees(math.atan2(y_, x)) % 360
        dec = math.degrees(math.asin(max(-1.0, min(1.0, z))))

        # Hour angle at rise/set (horizon -0.5 deg for limb + parallax)
        lat_r = math.radians(lat)
        dec_r = math.radians(dec)
        cos_H = (math.sin(math.radians(-0.5)) - math.sin(lat_r) * math.sin(dec_r)) / \
                (math.cos(lat_r) * math.cos(dec_r))
        if abs(cos_H) > 1:
            return {"moon_rise_min": None, "moon_set_min": None}

        H_deg = math.degrees(math.acos(cos_H))

        # GMST at 0h UT for the date
        jd0 = jd - 0.5
        T0  = (jd0 - 2451545.0) / 36525.0
        GMST = (100.4606184 + 36000.77004 * T0 + 0.000387933 * T0 ** 2) % 360

        # Hour angle of moon at 0h UT, then derive transit and rise/set
        HA0 = (GMST + lon - ra + 180) % 360 - 180   # -180...+180 deg
        RATE = 15.04107                               # deg/hour (sidereal)
        t_transit = (-HA0 / RATE) % 24               # hours from midnight UTC
        H_h = H_deg / RATE

        rise_min = round(((t_transit - H_h) % 24) * 60) % 1440
        set_min  = round(((t_transit + H_h) % 24) * 60) % 1440

        return {"moon_rise_min": rise_min, "moon_set_min": set_min}
    except Exception:
        return {"moon_rise_min": None, "moon_set_min": None}
