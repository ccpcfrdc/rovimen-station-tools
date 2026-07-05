"""Tests for the authenticated-endpoint resource caps.

These bound the endpoints that could OOM the VPS, fill /tmp, or pin the
single gunicorn worker. The route handlers themselves need Flask app context
+ station fan-out that is awkward to stand up here, so we test the pure
cap / eviction helpers and the module-level constants that gate them.
"""

from __future__ import annotations

import os


# ---------------------------------------------------------------------------
# public_api MP4 remux-cache eviction (disk-exhaustion cap)
# ---------------------------------------------------------------------------

class TestMp4CacheEviction:
    def _make(self, d, name: str, size: int, atime: float) -> None:
        p = d / name
        p.write_bytes(b"\0" * size)
        os.utime(p, (atime, atime))

    def test_evicts_lru_until_under_budget(self, tmp_path, monkeypatch):
        import public_api

        monkeypatch.setattr(public_api, "_MP4_CACHE_DIR", tmp_path)
        # Three 100-byte entries with increasing atime (a.mp4 oldest).
        self._make(tmp_path, "a.mp4", 100, atime=1000)
        self._make(tmp_path, "b.mp4", 100, atime=2000)
        self._make(tmp_path, "c.mp4", 100, atime=3000)

        # Budget 250, incoming 100 -> must get total (300) + incoming (100)
        # down to <= 250, i.e. evict the two oldest (a, b).
        public_api._evict_mp4_cache(incoming_bytes=100, max_bytes=250)

        remaining = {p.name for p in tmp_path.glob("*.mp4")}
        assert remaining == {"c.mp4"}

    def test_keeps_everything_when_under_budget(self, tmp_path, monkeypatch):
        import public_api

        monkeypatch.setattr(public_api, "_MP4_CACHE_DIR", tmp_path)
        self._make(tmp_path, "a.mp4", 100, atime=1000)
        self._make(tmp_path, "b.mp4", 100, atime=2000)

        public_api._evict_mp4_cache(incoming_bytes=0, max_bytes=10_000)

        assert {p.name for p in tmp_path.glob("*.mp4")} == {"a.mp4", "b.mp4"}

    def test_never_evicts_inflight_remux_temp(self, tmp_path, monkeypatch):
        import public_api

        monkeypatch.setattr(public_api, "_MP4_CACHE_DIR", tmp_path)
        # An in-flight temp file (pub_remux_*) must survive eviction even when
        # the cache is way over budget -- a concurrent request may be writing it.
        self._make(tmp_path, "pub_remux_abcd.mp4", 1000, atime=1)
        self._make(tmp_path, "old.mp4", 1000, atime=2)

        public_api._evict_mp4_cache(incoming_bytes=0, max_bytes=100)

        remaining = {p.name for p in tmp_path.glob("*.mp4")}
        assert "pub_remux_abcd.mp4" in remaining
        assert "old.mp4" not in remaining

    def test_zero_or_negative_budget_is_noop(self, tmp_path, monkeypatch):
        import public_api

        monkeypatch.setattr(public_api, "_MP4_CACHE_DIR", tmp_path)
        self._make(tmp_path, "a.mp4", 100, atime=1000)

        # max_bytes <= 0 means "disabled" -- don't nuke the cache.
        public_api._evict_mp4_cache(incoming_bytes=0, max_bytes=0)

        assert {p.name for p in tmp_path.glob("*.mp4")} == {"a.mp4"}


# ---------------------------------------------------------------------------
# public_api MP4 remux concurrency cap (worker-starvation cap)
# ---------------------------------------------------------------------------

