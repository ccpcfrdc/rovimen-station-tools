"""Authentication, user management, and access control."""

import copy
import fcntl
import functools
import logging
import os
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from werkzeug.security import check_password_hash
from flask import Flask, abort, jsonify, redirect, request, session, url_for

import cf_access
import security
from models import DashboardConfig, UserConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

USERS_PATH = Path(os.environ.get("ROVIMEN_USERS_PATH", "/opt/rovimen/users.yaml"))
SECRET_KEY_PATH = Path(
    os.environ.get("ROVIMEN_SECRET_KEY_FILE", "/opt/rovimen/secret.key")
)

_USERS_BACKUP_COUNT = 3


# ---------------------------------------------------------------------------
# Secret key management
# ---------------------------------------------------------------------------


def _load_or_create_secret_key() -> str:
    """Return Flask's session signing key, persisting it across restarts.

    Resolution order:
      1. `ROVIMEN_SECRET_KEY` env (highest priority — systemd-managed
         deployments stay unchanged).
      2. `ROVIMEN_SECRET_KEY_FILE` (default /opt/rovimen/secret.key).
      3. Generate a fresh 32-byte hex token and write it to the file
         with mode 0600 so the next restart picks it up.

    Without this, `app.secret_key = os.urandom(...)` rotates on every
    process restart and every signed-in user gets force-logged-out.
    """
    env = os.environ.get("ROVIMEN_SECRET_KEY")
    if env:
        return env
    try:
        if SECRET_KEY_PATH.exists():
            existing = SECRET_KEY_PATH.read_text().strip()
            if existing:
                return existing
    except OSError:
        pass
    new_key = secrets.token_hex(32)
    try:
        SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        SECRET_KEY_PATH.write_text(new_key)
        os.chmod(SECRET_KEY_PATH, 0o600)
    except OSError:
        # Read-only FS or permission issue — fall back to an in-memory
        # key, accepting that this process's sessions won't survive a
        # restart. Logged so the operator can fix permissions.
        logger.warning(
            "Could not persist secret key to %s; sessions will not survive restart",
            SECRET_KEY_PATH,
        )
    return new_key


# ---------------------------------------------------------------------------
# User cache & parsing
# ---------------------------------------------------------------------------

# UserConfig was moved to dashboard/models.py and re-exported above; the
# cache lives here because it is mutated by routes that still close over
# create_app() locals.
_users_cache: tuple[int, dict[str, UserConfig]] | None = None
_users_cache_lock = threading.Lock()


def _parse_users_file() -> dict[str, UserConfig]:
    raw = yaml.safe_load(USERS_PATH.read_text()) or {}
    result: dict[str, UserConfig] = {}
    for username, data in (raw.get("users") or {}).items():
        try:
            result[username] = UserConfig.model_validate(data)
        except Exception:
            logger.warning("Skipping user %r: validation failed", username, exc_info=True)
    return result


def _load_users() -> dict[str, UserConfig]:
    """Return parsed users.yaml, memoised on file mtime.

    On prod (CF Access enabled) this is hit on every request via
    _cf_access_before_request → _find_user_by_email, so the YAML+pydantic
    parse used to run for every single HTTP call. mtime invalidation is
    cheap (one stat) and exact: users.yaml only changes via _save_users
    (which clears the cache below) or external admin scripts holding the
    file lock — both bump the inode mtime via atomic rename.
    """
    global _users_cache
    try:
        st = USERS_PATH.stat()
    except FileNotFoundError:
        _users_cache = None
        return {}
    except OSError:
        return {}
    mtime_ns = st.st_mtime_ns
    cached = _users_cache
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    with _users_cache_lock:
        cached = _users_cache
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]
        result = _parse_users_file()
        _users_cache = (mtime_ns, result)
        return result


def _invalidate_users_cache() -> None:
    global _users_cache
    _users_cache = None


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------

USERS_LOCK_PATH = USERS_PATH.with_suffix(".lock")


