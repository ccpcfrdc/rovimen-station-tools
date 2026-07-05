#!/usr/bin/env python3
"""janitor_storage_watchdog.py — Disk lifecycle cron job for ROVIMEN stations.

Codename: atium

Runs every 10 minutes via cron. Each run does two things:
  1. Normal sweep: age-based retention for all tiers (videos, stacks, timelapse, RMS)
  2. Disk check: pressure-based escalation, chained until disk is under threshold

Handles single and dual-filesystem layouts:
  - Single fs: videos/stacks/timelapse and RMS data on the same device
  - Dual fs:   videocapture_path on external drive, home on root drive —
               each filesystem monitored and escalated independently

Cron entry (installed to /etc/cron.d/rovimen-janitor):
  */10 * * * * {user} {venv}/bin/python {scripts}/janitor_storage_watchdog.py -c {scripts}/config.json

Standalone:
  python janitor_storage_watchdog.py -c config.json
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rovimen_lock
import flags_manager

logger = logging.getLogger('atium')


def _is_locked(mkv: Path, station_id: str, date_str: str, cfg: dict) -> bool:
    """Check lock: state.json first, .locked sidecar as fallback."""
    try:
        state = flags_manager.load(station_id, date_str, cfg)
        if mkv.name in state.get('chunks', {}):
            return state['chunks'][mkv.name].get('lock') is not None
    except Exception:
        pass
    return rovimen_lock.is_locked(mkv)

RETENTION_DAYS = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cutoff(days: int) -> str:
    """Return YYYYMMDD cutoff: dates strictly before this are eligible for deletion."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y%m%d')


