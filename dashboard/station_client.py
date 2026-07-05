"""HTTP session pool, station API client, and polling cache."""

import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from cache_backend import (
    StationStateBackend,
    make_station_state_backend,
)
from models import DashboardConfig
from tunnels import _TunnelDown  # noqa: F401 -- re-exported for backward compat

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pooled HTTP sessions for the 30+ outbound station calls
# ---------------------------------------------------------------------------
#
# Every page on the dashboard fans out to several stations over Tailscale
# (~70 ms RTT to Romania). A fresh `requests.get` builds a new TCP+TLS
# connection for every call; with keep-alive plus an HTTPAdapter we save
# 1x round-trip + TLS handshake per call on every cache miss. One Session
# per station base URL means each station's connection pool is independent
# -- a slow station can't starve a fast one.
_station_sessions: dict[str, requests.Session] = {}
_station_sessions_lock = threading.Lock()


def _get_station_session(base_url: str) -> requests.Session:
    """Return a pooled requests.Session for a given station base URL.

    Pool sizing: 30 reused connections, 80 hard cap, non-blocking -- so a
    burst (e.g. overview fan-out across all cameras of one station, or
    parallel /api/vitals + /api/status + thumbnail prefetch) spawns a
    few new sockets rather than blocking workers. `Retry` covers
    transient 5xx blips from station nginx restarts; the backoff (0.2 s)
    is short enough that timeouts still trip on truly dead hosts.
    """
    sess = _station_sessions.get(base_url)
    if sess is not None:
        return sess
    with _station_sessions_lock:
        sess = _station_sessions.get(base_url)
        if sess is not None:
            return sess
        sess = requests.Session()
        # Connect-failure retries are explicitly disabled: when a station is
        # offline, urllib3's default of falling through to `total` would
        # turn a configured 10s timeout into ~30s of wall time (initial +
        # 2 retries with backoff). That dominated /api/config/all and
        # /api/settings/<offline-host> latency. Read retries are also off
        # for the same reason -- a dead station should fail fast, not stall.
        # We only retry on transient 5xx (nginx restart, etc.).
        retry = Retry(
            total=2,
            connect=0,
            read=0,
            backoff_factor=0.2,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            pool_connections=30,
            pool_maxsize=80,
            max_retries=retry,
            pool_block=False,
        )
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        _station_sessions[base_url] = sess
        return sess


def _session_for_url(url: str) -> requests.Session:
    """Resolve `http(s)://host:port` prefix from a full URL so the same
    Session is reused for /api/foo and /color_capture/... on one station.
    """
    # Strip everything after the third '/' (the path) -- keep scheme://host:port.
    try:
        scheme_end = url.index("://") + 3
        path_start = url.find("/", scheme_end)
    except ValueError:
        return _get_station_session(url)
    base = url if path_start < 0 else url[:path_start]
    return _get_station_session(base)


def _evict_station_session(base_url: str) -> None:
    """Remove and close the pooled Session for a base URL (e.g. after a tunnel
    reconnects on a new port). Without this, orphaned Sessions keyed by dead
    ``127.0.0.1:<old_port>`` accumulate connection pools that never get reused."""
    with _station_sessions_lock:
        sess = _station_sessions.pop(base_url, None)
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass


def _streaming_proxy(resp, chunk_size: int = 65536):
    """Yield chunks from a ``requests.Response`` opened with ``stream=True``,
    guaranteeing the upstream connection is closed when the generator exits
    (including on client disconnect / GeneratorExit). Without this wrapper,
    abandoned ``iter_content()`` generators leave the Response object -- and its
    socket buffer -- alive until GC collects it, leaking ~32 KB-several MB per
    abandoned request."""
    try:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            yield chunk
    except GeneratorExit:
        pass
    finally:
        try:
            resp.close()
        except Exception:
            pass


_sshfs_leak_count = 0
_sshfs_leak_lock = threading.Lock()
_SSHFS_CIRCUIT_BREAKER_THRESHOLD = 15

