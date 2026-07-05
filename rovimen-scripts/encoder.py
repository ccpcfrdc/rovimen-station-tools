#!/usr/bin/env python3
"""encoder.py — Per-chunk MKV re-encoder.

Codename: coppermind

Applies OSD overlay, rotation and colour calibration to MKV chunks, writing
reencoded=True into state.json on success. Lock state survives the atomic
rename untouched. Whether a clip has been encoded is tracked exclusively by
the reencoded flag — compression_level controls encode quality only.

compression_level scale:
  0  — raw -c:v copy at capture; encoder copies as-is (no OSD applied)
  1  — VAAPI QP19 / libx264 CRF20
  2  — VAAPI QP20 / libx264 CRF21 (default)
  3  — VAAPI QP21 / libx264 CRF22
  4  — VAAPI QP22 / libx264 CRF23

Called by dawn_process Phase 4/5, or standalone:
    python encoder.py --chunk /path/to/chunk.mkv -c config.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from overlay import (
    build_drawtext_annotations,
    enrich_station_cfg_pointing,
    measure_text_width,
)
import flags_manager

logger = logging.getLogger(__name__)

_CHUNK_RE = re.compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_color\.mkv$')
# Stitched boundary clips produced by detection_lock: two time components.
# Groups 1–3 are identical positions to _CHUNK_RE; group 3 is the start time.
_STITCHED_CHUNK_RE = re.compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_\d{6}_color\.mkv$')
COMPRESSION_QP  = {1: 19, 2: 20, 3: 21, 4: 22}   # h264_vaapi QP
COMPRESSION_CRF = {1: 20, 2: 21, 3: 22, 4: 23}   # libx264 CRF (fallback)
CRF_RATE_FLOOR = ['-minrate', '1M', '-maxrate', '8M', '-bufsize', '2M']
# Locked (detected meteor) clips are encoded at the highest quality tier (level 1)
# using the same VAAPI/libx264 path as all other clips.
LOCKED_QUALITY_LEVEL = 1

# Per-device VAAPI availability cache — probed once, reused across all chunks.
_vaapi_ok: dict[str, bool] = {}
_vaapi_device_cache: str | None = None
_vaapi_probe_lock = threading.Lock()

# Per camera/night calibration cache — computed once from the first chunk,
# reused for all subsequent chunks of the same camera/night.
_calibration_cache: dict[tuple[str, str], tuple[float, float, float, float]] = {}
_calibration_cache_lock = threading.Lock()


def _check_vaapi(device: str) -> bool:
    """Test if h264_vaapi encoding works on this device (cached per device)."""
    if device in _vaapi_ok:
        return _vaapi_ok[device]
    try:
        result = subprocess.run(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-vaapi_device', device,
                '-f', 'lavfi', '-i', 'testsrc=duration=0.04:size=32x32:rate=25',
                '-vf', 'format=nv12,hwupload',
                '-c:v', 'h264_vaapi', '-qp', '20',
                '-f', 'null', '-',
            ],
            capture_output=True, timeout=10,
        )
        ok = result.returncode == 0
    except Exception:
        ok = False
    _vaapi_ok[device] = ok
    if not ok:
        logger.warning('VAAPI unavailable on %s', device)
    return ok


def _find_vaapi_device() -> str | None:
    """Return the first working VAAPI device, probed once and cached."""
    global _vaapi_device_cache
    if _vaapi_device_cache is not None:
        return _vaapi_device_cache if _vaapi_device_cache else None
    with _vaapi_probe_lock:
        if _vaapi_device_cache is not None:
            return _vaapi_device_cache if _vaapi_device_cache else None
        dri = Path('/dev/dri')
        for device in sorted(dri.glob('renderD*')) if dri.exists() else []:
            if _check_vaapi(str(device)):
                _vaapi_device_cache = str(device)
                logger.info('VAAPI device: %s', device)
                return _vaapi_device_cache
        _vaapi_device_cache = ''  # sentinel: probed, nothing found
        logger.warning('No working VAAPI device found — using libx264 fallback')
        return None


def _fpn_filter_prefix(fpn_png_path: Path | None) -> str:
    """Return an ffmpeg filter_complex prefix for FPN subtraction.

    When *fpn_png_path* points to a valid correction PNG, returns a filter
    fragment that loads it via ``movie=`` and subtracts it from ``[0:v]``
    using ``blend=all_mode=subtract`` (clamps at 0 — correct for the
    always-positive hot-pixel/row-banding offsets we store).

    The returned string ends with the corrected stream ready for further
    chaining — the caller appends rotation, colour, overlay, etc.

    Returns empty string when FPN is not available.
    """
    if fpn_png_path is None or not fpn_png_path.exists():
        return ''
    try:
        import fpn_calibration
        return fpn_calibration.ffmpeg_filter_prefix(fpn_png_path)
    except ImportError:
        escaped = str(fpn_png_path).replace('\\', '\\\\').replace("'", "\\'").replace(':', '\\:')
        return (
            f"[0:v]format=rgb24[_main];"
            f"movie='{escaped}',format=rgb24[_fpn];"
            f"[_main][_fpn]blend=all_mode=subtract"
        )


def _libx264_thread_count(cfg: dict) -> int:
    """Compute per-chunk thread allocation for libx264.

    In batch mode (morning pass) cfg contains ``_encode_threads`` injected by
    process_night — use it directly.  In real-time capture mode (no override)
    use the conservative per-camera calculation that assumes all cameras may
    be encoding simultaneously.
    """
    if '_encode_threads' in cfg:
        return int(cfg['_encode_threads'])
    total = os.cpu_count() or 4
    max_threads = max(1, total - 1)
    num_cameras = max(1, len(cfg.get('stations', {}) or {}))
    threads_per_cam = 2 if max_threads >= num_cameras * 2 else 1
    return threads_per_cam


def _batch_cfg(cfg: dict) -> dict:
    """Return a shallow-copy of cfg with batch encode thread count injected.

    dawn_encode_parallelism (default cpu_cores // 2) chunks run in parallel,
    each getting cpu_cores // parallelism libx264 threads.  For a 6-core
    machine this gives 3 parallel chunks × 2 threads = 6 cores fully used,
    vs the current 1 thread × 1 chunk = 1 core.
    """
    cpu_cores = cfg.get('_cpu_cores', os.cpu_count() or 4)
    parallelism = max(1, cfg.get('dawn_encode_parallelism', cpu_cores // 2))
    threads_per_chunk = max(1, cpu_cores // parallelism)
    return {**cfg, '_encode_threads': threads_per_chunk, '_encode_parallelism': parallelism}


def _night_date(date_str: str, time_str: str) -> str:
    if int(time_str[:2]) < 12:
        dt = datetime(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:]),
                      tzinfo=timezone.utc) - timedelta(days=1)
        return dt.strftime('%Y%m%d')
    return date_str


def _cpu_filter_chain(gain_r: float, gain_g: float, gain_b: float, gamma: float,
                      rotate: bool,
                      annotation: list[str] | None = None,
                      bar_h: int = 0,
                      display_levels: dict | None = None,
                      full_range: bool = False) -> list[str]:
    filters: list[str] = []
    # Some cameras (e.g. the Berlin XMEye DE001B/C/D) emit full-range pixel data
    # but flag it limited-range (color_range=tv) in the H.264 VUI. Honouring that
    # flag during the YUV->RGB conversion below crushes everything under 16 to
    # black. Force full-range interpretation so the calibration sees the real
    # data. No-op for cameras already tagged full (Romania). Pair with the
    # `-color_range pc` output flag so playback isn't re-crushed.
    if full_range:
        filters.append('scale=in_range=full:out_range=full')
    if rotate:
        filters += ['vflip', 'hflip']
    # Per-channel gain (colorchannelmixer preserves saturation better than lutrgb
    # for near-unity gains). Skip if effectively identity.
    if not (abs(gain_r - 1.0) < 1e-3 and abs(gain_g - 1.0) < 1e-3 and abs(gain_b - 1.0) < 1e-3):
        filters.append(f'colorchannelmixer=rr={gain_r:.4f}:gg={gain_g:.4f}:bb={gain_b:.4f}')
    # Calibration gamma. ffmpeg's eq=gamma uses the inverse convention
    # (pow(val/255, 1/G)) so we pass 1/gamma to match apply_calibration_np.
    eq_parts: list[str] = []
    if gamma and abs(gamma - 1.0) > 1e-3:
        eq_parts.append(f'gamma={1.0 / gamma:.4f}')
    # Display-only contrast/brightness lift for light-polluted sites.
    if display_levels:
        contrast = display_levels.get('contrast')
        brightness = display_levels.get('brightness')
        if contrast is not None:
            eq_parts.append(f'contrast={contrast}')
        if brightness is not None:
            eq_parts.append(f'brightness={brightness}')
    if eq_parts:
        filters.append('eq=' + ':'.join(eq_parts))
    if bar_h > 0:
        filters.append(f'pad=iw:ih+{bar_h}:0:0:black')
    if annotation:
        filters.extend(annotation)
    return filters


def _build_cmd(src: Path, dst_tmp: Path, compression_level: int,
               gain_r: float, gain_g: float, gain_b: float, gamma: float,
               rotate: bool,
               overlay_cfg: dict | None, station_id: str,
               station_cfg: dict, chunk_epoch: int,
               vaapi_device: str, use_vaapi: bool = True,
               display_levels: dict | None = None,
               fpn_png_path: Path | None = None,
               full_range: bool = False,
               preset: str = 'fast') -> list[str] | None:
    if compression_level == 0:
        return None  # level 0 = raw copy, no ffmpeg re-encode

    qp  = COMPRESSION_QP[compression_level]
    crf = COMPRESSION_CRF[compression_level]

    annotation: list[str] = []
    bar_h: int = 0
    if overlay_cfg and station_id and chunk_epoch:
        annotation, bar_h = build_drawtext_annotations(
            overlay_cfg, station_id, station_cfg, chunk_epoch,
        )

    logo_path = (overlay_cfg or {}).get('logo', '')
    logo_opacity = (overlay_cfg or {}).get('logo_opacity', 0.8)
    show_logo = (overlay_cfg or {}).get('show_logo', True)
    use_logo = bool(annotation and show_logo and logo_path and Path(logo_path).exists())

    cpu_filters = _cpu_filter_chain(gain_r, gain_g, gain_b, gamma, rotate,
                                     annotation or None, bar_h,
                                     display_levels=display_levels,
                                     full_range=full_range)

    # FPN correction prefix — when active, prepends a movie+blend subtraction
    # to the filter graph.  This MUST precede rotation and colour calibration
    # since it corrects the raw sensor pattern in its native orientation.
    fpn_prefix = _fpn_filter_prefix(fpn_png_path)

    # When FPN is active, the blend output replaces [0:v] as the source for
    # all downstream filters.  In filter_complex mode the blend output feeds
    # directly into the cpu_chain; in simple -vf mode we must promote to
    # filter_complex.
    def _with_fpn_and_cpu(cpu_chain: str) -> str:
        """Combine FPN prefix + cpu_chain into a single filter_complex fragment.

        Without FPN: returns '[0:v]' + cpu_chain (standard).
        With FPN: movie→blend feeds into cpu_chain.
        """
        if fpn_prefix:
            return f'{fpn_prefix},{cpu_chain}'
        return f'[0:v]{cpu_chain}'

    if use_vaapi:
        if use_logo:
            font_path = overlay_cfg.get('font', '')
            font_size = overlay_cfg.get('font_size', 19)
            network = overlay_cfg.get('network', 'ROVIMEN')
            style = overlay_cfg.get('style', 'standard')
            MARGIN = 14

            cpu_chain = ','.join(cpu_filters) if cpu_filters else 'null'
            ntw = measure_text_width(font_path, font_size, network)
            try:
                from PIL import Image as _PILImg
                with _PILImg.open(logo_path) as _li:
                    _lw, _lh = _li.size
                logo_h = max(1, round(ntw * _lh / _lw * 3 // 2))
            except Exception:
                logo_h = max(1, font_size * 3 // 4)
            logo_w = ntw * 3 // 2
            logo_x = MARGIN + ntw + 8
            logo_y = f'H-{bar_h}+{MARGIN}' if style == 'cinema' else str(MARGIN)

            filter_complex = (
                f'{_with_fpn_and_cpu(cpu_chain)}[vmain];'
                f'[1:v]scale={logo_w}:-1,format=rgba,'
                f'colorchannelmixer=aa={logo_opacity}[logo];'
                f'[vmain][logo]overlay=x={logo_x}:y={logo_y},'
                f'format=nv12,hwupload[out]'
            )
            return [
                'ffmpeg', '-y',
                '-vaapi_device', vaapi_device,
                '-i', str(src),
                '-i', logo_path,
                '-filter_complex', filter_complex,
                '-map', '[out]',
                '-c:v', 'h264_vaapi',
                '-qp', str(qp),
                '-an',
                str(dst_tmp),
            ]
        else:
            if fpn_prefix:
                cpu_chain = ','.join(cpu_filters) if cpu_filters else 'null'
                filter_complex = (
                    f'{fpn_prefix},{cpu_chain},'
                    f'format=nv12,hwupload[out]'
                )
                return [
                    'ffmpeg', '-y',
                    '-vaapi_device', vaapi_device,
                    '-i', str(src),
                    '-filter_complex', filter_complex,
                    '-map', '[out]',
                    '-c:v', 'h264_vaapi',
                    '-qp', str(qp),
                    '-an',
                    str(dst_tmp),
                ]
            else:
                vf_filters = list(cpu_filters)
                vf_filters.append('format=nv12,hwupload')
                vf = ','.join(vf_filters)
                return [
                    'ffmpeg', '-y',
                    '-vaapi_device', vaapi_device,
                    '-i', str(src),
                    '-vf', vf,
                    '-c:v', 'h264_vaapi',
                    '-qp', str(qp),
                    '-an',
                    str(dst_tmp),
                ]
    else:
        # libx264 CPU fallback
        if use_logo:
            font_path = overlay_cfg.get('font', '')
            font_size = overlay_cfg.get('font_size', 19)
            network = overlay_cfg.get('network', 'ROVIMEN')
            style = overlay_cfg.get('style', 'standard')
            MARGIN = 14

            cpu_chain = ','.join(cpu_filters) if cpu_filters else 'null'
            ntw = measure_text_width(font_path, font_size, network)
            try:
                from PIL import Image as _PILImg
                with _PILImg.open(logo_path) as _li:
                    _lw, _lh = _li.size
            except Exception:
                pass
            logo_w = ntw * 3 // 2
            logo_x = MARGIN + ntw + 8
            logo_y = f'H-{bar_h}+{MARGIN}' if style == 'cinema' else str(MARGIN)

            filter_complex = (
                f'{_with_fpn_and_cpu(cpu_chain)}[vmain];'
                f'[1:v]scale={logo_w}:-1,format=rgba,'
                f'colorchannelmixer=aa={logo_opacity}[logo];'
                f'[vmain][logo]overlay=x={logo_x}:y={logo_y}[out]'
            )
            return [
                'ffmpeg', '-y',
                '-i', str(src),
                '-i', logo_path,
                '-filter_complex', filter_complex,
                '-map', '[out]',
                '-c:v', 'libx264', '-crf', str(crf), '-preset', preset,
                *CRF_RATE_FLOOR,
                '-an',
                str(dst_tmp),
            ]
        else:
            if fpn_prefix:
                cpu_chain = ','.join(cpu_filters) if cpu_filters else 'null'
                filter_complex = f'{fpn_prefix},{cpu_chain}[out]'
                return [
                    'ffmpeg', '-y',
                    '-i', str(src),
                    '-filter_complex', filter_complex,
                    '-map', '[out]',
                    '-c:v', 'libx264', '-crf', str(crf), '-preset', preset,
                    *CRF_RATE_FLOOR,
                    '-an',
                    str(dst_tmp),
                ]
            else:
                cmd = ['ffmpeg', '-y', '-i', str(src)]
                if cpu_filters:
                    cmd += ['-vf', ','.join(cpu_filters)]
                cmd += ['-c:v', 'libx264', '-crf', str(crf), '-preset', preset, *CRF_RATE_FLOOR, '-an', str(dst_tmp)]
                return cmd


def process_chunk(mkv: Path, cfg: dict) -> bool:
    """Re-encode an MKV chunk in-place, applying OSD overlay, rotation and colour
    calibration. Idempotent: returns True immediately if already reencoded.
    Reads any lock state before encoding and re-applies it after, so detection
    locks survive the in-place overwrite.
    """
    # coppermind: VAAPI encode in-place with OSD annotation

    m = _CHUNK_RE.match(mkv.name) or _STITCHED_CHUNK_RE.match(mkv.name)
    if not m:
        logger.warning('Skipping — filename does not match pattern: %s', mkv.name)
        return False

    station_id, date_str, time_str = m.group(1), m.group(2), m.group(3)
    night_str = _night_date(date_str, time_str)

    # Load once — used for both reencoded check and lock check below.
    chunk_state = flags_manager.load(
        station_id, night_str, cfg
    ).get('chunks', {}).get(mkv.name, {})

    # Stitched clips created before the calendar-date naming fix used the night
    # date in their filename, so _night_date() subtracts an extra day and misses
    # the state.json entry. Fall back to date_str directly as a migration shim.
    if not chunk_state and _STITCHED_CHUNK_RE.match(mkv.name) and night_str != date_str:
        chunk_state = flags_manager.load(
            station_id, date_str, cfg
        ).get('chunks', {}).get(mkv.name, {})

    # Idempotent check — prefer state.json, fall back to .reencoded sidecar for
    # nights that pre-date the pipeline rewrite.
    if chunk_state.get('reencoded', False):
        return True
    if Path(str(mkv) + '.reencoded').exists():
        return True

    if not chunk_state.get('ready', False):
        logger.warning('Skipping — chunk not ready: %s', mkv.name)
        return False

    import color_calibration as cc
    station_cfg = cfg.get('stations', {}).get(station_id, {})
    station_cfg = enrich_station_cfg_pointing(station_id, station_cfg, cfg)
    # Cameras that emit full-range data tagged limited (color_range=tv) need
    # full-range interpretation during calibration + a full-range output tag,
    # else the YUV->RGB crushes blacks (Berlin XMEye). Station-level opt-in.
    full_range = bool(cfg.get('full_range_fix', False))

    cache_key = (station_id, night_str)
    with _calibration_cache_lock:
        cached = _calibration_cache.get(cache_key)

    if cached is not None:
        gain_r, gain_g, gain_b, gamma = cached
    else:
        calib = cc.resolve_calibration_from_mkv(mkv, station_cfg, full_range=full_range)
        if calib is None:
            logger.warning('adaptive gain derivation failed for %s — skipping calibration', mkv.name)
            post = cc.resolve_color_post(station_cfg)
            gain_r, gain_g, gain_b, gamma = 1.0, 1.0, 1.0, float(post['gamma'])
        else:
            gain_r, gain_g, gain_b, gamma = calib
            with _calibration_cache_lock:
                _calibration_cache[cache_key] = calib
    rotate = station_cfg.get('rotate', False)
    display_levels = cfg.get('display_levels') or {}
    compression_level = cfg.get('compression_level', 2)
    if compression_level not in (1, 2, 3, 4):
        compression_level = 2
    timeout_s = cfg.get('timeout_s', cfg.get('segment_duration', 20) * 5)
    caps = cfg.get('capabilities', {})
    want_hw = caps.get('hwencode', True)
    vaapi_device = _find_vaapi_device() if want_hw else None
    use_hw = vaapi_device is not None

    # Stitched boundary clips have mid-stream H.264 parameter changes that cause
    # VAAPI hwupload to fail when reinitializing the filter graph at the splice
    # point. Force CPU path for these clips.
    if _STITCHED_CHUNK_RE.match(mkv.name):
        use_hw = False
        vaapi_device = None

    # Locked (detected meteor) clips are encoded at the highest quality tier.
    is_locked = chunk_state.get('lock') is not None
    if not is_locked:
        # Fall back to .locked sidecar for backward compat
        is_locked = Path(str(mkv) + '.locked').exists()

    # Force quality level 1 (best) for locked clips, regardless of config.
    effective_level = LOCKED_QUALITY_LEVEL if is_locked else compression_level
    if effective_level not in (1, 2, 3, 4):
        effective_level = 2
    preset = 'fast' if is_locked else 'veryfast'

    encoder_name = (f'h264_vaapi ({vaapi_device})' if use_hw
                    else f'libx264 CRF{COMPRESSION_CRF[effective_level]}')  # vaapi_device non-None when use_hw
    logger.debug('%s clip — encoder: %s  QP/CRF=%s',
                 'Locked' if is_locked else 'Unlocked', encoder_name,
                 COMPRESSION_QP[effective_level] if use_hw else COMPRESSION_CRF[effective_level])

    overlay_cfg = cfg.get('overlay') or {}
    if not overlay_cfg.get('enabled', True):
        overlay_cfg = {}
    chunk_epoch = 0
    if overlay_cfg:
        try:
            chunk_epoch = int(datetime(
                int(date_str[:4]), int(date_str[4:6]), int(date_str[6:]),
                int(time_str[:2]), int(time_str[2:4]), int(time_str[4:]),
                tzinfo=timezone.utc,
            ).timestamp())
        except Exception:
            pass

    if not mkv.exists():
        logger.info('Skipping — source vanished: %s', mkv.name)
        return False

    # FPN correction PNG (built by darkfield in dawn_process Phase 0)
    fpn_png: Path | None = None
    fpn_svc = cfg.get('services', {}).get('fpn_calibration', {})
    if fpn_svc.get('enabled', False):
        try:
            import fpn_calibration
            fpn_png = fpn_calibration.get_correction_png_path(station_id, night_str, cfg)
        except Exception:
            pass

    dst_tmp = mkv.parent / (mkv.name + '.tmp.mkv')

    cmd = _build_cmd(
        mkv, dst_tmp, effective_level,
        gain_r=gain_r, gain_g=gain_g, gain_b=gain_b, gamma=gamma, rotate=rotate,
        overlay_cfg=overlay_cfg, station_id=station_id,
        station_cfg=station_cfg, chunk_epoch=chunk_epoch,
        vaapi_device=vaapi_device, use_vaapi=use_hw,
        display_levels=display_levels,
        fpn_png_path=fpn_png,
        full_range=full_range,
        preset=preset,
    )
    if not use_hw and cmd is not None:
        # Inject libx264 thread count into the CPU-fallback command
        thr = str(_libx264_thread_count(cfg))
        cmd = cmd[:-1] + ['-threads', thr, cmd[-1]]
    if full_range and cmd is not None:
        # Tag the output full-range so players don't re-crush the (now correct)
        # full-range data. Inserted before the output path, works for both the
        # vaapi and libx264 branches.
        cmd = cmd[:-1] + ['-color_range', 'pc', cmd[-1]]

    t0 = time.monotonic()
    outcome = 'ok'
    proc = None

    if cmd is None:
        try:
            shutil.copy2(mkv, dst_tmp)
        except Exception as exc:
            outcome = 'error'
            logger.error('FAILED %s — copy error: %s', mkv.name, exc)
    else:
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env={**os.environ, 'TZ': 'UTC'},
            )
            try:
                _, stderr_bytes = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                outcome = 'timeout'
                stderr_bytes = b''
        except Exception as exc:
            outcome = 'error'
            stderr_bytes = b''
            logger.error('FAILED %s — could not launch ffmpeg: %s', mkv.name, exc)

    wall = time.monotonic() - t0

    if outcome == 'timeout':
        dst_tmp.unlink(missing_ok=True)
        logger.warning('TIMEOUT %s  timeout=%ds', mkv.name, timeout_s)
        return False

    if outcome == 'error' or (proc and proc.returncode != 0):
        dst_tmp.unlink(missing_ok=True)
        rc = proc.returncode if proc else -1
        tail = stderr_bytes[-300:].decode('utf-8', errors='replace') if stderr_bytes else ''
        logger.error('FAILED %s  rc=%d  wall=%.1fs  stderr=...%s', mkv.name, rc, wall, tail)
        return False

    # Success: rename tmp over original (in-place).
    # The .locked sidecar (chunk.mkv.locked) is a separate file — it survives
    # the rename untouched, so no unlock/relock dance is needed.
    src_bytes = mkv.stat().st_size if mkv.exists() else 0
    dst_bytes = dst_tmp.stat().st_size if dst_tmp.exists() else 0
    dst_tmp.rename(mkv)           # atomic in-place replace

    flags_manager.mark_reencoded(station_id, night_str, mkv.name, cfg)
    ratio = 100.0 * (1 - dst_bytes / src_bytes) if src_bytes else 0.0
    segment_secs = cfg.get('segment_duration', 20)
    speed = segment_secs / wall if wall else 0
    logger.info('DONE %s  wall=%.1fs  src=%.1fMB  dst=%.1fMB  saved=%.1f%%  speed=%.2fx',
                mkv.name, wall, src_bytes / 1e6, dst_bytes / 1e6, ratio, speed)
    return True


def process_night_locked_only(station_id: str, date_str: str, cfg: dict) -> None:
    """Re-encode only locked (detection) chunks — called before archive upload.

    Applies the full pipeline (rotate + colour calibration + overlay) so that
    uploaded clips are presentation-quality without waiting for the bulk
    reencode pass.  process_chunk() is idempotent — these chunks are skipped
    when process_night() runs later.
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
        return

    state = flags_manager.load(station_id, date_str, cfg)
    chunks_state = state.get('chunks', {})

    locked = []
    for mkv in sorted(night_dir.glob('*_color.mkv')):
        chunk_data = chunks_state.get(mkv.name, {})
        is_locked = chunk_data.get('lock') is not None
        if not is_locked:
            is_locked = Path(str(mkv) + '.locked').exists()
        if is_locked:
            locked.append(mkv)

    if not locked:
        logger.info('[%s] coppermind: no locked chunks for %s', station_id, date_str)
        return

    bcfg = _batch_cfg(cfg)
    parallelism = bcfg['_encode_parallelism']
    logger.info('[%s] coppermind: encoding %d locked chunk(s) for %s (workers=%d, threads/chunk=%d)',
                station_id, len(locked), date_str, parallelism, bcfg['_encode_threads'])
    ok = 0
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(process_chunk, mkv, bcfg): mkv for mkv in locked}
        for fut in as_completed(futures):
            mkv = futures[fut]
            try:
                if fut.result():
                    ok += 1
            except Exception:
                logger.exception('[%s] coppermind locked encode failed for %s', station_id, mkv.name)
    logger.info('[%s] coppermind: %d/%d locked chunk(s) encoded for %s',
                station_id, ok, len(locked), date_str)


