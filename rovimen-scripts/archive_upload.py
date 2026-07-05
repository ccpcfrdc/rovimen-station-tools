#!/usr/bin/env python3
"""archive_upload.py — Station-side archive uploader.

Codename: karbranth

Periodically uploads color capture data to the central archive (Hetzner VPS
or storage box) via rsync over SSH.  Tracks uploaded status in state.json
so the janitor knows which files are safe to delete.

Reads configuration from the "archive" block in config.json:
  host, port, base_path, upload_meteors, upload_timelapses, upload_stacks,
  interval_minutes

Called by nightwatcher as a background thread, or standalone:
    python archive_upload.py -c config.json            # single pass
    python archive_upload.py -c config.json --daemon   # long-lived loop
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from dataclasses import dataclass, field

import rovimen_lock
import flags_manager
import encoder

logger = logging.getLogger(__name__)


def _is_locked(mkv: Path, station_id: str, date_str: str, cfg: dict) -> bool:
    """Check lock: state.json first, .locked sidecar as fallback."""
    try:
        state = flags_manager.load(station_id, date_str, cfg)
        if mkv.name in state.get('chunks', {}):
            return state['chunks'][mkv.name].get('lock') is not None
    except Exception:
        pass
    return rovimen_lock.is_locked(mkv)


# ---------------------------------------------------------------------------
# rsync transport
# ---------------------------------------------------------------------------

def _ssh_opts(port: int, ssh_key: str) -> list[str]:
    opts = ['-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-o', 'ControlMaster=auto',
            '-o', 'ControlPath=/tmp/rovimen-ssh-%r@%h:%p',
            '-o', 'ControlPersist=300',
            '-p', str(port)]
    if ssh_key:
        opts += ['-i', ssh_key]
    return opts


def _ssh_mkdir(remote_dir: str, host: str, port: int,
               user: str = 'root', ssh_key: str = '') -> None:
    opts = _ssh_opts(port, ssh_key)
    try:
        result = subprocess.run(
            ['ssh'] + opts + [f'{user}@{host}', f'mkdir -p {remote_dir}'],
            capture_output=True, timeout=20,
        )
        if result.returncode != 0:
            logger.warning('ssh mkdir failed for %s: %s', remote_dir,
                           result.stderr[:200].decode('utf-8', errors='replace')
                           if isinstance(result.stderr, bytes) else str(result.stderr)[:200])
    except subprocess.TimeoutExpired:
        logger.warning('ssh mkdir timed out for %s', remote_dir)
    except Exception as exc:
        logger.warning('ssh mkdir error for %s: %s', remote_dir, exc)


def ssh_delete(remote_path: str, host: str, port: int,
               user: str = 'root', ssh_key: str = '') -> bool:
    """Delete a single file on the remote host via SSH rm -f."""
    opts = _ssh_opts(port, ssh_key)
    try:
        result = subprocess.run(
            ['ssh'] + opts + [f'{user}@{host}', f'rm -f {remote_path}'],
            capture_output=True, timeout=20,
        )
        if result.returncode == 0:
            logger.info('Deleted remote %s', remote_path)
        else:
            logger.warning('ssh_delete non-zero exit for %s: %s', remote_path, result.stderr[:200])
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception) as exc:
        logger.error('ssh_delete failed for %s: %s', remote_path, exc)
        return False


def rsync_file(local: Path, remote_path: str, host: str, port: int,
               user: str = 'root', ssh_key: str = '') -> bool:
    """Upload a single file via rsync. Opens two SSH connections (mkdir + rsync)."""
    remote_dir = str(Path(remote_path).parent)
    _ssh_mkdir(remote_dir, host, port, user, ssh_key)

    opts = _ssh_opts(port, ssh_key)
    cmd = [
        'rsync', '-rltz', '--timeout=60', '--no-owner', '--no-group', '--no-perms',
        '-e', f'ssh {" ".join(opts)}',
        str(local), f'{user}@{host}:{remote_path}',
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error('rsync failed for %s: %s', local.name, result.stderr[:200])
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error('rsync timed out for %s', local.name)
        return False


def rsync_batch(local_files: list[Path], local_dir: Path, remote_dir: str,
                host: str, port: int, user: str = 'root', ssh_key: str = '') -> list[Path]:
    """Upload multiple files from the same directory in a single rsync connection.

    Returns the list of files that were successfully transferred.
    """
    if not local_files:
        return []

    _ssh_mkdir(remote_dir, host, port, user, ssh_key)

    opts = _ssh_opts(port, ssh_key)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as fh:
        tmp = Path(fh.name)
        for p in local_files:
            fh.write(p.name + '\n')

    try:
        cmd = [
            'rsync', '-rltz', '--timeout=60', '--no-owner', '--no-group', '--no-perms',
            '--files-from', str(tmp),
            '-e', f'ssh {" ".join(opts)}',
            str(local_dir) + '/',
            f'{user}@{host}:{remote_dir}/',
        ]
        # Allow at least 300s, plus 1s per file for large batches
        timeout = max(300, len(local_files))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.error('rsync batch failed (%d files): %s',
                         len(local_files), result.stderr[:200])
            logger.info('Falling back to per-file upload for %d files', len(local_files))
            uploaded = []
            for p in local_files:
                remote_path = f'{remote_dir}/{p.name}'
                if rsync_file(p, remote_path, host, port, user, ssh_key):
                    uploaded.append(p)
            return uploaded
        return local_files
    except subprocess.TimeoutExpired:
        logger.error('rsync batch timed out (%d files)', len(local_files))
        return []
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# RMS file helpers
# ---------------------------------------------------------------------------

# Matches compact format: STATION_YYYYMMDD_HHMMSS_MICROSECONDS[_optional]
# e.g. RO000H_20260609_183349_993423
_ARC_SESSION_RE = re.compile(r'^[A-Z0-9]+_(\d{8})_(\d{6})_\d+')


def _rms_night_date(yyyymmdd: str, hhmmss: str) -> str:
    """Return observation night date (YYYYMMDD); nights start at 12:00 UTC."""
    if int(hhmmss[:2]) < 12:
        dt = datetime.strptime(yyyymmdd, '%Y%m%d') - timedelta(days=1)
        return dt.strftime('%Y%m%d')
    return yyyymmdd


# ---------------------------------------------------------------------------
# Upload cycle
# ---------------------------------------------------------------------------

@dataclass
class _ScanResults:
    locked_mkvs: list[tuple[Path, str, str]] = field(default_factory=list)
    stacks: list[tuple[Path, str, str]] = field(default_factory=list)
    timelapses: list[tuple[Path, str, str]] = field(default_factory=list)
    state_jsons: list[tuple[Path, str, str]] = field(default_factory=list)


class _Uploader:
    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        arc = cfg.get('archive', {})
        self.enabled: bool = arc.get('enabled', False)
        self.host: str = arc.get('host', '')
        self.port: int = int(arc.get('port', 22))
        self.user: str = arc.get('user', 'root')
        self.ssh_key: str = arc.get('ssh_key', '')
        self.base_path: str = arc.get('base_path', '/rovimen')
        self.upload_meteors: bool = arc.get('upload_meteors', True)
        self.upload_timelapses: bool = arc.get('upload_timelapses', True)
        self.upload_stacks: bool = arc.get('upload_stacks', False)
        self.upload_rms: bool = arc.get('upload_rms', True)

        self.videocapture_path = Path(
            cfg.get('videocapture_path') or
            cfg.get('color_video_path') or
            cfg.get('reenc_path') or
            cfg.get('color_capture_path') or
            cfg.get('ssd_color_path') or
            str(Path.home() / 'color_capture')
        )
        self.stations = cfg.get('stations', {})
        self._avg_bps: float = 5 * 1024 * 1024  # seed: 5 MB/s

    def _remote(self, camera: str, date: str, subdir: str, filename: str) -> str:
        return f'{self.base_path}/{camera}/{date}/{subdir}/{filename}'

    def _mark_uploaded(self, local: Path, camera: str, date: str) -> None:
        name = local.name
        try:
            if name.endswith('_color.mkv'):
                flags_manager.mark_chunk_uploaded(camera, date, name, self._cfg)
            elif name.endswith('_night_stack.webp'):
                # Must be checked before _stack.webp — night stacks end with _stack.webp too
                flags_manager.mark_night_stack_uploaded(camera, date, self._cfg)
            elif name.endswith('_stack.webp'):
                chunk_name = name.replace('_stack.webp', '_color.mkv')
                flags_manager.mark_chunk_stack_uploaded(camera, date, chunk_name, self._cfg)
            elif name.endswith('_timelapse.mp4'):
                flags_manager.mark_timelapse_uploaded(camera, date, self._cfg)
        except OSError as exc:
            # Corrupted or unwritable night directory — log and continue.
            # The file was already uploaded; worst case it gets re-uploaded next cycle.
            logger.warning('Could not persist uploaded flag for %s (%s/%s): %s',
                           name, camera, date, exc)

    def _upload(self, local: Path, camera: str, date: str, subdir: str) -> bool:
        remote = self._remote(camera, date, subdir, local.name)
        if rsync_file(local, remote, self.host, self.port, self.user, self.ssh_key):
            self._mark_uploaded(local, camera, date)
            logger.info('Uploaded %s → %s', local.name, remote)
            return True
        return False

    def _is_reencoded(self, mkv: Path, station_id: str, date_str: str) -> bool:
        """Check reencoded flag: state.json first, .reencoded sidecar as fallback."""
        try:
            state = flags_manager.load(station_id, date_str, self._cfg)
            chunk = state.get('chunks', {}).get(mkv.name)
            if chunk is not None:
                return bool(chunk.get('reencoded', False))
        except Exception:
            pass
        return Path(str(mkv) + '.reencoded').exists()

    def _scan_all(self) -> _ScanResults:
        """Walk the directory tree once and collect all uploadable files."""
        results = _ScanResults()
        for station_id in self.stations:
            station_dir = self.videocapture_path / station_id
            if not station_dir.exists():
                continue
            for date_dir in station_dir.iterdir():
                if not date_dir.is_dir() or len(date_dir.name) != 8:
                    continue
                date_str = date_dir.name
                sj = date_dir / 'state.json'
                if sj.exists():
                    results.state_jsons.append((sj, station_id, date_str))
                for mkv in date_dir.glob('*_color.mkv'):
                    if flags_manager.is_chunk_uploaded(station_id, date_str, mkv.name, self._cfg):
                        continue
                    if not _is_locked(mkv, station_id, date_str, self._cfg):
                        continue
                    if not self._is_reencoded(mkv, station_id, date_str):
                        continue
                    results.locked_mkvs.append((mkv, station_id, date_str))
                stacks_dir = date_dir / 'stacks'
                if stacks_dir.exists():
                    for webp in stacks_dir.glob('*_stack.webp'):
                        chunk_name = webp.name.replace('_stack.webp', '_color.mkv')
                        if not flags_manager.is_chunk_stack_uploaded(station_id, date_str, chunk_name, self._cfg):
                            results.stacks.append((webp, station_id, date_str))
                for f in date_dir.glob('*_timelapse.mp4'):
                    if not flags_manager.is_timelapse_uploaded(station_id, date_str, self._cfg):
                        results.timelapses.append((f, station_id, date_str))
                for f in date_dir.glob('*_night_stack.webp'):
                    if not flags_manager.is_night_stack_uploaded(station_id, date_str, self._cfg):
                        results.timelapses.append((f, station_id, date_str))
        return results

    def _fmt_size(self, nbytes: int) -> str:
        if nbytes >= 1024 ** 3:
            return f'{nbytes / 1024 ** 3:.1f} GB'
        if nbytes >= 1024 ** 2:
            return f'{nbytes / 1024 ** 2:.0f} MB'
        return f'{nbytes / 1024:.0f} KB'

    def _log_upload_start(self, label: str, station_id: str, date_str: str,
                          files: list[Path]) -> tuple[int, float]:
        total = sum(f.stat().st_size for f in files if f.exists())
        eta_s = total / self._avg_bps if self._avg_bps > 0 else 0
        eta_str = f'{eta_s:.0f}s' if eta_s < 120 else f'{eta_s / 60:.0f} min'
        logger.info('[%s/%s] Uploading %d %s (%s), ETA ~%s',
                    station_id, date_str, len(files), label,
                    self._fmt_size(total), eta_str)
        return total, time.monotonic()

    def _log_upload_done(self, label: str, station_id: str, date_str: str,
                         n: int, total_bytes: int, t0: float) -> None:
        elapsed = time.monotonic() - t0
        if elapsed > 0 and total_bytes > 0:
            bps = total_bytes / elapsed
            self._avg_bps = 0.7 * self._avg_bps + 0.3 * bps
            speed_str = f'{bps / 1024 ** 2:.1f} MB/s'
        else:
            speed_str = 'n/a'
        logger.info('[%s/%s] %s uploaded: %d file(s) in %.0fs (%s)',
                    station_id, date_str, label.capitalize(), n, elapsed, speed_str)

    def _find_rms_files(self, camera: str, date_str: str) -> list[Path]:
        """Return FTPdetectinfo + radiants files for a camera/night from ArchivedFiles."""
        sinfo = self.stations.get(camera, {})
        rms_data_path = sinfo.get('rms_data_path')
        if not rms_data_path:
            return []
        archived_base = Path(rms_data_path) / 'ArchivedFiles'
        if not archived_base.exists():
            return []
        files: list[Path] = []
        try:
            for session_dir in sorted(archived_base.iterdir()):
                if not session_dir.is_dir():
                    continue
                m = _ARC_SESSION_RE.match(session_dir.name)
                if not m:
                    continue
                if _rms_night_date(m.group(1), m.group(2)) != date_str:
                    continue
                for f in session_dir.glob('*_radiants.txt'):
                    files.append(f)
                for f in session_dir.glob('FTPdetectinfo_*.txt'):
                    if '_unfiltered' not in f.name and '_backup' not in f.name:
                        files.append(f)
        except OSError as exc:
            logger.warning('Could not scan RMS sessions for %s/%s: %s', camera, date_str, exc)
        return files

    def _upload_rms_files(self, camera: str, date_str: str) -> int:
        """Upload FTPdetectinfo + radiants files to the archive rms/ subdir."""
        if not self.upload_rms:
            return 0
        files = self._find_rms_files(camera, date_str)
        if not files:
            return 0
        remote_dir = f'{self.base_path}/{camera}/{date_str}/rms'
        count = 0
        for f in files:
            if rsync_file(f, f'{remote_dir}/{f.name}',
                          self.host, self.port, self.user, self.ssh_key):
                logger.info('Uploaded RMS file %s → %s/%s', f.name, remote_dir, f.name)
                count += 1
        return count

    def _upload_state_json(self, station_id: str, date_str: str) -> None:
        """Upload state.json so the archive can reconstruct detection offsets."""
        state_path = self.videocapture_path / station_id / date_str / 'state.json'
        if not state_path.exists():
            return
        remote = f'{self.base_path}/{station_id}/{date_str}/state.json'
        rsync_file(state_path, remote, self.host, self.port, self.user, self.ssh_key)

    def _scan_state_jsons(self) -> list[tuple[Path, str, str]]:
        """Return (state_json_path, station_id, date) for all nights that have a state.json."""
        return self._scan_all().state_jsons

    def _scan_locked_mkvs(self) -> list[tuple[Path, str, str]]:
        return self._scan_all().locked_mkvs

    def _scan_stacks(self) -> list[tuple[Path, str, str]]:
        return self._scan_all().stacks

    def _scan_timelapses(self) -> list[tuple[Path, str, str]]:
        return self._scan_all().timelapses

    def cycle_night(self, station_id: str, date_str: str) -> int:
        """Upload all pending data for a single station/date (current-night fast path).

        Processes stacks → locked MKVs → timelapses for exactly one night,
        then syncs state.json.  Used by Phase 4 of dawn_process so meteor clips
        are uploaded before the long Phase 5 bulk reencode begins.
        """
        if not self.enabled:
            return 0
        count = 0
        date_dir = self.videocapture_path / station_id / date_str
        if not date_dir.exists():
            return 0

        if self.upload_stacks:
            stacks_dir = date_dir / 'stacks'
            if stacks_dir.exists():
                pending = [
                    webp for webp in sorted(stacks_dir.glob('*_stack.webp'))
                    if not flags_manager.is_chunk_stack_uploaded(
                        station_id, date_str,
                        webp.name.replace('_stack.webp', '_color.mkv'),
                        self._cfg,
                    )
                ]
                if pending:
                    total_bytes, t0 = self._log_upload_start('stacks', station_id, date_str, pending)
                    remote_dir = f'{self.base_path}/{station_id}/{date_str}/stacks'
                    uploaded = rsync_batch(
                        pending, stacks_dir, remote_dir,
                        self.host, self.port, self.user, self.ssh_key,
                    )
                    for p in uploaded:
                        self._mark_uploaded(p, station_id, date_str)
                    self._log_upload_done('stacks', station_id, date_str, len(uploaded), total_bytes, t0)
                    count += len(uploaded)

        if self.upload_meteors:
            pending_meteors = [
                mkv for mkv in sorted(date_dir.glob('*_color.mkv'))
                if not flags_manager.is_chunk_uploaded(station_id, date_str, mkv.name, self._cfg)
                and _is_locked(mkv, station_id, date_str, self._cfg)
                and self._is_reencoded(mkv, station_id, date_str)
            ]
            if pending_meteors:
                total_bytes, t0 = self._log_upload_start('meteor clips', station_id, date_str, pending_meteors)
                remote_dir = f'{self.base_path}/{station_id}/{date_str}/meteors'
                uploaded = rsync_batch(
                    pending_meteors, date_dir, remote_dir,
                    self.host, self.port, self.user, self.ssh_key,
                )
                for p in uploaded:
                    self._mark_uploaded(p, station_id, date_str)
                pending_stacks = []
                for mkv in uploaded:
                    stack = date_dir / 'stacks' / mkv.name.replace('_color.mkv', '_stack.webp')
                    if stack.exists() and not flags_manager.is_chunk_stack_uploaded(
                            station_id, date_str, mkv.name, self._cfg):
                        pending_stacks.append(stack)
                stack_ok = 0
                if pending_stacks:
                    stacks_dir = date_dir / 'stacks'
                    stack_remote = f'{self.base_path}/{station_id}/{date_str}/meteors'
                    stack_uploaded = rsync_batch(
                        pending_stacks, stacks_dir, stack_remote,
                        self.host, self.port, self.user, self.ssh_key,
                    )
                    for p in stack_uploaded:
                        self._mark_uploaded(p, station_id, date_str)
                    stack_ok = len(stack_uploaded)
                self._log_upload_done('meteor clips', station_id, date_str, len(uploaded), total_bytes, t0)
                count += len(uploaded) + stack_ok

        if self.upload_timelapses:
            timelapse_files = [
                f for f in date_dir.glob('*_timelapse.mp4')
                if not flags_manager.is_timelapse_uploaded(station_id, date_str, self._cfg)
            ] + [
                f for f in date_dir.glob('*_night_stack.webp')
                if not flags_manager.is_night_stack_uploaded(station_id, date_str, self._cfg)
            ]
            if timelapse_files:
                total_bytes, t0 = self._log_upload_start('timelapses', station_id, date_str, timelapse_files)
                tl_count = 0
                for f in timelapse_files:
                    subdir = 'timelapse'
                    if self._upload(f, station_id, date_str, subdir):
                        tl_count += 1
                        count += 1
                self._log_upload_done('timelapses', station_id, date_str, tl_count, total_bytes, t0)

        if self.upload_meteors:
            self._upload_state_json(station_id, date_str)
            self._upload_rms_files(station_id, date_str)

        return count

    def cycle(self, stop_event: threading.Event | None = None) -> int:
        if not self.enabled:
            return 0
        count = 0

        def _should_stop() -> bool:
            return stop_event is not None and stop_event.is_set()

        scan = self._scan_all()

        if self.upload_stacks:
            # Group pending stacks by (station, date) so each night dir is
            # transferred in a single rsync connection instead of one per file.
            groups: dict[tuple[str, str], list[Path]] = {}
            for path, station, date in scan.stacks:
                groups.setdefault((station, date), []).append(path)

            for (station, date), paths in groups.items():
                if _should_stop():
                    break
                local_dir = paths[0].parent  # the stacks/ subdir
                remote_dir = f'{self.base_path}/{station}/{date}/stacks'
                uploaded = rsync_batch(
                    paths, local_dir, remote_dir,
                    self.host, self.port, self.user, self.ssh_key,
                )
                for p in uploaded:
                    self._mark_uploaded(p, station, date)
                if uploaded:
                    logger.info('Batch uploaded %d stack(s) for %s/%s',
                                len(uploaded), station, date)
                count += len(uploaded)

        if self.upload_meteors:
            meteor_groups: dict[tuple[str, str], list[Path]] = {}
            for path, station, date in scan.locked_mkvs:
                meteor_groups.setdefault((station, date), []).append(path)

            for (station, date), paths in meteor_groups.items():
                if _should_stop():
                    break
                date_dir_local = paths[0].parent
                remote_dir = f'{self.base_path}/{station}/{date}/meteors'
                uploaded = rsync_batch(
                    paths, date_dir_local, remote_dir,
                    self.host, self.port, self.user, self.ssh_key,
                )
                for p in uploaded:
                    self._mark_uploaded(p, station, date)
                for mkv in uploaded:
                    stack = mkv.parent / 'stacks' / mkv.name.replace('_color.mkv', '_stack.webp')
                    if stack.exists() and not flags_manager.is_chunk_stack_uploaded(station, date, mkv.name, self._cfg):
                        if self._upload(stack, station, date, 'meteors'):
                            count += 1
                if uploaded:
                    logger.info('Batch uploaded %d meteor clip(s) for %s/%s',
                                len(uploaded), station, date)
                count += len(uploaded)

        if self.upload_timelapses:
            for path, station, date in scan.timelapses:
                if _should_stop():
                    break
                if self._upload(path, station, date, 'timelapse'):
                    count += 1

        # Keep state.json in sync on the archive so detection offsets are available.
        # rsync skips unchanged files, so this is cheap for already-synced nights.
        if self.upload_meteors:
            for _sj, station, date in scan.state_jsons:
                if _should_stop():
                    break
                self._upload_state_json(station, date)
                self._upload_rms_files(station, date)

        return count


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_night(station_id: str, date_str: str, cfg: dict) -> int:
    """Upload all pending data for a single station/date (Phase 4 fast path).

    Only touches the specified night — no backlog scanning.  Returns the number
    of files uploaded.
    """
    uploader = _Uploader(cfg)
    if not uploader.enabled:
        return 0
    count = uploader.cycle_night(station_id, date_str)
    if count:
        logger.info('[%s/%s] Uploaded %d file(s)', station_id, date_str, count)
    return count


def run_once(station_id: str, date_str: str, cfg: dict) -> int:
    """Full-scan upload pass (all stations, all nights).

    Called as a backfill sweep after Phase 5 when time permits.
    Returns the number of files uploaded.
    """
    uploader = _Uploader(cfg)
    if not uploader.enabled:
        logger.info('[%s/%s] Archive upload disabled in config', station_id, date_str)
        return 0
    logger.info('[%s/%s] Running full-scan upload pass', station_id, date_str)
    count = uploader.cycle()
    logger.info('[%s/%s] Upload pass complete: %d file(s)', station_id, date_str, count)
    return count


def upload_chunk(station_id: str, date_str: str, mkv: Path, cfg: dict) -> bool:
    """Upload a single locked MKV (+ its stack if present) to the archive.

    Called immediately after a manual lock is created so the clip is available
    in the archive without waiting for the next dawn process.
    Re-encodes the clip first (colour calibration + overlay) so the archive
    always receives presentation-quality video, matching dawn_process behaviour.
    Returns True if the MKV was uploaded successfully.
    """
    uploader = _Uploader(cfg)
    if not uploader.enabled:
        return False
    try:
        encoder.process_chunk(mkv, cfg)
    except Exception:
        logger.exception('Re-encode failed for %s — uploading raw', mkv.name)
    ok = uploader._upload(mkv, station_id, date_str, 'meteors')
    stack = mkv.parent / 'stacks' / mkv.name.replace('_color.mkv', '_stack.webp')
    if stack.exists() and not flags_manager.is_chunk_stack_uploaded(
            station_id, date_str, mkv.name, cfg):
        uploader._upload(stack, station_id, date_str, 'meteors')
    uploader._upload_state_json(station_id, date_str)
    return ok


def delete_chunk(station_id: str, date_str: str, filename: str, cfg: dict) -> bool:
    """Remove a chunk from the archive after it has been unlocked.

    Deletes the MKV and its stack WebP from the remote host, then clears the
    uploaded flags in state.json so a re-lock triggers a fresh upload.
    Returns True if the remote MKV was deleted successfully.
    """
    uploader = _Uploader(cfg)
    if not uploader.enabled:
        return False
    remote_mkv   = uploader._remote(station_id, date_str, 'meteors', filename)
    stack_name   = filename.replace('_color.mkv', '_stack.webp')
    remote_stack = uploader._remote(station_id, date_str, 'meteors', stack_name)
    ok = ssh_delete(remote_mkv,   uploader.host, uploader.port, uploader.user, uploader.ssh_key)
    ssh_delete(remote_stack, uploader.host, uploader.port, uploader.user, uploader.ssh_key)
    flags_manager.unmark_chunk_uploaded(station_id, date_str, filename, cfg)
    uploader._upload_state_json(station_id, date_str)
    return ok


# ---------------------------------------------------------------------------
# Background loop (kept for backward compatibility)
# ---------------------------------------------------------------------------

def run_upload_loop(cfg: dict, stop_event: threading.Event) -> None:
    """Background upload loop. Runs until stop_event is set.

    karbranth: periodic archive transfer loop.
    """
    arc = cfg.get('archive', {})
    interval = int(arc.get('interval_minutes', 20)) * 60
    uploader = _Uploader(cfg)

    if not uploader.enabled:
        logger.info('Archive upload disabled in config')
        return

    logger.info('Archive upload loop started: %s:%d  interval=%ds',
                uploader.host, uploader.port, interval)

    while not stop_event.is_set():
        try:
            count = uploader.cycle(stop_event)
            if count:
                logger.info('Upload cycle: %d file(s) uploaded', count)
        except Exception:
            logger.exception('Upload cycle failed')

        # Sleep in small increments so we can respond to stop quickly
        for _ in range(interval // 5):
            if stop_event.is_set():
                break
            stop_event.wait(5)

    logger.info('Archive upload loop stopped')


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description='ROVIMEN archive uploader')
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    parser.add_argument('--daemon', action='store_true',
                        help='Run as a long-lived daemon (loop with interval_minutes)')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    with open(args.config) as f:
        cfg = json.load(f)

    if args.daemon:
        stop = threading.Event()

        def _shutdown(sig, _frame):
            logger.info('Signal %d — stopping', sig)
            stop.set()

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        run_upload_loop(cfg, stop)
    else:
        # Single-pass mode: upload everything and exit
        uploader = _Uploader(cfg)
        if not uploader.enabled:
            logger.info('Archive upload disabled in config')
            return
        count = uploader.cycle()
        logger.info('Single-pass upload complete: %d file(s)', count)


if __name__ == '__main__':
    main()
