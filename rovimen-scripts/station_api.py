#!/usr/bin/env python3
"""
station_api.py — Station-side REST API

Replaces: SSHFS mounts, SSH collect scripts, python3 -m http.server.
Run as a systemd service on each station (port 7779).

Endpoints
─────────
GET /api/status          services, storage, disk, RMS detections  (cached 60s)
GET /api/vitals          CPU %, RAM %, temp                        (cached 15s)
GET /api/nights/<cam>    list night date dirs for a camera
GET /api/chunks/<cam>/<date>?locked_only=0&from=HH:MM&to=HH:MM
GET /api/rms-detections/<cam>/<date>  enriched meteor data (mag, shower, radiant)
GET /api/timelapses      timelapse listing per camera
GET /api/settings        config.json contents
GET /api/encoding/stats  file-size reduction stats for last reencoded night (cached 1h)
GET /api/latest_stack/<cam>        most recent _captured_stack.jpg across all sessions
GET /api/latest_chunk_stack/<cam>  freshest per-chunk colour stack (rolling ~5 min)
GET /api/latest_frame/<cam>        chunk stack → captured_stack fallback (live tile)
GET /api/latest_ff_maxpixel/<cam>  single newest FF maxpixel, BW WebP, no disk write

GET /color_capture/<cam>/<date>/<file>    serve MKV / WebP (with Range support)
GET /color_timelapse/<cam>/<date>/<file>  serve MP4        (with Range support)
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, stream_with_context

import archive_upload
import rovimen_lock
try:
    import flags_manager as _flags_manager
    _STATE_MANAGER_OK = True
except ImportError:
    _STATE_MANAGER_OK = False

app = Flask(__name__)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Network read-only guard (reversed-HTTP push migration)
# ---------------------------------------------------------------------------
# When ROVIMEN_STATION_READONLY=1 the station API becomes read-only *from the
# network*: state-changing endpoints only accept requests from loopback. Remote
# callers are 403'd and must instead route the action through the signed command
# channel (dashboard enqueues -> CommandWorker in rovimen_pusher.py long-polls,
# verifies the ed25519 signature, and POSTs to 127.0.0.1:7779 from loopback).
#
# Default OFF -> behaviour is byte-for-byte identical to today. Only stations
# that have been cut over to push set the flag, so this is a no-op fleet-wide
# until a station opts in.
READONLY_ENV = "ROVIMEN_STATION_READONLY"

# Loopback source addresses that are always permitted to mutate. The
# CommandWorker's local POST originates from one of these.
_LOOPBACK_ADDRS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

# Mutating endpoint functions (Flask view names) guarded when read-only mode is
# on. GET/read endpoints and the diagnostic /api/probe are deliberately absent —
# they are never blocked. Kept as endpoint (function) names rather than URL
# prefixes so a route rename fails loud in tests instead of silently unguarding.
_MUTATING_ENDPOINTS = frozenset({
    "api_reboot",
    "api_restart_service",
    "api_settings_patch",
    "api_lock",
    "api_archive_test",
    "api_updater_run",
    "api_updater_check",
    "api_services_restart",
})


def _readonly_enabled() -> bool:
    return os.environ.get(READONLY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback(remote_addr: str | None) -> bool:
    return bool(remote_addr) and remote_addr in _LOOPBACK_ADDRS


@app.before_request
def _enforce_network_readonly():
    """Block non-loopback callers of mutating endpoints when read-only mode is on.

    Returns a 403 JSON body directing the caller to the signed command channel;
    returning ``None`` lets the request proceed unchanged (the default when the
    flag is off, the endpoint is read-only, or the caller is loopback)."""
    if not _readonly_enabled():
        return None
    if request.endpoint not in _MUTATING_ENDPOINTS:
        return None
    if _is_loopback(request.remote_addr):
        return None
    logger.warning(
        "readonly: blocked %s %s from %s",
        request.method, request.path, request.remote_addr,
    )
    resp = jsonify({
        "error": "station_readonly",
        "hint": "route via signed command channel",
    })
    resp.status_code = 403
    return resp


BASE        = Path.home()
CONFIG_PATH = next(
    (p for p in [
        BASE / 'rovimen_scripts' / 'config.json',
        BASE / 'meteor_detector' / 'config.json',
    ] if p.exists()),
    BASE / 'rovimen_scripts' / 'config.json',  # default
)
CHUNK_RE    = re.compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_color\.mkv$')


def _local_to_utc(date_str, time_str):
    """Convert local date+time strings to UTC. Returns (utc_date, utc_time)."""
    import time as _time
    local_dt = datetime(
        int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]),
        int(time_str[:2]), int(time_str[2:4]), int(time_str[4:6]),
    )
    # Use time.timezone/altzone — single syscall, no DST-boundary race from two now() calls.
    is_dst = _time.daylight and _time.localtime().tm_isdst > 0
    utc_offset_s = -(_time.altzone if is_dst else _time.timezone)
    utc_dt = local_dt - timedelta(seconds=utc_offset_s)
    return utc_dt.strftime('%Y%m%d'), utc_dt.strftime('%H%M%S')

# ---------------------------------------------------------------------------
# Meteor dome video — async job registry
# ---------------------------------------------------------------------------

import io as _io
import secrets as _secrets
import uuid as _uuid

# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------

_cache      = {}
_cache_lock = threading.Lock()


_cache_inflight: dict[str, threading.Event] = {}


def cached(key: str, ttl: float, fn):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.monotonic() - entry['ts'] < ttl:
            return entry['val']
        wait_evt = _cache_inflight.get(key)

    if wait_evt is not None:
        wait_evt.wait(timeout=ttl + 30)
        with _cache_lock:
            entry = _cache.get(key)
            if entry and time.monotonic() - entry['ts'] < ttl:
                return entry['val']

    evt = threading.Event()
    with _cache_lock:
        existing = _cache_inflight.get(key)
        if existing is not None:
            wait_evt = existing
        else:
            _cache_inflight[key] = evt
            wait_evt = None

    if wait_evt is not None:
        wait_evt.wait(timeout=ttl + 30)
        with _cache_lock:
            entry = _cache.get(key)
            if entry and time.monotonic() - entry['ts'] < ttl:
                return entry['val']
        return fn()

    try:
        val = fn()
    finally:
        with _cache_lock:
            _cache_inflight.pop(key, None)
    with _cache_lock:
        _cache[key] = {'val': val, 'ts': time.monotonic()}
    evt.set()
    return val


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    # Cached for 5 s: most request handlers call load_config() one or more times
    # per request, and the dashboard polls every 60 s. Re-reading + parsing
    # config.json on every call burned syscalls + JSON-parse time for data that
    # only changes when the operator edits the file. 5 s is short enough that
    # config edits are picked up within the next poll, long enough to coalesce
    # all the load_config() calls within a single request.
    return cached('config', 5, lambda: json.loads(CONFIG_PATH.read_text()))


def _deep_merge(base: dict, update: dict) -> None:
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def capture_base() -> Path:
    cfg = load_config()
    return Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(BASE / 'color_capture')
    )


def _rms_camera_dirs() -> list[tuple[str, Path, str | None]]:
    """Discover RMS camera directories and station codes.

    Returns list of (cam_label, cam_dir, station_code) tuples.
    Uses config.json stations dict if available (works for any layout),
    falls back to glob('cam*') for legacy gmn0002/gmn0003 layout.
    """
    cfg = load_config()
    stations = cfg.get('stations', {})
    results = []
    if stations:
        for sid, sinfo in stations.items():
            rms_path = sinfo.get('rms_data_path')
            if rms_path:
                cam_dir = Path(rms_path)
                if cam_dir.exists():
                    results.append((cam_dir.name, cam_dir, sid))
    if not results:
        # Fallback: scan ~/RMS_data/ for any subdirectory containing ArchivedFiles or CapturedFiles
        rms_base = BASE / 'RMS_data'
        if rms_base.exists():
            for d in sorted(rms_base.iterdir()):
                if d.is_dir() and ((d / 'ArchivedFiles').exists() or (d / 'CapturedFiles').exists()):
                    results.append((d.name, d, None))
    return results




def _du(path: Path) -> int:
    """Return total bytes for a directory tree via du -sbL (follows symlinks)."""
    try:
        r = subprocess.run(['du', '-sbL', str(path)], capture_output=True, text=True, timeout=30)
        return int(r.stdout.split()[0]) if r.returncode == 0 else 0
    except Exception:
        return 0


def _get_device(path: Path) -> str:
    """Return the block device backing path (follows symlinks)."""
    try:
        r = subprocess.run(['df', '--output=source', str(path)],
                           capture_output=True, text=True)
        lines = r.stdout.strip().splitlines()
        return lines[1].strip() if len(lines) >= 2 else ''
    except Exception:
        return ''


def _night_date(date_str: str, time_str: str) -> str:
    """Return night date for a session timestamp.

    A session that starts before noon UTC belongs to the previous night
    (e.g. RO000W_20260412_005904_* belongs to night 20260411).
    """
    if int(time_str[:2]) < 12:
        d = (datetime(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:]),
                       tzinfo=timezone.utc)
             - timedelta(days=1))
        return d.strftime('%Y%m%d')
    return date_str


def _safe_iterdir(d: Path) -> list[Path]:
    """Like list(d.iterdir()) but returns [] on I/O errors."""
    try:
        return list(d.iterdir())
    except OSError:
        logger.warning('I/O error listing %s — skipping', d)
        return []


def _safe_is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _safe_is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def _date_range(path: Path):
    """Return (oldest, newest, count) of YYYYMMDD subdirs anywhere under path.

    Tolerates per-subdir OSError (e.g. NTFS index corruption returning EIO on
    iterdir/listdir): the bad subtree is logged and skipped instead of taking
    the whole walker down.
    """
    dates: set[str] = set()

    def _onerror(exc: OSError) -> None:
        sys.stderr.write(f'_date_range: skipping unreadable {exc.filename}: {exc}\n')

    for root, dirs, _files in os.walk(path, onerror=_onerror):
        for part in Path(root).parts:
            if len(part) == 8 and part.isdigit():
                dates.add(part)
        for d in dirs:
            if len(d) == 8 and d.isdigit():
                dates.add(d)

    sdates = sorted(dates)
    return (sdates[0] if sdates else None,
            sdates[-1] if sdates else None,
            len(sdates))


# ---------------------------------------------------------------------------
# GET /api/vitals
# ---------------------------------------------------------------------------

import time as _time
_io_prev: dict = {}  # ts, write_bytes
_io_lock = threading.Lock()
_proc_cpu_prev: dict[int, float] = {}  # pid -> cumulative cpu seconds
_proc_cpu_ts: float = 0.0
_proc_cpu_result: list = []
_proc_cpu_result_ts: float = 0.0
_proc_cpu_lock = threading.Lock()
_PROC_CACHE_TTL: float = 2.0   # recompute every 2 s, aligned with fast-poll


def _top_procs_by_cpu() -> list[dict]:
    """Return top-5 CPU consumers by name, using cumulative CPU time deltas.
    Has its own 5 s cache so it runs every fast-poll, not every 15 s vitals cache."""
    global _proc_cpu_ts, _proc_cpu_result, _proc_cpu_result_ts
    import psutil
    now = _time.monotonic()

    with _proc_cpu_lock:
        if now - _proc_cpu_result_ts < _PROC_CACHE_TTL:
            return list(_proc_cpu_result)
        prev_snapshot = dict(_proc_cpu_prev)
        prev_ts = _proc_cpu_ts

    dt = now - prev_ts if prev_ts else 0.0

    curr: dict[int, tuple[str, float]] = {}
    for p in psutil.process_iter(['pid', 'name', 'cpu_times', 'status']):
        try:
            if p.info['status'] == 'zombie':
                continue
            t = p.info['cpu_times']
            curr[p.pid] = (p.info['name'] or '?', t.user + t.system)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    by_name: dict[str, float] = {}
    if dt > 0 and prev_snapshot:
        for pid, (name, total) in curr.items():
            prev_total = prev_snapshot.get(pid)
            if prev_total is not None:
                delta_pct = (total - prev_total) / dt * 100
                if delta_pct > 0:
                    by_name[name] = by_name.get(name, 0.0) + delta_pct

    top = sorted(by_name.items(), key=lambda x: x[1], reverse=True)[:5]
    result = [{'name': name, 'cores': round(pct / 100, 1)} for name, pct in top if pct > 0]

    with _proc_cpu_lock:
        _proc_cpu_prev.clear()
        _proc_cpu_prev.update({pid: total for pid, (_, total) in curr.items()})
        _proc_cpu_ts = now
        _proc_cpu_result = result
        _proc_cpu_result_ts = now
    return result


def _disk_write_mbps() -> float:
    """Return disk write MB/s averaged since the last call."""
    try:
        import psutil
        io = psutil.disk_io_counters()
        now = _time.monotonic()
        with _io_lock:
            prev = _io_prev.copy()
            _io_prev['ts'] = now
            _io_prev['write'] = io.write_bytes
        if prev.get('ts') and (now - prev['ts']) < 120:
            dt = now - prev['ts']
            return round((io.write_bytes - prev['write']) / dt / 1e6, 2)
    except Exception:
        pass
    return 0.0


@app.route('/api/vitals')
def api_vitals():
    def collect():
        result = {'online': True, 'last_updated': datetime.now(timezone.utc).isoformat()}
        try:
            import psutil
            per_core_pct = psutil.cpu_percent(percpu=True, interval=1)
            result['cpu_pct'] = round(sum(per_core_pct) / len(per_core_pct), 1) if per_core_pct else 0
            freqs = psutil.cpu_freq(percpu=True) or []
            result['cores'] = [
                {
                    'load_pct': round(p, 1),
                    'freq_ghz': round(freqs[i].current / 1000, 2) if i < len(freqs) else None,
                }
                for i, p in enumerate(per_core_pct)
            ]
            vm = psutil.virtual_memory()
            result['ram_pct'] = round(vm.percent, 1)
            result['ram_used_mb'] = vm.used // (1024 * 1024)
            result['ram_total_mb'] = vm.total // (1024 * 1024)
            for key in ('coretemp', 'k10temp', 'cpu_thermal'):
                temps = psutil.sensors_temperatures().get(key, [])
                if temps:
                    result['temp_c'] = round(temps[0].current, 1)
                    break
            result['total_cores'] = len(per_core_pct)
        except ImportError:
            result['error'] = 'psutil not installed'
        result['disk_write_mbps'] = _disk_write_mbps()
        return result
    # 15 s TTL: collect() blocks 1 full second inside psutil.cpu_percent(interval=1).
    # The dashboard polls vitals every 60 s, so a 2 s TTL meant ~30 wasted refreshes
    # (and 30 wasted seconds of CPU sampling) between dashboard reads. 15 s still
    # gives the operator a fresh reading on every dashboard poll, while cutting
    # the sampling cost by ~7.5x.
    data = dict(cached('vitals', 15, collect))
    try:
        data['top_procs'] = _top_procs_by_cpu()
    except Exception:
        data['top_procs'] = []
    return jsonify(data)


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------

@app.route('/api/status')
def api_status():
    def collect():
        result = {'online': True, 'last_updated': datetime.now(timezone.utc).isoformat()}

        # ── Services ──────────────────────────────────────────────────────
        # Detect RMS instances: either RMS_cam{i} dirs (gmn style) or
        # StartCapture processes (Marius/Lucian style)
        cam_dirs = sorted(p for p in BASE.glob('RMS_cam*') if p.is_dir())
        rms_svcs = []
        for cam_dir in cam_dirs:
            m = re.match(r'RMS_cam(\d+)$', cam_dir.name)
            if m:
                rms_svcs.append(f'rms-cam{m.group(1)}')
        if not rms_svcs:
            # Fallback: count running StartCapture processes
            r = subprocess.run(['pgrep', '-af', 'StartCapture'],
                               capture_output=True, text=True)
            n_rms = len([l for l in r.stdout.strip().splitlines()
                         if 'StartCapture' in l and 'pgrep' not in l])
            if n_rms > 0:
                rms_svcs = ['rms-capture']
        rovimen_svcs = ['color-capture', 'rovimen-station-api', 'detection-indexer']

        # Map service names to process patterns for pgrep fallback
        proc_patterns = {
            'color-capture': 'color_capture.py',
            'rovimen-station-api': 'station_api.py',
            'detection-indexer': 'detection_indexer.py',
        }
        for svc in rms_svcs:
            m = re.match(r'rms-cam(\d+)$', svc)
            if m:
                proc_patterns[svc] = f'RMS_cam{m.group(1)}'
        proc_patterns['rms-capture'] = 'StartCapture'

        def _svc_disabled(svc: str) -> bool:
            """True if the unit file exists but is deliberately disabled."""
            r = subprocess.run(
                ['systemctl', 'is-enabled', svc],
                capture_output=True, text=True,
            )
            # "disabled" / "masked" → deliberately off. Anything else (enabled,
            # static, alias, generated, indirect, transient, or "not found")
            # means we should still report on the service.
            return r.stdout.strip() in ('disabled', 'masked')

        def _svc_active(svc: str) -> bool:
            # Try systemctl is-active first — it correctly reports 'activating'
            # (not 'inactive') during restart gaps, so brief systemd restarts
            # don't flip the status.  Falls back to pgrep for nohup-style
            # stations that don't have systemd unit files.
            r = subprocess.run(
                ['systemctl', 'is-active', '--quiet', svc],
                capture_output=True,
            )
            if r.returncode == 0:
                return True
            # For rms-* services, always fall through to pgrep: the actual
            # systemd unit may have a different name (gmn-capture vs
            # rms-capture) and some systemd versions return exit 3 instead
            # of 4 for unknown units, blocking the fallback.
            if not svc.startswith('rms-') and r.returncode != 4:
                return False
            pattern = proc_patterns.get(svc, svc)
            r2 = subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True)
            return r2.returncode == 0 and bool(r2.stdout.strip())

        # Each service requires up to 3 serial subprocess.run() calls
        # (is-enabled + is-active + optional pgrep). For 6 services that's
        # ~12-18 forks in series, dominating the /api/status response. Fan
        # them out across a small thread pool — the work is pure subprocess
        # IO so threads parallelize cleanly without contending on the GIL.
        all_svcs = rms_svcs + rovimen_svcs

        def _probe(svc: str) -> tuple[str, str | None]:
            if _svc_disabled(svc):
                return svc, None
            return svc, 'active' if _svc_active(svc) else 'inactive'

        svc_status: dict[str, str] = {}
        if all_svcs:
            with ThreadPoolExecutor(max_workers=6) as ex:
                for svc, status in ex.map(_probe, all_svcs):
                    if status is not None:
                        svc_status[svc] = status
        result['services'] = svc_status

        # ── Storage ───────────────────────────────────────────────────────
        cfg = load_config()
        folders = {
            'color_capture': capture_base(),
        }
        storage     = {}
        for name, path in folders.items():
            if not path.exists():
                storage[name] = None
                continue
            total = _du(path)
            oldest, newest, days = _date_range(path)
            storage[name] = {'bytes': total, 'oldest': oldest,
                             'newest': newest, 'days': days,
                             'device': _get_device(path)}

        # RMS CapturedFiles + ArchivedFiles
        rms_total = 0
        rms_dates = []
        for _label, cam_dir, _sid in _rms_camera_dirs():
            for sub in ('CapturedFiles', 'ArchivedFiles'):
                sub_dir = cam_dir / sub
                if not sub_dir.exists():
                    continue
                rms_total += _du(sub_dir)
                rms_dates += [p.name for p in sub_dir.iterdir()
                              if p.is_dir() and len(p.name) >= 8 and p.name[:8].isdigit()]
        rms_dates = sorted(set(rms_dates))
        main_device = _get_device(BASE)
        storage['rms'] = {'bytes': rms_total,
                          'oldest': rms_dates[0]  if rms_dates else None,
                          'newest': rms_dates[-1] if rms_dates else None,
                          'days':   len(rms_dates),
                          'device': main_device}
        result['storage'] = storage

        # ── Disk ──────────────────────────────────────────────────────────
        r = subprocess.run(['df', '-BM', str(BASE)], capture_output=True, text=True)
        lines = r.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            used_mb  = int(parts[2].rstrip('M'))
            total_mb = int(parts[1].rstrip('M'))
            # only count folders on the same device toward other_mb
            main_known_mb = sum(
                v['bytes'] // (1024 * 1024)
                for v in storage.values()
                if v and v.get('device') == main_device
            )
            result['disk'] = {
                'used_mb':  used_mb,
                'total_mb': total_mb,
                'pct':      int(parts[4].rstrip('%')),
                'other_mb': max(0, used_mb - main_known_mb),
                'device':   main_device,
            }

        # ── Extra disks (any mounted block device that isn't root/boot/virtual) ──
        skip_sources = {'tmpfs', 'efivarfs', 'devtmpfs', 'udev', 'overlay', 'none'}
        skip_mounts  = {'/', '/boot', '/boot/efi', '/sys/firmware/efi/efivars',
                        '/mnt/ramdisk'}
        extra_disks  = []
        r2 = subprocess.run(['df', '-BM', '--output=source,size,used,avail,pcent,target'],
                            capture_output=True, text=True)
        for line in r2.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) < 6:
                continue
            src, size, used_s, avail_s, pct_s, mount = parts
            if (src in skip_sources or src.startswith('/dev/loop')
                    or mount in skip_mounts or mount.startswith('/boot')
                    or mount.startswith('/sys') or mount.startswith('/proc')
                    or mount.startswith('/dev/') or mount == '/'):
                continue
            try:
                extra_disks.append({
                    'source':   src,
                    'mount':    mount,
                    'total_mb': int(size.rstrip('M')),
                    'used_mb':  int(used_s.rstrip('M')),
                    'avail_mb': int(avail_s.rstrip('M')),
                    'pct':      int(pct_s.rstrip('%')),
                })
            except ValueError:
                pass
        result['extra_disks'] = extra_disks

        # ── RMS detections ────────────────────────────────────────────────
        # Aggregate ALL FTPdetectinfo files from sessions belonging to the
        # most recent observation night, instead of reading just the newest
        # file. RMS often splits a night into multiple sessions (e.g. an
        # evening session and a post-midnight session — the latter is filed
        # under the next calendar date but belongs to the same night). The
        # old "latest file only" logic missed those sessions, so the dashboard
        # showed wrong/partial counts. Same fix as detection_lock.process_night.
        _arc_re = re.compile(r'^[A-Z0-9]+_(\d{8})_(\d{6})_')
        rms_result = {}
        for cam_label, cam_dir, station_code in _rms_camera_dirs():
            night_files: dict[str, list[Path]] = {}
            for sub in ('ArchivedFiles', 'CapturedFiles'):
                sub_dir = cam_dir / sub
                if not sub_dir.exists():
                    continue
                try:
                    sessions = [p for p in sub_dir.iterdir() if p.is_dir()]
                except OSError:
                    continue
                for session in sessions:
                    m = _arc_re.match(session.name)
                    if not m:
                        continue
                    night = _night_date(m.group(1), m.group(2))
                    try:
                        ftps = [
                            f for f in session.glob('FTPdetectinfo_*.txt')
                            if '_unfiltered' not in f.name and '_backup' not in f.name
                        ]
                    except OSError:
                        continue
                    if ftps:
                        night_files.setdefault(night, []).extend(ftps)

            if not night_files:
                rms_result[cam_label] = {'station_code': station_code,
                                         'total_count': 0, 'last_entries': []}
                continue

            latest_night = max(night_files.keys())
            files_for_night = night_files[latest_night]

            total_count = 0
            entries: list[str] = []
            for ftpdet in files_for_night:
                try:
                    for line in ftpdet.read_text(errors='ignore').splitlines():
                        m = re.match(r'FF_[A-Z0-9]+_(\d{8})_(\d{6})', line.strip())
                        if m:
                            total_count += 1
                            d, t = m.group(1), m.group(2)
                            entries.append(
                                f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]} UTC")
                except Exception:
                    pass
            entries.sort()
            rms_result[cam_label] = {
                'station_code':  station_code,
                'total_count':   total_count,
                'last_entries':  entries[-10:],
                'ftp_file':      files_for_night[0].name,
                'night':         latest_night,
                'session_count': len(files_for_night),
            }
        result['rms'] = rms_result
        return result

    return jsonify(cached('status', 60, collect))


# ---------------------------------------------------------------------------
# GET /api/nights/<cam>
# ---------------------------------------------------------------------------

@app.route('/api/nights/<cam>')
def api_nights(cam):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    cap = capture_base() / cam
    if not cap.exists():
        return jsonify([])

    def _has_mkvs(d: Path) -> bool:
        try:
            return any(f.suffix == '.mkv' for f in d.iterdir() if f.is_file())
        except OSError:
            return False

    nights = sorted(
        [d.name for d in _safe_iterdir(cap)
         if _safe_is_dir(d) and re.match(r'^\d{8}$', d.name)
         and _has_mkvs(d)],
        reverse=True,
    )
    return jsonify(nights)


# ---------------------------------------------------------------------------
# GET /api/rmsnights/<cam>
# ---------------------------------------------------------------------------

@app.route('/api/rmsnights/<cam>')
def api_rmsnights(cam):
    """Return list of nights (YYYYMMDD) for which RMS data exists for this camera."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    cam_dir = None
    for _label, dir_path, station_code in _rms_camera_dirs():
        if station_code == cam:
            cam_dir = dir_path
            break
    if not cam_dir or not cam_dir.exists():
        return jsonify([])
    dates: set[str] = set()
    for sub in ('ArchivedFiles', 'CapturedFiles'):
        sub_dir = cam_dir / sub
        if not sub_dir.exists():
            continue
        for d in sub_dir.iterdir():
            if not d.is_dir():
                continue
            m = re.match(r'^[A-Z0-9]+_(\d{8})_', d.name)
            if m:
                dates.add(m.group(1))
    return jsonify(sorted(dates, reverse=True))


