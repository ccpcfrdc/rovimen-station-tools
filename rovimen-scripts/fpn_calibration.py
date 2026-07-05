#!/usr/bin/env python3
"""fpn_calibration.py -- Fixed-pattern noise calibration builder.

Codename: darkfield

Builds a per-camera FPN correction frame from the night's own dark-sky
video data.  The correction captures two artifacts visible in IMX291
compressed video:

  1. Hot pixels — isolated pixels with anomalously high dark current,
     detected as deviations from a local 5x5 median.
  2. Row banding — per-row brightness offset caused by column-parallel
     ADC differences in the IMX291 readout (0.80 DN measured).

Column banding (0.26 DN) is below the H.264 quantization floor at
8 Mbps and is not corrected.

Calibration is built once per camera per night from ~50 sample frames
drawn from the middle of the night (avoiding twilight), taking ~3-5 s
per camera.  Results are saved as:

  - ``fpn_correction.npy`` — int16 (H, W, 3) for numpy consumers
  - ``fpn_correction.png`` — uint8 RGB for ffmpeg blend=subtract
  - ``fpn_meta.json``      — statistics and build parameters

Called by dawn_process Phase 0, consumed by stacker (numpy) and encoder
(ffmpeg filter graph).

Standalone::

    python fpn_calibration.py -c config.json --station RO000A --date 20260528
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

WIDTH = 1280
HEIGHT = 720

# --- Tunable defaults (overridable via cfg["fpn_calibration"]) -----------

DEFAULT_HOT_THRESHOLD = 10      # DN above local median to flag as hot pixel
DEFAULT_SAMPLE_CHUNKS = 10      # number of chunks to sample from mid-night
DEFAULT_FRAMES_PER_CHUNK = 5    # frames decoded per sampled chunk
DEFAULT_TWILIGHT_SKIP_PCT = 10  # % of chunks to skip at start/end of night
DEFAULT_LOCAL_KERNEL = 5        # hot-pixel detection neighborhood size


# --- Helpers --------------------------------------------------------------

def _capture_root(cfg: dict) -> Path:
    return Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(Path.home() / 'color_capture')
    )


def _fpn_cfg(cfg: dict) -> dict:
    return cfg.get('fpn_calibration', {})


def _calib_dir(station_id: str, date_str: str, cfg: dict) -> Path:
    return _capture_root(cfg) / station_id / date_str / 'calibration'


def _decode_sample_frames(mkv: Path, n_frames: int = 5) -> np.ndarray | None:
    """Decode *n_frames* evenly spaced frames from *mkv* via ffmpeg pipe.

    Returns (n_frames, HEIGHT, WIDTH, 3) uint8 or None on failure.
    """
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-i', str(mkv),
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1',
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        logger.error('ffmpeg not found')
        return None

    frame_bytes = WIDTH * HEIGHT * 3
    frames: list[np.ndarray] = []
    total_read = 0
    try:
        while len(frames) < n_frames * 5:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            total_read += 1
            frames.append(np.frombuffer(raw, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3).copy())
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()

    if len(frames) < n_frames:
        return np.array(frames, dtype=np.uint8) if frames else None

    # Pick n_frames evenly spaced from what we decoded
    indices = np.linspace(0, len(frames) - 1, n_frames, dtype=int)
    return np.array([frames[i] for i in indices], dtype=np.uint8)


def _select_mid_night_chunks(
    chunks: list[Path],
    n_sample: int,
    skip_pct: int,
) -> list[Path]:
    """Select *n_sample* chunks from the middle of the night, skipping twilight."""
    if len(chunks) <= 2:
        return chunks

    n_skip = max(1, len(chunks) * skip_pct // 100)
    usable = chunks[n_skip:-n_skip] if len(chunks) > 2 * n_skip else chunks

    if len(usable) <= n_sample:
        return usable

    indices = np.linspace(0, len(usable) - 1, n_sample, dtype=int)
    return [usable[i] for i in indices]


def _median_filter_2d(arr: np.ndarray, size: int) -> np.ndarray:
    """Per-channel 2D median filter.  Prefers scipy when available (~0.9 s on
    720p), falls back to a stride-tricks implementation (~1.2 s).
    """
    try:
        from scipy.ndimage import median_filter
        return median_filter(arr, size=(size, size, 1))
    except ImportError:
        pass

    h, w, c = arr.shape
    half = size // 2
    padded = np.pad(arr, ((half, half), (half, half), (0, 0)), mode='edge')
    out = np.empty_like(arr)
    for ch in range(c):
        p = padded[:, :, ch]
        strides = p.strides
        shape = (h, w, size, size)
        st = (strides[0], strides[1], strides[0], strides[1])
        windows = np.lib.stride_tricks.as_strided(p, shape=shape, strides=st)
        out[:, :, ch] = np.median(windows.reshape(h, w, -1), axis=2)
    return out


# --- Public API -----------------------------------------------------------

def build_calibration(
    station_id: str,
    date_str: str,
    cfg: dict,
) -> Path | None:
    """Build FPN calibration for one camera-night.

    Selects sample chunks from mid-night, decodes frames, computes the
    temporal median, then extracts hot pixels and row banding.

    Returns the calibration directory path, or None if insufficient data.
    """
    t0 = time.monotonic()
    fc = _fpn_cfg(cfg)
    hot_threshold = fc.get('hot_pixel_threshold', DEFAULT_HOT_THRESHOLD)
    n_chunks = fc.get('sample_chunks', DEFAULT_SAMPLE_CHUNKS)
    n_frames = fc.get('frames_per_chunk', DEFAULT_FRAMES_PER_CHUNK)
    skip_pct = fc.get('twilight_skip_pct', DEFAULT_TWILIGHT_SKIP_PCT)
    kernel = fc.get('local_kernel', DEFAULT_LOCAL_KERNEL)

    night_dir = _capture_root(cfg) / station_id / date_str
    if not night_dir.exists():
        logger.warning('[%s] darkfield: night dir not found: %s', station_id, night_dir)
        return None

    chunks = sorted(night_dir.glob('*_color.mkv'))
    if len(chunks) < 3:
        logger.warning('[%s] darkfield: only %d chunks — need at least 3', station_id, len(chunks))
        return None

    selected = _select_mid_night_chunks(chunks, n_chunks, skip_pct)
    logger.info('[%s] darkfield: sampling %d chunks (%d frames each) from %d total',
                station_id, len(selected), n_frames, len(chunks))

    all_frames: list[np.ndarray] = []
    for mkv in selected:
        frames = _decode_sample_frames(mkv, n_frames)
        if frames is not None and len(frames) > 0:
            all_frames.append(frames)

    if not all_frames:
        logger.warning('[%s] darkfield: no frames decoded', station_id)
        return None

    stack = np.concatenate(all_frames, axis=0)  # (N, H, W, 3) uint8
    total_frames = stack.shape[0]
    logger.debug('[%s] darkfield: computing median of %d frames', station_id, total_frames)

    # --- Temporal median ---
    median_frame = np.median(stack.astype(np.float32), axis=0)  # (H, W, 3) float32

    # --- Hot pixel detection ---
    local_med = _median_filter_2d(median_frame, kernel)
    hot_excess = median_frame - local_med  # positive = brighter than neighborhood
    hot_mask = np.any(np.abs(hot_excess) > hot_threshold, axis=2)  # (H, W) bool
    n_hot = int(hot_mask.sum())

    # --- Row banding ---
    # Per-row median across columns, minus the global per-row trend.
    # This isolates the row-level ADC offset from the actual sky gradient.
    row_medians = np.median(median_frame, axis=1, keepdims=True)  # (H, 1, 3)
    row_global = np.median(row_medians, axis=0, keepdims=True)    # (1, 1, 3)
    row_offsets = row_medians - row_global                        # (H, 1, 3)
    row_amplitude = float(np.std(row_offsets))

    # --- Combined correction frame ---
    # Start with row banding everywhere, then overlay hot pixel excess
    # at flagged locations (hot pixel excess already includes any row
    # component at that pixel, so it replaces rather than adds).
    correction = np.broadcast_to(row_offsets, median_frame.shape).copy()  # (H, W, 3) float32
    correction[hot_mask] = hot_excess[hot_mask]

    # --- Save ---
    calib_dir = _calib_dir(station_id, date_str, cfg)
    calib_dir.mkdir(parents=True, exist_ok=True)

    corr_int16 = correction.astype(np.int16)
    np.save(calib_dir / 'fpn_correction.npy', corr_int16)
    np.save(calib_dir / 'fpn_hot_pixel_mask.npy', hot_mask)

    # PNG for ffmpeg: clamp to [0, 255] uint8.  Negative corrections
    # (cold pixels / dark rows) are rare and small; the PNG drops them
    # but the numpy path preserves them via int16.
    corr_png = np.clip(correction, 0, 255).astype(np.uint8)
    Image.fromarray(corr_png).save(
        str(calib_dir / 'fpn_correction.png'), optimize=True,
    )

    # Metadata
    meta = {
        'station_id': station_id,
        'date': date_str,
        'chunks_sampled': len(selected),
        'frames_total': total_frames,
        'hot_pixels': n_hot,
        'hot_threshold': hot_threshold,
        'local_kernel': kernel,
        'row_banding_std': round(row_amplitude, 3),
        'correction_mean': round(float(correction.mean()), 3),
        'correction_max': round(float(correction.max()), 1),
        'correction_min': round(float(correction.min()), 1),
        'build_wall_s': round(time.monotonic() - t0, 2),
    }
    with open(calib_dir / 'fpn_meta.json', 'w') as f:
        json.dump(meta, f, indent=2)

    logger.info(
        '[%s] darkfield: built calibration — %d hot pixels, row banding std=%.3f DN, '
        'correction range [%.1f, %.1f], wall=%.1fs',
        station_id, n_hot, row_amplitude,
        meta['correction_min'], meta['correction_max'], meta['build_wall_s'],
    )
    return calib_dir


def load_correction_np(
    station_id: str,
    date_str: str,
    cfg: dict,
) -> np.ndarray | None:
    """Load the int16 (H, W, 3) correction array, or None if unavailable."""
    p = _calib_dir(station_id, date_str, cfg) / 'fpn_correction.npy'
    if not p.exists():
        return None
    try:
        arr = np.load(p)
        if arr.shape == (HEIGHT, WIDTH, 3):
            return arr
        logger.warning('[%s] darkfield: correction shape mismatch: %s', station_id, arr.shape)
    except Exception as e:
        logger.warning('[%s] darkfield: failed to load correction: %s', station_id, e)
    return None


def get_correction_png_path(
    station_id: str,
    date_str: str,
    cfg: dict,
) -> Path | None:
    """Return the path to the ffmpeg-ready correction PNG, or None."""
    p = _calib_dir(station_id, date_str, cfg) / 'fpn_correction.png'
    return p if p.exists() else None


def apply_numpy(
    frames: np.ndarray,
    correction: np.ndarray,
) -> np.ndarray:
    """Subtract FPN correction from decoded frames.

    frames:     (N, H, W, 3) uint8  or  (H, W, 3) uint8
    correction: (H, W, 3) int16

    Returns same shape as input, uint8, clamped to [0, 255].
    """
    if frames.ndim == 3:
        return np.clip(
            frames.astype(np.int16) - correction,
            0, 255,
        ).astype(np.uint8)
    return np.clip(
        frames.astype(np.int16) - correction[np.newaxis],
        0, 255,
    ).astype(np.uint8)


def ffmpeg_filter_prefix(fpn_png_path: Path) -> str:
    """Return an ffmpeg filter_complex prefix for FPN subtraction.

    The returned string produces a corrected stream from ``[0:v]`` that
    subsequent filters can chain onto.  It ends WITHOUT a trailing comma
    or semicolon — the caller appends whatever comes next.

    Example output::

        movie='/path/to/fpn_correction.png',format=rgb24[_fpn];
        [0:v][_fpn]blend=all_mode=subtract

    The ``blend=all_mode=subtract`` computes ``max(A-B, 0)`` per pixel,
    which is exactly the clamped subtraction we need.
    """
    escaped = str(fpn_png_path).replace('\\', '\\\\').replace("'", "\\'").replace(':', '\\:')
    return (
        f"[0:v]format=rgb24[_main];"
        f"movie='{escaped}',format=rgb24[_fpn];"
        f"[_main][_fpn]blend=all_mode=subtract"
    )


# --- CLI ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description='Build FPN calibration for a camera-night')
    parser.add_argument('--station', required=True, help='Station ID (e.g. RO000A)')
    parser.add_argument('--date', required=True, help='Night date YYYYMMDD')
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    with open(args.config) as f:
        cfg = json.load(f)

    result = build_calibration(args.station, args.date, cfg)
    if result:
        print(f'Calibration saved to {result}')
        return 0
    print('Calibration failed — not enough data')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
