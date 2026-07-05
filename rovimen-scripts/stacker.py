#!/usr/bin/env python3
"""stacker.py — Per-chunk maxpixel stack builder.

Codename: palinopsia

Reads each 20-second MKV chunk via ffmpeg rawvideo pipe, computes the
per-pixel maximum across all frames, applies OSD overlay, saves a WebP
stack image and thumbnail, and writes a .stacked touch sidecar.

Decoding is always software (CPU).  VAAPI decode was benchmarked as slower
than software on the i5-8500 (i965 driver) due to GPU↔CPU transfer overhead,
and has been removed.  VAAPI is used only by the encoder (h264_vaapi encode).

Called by the morning processing sequence (process_night) or standalone:
    python stacker.py --chunk /path/to/chunk.mkv -c config.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

import overlay
import flags_manager

logger = logging.getLogger(__name__)

# Serialises flags_manager read-modify-write when multiple workers run in parallel.
_flags_lock = threading.Lock()

WIDTH = 1280
HEIGHT = 720

_CHUNK_RE = __import__('re').compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_color\.mkv$')


def _load_fpn_correction(station_id: str, night_str: str, cfg: dict) -> np.ndarray | None:
    """Load the FPN correction array for this camera-night, or None."""
    fpn_svc = cfg.get('services', {}).get('fpn_calibration', {})
    if not fpn_svc.get('enabled', False):
        return None
    try:
        import fpn_calibration
        return fpn_calibration.load_correction_np(station_id, night_str, cfg)
    except Exception:
        return None


def _probe_video_height(mkv: Path) -> int:
    """Return the encoded video height via ffprobe. Falls back to HEIGHT on error."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'stream=height',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(mkv)],
            capture_output=True, text=True,
        )
        return int(result.stdout.strip().splitlines()[0])
    except Exception:
        return HEIGHT


