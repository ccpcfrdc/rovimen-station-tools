#!/usr/bin/env python3
"""dawn_process.py — Post-RMS morning processing cron job.

Codename: sunheart

Runs every 15 minutes from 04:00–10:00 UTC.  Exits immediately if RMS has not
finished (no FTPdetectinfo found) or if morning_done is already True for a
station.  When all cameras have finished RMS, runs the morning pipeline:

  Phase 1 — Stack all cameras (parallel batches)
  Phase 2 — Verify stacking completeness, rerun for gaps
  Phase 3 — Timelapse (all cameras)
  Phase 4 — EON detection pass + encode locked clips
  Phase 5 — Upload stacks + locked clips to archive
  Phase 6 — Bulk reencode remaining chunks (long-running; idempotent)

morning_done is set per camera after Phase 6 completes for that camera.

Cron entry — installed to /etc/cron.d/rovimen-dawn:
  */15 4-10 * * * {user} {venv}/bin/python {scripts}/dawn_process.py \\
      >> ~/logs/rovimen_dawn.log 2>&1

Standalone usage:
  python dawn_process.py [-c config.json] [--station RO000H] [--date 20260322]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LOCK_FILE = Path('/tmp/rovimen_dawn.lock')

import archive_upload
import detection_lock
import encoder
import stacker
import flags_manager
import timelapse_build

logger = logging.getLogger('dawn_process')

DEFAULT_CONFIG = Path.home() / 'rovimen_scripts' / 'config.json'

_ARCHIVE_DIR_RE = re.compile(r'([A-Z0-9]+)_(\d{8})_(\d{6})_\d+')
_CHUNK_RE = re.compile(r'^[A-Z0-9]+_\d{8}_\d{6}_color\.mkv$')


# ---------------------------------------------------------------------------
# Night-date helpers
# ---------------------------------------------------------------------------

def _night_date() -> str:
    """Return the date string for the most recently completed night.

    The cron window is 04:00–10:00 UTC, so we are always before noon —
    last night's data is under yesterday's date.
    """
    now = datetime.now(timezone.utc)
    if now.hour < 12:
        return (now - timedelta(days=1)).strftime('%Y%m%d')
    return now.strftime('%Y%m%d')


def _archive_night(date_s: str, time_s: str) -> str:
    """Convert an ArchivedFiles directory timestamp to its night date."""
    if int(time_s[:2]) < 12:
        dt = datetime(int(date_s[:4]), int(date_s[4:6]), int(date_s[6:]),
                      tzinfo=timezone.utc) - timedelta(days=1)
        return dt.strftime('%Y%m%d')
    return date_s


# ---------------------------------------------------------------------------
# RMS completion check
# ---------------------------------------------------------------------------

def _rms_done(station_id: str, date_str: str, rms_data_path: str) -> bool:
    """Return True if at least one valid FTPdetectinfo exists for this night."""
    rms_data = Path(rms_data_path)
    for subdir_name in ('ArchivedFiles', 'CapturedFiles'):
        base = rms_data / subdir_name
        if not base.exists():
            continue
        for arc_dir in sorted(base.iterdir()):
            if not arc_dir.is_dir():
                continue
            m = _ARCHIVE_DIR_RE.match(arc_dir.name)
            if not m:
                continue
            if m.group(1) != station_id:
                continue
            if _archive_night(m.group(2), m.group(3)) != date_str:
                continue
            ftpdets = [
                f for f in arc_dir.glob('FTPdetectinfo_*.txt')
                if '_unfiltered' not in f.name and '_backup' not in f.name
            ]
            if ftpdets:
                return True
    return False


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _svc_enabled(cfg: dict, service: str) -> bool:
    """Return True if the service is enabled in config."""
    services = cfg.get('services', {})
    svc = services.get(service, {})
    if isinstance(svc, dict):
        return bool(svc.get('enabled', False))
    return service in cfg.get('active_services', [])


def _stacker_enabled(cfg: dict) -> bool:
    if _svc_enabled(cfg, 'stacker'):
        return True
    return int(cfg.get('stacker_mode', 0)) != 0


def _uploader_enabled(cfg: dict) -> bool:
    if _svc_enabled(cfg, 'archive_upload'):
        return True
    return bool(cfg.get('archive', {}).get('enabled', False))


# ---------------------------------------------------------------------------
# Stale lock sweep
# ---------------------------------------------------------------------------

def _capture_root(cfg: dict) -> Path:
    return Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(Path.home() / 'color_capture')
    )


def _sweep_stale_locks(cfg: dict) -> None:
    """Clear lock fields in state.json for nights older than stale_lock_days."""
    days = cfg.get('stale_lock_days', 7)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y%m%d')
    capture_root = _capture_root(cfg)
    if not capture_root.exists():
        return
    count = 0
    try:
        station_dirs = list(capture_root.iterdir())
    except OSError as exc:
        logger.warning('Cannot list %s: %s', capture_root, exc)
        return
    for station_dir in station_dirs:
        if not station_dir.is_dir():
            continue
        try:
            date_dirs = list(station_dir.iterdir())
        except OSError:
            continue
        for date_dir in date_dirs:
            if not date_dir.is_dir() or date_dir.name >= cutoff:
                continue
            sid = station_dir.name
            dstr = date_dir.name
            sstate = flags_manager.load(sid, dstr, cfg)
            changed = False
            for chunk_name, chunk in sstate.get('chunks', {}).items():
                if chunk.get('lock') is not None:
                    chunk['lock'] = None
                    changed = True
                    count += 1
                    logger.info('[stale-lock] Cleared lock for %s/%s/%s',
                                sid, dstr, chunk_name)
            if changed:
                flags_manager.save(sstate, sid, dstr, cfg)
    if count:
        logger.info('[stale-lock] Cleared %d lock(s) older than %d days', count, days)


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------

def _reconcile_ready(stations: dict, date_str: str, cfg: dict) -> None:
    """Mark on-disk chunks as ready if their flag is missing in state.json.

    Fixes chunks whose IN_CLOSE_WRITE event was dropped by the inotify watcher
    (typically after a CPU/I/O stall that causes a burst of events, crashing
    the watcher before mark_ready() is called).  Safe to call on completed
    nights — all MKV files in the night directory are fully written by dawn.
    """
    capture_root = _capture_root(cfg)
    for station_id in stations:
        night_dir = capture_root / station_id / date_str
        if not night_dir.exists():
            continue
        state = flags_manager.load(station_id, date_str, cfg)
        chunk_states = state.get('chunks', {})
        to_fix = [
            mkv.name for mkv in sorted(night_dir.glob('*_color.mkv'))
            if _CHUNK_RE.match(mkv.name)
            and not chunk_states.get(mkv.name, {}).get('ready', False)
        ]
        if to_fix:
            flags_manager.mark_ready_batch(station_id, date_str, to_fix, cfg)
        if to_fix:
            logger.warning(
                '[%s] Reconciled %d not-ready chunk(s) for %s — '
                'inotify events likely dropped during watcher crash: %s',
                station_id, len(to_fix), date_str, ', '.join(to_fix),
            )



def _run_stack_all(stations: dict, date_str: str, cfg: dict) -> None:
    """Phase 1: Stack cameras with controlled parallelism.

    perfile mode (one ffmpeg per chunk, parallel workers per camera) is used —
    concat mode silently abandons chunks when decoded frame count < container
    metadata, and has been disabled.

    Cameras are stacked in parallel batches of ``dawn_stack_parallelism``
    (default 2).  On a 6-core i5-8500 with stack_workers=2 each camera uses
    ~3 cores on average; two cameras in parallel fills all 6 cores with no
    contention.  Running all 4 at once overshoots and causes context-switch
    overhead.  Set ``dawn_stack_parallelism: 1`` in config to go sequential,
    or raise it for NVMe-backed stations with more cores.
    """
    cpu_cores = cfg.get('_cpu_cores', 4)
    parallelism = max(1, cfg.get('dawn_stack_parallelism', cpu_cores // 3))
    station_ids = list(stations)

    for i in range(0, len(station_ids), parallelism):
        batch = station_ids[i:i + parallelism]
        if len(batch) == 1:
            try:
                stacker.process_night(batch[0], date_str, cfg)
            except Exception:
                logger.exception('[%s] Stacking failed', batch[0])
        else:
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futs = {pool.submit(stacker.process_night, sid, date_str, cfg): sid
                        for sid in batch}
                for fut in as_completed(futs):
                    sid = futs[fut]
                    try:
                        fut.result()
                    except Exception:
                        logger.exception('[%s] Stacking failed', sid)


_STACK_COVERAGE_THRESHOLD = 0.85  # defer timelapse if fewer than 85% of stacks are present


def _stack_coverage(station_id: str, date_str: str, cfg: dict) -> tuple[int, int]:
    """Return (ready_count, missing_stack_count) for a station/date.

    A chunk counts as missing if it is ready in state.json but has no stack
    WebP in <night_dir>/stacks/.
    """
    capture_root = _capture_root(cfg)
    night_dir = capture_root / station_id / date_str
    stacks_dir = night_dir / 'stacks'
    sstate = flags_manager.load(station_id, date_str, cfg)
    chunk_states = sstate.get('chunks', {})
    ready = [n for n, v in chunk_states.items() if v.get('ready')]
    missing = [
        n for n in ready
        if not chunk_states[n].get('stacked')
        and not (stacks_dir / n.replace('_color.mkv', '_stack.webp')).exists()
    ]
    return len(ready), len(missing)


def _run_timelapse_all(stations: dict, date_str: str, cfg: dict) -> None:
    """Phase 3: Build timelapse for cameras with adequate stack coverage.

    Cameras missing more than 15% of stacks are skipped and flagged as
    timelapse_deferred so the retry sweep can pick them up on subsequent days.
    """
    for station_id in stations:
        if flags_manager.is_timelapse_done(station_id, date_str, cfg):
            logger.info('[%s] Timelapse already done for %s — skipping', station_id, date_str)
            continue
        ready, missing = _stack_coverage(station_id, date_str, cfg)
        if ready > 0:
            coverage = (ready - missing) / ready
            if coverage < _STACK_COVERAGE_THRESHOLD:
                logger.warning(
                    '[%s] Stack coverage %.1f%% (%d/%d missing) — deferring timelapse for %s',
                    station_id, coverage * 100, missing, ready, date_str,
                )
                flags_manager.mark_timelapse_deferred(station_id, date_str, cfg)
                continue
        try:
            timelapse_build.build(station_id, date_str, cfg)
        except Exception:
            logger.exception('[%s] Timelapse failed', station_id)


def _retry_deferred_timelapses(stations: dict, cfg: dict) -> None:
    """Sweep the last 3 nights for deferred timelapses and retry if coverage improved.

    After 2 days we build with whatever stacks exist rather than waiting further.
    Called at the start of main() so retries happen even on nights already marked
    morning_done.
    """
    today = datetime.now(timezone.utc).date()
    for days_ago in range(1, 4):
        date_str = (today - timedelta(days=days_ago)).strftime('%Y%m%d')
        for station_id in stations:
            if not flags_manager.is_timelapse_deferred(station_id, date_str, cfg):
                continue
            if flags_manager.is_timelapse_done(station_id, date_str, cfg):
                continue
            ready, missing = _stack_coverage(station_id, date_str, cfg)
            coverage = (ready - missing) / ready if ready > 0 else 1.0
            give_up = days_ago >= 2
            if coverage >= _STACK_COVERAGE_THRESHOLD or give_up:
                reason = 'coverage recovered' if coverage >= _STACK_COVERAGE_THRESHOLD else 'deadline (2d)'
                logger.info(
                    '[%s] Retrying deferred timelapse for %s (%s, coverage=%.1f%%)',
                    station_id, date_str, reason, coverage * 100,
                )
                try:
                    timelapse_build.build(station_id, date_str, cfg)
                except Exception:
                    logger.exception('[%s] Deferred timelapse retry failed for %s',
                                     station_id, date_str)
            else:
                logger.info(
                    '[%s] Deferred timelapse for %s still waiting (coverage=%.1f%%)',
                    station_id, date_str, coverage * 100,
                )


def _verify_and_restack(stations: dict, date_str: str, cfg: dict) -> None:
    """Phase 2: Verify stacking completeness and rerun for gaps.

    After the initial stack pass, checks each camera for missing stacks
    and reruns the stacker for cameras with gaps.
    """
    for station_id in stations:
        ready, missing = _stack_coverage(station_id, date_str, cfg)
        if missing > 0:
            logger.info('[%s] %d/%d stacks missing after first pass — restacking',
                        station_id, missing, ready)
            try:
                stacker.process_night(station_id, date_str, cfg)
            except Exception:
                logger.exception('[%s] Restack pass failed', station_id)
            ready2, missing2 = _stack_coverage(station_id, date_str, cfg)
            if missing2 > 0:
                logger.warning('[%s] Still %d/%d stacks missing after restack',
                               station_id, missing2, ready2)
        else:
            logger.info('[%s] All %d stacks present', station_id, ready)


def _run_encode_locked_all(stations: dict, date_str: str, cfg: dict) -> None:
    """Phase 4: EON detection pass + encode locked clips.

    Runs detection_lock to reconcile FTPdetectinfo locks, then encodes
    only the locked (detection) clips with the full pipeline (rotate +
    colour calibration + overlay).  process_chunk() is idempotent — clips
    encoded here are skipped by encoder.process_night() in Phase 6.

    Cameras are processed in parallel — each camera writes to its own
    state.json, so there is no cross-camera contention.
    """
    for station_id in stations:
        flags_manager.mark_rms_complete(station_id, date_str, cfg)

    def _detect_and_encode(station_id: str, encode_cfg: dict) -> None:
        try:
            detection_lock.process_night(station_id, date_str, cfg)
        except Exception:
            logger.exception('[%s] EON detection pass failed', station_id)
        try:
            encoder.process_night_locked_only(station_id, date_str, encode_cfg)
        except Exception:
            logger.exception('[%s] Locked clip encode failed', station_id)

    if len(stations) <= 1:
        for sid in stations:
            _detect_and_encode(sid, cfg)
    else:
        parallel_cfg = {**cfg, 'dawn_encode_parallelism': 1}
        with ThreadPoolExecutor(max_workers=len(stations)) as pool:
            futs = {pool.submit(_detect_and_encode, sid, parallel_cfg): sid for sid in stations}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    logger.exception('[%s] Phase 4 failed', futs[fut])


def _run_upload_all(stations: dict, date_str: str, cfg: dict) -> None:
    """Phase 5: Upload stacks + locked clips to archive.

    Only the current night's data is uploaded here — backlog from previous
    nights is handled by _run_backfill_upload() after Phase 6.

    Cameras are uploaded in parallel — each camera creates an independent
    _Uploader and writes to its own state.json.
    """
    if len(stations) <= 1:
        for station_id in stations:
            try:
                archive_upload.run_night(station_id, date_str, cfg)
            except Exception:
                logger.exception('[%s] Current-night upload failed', station_id)
    else:
        with ThreadPoolExecutor(max_workers=len(stations)) as pool:
            futs = {pool.submit(archive_upload.run_night, sid, date_str, cfg): sid
                    for sid in stations}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    logger.exception('[%s] Current-night upload failed', futs[fut])


def _run_backfill_upload(cfg: dict) -> None:
    """Post-Phase-6 backfill: upload previous nights if time permits.

    Runs a full-scan pass (all stations, all nights) so that stacks and
    meteor clips from earlier nights are eventually synced to the archive.
    Skipped if fewer than 20 minutes remain before the 11:00 UTC hard
    deadline — not enough time to make meaningful progress.
    """
    now = datetime.now(timezone.utc)
    deadline = now.replace(hour=11, minute=0, second=0, microsecond=0)
    minutes_left = (deadline - now).total_seconds() / 60
    if minutes_left < 20:
        logger.info('Only %.0f min before deadline — skipping backfill upload', minutes_left)
        return
    logger.info('%.0f min before deadline — running backfill upload sweep', minutes_left)
    first = next(iter(cfg.get('stations', {})), None)
    if not first:
        return
    try:
        archive_upload.run_once(first, '', cfg)
    except Exception:
        logger.exception('Backfill upload sweep failed')


def _run_reencode_all(stations: dict, date_str: str, cfg: dict) -> None:
    """Phase 6: Full bulk reencode of all chunks for all cameras.

    Runs after upload so the archive receives clips without waiting for the
    full night's pass.  process_night() is idempotent — locked clips already
    encoded in Phase 4 are skipped automatically.

    This is the long-running phase and may be interrupted by the daily reboot.
    morning_done is marked per camera as each camera's reencode completes so
    that interrupted cameras are resumed next morning.
    """
    for station_id in stations:
        try:
            encoder.process_night(station_id, date_str, cfg)
        except Exception:
            logger.exception('[%s] Re-encode failed', station_id)
        # Final stacking pass: picks up any chunks written after Phase 2 ran
        # (write-race at 04:00 — last ffmpeg segments close while dawn is already
        # stacking earlier chunks; the encoder globs them fresh so they get
        # reencoded, but stacking is never revisited without this pass).
        if _stacker_enabled(cfg):
            try:
                stacker.process_night(station_id, date_str, cfg)
            except Exception:
                logger.exception('[%s] Final stack pass failed', station_id)
        flags_manager.mark_morning_done(station_id, date_str, cfg)
        logger.info('=== [%s] Morning sequence complete: %s ===', station_id, date_str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_MODULE_LOGS: dict[str, str] = {
    'stacker': 'stacker.log',
    'encoder': 'encoder.log',
    'timelapse_build': 'timelapse.log',
}

_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s',
                         datefmt='%Y-%m-%dT%H:%M:%S')


def _attach_module_logs(cfg: dict) -> None:
    """Attach per-module FileHandlers after config is loaded.

    Each of stacker, encoder, and timelapse_build gets its own log file under
    log_path (default ~/logs/).  Records still propagate to the root handler so
    rovimen_dawn.log remains a complete record of the morning sequence.
    """
    log_dir = Path(cfg.get('log_path') or (Path.home() / 'logs'))
    log_dir.mkdir(parents=True, exist_ok=True)
    for module_name, filename in _MODULE_LOGS.items():
        mod_logger = logging.getLogger(module_name)
        if any(isinstance(h, logging.FileHandler) and
               Path(h.baseFilename).name == filename
               for h in mod_logger.handlers):
            continue  # already attached (e.g. second call in same process)
        fh = logging.FileHandler(log_dir / filename)
        fh.setFormatter(_fmt)
        mod_logger.addHandler(fh)


def main() -> int:
    # Ensure only one instance runs at a time — cron fires every 15 min and
    # processing a full night can take longer than that on slow stations.
    _lock_fd = open(_LOCK_FILE, 'w')
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _lock_fd.close()
        print('dawn_process: another instance is running — exiting', file=sys.stderr)
        return 0

    try:
        return _main_locked(_lock_fd)
    finally:
        _lock_fd.close()


def _main_locked(_lock_fd) -> int:
    parser = argparse.ArgumentParser(description='Post-RMS morning processing')
    parser.add_argument('-c', '--config', default=str(DEFAULT_CONFIG),
                        help='Path to config.json')
    parser.add_argument('--station', help='Process a specific station only')
    parser.add_argument('--date', help='Override night date (YYYYMMDD)')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    )

    try:
        with open(args.config) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        logger.error('Config not found: %s', args.config)
        return 1

    _attach_module_logs(cfg)

    # Inject cpu_cores from hardware.json (co-located with config.json) so
    # phase functions can derive parallelism without needing the config path.
    hw_path = Path(args.config).parent / 'hardware.json'
    try:
        hw = json.loads(hw_path.read_text())
        cfg['_cpu_cores'] = int(hw.get('cpu_cores', 4))
    except Exception:
        cfg['_cpu_cores'] = 4

    date_str = args.date or _night_date()

    # Hard deadline — do not start (or continue) automatic processing after 11:00 UTC.
    # Protects against a late-starting cron slot or manual invocation creeping into
    # the reboot window.  Bypass with --date for manual/debug runs.
    if not args.date:
        now_utc = datetime.now(timezone.utc)
        if now_utc.hour >= 11:
            logger.info('Past 11:00 UTC deadline (%s) — skipping', now_utc.strftime('%H:%M'))
            return 0

    stations = cfg.get('stations', {})
    if args.station:
        if args.station not in stations:
            logger.error('Station %s not in config', args.station)
            return 1
        stations = {args.station: stations[args.station]}

    # Drop decommissioned cameras (e.g. dead hardware whose rms-camN.service
    # is disabled). Keeping them in the loop blocks the entire station: the
    # RMS-finished gate below waits for an FTPdetectinfo that will never
    # appear, every dawn cron tick, until the 11:00 UTC deadline kills the
    # whole night's processing for the surviving cameras too.
    decommissioned = [
        sid for sid, scfg in stations.items() if scfg.get('decommissioned')
    ]
    if decommissioned:
        logger.info('Skipping decommissioned cameras: %s', ', '.join(decommissioned))
        stations = {
            sid: scfg for sid, scfg in stations.items() if sid not in decommissioned
        }
        if not stations:
            logger.info('No active stations remaining — nothing to do')
            return 0

    # Retry any timelapses deferred from previous nights due to low stack coverage.
    if _stacker_enabled(cfg) and _svc_enabled(cfg, 'timelapse_build'):
        _retry_deferred_timelapses(stations, cfg)

    # Only process once ALL pending stations have finished RMS post-processing.
    # Processing one camera while others are still running DetectStarsAndMeteors
    # starves RMS of CPU headroom.
    pending = {
        sid: scfg for sid, scfg in stations.items()
        if not flags_manager.is_morning_done(sid, date_str, cfg)
    }

    if not pending:
        logger.debug('All stations already morning_done for %s', date_str)
        return 0

    waiting = [
        sid for sid, scfg in pending.items()
        if not _rms_done(sid, date_str, scfg.get('rms_data_path', ''))
    ]
    if waiting:
        logger.info('Waiting for RMS to finish on: %s — will retry', ', '.join(waiting))
        return 0

    def _past_deadline() -> bool:
        if args.date:
            return False
        return datetime.now(timezone.utc).hour >= 11

    # Reconcile any chunks whose ready flag was missed by the inotify watcher
    _reconcile_ready(pending, date_str, cfg)

    # Phase 0: FPN calibration (build per-camera correction frames)
    if _svc_enabled(cfg, 'fpn_calibration'):
        import fpn_calibration
        for station_id in pending:
            try:
                fpn_calibration.build_calibration(station_id, date_str, cfg)
            except Exception:
                logger.exception('[%s] FPN calibration failed — continuing without', station_id)

    # Phase 1: Stack all cameras
    if _stacker_enabled(cfg):
        _run_stack_all(pending, date_str, cfg)

    if _past_deadline():
        logger.info('Past 11:00 UTC deadline after Phase 1 — skipping optional phases, running critical path')
    else:
        # Phase 2: Verify stacking completeness, rerun for gaps
        if _stacker_enabled(cfg):
            _verify_and_restack(pending, date_str, cfg)

        # Phase 3: Timelapse (requires stacker output)
        if _stacker_enabled(cfg) and _svc_enabled(cfg, 'timelapse_build'):
            _run_timelapse_all(pending, date_str, cfg)

    # Phase 4: Detection lock + encode locked clips (critical — always runs)
    _run_encode_locked_all(pending, date_str, cfg)

    # Colour meteor-only night stack (needs detection info from Phase 4)
    if _stacker_enabled(cfg):
        import stacker as _stacker_mod
        for station_id in pending:
            try:
                _stacker_mod.build_night_color_meteor_stack(station_id, date_str, cfg)
            except Exception as e:
                logger.warning('[%s] night meteor stack failed: %s', station_id, e)

    # Phase 5: Upload stacks + locked clips to archive (critical — always runs)
    if _uploader_enabled(cfg):
        _run_upload_all(pending, date_str, cfg)

    if _past_deadline():
        logger.info('Past 11:00 UTC deadline — skipping bulk reencode and backfill')
    else:
        # Phase 6: Bulk reencode remaining chunks (long-running; marks morning_done)
        if _svc_enabled(cfg, 'reencode'):
            _run_reencode_all(pending, date_str, cfg)
        else:
            for station_id in pending:
                flags_manager.mark_morning_done(station_id, date_str, cfg)
                logger.info('=== [%s] Morning sequence complete: %s ===', station_id, date_str)

        # Backfill: upload previous-night stacks/meteors if time remains before deadline.
        if _uploader_enabled(cfg) and not args.date:
            _run_backfill_upload(cfg)

    # Mark morning_done if we skipped Phase 6 due to deadline
    if _past_deadline():
        for station_id in pending:
            if not flags_manager.is_morning_done(station_id, date_str, cfg):
                flags_manager.mark_morning_done(station_id, date_str, cfg)
                logger.info('=== [%s] Morning sequence complete (deadline, reencode deferred): %s ===',
                            station_id, date_str)

    try:
        _sweep_stale_locks(cfg)
    except OSError:
        logger.exception('Stale lock sweep failed (EIO?) — continuing')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
