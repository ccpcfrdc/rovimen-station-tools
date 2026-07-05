"""Contract tests for the pluggable StationCache storage backend.

Proves that:

* the in-memory backend behaves exactly as the historical per-process dicts;
* the Redis backend (via ``fakeredis``) satisfies the same get/set/all_status
  contract, so a heartbeat ingested by one worker is visible to a read on
  another;
* ``StationCache`` delegates to whichever backend it is given without changing
  its public contract (get/set status+vitals, snapshot bytes, diff-suppressed
  SSE fan-out);
* the factory falls back to in-memory when ``ROVIMEN_REDIS_URL`` is unset,
  guaranteeing zero behaviour change for single-worker dev + tests.
"""

from __future__ import annotations

import json

import pytest

from cache_backend import (
    InMemoryStationStateBackend,
    RedisStationStateBackend,
    make_station_state_backend,
)
from station_client import StationCache

fakeredis = pytest.importorskip("fakeredis")


def _fake_redis_backend() -> RedisStationStateBackend:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    return RedisStationStateBackend(client, prefix="test", ttl_seconds=0)


# Every backend implementation must pass this identical contract.
BACKENDS = [
    pytest.param(lambda: InMemoryStationStateBackend(), id="memory"),
    pytest.param(_fake_redis_backend, id="redis"),
]


@pytest.mark.parametrize("make_backend", BACKENDS)
class TestBackendContract:
    def test_status_roundtrip(self, make_backend) -> None:
        b = make_backend()
        assert b.get_status("gmn0002") is None
        b.set_status("gmn0002", {"online": True, "rms_running": True})
        assert b.get_status("gmn0002") == {"online": True, "rms_running": True}

    def test_vitals_roundtrip(self, make_backend) -> None:
        b = make_backend()
        assert b.get_vitals("gmn0002") is None
        b.set_vitals("gmn0002", {"cpu_pct": 12.5})
        assert b.get_vitals("gmn0002") == {"cpu_pct": 12.5}

    def test_status_and_vitals_isolated(self, make_backend) -> None:
        b = make_backend()
        b.set_status("gmn0002", {"online": True})
        b.set_vitals("gmn0002", {"cpu_pct": 1.0})
        # A status write must not clobber vitals and vice versa.
        assert b.get_status("gmn0002") == {"online": True}
        assert b.get_vitals("gmn0002") == {"cpu_pct": 1.0}

    def test_overwrite_is_last_writer_wins(self, make_backend) -> None:
        b = make_backend()
        b.set_status("gmn0002", {"online": False})
        b.set_status("gmn0002", {"online": True})
        assert b.get_status("gmn0002") == {"online": True}

    def test_all_status_missing_keys_are_empty(self, make_backend) -> None:
        b = make_backend()
        b.set_status("gmn0002", {"online": True})
        out = b.all_status(("gmn0002", "gmn0003"))
        assert out == {"gmn0002": {"online": True}, "gmn0003": {}}

    def test_all_status_empty_input(self, make_backend) -> None:
        b = make_backend()
        assert b.all_status(()) == {}

    # -- live thumbnails ---------------------------------------------------
    def test_live_thumb_roundtrip_and_bytes_exact(self, make_backend) -> None:
        b = make_backend()
        assert b.get_live_thumb("gmn0002", "RO000M") is None
        # Non-ASCII, non-UTF8 binary to prove bytes survive base64/JSON exactly.
        raw = bytes(range(256)) + b"\xff\x00webp"
        b.set_live_thumb(
            "gmn0002", "RO000M", raw, "image/webp",
            "2026-06-30T22:41:07.120Z",
        )
        got = b.get_live_thumb("gmn0002", "RO000M")
        assert got is not None
        assert got["data"] == raw  # exact byte-for-byte round-trip
        assert isinstance(got["data"], bytes)
        assert got["content_type"] == "image/webp"
        assert got["ff_timestamp"] == "2026-06-30T22:41:07.120Z"
        assert isinstance(got["received_at"], float)

    def test_live_thumb_missing_is_none(self, make_backend) -> None:
        b = make_backend()
        assert b.get_live_thumb("gmn0002", "RO000M") is None

    def test_live_thumb_last_writer_wins_per_cam(self, make_backend) -> None:
        b = make_backend()
        b.set_live_thumb("gmn0002", "RO000M", b"old", "image/webp", None)
        b.set_live_thumb("gmn0002", "RO000M", b"new", "image/webp", None)
        assert b.get_live_thumb("gmn0002", "RO000M")["data"] == b"new"

    def test_live_thumb_isolated_per_station_and_cam(self, make_backend) -> None:
        b = make_backend()
        b.set_live_thumb("gmn0002", "RO000M", b"a", "image/webp", None)
        b.set_live_thumb("gmn0002", "RO000N", b"b", "image/webp", None)
        b.set_live_thumb("gmnro10", "RO000M", b"c", "image/webp", None)
        assert b.get_live_thumb("gmn0002", "RO000M")["data"] == b"a"
        assert b.get_live_thumb("gmn0002", "RO000N")["data"] == b"b"
        assert b.get_live_thumb("gmnro10", "RO000M")["data"] == b"c"

    def test_live_thumb_null_ff_timestamp(self, make_backend) -> None:
        b = make_backend()
        b.set_live_thumb("gmn0002", "RO000M", b"x", "image/webp", None)
        assert b.get_live_thumb("gmn0002", "RO000M")["ff_timestamp"] is None


