#!/usr/bin/env python3
from __future__ import annotations
"""
Color Video Capture Daemon for RMS Meteor Stations

Captures color RTSP video alongside RMS's grayscale processing pipeline.
Uses inotify to detect FF file activity (night started/stopped) and
runs ffmpeg to write 20-second MKV chunks directly to videocapture_path.

Tee mode (color_tee=True in config.json): instead of spawning ffmpeg with
its own RTSP connection, links raw video segments from RMS's built-in
splitmuxsink (raw_video_save=True in the RMS .config). This halves the
number of RTSP connections per camera, which is required on stations with
10 Mbps ethernet where two concurrent streams exceed the link capacity.

compression_level 0: -c copy (raw, no annotation)
compression_level 1-4: hardware or software encode with rotation, color gains,
                   and OSD annotation bar using wall-clock per-frame timestamps.

Usage:
    python color_capture.py -c config.json
    python color_capture.py -c config.json --test-duration 300
"""

import errno
import os
import sys
import json
import queue as _queue
import shutil
import signal
import logging
import argparse
import threading
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Try inotify — required on Linux, skip gracefully for dev/testing
try:
    import inotify.adapters
    INOTIFY_AVAILABLE = True
except ImportError:
    INOTIFY_AVAILABLE = False

try:
    import overlay
    _OVERLAY_OK = True
except ImportError:
    _OVERLAY_OK = False

import flags_manager

_CHUNK_NAME_RE = __import__('re').compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_color\.mkv$')
# Matches RMS splitmuxsink segment names (see BufferedCapture.moveSegment)
_VIDEO_SEGMENT_RE = __import__('re').compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_\d{6}_video\.mkv$')


