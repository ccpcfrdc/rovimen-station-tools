#!/usr/bin/env python3
"""unlock_old_locks.py — Remove .locked sidecars from color_capture MKVs older than N days.

Scans color_capture/<STATION>/<DATE>/ for locked MKVs in date directories
older than --days. Removes their .locked sidecars so the nightwatcher
retention cleanup can proceed normally on its next run.

Usage:
    python3 unlock_old_locks.py --days 30
    python3 unlock_old_locks.py --days 30 --dry-run
    python3 unlock_old_locks.py --days 30 --config /path/to/config.json
"""

from __future__ import annotations
import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rovimen_lock
import flags_manager

logging.basicConfig(
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO,
)
log = logging.getLogger(__name__)

DEFAULT_CONFIG = Path.home() / 'rovimen_scripts' / 'config.json'


def _is_locked(mkv: Path, station_id: str, date_str: str, cfg: dict) -> bool:
    """Check lock: state.json first, .locked sidecar as fallback."""
    try:
        state = flags_manager.load(station_id, date_str, cfg)
        if mkv.name in state.get('chunks', {}):
            return state['chunks'][mkv.name].get('lock') is not None
    except Exception:
        pass
    return rovimen_lock.is_locked(mkv)


def _get_lock_type(mkv: Path, station_id: str, date_str: str, cfg: dict) -> str | None:
    """Get lock type: state.json first, .locked sidecar as fallback."""
    try:
        state = flags_manager.load(station_id, date_str, cfg)
        lock = state.get('chunks', {}).get(mkv.name, {}).get('lock')
        if lock is not None:
            return lock.get('lock_type')
    except Exception:
        pass
    return rovimen_lock.get_lock_type(mkv)


def main() -> None:
    parser = argparse.ArgumentParser(description='Unlock color_capture MKVs older than N days')
    parser.add_argument('--days', type=int, required=True,
                        help='Unlock locks on date dirs older than this many days')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print what would be unlocked without making changes')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG,
                        help=f'Path to config.json (default: {DEFAULT_CONFIG})')
    args = parser.parse_args()

    if args.days < 1:
        log.error('--days must be at least 1')
        sys.exit(1)

    try:
        cfg = json.loads(args.config.read_text())
    except Exception as exc:
        log.error('Failed to read config %s: %s', args.config, exc)
        sys.exit(1)

    capture_root = Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(Path.home() / 'color_capture')
    )
    if not capture_root.exists():
        log.error('color_capture path not found: %s', capture_root)
        sys.exit(1)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime('%Y%m%d')
    log.info('Scanning %s for locks older than %d days (before %s)%s',
             capture_root, args.days, cutoff, ' [DRY RUN]' if args.dry_run else '')

    unlocked = 0
    errors   = 0

    for station_dir in sorted(capture_root.iterdir()):
        if not station_dir.is_dir():
            continue
        for date_dir in sorted(station_dir.iterdir()):
            if not date_dir.is_dir() or date_dir.name > cutoff:
                continue
            for mkv in sorted(date_dir.glob('*_color.mkv')):
                station_id = station_dir.name
                if not _is_locked(mkv, station_id, date_dir.name, cfg):
                    continue
                lock_type = _get_lock_type(mkv, station_id, date_dir.name, cfg)
                if args.dry_run:
                    log.info('[DRY RUN] Would unlock %s/%s  (%s)',
                             date_dir.name, mkv.name, lock_type)
                    unlocked += 1
                else:
                    try:
                        rovimen_lock.unlock(mkv)
                        log.info('Unlocked %s/%s  (%s)', date_dir.name, mkv.name, lock_type)
                        unlocked += 1
                    except Exception as exc:
                        log.error('Failed to unlock %s: %s', mkv.name, exc)
                        errors += 1

    log.info('Done. %s %d lock(s)%s',
             'Would unlock' if args.dry_run else 'Unlocked',
             unlocked,
             f', {errors} error(s)' if errors else '')

    if errors:
        sys.exit(1)


if __name__ == '__main__':
    main()