@pytest.mark.parametrize("make_backend", BACKENDS)
class TestSeqContract:
    """The seq/replay guard behaves identically on both backends.

    Accept iff seq > stored; monotonic-max; per-(host, kind); atomic bump.
    Mirrors the drop semantics the ingest replay tests assert.
    """

    def test_unseen_starts_at_zero(self, make_backend) -> None:
        b = make_backend()
        assert b.seq_get("gmn0002", "seq") == 0

    def test_bump_accepts_first_positive(self, make_backend) -> None:
        b = make_backend()
        assert b.seq_bump("gmn0002", "seq", 1) is True
        assert b.seq_get("gmn0002", "seq") == 1

    def test_equal_seq_is_replay(self, make_backend) -> None:
        b = make_backend()
        assert b.seq_bump("gmn0002", "seq", 5) is True
        assert b.seq_bump("gmn0002", "seq", 5) is False  # replay
        assert b.seq_get("gmn0002", "seq") == 5

    def test_older_seq_is_dropped_and_does_not_regress(self, make_backend) -> None:
        b = make_backend()
        assert b.seq_bump("gmn0002", "seq", 10) is True
        assert b.seq_bump("gmn0002", "seq", 4) is False  # older
        assert b.seq_get("gmn0002", "seq") == 10  # not regressed

    def test_zero_on_fresh_is_replay(self, make_backend) -> None:
        b = make_backend()
        # last_seq defaults to 0, so seq=0 is <= 0 → drop (matches SQLite path).
        assert b.seq_bump("gmn0002", "seq", 0) is False

    def test_seq_is_per_host_and_kind(self, make_backend) -> None:
        b = make_backend()
        b.seq_bump("gmn0002", "seq", 10)
        assert b.seq_get("gmnro10", "seq") == 0
        assert b.seq_get("gmn0002", "other") == 0

    def test_concurrent_bumps_exactly_one_wins(self, make_backend) -> None:
        """Two threads racing the SAME seq: exactly one accept, monotonic."""
        import threading

        b = make_backend()
        b.seq_bump("gmn0002", "seq", 5)  # prime last_seq=5

        results: list[bool] = []
        res_lock = threading.Lock()
        start = threading.Barrier(2)

        def worker() -> None:
            start.wait()
            got = b.seq_bump("gmn0002", "seq", 6)
            with res_lock:
                results.append(got)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r) == 1  # exactly one accepted
        assert b.seq_get("gmn0002", "seq") == 6  # advanced exactly once


