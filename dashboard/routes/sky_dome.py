"""Sky dome rendering, timelapse, latest-frame proxies, and twilight.

Extracted from rovimen_dashboard.py.  All routes preserved verbatim -- same
URLs, same behaviour, same decorators.  Wired in from ``create_app()`` via
``register_sky_dome_routes`` (routes) and ``start_dome_scheduler`` (background
daemon thread).

Closure state (locks, TTL constants, scheduler dicts) is module-private.
Shared state (``platepar_cache``, ``_PLATEPAR_TTL``) is passed in from the
caller so that the platepar proxy route and this module share the same cache.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file

from cache_store import THUMB_CACHE_DIR, _thumb_cache_has_space

from auth import require_station, station_is_public_for_request
from http_caching import _json_cached
from route_helpers import lookup_station, media_url as _rh_media_url
from security import public_route
from station_client import station_url, station_get_raw, _session_for_url

logger = logging.getLogger(__name__)

# ── Dome cache TTLs ─────────────────────────────────────────────────────
# Night TTL is the *fallback* freshness window: if the background
# scheduler stalls (slow station, mid-warmup, transient timeout), the
# next /api/sky_dome GET re-renders synchronously after this many
# seconds. Kept just above the scheduler's 60 s cadence so a healthy
# station serves the pre-rendered file, but a stalled one self-heals
# within ~90 s instead of going stale for five minutes.
_SKY_DOME_NIGHT_TTL = 90    # 90 s — slightly above scheduler cadence
# Day source is "static" only in the sense that no fresh capture is
# happening — but a station that just came online at noon (or a config
# change pointing latest_stack at a new file) would otherwise wait an
# hour for the first colour stack. 30 min is a workable compromise
# between bandwidth and "stale-on-first-load" UX (P1-43).
_SKY_DOME_DAY_TTL   = 1800  # 30 min — source is static-ish

# ── Per-station render serialisation ────────────────────────────────────
_sky_dome_locks: dict[tuple, threading.Lock] = {}
_sky_dome_locks_lock = threading.Lock()

# ── Sky-dome pre-render scheduler constants ─────────────────────────────
_DOME_PRERENDER_INTERVAL_NIGHT_S = 60      # 1 min cadence when sun < 0° (sunset to sunrise)
_DOME_PRERENDER_INTERVAL_DAY_S = 1800      # 30 min cadence otherwise (P1-43)
_DOME_TIMELAPSE_RETENTION_DAYS = 92
_DOME_FAILURE_MUTE_THRESHOLD = 3           # consecutive failures before muting
# A brief network blip used to freeze a station's dome for 10 minutes,
# which is far longer than the user is willing to stare at a stale
# widget while clouds roll in. 2 min still avoids beating on a truly
# dead station every cycle, but recovers within one "feels live" window.
_DOME_FAILURE_MUTE_DURATION_S = 120        # 2 min mute
_DOME_FRAME_FPS = 12                       # output MP4 framerate
_DOME_COLOUR_NIGHT_INTERVAL_S = 600        # render colour every 10 min at night
_DOME_TIMELAPSE_TARGET_FRAMES = 200        # how many dome frames to render from camera timelapses
_DOME_TIMELAPSE_PX = 800                   # dome resolution for timelapse (lower than dashboard 1600)

# ── Scheduler runtime state ─────────────────────────────────────────────
_dome_next_render: dict[str, float] = {}              # host_key -> next render epoch
_dome_consecutive_failures: dict[str, int] = {}       # host_key -> count
_dome_failure_mute_until: dict[str, float] = {}       # host_key -> epoch
_dome_last_sun_alt: dict[str, float] = {}             # host_key -> last computed sun_alt for dawn detection


def register_sky_dome_routes(
    app: Flask,
    config,
    tunnels,
    cache,
    *,
    platepar_cache: dict[str, tuple[float, Any]],
    platepar_ttl: float,
) -> None:
    """Attach sky-dome, latest-frame/ff/chunk, and twilight routes.

    Parameters
    ----------
    platepar_cache, platepar_ttl:
        Shared platepar cache dict and TTL — also used by the ``/api/platepar``
        route that stays in the main module.
    """
    from astronomy import _compute_twilight, _compute_moon_phase, _compute_moon_rise_set
    from functools import partial

    _require_station = partial(lookup_station, config)

    def _media_url(host_key: str, *path_parts: str) -> str:
        return _rh_media_url(config, tunnels, host_key, *path_parts)

    # ── Dome cache path helpers ──────────────────────────────────────────

    def _sky_dome_cache_path(host_key: str, variant: str = "stack", overlay: bool = False) -> Path:
        # Colour stack keeps the legacy filename so existing cached PNGs aren't
        # orphaned; BW FF maxpixel gets its own sidecar.
        suffix = "_overlay" if overlay else ""
        filename = f"sky_dome{suffix}.png" if variant == "stack" else f"sky_dome_{variant}{suffix}.png"
        return THUMB_CACHE_DIR / host_key / filename

    def _sky_dome_meta_path(host_key: str, variant: str = "stack") -> Path:
        """Sidecar JSON next to the cached PNG carrying capture-time
        provenance for the freshness indicator on the dashboard card.

        Only written for the ``ff_max`` variant today — the colour stack
        path doesn't surface a per-camera capture timestamp from the
        station.
        """
        filename = "sky_dome.meta.json" if variant == "stack" else f"sky_dome_{variant}.meta.json"
        return THUMB_CACHE_DIR / host_key / filename

    def _sky_dome_lock_for(host_key: str, variant: str = "stack", overlay: bool = False) -> threading.Lock:
        # Key locks on (host, variant, overlay) so a slow colour render doesn't serialize
        # the parallel BW render (and vice-versa).
        key = (host_key, variant, overlay)
        with _sky_dome_locks_lock:
            lock = _sky_dome_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                _sky_dome_locks[key] = lock
            return lock

    def _sky_dome_ttl_for(host_key: str) -> int:
        """Return cache TTL in seconds — short at night (chunk stacks roll
        forward every ~5 min), long during the day (source is static).

        Uses civil twilight (sun at -6 deg) as the night boundary. Stations
        without lat/lon fall back to the night TTL, since the dome only
        ever updates as a function of new data anyway.
        """
        station = config.stations.get(host_key)
        if station is None or station.lat is None or station.lon is None:
            return _SKY_DOME_NIGHT_TTL
        now = datetime.now(timezone.utc)
        try:
            tw = _compute_twilight(
                float(station.lat), float(station.lon),
                now.strftime("%Y%m%d"), horizon=-6.0,
            )
            sunset_min = tw.get("sunset_min")
            sunrise_min = tw.get("sunrise_min")
        except Exception:
            return _SKY_DOME_NIGHT_TTL
        if sunset_min is None or sunrise_min is None:
            return _SKY_DOME_NIGHT_TTL
        now_min = now.hour * 60 + now.minute
        # Night window crosses UTC midnight at most longitudes — handle
        # both orderings rather than assuming sunset > sunrise.
        if sunset_min > sunrise_min:
            is_night = now_min >= sunset_min or now_min < sunrise_min
        else:
            is_night = not (sunset_min <= now_min < sunrise_min)
        return _SKY_DOME_NIGHT_TTL if is_night else _SKY_DOME_DAY_TTL

    # ── Platepar fetcher (reuses shared cache) ───────────────────────────

    def _fetch_platepar_for_dome(host_key: str) -> dict[str, Any] | None:
        """Reuse the same cache+SWR pattern as /api/platepar/<host>."""
        cached = platepar_cache.get(host_key)
        now_mono = time.monotonic()
        if cached and now_mono < cached[0]:
            return cached[1]
        try:
            payload = station_get_raw(config, tunnels, host_key, "/api/platepar")
            platepar_cache[host_key] = (now_mono + platepar_ttl, payload)
            return payload
        except Exception:
            return cached[1] if cached else None

    # ── Stack image fallback fetcher ─────────────────────────────────────

    def _fetch_stack_bytes_via_rmsplots(
        host_key: str, cam_code: str, variant: str = "stack",
    ) -> bytes | None:
        """Walk /api/nights/<cam> + /api/rms/plots + /api/rms/plot_image to
        grab the most-recent captured_stack.jpg without relying on the new
        /api/latest_stack endpoint. Used as a fallback so the dome still
        renders on stations that haven't pulled the post-#145 bundle (every
        station today, because default update_channel is ``main`` not ``dev``).
        Returns None if no captured stack exists in the last few nights or
        any request fails.

        ``variant``:
          - ``stack`` (default): look for ``*_captured_stack.jpg``.
          - ``ff_max``: look for the freshest ``*_maxpixel.png`` /
            ``*_maxpixel.jpg`` (one per FF, ~10 s coverage each).
        """
        try:
            nights = station_get_raw(
                config, tunnels, host_key, f"/api/nights/{cam_code}", timeout=10,
            )
        except Exception:
            return None
        if not nights:
            return None

        def _match(p: dict) -> bool:
            fn = p.get("filename", "") if isinstance(p, dict) else ""
            if variant == "ff_max":
                return fn.endswith("_maxpixel.png") or fn.endswith("_maxpixel.jpg")
            return fn.endswith("_captured_stack.jpg")

        # Newest night first; check up to 5 most-recent nights — older
        # captures still have a captured_stack.jpg from RMS' dawn pass.
        for date in sorted(nights, reverse=True)[:5]:
            try:
                plots_url = _media_url(host_key, "api", "rms", "plots", cam_code, date)
                plots_resp = _session_for_url(plots_url).get(plots_url, timeout=10)
                if not plots_resp.ok:
                    continue
                plots = plots_resp.json()
                if variant == "ff_max":
                    # Pick the newest matching maxpixel by filename — FF names
                    # embed a UTC timestamp so lexical max is the freshest.
                    matches = [p for p in plots if _match(p)]
                    if not matches:
                        continue
                    target = max(matches, key=lambda p: p.get("filename", ""))
                else:
                    target = next((p for p in plots if _match(p)), None)
                    if target is None:
                        continue
                img_url = _media_url(
                    host_key, "api", "rms", "plot_image", cam_code, date, target["filename"],
                )
                img_resp = _session_for_url(img_url).get(img_url, timeout=15)
                if img_resp.ok and img_resp.content:
                    return img_resp.content
            except Exception:
                continue
        return None

    # ── Dome PNG renderer ────────────────────────────────────────────────

    def _render_sky_dome_png(host_key: str, variant: str = "stack", overlay: bool = False) -> bytes | None:
        """Build the celestial-dome PNG for ``host_key``. Returns None if no
        camera in the station has both a valid platepar AND a reachable stack
        image — the route uses that to fall back to a stale cached PNG.

        ``variant``:
          - ``stack`` (default): colour ``_stack.webp`` -> RMS captured_stack ->
            walk /api/nights for ``_captured_stack.jpg``. Time scale: ~5 min.
          - ``ff_max``: per-camera newest BW FF maxpixel via the
            ``/api/latest_ff_maxpixel/<cam>`` station endpoint, with a walk
            fallback to the freshest ``_maxpixel.png`` / ``_maxpixel.jpg``
            served by ``/api/rms/plot_image``. Time scale: ~10 s.
        """
        import io as _io
        import numpy as _np
        from PIL import Image as _PILImage
        from celestial_dome import Plate, render_station_dome

        platepar_payload = _fetch_platepar_for_dome(host_key)
        if not platepar_payload:
            return None

        station = config.stations.get(host_key)
        if station is None:
            return None
        label = station.label or host_key
        cams = [c.code for c in station.cameras]

        plates_with_images: list[tuple[Plate, _np.ndarray]] = []
        # Track newest FF capture time across cameras for the freshness label.
        # Stations on pre-bundle-261 don't emit X-FF-Timestamp; cameras_used
        # stays empty in that case and the sidecar JSON is not written.
        cameras_used: list[dict[str, str]] = []
        for cam_code in cams:
            entry = platepar_payload.get(cam_code)
            if not entry or entry.get("error"):
                continue
            try:
                plate = Plate.from_dict(entry, cam_code)
            except (KeyError, TypeError, ValueError):
                continue
            img_bytes: bytes | None = None
            if variant == "ff_max":
                # Single newest FF maxpixel — ~10 s of sky, BW. The walk
                # fallback below picks up the same content on stations that
                # haven't pulled the post-#16x bundle.
                try:
                    url = _media_url(host_key, "api", "latest_ff_maxpixel", cam_code)
                    resp = _session_for_url(url).get(url, timeout=15)
                    if resp.status_code == 200 and resp.content:
                        img_bytes = resp.content
                        ts = resp.headers.get("X-FF-Timestamp")
                        if ts:
                            cameras_used.append({"code": cam_code, "capture_time": ts})
                except Exception:
                    pass
            else:
                # Three-tier source fallback so the dome renders on any
                # station regardless of bundle vintage:
                #   1) /api/latest_frame  — post-#150 rolling chunk stack
                #   2) /api/latest_stack  — post-#145 most-recent captured_stack
                #   3) walk /api/nights + /api/rms/plot_image — pre-#145, every
                #      station has these endpoints today.
                for path_parts in (("api", "latest_frame", cam_code),
                                   ("api", "latest_stack", cam_code)):
                    try:
                        url = _media_url(host_key, *path_parts)
                        resp = _session_for_url(url).get(url, timeout=15)
                        if resp.status_code == 200 and resp.content:
                            img_bytes = resp.content
                            break
                    except Exception:
                        continue
            if img_bytes is None:
                img_bytes = _fetch_stack_bytes_via_rmsplots(
                    host_key, cam_code, variant=variant,
                )
            if img_bytes is None:
                continue
            try:
                pil_img = _PILImage.open(_io.BytesIO(img_bytes)).convert("RGB")
                # Stack images from the station stacker are pre-rotated 180
                # when the camera config says rotate=true. The dome projection
                # uses platepar coordinates that map native sensor pixels, so
                # undo the rotation to restore native orientation.
                cam_cfg = next(
                    (c for c in station.cameras if c.code == cam_code), None
                )
                if cam_cfg and cam_cfg.rotate and variant != "ff_max":
                    pil_img = pil_img.rotate(180)
                img = _np.asarray(pil_img)
            except Exception:
                continue
            plates_with_images.append((plate, img))

        if not plates_with_images:
            return None

        dome = render_station_dome(
            plates_with_images,
            station=host_key,
            label=label,
            lat=station.lat if station else None,
            lon=station.lon if station else None,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            overlay=overlay,
        )
        buf = _io.BytesIO()
        dome.save(buf, format="PNG", optimize=True)

        # Freshness sidecar — written next to the PNG so the dashboard's
        # "Image vs Now" label has accurate provenance. Only meaningful for
        # ff_max (the colour stack route doesn't surface per-FF timestamps).
        if variant == "ff_max" and cameras_used:
            try:
                meta = {
                    "variant": variant,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "capture_time": max(c["capture_time"] for c in cameras_used),
                    "cameras_used": cameras_used,
                }
                meta_path = _sky_dome_meta_path(host_key, variant)
                meta_path.parent.mkdir(parents=True, exist_ok=True)
                meta_path.write_text(json.dumps(meta))
            except Exception:
                logger.warning(
                    "sky_dome meta write failed for %s/%s",
                    host_key, variant, exc_info=True,
                )

        return buf.getvalue()

    # Cache headers for every dome response — the scheduler keeps the disk
    # file hot, so clients should always re-validate rather than serve a
    # stale browser-cached PNG.
    _SKY_DOME_NO_STORE_HEADERS = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    def _sky_dome_send_file(cache_file: Path) -> Response:
        resp = send_file(cache_file, mimetype="image/png", conditional=True)
        for k, v in _SKY_DOME_NO_STORE_HEADERS.items():
            resp.headers[k] = v
        return resp

    # ── /api/sky_dome/<host_key>.png ─────────────────────────────────────

    @app.route("/api/sky_dome/<host_key>.png")
    @require_station
    def api_sky_dome(host_key: str):
        """Per-station celestial hemisphere — each camera's freshest stack
        reprojected onto a single 1600px disc, with FOV outlines and
        cardinal labels.

        Query param ``variant``:
          - ``stack`` (default): colour chunk stack / RMS captured_stack —
            the original behaviour, kept for backward compat and scheduler
            reuse.
          - ``ff_max``: BW FF maxpixel (single newest FF per camera, ~10 s
            of sky). Drives the live "Sky right now" widget on the Video DB
            tab.

        Query param ``overlay``:
          - ``false`` (default): clean mosaic without cardinal labels,
            FOV outlines, camera legend, or metadata text.
          - ``true`` / ``1`` / ``yes``: full annotated dome with overlays.

        TTL is dynamic: ~5 min at night (so the panorama rolls forward
        with each new colour chunk stack) and 1 h during the day (the
        source image doesn't change). Falls back to the last-known PNG
        when the station is offline.
        """
        _require_station(host_key)
        variant = request.args.get("variant", "stack")
        if variant not in ("stack", "ff_max"):
            variant = "stack"
        overlay = request.args.get("overlay", "false").lower() in ("true", "1", "yes")
        cache_file = _sky_dome_cache_path(host_key, variant, overlay=overlay)
        ttl = _sky_dome_ttl_for(host_key)
        now = time.time()

        if cache_file.exists() and now - cache_file.stat().st_mtime < ttl:
            return _sky_dome_send_file(cache_file)

        lock = _sky_dome_lock_for(host_key, variant, overlay=overlay)
        with lock:
            # Recheck after acquiring the lock — another worker may have
            # rendered while we were waiting.
            if cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl:
                return _sky_dome_send_file(cache_file)

            try:
                png_bytes = _render_sky_dome_png(host_key, variant, overlay=overlay)
            except Exception:
                logger.exception(
                    "sky_dome render failed for %s variant=%s overlay=%s",
                    host_key, variant, overlay,
                )
                png_bytes = None

            if png_bytes is not None and _thumb_cache_has_space():
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_bytes(png_bytes)
                except Exception:
                    logger.warning(
                        "sky_dome cache write failed for %s variant=%s",
                        host_key, variant, exc_info=True,
                    )

        if png_bytes is not None:
            resp = Response(png_bytes, status=200, mimetype="image/png")
            for k, v in _SKY_DOME_NO_STORE_HEADERS.items():
                resp.headers[k] = v
            return resp
        # Render failed — serve stale cached PNG if we have one.
        if cache_file.exists():
            return _sky_dome_send_file(cache_file)
        return jsonify({"error": "no platepar/stack available"}), 503

    # ── /api/sky_dome_meta/<host_key> ────────────────────────────────────

    @app.route("/api/sky_dome_meta/<host_key>")
    @require_station
    def api_sky_dome_meta(host_key: str):
        """Freshness sidecar for the dome card on the dashboard. Reports the
        newest FF capture time across cameras the last render used, plus
        when the render finished. The frontend uses this to colour-code an
        ``Image: HH:MM:SS UTC`` label next to a live ``Now`` clock.

        Returns 200 with ``{capture_time: null}`` (rather than 404) when no
        sidecar exists yet — keeps the frontend code simple.
        """
        _require_station(host_key)
        variant = request.args.get("variant", "ff_max")
        if variant not in ("stack", "ff_max"):
            variant = "ff_max"
        meta_file = _sky_dome_meta_path(host_key, variant)
        if not meta_file.exists():
            return jsonify({
                "capture_time": None,
                "generated_at": None,
                "cameras_used": [],
                "variant": variant,
            })
        try:
            data = json.loads(meta_file.read_text())
        except Exception:
            return jsonify({"capture_time": None, "generated_at": None,
                            "cameras_used": [], "variant": variant})
        resp = jsonify(data)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

    # ── Sky-dome pre-render scheduler + nightly timelapse ────────────────
    # The scheduler thread keeps both dome variants warm on a twilight-aware
    # cadence, archives the BW (``ff_max``) frames through the night, and
    # stitches them into a 12 fps MP4 at civil dawn. Failures back off via a
    # consecutive-failure -> 10-min mute. See the constants block near
    # ``_SKY_DOME_DAY_TTL`` for the knobs.

    def _sky_dome_archive_dir(host_key: str, date_yyyymmdd: str) -> Path:
        return THUMB_CACHE_DIR / host_key / "sky_dome_archive" / date_yyyymmdd

    def _sky_dome_timelapse_dir(host_key: str) -> Path:
        return THUMB_CACHE_DIR / host_key / "sky_dome_timelapse"

    def _sky_dome_timelapse_path(host_key: str, date_yyyymmdd: str) -> Path:
        return _sky_dome_timelapse_dir(host_key) / f"{date_yyyymmdd}.mp4"

    def _station_sun_alt(host_key: str) -> float | None:
        """Sun altitude in degrees right now for the station's lat/lon.

        Returns None when the station has no coords. Same NOAA solar
        algorithm as ``_compute_twilight`` but evaluated at "now" rather
        than producing a sunset/sunrise minute pair.
        """
        station = config.stations.get(host_key)
        if station is None or station.lat is None or station.lon is None:
            return None
        try:
            lat = float(station.lat)
            lon = float(station.lon)
        except (TypeError, ValueError):
            return None
        now = datetime.now(timezone.utc)
        try:
            year, month, day = now.year, now.month, now.day
            if month <= 2:
                year -= 1
                month += 12
            A = int(year / 100)
            B = 2 - A + int(A / 4)
            day_frac = (now.hour + now.minute / 60.0 + now.second / 3600.0) / 24.0
            jd = (int(365.25 * (year + 4716)) + int(30.6001 * (month + 1))
                  + day + day_frac + B - 1524.5)
            jc = (jd - 2451545.0) / 36525.0
            L0 = (280.46646 + jc * (36000.76983 + 0.0003032 * jc)) % 360
            M = (357.52911 + jc * (35999.05029 - 0.0001537 * jc)) % 360
            Mr = math.radians(M)
            C = (math.sin(Mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
                 + math.sin(2 * Mr) * (0.019993 - 0.000101 * jc)
                 + math.sin(3 * Mr) * 0.000289)
            sun_lon = L0 + C
            omega = 125.04 - 1934.136 * jc
            sun_app = sun_lon - 0.00569 - 0.00478 * math.sin(math.radians(omega))
            obliq0 = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
            obliq = obliq0 + 0.00256 * math.cos(math.radians(omega))
            sin_dec = math.sin(math.radians(obliq)) * math.sin(math.radians(sun_app))
            dec = math.asin(sin_dec)
            gmst = (280.46061837
                    + 360.98564736629 * (jd - 2451545.0)
                    + jc * jc * (0.000387933 - jc / 38710000.0)) % 360
            ra = math.degrees(math.atan2(
                math.cos(math.radians(obliq)) * math.sin(math.radians(sun_app)),
                math.cos(math.radians(sun_app)),
            )) % 360
            ha = math.radians((gmst + lon - ra + 540) % 360 - 180)
            lat_r = math.radians(lat)
            sin_alt = math.sin(lat_r) * math.sin(dec) + math.cos(lat_r) * math.cos(dec) * math.cos(ha)
            sin_alt = max(-1.0, min(1.0, sin_alt))
            return math.degrees(math.asin(sin_alt))
        except Exception:
            return None

    def _dome_capture_night(now: datetime | None = None) -> str:
        """Return the capture-night date string (YYYYMMDD) for ``now``.

        A capture night is identified by its evening UTC date — frames
        captured after UTC midnight still belong to the prior evening's
        night so they end up in the same archive directory and the same
        timelapse. Before noon UTC we attribute to the previous day; after
        noon to today.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if now.hour < 12:
            return (now - timedelta(days=1)).strftime("%Y%m%d")
        return now.strftime("%Y%m%d")

    def _dome_frame_sort_key(now: datetime) -> str:
        """Return a 4-digit filename prefix that sorts chronologically
        within a capture night (noon-to-noon UTC).

        Plain HHMM naming puts post-midnight frames (0000-0359) before
        evening frames (1900-2359) when sorted lexicographically. Using
        minutes-since-noon-UTC gives a monotonic sequence:
          12:00 UTC -> 0000, 19:00 -> 0420, 00:00 -> 0720, 04:00 -> 0960
        """
        offset = ((now.hour + 12) % 24) * 60 + now.minute
        return f"{offset:04d}"

    def _archive_dome_frame_for_night(host_key: str, png_bytes: bytes) -> None:
        """Write a BW dome frame into the per-night archive directory.

        Filename is ``HHMM.png`` in UTC — sorted lexicographically the
        frames stay in chronological order across midnight because the
        capture-night attribution rolls the date forward at noon UTC.
        Atomic write (``.tmp`` -> ``rename``).
        """
        if not _thumb_cache_has_space():
            return
        now = datetime.now(timezone.utc)
        night = _dome_capture_night(now)
        archive = _sky_dome_archive_dir(host_key, night)
        try:
            archive.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(
                "dome archive mkdir failed for %s/%s", host_key, night, exc_info=True,
            )
            return
        fname = f"{_dome_frame_sort_key(now)}.png"
        target = archive / fname
        tmp = archive / f".{fname}.tmp"
        try:
            tmp.write_bytes(png_bytes)
            tmp.replace(target)
        except OSError:
            logger.warning(
                "dome archive write failed for %s/%s", host_key, fname, exc_info=True,
            )
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _render_and_archive_sky_dome(host_key: str, variant: str) -> bool:
        """Render ``variant`` and atomically swap the cached PNG. When the
        BW variant succeeds, also drop a copy in the per-night archive
        directory for tomorrow's timelapse. Returns True on success.
        """
        try:
            png_bytes = _render_sky_dome_png(host_key, variant=variant)
        except TypeError:
            # Pre-merge: ``_render_sky_dome_png`` doesn't yet accept
            # ``variant``. Fall back to the unparameterised call so the
            # scheduler keeps at least the colour cache hot until the
            # parallel branch lands.
            try:
                png_bytes = _render_sky_dome_png(host_key)
            except Exception:
                logger.exception(
                    "dome render crashed for %s variant=%s", host_key, variant,
                )
                return False
        except Exception:
            logger.exception(
                "dome render crashed for %s variant=%s", host_key, variant,
            )
            return False
        if not png_bytes:
            return False
        cache_file = _sky_dome_cache_path(host_key, variant)
        if not _thumb_cache_has_space():
            return False
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(cache_file.suffix + ".tmp")
            tmp.write_bytes(png_bytes)
            tmp.replace(cache_file)
        except OSError:
            logger.warning(
                "dome cache write failed for %s variant=%s",
                host_key, variant, exc_info=True,
            )
            return False
        if variant == "ff_max":
            _archive_dome_frame_for_night(host_key, png_bytes)
        return True

    def _build_sky_dome_timelapse(host_key: str, date_yyyymmdd: str) -> bool:
        """Stitch archived BW frames for one night into an MP4.

        On success the archive directory is removed (frames have been
        consumed). Skips when fewer than 10 frames are present (the
        night is too short to bother). Returns True on a successful
        build.
        """
        archive = _sky_dome_archive_dir(host_key, date_yyyymmdd)
        if not archive.exists():
            return False
        pngs = sorted(archive.glob("*.png"))
        if len(pngs) < 10:
            logger.info(
                "dome timelapse skip %s/%s: only %d frames",
                host_key, date_yyyymmdd, len(pngs),
            )
            return False
        out = _sky_dome_timelapse_path(host_key, date_yyyymmdd)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(
                "dome timelapse mkdir failed for %s/%s",
                host_key, date_yyyymmdd, exc_info=True,
            )
            return False
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(_DOME_FRAME_FPS),
            "-pattern_type", "glob",
            "-i", str(archive / "*.png"),
            "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
        except FileNotFoundError:
            logger.warning("ffmpeg not on PATH — cannot build dome timelapse")
            return False
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg timeout for %s/%s", host_key, date_yyyymmdd)
            return False
        if result.returncode != 0:
            tail = result.stderr.decode(errors="replace")[-500:] if result.stderr else ""
            logger.warning(
                "ffmpeg failed for %s/%s: %s",
                host_key, date_yyyymmdd, tail,
            )
            return False
        shutil.rmtree(archive, ignore_errors=True)
        logger.info(
            "dome timelapse built %s/%s (%d frames)",
            host_key, date_yyyymmdd, len(pngs),
        )
        return True

    def _build_sky_dome_timelapse_from_mp4s(host_key: str, date_yyyymmdd: str) -> bool:
        """Build sky dome timelapse by projecting per-camera color timelapse MP4s.

        Downloads each camera's timelapse MP4 for ``date_yyyymmdd``, extracts
        evenly-spaced frames, projects them through the platepar onto a celestial
        dome, then stitches the projected frames into an MP4.
        """
        import io as _io
        import os as _os
        import tempfile
        import numpy as _np
        from PIL import Image as _PILImage
        from celestial_dome import (
            Plate, normalize_camera_image as _normalize_cam,
            render_timelapse_batch, render_station_dome_timelapse,
        )

        # Logo path: VPS deploys assets/ one level above routes/, then repo fallback.
        _LOGO_CANDIDATES = [
            Path(__file__).parents[1] / "assets" / "gmn_sphere.png",
            Path(__file__).parents[2] / "rovimen-scripts" / "assets" / "gmn_sphere.png",
        ]
        _logo_path = next((p for p in _LOGO_CANDIDATES if p.exists()), None)

        platepar_payload = _fetch_platepar_for_dome(host_key)
        if not platepar_payload:
            logger.warning("dome timelapse build: no platepar for %s", host_key)
            return False

        station = config.stations.get(host_key)
        if not station:
            return False

        try:
            tl_data = station_get_raw(config, tunnels, host_key, "/api/timelapses", timeout=15)
        except Exception:
            logger.warning(
                "dome timelapse build: failed to fetch timelapse list for %s",
                host_key, exc_info=True,
            )
            return False

        with tempfile.TemporaryDirectory() as _tmpdir:
            tmpdir = Path(_tmpdir)

            # Download each camera's timelapse MP4 for this night
            cam_mp4: dict[str, Path] = {}
            for cam_cfg in station.cameras:
                cam = cam_cfg.code
                entries = tl_data.get(cam, [])
                entry = next((e for e in entries if e.get("date") == date_yyyymmdd), None)
                if not entry:
                    continue
                filename = entry.get("filename")
                if not filename:
                    continue
                try:
                    url = _media_url(host_key, "color_timelapse", cam, date_yyyymmdd, filename)
                    resp = _session_for_url(url).get(url, timeout=120, stream=True)
                    if not resp.ok:
                        logger.warning(
                            "dome timelapse build: %s/%s returned %s",
                            host_key, cam, resp.status_code,
                        )
                        continue
                    mp4_path = tmpdir / f"{cam}.mp4"
                    with mp4_path.open("wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 20):
                            fh.write(chunk)
                    cam_mp4[cam] = mp4_path
                except Exception:
                    logger.warning(
                        "dome timelapse build: download failed for %s/%s",
                        host_key, cam, exc_info=True,
                    )
                    continue

            if not cam_mp4:
                logger.warning(
                    "dome timelapse build: no timelapse MP4s found for %s/%s",
                    host_key, date_yyyymmdd,
                )
                return False

            # Extract evenly-spaced frames from each MP4
            cam_frames: dict[str, list[Path]] = {}
            for cam, mp4_path in cam_mp4.items():
                try:
                    probe = subprocess.run(
                        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                         "-count_packets", "-show_entries", "stream=nb_read_packets",
                         "-of", "csv=p=0", str(mp4_path)],
                        capture_output=True, text=True, timeout=30, check=False,
                    )
                    total = int(probe.stdout.strip())
                except Exception:
                    total = 0
                if total < 10:
                    logger.warning(
                        "dome timelapse build: too few frames (%d) in %s/%s",
                        total, host_key, cam,
                    )
                    continue
                step = max(1, total // _DOME_TIMELAPSE_TARGET_FRAMES)
                frames_dir = tmpdir / f"frames_{cam}"
                frames_dir.mkdir()
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", str(mp4_path),
                     "-vf", f"select=not(mod(n\\,{step}))",
                     "-vsync", "0",
                     str(frames_dir / "%04d.png")],
                    capture_output=True, timeout=180, check=False,
                )
                if result.returncode != 0:
                    logger.warning(
                        "dome timelapse build: frame extract failed for %s/%s",
                        host_key, cam,
                    )
                    continue
                frames = sorted(frames_dir.glob("*.png"))
                if frames:
                    cam_frames[cam] = frames

            if not cam_frames:
                return False

            n = min(len(v) for v in cam_frames.values())
            if n < 10:
                logger.warning(
                    "dome timelapse build: too few extracted frames (%d) for %s/%s",
                    n, host_key, date_yyyymmdd,
                )
                return False

            # Compute per-frame UTC timestamps by interpolating between sunset
            # and sunrise for this night using civil twilight (-6 deg).
            _sunset_min: float | None = None
            _night_dur_min: float | None = None
            _night_date_str = f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:]}"
            if station.lat is not None and station.lon is not None:
                try:
                    from astronomy import _compute_twilight
                    tw = _compute_twilight(
                        float(station.lat), float(station.lon),
                        date_yyyymmdd, horizon=-6.0,
                    )
                    sm = tw.get("sunset_min")
                    rm = tw.get("sunrise_min")
                    if sm is not None and rm is not None:
                        _sunset_min = float(sm)
                        # Night spans sunset → midnight → sunrise next day
                        _night_dur_min = float((1440 - sm) + rm)
                except Exception:
                    pass

            def _frame_timestamp(i: int, total: int) -> str:
                if _sunset_min is not None and _night_dur_min is not None and total > 1:
                    offset = (i / (total - 1)) * _night_dur_min
                    abs_min = (_sunset_min + offset) % 1440
                    h = int(abs_min // 60)
                    m = int(abs_min % 60)
                    return f"{_night_date_str}  {h:02d}:{m:02d} UTC"
                return _night_date_str

            # Build plate list and pre-normalize all frames (rotation + crop done once,
            # not per frame).  Frames that fail to load are stored as None so the
            # index alignment across cameras is preserved for render_timelapse_batch.
            plates_ordered: list = []
            normalized_per_cam: list[list] = []
            for cam_cfg in station.cameras:
                cam = cam_cfg.code
                frames = cam_frames.get(cam)
                if not frames:
                    continue
                entry = platepar_payload.get(cam)
                if not entry or entry.get("error"):
                    continue
                try:
                    plate = Plate.from_dict(entry, cam, rotate=bool(cam_cfg.rotate))
                except (KeyError, TypeError, ValueError):
                    continue
                y_res = int(entry.get("Y_res", 0))
                cam_normed: list = []
                for frame_path in frames[:n]:
                    try:
                        pil_img = _PILImage.open(frame_path).convert("RGB")
                        arr = _np.asarray(pil_img)
                        if y_res:
                            arr = arr[:y_res, :, :].copy()
                        cam_normed.append(_normalize_cam(arr, plate))
                    except Exception:
                        cam_normed.append(None)
                plates_ordered.append(plate)
                normalized_per_cam.append(cam_normed)

            if not plates_ordered:
                logger.warning(
                    "dome timelapse build: no valid cameras for %s/%s",
                    host_key, date_yyyymmdd,
                )
                return False

            timestamps = [_frame_timestamp(i, n) for i in range(n)]
            n_workers = min(_os.cpu_count() or 4, 8)
            try:
                dome_images = render_timelapse_batch(
                    plates_ordered,
                    normalized_per_cam,
                    n,
                    timestamps=timestamps,
                    logo_path=_logo_path,
                    dome_px=_DOME_TIMELAPSE_PX,
                    n_workers=n_workers,
                )
            except Exception:
                logger.warning(
                    "dome timelapse build: batch render failed for %s/%s, falling back",
                    host_key, date_yyyymmdd, exc_info=True,
                )
                dome_images = []

            dome_frames_dir = tmpdir / "dome_frames"
            dome_frames_dir.mkdir()
            rendered = 0
            for i, dome in enumerate(dome_images):
                if dome is None:
                    continue
                try:
                    buf = _io.BytesIO()
                    dome.save(buf, format="PNG")
                    (dome_frames_dir / f"{i:04d}.png").write_bytes(buf.getvalue())
                    rendered += 1
                except Exception:
                    logger.warning(
                        "dome timelapse build: frame save failed at %d for %s",
                        i, host_key,
                    )

            if rendered < 10:
                logger.warning(
                    "dome timelapse build: only %d dome frames rendered for %s/%s",
                    rendered, host_key, date_yyyymmdd,
                )
                return False

            out = _sky_dome_timelapse_path(host_key, date_yyyymmdd)
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning(
                    "dome timelapse build: mkdir failed for %s/%s",
                    host_key, date_yyyymmdd, exc_info=True,
                )
                return False

            cmd = [
                "ffmpeg", "-y",
                "-framerate", str(_DOME_FRAME_FPS),
                "-pattern_type", "glob",
                "-i", str(dome_frames_dir / "*.png"),
                "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(out),
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
            except FileNotFoundError:
                logger.warning("dome timelapse build: ffmpeg not on PATH")
                return False
            except subprocess.TimeoutExpired:
                logger.warning(
                    "dome timelapse build: ffmpeg stitch timed out for %s/%s",
                    host_key, date_yyyymmdd,
                )
                return False
            if result.returncode != 0:
                tail = result.stderr.decode(errors="replace")[-500:] if result.stderr else ""
                logger.warning(
                    "dome timelapse build: ffmpeg stitch failed for %s/%s: %s",
                    host_key, date_yyyymmdd, tail,
                )
                return False

            logger.info(
                "dome timelapse built from camera timelapses %s/%s (%d frames)",
                host_key, date_yyyymmdd, rendered,
            )
            return True

    def _prune_sky_dome_timelapses() -> None:
        """Delete dome timelapse MP4s older than the retention window."""
        cutoff = time.time() - _DOME_TIMELAPSE_RETENTION_DAYS * 86400
        removed = 0
        try:
            for host_dir in THUMB_CACHE_DIR.glob("*/sky_dome_timelapse"):
                for mp4 in host_dir.glob("*.mp4"):
                    try:
                        if mp4.stat().st_mtime < cutoff:
                            mp4.unlink()
                            removed += 1
                    except OSError:
                        continue
        except OSError:
            logger.warning("dome timelapse prune walk failed", exc_info=True)
            return
        if removed:
            logger.info("dome timelapse prune: removed %d expired mp4(s)", removed)

    # ── Scheduler function (started via start_dome_scheduler) ────────────

    def _sky_dome_scheduler() -> None:
        """Daemon loop — pre-renders both dome variants per station on a
        twilight-aware cadence and builds the BW timelapse at civil dawn.

        Cadence per station (independent):
          - BW (``ff_max``): every ``_DOME_PRERENDER_INTERVAL_NIGHT_S``
            when sun < -6 deg, otherwise ``_DOME_PRERENDER_INTERVAL_DAY_S``.
          - Colour (``stack``): every ``_DOME_COLOUR_NIGHT_INTERVAL_S`` at
            night, otherwise daily.

        Per-station consecutive failures (>= ``_DOME_FAILURE_MUTE_THRESHOLD``)
        mute the station for ``_DOME_FAILURE_MUTE_DURATION_S``. Exceptions
        in one station's render never starve the others — the loop
        catches and carries on.

        Renders are parallelised across stations so that slow/unreachable
        stations don't starve reachable ones.  Dawn crossing (sun_alt -6 deg
        from below) triggers the previous night's MP4 build.  Daily prune
        sweeps expired MP4s.
        """
        logger.info("sky-dome scheduler starting")
        last_prune = 0.0
        last_sun_alt: dict[str, float] = {host_key: -90.0 for host_key in config.stations}
        last_color_render: dict[str, float] = {}

        _DOME_POOL_SIZE = min(3, len(config.stations) or 1)

        def _render_station(host_key: str, now: float) -> tuple[str, bool]:
            """Render ff_max (+ colour when due) for one station.

            Returns (host_key, ok_ff).  Runs inside the thread pool.
            """
            ok_ff = False
            try:
                ok_ff = _render_and_archive_sky_dome(host_key, "ff_max")
            except Exception:
                logger.warning(
                    "dome ff_max render failed for %s",
                    host_key, exc_info=True,
                )

            sun_alt = _station_sun_alt(host_key)
            is_night = sun_alt is not None and sun_alt < 0.0
            colour_interval = (
                _DOME_COLOUR_NIGHT_INTERVAL_S if is_night
                else _DOME_PRERENDER_INTERVAL_DAY_S
            )
            if now - last_color_render.get(host_key, 0) >= colour_interval:
                try:
                    _render_and_archive_sky_dome(host_key, "stack")
                except Exception:
                    logger.warning(
                        "dome stack render failed for %s",
                        host_key, exc_info=True,
                    )
                last_color_render[host_key] = now

            return host_key, ok_ff

        # Warm-up — parallel render per station, both variants, so the
        # disk cache is hot before the first user request lands.
        def _warmup_station(host_key: str) -> None:
            try:
                _render_and_archive_sky_dome(host_key, "stack")
            except Exception:
                logger.warning(
                    "dome warm-up (stack) failed for %s", host_key, exc_info=True,
                )
            try:
                _render_and_archive_sky_dome(host_key, "ff_max")
            except Exception:
                logger.warning(
                    "dome warm-up (ff_max) failed for %s", host_key, exc_info=True,
                )

        with ThreadPoolExecutor(max_workers=_DOME_POOL_SIZE) as pool:
            list(pool.map(_warmup_station, list(config.stations)))

        while True:
            try:
                now = time.time()

                if now - last_prune > 86400:
                    try:
                        _prune_sky_dome_timelapses()
                    except Exception:
                        logger.exception("dome timelapse prune failed")
                    last_prune = now

                # Collect stations that are due for a render.
                due: list[str] = []
                for host_key in list(config.stations):
                    if _dome_failure_mute_until.get(host_key, 0) > now:
                        continue
                    if _dome_next_render.get(host_key, 0) <= now:
                        due.append(host_key)

                # Render due stations in parallel.
                if due:
                    with ThreadPoolExecutor(max_workers=_DOME_POOL_SIZE) as pool:
                        futures = {
                            pool.submit(_render_station, hk, now): hk
                            for hk in due
                        }
                        for fut in as_completed(futures):
                            host_key = futures[fut]
                            try:
                                _, ok_ff = fut.result()
                            except Exception:
                                logger.exception(
                                    "dome render worker crashed for %s", host_key,
                                )
                                ok_ff = False

                            sun_alt = _station_sun_alt(host_key)
                            is_night = sun_alt is not None and sun_alt < 0.0
                            interval = (
                                _DOME_PRERENDER_INTERVAL_NIGHT_S if is_night
                                else _DOME_PRERENDER_INTERVAL_DAY_S
                            )

                            if ok_ff:
                                _dome_consecutive_failures[host_key] = 0
                            else:
                                _dome_consecutive_failures[host_key] = (
                                    _dome_consecutive_failures.get(host_key, 0) + 1
                                )
                                if (_dome_consecutive_failures[host_key]
                                        >= _DOME_FAILURE_MUTE_THRESHOLD):
                                    _dome_failure_mute_until[host_key] = (
                                        now + _DOME_FAILURE_MUTE_DURATION_S
                                    )
                                    logger.warning(
                                        "dome render muted for %s for %ds",
                                        host_key, _DOME_FAILURE_MUTE_DURATION_S,
                                    )
                                    _dome_consecutive_failures[host_key] = 0

                            logger.debug(
                                "dome scheduled %s: sun_alt=%s is_night=%s next=+%ds",
                                host_key,
                                "%.1f" % sun_alt if sun_alt is not None else "n/a",
                                is_night, interval,
                            )
                            _dome_next_render[host_key] = time.time() + interval

                # Dawn detection runs in the main thread (fast, no I/O).
                for host_key in list(config.stations):
                    sun_alt = _station_sun_alt(host_key)
                    prev_alt = last_sun_alt.get(host_key)
                    if (sun_alt is not None and prev_alt is not None
                            and prev_alt < -6.0 <= sun_alt):
                        last_night = _dome_capture_night(
                            datetime.now(timezone.utc) - timedelta(hours=2),
                        )
                        logger.info(
                            "dawn detected for %s, building timelapse for %s",
                            host_key, last_night,
                        )
                        try:
                            _build_sky_dome_timelapse_from_mp4s(host_key, last_night)
                        except Exception:
                            logger.warning(
                                "dome timelapse build failed for %s/%s",
                                host_key, last_night, exc_info=True,
                            )
                    if sun_alt is not None:
                        last_sun_alt[host_key] = sun_alt
                        _dome_last_sun_alt[host_key] = sun_alt
            except Exception:
                logger.exception("sky-dome scheduler outer loop error")
            time.sleep(15)

    # Store the scheduler function on the module for start_dome_scheduler.
    register_sky_dome_routes._scheduler = _sky_dome_scheduler  # type: ignore[attr-defined]

    # ── /api/sky_dome_timelapse routes ───────────────────────────────────

    @app.route("/api/sky_dome_timelapse/<host_key>/dates")
    @require_station
    def api_sky_dome_timelapse_dates(host_key: str):
        """List available BW dome timelapse dates for a station, newest first."""
        _require_station(host_key)
        d = _sky_dome_timelapse_dir(host_key)
        if not d.exists():
            return jsonify({"dates": []})
        dates = sorted(
            (p.stem for p in d.glob("*.mp4")),
            reverse=True,
        )
        return jsonify({"dates": dates})

    @app.route("/api/sky_dome_timelapse/<host_key>/<date>.mp4")
    @require_station
    def api_sky_dome_timelapse_mp4(host_key: str, date: str):
        """Serve a single BW dome timelapse MP4 for ``date`` (YYYYMMDD)."""
        _require_station(host_key)
        if not re.fullmatch(r"\d{8}", date):
            abort(400)
        path = _sky_dome_timelapse_path(host_key, date)
        if not path.exists():
            abort(404)
        response = send_file(path, mimetype="video/mp4", conditional=True)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    # ── /api/latest_frame, /api/latest_ff_maxpixel, /api/latest_chunk ───

    @app.route("/api/latest_frame/<host_key>/<cam>")
    @require_station
    def api_latest_frame(host_key: str, cam: str):
        """Live per-camera snapshot for the Final Data Products tab.

        Proxies the station's /api/latest_frame, which already prefers the
        rolling colour chunk stack and falls back to the RMS captured_stack.
        Falls further back to /api/latest_stack for older deployments that
        predate the new endpoint, so the tile still renders on stations
        running the previous rovimen-scripts bundle.

        The response is streamed rather than cached on the VPS — the
        underlying file rotates every few minutes and we don't want to
        serve a stale snapshot to a tile labelled "latest".
        """
        _require_station(host_key)
        if not re.match(r"^[A-Z0-9]+$", cam, re.IGNORECASE):
            abort(400)
        # Try the new combined endpoint first.
        try:
            url = _media_url(host_key, "api", "latest_frame", cam)
            resp = _session_for_url(url).get(url, timeout=15)
            if resp.status_code == 200:
                return Response(
                    resp.content, status=200,
                    headers={
                        "Content-Type": resp.headers.get("Content-Type", "image/webp"),
                        "Cache-Control": "public, max-age=60",
                    },
                )
        except Exception:
            pass
        # Old deployments — try the captured_stack-only endpoint.
        try:
            url = _media_url(host_key, "api", "latest_stack", cam)
            resp = _session_for_url(url).get(url, timeout=15)
            if resp.status_code == 200:
                return Response(
                    resp.content, status=200,
                    headers={
                        "Content-Type": resp.headers.get("Content-Type", "image/jpeg"),
                        "Cache-Control": "public, max-age=60",
                    },
                )
        except Exception:
            pass
        # Pre-#145 stations (default update_channel) — walk the existing
        # /api/nights + /api/rms/plot_image chain so the FDP tile renders
        # for stations that haven't pulled either of the new endpoints.
        img_bytes = _fetch_stack_bytes_via_rmsplots(host_key, cam.upper())
        if img_bytes is not None:
            return Response(
                img_bytes, status=200,
                headers={"Content-Type": "image/jpeg", "Cache-Control": "public, max-age=60"},
            )
        return jsonify({"error": "no recent frame available"}), 503

    @app.route("/api/latest_ff_maxpixel/<host_key>/<cam>")
    @require_station
    def api_latest_ff_maxpixel(host_key: str, cam: str):
        """BW FF maxpixel for the overview map station band.

        Prefers the latest pushed thumbnail (reversed-HTTP push §9): the station
        pusher POSTs the newest FF maxpixel to the VPS ingest and it is cached
        latest-per-(station,cam) in memory, so the dashboard need not reach into
        :7779. Falls back to the on-demand :7779 proxy when no pushed thumbnail
        is present (station not yet enrolled in push, or dashboard just
        restarted) — the pull path is preserved unchanged.
        """
        _require_station(host_key)
        if not re.match(r"^[A-Z0-9]+$", cam, re.IGNORECASE):
            abort(400)
        # 1) Pushed thumbnail (no station reach). Serve if present.
        thumb = cache.get_live_thumb(host_key, cam.upper())
        if thumb and thumb.get("data"):
            hdrs = {
                "Content-Type": thumb.get("content_type", "image/webp"),
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Access-Control-Expose-Headers": "X-FF-Timestamp",
                "X-Thumb-Source": "push",
            }
            if thumb.get("ff_timestamp"):
                hdrs["X-FF-Timestamp"] = thumb["ff_timestamp"]
            return Response(thumb["data"], status=200, headers=hdrs)
        # 2) Fallback: on-demand pull to the station :7779 (unchanged).
        try:
            url = _media_url(host_key, "api", "latest_ff_maxpixel", cam)
            resp = _session_for_url(url).get(url, timeout=15)
            if resp.status_code == 200 and resp.content:
                hdrs = {
                    "Content-Type": resp.headers.get("Content-Type", "image/webp"),
                    "Cache-Control": "public, max-age=30",
                    "Access-Control-Expose-Headers": "X-FF-Timestamp",
                }
                ff_ts = resp.headers.get("X-FF-Timestamp")
                if ff_ts:
                    hdrs["X-FF-Timestamp"] = ff_ts
                return Response(resp.content, status=200, headers=hdrs)
        except Exception:
            pass
        return jsonify({"error": "no ff maxpixel available"}), 503

    @app.route("/api/latest_chunk/<host_key>/<cam>")
    @require_station
    def api_latest_chunk(host_key: str, cam: str):
        """Return metadata for the most recent color chunk on a camera."""
        _require_station(host_key)
        if not re.match(r"^[A-Z0-9]+$", cam, re.IGNORECASE):
            abort(400)
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        now = _dt.now(_tz.utc)
        dates = [now.strftime("%Y%m%d"), (now - _td(days=1)).strftime("%Y%m%d")]
        for date in dates:
            try:
                raw = station_get_raw(
                    config, tunnels, host_key, f"/api/chunks/{cam}/{date}"
                )
            except Exception:
                continue
            chunks = raw.get("chunks", raw) if isinstance(raw, dict) else raw
            if not chunks:
                continue
            latest = chunks[-1]
            fn = latest.get("filename", "")
            return jsonify({
                "date": date,
                "filename": fn,
                "video_url": f"/video/{host_key}/{cam}/{date}/{fn}",
                "time": latest.get("time"),
            })
        return jsonify({"error": "no chunks available"}), 404

    # ── /api/twilight/<host_key>/<date> ──────────────────────────────────

    @app.route("/api/twilight/<host_key>/<date>")
    @public_route
    def api_twilight(host_key: str, date: str):
        """Return sunset/sunrise UTC times for a station on a given date.

        Pure-function output (station lat/lon x date) that never changes
        once computed — paired with lru_cache on the underlying compute
        helpers, a 1 h browser cache keeps tab-switching free of work.

        Plain (page-less) ``@public_route``: the Detections + station-detail
        twilight sliders all need it, so it's always-eligible rather than tied
        to one page toggle. The payload is derived purely from the station's
        published lat/lon and the date (sunset/sunrise/moon) — no IP, cam_ip,
        or host path. Anonymous callers may still only read it for a
        ``public: true`` station: a private/commissioning station 404s so its
        very coordinates aren't disclosed. Logged-in operators keep the fleet."""
        _require_station(host_key)
        if not station_is_public_for_request(config, host_key):
            abort(404)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        station = config.stations.get(host_key)
        if not station or station.lat is None or station.lon is None:
            return _json_cached({
                "sunset_utc": "18:00", "sunrise_utc": "06:00",
                "sunset_min": 1080, "sunrise_min": 360,
                "duration_hours": 12.0,
                "nautical_sunset_min": 1065, "nautical_sunrise_min": 375,
                "civil_sunset_min": 1050, "civil_sunrise_min": 390,
            }, max_age=3600)
        astro    = _compute_twilight(station.lat, station.lon, date, horizon=-18.0)
        nautical = _compute_twilight(station.lat, station.lon, date, horizon=-12.0)
        civil    = _compute_twilight(station.lat, station.lon, date, horizon=-6.0)
        return _json_cached({
            **astro,
            "nautical_sunset_min":  nautical.get("sunset_min"),
            "nautical_sunrise_min": nautical.get("sunrise_min"),
            "civil_sunset_min":     civil.get("sunset_min"),
            "civil_sunrise_min":    civil.get("sunrise_min"),
            **_compute_moon_phase(date),
            **_compute_moon_rise_set(station.lat, station.lon, date),
        }, max_age=3600)


def start_dome_scheduler() -> threading.Thread:
    """Create and start the sky-dome background scheduler thread.

    Must be called after ``register_sky_dome_routes`` — the scheduler
    function is attached as an attribute during registration.
    """
    scheduler_fn = getattr(register_sky_dome_routes, "_scheduler", None)
    if scheduler_fn is None:
        raise RuntimeError(
            "start_dome_scheduler called before register_sky_dome_routes"
        )
    t = threading.Thread(
        target=scheduler_fn,
        daemon=True,
        name="sky-dome-scheduler",
    )
    t.start()
    return t
