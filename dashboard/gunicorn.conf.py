"""Gunicorn config for the ROVIMEN dashboard (prod + dev tiers).

Lives in ``dashboard/`` so it is rsynced flat to ``/opt/rovimen[-dev]/`` by the
deploy workflow alongside the app code, and is loaded via
``gunicorn --config /opt/rovimen[-dev]/gunicorn.conf.py``. Everything here is
overridable by env so the same file serves both tiers and the worker count can
be raised without editing the systemd unit.

Multi-worker scaling (see docs/reversed_http_push_design.md §9):
  * ``GUNICORN_WORKERS``  — number of worker processes. Default **1**, which
    preserves the current single-worker behaviour exactly. Raising this REQUIRES
    ``ROVIMEN_REDIS_URL`` to be set, otherwise the pushed status/vitals cache and
    the public-API rate limiter are per-process and DESYNC across workers. When
    workers > 1 and no Redis URL is configured, ``on_starting`` logs a loud
    warning (the app still boots — degraded, not dead).

    MEMORY: each worker is a full process with its OWN in-heap image/prefetch/
    proxy caches (Redis shares only status/vitals/rate-limit/live-thumb). Those
    per-process caches are now LRU-bounded in ``create_app`` (see the
    ``ROVIMEN_*_CACHE_MAX`` knobs) so a worker's RSS is bounded regardless of
    uptime — >1 worker no longer walks the box into the OOM killer. The systemd
    unit also sets a ``MemoryMax`` safety net so a runaway is cgroup-killed and
    restarted instead of taking the whole box down.
  * ``GUNICORN_THREADS``  — threads per worker (gthread class). Default 32 (prod)
    is expressed via env; the units set 16 for dev.
  * ``GUNICORN_BIND``     — bind address. Default 127.0.0.1:17777.
  * ``GUNICORN_TIMEOUT`` / ``GUNICORN_GRACEFUL_TIMEOUT`` — request + shutdown
    deadlines.
"""

import logging
import os

logger = logging.getLogger("gunicorn.error")

# ── Worker model ──────────────────────────────────────────────────────────
worker_class = "gthread"
workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "32"))

# ── Networking / lifecycle ─────────────────────────────────────────────────
bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:17777")
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
accesslog = os.environ.get("GUNICORN_ACCESSLOG", "-")


def post_worker_init(worker) -> None:  # noqa: ANN001 -- gunicorn worker arg
    """Start the dashboard's background workers AFTER the worker is serving.

    gunicorn calls this once per worker, after the worker has loaded the WSGI
    app and is about to enter its request loop — i.e. strictly after the
    fork/exec and after ``create_app()`` has returned. Starting the pollers,
    prefetch loop and archive-index build here (rather than inline in
    ``create_app``) keeps the fork→ready window free of thread-spawns and lock
    contention, which is what probabilistically wedged the worker on
    ``systemctl restart`` (fork-after-threads deadlock).

    The app also arms a short ``threading.Timer`` fallback in ``create_app`` for
    launch paths without gunicorn hooks; ``_start_background_workers()`` is
    idempotent, so triggering it from both places is harmless.
    """
    try:
        wsgi = worker.app.wsgi()
        start = getattr(wsgi, "_start_background_workers", None)
        if callable(start):
            start()
    except Exception:
        # Never let a background-startup hiccup take down a worker that is
        # otherwise ready to serve; the create_app timer fallback still fires.
        worker.log.exception("post_worker_init: background startup failed")


def on_starting(server) -> None:  # noqa: ANN001 -- gunicorn server arg
    """Guard: multi-worker without a shared Redis backend desyncs caches.

    The pushed StationCache (status/vitals) and the public-API rate limiter are
    per-process unless ``ROVIMEN_REDIS_URL`` is set. With more than one worker
    and no Redis, a read served by one worker won't see what another ingested,
    and rate limits multiply by the worker count. We refuse to fail closed
    (the dashboard must stay up), but we make the misconfiguration impossible
    to miss in the logs.
    """
    if workers > 1 and not os.environ.get("ROVIMEN_REDIS_URL"):
        msg = (
            "GUNICORN_WORKERS=%d but ROVIMEN_REDIS_URL is unset — the pushed "
            "station cache and rate limiter are PER-WORKER and will desync. "
            "Set ROVIMEN_REDIS_URL to a shared Redis before running >1 worker."
        )
        # Log through gunicorn's error logger AND stderr so it surfaces
        # regardless of how logging is configured at boot.
        try:
            server.log.warning(msg, workers)
        except Exception:
            logger.warning(msg, workers)
        print("WARNING: " + (msg % workers), flush=True)