# ---------------------------------------------------------------------------
# GET /api/chunks/<cam>/<date>
# ---------------------------------------------------------------------------

@app.route('/api/chunks/<cam>/<date>')
def api_chunks(cam, date):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)

    locked_only = request.args.get('locked_only') == '1'
    lock_type_filter = request.args.get('lock_type', '')
    from_s = request.args.get('from', '').replace(':', '')
    to_s   = request.args.get('to',   '').replace(':', '')

    cap_dir = capture_base() / cam / date
    if not cap_dir.exists():
        return jsonify([])

    cfg = load_config()

    # Load state.json for this night if available (primary source).
    # Falls back to .locked sidecars (legacy) when state.json is absent.
    night_state: dict = {}
    night_chunks_state: dict = {}
    if _STATE_MANAGER_OK:
        try:
            night_state = _flags_manager.load(cam, date, cfg)
            night_chunks_state = night_state.get('chunks', {})
        except Exception:
            pass

    def _get_lock_info_for(mkv: Path) -> tuple[str | None, str | None, str | None]:
        """Return (lock_type, detection_time, meteor_time) for a chunk.

        Reads from state.json if available, falls back to .locked sidecar.
        """
        cstate = night_chunks_state.get(mkv.name, {})
        lock = cstate.get('lock')
        if lock is not None:
            return lock.get('lock_type'), lock.get('detection_time'), lock.get('meteor_time')
        # Backward compat: .locked sidecar
        li = rovimen_lock.get_lock_info(mkv)
        if li is not None:
            return li.get('lock_type'), li.get('detection_time'), li.get('meteor_time')
        return None, None, None

    def _is_reencoded(mkv: Path) -> bool:
        cstate = night_chunks_state.get(mkv.name, {})
        if cstate.get('reencoded', False):
            return True
        return Path(str(mkv) + '.reencoded').exists()

    def _is_ready(mkv: Path) -> bool:
        cstate = night_chunks_state.get(mkv.name, {})
        if cstate.get('ready', False):
            return True
        # Legacy: if state.json absent, treat all MKVs in the dir as ready
        return not night_chunks_state  # no state.json → show everything

    chunks = []
    # Search both flat layout (legacy) and videos/ subdirectory (new coppermind layout)
    mkv_files = list(cap_dir.glob('*_color.mkv'))
    videos_dir = cap_dir / 'videos'
    if videos_dir.exists():
        mkv_files.extend(videos_dir.glob('*_color.mkv'))
    for mkv in sorted(mkv_files):
        m = CHUNK_RE.match(mkv.name)
        if not m:
            continue
        time_part = m.group(3)
        hhmm      = time_part[:4]

        if from_s and to_s and from_s > to_s:
            if hhmm < from_s and hhmm > to_s:
                continue
        else:
            if from_s and hhmm < from_s:
                continue
            if to_s and hhmm > to_s:
                continue

        # Only return chunks that are ready (state.json primary; all chunks if legacy)
        if night_chunks_state and not _is_ready(mkv):
            continue

        lock_stem = mkv.stem[:-6]  # strip _color suffix — used for stack filename below
        lock_type, detection_time_str, mt_str = _get_lock_info_for(mkv)
        locked = lock_type is not None
        if locked_only and not locked:
            continue
        if lock_type_filter and lock_type != lock_type_filter:
            continue

        # Check stacks/ subdirectory (primary), fall back to flat layout (legacy)
        stack_file = cap_dir / 'stacks' / f'{lock_stem}_stack.webp'
        if not stack_file.exists():
            stack_file = cap_dir / f'{lock_stem}_stack.webp'
        stack_name = stack_file.name
        try:
            size_mb = round(mkv.stat().st_size / (1024 * 1024), 1)
        except OSError:
            size_mb = 0

        utc_date, utc_time = _local_to_utc(m.group(2), time_part)
        det_offset_s = None
        if lock_type == 'detection':
            try:
                chk_s = int(time_part[:2]) * 3600 + int(time_part[2:4]) * 60 + int(time_part[4:6])
                rtsp_delay = cfg.get('rtsp_capture_delay_s', 0)
                if mt_str:
                    mt = datetime.fromisoformat(mt_str)
                    det_s = mt.hour * 3600 + mt.minute * 60 + mt.second + mt.microsecond / 1e6
                    diff = det_s - chk_s
                    if diff < -43200:
                        diff += 86400
                    elif diff < 0:
                        diff = 0.0
                    det_offset_s = round(diff, 2)
                elif detection_time_str:
                    ts = detection_time_str.split('_', 1)[-1]
                    if len(ts) == 6:
                        det_s = int(ts[:2]) * 3600 + int(ts[2:4]) * 60 + int(ts[4:6])
                        diff = det_s - chk_s
                        if diff < -43200:
                            diff += 86400
                        elif diff < 0:
                            diff = 0.0
                        det_offset_s = round(diff, 1)
            except Exception:
                pass

        chunks.append({
            'filename':  mkv.name,
            'time':      f'{utc_time[:2]}:{utc_time[2:4]}:{utc_time[4:6]}',
            'stack':     stack_name,
            'size_mb':   size_mb,
            'locked':    locked,
            'lock_type': lock_type,
            'detection_offset_s': det_offset_s,
            'meteor_time': mt_str,
            'reencoded': _is_reencoded(mkv),
        })
    morning_done = bool(night_state.get('morning_done', False))
    return jsonify({"morning_done": morning_done, "chunks": chunks})