@contextmanager
def _users_lock():
    """Exclusive file lock guarding all users.yaml writes.

    Cooperative — every code path that reads-then-writes users.yaml must
    take this lock. Concurrent processes (the dashboard service + admin
    Python scripts run via SSH) coordinate through the same lock file at
    ``USERS_PATH.with_suffix('.lock')``.

    External scripts should use the same pattern:

        import fcntl
        with open('/opt/rovimen/users.yaml.lock', 'a+') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            # … read users.yaml, modify, write …
    """
    USERS_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(USERS_LOCK_PATH, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            # flock auto-released when the file closes on context exit.
            pass


# ---------------------------------------------------------------------------
# User persistence
# ---------------------------------------------------------------------------


def _backup_users_file() -> None:
    """Rotate up to ``_USERS_BACKUP_COUNT`` backups of users.yaml.

    Called inside the exclusive lock before every write.  Backups are
    named ``users.yaml.bak.1`` (most recent) through ``.bak.N``.
    """
    if not USERS_PATH.exists():
        return
    for i in range(_USERS_BACKUP_COUNT, 1, -1):
        older = USERS_PATH.with_suffix(f".yaml.bak.{i}")
        newer = USERS_PATH.with_suffix(f".yaml.bak.{i - 1}")
        if newer.exists():
            shutil.copy2(newer, older)
    shutil.copy2(USERS_PATH, USERS_PATH.with_suffix(".yaml.bak.1"))


def _check_user_count(new_count: int) -> None:
    """Refuse to write if the new user count is suspiciously low.

    Prevents a bug from silently wiping accounts by aborting the write
    when the new file would contain fewer than half the existing accounts
    (threshold only kicks in when the existing file has 4+ users).
    """
    if not USERS_PATH.exists():
        return
    try:
        existing = yaml.safe_load(USERS_PATH.read_text()) or {}
        old_count = len(existing.get("users") or {})
    except Exception:
        return
    if old_count >= 4 and new_count < old_count // 2:
        raise RuntimeError(
            f"Refusing to write users.yaml: new file has {new_count} accounts "
            f"but existing file has {old_count}. This looks like data loss."
        )


def _save_users(users: dict[str, UserConfig]) -> None:
    """Atomic, locked write of users.yaml.

    Writes to a temp file and renames into place (atomic on POSIX) so a
    concurrent reader never sees a half-written file. Wrapped in
    ``_users_lock()`` so two writers don't race.
    """
    with _users_lock():
        data = {"users": {k: v.model_dump() for k, v in users.items()}}
        _check_user_count(len(users))
        _backup_users_file()
        tmp = USERS_PATH.with_suffix(".tmp")
        tmp.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True)
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(USERS_PATH)
    # 0600 on the live file too, in case rename inherited odd perms.
    security.lock_file_perms(USERS_PATH, 0o600)
    _invalidate_users_cache()


def _update_users(mutator) -> None:
    """Atomic load → mutator(users) → save under exclusive lock.

    Use this anywhere you'd otherwise write the
    ``users = _load_users(); …mutate…; _save_users(users)`` pattern —
    it eliminates the read-modify-write race that lets a concurrent
    writer silently overwrite the change.

    Raw YAML entries that fail Pydantic validation are preserved through
    the write-back so that a schema change never silently drops accounts.
    """
    with _users_lock():
        raw = yaml.safe_load(USERS_PATH.read_text()) or {}
        raw_users = raw.get("users") or {}
        users_before = _load_users()
        users = copy.deepcopy(users_before)
        mutator(users)
        merged = {}
        for k, v in raw_users.items():
            if k not in users_before:
                merged[k] = v
        for k, v in users.items():
            merged[k] = v.model_dump()
        _check_user_count(len(merged))
        _backup_users_file()
        data = {"users": merged}
        tmp = USERS_PATH.with_suffix(".tmp")
        tmp.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True)
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(USERS_PATH)
    security.lock_file_perms(USERS_PATH, 0o600)
    _invalidate_users_cache()


# ---------------------------------------------------------------------------
# Credential checking
# ---------------------------------------------------------------------------