def _disk_pct(path: Path) -> int:
    """Return disk usage percentage (0–100) for the filesystem containing path."""
    usage = shutil.disk_usage(path)
    return int(usage.used * 100 // usage.total)


def _existing_parent(p: Path) -> Path:
    """Return p itself if it exists, else the nearest existing ancestor."""
    while not p.exists():
        p = p.parent
    return p


def _safe_iterdir(d: Path) -> list[Path]:
    """Like list(d.iterdir()) but returns [] on I/O errors instead of crashing."""
    try:
        return list(d.iterdir())
    except OSError:
        logger.warning('I/O error listing %s — skipping directory', d)
        return []


# ---------------------------------------------------------------------------
# Janitor
# ---------------------------------------------------------------------------

class Janitor:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        with open(config_path) as f:
            self.cfg = json.load(f)

        self.capture_path = Path(
            self.cfg.get('videocapture_path') or
            self.cfg.get('color_video_path') or
            self.cfg.get('reenc_path') or
            self.cfg.get('color_capture_path') or
            self.cfg.get('ssd_color_path') or
            str(Path.home() / 'color_capture')
        )
        self.log_base = Path(self.cfg.get('log_path', str(Path.home() / 'logs')))

        ret = self.cfg.get('retention', {})
        self.color_retention    = ret.get('color_days',    self.cfg.get('color_retention_days', 2))
        self.locked_retention   = ret.get('locked_days',   self.cfg.get('locked_retention_days', 7))
        self.stack_retention    = ret.get('stacks_days',   self.cfg.get('stack_retention_days', RETENTION_DAYS))
        self.tl_retention       = ret.get('timelapse_days', self.cfg.get('timelapse_retention_days', RETENTION_DAYS))
        self.captured_retention = ret.get('captured_days', 1)
        self.archive_enabled    = self.cfg.get('archive', self.cfg.get('central_archive', {})).get('enabled', False)

        disk = self.cfg.get('disk', {})
        self.warn_pct         = disk.get('warn_pct',         self.cfg.get('disk_warn_pct', 85))
        self.nuclear_pct      = disk.get('nuclear_pct',      self.cfg.get('disk_nuclear_pct', 90))
        self.extreme_pct      = disk.get('extreme_pct',      self.cfg.get('disk_extreme_pct', 95))
        self.home_warn_pct    = disk.get('home_warn_pct',    80)
        self.home_nuclear_pct = disk.get('home_nuclear_pct', 90)

        self.station_ids: list[str] = list(self.cfg.get('stations', {}).keys())

        # Detect whether capture path and home are on the same filesystem.
        # Walk up to nearest existing parent in case capture_path hasn't been
        # created yet on first boot.
        capture_dev = os.stat(_existing_parent(self.capture_path)).st_dev
        home_dev    = os.stat(Path.home()).st_dev
        self.same_fs = (capture_dev == home_dev)

        self._setup_logging()

    def _setup_logging(self) -> None:
        self.log_base.mkdir(parents=True, exist_ok=True)
        log_file = self.log_base / 'janitor.log'

        logger.setLevel(logging.DEBUG)

        fh = logging.handlers.TimedRotatingFileHandler(
            log_file, when='midnight', backupCount=14, utc=True,
        )
        fh.suffix = '%Y%m%d'
        fh.extMatch = re.compile(r'^\.\d{8}$')
        fh.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
        ))
        logger.addHandler(fh)

        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
        ))
        logger.addHandler(sh)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        # atium: disk lifecycle cron — sweep then check
        if not self.capture_path.exists():
            logger.warning(
                'capture path %s does not exist — external drive may be unmounted; skipping',
                self.capture_path,
            )
            return
        logger.debug(
            'atium run: capture=%s  same_fs=%s  stations=%s',
            self.capture_path, self.same_fs, self.station_ids,
        )
        # Snapshot disk usage before sweep so we can report bytes freed
        try:
            _before = shutil.disk_usage(self.capture_path).used
        except OSError:
            _before = 0

        self._run_normal_sweep()
        self._run_disk_check()
        if not self.same_fs:
            self._run_home_check()

        try:
            _after = shutil.disk_usage(self.capture_path).used
            _deleted = max(0, _before - _after)
        except OSError:
            _deleted = 0
        self._write_state(_deleted)

    # ------------------------------------------------------------------
    # State file
    # ------------------------------------------------------------------

    def _write_state(self, deleted_bytes: int = 0) -> None:
        """Write a lightweight JSON state file for the station API to serve."""
        state: dict = {
            'last_run': datetime.now(timezone.utc).isoformat(),
            'deleted_gb': round(deleted_bytes / 1e9, 3),
            'filesystems': {},
        }

        def _fs_entry(path: Path, warn: int, nuclear: int, extreme: int | None) -> dict:
            usage = shutil.disk_usage(path)
            total_gb = usage.total / 1e9
            pct      = int(usage.used * 100 // usage.total)
            gb_until_warn = round(
                max(0.0, (warn / 100 - usage.used / usage.total) * total_gb), 1
            ) if pct < warn else 0.0
            return {
                'pct':           pct,
                'total_gb':      round(total_gb, 1),
                'free_gb':       round(usage.free / 1e9, 1),
                'warn_pct':      warn,
                'nuclear_pct':   nuclear,
                'extreme_pct':   extreme,
                'gb_until_warn': gb_until_warn,
            }

        try:
            state['filesystems'][str(self.capture_path)] = _fs_entry(
                self.capture_path,
                self.warn_pct, self.nuclear_pct, self.extreme_pct,
            )
        except OSError:
            pass

        if not self.same_fs:
            try:
                state['filesystems'][str(Path.home())] = _fs_entry(
                    Path.home(),
                    self.home_warn_pct, self.home_nuclear_pct, None,
                )
            except OSError:
                pass

        try:
            state_path = self.log_base / 'janitor_state.json'
            tmp = state_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(state, indent=2))
            os.replace(tmp, state_path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Normal sweep — age-based retention
    # ------------------------------------------------------------------

    def _run_normal_sweep(self) -> None:
        for sid in self.station_ids:
            try:
                self._purge_station(sid)
            except Exception:
                logger.exception('[%s] Error in normal sweep', sid)
        try:
            self._purge_captured_files()
        except Exception:
            logger.exception('Error in CapturedFiles sweep')

    def _purge_station(self, station_id: str) -> None:
        capture_cutoff = _cutoff(self.color_retention)
        locked_cutoff  = _cutoff(self.locked_retention)
        tl_cutoff      = _cutoff(self.tl_retention)
        stack_cutoff   = _cutoff(self.stack_retention)

        cap_station = self.capture_path / station_id
        if not cap_station.exists():
            return

        for d in _safe_iterdir(cap_station):
            try:
                if not d.is_dir():
                    continue
            except OSError:
                logger.warning('[%s] I/O error checking %s — skipping', station_id, d)
                continue

            # videos/
            videos_dir = d / 'videos'
            if videos_dir.exists():
                for f in _safe_iterdir(videos_dir):
                    try:
                        if not f.is_file():
                            continue
                    except OSError:
                        logger.warning('[%s] I/O error on %s — skipping', station_id, f)
                        continue

                    if f.suffix == '.mkv':
                        if flags_manager.is_chunk_uploaded(station_id, d.name, f.name, self.cfg):
                            rovimen_lock.unlock(f)
                            f.unlink(missing_ok=True)
                            logger.debug('[%s] Purged uploaded chunk: %s', station_id, f.name)
                            continue

                        if _is_locked(f, station_id, d.name, self.cfg):
                            if d.name < locked_cutoff:
                                try:
                                    rovimen_lock.unlock(f)
                                    f.unlink()
                                    logger.info('[%s] Purged expired locked chunk: %s', station_id, f.name)
                                except Exception:
                                    logger.exception('[%s] Failed to purge locked chunk %s', station_id, f.name)
                            elif self.archive_enabled and not flags_manager.is_chunk_uploaded(station_id, d.name, f.name, self.cfg):
                                logger.debug('[%s] Holding %s — locked, not yet uploaded', station_id, f.name)
                        elif d.name < capture_cutoff:
                            f.unlink(missing_ok=True)
                            logger.debug('[%s] Purged %s', station_id, f.name)

                    elif d.name < capture_cutoff:
                        parts = f.stem.split('_')
                        if len(parts) >= 3:
                            mkv = videos_dir / f'{parts[0]}_{parts[1]}_{parts[2]}_color.mkv'
                            if mkv.exists() and _is_locked(mkv, station_id, d.name, self.cfg):
                                continue
                        f.unlink(missing_ok=True)
                        logger.debug('[%s] Purged sidecar %s', station_id, f.name)

                try:
                    videos_dir.rmdir()
                except OSError:
                    pass

            # stacks/thumbs/ — purge thumbnails first so stacks/ can rmdir cleanly
            thumbs_dir = d / 'stacks' / 'thumbs'
            if thumbs_dir.exists() and d.name < stack_cutoff:
                for f in _safe_iterdir(thumbs_dir):
                    try:
                        if f.is_file():
                            f.unlink(missing_ok=True)
                            logger.debug('[%s] Purged thumb %s', station_id, f.name)
                    except OSError:
                        logger.warning('[%s] I/O error on thumb %s — skipping', station_id, f)
                try:
                    thumbs_dir.rmdir()
                except OSError:
                    pass

            # stacks/
            stacks_dir = d / 'stacks'
            if stacks_dir.exists() and d.name < stack_cutoff:
                for f in _safe_iterdir(stacks_dir):
                    try:
                        if not f.is_file():
                            continue
                    except OSError:
                        logger.warning('[%s] I/O error on stack %s — skipping', station_id, f)
                        continue
                    parts = f.stem.split('_')
                    if len(parts) >= 3:
                        mkv = d / 'videos' / f'{parts[0]}_{parts[1]}_{parts[2]}_color.mkv'
                        if mkv.exists() and _is_locked(mkv, station_id, d.name, self.cfg):
                            continue
                    f.unlink(missing_ok=True)
                    logger.debug('[%s] Purged stack %s', station_id, f.name)
                try:
                    stacks_dir.rmdir()
                except OSError:
                    pass

            # timelapse + night stack — files directly in date dir
            if d.name < tl_cutoff:
                for f in _safe_iterdir(d):
                    try:
                        if not f.is_file():
                            continue
                    except OSError:
                        logger.warning('[%s] I/O error on %s — skipping', station_id, f)
                        continue
                    if f.name.endswith('_timelapse.mp4'):
                        if self.archive_enabled and not flags_manager.is_timelapse_uploaded(station_id, d.name, self.cfg):
                            logger.debug('[%s] Timelapse %s retained — not yet uploaded', station_id, f.name)
                            continue
                        f.unlink(missing_ok=True)
                        logger.info('[%s] Purged timelapse: %s/%s', station_id, d.name, f.name)
                    elif f.name.endswith('_night_stack.webp'):
                        if self.archive_enabled and not flags_manager.is_night_stack_uploaded(station_id, d.name, self.cfg):
                            logger.debug('[%s] Night stack %s retained — not yet uploaded', station_id, f.name)
                            continue
                        f.unlink(missing_ok=True)
                        logger.info('[%s] Purged night stack: %s/%s', station_id, d.name, f.name)

            # Legacy flat-layout sweep
            for f in _safe_iterdir(d):
                try:
                    if not f.is_file():
                        continue
                except OSError:
                    logger.warning('[%s] I/O error on %s — skipping', station_id, f)
                    continue
                if f.suffix == '.mkv':
                    if _is_locked(f, station_id, d.name, self.cfg):
                        if d.name < locked_cutoff:
                            try:
                                rovimen_lock.unlock(f)
                                f.unlink()
                                logger.info('[%s] Purged legacy locked chunk: %s', station_id, f.name)
                            except Exception:
                                logger.exception('[%s] Failed to purge legacy chunk %s', station_id, f.name)
                    elif d.name < capture_cutoff:
                        f.unlink(missing_ok=True)
                        logger.debug('[%s] Purged legacy chunk %s', station_id, f.name)
                elif d.name < capture_cutoff and f.name != 'state.json':
                    f.unlink(missing_ok=True)
                    logger.debug('[%s] Purged legacy sidecar %s', station_id, f.name)

            try:
                d.rmdir()
                logger.info('[%s] Purged capture dir: %s', station_id, d)
            except OSError:
                pass

    def _purge_captured_files(self) -> None:
        """Prune RMS CapturedFiles dirs older than captured_retention days
        that have been sent to GMN (both _imgdata.tar.bz2 and _metadata.tar.bz2
        exist in ArchivedFiles).
        """
        cutoff = _cutoff(self.captured_retention)
        for sid, sinfo in self.cfg.get('stations', {}).items():
            rms_data_path = sinfo.get('rms_data_path')
            if not rms_data_path:
                continue
            captured_dir = Path(rms_data_path) / 'CapturedFiles'
            archived_dir = Path(rms_data_path) / 'ArchivedFiles'
            if not captured_dir.exists():
                continue
            for session in list(captured_dir.iterdir()):
                if not session.is_dir():
                    continue
                parts = session.name.split('_')
                if len(parts) < 2 or not parts[1].isdigit() or len(parts[1]) != 8:
                    continue
                if parts[1] >= cutoff:
                    continue
                imgdata  = archived_dir / f'{session.name}_imgdata.tar.bz2'
                metadata = archived_dir / f'{session.name}_metadata.tar.bz2'
                if not imgdata.exists() or not metadata.exists():
                    logger.debug('[%s] Holding CapturedFiles %s — not yet sent to GMN',
                                 sid, session.name)
                    continue
                logger.info('[%s] Pruning CapturedFiles %s (sent to GMN)', sid, session.name)
                shutil.rmtree(session, ignore_errors=True)

    # ------------------------------------------------------------------
    # Disk check — capture filesystem
    # ------------------------------------------------------------------

    def _run_disk_check(self) -> None:
        if not self.capture_path.exists():
            return
        try:
            pct = _disk_pct(self.capture_path)
        except OSError:
            logger.warning('Could not read disk usage for %s', self.capture_path)
            return

        if pct < self.warn_pct:
            return

        logger.info('Capture disk at %d%% — starting escalation', pct)

        if pct >= self.warn_pct:
            self._escalate_uploaded()
            pct = _disk_pct(self.capture_path)

        if pct >= self.nuclear_pct:
            self._escalate_nuclear(also_stacks=False)
            pct = _disk_pct(self.capture_path)

        if pct >= self.extreme_pct:
            self._escalate_nuclear(also_stacks=True)
            pct = _disk_pct(self.capture_path)

        if self.same_fs and pct >= self.home_warn_pct:
            self._escalate_rms_soft()
            pct = _disk_pct(self.capture_path)

        if self.same_fs and pct >= self.home_nuclear_pct:
            self._escalate_rms_hard()
            pct = _disk_pct(self.capture_path)

        if pct >= self.warn_pct:
            logger.error(
                'Disk still at %d%% after full escalation — nothing left to delete', pct,
            )
        else:
            logger.info('Escalation complete — disk now at %d%%', pct)

    # ------------------------------------------------------------------
    # Disk check — home filesystem (only when separate from capture)
    # ------------------------------------------------------------------

    def _run_home_check(self) -> None:
        try:
            pct = _disk_pct(Path.home())
        except OSError:
            logger.warning('Could not read home disk usage')
            return

        if pct < self.home_warn_pct:
            return

        logger.info('Home disk at %d%% — starting RMS escalation', pct)

        if pct >= self.home_warn_pct:
            self._escalate_rms_soft()
            pct = _disk_pct(Path.home())

        if pct >= self.home_nuclear_pct:
            self._escalate_rms_hard()
            pct = _disk_pct(Path.home())

        if pct >= self.home_warn_pct:
            logger.error('Home disk still at %d%% after RMS pruning', pct)
        else:
            logger.info('Home escalation complete — disk now at %d%%', pct)

    # ------------------------------------------------------------------
    # Escalation — capture filesystem
    # ------------------------------------------------------------------

    def _escalate_uploaded(self) -> None:
        """Level 1 — delete uploaded MKVs immediately."""
        deleted = 0
        for sid in self.station_ids:
            cap_station = self.capture_path / sid
            if not cap_station.exists():
                continue
            for d in _safe_iterdir(cap_station):
                try:
                    if not d.is_dir():
                        continue
                except OSError:
                    continue
                videos_dir = d / 'videos'
                mkv_dir = videos_dir if videos_dir.exists() else d
                for f in _safe_iterdir(mkv_dir):
                    try:
                        if not f.is_file() or f.suffix != '.mkv':
                            continue
                    except OSError:
                        logger.warning('[%s] I/O error on %s — skipping', sid, f)
                        continue
                    if not flags_manager.is_chunk_uploaded(sid, d.name, f.name, self.cfg):
                        continue
                    rovimen_lock.unlock(f)
                    f.unlink(missing_ok=True)
                    logger.warning('[%s] Pressure: deleted uploaded chunk %s', sid, f.name)
                    deleted += 1
        logger.warning('Pressure escalation: deleted %d uploaded MKVs', deleted)

    def _trigger_uploader(self) -> None:
        import subprocess
        try:
            subprocess.run(
                ['systemctl', 'start', 'rovimen-uploader.service'],
                capture_output=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    def _escalate_nuclear(self, also_stacks: bool) -> None:
        """Level 2/3 — delete oldest date dirs, oldest first.

        also_stacks=False (nuclear): unlocked MKVs + uploaded locked MKVs.
        also_stacks=True  (extreme): also stacks/ tree + timelapse files.
        """
        all_date_dirs: list[tuple[str, str, Path]] = []
        for sid in self.station_ids:
            cap_station = self.capture_path / sid
            if not cap_station.exists():
                continue
            for d in _safe_iterdir(cap_station):
                try:
                    is_dir = d.is_dir()
                except OSError:
                    continue
                if is_dir and d.name.isdigit() and len(d.name) == 8:
                    all_date_dirs.append((d.name, sid, d))
        all_date_dirs.sort(key=lambda x: x[0])

        level = 'Extreme' if also_stacks else 'Nuclear'
        if not also_stacks:
            self._trigger_uploader()

        for date_str, sid, date_dir in all_date_dirs:
            try:
                pct = _disk_pct(self.capture_path)
            except OSError:
                break
            if pct < self.warn_pct:
                logger.info('Disk below %d%% — stopping %s escalation', self.warn_pct, level)
                break

            videos_dir = date_dir / 'videos'
            mkv_dir = videos_dir if videos_dir.exists() else date_dir

            for f in _safe_iterdir(mkv_dir):
                try:
                    if not f.is_file():
                        continue
                except OSError:
                    logger.warning('[%s] I/O error on %s — skipping', sid, f)
                    continue
                if mkv_dir is date_dir and f.is_dir():
                    continue
                if f.suffix == '.mkv' and _is_locked(f, sid, date_str, self.cfg):
                    if not flags_manager.is_chunk_uploaded(sid, date_str, f.name, self.cfg):
                        if not also_stacks:
                            logger.debug('[%s] Nuclear: skipping locked clip %s (not uploaded)', sid, f.name)
                            continue
                        else:
                            logger.warning('WARNING [%s] Extreme: force-deleting locked clip %s/%s', sid, date_str, f.name)
                    rovimen_lock.unlock(f)

                logger.warning('WARNING [%s] %s: deleting %s/%s', sid, level, date_str, f.name)
                f.unlink(missing_ok=True)

            if videos_dir.exists():
                try:
                    videos_dir.rmdir()
                except OSError:
                    pass

            if also_stacks:
                stacks_dir = date_dir / 'stacks'
                if stacks_dir.exists():
                    logger.warning('WARNING [%s] Extreme: deleting %s/stacks/', sid, date_str)
                    shutil.rmtree(stacks_dir, ignore_errors=True)
                for f in _safe_iterdir(date_dir):
                    try:
                        is_file = f.is_file()
                    except OSError:
                        continue
                    if is_file and (
                        f.name.endswith('_timelapse.mp4') or
                        f.name.endswith('_night_stack.webp')
                    ):
                        logger.warning('WARNING [%s] Extreme: deleting %s/%s', sid, date_str, f.name)
                        f.unlink(missing_ok=True)

            try:
                date_dir.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Escalation — home filesystem (RMS data)
    # ------------------------------------------------------------------

    def _escalate_rms_soft(self) -> None:
        """Soft RMS pruning — delete old FramesFiles and TimeFiles (>2 days).

        Safe to run at any time; does not affect RMS capture or processing.
        """
        cutoff = _cutoff(2)
        for sid, sinfo in self.cfg.get('stations', {}).items():
            rms_data_path = sinfo.get('rms_data_path')
            if not rms_data_path:
                continue
            rms_data = Path(rms_data_path)
            for subdir_name in ('FramesFiles', 'TimeFiles'):
                subdir = rms_data / subdir_name
                if not subdir.exists():
                    continue
                for entry in list(subdir.iterdir()):
                    if entry.is_dir() and entry.name < cutoff:
                        logger.warning('[%s] RMS soft: deleting %s/%s', sid, subdir_name, entry.name)
                        shutil.rmtree(entry, ignore_errors=True)

    def _escalate_rms_hard(self) -> None:
        """Hard RMS pruning — prune CapturedFiles and ArchivedFiles to 3 newest.

        More aggressive; removes older capture sessions and archive results.
        """
        for sid, sinfo in self.cfg.get('stations', {}).items():
            rms_data_path = sinfo.get('rms_data_path')
            if not rms_data_path:
                continue
            rms_data = Path(rms_data_path)
            for subdir_name in ('CapturedFiles', 'ArchivedFiles'):
                subdir = rms_data / subdir_name
                if not subdir.exists():
                    continue
                # Group by camera (directory name prefix up to first '_') so that
                # "keep 3 newest" applies per-camera, not across all cameras combined.
                by_camera: dict[str, list[Path]] = {}
                for d in subdir.iterdir():
                    if not d.is_dir():
                        continue
                    camera = d.name.split('_')[0]
                    by_camera.setdefault(camera, []).append(d)
                for camera, cam_dirs in by_camera.items():
                    cam_dirs.sort(key=lambda d: d.name, reverse=True)
                    for old_dir in cam_dirs[3:]:
                        logger.warning('[%s] RMS hard: deleting %s/%s', sid, subdir_name, old_dir.name)
                        shutil.rmtree(old_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='ROVIMEN disk lifecycle cron job — retention + pressure escalation'
    )
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    args = parser.parse_args()

    janitor = Janitor(args.config)
    janitor.run()


if __name__ == '__main__':
    main()