# ---------------------------------------------------------------------------
# POST /api/lock/<cam>/<date>/<filename>   {"locked": true|false}
# ---------------------------------------------------------------------------

@app.route('/api/lock/<cam>/<date>/<filename>', methods=['POST'])
def api_lock(cam, date, filename):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    m = CHUNK_RE.match(filename)
    if not m:
        abort(400)
    body = request.get_json(silent=True) or {}
    lock_it = bool(body.get('locked', True))
    cap_dir = capture_base() / cam / date
    mkv = cap_dir / filename
    if not mkv.exists():
        abort(404)
    try:
        cfg = load_config()
        if lock_it:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if _STATE_MANAGER_OK:
                lock_info = {
                    'lock_type': 'manual',
                    'detection_time': now.strftime('%Y%m%d_%H%M%S'),
                    'meteor_time': None,
                }
                _flags_manager.lock_chunk(cam, date, filename, lock_info, cfg)
            threading.Thread(
                target=archive_upload.upload_chunk,
                args=(cam, date, mkv, cfg),
                daemon=True,
            ).start()
        else:
            rovimen_lock.unlock(mkv)  # remove legacy sidecar if present
            if _STATE_MANAGER_OK:
                _flags_manager.unlock_chunk(cam, date, filename, cfg)
            threading.Thread(
                target=archive_upload.delete_chunk,
                args=(cam, date, filename, cfg),
                daemon=True,
            ).start()
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify({'locked': lock_it, 'lock_type': 'manual' if lock_it else None})


# ---------------------------------------------------------------------------
# GET /api/shortclip/<cam>/<date>/<filename>?pre=2&post=5
# ---------------------------------------------------------------------------

@app.route('/api/shortclip/<cam>/<date>/<filename>')
def api_shortclip(cam, date, filename):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    m = CHUNK_RE.match(filename)
    if not m:
        abort(400)
    cfg_now = load_config()
    cap_dir = capture_base() / cam / date
    mkv     = cap_dir / filename
    if not mkv.exists():
        abort(404)

    if 'ss' in request.args and 't' in request.args:
        # Explicit seek offset + duration supplied by the trim UI
        offset   = max(0.0, float(request.args['ss']))
        duration = max(0.5, float(request.args['t']))
    else:
        pre      = max(0.0, float(request.args.get('pre',  cfg_now.get('pre_detection_seconds',  2))))
        post     = max(0.0, float(request.args.get('post', cfg_now.get('post_detection_seconds', 5))))
        duration = pre + post
        # Compute seek offset from detection time (state.json first, sidecar fallback)
        offset = max(0.0, 10.0 - pre)  # fallback: centre of 20 s chunk
        try:
            _lock_info = None
            if _STATE_MANAGER_OK:
                try:
                    night_state = _flags_manager.load(cam, date, cfg_now)
                    _lock_info = night_state.get('chunks', {}).get(filename, {}).get('lock')
                except Exception:
                    pass
            if _lock_info is None:
                _lock_info = rovimen_lock.get_lock_info(mkv)
            chk_str  = m.group(3)
            chk_secs = int(chk_str[:2]) * 3600 + int(chk_str[2:4]) * 60 + int(chk_str[4:6])
            rtsp_delay = cfg_now.get('rtsp_capture_delay_s', 0)
            mt_str = _lock_info.get('meteor_time') if _lock_info else None
            if mt_str:
                mt = datetime.fromisoformat(mt_str)
                det_secs = mt.hour * 3600 + mt.minute * 60 + mt.second + mt.microsecond / 1e6
                diff = det_secs - chk_secs
                if diff < -43200:
                    diff += 86400
                elif diff < 0:
                    diff = 0.0
                offset = max(0.0, diff - pre)
            else:
                det_time = _lock_info.get('detection_time') if _lock_info else None
                if det_time:
                    time_str = det_time.split('_', 1)[-1]
                    if len(time_str) == 6:
                        det_secs = int(time_str[:2]) * 3600 + int(time_str[2:4]) * 60 + int(time_str[4:6])
                        diff = det_secs - chk_secs
                        if diff < -43200:
                            diff += 86400
                        elif diff < 0:
                            diff = 0.0
                        offset = max(0.0, diff - pre)
        except (ValueError, AttributeError):
            pass

    import tempfile, os
    fd, tmp_path = tempfile.mkstemp(suffix='.mp4')
    os.close(fd)
    try:
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-ss', f'{offset:.3f}', '-i', str(mkv),
            '-t', f'{duration:.3f}',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
            '-an', '-movflags', '+faststart',
            '-y', tmp_path,
        ]
        result = subprocess.run(cmd, stderr=subprocess.DEVNULL, timeout=120)
        if result.returncode != 0:
            abort(500)
        clip_stem = mkv.stem.replace('_color', '')
        return send_file(tmp_path, mimetype='video/mp4', as_attachment=True,
                         download_name=f'{clip_stem}_clip.mp4')
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GET /api/stitch_video/<cam>/<date>?f1=<filename1>&f2=<filename2>
# GET /api/shortclip_stitch/<cam>/<date>?f1=...&f2=...&ss=...&t=...
# ---------------------------------------------------------------------------

def _stitch_concat_list(cap_dir: Path, f1: str, f2: str) -> tuple[Path, Path, Path] | None:
    """Validate f1/f2, return (mkv1, mkv2, cap_dir). Aborts on bad input."""
    for fn in (f1, f2):
        if not re.match(r'^[\w.\-]+\.mkv$', fn):
            abort(400)
    mkv1 = cap_dir / f1
    mkv2 = cap_dir / f2
    if not mkv1.exists() or not mkv2.exists():
        abort(404)
    return mkv1, mkv2


@app.route('/api/stitch_video/<cam>/<date>')
def api_stitch_video(cam, date):
    """Concatenate two MKV chunks (copy codec) and serve as seekable MP4."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    f1 = request.args.get('f1', '')
    f2 = request.args.get('f2', '')
    cap_dir = capture_base() / cam / date
    mkv1, mkv2 = _stitch_concat_list(cap_dir, f1, f2)

    import tempfile, os as _os
    fd_list, list_path = tempfile.mkstemp(suffix='.txt')
    fd_out,  out_path  = tempfile.mkstemp(suffix='.mp4')
    try:
        with _os.fdopen(fd_list, 'w') as fh:
            fh.write(f"file '{mkv1}'\nfile '{mkv2}'\n")
        _os.close(fd_out)
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-f', 'concat', '-safe', '0', '-i', list_path,
            '-c', 'copy', '-movflags', '+faststart',
            '-y', out_path,
        ]
        result = subprocess.run(cmd, stderr=subprocess.DEVNULL, timeout=120)
        if result.returncode != 0:
            abort(500)
        return send_file(out_path, mimetype='video/mp4', conditional=True)
    finally:
        try:
            _os.unlink(list_path)
        except OSError:
            pass
        # out_path cleaned up by OS after Flask is done (send_file keeps fd open)


@app.route('/api/shortclip_stitch/<cam>/<date>')
def api_shortclip_stitch(cam, date):
    """Concatenate two chunks, then trim and re-encode as a download clip."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    f1 = request.args.get('f1', '')
    f2 = request.args.get('f2', '')
    try:
        offset   = max(0.0, float(request.args.get('ss', 0)))
        duration = max(0.5, float(request.args.get('t', 10)))
    except ValueError:
        abort(400)
    cap_dir = capture_base() / cam / date
    mkv1, mkv2 = _stitch_concat_list(cap_dir, f1, f2)

    import tempfile, os as _os
    fd_list, list_path = tempfile.mkstemp(suffix='.txt')
    fd_out,  out_path  = tempfile.mkstemp(suffix='.mp4')
    try:
        with _os.fdopen(fd_list, 'w') as fh:
            fh.write(f"file '{mkv1}'\nfile '{mkv2}'\n")
        _os.close(fd_out)
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-f', 'concat', '-safe', '0', '-i', list_path,
            '-ss', f'{offset:.3f}', '-t', f'{duration:.3f}',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
            '-an', '-movflags', '+faststart',
            '-y', out_path,
        ]
        result = subprocess.run(cmd, stderr=subprocess.DEVNULL, timeout=120)
        if result.returncode != 0:
            abort(500)
        stem = Path(f1).stem.replace('_color', '')
        return send_file(out_path, mimetype='video/mp4', as_attachment=True,
                         download_name=f'{stem}_stitch_clip.mp4')
    finally:
        try:
            _os.unlink(list_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GET /api/timelapses
# ---------------------------------------------------------------------------

@app.route('/api/timelapses')
def api_timelapses():
    cap = capture_base()
    result = {}
    if not cap.exists():
        return jsonify(result)
    for cam_dir in sorted(cap.iterdir()):
        if not cam_dir.is_dir():
            continue
        entries = []
        for date_dir in sorted(cam_dir.iterdir(), reverse=True):
            if not date_dir.is_dir() or not re.match(r'^\d{8}$', date_dir.name):
                continue
            for mp4 in sorted(date_dir.glob('*_timelapse.mp4'), reverse=True):
                stack = date_dir / mp4.name.replace('_timelapse.mp4', '_night_stack.webp')
                entries.append({
                    'date': date_dir.name,
                    'filename': mp4.name,
                    'night_stack': stack.name if stack.exists() else None,
                })
        if entries:
            result[cam_dir.name] = entries
    return jsonify(result)


# ---------------------------------------------------------------------------
# GET /api/settings
# ---------------------------------------------------------------------------

@app.route('/api/settings')
def api_settings():
    try:
        return jsonify(load_config())
    except Exception as e:
        return jsonify({'__error': str(e)}), 500


@app.route('/api/hardware')
def api_hardware():
    """Return the hardware profile written by hardware_assessment.sh."""
    hw_path = CONFIG_PATH.parent / 'hardware.json'
    if not hw_path.exists():
        return jsonify({'__missing': True,
                        'hint': 'Run hardware_assessment.sh to generate hardware profile'}), 404
    try:
        return jsonify(json.loads(hw_path.read_text()))
    except Exception as e:
        return jsonify({'__error': str(e)}), 500


@app.route('/api/reboot', methods=['POST'])
def api_reboot():
    """Schedule an immediate reboot.

    Note: unauthenticated — intentional. Station API is only reachable over
    Tailscale, which provides network-level access control. All tailnet peers
    are trusted operators.
    """
    import subprocess
    try:
        subprocess.Popen(['sudo', 'reboot'])
        return jsonify({'status': 'rebooting'})
    except Exception as e:
        return jsonify({'__error': str(e)}), 500


@app.route('/api/probe', methods=['POST'])
def api_probe():
    """Run hardware_assessment.sh and return the updated hardware profile."""
    import subprocess
    probe_path = CONFIG_PATH.parent / 'hardware_assessment.sh'
    if not probe_path.exists():
        return jsonify({'__error': 'hardware_assessment.sh not found in rovimen_scripts/'}), 404
    try:
        subprocess.run(
            ['bash', str(probe_path)],
            cwd=str(CONFIG_PATH.parent),
            timeout=30,
            check=True,
        )
        hw_path = CONFIG_PATH.parent / 'hardware.json'
        return jsonify(json.loads(hw_path.read_text()))
    except subprocess.TimeoutExpired:
        return jsonify({'__error': 'Probe timed out after 30s'}), 504
    except subprocess.CalledProcessError as e:
        return jsonify({'__error': f'Probe failed: {e}'}), 500
    except Exception as e:
        return jsonify({'__error': str(e)}), 500


@app.route('/api/detection-stats')
def api_detection_stats():
    """Return detection counts per camera per night for the last N nights.

    Response:
      {
        "cameras": ["RO000N", "RO000Q", ...],
        "nights":  ["20260318", "20260317", ...],   # newest first
        "counts":  { "RO000N": { "20260318": 3, "20260317": 1, ... }, ... }
      }
    """
    days = int(request.args.get('days', 365))
    cfg  = load_config()
    cap  = capture_base()
    cameras = list(cfg.get('stations', {}).keys())

    # Collect all night dirs across all cameras to build a unified sorted list
    all_nights: set[str] = set()
    for cam in cameras:
        cam_dir = cap / cam
        if not cam_dir.exists():
            continue
        for d in cam_dir.iterdir():
            if d.is_dir() and re.match(r'^\d{8}$', d.name):
                all_nights.add(d.name)

    nights_sorted = sorted(all_nights, reverse=True)[:days]

    counts: dict[str, dict[str, int]] = {}
    for cam in cameras:
        cam_counts: dict[str, int] = {}
        for night in nights_sorted:
            night_dir = cap / cam / night
            if not night_dir.exists():
                cam_counts[night] = 0
                continue
            n = 0
            # Primary: read from state.json if available
            counted_from_state = False
            if _STATE_MANAGER_OK:
                try:
                    nstate = _flags_manager.load(cam, night, cfg)
                    if nstate.get('chunks'):  # state.json exists and has chunks
                        for cname, cdata in nstate['chunks'].items():
                            lock = cdata.get('lock')
                            if lock and lock.get('lock_type') == 'detection':
                                n += 1
                        counted_from_state = True
                except Exception:
                    pass
            # Fallback: .locked sidecars for nights without state.json
            if not counted_from_state:
                for lf in night_dir.glob('*.locked'):
                    try:
                        raw = lf.read_text().strip()
                        if not raw:
                            continue
                        try:
                            info = json.loads(raw)
                            if info.get('lock_type') == 'detection':
                                n += 1
                        except json.JSONDecodeError:
                            if raw.startswith('detection:'):
                                n += 1
                    except Exception:
                        pass
            cam_counts[night] = n
        counts[cam] = cam_counts

    return jsonify({'cameras': cameras, 'nights': nights_sorted, 'counts': counts})


# ---------------------------------------------------------------------------
# GET /api/rms-detections/<cam>/<date>
# ---------------------------------------------------------------------------

def _rms_sessions_for_night(cam: str, date: str, cfg: dict) -> list[Path]:
    """Return all RMS session dirs for a given camera and observation night.

    A single observation night may span multiple sessions (pre-midnight +
    post-midnight). Uses _night_date() to correctly group them.
    """
    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if not rms_path:
        return []
    rms_dir = Path(rms_path)
    arc_re = re.compile(r'^[A-Z0-9]+_(\d{8})_(\d{6})_')
    sessions: list[Path] = []
    for sub in ('ArchivedFiles', 'CapturedFiles'):
        sub_dir = rms_dir / sub
        if not sub_dir.exists():
            continue
        try:
            for d in sub_dir.iterdir():
                if not d.is_dir():
                    continue
                m = arc_re.match(d.name)
                if m and _night_date(m.group(1), m.group(2)) == date:
                    sessions.append(d)
        except OSError:
            continue
    return sessions


_FF_RE = re.compile(r"^FF_([A-Z0-9]+)_(\d{8})_(\d{6})_(\d{3})_")
# Maximum begin-time difference (seconds) for a radiants row to match an FTP
# row.  Both derive from the same meteor so they agree to ~1 ms; radiants
# time_utc is truncated to whole seconds, hence the small but non-zero window.
_JOIN_TOLERANCE_S = 2.0


def _ff_block_start(ff_file: str) -> datetime | None:
    """UTC datetime of the FF block start encoded in an FF filename."""
    m = _FF_RE.match(ff_file)
    if not m:
        return None
    date_s, time_s, ms_s = m.group(2), m.group(3), m.group(4)
    try:
        return datetime(
            int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8]),
            int(time_s[:2]), int(time_s[2:4]), int(time_s[4:6]),
            int(ms_s) * 1000,
        )
    except ValueError:
        return None