class TestMp4RemuxConcurrency:
    def test_default_cap_is_sane(self):
        import public_api

        assert public_api._MP4_MAX_CONCURRENT >= 1

    def test_cache_hit_does_not_consume_a_slot(self, tmp_path, monkeypatch):
        import threading

        import public_api

        monkeypatch.setattr(public_api, "_MP4_CACHE_DIR", tmp_path)
        # Fully exhaust the semaphore: no remux slots are free.
        sem = threading.BoundedSemaphore(1)
        sem.acquire()
        monkeypatch.setattr(public_api, "_mp4_remux_sem", sem)
        # Fail loudly if the hit path tries to spawn ffmpeg.
        monkeypatch.setattr(
            public_api, "_remux_mkv_to_mp4",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")),
        )

        source = tmp_path / "clip.mkv"
        source.write_bytes(b"src")
        # Pre-seed the cache with a fresher-than-source MP4 (a cache hit).
        cache_path = public_api._mp4_cache_path(source)
        cache_path.write_bytes(b"mp4")
        os.utime(cache_path, (source.stat().st_mtime + 10,) * 2)

        # No Flask context needed: send_file just streams the file. A cache hit
        # must return without waiting on / consuming the exhausted semaphore.
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context("/media/v1/clip/x/y/clip.mkv"):
            resp = public_api._serve_as_mp4(source, "clip.mkv")
        assert resp.status_code == 200

    def test_exhausted_semaphore_returns_busy_without_spawning(
        self, tmp_path, monkeypatch,
    ):
        import threading

        import public_api

        monkeypatch.setattr(public_api, "_MP4_CACHE_DIR", tmp_path)
        # Zero free slots and a near-instant acquire timeout.
        sem = threading.BoundedSemaphore(1)
        sem.acquire()
        monkeypatch.setattr(public_api, "_mp4_remux_sem", sem)
        monkeypatch.setattr(public_api, "_MP4_REMUX_ACQUIRE_TIMEOUT", 0.05)
        monkeypatch.setattr(
            public_api, "_remux_mkv_to_mp4",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")),
        )

        source = tmp_path / "miss.mkv"
        source.write_bytes(b"src")
        # No cached MP4 exists -> this is a genuine cache miss.
        import pytest

        with pytest.raises(public_api._RemuxBusy):
            public_api._serve_as_mp4(source, "miss.mkv")


# ---------------------------------------------------------------------------
# compilation clip-count cap
# ---------------------------------------------------------------------------

class TestCompilationClipCap:
    def test_over_cap_detected(self):
        import routes.compilation as comp

        over = [{"cam": "RO000A"}] * (comp.MAX_CLIPS + 1)
        assert comp._clips_over_cap(over) is True

    def test_at_cap_allowed(self):
        import routes.compilation as comp

        at = [{"cam": "RO000A"}] * comp.MAX_CLIPS
        assert comp._clips_over_cap(at) is False

    def test_non_list_is_not_over_cap(self):
        import routes.compilation as comp

        # A non-list clips value is handled (and dropped) by _validate_clips;
        # the cap helper must not raise on it.
        assert comp._clips_over_cap(None) is False
        assert comp._clips_over_cap("nope") is False

    def test_default_cap_is_sane(self):
        import routes.compilation as comp

        assert 1 <= comp.MAX_CLIPS <= 1000


# ---------------------------------------------------------------------------
# media.py batch-download / crop source caps
# ---------------------------------------------------------------------------

class _FakeResp:
    """Minimal stand-in for a streamed requests.Response."""

    def __init__(self, total_bytes: int, chunk: int = 65536):
        self._chunks = []
        remaining = total_bytes
        while remaining > 0:
            n = min(chunk, remaining)
            self._chunks.append(b"\0" * n)
            remaining -= n

    def iter_content(self, chunk_size: int):
        yield from self._chunks


