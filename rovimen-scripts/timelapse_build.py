#!/usr/bin/env python3
"""timelapse_build.py — Per-night timelapse and night-stack builder.

Codename: bendalloy

Builds an MP4 timelapse from maxpixel stack WebPs and computes a per-pixel
maximum night stack from dark-sky thumbnails (20:00–02:00 UTC window).

Called by the morning processing sequence, or standalone:
    python timelapse_build.py -c config.json --station RO000H --date 20260315
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

import flags_manager

logger = logging.getLogger(__name__)


def build_timelapse(date_str: str, station_id: str, stacks_dir: Path,
                    out_dir: Path, *, rotate: bool = False) -> Path | None:
    """Build MP4 timelapse from stacks. Returns output path on success, None on failure.

    When rotate is True, applies vflip+hflip (180-degree rotation) to match
    the camera's physical orientation.  Stack images saved by stacker.py are
    already rotated, so this flag is only needed when stacks were produced
    from raw (unrotated) frames — kept as a safety net.
    """
    webps = sorted(stacks_dir.glob('*_stack.webp'))
    if not webps:
        logger.warning('[%s] No frames for timelapse: %s', station_id, date_str)
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f'{station_id}_{date_str}_timelapse.mp4'
    logger.info('[%s] Building timelapse: %d frames → %s', station_id, len(webps), out.name)

    vf_filters: list[str] = []
    if rotate:
        vf_filters.extend(['vflip', 'hflip'])

    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-f', 'image2', '-pattern_type', 'glob',
        '-framerate', '25',
        '-i', str(stacks_dir / '*_stack.webp'),
    ]
    if vf_filters:
        cmd += ['-vf', ','.join(vf_filters)]
    cmd += [
        '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        '-y', str(out),
    ]
    rc = subprocess.call(cmd)
    if rc == 0:
        logger.info('[%s] Timelapse saved: %s', station_id, out.name)
        return out
    logger.error('[%s] Timelapse build failed (rc=%d) for %s', station_id, rc, date_str)
    return None


def build_night_stack(date_str: str, station_id: str, thumbs_dir: Path,
                      out_dir: Path) -> None:
    """Compute per-pixel maximum of dark-sky thumbnails (20:00–02:00 UTC)."""
    # bendalloy: overnight accumulation
    all_thumbs = sorted(thumbs_dir.glob('*_stack.webp'))
    thumbs = []
    for t in all_thumbs:
        parts = t.stem.split('_')
        if len(parts) >= 3:
            hhmm = int(parts[2][:4])
            if hhmm >= 2000 or hhmm <= 200:
                thumbs.append(t)
    if not thumbs:
        logger.warning('[%s] No thumbnails in 20:00–02:00 window for night stack: %s',
                       station_id, date_str)
        return
    logger.info('[%s] Building night stack: %d/%d thumbnails → %s',
                station_id, len(thumbs), len(all_thumbs), date_str)
    try:
        acc: np.ndarray | None = None
        h = w = 0
        skipped = 0
        for t in thumbs:
            try:
                img = Image.open(t).convert('RGB')
            except Exception:
                logger.warning('[%s] Skipping unreadable thumbnail: %s', station_id, t.name)
                skipped += 1
                continue
            if acc is None:
                h, w = img.height, img.width
                acc = np.zeros((h, w, 3), dtype=np.uint8)
            elif img.size != (w, h):
                img = img.resize((w, h), Image.LANCZOS)
            acc = np.maximum(acc, np.array(img, dtype=np.uint8))
        if acc is None:
            logger.warning('[%s] All %d thumbnails unreadable for %s',
                           station_id, len(thumbs), date_str)
            return
        if skipped:
            logger.warning('[%s] Skipped %d/%d unreadable thumbnails for %s',
                           station_id, skipped, len(thumbs), date_str)
        out = out_dir / f'{station_id}_{date_str}_night_stack.webp'
        Image.fromarray(acc).save(str(out), lossless=True)
        logger.info('[%s] Night stack saved: %s', station_id, out.name)
    except Exception:
        logger.exception('[%s] Night stack build failed for %s', station_id, date_str)


def build(station_id: str, date_str: str, cfg: dict) -> None:
    """Build timelapse + night stack for a station/date.

    Called by the morning processing sequence after stacker.process_night()
    has run. Reads stacks from videocapture_path/STATION/DATE/stacks/.
    Skips immediately if stacker is disabled in config (no stacks to assemble).
    """
    services = cfg.get('services', {})
    stacker_enabled = services.get('stacker', {}).get('enabled', True)

    if not stacker_enabled:
        logger.info('[%s] stacker disabled — skipping timelapse build for %s',
                    station_id, date_str)
        flags_manager.mark_timelapse_done(station_id, date_str, cfg)
        return

    capture_path = Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(Path.home() / 'color_capture')
    )
    stacks_dir = capture_path / station_id / date_str / 'stacks'
    thumbs_dir = stacks_dir / 'thumbs'
    tl_dir = capture_path / station_id / date_str

    if not stacks_dir.exists() or not any(stacks_dir.glob('*_stack.webp')):
        logger.warning('[%s] No stacks found in %s — timelapse skipped', station_id, stacks_dir)
        flags_manager.mark_timelapse_done(station_id, date_str, cfg)
        return

    result = build_timelapse(date_str, station_id, stacks_dir, tl_dir)
    if result is not None:
        flags_manager.mark_timelapse_done(station_id, date_str, cfg)

    if thumbs_dir.exists():
        build_night_stack(date_str, station_id, thumbs_dir, tl_dir)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Build timelapse and night stack for a station/date'
    )
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    parser.add_argument('--station', required=True, help='Station ID, e.g. RO000H')
    parser.add_argument('--date', required=True, help='Night date YYYYMMDD')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    with open(args.config) as f:
        cfg = json.load(f)

    build(args.station, args.date, cfg)
    return 0


if __name__ == '__main__':
    sys.exit(main())