def _parse_radiants_txt(path: Path) -> list[dict]:
    """Parse an RMS *_radiants.txt file into a list of meteor dicts.

    Each dict carries a ``_begin_dt`` datetime key used for time-based joining
    to FTPdetectinfo entries in ``api_rms_detections``.
    """
    results = []
    try:
        text = path.read_text(errors='ignore')
    except OSError:
        return results

    # Extract shower counts from header
    shower_counts: dict[str, int] = {}
    in_counts = False
    for line in text.splitlines():
        if '# Code, Count' in line:
            in_counts = True
            continue
        if in_counts:
            if not line.startswith('#'):
                break
            m = re.match(r'#\s+(\S+),\s+(\d+)', line)
            if m:
                shower_counts[m.group(1)] = int(m.group(2))

    # Parse meteor data lines (start with a digit, not '#')
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # Format: datetime(24), jd(20), lasun(10), shower(6), ra_beg(6), dec_beg(7),
        #         ra_end(6), dec_end(7), ra_rad(6), dec_rad(7), theta0, phi0,
        #         beg_phase, end_phase, AppMag(6), AbsMag(6), RadElev(7)
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 17:
            continue
        try:
            dt_str = parts[0].strip()  # "20260412 19:39:06.694319"
            try:
                begin_dt = datetime.strptime(dt_str, '%Y%m%d %H:%M:%S.%f')
            except ValueError:
                begin_dt = datetime.strptime(dt_str.split('.')[0], '%Y%m%d %H:%M:%S')
            jd = float(parts[1])
            solar_lon = float(parts[2])
            shower = parts[3].strip()
            if shower == '...':
                shower = 'SPO'
            ra_beg = float(parts[4])
            dec_beg = float(parts[5])
            ra_end = float(parts[6])
            dec_end = float(parts[7])

            def _parse_opt(s: str) -> float | None:
                s = s.strip()
                return None if s == 'None' else float(s)

            ra_rad = _parse_opt(parts[8])
            dec_rad = _parse_opt(parts[9])
            app_mag = _parse_opt(parts[14])
            abs_mag = _parse_opt(parts[15])
            rad_elev = _parse_opt(parts[16])

            results.append({
                '_begin_dt': begin_dt,
                'time_utc': begin_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                'jd': jd,
                'solar_lon': solar_lon,
                'shower': shower,
                'ra_beg': ra_beg,
                'dec_beg': dec_beg,
                'ra_end': ra_end,
                'dec_end': dec_end,
                'ra_radiant': ra_rad,
                'dec_radiant': dec_rad,
                'mag_apparent': app_mag,
                'mag_absolute': abs_mag,
                'radiant_elev': rad_elev,
            })
        except (ValueError, IndexError):
            continue
    return results


def _parse_ftpdetectinfo_full(path: Path) -> list[dict]:
    """Parse FTPdetectinfo for FF filenames, duration, and per-frame magnitudes."""
    results = []
    try:
        lines = path.read_text(errors='ignore').splitlines()
    except OSError:
        return results

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('FF_') and line.endswith('.fits'):
            ff_file = line
            i += 1
            if i < len(lines) and 'Recalibrated' in lines[i]:
                i += 1
            if i >= len(lines):
                break
            header = lines[i].strip().split()
            if len(header) < 10:
                i += 1
                continue
            station = header[0]
            num_segments = int(header[2])
            fps = float(header[3])
            i += 1

            frames: list[dict] = []
            while i < len(lines):
                data_line = lines[i].strip()
                if not data_line or data_line.startswith('-') or data_line.startswith('FF_'):
                    break
                fparts = data_line.split()
                if len(fparts) >= 12:
                    try:
                        frames.append({
                            'frame': float(fparts[0]),
                            'ra': float(fparts[3]),
                            'dec': float(fparts[4]),
                            'azim': float(fparts[5]),
                            'elev': float(fparts[6]),
                            'mag': float(fparts[8]),
                        })
                    except ValueError:
                        break
                else:
                    break
                i += 1

            if frames:
                duration_s = (frames[-1]['frame'] - frames[0]['frame']) / fps if fps > 0 else 0
                # Angular velocity: total angular distance / duration
                ang_vel = None
                if duration_s > 0 and len(frames) >= 2:
                    ra0, dec0 = math.radians(frames[0]['ra']), math.radians(frames[0]['dec'])
                    ra1, dec1 = math.radians(frames[-1]['ra']), math.radians(frames[-1]['dec'])
                    # Haversine
                    dlat = dec1 - dec0
                    dlon = ra1 - ra0
                    a = (math.sin(dlat / 2) ** 2
                         + math.cos(dec0) * math.cos(dec1) * math.sin(dlon / 2) ** 2)
                    ang_dist = math.degrees(2 * math.asin(min(1.0, math.sqrt(a))))
                    ang_vel = round(ang_dist / duration_s, 2)

                begin_dt = _ff_block_start(ff_file)
                if begin_dt is not None and fps > 0:
                    begin_dt = begin_dt + timedelta(seconds=frames[0]['frame'] / fps)
                results.append({
                    '_begin_dt': begin_dt,
                    'ff_file': ff_file,
                    'station': station,
                    'num_segments': num_segments,
                    'fps': fps,
                    'duration_s': round(duration_s, 3),
                    'angular_velocity': ang_vel,
                    'peak_mag': min(f['mag'] for f in frames),
                    'azim_beg': frames[0]['azim'],
                    'elev_beg': frames[0]['elev'],
                    'azim_end': frames[-1]['azim'],
                    'elev_end': frames[-1]['elev'],
                })
            continue
        i += 1
    return results


