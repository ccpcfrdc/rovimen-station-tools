"""Highlights API routes -- top-10 meteors, aggregation, and media serving."""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

from http_caching import _json_cached
from security import public_route

logger = logging.getLogger(__name__)


def register_highlights_routes(app: Flask, config, cache, tunnels) -> None:

    # Lazy-import siblings that live in the same dashboard package dir.
    import gmn_data

    # ``limiter`` is stashed on the app by create_app().
    limiter = app._limiter  # type: ignore[attr-defined]

    def _abort_if_camera_not_public_for_anon(camera: str) -> None:
        """404 an anonymous request for a camera whose owning station is not
        ``public: true``.

        The highlights media routes key on camera code, not host_key, so this
        maps the code back to its owning station(s) and applies the same
        per-station ``public`` filter the rest of the anon surface enforces
        (PR #628). Logged-in accounts keep fleet-wide read access. Fails
        closed: an unknown camera code (no owner in the registry) is 404'd for
        anon, matching ``station_is_public_for_request``'s unknown-key rule."""
        from auth import _host_keys_for_camera, is_anonymous, \
            station_is_public_for_request

        if not is_anonymous():
            return
        owners = _host_keys_for_camera(config, camera)
        if not owners or not any(
            station_is_public_for_request(config, hk) for hk in owners
        ):
            abort(404)

    # _compute_detections_payload is a closure defined in create_app() and
    # stashed on the app object so extracted route modules can reach it.
    _compute_detections_payload = app._compute_detections_payload  # type: ignore[attr-defined]

    # Endpoint-level cache for the (start, end) aggregation.  The per-date
    # detections payload is already cached, but this route additionally re-runs
    # the GMN event join, orbit tallies, coverage geometry and the top-10 sort
    # on every call.  Over a multi-week range that is slow enough to exhaust
    # nginx's upstream timeout and surface as a 503 -- and because gunicorn runs
    # one gthread worker, a slow recompute also stalls sibling requests (e.g.
    # thumbnails) sharing the thread pool.  Caching the assembled payload makes
    # repeat loads instant.  Threads share this dict, so guard it with a lock.
    _highlights_cache: "dict[tuple[str, str, bool], tuple[float, dict]]" = {}
    _highlights_lock = threading.Lock()
    _HIGHLIGHTS_MAX_ENTRIES = 64

    def _highlights_browser_max_age(end_str: str) -> int:
        """Past-only ranges are immutable (long cache); ranges touching today
        still accrue detections (short cache)."""
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return 1800 if end_str < today_iso else 120

    # ── Highlights data endpoint ─────────────────────────────────────────

    @app.route("/api/highlights/data")
    @public_route(page="highlights")
    def highlights_data():
        """Aggregate detections + events + orbits across an arbitrary date range.

        Query params: start=YYYY-MM-DD, end=YYYY-MM-DD (inclusive).
        Returns stats summary + top-10 most impressive meteors.

        Public under the "highlights" page toggle: the payload is aggregate
        network stats plus a curated top-10 of GMN-confirmed multi-station
        meteors (time, station label, camera code, shower, magnitude,
        duration, and clip/stack URLs). No station IPs, cam_ip, host paths,
        or per-user data — every field is already surfaced on the public
        media/detection surface, so anon exposure adds nothing sensitive.
        """
        import coverage as coverage_mod
        import platepar_store as platepar_store_mod
        from auth import is_anonymous

        start_str = request.args.get("start", "")
        end_str   = request.args.get("end", "")
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d").date()
            end   = datetime.strptime(end_str,   "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "invalid dates; expected YYYY-MM-DD"}), 400
        if start > end:
            return jsonify({"error": "start must be <= end"}), 400
        if (end - start).days > 365:
            return jsonify({"error": "date range too large; maximum is 365 days"}), 400

        # Anonymous public visitors (highlights page toggled on) only ever see
        # detections from ``public: true`` stations — same opt-in set as the
        # /media/v1 and /api/stations surfaces (PR #628). Logged-in accounts
        # keep the full fleet. The filter runs on camera code because that's
        # what detections carry; anything not owned by a known public station
        # fails closed (dropped). The cache key includes ``anon`` so an admin
        # payload (full fleet) can never be served to an anonymous caller.
        anon = is_anonymous()
        _public_cams: set[str] | None = None
        if anon:
            _public_cams = {
                c.code.upper()
                for hk, st in config.stations.items()
                if st.public
                for c in st.cameras
            }

        def _visible_to_request(det: dict) -> bool:
            if not anon:
                return True
            cam = (det.get("camera") or "").upper()
            return bool(cam) and cam in (_public_cams or set())

        ttl = _highlights_browser_max_age(end_str)
        cache_key = (start_str, end_str, anon)
        now_mono = time.monotonic()
        with _highlights_lock:
            hit = _highlights_cache.get(cache_key)
            if hit is not None and now_mono < hit[0]:
                return _json_cached(hit[1], max_age=ttl)

        # Build chronological date list
        dates = []
        cur = start
        while cur <= end:
            dates.append(cur)
            cur += timedelta(days=1)

        all_dets       = []   # formatted detection dicts
        dual_events    = 0
        shower_counts  = {}
        monthly_det    = {}   # YYYY-MM -> int
        monthly_ev     = {}   # YYYY-MM -> int

        # Matches HHMMSS in filenames like RO000M_20260601_221530_color.mkv or _221530.mp4
        _CHUNK_TIME_RE = re.compile(r"_(\d{6})(?:_|\.)")

        def _fmt_det(det, date_iso):
            cam      = det.get("cam") or det.get("camera")
            filename = det.get("filename")
            if not (cam and filename):
                return None
            rms       = det.get("rms") or {}
            stack     = det.get("stack")
            peak_mag  = rms.get("mag_apparent")
            if peak_mag is None:
                peak_mag = rms.get("mag_absolute")
            # Use internal routes -- no public-station gate, auth-protected by before_request
            clip_url  = f"/api/highlights/clip/{cam}/{date_iso}/{filename}"
            stack_url = f"/api/highlights/stack/{cam}/{date_iso}/{stack}" if stack else None

            # Compute detection offset within clip from filename chunk-time + meteor_time
            detection_offset_s = None
            mt_str = det.get("meteor_time")
            m = _CHUNK_TIME_RE.search(filename)
            if m and mt_str:
                try:
                    chk = m.group(1)
                    chk_secs = int(chk[:2]) * 3600 + int(chk[2:4]) * 60 + int(chk[4:6])
                    mt = datetime.fromisoformat(mt_str.replace(" ", "T"))
                    mt_secs = mt.hour * 3600 + mt.minute * 60 + mt.second + mt.microsecond / 1e6
                    diff = mt_secs - chk_secs
                    if diff < -43200:
                        diff += 86400
                    detection_offset_s = round(max(0.0, diff), 2)
                except (ValueError, AttributeError):
                    pass

            return {
                "time_utc":            mt_str,
                "station_label":       det.get("station_label"),
                "camera":              cam,
                "filename":            filename,
                "date":                date_iso,
                "shower":              rms.get("shower"),
                "peak_magnitude":      peak_mag,
                "duration_s":          rms.get("duration_s"),
                "detection_offset_s":  detection_offset_s,
                "clip_url":            clip_url,
                "stack_url":           stack_url,
                "thumbnail_url":       stack_url,
            }

        def _norm_time(ts: str) -> str:
            """Normalize ISO/space-separated timestamp to 19-char YYYY-MM-DDTHH:MM:SS."""
            return ts.replace(" ", "T")[:19] if ts else ""

        def _time_diff_s(t1: str, t2: str) -> float:
            try:
                d1 = datetime.fromisoformat(t1)
                d2 = datetime.fromisoformat(t2)
                return abs((d1 - d2).total_seconds())
            except (ValueError, TypeError):
                return float("inf")

        def fetch_date(d):
            date_yyyymmdd = d.strftime("%Y%m%d")
            date_iso      = d.strftime("%Y-%m-%d")
            month_str     = d.strftime("%Y-%m")
            payload = _compute_detections_payload(date_yyyymmdd)
            raw     = payload.get("detections", []) or []
            fmted   = [r for r in (_fmt_det(det, date_iso) for det in raw)
                       if r and _visible_to_request(r)]

            # GMN trajectory data -- multi-station events with magnitude/shower/duration
            gmn     = gmn_data.events_for_date(date_iso)
            ev_cnt  = 0
            # station_code -> [(norm_time, peak_mag, duration_s, shower)]
            gmn_lookup: dict[str, list[tuple]] = {}
            for e in (gmn.get("events") or []):
                stations = e.get("stations") or []
                if len(stations) >= 2:
                    ev_cnt += 1
                ev_t = _norm_time(e.get("time", ""))
                for sta in stations:
                    gmn_lookup.setdefault(sta, []).append(
                        (ev_t, e.get("peak_mag"), e.get("duration_s"), e.get("shower"))
                    )

            # Enrich detections with GMN trajectory values and mark confirmed ones.
            # GMN magnitude is the calculated absolute magnitude (multi-station),
            # more reliable than apparent magnitude from a single camera.
            for r in fmted:
                cam = r.get("camera") or ""
                mt  = _norm_time(r.get("time_utc") or "")
                r["gmn_confirmed"] = False
                for ev_t, peak_mag, dur, shower in gmn_lookup.get(cam, []):
                    if mt and _time_diff_s(mt, ev_t) <= 3:
                        r["gmn_confirmed"]  = True
                        r["peak_magnitude"] = peak_mag   # authoritative absolute mag
                        r["duration_s"]     = dur
                        if shower is not None:
                            r["shower"] = shower
                        # Propagate GMN event time if the archive lock had no meteor_time
                        if not r.get("time_utc"):
                            r["time_utc"] = ev_t
                        # Derive detection_offset_s from GMN event time when archive
                        # lock didn't carry meteor_time (older lock format)
                        if r.get("detection_offset_s") is None and ev_t:
                            fname = r.get("filename", "")
                            m2 = _CHUNK_TIME_RE.search(fname)
                            if m2:
                                try:
                                    chk = m2.group(1)
                                    chk_secs = (int(chk[:2]) * 3600 + int(chk[2:4]) * 60
                                                + int(chk[4:6]))
                                    gmt = datetime.fromisoformat(ev_t)
                                    gmt_secs = (gmt.hour * 3600 + gmt.minute * 60
                                                + gmt.second + gmt.microsecond / 1e6)
                                    diff = gmt_secs - chk_secs
                                    if diff < -43200:
                                        diff += 86400
                                    r["detection_offset_s"] = round(max(0.0, diff), 2)
                                except (ValueError, AttributeError):
                                    pass
                        break

            return month_str, fmted, ev_cnt

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(fetch_date, d): d for d in dates}
            for fut in as_completed(futures):
                try:
                    month_str, fmted, ev_cnt = fut.result()
                except Exception:
                    continue
                all_dets.extend(fmted)
                dual_events += ev_cnt
                monthly_det[month_str] = monthly_det.get(month_str, 0) + len(fmted)
                monthly_ev[month_str]  = monthly_ev.get(month_str, 0) + ev_cnt
                for det in fmted:
                    sh = (det.get("shower") or "").upper()
                    if sh:
                        shower_counts[sh] = shower_counts.get(sh, 0) + 1

        # Orbit counts -- read daily GMN orbit tallies for each month in range
        months_in_range: set[tuple[int, int]] = set()
        cur = start.replace(day=1)
        while cur <= end:
            months_in_range.add((cur.year, cur.month))
            if cur.month == 12:
                cur = cur.replace(year=cur.year + 1, month=1)
            else:
                cur = cur.replace(month=cur.month + 1)

        new_orbits = 0
        for year, month in months_in_range:
            for date_str, count in gmn_data._daily_counts_for_month(year, month).items():
                try:
                    d = datetime.strptime(date_str, "%Y%m%d").date()
                    if start <= d <= end:
                        new_orbits += count
                except ValueError:
                    pass

        # Orbits this calendar month (1st of current month -> today UTC)
        today_utc   = datetime.now(timezone.utc).date()
        month_start = today_utc.replace(day=1)
        orbits_month_to_date = 0
        for date_str, count in gmn_data._daily_counts_for_month(today_utc.year, today_utc.month).items():
            try:
                d = datetime.strptime(date_str, "%Y%m%d").date()
                if month_start <= d <= today_utc:
                    orbits_month_to_date += count
            except ValueError:
                pass

        # Active cameras + locations from station cache
        active_stations = 0
        active_cameras  = 0
        for hk, st_cfg in config.stations.items():
            st = cache.get_status(hk) or {}
            if st.get("online"):
                active_stations += 1
                active_cameras  += len(st_cfg.cameras)

        # 90 km dual-station coverage (static network geometry)
        try:
            cov = coverage_mod.get_coverage_stats(platepar_store_mod.get_all())
            coverage_90km = round(cov.get("dual_coverage_pct", 0), 1)
        except Exception:
            coverage_90km = None

        # Main shower (prefer non-sporadic)
        main_shower = "SPO"
        non_spo = {k: v for k, v in shower_counts.items() if k not in ("SPO", "")}
        if non_spo:
            main_shower = max(non_spo, key=non_spo.get)
        elif shower_counts:
            main_shower = max(shower_counts, key=shower_counts.get)

        # Top 10: GMN-confirmed multi-station only (absolute magnitude, clip + stack required)
        candidates = [
            d for d in all_dets
            if d.get("gmn_confirmed")
            and d.get("clip_url")
            and (d.get("stack_url") or d.get("thumbnail_url"))
        ]
        candidates.sort(key=lambda d: (
            d["peak_magnitude"] if d["peak_magnitude"] is not None else float("inf"),
            -(d.get("duration_s") or 0),
        ))
        top10 = candidates[:10]

        monthly_totals = [
            {
                "month":      m,
                "detections": monthly_det.get(m, 0),
                "events":     monthly_ev.get(m, 0),
            }
            for m in sorted(set(list(monthly_det) + list(monthly_ev)))
        ]

        payload = {
            "start":                 start_str,
            "end":                   end_str,
            "total_detections":      len(all_dets),
            "dual_station_events":   dual_events,
            "new_orbits":            new_orbits,
            "orbits_month_to_date":  orbits_month_to_date,
            "main_shower":           main_shower,
            "active_cameras":        active_cameras,
            "active_locations":      active_stations,
            "coverage_90km":         coverage_90km,
            "monthly_totals":        monthly_totals,
            "top10":                 top10,
        }

        with _highlights_lock:
            # Evict expired, then oldest, to cap memory.
            if len(_highlights_cache) >= _HIGHLIGHTS_MAX_ENTRIES:
                stale = [k for k, v in _highlights_cache.items() if v[0] <= now_mono]
                for k in stale:
                    _highlights_cache.pop(k, None)
                while len(_highlights_cache) >= _HIGHLIGHTS_MAX_ENTRIES:
                    _highlights_cache.pop(next(iter(_highlights_cache)), None)
            _highlights_cache[cache_key] = (time.monotonic() + ttl, payload)

        return _json_cached(payload, max_age=ttl)

    # ── Media serving endpoints ──────────────────────────────────────────

    @app.route("/api/highlights/shortclip/<camera>/<date>/<filename>")
    @public_route(page="highlights")
    @limiter.limit("5 per minute")
    def highlights_shortclip(camera: str, date: str, filename: str):
        """Trim and serve a clip from the archive for the highlights page.

        Query params: ss=<seek_seconds> t=<duration_seconds>
        Returns an MP4 attachment via ffmpeg stream copy from storagebox.
        """
        import subprocess as _sp, tempfile as _tf, os as _os
        import public_api as _pub

        if not re.match(r"^[A-Z0-9]{1,16}$", camera, re.IGNORECASE):
            abort(400)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            abort(400)
        if not re.match(r"^[A-Za-z0-9._-]+\.(mkv|mp4)$", filename):
            abort(400)
        _abort_if_camera_not_public_for_anon(camera)

        src = _pub._resolve_media_path(camera, date, filename, "meteors")
        if src is None:
            abort(404)

        try:
            ss = max(0.0, float(request.args.get("ss", 0)))
            t  = min(120.0, max(0.5, float(request.args.get("t", 20))))
        except ValueError:
            abort(400)

        fd, tmp = _tf.mkstemp(suffix=".mp4")
        _os.close(fd)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{ss:.3f}", "-i", str(src),
            "-t", f"{t:.3f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-an", "-movflags", "+faststart",
            "-y", tmp,
        ]
        try:
            result = _sp.run(cmd, timeout=120)
        except _sp.TimeoutExpired:
            _os.unlink(tmp)
            abort(504)
        if result.returncode != 0:
            _os.unlink(tmp)
            abort(500)
        stem = Path(filename).stem.replace("_color", "")
        resp = send_file(tmp, mimetype="video/mp4", as_attachment=True,
                         download_name=f"{stem}_clip.mp4")
        resp.call_on_close(lambda: _os.unlink(tmp) if _os.path.exists(tmp) else None)
        return resp

    @app.route("/api/highlights/clip/<camera>/<date>/<filename>")
    @public_route(page="highlights")
    def highlights_clip(camera: str, date: str, filename: str):
        """Serve a full clip from the archive for the highlights modal.

        Public under the "highlights" page toggle. Only clips referenced by
        the curated /api/highlights/data top-10 are ever linked, and the
        camera/date/filename are strictly validated below before any archive
        path is resolved, so this cannot enumerate arbitrary station media.
        """
        import public_api as _pub
        if not re.match(r"^[A-Z0-9]{1,16}$", camera, re.IGNORECASE):
            abort(400)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            abort(400)
        if not re.match(r"^[A-Za-z0-9._-]+\.(mkv|mp4)$", filename):
            abort(400)
        _abort_if_camera_not_public_for_anon(camera)
        path = _pub._resolve_media_path(camera, date, filename, "meteors")
        if path is None:
            abort(404)
        mime = "video/x-matroska" if filename.endswith(".mkv") else "video/mp4"
        resp = send_file(path, mimetype=mime, conditional=True)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    @app.route("/api/highlights/stack/<camera>/<date>/<filename>")
    @public_route(page="highlights")
    def highlights_stack(camera: str, date: str, filename: str):
        """Serve a stack image from the archive for the highlights modal.

        Public under the "highlights" page toggle; same validated-path
        rationale as ``highlights_clip``.
        """
        import public_api as _pub
        import mimetypes as _mt
        if not re.match(r"^[A-Z0-9]{1,16}$", camera, re.IGNORECASE):
            abort(400)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            abort(400)
        if not re.match(r"^[A-Za-z0-9._-]+\.(webp|jpg|png)$", filename):
            abort(400)
        _abort_if_camera_not_public_for_anon(camera)
        for subdir in ("stacks", "meteors"):
            path = _pub._resolve_media_path(camera, date, filename, subdir)
            if path is not None:
                mime = _mt.guess_type(filename)[0] or "image/webp"
                resp = send_file(path, mimetype=mime, conditional=True)
                resp.headers["Cache-Control"] = "private, max-age=3600"
                return resp
        abort(404)