def process_night(station_id: str, date_str: str, cfg: dict) -> None:
    """Re-encode all raw MKV chunks for a completed night (morning coppermind pass).

    Chunks are encoded in parallel using dawn_encode_parallelism workers
    (default: cpu_cores // 2), each worker getting cpu_cores // parallelism
    libx264 threads.  On a 6-core machine this gives 3 workers × 2 threads,
    fully utilising all cores vs the 1-core real-time encoding path.

    process_chunk() is idempotent — already-encoded chunks (including locked
    clips encoded by process_night_locked_only) are skipped automatically.
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
        logger.warning('[%s] coppermind: night dir not found: %s', station_id, night_dir)
        return

    chunks = sorted(night_dir.glob('*_color.mkv'))
    bcfg = _batch_cfg(cfg)
    parallelism = bcfg['_encode_parallelism']
    logger.info('[%s] coppermind: processing %d chunks for %s (workers=%d, threads/chunk=%d)',
                station_id, len(chunks), date_str, parallelism, bcfg['_encode_threads'])
    ok_count = 0
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(process_chunk, mkv, bcfg): mkv for mkv in chunks}
        for fut in as_completed(futures):
            mkv = futures[fut]
            try:
                if fut.result():
                    ok_count += 1
            except Exception:
                logger.exception('[%s] coppermind failed for %s', station_id, mkv.name)
    logger.info('[%s] coppermind complete: %d/%d ok for %s',
                station_id, ok_count, len(chunks), date_str)


def main() -> int:
    parser = argparse.ArgumentParser(description='VAAPI re-encode a chunk to disk')
    parser.add_argument('--chunk', required=True, help='Path to ramdisk .mkv chunk')
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
