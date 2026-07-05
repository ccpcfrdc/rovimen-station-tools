"""Atomic JSON store for compilation manifests.

A compilation is a user-curated list of meteor clips that get fetched, ffmpeg-
concat'd, optionally re-encoded with an intro, and (in Phase 2) uploaded to
YouTube. Each manifest carries the cart contents, build progress, output path,
and YouTube metadata. The whole list lives in a single JSON file written
atomically (tmp + os.replace) — same pattern as `flags_manager.save`.

This module is intentionally tiny: no DB, no migrations, no schema validation.
The dashboard validates incoming payloads before calling `upsert`.

Concurrency: a process-wide threading.Lock guards same-process races; an
fcntl.flock on a sidecar file guards races against any future process that
might touch the file (e.g. a future cron-triggered scheduler running in a
separate Python interpreter).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# Resolved at module import; the dashboard reads ROVIMEN_COMPILATIONS_PATH if
# the operator wants it elsewhere (e.g. on dev: /opt/rovimen-dev/compilations.json).
_DEFAULT_PATH = Path("/opt/rovimen/compilations.json")
_PATH = Path(os.environ.get("ROVIMEN_COMPILATIONS_PATH", str(_DEFAULT_PATH)))
_LOCK_PATH = Path(f"/tmp/rovimen_compilations_{_PATH.name}.lock")

_thread_lock = threading.Lock()


@contextmanager
def _locked() -> Iterator[None]:
    """Combined thread-lock + file-lock for any read-modify-write."""
    with _thread_lock:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOCK_PATH, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            yield


def _read_unlocked() -> list[dict]:
    if not _PATH.exists():
        return []
    try:
        data = json.loads(_PATH.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.error("Corrupt compilations file %s: %s", _PATH, exc)
        backup = _PATH.with_suffix(f".corrupt.{int(time.time())}")
        try:
            shutil.copy2(_PATH, backup)
        except OSError:
            pass
        return []
    if not isinstance(data, list):
        logger.warning("compilations.json wrong shape (%s) — starting from empty list", type(data).__name__)
        return []
    return data


def _write_unlocked(items: list[dict]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_name(_PATH.name + ".tmp")
    tmp.write_text(json.dumps(items, indent=2))
    os.replace(tmp, _PATH)


def load_all() -> list[dict]:
    """Return all manifests, newest first by created_at (best-effort sort)."""
    with _locked():
        items = _read_unlocked()
    items.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return items


def get(manifest_id: str) -> dict | None:
    """Return one manifest or None."""
    with _locked():
        for m in _read_unlocked():
            if m.get("id") == manifest_id:
                return m
    return None


def upsert(manifest: dict) -> dict:
    """Insert or replace by `id`. Returns the stored manifest."""
    mid = manifest.get("id")
    if not mid:
        raise ValueError("manifest must have an 'id'")
    with _locked():
        items = _read_unlocked()
        for i, existing in enumerate(items):
            if existing.get("id") == mid:
                items[i] = manifest
                break
        else:
            items.append(manifest)
        _write_unlocked(items)
    return manifest


def patch(manifest_id: str, fields: dict) -> dict | None:
    """Shallow-merge fields into the stored manifest. Returns updated record
    or None if not found. Intended for partial updates from the build thread
    (progress, status) without clobbering concurrent user edits to other
    fields."""
    with _locked():
        items = _read_unlocked()
        for i, existing in enumerate(items):
            if existing.get("id") == manifest_id:
                merged = {**existing, **fields}
                items[i] = merged
                _write_unlocked(items)
                return merged
    return None


def delete(manifest_id: str) -> bool:
    """Remove by id. Returns True if anything was removed."""
    with _locked():
        items = _read_unlocked()
        before = len(items)
        items = [m for m in items if m.get("id") != manifest_id]
        if len(items) == before:
            return False
        _write_unlocked(items)
    return True
