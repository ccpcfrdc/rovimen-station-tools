"""Regression test: /api/auth/status must reflect the *current* users.yaml
role + station assignments, not the login-time session snapshot.

Sessions are client-side cookies that live 90 days. Before this fix, a station
assigned to a host *after* their last login stayed invisible to them until they
re-logged in: the live-feed / ownership gate (and the per-station write gate)
key off ``session['stations']``, which was frozen at login. A freshly-logged-in
admin saw the cameras; the operator saw "No cameras configured for this
station". This guards that /api/auth/status re-resolves from users.yaml and
writes the fresh values back into the session.
"""

from __future__ import annotations

import pytest
from flask import Flask

import routes.auth_routes as auth_routes
from models import UserConfig
from routes.auth_routes import register_auth_routes


class _NoopLimiter:
    """Stand-in for flask-limiter: ``.limit(...)`` is a passthrough decorator."""

    def limit(self, *_args, **_kwargs):
        def _decorator(fn):
            return fn
        return _decorator


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app._limiter = _NoopLimiter()  # type: ignore[attr-defined]
    register_auth_routes(app, config=None, cache=None, tunnels=None)
    return app.test_client()


def _seed_users(monkeypatch, **users: UserConfig) -> None:
    # The route closes over the name imported into auth_routes, so patch it
    # there rather than on the auth module.
    monkeypatch.setattr(auth_routes, "_load_users", lambda: dict(users))


def test_status_picks_up_station_assigned_after_login(client, monkeypatch):
    # users.yaml now grants the host gmnro13 — but the live session predates it.
    _seed_users(monkeypatch,
                andrei=UserConfig(role="host", stations=["gmnro13"]))
    with client.session_transaction() as sess:
        sess["user"] = "andrei"
        sess["role"] = "host"
        sess["stations"] = []  # stale: assigned before gmnro13 existed

    body = client.get("/api/auth/status").get_json()
    assert body["role"] == "host"
    assert body["stations"] == ["gmnro13"]
    assert body["admin"] is False


def test_status_writes_refreshed_stations_back_into_session(client, monkeypatch):
    _seed_users(monkeypatch,
                andrei=UserConfig(role="host", stations=["gmnro13"]))
    with client.session_transaction() as sess:
        sess["user"] = "andrei"
        sess["role"] = "host"
        sess["stations"] = []

    client.get("/api/auth/status")
    with client.session_transaction() as sess:
        assert sess["stations"] == ["gmnro13"]


def test_status_reflects_role_change(client, monkeypatch):
    _seed_users(monkeypatch, alex=UserConfig(role="admin", stations=[]))
    with client.session_transaction() as sess:
        sess["user"] = "alex"
        sess["role"] = "host"  # stale, since promoted to admin
        sess["stations"] = ["gmnro02"]

    body = client.get("/api/auth/status").get_json()
    assert body["role"] == "admin"
    assert body["admin"] is True


def test_status_unchanged_when_user_absent_from_users_file(client, monkeypatch):
    # Env-defined admins (or removed accounts) aren't in users.yaml — don't
    # clobber their session.
    _seed_users(monkeypatch)  # empty users.yaml
    with client.session_transaction() as sess:
        sess["user"] = "envadmin"
        sess["role"] = "admin"
        sess["stations"] = []

    body = client.get("/api/auth/status").get_json()
    assert body["role"] == "admin"
    assert body["admin"] is True


# ─────────────────────────────────────────────────────────────────────────
# Privilege-revocation: session-epoch revalidation (audit H3)
#
# The 90-day session cookie snapshots role/stations at login. An admin
# de-scoping or demoting a user bumps ``session_epoch``; the before_request
# revalidation hook must then force the live session to adopt the current
# privilege level (or be cleared on expiry/deletion) on the *next* request,
# not only when the user happens to poll /api/auth/status.
# ─────────────────────────────────────────────────────────────────────────

import auth as auth_module  # noqa: E402


