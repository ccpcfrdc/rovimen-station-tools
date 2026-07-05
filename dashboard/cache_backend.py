"""Pluggable storage backend for the pushed status/vitals cache.

The dashboard's :class:`station_client.StationCache` holds the last-known
status and vitals for every station. Historically that state lived in two
per-process ``dict``s, which is correct for a single gunicorn worker but wrong
the moment we scale out: a heartbeat POST that lands on worker A would be
invisible to a read served by worker B (see
``docs/reversed_http_push_design.md`` §9 — "scales to 100 cameras").

This module factors the *storage* of that state out of ``StationCache`` behind
a tiny interface so it can be backed either by:

* :class:`InMemoryStationStateBackend` — the original per-process dicts, used
  when ``ROVIMEN_REDIS_URL`` is unset. Zero behaviour change for single-worker
  dev + tests.
* :class:`RedisStationStateBackend` — a shared Redis backend so every gunicorn
  worker reads and writes the *same* status/vitals, used when
  ``ROVIMEN_REDIS_URL`` is set.

The interface deliberately traffics only in plain ``dict``/``str`` — no redis
types leak out to :class:`StationCache` or its callers. ``StationCache`` keeps
owning the diff-signature suppression and the SSE fan-out; only the raw
last-value store moves here.

Note on the SSE fan-out: the pub/sub queues in ``StationCache`` remain
per-worker (a listener connected to worker A only receives events broadcast by
worker A). With a shared backend the *snapshot* a client gets on connect, and
every subsequent explicit read, is consistent across workers; live SSE deltas
are still per-worker best-effort. Cross-worker SSE fan-out (Redis pub/sub) is a
follow-on and is called out in the deploy notes — it is not required for
read/ingest consistency, which is what multi-worker correctness hinges on.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
import time
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Env var that opts the whole dashboard into a shared Redis backend. Unset =>
# in-process backend (current behaviour). Kept as a module constant so the
# rate limiter (security.init_limiter) and this module read the same name.
REDIS_URL_ENV = "ROVIMEN_REDIS_URL"

# Redis key prefix + TTL for pushed telemetry. A generous TTL lets a station
# that has gone quiet age out of the shared store rather than pinning stale
# state forever; reads still fall back to the durable station_state mirror.
_REDIS_PREFIX = os.environ.get("ROVIMEN_REDIS_PREFIX", "rovimen")
# 0 => no expiry. Telemetry is refreshed every 30-60 s per station, so an
# hour of TTL comfortably survives transient gaps while bounding staleness.
_REDIS_TTL_SECONDS = int(os.environ.get("ROVIMEN_REDIS_STATE_TTL", "3600"))
# Live thumbnails are pushed far more often (every 10-300 s per cam) and have
# zero archival value — the newest FF replaces the previous one. A short TTL
# lets a cam that stops pushing self-expire so a read falls back to the
# on-demand :7779 pull rather than serving a minutes-old frame indefinitely.
_REDIS_LIVE_THUMB_TTL_SECONDS = int(
    os.environ.get("ROVIMEN_REDIS_LIVE_THUMB_TTL", "300")
)


class StationStateBackend(Protocol):
    """Storage contract for the pushed status/vitals last-value store.

    All methods traffic in plain JSON-serialisable ``dict``s keyed by
    ``host_key``. Implementations must be safe to call from multiple threads.
    """

    def seq_get(self, host_key: str, kind: str) -> int:
        """Return the highest accepted ``seq`` for ``(host_key, kind)`` (0 if
        unseen). Used only to answer :func:`station_state.get_last_seq`; the
        accept/drop decision itself always goes through :meth:`seq_bump`."""
        ...

    def seq_bump(self, host_key: str, kind: str, seq: int) -> bool:
        """Atomic check-and-bump of the per-``(host_key, kind)`` monotonic seq.

        Returns ``True`` iff ``seq`` is strictly greater than the stored value
        (i.e. this push is *accepted* and the stored seq is advanced to
        ``seq``), and ``False`` when ``seq <= stored`` (a replay/older seq — the
        caller must drop). The check and the advance MUST be atomic so two
        concurrent pushes with the same seq can never both be accepted, and a
        stale seq can never regress a newer one.
        """
        ...

    def get_status(self, host_key: str) -> dict[str, Any] | None: ...

    def set_status(self, host_key: str, data: dict[str, Any]) -> None: ...

    def get_vitals(self, host_key: str) -> dict[str, Any] | None: ...

    def set_vitals(self, host_key: str, data: dict[str, Any]) -> None: ...

    def all_status(self, host_keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        """Return ``{host_key: status}`` for the given keys (missing => {})."""
        ...

    def set_live_thumb(
        self,
        host_key: str,
        cam: str,
        data: bytes,
        content_type: str,
        ff_timestamp: str | None,
    ) -> None:
        """Store the newest-FF maxpixel thumbnail for a ``(host_key, cam)``.

        Last-writer-wins per camera. The backend stamps ``received_at`` with
        ``time.time()`` on write so :meth:`get_live_thumb` mirrors the original
        in-memory envelope exactly.
        """
        ...

    def get_live_thumb(self, host_key: str, cam: str) -> dict[str, Any] | None:
        """Return the latest live thumbnail envelope for ``(host_key, cam)``.

        The envelope is ``{"data": bytes, "content_type": str,
        "ff_timestamp": str | None, "received_at": float}``, or ``None`` when
        no thumbnail has been stored (or it has expired).
        """
        ...


class InMemoryStationStateBackend:
    """Per-process backend — the original ``StationCache`` dict storage.

    This is the default when ``ROVIMEN_REDIS_URL`` is unset, so single-worker
    deployments and the test suite behave exactly as before.
    """

    def __init__(self) -> None:
        self._status: dict[str, dict[str, Any]] = {}
        self._vitals: dict[str, dict[str, Any]] = {}
        # Highest accepted seq per (host_key, kind) for replay-idempotency.
        self._seq: dict[tuple[str, str], int] = {}
        # Latest pushed live thumbnail per (host_key, cam). Ephemeral: the
        # newest FF has no archival value, so a dashboard restart simply falls
        # back to the on-demand :7779 pull until the next push arrives.
        self._live_thumbs: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def seq_get(self, host_key: str, kind: str) -> int:
        with self._lock:
            return self._seq.get((host_key, kind), 0)

    def seq_bump(self, host_key: str, kind: str, seq: int) -> bool:
        # The lock makes the read/compare/advance atomic against concurrent
        # bumps, so exactly one of two racing same-seq pushes wins.
        with self._lock:
            current = self._seq.get((host_key, kind), 0)
            if seq <= current:
                return False
            self._seq[(host_key, kind)] = seq
            return True

    def get_status(self, host_key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._status.get(host_key)

    def set_status(self, host_key: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._status[host_key] = data

    def get_vitals(self, host_key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._vitals.get(host_key)

    def set_vitals(self, host_key: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._vitals[host_key] = data

    def all_status(self, host_keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: (self._status.get(key) or {}) for key in host_keys}

    def set_live_thumb(
        self,
        host_key: str,
        cam: str,
        data: bytes,
        content_type: str,
        ff_timestamp: str | None,
    ) -> None:
        with self._lock:
            self._live_thumbs[(host_key, cam)] = {
                "data": data,
                "content_type": content_type,
                "ff_timestamp": ff_timestamp,
                "received_at": time.time(),
            }

    def get_live_thumb(self, host_key: str, cam: str) -> dict[str, Any] | None:
        with self._lock:
            return self._live_thumbs.get((host_key, cam))


class RedisStationStateBackend:
    """Shared backend keeping status/vitals in Redis hashes.

    Two hashes — ``<prefix>:station:status`` and ``<prefix>:station:vitals`` —
    map ``host_key`` to a JSON blob. Live thumbnails live under per-``(host,
    cam)`` string keys (``<prefix>:live_thumb:<host>:<cam>``) so each frame
    carries its own short TTL. Every gunicorn worker points at the same Redis,
    so a heartbeat — or a live thumbnail — ingested by one worker is
    immediately visible to a read served by another. A per-hash TTL bounds
    staleness for stations that stop pushing.

    JSON (de)serialisation happens here so no redis or bytes types escape to
    :class:`StationCache`. Any Redis error is swallowed to ``None``/no-op and
    logged: telemetry display degrading to "no data" is strictly preferable to
    a Redis blip 500-ing a dashboard read.
    """

    def __init__(self, client: Any, *, prefix: str = _REDIS_PREFIX,
                 ttl_seconds: int = _REDIS_TTL_SECONDS,
                 live_thumb_ttl_seconds: int = _REDIS_LIVE_THUMB_TTL_SECONDS) -> None:
        self._r = client
        self._prefix = prefix
        self._status_key = f"{prefix}:station:status"
        self._vitals_key = f"{prefix}:station:vitals"
        self._seq_key = f"{prefix}:station:seq"
        self._ttl = ttl_seconds
        self._live_thumb_ttl = live_thumb_ttl_seconds
        # redis is importable here (this backend only exists when it is). Bind
        # the WatchError class so seq_bump's retry loop can catch it without a
        # top-level redis import (the package stays an optional dependency).
        import redis.exceptions as _redis_exceptions

        self._watch_error = _redis_exceptions.WatchError

    def _seq_field(self, host_key: str, kind: str) -> str:
        return f"{host_key}:{kind}"

    def seq_get(self, host_key: str, kind: str) -> int:
        try:
            raw = self._r.hget(self._seq_key, self._seq_field(host_key, kind))
        except Exception:
            logger.exception("redis seq_get failed for %s/%s", host_key, kind)
            # Fail closed to 0: an ingest handler treats 0 as "nothing seen",
            # so the seq guard degrades to accepting the push. seq_bump (the
            # authoritative accept/drop) still runs atomically; this read only
            # backs the advisory get_last_seq.
            return 0
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    def seq_bump(self, host_key: str, kind: str, seq: int) -> bool:
        """Atomic monotonic-max check-and-bump via an optimistic WATCH/MULTI
        transaction. The seq lives in a hash field per ``(host_key, kind)`` under
        ``<prefix>:station:seq`` so it survives independently of the status/
        vitals hashes and shares no TTL with them.

        WATCH guards the field between the read and the HSET: if any other client
        advances it in that window, EXEC fails with ``WatchError`` and we retry.
        The retry loop converges because each accepted bump strictly raises the
        stored value, so at most one racer with a given seq ever passes the
        ``seq > cur`` test — exactly-one-wins, and a stale seq never regresses a
        newer one. (Chosen over EVAL/Lua because it runs on any Redis and on the
        Lua-less fakeredis used in tests.)"""
        field = self._seq_field(host_key, kind)
        try:
            with self._r.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch(self._seq_key)
                        cur_raw = pipe.hget(self._seq_key, field)
                        try:
                            cur = int(cur_raw) if cur_raw is not None else 0
                        except (TypeError, ValueError):
                            cur = 0
                        if seq <= cur:
                            pipe.unwatch()
                            return False
                        pipe.multi()
                        pipe.hset(self._seq_key, field, seq)
                        pipe.execute()
                        return True
                    except self._watch_error:
                        # Someone advanced the field mid-transaction; re-read.
                        continue
        except Exception:
            # A Redis blip must not double-apply a push. Fail closed: report a
            # replay (drop) so we never mirror state twice. Pushers are
            # seq-idempotent and retry, so a dropped-on-error push is re-sent.
            logger.exception("redis seq_bump failed for %s/%s", host_key, kind)
            return False

    def _get(self, hash_key: str, host_key: str) -> dict[str, Any] | None:
        try:
            raw = self._r.hget(hash_key, host_key)
        except Exception:
            logger.exception("redis hget failed for %s/%s", hash_key, host_key)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("redis value for %s/%s is not valid JSON", hash_key, host_key)
            return None

    def _set(self, hash_key: str, host_key: str, data: dict[str, Any]) -> None:
        try:
            self._r.hset(hash_key, host_key, json.dumps(data, default=str))
            if self._ttl > 0:
                self._r.expire(hash_key, self._ttl)
        except Exception:
            logger.exception("redis hset failed for %s/%s", hash_key, host_key)

    def get_status(self, host_key: str) -> dict[str, Any] | None:
        return self._get(self._status_key, host_key)

    def set_status(self, host_key: str, data: dict[str, Any]) -> None:
        self._set(self._status_key, host_key, data)

    def get_vitals(self, host_key: str) -> dict[str, Any] | None:
        return self._get(self._vitals_key, host_key)

    def set_vitals(self, host_key: str, data: dict[str, Any]) -> None:
        self._set(self._vitals_key, host_key, data)

    def all_status(self, host_keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        if not host_keys:
            return {}
        try:
            raws = self._r.hmget(self._status_key, list(host_keys))
        except Exception:
            logger.exception("redis hmget failed for %s", self._status_key)
            return {key: {} for key in host_keys}
        out: dict[str, dict[str, Any]] = {}
        for key, raw in zip(host_keys, raws):
            if raw is None:
                out[key] = {}
                continue
            try:
                out[key] = json.loads(raw)
            except (ValueError, TypeError):
                out[key] = {}
        return out

    def _live_thumb_key(self, host_key: str, cam: str) -> str:
        # Per-(host,cam) string key so each thumbnail carries its own short TTL
        # and self-expires independently (a Redis hash shares one TTL across
        # all fields, which would keep a quiet cam's stale frame alive as long
        # as any other cam keeps refreshing the hash).
        return f"{self._prefix}:live_thumb:{host_key}:{cam}"

    def set_live_thumb(
        self,
        host_key: str,
        cam: str,
        data: bytes,
        content_type: str,
        ff_timestamp: str | None,
    ) -> None:
        # Raw bytes can't ride in JSON, so we base64 the payload into the same
        # JSON-envelope shape used for status/vitals. received_at is stamped
        # here to mirror the in-memory backend exactly.
        envelope = {
            "data_b64": base64.b64encode(data).decode("ascii"),
            "content_type": content_type,
            "ff_timestamp": ff_timestamp,
            "received_at": time.time(),
        }
        try:
            key = self._live_thumb_key(host_key, cam)
            if self._live_thumb_ttl > 0:
                self._r.set(key, json.dumps(envelope), ex=self._live_thumb_ttl)
            else:
                self._r.set(key, json.dumps(envelope))
        except Exception:
            logger.exception("redis live_thumb set failed for %s/%s", host_key, cam)

    def get_live_thumb(self, host_key: str, cam: str) -> dict[str, Any] | None:
        try:
            raw = self._r.get(self._live_thumb_key(host_key, cam))
        except Exception:
            logger.exception("redis live_thumb get failed for %s/%s", host_key, cam)
            return None
        if raw is None:
            return None
        try:
            envelope = json.loads(raw)
            data = base64.b64decode(envelope["data_b64"])
        except (ValueError, TypeError, KeyError, binascii.Error):
            logger.warning("redis live_thumb for %s/%s is malformed", host_key, cam)
            return None
        return {
            "data": data,
            "content_type": envelope.get("content_type", "image/webp"),
            "ff_timestamp": envelope.get("ff_timestamp"),
            "received_at": envelope.get("received_at"),
        }


def _redis_client_from_url(url: str) -> Any:
    """Build a redis client with decoded (str) responses. Imported lazily so
    the dashboard runs fine without the ``redis`` package installed when no
    ``ROVIMEN_REDIS_URL`` is configured."""
    import redis  # local import: optional dependency

    return redis.Redis.from_url(url, decode_responses=True)


def make_station_state_backend(
    redis_url: str | None = None,
) -> StationStateBackend:
    """Return the backend for the pushed status/vitals store.

    When ``redis_url`` (default: the ``ROVIMEN_REDIS_URL`` env var) is set, a
    :class:`RedisStationStateBackend` is returned so all gunicorn workers share
    one store. Otherwise the per-process :class:`InMemoryStationStateBackend`
    is returned, preserving the historical single-worker behaviour exactly.

    A misconfigured or unreachable Redis at startup falls back to the in-memory
    backend with a WARNING rather than crashing the worker — the dashboard must
    still boot. (Individual runtime Redis errors are handled per-call inside the
    Redis backend.)
    """
    url = redis_url if redis_url is not None else os.environ.get(REDIS_URL_ENV)
    if not url:
        return InMemoryStationStateBackend()
    try:
        client = _redis_client_from_url(url)
        # Fail fast at startup if Redis is unreachable so we log a clear reason
        # and fall back, instead of discovering it on the first read.
        client.ping()
        logger.info("StationCache using shared Redis backend: %s", url)
        return RedisStationStateBackend(client)
    except Exception:
        logger.exception(
            "%s is set but Redis is unreachable; falling back to per-process "
            "in-memory station cache (multi-worker reads will be inconsistent)",
            REDIS_URL_ENV,
        )
        return InMemoryStationStateBackend()


def make_seq_backend(
    redis_url: str | None = None,
) -> RedisStationStateBackend | None:
    """Return a Redis backend for the per-push seq/replay guard, or ``None``.

    ``station_state`` moves the hot per-push seq check-and-bump off the
    single-writer SQLite DB and onto Redis when ``ROVIMEN_REDIS_URL`` is set.
    Returning ``None`` when it is unset (or Redis is unreachable) tells
    ``station_state`` to keep the historical SQLite seq path exactly as today —
    zero behaviour change for single-worker dev + tests.

    Unlike :func:`make_station_state_backend`, this never returns an in-memory
    backend: an in-memory seq store would be per-worker and thus *wrong* for
    idempotency across gunicorn workers, and it would silently stop persisting
    the seq that ``get_last_seq``/``is_replay`` read from SQLite in the no-Redis
    path. So the only two outcomes are "shared Redis seq" or "SQLite seq".
    """
    url = redis_url if redis_url is not None else os.environ.get(REDIS_URL_ENV)
    if not url:
        return None
    try:
        client = _redis_client_from_url(url)
        client.ping()
        logger.info("station_state seq guard using shared Redis backend: %s", url)
        return RedisStationStateBackend(client)
    except Exception:
        logger.exception(
            "%s is set but Redis is unreachable; keeping the SQLite seq guard "
            "(single-writer contention remains until Redis is reachable)",
            REDIS_URL_ENV,
        )
        return None
