"""Per-station key store for the reversed-HTTP push ingest API.

Each station holds exactly one ingest key that authorises writing *its own*
telemetry (and nothing else) to ``/api/ingest/v1/<station>/*``. The key is bound
to a single ``host_key`` (``gmn0002``, ``gmnro10``, …) — the same identifier used
in ``dashboard_config.yaml``. A key for ``gmn0002`` cannot write ``gmnro10``'s
state; that binding is the property SSH-into-:7779 never had (see
``docs/reversed_http_push_design.md`` §2.2).

This mirrors ``api_keys.py`` (the public-API key store): 256-bit secrets minted
by an ops CLL (``tools/mint_station_key.py``), hashed at rest with werkzeug
pbkdf2, stored in ``station_keys.yaml`` (mode 0600), verified with a timing-safe
compare and short-circuited by an in-process LRU. The one structural difference
is the ``station`` field binding a key to its host_key.

Storage shape (``station_keys.yaml``)::

    keys:
      - id: gmn0002
        station: gmn0002
        secret_hash: "pbkdf2:sha256:600000$...."
        label: "Vaslui push agent"
        created_at: "2026-07-01T22:00:00Z"
        disabled: false
        rate_limit_override: null

Legacy plaintext ``secret`` rows are accepted on read and migrated to a hash on
first successful verify, exactly like ``api_keys.py``.
"""

from __future__ import annotations

import fcntl
import functools
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)


DEFAULT_KEYS_PATH = Path(
    os.environ.get("ROVIMEN_STATION_KEYS_PATH", "/opt/rovimen/station_keys.yaml")
)
# When "0", ingest key verification is bypassed — local dev / integration
# tests only. NEVER set to "0" on a deployment reachable from the internet.
REQUIRE_KEY_ENV = "ROVIMEN_STATION_KEYS_REQUIRED"

# Secret prefix so a leaked token is recognisable and greppable in logs.
SECRET_PREFIX = "rvmn"


class StationKey(BaseModel):
    """One row in the station keys file.

    ``station`` is the single ``host_key`` this key authorises. ``id`` defaults
    to the same value (one key per station), but is kept distinct so a station
    can be re-keyed (``gmn0002`` → ``gmn0002-2``) without losing the old row's
    audit trail — only the enabled row for a given ``station`` is honoured.
    """

    id: str
    station: str
    secret_hash: str = ""
    secret: str = ""
    label: str = ""
    created_at: str = ""
    disabled: bool = False
    rate_limit_override: str | None = None


class _KeysFile(BaseModel):
    keys: list[StationKey] = Field(default_factory=list)


# ── Cache ──────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
# Tuple shape: (mtime, by_id) — id index of enabled keys.
_cache: tuple[float, dict[str, StationKey]] | None = None
_write_lock = threading.Lock()


@functools.lru_cache(maxsize=256)
def _verify_cached(secret_hash: str, plaintext: str) -> bool:
    if not secret_hash or not plaintext:
        return False
    try:
        return check_password_hash(secret_hash, plaintext)
    except Exception:
        return False


def _load_raw(path: Path) -> _KeysFile:
    if not path.exists():
        return _KeysFile()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
        return _KeysFile.model_validate(raw)
    except Exception as exc:
        logger.error("station_keys: failed to load %s: %s", path, exc)
        return _KeysFile()


def _refresh_cache(path: Path) -> dict[str, StationKey]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    parsed = _load_raw(path)
    by_id: dict[str, StationKey] = {}
    for k in parsed.keys:
        if k.disabled:
            continue
        if not k.secret_hash and not k.secret:
            continue
        by_id[k.id] = k
    with _cache_lock:
        global _cache
        _cache = (mtime, by_id)
    return by_id


def _get_indexes(path: Path) -> dict[str, StationKey]:
    """Return ``by_id`` for the currently-enabled keys."""
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _cache_lock:
        if _cache and _cache[0] == mtime:
            return _cache[1]
    return _refresh_cache(path)


def _invalidate_cache() -> None:
    with _cache_lock:
        global _cache
        _cache = None
    _verify_cached.cache_clear()


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _write_file(path: Path, parsed: _KeysFile) -> None:
    """Serialise ``parsed`` to ``path`` inside an already-held flock context."""
    payload = yaml.safe_dump(parsed.model_dump(), sort_keys=False, allow_unicode=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _migrate_plaintext_to_hash(path: Path, key_id: str, plaintext: str) -> None:
    """Rewrite the row for ``key_id`` to drop ``secret`` and store
    ``secret_hash``. Idempotent."""
    try:
        with _write_lock:
            lock_path = _lock_path(path)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "a+") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                parsed = _load_raw(path)
                changed = False
                for k in parsed.keys:
                    if k.id != key_id:
                        continue
                    if k.secret and not k.secret_hash:
                        k.secret_hash = generate_password_hash(plaintext)
                        k.secret = ""
                        changed = True
                    break
                if not changed:
                    return
                _write_file(path, parsed)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        _invalidate_cache()
        logger.info("station_keys: migrated %r from plaintext to hashed", key_id)
    except Exception:
        logger.exception("station_keys: migration to hash failed for %r", key_id)


