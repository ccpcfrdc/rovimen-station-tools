"""Tests for the server->station command channel (dashboard/command_api.py).

Builds a minimal Flask app that mounts the command routes with DB/key/signing-key
paths pointed at tmp files, mirroring how create_app wires them. Exercises the
real command_store / command_signing / station_keys code.

Security invariants under test (docs/reversed_http_push_design.md §3):
  * enqueue is admin-only (401/403 for anon/non-admin)
  * disallowed command types are rejected
  * poll/ack require the per-station key and reject the wrong station
  * a valid signed command verifies on the station side; tampered/expired do not
  * ack updates status; redelivery is a no-op
  * fail-closed: no signing key -> enqueue 503
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask, session

import command_api
import command_signing
import command_store
import station_keys


@pytest.fixture(autouse=True)
def _require_keys(monkeypatch):
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "1")
    station_keys._invalidate_cache()
    yield
    station_keys._invalidate_cache()


@pytest.fixture
def env(tmp_path: Path):
    keys_path = tmp_path / "station_keys.yaml"
    cmd_path = tmp_path / "commands.db"
    priv_path = tmp_path / "signing.pem"
    pub_path = tmp_path / "signing.pub.pem"

    command_signing.generate_keypair(priv_path, pub_path)
    _, secret = station_keys.add_key("gmn0002", path=keys_path)
    station_keys._invalidate_cache()

    app = Flask(__name__)
    app.testing = True
    app.secret_key = "test-secret"

    # A tiny stand-in login gate is unnecessary: require_admin reads the session
    # directly, and the routes are registered raw. We drive the session via the
    # test client's session_transaction.
    command_api.register_command_routes(
        app,
        known_stations=lambda: {"gmn0002", "gmnro10"},
        command_db_path=cmd_path,
        station_keys_path=keys_path,
        signing_key_path=priv_path,
        limiter=None,
    )
    client = app.test_client()
    return {
        "app": app,
        "client": client,
        "secret": secret,
        "cmd_path": cmd_path,
        "priv_path": priv_path,
        "pub_path": pub_path,
    }


def _as_admin(client, user: str = "alex") -> None:
    with client.session_transaction() as sess:
        sess["user"] = user
        sess["role"] = "admin"


def _as_role(client, role: str, user: str = "bob") -> None:
    with client.session_transaction() as sess:
        sess["user"] = user
        sess["role"] = role


def _hdr(secret: str) -> dict[str, str]:
    return {"X-Station-Key": secret}


# ── enqueue authz ──────────────────────────────────────────────────────────


class TestEnqueueAuthz:
    def test_anon_enqueue_403(self, env):
        r = env["client"].post(
            "/api/fleet/gmn0002/commands", json={"type": "reboot"}
        )
        assert r.status_code == 403

    def test_non_admin_host_enqueue_403(self, env):
        _as_role(env["client"], "host")
        r = env["client"].post(
            "/api/fleet/gmn0002/commands", json={"type": "reboot"}
        )
        assert r.status_code == 403

    def test_admin_enqueue_201(self, env):
        _as_admin(env["client"])
        r = env["client"].post(
            "/api/fleet/gmn0002/commands",
            json={"type": "restart_service", "args": {"service": "rms-cam0"}},
        )
        assert r.status_code == 201
        body = r.get_json()
        assert body["type"] == "restart_service"
        assert body["id"].startswith("cmd_")
        assert body["status"] == "pending"

    def test_disallowed_type_rejected(self, env):
        _as_admin(env["client"])
        for bad in ("exec", "shell", "rm", "run_probe", "start_live_stream"):
            r = env["client"].post(
                "/api/fleet/gmn0002/commands",
                json={"type": bad, "args": {"cmd": "rm -rf /"}},
            )
            assert r.status_code == 400, bad
            assert r.get_json()["error"] == "disallowed_command_type"

    def test_unknown_station_404(self, env):
        _as_admin(env["client"])
        r = env["client"].post(
            "/api/fleet/gmn9999/commands", json={"type": "reboot"}
        )
        assert r.status_code == 404

    def test_fail_closed_no_signing_key(self, env, tmp_path):
        """No signing key on disk -> enqueue refuses (503), never unsigned."""
        env["priv_path"].unlink()
        _as_admin(env["client"])
        r = env["client"].post(
            "/api/fleet/gmn0002/commands", json={"type": "reboot"}
        )
        assert r.status_code == 503
        assert r.get_json()["error"] == "signing_unavailable"


# ── poll / ack station-key auth ─────────────────────────────────────────────


class TestPollAuth:
    def test_poll_missing_key_401(self, env):
        r = env["client"].get("/api/fleet/gmn0002/commands")
        assert r.status_code == 401

    def test_poll_wrong_station_403(self, env):
        r = env["client"].get(
            "/api/fleet/gmnro10/commands", headers=_hdr(env["secret"])
        )
        assert r.status_code == 403
        assert r.get_json()["error"] == "station_mismatch"

    def test_poll_query_key_rejected(self, env):
        r = env["client"].get(f"/api/fleet/gmn0002/commands?key={env['secret']}")
        assert r.status_code == 400

    def test_poll_empty_when_no_commands(self, env):
        r = env["client"].get(
            "/api/fleet/gmn0002/commands", headers=_hdr(env["secret"])
        )
        assert r.status_code == 200
        assert r.get_json()["commands"] == []

    def test_ack_wrong_station_403(self, env):
        r = env["client"].post(
            "/api/fleet/gmnro10/commands/cmd_x/ack",
            json={"status": "ok"}, headers=_hdr(env["secret"]),
        )
        assert r.status_code == 403


# ── end-to-end enqueue -> poll -> ack ────────────────────────────────────────


class TestLifecycle:
    def _enqueue(self, env, type="restart_service", args=None):
        _as_admin(env["client"])
        r = env["client"].post(
            "/api/fleet/gmn0002/commands",
            json={"type": type, "args": args or {"service": "rms-cam0"}},
        )
        assert r.status_code == 201
        return r.get_json()

    def test_poll_returns_signed_command(self, env):
        enq = self._enqueue(env)
        # New session client (clear admin session) but present station key.
        with env["client"].session_transaction() as sess:
            sess.clear()
        r = env["client"].get(
            "/api/fleet/gmn0002/commands", headers=_hdr(env["secret"])
        )
        assert r.status_code == 200
        cmds = r.get_json()["commands"]
        assert len(cmds) == 1
        cmd = cmds[0]
        assert cmd["id"] == enq["id"]
        assert cmd["type"] == "restart_service"
        assert cmd["sig"]
        assert cmd["not_after"]

    def test_signed_command_verifies_on_station(self, env):
        """The station-side verifier accepts the server's signature."""
        import command_verify

        enq = self._enqueue(env)
        with env["client"].session_transaction() as sess:
            sess.clear()
        cmd = env["client"].get(
            "/api/fleet/gmn0002/commands", headers=_hdr(env["secret"])
        ).get_json()["commands"][0]

        pubkey = command_verify.load_public_key_raw(env["pub_path"])
        assert pubkey is not None
        msg = command_verify.canonical_message(
            id=cmd["id"], station="gmn0002", type=cmd["type"], args=cmd["args"],
            issued_at=cmd["issued_at"], not_after=cmd["not_after"],
        )
        assert command_verify.verify(msg, cmd["sig"], pubkey) is True

    def test_tampered_command_rejected_by_verifier(self, env):
        import command_verify

        enq = self._enqueue(env)
        with env["client"].session_transaction() as sess:
            sess.clear()
        cmd = env["client"].get(
            "/api/fleet/gmn0002/commands", headers=_hdr(env["secret"])
        ).get_json()["commands"][0]
        pubkey = command_verify.load_public_key_raw(env["pub_path"])

        # Tamper the args (privilege escalation attempt): signature no longer matches.
        tampered = command_verify.canonical_message(
            id=cmd["id"], station="gmn0002", type=cmd["type"],
            args={"service": "sudo-shell"}, issued_at=cmd["issued_at"],
            not_after=cmd["not_after"],
        )
        assert command_verify.verify(tampered, cmd["sig"], pubkey) is False
        # Tamper the type similarly.
        tampered_type = command_verify.canonical_message(
            id=cmd["id"], station="gmn0002", type="reboot", args=cmd["args"],
            issued_at=cmd["issued_at"], not_after=cmd["not_after"],
        )
        assert command_verify.verify(tampered_type, cmd["sig"], pubkey) is False

    def test_expired_command_rejected(self, env):
        import command_verify

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        assert command_verify.not_expired(past) is False
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        assert command_verify.not_expired(future) is True

    def test_expired_command_not_served(self, env):
        """A command past not_after is transitioned to expired and not polled."""
        _as_admin(env["client"])
        r = env["client"].post(
            "/api/fleet/gmn0002/commands",
            json={"type": "reboot", "ttl_seconds": 1},
        )
        assert r.status_code == 201
        cmd_id = r.get_json()["id"]
        # Force-expire it in the store.
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        row = command_store.get_command(cmd_id, path=env["cmd_path"])
        import sqlite3
        con = sqlite3.connect(str(env["cmd_path"]))
        con.execute("UPDATE commands SET not_after=? WHERE id=?", (past, cmd_id))
        con.commit()
        con.close()
        with env["client"].session_transaction() as sess:
            sess.clear()
        r = env["client"].get(
            "/api/fleet/gmn0002/commands", headers=_hdr(env["secret"])
        )
        assert r.get_json()["commands"] == []
        assert command_store.get_command(cmd_id, path=env["cmd_path"])["status"] == "expired"

    def test_ack_updates_status(self, env):
        enq = self._enqueue(env)
        with env["client"].session_transaction() as sess:
            sess.clear()
        r = env["client"].post(
            f"/api/fleet/gmn0002/commands/{enq['id']}/ack",
            json={"status": "ok", "detail": "service restarted"},
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["acked"] is True
        row = command_store.get_command(enq["id"], path=env["cmd_path"])
        assert row["status"] == "acked"
        assert row["result"]["status"] == "ok"

    def test_ack_redelivery_is_noop(self, env):
        enq = self._enqueue(env)
        with env["client"].session_transaction() as sess:
            sess.clear()
        first = env["client"].post(
            f"/api/fleet/gmn0002/commands/{enq['id']}/ack",
            json={"status": "ok"}, headers=_hdr(env["secret"]),
        )
        assert first.get_json()["acked"] is True
        second = env["client"].post(
            f"/api/fleet/gmn0002/commands/{enq['id']}/ack",
            json={"status": "ok"}, headers=_hdr(env["secret"]),
        )
        assert second.status_code == 200
        assert second.get_json()["acked"] is False
        assert second.get_json()["redelivery"] is True

    def test_ack_unknown_command_404(self, env):
        r = env["client"].post(
            "/api/fleet/gmn0002/commands/cmd_nope/ack",
            json={"status": "ok"}, headers=_hdr(env["secret"]),
        )
        assert r.status_code == 404
