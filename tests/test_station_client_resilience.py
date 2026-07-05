"""Tests for poll-loop resilience in station_client.start_polling.

Verifies that a transient exception from pool.map does not kill the poll
thread — the loop must survive and continue iterating.

Design: rather than running real threads (which would require real timeouts),
we patch the ThreadPoolExecutor.map method to raise on the first call and
succeed on the second, then drive the loop body synchronously by calling the
inner function directly via a minimal harness.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

import station_client


# ---------------------------------------------------------------------------
# Minimal stubs so start_polling can be imported without a running dashboard.
# ---------------------------------------------------------------------------

def _make_config(n_stations: int = 2) -> SimpleNamespace:
    stations = {f"station{i}": SimpleNamespace(host=f"10.0.0.{i}") for i in range(n_stations)}
    return SimpleNamespace(stations=stations)


def _make_tunnels() -> MagicMock:
    return MagicMock()


def _make_cache() -> MagicMock:
    cache = MagicMock()
    cache.get_status.return_value = None
    return MagicMock()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPollLoopResilience:
    """poll_status and poll_vitals must survive pool.map raising an exception."""

    def test_poll_status_loop_survives_map_exception(self):
        """If pool.map raises, the loop body catches it and continues."""
        config = _make_config(2)
        tunnels = _make_tunnels()
        cache = _make_cache()

        iterations: list[int] = []
        call_count = 0

        def fake_map_for_status(fn, keys):
            nonlocal call_count
            call_count += 1
            iterations.append(call_count)
            if call_count == 1:
                raise RuntimeError("transient pool error")
            # On subsequent calls succeed but signal we should stop.
            return []

        # We patch ThreadPoolExecutor so we can control pool.map and also
        # break the loop after a couple of iterations by patching time.sleep
        # to raise StopIteration after the second iteration.
        sleep_count = 0

        def fake_sleep(seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise StopIteration("done")

        with patch("station_client.ThreadPoolExecutor") as mock_executor_cls, \
             patch("station_client.time") as mock_time:
            mock_pool = MagicMock()
            mock_pool.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.map.side_effect = fake_map_for_status
            mock_executor_cls.return_value = mock_pool
            mock_time.sleep.side_effect = fake_sleep

            # start_polling spawns daemon threads; we call poll_status directly
            # by extracting it. The simplest way: call start_polling and let
            # the thread run, then join with a short timeout.
            # However, to keep the test deterministic we drive it inline by
            # patching the Thread constructor to run the target synchronously.
            ran_target = []

            original_thread = threading.Thread

            def capture_thread(*args, **kwargs):
                target = kwargs.get("target") or (args[0] if args else None)
                t = MagicMock()
                ran_target.append(target)
                return t

            with patch("station_client.threading") as mock_threading:
                mock_threading.Thread.side_effect = capture_thread

                station_client.start_polling(config, tunnels, cache)

                # Two threads registered: poll_status and poll_vitals.
                assert len(ran_target) == 2

            # Now drive poll_status directly (first captured target).
            poll_status_fn = ran_target[0]

            with pytest.raises(StopIteration):
                poll_status_fn()

            # The loop must have attempted pool.map at least twice despite the
            # first call raising — proving the exception was caught and the loop
            # continued.
            assert call_count >= 2, (
                f"Expected pool.map called at least twice (survived exception), "
                f"got {call_count}"
            )

    def test_poll_status_guard_exists_in_source(self):
        """Structural check: the poll_status loop body is wrapped in try/except."""
        import inspect
        src = inspect.getsource(station_client.start_polling)
        # The fix adds a try/except around pool.map in poll_status.
        # We verify the keyword sequence is present.
        assert "status poll cycle failed" in src, (
            "poll_status loop body must wrap pool.map in try/except and log "
            "'status poll cycle failed'"
        )

    def test_poll_vitals_guard_exists_in_source(self):
        """Structural check: the poll_vitals loop body is wrapped in try/except."""
        import inspect
        src = inspect.getsource(station_client.start_polling)
        assert "vitals poll cycle failed" in src, (
            "poll_vitals loop body must wrap pool.map in try/except and log "
            "'vitals poll cycle failed'"
        )


class TestLiveThumbCache:
    """StationCache stores the pushed newest-FF maxpixel per (station, cam) (§9)."""

    def test_set_get_roundtrip(self):
        cache = station_client.StationCache()
        assert cache.get_live_thumb("gmn0002", "RO000M") is None
        cache.set_live_thumb("gmn0002", "RO000M", b"webp",
                             content_type="image/webp",
                             ff_timestamp="2026-06-30T22:41:07.120Z")
        got = cache.get_live_thumb("gmn0002", "RO000M")
        assert got["data"] == b"webp"
        assert got["content_type"] == "image/webp"
        assert got["ff_timestamp"] == "2026-06-30T22:41:07.120Z"
        assert "received_at" in got

    def test_latest_writer_wins_per_cam(self):
        cache = station_client.StationCache()
        cache.set_live_thumb("gmn0002", "RO000M", b"old")
        cache.set_live_thumb("gmn0002", "RO000M", b"new")
        assert cache.get_live_thumb("gmn0002", "RO000M")["data"] == b"new"

    def test_isolated_per_station_and_cam(self):
        cache = station_client.StationCache()
        cache.set_live_thumb("gmn0002", "RO000M", b"a")
        cache.set_live_thumb("gmn0002", "RO000N", b"b")
        cache.set_live_thumb("gmnro10", "RO000M", b"c")
        assert cache.get_live_thumb("gmn0002", "RO000M")["data"] == b"a"
        assert cache.get_live_thumb("gmn0002", "RO000N")["data"] == b"b"
        assert cache.get_live_thumb("gmnro10", "RO000M")["data"] == b"c"