def _with_sshfs_timeout(fn, timeout: float = 5.0, default=None):
    """Run *fn* in a worker thread; if it doesn't return within *timeout*
    seconds, return *default* and log a warning.

    SSHFS reads against the Hetzner Storage Box can hang for minutes when
    the mount is being remounted (or the box is rebooting). Wrapping
    metadata walks in this guard keeps a stuck mount from blocking Flask
    threads indefinitely -- the worker thread is left to finish in the
    background and gets garbage-collected when the function eventually
    returns.

    A circuit breaker trips after ``_SSHFS_CIRCUIT_BREAKER_THRESHOLD``
    *currently active* leaked threads -- at that point every call returns
    *default* immediately without spawning a new thread.  When a leaked
    thread eventually completes, the counter decrements so the breaker
    can recover without a restart.
    """
    global _sshfs_leak_count
    with _sshfs_leak_lock:
        if _sshfs_leak_count >= _SSHFS_CIRCUIT_BREAKER_THRESHOLD:
            logger.critical(
                "SSHFS circuit breaker: %d leaked threads, returning default",
                _sshfs_leak_count,
            )
            return default

    leaked = False
    box: dict[str, Any] = {"value": default, "done": False}

    def _runner() -> None:
        global _sshfs_leak_count
        try:
            box["value"] = fn()
        except Exception:
            box["value"] = default
        finally:
            box["done"] = True
            if leaked:
                with _sshfs_leak_lock:
                    _sshfs_leak_count -= 1
                logger.info(
                    "SSHFS leaked thread recovered (active: %d)",
                    _sshfs_leak_count,
                )

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout)
    if not box["done"]:
        leaked = True
        with _sshfs_leak_lock:
            _sshfs_leak_count += 1
        logger.warning(
            "SSHFS operation timed out after %.1fs (leaked threads: %d)",
            timeout, _sshfs_leak_count,
        )
        return default
    return box["value"]


# ---------------------------------------------------------------------------
# Station API client
# ---------------------------------------------------------------------------


def station_url(
    config: DashboardConfig, tunnels, host_key: str, path: str
) -> str:
    base = tunnels.get_api_base(host_key)
    if base is None:
        raise _TunnelDown(host_key)
    return f"{base}{path}"


def station_get_status(
    config: DashboardConfig,
    tunnels,
    host_key: str,
    path: str,
    timeout: int = 10,
) -> dict[str, Any]:
    """Fetch a status/vitals endpoint that returns a dict. Adds online/timestamp metadata."""
    try:
        url = station_url(config, tunnels, host_key, path)
        sess = _session_for_url(url)
        resp = sess.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        data["online"] = True
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        return data
    except Exception as exc:
        return {
            "online": False,
            "error": str(exc),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }


def station_get_raw(
    config: DashboardConfig,
    tunnels,
    host_key: str,
    path: str,
    timeout: int = 10,
) -> Any:
    """Fetch a station endpoint and return the raw JSON (list or dict)."""
    url = station_url(config, tunnels, host_key, path)
    sess = _session_for_url(url)
    resp = sess.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Cache and background polling
# ---------------------------------------------------------------------------


# Cap on simultaneous SSE subscribers. Each connection holds a thread (Flask's
# threaded WSGI) plus a bounded queue, so the memory cost is modest, but a
# runaway reconnect storm from a buggy client could pin the dashboard. 32 is
# far above any realistic legitimate ceiling (we have <10 humans on the team).
_MAX_STATUS_LISTENERS = 32

# Per-listener queue depth. Status events are infrequent (a station changes
# online/services state seconds-rarely, hours-typically), so even 16 would be
# plenty; 64 leaves room for a brief consumer stall without dropping events.
_STATUS_LISTENER_QUEUE_MAX = 64

# Sentinel fields used to decide whether two status payloads are "meaningfully"
# different. We deliberately ignore wall-clock timestamps the station API
# stamps on every reply -- comparing those would push an event every poll cycle
# even when nothing changed, defeating the point of SSE.
_STATUS_DIFF_FIELDS = (
    "online",
    "services",
    "disk",
    "storage",
    "extra_disks",
    "cameras",
    "color_capture_running",
    "rms_running",
)


def _status_diff_signature(data: dict[str, Any] | None) -> str:
    """Compact signature for change-detection. Stable json.dumps over the
    fields we actually surface in the UI; anything outside `_STATUS_DIFF_FIELDS`
    (e.g. server-stamped `updated_at`) is ignored to avoid spurious events."""
    if not data:
        return ""
    snapshot = {f: data.get(f) for f in _STATUS_DIFF_FIELDS if f in data}
    return json.dumps(snapshot, sort_keys=True, default=str, separators=(",", ":"))


