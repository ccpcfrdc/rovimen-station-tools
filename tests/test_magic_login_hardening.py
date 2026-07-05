"""Magic-login hardening: read-only role + short absolute TTL.

The ``/l/<token>`` link is a single factor (the URL token — no password, no
TOTP). So a redeemed link must (1) confer only a read-only session regardless
of the account's own role, and (2) be bounded by a short absolute expiry that
is re-checked on every request, independent of the 90-day permanent-session
cap. These guard both properties end-to-end through the real Flask handler.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask
from werkzeug.security import generate_password_hash

import auth as auth_mod
import routes.auth_routes as auth_routes
from models import UserConfig
from routes.auth_routes import (
    _MAGIC_SESSION_ROLE,
    _MAGIC_SESSION_TTL_SECONDS,
    register_auth_routes,
)


class _NoopLimiter:
    def limit(self, *_args, **_kwargs):
        def _decorator(fn):
            return fn
        return _decorator


@pytest.fixture
def client(monkeypatch):
    templates = Path(auth_routes.__file__).resolve().parent.parent / "templates"
    app = Flask(__name__, template_folder=str(templates))
    app.secret_key = "test-secret"
    # login.html references csrf_token() (Flask-WTF injects it in prod).
    app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"
    app._limiter = _NoopLimiter()  # type: ignore[attr-defined]
    # A minimal index target for _complete_login's redirect / url_for("index").
    app.add_url_rule("/", "index", lambda: "ok")
    register_auth_routes(app, config=None, cache=None, tunnels=None)
    return app.test_client()


def _seed_users(monkeypatch, **users: UserConfig) -> None:
    monkeypatch.setattr(auth_routes, "_load_users", lambda: dict(users))


def test_magic_role_is_read_only_by_construction():
    """The magic role must be one of the read-only account roles (not admin/host)."""
    assert _MAGIC_SESSION_ROLE in auth_mod._ACCOUNT_ROLES
    assert _MAGIC_SESSION_ROLE not in ("admin", "host")


def test_host_magic_link_downgrades_to_read_only(client, monkeypatch):
    """A host account redeeming a magic link gets a read-only session, marked
    magic, with a future absolute expiry and a non-permanent cookie."""
    token = "tok_host_readonly"
    _seed_users(monkeypatch, andrei=UserConfig(
        role="host", stations=["gmnro13"],
        magic_token=generate_password_hash(token)))

    resp = client.get(f"/l/{token}")
    assert resp.status_code == 302  # redirected into the app after login

    with client.session_transaction() as sess:
        assert sess["user"] == "andrei"
        assert sess["role"] == _MAGIC_SESSION_ROLE   # downgraded from host
        assert sess["role"] != "host"
        assert sess["admin"] is None
        assert sess.get("magic") is True
        assert sess.permanent is False               # not the 90-day session
        exp = datetime.fromisoformat(sess["magic_exp"])
        assert exp > datetime.now(timezone.utc)
        # bounded by the configured TTL (allow a little wall-clock slack)
        assert exp <= datetime.now(timezone.utc) + timedelta(
            seconds=_MAGIC_SESSION_TTL_SECONDS + 5)


def test_admin_magic_link_still_refused(client, monkeypatch):
    """Belt-and-suspenders: an account that is admin at redemption is refused,
    never downgraded-and-admitted."""
    token = "tok_admin_refused"
    _seed_users(monkeypatch, boss=UserConfig(
        role="admin", stations=[],
        magic_token=generate_password_hash(token)))

    resp = client.get(f"/l/{token}")
    assert resp.status_code == 401
    with client.session_transaction() as sess:
        assert "user" not in sess


def test_expired_magic_session_is_cleared_on_api(client):
    """A magic session past its absolute expiry is cleared and 401s on API."""
    with client.session_transaction() as sess:
        sess["user"] = "andrei"
        sess["role"] = _MAGIC_SESSION_ROLE
        sess["magic"] = True
        sess["magic_exp"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    resp = client.get("/api/auth/status")
    assert resp.status_code == 401
    with client.session_transaction() as sess:
        assert "user" not in sess  # session was cleared


def test_expired_magic_session_redirects_pages_to_login(client):
    with client.session_transaction() as sess:
        sess["user"] = "andrei"
        sess["role"] = _MAGIC_SESSION_ROLE
        sess["magic"] = True
        sess["magic_exp"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    resp = client.get("/totp/setup")  # a non-API page route
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_malformed_magic_exp_fails_closed(client):
    """A corrupt magic_exp stamp is treated as expired (fail safe)."""
    with client.session_transaction() as sess:
        sess["user"] = "andrei"
        sess["role"] = _MAGIC_SESSION_ROLE
        sess["magic"] = True
        sess["magic_exp"] = "not-a-date"

    resp = client.get("/api/auth/status")
    assert resp.status_code == 401


def test_non_magic_session_is_untouched(client, monkeypatch):
    """A normal (non-magic) session has no magic_exp and must pass through the
    before_request hook without being cleared."""
    _seed_users(monkeypatch, andrei=UserConfig(role="host", stations=["gmnro13"]))
    with client.session_transaction() as sess:
        sess["user"] = "andrei"
        sess["role"] = "host"
        sess["stations"] = ["gmnro13"]

    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess["user"] == "andrei"  # untouched
