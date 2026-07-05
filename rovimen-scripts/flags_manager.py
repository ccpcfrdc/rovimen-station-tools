#!/usr/bin/env python3
"""flags_manager.py — Central state.json manager for the ROVIMEN pipeline.

Replaces all sidecar files (.ready, .stacked, .reencoded, .locked)
with a single JSON state file per night per station.

Location: {videocapture_path}/{station_id}/{YYYYMMDD}/state.json

All writes are atomic (write to .tmp then os.replace). Per-night locks
prevent concurrent corruption: threading.Lock for same-process thread safety,
fcntl.flock for cross-process safety (color_capture, detection_lock, dawn_process,
station_api all run as separate processes).

Schema
------
{
  "station": "RO000H",
  "date": "20260322",
  "chunks": {
    "RO000H_20260322_210000_color.mkv": {
      "ready": true,
      "stacked": false,
      "reencoded": false,
      "lock": null           // or {"lock_type":..., "detection_time":..., "meteor_time":...}
    }
  },
  "rms_complete": false,
  "morning_done": false      // true once the full morning sequence has completed
}
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)
_unreadable_warned: set[str] = set()

# Per-night threading locks to prevent concurrent state corruption.
# Keyed by "station_id/date".
_locks: dict[str, threading.Lock] = {}
_locks_meta = threading.Lock()


def _get_lock(station_id: str, date_str: str) -> threading.Lock:
    key = f'{station_id}/{date_str}'
    with _locks_meta:
        if key not in _locks:
            # Evict stale entries to prevent unbounded growth in long-running daemons.
            # Clearing is safe: each returned Lock object is independent; callers
            # holding one are unaffected.  Cross-process safety comes from fcntl.
            if len(_locks) > 60:
                _locks.clear()
            _locks[key] = threading.Lock()
        return _locks[key]


@contextmanager
def _lock(station_id: str, date_str: str, cfg: dict):
    """Acquire thread + file lock for a load→modify→save sequence.

    threading.Lock prevents races between threads in the same process.
    fcntl.flock prevents races between separate processes (color_capture,
    detection_lock, dawn_process, station_api all run independently).

    Lock file lives in /tmp so it never needs the night dir to exist first.
    """
    with _get_lock(station_id, date_str):
        lock_path = Path(f'/tmp/rovimen_state_{station_id}_{date_str}.lock')
        with open(lock_path, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            yield


def _state_path(station_id: str, date_str: str, cfg: dict) -> Path:
    root = Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(Path.home() / 'color_capture')
    )
    return root / station_id / date_str / 'state.json'


def _empty_state(station_id: str, date_str: str) -> dict:
    return {
        'station': station_id,
        'date': date_str,
        'chunks': {},
        'rms_complete': False,
        'morning_done': False,
        'timelapse_done': False,
        'timelapse_deferred': False,
        'timelapse_uploaded': False,
        'night_stack_uploaded': False,
    }


def _empty_chunk() -> dict:
    return {
        'ready': False, 'stacked': False, 'reencoded': False, 'lock': None,
        'uploaded': False, 'stack_uploaded': False,
    }


# ---------------------------------------------------------------------------
# Core load / save
# ---------------------------------------------------------------------------

def load(station_id: str, date_str: str, cfg: dict) -> dict:
    """Load state.json.  Returns an empty state dict if the file is missing."""
    path = _state_path(station_id, date_str, cfg)
    try:
        if not path.exists():
            return _empty_state(station_id, date_str)
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        key = f'{station_id}/{date_str}'
        if key not in _unreadable_warned:
            _unreadable_warned.add(key)
            logger.warning('[%s/%s] state.json unreadable — using empty state', station_id, date_str)
        return _empty_state(station_id, date_str)


def save(state: dict, station_id: str, date_str: str, cfg: dict) -> None:
    """Atomically write state.json (tmp file + os.replace)."""
    path = _state_path(station_id, date_str, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('state.json.tmp')
    try:
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, path)
    except OSError as exc:
        key = f'{station_id}/{date_str}'
        if key not in _unreadable_warned:
            _unreadable_warned.add(key)
            logger.error('[%s/%s] state.json write failed (I/O error on disk?): %s',
                         station_id, date_str, exc)
        raise


# ---------------------------------------------------------------------------
# Chunk-level updates
# ---------------------------------------------------------------------------

def mark_ready(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    """Mark a chunk as ready (ffmpeg segment closed)."""
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['chunks'].setdefault(chunk_name, _empty_chunk())['ready'] = True
        save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Ready: %s', station_id, date_str, chunk_name)


def mark_ready_batch(station_id: str, date_str: str, chunk_names: list[str], cfg: dict) -> None:
    """Mark multiple chunks as ready in a single load-modify-save cycle."""
    if not chunk_names:
        return
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        for name in chunk_names:
            state['chunks'].setdefault(name, _empty_chunk())['ready'] = True
        save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Batch-ready: %d chunk(s)', station_id, date_str, len(chunk_names))


def lock_chunk(station_id: str, date_str: str, chunk_name: str,
               lock_info: dict, cfg: dict) -> None:
    """Set the lock field on a chunk (real-time detection lock)."""
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        chunk = state['chunks'].setdefault(chunk_name, _empty_chunk())
        chunk['ready'] = True  # if not already marked
        chunk['lock'] = lock_info
        save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Locked chunk: %s', station_id, date_str, chunk_name)


def unlock_chunk(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    """Clear the lock field on a chunk (false positive removal)."""
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        if chunk_name in state['chunks']:
            state['chunks'][chunk_name]['lock'] = None
            save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Unlocked chunk: %s', station_id, date_str, chunk_name)


def relock_chunk(station_id: str, date_str: str, chunk_name: str,
                 lock_info: dict, cfg: dict) -> None:
    """Replace the lock field with updated metadata (EON sub-second precision pass)."""
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        chunk = state['chunks'].setdefault(chunk_name, _empty_chunk())
        chunk['ready'] = True
        chunk['lock'] = lock_info
        save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Relocked chunk: %s', station_id, date_str, chunk_name)


def mark_stacked(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['chunks'].setdefault(chunk_name, _empty_chunk())['stacked'] = True
        save(state, station_id, date_str, cfg)


def mark_reencoded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['chunks'].setdefault(chunk_name, _empty_chunk())['reencoded'] = True
        save(state, station_id, date_str, cfg)


# ---------------------------------------------------------------------------
# Night-level updates
# ---------------------------------------------------------------------------

def mark_rms_complete(station_id: str, date_str: str, cfg: dict) -> None:
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['rms_complete'] = True
        save(state, station_id, date_str, cfg)
    logger.info('[%s/%s] RMS marked complete', station_id, date_str)


# ---------------------------------------------------------------------------
# Night completion
# ---------------------------------------------------------------------------

def mark_morning_done(station_id: str, date_str: str, cfg: dict) -> None:
    """Mark the morning sequence as fully complete for this night."""
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['morning_done'] = True
        save(state, station_id, date_str, cfg)
    logger.info('[%s/%s] Morning sequence marked done', station_id, date_str)


def is_morning_done(station_id: str, date_str: str, cfg: dict) -> bool:
    """Return True if the morning sequence has already completed for this night."""
    state = load(station_id, date_str, cfg)
    return bool(state.get('morning_done', False))


def mark_chunk_uploaded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    """Mark a chunk's MKV as uploaded to the archive."""
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['chunks'].setdefault(chunk_name, _empty_chunk())['uploaded'] = True
        save(state, station_id, date_str, cfg)