@app.route('/api/rms-detections/<cam>/<date>')
def api_rms_detections(cam: str, date: str):
    """Return enriched meteor detection data for a camera and observation night.

    Merges data from _radiants.txt (shower, radiant, abs mag) and
    FTPdetectinfo (FF file, duration, angular velocity).
    Cached for 120s.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)

    def collect():
        cfg = load_config()
        sessions = _rms_sessions_for_night(cam, date, cfg)
        if not sessions:
            return {'camera': cam, 'date': date, 'detections': []}

        # Collect radiants.txt and FTPdetectinfo from all sessions
        all_radiants: list[dict] = []
        all_ftp: list[dict] = []
        for session in sessions:
            for f in session.glob('*_radiants.txt'):
                all_radiants.extend(_parse_radiants_txt(f))
            for f in session.glob('FTPdetectinfo_*.txt'):
                if '_unfiltered' not in f.name and '_backup' not in f.name:
                    all_ftp.extend(_parse_ftpdetectinfo_full(f))

        # Join FTP entries to radiants by nearest begin-time within tolerance.
        # Index-alignment is wrong when the two lists differ in length (e.g. an
        # uncalibrated meteor in FTPdetectinfo is filtered from radiants) — it
        # would attach the wrong shower/radiant/magnitude to every subsequent row.
        used = [False] * len(all_radiants)
        detections: list[dict] = []

        for ftp in all_ftp:
            rad: dict | None = None
            if ftp.get('_begin_dt') is not None:
                best_idx, best_dt = -1, _JOIN_TOLERANCE_S + 1.0
                for idx, r in enumerate(all_radiants):
                    if used[idx] or r.get('_begin_dt') is None:
                        continue
                    diff = abs((ftp['_begin_dt'] - r['_begin_dt']).total_seconds())
                    if diff < best_dt:
                        best_dt, best_idx = diff, idx
                if best_idx >= 0 and best_dt <= _JOIN_TOLERANCE_S:
                    used[best_idx] = True
                    rad = all_radiants[best_idx]

            if rad is not None:
                bdt = rad['_begin_dt']
                time_utc = rad.get('time_utc')
                mag_apparent = rad.get('mag_apparent')
            else:
                bdt = ftp.get('_begin_dt')
                time_utc = bdt.strftime('%Y-%m-%dT%H:%M:%S') if bdt is not None else None
                mag_apparent = ftp.get('peak_mag')
            rad = rad or {}
            detections.append({
                'ff_file': ftp['ff_file'],
                'time_utc': time_utc,
                'jd': rad.get('jd'),
                'solar_lon': rad.get('solar_lon'),
                'shower': rad.get('shower'),
                'mag_apparent': mag_apparent,
                'mag_absolute': rad.get('mag_absolute'),
                'duration_s': ftp.get('duration_s'),
                'angular_velocity': ftp.get('angular_velocity'),
                'num_segments': ftp.get('num_segments'),
                'fps': ftp.get('fps'),
                'ra_beg': rad.get('ra_beg'),
                'dec_beg': rad.get('dec_beg'),
                'ra_end': rad.get('ra_end'),
                'dec_end': rad.get('dec_end'),
                'ra_radiant': rad.get('ra_radiant'),
                'dec_radiant': rad.get('dec_radiant'),
                'radiant_elev': rad.get('radiant_elev'),
                'azim_beg': ftp.get('azim_beg'),
                'elev_beg': ftp.get('elev_beg'),
                'azim_end': ftp.get('azim_end'),
                'elev_end': ftp.get('elev_end'),
            })

        # Radiants with no FTP partner (rare — uncalibrated detection in radiants
        # but not in FTPdetectinfo): keep them with a deterministic ff_file
        # placeholder so the caller still sees the shower/magnitude info.
        for idx, rad in enumerate(all_radiants):
            if used[idx]:
                continue
            bdt = rad.get('_begin_dt')
            ff_placeholder = (
                f"RAD_{bdt.strftime('%Y%m%d_%H%M%S_%f')}" if bdt is not None else 'RAD_unknown'
            )
            detections.append({
                'ff_file': ff_placeholder,
                'time_utc': rad.get('time_utc'),
                'jd': rad.get('jd'),
                'solar_lon': rad.get('solar_lon'),
                'shower': rad.get('shower'),
                'mag_apparent': rad.get('mag_apparent'),
                'mag_absolute': rad.get('mag_absolute'),
                'duration_s': None,
                'angular_velocity': None,
                'num_segments': None,
                'fps': None,
                'ra_beg': rad.get('ra_beg'),
                'dec_beg': rad.get('dec_beg'),
                'ra_end': rad.get('ra_end'),
                'dec_end': rad.get('dec_end'),
                'ra_radiant': rad.get('ra_radiant'),
                'dec_radiant': rad.get('dec_radiant'),
                'radiant_elev': rad.get('radiant_elev'),
                'azim_beg': None,
                'elev_beg': None,
                'azim_end': None,
                'elev_end': None,
            })

        return {'camera': cam, 'date': date, 'count': len(detections),
                'detections': detections}

    return jsonify(cached(f'rms-det:{cam}:{date}', 120, collect))


@app.route('/api/encoding/stats')
def api_encoding_stats():
    """Return file-size reduction stats for the most recent reencoded night.

    Compares actual MKV sizes against expected raw size (bitrate × segment_duration).
    Returns per-camera and overall stats so the dashboard can show real reduction numbers.
    """
    def _compute() -> dict:
        cfg          = load_config()
        raw_kbps     = cfg.get('raw_bitrate_kbps', 8192)
        segment_s    = cfg.get('segment_duration', 20)
        raw_mb       = raw_kbps / 8 / 1000 * segment_s
        cap_base     = capture_base()
        stations_cfg = cfg.get('stations', {})

        per_cam: dict[str, dict] = {}
        all_pcts: list[float]    = []
        all_sizes: list[float]   = []

        for cam_dir in sorted(cap_base.iterdir()):
            if not cam_dir.is_dir():
                continue
            cam = cam_dir.name
            # Find the most recent night that has at least one .reencoded sidecar
            night_dir: Path | None = None
            for d in sorted(cam_dir.iterdir(), reverse=True):
                if d.is_dir() and any(d.glob('*_color.mkv.reencoded')):
                    night_dir = d
                    break
            if night_dir is None:
                continue

            pcts: list[float]  = []
            sizes: list[float] = []
            for mkv in night_dir.glob('*_color.mkv'):
                if not Path(str(mkv) + '.reencoded').exists():
                    continue
                actual_mb = mkv.stat().st_size / 1e6
                # Skip corrupted/truncated chunks and obvious raw-passthrough outliers
                if actual_mb < 0.5 or actual_mb > raw_mb * 1.1:
                    continue
                pct = 100.0 * (1.0 - actual_mb / raw_mb)
                pcts.append(pct)
                sizes.append(actual_mb)

            if not pcts:
                continue

            pcts_sorted  = sorted(pcts)
            sizes_sorted = sorted(sizes)
            per_cam[cam] = {
                'night':    night_dir.name,
                'chunks':   len(pcts),
                'avg':      round(sum(pcts) / len(pcts), 1),
                'median':   round(pcts_sorted[len(pcts) // 2], 1),
                'min':      round(pcts_sorted[0], 1),
                'max':      round(pcts_sorted[-1], 1),
                'size_avg': round(sum(sizes) / len(sizes), 1),
                'size_min': round(sizes_sorted[0], 1),
                'size_max': round(sizes_sorted[-1], 1),
            }
            all_pcts.extend(pcts)
            all_sizes.extend(sizes)

        overall: dict = {}
        if all_pcts:
            all_sorted    = sorted(all_pcts)
            sizes_sorted  = sorted(all_sizes)
            overall       = {
                'chunks':   len(all_pcts),
                'avg':      round(sum(all_pcts) / len(all_pcts), 1),
                'median':   round(all_sorted[len(all_pcts) // 2], 1),
                'min':      round(all_sorted[0], 1),
                'max':      round(all_sorted[-1], 1),
                'size_avg': round(sum(all_sizes) / len(all_sizes), 1),
                'size_min': round(sizes_sorted[0], 1),
                'size_max': round(sizes_sorted[-1], 1),
            }

        return {'cameras': per_cam, 'overall': overall, 'raw_mb': round(raw_mb, 1)}

    try:
        return jsonify(cached('encoding_stats', 3600, _compute))
    except Exception as e:
        return jsonify({'__error': str(e)}), 500


@app.route('/api/overlay_preview', methods=['POST'])
def api_overlay_preview():
    import os, tempfile
    cfg = load_config()
    ov  = {**cfg.get('overlay', {}), **(request.get_json(silent=True) or {})}

    font_path    = ov.get('font', '')
    font_size    = int(ov.get('font_size', 19))
    opacity      = float(ov.get('text_opacity', 0.4))
    logo_path    = ov.get('logo', '')
    logo_opacity = float(ov.get('logo_opacity', 0.8))
    logo_size    = int(ov.get('logo_size', 0))
    network      = ov.get('network', 'ROVIMEN')
    coords       = ov.get('coords', '')
    az           = float(ov.get('az', 0))
    alt          = float(ov.get('alt', 0))
    style        = ov.get('style', 'standard')
    show_logo      = ov.get('show_logo', True)
    show_network   = ov.get('show_network', True)
    show_station   = ov.get('show_station', True)
    show_coords    = ov.get('show_coords', True)
    show_pointing  = ov.get('show_pointing', True)
    show_timestamp = ov.get('show_timestamp', True)

    stations   = cfg.get('stations', {})
    station_id = next(iter(stations), 'STATION')

    if not Path(font_path).exists():
        return jsonify({'error': 'font not found'}), 400

    MARGIN = 14
    GAP    = 10
    sh_op  = opacity * 0.8
    az_alt = f'ALT {alt:.1f}  AZ {az:.1f}'
    now_us = int(datetime.now(timezone.utc).timestamp()) * 1_000_000

    def dt(text, x, y):
        return (f"drawtext=fontfile={font_path}:text='{text}'"
                f":fontsize={font_size}:fontcolor=white@{opacity:.2f}"
                f":shadowcolor=black@{sh_op:.2f}:shadowx=2:shadowy=2:x={x}:y={y}")

    def ts(x, y):
        return (f"drawtext=fontfile={font_path}:expansion=strftime"
                f":basetime={now_us}:text='%Y-%m-%d  %T UTC'"
                f":fontsize={font_size}:fontcolor=white@{opacity:.2f}"
                f":shadowcolor=black@{sh_op:.2f}:shadowx=2:shadowy=2:x={x}:y={y}")

    if style == 'cinema':
        # Row 0: ROVIMEN+logo (left)  |  az+alt (right)
        # Row 1: timestamp   (left)   |  station_id + coords (right)
        bar_h  = 2 * MARGIN + 2 * font_size + 1 * GAP
        y      = [f'h-{bar_h}+{MARGIN + i * (font_size + GAP)}' for i in range(2)]
        filters = [f'pad=iw:ih+{bar_h}:0:0:black']
        if show_network:   filters.append(dt(network,  str(MARGIN),      y[0]))
        if show_pointing:  filters.append(dt(az_alt,   f'w-{MARGIN}-tw', y[0]))
        if show_timestamp: filters.append(ts(          str(MARGIN),      y[1]))
        parts = []
        if show_station: parts.append(station_id)
        if show_coords:  parts.append(coords)
        if parts:        filters.append(dt('  '.join(parts), f'w-{MARGIN}-tw', y[1]))
    else:
        bar_h   = 0
        filters = []
        if show_timestamp: filters.append(ts(            f'w-{MARGIN}-tw', f'h-{MARGIN}-th'))
        if show_pointing:  filters.append(dt(az_alt,     f'w-{MARGIN}-tw', f'h-{MARGIN}-2*th-{GAP}'))
        if show_coords:    filters.append(dt(coords,     f'w-{MARGIN}-tw', f'h-{MARGIN}-3*th-{GAP*2}'))
        if show_station:   filters.append(dt(station_id, f'w-{MARGIN}-tw', f'h-{MARGIN}-4*th-{GAP*3}'))
        if show_network:   filters.append(dt(network,    str(MARGIN),       str(MARGIN)))

    fd, out_path = tempfile.mkstemp(suffix='.png')
    os.close(fd)
    try:
        use_logo = show_logo and logo_path and Path(logo_path).exists()
        if use_logo:
            try:
                from PIL import ImageFont, ImageDraw, Image as _Img
                _f  = ImageFont.truetype(font_path, font_size)
                _d  = ImageDraw.Draw(_Img.new('RGB', (2000, 100)))
                ntw = _d.textbbox((0, 0), network, font=_f)[2] - _d.textbbox((0, 0), network, font=_f)[0]
                bb  = _d.textbbox((0, 0), 'Ag', font=_f)
                logo_h = logo_size if logo_size > 0 else bb[3] - bb[1] + 1
            except Exception:
                ntw = int(font_size * 0.65 * len(network))
                logo_h = logo_size if logo_size > 0 else font_size
            logo_x   = MARGIN + ntw + 8
            text_center = f'H-{bar_h}+{MARGIN}+{font_size}/2' if style == 'cinema' else f'{MARGIN}+{font_size}/2'
            logo_y   = f'({text_center})-{logo_h}/2'
            vf_chain = ','.join(filters) if filters else 'null'
            fc = (f'[0:v]{vf_chain}[vmain];'
                  f'[1:v]scale=-2:{logo_h},format=rgba,colorchannelmixer=aa={logo_opacity}[logo];'
                  f'[vmain][logo]overlay=x={logo_x}:y={logo_y}[out]')
            cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                   '-f', 'lavfi', '-i', 'color=c=0x111111:size=1280x720:rate=1:duration=1',
                   '-i', logo_path, '-filter_complex', fc, '-map', '[out]',
                   '-frames:v', '1', out_path]
        else:
            vf  = ','.join(filters) if filters else 'null'
            cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                   '-f', 'lavfi', '-i', 'color=c=0x111111:size=1280x720:rate=1:duration=1',
                   '-vf', vf, '-frames:v', '1', out_path]

        r = subprocess.run(cmd, capture_output=True, timeout=15)
        if r.returncode != 0:
            return jsonify({'error': r.stderr.decode(errors='replace')[-300:]}), 500
        return Response(Path(out_path).read_bytes(), mimetype='image/png')
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# POST /api/restart/<service>
# ---------------------------------------------------------------------------

ALLOWED_SERVICES = {
    'color-capture', 'rovimen-station-api', 'rovimen-uploader',
}


@app.route('/api/restart/<service>', methods=['POST'])
def api_restart_service(service):
    if service not in ALLOWED_SERVICES:
        return jsonify({'error': f'Service {service!r} not in allowed list'}), 400
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'restart', service],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return jsonify({'error': result.stderr.strip()[:200]}), 500
        return jsonify({'ok': True, 'service': service})
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Restart timed out'}), 500
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# GET /api/logs/<service>?lines=200
# ---------------------------------------------------------------------------

_ALLOWED_LOG_SERVICES = {'color-capture', 'rovimen-station-api', 'dawn'}

# File-based log services: service name → filename under log_path (~/logs/ by default)
_FILE_LOG_SERVICES: dict[str, str] = {
    'dawn':      'rovimen_dawn.log',
    'stacker':   'stacker.log',
    'encoder':   'encoder.log',
    'timelapse': 'timelapse.log',
    'janitor':   'janitor.log',
}


def _find_latest_rms_log(station_id: str, cfg: dict) -> Path | None:
    """Return the most recently modified RMS log file for a station.

    RMS may write logs to either:
      - <rms_data_path>/logs/          (per-camera, e.g. RMS_data/cam1/logs/)
      - <rms_data_path>/../logs/       (shared parent dir, e.g. RMS_data/logs/)
    Try the per-camera dir first; fall back to the parent dir filtered by station_id.
    """
    scfg = cfg.get('stations', {}).get(station_id, {})
    rms_data_path = scfg.get('rms_data_path', '')
    if not rms_data_path:
        return None
    logs_dir = Path(rms_data_path) / 'logs'
    if logs_dir.exists():
        logs = sorted(logs_dir.glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            return logs[0]
    # Fallback: shared logs dir one level up, filter by station_id in filename
    parent_logs_dir = Path(rms_data_path).parent / 'logs'
    if parent_logs_dir.exists():
        logs = sorted(
            parent_logs_dir.glob(f'*{station_id}*.log'),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if logs:
            return logs[0]
    return None


def _tail_file(path: Path, lines: int) -> list[str]:
    """Return last `lines` lines of a file. lines=0 returns last 5000 lines."""
    if lines == 0:
        lines = 5000
    result = subprocess.run(
        ['tail', '-n', str(lines), str(path)],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout.splitlines()


@app.route('/api/logs/<service>', methods=['GET'])
def api_logs(service):
    raw = int(request.args.get('lines', 200))
    lines = 0 if raw == 0 else min(raw, 1000)

    # File-based logs (dawn, stacker, encoder, timelapse)
    if service in _FILE_LOG_SERVICES:
        cfg = load_config()
        log_dir = Path(cfg.get('log_path') or (Path.home() / 'logs'))
        log_file = log_dir / _FILE_LOG_SERVICES[service]
        if not log_file.exists():
            return jsonify({'service': service, 'lines': [], 'source': 'file',
                            'note': f'No {service} log found yet'})
        try:
            return jsonify({'service': service, 'lines': _tail_file(log_file, lines), 'source': 'file'})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 500

    # RMS per-camera log (rms-RO000H, rms-RO000J, …)
    if service.startswith('rms-'):
        station_id = service[4:]
        cfg = load_config()
        log_file = _find_latest_rms_log(station_id, cfg)
        if not log_file:
            return jsonify({'service': service, 'lines': [], 'source': 'file',
                            'note': f'No RMS log found for {station_id}'})
        try:
            return jsonify({'service': service, 'lines': _tail_file(log_file, lines),
                            'source': 'file', 'log_file': log_file.name})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 500

    # Systemd journal for remaining services
    if service not in _ALLOWED_LOG_SERVICES:
        return jsonify({'error': f'Service {service!r} not in allowed list'}), 400
    try:
        result = subprocess.run(
            ['journalctl', '-u', service, '-n', str(lines), '--no-pager', '--output=short-iso'],
            capture_output=True, text=True, timeout=10,
        )
        return jsonify({'service': service, 'lines': result.stdout.splitlines(), 'source': 'journal'})
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'journalctl timed out'}), 500
    except Exception as exc:
        return jsonify({'error': str(exc)})


@app.route('/api/settings', methods=['PATCH'])
def api_settings_patch():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)
    try:
        cfg = load_config()
        _deep_merge(cfg, payload)
        tmp = CONFIG_PATH.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(cfg, indent=2))
        os.replace(tmp, CONFIG_PATH)
        with _cache_lock:
            _cache.pop('config', None)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Archive / updater / services / dawn endpoints
# ---------------------------------------------------------------------------

@app.route('/api/archive/test', methods=['POST'])
def api_archive_test():
    """Test SSH connectivity to the configured archive host."""
    cfg = load_config()
    arc = cfg.get('archive', {})
    host = arc.get('host', '')
    port = int(arc.get('port', 22))
    user = arc.get('user', 'root')
    ssh_key = arc.get('ssh_key', '')
    if not host:
        return jsonify({'ok': False, 'error': 'No archive host configured'}), 400
    ssh_opts = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-p', str(port)]
    if ssh_key:
        ssh_opts += ['-i', ssh_key]
    try:
        result = subprocess.run(
            ['ssh'] + ssh_opts + [f'{user}@{host}', 'echo OK'],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and 'OK' in result.stdout:
            return jsonify({'ok': True})
        return jsonify({'ok': False, 'error': result.stderr.strip() or f'Exit {result.returncode}'})
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False, 'error': 'Connection timed out'}), 504
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


_UPDATER_CACHE = Path('/tmp/rovimen_updater_cache.json')


@app.route('/api/updater/status')
def api_updater_status():
    """Return local version, channel, and VPS host (no SSH call).

    Also returns the last-known remote version from the cache written by
    api_updater_check so the dashboard can show it on page load without
    making an SSH round-trip every time.
    """
    cfg = load_config()
    version_file = CONFIG_PATH.parent / '.version'
    local_ver = version_file.read_text().strip() if version_file.exists() else 'none'
    cached: dict = {}
    try:
        cached = json.loads(_UPDATER_CACHE.read_text())
    except Exception:
        pass
    return jsonify({
        'local':      local_ver,
        'channel':    cfg.get('update_channel', 'main'),
        'vps_host':   cfg.get('vps_host', ''),
        'remote':     cached.get('remote', ''),
        'status':     cached.get('status', ''),
        'up_to_date': cached.get('up_to_date', False),
    })


@app.route('/api/updater/check', methods=['POST'])
def api_updater_check():
    """Run updater.sh --check and parse the version/status lines."""
    updater_path = CONFIG_PATH.parent / 'updater.sh'
    if not updater_path.exists():
        return jsonify({'error': 'updater.sh not found'}), 404
    try:
        result = subprocess.run(
            ['bash', str(updater_path), '--check'],
            capture_output=True, text=True, timeout=30,
            cwd=str(CONFIG_PATH.parent),
        )
        parsed: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if ':' in line:
                k, _, v = line.partition(':')
                parsed[k.strip().lower()] = v.strip()
        up_to_date = parsed.get('status', '').lower() == 'up to date'
        payload = {
            'channel':    parsed.get('channel', ''),
            'local':      parsed.get('local', ''),
            'remote':     parsed.get('remote', ''),
            'status':     parsed.get('status', ''),
            'up_to_date': up_to_date,
        }
        # Cache remote version so /api/updater/status can serve it on page load
        try:
            _UPDATER_CACHE.write_text(json.dumps({
                'remote':     payload['remote'],
                'status':     payload['status'],
                'up_to_date': up_to_date,
            }))
        except Exception:
            pass
        return jsonify(payload)
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Check timed out after 30s'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


_UPDATE_LOG = Path('/tmp/rovimen_update.log')
_UPDATE_PID = Path('/tmp/rovimen_update.pid')
_update_proc: subprocess.Popen | None = None


@app.route('/api/updater/run', methods=['POST'])
def api_updater_run():
    """Start updater.sh in the background.  Output → /tmp/rovimen_update.log."""
    global _update_proc
    updater_path = CONFIG_PATH.parent / 'updater.sh'
    if not updater_path.exists():
        return jsonify({'ok': False, 'error': 'updater.sh not found'}), 404
    try:
        log_fh = _UPDATE_LOG.open('w')
        try:
            proc = subprocess.Popen(
                ['bash', str(updater_path)],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(CONFIG_PATH.parent),
            )
        finally:
            log_fh.close()  # child keeps the fd; parent closes its copy
        _UPDATE_PID.write_text(str(proc.pid))
        _update_proc = proc
        return jsonify({'ok': True, 'pid': proc.pid})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/updater/log')
def api_updater_log():
    """Return update log lines and whether the updater process is still running."""
    lines: list[str] = []
    if _UPDATE_LOG.exists():
        try:
            lines = _UPDATE_LOG.read_text().splitlines()
        except OSError:
            pass

    running = False
    if _update_proc is not None:
        # poll() reaps the child if it has exited — avoids false-positive from zombies
        running = _update_proc.poll() is None
    elif _UPDATE_PID.exists():
        # Fallback: process started before this server instance (e.g. after a restart)
        try:
            pid = int(_UPDATE_PID.read_text().strip())
            os.kill(pid, 0)
            running = True
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass

    return jsonify({'lines': lines, 'running': running, 'exit_code': None})


@app.route('/api/services/restart', methods=['POST'])
def api_services_restart():
    """Restart color-capture then rovimen-station-api (self-restart via background thread)."""
    errors: list[str] = []
    try:
        subprocess.run(
            ['sudo', 'systemctl', 'restart', 'color-capture'],
            capture_output=True, timeout=15, check=True,
        )
    except Exception as e:
        errors.append(f'color-capture: {e}')

    def _restart_self() -> None:
        time.sleep(0.5)
        subprocess.run(['sudo', 'systemctl', 'restart', 'rovimen-station-api'],
                       capture_output=True)

    threading.Thread(target=_restart_self, daemon=True).start()
    if errors:
        return jsonify({'ok': False, 'errors': errors}), 500
    return jsonify({'ok': True})


@app.route('/api/rovimen', methods=['POST'])
def api_rovimen_toggle():
    """Enable or disable all ROVIMEN services (except station-api) via toggle_rovimen.sh."""
    payload = request.get_json(silent=True) or {}
    enabled = payload.get('enabled')
    if enabled is None:
        return jsonify({'error': 'missing enabled field'}), 400
    cmd = 'on' if enabled else 'off'
    script = Path.home() / 'rovimen_scripts' / 'toggle_rovimen.sh'
    if not script.exists():
        return jsonify({'error': 'toggle_rovimen.sh not found'}), 500
    try:
        result = subprocess.run(
            ['bash', str(script), cmd],
            capture_output=True, text=True, timeout=30,
        )
        ok = result.returncode == 0
        out = (result.stdout + result.stderr).strip()
        resp: dict = {'ok': ok, 'output': out}
        if not ok:
            resp['error'] = out or 'toggle_rovimen.sh exited non-zero'
        return jsonify(resp)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rovimen/restart', methods=['POST'])
def api_rovimen_restart():
    """Restart all running ROVIMEN services via toggle_rovimen.sh restart."""
    script = Path.home() / 'rovimen_scripts' / 'toggle_rovimen.sh'
    if not script.exists():
        return jsonify({'error': 'toggle_rovimen.sh not found'}), 500
    try:
        result = subprocess.run(
            ['bash', str(script), 'restart'],
            capture_output=True, text=True, timeout=30,
        )
        return jsonify({'ok': result.returncode == 0, 'output': result.stdout + result.stderr})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rovimen/status')
def api_rovimen_status():
    """Run toggle_rovimen.sh status and return the output."""
    script = Path.home() / 'rovimen_scripts' / 'toggle_rovimen.sh'
    if not script.exists():
        return jsonify({'error': 'toggle_rovimen.sh not found'}), 500
    try:
        result = subprocess.run(
            ['bash', str(script), 'status'],
            capture_output=True, text=True, timeout=15,
        )
        return jsonify({'ok': True, 'output': result.stdout + result.stderr})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/color-capture', methods=['POST'])
def api_color_capture_toggle():
    """Start or stop the color-capture systemd service."""
    payload = request.get_json(silent=True) or {}
    enabled = payload.get('enabled')
    if enabled is None:
        return jsonify({'error': 'missing enabled field'}), 400
    # enable+start / disable+stop so the state persists across reboots
    verbs = ['enable', 'start'] if enabled else ['disable', 'stop']
    try:
        out_parts: list[str] = []
        ok = True
        for verb in verbs:
            r = subprocess.run(
                ['sudo', '-n', 'systemctl', verb, 'color-capture'],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                ok = False
            if (r.stdout + r.stderr).strip():
                out_parts.append((r.stdout + r.stderr).strip())
        with _cache_lock:
            _cache.pop('status', None)
        out = '\n'.join(out_parts)
        resp: dict = {'ok': ok, 'output': out}
        if not ok:
            resp['error'] = out or 'systemctl command failed'
        return jsonify(resp)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/dawn/run', methods=['POST'])
def api_dawn_run():
    """Start dawn_process.py for the requested date in the background."""
    payload = request.get_json(silent=True) or {}
    date_str = payload.get('date', '')
    if not re.match(r'^\d{8}$', date_str):
        return jsonify({'error': 'date must be YYYYMMDD'}), 400
    dawn_path = CONFIG_PATH.parent / 'dawn_process.py'
    if not dawn_path.exists():
        return jsonify({'error': 'dawn_process.py not found'}), 404
    try:
        cfg = load_config()
        log_dir = Path(cfg.get('log_path') or (Path.home() / 'logs'))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / _FILE_LOG_SERVICES['dawn']
        with open(log_file, 'a') as lf:
            proc = subprocess.Popen(
                [sys.executable, str(dawn_path), '-c', str(CONFIG_PATH), '--date', date_str],
                start_new_session=True,
                stdout=lf,
                stderr=lf,
            )
        return jsonify({'ok': True, 'pid': proc.pid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/dawn/progress')
def api_dawn_progress():
    """Return per-camera morning pipeline progress for the given date.

    Query param: date=YYYYMMDD (defaults to last completed night).
    """
    cfg = load_config()
    date_str = request.args.get('date', '')
    if not re.match(r'^\d{8}$', date_str):
        now = datetime.now(timezone.utc)
        date_str = (
            (now - timedelta(days=1)).strftime('%Y%m%d')
            if now.hour < 12 else now.strftime('%Y%m%d')
        )

    cameras: dict[str, dict] = {}
    for cam in cfg.get('stations', {}):
        if not _STATE_MANAGER_OK:
            cameras[cam] = {'error': 'flags_manager not available'}
            continue
        try:
            state = _flags_manager.load(cam, date_str, cfg)
            # Only count real captured chunks (STATION_YYYYMMDD_HHMMSS_color.mkv).
            # Other entries (e.g. night stack artefacts from an archive_upload bug)
            # are excluded so the denominator stays accurate.
            _chunk_re = re.compile(r'^[A-Z0-9]+_\d{8}_\d{6}_color\.mkv$')
            chunks = {
                k: v for k, v in state.get('chunks', {}).items()
                if _chunk_re.match(k)
            }
            total     = len(chunks)
            stacked   = sum(1 for c in chunks.values() if c.get('stacked'))
            reencoded = sum(1 for c in chunks.values() if c.get('reencoded'))
            locked    = sum(
                1 for c in chunks.values()
                if isinstance(c.get('lock'), dict)
                and c['lock'].get('lock_type') == 'detection'
            )
            uploaded  = sum(1 for c in chunks.values() if c.get('uploaded'))
            cameras[cam] = {
                'total':              total,
                'stacked':            stacked,
                'reencoded':          reencoded,
                'locked':             locked,
                'uploaded':           uploaded,
                'rms_complete':       bool(state.get('rms_complete', False)),
                'morning_done':       bool(state.get('morning_done', False)),
                'timelapse_done':     bool(state.get('timelapse_done', False)),
                'timelapse_uploaded': bool(state.get('timelapse_uploaded', False)),
            }
        except Exception as e:
            cameras[cam] = {'error': str(e)}

    try:
        r = subprocess.run(['pgrep', '-f', 'dawn_process'], capture_output=True)
        running = r.returncode == 0
    except Exception:
        running = False

    return jsonify({'date': date_str, 'cameras': cameras, 'running': running})

# ---------------------------------------------------------------------------
# GET /api/crons
# ---------------------------------------------------------------------------

@app.route('/api/crons')
def api_crons():
    """Return parsed crontab and /etc/cron.d/ entries for the station user."""
    entries: list[dict] = []

    # User crontab
    try:
        r = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split(None, 5)
                if len(parts) >= 6:
                    entries.append({
                        'schedule': ' '.join(parts[:5]),
                        'command':  parts[5],
                        'source':   'crontab',
                    })
    except Exception:
        pass

    # /etc/cron.d/ — lines have format: schedule user command
    cron_d = Path('/etc/cron.d')
    if cron_d.exists():
        for f in sorted(cron_d.iterdir()):
            if not f.is_file() or f.name.startswith('.'):
                continue
            try:
                for line in f.read_text(errors='ignore').splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split(None, 6)
                    if len(parts) >= 7:
                        entries.append({
                            'schedule': ' '.join(parts[:5]),
                            'command':  parts[6],
                            'source':   f'/etc/cron.d/{f.name}',
                        })
            except Exception:
                pass

    return jsonify(entries)


# ---------------------------------------------------------------------------
# GET /api/rms/status
# ---------------------------------------------------------------------------

@app.route('/api/rms/status')
def api_rms_status():
    """Return RMS processed/uploaded status per camera for a given night date.

    Query param: date=YYYYMMDD (defaults to last completed night).
    Processed: FTPdetectinfo_*.txt found in CapturedFiles or ArchivedFiles session dir.
    Uploaded:  session exists in ArchivedFiles (sent to GMN).
    """
    date_str = request.args.get('date', '')
    if not re.match(r'^\d{8}$', date_str):
        now = datetime.now(timezone.utc)
        date_str = (
            (now - timedelta(days=1)).strftime('%Y%m%d')
            if now.hour < 12 else now.strftime('%Y%m%d')
        )

    cfg = load_config()
    result: dict[str, dict] = {}

    # Detect which RMS instances are currently capturing (StartCapture running)
    # and which are processing (DetectStarsAndMeteors running).
    # Map cam index → bool by checking systemd service state.
    detecting_procs = subprocess.run(
        ['pgrep', '-af', 'DetectStarsAndMeteors'],
        capture_output=True, text=True
    ).stdout.lower()

    capture_procs = subprocess.run(
        ['pgrep', '-af', 'StartCapture'],
        capture_output=True, text=True
    ).stdout

    for cam, sinfo in cfg.get('stations', {}).items():
        rms_data_path = sinfo.get('rms_data_path')
        if not rms_data_path:
            result[cam] = {'processed': False, 'uploaded': False,
                           'capturing': False, 'processing': False}
            continue

        rms_dir = Path(rms_data_path)
        processed = False
        uploaded   = False

        # Match sessions on observation night, not raw calendar date in the
        # dir name. A post-midnight session (e.g. RO000W_20260412_005904_*)
        # belongs to night 20260411 and a substring match on the date_str
        # would miss it. Use _night_date() to translate session timestamps,
        # matching detection_lock.process_night().
        _arc_re = re.compile(r'^[A-Z0-9]+_(\d{8})_(\d{6})_')
        for sub, is_archived in (('ArchivedFiles', True), ('CapturedFiles', False)):
            sub_dir = rms_dir / sub
            if not sub_dir.exists():
                continue
            try:
                sessions = list(sub_dir.iterdir())
            except OSError:
                continue
            for session_dir in sessions:
                if not session_dir.is_dir():
                    continue
                m = _arc_re.match(session_dir.name)
                if not m:
                    continue
                if _night_date(m.group(1), m.group(2)) != date_str:
                    continue
                try:
                    has_ftp = any(session_dir.glob('FTPdetectinfo_*.txt'))
                except OSError:
                    has_ftp = False
                if has_ftp:
                    processed = True
                if is_archived:
                    uploaded = True

        # Derive RMS instance index from rms_data_path (e.g. .../cam2 → 2)
        import re as _re
        m = _re.search(r'cam(\d+)$', rms_data_path.rstrip('/'))
        cam_idx = m.group(1) if m else None
        # Detect capturing: station code appears in StartCapture args
        # (source/Stations layout: .../RO000T/.config) or RMS_cam{N} for
        # classic layout (.../RMS_cam1/.config). Station code is reliable
        # across both layouts.
        capturing = (cam in capture_procs
                     or bool(cam_idx and f'RMS_cam{cam_idx}' in capture_procs))
        processing = (cam in detecting_procs
                      or bool(cam_idx and f'rms_cam{cam_idx}' in detecting_procs))

        result[cam] = {'processed': processed, 'uploaded': uploaded,
                       'capturing': capturing, 'processing': processing}

    return jsonify({'date': date_str, 'cameras': result})


# ---------------------------------------------------------------------------
# GET /api/rms/plots/<cam>/<date>
# GET /api/rms/plot_image/<cam>/<date>/<filename>
# ---------------------------------------------------------------------------

_RMS_PLOT_LABELS: dict[str, str] = {
    '_DETECTED_thumbs.jpg':           'Detected thumbnails',
    '_CAPTURED_thumbs.jpg':           'Captured thumbnails',
    '_captured_stack.jpg':            'Captured stack',
    '_calib_report_astrometry.jpg':   'Astrometry calibration',
    '_calib_report_photometry.png':   'Photometry calibration',
    '_calibration_variation.png':     'Calibration variation',
    '_photometry_variation.png':      'Photometry variation',
    '_fieldsums.png':                 'Field sums',
    '_fieldsums_noavg.png':           'Field sums (no avg)',
    '_observing_periods.png':         'Observing periods',
    '_ff_intervals.png':              'FF intervals',
    '_radiants.png':                  'Radiants',
}
_RMS_PLOT_ORDER = list(_RMS_PLOT_LABELS.keys())


def _find_rms_session_dir(cam: str, date: str, cfg: dict) -> Path | None:
    """Return the most-recent RMS session dir for cam+date (ArchivedFiles preferred)."""
    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if not rms_path:
        return None
    rms_dir = Path(rms_path)
    prefix = f'{cam}_{date}_'
    candidates: list[Path] = []
    for sub in ('ArchivedFiles', 'CapturedFiles'):
        sub_dir = rms_dir / sub
        if not sub_dir.exists():
            continue
        for d in sub_dir.iterdir():
            if d.is_dir() and d.name.startswith(prefix):
                candidates.append(d)
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def _find_latest_platepar(cam: str, cfg: dict) -> Path | None:
    """Return the most recently modified platepar_cmn2010.cal for a camera.

    Search order (all candidates pooled, newest wins):
    1. ~/source/Stations/<cam>/platepar_cmn2010.cal  — active RMS platepar
    2. <rms_data_path>/ArchivedFiles/<session>/      — archived session platepars
    3. <rms_data_path>/CapturedFiles/<session>/      — captured session platepars
    """
    candidates: list[Path] = []

    # Active RMS platepar (standard location updated after each calibration)
    active_pp = BASE / 'source' / 'Stations' / cam / 'platepar_cmn2010.cal'
    if active_pp.exists():
        candidates.append(active_pp)

    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if rms_path:
        rms_dir = Path(rms_path)
        for sub in ('CapturedFiles', 'ArchivedFiles'):
            sub_dir = rms_dir / sub
            if not sub_dir.exists():
                continue
            for session in sub_dir.iterdir():
                if not session.is_dir():
                    continue
                pp = session / 'platepar_cmn2010.cal'
                if pp.exists():
                    candidates.append(pp)

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _find_latest_mask(cam: str, cfg: dict) -> Path | None:
    """Return the most recently modified mask.bmp for a camera.

    RMS stores the detection mask as ``mask.bmp`` alongside the platepar in the
    camera's config directory. We pool the same candidate locations used for the
    platepar (active station dir + RMS session dirs) and pick the newest.

    Search order (all candidates pooled, newest wins):
    1. ~/source/Stations/<cam>/mask.bmp           — active RMS mask
    2. <rms_data_path>/ArchivedFiles/<session>/   — archived session masks
    3. <rms_data_path>/CapturedFiles/<session>/   — captured session masks
    """
    candidates: list[Path] = []

    active_mask = BASE / 'source' / 'Stations' / cam / 'mask.bmp'
    if active_mask.exists():
        candidates.append(active_mask)

    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if rms_path:
        rms_dir = Path(rms_path)
        for sub in ('CapturedFiles', 'ArchivedFiles'):
            sub_dir = rms_dir / sub
            if not sub_dir.exists():
                continue
            for session in sub_dir.iterdir():
                if not session.is_dir():
                    continue
                m = session / 'mask.bmp'
                if m.exists():
                    candidates.append(m)

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _mask_contour(mask_path: Path, max_points: int = 64) -> list[list[float]] | None:
    """Derive a simplified boundary polygon of the UNMASKED region.

    RMS masks are grayscale bitmaps where 0 = masked (blocked by trees, buildings,
    horizon) and >0 = active (observable sky). We return a closed polygon of
    ``[nx, ny]`` points in normalised sensor coordinates (each in [-1, 1], matching
    the convention consumed by the dashboard's distortion model where
    ``x_img = nx * x_res / 2``), so the frontend can walk the actual sky boundary
    instead of the full sensor edge.

    Cheap column-scan envelope: for a downsampled set of columns we take the
    topmost and bottommost active pixel, then stitch the top edge (left→right) and
    bottom edge (right→left) into one loop. This captures the common horizon/tree
    occlusions (vertical clipping per column) without OpenCV/skimage. Returns
    None when no usable contour exists or the mask is effectively full
    (active fraction >= 0.999) so callers fall back to the sensor-edge walk.

    Interior holes and pure left/right side masks are not represented in v1 — see
    follow-up note in the PR.
    """
    if not _ensure_rms_venv_on_path():
        return None
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(mask_path) as im:
            arr = np.asarray(im)
    except Exception:
        return None

    if arr.ndim == 3:
        arr = arr[..., 0]
    if arr.ndim != 2 or arr.size == 0:
        return None

    height, width = arr.shape
    if height < 2 or width < 2:
        return None

    active = arr > 0
    if active.mean() >= 0.999:
        return None  # mask is effectively full — use sensor-edge walk

    n_cols = max(2, min(max_points // 2, width))
    cols = np.linspace(0, width - 1, n_cols).astype(int)

    top: list[tuple[int, int]] = []
    bot: list[tuple[int, int]] = []
    for c in cols:
        rows = np.nonzero(active[:, int(c)])[0]
        if rows.size == 0:
            continue
        top.append((int(c), int(rows[0])))
        bot.append((int(c), int(rows[-1])))

    if len(top) < 2:
        return None

    def _norm(x: int, y: int) -> list[float]:
        return [round(2.0 * x / width - 1.0, 4), round(2.0 * y / height - 1.0, 4)]

    poly = [_norm(x, y) for x, y in top] + [_norm(x, y) for x, y in reversed(bot)]
    return poly


@app.route('/api/platepar')
def api_platepar():
    """Return platepar data (pointing, FOV, rotation) for each configured camera."""
    cfg = load_config()
    stations = cfg.get('stations', {})
    result: dict[str, dict] = {}
    for cam in stations:
        pp_path = _find_latest_platepar(cam, cfg)
        if not pp_path:
            result[cam] = {'error': 'no platepar found'}
            continue
        try:
            pp = json.loads(pp_path.read_text())
        except Exception as e:
            result[cam] = {'error': str(e)}
            continue
        result[cam] = {
            'az_centre':               pp.get('az_centre'),
            'alt_centre':              pp.get('alt_centre'),
            'rotation_from_horiz':     pp.get('rotation_from_horiz'),
            'fov_h':                   pp.get('fov_h'),
            'fov_v':                   pp.get('fov_v'),
            'lat':                     pp.get('lat'),
            'lon':                     pp.get('lon'),
            'F_scale':                 pp.get('F_scale'),
            'X_res':                   pp.get('X_res'),
            'Y_res':                   pp.get('Y_res'),
            'x_poly_fwd':              pp.get('x_poly_fwd'),
            'y_poly_fwd':              pp.get('y_poly_fwd'),
            'equal_aspect':            pp.get('equal_aspect'),
            'asymmetry_corr':          pp.get('asymmetry_corr'),
            'force_distortion_centre': pp.get('force_distortion_centre'),
            'x_poly':                  pp.get('x_poly'),  # legacy / fallback
            'y_poly':                  pp.get('y_poly'),
            'distortion_type':         pp.get('distortion_type'),
            'RA_d':                    pp.get('RA_d'),
            'dec_d':                   pp.get('dec_d'),
            'pos_angle_ref':           pp.get('pos_angle_ref'),
            'source':                  str(pp_path),
        }
        # Optional: boundary of the unmasked sky region, so the dashboard can
        # render the FOV polygon clipped to the actual observable area rather
        # than the full sensor edge. None/absent → frontend uses sensor edges.
        try:
            mask_path = _find_latest_mask(cam, cfg)
            if mask_path is not None:
                contour = _mask_contour(mask_path)
                if contour:
                    result[cam]['mask_contour'] = contour
                    result[cam]['mask_source'] = str(mask_path)
        except Exception:
            pass
    return jsonify(result)


@app.route('/api/rms_cameras')
def api_rms_cameras():
    """Scan all RMS .config files and return camera codes and IPs.

    Supports two layouts:
      - ~/RMS_cam{1-8}/.config  (gmn0002/gmn0003 style)
      - ~/source/Stations/*/.config  (gmn0005 style)
    """
    def _parse_config(cfg_path: Path) -> dict | None:
        station_id = None
        camera_ip = None
        try:
            for line in cfg_path.read_text().splitlines():
                line = line.strip()
                if line.startswith('stationID'):
                    station_id = line.split(':', 1)[-1].strip()
                elif line.startswith('camera_ip'):
                    camera_ip = line.split(':', 1)[-1].strip()
                elif line.startswith('device') and camera_ip is None:
                    # e.g. device: rtsp://192.168.42.11:554/...
                    val = line.split(':', 1)[-1].strip()
                    m = re.search(r'rtsp://(?:[^@]+@)?([^:/]+)', val)
                    if m:
                        camera_ip = m.group(1)
        except Exception:
            return None
        if station_id:
            return {'code': station_id, 'cam_ip': camera_ip or ''}
        return None

    result: list[dict] = []
    seen: set[str] = set()

    # Layout 1: ~/RMS_cam{1-8}/.config
    for i in range(1, 9):
        cfg_path = BASE / f'RMS_cam{i}' / '.config'
        if cfg_path.exists():
            entry = _parse_config(cfg_path)
            if entry and entry['code'] not in seen:
                seen.add(entry['code'])
                result.append(entry)

    # Layout 2: ~/source/Stations/*/.config
    stations_dir = BASE / 'source' / 'Stations'
    if stations_dir.is_dir():
        for cfg_path in sorted(stations_dir.glob('*/.config')):
            entry = _parse_config(cfg_path)
            if entry and entry['code'] not in seen:
                seen.add(entry['code'])
                result.append(entry)

    return jsonify(result)


