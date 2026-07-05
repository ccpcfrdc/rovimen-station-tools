"""Tests for dashboard/ingest_api.py — reversed-HTTP push ingest endpoints.

Builds a minimal Flask app that mounts only the ingest routes (mirroring how
create_app wires them) with the DB/cache/key paths pointed at tmp files, so the
handlers exercise the real station_keys / station_state / detection_db code.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

import detection_db
import ingest_api
import station_keys
import station_state as ss


class _FakeCache:
    """Stand-in for StationCache.set_status/set_vitals — records the writes so
    tests can assert a pushed heartbeat lands in the same cache the pollers use."""

    def __init__(self) -> None:
        self.status: dict[str, dict[str, Any]] = {}
        self.vitals: dict[str, dict[str, Any]] = {}
        self.live_thumbs: dict[tuple[str, str], dict[str, Any]] = {}

    def set_status(self, host_key: str, data: dict[str, Any]) -> None:
        self.status[host_key] = data

    def set_vitals(self, host_key: str, data: dict[str, Any]) -> None:
        self.vitals[host_key] = data

    def set_live_thumb(self, host_key: str, cam: str, data: bytes,
                       *, content_type: str = "image/webp",
                       ff_timestamp: str | None = None) -> None:
        self.live_thumbs[(host_key, cam)] = {
            "data": data, "content_type": content_type,
            "ff_timestamp": ff_timestamp,
        }


@pytest.fixture(autouse=True)
def _require_keys(monkeypatch):
    """Force key enforcement on (production default) for the auth tests."""
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "1")
    station_keys._invalidate_cache()
    yield
    station_keys._invalidate_cache()


@pytest.fixture
def env(tmp_path: Path):
    keys_path = tmp_path / "station_keys.yaml"
    det_path = tmp_path / "detections.db"
    state_path = tmp_path / "state.db"
    cache = _FakeCache()

    # Mint one key for gmn0002.
    _, secret = station_keys.add_key("gmn0002", path=keys_path)
    station_keys._invalidate_cache()

    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=cache.set_status,
        cache_set_vitals=cache.set_vitals,
        cache_set_live_thumb=cache.set_live_thumb,
        known_stations=lambda: {"gmn0002", "gmnro10"},
        detections_db_path=det_path,
        station_state_path=state_path,
        station_keys_path=keys_path,
        limiter=None,
    )
    client = app.test_client()
    return {
        "client": client,
        "secret": secret,
        "keys_path": keys_path,
        "det_path": det_path,
        "state_path": state_path,
        "cache": cache,
    }


@pytest.fixture
def redis_env(tmp_path: Path, monkeypatch):
    """Same wiring as ``env`` but with the seq guard routed through a shared
    fakeredis backend, so the ingest handlers exercise the Redis seq path while
    the last-value mirror / media pointers stay in SQLite."""
    fakeredis = pytest.importorskip("fakeredis")
    import cache_backend

    keys_path = tmp_path / "station_keys.yaml"
    det_path = tmp_path / "detections.db"
    state_path = tmp_path / "state.db"
    cache = _FakeCache()

    _, secret = station_keys.add_key("gmn0002", path=keys_path)
    station_keys._invalidate_cache()

    # Force the station_state seq guard onto fakeredis for the duration.
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(
        cache_backend,
        "make_seq_backend",
        lambda redis_url=None: cache_backend.RedisStationStateBackend(
            client, prefix="ingest", ttl_seconds=0
        ),
    )
    ss.reset_seq_backend_for_tests()

    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=cache.set_status,
        cache_set_vitals=cache.set_vitals,
        cache_set_live_thumb=cache.set_live_thumb,
        known_stations=lambda: {"gmn0002", "gmnro10"},
        detections_db_path=det_path,
        station_state_path=state_path,
        station_keys_path=keys_path,
        limiter=None,
    )
    client_app = app.test_client()
    yield {
        "client": client_app,
        "secret": secret,
        "state_path": state_path,
        "det_path": det_path,
        "cache": cache,
        "redis": client,
    }
    ss.reset_seq_backend_for_tests()


def _sqlite_last_seq(state_path: Path, host_key: str) -> int:
    """Read the raw SQLite last_seq column directly (0 if the row/DB is absent).

    Used to prove the per-push seq write did NOT land in SQLite on the Redis
    path — the whole point of the fix.
    """
    import sqlite3

    if not Path(state_path).exists():
        return 0
    con = sqlite3.connect(str(state_path))
    try:
        row = con.execute(
            "SELECT last_seq FROM station_state WHERE host_key=?", (host_key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()
    return int(row[0]) if row and row[0] is not None else 0


def _hdr(secret: str) -> dict[str, str]:
    return {"X-Station-Key": secret}


def _envelope(seq: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {"seq": seq, "sent_at": "2026-07-01T22:00:00Z", "payload": payload}


# ── discovery index ─────────────────────────────────────────────────────


def test_index_is_open(env):
    r = env["client"].get("/api/ingest/v1")
    assert r.status_code == 200
    assert r.get_json()["service"] == "rovimen-ingest-api"


# ── auth ─────────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_key_401(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat", json=_envelope(1, {"online": True})
        )
        assert r.status_code == 401
        assert r.get_json()["error"] == "missing_or_invalid_station_key"

    def test_invalid_key_401(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(1, {"online": True}),
            headers=_hdr("rvmn_gmn0002_deadbeef"),
        )
        assert r.status_code == 401

    def test_wrong_station_403(self, env):
        """gmn0002's key POSTing to gmnro10 → 403 (core §2.2 property)."""
        r = env["client"].post(
            "/api/ingest/v1/gmnro10/heartbeat",
            json=_envelope(1, {"online": True}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 403
        assert r.get_json()["error"] == "station_mismatch"

    def test_bearer_token_accepted(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(1, {"online": True}),
            headers={"Authorization": f"Bearer {env['secret']}"},
        )
        assert r.status_code == 200

    def test_query_key_param_rejected(self, env):
        r = env["client"].post(
            f"/api/ingest/v1/gmn0002/heartbeat?key={env['secret']}",
            json=_envelope(1, {"online": True}),
        )
        assert r.status_code == 400
        assert r.get_json()["error"] == "url_key_param_disabled"

    def test_unknown_station_404(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn9999/heartbeat",
            json=_envelope(1, {"online": True}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 404

    def test_bad_station_id_404(self, env):
        r = env["client"].post(
            "/api/ingest/v1/BAD-ID/heartbeat",
            json=_envelope(1, {"online": True}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 404


# ── heartbeat ──────────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_writes_to_cache_and_state(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(5, {"online": True, "rms_running": True}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["dropped"] is False
        # Same cache the poll thread writes.
        assert env["cache"].status["gmn0002"]["online"] is True
        assert env["cache"].status["gmn0002"]["rms_running"] is True
        # Durable mirror + seq advanced.
        state = ss.get_state("gmn0002", path=env["state_path"])
        assert state["last_seq"] == 5

    def test_replay_dropped(self, env):
        c, s = env["client"], env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat", json=_envelope(5, {"online": True}), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/heartbeat", json=_envelope(5, {"online": False}), headers=_hdr(s))
        assert r.status_code == 200
        assert r.get_json()["dropped"] is True
        # Cache not overwritten by the replay.
        assert env["cache"].status["gmn0002"]["online"] is True

    def test_older_seq_dropped(self, env):
        c, s = env["client"], env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat", json=_envelope(10, {"online": True}), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/heartbeat", json=_envelope(4, {"online": False}), headers=_hdr(s))
        assert r.get_json()["dropped"] is True

    def test_malformed_envelope_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json={"payload": {"online": True}},  # missing seq
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422

    def test_heartbeat_string_services_accepted(self, env):
        # Stations report service states as strings ("active"/"inactive"), not
        # bools — the ingest schema must accept them (pilot regression 2026-07-01:
        # dict[str, bool] rejected real /api/status payloads with a 422).
        c, s = env["client"], env["secret"]
        r = c.post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(7, {"online": True,
                               "services": {"rms-cam1": "active",
                                            "color-capture": "active"}}),
            headers=_hdr(s),
        )
        assert r.status_code == 200
        assert r.get_json()["dropped"] is False
        assert env["cache"].status["gmn0002"]["services"]["rms-cam1"] == "active"

    def test_non_object_body_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            data="not json",
            content_type="application/json",
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422


# ── vitals ──────────────────────────────────────────────────────────────


class TestVitals:
    def test_writes_vitals_cache(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/vitals",
            json=_envelope(2, {"cpu_pct": 33.0, "ram_pct": 40.0}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert env["cache"].vitals["gmn0002"]["cpu_pct"] == 33.0

    def test_malformed_payload_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/vitals",
            json=_envelope(2, {"cpu_pct": "not-a-number"}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422


# ── detections ─────────────────────────────────────────────────────────────


class TestDetections:
    def _batch(self, seq: int, n: int = 1):
        dets = [
            {
                "cam": "RO000M", "date": "20260630",
                "ff_file": f"FF_RO000M_20260630_{i:03d}.fits", "meteor_no": 1,
                "time_utc": "2026-06-30T22:41:07Z", "shower": "SPO",
                "mag_apparent": -1.2,
            }
            for i in range(n)
        ]
        return _envelope(seq, {"window_since": "20260617", "detections": dets})

    def test_upserts_into_db(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections",
            json=self._batch(1, n=3),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["written"] == 3
        rows = detection_db.query_detections(["20260630"], path=env["det_path"])
        assert len(rows) == 3
        assert rows[0]["source"] == "push"

    def test_replay_dropped_no_write(self, env):
        c, s = env["client"], env["secret"]
        c.post("/api/ingest/v1/gmn0002/detections", json=self._batch(5, n=1), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/detections", json=self._batch(5, n=2), headers=_hdr(s))
        assert r.get_json()["dropped"] is True
        assert r.get_json()["written"] == 0

    def test_missing_required_column_422(self, env):
        bad = _envelope(1, {"detections": [{"cam": "RO000M"}]})  # no date/ff_file
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections", json=bad, headers=_hdr(env["secret"])
        )
        assert r.status_code == 422

    def test_empty_batch_ok(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections",
            json=_envelope(1, {"detections": []}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["written"] == 0


# ── media pointers ───────────────────────────────────────────────────────


class TestMedia:
    def test_stores_pointers(self, env):
        payload = {
            "timelapses": [{"cam": "RO000M", "date": "20260630",
                            "filename": "t.mp4", "night_stack": "n.webp"}],
            "clips": [{"cam": "RO000M", "date": "20260630", "filename": "c.mkv",
                       "locked": True, "meteor_time": "2026-06-30T22:41:07Z"}],
        }
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/media",
            json=_envelope(1, payload),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["written"] == 2
        rows = ss.get_media_pointers("gmn0002", path=env["state_path"])
        kinds = {row["kind"] for row in rows}
        assert kinds == {"timelapse", "clip"}

    def test_replay_dropped(self, env):
        c, s = env["client"], env["secret"]
        p = {"clips": [{"cam": "RO000M", "date": "20260630", "filename": "c.mkv"}]}
        c.post("/api/ingest/v1/gmn0002/media", json=_envelope(5, p), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/media", json=_envelope(5, p), headers=_hdr(s))
        assert r.get_json()["dropped"] is True

    def test_malformed_pointer_422(self, env):
        # clip missing required 'filename'
        bad = _envelope(1, {"clips": [{"cam": "RO000M", "date": "20260630"}]})
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/media", json=bad, headers=_hdr(env["secret"])
        )
        assert r.status_code == 422

    def test_clip_pointers_stored_with_lock_metadata(self, env):
        payload = {
            "clips": [{
                "cam": "RO000M", "date": "20260630",
                "filename": "RO000M_20260630_224107_color.mkv",
                "locked": True, "meteor_time": "2026-06-30T22:41:07Z",
                "lock_type": "detection",
            }],
        }
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/media",
            json=_envelope(3, payload),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["written"] == 1
        rows = ss.get_media_pointers("gmn0002", path=env["state_path"])
        clip_rows = [row for row in rows if row["kind"] == "clip"]
        assert len(clip_rows) == 1
        clip = clip_rows[0]
        assert clip["filename"] == "RO000M_20260630_224107_color.mkv"
        # locked / meteor_time / lock_type ride the extra JSON blob.
        assert clip["extra"]["locked"] is True
        assert clip["extra"]["meteor_time"] == "2026-06-30T22:41:07Z"
        assert clip["extra"]["lock_type"] == "detection"


# ── live thumbnail (newest-FF maxpixel WebP) ───────────────────────────────


class TestLiveThumb:
    def _thumb_body(self, seq: int, raw: bytes = b"\x00webpbytes",
                    content_type: str = "image/webp",
                    ff_ts: str | None = "2026-06-30T22:41:07.120Z"):
        return _envelope(seq, {
            "content_type": content_type,
            "ff_timestamp": ff_ts,
            "data_b64": base64.b64encode(raw).decode("ascii"),
        })

    def test_stores_latest_per_cam(self, env):
        c, s = env["client"], env["secret"]
        r = c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
                   json=self._thumb_body(1, b"first"), headers=_hdr(s))
        assert r.status_code == 200
        assert r.get_json()["bytes"] == len(b"first")
        stored = env["cache"].live_thumbs[("gmn0002", "RO000M")]
        assert stored["data"] == b"first"
        assert stored["content_type"] == "image/webp"
        assert stored["ff_timestamp"] == "2026-06-30T22:41:07.120Z"
        # A newer seq overwrites (latest-writer-wins per cam).
        r2 = c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
                    json=self._thumb_body(2, b"second"), headers=_hdr(s))
        assert r2.status_code == 200
        assert env["cache"].live_thumbs[("gmn0002", "RO000M")]["data"] == b"second"

    def test_separate_cams_kept_independently(self, env):
        c, s = env["client"], env["secret"]
        c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
               json=self._thumb_body(1, b"m"), headers=_hdr(s))
        c.post("/api/ingest/v1/gmn0002/live_thumb/RO000N",
               json=self._thumb_body(2, b"n"), headers=_hdr(s))
        assert env["cache"].live_thumbs[("gmn0002", "RO000M")]["data"] == b"m"
        assert env["cache"].live_thumbs[("gmn0002", "RO000N")]["data"] == b"n"

    def test_replay_dropped_no_overwrite(self, env):
        c, s = env["client"], env["secret"]
        c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
               json=self._thumb_body(5, b"keep"), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
                   json=self._thumb_body(5, b"stale"), headers=_hdr(s))
        assert r.get_json()["dropped"] is True
        assert env["cache"].live_thumbs[("gmn0002", "RO000M")]["data"] == b"keep"

    def test_missing_key_401(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/RO000M", json=self._thumb_body(1)
        )
        assert r.status_code == 401

    def test_wrong_station_403(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmnro10/live_thumb/RO000M",
            json=self._thumb_body(1), headers=_hdr(env["secret"]),
        )
        assert r.status_code == 403

    def test_bad_cam_404(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/bad-cam",
            json=self._thumb_body(1), headers=_hdr(env["secret"]),
        )
        assert r.status_code == 404

    def test_malformed_envelope_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/RO000M",
            json={"payload": {"data_b64": "AAAA"}},  # missing seq
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422

    def test_missing_data_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/RO000M",
            json=_envelope(1, {"content_type": "image/webp"}),  # no data_b64
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422

    def test_invalid_base64_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/RO000M",
            json=_envelope(1, {"data_b64": "not valid base64!!!"}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422

    def test_disallowed_content_type_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/RO000M",
            json=self._thumb_body(1, content_type="video/mp4"),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422


# ── Redis seq path (seq/replay guard offloaded from SQLite) ──────────────────


class TestRedisSeqPath:
    """With ROVIMEN_REDIS_URL configured, the per-push seq check-and-bump runs
    in Redis, not SQLite — but the accept/replay/older-seq DROP semantics that
    the SQLite path (and the tests above) assert must be identical."""

    def test_heartbeat_accept_then_replay_dropped(self, redis_env):
        c, s = redis_env["client"], redis_env["secret"]
        r1 = c.post("/api/ingest/v1/gmn0002/heartbeat",
                    json=_envelope(5, {"online": True}), headers=_hdr(s))
        assert r1.status_code == 200
        assert r1.get_json()["dropped"] is False
        # Same seq again → replay, cache not overwritten.
        r2 = c.post("/api/ingest/v1/gmn0002/heartbeat",
                    json=_envelope(5, {"online": False}), headers=_hdr(s))
        assert r2.get_json()["dropped"] is True
        assert redis_env["cache"].status["gmn0002"]["online"] is True

    def test_older_seq_dropped(self, redis_env):
        c, s = redis_env["client"], redis_env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat",
               json=_envelope(10, {"online": True}), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/heartbeat",
                   json=_envelope(4, {"online": False}), headers=_hdr(s))
        assert r.get_json()["dropped"] is True

    def test_seq_lives_in_redis_not_sqlite(self, redis_env):
        """The per-push seq write must NOT touch the single-writer SQLite DB."""
        c, s = redis_env["client"], redis_env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat",
               json=_envelope(7, {"online": True}), headers=_hdr(s))
        # Seq is in Redis…
        assert int(redis_env["redis"].hget("ingest:station:seq", "gmn0002:seq")) == 7
        # …and the SQLite last_seq column was NOT bumped by the push.
        assert _sqlite_last_seq(redis_env["state_path"], "gmn0002") == 0

    def test_mirror_still_persisted_to_sqlite(self, redis_env):
        """Durable last-value mirror stays in SQLite even on the Redis seq path
        (only the hot seq write moves)."""
        c, s = redis_env["client"], redis_env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat",
               json=_envelope(3, {"online": True, "rms_running": True}),
               headers=_hdr(s))
        state = ss.get_state("gmn0002", path=redis_env["state_path"])
        assert state is not None
        assert state["last_status"]["online"] is True
        assert state["last_status"]["rms_running"] is True
        # get_last_seq now reads Redis, so it reflects the accepted seq.
        assert ss.get_last_seq("gmn0002", path=redis_env["state_path"]) == 3

    def test_detections_replay_dropped_no_write(self, redis_env):
        c, s = redis_env["client"], redis_env["secret"]
        batch = _envelope(5, {"detections": [{
            "cam": "RO000M", "date": "20260630",
            "ff_file": "FF_RO000M_20260630_000.fits", "meteor_no": 1,
        }]})
        c.post("/api/ingest/v1/gmn0002/detections", json=batch, headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/detections", json=batch, headers=_hdr(s))
        assert r.get_json()["dropped"] is True
        assert r.get_json()["written"] == 0

    def test_media_replay_dropped(self, redis_env):
        c, s = redis_env["client"], redis_env["secret"]
        p = {"clips": [{"cam": "RO000M", "date": "20260630", "filename": "c.mkv"}]}
        c.post("/api/ingest/v1/gmn0002/media", json=_envelope(5, p), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/media", json=_envelope(5, p), headers=_hdr(s))
        assert r.get_json()["dropped"] is True

    def test_live_thumb_replay_no_overwrite(self, redis_env):
        c, s = redis_env["client"], redis_env["secret"]
        body1 = _envelope(5, {
            "content_type": "image/webp",
            "data_b64": base64.b64encode(b"keep").decode("ascii"),
        })
        body2 = _envelope(5, {
            "content_type": "image/webp",
            "data_b64": base64.b64encode(b"stale").decode("ascii"),
        })
        c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M", json=body1, headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M", json=body2, headers=_hdr(s))
        assert r.get_json()["dropped"] is True
        assert redis_env["cache"].live_thumbs[("gmn0002", "RO000M")]["data"] == b"keep"

    def test_accept_seq_never_writes_sqlite_seq_column(self, redis_env, monkeypatch):
        """Prove the seq write bypasses SQLite: spy on accept_seq's SQLite branch
        by asserting no INSERT advances station_state.last_seq. A detections push
        (whose only durable seq write WAS advance_seq) must record seq in Redis
        and leave the SQLite last_seq column untouched."""
        c, s = redis_env["client"], redis_env["secret"]
        batch = _envelope(11, {"detections": [{
            "cam": "RO000M", "date": "20260630",
            "ff_file": "FF_RO000M_20260630_000.fits", "meteor_no": 1,
        }]})
        r = c.post("/api/ingest/v1/gmn0002/detections", json=batch, headers=_hdr(s))
        assert r.status_code == 200
        # Seq recorded in Redis…
        assert int(redis_env["redis"].hget("ingest:station:seq", "gmn0002:seq")) == 11
        # …and detections do not even create a station_state row (advance_seq is
        # now a Redis-only op for detections), so the SQLite seq stays 0.
        assert _sqlite_last_seq(redis_env["state_path"], "gmn0002") == 0