class StationCache:
    def __init__(self, backend: StationStateBackend | None = None) -> None:
        # The raw last-value store for pushed/polled status + vitals. Defaults
        # to the per-process in-memory backend (unchanged single-worker
        # behaviour); when ROVIMEN_REDIS_URL is set, make_station_state_backend
        # returns a shared Redis backend so every gunicorn worker reads/writes
        # the same telemetry. No redis types cross this boundary.
        self._backend: StationStateBackend = (
            backend if backend is not None else make_station_state_backend()
        )
        # Last signature per host -- used by set_status to suppress no-op
        # broadcasts. Kept inside the lock so writers see a consistent view.
        # This stays per-process: it only gates the local SSE fan-out, and a
        # spurious broadcast on a worker that hasn't seen the prior value is
        # harmless (the payload is identical, clients dedup on content).
        self._status_sig: dict[str, str] = {}
        self._lock = threading.Lock()
        # Pub/sub for /api/events/status. Listeners are bounded Queues of
        # pre-encoded SSE event bytes (see _broadcast_status). On a full queue
        # we drop the event (slow-consumer policy) rather than block the
        # polling thread that owns set_status.
        self._status_listeners: set[queue.Queue[bytes]] = set()
        self._status_listeners_lock = threading.Lock()
        # Monotonic version bumped on any status write. Snapshot encoders share
        # cached bytes across listeners that arrive within the same version.
        self._cache_version: int = 0
        self._snapshot_bytes: tuple[int, bytes] | None = None

    def get_status(self, host_key: str) -> dict[str, Any] | None:
        return self._backend.get_status(host_key)

    def set_status(self, host_key: str, data: dict[str, Any]) -> None:
        with self._lock:
            new_sig = _status_diff_signature(data)
            prev_sig = self._status_sig.get(host_key)
            self._status_sig[host_key] = new_sig
            self._cache_version += 1
            self._snapshot_bytes = None
            changed = new_sig != prev_sig
        # Write to the (possibly shared) backend outside the local lock — the
        # backend has its own synchronisation and may do network I/O (Redis).
        self._backend.set_status(host_key, data)
        if changed:
            payload = json.dumps(
                {"host": host_key, "status": data}, default=str
            )
            event = f"event: status\ndata: {payload}\n\n".encode("utf-8")
            self._broadcast_status(event)

    def get_vitals(self, host_key: str) -> dict[str, Any] | None:
        return self._backend.get_vitals(host_key)

    def set_vitals(self, host_key: str, data: dict[str, Any]) -> None:
        self._backend.set_vitals(host_key, data)

    # -- live thumbnails (pushed newest-FF maxpixel WebP) ------------------
    def set_live_thumb(
        self,
        host_key: str,
        cam: str,
        data: bytes,
        *,
        content_type: str = "image/webp",
        ff_timestamp: str | None = None,
    ) -> None:
        """Store the latest pushed live thumbnail for a (station, cam).

        Last-writer-wins, keyed by camera. Delegates to the backend so the
        thumbnail is shared across gunicorn workers when a Redis backend is
        configured, and behaves exactly as the historical per-process dict when
        it isn't. ``received_at`` is stamped by the backend on write."""
        self._backend.set_live_thumb(
            host_key, cam, data, content_type, ff_timestamp
        )

    def get_live_thumb(self, host_key: str, cam: str) -> dict[str, Any] | None:
        """Return the latest pushed live thumbnail for a (station, cam), or None."""
        return self._backend.get_live_thumb(host_key, cam)

    # -- SSE pub/sub plumbing ----------------------------------------------
    def snapshot_bytes(self, host_keys: tuple[str, ...]) -> bytes:
        """Build the SSE `snapshot` event bytes for the given station ordering.
        Cached across concurrent registrations within the same cache version so
        a reconnect storm doesn't re-encode the same payload per client."""
        with self._lock:
            cached = self._snapshot_bytes
            version = self._cache_version
            if cached is not None and cached[0] == version:
                return cached[1]
        # Read the (possibly shared) backend outside the local lock — it may
        # do network I/O. Re-check the version cache after building in case a
        # concurrent write bumped it; worst case we build one extra snapshot.
        snapshot = self._backend.all_status(tuple(host_keys))
        payload = json.dumps(snapshot, default=str)
        event = f"event: snapshot\ndata: {payload}\n\n".encode("utf-8")
        with self._lock:
            self._snapshot_bytes = (version, event)
            return event

    def can_register_status_listener(self) -> bool:
        """Cheap, lock-free-ish probe used to gate the SSE route's 503 response
        before we enter the generator. The actual register call still re-checks
        the cap under the lock (the count may have grown between the probe and
        the registration), so this is purely an early-return optimisation that
        keeps the 503 path out of the streaming response path."""
        with self._status_listeners_lock:
            return len(self._status_listeners) < _MAX_STATUS_LISTENERS

    def register_status_listener(self) -> queue.Queue[bytes] | None:
        """Register a new SSE listener. Returns the queue to consume from,
        or None if the per-process listener cap has been hit (caller must
        respond with 503 -- the client's EventSource will retry on its own
        backoff, which is the desired behaviour during a transient storm)."""
        q: queue.Queue[bytes] = queue.Queue(maxsize=_STATUS_LISTENER_QUEUE_MAX)
        with self._status_listeners_lock:
            if len(self._status_listeners) >= _MAX_STATUS_LISTENERS:
                logger.warning(
                    "status SSE listener cap (%d) reached -- rejecting new subscriber",
                    _MAX_STATUS_LISTENERS,
                )
                return None
            self._status_listeners.add(q)
        return q

    def unregister_status_listener(self, q: queue.Queue[bytes]) -> None:
        with self._status_listeners_lock:
            self._status_listeners.discard(q)

    def _broadcast_status(self, event: bytes) -> None:
        """Fan out a pre-encoded SSE event to every subscribed queue. Snapshot
        the listener set under its lock so the iteration can't trip on a
        concurrent register/unregister. Drop on full -- never block the polling
        thread."""
        with self._status_listeners_lock:
            listeners = tuple(self._status_listeners)
        for q in listeners:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Slow consumer -- they'll resync on the next change or via
                # the periodic fallback fetch from the client.
                logger.debug("status SSE listener queue full -- dropping event")