@app.route('/api/rms/plots/<cam>/<date>')
def api_rms_plots(cam: str, date: str):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    cfg = load_config()
    session_dir = _find_rms_session_dir(cam, date, cfg)
    if not session_dir:
        return jsonify([])
    plots: list[dict] = []
    for f in session_dir.iterdir():
        if f.suffix not in ('.jpg', '.png'):
            continue
        label: str | None = None
        order = 999
        for i, suffix in enumerate(_RMS_PLOT_ORDER):
            if f.name.endswith(suffix):
                label = _RMS_PLOT_LABELS[suffix]
                order = i
                break
        if label is None and '_stack_' in f.name and f.name.endswith('.jpg'):
            label = 'Meteor stack'
            order = 2
        if label is None:
            continue
        plots.append({'filename': f.name, 'label': label, 'order': order})
    plots.sort(key=lambda p: p['order'])
    return jsonify(plots)


@app.route('/api/rms/plot_image/<cam>/<date>/<filename>')
def api_rms_plot_image(cam: str, date: str, filename: str):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    if not re.match(r'^[\w._-]+\.(jpg|png)$', filename):
        abort(400)
    cfg = load_config()
    session_dir = _find_rms_session_dir(cam, date, cfg)
    if not session_dir:
        abort(404)
    path = session_dir / filename
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(str(path), conditional=True)


