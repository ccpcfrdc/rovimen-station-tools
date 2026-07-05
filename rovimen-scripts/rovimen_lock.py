#!/usr/bin/env python3
"""rovimen_lock.py — MKV locking via JSON sidecar files.

Lock file: <mkv>.locked
Format: {"lock_type": "detection", "detection_time": "20260314_023422"}

Four functions: lock / unlock / is_locked / get_lock_info
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + '.locked')


def lock(path: Path, lock_type: str, detection_time: datetime | None = None,
         meteor_time: datetime | None = None) -> None:
    """Write a JSON .locked sidecar.  No-op if sidecar already exists.

    detection_time: FF block start time (second precision) — legacy field, kept for compat.
    meteor_time:    precise meteor timestamp from FTPdetectinfo (sub-second ISO string).
                    Set during real-time lock (second precision) and updated by EON pass
                    with sub-second precision from FTPdetectinfo.
    """
    lp = _lock_path(path)
    if lp.exists():
        return
    metadata: dict = {
        'lock_type': lock_type,
        'detection_time': detection_time.strftime('%Y%m%d_%H%M%S') if detection_time else None,
    }
    if meteor_time is not None:
        metadata['meteor_time'] = meteor_time.isoformat()
    tmp = lp.with_name(lp.name + '.tmp')
    tmp.write_text(json.dumps(metadata))
    tmp.rename(lp)  # atomic on same filesystem


def relock(path: Path, lock_type: str, detection_time: datetime | None = None,
           meteor_time: datetime | None = None) -> None:
    """Remove existing sidecar and write a new one with updated metadata."""
    unlock(path)
    lock(path, lock_type, detection_time, meteor_time)


def unlock(path: Path) -> None:
    """Remove the .locked sidecar if present."""
    _lock_path(path).unlink(missing_ok=True)


def is_locked(path: Path) -> bool:
    return _lock_path(path).exists()


def get_lock_info(path: Path) -> dict | None:
    """Return {'lock_type': ..., 'detection_time': ...} or None if not locked."""
    lp = _lock_path(path)
    if not lp.exists():
        return None
    try:
        data = json.loads(lp.read_text())
        return {
            'lock_type': data.get('lock_type'),
            'detection_time': data.get('detection_time'),
            'meteor_time': data.get('meteor_time'),
        }
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def get_lock_type(path: Path) -> str | None:
    """Return lock_type string or None if not locked.  Compat shim for station API."""
    info = get_lock_info(path)
    return info.get('lock_type') if info else None
