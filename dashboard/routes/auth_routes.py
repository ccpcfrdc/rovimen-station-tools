"""Authentication routes -- login, magic link, TOTP, set-password, logout.

Extracted from rovimen_dashboard.py as part of the route-extraction effort.
All routes preserved verbatim -- same URLs, same behaviour, same decorators.
Wired in from ``create_app()`` via ``register_auth_routes``.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import security
from auth import (
    _check_credentials,
    _load_users,
    _session_role,
    _update_users,
)
from models import UserConfig

logger = logging.getLogger(__name__)

# Magic-login sessions are deliberately low-privilege and short-lived. The
# bearer token in the URL is a *single* factor (no password, no TOTP), so a
# leaked link must not confer write access nor a long-lived session. We force
# the redeemed session to a read-only role and bound it with an absolute expiry
# that is re-checked on every request -- independent of, and stricter than, the
# account-level ``expires_at`` and the app-wide 90-day permanent-session cap.
_MAGIC_SESSION_ROLE = "visitor"            # read-only; see auth._has_station_write_access
_MAGIC_SESSION_TTL_SECONDS = 12 * 60 * 60  # absolute cap on a redeemed magic session (12h)


def register_auth_routes(app: Flask, config, cache, tunnels) -> None:

    # ``limiter`` is stashed on the app by create_app().
    limiter = app._limiter  # type: ignore[attr-defined]

    @app.before_request
    def _enforce_magic_session_ttl():
        """Expire redeemed magic-login sessions at their absolute cap.

        Magic sessions carry a ``magic_exp`` stamp (set in ``magic_login``).
        Once it is past, clear the session and short-circuit -- a page hit is
        bounced to /login, an API hit gets 401 -- so a magic link can never
        outlive its short window even if the browser stays open. No-op for
        every non-magic session: the common path is a single dict lookup.
        """
        exp = session.get("magic_exp")
        if not exp:
            return
        try:
            expired = datetime.now(timezone.utc) > datetime.fromisoformat(exp)
        except ValueError:
            expired = True  # malformed stamp -> fail safe (treat as expired)
        if expired:
            session.clear()
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "session expired"}), 401
            return redirect(url_for("login"))

    # ── Shared helper ────────────────────────────────────────────────────

    def _complete_login(username: str, u: UserConfig | None,
                        result: dict[str, Any]) -> Any:
        """Final session-establishment step shared by /login and /totp/verify.

        Writes a fresh random ``_csid`` after ``session.clear()`` so the
        signed-cookie payload differs from any pre-login state even when
        the rest of the session would otherwise be deterministic. Cuts the
        session-fixation window: an attacker who planted a session cookie
        in the victim's browser pre-login can't re-use it after the victim
        authenticates because the cookie value rotates here.
        """
        session.clear()
        session.permanent    = True
        session["_csid"]     = secrets.token_urlsafe(16)
        session["user"]      = username
        session["role"]      = result["role"]
        session["stations"]  = result["stations"]
        session["admin"]     = username if result["role"] == "admin" else None
        # Snapshot the account's session-epoch so the before_request
        # revalidation gate (audit H3) can detect a later admin-side
        # role/stations/password/expiry change and force a refresh. None for
        # env-var admins / accounts not in users.yaml → 0, matching the model
        # default so those sessions never spuriously revalidate.
        session["session_epoch"] = u.session_epoch if u is not None else 0
        # Record the factor honestly: only "totp" when a TOTP secret was
        # actually verified to reach this point.
        if u is not None and u.totp_secret:
            session["mfa"]   = "totp"
        security.audit_request("login", username=username, result="ok")
        next_url = request.args.get("next", "")
        _p = urlparse(next_url)
        if (not next_url or not next_url.startswith("/")
                or next_url.startswith("//") or "\\" in next_url
                or _p.netloc or _p.scheme):
            next_url = url_for("index")
        return redirect(next_url)

    # ── /login ───────────────────────────────────────────────────────────

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    @limiter.limit("60 per hour", methods=["POST"])
    def login():
        if request.method == "GET":
            # A Cloudflare-Access user whose identity is already proven but who
            # still owes TOTP lands here when the auth gate bounces a protected
            # page. Route them straight to the second factor instead of showing
            # a password form they don't have a password for (audit H4).
            if session.get("cf_pending") and session.get("pending_login"):
                pending = session["pending_login"]
                u_pending = _load_users().get(pending)
                if u_pending is not None:
                    if u_pending.totp_secret:
                        return redirect(url_for("totp_verify"))
                    if u_pending.require_totp:
                        return redirect(url_for("totp_setup"))
            return render_template("login.html", error=None)
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "")
        # Per-username throttle: defends against distributed brute force
        # that evades the per-IP Flask-Limiter. We deliberately return the
        # same "Invalid credentials" + 401 that bad creds would so the
        # lockout itself isn't a username-enumeration oracle.
        if security.is_user_locked(username):
            security.audit_request("login", username=username or "?",
                                   result="locked")
            return render_template("login.html", error="Invalid credentials"), 401
        result = _check_credentials(username, password)
        if not result:
            tripped = security.record_login_failure(username)
            security.audit_request("login", username=username or "?",
                                   result="bad_creds",
                                   tripped_lockout=tripped or None)
            return render_template("login.html", error="Invalid credentials"), 401
        # Credentials accepted (or reset-token recognised) — clear the
        # per-username throttle so honest typos don't accumulate.
        security.clear_login_failures(username)
        if result.get("needs_reset"):
            # Burn the one-time reset token the instant it is accepted, not
            # only when /set-password completes. Otherwise an abandoned
            # set-password flow leaves the token replayable for the rest of
            # its 24 h TTL. The set-password step is authorised by the
            # ``pending_reset`` session key below, not by the token, so
            # clearing it here does not strand the user. Idempotent: the
            # set-password mutator clears these fields again on completion.
            def _burn_reset_token(users: dict[str, UserConfig]) -> None:
                uu = users.get(username)
                if uu is not None:
                    uu.reset_token        = None
                    uu.reset_token_expiry = None

            _update_users(_burn_reset_token)
            session.clear()
            session["pending_reset"] = username
            security.audit_request("login", username=username,
                                   result="needs_reset")
            return redirect(url_for("set_password"))
        # TOTP gating: if the user is enrolled OR is required to enrol,
        # password alone is not enough — defer to step 2.
        users = _load_users()
        u = users.get(username)
        if u and (u.totp_secret or u.require_totp):
            session.clear()
            session["pending_login"] = username
            if not u.totp_secret:
                # require_totp=True but not enrolled yet → onboarding screen.
                security.audit_request("login", username=username,
                                       result="totp_enroll_required")
                return redirect(url_for("totp_setup"))
            security.audit_request("login", username=username,
                                   result="totp_required")
            return redirect(url_for("totp_verify"))
        return _complete_login(username, u, result)

    # ── /l/<token> (magic login) ─────────────────────────────────────────

    @app.route("/l/<token>")
    @limiter.limit("10 per minute")
    @limiter.limit("60 per hour")
    def magic_login(token: str):
        """One-click magic-login link.

        The ``token`` in the path IS the credential. We look up the user
        whose stored ``magic_token`` hash matches (constant-time inside
        ``check_password_hash``), honour the account-level ``expires_at``,
        and then establish a normal 7-day session — no password, no TOTP.
        Deliberately frictionless for a short-lived, station-scoped guest
        account; the security boundary is the unguessable 32-byte token
        plus the account expiry, not a second factor.

        On any miss we fall through to the standard login page with a
        generic error so the link can't be probed as an oracle.
        """
        users = _load_users()
        matched: tuple[str, UserConfig] | None = None
        for uname, u in users.items():
            if not u.magic_token:
                continue
            # No early break — keep the work uniform across the (tiny) user
            # set so a match isn't distinguishable by timing.
            if check_password_hash(u.magic_token, token):
                matched = (uname, u)
        if matched is None:
            security.audit_request("magic_login", username="?",
                                   result="bad_token")
            return render_template("login.html",
                                   error="This link is invalid or has expired."), 401
        username, u = matched
        # Re-check role at redemption, not just at mint. The mint endpoint
        # refuses admin accounts, but an account could be promoted to admin
        # AFTER a link was issued — a stale host-era token must never log in
        # as the now-admin account. (The user-PATCH path also clears the
        # token on a role change; this is the belt to that suspenders.)
        if u.role == "admin":
            security.audit_request("magic_login", username=username,
                                   result="admin_refused")
            return render_template(
                "login.html",
                error="This link is invalid or has expired."), 401
        # Account-level expiry — same gate as password login in
        # _check_credentials. A magic link never outlives its account.
        if u.expires_at:
            try:
                exp = datetime.fromisoformat(u.expires_at).astimezone(timezone.utc)
                if datetime.now(timezone.utc) > exp:
                    security.audit_request("magic_login", username=username,
                                           result="expired")
                    return render_template(
                        "login.html",
                        error="This link is invalid or has expired."), 401
            except ValueError:
                pass  # malformed expiry → fail safe to normal (non-expired) path
        # Single-factor login (the URL token) → downgrade to a read-only role
        # so a leaked link can never write, regardless of the account's own
        # role. Admin is already refused above; host loses write via the link
        # (they can still password+TOTP for write access).
        result = {"role": _MAGIC_SESSION_ROLE, "stations": u.stations,
                  "display_name": u.display_name}
        security.audit_request("magic_login", username=username, result="ok",
                               downgraded_role=_MAGIC_SESSION_ROLE)
        resp = _complete_login(username, u, result)
        # _complete_login establishes a permanent (90-day) session; override it
        # for magic links: a browser-session cookie plus an absolute expiry
        # re-checked each request by _enforce_magic_session_ttl. (Session stays
        # mutable here — Flask serialises the cookie after the view returns.)
        session.permanent = False
        session["magic"] = True
        session["magic_exp"] = (
            datetime.now(timezone.utc)
            + timedelta(seconds=_MAGIC_SESSION_TTL_SECONDS)
        ).isoformat()
        return resp

    # ── /totp/verify ─────────────────────────────────────────────────────

    @app.route("/totp/verify", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    @limiter.limit("60 per hour", methods=["POST"])
    def totp_verify():
        username = session.get("pending_login")
        if not username:
            return redirect(url_for("login"))
        users = _load_users()
        u = users.get(username)
        if not u or not u.totp_secret:
            session.clear()
            return redirect(url_for("login"))
        # Per-username lockout shared with /login. Without this, a valid
        # password + pending-login session could be used to brute-force the
        # 1M-entry six-digit codespace within the ±30 s window. Returning a
        # uniform "Invalid code" keeps the lockout from doubling as an
        # account-state oracle.
        if security.is_user_locked(username):
            security.audit_request("login", username=username,
                                   result="totp_locked")
            session.pop("pending_login", None)
            return render_template("totp_verify.html",
                                   error="Invalid code"), 401
        if request.method == "GET":
            return render_template("totp_verify.html", error=None)
        code = (request.form.get("code") or "").strip()
        if not security.verify_totp(u.totp_secret, code):
            tripped = security.record_login_failure(username)
            security.audit_request("login", username=username,
                                   result="bad_totp",
                                   tripped_lockout=tripped or None)
            if tripped:
                # Force restart from /login once the threshold is reached.
                # The lockout itself will block re-entry there until it
                # expires; clearing pending_login prevents the attacker
                # from re-submitting against the same pre-login session.
                session.pop("pending_login", None)
            return render_template("totp_verify.html",
                                   error="Invalid code"), 401
        # Successful TOTP — drop any accumulated failures from honest typos
        # on the same account so the next login starts with a fresh budget.
        security.clear_login_failures(username)
        result = {"role": u.role, "stations": u.stations,
                  "display_name": u.display_name}
        return _complete_login(username, u, result)

    # ── /totp/setup ──────────────────────────────────────────────────────

    @app.route("/totp/setup", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def totp_setup():
        """First-time TOTP enrolment.

        Triggered when the user has `require_totp=True` but no
        `totp_secret` yet. The proposed secret is stashed in the session
        until the user confirms it by entering a valid 6-digit code.
        """
        username = (session.get("pending_login")
                    or session.get("user"))  # also reachable post-login
        if not username:
            return redirect(url_for("login"))
        users = _load_users()
        u = users.get(username)
        if not u:
            session.clear()
            return redirect(url_for("login"))
        if request.method == "GET":
            secret = u.totp_secret or session.get("totp_setup_secret")
            if not secret:
                secret = security.generate_totp_secret()
                session["totp_setup_secret"] = secret
            uri = security.totp_provisioning_uri(secret, username)
            return render_template(
                "totp_setup.html",
                secret=secret,
                qr_data_uri=security.totp_qr_data_uri(uri),
                error=None,
            )
        # POST: verify the code, persist on success.
        secret = u.totp_secret or session.get("totp_setup_secret") or ""
        code = (request.form.get("code") or "").strip()
        if not security.verify_totp(secret, code):
            uri = security.totp_provisioning_uri(secret, username)
            security.audit_request("totp_setup", username=username,
                                   result="bad_code")
            return render_template(
                "totp_setup.html",
                secret=secret,
                qr_data_uri=security.totp_qr_data_uri(uri),
                error="Invalid code — try again",
            ), 401
        user_snapshot: dict[str, Any] = {}

        def _mutate(users: dict[str, UserConfig]) -> None:
            uu = users.get(username)
            if uu is None:
                return
            uu.totp_secret = secret
            uu.require_totp = True
            user_snapshot["role"] = uu.role
            user_snapshot["stations"] = uu.stations
            user_snapshot["display_name"] = uu.display_name
            user_snapshot["totp_secret"] = uu.totp_secret

        _update_users(_mutate)
        session.pop("totp_setup_secret", None)
        security.audit_request("totp_setup", username=username, result="ok")
        if not user_snapshot:
            session.clear()
            return redirect(url_for("login"))
        if session.get("pending_login") == username:
            u_fresh = _load_users().get(username)
            result = {"role": user_snapshot["role"],
                      "stations": user_snapshot["stations"],
                      "display_name": user_snapshot["display_name"]}
            return _complete_login(username, u_fresh, result)
        return redirect(url_for("index"))

    # ── /set-password ────────────────────────────────────────────────────

    @app.route("/set-password", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def set_password():
        username = session.get("pending_reset")
        if not username:
            return redirect(url_for("login"))
        if request.method == "GET":
            return render_template("set_password.html", error=None)
        new_pw  = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        # Strict floor — short passwords are the single biggest auth risk.
        # NIST 800-63B recommends 8 chars minimum; we go higher because
        # operator turnover is low and we want defence against credential
        # stuffing from breached lookup lists.
        if len(new_pw) < 14:
            return render_template("set_password.html",
                                   error="Password must be at least 14 characters")
        if new_pw != confirm:
            return render_template("set_password.html",
                                   error="Passwords do not match")
        pw_hash = generate_password_hash(new_pw)
        user_snapshot: dict[str, Any] = {}

        class _NoSuchUser(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NoSuchUser()
            u = users[username]
            u.password_hash      = pw_hash
            u.reset_token        = None
            u.reset_token_expiry = None
            # Setting a new password is a privilege-relevant change: bump the
            # session epoch so any *other* live session for this account is
            # force-revalidated against users.yaml on its next request (audit
            # H3), keeping the role/station snapshot from drifting stale.
            u.session_epoch      = (u.session_epoch or 0) + 1
            user_snapshot["require_totp"] = u.require_totp
            user_snapshot["totp_secret"]  = u.totp_secret
            user_snapshot["role"]         = u.role
            user_snapshot["stations"]     = u.stations
            user_snapshot["session_epoch"] = u.session_epoch

        try:
            _update_users(_mutate)
        except _NoSuchUser:
            session.clear()
            return redirect(url_for("login"))
        security.audit_request("set_password", username=username, result="ok")
        session.clear()
        session["_csid"]     = secrets.token_urlsafe(16)
        if user_snapshot["require_totp"] and not user_snapshot["totp_secret"]:
            session["pending_login"] = username
            return redirect(url_for("totp_setup"))
        session.permanent    = True
        session["user"]      = username
        session["role"]      = user_snapshot["role"]
        session["stations"]  = user_snapshot["stations"]
        session["admin"]     = username if user_snapshot["role"] == "admin" else None
        session["session_epoch"] = user_snapshot["session_epoch"]
        return redirect(url_for("index"))

    # ── /logout ──────────────────────────────────────────────────────────

    @app.route("/logout")
    def logout():
        user = session.get("user")
        session.clear()
        if user:
            security.audit_request("logout", username=user, result="ok")
        return redirect(url_for("index"))

    # ── /api/auth/status ─────────────────────────────────────────────────

    @app.route("/api/auth/status")
    @security.public_route
    def api_auth_status():
        # Re-resolve role + station assignments from users.yaml instead of
        # trusting the login-time session snapshot. Sessions are client-side
        # cookies that live 90 days, so a station assigned to a host *after*
        # their last login would otherwise stay invisible to them until they
        # re-logged in — the live-feed / ownership gate keys off this list, and
        # so does the per-station write gate. Refreshing here (the frontend
        # calls this on every page load) propagates the current assignment back
        # into the session for all subsequent requests. _load_users() is
        # memoised on file mtime, so this is a cheap stat on the hot path.
        username = session.get("user")
        if username:
            u = _load_users().get(username)
            if u is not None:
                session["role"]     = u.role
                session["stations"] = u.stations
                session["admin"]    = username if u.role == "admin" else None
        role = _session_role()
        return jsonify({
            "user":     username,
            "role":     role,
            "stations": session.get("stations", []),
            "admin":    role == "admin",
        })
