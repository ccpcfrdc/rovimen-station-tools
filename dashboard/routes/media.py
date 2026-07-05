"""Media, videodb, and live-stream routes.

Extracted from rovimen_dashboard.py.  All routes preserved verbatim -- same
URLs, same behaviour, same decorators.  Wired in from ``create_app()`` via
``register_media_routes``.

Closure state (archive index, prefetch caches, proxy cache, live-stream
semaphores) is passed in from the caller so that the main module and this
module share the same cache instances.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    request,
    send_file,
    session,
    stream_with_context,
)

from cache_store import (
    ARCHIVE_PATH,
    CACHE_PATH,
    NGINX_ACCEL,
    THUMB_ARCHIVE_DIR,
    THUMB_CACHE_DIR,
    _downsample_thumbnail_bytes,
    _drop_future_dates,
    _thumb_archive_path,
    _thumb_cache_has_space,
    _thumb_cache_path,
)

import command_dispatch
from auth import require_station, station_is_public_for_request
from http_caching import _json_cached
from security import public_route
from routes.archive import _parse_radiants_txt
from route_helpers import (
    lookup_station,
    invalidate_proxy_cache,
    proxy_error_response,
    validate_media_params,
    media_url,
)
from station_client import (
    station_url,
    station_get_raw,
    _session_for_url,
    _streaming_proxy,
    _with_sshfs_timeout,
)
from tunnels import _TunnelDown

logger = logging.getLogger(__name__)

# Per-file body cap when fetching a clip for batch download (a single
# pathological chunk can't blow past this).
_BATCH_PER_FILE_MAX_BYTES = 50 * 1024 * 1024
# Per-request aggregate cap across all selected files. gunicorn runs 1 worker
# with 32 threads on the VPS; without this a single request (50 files just
# under the per-file cap) could pull ~2.5 GB into one temp dir and several
# concurrent requests would fill the disk / OOM the box. Configurable via env.
_BATCH_TOTAL_MAX_BYTES = int(
    os.environ.get("ROVIMEN_BATCH_DOWNLOAD_MAX_BYTES", str(200 * 1024 * 1024))
)
# Cap on the upper download size for the single-clip crop fallback (the source
# is streamed to /tmp before ffmpeg runs); a 15-min capture chunk is ~900 MB.
_CROP_SOURCE_MAX_BYTES = int(
    os.environ.get("ROVIMEN_CROP_SOURCE_MAX_BYTES", str(500 * 1024 * 1024))
)

# ── Crop+overlay helpers (module-level) ────────────────────────────────────────

_BUNDLE_PATH = Path("/opt/rovimen/station-bundle")
_BUNDLE_FONT = _BUNDLE_PATH / "fonts" / "VCR_OSD_MONO_1.001.ttf"
_BUNDLE_LOGO = _BUNDLE_PATH / "assets" / "astromania_text.png"


def _silent_unlink_path(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _stream_download_capped(resp, dest_path: str, max_bytes: int) -> bool:
    """Stream a ``requests`` response body to ``dest_path``, aborting if it
    exceeds ``max_bytes``.

    Returns True on success, False if the cap was hit (the partial file is
    removed). The crop routes call this for the station fallback so a giant
    capture chunk can't fill the VPS /tmp partition before ffmpeg starts.
    """
    written = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(65536):
            written += len(chunk)
            if written > max_bytes:
                f.close()
                try:
                    os.unlink(dest_path)
                except OSError:
                    pass
                return False
            f.write(chunk)
    return True


class _ZipSink:
    """Append-only file-like that buffers what ``zipfile`` writes and lets a
    generator drain it.

    ``ZipFile`` records absolute stream offsets in each entry header and the
    central directory, so the object it writes to must report a monotonically
    increasing ``tell()`` even though we discard bytes after yielding them.
    This sink keeps the running offset while emptying its buffer on each
    ``drain()`` -- that is what lets us stream the archive without holding it
    all in RAM. ``zipfile`` only writes sequentially here (no seek back), so a
    forward-only sink is sufficient.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:  # pragma: no cover - zipfile calls this
        pass

    def drain(self) -> bytes:
        if not self._buf:
            return b""
        out = bytes(self._buf)
        self._buf.clear()
        return out


def _stream_zip_from_files(entries, cleanup_dir: str | None = None):
    """Yield a ZIP archive (stored, no compression) built from on-disk files.

    ``entries`` is an ordered iterable of ``(arcname, source_path)`` tuples.
    Each source file is streamed straight into the zip entry and the sink is
    drained between blocks, so the full archive never resides in RAM at once --
    this is what keeps the batch-download route from OOM-ing the single
    gunicorn worker. ``cleanup_dir`` (if given) is removed once the generator
    is exhausted or closed.
    """
    try:
        sink = _ZipSink()
        with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for arcname, source_path in entries:
                with zf.open(arcname, mode="w") as dst, open(source_path, "rb") as src:
                    while True:
                        block = src.read(65536)
                        if not block:
                            break
                        dst.write(block)
                        chunk = sink.drain()
                        if chunk:
                            yield chunk
                # Flush the per-entry data descriptor before the next entry.
                chunk = sink.drain()
                if chunk:
                    yield chunk
        # Central directory written on ZipFile close.
        tail = sink.drain()
        if tail:
            yield tail
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def _load_overlay_module():
    p = _BUNDLE_PATH / "overlay.py"
    if not p.exists():
        return None
    spec = importlib.util.spec_from_file_location("rovimen_overlay", str(p))
    if spec is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_overlay_mod = _load_overlay_module()


def _build_crop_cmd(
    in_path: str, out_path: str,
    x: float, y: float, w: float, h: float,
    station_code: str, filename: str,
    ss: float, t: float,
    overlay_cfg: dict, station_cfg: dict,
    vframes: int | None = None,
) -> list[str]:
    """Build ffmpeg cmd list: crop → scale 1280×720 → station overlay.

    Uses the same overlay.py logic as the station encoder so style is identical.
    Falls back to bare crop+scale if overlay module or config is unavailable.

    When vframes is set, omits -t and uses -vframes instead (for still-frame
    extraction). The output path extension determines the codec (e.g. .jpg).
    """
    crop_scale = (
        f"crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f},"
        f"scale=1280:720:flags=lanczos,setsar=1"
    )

    # epoch of the trimmed clip start (for strftime basetime in drawtext)
    chunk_epoch = 0
    m = re.match(r"^.+?_(\d{8})_(\d{6})", filename)
    if m:
        try:
            dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            chunk_epoch = int(dt.timestamp()) + int(ss)
        except Exception:
            pass

    if vframes is not None:
        base_cmd = ["ffmpeg", "-y", "-ss", f"{ss:.3f}", "-i", in_path]
        enc_args  = ["-vframes", str(vframes), "-q:v", "2"]
    else:
        base_cmd = ["ffmpeg", "-y", "-ss", f"{ss:.3f}", "-t", f"{t:.3f}", "-i", in_path]
        enc_args  = ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-an"]

    if _overlay_mod is None or not overlay_cfg:
        return [*base_cmd, "-vf", crop_scale, *enc_args, out_path]

    try:
        filters, bar_h = _overlay_mod.build_drawtext_annotations(
            overlay_cfg, station_code, station_cfg, chunk_epoch,
        )
    except Exception:
        filters, bar_h = [], 0

    logo_path    = overlay_cfg.get("logo", "")
    logo_opacity = float(overlay_cfg.get("logo_opacity", 0.8))
    use_logo     = bool(overlay_cfg.get("show_logo", True) and logo_path and Path(logo_path).exists())

    vf_parts = [crop_scale]
    if bar_h > 0:
        vf_parts.append(f"pad=iw:ih+{bar_h}:0:0:black")
    vf_parts.extend(filters)

    if use_logo:
        font_path  = overlay_cfg.get("font", "")
        font_size  = int(overlay_cfg.get("font_size", 19))
        network    = overlay_cfg.get("network", "ROVIMEN")
        style      = overlay_cfg.get("style", "standard")
        MARGIN     = 14
        logo_size  = int(overlay_cfg.get("logo_size", 0))
        try:
            ntw    = _overlay_mod.measure_text_width(font_path, font_size, network)
            logo_h = logo_size if logo_size > 0 else _overlay_mod.measure_text_height(font_path, font_size) + 1
        except Exception:
            ntw    = int(font_size * 0.65 * len(network))
            logo_h = font_size + 1
        logo_x      = MARGIN + ntw + 8
        text_center = f"H-{bar_h}+{MARGIN}+{font_size}/2" if style == "cinema" else f"{MARGIN}+{font_size}/2"
        logo_y      = f"({text_center})-{logo_h}/2"
        fc = (
            f"[0:v]{','.join(vf_parts)}[vmain];"
            f"[1:v]scale=-2:{logo_h},format=rgba,colorchannelmixer=aa={logo_opacity}[logo];"
            f"[vmain][logo]overlay=x={logo_x}:y={logo_y}[out]"
        )
        return [*base_cmd, "-i", logo_path, "-filter_complex", fc, "-map", "[out]", *enc_args, out_path]

    return [*base_cmd, "-vf", ",".join(vf_parts), *enc_args, out_path]


