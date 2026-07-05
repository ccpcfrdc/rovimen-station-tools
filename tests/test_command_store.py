"""Tests for dashboard/command_store.py — the command queue SQLite store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import command_store


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _enqueue(path, station="gmn0002", type="reboot", args=None, ttl=300):
    now = datetime.now(timezone.utc)
    cid = command_store.new_command_id()
    command_store.enqueue(
        id=cid, host_key=station, type=type, args=args or {},
        created_by="alex", issued_at=_iso(now),
        not_after=_iso(now + timedelta(seconds=ttl)), sig="sig", path=path,
    )
    return cid


def test_allowlist_is_strict():
    assert command_store.ALLOWED_TYPES == frozenset(
        {"restart_service", "reboot", "patch_settings", "lock_clip",
         "trigger_upload", "run_updater", "restart_services"}
    )
    assert "exec" not in command_store.ALLOWED_TYPES
    assert "shell" not in command_store.ALLOWED_TYPES


def test_enqueue_and_pending(tmp_path):
    p = tmp_path / "cmd.db"
    cid = _enqueue(p, type="restart_service", args={"service": "rms-cam0"})
    pending = command_store.pending_for_station("gmn0002", path=p)
    assert len(pending) == 1
    assert pending[0]["id"] == cid
    assert pending[0]["type"] == "restart_service"
    assert pending[0]["args"] == {"service": "rms-cam0"}
    # Another station sees nothing.
    assert command_store.pending_for_station("gmnro10", path=p) == []


def test_expired_command_transitions_and_excluded(tmp_path):
    p = tmp_path / "cmd.db"
    cid = _enqueue(p, ttl=-60)  # already past
    assert command_store.pending_for_station("gmn0002", path=p) == []
    assert command_store.get_command(cid, path=p)["status"] == "expired"


def test_ack_marks_acked(tmp_path):
    p = tmp_path / "cmd.db"
    cid = _enqueue(p)
    assert command_store.ack(cid, "gmn0002", {"status": "ok"}, path=p) is True
    row = command_store.get_command(cid, path=p)
    assert row["status"] == "acked"
    assert row["result"]["status"] == "ok"
    # Not returned as pending anymore.
    assert command_store.pending_for_station("gmn0002", path=p) == []


def test_ack_wrong_station_noop(tmp_path):
    p = tmp_path / "cmd.db"
    cid = _enqueue(p)
    assert command_store.ack(cid, "gmnro10", {"status": "ok"}, path=p) is False
    assert command_store.get_command(cid, path=p)["status"] == "pending"


def test_ack_redelivery_noop(tmp_path):
    p = tmp_path / "cmd.db"
    cid = _enqueue(p)
    assert command_store.ack(cid, "gmn0002", {"status": "ok"}, path=p) is True
    assert command_store.ack(cid, "gmn0002", {"status": "ok"}, path=p) is False