def _check_credentials(username: str, password: str) -> dict | None:
    """Return session dict if credentials are valid, else None.
    Includes needs_reset=True when the password is a one-time reset token."""
    users = _load_users()
    if username in users:
        u = users[username]
        # Account-level expiry — blocks every auth attempt once the account
        # has expired. Distinct from reset_token_expiry, which only gates
        # the initial first-login token. Used for tester accounts.
        if u.expires_at:
            try:
                exp = datetime.fromisoformat(u.expires_at).astimezone(timezone.utc)
                if datetime.now(timezone.utc) > exp:
                    return None
            except ValueError:
                pass  # malformed → safe path: ignore, fall through to normal auth
        # If a reset token is active AND not expired, accept it instead of
        # the normal password.  If the token has expired, fall through to
        # normal password auth so the user isn't locked out (#336).
        if u.reset_token and password and u.reset_token_expiry:
            try:
                expiry = datetime.fromisoformat(u.reset_token_expiry)
                token_valid = datetime.now(timezone.utc) < expiry.astimezone(timezone.utc)
                if token_valid:
                    if check_password_hash(u.reset_token, password):
                        return {"role": u.role, "stations": u.stations,
                                "display_name": u.display_name, "needs_reset": True}
                    return None  # valid token window, wrong password
            except ValueError:
                pass  # malformed expiry → fall through to normal auth
        if check_password_hash(u.password_hash, password):
            return {"role": u.role, "stations": u.stations, "display_name": u.display_name}
    # Legacy env-var fallback (admin only, for existing deployments). Routed
    # through the per-username lockout — without this, a distributed brute
    # force against the env-var admin's password evades both the YAML user
    # lockout and any account-enumeration mitigation.
    for i in (1, 2):
        ev_user = os.environ.get(f"ROVIMEN_ADMIN_USER_{i}")
        ev_pass = os.environ.get(f"ROVIMEN_ADMIN_PASS_{i}")
        if not (ev_user and ev_pass):
            continue
        if ev_user != username:
            continue
        if secrets.compare_digest(ev_pass, password):
            return {"role": "admin", "stations": [], "display_name": username}
    return None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _session_role() -> str:
    return session.get("role", "guest")


# Product rule: every signed-in account has fleet-wide READ access to all
# stations' data and videos. ``host`` station assignment no longer restricts
# *viewing* — it only scopes *writes* (see _has_station_write_access). Keep
# this in sync with the assignable roles in ``admin_config.py`` so a valid
# account can never be dropped to a 403 by the data-route gate (issues #522,
# #557). Unknown/guest sessions still fail closed.
_ACCOUNT_ROLES = ("admin", "host", "visitor", "press")


def _has_station_access(host_key: str) -> bool:
    """READ gate: any signed-in account may view every station's data/videos.

    Per the fleet-wide-read rule, station ownership is irrelevant to viewing;
    only an unrecognised/guest role fails closed. ``host_key`` is kept in the
    signature for call-site symmetry with the write gate.
    """
    return _session_role() in _ACCOUNT_ROLES


def _has_station_write_access(host_key: str) -> bool:
    """WRITE gate: admin may mutate any station; host only its assigned ones.

    visitor/press (read-only) and unknown/guest roles can never write. This is
    the per-station scoping that ``_has_station_access`` used to enforce before
    viewing was opened fleet-wide.
    """
    role = _session_role()
    if role == "admin":
        return True
    if role == "host":
        return host_key in (session.get("stations") or [])
    return False


def _host_keys_for_camera(config: DashboardConfig, cam_code: str) -> list[str]:
    """Return the host_keys that own the given camera code.

    Used by archive / cached-file routes that take a camera code (e.g.
    ``RO000A``) instead of a host_key. A camera should only belong to one
    station, but we return a list to fail-closed if the config ever drifts
    (the caller checks access against every owner).
    """
    if not cam_code:
        return []
    cu = cam_code.upper()
    return [k for k, st in config.stations.items()
            if any(c.code.upper() == cu for c in st.cameras)]


def _has_camera_access(config: DashboardConfig, cam_code: str) -> bool:
    """True if the session can access at least one station that owns this camera.

    Admin sessions always pass. For non-admins the camera must belong to a
    station the user has access to. Unknown cameras (no owner station in
    the registry) fail closed: a non-admin can't probe arbitrary camera
    codes via the archive routes.
    """
    if _session_role() == "admin":
        return True
    owners = _host_keys_for_camera(config, cam_code)
    if not owners:
        return False
    return any(_has_station_access(h) for h in owners)


# ---------------------------------------------------------------------------
# Anonymous public-exposure helpers
# ---------------------------------------------------------------------------
#
# When the dashboard is exposed publicly (see security.install_auth_gate /
# @public_route), a request may arrive with NO login session. Those requests
# are allowed to reach the small set of tagged read-only viewing routes, but
# must never see stations flagged ``public: false`` in dashboard_config.yaml
# (commissioning / opted-out). Logged-in users are unaffected — they keep
# fleet-wide read access, including non-public stations. These helpers give
# every read/media route one shared, consistent definition of "is this
# request an anonymous public visitor, and may it see this station."