def _videodb_rms_from_archive(cam_code: str, date: str):
    """Return RMS detection data parsed from archive rms/ files for old nights.

    Merges _radiants.txt (shower, mag, radiant, solar_lon) with
    FTPdetectinfo (duration_s, fps, num_segments), matched by position —
    the same order RMS writes them.
    """
    from routes.archive import _parse_ftpdetectinfo
    rms_dir = ARCHIVE_PATH / cam_code / date / "rms"
    detections: list[dict] = []
    if rms_dir.is_dir():
        try:
            all_radiants: list[dict] = []
            for f in sorted(rms_dir.glob("*_radiants.txt")):
                all_radiants.extend(_parse_radiants_txt(f))
            all_ftp: list[dict] = []
            for f in sorted(rms_dir.glob("FTPdetectinfo_*.txt")):
                if "_unfiltered" not in f.name and "_backup" not in f.name:
                    all_ftp.extend(_parse_ftpdetectinfo(f))
            for i, rad in enumerate(all_radiants):
                det = dict(rad)
                if i < len(all_ftp):
                    det["duration_s"] = all_ftp[i].get("duration_s")
                    det["fps"] = all_ftp[i].get("fps")
                    det["num_segments"] = all_ftp[i].get("num_segments")
                detections.append(det)
        except Exception:
            pass
    return jsonify({"camera": cam_code, "date": date, "detections": detections})