def _find_latest_captured_stack(cam: str, cfg: dict) -> Path | None:
    """Return the most recently modified *_captured_stack.jpg across all RMS sessions.

    Search ArchivedFiles first (preferred — these are post-processing copies),
    then CapturedFiles. Pool all matches and pick by mtime so we always serve
    the freshest one regardless of which subtree it lives under.
    """
    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if not rms_path:
        return None
    rms_dir = Path(rms_path)
    candidates: list[Path] = []
    for sub in ('ArchivedFiles', 'CapturedFiles'):
        sub_dir = rms_dir / sub
        if not sub_dir.exists():
            continue
        for session in sub_dir.iterdir():
            if not session.is_dir() or not session.name.startswith(f'{cam}_'):
                continue
            for f in session.iterdir():
                if f.name.endswith('_captured_stack.jpg'):
                    candidates.append(f)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@app.route('/api/latest_stack/<cam>')
def api_latest_stack(cam: str):
    """Serve the most recent _captured_stack.jpg for a camera, regardless of date.

    Used by the dashboard celestial-dome route to assemble one current-pointing
    background image per camera without walking the night/plots tree from the
    client side.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    cfg = load_config()
    path = _find_latest_captured_stack(cam, cfg)
    if not path:
        abort(404)
    return send_file(str(path), conditional=True)


def _find_latest_chunk_stack(cam: str) -> Path | None:
    """Return the freshest per-chunk colour ``*_stack.webp`` for a camera.

    Walks ``<capture_base>/<cam>/<night>/stacks/`` across all nights and picks
    the most recently modified file matching ``*_stack.webp`` (excluding the
    ``*_avg.webp`` avgpx companions written by ``stacker._finish_stack`` and
    the per-night ``*_color_meteor_stack.webp`` composite).

    This is the time-varying source used by the dashboard sky-dome panorama:
    each capture chunk is ~5 min, so the background rolls forward with the
    night. Falls back to the RMS captured_stack via ``_find_latest_captured_stack``
    in the route handler when no per-chunk colour stack exists yet (early in
    the evening, station with stacker disabled).
    """
    cap_root = capture_base() / cam
    if not cap_root.exists():
        return None
    candidates: list[Path] = []
    try:
        night_dirs = sorted(
            (d for d in cap_root.iterdir() if d.is_dir() and re.match(r'^\d{8}$', d.name)),
            key=lambda d: d.name,
            reverse=True,
        )
    except OSError:
        return None
    # Scan only the two most-recent night directories. Older stacks are stale
    # for "live" purposes, and on stations with many nights the full walk
    # adds avoidable I/O to a frequently-hit endpoint.
    for night_dir in night_dirs[:2]:
        stacks_dir = night_dir / 'stacks'
        if not stacks_dir.is_dir():
            continue
        try:
            for f in stacks_dir.iterdir():
                name = f.name
                if not name.endswith('_stack.webp'):
                    continue
                if name.endswith('_avg.webp'):
                    continue  # avgpx companion, not a display stack
                if name.endswith('_color_meteor_stack.webp'):
                    continue  # per-night composite, not per-chunk
                if f.is_file():
                    candidates.append(f)
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@app.route('/api/latest_chunk_stack/<cam>')
def api_latest_chunk_stack(cam: str):
    """Serve the freshest per-chunk colour ``*_stack.webp`` for a camera.

    Source for the time-varying sky-dome panorama. Returns 404 when no
    per-chunk colour stack exists yet (the dashboard then falls back to
    ``/api/latest_stack`` which serves the RMS captured_stack.jpg).
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    path = _find_latest_chunk_stack(cam)
    if not path:
        abort(404)
    return send_file(str(path), conditional=True, mimetype='image/webp')


@app.route('/api/latest_frame/<cam>')
def api_latest_frame(cam: str):
    """Return the freshest visual snapshot for a camera, in this order:

      1. Latest per-chunk colour ``*_stack.webp`` (rolling ~5 min, full colour).
      2. RMS ``_captured_stack.jpg`` (per-session FF mosaic, BW, ~10 min).

    Single endpoint for the dashboard "live frame" tile — keeps the
    fallback decision station-side so the dashboard doesn't have to chase
    two 404s round-trip when a camera has no colour stacker output.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    chunk = _find_latest_chunk_stack(cam)
    if chunk:
        return send_file(str(chunk), conditional=True, mimetype='image/webp')
    cfg = load_config()
    capt = _find_latest_captured_stack(cam, cfg)
    if capt:
        return send_file(str(capt), conditional=True)
    abort(404)


@app.route('/api/color-meteor-stack/<cam>/<date>')
def api_color_meteor_stack(cam: str, date: str):
    """Serve the per-night colour meteor stack built by
    ``stacker.build_night_color_meteor_stack`` during dawn processing."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    path = capture_base() / cam / date / 'stacks' / f'{cam}_{date}_color_meteor_stack.webp'
    if not path.exists():
        abort(404)
    return send_file(str(path), conditional=True, mimetype='image/webp')


# ---------------------------------------------------------------------------
# GET  /api/logo/list
# POST /api/logo/upload
# ---------------------------------------------------------------------------

_ASSETS_DIR = BASE / 'rovimen_scripts' / 'assets'
_LOGO_EXTS  = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}


@app.route('/api/logo/list')
def api_logo_list():
    """List image files available in the assets directory."""
    if not _ASSETS_DIR.exists():
        return jsonify([])
    files = sorted(
        f.name for f in _ASSETS_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in _LOGO_EXTS
    )
    return jsonify([{'filename': f, 'path': str(_ASSETS_DIR / f)} for f in files])


@app.route('/api/logo/upload', methods=['POST'])
def api_logo_upload():
    """Upload a new logo image to the assets directory."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in _LOGO_EXTS:
        return jsonify({'error': f'Unsupported file type: {ext}'}), 400
    safe_name = re.sub(r'[^\w.\-]', '_', Path(f.filename).name)
    _ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _ASSETS_DIR / safe_name
    f.save(str(dest))
    return jsonify({'filename': safe_name, 'path': str(dest)})


# ---------------------------------------------------------------------------
# GET /api/storagewatch/status
# ---------------------------------------------------------------------------

_STORAGEWATCH_STATE = BASE / 'logs' / 'janitor_state.json'


@app.route('/api/storagewatch/status')
def api_storagewatch_status():
    """Return janitor last-run state written by janitor_storage_watchdog.py."""
    if not _STORAGEWATCH_STATE.exists():
        return jsonify({'error': 'no_state'}), 404
    try:
        return jsonify(json.loads(_STORAGEWATCH_STATE.read_text()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# POST /api/encode_chunk
# ---------------------------------------------------------------------------

@app.route('/api/encode_chunk', methods=['POST'])
def api_encode_chunk():
    """Encode a single raw MKV chunk in-place (overlay, colour cal, rotation).

    Body JSON: {"camera": "RO000H", "date": "20260326", "filename": "...mkv"}
    Returns 200 {"ok": true} on success, or 4xx/5xx with {"error": "..."}.
    Idempotent: if .reencoded sidecar already exists, returns 200 immediately.
    """
    data = request.get_json(silent=True) or {}
    cam      = data.get('camera', '')
    date     = data.get('date', '')
    filename = data.get('filename', '')
    if not re.match(r'^[A-Z0-9]+$', cam):
        return jsonify({'error': 'invalid camera'}), 400
    if not re.match(r'^\d{8}$', date):
        return jsonify({'error': 'invalid date'}), 400
    if not re.match(r'^[\w.\-]+\.mkv$', filename):
        return jsonify({'error': 'invalid filename'}), 400

    mkv = capture_base() / cam / date / filename
    if not mkv.exists():
        return jsonify({'error': 'file not found'}), 404

    try:
        import encoder as _encoder  # local import — only needed for this endpoint
        cfg = load_config()
        ok  = _encoder.process_chunk(mkv, cfg)
        if not ok:
            return jsonify({'ok': False, 'error': 'encoder skipped chunk (not ready or already encoded)'}), 422
        return jsonify({'ok': True})
    except Exception as e:
        logger.exception('encode_chunk failed: %s', e)
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# File serving  (with HTTP Range support for video seeking)
# ---------------------------------------------------------------------------

THUMB_W = 480  # max thumbnail width (px)
THUMB_Q = 80   # lossy WebP quality


def _serve(path: Path):
    if not path.exists():
        abort(404)
    return send_file(path, conditional=True)


def _make_thumbnail(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['ffmpeg', '-y', '-i', str(src),
         '-vf', f'scale={THUMB_W}:-2',
         '-quality', str(THUMB_Q),
         str(dst)],
        capture_output=True, check=True, timeout=30,
    )


def _make_thumbnail_from_video(mkv: Path, dst: Path) -> None:
    """Extract a single frame from the middle of an MKV and save as WebP thumbnail."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['ffmpeg', '-y', '-ss', '10', '-i', str(mkv),
         '-frames:v', '1', '-vf', f'scale={THUMB_W}:-2',
         '-quality', str(THUMB_Q),
         str(dst)],
        capture_output=True, check=True, timeout=30,
    )


def _local_utc_offset_minutes():
    """Return the local timezone offset in minutes (e.g. EET = +120)."""
    local_now = datetime.now()
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    return round((local_now - utc_now).total_seconds() / 60)


def _find_ff_maxpixels(cam, date, time_str):
    """Find ALL FF FITS files that overlap a 20s chunk window.

    MKV timestamps are in local time; FF timestamps are always UTC.
    Returns a list of FF paths covering the chunk's time range.
    """
    cfg = load_config()
    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if not rms_path:
        return []
    rms_dir = Path(rms_path)
    segment_duration = cfg.get('segment_duration', 20)

    # Convert chunk local time to UTC
    utc_date, utc_time = _local_to_utc(date, time_str)
    ch_secs_utc = int(utc_time[:2]) * 3600 + int(utc_time[2:4]) * 60 + int(utc_time[4:6])

    # Search both the chunk date and UTC date (handles midnight boundary)
    search_dates = list(set([date, utc_date]))

    matches = []
    for sub in ('CapturedFiles', 'ArchivedFiles'):
        parent = rms_dir / sub if (rms_dir / sub).exists() else rms_dir.parent / sub
        if not parent.exists():
            continue
        for date_dir in parent.iterdir():
            if not date_dir.is_dir() or not date_dir.name.startswith(cam):
                continue
            for search_date in search_dates:
                for ff in date_dir.glob(f'FF_{cam}_{search_date}_*.fits'):
                    m = re.match(r'FF_[A-Z0-9]+_\d{8}_(\d{6})', ff.name)
                    if not m:
                        continue
                    ff_time = m.group(1)
                    ff_secs = int(ff_time[:2]) * 3600 + int(ff_time[2:4]) * 60 + int(ff_time[4:6])
                    diff = ff_secs - ch_secs_utc
                    if diff < -43200:
                        diff += 86400
                    if diff > 43200:
                        diff -= 86400
                    if -5 <= diff <= segment_duration + 5:
                        matches.append((diff, ff))
    matches.sort()
    return [ff for _, ff in matches]


