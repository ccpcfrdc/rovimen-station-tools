"""Tests for dashboard-side command routing (dashboard/command_dispatch.py + routes).

A push-migrated station (``push_enabled: true``) holds no inbound port, so the
dashboard must enqueue a *signed* command instead of POSTing/PATCHing its :7779
API directly (docs/reversed_http_push_design.md §3). A station without the flag
(the default fleet state) must keep the exact direct-HTTP path.

Invariants under test:
  * push_enabled station -> reboot / restart / settings-PATCH / archive-test
    enqueue a signed command (verifiable), return 202, and do NOT hit the network
  * non-push station    -> the direct HTTP call is made (outbound mocked) and no
    command is enqueued
  * a bad command type is rejected before signing
  * missing signing key -> SigningUnavailable (routes map to 503)
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from flask import Flask

import command_dispatch
import command_signing
import command_store
from models import DashboardConfig, StationConfig


# ── command_dispatch unit-level ──────────────────────────────────────────────


@pytest.fixture
def signed_env(tmp_path, monkeypatch):
    priv = tmp_path / "signing.pem"
    pub = tmp_path / "signing.pub.pem"
    cmd_db = tmp_path / "commands.db"
    command_signing.generate_keypair(priv, pub)
    monkeypatch.setattr(command_signing, "DEFAULT_SIGNING_KEY_PATH", priv)
    monkeypatch.setattr(command_store, "DB_PATH", cmd_db)
    return priv, pub, cmd_db


def test_enqueue_signed_command_round_trip(signed_env):
    priv, pub, cmd_db = signed_env
    cmd_id = command_dispatch.enqueue_signed_command(
        host_key="gmn0002", type="reboot", issued_by="tester",
    )
    assert cmd_id.startswith("cmd_")
    pending = command_store.pending_for_station("gmn0002", path=cmd_db)
    assert len(pending) == 1
    cmd = pending[0]
    assert cmd["type"] == "reboot"
    # Signature verifies against the public key over the canonical message.
    from cryptography.hazmat.primitives import serialization

    pubkey = serialization.load_pem_public_key(pub.read_bytes())
    msg = command_signing.canonical_message(
        id=cmd["id"], station="gmn0002", type="reboot", args=cmd["args"],
        issued_at=cmd["issued_at"], not_after=cmd["not_after"],
    )
    assert command_signing.verify(msg, cmd["sig"], pubkey)


def test_enqueue_rejects_disallowed_type(signed_env):
    with pytest.raises(ValueError):
        command_dispatch.enqueue_signed_command(host_key="gmn0002", type="exec")


def test_run_updater_allowlisted_both_sides():
    """run_updater is allowlisted on the dashboard store and the station worker."""
    import sys

    assert "run_updater" in command_store.ALLOWED_TYPES
    sys.path.insert(0, "rovimen-scripts")
    import rovimen_pusher

    assert "run_updater" in rovimen_pusher.CommandWorker.ALLOWED_TYPES


def test_restart_services_allowlisted_both_sides():
    """restart_services is allowlisted on the dashboard store and station worker."""
    import sys

    assert "restart_services" in command_store.ALLOWED_TYPES
    sys.path.insert(0, "rovimen-scripts")
    import rovimen_pusher

    assert "restart_services" in rovimen_pusher.CommandWorker.ALLOWED_TYPES


def test_enqueue_missing_signing_key_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        command_signing, "DEFAULT_SIGNING_KEY_PATH", tmp_path / "nope.pem"
    )
    monkeypatch.setattr(command_store, "DB_PATH", tmp_path / "commands.db")
    with pytest.raises(command_dispatch.SigningUnavailable):
        command_dispatch.enqueue_signed_command(host_key="gmn0002", type="reboot")


def test_should_push_reads_flag():
    cfg = DashboardConfig(stations={
        "pushy": StationConfig(ip="1.2.3.4", label="Pushy", push_enabled=True),
        "polly": StationConfig(ip="1.2.3.5", label="Polly"),
    })
    assert command_dispatch.should_push(cfg, "pushy")
    assert not command_dispatch.should_push(cfg, "polly")
    assert not command_dispatch.should_push(cfg, "unknown")


# ── route-level: push vs direct ──────────────────────────────────────────────


def _make_config(push: bool) -> DashboardConfig:
    return DashboardConfig(stations={
        "gmn0002": StationConfig(ip="100.64.0.6", label="Vaslui", push_enabled=push),
    })


@pytest.fixture
def ops_app(signed_env, monkeypatch, request):
    """Flask app with station_ops + station_proxy routes for the given push flag."""
    push = getattr(request, "param", False)
    config = _make_config(push)

    from routes.station_ops import register_station_ops_routes
    from routes.station_proxy import register_station_proxy_routes

    app = Flask(__name__)
    app.testing = True
    app.secret_key = "test-secret"

    lock = threading.Lock()
    register_station_ops_routes(
        app, config, tunnels=None, cache=_DummyCache(),
        rms_plots_list_cache={}, rms_plots_list_cache_lock=lock,
        rms_plots_list_ttl=300.0, proxy_cache={}, proxy_cache_lock=lock,
    )
    register_station_proxy_routes(
        app, config, tunnels=None, cache=_DummyCache(),
        timelapses_swr_cache={}, timelapses_swr_inflight=set(),
        timelapses_swr_lock=lock, timelapses_fresh_ttl=60.0,
        platepar_cache={}, platepar_ttl=300.0,
        proxy_cache={}, proxy_cache_lock=lock,
        prefetch_executor=ThreadPoolExecutor(max_workers=1),
        archive_idx=_DummyArchive(),
    )
    return app, config


class _DummyCache:
    def get_status(self, host_key):  # noqa: ANN001
        return {"online": True}


class _DummyArchive:
    def nights(self, cam):  # noqa: ANN001
        return []

    def night_files(self, cam, date):  # noqa: ANN001
        return None


def _admin_client(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["role"] = "admin"
    return client


@pytest.fixture(autouse=True)
def _admin_role(monkeypatch):
    """Make require_station treat our test session as an admin with write access."""
    import auth

    monkeypatch.setattr(auth, "_has_station_access", lambda hk: True, raising=False)
    monkeypatch.setattr(auth, "_has_station_write_access", lambda hk: True, raising=False)


@pytest.mark.parametrize("ops_app", [True], indirect=True)
def test_reboot_push_enabled_enqueues_command(ops_app, monkeypatch, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    # Any outbound HTTP would be a bug on the push path — trip it if called.
    import routes.station_ops as so

    monkeypatch.setattr(
        so, "_session_for_url",
        lambda url: pytest.fail("push path must not hit the network"),
    )
    client = _admin_client(app)
    resp = client.post("/api/reboot/gmn0002")
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["queued"] is True
    assert body["type"] == "reboot"
    assert body["command_id"].startswith("cmd_")
    assert len(command_store.pending_for_station("gmn0002", path=cmd_db)) == 1


@pytest.mark.parametrize("ops_app", [True], indirect=True)
def test_restart_service_push_enabled_enqueues_with_args(ops_app, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    client = _admin_client(app)
    resp = client.post("/api/restart/gmn0002/rms.service")
    assert resp.status_code == 202
    assert resp.get_json()["type"] == "restart_service"
    pending = command_store.pending_for_station("gmn0002", path=cmd_db)
    assert pending[0]["args"] == {"service": "rms.service"}


@pytest.mark.parametrize("ops_app", [True], indirect=True)
def test_archive_test_push_enabled_enqueues_trigger_upload(ops_app, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    client = _admin_client(app)
    resp = client.post("/api/archive/test/gmn0002")
    assert resp.status_code == 202
    assert resp.get_json()["type"] == "trigger_upload"


@pytest.mark.parametrize("ops_app", [True], indirect=True)
def test_updater_run_push_enabled_enqueues_run_updater(ops_app, monkeypatch, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    import routes.station_ops as so

    monkeypatch.setattr(
        so, "_session_for_url",
        lambda url: pytest.fail("push path must not hit the network"),
    )
    client = _admin_client(app)
    resp = client.post("/api/updater/run/gmn0002")
    assert resp.status_code == 202
    assert resp.get_json()["type"] == "run_updater"
    pending = command_store.pending_for_station("gmn0002", path=cmd_db)
    assert len(pending) == 1
    assert pending[0]["type"] == "run_updater"
    assert pending[0]["args"] == {}


@pytest.mark.parametrize("ops_app", [True], indirect=True)
def test_services_restart_push_enabled_enqueues_restart_services(ops_app, monkeypatch, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    import routes.station_ops as so

    monkeypatch.setattr(
        so, "_session_for_url",
        lambda url: pytest.fail("push path must not hit the network"),
    )
    client = _admin_client(app)
    resp = client.post("/api/services/restart/gmn0002")
    assert resp.status_code == 202
    assert resp.get_json()["type"] == "restart_services"
    pending = command_store.pending_for_station("gmn0002", path=cmd_db)
    assert len(pending) == 1
    assert pending[0]["type"] == "restart_services"
    assert pending[0]["args"] == {}


@pytest.mark.parametrize("ops_app", [True], indirect=True)
def test_updater_check_push_enabled_enqueues_run_updater_check(ops_app, monkeypatch, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    import routes.station_ops as so

    monkeypatch.setattr(
        so, "_session_for_url",
        lambda url: pytest.fail("push path must not hit the network"),
    )
    client = _admin_client(app)
    resp = client.post("/api/updater/check/gmn0002")
    assert resp.status_code == 202
    assert resp.get_json()["type"] == "run_updater"
    pending = command_store.pending_for_station("gmn0002", path=cmd_db)
    assert pending[0]["type"] == "run_updater"
    assert pending[0]["args"] == {"check": True}


@pytest.mark.parametrize("ops_app", [True], indirect=True)
def test_settings_patch_push_enabled_enqueues_patch_settings(ops_app, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    client = _admin_client(app)
    resp = client.patch("/api/settings/gmn0002", json={"upload_delay": 42})
    assert resp.status_code == 202
    pending = command_store.pending_for_station("gmn0002", path=cmd_db)
    assert pending[0]["type"] == "patch_settings"
    assert pending[0]["args"] == {"settings": {"upload_delay": 42}}


# ── non-push station keeps the direct HTTP path ──────────────────────────────


class _FakeResp:
    status_code = 200

    def json(self):
        return {"status": "rebooting"}

    def raise_for_status(self):
        return None


@pytest.mark.parametrize("ops_app", [False], indirect=True)
def test_reboot_non_push_uses_direct_http(ops_app, monkeypatch, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    import routes.station_ops as so

    calls: list[str] = []

    class _Sess:
        def post(self, url, **kw):  # noqa: ANN001
            calls.append(url)
            return _FakeResp()

    monkeypatch.setattr(so, "station_url", lambda *a, **k: "http://station/api/reboot")
    monkeypatch.setattr(so, "_session_for_url", lambda url: _Sess())

    client = _admin_client(app)
    resp = client.post("/api/reboot/gmn0002")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "rebooting"}
    assert calls == ["http://station/api/reboot"]
    # Nothing was enqueued on the direct path.
    assert command_store.pending_for_station("gmn0002", path=cmd_db) == []


@pytest.mark.parametrize("ops_app", [False], indirect=True)
def test_updater_run_non_push_uses_direct_http(ops_app, monkeypatch, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    import routes.station_ops as so

    calls: list[str] = []

    class _Sess:
        def post(self, url, **kw):  # noqa: ANN001
            calls.append(url)
            return _FakeResp()

    monkeypatch.setattr(so, "station_url", lambda *a, **k: "http://station/api/updater/run")
    monkeypatch.setattr(so, "_session_for_url", lambda url: _Sess())

    client = _admin_client(app)
    resp = client.post("/api/updater/run/gmn0002")
    assert resp.status_code == 200
    assert calls == ["http://station/api/updater/run"]
    # Nothing enqueued on the direct path.
    assert command_store.pending_for_station("gmn0002", path=cmd_db) == []


@pytest.mark.parametrize("ops_app", [False], indirect=True)
def test_services_restart_non_push_uses_direct_http(ops_app, monkeypatch, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    import routes.station_ops as so

    calls: list[str] = []

    class _Sess:
        def post(self, url, **kw):  # noqa: ANN001
            calls.append(url)
            return _FakeResp()

    monkeypatch.setattr(so, "station_url", lambda *a, **k: "http://station/api/services/restart")
    monkeypatch.setattr(so, "_session_for_url", lambda url: _Sess())

    client = _admin_client(app)
    resp = client.post("/api/services/restart/gmn0002")
    assert resp.status_code == 200
    assert calls == ["http://station/api/services/restart"]
    # Nothing enqueued on the direct path.
    assert command_store.pending_for_station("gmn0002", path=cmd_db) == []


@pytest.mark.parametrize("ops_app", [False], indirect=True)
def test_settings_patch_non_push_uses_direct_http(ops_app, monkeypatch, signed_env):
    app, config = ops_app
    _, _, cmd_db = signed_env
    import routes.station_proxy as sp

    calls: list[str] = []

    class _Sess:
        def patch(self, url, **kw):  # noqa: ANN001
            calls.append(url)
            return _FakeResp()

    monkeypatch.setattr(sp, "station_url", lambda *a, **k: "http://station/api/settings")
    monkeypatch.setattr(sp, "_session_for_url", lambda url: _Sess())

    client = _admin_client(app)
    resp = client.patch("/api/settings/gmn0002", json={"upload_delay": 42})
    assert resp.status_code == 200
    assert calls == ["http://station/api/settings"]
    assert command_store.pending_for_station("gmn0002", path=cmd_db) == []