def is_chunk_uploaded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> bool:
    state = load(station_id, date_str, cfg)
    return bool(state.get('chunks', {}).get(chunk_name, {}).get('uploaded', False))


def unmark_chunk_uploaded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    """Clear uploaded + stack_uploaded flags (e.g. after remote deletion on unlock)."""
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        chunk = state.get('chunks', {}).get(chunk_name)
        if chunk is not None:
            chunk['uploaded'] = False
            chunk['stack_uploaded'] = False
            save(state, station_id, date_str, cfg)


def mark_chunk_stack_uploaded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    """Mark a chunk's detection stack WebP as uploaded to the archive."""
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['chunks'].setdefault(chunk_name, _empty_chunk())['stack_uploaded'] = True
        save(state, station_id, date_str, cfg)


def is_chunk_stack_uploaded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> bool:
    state = load(station_id, date_str, cfg)
    return bool(state.get('chunks', {}).get(chunk_name, {}).get('stack_uploaded', False))


def mark_timelapse_uploaded(station_id: str, date_str: str, cfg: dict) -> None:
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['timelapse_uploaded'] = True
        save(state, station_id, date_str, cfg)


def is_timelapse_uploaded(station_id: str, date_str: str, cfg: dict) -> bool:
    state = load(station_id, date_str, cfg)
    return bool(state.get('timelapse_uploaded', False))


def mark_night_stack_uploaded(station_id: str, date_str: str, cfg: dict) -> None:
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['night_stack_uploaded'] = True
        save(state, station_id, date_str, cfg)


def is_night_stack_uploaded(station_id: str, date_str: str, cfg: dict) -> bool:
    state = load(station_id, date_str, cfg)
    return bool(state.get('night_stack_uploaded', False))


def mark_timelapse_done(station_id: str, date_str: str, cfg: dict) -> None:
    """Mark timelapse as done for this night (built, skipped, or disabled)."""
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['timelapse_done'] = True
        state['timelapse_deferred'] = False
        save(state, station_id, date_str, cfg)
    logger.info('[%s/%s] Timelapse marked done', station_id, date_str)


def is_timelapse_done(station_id: str, date_str: str, cfg: dict) -> bool:
    """Return True if timelapse has been built or was not applicable for this night."""
    state = load(station_id, date_str, cfg)
    return bool(state.get('timelapse_done', False))


def mark_timelapse_deferred(station_id: str, date_str: str, cfg: dict) -> None:
    """Mark timelapse as deferred — stack coverage was insufficient at dawn."""
    with _lock(station_id, date_str, cfg):
        state = load(station_id, date_str, cfg)
        state['timelapse_deferred'] = True
        save(state, station_id, date_str, cfg)
    logger.warning('[%s/%s] Timelapse deferred (stack coverage too low)', station_id, date_str)


def is_timelapse_deferred(station_id: str, date_str: str, cfg: dict) -> bool:
    """Return True if timelapse was skipped due to insufficient stack coverage."""
    state = load(station_id, date_str, cfg)
    return bool(state.get('timelapse_deferred', False))
