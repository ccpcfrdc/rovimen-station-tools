#!/usr/bin/env python3
"""detection_lock.py — End-of-night detection locking and reconciliation.

Codename: makeawish

Parses ArchivedFiles/FTPdetectinfo at end-of-night to lock matching MKV
chunks (confirmed detections) and remove false positives.

Called by dawn_process.py, or standalone:
    python detection_lock.py -c config.json --station RO000H --date 20260315
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

import flags_manager

logger = logging.getLogger(__name__)

FF_DURATION = 256 / 25.0  # 10.24 s — one FF block at 25 fps

_CHUNK_RE = re.compile(r'^[A-Z0-9]+_(\d{8})_(\d{6})_color\.mkv$')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(date_s: str, time_s: str) -> datetime:
    return datetime.strptime(f'{date_s}_{time_s}', '%Y%m%d_%H%M%S')


def _night_date(date_str: str, time_str: str) -> str:
    if int(time_str[:2]) < 12:
        dt = datetime(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:]),
                      tzinfo=timezone.utc) - timedelta(days=1)
        return dt.strftime('%Y%m%d')
    return date_str


def _chunk_time(mkv: Path) -> datetime | None:
    m = _CHUNK_RE.match(mkv.name)
    return _parse_dt(m.group(1), m.group(2)) if m else None


def _find_chunks_for_time(ff_time: datetime,
                           chunk_dir: Path,
                           segment_secs: int) -> list[Path]:
    """Return chunks in chunk_dir that overlap the FF block starting at ff_time."""
    if not chunk_dir.exists():
        return []
    pairs = sorted(
        [(ct, p) for p in chunk_dir.glob('*_color.mkv')
         if (ct := _chunk_time(p)) is not None],
        key=lambda x: x[0],
    )
    result: list[Path] = []
    for i, (ts, chunk) in enumerate(pairs):
        next_ts = (pairs[i + 1][0] if i + 1 < len(pairs)
                   else ts + timedelta(seconds=segment_secs + 5))
        if ts <= ff_time < next_ts:
            result.append(chunk)
            ff_end = ff_time + timedelta(seconds=FF_DURATION)
            if ff_end > next_ts and i + 1 < len(pairs):
                result.append(pairs[i + 1][1])
            break
    return result


def _parse_ftpdetectinfo(path: Path) -> list[datetime]:
    """Return list of meteor start datetimes from an FTPdetectinfo file."""
    times: list[datetime] = []
    try:
        lines = path.read_text().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('FF_'):
                parts = line.split('_')
                try:
                    ff_ms = int(parts[4]) / 1000.0
                    ff_base = datetime.strptime(f'{parts[2]}_{parts[3]}', '%Y%m%d_%H%M%S')
                    ff_time = ff_base + timedelta(seconds=ff_ms)
                except (IndexError, ValueError):
                    i += 1
                    continue
                fps = 25.0
                meteor_time = ff_time
                for j in range(i + 1, min(i + 20, len(lines))):
                    seg = lines[j].strip().split()
                    if not seg:
                        continue
                    if lines[j].strip().startswith('FF_') or '---' in lines[j]:
                        break
                    # Detection row: seg[0] is a frame number (float) — must check
                    # before the fps/header check, because detection rows also have
                    # floats at seg[3] (RA) that would otherwise be mistaken for fps.
                    if re.match(r'^\d+\.\d+$', seg[0]):
                        try:
                            meteor_time = ff_time + timedelta(seconds=float(seg[0]) / fps)
                        except ValueError:
                            pass
                        break
                    # Camera header row: non-numeric seg[0], fps at seg[3].
                    if len(seg) >= 4 and re.match(r'^\d+\.\d+$', seg[3]):
                        try:
                            fps = float(seg[3])
                        except ValueError:
                            pass
                times.append(meteor_time)
            i += 1
    except OSError:
        pass
    return times


STITCH_BOUNDARY_SECS = 2.0  # stitch adjacent clips when detection is within 2s of clip edge


def _stitch_clips(clip_a: Path, clip_b: Path,
                  station_id: str, date_str: str,
                  chunk_dir: Path, cfg: dict) -> Path | None:
    """Stitch the last 10s of clip_a and the first 10s of clip_b into a new clip.

    Used when a detection falls within STITCH_BOUNDARY_SECS of a clip boundary,
    ensuring the full event is captured in a single locked file rather than
    across two separate clips.

    Output filename: {station_id}_{date_str}_{T_start}_{T_end}_color.mkv
      T_start = clip_a.start_time + half_segment  (start of extracted window)
      T_end   = clip_b.start_time + half_segment  (end of extracted window)

    Returns the output Path on success, or None on failure.
    """
    segment_secs = cfg.get('segment_duration', 20)
    half = segment_secs // 2  # 10s for the default 20s segment

    m_a = _CHUNK_RE.match(clip_a.name)
    m_b = _CHUNK_RE.match(clip_b.name)
    if not m_a or not m_b:
        logger.warning('[%s] Cannot stitch: unexpected filename format (%s, %s)',
                       station_id, clip_a.name, clip_b.name)
        return None

    ts_a = _parse_dt(m_a.group(1), m_a.group(2))
    ts_b = _parse_dt(m_b.group(1), m_b.group(2))

    t_start = (ts_a + timedelta(seconds=half)).strftime('%H%M%S')
    t_end   = (ts_b + timedelta(seconds=half)).strftime('%H%M%S')
    # Use ts_a's calendar date in the filename — regular chunks also embed the
    # calendar date, and encoder._night_date() converts it back to the night
    # date to locate state.json.  Using date_str (night date) here causes
    # _night_date() to subtract an extra day for early-morning clips, pointing
    # to the wrong state.json and breaking the ready/locked lookup.
    cal_date = (ts_a + timedelta(seconds=half)).strftime('%Y%m%d')
    out_name = f'{station_id}_{cal_date}_{t_start}_{t_end}_color.mkv'
    out_path = chunk_dir / out_name

    if out_path.exists():
        logger.info('[%s] Stitched clip already exists: %s', station_id, out_name)
        _stitch_stacks(clip_a, clip_b, out_path, station_id, date_str, cfg)
        return out_path

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        part_a = tmp / 'part_a.mkv'
        part_b = tmp / 'part_b.mkv'
        concat_list = tmp / 'concat.txt'
        # Write concat output inside tmp_dir first — avoids ffmpeg muxer errors
        # on non-local filesystems (e.g. NTFS via fuseblk on /mnt/data).
        out_tmp_local = tmp / 'stitched.mkv'
        out_tmp = out_path.with_name(out_path.name + '.tmp')

        # Extract last `half` seconds of clip_a
        r = subprocess.run(
            ['ffmpeg', '-y', '-ss', str(half), '-i', str(clip_a),
             '-t', str(half), '-c', 'copy', '-avoid_negative_ts', '1', str(part_a)],
            capture_output=True, timeout=60,
        )
        if r.returncode != 0:
            logger.error('[%s] Stitch: failed extracting tail of %s: %s',
                         station_id, clip_a.name,
                         r.stderr[-300:].decode('utf-8', errors='replace'))
            return None

        # Extract first `half` seconds of clip_b
        r = subprocess.run(
            ['ffmpeg', '-y', '-t', str(half), '-i', str(clip_b),
             '-c', 'copy', str(part_b)],
            capture_output=True, timeout=60,
        )
        if r.returncode != 0:
            logger.error('[%s] Stitch: failed extracting head of %s: %s',
                         station_id, clip_b.name,
                         r.stderr[-300:].decode('utf-8', errors='replace'))
            return None

        # Concatenate the two parts into local tmp (avoids NTFS muxer issues)
        concat_list.write_text(f"file '{part_a}'\nfile '{part_b}'\n")
        r = subprocess.run(
            ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', str(concat_list),
             '-c', 'copy', str(out_tmp_local)],
            capture_output=True, timeout=120,
        )
        if r.returncode != 0:
            logger.error('[%s] Stitch: concat failed for %s + %s: %s',
                         station_id, clip_a.name, clip_b.name,
                         r.stderr[-300:].decode('utf-8', errors='replace'))
            return None

        # Move from local tmp to final destination atomically
        shutil.move(str(out_tmp_local), out_tmp)
        try:
            out_tmp.rename(out_path)
        except OSError as e:
            out_tmp.unlink(missing_ok=True)
            logger.error('[%s] Stitch rename failed: %s', station_id, e)
            return None

    logger.info('[%s] Created stitched clip: %s', station_id, out_name)

    _stitch_stacks(clip_a, clip_b, out_path, station_id, date_str, cfg)
    return out_path


def _stitch_stacks(clip_a: Path, clip_b: Path, stitched_clip: Path,
                   station_id: str, date_str: str, cfg: dict) -> None:
    """Create a max-pixel merged stack for a stitched clip.

    Combines the two source chunk stacks into one stack matching the
    stitched clip's filename, so the archive has a stack for every
    detection clip.
    """
    stacks_dir = clip_a.parent / 'stacks'
    if not stacks_dir.exists():
        return

    stack_a = stacks_dir / clip_a.name.replace('_color.mkv', '_stack.webp')
    stack_b = stacks_dir / clip_b.name.replace('_color.mkv', '_stack.webp')
    out_name = stitched_clip.name.replace('_color.mkv', '_stack.webp')
    out_path = stacks_dir / out_name

    if out_path.exists():
        return

    if not stack_a.exists() or not stack_b.exists():
        logger.warning('[%s] Cannot stitch stacks: missing %s or %s',
                       station_id, stack_a.name, stack_b.name)
        return

    try:
        a = np.array(Image.open(stack_a))
        b = np.array(Image.open(stack_b))
        merged = np.maximum(a, b)
        Image.fromarray(merged).save(out_path, 'WEBP', quality=95, method=4)
        flags_manager.mark_stacked(station_id, date_str, stitched_clip.name, cfg)
        logger.info('[%s] Created stitched stack: %s', station_id, out_name)
    except Exception:
        logger.exception('[%s] Failed to stitch stacks for %s', station_id, out_name)


def process_night(station_id: str, date_str: str, cfg: dict) -> None:
    """EON FTPdetectinfo pass — confirm confirmed detections, remove false positives.

    makeawish: post-night FTPdetectinfo reconciliation pass.
    """
    rms_data = Path(cfg['stations'][station_id]['rms_data_path'])
    archived_base = rms_data / 'ArchivedFiles'
    segment_secs = cfg.get('segment_duration', 20)
    videocapture_path = Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(Path.home() / 'color_capture')
    )

    # Match archived session dirs whose observation night equals date_str.
    # Sessions that started after midnight are filed under the next calendar
    # date (e.g. RO000W_20260412_005904_* belongs to night 20260411), so a
    # plain glob on date_str misses them.  Use _night_date() to convert each
    # directory's timestamp to a night date before comparing.
    _arc_re = re.compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_')
    try:
        night_dirs = sorted([
            d for d in archived_base.iterdir()
            if d.is_dir()
            and (m := _arc_re.match(d.name))
            and m.group(1) == station_id
            and _night_date(m.group(2), m.group(3)) == date_str
        ])
    except OSError as exc:
        logger.error('[%s] Cannot list ArchivedFiles (%s) — skipping EON pass', station_id, exc)
        return
    if not night_dirs:
        logger.warning('[%s] No ArchivedFiles for %s — skipping confirm pass',
                       station_id, date_str)
        return

    confirmed_times: list[datetime] = []
    for night_dir in night_dirs:
        ftpdets = [
            f for f in night_dir.glob('FTPdetectinfo_*.txt')
            if '_unfiltered' not in f.name and '_backup' not in f.name
        ]
        for ftpdet in ftpdets:
            confirmed_times.extend(_parse_ftpdetectinfo(ftpdet))

    logger.info('[%s] %d confirmed detection(s) for %s', station_id,
                len(confirmed_times), date_str)

    chunk_dir = videocapture_path / station_id / date_str
    if not chunk_dir.exists():
        return

    all_chunks = sorted(
        [(ct, p) for p in chunk_dir.glob('*_color.mkv')
         if (ct := _chunk_time(p)) is not None],
        key=lambda x: x[0],
    )

    def _lock(mkv: Path, lock_info: dict) -> None:
        if mkv in confirmed_chunks:
            return
        confirmed_chunks.add(mkv)
        try:
            flags_manager.relock_chunk(station_id, date_str, mkv.name, lock_info, cfg)
            logger.info('[%s] Confirmed lock: %s', station_id, mkv.name)
        except Exception:
            logger.exception('[%s] Failed to relock %s', station_id, mkv.name)

    confirmed_chunks: set[Path] = set()
    for ft in confirmed_times:
        # FTPdetectinfo times are real-world frame timestamps that match the color
        # video directly — match chunks on ft with no RTSP delay offset.
        # (rtsp_capture_delay_s is relevant only for real-time event-driven locking.)
        for idx, (chunk_ts, mkv) in enumerate(all_chunks):
            chunk_end = chunk_ts + timedelta(seconds=segment_secs)
            if not (chunk_ts <= ft <= chunk_end):
                continue

            video_pos = (ft - chunk_ts).total_seconds()
            lock_info = {
                'lock_type': 'detection',
                'detection_time': ft.strftime('%Y%m%d_%H%M%S'),
                'meteor_time': ft.isoformat(),
            }

            # Boundary stitch: detection within 1s of a clip edge → stitch the two
            # adjacent clips and lock the resulting file instead of either original.
            # Falls back to a normal single-clip lock if stitching fails.
            stitched = False
            if video_pos < STITCH_BOUNDARY_SECS and idx > 0:
                stitched_path = _stitch_clips(
                    all_chunks[idx - 1][1], mkv,
                    station_id, date_str, chunk_dir, cfg,
                )
                if stitched_path:
                    try:
                        flags_manager.relock_chunk(
                            station_id, date_str, stitched_path.name, lock_info, cfg)
                        confirmed_chunks.add(stitched_path)
                        logger.info('[%s] Boundary stitch (pre): %s + %s → %s',
                                    station_id, all_chunks[idx - 1][1].name,
                                    mkv.name, stitched_path.name)
                        stitched = True
                    except Exception:
                        logger.exception('[%s] Failed to lock stitched clip %s',
                                         station_id, stitched_path.name)

            elif video_pos > segment_secs - STITCH_BOUNDARY_SECS and idx + 1 < len(all_chunks):
                stitched_path = _stitch_clips(
                    mkv, all_chunks[idx + 1][1],
                    station_id, date_str, chunk_dir, cfg,
                )
                if stitched_path:
                    try:
                        flags_manager.relock_chunk(
                            station_id, date_str, stitched_path.name, lock_info, cfg)
                        confirmed_chunks.add(stitched_path)
                        logger.info('[%s] Boundary stitch (post): %s + %s → %s',
                                    station_id, mkv.name,
                                    all_chunks[idx + 1][1].name, stitched_path.name)
                        stitched = True
                    except Exception:
                        logger.exception('[%s] Failed to lock stitched clip %s',
                                         station_id, stitched_path.name)

            if not stitched:
                _lock(mkv, lock_info)

            break

    # Remove false-positive real-time locks.
    removed = 0
    sstate = flags_manager.load(station_id, date_str, cfg)
    for _, mkv in all_chunks:
        chunk_entry = sstate.get('chunks', {}).get(mkv.name, {})
        is_locked_in_state = chunk_entry.get('lock') is not None
        # Also check legacy .locked sidecar for backward compat
        is_locked_legacy = Path(str(mkv) + '.locked').exists()
        if (is_locked_in_state or is_locked_legacy) and mkv not in confirmed_chunks:
            try:
                flags_manager.unlock_chunk(station_id, date_str, mkv.name, cfg)
                removed += 1
                logger.info('[%s] Removed false-positive lock: %s', station_id, mkv.name)
            except Exception:
                logger.exception('[%s] Failed to remove false-positive %s', station_id, mkv.name)

    logger.info('[%s] EON lock pass: %d confirmed, %d false positives removed for %s',
                station_id, len(confirmed_chunks), removed, date_str)


def main() -> int:
    parser = argparse.ArgumentParser(description='EON FTPdetectinfo lock reconciliation')
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    parser.add_argument('--station', required=True, help='Station ID')
    parser.add_argument('--date', required=True, help='Night date YYYYMMDD')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    with open(args.config) as f:
        cfg = json.load(f)

    process_night(args.station, args.date, cfg)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
