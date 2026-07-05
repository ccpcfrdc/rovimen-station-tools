"""Public read-only API for the ROVIMEN dashboard.

Exposes a versioned, CORS-open, unauthenticated surface for third-party
consumers (astromania.org, individual operators' own websites, etc.) at:

    /api/public/v1/...        — JSON endpoints
    /media/v1/...             — files served from the storage box archive

Only stations with ``public: true`` in dashboard_config.yaml are exposed.
Multi-station events may list witnesses from any GMN country, but their
clip/stack URLs are only emitted for the stations we own and have flagged
public; non-public witnesses surface as ``{code, lat, lon}`` markers only.

The module is a thin adapter: it never re-implements compute, it delegates
to the closures and module-level helpers already living inside
``rovimen_dashboard.py`` (which is where caching, SSHFS-timeout guards,
the GMN poller, etc. live). That keeps a single source of truth for the
data shape while letting third-party consumers depend on a contract that
doesn't drift with internal refactors.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import subprocess
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, abort, g, jsonify, request, send_file, stream_with_context

import api_keys
import coverage as coverage_mod
import detection_db
import gmn_data
import platepar_store
import rovimen_dashboard as rd

logger = logging.getLogger(__name__)

# ── Schema contract ─────────────────────────────────────────────────────
#
# Bump SCHEMA_VERSION when an existing field changes meaning or disappears.
# Add new optional fields freely without bumping — additive changes don't
# break consumers that ignore unknown keys.
SCHEMA_VERSION = "1.0.0"

# ── Validation regexes (defence-in-depth against path traversal) ────────
#
# These regexes are NOT the security boundary — they're a cheap pre-filter
# that rejects obvious garbage before the path-resolve check in
# ``_resolve_media_path`` (the actual safety net). Don't loosen them
# without re-checking the resolve guard there.
_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_COMPACT_RE = re.compile(r"^\d{8}$")
_CAMERA_RE = re.compile(r"^[A-Z0-9]{1,16}$")
_STATION_RE = re.compile(r"^[a-z0-9]{1,16}$")
_SHOWER_RE = re.compile(r"^[A-Z]{2,5}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.(mkv|mp4|webp|jpg|bin)$")
_MEDIA_SUBDIRS = {"meteors", "timelapse", "stacks", "rms"}

# Hard limit on the ``limit`` query parameter (pagination page size).
_MAX_LIMIT = 500
# Upper bound on a freeform event_id captured by ``<path:event_id>`` — Flask
# would otherwise accept arbitrary-length input through to our string ops.
_MAX_EVENT_ID_LEN = 256
# Rate-limit budgets applied per client IP via Flask-Limiter.
# Tuned for a real consumer (astromania.org poll loop @ 60 s + a handful of
# concurrent visitors) with substantial headroom; abuse limits kick in well
# above legitimate traffic but still cap the SSHFS / cache-fanout cost a
# scraper can impose.
_DEFAULT_RATE = "60/minute;1000/hour"     # JSON endpoints
_HEAVY_RATE = "20/minute;200/hour"        # /stats with month/all, /events, /timelapses
_MEDIA_RATE = "300/minute;5000/hour"      # /media/v1/* (each video play hits this)


# MP4 remux cache — stores completed remuxes so repeat requests are instant.
# Location is configurable; defaults to a tmpfs-friendly directory that
# survives the process but is cleared on reboot (acceptable: re-remux is cheap).
_MP4_CACHE_DIR = Path(
    os.environ.get("ROVIMEN_MP4_CACHE_DIR", "/tmp/rovimen_mp4_cache")
)
_MP4_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Total-size cap for the MP4 remux cache. The media routes are keyless, so a
# crawler walking the detection feed with ``?format=mp4`` would otherwise
# materialise one MP4 per clip until ``/tmp`` (or wherever the cache points)
# fills up and takes the whole VPS down. Cap the directory and evict the
# least-recently-used entries when a fresh remux would push it over.
_MP4_CACHE_MAX_BYTES = int(
    os.environ.get("ROVIMEN_MP4_CACHE_MAX_BYTES", str(2 * 1024 * 1024 * 1024))
)

# Concurrency cap for on-the-fly remuxes. The disk cap above stops the cache
# dir from filling up, but a keyless crawler hitting ``?format=mp4`` with many
# distinct filenames would still spawn one 30 s ffmpeg per request and starve
# the single gunicorn worker. Bound the number of simultaneous ffmpeg spawns;
# once exhausted a fresh cache miss returns 503 (with Retry-After) rather than
# queueing behind a busy worker. A cache hit never touches the semaphore.
_MP4_MAX_CONCURRENT = max(
    1, int(os.environ.get("ROVIMEN_MP4_MAX_CONCURRENT", "2"))
)
_mp4_remux_sem = threading.BoundedSemaphore(_MP4_MAX_CONCURRENT)
# How long a cache miss waits for a free remux slot before giving up with 503.
_MP4_REMUX_ACQUIRE_TIMEOUT = float(
    os.environ.get("ROVIMEN_MP4_REMUX_ACQUIRE_TIMEOUT", "2.0")
)


class _RemuxBusy(Exception):
    """Raised when no remux slot is free within the acquire timeout."""


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _mp4_cache_path(source: Path) -> Path:
    """Deterministic cache path for a given source MKV."""
    h = hashlib.sha256(str(source).encode()).hexdigest()[:16]
    return _MP4_CACHE_DIR / f"{h}_{source.stem}.mp4"


def _evict_mp4_cache(incoming_bytes: int = 0, max_bytes: int | None = None) -> None:
    """Evict least-recently-used cached MP4s until the cache plus an incoming
    file of ``incoming_bytes`` fits within ``max_bytes``.

    LRU is approximated by ``st_atime`` (falling back to ``st_mtime``). Since
    ``send_file`` reads the file on every cache hit, the access time tracks
    real usage well enough on a typical VPS filesystem. In-flight
    ``pub_remux_*`` temp files are counted toward the budget but never evicted
    (a concurrent request may be mid-write on them).
    """
    if max_bytes is None:
        max_bytes = _MP4_CACHE_MAX_BYTES
    if max_bytes <= 0:
        return
    try:
        entries = list(_MP4_CACHE_DIR.glob("*.mp4"))
    except OSError:
        return
    files: list[tuple[float, int, Path]] = []
    total = 0
    for p in entries:
        try:
            st = p.stat()
        except OSError:
            continue
        total += st.st_size
        # Skip in-flight remux temp files: they may be mid-write and about to
        # be renamed into the final cache path by a concurrent request.
        if p.name.startswith("pub_remux_"):
            continue
        atime = getattr(st, "st_atime", None) or st.st_mtime
        files.append((atime, st.st_size, p))
    # Oldest first so least-recently-used entries are dropped before fresh ones.
    files.sort(key=lambda e: e[0])
    budget = max_bytes - max(0, incoming_bytes)
    for _atime, size, p in files:
        if total <= budget:
            break
        _silent_unlink(p)
        total -= size


def _remux_mkv_to_mp4(source: Path, dest: Path) -> None:
    """Remux MKV → seekable MP4 (H.264 copy, faststart). Raises on error."""
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(source),
                "-c:v", "copy", "-c:a", "copy",
                "-movflags", "+faststart",
                "-f", "mp4", "-y", str(dest),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        _, stderr = proc.communicate(timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg exited {proc.returncode}: "
                f"{stderr.decode(errors='replace')[:200]}"
            )
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
            proc.wait(timeout=5)
        _silent_unlink(dest)
        raise
    except Exception:
        _silent_unlink(dest)
        raise


def _serve_as_mp4(source: Path, original_name: str) -> Response:
    """Remux *source* (MKV) to a seekable MP4 and serve it.

    Cache hit (repeat request): served directly from disk with full
    range-request support and immutable Cache-Control — zero remux overhead.

    Cache miss (first request): remux via ffmpeg (~1-2s for a short clip),
    store result in _MP4_CACHE_DIR, then serve. Cache survives until reboot.
    """
    mp4_name = original_name.rsplit(".", 1)[0] + ".mp4"
    cache_path = _mp4_cache_path(source)

    # Cache hit — MKVs are write-once so mtime equality is sufficient.
    if cache_path.exists():
        try:
            if cache_path.stat().st_mtime >= source.stat().st_mtime:
                resp = send_file(cache_path, mimetype="video/mp4",
                                 download_name=mp4_name, conditional=True)
                resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
                return resp
        except OSError:
            pass

    # Cache miss — spawning ffmpeg. Bound the number of simultaneous spawns so a
    # keyless crawl can't pin the single worker with many 30 s remuxes. Acquire
    # with a short timeout instead of blocking indefinitely; if no slot frees up
    # in time, surface a busy signal (503 + Retry-After) to the caller.
    if not _mp4_remux_sem.acquire(timeout=_MP4_REMUX_ACQUIRE_TIMEOUT):
        raise _RemuxBusy()
    try:
        # Re-check the cache: another request may have finished this exact remux
        # while we waited for a slot, so we can serve it without spawning.
        if cache_path.exists():
            try:
                if cache_path.stat().st_mtime >= source.stat().st_mtime:
                    resp = send_file(cache_path, mimetype="video/mp4",
                                     download_name=mp4_name, conditional=True)
                    resp.headers["Cache-Control"] = (
                        "public, max-age=86400, immutable"
                    )
                    return resp
            except OSError:
                pass

        # Remux to a unique temp file then atomically promote to cache.
        fd, tmp_str = tempfile.mkstemp(suffix=".mp4", prefix="pub_remux_",
                                       dir=_MP4_CACHE_DIR)
        os.close(fd)
        tmp_path = Path(tmp_str)
        try:
            _remux_mkv_to_mp4(source, tmp_path)
            # Make room for the new entry before promoting it into the cache, so
            # a keyless ?format=mp4 crawl can't grow the cache dir without bound.
            try:
                _evict_mp4_cache(incoming_bytes=tmp_path.stat().st_size)
            except OSError:
                pass
            tmp_path.rename(cache_path)
        except Exception:
            _silent_unlink(tmp_path)
            logger.exception("public_api: MKV remux failed for %s", original_name)
            abort(500)
    finally:
        _mp4_remux_sem.release()

    resp = send_file(cache_path, mimetype="video/mp4",
                     download_name=mp4_name, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
    return resp


def _validate_iso_date(iso_date: str) -> None:
    """Abort 400 unless ``iso_date`` is a real ``YYYY-MM-DD`` date.

    The regex check alone passes 2026-13-99 (which would later silently turn
    into "20261399" — junk that nothing finds) so we also parse it. Keeping
    the regex pre-filter avoids paying datetime.strptime on every request
    when a consumer is hammering with obviously-wrong input.
    """
    if not _DATE_ISO_RE.match(iso_date):
        abort(400, description="invalid date (expected YYYY-MM-DD)")
    try:
        datetime.strptime(iso_date, "%Y-%m-%d")
    except ValueError:
        abort(400, description="invalid date (expected YYYY-MM-DD)")


def _iso_to_compact(iso_date: str) -> str:
    """Convert ``YYYY-MM-DD`` to ``YYYYMMDD``. Validates first."""
    _validate_iso_date(iso_date)
    return iso_date.replace("-", "")


def _compact_to_iso(compact: str) -> str:
    """Convert ``YYYYMMDD`` to ``YYYY-MM-DD``. Defensive against bad inputs
    coming back from internal caches — we mint date strings on the way in,
    but this guards against future drift."""
    if not _DATE_COMPACT_RE.match(compact):
        return compact
    return f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]}"


def _today_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _last_night_iso_utc() -> str:
    """The 'tonight' anchor: yesterday's UTC date covers the night that just
    finished. Used as the default ``date`` for /detections and /events so a
    consumer calling at 09:00 local time gets the night that ran through
    dawn rather than a not-yet-started new night."""
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def _parse_optional_float(name: str) -> float | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        abort(400, description=f"{name} must be a number")


def _parse_optional_int(name: str, *, default: int, lo: int, hi: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        abort(400, description=f"{name} must be an integer")
    if v < lo or v > hi:
        abort(400, description=f"{name} must be between {lo} and {hi}")
    return v


def _parse_repeatable(name: str, validator: re.Pattern | None = None) -> list[str]:
    values = request.args.getlist(name)
    out: list[str] = []
    for v in values:
        # Allow comma-separated as well as repeated query params — friendlier
        # to consumers building URLs by hand.
        for part in v.split(","):
            p = part.strip()
            if not p:
                continue
            if validator and not validator.match(p):
                abort(400, description=f"invalid value for {name}: {p}")
            out.append(p)
    return out


# ── Station projection ─────────────────────────────────────────────────


def _public_stations(config: rd.DashboardConfig) -> dict[str, rd.StationConfig]:
    """Subset of config.stations that opted in to public exposure."""
    return {k: s for k, s in config.stations.items() if s.public}


def _camera_to_public(cam: rd.CameraConfig) -> dict[str, Any]:
    return {
        "code": cam.code,
        "label": cam.label or None,
        "azimuth": cam.az,
        "elevation": cam.alt,
    }


def _station_to_public(host_key: str, st: rd.StationConfig,
                        status: dict[str, Any] | None) -> dict[str, Any]:
    online = bool((status or {}).get("online"))
    last_updated = (status or {}).get("last_updated")
    return {
        "id": host_key,
        "label": st.label,
        "location_name": st.location_name or st.label,
        "latitude": st.lat,
        "longitude": st.lon,
        "country": _infer_country(host_key),
        "online": online,
        "last_seen_utc": last_updated,
        "cameras": [_camera_to_public(c) for c in st.cameras],
    }


def _infer_country(host_key: str) -> str | None:
    # Conservative: derive only from the host_key prefix. Returns None when
    # we can't classify so consumers see `null` rather than empty string.
    if host_key.startswith("gmnro"):
        return "RO"
    if host_key.startswith("gmnde"):
        return "DE"
    return None


# ── Detection projection ───────────────────────────────────────────────


def _detection_id(date_iso: str, host_key: str, cam: str, filename: str) -> str:
    """Stable, URL-safe detection id consumers can store + dedupe by.
    Embeds enough to reconstruct media URLs without an extra round-trip."""
    return f"{date_iso}:{host_key}:{cam}:{filename}"


def _parse_detection_id(det_id: str) -> tuple[str, str, str, str]:
    parts = det_id.split(":", 3)
    if len(parts) != 4:
        abort(404)
    date_iso, host_key, cam, filename = parts
    if not _DATE_ISO_RE.match(date_iso):
        abort(404)
    if not _STATION_RE.match(host_key):
        abort(404)
    if not _CAMERA_RE.match(cam):
        abort(404)
    if not _FILENAME_RE.match(filename):
        abort(404)
    return date_iso, host_key, cam, filename


def _public_base_url() -> str:
    """Canonical base URL prefix for ``clip_url`` / ``stack_url`` / etc.

    Resolution order:

    1. ``ROVIMEN_PUBLIC_BASE_URL`` env var when set — wins always. This is
       the right knob for production: ops pins the canonical public host
       (e.g. ``https://dashboard.example.net``) once and the API emits
       embeddable URLs regardless of how the in-front proxy forwards
       headers.
    2. ``request.host_url`` from Flask. Works in dev, and works through
       any proxy that correctly sets ``X-Forwarded-Proto`` (ProxyFix is
       already wired in ``security.configure_hardening``). Falls down
       when the proxy doesn't forward scheme — Tailscale Serve in
       front of the VPS terminates TLS but doesn't propagate
       ``X-Forwarded-Proto``, so without the env override
       ``clip_url`` comes back as ``http://`` and astromania.org
       (HTTPS) gets Mixed Content blocks when embedding ``<video>``.

    Returns the base WITHOUT a trailing slash for clean concatenation.
    """
    override = os.environ.get("ROVIMEN_PUBLIC_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    return request.host_url.rstrip("/")


def _media_clip_url(cam: str, date_iso: str, filename: str) -> str:
    return f"{_public_base_url()}/media/v1/clip/{cam}/{date_iso}/{filename}"


def _media_stack_url(cam: str, date_iso: str, filename: str) -> str:
    return f"{_public_base_url()}/media/v1/stack/{cam}/{date_iso}/{filename}"


def _media_timelapse_url(cam: str, date_iso: str, filename: str) -> str:
    return f"{_public_base_url()}/media/v1/timelapse/{cam}/{date_iso}/{filename}"


def _media_nightstack_url(cam: str, date_iso: str, filename: str) -> str:
    return f"{_public_base_url()}/media/v1/nightstack/{cam}/{date_iso}/{filename}"


def _detection_to_public(
    det: dict[str, Any], date_iso: str,
    public_host_keys: set[str],
) -> dict[str, Any] | None:
    """Project a single internal detection record onto the public schema.

    Returns ``None`` if the detection belongs to a non-public station — the
    caller filters those out.
    """
    host_key = det.get("host_key")
    if host_key not in public_host_keys:
        return None
    cam = det.get("cam")
    filename = det.get("filename")
    if not (cam and filename):
        return None

    rms = det.get("rms") or {}

    # Apparent magnitude preferred; fall back to absolute. Use an explicit
    # None test so a literal 0.0 (very bright fireball) isn't treated as
    # "missing" by ``or``.
    peak_mag = rms.get("mag_apparent")
    if peak_mag is None:
        peak_mag = rms.get("mag_absolute")

    clip_url = _media_clip_url(cam, date_iso, filename)
    stack_name = det.get("stack")
    stack_url = _media_stack_url(cam, date_iso, stack_name) if stack_name else None

    return {
        "id": _detection_id(date_iso, host_key, cam, filename),
        "station_id": host_key,
        "station_label": det.get("station_label"),
        "camera": cam,
        "date": date_iso,
        "time_utc": det.get("meteor_time"),
        "shower": rms.get("shower"),
        "peak_magnitude": peak_mag,
        "absolute_magnitude": rms.get("mag_absolute"),
        "duration_s": rms.get("duration_s"),
        "angular_velocity_deg_s": rms.get("angular_velocity"),
        "ra_radiant_deg": rms.get("ra_radiant"),
        "dec_radiant_deg": rms.get("dec_radiant"),
        "radiant_elevation_deg": rms.get("radiant_elev"),
        "solar_longitude_deg": rms.get("solar_lon"),
        "fps": rms.get("fps"),
        "lock_type": det.get("lock_type"),
        "detection_offset_s": det.get("detection_offset_s"),
        "clip_url": clip_url,
        "stack_url": stack_url,
        "thumbnail_url": stack_url,  # alias for consumers expecting `thumbnail_url`
    }


def _detection_from_index_row(
    row: dict[str, Any],
    date_iso: str,
    cam_to_host: dict[str, str],
    public_stations: dict[str, Any],
) -> dict[str, Any] | None:
    """Project a detection_db index row onto the public schema.

    When chunk_file is populated in the index, the detection ID and clip_url
    use the MKV chunk filename (same as the slow path). When chunk_file is
    absent (older rows not yet re-indexed), the FF filename is used for the ID
    and clip_url is None.
    """
    cam = row.get("cam")
    host_key = cam_to_host.get(cam or "")
    if not host_key:
        return None
    st = public_stations.get(host_key)
    mag = row.get("mag_apparent") if row.get("mag_apparent") is not None else row.get("mag_absolute")
    chunk_file = row.get("chunk_file")
    id_filename = chunk_file or (row.get("ff_file") or "")
    clip_url = _media_clip_url(cam, date_iso, chunk_file) if chunk_file else None
    return {
        "id": _detection_id(date_iso, host_key, cam, id_filename),
        "station_id": host_key,
        "station_label": st.label if st else None,
        "camera": cam,
        "date": date_iso,
        "time_utc": row.get("time_utc"),
        "shower": row.get("shower"),
        "peak_magnitude": mag,
        "absolute_magnitude": row.get("mag_absolute"),
        "duration_s": row.get("duration_s"),
        "angular_velocity_deg_s": row.get("angular_velocity"),
        "ra_radiant_deg": row.get("ra_radiant"),
        "dec_radiant_deg": row.get("dec_radiant"),
        "radiant_elevation_deg": row.get("radiant_elev"),
        "solar_longitude_deg": row.get("solar_lon"),
        "fps": row.get("fps"),
        "lock_type": None,
        "detection_offset_s": None,
        "clip_url": clip_url,
        "stack_url": None,
        "thumbnail_url": None,
    }


def _filter_detection(
    d: dict[str, Any], *,
    min_mag: float | None,
    max_mag: float | None,
    stations: list[str] | None,
    showers: list[str] | None,
) -> bool:
    if stations and d["station_id"] not in stations:
        return False
    if showers:
        sh = (d.get("shower") or "").upper()
        if sh not in showers:
            return False
    pm = d.get("peak_magnitude")
    if min_mag is not None and (pm is None or pm < min_mag):
        return False
    if max_mag is not None and (pm is None or pm > max_mag):
        return False
    return True


# ── Event projection (multi-station / GMN trajectory) ──────────────────


def _event_to_public(
    ev: dict[str, Any], date_iso: str,
    public_host_keys: set[str],
) -> dict[str, Any]:
    """Project an internal multi-station event onto the public schema.

    Multi-station events come from ``_compute_detections_payload`` and have
    ``witnesses`` already grouped per (host_key, cam). We attach playable
    clip URLs for public witnesses and drop non-public witnesses to a
    coordinate-only marker.
    """
    witnesses_out: list[dict[str, Any]] = []
    peak_mags: list[float] = []
    durations: list[float] = []

    for w in ev.get("witnesses", []) or []:
        host_key = w.get("host_key")
        cam = w.get("cam")
        rms = w.get("rms") or {}

        # Collect magnitude + duration across all witnesses for event-level aggregates.
        pm = rms.get("mag_apparent")
        if pm is None:
            pm = rms.get("mag_absolute")
        if pm is not None:
            peak_mags.append(pm)
        dur = rms.get("duration_s")
        if dur is not None:
            durations.append(dur)

        if host_key in public_host_keys:
            stack_name = w.get("stack")
            witnesses_out.append({
                "station_id": host_key,
                "station_label": w.get("station_label"),
                "camera": cam,
                "time_utc": w.get("meteor_time"),
                "peak_magnitude": pm,
                "duration_s": dur,
                "clip_url": _media_clip_url(cam, date_iso, w.get("filename") or "") if w.get("filename") else None,
                "stack_url": _media_stack_url(cam, date_iso, stack_name) if stack_name else None,
                "public": True,
            })
        else:
            witnesses_out.append({
                "station_id": host_key,
                "camera": cam,
                "public": False,
            })

    # Brightest magnitude across witnesses (lowest numerical value = brightest).
    event_peak_mag = min(peak_mags) if peak_mags else None
    # Longest duration is the most complete measurement.
    event_duration = max(durations) if durations else None

    return {
        "id": f"local:{ev.get('event_time')}",
        "time_utc": ev.get("event_time"),
        "date": date_iso,
        "witness_count": ev.get("witness_count"),
        "station_count": ev.get("station_count"),
        "peak_magnitude": event_peak_mag,
        "duration_s": event_duration,
        "witnesses": witnesses_out,
        "trajectory": None,  # only GMN-resolved events carry an orbit
        "source": "rovimen",
    }


def _gmn_event_to_public(
    ev: dict[str, Any], date_iso: str,
    public_stations: dict[str, rd.StationConfig],
    detections_today: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project a GMN-resolved multi-station event onto the public schema.

    Witnesses come back from GMN as raw RMS station codes (e.g. ``RO000A``).
    We resolve them against our own station list to enrich with lat/lon and,
    when the witness is one of our public cameras AND a local detection
    matches within ±2 s, embed the clip URL.
    """
    raw_stations = ev.get("stations") or []

    # Build a quick lookup: cam code -> (host_key, station object) for OUR cams.
    cam_to_owner: dict[str, tuple[str, rd.StationConfig]] = {}
    for host_key, st in public_stations.items():
        for c in st.cameras:
            cam_to_owner[c.code.upper()] = (host_key, st)

    ev_time_str = ev.get("time")
    ev_time_dt: datetime | None = None
    if ev_time_str:
        try:
            ev_time_dt = datetime.fromisoformat(ev_time_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            ev_time_dt = None

    # Index local detections by (host_key, cam) for the ±2 s match.
    local_by_cam: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for d in detections_today:
        local_by_cam.setdefault((d["station_id"], d["camera"]), []).append(d)

    witnesses_out: list[dict[str, Any]] = []
    for st_code in raw_stations:
        upper = (st_code or "").upper()
        owner = cam_to_owner.get(upper)
        if owner is None:
            # Foreign witness (e.g. another country's GMN site) — emit a
            # marker but no clip URLs.
            witnesses_out.append({
                "rms_code": upper,
                "country": upper[:2] if len(upper) >= 2 else None,
                "public": False,
            })
            continue
        host_key, st = owner
        match_url = None
        match_stack = None
        match_time = None
        if ev_time_dt is not None:
            for d in local_by_cam.get((host_key, upper), []):
                t = d.get("time_utc")
                if not t:
                    continue
                try:
                    t_dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if abs((t_dt - ev_time_dt).total_seconds()) <= 2.0:
                    match_url = d.get("clip_url")
                    match_stack = d.get("stack_url")
                    match_time = t
                    break
        witnesses_out.append({
            "station_id": host_key,
            "station_label": st.label,
            "rms_code": upper,
            "camera": upper,
            "latitude": st.lat,
            "longitude": st.lon,
            "country": _infer_country(host_key),
            "time_utc": match_time,
            "clip_url": match_url,
            "stack_url": match_stack,
            "public": True,
        })

    return {
        "id": f"gmn:{ev.get('id')}",
        "trajectory_id": ev.get("id"),
        "time_utc": ev_time_str,
        "date": date_iso,
        "witness_count": len(raw_stations),
        "witnesses": witnesses_out,
        "shower": ev.get("shower"),
        "peak_magnitude": ev.get("peak_mag"),
        "duration_s": ev.get("duration_s"),
        "velocity_km_s": ev.get("velocity"),
        "trajectory": {
            "lat_begin": ev.get("lat_begin"),
            "lon_begin": ev.get("lon_begin"),
            "altitude_begin_km": ev.get("altitude_begin"),
            "lat_end": ev.get("lat_end"),
            "lon_end": ev.get("lon_end"),
            "altitude_end_km": ev.get("altitude_end"),
        },
        "orbit": {
            "a_au": ev.get("orbit_a"),
            "e": ev.get("orbit_e"),
            "i_deg": ev.get("orbit_i"),
            "peri_deg": ev.get("orbit_peri"),
            "node_deg": ev.get("orbit_node"),
            "q_au": ev.get("orbit_q"),
            "q_aph_au": ev.get("orbit_q_aph"),
            "tisserand": ev.get("tisserand"),
            "ra_geo_deg": ev.get("ra_geo"),
            "dec_geo_deg": ev.get("dec_geo"),
            "solar_lon_deg": ev.get("sol_lon"),
        },
        "source": "gmn",
    }


# ── Media path resolution ──────────────────────────────────────────────


def _resolve_media_path(camera: str, date_iso: str, filename: str,
                         subdir: str) -> Path | None:
    """Map a public media URL to a path inside ARCHIVE_PATH.

    Returns ``None`` if any component fails validation or the file doesn't
    exist. ``subdir`` is one of ``_MEDIA_SUBDIRS``. The date arrives as ISO
    YYYY-MM-DD on the wire; on disk the directory is YYYYMMDD.
    """
    if not _CAMERA_RE.match(camera):
        return None
    if not _DATE_ISO_RE.match(date_iso):
        return None
    if subdir not in _MEDIA_SUBDIRS:
        return None
    if not _FILENAME_RE.match(filename):
        return None
    compact = date_iso.replace("-", "")
    path = rd.ARCHIVE_PATH / camera / compact / subdir / filename
    # Resolve to absolute and check it stays under ARCHIVE_PATH — defence
    # against any future regex hole that lets ".." slip through. We use
    # Path.is_relative_to (Py 3.9+) rather than a string-prefix compare to
    # avoid a sibling-directory collision (e.g. /srv/rovimen/archive vs
    # /srv/rovimen/archive-evil would both pass startswith).
    try:
        resolved = path.resolve()
        archive_root = rd.ARCHIVE_PATH.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        if not resolved.is_relative_to(archive_root):
            return None
    except ValueError:
        return None
    if not resolved.is_file():
        return None
    return resolved


# ── Cache header helpers ───────────────────────────────────────────────


def _public_json(payload: Any, *, max_age: int) -> Response:
    """Wrap rd._json_cached for public endpoints with a CDN-friendly header.

    Switches Cache-Control from ``private`` (used for authenticated dashboard
    endpoints) to ``public`` so a CDN / browser cache shared across many
    visitors is allowed — these are anonymous responses.
    """
    resp = rd._json_cached(payload, max_age=max_age)
    resp.headers["Cache-Control"] = f"public, max-age={max_age}"
    return resp


def _detections_max_age(date_iso: str) -> int:
    """Tonight: 60s (clips still being locked). Past nights: 1h (sealed)."""
    today = _today_iso_utc()
    return 60 if date_iso >= today else 3600


# ── Registration ───────────────────────────────────────────────────────


def register_public_routes(
    app: Flask,
    *,
    config: rd.DashboardConfig,
    compute_detections_payload: Callable[[str], dict[str, Any]],
    get_timelapses_payload: Callable[[str], dict[str, Any]],
    station_cache_get_status: Callable[[str], dict[str, Any] | None],
    limiter: Any = None,
) -> None:
    """Mount the public API onto an existing Flask app.

    Args:
        app: The Flask application to attach routes to.
        config: Loaded DashboardConfig; ``station.public`` decides exposure.
        compute_detections_payload: Closure over ``create_app`` that returns
            the cached per-night detections payload (date arg is YYYYMMDD).
        get_timelapses_payload: SWR-cached accessor returning the per-host
            timelapses payload — passing the cache-aware version (not the
            raw compute) is mandatory so a public hit can't trigger a fresh
            per-cam SSHFS walk.
        station_cache_get_status: ``StationCache.get_status`` so we can
            report ``online`` without forcing a tunnel round-trip.
        limiter: Flask-Limiter instance from ``security.init_limiter``.
            When provided, every public view is decorated with a per-IP
            budget (see ``_DEFAULT_RATE`` / ``_HEAVY_RATE`` / ``_MEDIA_RATE``).
            Passing ``None`` is supported for tests / standalone use but
            should not happen in production wiring.
    """

    # ── Public-camera allowlist (recomputed per request) ────────────────
    #
    # /media/v1/* is keyless on purpose — browsers can't attach API-key
    # headers to ``<video src=>``. We compensate by serving media only for
    # cameras owned by stations that opted in (``public: true``). A leaked
    # URL for a non-public station's clip would otherwise be fully
    # fetchable forever; cap that off here at the edge of the surface.
    #
    # Recomputed from the live config on every media request so that
    # toggling ``public`` via the admin UI takes effect immediately
    # without a process restart. The scan is cheap — config.stations is
    # already in memory, typically <20 entries.

    def _public_media_cameras() -> frozenset[str]:
        """Camera codes belonging to ``public: true`` stations, upper-cased."""
        return frozenset(
            c.code.upper()
            for st in _public_stations(config).values()
            for c in st.cameras
        )

    def _camera_is_public_media(camera: str) -> bool:
        """True if ``camera`` is owned by a ``public: true`` station.

        Caller is responsible for falling back to ``abort(404)`` (not 403)
        on a miss — we don't want to disclose that a non-public station's
        URL exists.
        """
        if not camera:
            return False
        return camera.upper() in _public_media_cameras()

    def _limiter_key() -> str:
        """Rate-limit identity: prefer the authenticated API key id, fall
        back to the client IP. A legitimate consumer's traffic is bucketed
        per-key (independent of where their requests originate), while
        keyless requests (only possible on /media/v1/ or when
        ``ROVIMEN_API_KEYS_REQUIRED=0``) bucket per-IP as before."""
        key = getattr(g, "api_key", None)
        if key is not None:
            return f"key:{key.id}"
        from security import _client_ip
        return _client_ip()

    def _rate_limited(rate: str) -> Callable:
        """Return a decorator that applies the given limiter rate, or a
        no-op when no limiter was wired. Lets us write
        ``@_rate_limited(_HEAVY_RATE)`` inline on each view without
        branching at every call site. When the request has a key with a
        per-key override, that override wins over ``rate``."""
        if limiter is None:
            return lambda fn: fn

        def _effective_limit() -> str:
            key = getattr(g, "api_key", None)
            if key is not None and key.rate_limit_override:
                return key.rate_limit_override
            return rate

        return limiter.limit(_effective_limit, key_func=_limiter_key)

    # ── API-key gate (JSON endpoints only — /media/v1 stays keyless) ──
    def _require_api_key(view: Callable) -> Callable:
        """Reject requests to the wrapped view unless the caller carries
        a valid ``X-API-Key`` header or ``Authorization: Bearer`` token.

        ``?key=`` URL parameter is explicitly NOT accepted: it leaks
        into nginx access logs, browser history and ``Referer``
        headers. Requests that try to pass it get a 400 with a clear
        hint pointing to the header form.

        On success, attaches the matching :class:`api_keys.ApiKey` to
        Flask's ``g`` so downstream helpers (``_limiter_key``, audit log)
        can identify the consumer.

        Bypassed when ``ROVIMEN_API_KEYS_REQUIRED=0`` (local dev) — but
        ``?key=`` is rejected even in dev so accidental URLs don't end
        up in production tests.
        """
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            # Explicit refusal of the URL-parameter form: shipping a
            # secret in the query string is a logging / referer leak
            # vector, and we'd rather break consumers loudly than let
            # them silently keep the bad pattern.
            if "key" in request.args:
                resp = jsonify({
                    "error": "url_key_param_disabled",
                    "detail": (
                        "API keys must be passed via the X-API-Key "
                        "header or Authorization: Bearer; the ?key= "
                        "URL parameter is no longer accepted."
                    ),
                })
                resp.status_code = 400
                return resp
            secret = (
                request.headers.get("X-API-Key")
                or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                or ""
            ).strip()
            if not api_keys.is_required():
                # Soft-mode: accept the secret if supplied (so per-key
                # rate-limit + audit still work in dev), otherwise pass.
                key = api_keys.validate(secret) if secret else None
                if key is not None:
                    g.api_key = key
                return view(*args, **kwargs)
            key = api_keys.validate(secret) if secret else None
            if key is None:
                resp = jsonify({
                    "error": "missing_or_invalid_api_key",
                    "detail": (
                        "supply your API key via the X-API-Key header "
                        "or Authorization: Bearer <key>"
                    ),
                })
                resp.status_code = 401
                # Hint that the client may want to obtain a key.
                resp.headers["WWW-Authenticate"] = (
                    'ApiKey realm="rovimen-public-api"'
                )
                return resp
            g.api_key = key
            return view(*args, **kwargs)
        return wrapper

    # ── CORS injection (scoped to public surface only) ────────────────
    @app.after_request
    def _public_cors(response: Response) -> Response:
        path = request.path or ""
        if (path.startswith("/api/public/v1/") or path.startswith("/media/v1/")
                or path == "/api/public/v1"):
            # Wildcard origin is intentional: this is a read-only,
            # unauthenticated, no-cookie surface — any website can embed.
            # We do NOT add `Vary: Origin` here: with ACAO `*` the
            # response body is identical for every origin, and a `Vary`
            # entry would needlessly split shared-proxy caches. We DO
            # add `Vary: Cookie` to keep an authenticated dashboard
            # response from ever being served as an anonymous cache hit
            # in case a misrouted request lands on a public path while
            # carrying a session cookie.
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers.add("Vary", "Cookie")
            response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "X-API-Key, Authorization, If-None-Match, Range"
            response.headers["Access-Control-Expose-Headers"] = (
                "ETag, Content-Length, Content-Range, Accept-Ranges"
            )
        return response

    # Pre-flight handler — Flask routes only GET/HEAD by default for our
    # endpoints, so a CORS preflight on a path that exists would 405. Catch
    # OPTIONS centrally and return the same CORS headers above.
    @app.route("/api/public/v1/<path:_>", methods=["OPTIONS"])
    @app.route("/media/v1/<path:_>", methods=["OPTIONS"])
    def _public_preflight(_):  # noqa: ANN001 — path var unused
        return ("", 204)

    def _safe_detections(compact: str) -> dict[str, Any]:
        """Call the detections-payload closure, returning an empty shell on
        any internal error rather than letting a 500 HTML page leak to a
        third-party consumer. The closure can raise (SSH tunnel timeouts,
        cache lock contention during a station outage) — we'd rather hand
        back ``{"detections": [], "events": []}`` so the consumer can keep
        rendering its UI without an error state."""
        try:
            return compute_detections_payload(compact) or {}
        except Exception:
            logger.exception("public_api: compute_detections_payload failed for %s", compact)
            return {"detections": [], "events": []}

    def _safe_gmn(date_iso: str) -> dict[str, Any]:
        """Same defensive wrapper around the GMN cache lookup. ``_gmn_get_cached``
        returns a stub on miss but is defensive against future drift."""
        try:
            return rd._gmn_get_cached(date_iso) or {}
        except Exception:
            logger.exception("public_api: _gmn_get_cached failed for %s", date_iso)
            return {"events": []}

    def _safe_timelapses(host_key: str) -> dict[str, Any]:
        try:
            return get_timelapses_payload(host_key) or {}
        except Exception:
            logger.exception("public_api: timelapses payload failed for %s", host_key)
            return {}

    # ── JSON 429 handler for the public surface ───────────────────────
    # Flask-Limiter's default error response is text/html; a programmatic
    # consumer (astromania.org's WordPress plugin, an operator's curl
    # script) wants a JSON envelope and a Retry-After header it can
    # honour without parsing HTML. Scope the override to public paths so
    # the authenticated dashboard's 429 behaviour stays untouched.
    @app.errorhandler(429)
    def _public_429(exc):  # noqa: ANN001 — Werkzeug HTTPException subtype
        path = request.path or ""
        is_public = (
            path.startswith("/api/public/v1/")
            or path.startswith("/media/v1/")
            or path == "/api/public/v1"
        )
        if not is_public:
            # Defer to whatever default Flask had (HTML page).
            return exc.get_response()
        # Flask-Limiter attaches a ``limit`` attribute carrying reset metadata.
        # Compute Retry-After defensively — older flask-limiter versions
        # expose ``reset_at`` (unix seconds) while newer ones expose
        # ``limit.reset_at`` or only ``description``. Default to 60 s.
        retry_after = 60
        try:
            reset_at = getattr(exc, "reset_at", None) or getattr(
                getattr(exc, "limit", None), "reset_at", None
            )
            if reset_at:
                import time as _time
                retry_after = max(1, int(reset_at - _time.time()))
        except Exception:
            pass
        body = {
            "error": "rate_limit_exceeded",
            "detail": str(getattr(exc, "description", "Too many requests")),
            "retry_after_seconds": retry_after,
        }
        resp = jsonify(body)
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    # ── Index ─────────────────────────────────────────────────────────
    @app.route("/api/public/v1")
    @app.route("/api/public/v1/")
    @_rate_limited(_DEFAULT_RATE)
    def public_index():
        return _public_json(
            {
                "service": "rovimen-public-api",
                "schema_version": SCHEMA_VERSION,
                "documentation_url": "https://github.com/alextudorica/rovimen-station-tools/blob/main/docs/public_api.md",
                "endpoints": {
                    "stations":      "/api/public/v1/stations",
                    "station":       "/api/public/v1/stations/<id>",
                    "detections":    "/api/public/v1/detections",
                    "detection":     "/api/public/v1/detections/<id>",
                    "events":        "/api/public/v1/events",
                    "event":         "/api/public/v1/events/<id>",
                    "timelapses":    "/api/public/v1/timelapses",
                    "nightstacks":   "/api/public/v1/nightstacks",
                    "stats":         "/api/public/v1/stats",
                    "fov":           "/api/public/v1/fov",
                    "orbits":        "/api/public/v1/orbits",
                },
                "media": {
                    "clip":         "/media/v1/clip/<camera>/<date>/<filename>",
                    "stack":        "/media/v1/stack/<camera>/<date>/<filename>",
                    "timelapse":    "/media/v1/timelapse/<camera>/<date>/<filename>",
                    "nightstack":   "/media/v1/nightstack/<camera>/<date>/<filename>",
                },
                "now_utc": datetime.now(timezone.utc).isoformat(),
            },
            max_age=3600,
        )

    # ── Stations ──────────────────────────────────────────────────────
    @app.route("/api/public/v1/stations")
    @_require_api_key
    @_rate_limited(_DEFAULT_RATE)
    def public_stations():
        out = [
            _station_to_public(k, s, station_cache_get_status(k))
            for k, s in _public_stations(config).items()
        ]
        out.sort(key=lambda x: x["id"])
        return _public_json({
            "stations": out,
            "count": len(out),
        }, max_age=300)

    @app.route("/api/public/v1/stations/<station_id>")
    @_require_api_key
    @_rate_limited(_DEFAULT_RATE)
    def public_station_detail(station_id: str):
        if not _STATION_RE.match(station_id):
            abort(404)
        public = _public_stations(config)
        st = public.get(station_id)
        if st is None:
            abort(404)
        body = _station_to_public(station_id, st, station_cache_get_status(station_id))
        return _public_json(body, max_age=300)

    # ── Detections (single-station meteor clips) ──────────────────────
    @app.route("/api/public/v1/detections")
    @_require_api_key
    @_rate_limited(_DEFAULT_RATE)
    def public_detections():
        date_iso = request.args.get("date") or _last_night_iso_utc()
        _validate_iso_date(date_iso)
        compact = date_iso.replace("-", "")

        limit = _parse_optional_int("limit", default=100, lo=1, hi=_MAX_LIMIT)
        offset = _parse_optional_int("offset", default=0, lo=0, hi=10_000)
        min_mag = _parse_optional_float("min_mag")
        max_mag = _parse_optional_float("max_mag")
        stations = _parse_repeatable("station", _STATION_RE) or None
        showers = [s.upper() for s in _parse_repeatable("shower", _SHOWER_RE)] or None
        order = request.args.get("order", "time")
        if order not in {"time", "time_desc", "mag"}:
            abort(400, description="order must be one of: time, time_desc, mag")

        public_stations_map = _public_stations(config)
        public_host_keys = set(public_stations_map.keys())
        public_cams: set[str] = {
            cam.code for st in public_stations_map.values() for cam in st.cameras
        }
        cam_to_host: dict[str, str] = {
            cam.code: hk
            for hk, st in public_stations_map.items()
            for cam in st.cameras
        }

        projected: list[dict[str, Any]] = []
        is_tonight = compact >= datetime.now(timezone.utc).strftime("%Y%m%d")
        use_index = not is_tonight and detection_db.covers_dates([compact])
        if use_index:
            try:
                # Restrict to public cams; further restrict if station filter given.
                cam_filter = public_cams
                if stations:
                    station_set = set(stations)
                    cam_filter = {
                        c for c, h in cam_to_host.items()
                        if h in station_set and c in public_cams
                    }
                rows = detection_db.query_detections(
                    dates=[compact],
                    cam_filter=cam_filter,
                    min_mag=min_mag,
                    shower=showers[0] if showers and len(showers) == 1 else None,
                )
                for row in rows:
                    p = _detection_from_index_row(row, date_iso, cam_to_host, public_stations_map)
                    if p is None:
                        continue
                    if not _filter_detection(
                        p, min_mag=None, max_mag=max_mag, stations=None,
                        showers=showers if showers and len(showers) > 1 else None,
                    ):
                        continue
                    projected.append(p)
            except Exception:
                logger.warning("detection_db query_detections failed for %s, falling back", compact)
                use_index = False

        if not use_index:
            payload = _safe_detections(compact)
            for det in payload.get("detections") or []:
                p = _detection_to_public(det, date_iso, public_host_keys)
                if p is None:
                    continue
                if not _filter_detection(
                    p, min_mag=min_mag, max_mag=max_mag,
                    stations=stations, showers=showers,
                ):
                    continue
                projected.append(p)

        if order == "mag":
            # Bright first; missing magnitude sinks to bottom. Plain `or 99`
            # would mis-sort a true magnitude-0.0 detection (bright fireball)
            # into the missing bucket — use explicit None test instead.
            def _mag_key(d: dict[str, Any]) -> tuple[bool, float]:
                pm = d.get("peak_magnitude")
                return (pm is None, 99.0 if pm is None else float(pm))
            projected.sort(key=_mag_key)
        elif order == "time_desc":
            projected.sort(key=lambda d: d.get("time_utc") or "", reverse=True)
        else:  # "time" — ascending
            projected.sort(key=lambda d: d.get("time_utc") or "")

        total = len(projected)
        page = projected[offset: offset + limit]

        return _public_json({
            "date": date_iso,
            "count": len(page),
            "total": total,
            "offset": offset,
            "limit": limit,
            "detections": page,
        }, max_age=_detections_max_age(date_iso))

    @app.route("/api/public/v1/detections/<path:detection_id>")
    @_require_api_key
    @_rate_limited(_DEFAULT_RATE)
    def public_detection_detail(detection_id: str):
        if len(detection_id) > _MAX_EVENT_ID_LEN:
            abort(404)
        date_iso, host_key, cam, filename = _parse_detection_id(detection_id)
        public_host_keys = set(_public_stations(config).keys())
        if host_key not in public_host_keys:
            abort(404)
        compact = date_iso.replace("-", "")
        payload = _safe_detections(compact)
        for det in payload.get("detections") or []:
            if (det.get("host_key") == host_key
                    and det.get("cam") == cam
                    and det.get("filename") == filename):
                p = _detection_to_public(det, date_iso, public_host_keys)
                if p is None:
                    abort(404)
                return _public_json(p, max_age=_detections_max_age(date_iso))
        abort(404)

    # ── Events (multi-station / trajectory) ───────────────────────────
    @app.route("/api/public/v1/events")
    @_require_api_key
    @_rate_limited(_HEAVY_RATE)
    def public_events():
        date_iso = request.args.get("date") or _last_night_iso_utc()
        _validate_iso_date(date_iso)
        compact = date_iso.replace("-", "")
        only_trajectory = request.args.get("has_trajectory") in ("1", "true", "yes")
        limit = _parse_optional_int("limit", default=200, lo=1, hi=_MAX_LIMIT)
        offset = _parse_optional_int("offset", default=0, lo=0, hi=10_000)
        order = request.args.get("order", "time")
        if order not in {"time", "time_desc", "mag", "duration"}:
            abort(400, description="order must be one of: time, time_desc, mag, duration")

        public_stations = _public_stations(config)
        public_host_keys = set(public_stations.keys())
        # Camera codes owned by any opted-in station (RO or future DE).
        # Used both for filtering local events and for the GMN-witness
        # check — replaces the previous hard-coded ``RO`` prefix so a
        # DE station that flips ``public: true`` later starts surfacing
        # automatically.
        cam_owner_codes = {
            c.code.upper()
            for st in public_stations.values()
            for c in st.cameras
        }
        cam_to_host: dict[str, str] = {
            cam.code: hk
            for hk, st in public_stations.items()
            for cam in st.cameras
        }
        public_cams: set[str] = set(cam_to_host.keys())

        # Project local detections first — needed to enrich GMN witnesses.
        # Load both date_iso and date_iso-1: GMN files early-morning-UTC meteors
        # under the calendar day (e.g. 2026-06-14 00:33 UTC), but RMS stores them
        # under the previous evening's night folder (20260613).
        prev_date_iso = (date.fromisoformat(date_iso) - timedelta(days=1)).isoformat()
        prev_compact = prev_date_iso.replace("-", "")

        # Fast path: use the index to build local_detections when both nights are
        # past and covered. clip_url will be null (index has FF filenames, not chunk
        # filenames) — GMN witness enrichment gets metadata but no video links.
        # detections_payload is still fetched via slow path when needed for local
        # (non-GMN) multi-station events (only_trajectory=False).
        is_tonight = compact >= datetime.now(timezone.utc).strftime("%Y%m%d")
        use_index = not is_tonight and detection_db.covers_dates([compact, prev_compact])
        detections_payload: dict[str, Any] = {}
        if use_index:
            try:
                index_rows = detection_db.query_detections(
                    dates=[compact, prev_compact],
                    cam_filter=public_cams,
                )
                local_detections = []
                for row in index_rows:
                    d_iso = date_iso if row.get("date") == compact else prev_date_iso
                    p = _detection_from_index_row(row, d_iso, cam_to_host, public_stations)
                    if p is not None:
                        local_detections.append(p)
            except Exception:
                logger.warning("detection_db query failed for events %s, falling back", compact)
                use_index = False

        if not use_index:
            detections_payload = _safe_detections(compact)
            prev_detections_payload = _safe_detections(prev_compact)
            local_detections = [
                d for d in (
                    _detection_to_public(raw, date_iso, public_host_keys)
                    for raw in detections_payload.get("detections") or []
                ) if d is not None
            ] + [
                d for d in (
                    _detection_to_public(raw, prev_date_iso, public_host_keys)
                    for raw in prev_detections_payload.get("detections") or []
                ) if d is not None
            ]

        # For local (non-GMN) multi-station events we need the full payload, which
        # includes the correlation result. Fetch via slow path if not already done.
        # Skip when use_index=True: detections_payload stays {} but the multi-station
        # loop below will just iterate over nothing — the index gives us local_detections
        # already and we don't want to pay the SSHFS scan cost on every /events call.
        if not only_trajectory and not use_index and not detections_payload:
            detections_payload = _safe_detections(compact)

        events: list[dict[str, Any]] = []

        # GMN events (have trajectory + orbit).
        gmn_payload = _safe_gmn(date_iso)
        for ev in gmn_payload.get("events") or []:
            # Surface only events where at least one witness is one of our
            # public cameras. (Was previously hard-coded to "RO" prefix —
            # the public-station set is the right boundary, not the RMS
            # country code.)
            stations_in_ev = [
                (s or "").upper() for s in (ev.get("stations") or [])
            ]
            if not any(s in cam_owner_codes for s in stations_in_ev):
                continue
            events.append(_gmn_event_to_public(
                ev, date_iso, public_stations, local_detections,
            ))

        if not only_trajectory:
            import bisect as _bisect

            _existing_timestamps: list[float] = []
            for _ev in events:
                _t = _ev.get("time_utc")
                if not _t:
                    continue
                try:
                    _existing_timestamps.append(
                        datetime.fromisoformat(_t.replace("Z", "+00:00")).timestamp()
                    )
                except (ValueError, AttributeError):
                    continue
            _existing_timestamps.sort()

            def _close_to_existing(t: str) -> bool:
                try:
                    t_ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError):
                    return False
                idx = _bisect.bisect_left(_existing_timestamps, t_ts - 3.0)
                while idx < len(_existing_timestamps) and _existing_timestamps[idx] <= t_ts + 3.0:
                    if abs(_existing_timestamps[idx] - t_ts) <= 3.0:
                        return True
                    idx += 1
                return False

            for ev in detections_payload.get("events") or []:
                # Only count events that involve at least one of OUR public
                # cameras (witness from a foreign / non-public station alone
                # wouldn't surface a playable clip).
                cams_in_event = {
                    (w.get("cam") or "").upper()
                    for w in ev.get("witnesses") or []
                }
                if not cams_in_event & cam_owner_codes:
                    continue
                t = ev.get("event_time")
                if t and _close_to_existing(t):
                    continue
                events.append(_event_to_public(ev, date_iso, public_host_keys))

        if order == "mag":
            def _ev_mag_key(e: dict[str, Any]) -> tuple[bool, float]:
                pm = e.get("peak_magnitude")
                return (pm is None, 99.0 if pm is None else float(pm))
            events.sort(key=_ev_mag_key)
        elif order == "duration":
            events.sort(key=lambda e: (e.get("duration_s") is None, -(e.get("duration_s") or 0.0)))
        elif order == "time_desc":
            events.sort(key=lambda e: e.get("time_utc") or "", reverse=True)
        else:
            events.sort(key=lambda e: e.get("time_utc") or "")
        total = len(events)
        page = events[offset: offset + limit]

        return _public_json({
            "date": date_iso,
            "count": len(page),
            "total": total,
            "offset": offset,
            "limit": limit,
            "events": page,
        }, max_age=_detections_max_age(date_iso))

    @app.route("/api/public/v1/events/<path:event_id>")
    @_require_api_key
    @_rate_limited(_DEFAULT_RATE)
    def public_event_detail(event_id: str):
        # Event IDs are either "gmn:<trajectory_id>" or "local:<iso_time>".
        if len(event_id) > _MAX_EVENT_ID_LEN:
            abort(404)
        if ":" not in event_id:
            abort(404)
        kind, _, rest = event_id.partition(":")
        if kind not in {"gmn", "local"}:
            abort(404)
        date_iso = request.args.get("date") or _last_night_iso_utc()
        _validate_iso_date(date_iso)
        compact = date_iso.replace("-", "")
        public_stations = _public_stations(config)
        public_host_keys = set(public_stations.keys())

        detections_payload = _safe_detections(compact)
        local_detections = [
            d for d in (
                _detection_to_public(raw, date_iso, public_host_keys)
                for raw in detections_payload.get("detections") or []
            ) if d is not None
        ]

        if kind == "gmn":
            gmn_payload = _safe_gmn(date_iso)
            for ev in gmn_payload.get("events") or []:
                if ev.get("id") == rest:
                    proj = _gmn_event_to_public(ev, date_iso, public_stations, local_detections)
                    return _public_json(proj, max_age=_detections_max_age(date_iso))
            abort(404)
        else:  # local
            for ev in detections_payload.get("events") or []:
                if ev.get("event_time") == rest:
                    proj = _event_to_public(ev, date_iso, public_host_keys)
                    return _public_json(proj, max_age=_detections_max_age(date_iso))
            abort(404)

    # ── Timelapses ────────────────────────────────────────────────────
    @app.route("/api/public/v1/timelapses")
    @_require_api_key
    @_rate_limited(_HEAVY_RATE)
    def public_timelapses():
        stations = _parse_repeatable("station", _STATION_RE) or None
        cameras = [c.upper() for c in _parse_repeatable("camera", _CAMERA_RE)] or None
        date_iso = request.args.get("date")
        if date_iso is not None:
            _validate_iso_date(date_iso)
        limit = _parse_optional_int("limit", default=200, lo=1, hi=_MAX_LIMIT)
        offset = _parse_optional_int("offset", default=0, lo=0, hi=10_000)
        compact = date_iso.replace("-", "") if date_iso else None

        public_stations = _public_stations(config)
        rows: list[dict[str, Any]] = []

        for host_key, st in public_stations.items():
            if stations and host_key not in stations:
                continue
            payload = _safe_timelapses(host_key)
            for cam_obj in st.cameras:
                cam = cam_obj.code
                if cameras and cam.upper() not in cameras:
                    continue
                entries = payload.get(cam, []) or []
                for e in entries:
                    e_date = e.get("date") or ""
                    if compact and e_date != compact:
                        continue
                    filename = e.get("filename")
                    if not filename:
                        continue
                    e_iso = _compact_to_iso(e_date)
                    night_stack = e.get("night_stack")
                    rows.append({
                        "station_id": host_key,
                        "station_label": st.label,
                        "camera": cam,
                        "date": e_iso,
                        "filename": filename,
                        "timelapse_url": _media_timelapse_url(cam, e_iso, filename),
                        "nightstack_url": (
                            _media_nightstack_url(cam, e_iso, night_stack)
                            if night_stack else None
                        ),
                    })

        rows.sort(key=lambda r: (r["date"], r["station_id"], r["camera"]), reverse=True)
        total = len(rows)
        page = rows[offset: offset + limit]
        return _public_json({
            "count": len(page),
            "total": total,
            "offset": offset,
            "limit": limit,
            "timelapses": page,
        }, max_age=300)

    # ── Night stacks (alias filter on timelapses) ─────────────────────
    @app.route("/api/public/v1/nightstacks")
    @_require_api_key
    @_rate_limited(_HEAVY_RATE)
    def public_nightstacks():
        stations = _parse_repeatable("station", _STATION_RE) or None
        cameras = [c.upper() for c in _parse_repeatable("camera", _CAMERA_RE)] or None
        date_iso = request.args.get("date")
        if date_iso is not None:
            _validate_iso_date(date_iso)
        limit = _parse_optional_int("limit", default=200, lo=1, hi=_MAX_LIMIT)
        offset = _parse_optional_int("offset", default=0, lo=0, hi=10_000)
        compact = date_iso.replace("-", "") if date_iso else None

        public_stations = _public_stations(config)
        rows: list[dict[str, Any]] = []
        for host_key, st in public_stations.items():
            if stations and host_key not in stations:
                continue
            payload = _safe_timelapses(host_key)
            for cam_obj in st.cameras:
                cam = cam_obj.code
                if cameras and cam.upper() not in cameras:
                    continue
                for e in payload.get(cam, []) or []:
                    night_stack = e.get("night_stack")
                    if not night_stack:
                        continue
                    e_date = e.get("date") or ""
                    if compact and e_date != compact:
                        continue
                    e_iso = _compact_to_iso(e_date)
                    rows.append({
                        "station_id": host_key,
                        "station_label": st.label,
                        "camera": cam,
                        "date": e_iso,
                        "filename": night_stack,
                        "nightstack_url": _media_nightstack_url(cam, e_iso, night_stack),
                    })

        rows.sort(key=lambda r: (r["date"], r["station_id"], r["camera"]), reverse=True)
        total = len(rows)
        page = rows[offset: offset + limit]
        return _public_json({
            "count": len(page),
            "total": total,
            "offset": offset,
            "limit": limit,
            "nightstacks": page,
        }, max_age=300)

    # ── Stats ─────────────────────────────────────────────────────────
    @app.route("/api/public/v1/stats")
    @_require_api_key
    @_rate_limited(_HEAVY_RATE)
    def public_stats():
        period = request.args.get("period", "tonight")
        if period not in {"tonight", "day", "month", "all"}:
            abort(400, description="period must be tonight|day|month|all")
        anchor_iso = request.args.get("date") or _last_night_iso_utc()
        _validate_iso_date(anchor_iso)

        public_host_keys = set(_public_stations(config).keys())
        online_count = sum(
            1 for k in public_host_keys
            if (station_cache_get_status(k) or {}).get("online")
        )

        dates: list[str]
        if period in {"tonight", "day"}:
            dates = [anchor_iso]
        elif period == "month":
            anchor_dt = datetime.strptime(anchor_iso, "%Y-%m-%d").date()
            first = anchor_dt.replace(day=1)
            dates = []
            cur = first
            while cur <= anchor_dt:
                dates.append(cur.isoformat())
                cur += timedelta(days=1)
        else:  # all
            # 'all' is bounded to the last 90 days — unbounded scans of the
            # detection cache would be expensive and consumers asking for
            # 'all' really mean 'recent history'. Document the cap.
            anchor_dt = datetime.strptime(anchor_iso, "%Y-%m-%d").date()
            dates = [(anchor_dt - timedelta(days=i)).isoformat() for i in range(90)]

        total_detections = 0
        per_station: dict[str, int] = {}
        per_shower: dict[str, int] = {}
        brightest: dict[str, Any] | None = None

        # Public cameras across all public stations
        public_cams: set[str] = {
            cam.code
            for st in _public_stations(config).values()
            for cam in st.cameras
        }
        compact_dates = [d.replace("-", "") for d in dates]
        cam_to_host = {
            cam.code: hk
            for hk, st in _public_stations(config).items()
            for cam in st.cameras
        }

        # Fast path only when the index actually covers the whole requested
        # range; otherwise a partially-populated DB would report false zeros.
        use_fast = period in {"month", "all"} and detection_db.covers_dates(compact_dates)
        if use_fast:
            # Fast path: single SQL aggregation over the index
            try:
                stats = detection_db.aggregate_stats(compact_dates, cam_filter=public_cams)
                total_detections = stats["total"]
                per_shower = dict(stats["per_shower"])
                # Per-station breakdown, folded from per-camera counts.
                per_station = {}
                for cam_code, cnt in stats.get("per_cam", {}).items():
                    hk = cam_to_host.get(cam_code)
                    if hk:
                        per_station[hk] = per_station.get(hk, 0) + cnt
                br = stats.get("brightest")
                if br:
                    pm = br.get("mag_apparent") if br.get("mag_apparent") is not None else br.get("mag_absolute")
                    if pm is not None:
                        hk = cam_to_host.get(br.get("cam") or "", "")
                        d_iso = br.get("date", "")
                        if len(d_iso) == 8:
                            d_iso = f"{d_iso[:4]}-{d_iso[4:6]}-{d_iso[6:]}"
                        brightest = {
                            "detection_id": _detection_id(
                                d_iso, hk, br.get("cam") or "", br.get("ff_file") or "",
                            ),
                            "station_id": hk,
                            "camera": br.get("cam"),
                            "time_utc": br.get("time_utc"),
                            "peak_magnitude": pm,
                            "shower": br.get("shower"),
                        }
            except Exception as exc:
                logger.warning("detection_db stats query failed, falling back: %s", exc)
                use_fast = False

        if not use_fast:
            # Slow path: fan out to per-date detections cache (tonight/day, or
            # DB not ready). Reset any partial fast-path state first.
            total_detections = 0
            per_station = {}
            per_shower = {}
            brightest = None
            for d_iso in dates:
                compact = d_iso.replace("-", "")
                payload = _safe_detections(compact)
                for det in payload.get("detections") or []:
                    if det.get("host_key") not in public_host_keys:
                        continue
                    total_detections += 1
                    hk = det.get("host_key")
                    per_station[hk] = per_station.get(hk, 0) + 1
                    rms = det.get("rms") or {}
                    sh = rms.get("shower")
                    if sh:
                        per_shower[sh] = per_shower.get(sh, 0) + 1
                    pm = rms.get("mag_apparent")
                    if pm is None:
                        pm = rms.get("mag_absolute")
                    if pm is not None and (brightest is None or pm < brightest["peak_magnitude"]):
                        brightest = {
                            "detection_id": _detection_id(
                                d_iso, hk, det.get("cam") or "", det.get("filename") or "",
                            ),
                            "station_id": hk,
                            "camera": det.get("cam"),
                            "time_utc": det.get("meteor_time"),
                            "peak_magnitude": pm,
                            "shower": sh,
                        }

        top_showers = sorted(
            ({"code": k, "count": v} for k, v in per_shower.items()),
            key=lambda r: r["count"], reverse=True,
        )[:10]

        coverage = coverage_mod.get_coverage_stats(platepar_store.get_all())

        return _public_json({
            "period": period,
            "anchor_date": anchor_iso,
            "dates": dates if period in {"tonight", "day"} else None,
            "online_stations": online_count,
            "total_stations": len(public_host_keys),
            "detection_count": total_detections,
            "per_station": per_station,
            "top_showers": top_showers,
            "brightest": brightest,
            "coverage_pct":          coverage["coverage_pct"],
            "dual_coverage_pct":     coverage["dual_coverage_pct"],
            "coverage_pct_40":       coverage["coverage_pct_40"],
            "dual_coverage_pct_40":  coverage["dual_coverage_pct_40"],
        }, max_age=_detections_max_age(anchor_iso))

    @app.route("/api/public/v1/fov")
    @_require_api_key
    @_rate_limited(_DEFAULT_RATE)
    def public_fov():
        """Camera FOV footprints as GeoJSON features for map overlays.

        Query parameters:
          alt   Target altitude in km: 75 (meteors, default) or 40 (fireballs/bolides).
        """
        raw_alt = request.args.get("alt", "75")
        try:
            alt_km = float(raw_alt)
        except ValueError:
            abort(400, description="alt must be a number (75 or 40)")
        # Snap to one of the two supported altitudes.
        H = coverage_mod.BOLIDE_ALT_KM if alt_km < 65 else coverage_mod.METEOR_ALT_KM

        public_stations = _public_stations(config)
        cam_to_station: dict[str, str] = {
            c.code.upper(): host_key
            for host_key, st in public_stations.items()
            for c in st.cameras
        }

        features = coverage_mod.camera_footprints_geojson(
            platepar_store.get_all(), cam_to_station, H
        )

        return _public_json({
            "type":     "FeatureCollection",
            "alt_km":   H,
            "features": features,
        }, max_age=3600)

    @app.route("/api/public/v1/orbits")
    @_require_api_key
    @_rate_limited(_HEAVY_RATE)
    def public_orbits():
        """GMN-confirmed heliocentric orbits for the given month.

        Returns events from the GMN monthly trajectory file where at least one
        witness is one of our public cameras. Each orbit includes the full set
        of heliocentric orbital elements published by GMN, plus trajectory
        endpoints (begin/end lat/lon/altitude), radiant, shower association,
        magnitude and duration.

        Query parameters:
          month   YYYY-MM (default: current UTC month)
          limit   1–500, default 500
          offset  default 0
          shower  IAU code filter, e.g. "PER" (repeatable)
        """
        raw_month = request.args.get("month")
        if raw_month:
            if not re.match(r"^\d{4}-\d{2}$", raw_month):
                abort(400, description="month must be YYYY-MM")
            try:
                month_dt = datetime.strptime(raw_month, "%Y-%m")
            except ValueError:
                abort(400, description="month must be a valid YYYY-MM")
        else:
            now = datetime.now(timezone.utc)
            month_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        year, month = month_dt.year, month_dt.month
        limit  = _parse_optional_int("limit",  default=500, lo=1, hi=_MAX_LIMIT)
        offset = _parse_optional_int("offset", default=0,   lo=0, hi=100_000)
        shower_filter = {s.upper() for s in _parse_repeatable("shower", _SHOWER_RE)}

        public_stations = _public_stations(config)
        # Camera codes for all our opted-in stations.
        our_cam_codes: frozenset[str] = frozenset(
            c.code.upper()
            for st in public_stations.values()
            for c in st.cameras
        )

        try:
            path = gmn_data.ensure_monthly_cached(year, month)
        except Exception as exc:
            return _public_json({
                "month": f"{year:04d}-{month:02d}",
                "count": 0,
                "total": 0,
                "offset": offset,
                "limit": limit,
                "orbits": [],
                "error": f"GMN data unavailable: {exc}",
            }, max_age=300)

        orbits: list[dict[str, Any]] = []
        for ev in gmn_data.parse_traj_summary(path):
            stations_upper = [s.upper() for s in (ev.get("stations") or [])]
            our_witnesses = [s for s in stations_upper if s in our_cam_codes]
            if not our_witnesses:
                continue
            if shower_filter:
                shower = (ev.get("shower") or "").upper()
                if shower not in shower_filter:
                    continue
            orbits.append({
                "id": ev.get("id"),
                "time_utc": ev.get("time"),
                "shower": ev.get("shower"),
                "peak_magnitude": ev.get("peak_mag"),
                "duration_s": ev.get("duration_s"),
                "velocity_km_s": ev.get("velocity"),
                "radiant": {
                    "ra_geo_deg": ev.get("ra_geo"),
                    "dec_geo_deg": ev.get("dec_geo"),
                    "solar_lon_deg": ev.get("sol_lon"),
                },
                "trajectory": {
                    "lat_begin": ev.get("lat_begin"),
                    "lon_begin": ev.get("lon_begin"),
                    "altitude_begin_km": ev.get("altitude_begin"),
                    "lat_end": ev.get("lat_end"),
                    "lon_end": ev.get("lon_end"),
                    "altitude_end_km": ev.get("altitude_end"),
                },
                "orbit": {
                    "a_au":       ev.get("orbit_a"),
                    "e":          ev.get("orbit_e"),
                    "i_deg":      ev.get("orbit_i"),
                    "peri_deg":   ev.get("orbit_peri"),
                    "node_deg":   ev.get("orbit_node"),
                    "q_au":       ev.get("orbit_q"),
                    "q_aph_au":   ev.get("orbit_q_aph"),
                    "tisserand":  ev.get("tisserand"),
                },
                "witness_stations": stations_upper,
                "our_witness_stations": our_witnesses,
            })

        total = len(orbits)
        page  = orbits[offset: offset + limit]

        # Monthly files update once a day at most; cache for 1 hour.
        return _public_json({
            "month":   f"{year:04d}-{month:02d}",
            "count":   len(page),
            "total":   total,
            "offset":  offset,
            "limit":   limit,
            "orbits":  page,
        }, max_age=3600)

    @app.route("/api/public/v1/orbit_stats")
    @_require_api_key
    @_rate_limited(_DEFAULT_RATE)
    def public_orbit_stats():
        """Aggregated orbit statistics for the current calendar year.

        Returns month_total, year_total, last_orbit_date, and last_day_total
        in a single call so consumers don't have to make one request per month.
        Counts only orbits witnessed by at least one of our public cameras.
        """
        now = datetime.now(timezone.utc)
        year, cur_month = now.year, now.month

        our_cam_codes: frozenset[str] = frozenset(
            c.code.upper()
            for st in _public_stations(config).values()
            for c in st.cameras
        )

        year_total = 0
        month_total = 0
        last_date_key = ""  # YYYYMMDD of most-recent orbit

        for m in range(1, cur_month + 1):
            try:
                gmn_data.ensure_monthly_cached(year, m)
            except Exception:
                pass
            daily = gmn_data.our_daily_counts_for_month(year, m, our_cam_codes)
            m_total = sum(daily.values())
            year_total += m_total
            if m == cur_month:
                month_total = m_total
            for dk in daily:
                if dk > last_date_key:
                    last_date_key = dk

        # If the current month has no orbits yet, look back one more month.
        if not last_date_key and cur_month > 1:
            try:
                gmn_data.ensure_monthly_cached(year, cur_month - 1)
            except Exception:
                pass
            prev_daily = gmn_data.our_daily_counts_for_month(
                year, cur_month - 1, our_cam_codes
            )
            if prev_daily:
                last_date_key = max(prev_daily)

        last_orbit_date: str | None = None
        last_day_total = 0
        if last_date_key:
            last_orbit_date = (
                f"{last_date_key[:4]}-{last_date_key[4:6]}-{last_date_key[6:8]}"
            )
            lm = int(last_date_key[4:6])
            day_counts = gmn_data.our_daily_counts_for_month(year, lm, our_cam_codes)
            last_day_total = day_counts.get(last_date_key, 0)

        return _public_json({
            "month":           f"{year:04d}-{cur_month:02d}",
            "year":            str(year),
            "month_total":     month_total,
            "year_total":      year_total,
            "last_orbit_date": last_orbit_date,
            "last_day_total":  last_day_total,
        }, max_age=3600)

    # ── Media: clip / stack / timelapse / nightstack ──────────────────
    def _serve_media(camera: str, date_iso: str, filename: str, subdir: str,
                      *, mime: str | None = None) -> Response:
        # Public-station gate: cameras owned by a station that did NOT opt
        # in to ``public: true`` are 404, not 403 — we don't reveal that
        # the URL exists. Same status code the path-resolve miss returns.
        if not _camera_is_public_media(camera):
            abort(404)
        path = _resolve_media_path(camera, date_iso, filename, subdir)
        if path is None:
            abort(404)
        kwargs: dict[str, Any] = {"conditional": True}
        if mime:
            kwargs["mimetype"] = mime
        resp = send_file(path, **kwargs)
        # Archive files are immutable once written; one day is safe and
        # plenty for a CDN. Add Accept-Ranges so <video> tags can seek.
        resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
        resp.headers.setdefault("Accept-Ranges", "bytes")
        return resp

    @app.route("/media/v1/clip/<camera>/<date>/<filename>")
    @_rate_limited(_MEDIA_RATE)
    def public_media_clip(camera: str, date: str, filename: str):
        # Color clips are H.264-in-Matroska (.mkv). iOS Safari/Chrome cannot
        # play MKV, so callers (e.g. the WordPress proxy) may pass ?format=mp4
        # to get an on-the-fly remux to seekable MP4 (copy, no re-encode).
        want_mp4 = request.args.get("format") == "mp4"
        if want_mp4 and filename.endswith(".mkv"):
            if not _camera_is_public_media(camera):
                abort(404)
            path = _resolve_media_path(camera, date, filename, "meteors")
            if path is None:
                abort(404)
            try:
                return _serve_as_mp4(path, filename)
            except _RemuxBusy:
                retry_after = 5
                resp = jsonify({
                    "error": "remux_busy",
                    "detail": "too many MP4 remuxes in progress; retry shortly",
                    "retry_after_seconds": retry_after,
                })
                resp.status_code = 503
                resp.headers["Retry-After"] = str(retry_after)
                return resp
        mime = "video/x-matroska" if filename.endswith(".mkv") else "video/mp4"
        return _serve_media(camera, date, filename, "meteors", mime=mime)

    @app.route("/media/v1/stack/<camera>/<date>/<filename>")
    @_rate_limited(_MEDIA_RATE)
    def public_media_stack(camera: str, date: str, filename: str):
        if not _camera_is_public_media(camera):
            abort(404)
        import mimetypes as _mt
        mime = _mt.guess_type(filename)[0] or "image/webp"
        for subdir in ("stacks", "meteors"):
            path = _resolve_media_path(camera, date, filename, subdir)
            if path is not None:
                resp = send_file(path, mimetype=mime, conditional=True)
                resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
                return resp
        abort(404)

    @app.route("/media/v1/timelapse/<camera>/<date>/<filename>")
    @_rate_limited(_MEDIA_RATE)
    def public_media_timelapse(camera: str, date: str, filename: str):
        return _serve_media(camera, date, filename, "timelapse", mime="video/mp4")

    @app.route("/media/v1/nightstack/<camera>/<date>/<filename>")
    @_rate_limited(_MEDIA_RATE)
    def public_media_nightstack(camera: str, date: str, filename: str):
        return _serve_media(camera, date, filename, "timelapse", mime="image/webp")
