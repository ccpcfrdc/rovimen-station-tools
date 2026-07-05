"""Security hardening: TOTP MFA, audit log, security headers, cookie config.

This module is intentionally small — the heavy lifting (Flask-Limiter for
rate-limit, pyotp for TOTP, qrcode for the enrollment QR) is delegated to
well-maintained libraries. Everything below is glue and policy.

All knobs read from environment variables so behaviour can be tuned per
deployment without code changes:

* ``ROVIMEN_AUDIT_LOG_PATH`` — append-only JSON audit log. Default
  ``/opt/rovimen/audit.log``. Falls back to stderr if the file is not
  writable.
* ``ROVIMEN_COOKIE_SECURE`` — ``"0"`` to allow session cookies over plain
  HTTP (dev-only). Default ``"1"`` (HTTPS only).
* ``ROVIMEN_TOTP_ISSUER`` — what shows in the authenticator app for the
  account. Default ``"Rovimen"``.
"""

from __future__ import annotations

import atexit
import base64
import io
import json
import logging
import os
import queue
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyotp
import qrcode
from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

logger = logging.getLogger(__name__)

# ── Audit log ─────────────────────────────────────────────────────────

AUDIT_LOG_PATH = Path(
    os.environ.get("ROVIMEN_AUDIT_LOG_PATH", "/opt/rovimen/audit.log")
)
_audit_lock = threading.Lock()
_audit_logger: logging.Logger | None = None


def _get_audit_logger() -> logging.Logger:
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger
    lg = logging.getLogger("rovimen.audit")
    lg.setLevel(logging.INFO)
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(AUDIT_LOG_PATH, mode="a")
        handler.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(handler)
        lg.propagate = False
    except OSError as exc:
        logger.warning("audit log unavailable at %s: %s", AUDIT_LOG_PATH, exc)
    _audit_logger = lg
    return lg


def audit(event: str, **fields: Any) -> None:
    """Append a JSON line to the audit log.

    ``event`` is required; ``fields`` are merged into the record. A UTC
    timestamp is always added. Thread-safe.
    """
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "event": event}
    record.update(fields)
    with _audit_lock:
        _get_audit_logger().info(json.dumps(record, separators=(",", ":")))


def _client_ip() -> str:
    """Best-effort real client IP.

    Cloudflare sets ``CF-Connecting-IP`` for tunnel traffic, but we only
    trust that header when Cloudflare Access is actually configured
    (``ROVIMEN_CF_AUD`` is set). Without CF Access any Tailscale peer
    could spoof the header to bypass per-IP rate limits.

    ProxyFix populates ``remote_addr`` from the first ``X-Forwarded-For``
    hop (Tailscale serve / nginx). Falls back to the immediate TCP peer.
    """
    if os.environ.get("ROVIMEN_CF_AUD") and request.headers.get("CF-Connecting-IP"):
        return request.headers["CF-Connecting-IP"]
    return request.remote_addr or "-"


def audit_request(event: str, **fields: Any) -> None:
    """audit() that auto-fills IP + truncated UA from the current request."""
    audit(
        event,
        ip=_client_ip(),
        ua=(request.headers.get("User-Agent") or "")[:200],
        **fields,
    )


# ── Per-user activity log ────────────────────────────────────────────

ACTIVITY_LOG_PATH = Path(
    os.environ.get("ROVIMEN_ACTIVITY_LOG_PATH", "/opt/rovimen/activity.log")
)

# Async writer: the after_request hook drops records onto a bounded queue
# and a dedicated daemon thread drains it. This keeps disk latency
# (especially fsync on rotation) off the request path; under load every
# worker used to serialise behind one lock + the global logging lock.
_ACTIVITY_QUEUE_MAX = 10000
_activity_queue: queue.Queue[dict] = queue.Queue(maxsize=_ACTIVITY_QUEUE_MAX)
_activity_writer_thread: threading.Thread | None = None
_activity_writer_lock = threading.Lock()
# Sentinel pushed onto the queue to ask the writer loop to exit cleanly
# during shutdown flush. Plain ``None`` works because every legitimate
# record is a dict.
_ACTIVITY_STOP = object()

# Drop tracking (P1-15). The queue is bounded; under load or during an
# incident the cost of blocking the request thread on a slow disk far
# outweighs the cost of dropping audit lines, so overflow is intentional
# — but it must be VISIBLE rather than silent.
_activity_log_dropped_total: int = 0
_activity_dropped_lock = threading.Lock()
# Emit a WARNING every Nth drop so operators see something in syslog
# without flooding it on a sustained overflow.
_DROP_LOG_EVERY = 100
# Set by ``flush_activity_log_on_exit`` once the shutdown sequence
# starts; further enqueues become best-effort no-blocks so a final
# burst from request threads can't keep the writer alive forever.
_activity_shutdown = threading.Event()