def register_media_routes(
    app: Flask,
    config,
    tunnels,
    cache,
    *,
    archive_idx,
    morning_done: dict,
    bw_prefetched: dict,
    color_prefetched: dict,
    prefetched_lock: threading.Lock,
    prefetched_max_age_s: float,
    prefetch_night,
    prefetch_executor: ThreadPoolExecutor,
    prefetch_archive_thumbs,
    proxy_cache: dict[tuple[str, str], tuple[float, Any]],
    proxy_cache_lock: threading.Lock,
    live_stream_triple_sem,
    live_stream_global_sem: threading.BoundedSemaphore,
    live_stream_retry_after_s: int,
) -> None:
    _live_stream_triple_sem = live_stream_triple_sem
    _live_stream_global_sem = live_stream_global_sem
    _LIVE_STREAM_RETRY_AFTER_S = live_stream_retry_after_s

    _require_station_base = lookup_station
    _invalidate_proxy_cache_base = invalidate_proxy_cache
    _validate_media_params_base = validate_media_params
    _media_url_base = media_url
    from functools import partial

    _require_station = partial(_require_station_base, config)
    _invalidate_proxy_cache = partial(_invalidate_proxy_cache_base, proxy_cache, proxy_cache_lock)

    # ── Per-station overlay config cache (30-min TTL) ────────────────────
    _ov_cache: dict[str, tuple[float, dict, dict]] = {}

    def _get_overlay_cfg(host_key: str, station_code: str) -> tuple[dict, dict]:
        """Return (overlay_cfg, station_cfg) from station /api/settings, cached 30 min.

        Font and logo paths are remapped to the VPS station-bundle copies so the
        overlay rendered here matches the one burned in by the station encoder.
        """
        now = time.monotonic()
        cached = _ov_cache.get(host_key)
        if cached and cached[0] > now:
            return cached[1], cached[2].get(station_code, {})
        try:
            settings = station_get_raw(config, tunnels, host_key, "/api/settings", timeout=8)
            if not isinstance(settings, dict):
                settings = {}
        except Exception:
            settings = {}
        overlay_cfg   = dict(settings.get("overlay", {}))
        stations_cfg  = settings.get("stations", {})
        # Remap station-local font/logo paths to VPS bundle equivalents
        if _BUNDLE_FONT.exists():
            overlay_cfg["font"] = str(_BUNDLE_FONT)
        if _BUNDLE_LOGO.exists():
            overlay_cfg["logo"] = str(_BUNDLE_LOGO)
        _ov_cache[host_key] = (now + 1800, overlay_cfg, stations_cfg)
        return overlay_cfg, stations_cfg.get(station_code, {})
    _proxy_error_response = proxy_error_response

    # ── Archive helpers ──────────────────────────────────────────────────

    def _archive_nights(cam_code: str) -> list[str]:
        """Return dates that have at least one .mkv/.mp4 in meteors/.
        Reads directly from the in-memory archive index -- O(1), no SSHFS."""
        return archive_idx.nights_with_meteors(cam_code)

    def _archive_chunks_fallback(host_key: str, cam_code: str, date: str):
        ni = archive_idx.night_files(cam_code, date)
        if ni is None:
            return jsonify([])
        meteor_names = set(ni.meteor_files)

        # Read state.json to recover per-chunk meteor_time.
        state_path = ARCHIVE_PATH / cam_code / date / "state.json"
        def _read_state():
            if not state_path.exists():
                return {}
            return json.loads(state_path.read_text()).get("chunks") or {}
        state_chunks = _with_sshfs_timeout(_read_state, timeout=5.0, default={})

        # One scandir pass to get file sizes for all meteor files.
        meteors_dir = ARCHIVE_PATH / cam_code / date / "meteors"
        def _scan_sizes():
            if not meteors_dir.is_dir():
                return {}
            import os
            return {e.name: e.stat().st_size for e in os.scandir(meteors_dir)}
        file_sizes = _with_sshfs_timeout(_scan_sizes, timeout=5.0, default={})

        chunks: list[dict[str, Any]] = []
        for name in ni.meteor_files:
            if not (name.endswith(".mkv") or name.endswith(".mp4")):
                continue
            stack_name = (
                name.replace("_color.mkv", "_stack.webp")
                .replace(".mp4", "_stack.webp")
            )
            time_str = "00:00:00"
            stem = name.rsplit(".", 1)[0]
            parts = stem.split("_")
            if len(parts) >= 3:
                t = parts[2]
                if len(t) == 6 and t.isdigit():
                    time_str = f"{t[:2]}:{t[2:4]}:{t[4:6]}"
            if stack_name in ni.stack_files:
                stack_subdir = "stacks"
            elif stack_name in meteor_names:
                stack_subdir = "meteors"
            else:
                stack_subdir = None
            chunk_state = state_chunks.get(name) or {}
            lock = chunk_state.get("lock") if isinstance(chunk_state.get("lock"), dict) else {}
            meteor_time = lock.get("meteor_time")
            size_bytes = file_sizes.get(name)
            size_mb = round(size_bytes / 1_048_576, 1) if size_bytes else None
            chunks.append({
                "filename": name,
                "time": time_str,
                "stack": stack_name if stack_subdir else None,
                "stack_subdir": stack_subdir,
                "size_mb": size_mb,
                "locked": True,
                "lock_type": lock.get("lock_type", "detection"),
                "detection_offset_s": None,
                "meteor_time": meteor_time,
                "reencoded": False,
                "source": "archive",
            })
        if chunks:
            threading.Thread(
                target=prefetch_archive_thumbs,
                args=(host_key, cam_code, date),
                daemon=True,
            ).start()
        return jsonify(chunks)

    # ── Videodb endpoints ────────────────────────────────────────────────

    @app.route("/api/videodb/nights/<host_key>/<cam_code>")
    @require_station
    def api_videodb_nights(host_key: str, cam_code: str):
        _require_station(host_key)
        if not re.match(r"^[A-Z0-9]+$", cam_code, re.IGNORECASE):
            abort(400)
        try:
            station_nights = station_get_raw(config, tunnels, host_key, f"/api/nights/{cam_code}")
            if not isinstance(station_nights, list):
                station_nights = []
        except Exception:
            station_nights = []
        archive = _archive_nights(cam_code)
        station_set = set(station_nights)
        merged = station_nights + [n for n in archive if n not in station_set]
        merged = _drop_future_dates(merged)
        merged.sort(reverse=True)
        # Night list changes once per day at most; the camera "Nights" picker
        # is hit on every station tab switch. ETag + 30 s lets repeated tab
        # toggles short-circuit to 304.
        return _json_cached(merged, max_age=30)

    @app.route("/api/videodb/rmsnights/<host_key>/<cam_code>")
    @require_station
    def api_videodb_rmsnights(host_key: str, cam_code: str):
        _require_station(host_key)
        if not re.match(r"^[A-Z0-9]+$", cam_code):
            abort(400)
        try:
            nights = station_get_raw(config, tunnels, host_key, f"/api/rmsnights/{cam_code}")
            if not isinstance(nights, list):
                nights = []
        except Exception:
            nights = []
        archive_nights = {
            d for d in archive_idx.nights(cam_code)
            if (ARCHIVE_PATH / cam_code / d / "rms").is_dir()
        }
        merged = sorted(set(nights) | archive_nights, reverse=True)
        return jsonify(_drop_future_dates(merged))

    @app.route("/api/videodb/rms-detections/<host_key>/<cam_code>/<date>")
    @require_station
    def api_videodb_rms_detections(host_key: str, cam_code: str, date: str):
        """Proxy enriched RMS detection data (magnitude, shower, radiant, etc.)."""
        _require_station(host_key)
        if not re.match(r"^[A-Z0-9]+$", cam_code):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        try:
            req_date = datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            abort(400)
        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        if req_date < cutoff:
            return _videodb_rms_from_archive(cam_code, date)
        try:
            data = station_get_raw(
                config, tunnels, host_key,
                f"/api/rms-detections/{cam_code}/{date}",
                timeout=10,
            )
            if not isinstance(data, dict):
                data = {"camera": cam_code, "date": date, "detections": []}
        except Exception:
            data = {"camera": cam_code, "date": date, "detections": []}
        if not data.get("detections"):
            return _videodb_rms_from_archive(cam_code, date)
        return jsonify(data)

    @app.route("/api/videodb/chunks/<host_key>/<cam_code>/<date>")
    @require_station
    def api_videodb_chunks(host_key: str, cam_code: str, date: str):
        _require_station(host_key)
        if not re.match(r"^[A-Z0-9]+$", cam_code, re.IGNORECASE):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        # Old nights are immutable on the archive -- skip the station round-trip.
        try:
            req_date = datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            abort(400)
        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        if req_date < cutoff:
            return _archive_chunks_fallback(host_key, cam_code, date)
        qs = request.query_string.decode()
        path = f"/api/chunks/{cam_code}/{date}"
        if qs:
            path += f"?{qs}"
        try:
            raw = station_get_raw(config, tunnels, host_key, path)
        except Exception:
            return _archive_chunks_fallback(host_key, cam_code, date)
        # Empty plain list means the date folder doesn't exist on station -- try archive.
        if isinstance(raw, list) and not raw:
            return _archive_chunks_fallback(host_key, cam_code, date)
        # New station API returns {"morning_done": bool, "chunks": [...]}
        # Old API returns a plain list -- handle both for backward compat.
        if isinstance(raw, dict) and "chunks" in raw:
            key = (host_key, cam_code, date)
            was_done = morning_done.get(key, False)
            is_done = bool(raw.get("morning_done", False))
            # Store as monotonic timestamp (0.0 = not done) so the prefetch
            # loop can prune historical entries under _prefetched_lock.
            morning_done[key] = time.monotonic() if is_done else 0.0
            # Prefetch runs for ALL stations -- direct and proxy_media alike.
            # Do not gate this on _needs_proxy; the cache benefits every user
            # regardless of whether they access via Tailscale or direct IP.
            chunks_list = raw["chunks"]
            # No locked clips on station -- fall back to archive regardless of morning_done.
            # (morning_done may be false on old nights even when processing finished.)
            if not chunks_list:
                return _archive_chunks_fallback(host_key, cam_code, date)
            # BW pass: prefetch on first chunks fetch for any night.
            # Bounded via _prefetch_executor (8 workers) so a many-cam
            # morning_done flip doesn't spawn 40+ raw threads.
            now_mono = time.monotonic()
            with prefetched_lock:
                bw_seen = key in bw_prefetched
                if not bw_seen:
                    bw_prefetched[key] = now_mono
                color_seen = key in color_prefetched
                if is_done and not was_done and not color_seen:
                    color_prefetched[key] = now_mono
            if not bw_seen:
                prefetch_executor.submit(
                    prefetch_night, host_key, cam_code, date, chunks_list,
                    force=False,
                )
            # Color pass: force-overwrite when morning_done first becomes True
            if is_done and not was_done and not color_seen:
                prefetch_executor.submit(
                    prefetch_night, host_key, cam_code, date, chunks_list,
                    force=True,
                )
            # ETag short-circuits the 200+ KB payload to 304 on poll cycles
            # where the chunk list hasn't changed (the common case once the
            # night is archived).
            return _json_cached(raw["chunks"], max_age=15)
        return _json_cached(raw, max_age=15)

    # ── Media proxy (redirect to station fileserver) ──────────────────────

    def _validate_media_params(
        host_key: str, station_code: str, date: str, filename: str, ext_re: str,
    ) -> None:
        # Per-station public-flag enforcement for anonymous visitors. When the
        # dashboard is publicly exposed, media for a ``public: false`` station
        # must 404 for anyone without a login session (closes the gap the
        # security review flagged: the flag was honoured only in public_api.py,
        # not in these internal serving handlers). Logged-in users keep full
        # fleet-wide access. Runs BEFORE base validation so a private station's
        # existence isn't disclosed via a differential error path.
        if not station_is_public_for_request(config, host_key):
            abort(404)
        _validate_media_params_base(config, host_key, station_code, date, filename, ext_re)

    def _media_url(host_key: str, *path_parts: str) -> str:
        return _media_url_base(config, tunnels, host_key, *path_parts)

    def _needs_proxy(host_key: str) -> bool:
        """True for stations the browser can't reach directly (proxy_media or tunnel)."""
        station = config.stations.get(host_key)
        return (station is not None and station.proxy_media) or tunnels.needs_tunnel(host_key)

    def _proxy_response(url: str) -> Response:
        """Fetch url server-side and stream the response back to the browser."""
        try:
            fwd_headers = {}
            if "Range" in request.headers:
                fwd_headers["Range"] = request.headers["Range"]
            resp = _session_for_url(url).get(
                url, timeout=60, stream=True, headers=fwd_headers,
            )
            out_headers = {
                "Content-Type": resp.headers.get("Content-Type", "application/octet-stream"),
            }
            if "Content-Length" in resp.headers:
                out_headers["Content-Length"] = resp.headers["Content-Length"]
            if "Content-Range" in resp.headers:
                out_headers["Content-Range"] = resp.headers["Content-Range"]
            if "Accept-Ranges" in resp.headers:
                out_headers["Accept-Ranges"] = resp.headers["Accept-Ranges"]
            return Response(
                _streaming_proxy(resp),
                status=resp.status_code,
                headers=out_headers,
            )
        except Exception:
            abort(502)

    def _serve_thumbnail(host_key: str, *path_parts: str) -> Response:
        """Serve thumbnails via tiered cache with write-through promotion.

        Lookup order -- fastest first:
          1. SSD hot cache (THUMB_CACHE_DIR, 3-day retention) -> nginx
             X-Accel-Redirect when NGINX_ACCEL=1.
          2. Cold-tier thumb archive (THUMB_ARCHIVE_DIR on storage box,
             unlimited retention) -> hot-promote to SSD, serve inline.
          3. Station HTTP -- for live nights not yet in any cache.
          4. Full-res archive stacks on storage box -> downsample +
             hot-promote to SSD, serve inline.
        """
        cam, date, filename = path_parts[1], path_parts[2], path_parts[3]
        cache_file = _thumb_cache_path(host_key, cam, date, filename)

        # 1) NVMe cache hit (the common case once a night is prefetched).
        if cache_file.exists():
            if NGINX_ACCEL:
                accel_path = "/internal/thumb_cache/" + "/".join(
                    [host_key, cam, date, filename]
                )
                return Response(
                    headers={"X-Accel-Redirect": accel_path, "Content-Type": "image/webp"}
                )
            return send_file(cache_file, mimetype="image/webp", conditional=True)

        # 2) Cold-tier thumb archive on storage box (unlimited retention).
        # Already-downsampled thumbnails promoted here by the hourly prune.
        cold_path = _thumb_archive_path(host_key, cam, date, filename)
        if cold_path is not None:
            try:
                # SSHFS stat -- guard so a stalled storagebox mount can't pin
                # the worker thread here.
                if _with_sshfs_timeout(cold_path.is_file, timeout=5.0, default=False):
                    data = cold_path.read_bytes()
                    if _thumb_cache_has_space():
                        try:
                            cache_file.parent.mkdir(parents=True, exist_ok=True)
                            cache_file.write_bytes(data)
                        except Exception:
                            pass
                    if NGINX_ACCEL and cache_file.exists():
                        accel_path = "/internal/thumb_cache/" + "/".join(
                            [host_key, cam, date, filename]
                        )
                        return Response(
                            headers={"X-Accel-Redirect": accel_path,
                                     "Content-Type": "image/webp"},
                        )
                    return Response(data, status=200,
                                   headers={"Content-Type": "image/webp"})
            except OSError:
                pass

        # 3) Station -- fetched first for tonight so live nights stay snappy.
        # Past dates skip to the archive to avoid a 15 s timeout on stations
        # that have already pruned old thumbnails.
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        if date >= today:
            try:
                url = _media_url(host_key, *path_parts)
                resp = _session_for_url(url).get(url, timeout=15)
                if resp.status_code == 200:
                    body = _downsample_thumbnail_bytes(resp.content)
                    if _thumb_cache_has_space():
                        try:
                            cache_file.parent.mkdir(parents=True, exist_ok=True)
                            cache_file.write_bytes(body)
                        except Exception:
                            pass
                    return Response(
                        body, status=200,
                        headers={"Content-Type": "image/webp"},
                    )
            except Exception:
                pass

        # 4) Storage-box archive fallback. Most thumbs live in stacks/; legacy
        # captures wrote them flat alongside the .mkv in meteors/.
        for subdir in ("stacks", "meteors"):
            src = ARCHIVE_PATH / cam / date / subdir / filename
            try:
                # SSHFS stat under the timeout guard (stalled mount -> skip).
                if not _with_sshfs_timeout(src.is_file, timeout=5.0, default=False):
                    continue
            except OSError:
                continue
            if not _thumb_cache_has_space():
                # Cache is full -- serve straight from SSHFS rather than
                # bomb the worker on a disk-full copy. Skipping the
                # promotion keeps the request alive even when the NVMe
                # is past the eviction threshold.
                return send_file(src, mimetype="image/webp", conditional=True)
            try:
                # Hot-promote to NVMe so the next hit goes through X-Accel-Redirect.
                # Atomic .part-rename to avoid serving truncated webp under load.
                # Downsample on write so legacy full-res stacks in the archive
                # don't get promoted verbatim into the (small) NVMe cache.
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_file.with_suffix(cache_file.suffix + ".part")
                src_bytes = src.read_bytes()
                out_bytes = _downsample_thumbnail_bytes(src_bytes)
                tmp.write_bytes(out_bytes)
                tmp.replace(cache_file)
                if NGINX_ACCEL:
                    accel_path = "/internal/thumb_cache/" + "/".join(
                        [host_key, cam, date, filename]
                    )
                    return Response(
                        headers={"X-Accel-Redirect": accel_path,
                                 "Content-Type": "image/webp"},
                    )
                return send_file(cache_file, mimetype="image/webp", conditional=True)
            except Exception:
                # If the copy failed (disk full / permission), serve directly
                # from SSHFS -- slower, but the user still sees the image.
                return send_file(src, mimetype="image/webp", conditional=True)

        abort(404)

    def _serve_or_redirect(host_key: str, *path_parts: str) -> Response:
        """Proxy all media through the VPS for all stations.

        Do not gate on _needs_proxy -- consistent behaviour regardless of
        whether a station is directly reachable.
        """
        url = _media_url(host_key, *path_parts)
        return _proxy_response(url)

    def _serve_or_archive(host_key: str, archive_subdir: str, *path_parts: str) -> Response:
        """Prefer the storagebox archive (processed/re-encoded clip); fall
        back to the station for tonight's live data or when the archive copy
        doesn't exist yet.

        ``path_parts`` is the station-side URL tail, expected as
        ``(<endpoint>, <cam>, <date>, <filename>)`` matching ``_media_url``.
        """
        if len(path_parts) < 4:
            return _serve_or_redirect(host_key, *path_parts)
        _, station_code, date, filename = path_parts[0], path_parts[1], path_parts[2], path_parts[3]

        # --- Archive (primary): processed / re-encoded clip on storagebox ---
        # Guard the SSHFS stat: if the storagebox mount stalls the timeout
        # returns False and we fall through to the station fallback below
        # rather than pinning the worker thread.
        archive_file = ARCHIVE_PATH / station_code / date / archive_subdir / filename
        if _with_sshfs_timeout(
            lambda: archive_file.exists() and archive_file.is_file(),
            timeout=5.0, default=False,
        ):
            mime = "video/mp4" if filename.endswith(".mp4") else (
                "video/x-matroska" if filename.endswith(".mkv") else (
                    "image/webp" if filename.endswith(".webp") else "application/octet-stream"
                )
            )
            return send_file(archive_file, mimetype=mime, conditional=True,
                             as_attachment=True, download_name=filename)

        # --- Station fallback: live / not-yet-archived data ----------------
        try:
            url = _media_url(host_key, *path_parts)
        except _TunnelDown:
            url = None

        if url is not None:
            try:
                fwd_headers = {}
                if "Range" in request.headers:
                    fwd_headers["Range"] = request.headers["Range"]
                resp = _session_for_url(url).get(
                    url, timeout=60, stream=True, headers=fwd_headers,
                )
                if resp.status_code in (200, 206):
                    out_headers = {
                        "Content-Type": resp.headers.get("Content-Type", "application/octet-stream"),
                    }
                    for h in ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition"):
                        if h in resp.headers:
                            out_headers[h] = resp.headers[h]
                    return Response(
                        _streaming_proxy(resp),
                        status=resp.status_code,
                        headers=out_headers,
                    )
                try:
                    resp.close()
                except Exception:
                    pass
            except Exception:
                pass

        return jsonify({
            "error": "not_found",
            "detail": (
                f"{filename} is not on the station and not in the archive at "
                f"{station_code}/{date}/{archive_subdir}/"
            ),
        }), 404

    # ── Media serving routes ─────────────────────────────────────────────

    @app.route("/timelapse/<host_key>/<station_code>/<date>/<filename>")
    @public_route
    def timelapse(host_key: str, station_code: str, date: str, filename: str):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.mp4$"
        )
        return _serve_or_archive(host_key, "timelapse", "color_timelapse", station_code, date, filename)

    @app.route("/stack/<host_key>/<station_code>/<date>/<filename>")
    @public_route
    def stack_image(host_key: str, station_code: str, date: str, filename: str):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.webp$"
        )
        return _serve_thumbnail(host_key, "thumbnail", station_code, date, filename)

    @app.route("/thumbnail/<host_key>/<station_code>/<date>/<filename>")
    @public_route
    def thumbnail(host_key: str, station_code: str, date: str, filename: str):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.webp$"
        )
        return _serve_thumbnail(host_key, "thumbnail", station_code, date, filename)

    @app.route("/video/<host_key>/<station_code>/<date>/<filename>")
    @public_route
    def video_serve(host_key: str, station_code: str, date: str, filename: str):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
        )
        return _serve_or_archive(host_key, "meteors", "color_capture", station_code, date, filename)

    @app.route("/download/<host_key>/<station_code>/<date>/<filename>")
    @public_route
    def video_download(host_key: str, station_code: str, date: str, filename: str):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
        )
        resp = _serve_or_archive(host_key, "meteors", "color_capture", station_code, date, filename)
        # Unwrap (response, status_code) tuples returned by the 404 fallback.
        actual = resp[0] if isinstance(resp, tuple) else resp
        if isinstance(actual, Response):
            actual.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    @app.route("/fullstack/<host_key>/<station_code>/<date>/<filename>")
    @public_route
    def fullstack(host_key: str, station_code: str, date: str, filename: str):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.webp$"
        )
        # Try the full-resolution stack first (station-side color_capture dir).
        # If missing (stacker didn't run for that chunk, or it's an older
        # upload without stacks), fall back to the 256 px thumbnail so the
        # user at least gets a stack image rather than a 404. Full on-demand
        # generation from the MKV is a separate TODO (see
        # _generate_stack_from_mkv docstring).
        try:
            upstream = _media_url(host_key, "color_capture", station_code, date, filename)
            head = _session_for_url(upstream).head(
                upstream, timeout=10, allow_redirects=True,
            )
            if head.status_code == 200:
                return _proxy_response(upstream)
        except Exception:
            pass
        generated = _generate_stack_from_mkv(host_key, station_code, date, filename)
        if generated is not None:
            return send_file(generated, mimetype="image/webp", conditional=True)
        return _serve_thumbnail(host_key, "thumbnail", station_code, date, filename)

    def _generate_stack_from_mkv(host_key: str, station_code: str,
                                  date: str, filename: str) -> Path | None:
        """Return a cached full-resolution max-pixel stack if one was previously
        generated for this chunk. Returns None if no cache exists.

        On-demand generation from the MKV is TODO -- it requires a per-frame
        max accumulator (numpy/PIL) which isn't currently installed in the
        dashboard venv. For now we rely on the station-side stacker having
        already produced the webp; if it hasn't, the caller falls back to the
        thumbnail so the user at least gets a small stack instead of a 404.
        """
        if not filename.endswith("_stack.webp"):
            return None
        cache_file = CACHE_PATH / "fullstack" / host_key / station_code / date / filename
        return cache_file if cache_file.exists() else None

    @app.route("/night_stack/<host_key>/<station_code>/<date>/<filename>")
    @public_route
    def night_stack(host_key: str, station_code: str, date: str, filename: str):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.webp$"
        )
        # On the storagebox, the per-night composite (<cam>_<date>_night_stack.webp)
        # lives next to the timelapse mp4 in the <cam>/<date>/timelapse/ subdir,
        # not in stacks/ (which is per-chunk thumbnails).
        return _serve_or_archive(host_key, "timelapse", "night_stack", station_code, date, filename)

    @app.route("/timelapse_download/<host_key>/<station_code>/<date>/<filename>")
    @public_route
    def timelapse_download(
        host_key: str, station_code: str, date: str, filename: str
    ):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.mp4$"
        )
        return _serve_or_archive(host_key, "timelapse", "color_timelapse", station_code, date, filename)

    @app.route("/shortclip/<host_key>/<station_code>/<date>/<filename>")
    @require_station
    def shortclip(host_key: str, station_code: str, date: str, filename: str):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
        )
        url = _media_url(host_key, "api/shortclip", station_code, date, filename)
        qs = request.query_string.decode()
        if qs:
            url += f"?{qs}"
        # Always proxy -- never redirect. A redirect to the station IP is cross-origin,
        # and browsers ignore the `download` attribute for cross-origin URLs, causing
        # the browser to navigate to the URL instead of downloading the file.
        try:
            resp = _session_for_url(url).get(url, timeout=120, stream=True)
            if resp.status_code != 200:
                # Drop the upstream HTML 404 body -- otherwise the browser saves
                # it as <filename>.html when the user clicked download.
                try:
                    resp.close()
                except Exception:
                    pass
                return jsonify({
                    "error": "shortclip_unavailable",
                    "detail": (
                        f"{filename} is no longer on the station (likely pruned by "
                        f"color_days retention). Re-encoded shortclips can't be "
                        f"reconstructed from the archive on the VPS yet."
                    ),
                }), 404
            out_headers: dict[str, str] = {
                "Content-Type": resp.headers.get("Content-Type", "video/mp4"),
            }
            for h in ("Content-Length", "Content-Disposition"):
                if h in resp.headers:
                    out_headers[h] = resp.headers[h]
            return Response(
                _streaming_proxy(resp),
                status=200,
                headers=out_headers,
            )
        except Exception:
            abort(502)

    @app.route("/stitch_video/<host_key>/<station_code>/<date>")
    @require_station
    def stitch_video(host_key: str, station_code: str, date: str):
        _require_station(host_key)
        if not re.match(r"^[A-Z0-9]+$", station_code):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        url = station_url(config, tunnels, host_key, f"/api/stitch_video/{station_code}/{date}")
        qs = request.query_string.decode()
        if qs:
            url += f"?{qs}"
        try:
            resp = _session_for_url(url).get(url, timeout=60, stream=True)
            if resp.status_code != 200:
                try:
                    resp.close()
                except Exception:
                    pass
                return jsonify({
                    "error": "stitch_unavailable",
                    "detail": "one or both source chunks no longer on the station",
                }), 404
            out_headers: dict[str, str] = {
                "Content-Type": resp.headers.get("Content-Type", "video/mp4"),
                "Accept-Ranges": "bytes",
            }
            for h in ("Content-Length", "Content-Range", "Content-Disposition"):
                if h in resp.headers:
                    out_headers[h] = resp.headers[h]
            return Response(
                _streaming_proxy(resp),
                status=200,
                headers=out_headers,
            )
        except Exception as exc:
            return _proxy_error_response(host_key, exc)

    @app.route("/shortclip_stitch/<host_key>/<station_code>/<date>")
    @require_station
    def shortclip_stitch(host_key: str, station_code: str, date: str):
        _require_station(host_key)
        if not re.match(r"^[A-Z0-9]+$", station_code):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        url = station_url(config, tunnels, host_key, f"/api/shortclip_stitch/{station_code}/{date}")
        qs = request.query_string.decode()
        if qs:
            url += f"?{qs}"
        try:
            resp = _session_for_url(url).get(url, timeout=120, stream=True)
            if resp.status_code != 200:
                try:
                    resp.close()
                except Exception:
                    pass
                return jsonify({
                    "error": "shortclip_stitch_unavailable",
                    "detail": "one or both source chunks no longer on the station",
                }), 404
            out_headers: dict[str, str] = {
                "Content-Type": resp.headers.get("Content-Type", "video/mp4"),
            }
            for h in ("Content-Length", "Content-Disposition"):
                if h in resp.headers:
                    out_headers[h] = resp.headers[h]
            return Response(
                _streaming_proxy(resp),
                status=200,
                headers=out_headers,
            )
        except Exception as exc:
            return _proxy_error_response(host_key, exc)

    @app.route("/loop_clip/<host_key>/<station_code>/<date>/<filename>")
    @require_station
    def loop_clip(host_key: str, station_code: str, date: str, filename: str):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
        )
        try:
            start = float(request.args.get("start", 0))
            end   = float(request.args.get("end",   0))
            loops = max(1, min(10, int(request.args.get("loops", 2))))
        except (ValueError, TypeError):
            abort(400)
        if end <= start:
            abort(400)
        duration = end - start

        # Fetch the trimmed segment from the station's shortclip endpoint
        seg_url = _media_url(host_key, "api/shortclip", station_code, date, filename)
        seg_url += f"?ss={start:.3f}&t={duration:.3f}"
        try:
            resp = _session_for_url(seg_url).get(seg_url, timeout=120, stream=True)
            if resp.status_code != 200:
                try:
                    resp.close()
                except Exception:
                    pass
                return jsonify({"error": "source_unavailable",
                                "detail": "clip not found on station"}), 404

            with tempfile.TemporaryDirectory() as tmpdir:
                seg_path    = f"{tmpdir}/segment.mp4"
                concat_path = f"{tmpdir}/concat.txt"
                output_path = f"{tmpdir}/output.mp4"

                with open(seg_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        f.write(chunk)

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
                data,
                status=200,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Disposition": f'attachment; filename="{outname}"',
                    "Content-Length": str(len(data)),
                },
            )
        except subprocess.CalledProcessError as exc:
            logger.error("loop_clip ffmpeg failed: %s", exc.stderr)
            abort(500)
        except Exception as exc:
            return _proxy_error_response(host_key, exc)

    @app.route("/download_cropped/<host_key>/<station_code>/<date>/<filename>")
    @require_station
    def download_cropped(host_key: str, station_code: str, date: str, filename: str):
        """Crop a trimmed clip to a region of interest and scale to 1280×720."""
        _validate_media_params(host_key, station_code, date, filename, r"^[\w._-]+\.mkv$")
        try:
            x  = max(0.0, min(0.99, float(request.args.get("x", 0))))
            y  = max(0.0, min(0.99, float(request.args.get("y", 0))))
            w  = max(0.01, min(1.0,  float(request.args.get("w", 1))))
            h  = max(0.01, min(1.0,  float(request.args.get("h", 1))))
            ss = max(0.0,  float(request.args.get("ss", 0)))
            t  = max(0.1,  float(request.args.get("t",  20)))
        except (ValueError, TypeError):
            abort(400)
        # Clamp so crop doesn't exceed frame bounds
        w = min(w, 1.0 - x)
        h = min(h, 1.0 - y)

        out_fd, out_str = tempfile.mkstemp(suffix=".mp4", prefix="crop_")
        os.close(out_fd)
        out_path = out_str
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                # Resolve source — archive (processed, with overlay) first,
                # then station's color_capture endpoint as fallback.
                archive_file = ARCHIVE_PATH / station_code / date / "meteors" / filename
                if archive_file.exists() and archive_file.is_file():
                    in_path = str(archive_file)
                else:
                    try:
                        src_url = _media_url(host_key, "color_capture", station_code, date, filename)
                    except Exception:
                        os.unlink(out_path)
                        return jsonify({"error": "source_unavailable",
                                        "detail": "station unreachable"}), 502
                    resp = _session_for_url(src_url).get(src_url, timeout=120, stream=True)
                    if resp.status_code != 200:
                        try:
                            resp.close()
                        except Exception:
                            pass
                        os.unlink(out_path)
                        return jsonify({"error": "source_unavailable",
                                        "detail": "clip not found"}), 404
                    download_in = f"{tmpdir}/input.mkv"
                    try:
                        ok = _stream_download_capped(resp, download_in, _CROP_SOURCE_MAX_BYTES)
                    finally:
                        resp.close()
                    if not ok:
                        os.unlink(out_path)
                        return jsonify({"error": "source_too_large",
                                        "detail": "clip exceeds size cap"}), 413
                    in_path = download_in

                overlay_cfg, station_cfg_ov = _get_overlay_cfg(host_key, station_code)
                cmd = _build_crop_cmd(
                    in_path, out_path, x, y, w, h,
                    station_code, filename, ss, t,
                    overlay_cfg, station_cfg_ov,
                )
                subprocess.run(cmd, check=True, capture_output=True, timeout=180)

            stem    = filename.rsplit(".", 1)[0]
            outname = f"{stem}_crop.mp4"
            resp_out = send_file(
                out_path, mimetype="video/mp4",
                as_attachment=True, download_name=outname,
            )
            resp_out.call_on_close(lambda: _silent_unlink_path(out_path))
            return resp_out
        except subprocess.CalledProcessError as exc:
            _silent_unlink_path(out_path)
            logger.error("download_cropped ffmpeg failed: %s", exc.stderr)
            abort(500)
        except Exception as exc:
            _silent_unlink_path(out_path)
            return _proxy_error_response(host_key, exc)

    @app.route("/download_cropped_frame/<host_key>/<station_code>/<date>/<filename>")
    @require_station
    def download_cropped_frame(host_key: str, station_code: str, date: str, filename: str):
        """Extract a single cropped frame with the same station overlay as clips."""
        _validate_media_params(host_key, station_code, date, filename, r"^[\w._-]+\.mkv$")
        try:
            x  = max(0.0, min(0.99, float(request.args.get("x", 0))))
            y  = max(0.0, min(0.99, float(request.args.get("y", 0))))
            w  = max(0.01, min(1.0,  float(request.args.get("w", 1))))
            h  = max(0.01, min(1.0,  float(request.args.get("h", 1))))
            ss = max(0.0,  float(request.args.get("ss", 0)))
        except (ValueError, TypeError):
            abort(400)
        w = min(w, 1.0 - x)
        h = min(h, 1.0 - y)

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                archive_file = ARCHIVE_PATH / station_code / date / "meteors" / filename
                if archive_file.exists() and archive_file.is_file():
                    in_path = str(archive_file)
                else:
                    try:
                        src_url = _media_url(host_key, "color_capture", station_code, date, filename)
                    except Exception:
                        return jsonify({"error": "source_unavailable",
                                        "detail": "station unreachable"}), 502
                    resp = _session_for_url(src_url).get(src_url, timeout=120, stream=True)
                    if resp.status_code != 200:
                        try:
                            resp.close()
                        except Exception:
                            pass
                        return jsonify({"error": "source_unavailable",
                                        "detail": "clip not found"}), 404
                    download_in = f"{tmpdir}/input.mkv"
                    try:
                        ok = _stream_download_capped(resp, download_in, _CROP_SOURCE_MAX_BYTES)
                    finally:
                        resp.close()
                    if not ok:
                        return jsonify({"error": "source_too_large",
                                        "detail": "clip exceeds size cap"}), 413
                    in_path = download_in

                overlay_cfg, station_cfg_ov = _get_overlay_cfg(host_key, station_code)
                out_path = f"{tmpdir}/frame.jpg"
                cmd = _build_crop_cmd(
                    in_path, out_path, x, y, w, h,
                    station_code, filename, ss, 0,
                    overlay_cfg, station_cfg_ov,
                    vframes=1,
                )
                subprocess.run(cmd, check=True, capture_output=True, timeout=60)
                with open(out_path, "rb") as f:
                    data = f.read()

            stem    = filename.rsplit(".", 1)[0]
            sec_str = f"{ss:.2f}".replace(".", "s")
            outname = f"{stem}_crop_{sec_str}.jpg"
            return Response(
                data, status=200,
                headers={
                    "Content-Type": "image/jpeg",
                    "Content-Disposition": f'attachment; filename="{outname}"',
                    "Content-Length": str(len(data)),
                },
            )
        except subprocess.CalledProcessError as exc:
            logger.error("download_cropped_frame ffmpeg failed: %s", exc.stderr)
            abort(500)
        except Exception as exc:
            return _proxy_error_response(host_key, exc)

    @app.route("/cropped_loop_clip/<host_key>/<station_code>/<date>/<filename>")
    @require_station
    def cropped_loop_clip(host_key: str, station_code: str, date: str, filename: str):
        """Crop a trimmed clip, add overlay, and loop N times."""
        _validate_media_params(host_key, station_code, date, filename, r"^[\w._-]+\.mkv$")
        try:
            x     = max(0.0, min(0.99, float(request.args.get("x", 0))))
            y     = max(0.0, min(0.99, float(request.args.get("y", 0))))
            w     = max(0.01, min(1.0,  float(request.args.get("w", 1))))
            h     = max(0.01, min(1.0,  float(request.args.get("h", 1))))
            ss    = max(0.0,  float(request.args.get("ss", 0)))
            t     = max(0.1,  float(request.args.get("t",  20)))
            loops = max(1, min(10, int(request.args.get("loops", 2))))
        except (ValueError, TypeError):
            abort(400)
        w = min(w, 1.0 - x)
        h = min(h, 1.0 - y)

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                archive_file = ARCHIVE_PATH / station_code / date / "meteors" / filename
                if archive_file.exists() and archive_file.is_file():
                    in_path = str(archive_file)
                else:
                    try:
                        src_url = _media_url(host_key, "color_capture", station_code, date, filename)
                    except Exception:
                        return jsonify({"error": "source_unavailable",
                                        "detail": "station unreachable"}), 502
                    resp = _session_for_url(src_url).get(src_url, timeout=120, stream=True)
                    if resp.status_code != 200:
                        try:
                            resp.close()
                        except Exception:
                            pass
                        return jsonify({"error": "source_unavailable",
                                        "detail": "clip not found"}), 404
                    download_in = f"{tmpdir}/input.mkv"
                    try:
                        ok = _stream_download_capped(resp, download_in, _CROP_SOURCE_MAX_BYTES)
                    finally:
                        resp.close()
                    if not ok:
                        return jsonify({"error": "source_too_large",
                                        "detail": "clip exceeds size cap"}), 413
                    in_path = download_in

                overlay_cfg, station_cfg_ov = _get_overlay_cfg(host_key, station_code)
                seg_path    = f"{tmpdir}/segment.mp4"
                concat_path = f"{tmpdir}/concat.txt"
                out_path    = f"{tmpdir}/output.mp4"
                cmd = _build_crop_cmd(
                    in_path, seg_path, x, y, w, h,
                    station_code, filename, ss, t,
                    overlay_cfg, station_cfg_ov,
                )
                subprocess.run(cmd, check=True, capture_output=True, timeout=180)
                with open(concat_path, "w") as f:
                    for _ in range(loops):
                        f.write(f"file '{seg_path}'\n")
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", concat_path, "-c", "copy", out_path],
                    check=True, capture_output=True, timeout=120,
                )
                with open(out_path, "rb") as f:
                    data = f.read()

            stem    = filename.rsplit(".", 1)[0]
            outname = f"{stem}_crop_loop{loops}x.mp4"
            return Response(
                data, status=200,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Disposition": f'attachment; filename="{outname}"',
                    "Content-Length": str(len(data)),
                },
            )
        except subprocess.CalledProcessError as exc:
            logger.error("cropped_loop_clip ffmpeg failed: %s", exc.stderr)
            abort(500)
        except Exception as exc:
            return _proxy_error_response(host_key, exc)

    @app.route("/api/batch-download/<host_key>/<station_code>/<date>", methods=["POST"])
    @require_station
    def api_batch_download(host_key: str, station_code: str, date: str):
        if not re.match(r"^[A-Z0-9]+$", station_code, re.IGNORECASE):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)

        filenames = request.form.getlist("files")
        if not filenames or len(filenames) > 50:
            abort(400)
        for fn in filenames:
            if not re.match(r"^[\w._-]+_color\.mkv$", fn):
                abort(400)

        # Shared aggregate-size budget across the parallel fetch workers. The
        # old code buffered every file in RAM and then built the whole ZIP in
        # a second in-RAM buffer (peak ~2x the data) -- a single 50-file
        # request could OOM the worker. We now stream each fetched file to a
        # temp dir on disk, enforce a per-request aggregate cap, and stream the
        # ZIP back to the client without holding it all in memory.
        budget_lock = threading.Lock()
        budget = {"used": 0}
        tmpdir = tempfile.mkdtemp(prefix="batchdl_")

        def _fetch_one(args: tuple[int, str]) -> tuple[str, str, Path] | None:
            # Combine lock-type lookup + media fetch into a single future
            # per file so 8 workers run the (lock + media) pairs in
            # parallel. The previous serial loop paid ~125 ms x 2 RTT
            # x N files at minimum; the bottleneck is now the slowest
            # single file rather than their sum.
            idx, fn = args
            try:
                lock_url = station_url(
                    config, tunnels, host_key,
                    f"/api/lock/{station_code}/{date}/{fn}",
                )
                sess = _session_for_url(lock_url)
            except Exception:
                return None
            lock_type = None
            try:
                lr = sess.get(lock_url, timeout=5)
                if lr.ok:
                    lock_type = lr.json().get("lock_type")
            except Exception:
                pass

            if lock_type == "detection":
                media_url = station_url(
                    config, tunnels, host_key,
                    f"/api/shortclip/{station_code}/{date}/{fn}",
                )
                zip_name = fn.replace("_color.mkv", "_clip.mp4")
            else:
                media_url = station_url(
                    config, tunnels, host_key,
                    f"/color_capture/{station_code}/{date}/{fn}",
                )
                zip_name = fn

            out_path = Path(tmpdir) / f"{idx:03d}.part"
            try:
                resp = _session_for_url(media_url).get(
                    media_url, timeout=120, stream=True,
                )
                if not resp.ok:
                    resp.close()
                    return None
                written = 0
                try:
                    with open(out_path, "wb") as f:
                        for chunk in resp.iter_content(65536):
                            written += len(chunk)
                            # Per-file cap (a single bad chunk can't run away)...
                            if written > _BATCH_PER_FILE_MAX_BYTES:
                                return None
                            # ...and the shared per-request aggregate cap.
                            with budget_lock:
                                if budget["used"] + len(chunk) > _BATCH_TOTAL_MAX_BYTES:
                                    return None
                                budget["used"] += len(chunk)
                            f.write(chunk)
                finally:
                    resp.close()
                return fn, zip_name, out_path
            except Exception:
                return None

        results: dict[str, tuple[str, Path]] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for r in pool.map(_fetch_one, list(enumerate(filenames))):
                if r is None:
                    continue
                fn, zip_name, out_path = r
                results[fn] = (zip_name, out_path)

        # Preserve the caller-supplied order so the zip matches the selection
        # UI on the client side.
        entries = [
            (results[fn][0], results[fn][1])
            for fn in filenames
            if fn in results
        ]

        zip_name  = f"rovimen_{station_code}_{date}.zip"
        return Response(
            stream_with_context(
                _stream_zip_from_files(entries, cleanup_dir=tmpdir)
            ),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
        )

    @app.route(
        "/api/lock/<host_key>/<station_code>/<date>/<filename>", methods=["POST"]
    )
    @require_station
    def api_lock(
        host_key: str, station_code: str, date: str, filename: str
    ):
        _validate_media_params(
            host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
        )
        if command_dispatch.should_push(config, host_key):
            body = request.get_json(silent=True) or {}
            locked = bool(body.get("locked", True))
            try:
                cmd_id = command_dispatch.enqueue_signed_command(
                    host_key=host_key, type="lock_clip",
                    args={
                        "cam": station_code, "date": date,
                        "filename": filename, "locked": locked,
                    },
                )
            except command_dispatch.SigningUnavailable:
                return command_dispatch.signing_unavailable_response()
            _invalidate_proxy_cache(host_key, f"/api/chunks/{station_code}/{date}")
            return command_dispatch.queued_response(host_key, "lock_clip", cmd_id)
        try:
            url = station_url(
                config, tunnels, host_key,
                f"/api/lock/{station_code}/{date}/{filename}",
            )
            resp = _session_for_url(url).post(
                url,
                json=request.get_json(silent=True),
                timeout=10,
            )
            resp.raise_for_status()
            # Drop any cached chunks payload for this (host, cam, date) so the
            # next GET /api/videodb/chunks/... fetches fresh from the station
            # and reflects the lock change immediately instead of replaying a
            # stale `locked=False` body from the proxy cache. The route is a
            # no-op if the key was never written (the chunks endpoint doesn't
            # currently route through _proxy_get), but matches the key shape
            # _proxy_cache uses elsewhere so the invalidation stays correct
            # if the chunks path is later moved behind _proxy_get.
            _invalidate_proxy_cache(host_key, f"/api/chunks/{station_code}/{date}")
            return jsonify(resp.json())
        except Exception as exc:
            return _proxy_error_response(host_key, exc)

    # ── Live camera stream (proxied via SSH+ffmpeg on station) ────────────

    @app.route("/stream/<host_key>/<camera_code>")
    @require_station
    def stream(host_key: str, camera_code: str):
        station = _require_station(host_key)
        if not re.match(r"^[A-Z0-9]+$", camera_code, re.IGNORECASE):
            abort(400)

        cam = next((c for c in station.cameras if c.code == camera_code), None)
        if cam is None:
            abort(404)

        # Concurrency cap: per-(user, host, camera) + global. Both are
        # acquired non-blockingly so a refused request fails fast with 429
        # rather than tying up a Flask worker.
        user = session.get("user") or "anonymous"
        triple_sem = _live_stream_triple_sem(user, host_key, camera_code)
        if not triple_sem.acquire(blocking=False):
            return jsonify({
                "error": "too_many_streams",
                "scope": "per_user_station_camera",
                "retry_after_s": _LIVE_STREAM_RETRY_AFTER_S,
            }), 429
        if not _live_stream_global_sem.acquire(blocking=False):
            triple_sem.release()
            return jsonify({
                "error": "too_many_streams",
                "scope": "global",
                "retry_after_s": _LIVE_STREAM_RETRY_AFTER_S,
            }), 429

        rotate_flag = "1" if cam.rotate else "0"
        stream_path = f"/api/stream/{camera_code}?rotate={rotate_flag}"

        try:
            url = station_url(config, tunnels, host_key, stream_path)
        except _TunnelDown:
            triple_sem.release()
            _live_stream_global_sem.release()
            return jsonify({"error": "station_unreachable"}), 502

        try:
            upstream = requests.get(url, stream=True, timeout=(8, 60))
            upstream.raise_for_status()
        except Exception as exc:
            triple_sem.release()
            _live_stream_global_sem.release()
            logger.warning("Live stream %s/%s failed: %s", host_key, camera_code, exc)
            return jsonify({"error": "stream_unavailable"}), 502

        def relay():
            try:
                for chunk in upstream.iter_content(chunk_size=32768):
                    if chunk:
                        yield chunk
            except GeneratorExit:
                pass
            finally:
                upstream.close()
                try:
                    _live_stream_global_sem.release()
                except ValueError:
                    pass
                try:
                    triple_sem.release()
                except ValueError:
                    pass

        return Response(
            relay(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
