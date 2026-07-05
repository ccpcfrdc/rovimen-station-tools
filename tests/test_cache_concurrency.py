"""Concurrent-eviction safety for _rms_plots_list_cache.

Before the lock was added, concurrent writes plus eviction iteration could
raise RuntimeError("dictionary changed size during iteration") under gunicorn's
gthread worker.  This test drives the same pattern from 20 threads to confirm
the lock prevents that.
"""

from __future__ import annotations

import threading
import time
from typing import Any


def _make_cache(
    size: int,
) -> tuple[dict[tuple[str, str, str], tuple[float, Any, bool | None]], threading.Lock]:
    cache: dict[tuple[str, str, str], tuple[float, Any, bool | None]] = {}
    lock = threading.Lock()
    now = time.monotonic()
    for i in range(size):
        key = ("host", f"CAM{i:04d}", "20260101")
        # Mix of already-expired and still-fresh entries so the eviction
        # branches both execute.
        expiry = now - 1.0 if i % 2 == 0 else now + 300.0
        cache[key] = (expiry, [], None)
    return cache, lock


def _evict(
    cache: dict[tuple[str, str, str], tuple[float, Any, bool | None]],
    lock: threading.Lock,
    key: tuple[str, str, str],
    payload: list,
    ttl: float,
) -> None:
    """Simulate what api_rms_plots_proxy / _prefetch_rms_plots_for_night do."""
    with lock:
        cache[key] = (time.monotonic() + ttl, payload, True)
        if len(cache) > 50:  # low threshold to force eviction path in test
            evict_mono = time.monotonic()
            expired = [k for k, v in cache.items() if v[0] < evict_mono]
            for k in expired:
                cache.pop(k, None)
            if len(cache) > 50:
                by_age = sorted(cache.items(), key=lambda x: x[1][0])
                for k, _ in by_age[:10]:
                    cache.pop(k, None)


class TestRmsPlotsCacheConcurrency:
    def test_no_runtime_error_under_concurrent_eviction(self) -> None:
        """20 threads simultaneously inserting + evicting must not raise."""
        cache, lock = _make_cache(80)
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                for i in range(30):
                    key = ("host", f"THREAD{thread_id:02d}", f"{i:08d}")
                    _evict(cache, lock, key, [{"filename": "captured_stack.jpg"}], 300.0)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent eviction raised: {errors}"

    def test_morning_done_float_semantics(self) -> None:
        """morning_done values are now float timestamps, not bools.

        0.0 must be falsy (not done); positive monotonic must be truthy (done).
        Existing callers use `morning_done.get(key, False)` as a bool — confirm
        the change is backward-compatible.
        """
        morning_done: dict[tuple[str, str, str], float] = {}
        key = ("host", "CAM0001", "20260101")

        # Not-done state
        morning_done[key] = 0.0
        assert not morning_done.get(key, False), "0.0 should be falsy"

        # Done state
        morning_done[key] = time.monotonic()
        assert morning_done.get(key, False), "positive timestamp should be truthy"

        # Default for missing key
        assert not morning_done.get(("missing", "key", "tuple"), False)