@pytest.fixture
def gated_client(monkeypatch):
    """A minimal app wiring the real revalidation hook + auth gate in front
    of a protected route, exactly as create_app() orders them."""
    import security

    app = Flask(__name__)
    app.secret_key = "test-secret"

    @app.route("/api/whoami")
    def whoami():
        from flask import session as s
        return {
            "user": s.get("user"),
            "role": s.get("role"),
            "stations": s.get("stations", []),
            "epoch": s.get("session_epoch"),
        }

    # Same registration order as rovimen_dashboard.create_app: revalidation
    # BEFORE the auth gate so a cleared session fails closed on this request.
    auth_module.install_session_revalidation(app)
    security.install_auth_gate(app)
    return app.test_client()


def _seed_auth_users(monkeypatch, **users: UserConfig) -> None:
    # revalidate_session() closes over the name in the auth module.
    monkeypatch.setattr(auth_module, "_load_users", lambda: dict(users))


def test_revalidate_refreshes_stations_on_epoch_bump(gated_client, monkeypatch):
    # users.yaml now reflects a de-scope (gmnro13 removed) AND a bumped epoch.
    _seed_auth_users(monkeypatch,
                     andrei=UserConfig(role="host", stations=[],
                                       session_epoch=1))
    with gated_client.session_transaction() as sess:
        sess["user"] = "andrei"
        sess["role"] = "host"
        sess["stations"] = ["gmnro13"]   # stale grant from before the de-scope
        sess["session_epoch"] = 0        # pre-bump snapshot

    body = gated_client.get("/api/whoami").get_json()
    assert body["stations"] == []        # live value adopted
    assert body["epoch"] == 1            # epoch re-stamped


def test_revalidate_demotes_admin_to_host_on_epoch_bump(gated_client, monkeypatch):
    _seed_auth_users(monkeypatch,
                     alex=UserConfig(role="host", stations=["gmnro02"],
                                     session_epoch=5))
    with gated_client.session_transaction() as sess:
        sess["user"] = "alex"
        sess["role"] = "admin"           # stale: was admin, now demoted
        sess["stations"] = []
        sess["session_epoch"] = 4

    body = gated_client.get("/api/whoami").get_json()
    assert body["role"] == "host"
    assert body["epoch"] == 5


def test_revalidate_noop_when_epoch_matches(gated_client, monkeypatch):
    # No privilege change → session left exactly as-is, no surprise refresh.
    _seed_auth_users(monkeypatch,
                     andrei=UserConfig(role="host", stations=["gmnro13"],
                                       session_epoch=3))
    with gated_client.session_transaction() as sess:
        sess["user"] = "andrei"
        sess["role"] = "host"
        sess["stations"] = ["gmnro13"]
        sess["session_epoch"] = 3

    body = gated_client.get("/api/whoami").get_json()
    assert body["stations"] == ["gmnro13"]
    assert body["epoch"] == 3


def test_revalidate_clears_session_when_account_expired(gated_client, monkeypatch):
    # Past expires_at → the hook clears the session; the auth gate (registered
    # after) then 401s the API request on the SAME request (fail closed).
    _seed_auth_users(monkeypatch,
                     tester=UserConfig(role="host", stations=[],
                                       expires_at="2000-01-01T00:00:00+00:00"))
    with gated_client.session_transaction() as sess:
        sess["user"] = "tester"
        sess["role"] = "host"
        sess["session_epoch"] = 0

    resp = gated_client.get("/api/whoami")
    assert resp.status_code == 401       # gate saw no user after the clear


def test_revalidate_ignores_env_admin_absent_from_file(gated_client, monkeypatch):
    # Accounts not in users.yaml (env-var admins) must not be clobbered.
    _seed_auth_users(monkeypatch)        # empty file
    with gated_client.session_transaction() as sess:
        sess["user"] = "envadmin"
        sess["role"] = "admin"
        sess["session_epoch"] = 0

    body = gated_client.get("/api/whoami").get_json()
    assert body["user"] == "envadmin"
    assert body["role"] == "admin"


# ─────────────────────────────────────────────────────────────────────────
# CF Access bridge must not bypass MFA (audit H4)
#
# Seeding a full session from the verified email claim alone silently
# downgrades a TOTP-enrolled (or TOTP-required) account to single factor.
# The bridge must instead seed only a pending login and defer to /totp/*.
# ─────────────────────────────────────────────────────────────────────────


