"""Archive and video-cache routes -- storagebox reads, MKV remux, cached-video proxy.

Extracted from rovimen_dashboard.py.  All routes preserved verbatim -- same URLs,
same behaviour, same decorators.  Wired in from ``create_app()`` via
``register_archive_routes``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, redirect, request, send_file

from cache_store import (
    ARCHIVE_PATH,
    CACHE_PATH,
    CACHE_TTL_HOURS,
    NGINX_ACCEL,
    _disk_cache_write,
    _kick_cache_refresh,
    _drop_future_dates,
    _thumb_cache_path,
)

from auth import (
    require_station,
    _session_role,
    _has_camera_access,
    _host_keys_for_camera,
    is_anonymous,
    station_is_public_for_request,
)
from http_caching import _json_cached
from route_helpers import compute_detection_offset, validate_media_params, media_url
from security import public_route
from station_client import (
    station_url,
    station_get_raw,
    _session_for_url,
    _streaming_proxy,
    _with_sshfs_timeout,
)
from tunnels import _TunnelDown

logger = logging.getLogger(__name__)

_media_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-remux")

# Bounds concurrent stitch-multi (filter_complex libx264) jobs. The work runs
# on ``_media_executor`` (2 threads) but a semaphore also caps how many request
# threads can be *waiting* on a stitch at once, so a burst of stitch POSTs
# can't pin the whole gunicorn gthread pool for 90 s each.
_STITCH_CONCURRENCY = int(os.environ.get("ROVIMEN_STITCH_CONCURRENCY", "2"))
_stitch_sem = threading.BoundedSemaphore(_STITCH_CONCURRENCY)


def _run_ffmpeg_capped(cmd: list[str], timeout: float) -> tuple[int, bytes, bool]:
    """Run an ffmpeg command, killing it on timeout.

    Returns ``(returncode, stderr, timed_out)``. Intended to be submitted to
    ``_media_executor`` so the heavy libx264 work runs on a bounded pool
    instead of directly on the gunicorn request thread.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stderr, False
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return -1, b"", True


# ---------------------------------------------------------------------------
# RMS file parsers (read from archive rms/ subdir)
#
# The implementations now live in ``rms_parse`` (stdlib-only) so non-Flask
# code can reuse them. Re-exported here under their original underscore
# names to keep existing importers (routes.detections, routes.media) working.
# ---------------------------------------------------------------------------

from rms_parse import (  # noqa: E402
    parse_radiants_txt as _parse_radiants_txt,
    parse_ftpdetectinfo as _parse_ftpdetectinfo,
)