def _activity_writer_loop() -> None:
    fh = None
    while True:
        record = _activity_queue.get()
        if record is _ACTIVITY_STOP:
            # Drain anything still pending, then exit. The shutdown path
            # below pushes the sentinel AFTER it's done enqueueing
            # everything it wants flushed.
            if fh is not None:
                try:
                    fh.flush()
                    fh.close()
                except OSError:
                    pass
            return
        line = json.dumps(record, separators=(",", ":")) + "\n"
        for _attempt in (0, 1):
            try:
                if fh is None:
                    ACTIVITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    fh = open(ACTIVITY_LOG_PATH, "a", encoding="utf-8")
                fh.write(line)
                fh.flush()
                break
            except OSError as exc:
                logger.warning("activity log write failed: %s", exc)
                try:
                    if fh is not None:
                        fh.close()
                except OSError:
                    pass
                fh = None


def _ensure_activity_writer() -> None:
    global _activity_writer_thread
    if _activity_writer_thread is not None and _activity_writer_thread.is_alive():
        return
    with _activity_writer_lock:
        if _activity_writer_thread is not None and _activity_writer_thread.is_alive():
            return
        t = threading.Thread(
            target=_activity_writer_loop,
            name="activity-log-writer",
            daemon=True,
        )
        t.start()
        _activity_writer_thread = t


def activity_log_stats() -> dict[str, int]:
    """Snapshot of writer-queue state for the admin health endpoint."""
    with _activity_dropped_lock:
        dropped = _activity_log_dropped_total
    return {
        "dropped_since_boot": dropped,
        "queue_depth": _activity_queue.qsize(),
        "queue_maxsize": _ACTIVITY_QUEUE_MAX,
    }


def _note_activity_drop() -> None:
    """Bump the drop counter and emit a periodic WARNING.

    Called from the after_request enqueue path on every ``queue.Full``.
    """
    global _activity_log_dropped_total
    with _activity_dropped_lock:
        _activity_log_dropped_total += 1
        n = _activity_log_dropped_total
    if n % _DROP_LOG_EVERY == 0:
        logger.warning(
            "activity log dropped %d records since boot (queue_max=%d)",
            n,
            _ACTIVITY_QUEUE_MAX,
        )


def flush_activity_log_on_exit(timeout: float = 2.0) -> None:
    """Drain the activity-log queue and let the writer exit cleanly.

    Wired to ``atexit`` + ``SIGTERM`` by :func:`install_activity_log_shutdown_hooks`
    so a normal process stop doesn't lose in-flight audit lines. The
    deadline is a hard ceiling — if the disk is too slow we give up
    rather than block ``systemd stop`` forever.
    """
    if _activity_shutdown.is_set():
        return
    _activity_shutdown.set()
    t = _activity_writer_thread
    if t is None or not t.is_alive():
        # Writer never started → nothing to flush; if records are sitting
        # in the queue they were never going to be written anyway.
        return
    deadline = time.monotonic() + max(0.0, timeout)
    # Push the stop sentinel so the writer drains everything pending and
    # exits. The writer processes records FIFO so the sentinel only fires
    # after every prior record has been written.
    try:
        _activity_queue.put_nowait(_ACTIVITY_STOP)
    except queue.Full:
        # No room to even signal stop. The writer will still drain on
        # its next iteration; we just can't guarantee a clean exit
        # marker. The OS will reap the daemon thread on process exit.
        return
    remaining = deadline - time.monotonic()
    if remaining > 0:
        t.join(timeout=remaining)


def install_activity_log_shutdown_hooks() -> None:
    """Register atexit + SIGTERM handlers that flush the activity log.

    Idempotent. Called once from ``create_app``.
    """
    atexit.register(flush_activity_log_on_exit)

    # signal.signal can only be installed from the main thread; in a
    # gunicorn worker that's usually fine because create_app runs there
    # on import. Fall back to atexit-only when not possible.
    try:
        prev = signal.getsignal(signal.SIGTERM)

        def _handler(signum, frame):
            flush_activity_log_on_exit()
            # Chain back to any previously-installed handler so
            # frameworks that wired their own (gunicorn worker shutdown
            # signaller, for instance) still run.
            if callable(prev) and prev not in (signal.SIG_DFL, signal.SIG_IGN):
                try:
                    prev(signum, frame)
                except Exception:
                    logger.exception(
                        "previous SIGTERM handler raised after activity-log flush"
                    )
            # If nothing else handled it, default behaviour is to exit;
            # leaving the signal masked is fine — atexit will run on
            # the natural exit path that follows.

        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        # ValueError: not in main thread. OSError: platform refusal.
        logger.debug(
            "could not install SIGTERM handler for activity log; "
            "atexit-only flush remains in place"
        )