def is_anonymous() -> bool:
    """True when there is no logged-in session (public/anonymous visitor)."""
    return not session.get("user")


def station_is_public_for_request(config: DashboardConfig, host_key: str) -> bool:
    """Whether the CURRENT request may view ``host_key``'s data/media.

    Logged-in users retain full fleet-wide read access (every station,
    public or not). Anonymous visitors may only see stations whose
    ``public`` flag is true; an unknown host_key fails closed. This mirrors
    the ``public: true`` filter that public_api.py applies to /media/v1/*,
    so the internal read surface and the versioned public API agree on
    exactly which stations are visible to the anonymous public.
    """
    if not is_anonymous():
        return True
    station = config.stations.get(host_key)
    return bool(station is not None and station.public)


def camera_is_public_for_request(config: DashboardConfig, cam_code: str) -> bool:
    """Camera-code variant of :func:`station_is_public_for_request`.

    Logged-in users always pass. For anonymous visitors the camera must
    belong to at least one ``public: true`` station; unknown cameras fail
    closed so the public can't enumerate private/commissioning gear.
    """
    if not is_anonymous():
        return True
    owners = _host_keys_for_camera(config, cam_code)
    if not owners:
        return False
    return any(station_is_public_for_request(config, h) for h in owners)


def _find_user_by_email(email: str) -> tuple[str, UserConfig] | None:
    """Return (username, UserConfig) for the user whose `email` matches.

    Matching is case-insensitive. Returns None if no user has this email or
    if multiple users share it (ambiguous → refuse rather than guess).
    """
    target = (email or "").strip().lower()
    if not target:
        return None
    users = _load_users()
    matches = [
        (uname, u) for uname, u in users.items()
        if (u.email or "").strip().lower() == target
    ]
    if len(matches) != 1:
        return None
    return matches[0]


# ---------------------------------------------------------------------------
# Cloudflare Access integration
# ---------------------------------------------------------------------------


def _cf_access_before_request() -> None:
    """Auto-login Flask sessions for verified Cloudflare Access requests.

    Runs on every request. When a valid CF Access JWT is present *and* the
    current session is not already logged in as the matching user, look up
    the email in users.yaml and seed the session. No-op if CF Access is
    disabled (env unset) or the JWT header is absent — that path keeps the
    legacy password login working for Tailscale ops access.
    """
    claims = cf_access.verify_request_token(request.headers, request.cookies)
    if not claims:
        return
    matched = _find_user_by_email(claims["email"])
    if not matched:
        # Verified visitor, but no row in users.yaml. We don't grant access
        # — Cloudflare Access already proved identity, but role/station
        # mapping is the app's call. Logging once per request is fine; CF
        # is the rate limiter at the edge.
        logger.info("CF Access: %s authenticated but not in users.yaml", claims["email"])
        return
    username, u = matched
    if session.get("user") == username and session.get("cf_email") == claims["email"]:
        return  # already logged in via this path; nothing to refresh
    # If the same user is already authenticated (e.g. they just completed
    # ``_complete_login`` via /login or /totp/verify, both of which wipe
    # cf_email), do NOT clear the session — that would discard the freshly
    # written ``_csid``, ``mfa`` and ``stations`` keys on the very next
    # request. Just stamp ``cf_email`` so subsequent passes early-return on
    # the equality check above.
    if session.get("user") == username:
        session["cf_email"] = claims["email"]
        return
    # Rotate the cookie value on every auto-login transition: a pre-login
    # session-fixation cookie planted in the victim's browser would
    # otherwise survive into the authenticated state (CF Access verifies
    # identity, but the SID is still whatever the attacker pinned).
    session.clear()
    session.permanent = True
    session["_csid"]    = secrets.token_urlsafe(16)
    # Cloudflare Access proves *identity*, but it is not a second factor for
    # accounts that enrolled (or are required to enrol) TOTP. Granting a full
    # session straight from the email claim would silently downgrade those
    # accounts to single-factor (audit H4). When TOTP applies, seed only a
    # pending login plus a marker that identity is already CF-proven, and let
    # the normal flow route the browser to /totp/verify (or /totp/setup). Do
    # NOT set ``user`` — the auth gate then redirects to the login flow.
    if u.totp_secret or u.require_totp:
        session["pending_login"] = username
        session["cf_pending"]    = True  # identity proven by CF, TOTP still owed
        session["cf_email"]      = claims["email"]
        return
    session["user"]     = username
    session["role"]     = u.role
    session["stations"] = u.stations
    session["admin"]    = username if u.role == "admin" else None
    session["session_epoch"] = u.session_epoch
    # No TOTP on this account → CF Access *is* the only factor. Record the
    # session's factor honestly so audit/usage can tell CF-bridge sessions
    # apart from password+TOTP ones; never claim "totp" when none was checked.
    session["mfa"]      = "cf"
    session["cf_email"] = claims["email"]


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------


