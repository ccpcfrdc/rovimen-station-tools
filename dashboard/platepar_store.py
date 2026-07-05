"""Persistent, process-wide platepar cache.

Platepars are fetched live from each station's /api/platepar endpoint by
rovimen_dashboard.py.  This module keeps a disk-backed copy keyed by camera
code so that coverage can be computed even for stations that are currently
offline — we use the last-known platepar.

Storage: ROVIMEN_PLATEPAR_STORE env var, default /opt/rovimen/platepar_store.json
Schema:  { "<CAM_CODE>": { "lat": …, "lon": …, "az_centre": …, … } }
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_store: dict[str, dict] = {}

STORE_PATH = Path(os.environ.get("ROVIMEN_PLATEPAR_STORE", "/opt/rovimen/platepar_store.json"))

_REQUIRED_FIELDS = {"lat", "lon", "az_centre", "alt_centre", "fov_h", "fov_v"}

_OPTIONAL_FIELDS = {
    "rotation_from_horiz", "F_scale", "X_res", "Y_res",
    "x_poly_fwd", "x_poly", "y_poly_fwd", "y_poly",
    "equal_aspect", "asymmetry_corr", "distortion_type",
    "RA_d", "dec_d", "pos_angle_ref",
}


def load() -> None:
    """Load store from disk.  Called once at import time."""
    global _store
    if not STORE_PATH.exists():
        return
    try:
        with _lock:
            _store = json.loads(STORE_PATH.read_text())
        logger.info("platepar_store: loaded %d cameras from %s: %s",
                    len(_store), STORE_PATH, ", ".join(sorted(_store.keys())))
    except Exception as exc:
        logger.warning("platepar_store: failed to load %s: %s", STORE_PATH, exc)


def update(cam_code: str, data: dict) -> None:
    """Persist one camera's platepar.  Silently skips incomplete entries."""
    if not _REQUIRED_FIELDS.issubset(data):
        return
    with _lock:
        entry = {k: data[k] for k in _REQUIRED_FIELDS}
        for k in _OPTIONAL_FIELDS:
            if k in data and data[k] is not None:
                entry[k] = data[k]
        _store[cam_code] = entry
        _write_locked()


def update_host(host_payload: dict) -> None:
    """Persist all cameras from a full /api/platepar host response."""
    changed = False
    skipped = []
    with _lock:
        for cam_code, pp in host_payload.items():
            if isinstance(pp, dict) and _REQUIRED_FIELDS.issubset(pp):
                entry = {k: pp[k] for k in _REQUIRED_FIELDS}
                for k in _OPTIONAL_FIELDS:
                    if k in pp and pp[k] is not None:
                        entry[k] = pp[k]
                _store[cam_code] = entry
                changed = True
            else:
                skipped.append(cam_code)
        if changed:
            _write_locked()
    if skipped:
        logger.info("platepar_store: skipped %s (missing required fields or error)", ", ".join(skipped))
    if changed:
        logger.info("platepar_store: updated, now %d cameras total", len(_store))
        _notify_change()


_change_callbacks: list = []


def on_change(callback) -> None:
    _change_callbacks.append(callback)


def _notify_change() -> None:
    for cb in _change_callbacks:
        try:
            cb()
        except Exception:
            pass


def get_all() -> dict[str, dict]:
    with _lock:
        return dict(_store)


def _write_locked() -> None:
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_store, indent=2))
        tmp.replace(STORE_PATH)
    except Exception as exc:
        logger.warning("platepar_store: write failed: %s", exc)


load()
