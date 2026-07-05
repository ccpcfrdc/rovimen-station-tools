"""Access-control tests for the data-route guards (issues #522, #557).

Fleet-wide-read rule: every signed-in account (admin/host/visitor/press) may
VIEW every station's data and videos; only writes stay scoped (admin any
station, host its own). Regression guard against (a) #522, where valid roles
got a 403 on data, and (b) #557, where ``host`` users were restricted to their
assigned stations for *viewing* too.

These tests drive the real ``require_station``/``require_admin`` decorators
through a minimal Flask app and the Flask test client, so they exercise the
genuine session → role → access path rather than a re-implementation.
"""

from __future__ import annotations

import pytest
from flask import Flask, jsonify

import auth


@pytest.fixture
def client():
    app = Flask(__name__)
    app.secret_key = "test-secret"

    @app.route("/api/vitals/<host_key>", methods=["GET"])
    @auth.require_station
    def vitals(host_key: str):
        return jsonify(ok=True)

    @app.route("/api/settings/<host_key>", methods=["POST"])
    @auth.require_station
    def settings(host_key: str):
        return jsonify(ok=True)

    @app.route("/api/admin/thing", methods=["GET"])
    @auth.require_admin
    def admin_thing():
        return jsonify(ok=True)

    return app.test_client()


def _login(client, role: str, stations: list[str] | None = None) -> None:
    with client.session_transaction() as sess:
        sess["user"] = "alice"
        sess["role"] = role
        sess["stations"] = stations or []


# ── Read access to data views ────────────────────────────────────────────


@pytest.mark.parametrize("role", ["admin", "visitor", "press"])
def test_fleet_read_roles_can_view_any_station(client, role):
    """admin/visitor/press are fleet-wide read roles — they must not 403."""
    _login(client, role)
    resp = client.get("/api/vitals/gmnro02")
    assert resp.status_code == 200


def test_press_role_can_read_data(client):
    """Core #522 regression: a logged-in press user must NOT get 403 on data."""
    _login(client, "press")
    assert client.get("/api/vitals/gmnro02").status_code == 200


def test_host_can_read_own_station(client):
    _login(client, "host", stations=["gmnro02"])
    assert client.get("/api/vitals/gmnro02").status_code == 200


def test_host_can_read_other_station(client):
    """Fleet-wide read: a host views any station, not just its assigned ones."""
    _login(client, "host", stations=["gmnro02"])
    assert client.get("/api/vitals/gmnro07").status_code == 200


# ── Anonymous / guest must stay locked out ───────────────────────────────


def test_guest_gets_401_on_data(client):
    """No session → not authenticated → 401, never silently granted access."""
    assert client.get("/api/vitals/gmnro02").status_code == 401


def test_unrecognised_role_fails_closed(client):
    """An unknown role is not a fleet read role and owns no stations → 403."""
    _login(client, "bogus")
    assert client.get("/api/vitals/gmnro02").status_code == 403


# ── Read-only roles cannot write ─────────────────────────────────────────


@pytest.mark.parametrize("role", ["visitor", "press"])
def test_read_only_roles_rejected_on_write(client, role):
    """visitor/press may read but never mutate station state."""
    _login(client, role)
    assert client.post("/api/settings/gmnro02").status_code == 403


def test_admin_can_write(client):
    _login(client, "admin")
    assert client.post("/api/settings/gmnro02").status_code == 200


def test_host_can_write_own_station(client):
    _login(client, "host", stations=["gmnro02"])
    assert client.post("/api/settings/gmnro02").status_code == 200


def test_host_cannot_write_other_station(client):
    """Writes stay scoped: a host may not mutate a station it doesn't own."""
    _login(client, "host", stations=["gmnro02"])
    assert client.post("/api/settings/gmnro07").status_code == 403


# ── Admin-only routes still gate non-admins ──────────────────────────────


@pytest.mark.parametrize("role", ["visitor", "press", "host"])
def test_non_admin_roles_blocked_from_admin_route(client, role):
    _login(client, role, stations=["gmnro02"])
    assert client.get("/api/admin/thing").status_code == 403


def test_guest_blocked_from_admin_route(client):
    """A guest hitting an admin /api/ route gets 403 (require_admin's path)."""
    assert client.get("/api/admin/thing").status_code == 403


def test_admin_allowed_on_admin_route(client):
    _login(client, "admin")
    assert client.get("/api/admin/thing").status_code == 200