def _drive_cf_bridge(monkeypatch, user: UserConfig, email: str = "u@example.com"):
    """Run _cf_access_before_request() inside a request context with a stubbed
    verified CF claim, returning the resulting Flask session dict (a copy)."""
    app = Flask(__name__)
    app.secret_key = "test-secret"
    monkeypatch.setattr(
        auth_module.cf_access, "verify_request_token",
        lambda headers, cookies: {"email": email},
    )
    monkeypatch.setattr(
        auth_module, "_find_user_by_email",
        lambda e: ("u", user) if e == email else None,
    )
    from flask import session as s
    with app.test_request_context("/"):
        auth_module._cf_access_before_request()
        return dict(s)


def test_cf_bridge_totp_enrolled_user_gets_no_full_session(monkeypatch):
    sess = _drive_cf_bridge(
        monkeypatch,
        UserConfig(role="host", stations=["gmnro02"],
                   totp_secret="JBSWY3DPEHPK3PXP", require_totp=True),
    )
    # NOT a full session: no authenticated principal, no role/stations granted.
    assert "user" not in sess
    assert "role" not in sess
    # Only a pending login, marked as CF-proven, so /totp/verify can finish it.
    assert sess.get("pending_login") == "u"
    assert sess.get("cf_pending") is True


def test_cf_bridge_totp_required_unenrolled_user_gets_no_full_session(monkeypatch):
    # require_totp=True but not yet enrolled (no secret) → still must not be a
    # full session; flow routes to enrolment.
    sess = _drive_cf_bridge(
        monkeypatch,
        UserConfig(role="host", stations=[], totp_secret=None,
                   require_totp=True),
    )
    assert "user" not in sess
    assert sess.get("pending_login") == "u"
    assert sess.get("cf_pending") is True


def test_cf_bridge_non_totp_user_still_gets_full_session(monkeypatch):
    # No TOTP at all → CF Access is the only factor by design; full session is
    # granted, the factor is recorded honestly as "cf" (never "totp"), and the
    # epoch is stamped for revalidation.
    sess = _drive_cf_bridge(
        monkeypatch,
        UserConfig(role="host", stations=["gmnro02"], totp_secret=None,
                   require_totp=False, session_epoch=2),
    )
    assert sess.get("user") == "u"
    assert sess.get("role") == "host"
    assert sess.get("mfa") == "cf"
    assert sess.get("session_epoch") == 2


# ─────────────────────────────────────────────────────────────────────────
# One-time reset token must burn on acceptance, not on set-password
# completion (MEDIUM). An abandoned set-password flow previously left the
# token replayable for the rest of its 24 h TTL.
# ─────────────────────────────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402

from werkzeug.security import generate_password_hash  # noqa: E402


@pytest.fixture
def login_client(monkeypatch):
    """A client wired to the real /login route over an in-memory user store,
    so the reset-token burn (an _update_users write) is genuinely exercised."""
    import security

    # No lockout / audit side effects in tests.
    monkeypatch.setattr(security, "is_user_locked", lambda u: False)
    monkeypatch.setattr(security, "record_login_failure", lambda u: False)
    monkeypatch.setattr(security, "clear_login_failures", lambda u: None)
    monkeypatch.setattr(security, "audit_request", lambda *a, **k: None)

    # Point Jinja at the real dashboard templates so the 401 path (which
    # renders login.html) resolves.
    import os
    templates_dir = os.path.join(os.path.dirname(auth_module.__file__), "templates")
    app = Flask(__name__, template_folder=templates_dir)
    app.secret_key = "test-secret"
    app._limiter = _NoopLimiter()  # type: ignore[attr-defined]
    # login.html embeds {{ csrf_token() }} (Flask-WTF in prod). Stub it so the
    # template renders without wiring CSRFProtect into this minimal app.
    app.jinja_env.globals["csrf_token"] = lambda: ""

    # ``index`` is referenced by url_for in the login flow but lives in the
    # main dashboard module; register a thin stand-in so redirects resolve.
    # ``set_password`` is registered by register_auth_routes itself.
    @app.route("/")
    def index():
        return "home"

    register_auth_routes(app, config=None, cache=None, tunnels=None)
    return app