def _ensure_rms_venv_on_path() -> bool:
    """Add RMS venv site-packages and RMS source to sys.path. Returns True if numpy available."""
    import sys as _sys
    # RMS venv: ~/RMS/venv/lib/python*/site-packages
    rms_venv_lib = BASE / 'RMS' / 'venv' / 'lib'
    if rms_venv_lib.exists():
        for p in rms_venv_lib.glob('python*/site-packages'):
            if str(p) not in _sys.path:
                _sys.path.insert(0, str(p))
    # RMS source: ~/RMS (contains RMS package)
    rms_src = BASE / 'RMS'
    if rms_src.exists() and str(rms_src) not in _sys.path:
        _sys.path.insert(0, str(rms_src))
    try:
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def _read_ff_maxpixel(ff_path: Path):
    """Read maxpixel data from an FF FITS file. Returns numpy array or None."""
    _ensure_rms_venv_on_path()
    try:
        import numpy as np
    except ImportError:
        return None
    # Try astropy first
    try:
        from astropy.io import fits as afits
        with afits.open(str(ff_path)) as hdul:
            for ext in ('MAXPIXEL', 'MAX', 0):
                try:
                    if hdul[ext].data is not None:
                        return hdul[ext].data
                except (KeyError, IndexError):
                    continue
    except ImportError:
        pass
    # Try RMS FFfile
    try:
        from RMS.Formats.FFfile import read as readFF
        ff = readFF(str(ff_path.parent), ff_path.name)
        if ff is not None:
            return ff.maxpixel
    except Exception:
        pass
    return None


def _ff_maxpixel_to_webp(ff_paths) -> 'io.BytesIO | None':
    """Combine FF maxpixels in memory and return a grayscale WebP as BytesIO. No disk write."""
    _ensure_rms_venv_on_path()
    import io
    import numpy as np
    combined = None
    for ff_path in ff_paths:
        data = _read_ff_maxpixel(ff_path)
        if data is None:
            continue
        if data.ndim == 3:
            data = data[0] if data.shape[0] in (2, 4) else np.mean(data, axis=2)
        data = data.astype(np.uint8)
        combined = data if combined is None else np.maximum(combined, data)
    if combined is None:
        return None
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(combined, 'L').convert('RGB').save(buf, format='webp', lossless=True)
        buf.seek(0)
        return buf
    except Exception:
        return None


def _find_latest_ff(cam: str) -> Path | None:
    """Return the newest FF FITS file for <cam> across all RMS sessions.

    Filenames embed a UTC timestamp (``FF_<cam>_YYYYMMDD_HHMMSS_*.fits``) so
    the lexically-largest name is also the newest. We sort sessions by name
    (also timestamp-prefixed) so we only have to scan the two most recent.
    """
    cfg = load_config()
    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if not rms_path:
        return None
    rms_dir = Path(rms_path)
    # Newest sessions live in CapturedFiles; ArchivedFiles holds post-processed
    # copies that may lag by a day. Prefer CapturedFiles for "right now".
    for sub in ('CapturedFiles', 'ArchivedFiles'):
        parent = rms_dir / sub if (rms_dir / sub).exists() else rms_dir.parent / sub
        if not parent.exists():
            continue
        try:
            sessions = sorted(
                (d for d in parent.iterdir()
                 if d.is_dir() and d.name.startswith(f'{cam}_')),
                key=lambda d: d.name,
                reverse=True,
            )
        except OSError:
            continue
        # Two newest sessions is enough — older ones can't beat anything in
        # the newest by FF timestamp.
        for session in sessions[:2]:
            try:
                ffs = sorted(
                    session.glob(f'FF_{cam}_*.fits'),
                    key=lambda p: p.name,
                    reverse=True,
                )
            except OSError:
                continue
            if ffs:
                return ffs[0]
    return None


@app.route('/api/latest_ff_maxpixel/<cam>')
def api_latest_ff_maxpixel(cam: str):
    """Single newest FF maxpixel for <cam>, BW WebP, no disk write. 404 if none.

    Used by the dashboard's "Sky right now" widget on the Video DB tab —
    one FF covers ~10 s of sky, so polling this every minute keeps the
    live celestial dome rolling forward without waiting for the ~5 min
    per-chunk colour stacker pass.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    ff = _find_latest_ff(cam)
    if ff is None:
        abort(404)
    buf = _ff_maxpixel_to_webp([ff])
    if buf is None:
        abort(503)
    resp = send_file(buf, mimetype='image/webp')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    # FF filename format: FF_<STATION>_<YYYYMMDD>_<HHMMSS>_<MS>_<FRAMENUMBER>.fits
    # Surface the capture timestamp to the dashboard so it can show a
    # freshness indicator next to the dome image.
    m = re.match(r'^FF_[A-Z0-9]+_(\d{8})_(\d{6})_(\d{3})', ff.name)
    if m:
        d, t, ms = m.group(1), m.group(2), m.group(3)
        resp.headers['X-FF-Timestamp'] = (
            f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}.{ms}Z"
        )
        resp.headers['Access-Control-Expose-Headers'] = 'X-FF-Timestamp'
    return resp


@app.route('/thumbnail/<cam>/<date>/<filename>')
def serve_thumbnail(cam, date, filename):
    for part in (cam, date, filename):
        if '..' in part or '/' in part:
            abort(400)
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    if not re.match(r'^[\w._-]+\.webp$', filename):
        abort(400)

    cap_dir = capture_base() / cam / date
    src = cap_dir / filename
    dst = cap_dir / 'thumbs' / filename

    # Color stack may be at flat path (legacy) or stacks/ subdir (new layout)
    src_subdir = cap_dir / 'stacks' / filename
    color_stack = src if src.exists() else (src_subdir if src_subdir.exists() else None)

    if not dst.exists():
        if color_stack is not None:
            # Stack exists — generate thumbnail from it
            try:
                _make_thumbnail(color_stack, dst)
            except subprocess.CalledProcessError:
                abort(500)
        else:
            stem = filename.replace('_stack.webp', '')
            mkv = cap_dir / f'{stem}_color.mkv'

            # No color stack — serve FF BW maxpixel directly from memory (no disk write)
            m = re.match(r'^([A-Z0-9]+)_(\d{8})_(\d{6})$', stem)
            if m:
                ffs = _find_ff_maxpixels(m.group(1), m.group(2), m.group(3))
                if ffs:
                    import io
                    buf = _ff_maxpixel_to_webp(ffs)
                    if buf:
                        return send_file(buf, mimetype='image/webp')

            # Fallback: extract a frame from the MKV
            if not mkv.exists():
                abort(404)
            try:
                _make_thumbnail_from_video(mkv, dst)
            except subprocess.CalledProcessError:
                abort(500)

    return send_file(dst, conditional=True)


@app.route('/color_capture/<cam>/<date>/<filename>')
def serve_capture(cam, date, filename):
    for part in (cam, date, filename):
        if '..' in part or '/' in part:
            abort(400)
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    if not re.match(r'^[\w._-]+\.(mkv|webp)$', filename):
        abort(400)
    # Check flat layout first, then videos/ and stacks/ subdirectories
    base = capture_base() / cam / date
    path = base / filename
    if not path.exists():
        path = base / 'videos' / filename
    if not path.exists():
        path = base / 'stacks' / filename
    if path.exists():
        return _serve(path)

    # No stack on disk — serve FF BW maxpixel directly from memory
    if filename.endswith('_stack.webp'):
        stem = filename[:-len('_stack.webp')]
        m = re.match(r'^([A-Z0-9]+)_(\d{8})_(\d{6})$', stem)
        if m:
            ffs = _find_ff_maxpixels(m.group(1), m.group(2), m.group(3))
            if ffs:
                import io
                buf = _ff_maxpixel_to_webp(ffs)
                if buf:
                    return send_file(buf, mimetype='image/webp')

    return _serve(path)  # triggers 404


@app.route('/color_timelapse/<cam>/<date>/<filename>')
@app.route('/night_stack/<cam>/<date>/<filename>')
def serve_timelapse(cam, date, filename):
    for part in (cam, date, filename):
        if '..' in part or '/' in part:
            abort(400)
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    if not re.match(r'^[\w._-]+\.(mp4|webp)$', filename):
        abort(400)
    return _serve(capture_base() / cam / date / filename)


# ---------------------------------------------------------------------------
# Live MJPEG stream — called by the dashboard instead of SSH+shell ffmpeg
# ---------------------------------------------------------------------------

_STREAM_SEMS: dict[str, threading.Semaphore] = {}
_STREAM_SEM_LOCK = threading.Lock()
_STREAM_MAX_PER_CAM = 2
_STREAM_GLOBAL_SEM = threading.Semaphore(6)

_CAM_CODE_RE = re.compile(r'^[A-Z0-9]+$', re.IGNORECASE)
_IP_RE = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')


def _stream_sem(cam: str) -> threading.Semaphore:
    with _STREAM_SEM_LOCK:
        if cam not in _STREAM_SEMS:
            _STREAM_SEMS[cam] = threading.Semaphore(_STREAM_MAX_PER_CAM)
        return _STREAM_SEMS[cam]


@app.route('/api/stream/<cam_code>')
def api_stream(cam_code: str):
    """Stream MJPEG from a camera's RTSP feed via local ffmpeg.

    The dashboard calls this over HTTP instead of constructing a shell
    command string passed through SSH — eliminating shell-injection risk.
    """
    if not _CAM_CODE_RE.match(cam_code):
        abort(400)

    cfg = load_config()
    stations = cfg.get('stations', {})
    cam_entry = None
    for _sid, scfg in stations.items():
        if isinstance(scfg, dict) and scfg.get('station_id') == cam_code:
            cam_entry = scfg
            break
        if isinstance(scfg, dict):
            cam_ip = scfg.get('cam_ip', '')
            if scfg.get('station_id') == cam_code or _sid == cam_code:
                cam_entry = scfg
                break

    cam_ip = None
    rtsp_url = None
    rotate = False

    if cam_entry:
        cam_ip = cam_entry.get('cam_ip', '')
        rtsp_url = cam_entry.get('rtsp_url') or cam_entry.get('camera_rtsp')
        rotate = bool(cam_entry.get('rotate', False))

    if not cam_ip and not rtsp_url:
        rms_cameras_resp = []
        for i in range(1, 9):
            cfg_path = BASE / f'RMS_cam{i}' / '.config'
            if cfg_path.exists():
                try:
                    for line in cfg_path.read_text().splitlines():
                        line_s = line.strip()
                        if line_s.startswith('stationID'):
                            sid = line_s.split(':', 1)[-1].strip()
                            if sid == cam_code:
                                for l2 in cfg_path.read_text().splitlines():
                                    l2s = l2.strip()
                                    if l2s.startswith('camera_ip'):
                                        cam_ip = l2s.split(':', 1)[-1].strip()
                                    elif l2s.startswith('device') and not cam_ip:
                                        m = re.search(r'rtsp://(?:[^@]+@)?([^:/]+)', l2s)
                                        if m:
                                            cam_ip = m.group(1)
                                break
                except Exception:
                    pass

    if not rtsp_url:
        if not cam_ip or not _IP_RE.match(cam_ip):
            return jsonify({'error': 'camera not found or invalid IP'}), 404
        rtsp_url = (
            f"rtsp://admin:@{cam_ip}:554/"
            f"user=admin&password=&channel=1&stream=0.sdp"
        )

    rotate_q = request.args.get('rotate', '0')
    if rotate_q == '1':
        rotate = True

    vf = "scale=854:480,vflip,hflip" if rotate else "scale=854:480"

    per_cam = _stream_sem(cam_code)
    if not per_cam.acquire(blocking=False):
        return jsonify({'error': 'too_many_streams', 'scope': 'per_camera'}), 429
    if not _STREAM_GLOBAL_SEM.acquire(blocking=False):
        per_cam.release()
        return jsonify({'error': 'too_many_streams', 'scope': 'global'}), 429

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-vf", vf,
        "-r", "12", "-q:v", "5",
        "-f", "mjpeg", "pipe:1",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        per_cam.release()
        _STREAM_GLOBAL_SEM.release()
        return jsonify({'error': 'ffmpeg failed to start'}), 500

    import selectors

    def generate():
        buf = b""
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                ready = sel.select(timeout=30)
                if not ready:
                    break
                chunk = proc.stdout.read(32768)
                if not chunk:
                    break
                buf += chunk
                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi == -1:
                        buf = b""
                        break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi == -1:
                        buf = buf[soi:]
                        break
                    frame = buf[soi:eoi + 2]
                    buf = buf[eoi + 2:]
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + frame + b"\r\n"
                    )
        finally:
            sel.close()
            proc.kill()
            proc.wait()
            per_cam.release()
            _STREAM_GLOBAL_SEM.release()

    return Response(
        stream_with_context(generate()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


# ---------------------------------------------------------------------------
# GET /api/detections-index
# ---------------------------------------------------------------------------

_INDEX_DB = BASE / 'rovimen_scripts' / 'detections_index.db'
# Must match detection_indexer.COLS (and dashboard/detection_db.DETECTION_COLS
# minus the VPS-only ``source`` column) so the poller receives every field.
_INDEX_COLS = (
    'cam', 'date', 'ff_file', 'meteor_no',
    'time_utc', 'jd', 'solar_lon', 'shower',
    'mag_apparent', 'mag_absolute', 'duration_s',
    'ra_beg', 'dec_beg', 'ra_end', 'dec_end',
    'ra_radiant', 'dec_radiant', 'radiant_elev',
    'angular_velocity', 'num_segments', 'fps',
    'azim_beg', 'elev_beg', 'azim_end', 'elev_end',
    'chunk_file',
)


@app.route('/api/detections-index')
def api_detections_index():
    """Return all indexed detections for dates >= ?since=YYYYMMDD.

    The VPS index_poller calls this endpoint every few minutes and merges
    results into the fleet-wide detections.db, replacing per-request
    storagebox SSHFS file parsing.
    """
    since = request.args.get('since', '')
    if since and not re.match(r'^\d{8}$', since):
        abort(400, description='since must be YYYYMMDD')
    if not _INDEX_DB.exists():
        return jsonify({'detections': [], 'ready': False})
    try:
        from contextlib import closing
        with closing(sqlite3.connect(str(_INDEX_DB), check_same_thread=False)) as con:
            con.execute('PRAGMA busy_timeout=15000')
            con.row_factory = sqlite3.Row
            cols = ', '.join(_INDEX_COLS)
            if since:
                rows = con.execute(
                    f'SELECT {cols} FROM detections WHERE date >= ? ORDER BY date, time_utc',
                    (since,),
                ).fetchall()
            else:
                rows = con.execute(
                    f'SELECT {cols} FROM detections ORDER BY date, time_utc',
                ).fetchall()
        return jsonify({
            'ready': True,
            'count': len(rows),
            'detections': [dict(r) for r in rows],
        })
    except Exception as exc:
        logger.error('detections-index query failed: %s', exc)
        return jsonify({'detections': [], 'ready': False, 'error': str(exc)})


# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7779, threaded=True)