def _probe_fps(mkv: Path) -> float:
    """Read frame rate from stream header. Fast — reads container header only."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=r_frame_rate',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(mkv)],
            capture_output=True, text=True, timeout=5,
        )
        num, den = map(int, r.stdout.strip().split('/'))
        return num / den
    except Exception:
        return 25.0


def _probe_frame_count(mkv: Path) -> int | None:
    """Return frame count from container metadata. Fast — no decoding.

    Returns None if the metadata field is absent or unreadable (some containers
    omit nb_frames). Callers must handle None explicitly.
    """
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=nb_frames',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(mkv)],
            capture_output=True, text=True, timeout=5,
        )
        val = r.stdout.strip()
        if val and val != 'N/A':
            return int(val)
    except Exception:
        pass
    return None


def _decode_args(bar_h: int, threads: int = 0) -> tuple[list[str], list[str]]:
    """Return (pre_input_args, vf_args) for the ffmpeg software decode command.

    threads=0 lets ffmpeg choose automatically.
    """
    vf = ['-vf', f'crop=in_w:in_h-{bar_h}:0:0'] if bar_h > 0 else []
    return ['-threads', str(threads)], vf


def _night_date(date_str: str, time_str: str) -> str:
    if int(time_str[:2]) < 12:
        dt = datetime(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:]),
                      tzinfo=timezone.utc) - timedelta(days=1)
        return dt.strftime('%Y%m%d')
    return date_str


def _subtract_background(frame: np.ndarray, pct: float) -> np.ndarray:
    """Subtract a per-channel percentile pedestal from frame (display-only).

    Uses a stride of 4 in both spatial dimensions (~1/16 of pixels) so the
    np.percentile call costs ~1 ms per 720p frame.  Total overhead per night
    is ~3-5 s (500 frames * ~1 ms).  The result is clamped to [0, 255].
    """
    # Subsample for fast percentile estimation (every 4th pixel both axes)
    sample = frame[::4, ::4]
    bg = np.percentile(sample, pct, axis=(0, 1)).astype(np.int16)  # shape (3,)
    return np.clip(frame.astype(np.int16) - bg, 0, 255).astype(np.uint8)


def _compute_maxpixel(
    mkv: Path,
    bar_h: int = 0,
    threads: int = 0,
    bg_subtract_pct: float | None = None,
    fpn_correction: np.ndarray | None = None,
) -> tuple[np.ndarray | None, np.ndarray | None, int]:
    """Compute per-pixel max *and* per-pixel mean across all frames.

    Returns ``(maxpx, avgpx, n)``. The avg frame is used downstream as the
    reference for adaptive colour calibration (percentile-masked sky pixels);
    it averages out meteors and transient lights, unlike maxpx.

    If ``bar_h > 0``, the encoded video is HEIGHT+bar_h rows tall (annotation
    bar at bottom). ffmpeg crops the bar before outputting raw bytes so the
    numpy buffer always receives exactly WIDTH * HEIGHT * 3 bytes per frame.

    If ``bg_subtract_pct`` is set, each frame has its Nth-percentile per-channel
    background pedestal subtracted before both accumulators.  This is a
    display-only adjustment for light-polluted sites (e.g. Bortle 8-9).
    """
    pre, vf = _decode_args(bar_h, threads)
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        *pre,
        '-i', str(mkv),
        *vf,
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1',
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        logger.error('ffmpeg not found')
        return None, None, 0

    frame_shape = (HEIGHT, WIDTH, 3)
    frame_bytes = WIDTH * HEIGHT * 3
    maxpx = np.zeros(frame_shape, dtype=np.uint8)
    avgpx_sum = np.zeros(frame_shape, dtype=np.float32)
    n = 0
    batch_bytes = frame_bytes * 10
    try:
        while True:
            raw = proc.stdout.read(batch_bytes)
            if not raw:
                break
            frames = len(raw) // frame_bytes
            if frames == 0:
                break
            arr = np.frombuffer(raw[:frames * frame_bytes], dtype=np.uint8).reshape(frames, HEIGHT, WIDTH, 3)
            if fpn_correction is not None:
                arr = np.clip(
                    arr.astype(np.int16) - fpn_correction[np.newaxis],
                    0, 255,
                ).astype(np.uint8)
            if bg_subtract_pct is not None:
                # Subtract per-channel background pedestal before both
                # accumulators.  ~1 ms per frame overhead.
                # Note: arr from frombuffer is read-only — accumulate directly.
                for i in range(frames):
                    adj = _subtract_background(arr[i], bg_subtract_pct)
                    np.maximum(maxpx, adj, out=maxpx)
                    avgpx_sum += adj.astype(np.float32)
            else:
                np.maximum(maxpx, arr.max(axis=0), out=maxpx)
                avgpx_sum += arr.sum(axis=0, dtype=np.float32)
            n += frames
    finally:
        proc.stdout.close()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            logger.warning('ffmpeg hung during maxpixel — killed: %s', mkv.name)

    if n == 0:
        return None, None, 0
    avgpx = (avgpx_sum / n).clip(0, 255).astype(np.uint8)
    return maxpx, avgpx, n


def _save_webp_lossless(image: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp.webp')
    try:
        Image.fromarray(image, 'RGB').save(str(tmp), 'webp', lossless=True)
        os.replace(tmp, path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f'failed writing stack image: {exc}') from exc


def _save_thumbnail(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix('.tmp.webp')
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-y', '-i', str(src),
             '-vf', 'scale=480:-2', '-quality', '80', str(tmp)],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            stderr = result.stderr.decode(errors='replace').strip()
            logger.warning('Thumbnail failed for %s (rc=%d, size=%d): %s',
                           src.name, result.returncode,
                           tmp.stat().st_size if tmp.exists() else -1, stderr)
            tmp.unlink(missing_ok=True)
            return
        tmp.rename(dst)
    except Exception as e:
        logger.warning('Thumbnail failed for %s: %s', src.name, e)
        tmp.unlink(missing_ok=True)


def _finish_stack(
    mkv: Path,
    maxpx: np.ndarray,
    avgpx: np.ndarray | None,
    n_frames: int,
    station_id: str,
    night_str: str,
    date_str: str,
    time_str: str,
    cfg: dict,
    is_encoded: bool,
) -> None:
    """Save stack WebP + thumbnail + overlay, mark stacked.

    Shared by process_chunk (standalone) and _process_one (night worker).
    """
    import color_calibration as cc

    station_cfg = cfg.get('stations', {}).get(station_id, {})
    station_cfg = overlay.enrich_station_cfg_pointing(station_id, station_cfg, cfg)
    overlay_cfg = cfg.get('overlay')
    overlay_enabled = overlay_cfg.get('enabled', True) if overlay_cfg else True

    if not is_encoded:
        # Adaptive WB targeting the fleet's aesthetic night-sky colour,
        # derived from the chunk's own avg frame (masks out transient lights;
        # falls back to maxpx if no avg was provided). The camera's
        # ``color_post`` can override target + gamma (rare).
        ref = avgpx if avgpx is not None else maxpx
        gr, gg, gb, gamma = cc.resolve_calibration_from_frame(ref, station_cfg)
        rot = station_cfg.get('rotate', False)
        maxpx = cc.apply_calibration_np(maxpx, gr, gg, gb, gamma, rotate=rot)
        # Apply the SAME calibration to avgpx so that the downstream
        # ``max - avg`` meteor compositing (build_night_color_meteor_stack)
        # works in a single consistent colour space. Without this, the
        # difference picks up the calibration delta as spurious colour.
        if avgpx is not None:
            avgpx = cc.apply_calibration_np(avgpx, gr, gg, gb, gamma, rotate=rot)

    capture_path = Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(Path.home() / 'color_capture')
    )
    stacks_dir = capture_path / station_id / night_str / 'stacks'
    thumbs_dir = stacks_dir / 'thumbs'
    stacks_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.strptime(f'{date_str}_{time_str}', '%Y%m%d_%H%M%S')
    stem = f'{station_id}_{ts.strftime("%Y%m%d_%H%M%S")}_stack'
    webp = stacks_dir / f'{stem}.webp'

    _save_webp_lossless(maxpx, webp)

    # Save the chunk's avepixel alongside so dawn can build a night-level
    # colour meteor stack via ``max - avg`` per chunk, max-combined across
    # the whole night. Using WebP lossy is fine here (we never display this
    # image — only subtract it from the maxpx); ~50 KB per chunk vs ~500 KB
    # lossless, saves ~10 MB/cam/night.
    if avgpx is not None:
        avg_webp = stacks_dir / f'{stem}_avg.webp'
        try:
            Image.fromarray(avgpx, mode='RGB').save(
                avg_webp, format='WEBP', quality=85, method=4,
            )
        except Exception as e:
            logger.warning('[%s] avg save failed for %s: %s', station_id, avg_webp.name, e)

    if overlay_cfg and overlay_enabled:
        try:
            overlay.annotate_still(
                webp, webp, overlay_cfg, station_id, station_cfg,
                int(ts.replace(tzinfo=timezone.utc).timestamp()),
            )
        except Exception as e:
            logger.warning('[%s] Overlay failed for %s: %s', station_id, webp.name, e)
    thumb_dst = thumbs_dir / webp.name
    if not thumb_dst.exists():
        _save_thumbnail(webp, thumb_dst)

    with _flags_lock:
        flags_manager.mark_stacked(station_id, night_str, mkv.name, cfg)


def _process_one(
    mkv: Path,
    cfg: dict,
    is_encoded: bool,
    bar_h: int,
    ffmpeg_threads: int = 0,
    bg_subtract_pct: float | None = None,
    fpn_correction: np.ndarray | None = None,
) -> bool:
    """Stack a single MKV chunk. Called by the process_night worker pool.

    Idempotent — skips if already stacked. Returns True on success.
    """
    m = _CHUNK_RE.match(mkv.name)
    if not m:
        return False
    station_id, date_str, time_str = m.group(1), m.group(2), m.group(3)
    night_str = _night_date(date_str, time_str)

    with _flags_lock:
        chunk_state = (flags_manager.load(station_id, night_str, cfg)
                       .get('chunks', {}).get(mkv.name, {}))
    if chunk_state.get('stacked', False) or Path(str(mkv) + '.stacked').exists():
        return True
    if not chunk_state.get('ready', False):
        return False
    if not mkv.exists() or mkv.stat().st_size == 0:
        logger.warning('[%s] Skipping empty/missing MKV: %s', station_id, mkv.name)
        return False

    t0 = time.monotonic()
    maxpx, avgpx, n_frames = _compute_maxpixel(mkv, bar_h=bar_h, threads=ffmpeg_threads,
                                                bg_subtract_pct=bg_subtract_pct,
                                                fpn_correction=fpn_correction)
    if maxpx is None:
        logger.warning('[%s] Maxpixel failed (0 frames): %s', station_id, mkv.name)
        return False

    try:
        _finish_stack(mkv, maxpx, avgpx, n_frames, station_id, night_str,
                      date_str, time_str, cfg, is_encoded)
    except Exception:
        logger.exception('[%s] Post-process failed for %s', station_id, mkv.name)
        return False

    logger.info('[%s] Stacked %s  frames=%d  wall=%.2fs',
                station_id, mkv.name, n_frames, time.monotonic() - t0)
    return True


def process_chunk(mkv: Path, cfg: dict, is_encoded: bool = False) -> bool:
    """Compute maxpixel stack for chunk. Marks stacked in state.json on success.

    Idempotent: returns True immediately if already stacked.

    is_encoded: reserved for future use. color_capture always produces raw MKVs,
      so this is always False. When False, rotation and color gains are applied
      to the final stack image.
    """
    # palinopsia: per-chunk maxpixel accumulator

    m = _CHUNK_RE.match(mkv.name)
    if not m:
        logger.warning('Skipping — filename does not match pattern: %s', mkv.name)
        return False
    station_id, date_str, time_str = m.group(1), m.group(2), m.group(3)
    night_str = _night_date(date_str, time_str)
    chunk_state = flags_manager.load(station_id, night_str, cfg).get('chunks', {}).get(mkv.name, {})
    if chunk_state.get('stacked', False):
        return True
    if Path(str(mkv) + '.stacked').exists():
        return True

    if not chunk_state.get('ready', False):
        logger.warning('Skipping — chunk not ready: %s', mkv.name)
        return False
    if not mkv.exists() or mkv.stat().st_size == 0:
        logger.warning('[%s] Skipping empty/missing MKV: %s', station_id, mkv.name)
        return False

    bar_h = 0
    if is_encoded:
        actual_h = _probe_video_height(mkv)
        bar_h = max(0, actual_h - HEIGHT)

    display_levels = cfg.get('display_levels') or {}
    bg_pct = display_levels.get('background_subtract_pct')

    fpn_corr = _load_fpn_correction(station_id, night_str, cfg)

    t0 = time.monotonic()
    maxpx, avgpx, n_frames = _compute_maxpixel(mkv, bar_h=bar_h, bg_subtract_pct=bg_pct,
                                                fpn_correction=fpn_corr)
    if maxpx is None:
        logger.warning('[%s] Maxpixel failed (0 frames): %s', station_id, mkv.name)
        return False

    _finish_stack(mkv, maxpx, avgpx, n_frames, station_id, night_str, date_str, time_str, cfg, is_encoded)
    wall = time.monotonic() - t0
    logger.info('[%s] Stacked %s  frames=%d  wall=%.2fs', station_id, mkv.name, n_frames, wall)
    return True


def _stack_night_concat(
    station_id: str,
    pending: list[Path],
    chunks: list[Path],
    date_str: str,
    cfg: dict,
    is_encoded: bool,
    bar_h: int,
    workers: int,
) -> int:
    """Stack via ffmpeg concat batches.

    One ffmpeg process per batch reads chunks sequentially.  Python splits the
    output frame stream into per-chunk windows and accumulates the maxpixel
    stack for each.

    NOTE: concat mode is known to silently abandon chunks when a chunk has fewer
    decoded frames than its container metadata claims (dropped frames, early
    close).  Prefer perfile mode.  This path is retained only as a fallback.

    Returns count of successfully stacked chunks.
    """
    import tempfile

    segment_duration = cfg.get('segment_duration', 20)
    fps = _probe_fps(pending[0])
    frames_per_chunk = round(segment_duration * fps)
    batch_size = max(1, cfg.get('stack_batch_size', 500))
    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    display_levels = cfg.get('display_levels') or {}
    bg_pct = display_levels.get('background_subtract_pct')
    fpn_corr = _load_fpn_correction(station_id, date_str, cfg)

    logger.info(
        '[%s] Stacking %d/%d chunks for %s '
        '(mode=concat, workers=%d, batch=%d, frames/chunk=%d, decode=software, fpn=%s)',
        station_id, len(pending), len(chunks), date_str,
        workers, batch_size, frames_per_chunk, 'on' if fpn_corr is not None else 'off',
    )

    frame_shape = (HEIGHT, WIDTH, 3)
    frame_bytes = WIDTH * HEIGHT * 3

    def _run_batch(batch: list[Path]) -> int:
        items: list[tuple[Path, str, str, str, str, int]] = []
        perfile_fallback: list[Path] = []
        for mkv in batch:
            m = _CHUNK_RE.match(mkv.name)
            if not m:
                continue
            sid, ds, ts = m.group(1), m.group(2), m.group(3)
            ns = _night_date(ds, ts)
            with _flags_lock:
                cstate = (flags_manager.load(sid, ns, cfg)
                          .get('chunks', {}).get(mkv.name, {}))
            if cstate.get('stacked', False) or Path(str(mkv) + '.stacked').exists():
                continue
            if not cstate.get('ready', False):
                continue
            nframes = _probe_frame_count(mkv)
            if nframes is None:
                logger.debug('[%s] nb_frames unavailable for %s — queued for per-file fallback',
                             sid, mkv.name)
                perfile_fallback.append(mkv)
            else:
                items.append((mkv, sid, ns, ds, ts, nframes))

        ok = 0

        if items:
            fd, concat_path = tempfile.mkstemp(suffix='.txt', dir='/tmp', prefix='rovimen_stack_')
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write('ffconcat version 1.0\n')
                    for mkv, *_ in items:
                        f.write(f"file '{mkv.resolve()}'\n")

                pre, vf = _decode_args(bar_h)
                cmd = [
                    'ffmpeg', '-hide_banner', '-loglevel', 'error',
                    '-f', 'concat', '-safe', '0',
                    *pre, '-i', concat_path,
                    *vf,
                    '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1',
                ]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    for mkv, sid, ns, ds, ts, nframes in items:
                        t0 = time.monotonic()
                        maxpx = np.zeros(frame_shape, dtype=np.uint8)
                        avgpx_sum = np.zeros(frame_shape, dtype=np.float32)
                        n = 0
                        for _ in range(nframes):
                            raw = proc.stdout.read(frame_bytes)
                            if len(raw) < frame_bytes:
                                if n == 0:
                                    logger.warning('[%s] Pipe exhausted at %s — batch skipped',
                                                   sid, mkv.name)
                                break
                            frame = np.frombuffer(raw, dtype=np.uint8).reshape(frame_shape)
                            if fpn_corr is not None:
                                frame = np.clip(
                                    frame.astype(np.int16) - fpn_corr,
                                    0, 255,
                                ).astype(np.uint8)
                            if bg_pct is not None:
                                frame = _subtract_background(frame, bg_pct)
                            np.maximum(maxpx, frame, out=maxpx)
                            avgpx_sum += frame.astype(np.float32)
                            n += 1
                        if n == 0:
                            break
                        avgpx = (avgpx_sum / n).clip(0, 255).astype(np.uint8)
                        try:
                            _finish_stack(mkv, maxpx, avgpx, n, sid, ns, ds, ts, cfg, is_encoded)
                            logger.info('[%s] Stacked %s  frames=%d  wall=%.2fs',
                                        sid, mkv.name, n, time.monotonic() - t0)
                            ok += 1
                        except Exception:
                            logger.exception('[%s] Post-process failed for %s', sid, mkv.name)
                finally:
                    proc.stdout.close()
                    try:
                        proc.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                        logger.warning('ffmpeg hung during concat batch — killed')
            finally:
                os.unlink(concat_path)

        for mkv in perfile_fallback:
            try:
                if _process_one(mkv, cfg, is_encoded, bar_h,
                                bg_subtract_pct=bg_pct,
                                fpn_correction=fpn_corr):
                    ok += 1
            except Exception:
                logger.exception('Per-file fallback failed for %s', mkv.name)

        return ok

    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_batch, b): b for b in batches}
        for fut in as_completed(futures):
            try:
                ok += fut.result()
            except Exception:
                logger.exception('[%s] Batch failed', station_id)
    return ok


def _stack_night_perfile(
    station_id: str,
    pending: list[Path],
    chunks: list[Path],
    date_str: str,
    cfg: dict,
    is_encoded: bool,
    bar_h: int,
    workers: int,
) -> int:
    """Stack via parallel per-file workers.

    Each worker runs its own ffmpeg process on a single chunk.
    Returns count of successfully stacked chunks.
    """
    cpu_count = os.cpu_count() or 4
    ffmpeg_threads = max(1, cpu_count // workers)
    display_levels = cfg.get('display_levels') or {}
    bg_pct = display_levels.get('background_subtract_pct')
    fpn_corr = _load_fpn_correction(station_id, date_str, cfg)

    logger.info(
        '[%s] Stacking %d/%d chunks for %s '
        '(mode=perfile, workers=%d, threads/worker=%d, decode=software, fpn=%s)',
        station_id, len(pending), len(chunks), date_str,
        workers, ffmpeg_threads, 'on' if fpn_corr is not None else 'off',
    )
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_process_one, mkv, cfg, is_encoded,
                        bar_h, ffmpeg_threads, bg_pct,
                        fpn_correction=fpn_corr): mkv
            for mkv in pending
        }
        for fut in as_completed(futures):
            try:
                if fut.result():
                    ok += 1
            except Exception:
                logger.exception('[%s] Worker failed', station_id)
    return ok


def process_night(station_id: str, date_str: str, cfg: dict) -> None:
    """Stack all MKV chunks for a completed night. Idempotent.

    Two modes, selected by ``stack_mode`` in config:
    - ``"perfile"`` (default): one ffmpeg per chunk, parallel workers.
    - ``"concat"``: one ffmpeg per batch — known to silently abandon chunks
      when decoded frame count < container metadata; avoid unless debugging.
    """
    videocapture_path = Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(Path.home() / 'color_capture')
    )
    night_dir = videocapture_path / station_id / date_str
    if not night_dir.exists():
        logger.warning('[%s] Night dir not found for batch stack: %s', station_id, night_dir)
        return

    is_encoded = False  # color_capture always produces raw MKVs
    chunks = sorted(night_dir.glob('*_color.mkv'))
    sstate = flags_manager.load(station_id, date_str, cfg)
    chunk_states = sstate.get('chunks', {})
    pending = [
        mkv for mkv in chunks
        if not chunk_states.get(mkv.name, {}).get('stacked', False)
        and not Path(str(mkv) + '.stacked').exists()
    ]

    if not pending:
        logger.info('[%s] Nothing to stack for %s', station_id, date_str)
        return

    workers = max(1, cfg.get('stack_workers', 2))

    bar_h = 0
    if is_encoded:
        actual_h = _probe_video_height(pending[0])
        bar_h = max(0, actual_h - HEIGHT)

    mode = cfg.get('stack_mode', 'perfile')
    if mode == 'concat':
        ok = _stack_night_concat(station_id, pending, chunks, date_str, cfg,
                                 is_encoded, bar_h, workers)
    else:
        ok = _stack_night_perfile(station_id, pending, chunks, date_str, cfg,
                                  is_encoded, bar_h, workers)

    logger.info('[%s] Stack complete: %d/%d ok for %s', station_id, ok, len(pending), date_str)


def build_night_color_meteor_stack(
    station_id: str,
    night_str: str,
    cfg: dict,
) -> Path | None:
    """Build a per-night colour meteor stack for one camera.

    For every locked chunk in the night we have a ``*_stack.webp`` (maxpx)
    and a ``*_avg.webp`` (avgpx) saved by ``_finish_stack``. ``max - avg``
    per chunk isolates the transient meteor trail (and any other transients
    like aircraft — RMS's detection filter already ran, so locked==meteor).
    We max-combine across the night and apply the station's rotate flag.

    Output: ``<station_id>_<night_str>_color_meteor_stack.webp`` in
    ``<capture_path>/<station_id>/<night_str>/stacks/``.
    Returns the output path on success, or None.
    """
    capture_path = Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(Path.home() / 'color_capture')
    )
    stacks_dir = capture_path / station_id / night_str / 'stacks'
    if not stacks_dir.is_dir():
        logger.info('[%s] no stacks dir for %s — skipping night meteor stack', station_id, night_str)
        return None

    flags = flags_manager.load(station_id, night_str, cfg).get('chunks', {})
    locked_chunk_stems = set()
    for chunk_name, st in flags.items():
        if not isinstance(st, dict):
            continue
        if not st.get('lock'):
            continue
        m = _CHUNK_RE.match(chunk_name)
        if not m:
            continue
        sid, ds, ts = m.group(1), m.group(2), m.group(3)
        locked_chunk_stems.add(f'{sid}_{ds}_{ts}_stack')

    if not locked_chunk_stems:
        logger.info('[%s] no locked chunks for %s — no meteor stack to build', station_id, night_str)
        return None

    accum = None
    used = 0
    for stem in sorted(locked_chunk_stems):
        max_webp = stacks_dir / f'{stem}.webp'
        avg_webp = stacks_dir / f'{stem}_avg.webp'
        if not max_webp.exists() or not avg_webp.exists():
            logger.debug('[%s] skip %s (missing max or avg webp)', station_id, stem)
            continue
        try:
            mx = np.array(Image.open(max_webp).convert('RGB'), dtype=np.int16)
            av = np.array(Image.open(avg_webp).convert('RGB'), dtype=np.int16)
        except Exception as e:
            logger.warning('[%s] could not load %s: %s', station_id, stem, e)
            continue
        # The max stack may have an overlay bar appended (extra rows at bottom).
        # Crop mx to av's spatial dimensions before subtracting so the shapes
        # always align regardless of overlay height.
        if mx.shape != av.shape:
            h = min(mx.shape[0], av.shape[0])
            w = min(mx.shape[1], av.shape[1])
            mx = mx[:h, :w]
            av = av[:h, :w]
        diff = np.clip(mx - av, 0, 255).astype(np.uint8)
        accum = diff if accum is None else np.maximum(accum, diff)
        used += 1

    if accum is None or used == 0:
        logger.info('[%s] no usable meteor pairs for %s', station_id, night_str)
        return None

    # Rotation was already applied to both max and avg stacks when they were
    # saved by _finish_stack, so the difference is already correctly oriented.
    # Do NOT rotate again here.

    # Brighten: diff values are typically small. Linear 4× boost followed by
    # gamma=0.5 (sqrt) lifts dim trails into the visible range while preserving
    # colour ratios. Clip keeps us in [0, 255].
    stretched = np.clip(accum.astype(np.float32) * 2.0, 0, 255)
    accum = (np.power(stretched / 255.0, 0.5) * 255).clip(0, 255).astype(np.uint8)

    out = stacks_dir / f'{station_id}_{night_str}_color_meteor_stack.webp'
    try:
        Image.fromarray(accum, mode='RGB').save(
            out, format='WEBP', lossless=True, quality=100, method=4,
        )
        logger.info('[%s] built colour meteor stack from %d locked chunks → %s',
                    station_id, used, out.name)
        return out
    except Exception as e:
        logger.warning('[%s] meteor stack save failed: %s', station_id, e)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description='Compute maxpixel stack for a chunk')
    parser.add_argument('--chunk', required=True, help='Path to .mkv chunk')
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    with open(args.config) as f:
        cfg = json.load(f)

    ok = process_chunk(Path(args.chunk), cfg)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