def require_auth(f):
    """Decorator: require any logged-in session (host or admin)."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            if request.is_json or request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """Decorator: require admin role."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if _session_role() != "admin":
            if request.is_json or request.path.startswith("/api/"):
                abort(403)
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def require_station(f):
    """Decorator for station-specific routes. The wrapped function must have a
    `host_key` kwarg.

    Reads (GET) are open to every signed-in account (fleet-wide read rule).
    Writes (any other method) require admin or the host that owns the station;
    read-only roles (visitor/press) and unknown roles are rejected."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            if request.is_json or request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("login"))
        host_key = kwargs.get("host_key", "")
        allowed = (
            _has_station_access(host_key)
            if request.method == "GET"
            else _has_station_write_access(host_key)
        )
        if not allowed:
            if request.is_json or request.path.startswith("/api/"):
                abort(403)
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Session revalidation (privilege-revocation, audit H3)
# ---------------------------------------------------------------------------


def _account_is_expired(u: UserConfig) -> bool:
    """True if the account has an ``expires_at`` that is in the past.

    Malformed timestamps fail *open* here (treated as not-expired) to mirror
    ``_check_credentials`` — a typo in the expiry must not lock everyone out.
    The login path applies the same lenient parse.
    """
    if not u.expires_at:
        return False
    try:
        exp = datetime.fromisoformat(u.expires_at).astimezone(timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) > exp


def revalidate_session() -> None:
    """Re-check the live account state behind a logged-in session cookie.

    The session cookie is client-side and lives 90 days, so role/station
    grants snapshotted at login can outlive an admin's demotion/de-scope.
    This runs as a ``before_request`` hook (registered *before* the global
    auth gate so a cleared session fails closed on the *same* request) and:

      * clears the session when the account has been deleted or has passed
        its ``expires_at`` (the user is logged out immediately);
      * on a ``session_epoch`` mismatch — bumped by the admin mutators on any
        role/stations/password/expiry change — refreshes ``role``/``stations``
        from users.yaml and re-stamps the epoch, so the *current* privilege
        level (lower or higher) takes effect at once.

    ``_load_users()`` is memoised on the file mtime, so the steady-state cost
    is a single ``stat`` per request. Accounts not present in users.yaml
    (env-var admins, magic-link guests created elsewhere) are left untouched —
    there is no file row to compare against.
    """
    username = session.get("user")
    if not username:
        return  # anonymous — the auth gate handles it
    users = _load_users()
    u = users.get(username)
    if u is None:
        return  # not a users.yaml account (env admin / external) — don't clobber
    if _account_is_expired(u):
        # Past account expiry — log out now rather than waiting for the next
        # explicit auth attempt. Mirrors the _check_credentials expiry gate.
        session.clear()
        return
    live_epoch = u.session_epoch or 0
    if session.get("session_epoch") != live_epoch:
        # Privilege-relevant change since this session was minted. Re-resolve
        # the authoritative role/stations and adopt the new epoch so the
        # check passes on subsequent requests without another file parse.
        session["role"]          = u.role
        session["stations"]      = u.stations
        session["admin"]         = username if u.role == "admin" else None
        session["session_epoch"] = live_epoch


def install_session_revalidation(app: Flask) -> None:
    """Register :func:`revalidate_session` as a ``before_request`` hook.

    Must be registered *before* ``security.install_auth_gate`` so that when
    revalidation clears an expired/removed account's session, the auth gate
    (running afterwards) sees no ``user`` and redirects/401s on the same
    request — fail-closed, not one request late.
    """
    app.before_request(revalidate_session)
