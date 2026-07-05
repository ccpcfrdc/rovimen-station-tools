"""Tests for dashboard/usage_stats.py — log parsing, classification, reports."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import usage_stats


@pytest.fixture(autouse=True)
def _clear_usage_caches():
    """Reset module-level TTL caches so tests see fresh data."""
    usage_stats._usage_cache = None
    usage_stats._activity_cache = None
    yield
    usage_stats._usage_cache = None
    usage_stats._activity_cache = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Write a list of dicts as newline-delimited JSON."""
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def _ts(minutes_ago: int = 0) -> str:
    """ISO timestamp `minutes_ago` minutes before now, in UTC."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.isoformat(timespec="seconds")


# ── _parse_ts ────────────────────────────────────────────────────────────


class TestParseTs:
    def test_iso_with_timezone(self):
        dt = usage_stats._parse_ts("2026-05-01T12:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2026
        assert dt.month == 5
        assert dt.hour == 12

    def test_iso_without_timezone_assumes_utc(self):
        dt = usage_stats._parse_ts("2026-05-01T12:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_invalid_string_returns_none(self):
        assert usage_stats._parse_ts("not-a-date") is None

    def test_none_input_returns_none(self):
        assert usage_stats._parse_ts(None) is None

    def test_empty_string_returns_none(self):
        assert usage_stats._parse_ts("") is None


# ── _classify ────────────────────────────────────────────────────────────


class TestClassify:
    def test_root_returns_overview(self):
        assert usage_stats._classify("/") == "Overview"

    def test_events_page(self):
        assert usage_stats._classify("/events") == "Events"

    def test_api_detections(self):
        assert usage_stats._classify("/api/detections") == "Detections"

    def test_api_admin(self):
        assert usage_stats._classify("/api/admin/something") == "Admin actions"

    def test_public_api(self):
        assert usage_stats._classify("/api/public/v1/stations") == "Public API"

    def test_unknown_returns_none(self):
        assert usage_stats._classify("/unknown") is None

    def test_empty_string_returns_overview(self):
        assert usage_stats._classify("") == "Overview"

    def test_api_events(self):
        assert usage_stats._classify("/api/events") == "Events"

    def test_live_view(self):
        assert usage_stats._classify("/live_view") == "Live View"

    def test_settings(self):
        assert usage_stats._classify("/api/settings") == "Station Settings"


# ── _iter_log ────────────────────────────────────────────────────────────


class TestIterLog:
    def test_parses_jsonl(self, tmp_path):
        log = tmp_path / "test.log"
        records = [
            {"ts": _ts(5), "user": "alice", "path": "/"},
            {"ts": _ts(3), "user": "bob", "path": "/events"},
        ]
        _write_jsonl(log, records)

        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        results = list(usage_stats._iter_log(log, since))
        assert len(results) == 2
        assert results[0]["user"] == "alice"
        assert results[1]["user"] == "bob"
        # _ts enrichment
        assert "_ts" in results[0]

    def test_skips_records_before_since(self, tmp_path):
        log = tmp_path / "test.log"
        records = [
            {"ts": _ts(120), "user": "old", "path": "/"},
            {"ts": _ts(5), "user": "new", "path": "/events"},
        ]
        _write_jsonl(log, records)

        since = datetime.now(timezone.utc) - timedelta(minutes=30)
        results = list(usage_stats._iter_log(log, since))
        assert len(results) == 1
        assert results[0]["user"] == "new"

    def test_skips_malformed_lines(self, tmp_path):
        log = tmp_path / "test.log"
        with open(log, "w") as fh:
            fh.write("this is not json\n")
            fh.write(json.dumps({"ts": _ts(1), "user": "ok"}) + "\n")
            fh.write("{bad json\n")

        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        results = list(usage_stats._iter_log(log, since))
        assert len(results) == 1
        assert results[0]["user"] == "ok"

    def test_nonexistent_file(self, tmp_path):
        missing = tmp_path / "nope.log"
        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        results = list(usage_stats._iter_log(missing, since))
        assert results == []

    def test_empty_file(self, tmp_path):
        log = tmp_path / "empty.log"
        log.write_text("")
        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        results = list(usage_stats._iter_log(log, since))
        assert results == []

    def test_skips_non_dict_json(self, tmp_path):
        log = tmp_path / "test.log"
        with open(log, "w") as fh:
            fh.write("[1,2,3]\n")
            fh.write('"just a string"\n')
            fh.write(json.dumps({"ts": _ts(1), "user": "valid"}) + "\n")

        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        results = list(usage_stats._iter_log(log, since))
        assert len(results) == 1


# ── compute_user_activity ────────────────────────────────────────────────


class TestComputeUserActivity:
    def test_per_user_request_counts(self, tmp_path, monkeypatch):
        activity = tmp_path / "activity.log"
        audit = tmp_path / "audit.log"
        audit.write_text("")

        records = [
            {"ts": _ts(10), "user": "alice", "path": "/", "method": "GET", "status": 200},
            {"ts": _ts(9), "user": "alice", "path": "/events", "method": "GET", "status": 200},
            {"ts": _ts(8), "user": "bob", "path": "/", "method": "GET", "status": 200},
        ]
        _write_jsonl(activity, records)

        monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", activity)
        monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", audit)

        result = usage_stats.compute_user_activity(days=1)
        assert result["alice"]["requests"] == 2
        assert result["bob"]["requests"] == 1

    def test_active_seconds_with_idle_gap(self, tmp_path, monkeypatch):
        activity = tmp_path / "activity.log"
        audit = tmp_path / "audit.log"
        audit.write_text("")

        # 3 records: first two are 5 min apart (counted), then a 20 min gap (idle, not counted)
        records = [
            {"ts": _ts(30), "user": "alice", "path": "/", "method": "GET", "status": 200},
            {"ts": _ts(25), "user": "alice", "path": "/events", "method": "GET", "status": 200},
            {"ts": _ts(5), "user": "alice", "path": "/admin", "method": "GET", "status": 200},
        ]
        _write_jsonl(activity, records)

        monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", activity)
        monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", audit)

        result = usage_stats.compute_user_activity(days=1)
        alice = result["alice"]
        # 5 minutes = 300 seconds should be counted; the 20 min gap exceeds _IDLE_GAP_S threshold
        assert alice["active_seconds"] == 300

    def test_sessions_counted(self, tmp_path, monkeypatch):
        activity = tmp_path / "activity.log"
        audit = tmp_path / "audit.log"
        audit.write_text("")

        # Two clusters: min 30 and 25 (one session), then min 3 (new session after >15 min gap)
        records = [
            {"ts": _ts(30), "user": "alice", "path": "/", "method": "GET", "status": 200},
            {"ts": _ts(25), "user": "alice", "path": "/events", "method": "GET", "status": 200},
            {"ts": _ts(3), "user": "alice", "path": "/admin", "method": "GET", "status": 200},
        ]
        _write_jsonl(activity, records)

        monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", activity)
        monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", audit)

        result = usage_stats.compute_user_activity(days=1)
        assert result["alice"]["sessions"] == 2

    def test_last_seen_and_last_login(self, tmp_path, monkeypatch):
        activity = tmp_path / "activity.log"
        audit = tmp_path / "audit.log"

        activity_records = [
            {"ts": _ts(10), "user": "alice", "path": "/", "method": "GET", "status": 200},
            {"ts": _ts(5), "user": "alice", "path": "/events", "method": "GET", "status": 200},
        ]
        _write_jsonl(activity, activity_records)

        audit_records = [
            {"ts": _ts(15), "event": "login", "result": "ok", "username": "alice"},
        ]
        _write_jsonl(audit, audit_records)

        monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", activity)
        monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", audit)

        result = usage_stats.compute_user_activity(days=1)
        alice = result["alice"]
        assert alice["last_seen"] is not None
        assert alice["last_login"] is not None

    def test_user_with_login_but_no_activity(self, tmp_path, monkeypatch):
        activity = tmp_path / "activity.log"
        audit = tmp_path / "audit.log"
        activity.write_text("")

        audit_records = [
            {"ts": _ts(5), "event": "login", "result": "ok", "username": "ghost"},
        ]
        _write_jsonl(audit, audit_records)

        monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", activity)
        monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", audit)

        result = usage_stats.compute_user_activity(days=1)
        assert "ghost" in result
        assert result["ghost"]["requests"] == 0
        assert result["ghost"]["last_login"] is not None
        assert result["ghost"]["last_seen"] is None


# ── compute_usage ────────────────────────────────────────────────────────


class TestComputeUsage:
    def test_returns_correct_structure(self, tmp_path, monkeypatch):
        activity = tmp_path / "activity.log"
        audit = tmp_path / "audit.log"
        audit.write_text("")

        records = [
            {"ts": _ts(10), "user": "alice", "path": "/", "method": "GET", "status": 200},
            {"ts": _ts(5), "user": "bob", "path": "/events", "method": "GET", "status": 200},
        ]
        _write_jsonl(activity, records)

        monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", activity)
        monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", audit)

        result = usage_stats.compute_usage(days=1)
        assert "totals" in result
        assert "features" in result
        assert "users" in result
        assert "daily" in result
        assert "days" in result
        assert "since" in result
        assert "generated_at" in result

        assert result["totals"]["requests"] == 2
        assert result["totals"]["active_users"] == 2

    def test_daily_series_has_entry_for_every_day(self, tmp_path, monkeypatch):
        activity = tmp_path / "activity.log"
        audit = tmp_path / "audit.log"
        audit.write_text("")
        activity.write_text("")

        monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", activity)
        monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", audit)

        days = 7
        result = usage_stats.compute_usage(days=days)
        assert len(result["daily"]) == days
        # All dates should be distinct
        dates = [d["date"] for d in result["daily"]]
        assert len(set(dates)) == days

    def test_features_sorted_by_hits_descending(self, tmp_path, monkeypatch):
        activity = tmp_path / "activity.log"
        audit = tmp_path / "audit.log"
        audit.write_text("")

        records = [
            {"ts": _ts(10), "user": "alice", "path": "/events", "method": "GET", "status": 200},
            {"ts": _ts(9), "user": "alice", "path": "/events", "method": "GET", "status": 200},
            {"ts": _ts(8), "user": "alice", "path": "/events", "method": "GET", "status": 200},
            {"ts": _ts(7), "user": "bob", "path": "/", "method": "GET", "status": 200},
        ]
        _write_jsonl(activity, records)

        monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", activity)
        monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", audit)

        result = usage_stats.compute_usage(days=1)
        features = result["features"]
        assert len(features) >= 2
        # First feature should have the most hits
        hits = [f["hits"] for f in features]
        assert hits == sorted(hits, reverse=True)
        assert features[0]["feature"] == "Events"
        assert features[0]["hits"] == 3

    def test_zero_traffic_days_have_zero_hits(self, tmp_path, monkeypatch):
        activity = tmp_path / "activity.log"
        audit = tmp_path / "audit.log"
        audit.write_text("")
        activity.write_text("")

        monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", activity)
        monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", audit)

        result = usage_stats.compute_usage(days=3)
        for entry in result["daily"]:
            assert entry["hits"] == 0

    def test_totals_sessions(self, tmp_path, monkeypatch):
        activity = tmp_path / "activity.log"
        audit = tmp_path / "audit.log"
        audit.write_text("")

        records = [
            {"ts": _ts(60), "user": "alice", "path": "/", "method": "GET", "status": 200},
            {"ts": _ts(55), "user": "alice", "path": "/events", "method": "GET", "status": 200},
            {"ts": _ts(10), "user": "bob", "path": "/", "method": "GET", "status": 200},
        ]
        _write_jsonl(activity, records)

        monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", activity)
        monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", audit)

        result = usage_stats.compute_usage(days=1)
        # alice has 1 session, bob has 1 session
        assert result["totals"]["sessions"] >= 2