def test_reset_token_burned_on_login_and_cannot_replay(login_client, monkeypatch):
    token = "one-time-reset-token-value"
    store = {
        "bob": UserConfig(
            role="host",
            stations=["gmnro02"],
            password_hash="",  # no usable password yet (first-login reset)
            reset_token=generate_password_hash(token),
            reset_token_expiry=(
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).isoformat(),
        )
    }

    def _update(mutator):
        mutator(store)

    # _check_credentials reads from the auth module's _load_users; the route's
    # own TOTP check + the burn write go through the auth_routes names.
    monkeypatch.setattr(auth_module, "_load_users", lambda: store)
    monkeypatch.setattr(auth_routes, "_load_users", lambda: store)
    monkeypatch.setattr(auth_routes, "_update_users", _update)

    client = login_client.test_client()

    # First use: token accepted → redirect into the set-password flow.
    r1 = client.post("/login", data={"username": "bob", "password": token})
    assert r1.status_code == 302
    assert "/set-password" in r1.headers["Location"]
    # And the token is gone immediately, even though set-password never ran.
    assert store["bob"].reset_token is None
    assert store["bob"].reset_token_expiry is None

    # Replay the very same token: it must no longer authenticate. With no
    # password set and the token burned, credentials are rejected (401).
    with client.session_transaction() as sess:
        sess.clear()  # drop the pending_reset grant from the first call
    r2 = client.post("/login", data={"username": "bob", "password": token})
    assert r2.status_code == 401


# ─────────────────────────────────────────────────────────────────────────
# Admin mutators bump session_epoch on privilege-relevant writes (audit H3),
# which is what drives the revalidation above. Exercises the real PATCH /
# reset-password routes over an in-memory store.
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def admin_client(monkeypatch):
    import rovimen_dashboard as rd
    from admin_config import register_admin_user_routes

    store: dict[str, UserConfig] = {}

    def _update(mutator):
        mutator(store)

    monkeypatch.setattr(rd, "_load_users", lambda: store)
    monkeypatch.setattr(rd, "_update_users", _update)
    # Bypass the admin gate for the test — we're exercising mutator logic.
    monkeypatch.setattr(rd, "require_admin", lambda f: f)

    app = Flask(__name__)
    app.secret_key = "test-secret"
    register_admin_user_routes(app)
    return app.test_client(), store


def test_patch_role_change_bumps_epoch(admin_client):
    client, store = admin_client
    store["bob"] = UserConfig(role="host", stations=["gmnro02"], session_epoch=0)
    r = client.patch("/api/admin/users/bob", json={"role": "visitor"})
    assert r.status_code == 200
    assert store["bob"].role == "visitor"
    assert store["bob"].session_epoch == 1


def test_patch_stations_change_bumps_epoch(admin_client):
    client, store = admin_client
    store["bob"] = UserConfig(role="host", stations=["gmnro02"], session_epoch=2)
    r = client.patch("/api/admin/users/bob", json={"stations": []})
    assert r.status_code == 200
    assert store["bob"].stations == []
    assert store["bob"].session_epoch == 3


def test_patch_display_name_only_does_not_bump_epoch(admin_client):
    client, store = admin_client
    store["bob"] = UserConfig(role="host", stations=["gmnro02"],
                              display_name="Bob", session_epoch=4)
    r = client.patch("/api/admin/users/bob", json={"display_name": "Bobby"})
    assert r.status_code == 200
    assert store["bob"].display_name == "Bobby"
    assert store["bob"].session_epoch == 4  # unchanged — no privilege change


def test_patch_password_change_bumps_epoch(admin_client):
    client, store = admin_client
    store["bob"] = UserConfig(role="host", session_epoch=0)
    r = client.patch("/api/admin/users/bob",
                     json={"password": "averylongpassword123"})
    assert r.status_code == 200
    assert store["bob"].session_epoch == 1


def test_reset_password_route_bumps_epoch(admin_client):
    client, store = admin_client
    store["bob"] = UserConfig(role="host", session_epoch=0)
    r = client.post("/api/admin/users/bob/reset-password")
    assert r.status_code == 200
    assert store["bob"].reset_token is not None
    assert store["bob"].session_epoch == 1