class TestStreamDownloadCapped:
    def test_under_cap_writes_fully(self, tmp_path):
        import routes.media as media

        dest = tmp_path / "out.mkv"
        ok = media._stream_download_capped(_FakeResp(1000), str(dest), max_bytes=10_000)
        assert ok is True
        assert dest.stat().st_size == 1000

    def test_over_cap_aborts_and_removes_partial(self, tmp_path):
        import routes.media as media

        dest = tmp_path / "out.mkv"
        ok = media._stream_download_capped(
            _FakeResp(200_000), str(dest), max_bytes=100_000,
        )
        assert ok is False
        # Partial file must be cleaned up so it can't accumulate on /tmp.
        assert not dest.exists()

    def test_cap_constants_are_bounded(self):
        import routes.media as media

        # Aggregate batch cap and per-file cap must be positive and the
        # aggregate must not be absurdly large by default.
        assert media._BATCH_PER_FILE_MAX_BYTES > 0
        assert 0 < media._BATCH_TOTAL_MAX_BYTES <= 2 * 1024 * 1024 * 1024
        assert media._CROP_SOURCE_MAX_BYTES > 0


class TestStreamZipFromFiles:
    def test_produces_valid_extractable_zip(self, tmp_path):
        import io
        import zipfile

        import routes.media as media

        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"hello " * 5000)   # bigger than one 64K block
        b.write_bytes(b"world")

        entries = [("clip_a.mkv", str(a)), ("clip_b.mkv", str(b))]
        out = b"".join(media._stream_zip_from_files(entries))

        zf = zipfile.ZipFile(io.BytesIO(out))
        assert zf.namelist() == ["clip_a.mkv", "clip_b.mkv"]
        assert zf.read("clip_a.mkv") == a.read_bytes()
        assert zf.read("clip_b.mkv") == b.read_bytes()
        assert zf.testzip() is None  # CRCs all valid

    def test_cleanup_dir_removed_after_drain(self, tmp_path):
        import routes.media as media

        work = tmp_path / "work"
        work.mkdir()
        src = work / "f.bin"
        src.write_bytes(b"data")

        gen = media._stream_zip_from_files(
            [("f.mkv", str(src))], cleanup_dir=str(work),
        )
        # Drain fully -> the finally block must remove the temp dir.
        b"".join(gen)
        assert not work.exists()

    def test_empty_entries_yields_valid_empty_zip(self):
        import io
        import zipfile

        import routes.media as media

        out = b"".join(media._stream_zip_from_files([]))
        zf = zipfile.ZipFile(io.BytesIO(out))
        assert zf.namelist() == []


# ---------------------------------------------------------------------------
# archive.py stitch ffmpeg offload helper
# ---------------------------------------------------------------------------

class TestRunFfmpegCapped:
    def test_success_returns_code_and_stderr(self):
        import routes.archive as arch

        rc, stderr, timed_out = arch._run_ffmpeg_capped(
            ["sh", "-c", "echo err 1>&2; exit 0"], timeout=10,
        )
        assert rc == 0
        assert timed_out is False
        assert b"err" in stderr

    def test_nonzero_exit_propagates(self):
        import routes.archive as arch

        rc, _stderr, timed_out = arch._run_ffmpeg_capped(
            ["sh", "-c", "exit 3"], timeout=10,
        )
        assert rc == 3
        assert timed_out is False

    def test_timeout_kills_and_flags(self):
        import routes.archive as arch

        rc, _stderr, timed_out = arch._run_ffmpeg_capped(
            ["sh", "-c", "sleep 5"], timeout=0.3,
        )
        assert timed_out is True
        assert rc != 0

    def test_stitch_semaphore_bounded(self):
        import routes.archive as arch

        assert arch._STITCH_CONCURRENCY >= 1


# ---------------------------------------------------------------------------
# BoundedCache — per-process cache LRU cap (multi-worker OOM guard)
# ---------------------------------------------------------------------------
#
# Every gunicorn worker holds its own in-heap prefetch/proxy/platepar caches.
# Before this cap they grew with uptime × cameras × nights and 2 workers spiked
# the ~15 GB dev box into the OOM killer. These tests pin the invariant that
# makes >1 worker safe: each cache's entry count stays <= cap, evicting LRU.

