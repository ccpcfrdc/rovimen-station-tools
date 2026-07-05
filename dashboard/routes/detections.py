"""Detections routes -- fan-out, correlation, live-feed.

Extracted from rovimen_dashboard.py.  All routes preserved verbatim -- same URLs,
same behaviour, same decorators.  Wired in from ``create_app()`` via
``register_detections_routes``.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Flask, abort, jsonify, request

from auth import is_anonymous
from cache_store import _drop_future_dates, ARCHIVE_PATH
from models import StationConfig

from http_caching import _json_cached
from routes.archive import _parse_radiants_txt
from security import public_route
from station_client import _with_sshfs_timeout, station_get_raw
from tunnels import _TunnelDown

logger = logging.getLogger(__name__)


# ── Detection-grid helpers (issue #313) ──────────────────────────────────────
# Pure functions kept at module scope so they are unit-testable without a live
# Flask app or station fan-out. The grid view re-shapes the cached flat
# detections list into a time-bucket x camera matrix.

def _parse_hhmm(raw: str | None) -> int | None:
    """Parse "HH:MM" (UTC) into minutes-of-day, or None if absent/invalid.

    Lenient on purpose: a bad value collapses to "no bound" rather than a 400,
    so a stale query string never blanks the grid.
    """
    if not raw:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 24 or mm > 59 or (hh == 24 and mm != 0):
        return None
    return hh * 60 + mm


def _grid_columns(config) -> list[dict]:
    """Ordered camera columns across the whole fleet.

    Grouped by station (host_key sorted) so cameras from the same site sit
    next to each other; each column carries enough metadata for the frontend
    to label and group without a second /api/stations round-trip.
    """
    columns: list[dict] = []
    for host_key, station in sorted(config.stations.items()):
        for cam in station.cameras:
            columns.append({
                "cam": cam.code,
                "host_key": host_key,
                "station_label": station.label,
            })
    return columns


def _meteor_minute_of_day(meteor_time: str) -> int | None:
    """UTC minute-of-day for an ISO meteor_time, or None if unparseable."""
    if not meteor_time:
        return None
    try:
        dt = datetime.fromisoformat(meteor_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.hour * 60 + dt.minute


def _build_detection_grid(
    detections: list[dict],
    events: list[dict],
    columns: list[dict],
    bucket_min: int,
    time_from: int | None,
    time_to: int | None,
) -> list[dict]:
    """Re-shape the flat detections list into time-bucket rows.

    A row exists only for buckets that contain at least one detection (sparse
    rows -- a 5-min grid over a 12-hour night is 144 slots, mostly empty).
    Each row's ``cells`` is keyed by camera code. ``multi`` flags detections
    that belong to a multi-station event so the frontend can highlight rows
    where several cameras line up -- the whole point of the view.

    Rows are returned newest-first to match the rest of the Detections UI.
    """
    # Fast membership test: "is this (filename, cam) part of a 2+ station event?"
    multi_set: set[tuple[str, str]] = set()
    for ev in events:
        if not ev or (ev.get("witness_count") or 0) <= 1:
            continue
        for w in ev.get("witnesses", []) or []:
            multi_set.add((w.get("filename"), w.get("cam")))

    valid_cams = {c["cam"] for c in columns}
    buckets: dict[int, dict] = {}

    for det in detections:
        cam = det.get("cam")
        if cam not in valid_cams:
            continue
        minute = _meteor_minute_of_day(det.get("meteor_time"))
        if minute is None:
            continue
        if time_from is not None and minute < time_from:
            continue
        if time_to is not None and minute > time_to:
            continue

        bucket_idx = (minute // bucket_min) * bucket_min
        row = buckets.get(bucket_idx)
        if row is None:
            row = {
                "bucket_start_min": bucket_idx,
                "label": f"{bucket_idx // 60:02d}:{bucket_idx % 60:02d}",
                "cells": {},
                "total": 0,
                "station_count": 0,
                "has_multi": False,
            }
            buckets[bucket_idx] = row

        cell = {
            "host_key": det.get("host_key"),
            "cam": cam,
            "station_label": det.get("station_label"),
            "filename": det.get("filename"),
            "meteor_time": det.get("meteor_time"),
            "detection_offset_s": det.get("detection_offset_s"),
            "stack": det.get("stack"),
            "rms": det.get("rms"),
            "multi": (det.get("filename"), cam) in multi_set,
        }
        row["cells"].setdefault(cam, []).append(cell)
        row["total"] += 1
        if cell["multi"]:
            row["has_multi"] = True

    rows = sorted(buckets.values(), key=lambda r: r["bucket_start_min"], reverse=True)
    for row in rows:
        row["station_count"] = len({
            c["host_key"]
            for cells in row["cells"].values()
            for c in cells
        })
    return rows


# ── All-footage grid helpers (issue: inspect every camera in a time window) ───
# Unlike the detection grid above, the footage grid shows *continuous* recording
# (every 20 s color chunk), not just locked meteors, so an operator can sweep
# all cameras between two times when something happened that RMS didn't trigger
# on (a reported fireball, a satellite re-entry, aurora). Continuous chunks only
# live on-station for ``video_days_to_keep`` (~2 days); older nights degrade to
# the locked clips that survive in the VPS archive (``degraded`` flag).
#
# Pure + module-scoped so they are unit-testable without a live Flask app.

# Minutes from midnight to a noon-anchored ordinal (0 = 12:00, 720 = 00:00,
# 1440-1 = just before next noon). RMS nights run noon->noon, so this is what
# makes 02:00 (morning) sort *after* 22:00 (evening) chronologically.
def _noon_anchor(minute: int) -> int:
    return (minute - 720 + 1440) % 1440


def _hhmmss_to_min(time_str: str | None) -> int | None:
    """UTC minute-of-day for an "HH:MM:SS" (or "HH:MM") string, or None."""
    if not time_str:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})", time_str.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 24 or mm > 59:
        return None
    return hh * 60 + mm


def _in_footage_window(minute: int, time_from: int | None, time_to: int | None) -> bool:
    """Inclusive window test that also handles a window crossing midnight.

    When ``time_from > time_to`` (e.g. 22:00 -> 02:00) the window wraps past
    midnight, so a minute passes if it is *either* after ``from`` or before
    ``to`` -- mirroring the station API's own from/to handling.
    """
    if time_from is None and time_to is None:
        return True
    if time_from is not None and time_to is not None and time_from > time_to:
        return minute >= time_from or minute <= time_to
    if time_from is not None and minute < time_from:
        return False
    if time_to is not None and minute > time_to:
        return False
    return True


# The footage grid shows *every* clip in the window (no representative/expand),
# so the window is capped: 5 min at a 20 s segment cadence is ~15 clips/camera,
# already a dense row. Anything wider would flood the matrix.
FOOTAGE_MAX_WINDOW_MIN = 5


def _cap_footage_window(
    time_from: int | None,
    time_to: int | None,
    max_min: int = FOOTAGE_MAX_WINDOW_MIN,
) -> tuple[int | None, int | None]:
    """Clamp a [from, to] UTC-minute window to at most ``max_min`` minutes.

    Wrap-aware (a window may cross midnight). A single bound is expanded to a
    full ``max_min`` window; an empty/over-long span is clamped to ``max_min``.
    Returns the (possibly adjusted) bounds; both-None is left untouched.
    """
    if time_from is not None and time_to is not None:
        span = (time_to - time_from) % 1440
        if span == 0 or span > max_min:
            time_to = (time_from + max_min) % 1440
    elif time_from is not None:
        time_to = (time_from + max_min) % 1440
    elif time_to is not None:
        time_from = (time_to - max_min) % 1440
    return time_from, time_to


def _build_footage_grid(
    footage_by_cam: dict[str, dict],
    columns: list[dict],
    bucket_min: int,
    time_from: int | None,
    time_to: int | None,
) -> list[dict]:
    """Re-shape per-camera continuous chunks into time-bucket rows.

    Rows are chronological time buckets (earliest first, matching the
    noon->noon night), one per populated bucket. Each cell carries *every*
    chunk that camera recorded in that bucket; the frontend renders them all
    (the window is capped upstream, so a cell holds only a handful). The
    frontend transposes this into cameras-as-rows / time-as-columns.
    """
    valid_cams = {c["cam"] for c in columns}
    buckets: dict[int, dict] = {}

    for cam, info in footage_by_cam.items():
        if cam not in valid_cams:
            continue
        host_key = info.get("host_key")
        for ch in info.get("chunks", []) or []:
            minute = _hhmmss_to_min(ch.get("time"))
            if minute is None:
                continue
            if not _in_footage_window(minute, time_from, time_to):
                continue
            bucket_idx = (minute // bucket_min) * bucket_min
            row = buckets.get(bucket_idx)
            if row is None:
                row = {
                    "bucket_start_min": bucket_idx,
                    "label": f"{bucket_idx // 60:02d}:{bucket_idx % 60:02d}",
                    "cells": {},
                    "total": 0,
                    "station_count": 0,
                    "has_detection": False,
                }
                buckets[bucket_idx] = row
            row["cells"].setdefault(cam, []).append({
                "host_key": host_key,
                "filename": ch.get("filename"),
                "time": ch.get("time"),
                "stack": ch.get("stack"),
                "locked": bool(ch.get("locked")),
                "meteor_time": ch.get("meteor_time"),
                "detection_offset_s": ch.get("detection_offset_s"),
            })
            row["total"] += 1
            if ch.get("locked"):
                row["has_detection"] = True

    rows = sorted(buckets.values(), key=lambda r: _noon_anchor(r["bucket_start_min"]))
    for row in rows:
        row["station_count"] = len({
            c["host_key"]
            for cells in row["cells"].values()
            for c in cells
        })
        # Each cell carries the full (time-sorted) chunk list; the frontend
        # renders every clip in the bucket.
        cells_out: dict[str, dict] = {}
        for cam, chunks in row["cells"].items():
            chunks.sort(key=lambda c: c.get("time") or "")
            cells_out[cam] = {
                "host_key": chunks[0]["host_key"],
                "count": len(chunks),
                "chunks": chunks,
                "has_detection": any(c.get("locked") for c in chunks),
            }
        row["cells"] = cells_out
    return rows


def register_detections_routes(
    app: Flask,
    config,
    tunnels,
    cache,
    *,
    archive_idx,
    detections_cache: dict[str, tuple[float, Any]],
    detections_ttl: float,
) -> None:
    # Helpers stashed on the app by create_app().
    _archive_nights = app._archive_nights  # type: ignore[attr-defined]
    _read_archive_locked_chunks = app._read_archive_locked_chunks  # type: ignore[attr-defined]

    # ── Anonymous public filtering ────────────────────────────────────────
    # /events is a public page (page="events"), and its JS pulls detection
    # data from the routes below. For an anonymous caller those routes must
    # show ONLY ``public: true`` stations — commissioning / opted-out sites
    # never leak into the anon surface — while a logged-in operator keeps the
    # full fleet (fleet-wide read rule). The shared ``_compute_detections_payload``
    # cache is deliberately keyed on the *full fleet* (never on the anon flag),
    # so we filter its OUTPUT per request rather than caching an anon-specific
    # payload — this mirrors the highlights route (routes/highlights.py) and
    # guarantees an anon caller can never be handed a cached full-fleet body.
    # The detection payloads carry no IP / cam_ip / ssh_user / host-path
    # fields to begin with (only labels, camera codes, timestamps, filenames,
    # RMS science metadata), so dropping non-public stations is the whole of
    # the redaction here.

    def _public_host_keys() -> set[str]:
        return {hk for hk, st in config.stations.items() if st.public}

    def _public_cam_codes() -> set[str]:
        return {
            c.code.upper()
            for st in config.stations.values() if st.public
            for c in st.cameras
        }

    def _filter_payload_for_anon(payload: dict) -> dict:
        """Return a copy of a per-night detections payload with non-public
        stations/cameras stripped, for anonymous callers. A no-op (returns the
        same object) for logged-in callers."""
        if not is_anonymous():
            return payload
        pub_hosts = _public_host_keys()
        pub_cams = _public_cam_codes()

        by_station = {
            hk: data for hk, data in (payload.get("by_station") or {}).items()
            if hk in pub_hosts
        }
        detections = [
            d for d in (payload.get("detections") or [])
            if d.get("host_key") in pub_hosts
        ]
        # Re-filter each event's witnesses to public cameras and drop any
        # event that no longer spans >=2 public stations, so a non-public
        # witness can never appear (not even as an unlabelled marker) in the
        # anon events feed.
        events = []
        for ev in (payload.get("events") or []):
            witnesses = [
                w for w in (ev.get("witnesses") or [])
                if w.get("host_key") in pub_hosts
                and (w.get("cam") or "").upper() in pub_cams
            ]
            station_set = {w.get("host_key") for w in witnesses}
            if len(station_set) >= 2:
                events.append({
                    **ev,
                    "witnesses": witnesses,
                    "witness_count": len(witnesses),
                    "station_count": len(station_set),
                })
        return {
            **payload,
            "by_station": by_station,
            "detections": detections,
            "events": events,
        }

    # ── Detections fan-out ────────────────────────────────────────────────

    @app.route("/api/detections/nights")
    @public_route(page="events")
    def api_detections_nights():
        """Return union of all stations' capture nights, newest-first.

        Includes both (a) nights currently on-station and (b) nights retained
        only in the VPS archive. Without (b), the picker disappears nights
        within a few days as stations rotate out old MKVs.

        Public under the "events" page toggle (the Detections night picker
        needs it). Anonymous callers only union nights from ``public: true``
        stations, so a night that exists solely on a commissioning station
        never appears in the anon picker.
        """
        nights: set[str] = set()
        anon = is_anonymous()
        pub_hosts = _public_host_keys() if anon else None

        def fetch_nights(host_key: str, station: StationConfig) -> set[str]:
            result: set[str] = set()
            status = cache.get_status(host_key)
            if not status or not status.get("online"):
                return result
            cam = station.cameras[0] if station.cameras else None
            if not cam:
                return result
            try:
                raw = station_get_raw(
                    config, tunnels, host_key,
                    f"/api/nights/{cam.code}",
                    timeout=6,
                )
                for n in (raw if isinstance(raw, list) else []):
                    if re.match(r"^\d{8}$", str(n)):
                        result.add(str(n))
            except Exception:
                pass
            return result

        stations_iter = [
            (k, s) for k, s in config.stations.items()
            if pub_hosts is None or k in pub_hosts
        ]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(fetch_nights, k, s) for k, s in stations_iter]
            for f in as_completed(futures):
                try:
                    nights |= f.result()
                except Exception:
                    pass

        # Union in archive-resident nights per camera from the VPS storagebox.
        cam_codes = [
            cam.code for hk, st in config.stations.items()
            if pub_hosts is None or hk in pub_hosts
            for cam in st.cameras
        ]
        with ThreadPoolExecutor(max_workers=8) as pool:
            for ns in pool.map(_archive_nights, cam_codes):
                for n in ns or []:
                    nights.add(n)

        return jsonify(_drop_future_dates(sorted(nights, reverse=True)))

    # Fields surfaced from RMS detection metadata onto each locked chunk.
    _RMS_META_FIELDS = (
        "shower", "mag_apparent", "mag_absolute",
        "duration_s", "angular_velocity",
        "ra_radiant", "dec_radiant", "radiant_elev",
        "solar_lon", "num_segments", "fps",
    )

    def _compute_detections_payload(date: str) -> dict:
        """Build the per-night detections payload (cached).

        Shared by /api/detections/<date> and /api/detections/range.
        """
        cached = detections_cache.get(date)
        if cached and time.monotonic() < cached[0]:
            return cached[1]

        by_station: dict[str, Any] = {}

        is_tonight = date >= datetime.now(timezone.utc).strftime("%Y%m%d")

        def fetch_one_camera(host_key: str, cam_code: str, online: bool) -> tuple[str, list[dict]]:
            """Fetch one camera's locked chunks + RMS detection metadata.
            Past dates read the VPS archive (local disk, instant).
            Tonight fans out to station APIs for live data."""
            locked: list[dict] = []
            rms_by_time: dict[str, dict] = {}
            if not is_tonight:
                try:
                    locked = _read_archive_locked_chunks(cam_code, date)
                except Exception:
                    pass
                def _read_rms_archive():
                    rms_dir = ARCHIVE_PATH / cam_code / date / "rms"
                    result: dict[str, dict] = {}
                    if rms_dir.is_dir():
                        for f in sorted(rms_dir.glob("*_radiants.txt")):
                            for d in _parse_radiants_txt(f):
                                t = d.get("time_utc")
                                if t:
                                    result[t] = d
                    return result
                rms_by_time.update(
                    _with_sshfs_timeout(_read_rms_archive, timeout=5.0, default={})
                )
            if online and is_tonight:
                try:
                    raw = station_get_raw(
                        config, tunnels, host_key,
                        f"/api/chunks/{cam_code}/{date}",
                        timeout=6,
                    )
                    chunks = raw.get("chunks", raw) if isinstance(raw, dict) else raw
                    locked = [c for c in chunks if c.get("meteor_time")]
                except Exception:
                    locked = []
                try:
                    rms_data = station_get_raw(
                        config, tunnels, host_key,
                        f"/api/rms-detections/{cam_code}/{date}",
                        timeout=4,
                    )
                    if isinstance(rms_data, dict):
                        for d in rms_data.get("detections", []) or []:
                            t = d.get("time_utc")
                            if t:
                                rms_by_time[t] = d
                except Exception:
                    pass
            if not locked and is_tonight:
                try:
                    locked = _read_archive_locked_chunks(cam_code, date)
                except Exception:
                    pass
            # Merge RMS metadata onto matching locked chunks (second-precision match).
            for c in locked:
                mt = c.get("meteor_time")
                if not mt:
                    continue
                key = mt.split(".", 1)[0]
                src = rms_by_time.get(key)
                if src:
                    c["rms"] = {f: src.get(f) for f in _RMS_META_FIELDS}
            return cam_code, locked

        def fetch_station(host_key: str, station: StationConfig) -> tuple[str, dict]:
            result: dict[str, Any] = {"label": station.label, "online": False, "cameras": {}}
            status = cache.get_status(host_key)
            online = bool(status and status.get("online"))
            result["online"] = online
            cams = list(station.cameras)
            if not cams:
                return host_key, result
            with ThreadPoolExecutor(max_workers=min(8, len(cams))) as cam_pool:
                cam_futures = [
                    cam_pool.submit(fetch_one_camera, host_key, c.code, online)
                    for c in cams
                ]
                for fut in as_completed(cam_futures):
                    try:
                        cam_code, locked = fut.result()
                    except Exception:
                        continue
                    result["cameras"][cam_code] = locked
            return host_key, result

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(fetch_station, key, st): key
                for key, st in config.stations.items()
            }
            for future in as_completed(futures):
                try:
                    host_key, data = future.result()
                    by_station[host_key] = data
                except Exception:
                    pass

        # Build flat list of all detections for correlation
        all_det: list[dict] = []
        for host_key, st_data in by_station.items():
            for cam_code, chunks in st_data.get("cameras", {}).items():
                for chunk in chunks:
                    mt = chunk.get("meteor_time")
                    if mt:
                        all_det.append({
                            "host_key": host_key,
                            "station_label": st_data["label"],
                            "cam": cam_code,
                            "filename": chunk["filename"],
                            "meteor_time": mt,
                            "detection_offset_s": chunk.get("detection_offset_s"),
                            "stack": chunk.get("stack"),
                            "time": chunk.get("time"),
                            "lock_type": chunk.get("lock_type"),
                            "rms": chunk.get("rms"),
                        })

        all_det.sort(key=lambda x: x["meteor_time"])

        # Cluster detections within correlation_window_s
        window = config.correlation_window_s
        used: set[int] = set()
        events: list[dict] = []
        for i, det in enumerate(all_det):
            if i in used:
                continue
            group = [det]
            used.add(i)
            t0 = datetime.fromisoformat(det["meteor_time"])
            for j in range(i + 1, len(all_det)):
                if j in used:
                    continue
                t1 = datetime.fromisoformat(all_det[j]["meteor_time"])
                if abs((t1 - t0).total_seconds()) <= window:
                    group.append(all_det[j])
                    used.add(j)
            # Deduplicate by (host_key, cam): same camera may lock two adjacent
            # chunks for the same meteor (edge detection). Keep the one with the
            # larger detection_offset_s -- it has more pre-event context.
            seen: dict[tuple, dict] = {}
            for w in group:
                key = (w["host_key"], w["cam"])
                if key not in seen or (w.get("detection_offset_s") or 0) > (seen[key].get("detection_offset_s") or 0):
                    seen[key] = w
            witnesses = list(seen.values())
            station_set = {w["host_key"] for w in witnesses}
            if len(station_set) >= 2:
                events.append({
                    "event_time": det["meteor_time"],
                    "witness_count": len(witnesses),
                    "station_count": len(station_set),
                    "witnesses": witnesses,
                })

        payload = {
            "date": date,
            "correlation_window_s": window,
            "by_station": by_station,
            "events": events,
            "detections": all_det,
        }
        # Evict before inserting to cap cache size at 100 entries.
        if len(detections_cache) > 100:
            now_mono = time.monotonic()
            expired = [k for k, v in detections_cache.items() if v[0] < now_mono]
            for k in expired:
                detections_cache.pop(k, None)
            if len(detections_cache) > 100:
                oldest = sorted(detections_cache.items(), key=lambda x: x[1][0])
                for k, _ in oldest[:20]:
                    detections_cache.pop(k, None)
        ttl = detections_ttl
        if not is_tonight and not archive_idx.ready:
            ttl = 10
        detections_cache[date] = (time.monotonic() + ttl, payload)
        return payload

    # Expose to sub-modules (routes.highlights) that need it at request time.
    app._compute_detections_payload = _compute_detections_payload  # type: ignore[attr-defined]

    @app.route("/api/detections/<date>")
    @public_route(page="events")
    def api_detections(date: str):
        if not re.match(r"^\d{8}$", date):
            abort(400)
        payload = _filter_payload_for_anon(_compute_detections_payload(date))
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        max_age = 60 if date >= today else 3600
        return _json_cached(payload, max_age=max_age)

    @app.route("/api/detections/grid/<date>")
    @public_route(page="events")
    def api_detections_grid(date: str):
        """Time-bucket x camera matrix for one night (issue #313).

        Rows are fixed-width time buckets (default 5 min); columns are the
        cameras across the whole fleet. Each cell carries the detections that
        camera locked inside that bucket, so multi-station coincidences line
        up on the same row. The heavy fan-out is shared with the per-night
        detections endpoint via ``_compute_detections_payload`` -- this route
        only re-shapes the already-cached flat list, so a warm cache makes it
        cheap.

        Query params:
          bucket  -- bucket width in minutes (1-60, default 5)
          from    -- inclusive lower bound, "HH:MM" UTC (optional)
          to      -- inclusive upper bound, "HH:MM" UTC (optional)
        """
        if not re.match(r"^\d{8}$", date):
            abort(400)
        try:
            bucket_min = int(request.args.get("bucket", "5"))
        except (TypeError, ValueError):
            abort(400)
        if bucket_min < 1 or bucket_min > 60:
            abort(400)
        time_from = _parse_hhmm(request.args.get("from"))
        time_to = _parse_hhmm(request.args.get("to"))

        # Anon callers only see public stations: filter the shared per-night
        # payload (drops non-public detections/events/witnesses) AND the fleet
        # columns, so a non-public camera never appears as a grid column or
        # cell. Logged-in callers get the full-fleet grid.
        payload = _filter_payload_for_anon(_compute_detections_payload(date))
        columns = _grid_columns(config)
        if is_anonymous():
            pub_hosts = _public_host_keys()
            columns = [c for c in columns if c.get("host_key") in pub_hosts]
        rows = _build_detection_grid(
            payload.get("detections", []) or [],
            payload.get("events", []) or [],
            columns,
            bucket_min,
            time_from,
            time_to,
        )
        result = {
            "date": date,
            "bucket_min": bucket_min,
            "columns": columns,
            "rows": rows,
        }
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        max_age = 60 if date >= today else 3600
        return _json_cached(result, max_age=max_age)

    # ── All-footage fan-out (continuous chunks, not just detections) ──────
    footage_cache: dict[str, tuple[float, Any]] = {}

    def _compute_footage_payload(date: str) -> dict:
        """Fetch every camera's continuous chunks for a night (cached).

        Live stations serve the full color-chunk timeline from
        ``/api/chunks`` (no ``locked_only``); offline stations or nights whose
        continuous video has rotated off fall back to the locked clips kept in
        the VPS archive. ``degraded`` is true when no camera returned live
        continuous footage -- the frontend then warns that only detections
        survive for that night.
        """
        cached = footage_cache.get(date)
        if cached and time.monotonic() < cached[0]:
            return cached[1]

        footage_by_cam: dict[str, dict] = {}

        def fetch_cam(host_key: str, station: StationConfig, cam_code: str, online: bool):
            chunks: list[dict] = []
            source = "empty"
            if online:
                try:
                    raw = station_get_raw(
                        config, tunnels, host_key,
                        f"/api/chunks/{cam_code}/{date}",
                        timeout=8,
                    )
                    cl = raw.get("chunks", raw) if isinstance(raw, dict) else raw
                    if cl:
                        chunks = cl
                        source = "live"
                except Exception:
                    pass
            if not chunks:
                try:
                    arch = _read_archive_locked_chunks(cam_code, date)
                    if arch:
                        chunks = arch
                        source = "archive"
                except Exception:
                    pass
            return cam_code, host_key, station.label, chunks, source

        jobs: list[tuple[str, StationConfig, str, bool]] = []
        for host_key, station in config.stations.items():
            status = cache.get_status(host_key)
            online = bool(status and status.get("online"))
            for cam in station.cameras:
                jobs.append((host_key, station, cam.code, online))

        any_live = False
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(fetch_cam, *job) for job in jobs]
            for fut in as_completed(futures):
                try:
                    cam_code, host_key, label, chunks, source = fut.result()
                except Exception:
                    continue
                if source == "live":
                    any_live = True
                footage_by_cam[cam_code] = {
                    "host_key": host_key,
                    "station_label": label,
                    "chunks": chunks,
                    "source": source,
                }

        payload = {
            "date": date,
            "footage_by_cam": footage_by_cam,
            "degraded": not any_live,
        }
        is_tonight = date >= datetime.now(timezone.utc).strftime("%Y%m%d")
        ttl = 30 if is_tonight else 300
        if len(footage_cache) > 50:
            now_mono = time.monotonic()
            for k in [k for k, v in footage_cache.items() if v[0] < now_mono]:
                footage_cache.pop(k, None)
        footage_cache[date] = (time.monotonic() + ttl, payload)
        return payload

    @app.route("/api/footage/grid/<date>")
    @public_route(page="events")
    def api_footage_grid(date: str):
        """Time-bucket x camera matrix of *all* footage for one night.

        Every continuous chunk is a cell, so an operator can inspect every
        camera between two times. The window is capped to FOOTAGE_MAX_WINDOW_MIN
        because the view shows every clip. Query params:
          bucket -- bucket width in minutes (1-60, default 1)
          from   -- inclusive lower bound, "HH:MM" UTC (optional)
          to     -- inclusive upper bound, "HH:MM" UTC (optional; wraps midnight)

        Public under the "events" page toggle (the Detections all-footage grid
        needs it). Anonymous callers only ever see columns/cells for
        ``public: true`` stations: the shared footage cache stays full-fleet, so
        we drop non-public host_keys from both the columns and the footage map
        before building the grid. The cells carry host_key/cam/filename only —
        no ip/cam_ip/host-path — so the public-station filter is the whole of
        the redaction.
        """
        if not re.match(r"^\d{8}$", date):
            abort(400)
        try:
            bucket_min = int(request.args.get("bucket", "1"))
        except (TypeError, ValueError):
            abort(400)
        if bucket_min < 1 or bucket_min > 60:
            abort(400)
        time_from = _parse_hhmm(request.args.get("from"))
        time_to = _parse_hhmm(request.args.get("to"))
        # Defensive cap: the UI clamps too, but a raw API call must not be able
        # to request the whole night (thousands of clips) in this all-clips view.
        time_from, time_to = _cap_footage_window(time_from, time_to)

        payload = _compute_footage_payload(date)
        footage_by_cam = payload.get("footage_by_cam", {}) or {}
        columns = _grid_columns(config)
        if is_anonymous():
            pub_hosts = _public_host_keys()
            columns = [c for c in columns if c.get("host_key") in pub_hosts]
            footage_by_cam = {
                cam: data for cam, data in footage_by_cam.items()
                if data.get("host_key") in pub_hosts
            }
        rows = _build_footage_grid(
            footage_by_cam,
            columns,
            bucket_min,
            time_from,
            time_to,
        )
        result = {
            "date": date,
            "bucket_min": bucket_min,
            "max_window_min": FOOTAGE_MAX_WINDOW_MIN,
            "columns": columns,
            "rows": rows,
            "degraded": payload.get("degraded", False),
        }
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        max_age = 30 if date >= today else 300
        return _json_cached(result, max_age=max_age)

    @app.route("/api/detections/range")
    @public_route(page="events")
    def api_detections_range():
        """Flat chronological list of locked detections across a date range.

        Query params: from=YYYYMMDD, to=YYYYMMDD (inclusive).
        Each detection inherits the per-date enrichment: when RMS metadata
        exists for that meteor_time it is in the `rms` field, otherwise null.

        Public under the "events" page toggle. Anonymous callers only ever get
        detections from ``public: true`` stations; the shared per-date payload
        cache stays full-fleet, so we drop non-public host_keys per request.
        """
        date_from = request.args.get("from", "")
        date_to = request.args.get("to", "")
        if not re.match(r"^\d{8}$", date_from) or not re.match(r"^\d{8}$", date_to):
            abort(400)
        try:
            d_from = datetime.strptime(date_from, "%Y%m%d").date()
            d_to = datetime.strptime(date_to, "%Y%m%d").date()
        except ValueError:
            abort(400)
        if d_from > d_to:
            abort(400)

        dates: list[str] = []
        cur = d_from
        while cur <= d_to:
            dates.append(cur.strftime("%Y%m%d"))
            cur += timedelta(days=1)

        anon = is_anonymous()
        pub_hosts = _public_host_keys() if anon else None

        # Compute per-date in parallel; cache hits make most calls cheap.
        all_det: list[dict] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_compute_detections_payload, ds): ds for ds in dates}
            for future in as_completed(futures):
                ds = futures[future]
                try:
                    payload = future.result()
                except Exception:
                    continue
                for det in payload.get("detections", []) or []:
                    if pub_hosts is not None and det.get("host_key") not in pub_hosts:
                        continue
                    enriched = dict(det)
                    enriched["date"] = ds
                    all_det.append(enriched)

        all_det.sort(key=lambda x: x.get("meteor_time", ""))
        return jsonify({
            "from": date_from,
            "to": date_to,
            "count": len(all_det),
            "detections": all_det,
        })

    # ── Aggregated live-feed API ──────────────────────────────────────────

    @app.route("/api/live-feed")
    def api_live_feed():
        """For each online station, fetch the most recent 5 chunks per camera."""
        results = {}

        def fetch_station(host_key: str, station: StationConfig):
            station_data = {
                "label": station.label,
                "cameras": {},
                "online": False,
            }
            status = cache.get_status(host_key)
            if not status or not status.get("online"):
                return host_key, station_data

            station_data["online"] = True
            for cam in station.cameras:
                try:
                    # Get latest night date first
                    nights = station_get_raw(
                        config, tunnels, host_key,
                        f"/api/nights/{cam.code}",
                        timeout=10,
                    )
                    if not nights or not isinstance(nights, list):
                        station_data["cameras"][cam.code] = []
                        continue
                    latest_date = nights[0]
                    # Fetch chunks for that date
                    raw = station_get_raw(
                        config, tunnels, host_key,
                        f"/api/chunks/{cam.code}/{latest_date}",
                        timeout=15,
                    )
                    # New station API returns {"morning_done": bool, "chunks": [...]}
                    # Old API returns a plain list -- handle both.
                    if isinstance(raw, dict) and "chunks" in raw:
                        chunks = raw["chunks"]
                    elif isinstance(raw, list):
                        chunks = raw
                    else:
                        chunks = []
                    # Add date field to each chunk and take last 5
                    for c in chunks:
                        c["date"] = latest_date
                    station_data["cameras"][cam.code] = chunks[-5:]
                except Exception:
                    station_data["cameras"][cam.code] = []
            return host_key, station_data

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(fetch_station, key, station): key
                for key, station in config.stations.items()
            }
            for future in as_completed(futures):
                try:
                    host_key, data = future.result()
                    results[host_key] = data
                except Exception:
                    pass

        return _json_cached(results, max_age=10)