def validate(secret: str, path: Path = DEFAULT_KEYS_PATH) -> StationKey | None:
    """Return the enabled StationKey matching ``secret``, or None.

    Verification is timing-safe: legacy plaintext rows use
    ``secrets.compare_digest``; hashed rows use werkzeug's constant-time
    ``check_password_hash`` (cached). Nothing about a mismatch is disclosed.
    """
    if not secret:
        return None
    by_id = _get_indexes(path)
    for key in by_id.values():
        if key.secret_hash:
            if _verify_cached(key.secret_hash, secret):
                return key
        elif key.secret:
            if secrets.compare_digest(key.secret, secret):
                _migrate_plaintext_to_hash(path, key.id, secret)
                refreshed = _get_indexes(path).get(key.id)
                return refreshed or key
    return None


def authorizes(secret: str, station: str, path: Path = DEFAULT_KEYS_PATH) -> bool:
    """True iff ``secret`` is a valid enabled key bound to ``station``.

    This is the single authorisation check the ingest handler makes: a valid
    key that belongs to a *different* station returns False (the caller maps
    that to 403 — authenticated but wrong station — vs 401 for no/invalid key).
    """
    key = validate(secret, path=path)
    return key is not None and key.station == station


def is_required() -> bool:
    """Whether ingest endpoints should reject unkeyed requests. Defaults True."""
    return os.environ.get(REQUIRE_KEY_ENV, "1") != "0"


# ── Provisioning ───────────────────────────────────────────────────


def mint_secret(station: str) -> str:
    """A prefixed, station-tagged, 256-bit secret: ``rvmn_<station>_<hex>``.

    The prefix makes a leaked token recognisable; the station tag is advisory
    only — authorisation is decided by the stored ``station`` binding, never by
    parsing the token."""
    return f"{SECRET_PREFIX}_{station}_{secrets.token_hex(32)}"


def add_key(
    station: str,
    *,
    id: str | None = None,
    label: str = "",
    path: Path = DEFAULT_KEYS_PATH,
    rate_limit_override: str | None = None,
) -> tuple[StationKey, str]:
    """Mint a new key bound to ``station``, persist it, return
    ``(StationKey, plaintext_secret)``.

    ``id`` defaults to ``station``. Raises ValueError if an **enabled** key
    already exists for that id — disable the old one first (or pass a distinct
    ``id`` to rotate). The plaintext is returned once and never stored.
    """
    key_id = id or station
    with _write_lock:
        lock_path = _lock_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            parsed = _load_raw(path)
            for k in parsed.keys:
                if k.id == key_id and not k.disabled:
                    raise ValueError(f"station key id already exists: {key_id!r}")

            plaintext = mint_secret(station)
            new = StationKey(
                id=key_id,
                station=station,
                secret_hash=generate_password_hash(plaintext),
                label=label,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                rate_limit_override=rate_limit_override,
            )
            parsed.keys.append(new)
            _write_file(path, parsed)
        try:
            path.chmod(0o600)
        except OSError:
            logger.warning("station_keys: could not chmod 0600 on %s", path)
    _invalidate_cache()
    return new, plaintext


def disable_key(id: str, *, path: Path = DEFAULT_KEYS_PATH) -> bool:
    """Mark a key disabled in-place. Returns True if found."""
    with _write_lock:
        lock_path = _lock_path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            parsed = _load_raw(path)
            found = False
            for k in parsed.keys:
                if k.id == id:
                    k.disabled = True
                    found = True
            if not found:
                return False
            _write_file(path, parsed)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    _invalidate_cache()
    return True


def list_keys(path: Path = DEFAULT_KEYS_PATH) -> list[dict[str, Any]]:
    """Return a redacted listing — id / station / label / created_at / disabled,
    never the secret."""
    parsed = _load_raw(path)
    return [
        {
            "id": k.id,
            "station": k.station,
            "label": k.label,
            "created_at": k.created_at,
            "disabled": k.disabled,
            "rate_limit_override": k.rate_limit_override,
        }
        for k in parsed.keys
    ]
