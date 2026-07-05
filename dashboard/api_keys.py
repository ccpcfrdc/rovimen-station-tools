"""API key store for the public API.

Manual provisioning model: an operator runs ``tools/mint_api_key.py`` to
mint a 256-bit secret, the secret hash is appended to ``api_keys.yaml``
on the VPS, the operator hands the plaintext secret to the consumer
once, and from then on every request to ``/api/public/v1/*`` must carry
that secret as ``X-API-Key: <secret>`` (or ``Authorization: Bearer
<secret>``).

Storage shape (``api_keys.yaml``) — current (hashed) format:

    keys:
      - id: astromania-prod
        secret_hash: "pbkdf2:sha256:600000$...."   # werkzeug password hash
        label: "astromania.org WordPress plugin"
        created_at: "2026-05-24T13:30:00Z"
        disabled: false
        rate_limit_override: null

Storage shape (legacy, plaintext) — accepted on read for backward
compatibility, migrated to hashed form on first successful verify:

    keys:
      - id: astromania-prod
        secret: 9f7a2b91c4e3d6f5...    # 64 hex chars (256-bit random)
        ...

We hash secrets at rest with ``werkzeug.security.generate_password_hash``
for consistency with the dashboard user-auth side. The hashes are slow
by design (pbkdf2 600k iterations), so an in-process LRU short-circuits
repeat verifications.

Writes go through an exclusive ``fcntl.flock`` on a sidecar ``.lock``
file plus a same-process ``threading.Lock``, write-to-tmp + ``os.replace``
for atomicity — matching the ``_save_config`` / ``_save_users`` idiom in
``rovimen_dashboard.py``.
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
    os.environ.get("ROVIMEN_API_KEYS_PATH", "/opt/rovimen/api_keys.yaml")
)
# When set to "1" (the production default), every public JSON endpoint
# requires a valid key. When "0", the requirement is bypassed — useful
# for local development and integration tests where minting a key adds
# friction with no value. NEVER set this to "0" on a public-facing
# deployment.
REQUIRE_KEY_ENV = "ROVIMEN_API_KEYS_REQUIRED"


class ApiKey(BaseModel):
    """One row in the keys file.

    Either ``secret_hash`` (preferred, werkzeug pbkdf2 hash) or
    ``secret`` (legacy plaintext, migrated on read) is populated. New
    rows minted by :func:`add_key` only set ``secret_hash``.
    """

    id: str
    # New rows: hashed form only. Legacy rows may still carry a plaintext
    # ``secret`` until the first successful verify migrates them.
    secret_hash: str = ""
    secret: str = ""
    label: str = ""
    created_at: str = ""
    disabled: bool = False
    # Optional per-key Flask-Limiter override (string in the
    # ``"N/period;M/period"`` shape). When None the route's default
    # bucket applies — but keyed against the key id, not the IP, so
    # legitimate consumers get their own quota independent of where
    # their requests originate.
    rate_limit_override: str | None = None


class _KeysFile(BaseModel):
    keys: list[ApiKey] = Field(default_factory=list)


# ── Cache ──────────────────────────────────────────────────────────
# Re-reading + parsing the YAML on every request is wasteful. We keep
# an mtime-keyed cache so a fresh-minted key (which bumps the file's
# mtime) is picked up on the next request without a service restart.
_cache_lock = threading.Lock()
# Tuple shape: (mtime, by_id) — by_id is the only O(1) lookup we can
# do up-front, since the lookup-by-secret path has to iterate hashed
# rows and call check_password_hash.
_cache: tuple[float, dict[str, ApiKey]] | None = None

# Same-process serialisation for writers (load → mutate → save):
# pairs with the on-disk fcntl.flock for cross-process safety.
_write_lock = threading.Lock()


# ── Verify-cache ─────────────────────────────────────────────────
# check_password_hash is intentionally slow (pbkdf2 600k iterations).
# Re-hashing on every request would make even modest traffic CPU-bound.
# Keep a small LRU keyed by (hash, plaintext) so that once a consumer
# has been authenticated, subsequent calls are O(1). Bound at 256
# entries — a handful of consumers is the realistic ceiling.
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
        logger.error("api_keys: failed to load %s: %s", path, exc)
        return _KeysFile()


def _refresh_cache(path: Path) -> dict[str, ApiKey]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    parsed = _load_raw(path)
    by_id: dict[str, ApiKey] = {}
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


def _get_indexes(path: Path) -> dict[str, ApiKey]:
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


def _atomic_write(path: Path, parsed: _KeysFile) -> None:
    """Atomic, locked write of the keys file.

    Mirrors the ``_save_config`` / ``_save_users`` pattern in
    ``rovimen_dashboard.py``: same-process ``threading.Lock`` +
    ``fcntl.flock`` sidecar + write-to-tmp + ``os.replace``.
    """
    payload = yaml.safe_dump(
        parsed.model_dump(), sort_keys=False, allow_unicode=True
    )
    lock_path = _lock_path(path)
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(payload)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            logger.warning("api_keys: could not chmod 0600 on %s", path)
    _invalidate_cache()


def _migrate_plaintext_to_hash(path: Path, key_id: str, plaintext: str) -> None:
    """Rewrite the row for ``key_id`` to drop ``secret`` and store
    ``secret_hash``. Idempotent — if the row is already migrated this
    is a no-op."""
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
                payload = yaml.safe_dump(
                    parsed.model_dump(),
                    sort_keys=False,
                    allow_unicode=True,
                )
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(payload)
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        _invalidate_cache()
        logger.info("api_keys: migrated %r from plaintext to hashed", key_id)
    except Exception:
        logger.exception("api_keys: migration to hash failed for %r", key_id)


def validate(secret: str, path: Path = DEFAULT_KEYS_PATH) -> ApiKey | None:
    """Return the enabled ApiKey matching ``secret``, or None.

    Verification walks the in-memory id index, comparing the candidate
    against each enabled key's hash (cached). Legacy plaintext rows are
    accepted once on a successful match and rewritten to hashed form on
    the spot, so existing consumers keep working through the rollover.
    """
    if not secret:
        return None
    by_id = _get_indexes(path)
    for key in by_id.values():
        if key.secret_hash:
            if _verify_cached(key.secret_hash, secret):
                return key
        elif key.secret:
            # Legacy plaintext row — constant-time compare, then
            # opportunistically migrate to a hash for future requests.
            if secrets.compare_digest(key.secret, secret):
                _migrate_plaintext_to_hash(path, key.id, secret)
                # Return a fresh copy reflecting the migrated state so
                # the caller doesn't see a stale plaintext field.
                refreshed = _get_indexes(path).get(key.id)
                return refreshed or key
    return None


def is_required() -> bool:
    """Whether public JSON endpoints should reject unkeyed requests.

    Defaults to True — production-safe. Operators set
    ``ROVIMEN_API_KEYS_REQUIRED=0`` on local / dev boxes only.
    """
    return os.environ.get(REQUIRE_KEY_ENV, "1") != "0"


# ── Provisioning ───────────────────────────────────────────────────


def mint_secret() -> str:
    """256 bits of randomness, hex-encoded. Same shape as the session
    signing key (see ``rovimen_dashboard._load_or_create_secret_key``)."""
    return secrets.token_hex(32)


def add_key(
    id: str,
    label: str = "",
    *,
    path: Path = DEFAULT_KEYS_PATH,
    rate_limit_override: str | None = None,
) -> tuple[ApiKey, str]:
    """Mint a new key, persist it, return ``(ApiKey, plaintext_secret)``.

    Caller is responsible for showing the plaintext exactly once — it is
    not stored anywhere after this function returns.

    Raises ValueError on duplicate id. Idempotent on missing parent
    directory (creates it) but not on missing file (creates it too).
    """
    with _write_lock:
        lock_path = _lock_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            parsed = _load_raw(path)
            if any(k.id == id for k in parsed.keys):
                raise ValueError(f"api key id already exists: {id!r}")

            plaintext = mint_secret()
            new = ApiKey(
                id=id,
                secret_hash=generate_password_hash(plaintext),
                label=label,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                rate_limit_override=rate_limit_override,
            )
            parsed.keys.append(new)
            payload = yaml.safe_dump(
                parsed.model_dump(), sort_keys=False, allow_unicode=True
            )
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(payload)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            logger.warning("api_keys: could not chmod 0600 on %s", path)
    _invalidate_cache()
    return new, plaintext


def disable_key(id: str, *, path: Path = DEFAULT_KEYS_PATH) -> bool:
    """Mark a key disabled in-place. Returns True if found, False otherwise.

    Wrapped in the same flock + write-tmp + replace dance as
    :func:`add_key` so a concurrent ``add_key`` / ``disable_key`` /
    ``add_key`` sequence never races on the file.
    """
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
            payload = yaml.safe_dump(
                parsed.model_dump(), sort_keys=False, allow_unicode=True
            )
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(payload)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    _invalidate_cache()
    return True


def list_keys(path: Path = DEFAULT_KEYS_PATH) -> list[dict[str, Any]]:
    """Return a redacted listing — id / label / created_at / disabled,
    but NOT the secret. Safe to surface in an admin log line or a
    sysadmin-only API."""
    parsed = _load_raw(path)
    return [
        {
            "id": k.id,
            "label": k.label,
            "created_at": k.created_at,
            "disabled": k.disabled,
            "rate_limit_override": k.rate_limit_override,
        }
        for k in parsed.keys
    ]