def register_archive_routes(
    app: Flask,
    config,
    tunnels,
    cache,
    *,
    archive_idx,
    nights_full_cache: dict[str, tuple[float, list]],
    nights_full_ttl: float,
    prefetch_archive_thumbs=None,
) -> None:
    def _validate_archive_path(*parts: str) -> None:
        for p in parts:
            if ".." in p or "/" in p:
                abort(400)

    # ── Archive endpoints (read from /srv/rovimen/archive/) ────────────

    @app.route("/api/archive/cameras")
    def api_archive_cameras():
        cameras = archive_idx.cameras()
        if _session_role() != "admin":
            cameras = [c for c in cameras if _has_camera_access(config, c)]
        return jsonify(cameras)

    @app.route("/api/archive/nights/<camera>")
    def api_archive_nights(camera: str):
        _validate_archive_path(camera)
        if not re.match(r"^[A-Z0-9]+$", camera, re.IGNORECASE):
            abort(400)
        if not _has_camera_access(config, camera):
            abort(403)
        nights = archive_idx.nights(camera)
        return jsonify(_drop_future_dates(nights))

    @app.route("/api/archive/meteors/<camera>/<date>")
    def api_archive_meteors(camera: str, date: str):
        _validate_archive_path(camera, date)
        if not re.match(r"^[A-Z0-9]+$", camera):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        if not _has_camera_access(config, camera):
            abort(403)
        ni = archive_idx.night_files(camera, date)
        if ni is None:
            return jsonify([])
        meteor_names = set(ni.meteor_files)
        results = []
        for name in ni.meteor_files:
            if not (name.endswith(".mkv") or name.endswith(".mp4")):
                continue
            stack_name = name.replace("_color.mkv", "_stack.webp").replace(".mp4", "_stack.webp")
            if stack_name in meteor_names:
                stack, stack_subdir = stack_name, "meteors"
            elif stack_name in ni.stack_files:
                stack, stack_subdir = stack_name, "stacks"
            else:
                stack, stack_subdir = None, None
            results.append({
                "filename": name,
                "size_mb": None,
                "stack": stack,
                "stack_subdir": stack_subdir,
            })
        return jsonify(results)

    @app.route("/api/archive/indexed/<camera>/<date>")
    def api_archive_indexed(camera: str, date: str):
        """Return set of filenames stored in the archive for a camera/date."""
        _validate_archive_path(camera, date)
        if not re.match(r"^[A-Z0-9]+$", camera):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        if not _has_camera_access(config, camera):
            abort(403)
        return jsonify(sorted(archive_idx.all_night_files(camera, date)))

    @app.route("/api/archive/rms-detections/<camera>/<date>")
    @require_station
    def api_archive_rms_detections(camera: str, date: str):
        """Parse FTPdetectinfo + radiants from archive rms/ subdir and return detections."""
        _validate_archive_path(camera, date)
        if not re.match(r"^[A-Z0-9]+$", camera):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        if not _has_camera_access(config, camera):
            abort(403)

        def _parse() -> dict:
            rms_dir = ARCHIVE_PATH / camera / date / "rms"
            if not rms_dir.is_dir():
                return {"camera": camera, "date": date, "detections": []}
            all_radiants: list[dict] = []
            all_ftp: list[dict] = []
            for f in sorted(rms_dir.glob("*_radiants.txt")):
                all_radiants.extend(_parse_radiants_txt(f))
            for f in sorted(rms_dir.glob("FTPdetectinfo_*.txt")):
                if "_unfiltered" not in f.name and "_backup" not in f.name:
                    all_ftp.extend(_parse_ftpdetectinfo(f))
            all_radiants.sort(key=lambda r: r["jd"])
            all_ftp.sort(key=lambda f: f["ff_file"])
            detections = []
            for i, rad in enumerate(all_radiants):
                det = {**rad}
                if i < len(all_ftp):
                    det["ff_file"] = all_ftp[i]["ff_file"]
                    det["duration_s"] = all_ftp[i]["duration_s"]
                detections.append(det)
            for j in range(len(all_radiants), len(all_ftp)):
                detections.append({
                    "time_utc": None,
                    "shower": None,
                    "mag_apparent": None,
                    "ff_file": all_ftp[j]["ff_file"],
                    "duration_s": all_ftp[j]["duration_s"],
                })
            return {"camera": camera, "date": date, "detections": detections}

        return _json_cached(_parse(), max_age=300)

    @app.route("/api/archive/file/<camera>/<date>/<subdir>/<filename>")
    def api_archive_file(camera: str, date: str, subdir: str, filename: str):
        _validate_archive_path(camera, date, subdir, filename)
        if not re.match(r"^[A-Z0-9]+$", camera):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        if subdir not in ("meteors", "timelapse", "rms", "stacks"):
            abort(400)
        if not re.match(r"^[\w._-]+\.(mkv|mp4|webp|txt|json)$", filename):
            abort(400)
        if not _has_camera_access(config, camera):
            abort(403)

        is_webp = filename.endswith(".webp")

        if is_webp:
            owners = _host_keys_for_camera(config, camera)
            if owners:
                host_key = owners[0]
                cache_file = _thumb_cache_path(host_key, camera, date, filename)
                if cache_file.exists():
                    if NGINX_ACCEL:
                        accel = "/internal/thumb_cache/" + "/".join(
                            [host_key, camera, date, filename]
                        )
                        return Response(headers={
                            "X-Accel-Redirect": accel,
                            "Content-Type": "image/webp",
                            "Cache-Control": "public, max-age=2592000, immutable",
                        })
                    resp = send_file(cache_file, mimetype="image/webp",
                                     conditional=True)
                    resp.headers["Cache-Control"] = (
                        "public, max-age=2592000, immutable"
                    )
                    return resp
                if prefetch_archive_thumbs:
                    threading.Thread(
                        target=prefetch_archive_thumbs,
                        args=(host_key, camera, date),
                        daemon=True,
                    ).start()

        path = ARCHIVE_PATH / camera / date / subdir / filename
        # Guard the SSHFS stat so a stalled storagebox mount can't pin the
        # worker thread on .exists(). A timeout returns the sentinel and we
        # report 503 (mount stalled) rather than masking it as a 404.
        _MISS = object()
        exists = _with_sshfs_timeout(path.exists, timeout=5.0, default=_MISS)
        if exists is _MISS:
            abort(503)
        if not exists:
            abort(404)
        dl = request.args.get("download")
        resp = send_file(path, conditional=True, as_attachment=bool(dl),
                         download_name=filename if dl else None)
        if is_webp:
            resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
        return resp

    # ── nights_full (per-camera deep walk) ───────────────────────────────

    def _build_nights_full(camera: str) -> list:
        """Build the per-night payload from the archive index.

        Both directory listings and state.json data come from the
        in-memory archive index -- no SSHFS I/O at request time.
        """
        nights = _drop_future_dates(archive_idx.nights(camera))
        if not nights:
            return []
        result = []
        for date in nights:
            ni = archive_idx.night_files(camera, date)
            if ni is None:
                continue
            night_state_chunks: dict = ni.state_chunks or {}
            meteor_names = set(ni.meteor_files)
            meteors_entries: list[dict] = []
            for name in ni.meteor_files:
                if not (name.endswith(".mkv") or name.endswith(".mp4")):
                    continue
                stack_name = name.replace("_color.mkv", "_stack.webp").replace(".mp4", "_stack.webp")
                if stack_name in meteor_names:
                    stack, stack_subdir = stack_name, "meteors"
                elif stack_name in ni.stack_files:
                    stack, stack_subdir = stack_name, "stacks"
                else:
                    stack, stack_subdir = None, None
                mt_str = None
                det_offset_s = None
                lock = night_state_chunks.get(name, {}).get("lock")
                if lock:
                    mt_str = lock.get("meteor_time")
                    det_time_str = lock.get("detection_time")
                    det_offset_s = compute_detection_offset(name, mt_str, det_time_str)
                meteors_entries.append({
                    "filename": name,
                    "stack": stack,
                    "stack_subdir": stack_subdir,
                    "detection_offset_s": det_offset_s,
                    "meteor_time": mt_str,
                })
            timelapse = None
            timelapse_stack = None
            for fname in ni.timelapse_files:
                if fname.endswith(".mp4") and timelapse is None:
                    timelapse = fname
                elif fname.endswith("_night_stack.webp") and timelapse_stack is None:
                    timelapse_stack = fname
            result.append({"date": date, "meteors": meteors_entries,
                           "timelapse": timelapse, "timelapse_size_mb": None,
                           "timelapse_stack": timelapse_stack})
        return result

    def _refresh_nights_full(camera: str) -> None:
        """Run the deep walk and update both the in-memory and disk caches.
        Used by the live endpoint on stale-revalidate and by the startup
        pre-warm. Idempotent -- coalesced by `_kick_cache_refresh`."""
        fresh = _with_sshfs_timeout(
            lambda: _build_nights_full(camera), timeout=60.0, default=None,
        )
        if fresh is not None:
            nights_full_cache[camera] = (time.monotonic(), fresh)
            _disk_cache_write("nights_full", camera, fresh)

    # Expose to startup code in rovimen_dashboard.py that pre-warms caches.
    app._refresh_nights_full = _refresh_nights_full  # type: ignore[attr-defined]

    @app.route("/api/archive/nights_full/<camera>")
    def api_archive_nights_full(camera: str):
        """All nights for a camera with meteors + timelapse in one call.

        Cache strategy: stale-while-revalidate. A request never blocks on
        SSHFS as long as we have *any* prior data for this camera --
        disk-persisted entries from the last process count. The user
        sees the previous result instantly and the next request within
        a few seconds picks up the freshly-walked one.
        """
        _validate_archive_path(camera)
        if not re.match(r"^[A-Z0-9]+$", camera):
            abort(400)
        if not _has_camera_access(config, camera):
            abort(403)

        cached = nights_full_cache.get(camera)
        now = time.monotonic()

        if cached and (now - cached[0]) < nights_full_ttl:
            return _json_cached(cached[1], max_age=120)

        if cached is not None:
            _kick_cache_refresh("nights_full", camera,
                                lambda c=camera: _refresh_nights_full(c))
            return _json_cached(cached[1], max_age=15)

        # Truly cold (no in-memory and no disk-loaded entry). Block once
        # for a fresh build -- subsequent requests hit the cache. 60 s
        # SSHFS ceiling prevents a hung mount from freezing the worker.
        result = _with_sshfs_timeout(
            lambda: _build_nights_full(camera), timeout=60.0, default=None,
        )
        if result is None:
            return _json_cached([], max_age=15)
        nights_full_cache[camera] = (time.monotonic(), result)
        _disk_cache_write("nights_full", camera, result)
        return _json_cached(result, max_age=120)

    # ── On-demand video caching ───────────────────────────────────────────

    @app.route("/api/cached-video/<host_key>/<station_code>/<date>/<filename>")
    @public_route(page="events")
    def api_cached_video(host_key: str, station_code: str, date: str, filename: str):
        """Serve cached video instantly, or stream from station while caching.

        Query params:
            format=mp4  Convert MKV to MP4 on the fly via ffmpeg.

        Anonymous public visitors are NOT served through the gated station
        proxy (which needs a station tunnel + a login-scoped cache). Instead,
        for a ``public: true`` station's clip we 302-redirect to the keyless
        public media surface (``/media/v1/clip/...``, served from the storage
        box archive). The media handler enforces the same per-station public
        flag and returns a clean 404 when the clip was never uploaded — which
        the video modal renders as a tidy "clip not available" state rather
        than a dead player. A clip belonging to a ``public: false`` station is
        404'd here before any redirect so its existence stays undisclosed.
        Logged-in operators keep the full station-proxy path below.
        """
        want_mp4 = request.args.get("format") == "mp4"
        if is_anonymous():
            # Fail-closed: only clips from opted-in public stations, and only a
            # locked .mkv name. Everything else is 404 (existence undisclosed).
            if not station_is_public_for_request(config, host_key):
                abort(404)
            if not re.match(r"^[\w._-]+\.mkv$", filename) or ".." in date or "/" in date:
                abort(404)
            iso_date = f"{date[0:4]}-{date[4:6]}-{date[6:8]}" if re.match(r"^\d{8}$", date) else date
            target = f"/media/v1/clip/{station_code}/{iso_date}/{filename}"
            if want_mp4:
                target += "?format=mp4"
            return redirect(target, code=302)
        validate_media_params(
            config, host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
        )
        cache_dir = CACHE_PATH / station_code / date
        cached = cache_dir / filename
        try:
            url = media_url(config, tunnels, host_key, "color_capture", station_code, date, filename)
        except _TunnelDown:
            if cached.exists():
                if want_mp4:
                    return _stream_mkv_as_mp4(cached, filename)
                return send_file(cached, conditional=True)
            return jsonify({"error": "station tunnel down", "offline": True}), 502

        if cached.exists():
            stale = False
            try:
                head = _session_for_url(url).head(url, timeout=10, allow_redirects=True)
                upstream_size = head.headers.get("Content-Length")
                if (
                    head.status_code == 200
                    and upstream_size is not None
                    and int(upstream_size) != cached.stat().st_size
                ):
                    stale = True
                    logger.info(
                        "Cache invalidated for %s/%s/%s: upstream=%s cached=%d",
                        station_code, date, filename, upstream_size, cached.stat().st_size,
                    )
            except Exception as exc:
                logger.debug("HEAD validate failed for %s, serving cached: %s", filename, exc)
            if stale:
                try:
                    cached.unlink()
                except OSError:
                    pass
            else:
                if want_mp4:
                    return _stream_mkv_as_mp4(cached, filename)
                return send_file(cached, conditional=True)
        # Stream from station. Forward Range header so the browser can seek even
        # before the file is locally cached. Only cache on full (non-range) requests.
        range_hdr = request.headers.get("Range")
        upstream_headers = {"Range": range_hdr} if range_hdr else {}
        try:
            resp = _session_for_url(url).get(
                url, headers=upstream_headers, timeout=120, stream=True,
            )
            resp.raise_for_status()
            content_length = resp.headers.get("Content-Length")
            out_headers = {
                "Content-Type": "video/x-matroska",
                "Accept-Ranges": "bytes",
            }
            if content_length:
                out_headers["Content-Length"] = content_length
            if resp.headers.get("Content-Range"):
                out_headers["Content-Range"] = resp.headers["Content-Range"]

            if range_hdr:
                return Response(
                    _streaming_proxy(resp),
                    status=resp.status_code,
                    headers=out_headers,
                )

            # Full request -- stream to browser AND write to cache simultaneously
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix(".tmp")
            expected_size: int | None
            try:
                expected_size = int(content_length) if content_length else None
            except (TypeError, ValueError):
                expected_size = None

            def stream_and_cache():
                clean_exit = False
                try:
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=65536):
                            fh.write(chunk)
                            yield chunk
                    clean_exit = True
                except GeneratorExit:
                    tmp.unlink(missing_ok=True)
                    return
                except Exception:
                    pass
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
                try:
                    if not clean_exit:
                        tmp.unlink(missing_ok=True)
                        return
                    if expected_size is not None:
                        try:
                            actual_size = tmp.stat().st_size
                        except OSError:
                            tmp.unlink(missing_ok=True)
                            return
                        if actual_size != expected_size:
                            logger.warning(
                                "Refusing to promote truncated cache file %s "
                                "(expected %d bytes, got %d)",
                                tmp, expected_size, actual_size,
                            )
                            tmp.unlink(missing_ok=True)
                            return
                    tmp.rename(cached)
                except Exception:
                    tmp.unlink(missing_ok=True)

            return Response(stream_and_cache(), status=200, headers=out_headers)
        except Exception as exc:
            logger.warning("Cache fetch failed for %s: %s", filename, exc)
            # Station no longer has the file (cleaned up after archive upload).
            # Try the storagebox archive before giving up.
            for subdir in ("meteors", ""):
                archive_file = (
                    ARCHIVE_PATH / station_code / date / subdir / filename
                    if subdir
                    else ARCHIVE_PATH / station_code / date / filename
                )
                if archive_file.exists():
                    if want_mp4:
                        return _stream_mkv_as_mp4(archive_file, filename)
                    return send_file(
                        archive_file, mimetype="video/x-matroska", conditional=True,
                    )
            return jsonify({"error": "Video temporarily unavailable"}), 502

    @app.route("/api/cached-files/<station_code>/<date>")
    def api_cached_files(station_code: str, date: str):
        """Return list of cached filenames for a station/date."""
        for p in (station_code, date):
            if ".." in p or "/" in p:
                abort(400)
        if not _has_camera_access(config, station_code):
            abort(403)
        cache_dir = CACHE_PATH / station_code / date
        if not cache_dir.exists():
            return jsonify([])
        return jsonify(sorted(f.name for f in cache_dir.iterdir() if f.is_file()))

    # ── Cache cleanup (background) ────────────────────────────────────────

    def _cache_cleanup() -> None:
        while True:
            time.sleep(3600)  # every hour
            if not CACHE_PATH.exists():
                continue
            cutoff = datetime.now() - timedelta(hours=CACHE_TTL_HOURS)
            try:
                for station_dir in CACHE_PATH.iterdir():
                    if not station_dir.is_dir():
                        continue
                    for date_dir in station_dir.iterdir():
                        if not date_dir.is_dir():
                            continue
                        for f in date_dir.iterdir():
                            if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
                                f.unlink(missing_ok=True)
                        try:
                            date_dir.rmdir()
                        except OSError:
                            pass
                    try:
                        station_dir.rmdir()
                    except OSError:
                        pass
            except Exception:
                logger.exception("Cache cleanup error")

    # Skipped under ROVIMEN_DISABLE_BACKGROUND=1 (tests / E2E launcher): this is
    # a forever-sleeping daemon and each create_app would otherwise leak one.
    if os.environ.get("ROVIMEN_DISABLE_BACKGROUND", "0") != "1":
        threading.Thread(target=_cache_cleanup, daemon=True).start()

    # ── MKV to MP4 remux via thread pool ────────────────────────────────

    def _silent_unlink(path: Path) -> None:
        """Remove *path* ignoring errors -- safe for call_on_close callbacks."""
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    def _remux_to_mp4(source: Path, tmp_path: Path) -> None:
        """Run ffmpeg remux in the calling thread (submitted to pool).

        Writes a standard (non-fragmented) MP4 to *tmp_path*.  On any
        error the temp file is cleaned up so the caller never sees a
        partial file.
        """
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", str(source),
                    "-c:v", "copy", "-c:a", "copy",
                    "-movflags", "+faststart",
                    "-f", "mp4", "-y",
                    str(tmp_path),
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
            _silent_unlink(tmp_path)
            raise
        except Exception:
            _silent_unlink(tmp_path)
            raise

    # ── Multi-detection stitch ────────────────────────────────────────────

    _STITCH_CAM_RE      = re.compile(r"^[A-Z0-9]{1,16}$")
    _STITCH_DATE_RE     = re.compile(r"^\d{8}$")
    _STITCH_FILENAME_RE = re.compile(r"^[A-Za-z0-9_]+\.(mkv|mp4)$")

    @app.route("/api/stitch-multi", methods=["POST"])
    @require_station
    def api_stitch_multi():
        """Compose multiple detection clips into a single MP4.

        Body JSON:
          videos:       list of {cam, date, filename, offset}
          layout:       "grid" | "focused"
          focused_idx:  int (index into videos, -1 = none)
          master_start: float (seconds relative to detection, typically -winPre)
          master_end:   float (seconds relative to detection, typically +winPost)

        Returns an MP4 attachment.  Runs FFmpeg synchronously — intended for
        short clips (< 15 s), times out at 90 s.
        """
        body = request.get_json(force=True, silent=True) or {}
        videos      = body.get("videos") or []
        layout      = body.get("layout", "grid")
        focused_idx = int(body.get("focused_idx") or -1)
        master_start = float(body.get("master_start", -5))
        master_end   = float(body.get("master_end",    5))

        if not videos or len(videos) > 8:
            return jsonify({"error": "need 1–8 videos"}), 400

        # Validate and resolve each clip path
        clips: list[tuple[Path, float, float]] = []  # (path, ss, duration)
        archive_root = ARCHIVE_PATH.resolve()
        for v in videos:
            cam      = (v.get("cam") or "")
            date     = (v.get("date") or "")
            filename = (v.get("filename") or "")
            offset   = float(v.get("offset") or 0)
            if not _STITCH_CAM_RE.match(cam) \
               or not _STITCH_DATE_RE.match(date) \
               or not _STITCH_FILENAME_RE.match(filename):
                return jsonify({"error": f"invalid clip: {cam}/{date}/{filename}"}), 400
            clip_path = ARCHIVE_PATH / cam / date / "meteors" / filename
            try:
                if not clip_path.resolve().is_relative_to(archive_root):
                    return jsonify({"error": "path escape"}), 400
            except (ValueError, OSError):
                return jsonify({"error": "path resolve failed"}), 400
            if not clip_path.exists():
                return jsonify({"error": f"not found: {cam}/{date}/{filename}"}), 404
            ss       = max(0.0, offset + master_start)
            duration = max(0.5, offset + master_end - ss)
            clips.append((clip_path, ss, duration))

        n = len(clips)

        # --- Build FFmpeg filter_complex ---
        # Grid: all clips scaled to same height (360px), hstacked
        # Focused: focused clip at 540px, strip clips at 150px, vstacked
        GRID_H    = 360
        FOCUS_H   = 540
        STRIP_H   = 150

        if layout == "focused" and 0 <= focused_idx < n:
            # Reorder: focused first, then strip
            strip_clips = [c for i, c in enumerate(clips) if i != focused_idx]
            ordered = [clips[focused_idx]] + strip_clips
            n_strip = len(ordered) - 1
            OUT_W   = 960  # fixed output width — focused fills it, strip divides it
            STRIP_W = (OUT_W // n_strip) if n_strip > 0 else OUT_W
            # pad helper: scale to target size with letterbox + black pad
            def _pad(tag_in: str, w: int, h: int, tag_out: str) -> str:
                return (f"[{tag_in}]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black[{tag_out}]")

            filter_parts: list[str] = []
            inp_args: list[str] = []
            for i, (path, ss, dur) in enumerate(ordered):
                inp_args += ["-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(path)]

            filter_parts.append(_pad("0:v", OUT_W, FOCUS_H, "fv"))
            for i in range(1, len(ordered)):
                filter_parts.append(_pad(f"{i}:v", STRIP_W, STRIP_H, f"sv{i}"))

            if n_strip > 1:
                strip_inputs = "".join(f"[sv{i}]" for i in range(1, len(ordered)))
                filter_parts.append(f"{strip_inputs}hstack=inputs={n_strip}[strip]")
                # Pad strip to OUT_W in case n_strip doesn't divide evenly
                filter_parts.append(_pad("strip", OUT_W, STRIP_H, "strip_p"))
                filter_parts.append("[fv][strip_p]vstack[out]")
            elif n_strip == 1:
                filter_parts.append(_pad("sv1", OUT_W, STRIP_H, "strip_p"))
                filter_parts.append("[fv][strip_p]vstack[out]")
            else:
                filter_parts.append("[fv]copy[out]")
        else:
            # Grid: all same height
            inp_args = []
            filter_parts = []
            for i, (path, ss, dur) in enumerate(clips):
                inp_args += ["-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(path)]
                filter_parts.append(f"[{i}:v]scale=-2:{GRID_H}[v{i}]")
            if n > 1:
                vstack_inputs = "".join(f"[v{i}]" for i in range(n))
                filter_parts.append(f"{vstack_inputs}hstack=inputs={n}[out]")
            else:
                filter_parts.append("[v0]copy[out]")

        filter_str = ";".join(filter_parts)

        # Cap concurrent stitches: acquire without blocking so a flood of
        # stitch POSTs returns 503 immediately instead of parking request
        # threads. The actual ffmpeg run is offloaded to the bounded media
        # executor (max 2) so libx264 never runs on the request thread.
        if not _stitch_sem.acquire(blocking=False):
            return jsonify({"error": "busy", "detail": "too many stitches in progress"}), 503

        fd, tmp_str = tempfile.mkstemp(suffix=".mp4", prefix="stitch_")
        os.close(fd)
        tmp_path = Path(tmp_str)
        try:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                *inp_args,
                "-filter_complex", filter_str,
                "-map", "[out]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-pix_fmt", "yuv420p", "-an",
                "-movflags", "+faststart", "-y", str(tmp_path),
            ]
            logger.info("stitch-multi: %s clips layout=%s", n, layout)
            future = _media_executor.submit(_run_ffmpeg_capped, cmd, 90.0)
            # Give the pooled job a little headroom over its own 90 s ffmpeg
            # timeout so a queued job (executor saturated) still fails cleanly.
            returncode, stderr, timed_out = future.result(timeout=180)
            if timed_out:
                _silent_unlink(tmp_path)
                return jsonify({"error": "ffmpeg timeout"}), 504
            if returncode != 0:
                err = stderr.decode(errors="replace")[:400]
                logger.error("stitch-multi ffmpeg failed: %s", err)
                _silent_unlink(tmp_path)
                return jsonify({"error": "ffmpeg failed", "detail": err}), 500
        except Exception as exc:
            _silent_unlink(tmp_path)
            logger.exception("stitch-multi error")
            return jsonify({"error": str(exc)}), 500
        finally:
            _stitch_sem.release()

        resp = send_file(
            tmp_path, mimetype="video/mp4",
            as_attachment=True,
            download_name="rovimen_multi.mp4",
        )
        resp.call_on_close(lambda: _silent_unlink(tmp_path))
        return resp

    @app.route("/loop_clip_archive/<camera>/<date>/<filename>")
    @require_station
    def loop_clip_archive(camera: str, date: str, filename: str):
        """Build a looped MP4 from an archived MKV on the storagebox."""
        _validate_archive_path(camera, date, "meteors", filename)
        if not re.match(r"^[\w._-]+\.mkv$", filename):
            abort(400)
        try:
            start  = float(request.args.get("start", 0))
            end    = float(request.args.get("end",   0))
            loops  = max(1, min(10, int(request.args.get("loops", 2))))
        except (ValueError, TypeError):
            abort(400)
        if end <= start:
            abort(400)
        duration = end - start

        src_path = ARCHIVE_PATH / camera / date / "meteors" / filename
        if not src_path.exists():
            return jsonify({"error": "not found"}), 404

        with tempfile.TemporaryDirectory() as tmpdir:
            seg_path    = f"{tmpdir}/segment.mp4"
            concat_path = f"{tmpdir}/concat.txt"
            output_path = f"{tmpdir}/output.mp4"

            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                     "-i", str(src_path), "-c:v", "libx264", "-crf", "23",
                     "-preset", "fast", "-an", "-movflags", "+faststart", seg_path],
                    check=True, capture_output=True, timeout=120,
                )
            except subprocess.CalledProcessError as exc:
                logger.error("loop_clip_archive trim failed: %s", exc.stderr[-300:] if exc.stderr else "")
                abort(500)

            with open(concat_path, "w") as f:
                for _ in range(loops):
                    f.write(f"file '{seg_path}'\n")

            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", concat_path, "-c", "copy", output_path],
                check=True, capture_output=True, timeout=120,
            )

            with open(output_path, "rb") as f:
                data = f.read()

        stem    = filename.rsplit(".", 1)[0]
        outname = f"{stem}_loop{loops}x.mp4"
        return Response(
            data, status=200,
            headers={
                "Content-Type": "video/mp4",
                "Content-Disposition": f'attachment; filename="{outname}"',
                "Content-Length": str(len(data)),
            },
        )

    def _stream_mkv_as_mp4(source: Path, original_name: str) -> Response:
        """Remux MKV to MP4 via ffmpeg in a thread-pool worker, then send_file.

        The remux runs in ``_media_executor`` to cap concurrent ffmpeg
        processes at 2.  The request worker still blocks on the result
        (``future.result``), but the thread pool prevents more than 2
        remuxes from running at once.  The resulting MP4 uses +faststart
        for seekable output.  The temp file is cleaned up after Flask
        finishes streaming the response.
        """
        mp4_name = original_name.rsplit(".", 1)[0] + ".mp4"
        fd, tmp_str = tempfile.mkstemp(suffix=".mp4", prefix="remux_")
        os.close(fd)
        tmp_path = Path(tmp_str)
        try:
            future = _media_executor.submit(_remux_to_mp4, source, tmp_path)
            future.result(timeout=35)
        except Exception:
            _silent_unlink(tmp_path)
            logger.exception("MKV remux failed for %s", original_name)
            abort(500)
        resp = send_file(
            tmp_path,
            mimetype="video/mp4",
        )
        resp.call_on_close(lambda: _silent_unlink(tmp_path))
        return resp