# Paths to skip in the activity log — high-frequency polls + static assets
# would flood the file without telling us anything useful.
_ACTIVITY_SKIP_PREFIXES = (
    "/static/",
    "/sw.js",
    "/favicon.ico",
    "/robots.txt",
    "/manifest.json",
    # Polled by frontend on a timer; would dominate the log noise budget.
    "/api/auth/status",
    "/api/status/",        # per-station status poll
    "/api/vitals/",        # per-station vitals poll
    "/api/status/all",
    "/api/vitals/all",
    # SSE / long-poll streams — log once on connect, not every event
    "/api/events/status",
    # Thumbnails / media: huge volume, rarely insightful
    "/thumbnail/",
    "/video/",
    "/timelapse/",
    "/stream/",
    "/internal/thumb_cache/",
)


def log_request_activity(response):
    """``after_request`` hook: append one JSON line per authenticated hit.

    Skips static assets, high-frequency polls, and unauthenticated traffic
    so the file stays human-readable. The login page, /api/* endpoints
    (other than the high-volume polls), and per-station tabs all get
    captured — enough to answer "what was alex looking at this afternoon."

    The actual file write is handed off to a background daemon via a
    bounded queue; if the writer can't keep up the record is dropped
    silently — activity logging must never block a request.
    """
    try:
        from flask import session
        user = session.get("user")
        if not user:
            return response  # only log authenticated activity
        path = request.path or ""
        if any(path == p or (p.endswith("/") and path.startswith(p))
               for p in _ACTIVITY_SKIP_PREFIXES):
            return response
        record = {
            "ts":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user":   user,
            "method": request.method,
            "path":   path[:200],
            "status": response.status_code,
            "ip":     _client_ip(),
            "ref":    (request.headers.get("Referer") or "")[:120] or None,
        }
        _ensure_activity_writer()
        if _activity_shutdown.is_set():
            # Process is shutting down — don't grow the queue past the
            # records we already committed to flushing. Counts as a
            # drop for observability.
            _note_activity_drop()
            return response
        try:
            _activity_queue.put_nowait(record)
        except queue.Full:
            _note_activity_drop()
    except Exception:
        # Activity logging must never break a request. Swallow + move on.
        logger.exception("activity log enqueue failed")
    return response


# ── TOTP MFA ──────────────────────────────────────────────────────────

