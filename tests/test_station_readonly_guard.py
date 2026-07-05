"""Tests for the station API network read-only guard (station_api.py).

The guard (docs/reversed_http_push_design.md §3 — push migration) makes mutating
endpoints loopback-only when ROVIMEN_STATION_READONLY=1, so a push-migrated
station exposed publicly is not a remote mutation surface. State changes then
flow through the signed command channel, whose CommandWorker POSTs from loopback.

Invariants under test:
  * flag OFF -> mutating endpoints behave exactly as today (no 403 for remote)
  * flag ON  -> non-loopback caller of a mutating endpoint gets 403 station_readonly
  * flag ON  -> loopback caller of a mutating endpoint is NOT blocked
  * flag ON  -> read (GET) endpoints are never blocked, from any address
  * flag ON  -> /api/probe (diagnostic) is never blocked
"""

from __future__ import annotations

import pytest

import station_api


@pytest.fixture
def client(monkeypatch):
    station_api.app.testing = True
    # Neutralise the actual mutating side effects: we only care about the guard,
    # not whether the underlying handler succeeds. Each patched handler returns a
    # trivial 200 so a non-blocked call is distinguishable from a 403.
    def _ok(*_a, **_k):
        return {"ok": True}

    monkeypatch.setattr(station_api, "load_config", lambda: {}, raising=False)
    return station_api.app.test_client()


def _readonly_on(monkeypatch):
    monkeypatch.setenv(station_api.READONLY_ENV, "1")


def _readonly_off(monkeypatch):
    monkeypatch.delenv(station_api.READONLY_ENV, raising=False)


# ── flag OFF: behaviour preserved ────────────────────────────────────────────


def test_reboot_flag_off_not_blocked_from_remote(client, monkeypatch):
    _readonly_off(monkeypatch)
    monkeypatch.setattr(
        station_api.subprocess, "Popen", lambda *a, **k: None, raising=False
    )
    resp = client.post("/api/reboot", environ_overrides={"REMOTE_ADDR": "100.64.0.9"})
    # Not a 403 from the guard — the handler ran (returns 200 rebooting).
    assert resp.status_code != 403
    assert resp.get_json().get("error") != "station_readonly"


def test_settings_patch_flag_off_not_blocked_from_remote(client, monkeypatch):
    _readonly_off(monkeypatch)
    # Bad body -> handler aborts 400; the point is it is NOT a guard 403.
    resp = client.patch(
        "/api/settings", json=[], environ_overrides={"REMOTE_ADDR": "100.64.0.9"}
    )
    assert resp.status_code != 403


# ── flag ON: remote mutations blocked ────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/reboot"),
        ("post", "/api/restart/rms.service"),
        ("patch", "/api/settings"),
        ("post", "/api/lock/RO000H/20260101/RO000H_20260101_000000_color.mkv"),
        ("post", "/api/archive/test"),
        ("post", "/api/updater/run"),
        ("post", "/api/updater/check"),
        ("post", "/api/services/restart"),
    ],
)
def test_mutating_endpoint_blocked_from_remote_when_readonly(
    client, monkeypatch, method, path
):
    _readonly_on(monkeypatch)
    resp = getattr(client, method)(
        path, environ_overrides={"REMOTE_ADDR": "100.64.0.9"}
    )
    assert resp.status_code == 403
    body = resp.get_json()
    assert body == {
        "error": "station_readonly",
        "hint": "route via signed command channel",
    }


@pytest.mark.parametrize("loopback", ["127.0.0.1", "::1"])
def test_mutating_endpoint_allowed_from_loopback_when_readonly(
    client, monkeypatch, loopback
):
    _readonly_on(monkeypatch)
    monkeypatch.setattr(
        station_api.subprocess, "Popen", lambda *a, **k: None, raising=False
    )
    resp = client.post("/api/reboot", environ_overrides={"REMOTE_ADDR": loopback})
    # Loopback is allowed through the guard: the handler ran, no 403.
    assert resp.status_code != 403
    assert resp.get_json().get("error") != "station_readonly"


# ── flag ON: reads and probe never blocked ───────────────────────────────────


def test_get_settings_never_blocked_when_readonly(client, monkeypatch):
    _readonly_on(monkeypatch)
    resp = client.get(
        "/api/settings", environ_overrides={"REMOTE_ADDR": "100.64.0.9"}
    )
    assert resp.status_code != 403


def test_probe_never_blocked_when_readonly(client, monkeypatch):
    _readonly_on(monkeypatch)
    # probe just needs to not be a guard 403; the handler 404s (no script) —
    # which proves the request reached the handler rather than the guard.
    resp = client.post(
        "/api/probe", environ_overrides={"REMOTE_ADDR": "100.64.0.9"}
    )
    assert resp.status_code != 403
    assert resp.get_json().get("error") != "station_readonly"


# ── unit-level guard helpers ─────────────────────────────────────────────────


def test_is_loopback():
    assert station_api._is_loopback("127.0.0.1")
    assert station_api._is_loopback("::1")
    assert not station_api._is_loopback("100.64.0.9")
    assert not station_api._is_loopback(None)


def test_readonly_enabled_parsing(monkeypatch):
    for val in ("1", "true", "YES", "on"):
        monkeypatch.setenv(station_api.READONLY_ENV, val)
        assert station_api._readonly_enabled()
    for val in ("0", "", "false", "no"):
        monkeypatch.setenv(station_api.READONLY_ENV, val)
        assert not station_api._readonly_enabled()
    monkeypatch.delenv(station_api.READONLY_ENV, raising=False)
    assert not station_api._readonly_enabled()