class TestSeqRedisAtomicity:
    """Redis-specific seq guarantees over the SAME fakeredis (== two workers)."""

    def test_bump_shared_across_workers(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        worker_a = RedisStationStateBackend(client, prefix="sq", ttl_seconds=0)
        worker_b = RedisStationStateBackend(client, prefix="sq", ttl_seconds=0)
        assert worker_a.seq_bump("gmn0002", "seq", 3) is True
        # Worker B sees A's bump: a replay of the same seq is dropped there too.
        assert worker_b.seq_bump("gmn0002", "seq", 3) is False
        assert worker_b.seq_get("gmn0002", "seq") == 3

    def test_racing_workers_same_seq_one_wins(self) -> None:
        import threading

        client = fakeredis.FakeStrictRedis(decode_responses=True)
        workers = [
            RedisStationStateBackend(client, prefix="sq", ttl_seconds=0)
            for _ in range(8)
        ]
        for w in workers:
            w.seq_bump("gmn0002", "seq", 100)

        results: list[bool] = []
        res_lock = threading.Lock()
        start = threading.Barrier(len(workers))

        def worker(w: RedisStationStateBackend) -> None:
            start.wait()
            got = w.seq_bump("gmn0002", "seq", 101)
            with res_lock:
                results.append(got)

        threads = [threading.Thread(target=worker, args=(w,)) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r) == 1  # exactly one accept
        assert workers[0].seq_get("gmn0002", "seq") == 101

    def test_seq_bump_error_fails_closed_to_drop(self) -> None:
        """A Redis error on bump must report a replay (drop), never accept —
        so a blip can't double-apply a push."""
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        b = RedisStationStateBackend(client, prefix="sq", ttl_seconds=0)

        def _boom():
            raise RuntimeError("redis down")

        b._r.pipeline = _boom  # simulate the transaction blowing up
        assert b.seq_bump("gmn0002", "seq", 1) is False


class TestCrossWorkerSharing:
    """Two backends over the SAME fakeredis == two gunicorn workers.

    The whole point of the Redis backend: a write via one instance is visible
    to a read via another, which the in-memory backend cannot do.
    """

    def test_write_on_one_worker_visible_on_another(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        worker_a = RedisStationStateBackend(client, prefix="test", ttl_seconds=0)
        worker_b = RedisStationStateBackend(client, prefix="test", ttl_seconds=0)

        worker_a.set_status("gmn0002", {"online": True})
        worker_a.set_vitals("gmn0002", {"cpu_pct": 42.0})

        assert worker_b.get_status("gmn0002") == {"online": True}
        assert worker_b.get_vitals("gmn0002") == {"cpu_pct": 42.0}

    def test_in_memory_is_not_shared(self) -> None:
        worker_a = InMemoryStationStateBackend()
        worker_b = InMemoryStationStateBackend()
        worker_a.set_status("gmn0002", {"online": True})
        # In-memory: each worker is its own island (the bug Redis fixes).
        assert worker_b.get_status("gmn0002") is None

    def test_live_thumb_visible_across_workers(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        worker_a = RedisStationStateBackend(client, prefix="test", ttl_seconds=0)
        worker_b = RedisStationStateBackend(client, prefix="test", ttl_seconds=0)

        raw = bytes(range(256))
        worker_a.set_live_thumb("gmn0002", "RO000M", raw, "image/webp", "T")

        got = worker_b.get_live_thumb("gmn0002", "RO000M")
        assert got is not None
        assert got["data"] == raw  # exact bytes across the "worker" boundary
        assert got["content_type"] == "image/webp"
        assert got["ff_timestamp"] == "T"

    def test_live_thumb_in_memory_is_not_shared(self) -> None:
        worker_a = InMemoryStationStateBackend()
        worker_b = InMemoryStationStateBackend()
        worker_a.set_live_thumb("gmn0002", "RO000M", b"x", "image/webp", None)
        assert worker_b.get_live_thumb("gmn0002", "RO000M") is None


class TestRedisRobustness:
    def test_corrupt_json_returns_none(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        b = RedisStationStateBackend(client, prefix="test", ttl_seconds=0)
        # Write a non-JSON value directly under the hash field.
        client.hset("test:station:status", "gmn0002", "not-json{")
        assert b.get_status("gmn0002") is None

    def test_ttl_is_applied_when_positive(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        b = RedisStationStateBackend(client, prefix="test", ttl_seconds=60)
        b.set_status("gmn0002", {"online": True})
        assert 0 < client.ttl("test:station:status") <= 60

    def test_no_ttl_when_zero(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        b = RedisStationStateBackend(client, prefix="test", ttl_seconds=0)
        b.set_status("gmn0002", {"online": True})
        # -1 == key exists with no expiry.
        assert client.ttl("test:station:status") == -1

    def test_live_thumb_ttl_is_applied_when_positive(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        b = RedisStationStateBackend(
            client, prefix="test", live_thumb_ttl_seconds=300
        )
        b.set_live_thumb("gmn0002", "RO000M", b"x", "image/webp", None)
        assert 0 < client.ttl("test:live_thumb:gmn0002:RO000M") <= 300

    def test_live_thumb_no_ttl_when_zero(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        b = RedisStationStateBackend(
            client, prefix="test", live_thumb_ttl_seconds=0
        )
        b.set_live_thumb("gmn0002", "RO000M", b"x", "image/webp", None)
        assert client.ttl("test:live_thumb:gmn0002:RO000M") == -1

    def test_live_thumb_corrupt_json_returns_none(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        b = RedisStationStateBackend(client, prefix="test", ttl_seconds=0)
        client.set("test:live_thumb:gmn0002:RO000M", "not-json{")
        assert b.get_live_thumb("gmn0002", "RO000M") is None


class TestFactory:
    def test_unset_env_returns_in_memory(self, monkeypatch) -> None:
        monkeypatch.delenv("ROVIMEN_REDIS_URL", raising=False)
        backend = make_station_state_backend()
        assert isinstance(backend, InMemoryStationStateBackend)

    def test_empty_url_returns_in_memory(self) -> None:
        assert isinstance(
            make_station_state_backend(redis_url=""), InMemoryStationStateBackend
        )

    def test_unreachable_redis_falls_back_to_memory(self) -> None:
        # A syntactically valid but dead address must degrade to in-memory,
        # not crash the worker at startup.
        backend = make_station_state_backend(
            redis_url="redis://127.0.0.1:6390/0"
        )
        assert isinstance(backend, InMemoryStationStateBackend)


class TestStationCacheDelegation:
    """StationCache's public contract is unchanged regardless of backend."""

    def test_default_cache_is_in_memory_and_roundtrips(self, monkeypatch) -> None:
        monkeypatch.delenv("ROVIMEN_REDIS_URL", raising=False)
        cache = StationCache()
        assert cache.get_status("gmn0002") is None
        cache.set_status("gmn0002", {"online": True})
        assert cache.get_status("gmn0002") == {"online": True}
        cache.set_vitals("gmn0002", {"cpu_pct": 5.0})
        assert cache.get_vitals("gmn0002") == {"cpu_pct": 5.0}

    def test_cache_with_redis_backend_shares_state(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        backend_a = RedisStationStateBackend(client, prefix="c", ttl_seconds=0)
        backend_b = RedisStationStateBackend(client, prefix="c", ttl_seconds=0)
        cache_a = StationCache(backend=backend_a)
        cache_b = StationCache(backend=backend_b)

        cache_a.set_status("gmn0002", {"online": True, "rms_running": True})
        # A read on the "other worker" sees it.
        assert cache_b.get_status("gmn0002") == {"online": True, "rms_running": True}

    def test_snapshot_bytes_reads_backend(self) -> None:
        cache = StationCache(backend=InMemoryStationStateBackend())
        cache.set_status("gmn0002", {"online": True})
        cache.set_status("gmn0003", {"online": False})
        raw = cache.snapshot_bytes(("gmn0002", "gmn0003"))
        assert raw.startswith(b"event: snapshot\ndata: ")
        payload = json.loads(raw.split(b"data: ", 1)[1].strip())
        assert payload == {
            "gmn0002": {"online": True},
            "gmn0003": {"online": False},
        }

    def test_snapshot_bytes_over_redis(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        cache = StationCache(
            backend=RedisStationStateBackend(client, prefix="s", ttl_seconds=0)
        )
        cache.set_status("gmn0002", {"online": True})
        raw = cache.snapshot_bytes(("gmn0002", "gmn0003"))
        payload = json.loads(raw.split(b"data: ", 1)[1].strip())
        assert payload == {"gmn0002": {"online": True}, "gmn0003": {}}

    def test_set_status_fans_out_on_change_only(self) -> None:
        """The diff-suppression + SSE listener plumbing is preserved."""
        cache = StationCache(backend=InMemoryStationStateBackend())
        listener = cache.register_status_listener()
        assert listener is not None

        cache.set_status("gmn0002", {"online": True})
        first = listener.get_nowait()
        assert b"event: status" in first

        # Identical status (same diff signature) => no new event queued.
        cache.set_status("gmn0002", {"online": True})
        with pytest.raises(Exception):
            listener.get_nowait()

        cache.unregister_status_listener(listener)

    def test_live_thumb_default_cache_roundtrips(self, monkeypatch) -> None:
        monkeypatch.delenv("ROVIMEN_REDIS_URL", raising=False)
        cache = StationCache()
        assert cache.get_live_thumb("gmn0002", "RO000M") is None
        cache.set_live_thumb(
            "gmn0002", "RO000M", b"webp",
            content_type="image/webp",
            ff_timestamp="2026-06-30T22:41:07.120Z",
        )
        got = cache.get_live_thumb("gmn0002", "RO000M")
        assert got["data"] == b"webp"
        assert got["content_type"] == "image/webp"
        assert got["ff_timestamp"] == "2026-06-30T22:41:07.120Z"
        assert "received_at" in got

    def test_live_thumb_shared_across_workers_via_redis(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        cache_a = StationCache(
            backend=RedisStationStateBackend(client, prefix="t", ttl_seconds=0)
        )
        cache_b = StationCache(
            backend=RedisStationStateBackend(client, prefix="t", ttl_seconds=0)
        )
        raw = bytes(range(256))
        cache_a.set_live_thumb("gmn0002", "RO000M", raw)
        got = cache_b.get_live_thumb("gmn0002", "RO000M")
        assert got is not None
        assert got["data"] == raw  # a thumb pushed to worker A is served by B