def generate_totp_secret() -> str:
    """Fresh 160-bit base32 secret (RFC 6238 recommended length)."""
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, username: str) -> str:
    """``otpauth://`` URI for authenticator-app enrolment.

    The issuer is read from ``ROVIMEN_TOTP_ISSUER`` so the user sees a
    sensible name in their authenticator app (default ``Rovimen``).
    """
    issuer = os.environ.get("ROVIMEN_TOTP_ISSUER", "Rovimen")
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def totp_qr_data_uri(provisioning_uri: str) -> str:
    """Render an ``otpauth://`` URI as a ``data:image/png;base64,…`` URI.

    Embedded directly in the enrolment template so we don't need a route
    that streams a secret-bearing PNG.
    """
    img = qrcode.make(provisioning_uri, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def verify_totp(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code with ±30 s drift tolerance.

    Returns False (not an exception) on any malformed input so callers
    can treat it as a uniform auth-failure path.
    """
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
    except Exception:
        return False


# ── HTTP hardening ────────────────────────────────────────────────────

def add_security_headers(response):
    """Apply standard hardening headers to every response.

    Uses ``setdefault`` so route-specific overrides win.
    """
    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=31536000; includeSubDomains",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # SAMEORIGIN (not DENY): the admin "Config" tab renders /config inside a
    # same-origin <iframe>. DENY blocked it ("refused to connect"). SAMEORIGIN
    # still blocks cross-origin clickjacking; pairs with frame-ancestors 'self'.
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault(
        "Referrer-Policy", "strict-origin-when-cross-origin"
    )
    # Modest CSP — dashboard templates use inline styles + a few inline
    # event handlers, so we don't go to strict 'self' for scripts/styles.
    # The map needs Leaflet from unpkg.com (script + style) and OSM /
    # OpenTopo / CARTO / DJ Lorenz light-pollution tiles (img).
    # `img-src https:` is permissive but pragmatic — tile providers
    # don't execute, just render — and the alternative is listing every
    # tile CDN's wildcarded subdomain. `frame-ancestors 'none'` already
    # blocks clickjacking embedding.
    response.headers.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "img-src 'self' data: blob: https:; "
            "media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com; "
            "connect-src 'self' https:; "
            "frame-src 'self'; "
            "frame-ancestors 'self'"
        ),
    )
    return response


def configure_hardening(app: Flask) -> None:
    """Cookie flags, session lifetime, ProxyFix, security headers.

    Idempotent — safe to call once during ``create_app``.
    """
    # Trust one layer of forwarding proxy headers so SESSION_COOKIE_SECURE
    # works behind Tailscale Serve, nginx, and Cloudflare Tunnel (all of
    # which terminate TLS upstream of Flask).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

    app.config["SESSION_COOKIE_SECURE"] = (
        os.environ.get("ROVIMEN_COOKIE_SECURE", "1") == "1"
    )
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    # Lax (not Strict) so bookmarks / top-level GET navigation carry
    # the session. Still blocks cross-site POST CSRF.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # 90-day sessions on trusted devices. TOTP is enforced on every new
    # device / fresh-cookie login, so a stolen cookie still requires the
    # attacker to bypass MFA to register additional devices. Users can
    # rotate via /logout if a device is suspected compromised.
    app.permanent_session_lifetime = timedelta(days=90)

    app.after_request(add_security_headers)
    app.after_request(log_request_activity)
    _ensure_activity_writer()
    # Drain the activity log on SIGTERM / atexit so in-flight audit
    # records aren't lost when systemd stops the service (P1-15).
    install_activity_log_shutdown_hooks()


# ── Per-username login throttle ──────────────────────────────────────

# Defends against distributed brute force: an attacker rotating 100 cheap
# residential-proxy IPs at 10/min/IP each = 1000 attempts/min, evading
# the Flask-Limiter per-IP rate. This in-memory sliding-window counter
# tracks consecutive failures per (lowercased) username and locks the
# account out for LOCKOUT_SECONDS once THRESHOLD failures land within
# WINDOW_SECONDS. Cleared on a successful login.
#
# State is per-process and lost on restart, which is fine — restart is
# rare and resets are a legitimate admin tool. No persistence layer.

import time as _time

_LOGIN_WINDOW_SECONDS  = 15 * 60   # bucket window
_LOGIN_THRESHOLD       = 10        # failures within window → lock
_LOGIN_LOCKOUT_SECONDS = 30 * 60   # how long the lock holds

_login_failures: dict[str, dict] = {}
_login_failures_lock = threading.Lock()
_login_failures_call_count = 0
_LOGIN_FAILURES_MAX_SIZE = 10_000
_LOGIN_FAILURES_SWEEP_INTERVAL = 100


def _normalise_username(u: str) -> str:
    return (u or "").strip().lower()


def _sweep_stale_failures(now: float) -> None:
    """Remove entries whose window and lockout have both expired.

    Must be called while holding ``_login_failures_lock``.
    """
    stale = [
        u
        for u, e in _login_failures.items()
        if (now - e.get("first", 0) > _LOGIN_WINDOW_SECONDS
            and e.get("locked_until", 0) <= now)
    ]
    for u in stale:
        del _login_failures[u]


def is_user_locked(username: str) -> bool:
    """Return True if the username is currently in lockout.

    Callers MUST NOT leak this fact to clients — return the same
    "Invalid credentials" response as any other auth failure, otherwise
    the lockout itself becomes an account-enumeration oracle.
    """
    u = _normalise_username(username)
    if not u:
        return False
    with _login_failures_lock:
        e = _login_failures.get(u)
        if not e:
            return False
        locked_until = e.get("locked_until", 0)
        if locked_until > _time.time():
            return True
        # Lockout expired — clean up so the next attempt starts fresh.
        if locked_until and locked_until <= _time.time():
            _login_failures.pop(u, None)
        return False


def record_login_failure(username: str) -> bool:
    """Increment the failure counter; return True if THIS failure tripped
    the threshold and locked the account."""
    global _login_failures_call_count
    u = _normalise_username(username)
    if not u:
        return False
    now = _time.time()
    with _login_failures_lock:
        # Periodic eviction: sweep stale entries every N calls or when
        # the dict exceeds the max-size cap.
        _login_failures_call_count += 1
        if (_login_failures_call_count % _LOGIN_FAILURES_SWEEP_INTERVAL == 0
                or len(_login_failures) > _LOGIN_FAILURES_MAX_SIZE):
            _sweep_stale_failures(now)

        e = _login_failures.get(u, {"count": 0, "first": now})
        # Sliding window: if the first failure is older than the window,
        # this attempt starts a fresh window.
        if now - e.get("first", now) > _LOGIN_WINDOW_SECONDS:
            e = {"count": 0, "first": now}
        e["count"] = e.get("count", 0) + 1
        e["first"] = e.get("first", now)
        if e["count"] >= _LOGIN_THRESHOLD:
            e["locked_until"] = now + _LOGIN_LOCKOUT_SECONDS
        _login_failures[u] = e
        return e["count"] == _LOGIN_THRESHOLD  # only on the trip-edge


def clear_login_failures(username: str) -> None:
    """Reset on successful login."""
    u = _normalise_username(username)
    if not u:
        return
    with _login_failures_lock:
        _login_failures.pop(u, None)


def init_limiter(app: Flask):
    """Wire Flask-Limiter using the real client IP as the key.

    When running behind gunicorn with multiple workers, in-memory storage
    gives each worker its own counters — so the effective rate limit becomes
    N * configured_limit.  A shared Redis backend keeps a single counter
    across all workers.

    Set ``ROVIMEN_REDIS_URL`` (e.g. ``redis://localhost:6379``) to enable
    Redis-backed rate limiting.  When unset, falls back to per-process
    memory storage with a warning.
    """
    from flask_limiter import Limiter

    redis_url = os.environ.get("ROVIMEN_REDIS_URL")
    if redis_url:
        storage_uri = redis_url
        logger.info("Rate-limiter using Redis backend: %s", redis_url)
    else:
        storage_uri = "memory://"
        logger.warning(
            "ROVIMEN_REDIS_URL not set — rate limits are per-worker "
            "(ineffective with multiple gunicorn workers)"
        )

    limiter = Limiter(
        key_func=_client_ip,
        app=app,
        storage_uri=storage_uri,
        default_limits=[],
        strategy="fixed-window",
    )
    return limiter


# Paths that must remain reachable without a session — auth flow itself,
# static assets served by Flask when nginx isn't in front, plus a few
# browser-default hits we don't want spamming the audit log with 401s.
_AUTH_GATE_PUBLIC_PATHS = {
    "/login",
    "/logout",
    "/set-password",
    "/totp/setup",
    "/totp/verify",
    "/sw.js",
    "/favicon.ico",
    "/robots.txt",
    "/manifest.json",
    # Public API index — the route is registered as both with and without
    # a trailing slash, so the prefix check (which is anchored on the
    # trailing slash) wouldn't otherwise match the bare form.
    "/api/public/v1",
    # IAU shower reference data — public, no station-specific info
    "/api/showers",
    "/api/shower-year-counts",
}

# Path prefixes that bypass the auth gate. Reserved for surfaces that are
# explicitly designed as anonymous-public (no Set-Cookie, no session reads,
# read-only) — adding a prefix here is a deliberate widening of attack
# surface and must be reviewed.
_AUTH_GATE_PUBLIC_PREFIXES = (
    # Versioned public read-only API for third-party consumers
    # (astromania.org and any other operator embedding meteor data on
    # their own site). Implemented in dashboard/public_api.py; only
    # exposes stations with ``public: true`` in dashboard_config.yaml.
    # Pinned to the explicit v1 prefix so a future /api/public/v2/...
    # route is a deliberate decision, not a silent widening of the
    # unauthenticated surface.
    "/api/public/v1/",
    # Public media served from the storage box archive (clip / stack /
    # timelapse / nightstack files for the same opt-in station set).
    "/media/v1/",
    # Reversed-HTTP push ingest API (docs/reversed_http_push_design.md §2).
    # Stations POST their own telemetry here; each request authenticates with a
    # per-station key (X-Station-Key) that authorises exactly one host_key, so
    # these routes do their own auth and must bypass the login-session gate —
    # same rationale as /api/public/v1/. The per-station key can only forge one
    # station's telemetry; it opens no shell and reads nothing.
    "/api/ingest/v1/",
    # Reversed-HTTP push command channel (docs/reversed_http_push_design.md §3).
    # The station-key GET (poll) + POST /ack routes authenticate with the same
    # per-station key as ingest and must be reachable without a login session, so
    # the prefix bypasses the login gate. The admin *enqueue* POST on the same
    # prefix carries its own @require_admin decorator — the real gate — so an
    # anonymous enqueue is 403'd there, not silently admitted. A per-station key
    # can only read/ack its own station's commands; it opens no shell.
    "/api/fleet/",
    # One-click magic-login links: /l/<token>. The route itself validates
    # the bearer token and establishes the session, so it must be reachable
    # before a session exists — same rationale as /login.
    "/l/",
)


# ── Explicit-allow public-route tagging ───────────────────────────────
#
# The auth gate is EXPLICIT-ALLOW (deny-by-default): an unauthenticated
# request is rejected UNLESS its matched view function was tagged with
# ``@public_route``, or its path is one of the small static/auth/
# public-API exceptions below. A route that forgets the decorator stays
# login-gated — the fail-closed default — so accidentally shipping a new
# route never widens the anonymous attack surface.
#
# This inverts the previous model (login required for everything except a
# hand-maintained allowlist), where a new sensitive route was public by
# omission unless someone remembered to gate it. Now the sensitive default
# is safe and the *public* case is the one that must be opted into
# deliberately and reviewed.
#
# Marking a view public is necessary but not sufficient for exposure: the
# public read surface (overview/detections/events/timelapses/stacks/
# live-media) additionally enforces the per-station ``public: true`` flag
# for anonymous callers (see ``auth.station_is_public_for_request`` and the
# media serving handlers), so a ``public: false`` commissioning station is
# invisible/404 to the anonymous public even though the route is tagged.

_PUBLIC_ROUTE_ATTR = "_rovimen_public_route"
# Second attribute holding the optional stable *page key* a public view
# belongs to (e.g. "events", "live", "showers", "overview", "station").
# When set, the view is only actually reachable by an anonymous caller if
# that key is currently listed in ``public_pages`` (dashboard_config.yaml) —
# giving operators a runtime on/off switch per page without a code deploy.
# A public view with NO page key (media handlers, the IAU shower reference
# API) is always-eligible: it either serves reference data or does its own
# per-station ``public`` filtering, so there is nothing page-level to toggle.
_PUBLIC_PAGE_ATTR = "_rovimen_public_page"


def public_route(view=None, *, page: str | None = None):
    """Tag a Flask view function as reachable without a login session.

    Fail-closed: only views wearing this tag (plus the static/auth/public-API
    path exceptions) are allowed through :func:`install_auth_gate` for
    anonymous callers. Everything else keeps requiring a session.

    Usage::

        @public_route                 # always-eligible public view
        def api_showers(): ...

        @public_route(page="events")  # eligible AND runtime-toggleable
        def events_page(): ...

    When ``page`` is given the view additionally participates in the
    ``public_pages`` runtime toggle: an operator can drop the key from
    ``public_pages`` in dashboard_config.yaml and the page reverts to
    login-gated on the next restart — WITHOUT a code change. A page whose
    key is absent from ``public_pages`` (or an unknown key) stays gated —
    the fail-closed default.

    Tagging a route public does NOT bypass the per-station ``public`` flag —
    read/media routes still return 404/empty for ``public: false`` stations
    to anonymous callers. It only tells the global gate "don't 302/401 this
    view purely for lack of a session."
    """
    def _tag(v):
        setattr(v, _PUBLIC_ROUTE_ATTR, True)
        if page is not None:
            setattr(v, _PUBLIC_PAGE_ATTR, page)
        return v

    # Support both bare ``@public_route`` and ``@public_route(page=...)``.
    if view is not None:
        return _tag(view)
    return _tag


def _matched_public_view(app: Flask):
    """The view function matched for the current request if it is tagged
    ``@public_route``, else ``None`` (so the gate stays fail-closed when no
    rule matched — e.g. a 404)."""
    endpoint = getattr(request.url_rule, "endpoint", None)
    if not endpoint:
        return None
    view = app.view_functions.get(endpoint)
    if view is not None and getattr(view, _PUBLIC_ROUTE_ATTR, False):
        return view
    return None


def install_auth_gate(app: Flask, public_pages: "list[str] | set[str] | None" = None) -> None:
    """Explicit-allow auth gate: deny anonymous access unless the matched
    view is tagged ``@public_route`` (or hits a static/auth/public-API path).

    Always on. We deliberately removed the previous ``ROVIMEN_REQUIRE_LOGIN``
    env toggle: tying the security boundary to a single env-file line was
    one bad rollback away from making the whole dashboard public again.
    Per-route ``@require_admin`` / ``@require_station`` decorators continue
    to gate admin/station paths as a second, independent layer.

    Fail-closed inversion (this change): previously the gate required a
    session for *every* path except a hand-maintained allowlist, so read
    routes were protected only by that default-deny and a new route was
    public by omission. Now anonymous access is refused UNLESS the route
    explicitly opted in via ``@public_route`` — so forgetting the decorator
    leaves a route login-gated rather than exposed.

    Unauthenticated browser hits → 302 to ``/login?next=<path>`` so a
    deep-link bookmark still lands the user back where they were.
    Unauthenticated API hits → 401 (the dashboard JS treats that as
    session-expired and reloads to /login).

    The CF Access bridge (if enabled) runs *before* this gate as a
    separate ``before_request`` hook, so a valid JWT auto-creates the
    session and the gate then sees a logged-in user.

    ``public_pages`` is the runtime on/off switch: the set of page keys an
    operator currently exposes to anonymous visitors (from
    ``public_pages`` in dashboard_config.yaml). A ``@public_route(page=...)``
    view is reachable anonymously ONLY IF its page key is in this set.
    Fail-closed: a page key not in the set (or ``public_pages`` unset →
    empty set) keeps that page login-gated even though its route is
    eligible. Page-less public routes (media, shower reference API) ignore
    this set entirely — they have no page-level toggle. Sensitive routes
    are never ``@public_route`` at all, so no ``public_pages`` entry can
    ever expose them.
    """
    from flask import abort, redirect, request, session, url_for

    enabled_pages = frozenset(public_pages or ())

    @app.before_request
    def _auth_gate():
        if session.get("user"):
            return  # already authenticated (password or CF Access)
        path = request.path
        # Path-based exceptions: the auth flow itself, Flask-served static
        # assets, browser-default hits, and the self-authenticating public
        # API / media / ingest / magic-login surfaces (each does its own
        # auth or is deliberately anonymous, keyed, and read-only).
        if path in _AUTH_GATE_PUBLIC_PATHS or path.startswith("/static/"):
            return
        for prefix in _AUTH_GATE_PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return
        # Explicit-allow: the matched view opted into anonymous access via
        # @public_route. Per-station ``public`` filtering happens inside
        # those views for anonymous callers.
        view = _matched_public_view(app)
        if view is not None:
            page_key = getattr(view, _PUBLIC_PAGE_ATTR, None)
            # A page-scoped public view is only actually public when its key
            # is currently enabled in ``public_pages``. Page-less public
            # views (page_key is None) are always eligible.
            if page_key is None or page_key in enabled_pages:
                return
            # Eligible route, but the operator has toggled this page off →
            # fall through to the login gate (fail-closed).
        if path.startswith("/api/"):
            abort(401)
        return redirect(url_for("login", next=path))


# ── CSRF protection ──────────────────────────────────────────────────

def init_csrf(app: Flask):
    """Wire Flask-WTF CSRFProtect on the app.

    Protects every form POST/PUT/PATCH/DELETE by default. We exempt
    ``/api/*`` because those are JSON endpoints called from same-origin
    JS with the ``SameSite=Lax`` session cookie — adding a CSRF header
    everywhere would require touching every fetch() in the dashboard JS;
    Flask-WTF is also strict about header name, which clashes with our
    existing patterns. Phase 2 will tighten this up.

    Returns the CSRFProtect instance so the caller can ``csrf.exempt(view)``
    routes as they're being registered.
    """
    from flask_wtf.csrf import CSRFProtect

    csrf = CSRFProtect(app)
    return csrf


def exempt_api_routes_from_csrf(app: Flask, csrf) -> None:
    """Exempt every route whose URL starts with ``/api/`` from CSRF checks.

    Call AFTER all ``@app.route`` decorations are done — this iterates the
    URL map and marks the matching view functions exempt. Idempotent.
    """
    seen: set = set()
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api/") and rule.endpoint not in seen:
            view = app.view_functions.get(rule.endpoint)
            if view is not None:
                csrf.exempt(view)
                seen.add(rule.endpoint)


# ── Origin / Sec-Fetch-Site enforcement (CSRF Phase 1) ────────────────
#
# Phase 1 of the CSRF hardening from docs/audit-2026-05-24.md (P1-3).
# Phase 2 will layer a real CSRF token on top of every mutating route;
# until then we lean on browser headers that a cross-site attacker cannot
# spoof from a top-level ``<form action=...>`` POST.
#
# Why this exists: every ``/api/*`` route is CSRF-exempt today. The
# session cookie is ``SameSite=Lax``, which still travels with a top-level
# navigation POST initiated from a malicious page, and Flask happily
# accepts ``Content-Type: application/x-www-form-urlencoded`` on JSON
# routes. So a logged-in operator visiting evil.com is one ``<form>``
# auto-submit away from triggering a station reboot, an admin user
# create, a config edit, a lock toggle, etc.
#
# What this hook does: for every mutating ``/api/*`` request, require
# that EITHER ``Sec-Fetch-Site: same-origin`` is present OR ``Origin``
# matches the canonical dashboard host (request.host or one of the
# entries in ``ROVIMEN_TRUSTED_ORIGINS``). When BOTH headers are absent
# we pass — that's curl / cron / server-side scripts, which we don't
# want to break in Phase 1. Modern browsers (2020+) always send Origin
# on cross-site POST, so absent-Origin from a browser is implausible.
#
# Skipped paths: ``/api/public/v1/*`` is intentionally cross-origin
# (astromania.org and other read-only consumers, gated by API key).

_CSRF_PUBLIC_API_PREFIX = "/api/public/v1/"
_CSRF_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _parse_trusted_origins(raw: str) -> frozenset[str]:
    """Normalise a comma-separated ``ROVIMEN_TRUSTED_ORIGINS`` string into a
    set of ``scheme://host[:port]`` entries with trailing slashes stripped.
    Empty / whitespace-only entries are dropped silently."""
    out: set[str] = set()
    for piece in raw.split(","):
        piece = piece.strip().rstrip("/")
        if piece:
            out.add(piece)
    return frozenset(out)


def install_csrf_origin_check(app: Flask) -> None:
    """Register a ``before_request`` hook that rejects mutating ``/api/*``
    requests whose Origin / Sec-Fetch-Site indicate a cross-origin caller.

    Trusted origins are derived from two sources, OR'd together:

    * ``request.host_url`` (i.e. whatever scheme+host the request itself
      arrived on — this naturally covers same-origin XHR/fetch from the
      dashboard JS regardless of the deployed hostname).
    * ``ROVIMEN_TRUSTED_ORIGINS`` env var — comma-separated list of
      ``https://host[:port]`` entries. Set this when the dashboard is
      reachable under multiple hostnames (Tailscale Serve + a public
      reverse proxy, for example) so legitimate JS on either host can
      still mutate.

    Idempotent: registers exactly one ``before_request`` hook per app.
    """
    from flask import g, jsonify, request

    trusted_raw = os.environ.get("ROVIMEN_TRUSTED_ORIGINS", "")
    extra_trusted = _parse_trusted_origins(trusted_raw)
    if extra_trusted:
        logger.info(
            "csrf-origin: extra trusted origins configured: %s",
            ", ".join(sorted(extra_trusted)),
        )

    @app.before_request
    def _csrf_origin_gate():
        method = request.method
        if method not in _CSRF_STATE_CHANGING_METHODS:
            return None
        path = request.path or ""
        if not path.startswith("/api/"):
            return None
        # Public, key-authenticated, deliberately cross-origin surface.
        if path.startswith(_CSRF_PUBLIC_API_PREFIX):
            return None

        origin = (request.headers.get("Origin") or "").strip()
        sec_fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip()

        # Browser explicitly tagged the request as same-origin — accept.
        if sec_fetch_site == "same-origin":
            return None

        # Neither header present → could be a legitimate non-browser
        # client (curl, server-side script, cron job) OR a classic
        # <form method="POST"> cross-origin submission (browsers omit
        # Origin on plain form POSTs in some configurations).
        # Require Content-Type: application/json as an interim CSRF
        # defence: browsers cannot set this header on cross-origin form
        # submissions, but all dashboard JS and well-behaved API
        # clients already send JSON.  Phase 2 CSRF tokens will close
        # this gap fully.
        if not origin and not sec_fetch_site:
            ct = (request.content_type or "").strip().lower()
            if ct.startswith("application/json"):
                return None
            logger.warning(
                "csrf-origin: rejecting headerless %s %s — "
                "content_type=%r remote=%s ua=%r (interim CSRF guard)",
                method,
                path,
                request.content_type,
                request.remote_addr,
                (request.headers.get("User-Agent") or "")[:120],
            )
            g.csrf_reject = True
            return (
                jsonify(
                    error="Forbidden",
                    reason=(
                        "State-changing API requests without Origin or "
                        "Sec-Fetch-Site headers must set "
                        "Content-Type: application/json."
                    ),
                ),
                403,
            )

        # If Origin is present, it MUST match a trusted host. Even if
        # Sec-Fetch-Site is present (e.g. "cross-site"), that alone is
        # a hard reject — fall through to the comparison below.
        if origin:
            # Primary comparison: full scheme+host+port from request.host_url.
            own_host_origin = (request.host_url or "").rstrip("/")
            if own_host_origin and origin.rstrip("/") == own_host_origin:
                return None
            # Fallback: nginx proxy_set_header Host $host strips the port from
            # the Host header before forwarding, so request.host_url loses the
            # port while the browser's Origin still carries it.
            # e.g. Origin: http://100.64.0.1:17778
            #      request.host_url: http://100.64.0.1/   (port stripped)
            # Accept when Origin scheme+host matches request scheme+host and the
            # ONLY difference is an explicit port on the Origin side.  This is
            # limited to private deployments (Tailscale IPs, no public exposure).
            try:
                from urllib.parse import urlparse as _urlparse
                o = _urlparse(origin)
                r = _urlparse(own_host_origin)
                if o.scheme == r.scheme and o.hostname == r.hostname:
                    return None
            except Exception:
                pass
            if origin.rstrip("/") in extra_trusted:
                return None

        # Reject. Log a warning so we can spot false positives in prod
        # before users start filing tickets.
        logger.warning(
            "csrf-origin: rejecting %s %s — origin=%r sec_fetch_site=%r "
            "remote=%s ua=%r",
            method,
            path,
            origin,
            sec_fetch_site,
            request.remote_addr,
            (request.headers.get("User-Agent") or "")[:120],
        )
        try:
            from flask import session as _session
            actor = _session.get("user") if _session else None
        except Exception:
            actor = None
        try:
            audit(
                "csrf_origin_reject",
                actor=actor,
                method=method,
                path=path,
                origin=origin or None,
                sec_fetch_site=sec_fetch_site or None,
                remote=request.remote_addr,
            )
        except Exception:
            # audit() should never fail a request — swallow.
            pass

        resp = jsonify(
            {
                "error": "csrf_origin_mismatch",
                "detail": (
                    "Cross-origin state-changing request rejected. "
                    "Provide a same-origin Origin or Sec-Fetch-Site header, "
                    "or call this endpoint from a same-origin context."
                ),
            }
        )
        resp.status_code = 403
        return resp


# ── File-permission enforcement ───────────────────────────────────────

def lock_file_perms(path: Path, mode: int = 0o600) -> None:
    """``chmod`` a file (if it exists) and silently no-op when it doesn't.

    Used after writing ``users.yaml`` so the file never ends up with the
    default umask permissions (often 0644) — which would leak password
    hashes and TOTP secrets to any local reader.
    """
    try:
        if path.exists():
            os.chmod(path, mode)
    except OSError as exc:
        logger.warning("could not chmod %s to %o: %s", path, mode, exc)