class ColorCapture:
    """Manages color video capture for multiple RMS cameras."""

    def __init__(self, config_path: str, test_duration: Optional[int] = None):
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
        self.capture_path.mkdir(parents=True, exist_ok=True)

        self.segment_duration = self.cfg.get('segment_duration', 20)
        self.idle_timeout = self.cfg.get('ff_idle_timeout_minutes', 30) * 60
        self.min_disk_gb = self.cfg.get('min_disk_gb_free', 20)
        self.test_duration = test_duration

        self.color_tee = self.cfg.get('color_tee', False)

        # ffmpeg subprocesses keyed by station_id
        self._ffmpeg: dict[str, subprocess.Popen] = {}
        # Tee watcher threads keyed by station_id (used when color_tee=True)
        self._tee_watchers: dict[str, threading.Thread] = {}
        # Per-station stop events for tee watchers (set at dawn to stop the thread)
        self._tee_stop_events: dict[str, threading.Event] = {}
        # Last time we saw an FF file per station
        self._last_ff: dict[str, float] = {}
        # Stations paused due to low disk — suppresses per-FF-file log spam
        self._disk_paused: set[str] = set()
        self._running = False
        self._config_path = config_path
        self._last_segment_mtime: dict[str, tuple[float, float]] = {}
        self._ffmpeg_log_fh: dict[str, object] = {}

        # Stacker serialisation — at most `stacker_max_concurrent` stackers
        # run simultaneously so HDD I/O is not saturated by concurrent ffmpeg
        # rawvideo reads (root cause of the inotify watcher crash).
        # Default: 1 (fully serial). Increase only if capture_path is on SSD.
        self._stacker_workers: int = int(self.cfg.get('stacker_max_concurrent', 1))
        self._stacker_queue: _queue.Queue = _queue.Queue()
        # Chunk filenames currently queued or being stacked — prevents duplicate
        # launches when the watcher sees repeated events for the same chunk.
        self._stacker_queued: set[str] = set()
        self._stacker_lock = threading.Lock()

        self._setup_logging()

    def _load_cfg(self) -> None:
        try:
            with open(self._config_path) as f:
                self.cfg = json.load(f)
            new_tee = self.cfg.get('color_tee', False)
            if new_tee != self.color_tee:
                logging.getLogger('color_capture').warning(
                    'color_tee changed in config (%s -> %s) — restart required to take effect',
                    self.color_tee, new_tee,
                )
        except Exception:
            logging.getLogger('color_capture').exception(
                'Config reload failed — keeping previous config'
            )

    def _setup_logging(self):
        log_dir = Path(self.cfg.get('log_path') or str(Path.home() / 'logs')) / 'color_capture'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f'{self._night_date()}.log'

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(str(log_file)),
            ],
        )
        self.log = logging.getLogger('color_capture')

    # ------------------------------------------------------------------
    # Night date — boundary at noon UTC so one night = one folder
    # ------------------------------------------------------------------

    @staticmethod
    def _night_date() -> str:
        """Return the 'night date' as YYYYMMDD. Before noon UTC uses yesterday's date."""
        now = datetime.now(timezone.utc)
        if now.hour < 12:
            now -= timedelta(days=1)
        return now.strftime('%Y%m%d')

    # ------------------------------------------------------------------
    # Disk space check
    # ------------------------------------------------------------------

    def _disk_ok(self) -> bool:
        try:
            usage = shutil.disk_usage(str(self.capture_path))
        except OSError as exc:
            self.log.warning('disk_usage failed (%s) — assuming OK to avoid killing capture', exc)
            return True
        free_gb = usage.free / (1024 ** 3)
        if free_gb < self.min_disk_gb:
            self.log.warning('Low disk: %.1f GB free (need %d GB)', free_gb, self.min_disk_gb)
            return False
        return True

    # ------------------------------------------------------------------
    # Tee mode — link RMS raw_video_save segments into color_capture
    # ------------------------------------------------------------------

    @staticmethod
    def _color_name_from_video(video_filename: str) -> str | None:
        """Convert RMS video segment name to color capture name.

        RMS names segments like RO000Z_20260622_234512_123456_video.mkv.
        Returns the color_capture equivalent: RO000Z_20260622_234512_color.mkv.
        """
        m = _VIDEO_SEGMENT_RE.match(video_filename)
        if not m:
            return None
        return f'{m.group(1)}_{m.group(2)}_{m.group(3)}_color.mkv'

    def _rms_video_path(self, station_id: str) -> Path:
        """Return the RMS raw video directory for a station."""
        rms_data = Path(self.cfg['stations'][station_id]['rms_data_path'])
        return rms_data / 'video'

    def _start_tee_watcher(self, station_id: str) -> None:
        """Start a tee watcher thread for one station (replaces ffmpeg in tee mode)."""
        if not INOTIFY_AVAILABLE:
            self.log.error('inotify not available — cannot start tee watcher for %s', station_id)
            return
        if station_id in self._tee_watchers:
            t = self._tee_watchers[station_id]
            if t.is_alive():
                return
        stop_evt = threading.Event()
        self._tee_stop_events[station_id] = stop_evt
        t = threading.Thread(
            target=self._tee_watcher, args=(station_id, stop_evt),
            name=f'tee-{station_id}', daemon=True,
        )
        t.start()
        self._tee_watchers[station_id] = t
        self.log.info('Tee watcher started for %s', station_id)

    def _stop_tee_watcher(self, station_id: str) -> None:
        """Signal a tee watcher thread to stop."""
        evt = self._tee_stop_events.pop(station_id, None)
        if evt:
            evt.set()
        self._tee_watchers.pop(station_id, None)
        self.log.info('Tee watcher stop requested for %s', station_id)

    @staticmethod
    def _night_date_from_filename(date_str: str, time_str: str) -> str:
        """Derive night date from segment filename timestamp fields.

        Uses the same noon-UTC boundary as _night_date(), but based on the
        timestamp embedded in the filename rather than the current wall-clock.
        This ensures segments processed from a backlog after a crash go to the
        correct date folder.
        """
        if int(time_str[:2]) < 12:
            nd = (datetime(int(date_str[:4]), int(date_str[4:6]),
                           int(date_str[6:])) - timedelta(days=1))
            return nd.strftime('%Y%m%d')
        return date_str

    def _tee_watcher(self, station_id: str, stop_event: threading.Event) -> None:
        """Watch RMS video directory and hardlink segments into color_capture.

        Runs as a daemon thread. When RMS writes a raw video segment via its
        GStreamer tee + splitmuxsink, this thread detects the closed file and
        creates a hardlink (or copy on cross-device) in the color_capture
        directory with the standard _color.mkv naming. After linking, it
        touches the destination to emit IN_CLOSE_WRITE so the chunk-ready
        watcher picks it up for flags_manager and stacking.
        """
        rms_video = self._rms_video_path(station_id)
        backoff = 5

        while self._running and not stop_event.is_set():
            if not rms_video.exists():
                self.log.info('Waiting for RMS video dir %s to appear', rms_video)
                if stop_event.wait(timeout=10):
                    return
                continue

            try:
                inot = inotify.adapters.Inotify()
                watched: set[str] = set()

                def _add_recursive(base: str) -> None:
                    for root, _dirs, _files in os.walk(base):
                        if root not in watched:
                            try:
                                inot.add_watch(root)
                                watched.add(root)
                            except Exception as e:
                                self.log.warning('tee %s: add_watch %s: %s', station_id, root, e)

                _add_recursive(str(rms_video))
                self.log.info('Tee watcher for %s watching %d dirs under %s',
                              station_id, len(watched), rms_video)
                backoff = 5

                for event in inot.event_gen(yield_nones=True, timeout_s=5):
                    if not self._running or stop_event.is_set():
                        return

                    if event is None:
                        _add_recursive(str(rms_video))
                        continue

                    _, type_names, path, filename = event

                    if 'IN_ISDIR' in type_names and ('IN_CREATE' in type_names or 'IN_MOVED_TO' in type_names):
                        new_dir = os.path.join(path, filename)
                        if new_dir not in watched:
                            try:
                                inot.add_watch(new_dir)
                                watched.add(new_dir)
                            except Exception:
                                pass
                        continue

                    if 'IN_CLOSE_WRITE' not in type_names:
                        continue
                    if not filename.endswith('_video.mkv'):
                        continue

                    color_name = self._color_name_from_video(filename)
                    if not color_name:
                        continue

                    m = _VIDEO_SEGMENT_RE.match(filename)
                    night = self._night_date_from_filename(m.group(2), m.group(3))

                    if not self._disk_ok():
                        self.log.warning('Tee %s: disk full, skipping %s', station_id, filename)
                        continue

                    src = Path(path) / filename
                    out_dir = self.capture_path / station_id / night
                    out_dir.mkdir(parents=True, exist_ok=True)
                    dst = out_dir / color_name

                    if dst.exists():
                        self.log.debug('Tee %s: %s already exists, skipping', station_id, dst.name)
                        continue

                    try:
                        os.link(str(src), str(dst))
                        # Hardlinks emit IN_CREATE, not IN_CLOSE_WRITE. Touch
                        # the file so the chunk-ready watcher picks it up.
                        open(dst, 'ab').close()
                        self.log.info('Tee linked %s -> %s', filename, dst.name)
                    except OSError as e:
                        if e.errno == errno.EXDEV:
                            try:
                                shutil.copy2(str(src), str(dst))
                                self.log.info('Tee copied (cross-device) %s -> %s', filename, dst.name)
                            except Exception as ce:
                                self.log.warning('Tee copy failed %s: %s', filename, ce)
                        else:
                            self.log.warning('Tee link failed %s: %s', filename, e)

            except Exception:
                self.log.exception('Tee watcher for %s crashed, restarting in %ds', station_id, backoff)
                if stop_event.wait(timeout=backoff):
                    return
                backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------
    # Encoder selection — direct-to-disk with full annotation pipeline
    # ------------------------------------------------------------------

    def _encoder_args(self, station_id: str) -> list[str]:
        """Return ffmpeg video encoder args — always raw copy, no decode/encode at capture."""
        return ['-c:v', 'copy']

    # ------------------------------------------------------------------
    # ffmpeg management
    # ------------------------------------------------------------------

    def _start_ffmpeg(self, station_id: str):
        """Start ffmpeg segment recording for one camera.

        In tee mode (color_tee=True), starts a filesystem watcher instead of
        ffmpeg, linking RMS raw video segments into the color_capture directory.
        """
        if self.color_tee:
            self._start_tee_watcher(station_id)
            return

        if station_id in self._ffmpeg:
            if self._ffmpeg[station_id].poll() is None:
                return  # still running
            del self._ffmpeg[station_id]  # dead process — fall through to restart
        self._load_cfg()

        if not self._disk_ok():
            if station_id not in self._disk_paused:
                self.log.error('Disk full — pausing capture for %s', station_id)
                self._disk_paused.add(station_id)
            return

        station = self.cfg['stations'][station_id]
        # Prefer capture_rtsp when set: on bandwidth-starved sites (slow camera
        # links) the camera is fronted by a local RTSP relay so RMS and color
        # capture share a single upstream pull. camera_rtsp stays the real
        # camera URL (used by camera-control tooling); capture_rtsp points at
        # the loopback relay. Falls back to camera_rtsp when no relay is used.
        rtsp_url = station.get('capture_rtsp') or station['camera_rtsp']
        date_str = self._night_date()

        out_dir = self.capture_path / station_id / date_str
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(out_dir / f'{station_id}_%Y%m%d_%H%M%S_color.mkv')

        cmd = [
            'ffmpeg',
            '-hide_banner',
            '-loglevel', 'warning',
            '-rtsp_transport', 'tcp',
            '-i', rtsp_url,
            '-c:v', 'copy',
            '-an',
            '-f', 'segment',
            '-segment_time', str(self.segment_duration),
            '-reset_timestamps', '1',
            '-strftime', '1',
            '-segment_format', 'matroska',
            pattern,
        ]

        self.log.info('Starting ffmpeg for %s: %s', station_id, ' '.join(cmd[:10]) + ' ...')

        fh = None
        try:
            log_dir = Path(self.cfg.get('log_path') or str(Path.home() / 'logs')) / 'color_capture'
            log_dir.mkdir(parents=True, exist_ok=True)
            ffmpeg_log = log_dir / f'ffmpeg_{station_id}.log'
            fh = open(ffmpeg_log, 'a')
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=fh,
            )
            self._ffmpeg_log_fh[station_id] = fh
            self._ffmpeg[station_id] = proc
            self._last_ff[station_id] = time.monotonic()
            self.log.info('ffmpeg started for %s (pid %d)', station_id, proc.pid)
        except FileNotFoundError:
            if fh:
                fh.close()
            self.log.error('ffmpeg not found — is it installed?')
        except Exception as e:
            if fh:
                fh.close()
            self.log.error('Failed to start ffmpeg for %s: %s', station_id, e)

    def _stop_ffmpeg(self, station_id: str):
        """Gracefully stop ffmpeg for one camera."""
        proc = self._ffmpeg.pop(station_id, None)
        if proc is None:
            return
        fh = getattr(self, '_ffmpeg_log_fh', {}).pop(station_id, None)
        if fh:
            try:
                fh.close()
            except Exception:
                pass
        self.log.info('Stopping ffmpeg for %s (pid %d)', station_id, proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.log.warning('ffmpeg for %s did not die after kill', station_id)

    def _stop_all_ffmpeg(self):
        for sid in list(self._ffmpeg):
            self._stop_ffmpeg(sid)
        for sid in list(self._tee_watchers):
            self._stop_tee_watcher(sid)

    def _check_ffmpeg_health(self):
        """Restart ffmpeg if it died or produced no new segment recently.

        Skipped entirely in tee mode — tee watchers are passive and self-recovering.
        """
        if self.color_tee:
            return
        stall_threshold = self.segment_duration * 3
        for sid in list(self._ffmpeg):
            proc = self._ffmpeg[sid]
            if proc.poll() is not None:
                self.log.warning('ffmpeg for %s exited (code %s), restarting', sid, proc.returncode)
                del self._ffmpeg[sid]
                self._start_ffmpeg(sid)
                continue

            # Check for stalled ffmpeg (alive but not writing segments)
            date_str = self._night_date()
            out_dir = self.capture_path / sid / date_str
            if not out_dir.exists():
                continue
            segments = list(out_dir.glob('*_color.mkv'))
            if not segments:
                continue

            def _safe_mtime(p):
                try:
                    return p.stat().st_mtime
                except FileNotFoundError:
                    return 0.0

            segments = [p for p in segments if _safe_mtime(p) > 0]
            if not segments:
                continue
            latest = max(segments, key=_safe_mtime)
            newest_mtime = _safe_mtime(latest)
            prev = self._last_segment_mtime.get(sid)
            if prev is None or prev[0] != newest_mtime:
                self._last_segment_mtime[sid] = (newest_mtime, time.monotonic())
            age = time.monotonic() - self._last_segment_mtime[sid][1]
            if age > stall_threshold:
                self.log.warning(
                    'ffmpeg for %s stalled (last segment %.0fs ago, threshold %ds), restarting',
                    sid, age, stall_threshold,
                )
                self._stop_ffmpeg(sid)
                self._start_ffmpeg(sid)

    # ------------------------------------------------------------------
    # inotify-based FF file watching
    # ------------------------------------------------------------------

    def _get_station_for_ff(self, ff_filename: str) -> Optional[str]:
        """Extract station ID from FF filename like FF_RO000A_2026...fits."""
        parts = ff_filename.split('_')
        if len(parts) >= 2 and ff_filename.startswith('FF_'):
            candidate = parts[1]
            if candidate in self.cfg['stations']:
                return candidate
        return None

    def _watch_ff_files(self):
        """Watch all station CapturedFiles dirs for FF activity via inotify."""
        if not INOTIFY_AVAILABLE:
            self.log.error('inotify not available — install with: pip install inotify')
            return

        notifier = inotify.adapters.Inotify()

        # Watch each station's CapturedFiles directory
        watched_paths: dict[str, str] = {}  # path -> station_id
        for station_id, station_cfg in self.cfg['stations'].items():
            captured = Path(station_cfg['rms_data_path']) / 'CapturedFiles'
            captured.mkdir(parents=True, exist_ok=True)
            notifier.add_watch(
                str(captured),
                mask=(inotify.constants.IN_CLOSE_WRITE
                      | inotify.constants.IN_CREATE
                      | inotify.constants.IN_MOVED_TO),
            )
            watched_paths[str(captured)] = station_id
            self.log.info('Watching %s for station %s', captured, station_id)

            for sub in captured.iterdir():
                if sub.is_dir():
                    notifier.add_watch(
                        str(sub),
                        mask=inotify.constants.IN_CLOSE_WRITE | inotify.constants.IN_MOVED_TO,
                    )
                    watched_paths[str(sub)] = station_id
                    self.log.info('Watching subdir %s', sub)

        self.log.info('inotify watching %d paths', len(watched_paths))

        last_health_check = time.monotonic()
        last_cfg_reload = time.monotonic()
        start_time = time.monotonic()

        while self._running:
            for event in notifier.event_gen(timeout_s=5, yield_nones=True):
                if not self._running:
                    break

                if self.test_duration and (time.monotonic() - start_time) > self.test_duration:
                    self.log.info('Test duration reached (%ds), stopping', self.test_duration)
                    self._running = False
                    break

                now = time.monotonic()

                if event is not None:
                    (_, type_names, path, filename) = event

                    if 'IN_CREATE' in type_names or 'IN_ISDIR' in type_names:
                        new_path = os.path.join(path, filename)
                        if os.path.isdir(new_path):
                            station_id = watched_paths.get(path)
                            if station_id:
                                notifier.add_watch(
                                    new_path,
                                    mask=(inotify.constants.IN_CLOSE_WRITE
                                          | inotify.constants.IN_MOVED_TO),
                                )
                                watched_paths[new_path] = station_id
                                self.log.info('Now watching new subdir: %s', new_path)

                    if filename and filename.startswith('FF_') and filename.endswith('.fits'):
                        if 'IN_CLOSE_WRITE' in type_names or 'IN_MOVED_TO' in type_names:
                            station_id = self._get_station_for_ff(filename)
                            if station_id is None:
                                station_id = watched_paths.get(path)
                            if station_id:
                                self.log.info('FF activity: %s (%s)', filename, station_id)
                                self._last_ff[station_id] = now
                                already_running = (
                                    station_id in self._ffmpeg
                                    or (self.color_tee and station_id in self._tee_watchers
                                        and self._tee_watchers[station_id].is_alive())
                                )
                                if not already_running and station_id not in self._disk_paused:
                                    self.log.info('Night started for %s — starting capture', station_id)
                                    self._start_ffmpeg(station_id)

                # Config reload (every 60s)
                if now - last_cfg_reload > 60:
                    last_cfg_reload = now
                    self._load_cfg()

                # Periodic checks (every 30s)
                if now - last_health_check > 30:
                    last_health_check = now
                    self._check_ffmpeg_health()

                    # Resume any disk-paused stations if space has recovered
                    for sid in list(self._disk_paused):
                        if self._disk_ok():
                            self.log.info('Disk recovered — resuming capture for %s', sid)
                            self._disk_paused.discard(sid)
                            self._start_ffmpeg(sid)

                    for sid in list(self._ffmpeg):
                        last = self._last_ff.get(sid, 0)
                        if now - last > self.idle_timeout:
                            self.log.info(
                                'No FF activity for %s in %d min — stopping capture (dawn?)',
                                sid, self.idle_timeout // 60,
                            )
                            self._stop_ffmpeg(sid)

                    for sid in list(self._tee_watchers):
                        last = self._last_ff.get(sid, 0)
                        if now - last > self.idle_timeout:
                            self.log.info(
                                'No FF activity for %s in %d min — stopping tee watcher (dawn?)',
                                sid, self.idle_timeout // 60,
                            )
                            self._stop_tee_watcher(sid)

    # ------------------------------------------------------------------
    # Stacker worker — serialises stacker launches to protect HDD I/O
    # ------------------------------------------------------------------

    def _stacker_worker(self) -> None:
        """Pull chunks from the stacker queue and run them one at a time.

        Running multiple stackers concurrently causes all of them to read
        large MKV files from the HDD simultaneously, which stalls the disk
        for 30-60 s, blocks the inotify event loop, and crashes the watcher —
        leaving chunks with ready=False and no real-time stacks.

        With _stacker_workers=1 (default) all stackers are fully serial.
        Each one reads sequentially from the HDD and finishes in ~10-15 s.
        Use stacker_max_concurrent=2 only if capture_path is on an SSD.
        """
        while self._running:
            try:
                station_id, chunk_path, cmd = self._stacker_queue.get(timeout=2)
            except _queue.Empty:
                continue
            try:
                log_dir = Path(self.cfg.get('log_path') or str(Path.home() / 'logs'))
                log_dir.mkdir(parents=True, exist_ok=True)
                stacker_log = log_dir / 'stacker.log'
                stacker_cfg = self.cfg.get('services', {}).get('stacker', {})
                stacker_cfg = stacker_cfg if isinstance(stacker_cfg, dict) else {}
                nice_val = int(stacker_cfg.get('nice', 19))

                def _apply_nice(n: int = nice_val) -> None:
                    try:
                        os.nice(n)
                    except Exception:
                        pass
                    try:
                        subprocess.run(
                            ['ionice', '-c', '3', '-p', str(os.getpid())],
                            capture_output=True, timeout=5,
                        )
                    except Exception:
                        pass

                with open(stacker_log, 'a') as lf:
                    proc = subprocess.Popen(cmd, stdout=lf, stderr=lf,
                                            preexec_fn=_apply_nice)
                    try:
                        proc.wait(timeout=120)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                        self.log.warning('Stacker timed out for %s — killed', chunk_path.name)
                self.log.debug('Stacker finished for %s', chunk_path.name)
            except Exception as e:
                self.log.warning('Stacker failed for %s: %s', chunk_path.name, e)
            finally:
                with self._stacker_lock:
                    self._stacker_queued.discard(chunk_path.name)
                self._stacker_queue.task_done()

    # ------------------------------------------------------------------
    # Chunk ready watcher — replaces .ready sidecar with flags_manager
    # ------------------------------------------------------------------

    def _chunk_ready_watcher(self) -> None:
        """Watch capture_path for closed MKV segments; call flags_manager.mark_ready()
        and enqueue the chunk for real-time stacking.

        Uses a hand-rolled recursive watch (Inotify + os.walk) instead of
        InotifyTree so a single unreadable subdir (e.g. EIO from NTFS index
        corruption) only loses that subtree instead of crashing the whole
        watcher in a restart loop.
        """
        if not INOTIFY_AVAILABLE:
            self.log.warning('inotify not available — chunk-ready marking disabled')
            return
        backoff = 5
        while self._running:
            try:
                inot = inotify.adapters.Inotify()

                def _onwalkerr(exc: OSError) -> None:
                    self.log.warning(
                        'chunk-ready: skipping unreadable %s: %s',
                        getattr(exc, 'filename', '?'), exc,
                    )

                watched = 0
                for root, _dirs, _files in os.walk(
                    str(self.capture_path), onerror=_onwalkerr,
                ):
                    try:
                        inot.add_watch(root)
                        watched += 1
                    except Exception as e:
                        self.log.warning(
                            'chunk-ready: add_watch failed for %s: %s', root, e,
                        )
                self.log.info(
                    'Chunk-ready watcher watching: %s (%d dirs)',
                    self.capture_path, watched,
                )
                backoff = 5
                for event in inot.event_gen(yield_nones=True):
                    if not self._running:
                        return
                    if event is None:
                        continue
                    _, type_names, path, filename = event
                    # Auto-watch newly-created subdirectories so this loop
                    # behaves like InotifyTree did, including new YYYYMMDD
                    # date dirs created at midnight.
                    if 'IN_ISDIR' in type_names and 'IN_CREATE' in type_names:
                        new_dir = os.path.join(path, filename)
                        try:
                            inot.add_watch(new_dir)
                            self.log.debug(
                                'chunk-ready: added watch on new dir %s', new_dir,
                            )
                        except Exception as e:
                            self.log.warning(
                                'chunk-ready: add_watch failed for %s: %s',
                                new_dir, e,
                            )
                        continue
                    if 'IN_CLOSE_WRITE' not in type_names:
                        continue
                    if not filename.endswith('_color.mkv'):
                        continue
                    m = _CHUNK_NAME_RE.match(filename)
                    if not m:
                        continue
                    station_id = m.group(1)
                    date_str, time_str = m.group(2), m.group(3)
                    # Night date: before noon UTC → previous day's folder
                    if int(time_str[:2]) < 12:
                        nd = (datetime(int(date_str[:4]), int(date_str[4:6]),
                                       int(date_str[6:]))
                              - timedelta(days=1))
                        night_str = nd.strftime('%Y%m%d')
                    else:
                        night_str = date_str
                    try:
                        flags_manager.mark_ready(station_id, night_str, filename, self.cfg)
                        self.log.debug('flags_manager.mark_ready: %s', filename)
                    except Exception as e:
                        self.log.warning('mark_ready failed for %s: %s', filename, e)
                        continue

                    # Kill any stale dawn_process — capturing has started so
                    # morning processing is no longer safe to continue.
                    try:
                        import signal as _signal
                        r = subprocess.run(['pgrep', '-f', 'dawn_process.py'],
                                           capture_output=True, text=True)
                        for pid in r.stdout.split():
                            try:
                                os.kill(int(pid), _signal.SIGTERM)
                                self.log.info('Sent SIGTERM to stale dawn_process pid %s', pid)
                            except ProcessLookupError:
                                pass
                    except Exception as e:
                        self.log.debug('dawn_process check failed: %s', e)

                    # Enqueue for real-time stacking only if both stacker and realtime are enabled.
                    # If stacker is enabled but realtime is not, stacking happens in the morning sweep.
                    svc_stacker = self.cfg.get('services', {}).get('stacker', {})
                    stacker_cfg = svc_stacker if isinstance(svc_stacker, dict) else {}
                    if not stacker_cfg.get('enabled', True) or not stacker_cfg.get('realtime', True):
                        continue

                    # Deduplicated so a watcher restart cannot double-queue the same chunk.
                    mkv = Path(path) / filename
                    cmd = [sys.executable,
                           str(Path(__file__).parent / 'stacker.py'),
                           '--chunk', str(mkv),
                           '-c', self._config_path]
                    with self._stacker_lock:
                        if filename not in self._stacker_queued:
                            self._stacker_queued.add(filename)
                            self._stacker_queue.put((station_id, mkv, cmd))
                            self.log.debug(
                                'Queued stacker for %s (queue depth %d)',
                                filename, self._stacker_queue.qsize(),
                            )
            except Exception:
                self.log.exception('Chunk-ready watcher crashed — restarting in %ds', backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def run(self):
        """Run the capture daemon."""
        self.log.info('Color capture daemon starting')
        self.log.info('Stations: %s', list(self.cfg['stations'].keys()))
        self.log.info('Segment duration: %ds', self.segment_duration)
        self.log.info('Idle timeout: %d min, min disk: %d GB',
                      self.idle_timeout // 60, self.min_disk_gb)
        self.log.info('capture_path=%s  stacker_workers=%d',
                      self.capture_path, self._stacker_workers)
        if self.color_tee:
            self.log.info('TEE MODE enabled — will link RMS raw video segments instead of spawning ffmpeg')
            for sid in self.cfg['stations']:
                rms_vid = self._rms_video_path(sid)
                if not rms_vid.exists():
                    self.log.warning(
                        'color_tee: RMS video dir %s does not exist for %s — '
                        'ensure raw_video_save=True and raw_video_duration=20 in the RMS .config',
                        rms_vid, sid,
                    )

        self._running = True

        def _signal_handler(sig, frame):
            self.log.info('Signal %s received, shutting down', sig)
            self._running = False

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        for i in range(self._stacker_workers):
            threading.Thread(
                target=self._stacker_worker,
                name=f'stacker-worker-{i}',
                daemon=True,
            ).start()

        threading.Thread(target=self._chunk_ready_watcher, name='chunk-ready-watcher', daemon=True).start()

        try:
            self._watch_ff_files()
        except Exception as e:
            self.log.error('Fatal error: %s', e, exc_info=True)
        finally:
            self._stop_all_ffmpeg()
            self.log.info('Color capture daemon stopped')


def main():
    parser = argparse.ArgumentParser(description='Color video capture daemon for RMS')
    parser.add_argument('-c', '--config', required=True, help='Path to config JSON')
    parser.add_argument('--test-duration', type=int, default=None,
                        help='Run for N seconds then exit (testing)')
    args = parser.parse_args()

    daemon = ColorCapture(args.config, test_duration=args.test_duration)
    daemon.run()


if __name__ == '__main__':
    main()