class TestBoundedCache:
    def test_evicts_lru_at_cap(self):
        from cache_store import BoundedCache

        cap = 4
        c = BoundedCache(cap)
        for i in range(cap + 3):  # insert cap + N
            c[f"k{i}"] = i

        # Size never exceeds the cap.
        assert len(c) == cap
        # Oldest-inserted (k0..k2) evicted; newest (k3..k6) retained.
        assert set(c.keys()) == {"k3", "k4", "k5", "k6"}
        assert "k0" not in c
        assert c["k6"] == 6

    def test_reinsert_refreshes_recency(self):
        from cache_store import BoundedCache

        c = BoundedCache(3)
        c["a"] = 1
        c["b"] = 2
        c["c"] = 3
        # Touch "a" so it is now the most-recently-used, not the LRU.
        c["a"] = 10
        # Next insert must evict "b" (now the true LRU), not "a".
        c["d"] = 4

        assert len(c) == 3
        assert "b" not in c
        assert c["a"] == 10
        assert set(c.keys()) == {"a", "c", "d"}

    def test_zero_cap_is_unbounded(self):
        from cache_store import BoundedCache

        c = BoundedCache(0)  # 0 == disabled, behaves like a plain dict
        for i in range(1000):
            c[i] = i
        assert len(c) == 1000

    def test_get_and_pop_behave_like_dict(self):
        from cache_store import BoundedCache

        c = BoundedCache(8)
        c["x"] = 1
        assert c.get("x") == 1
        assert c.get("missing") is None
        assert c.get("missing", "dflt") == "dflt"
        assert c.pop("x") == 1
        assert "x" not in c
        assert c.pop("x", None) is None

    def test_eviction_is_thread_safe_under_a_lock(self):
        """The caches are mutated by request + background threads; eviction
        runs inside the caller's existing lock. Prove that concurrent locked
        inserts never exceed the cap and never raise (dict-resize RuntimeError
        or popitem-on-empty)."""
        import threading

        from cache_store import BoundedCache

        cap = 32
        c = BoundedCache(cap)
        lock = threading.Lock()
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker(base: int) -> None:
            try:
                barrier.wait()
                for i in range(500):
                    with lock:
                        c[(base, i)] = i
            except BaseException as exc:  # noqa: BLE001 -- record for assert
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(b,)) for b in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(c) == cap

    def test_cap_env_override_and_default(self, monkeypatch):
        from cache_store import _cache_cap

        # Default when the env var is absent.
        monkeypatch.delenv("ROVIMEN_TEST_CACHE_MAX", raising=False)
        assert _cache_cap("ROVIMEN_TEST_CACHE_MAX", 512) == 512

        # Explicit override wins.
        monkeypatch.setenv("ROVIMEN_TEST_CACHE_MAX", "10")
        assert _cache_cap("ROVIMEN_TEST_CACHE_MAX", 512) == 10

        # Non-integer falls back to the default (never raises at boot).
        monkeypatch.setenv("ROVIMEN_TEST_CACHE_MAX", "not-a-number")
        assert _cache_cap("ROVIMEN_TEST_CACHE_MAX", 512) == 512

        # Negative clamps to 0 (== disabled), never a negative cap.
        monkeypatch.setenv("ROVIMEN_TEST_CACHE_MAX", "-5")
        assert _cache_cap("ROVIMEN_TEST_CACHE_MAX", 512) == 0

    def test_normal_scale_does_not_evict(self):
        """Behaviour-preservation: at the tens-of-entries scale the 'cache
        sizes' log showed in normal operation, the default caps never evict,
        so nothing user-visible changes."""
        from cache_store import BoundedCache

        c = BoundedCache(512)  # matches the prefetch-dedup default
        # ~100 cameras × a couple of live nights — still well under the cap.
        n = 0
        for night in ("20260701", "20260702"):
            for host in range(20):
                for cam in range(5):
                    c[(f"host{host}", f"cam{cam}", night)] = float(n)
                    n += 1
        # No eviction: every distinct key is still present.
        assert n == 200
        assert len(c) == 200