def start_polling(
    config: DashboardConfig, tunnels, cache: StationCache
) -> None:
    # Pollers fan out across stations in parallel. A single offline station
    # used to stall the loop because the 90-second per-station timeout
    # blocked all subsequent stations in the same iteration; with N=8 and
    # 1 offline station the rest would lag by 90 s+. ThreadPoolExecutor +
    # tight per-station timeouts keeps each cycle bounded to ~15 s regardless
    # of how many stations are dead.
    _offline_streak: dict[str, int] = {}

    def _is_push_enabled(key: str) -> bool:
        """True when this station is on the push path, so the poller must skip
        it (its status/vitals cache is owned by the ingest API). Fail-safe:
        default False and any missing/malformed entry falls through to polling.
        """
        st = config.stations.get(key)
        return bool(getattr(st, "push_enabled", False))

    def _poll_one_status(key: str) -> None:
        if _is_push_enabled(key):
            # Station pushes its own status; leave the cache entry to the
            # ingest and never fabricate freshness here.
            return
        try:
            data = station_get_status(
                config, tunnels, key, "/api/status", timeout=8
            )
            if data.get("online"):
                _offline_streak[key] = 0
                cache.set_status(key, data)
            else:
                _offline_streak[key] = _offline_streak.get(key, 0) + 1
                if _offline_streak[key] >= 3:
                    cache.set_status(key, data)
                else:
                    prev = cache.get_status(key)
                    if prev and prev.get("online"):
                        pass
                    else:
                        cache.set_status(key, data)
        except Exception:
            logger.exception("status poll failed for %s", key)

    def _poll_one_vitals(key: str) -> None:
        if _is_push_enabled(key):
            # Vitals arrive via the ingest for push stations — skip polling.
            return
        try:
            data = station_get_status(
                config, tunnels, key, "/api/vitals", timeout=10
            )
            cache.set_vitals(key, data)
        except Exception:
            logger.exception("vitals poll failed for %s", key)

    def poll_status() -> None:
        n = max(len(config.stations), 1)
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="status-poll") as pool:
            while True:
                try:
                    list(pool.map(_poll_one_status, list(config.stations)))
                except Exception:
                    logger.exception("status poll cycle failed")
                time.sleep(60)

    def poll_vitals() -> None:
        n = max(len(config.stations), 1)
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="vitals-poll") as pool:
            while True:
                try:
                    list(pool.map(_poll_one_vitals, list(config.stations)))
                except Exception:
                    logger.exception("vitals poll cycle failed")
                time.sleep(30)

    threading.Thread(target=poll_status, daemon=True).start()
    threading.Thread(target=poll_vitals, daemon=True).start()
