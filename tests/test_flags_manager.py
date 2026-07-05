"""Tests for flags_manager.py — central state.json manager."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import flags_manager


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def station():
    return "RO000H"


@pytest.fixture
def date():
    return "20260322"


@pytest.fixture
def chunk():
    return "RO000H_20260322_210000_color.mkv"


@pytest.fixture
def cfg(tmp_path):
    """Config dict pointing videocapture_path at a tmp dir."""
    return {"videocapture_path": str(tmp_path / "color_capture")}


@pytest.fixture(autouse=True)
def _clear_warn_set():
    """Reset the module-level warning dedup set between tests."""
    flags_manager._unreadable_warned.clear()
    yield
    flags_manager._unreadable_warned.clear()


# -- _state_path fallback chain ---------------------------------------------

class TestStatePath:
    def test_uses_videocapture_path(self, tmp_path, station, date):
        cfg = {"videocapture_path": str(tmp_path / "vcap")}
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "vcap" / station / date / "state.json"
        )

    def test_falls_back_to_color_video_path(self, tmp_path, station, date):
        cfg = {"color_video_path": str(tmp_path / "cvid")}
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "cvid" / station / date / "state.json"
        )

    def test_falls_back_to_reenc_path(self, tmp_path, station, date):
        cfg = {"reenc_path": str(tmp_path / "reenc")}
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "reenc" / station / date / "state.json"
        )

    def test_falls_back_to_color_capture_path(self, tmp_path, station, date):
        cfg = {"color_capture_path": str(tmp_path / "ccap")}
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "ccap" / station / date / "state.json"
        )

    def test_falls_back_to_ssd_color_path(self, tmp_path, station, date):
        cfg = {"ssd_color_path": str(tmp_path / "ssd")}
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "ssd" / station / date / "state.json"
        )

    def test_falls_back_to_home_color_capture(self, station, date):
        cfg = {}
        expected = Path.home() / "color_capture" / station / date / "state.json"
        assert flags_manager._state_path(station, date, cfg) == expected

    def test_first_non_empty_wins(self, tmp_path, station, date):
        """videocapture_path="" should be falsy, so color_video_path wins."""
        cfg = {
            "videocapture_path": "",
            "color_video_path": str(tmp_path / "winner"),
        }
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "winner" / station / date / "state.json"
        )


# -- _empty_state / _empty_chunk --------------------------------------------

class TestEmptyStructures:
    def test_empty_state_fields(self, station, date):
        state = flags_manager._empty_state(station, date)
        assert state["station"] == station
        assert state["date"] == date
        assert state["chunks"] == {}
        assert state["rms_complete"] is False
        assert state["morning_done"] is False
        assert state["timelapse_done"] is False
        assert state["timelapse_deferred"] is False
        assert state["timelapse_uploaded"] is False
        assert state["night_stack_uploaded"] is False

    def test_empty_chunk_fields(self):
        chunk = flags_manager._empty_chunk()
        assert chunk == {
            "ready": False,
            "stacked": False,
            "reencoded": False,
            "lock": None,
            "uploaded": False,
            "stack_uploaded": False,
        }


# -- Load / Save core -------------------------------------------------------

class TestLoadSave:
    def test_load_returns_empty_when_no_file(self, station, date, cfg):
        state = flags_manager.load(station, date, cfg)
        assert state["station"] == station
        assert state["chunks"] == {}

    def test_save_creates_directory_structure(self, station, date, cfg):
        state = flags_manager._empty_state(station, date)
        flags_manager.save(state, station, date, cfg)
        path = flags_manager._state_path(station, date, cfg)
        assert path.exists()
        assert path.parent.is_dir()

    def test_save_writes_valid_json(self, station, date, cfg):
        state = flags_manager._empty_state(station, date)
        state["rms_complete"] = True
        flags_manager.save(state, station, date, cfg)
        path = flags_manager._state_path(station, date, cfg)
        loaded = json.loads(path.read_text())
        assert loaded["rms_complete"] is True

    def test_load_after_save_roundtrips(self, station, date, cfg, chunk):
        state = flags_manager._empty_state(station, date)
        state["chunks"][chunk] = flags_manager._empty_chunk()
        state["chunks"][chunk]["ready"] = True
        flags_manager.save(state, station, date, cfg)

        loaded = flags_manager.load(station, date, cfg)
        assert loaded == state

    def test_load_corrupted_json_returns_empty(self, station, date, cfg):
        """A corrupted state.json must not crash -- return empty state."""
        path = flags_manager._state_path(station, date, cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{{{not valid json")

        state = flags_manager.load(station, date, cfg)
        assert state["station"] == station
        assert state["chunks"] == {}

    def test_load_binary_garbage_returns_empty(self, station, date, cfg):
        path = flags_manager._state_path(station, date, cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\xff\xfe\x80")

        state = flags_manager.load(station, date, cfg)
        assert state["station"] == station


# -- Chunk-level mark functions ----------------------------------------------

class TestMarkReady:
    def test_sets_ready_flag(self, station, date, chunk, cfg):
        flags_manager.mark_ready(station, date, chunk, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["ready"] is True

    def test_creates_chunk_entry_if_missing(self, station, date, chunk, cfg):
        flags_manager.mark_ready(station, date, chunk, cfg)
        state = flags_manager.load(station, date, cfg)
        # Should have all default fields besides ready=True
        assert state["chunks"][chunk]["stacked"] is False
        assert state["chunks"][chunk]["lock"] is None


class TestMarkStacked:
    def test_sets_stacked_flag(self, station, date, chunk, cfg):
        flags_manager.mark_stacked(station, date, chunk, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["stacked"] is True


class TestMarkReencoded:
    def test_sets_reencoded_flag(self, station, date, chunk, cfg):
        flags_manager.mark_reencoded(station, date, chunk, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["reencoded"] is True


# -- Lock management ---------------------------------------------------------

class TestLockChunk:
    def test_sets_lock_metadata(self, station, date, chunk, cfg):
        lock_info = {"lock_type": "detection", "detection_time": "20260322_210500"}
        flags_manager.lock_chunk(station, date, chunk, lock_info, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["lock"] == lock_info

    def test_also_marks_ready(self, station, date, chunk, cfg):
        lock_info = {"lock_type": "detection"}
        flags_manager.lock_chunk(station, date, chunk, lock_info, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["ready"] is True

    def test_creates_chunk_entry_if_missing(self, station, date, cfg):
        new_chunk = "RO000H_20260322_220000_color.mkv"
        lock_info = {"lock_type": "manual"}
        flags_manager.lock_chunk(station, date, new_chunk, lock_info, cfg)
        state = flags_manager.load(station, date, cfg)
        assert new_chunk in state["chunks"]
        assert state["chunks"][new_chunk]["lock"] == lock_info


class TestUnlockChunk:
    def test_clears_lock(self, station, date, chunk, cfg):
        lock_info = {"lock_type": "detection"}
        flags_manager.lock_chunk(station, date, chunk, lock_info, cfg)
        flags_manager.unlock_chunk(station, date, chunk, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["lock"] is None

    def test_noop_on_nonexistent_chunk(self, station, date, cfg):
        """Unlocking a chunk that does not exist in state should not crash."""
        flags_manager.unlock_chunk(station, date, "nonexistent.mkv", cfg)
        state = flags_manager.load(station, date, cfg)
        assert "nonexistent.mkv" not in state["chunks"]


class TestRelockChunk:
    def test_replaces_lock_metadata(self, station, date, chunk, cfg):
        original = {"lock_type": "detection", "detection_time": "20260322_210500"}
        updated = {
            "lock_type": "detection",
            "detection_time": "20260322_210500",
            "meteor_time": "2026-03-22T21:05:03.456",
        }
        flags_manager.lock_chunk(station, date, chunk, original, cfg)
        flags_manager.relock_chunk(station, date, chunk, updated, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["lock"] == updated
        assert state["chunks"][chunk]["lock"]["meteor_time"] == "2026-03-22T21:05:03.456"

    def test_relock_on_unlocked_chunk(self, station, date, chunk, cfg):
        """Relock should work even if the chunk was never locked."""
        lock_info = {"lock_type": "eon_pass"}
        flags_manager.relock_chunk(station, date, chunk, lock_info, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["lock"] == lock_info


# -- Night-level flags -------------------------------------------------------

class TestRmsComplete:
    def test_marks_rms_complete(self, station, date, cfg):
        flags_manager.mark_rms_complete(station, date, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["rms_complete"] is True


class TestMorningDone:
    def test_marks_morning_done(self, station, date, cfg):
        flags_manager.mark_morning_done(station, date, cfg)
        assert flags_manager.is_morning_done(station, date, cfg) is True

    def test_is_morning_done_false_by_default(self, station, date, cfg):
        assert flags_manager.is_morning_done(station, date, cfg) is False


# -- Upload tracking ---------------------------------------------------------

class TestChunkUploaded:
    def test_mark_and_query(self, station, date, chunk, cfg):
        assert flags_manager.is_chunk_uploaded(station, date, chunk, cfg) is False
        flags_manager.mark_chunk_uploaded(station, date, chunk, cfg)
        assert flags_manager.is_chunk_uploaded(station, date, chunk, cfg) is True

    def test_unmark_clears_uploaded_and_stack_uploaded(self, station, date, chunk, cfg):
        flags_manager.mark_chunk_uploaded(station, date, chunk, cfg)
        flags_manager.mark_chunk_stack_uploaded(station, date, chunk, cfg)
        assert flags_manager.is_chunk_uploaded(station, date, chunk, cfg) is True
        assert flags_manager.is_chunk_stack_uploaded(station, date, chunk, cfg) is True

        flags_manager.unmark_chunk_uploaded(station, date, chunk, cfg)
        assert flags_manager.is_chunk_uploaded(station, date, chunk, cfg) is False
        assert flags_manager.is_chunk_stack_uploaded(station, date, chunk, cfg) is False

    def test_unmark_noop_on_missing_chunk(self, station, date, cfg):
        """Unmarking a non-existent chunk should not crash or create it."""
        flags_manager.unmark_chunk_uploaded(station, date, "ghost.mkv", cfg)
        state = flags_manager.load(station, date, cfg)
        assert "ghost.mkv" not in state["chunks"]


class TestChunkStackUploaded:
    def test_mark_and_query(self, station, date, chunk, cfg):
        assert flags_manager.is_chunk_stack_uploaded(station, date, chunk, cfg) is False
        flags_manager.mark_chunk_stack_uploaded(station, date, chunk, cfg)
        assert flags_manager.is_chunk_stack_uploaded(station, date, chunk, cfg) is True


class TestTimelapseUploaded:
    def test_mark_and_query(self, station, date, cfg):
        assert flags_manager.is_timelapse_uploaded(station, date, cfg) is False
        flags_manager.mark_timelapse_uploaded(station, date, cfg)
        assert flags_manager.is_timelapse_uploaded(station, date, cfg) is True


class TestNightStackUploaded:
    def test_mark_and_query(self, station, date, cfg):
        assert flags_manager.is_night_stack_uploaded(station, date, cfg) is False
        flags_manager.mark_night_stack_uploaded(station, date, cfg)
        assert flags_manager.is_night_stack_uploaded(station, date, cfg) is True


# -- Timelapse done / deferred -----------------------------------------------

class TestTimelapseDone:
    def test_mark_and_query(self, station, date, cfg):
        assert flags_manager.is_timelapse_done(station, date, cfg) is False
        flags_manager.mark_timelapse_done(station, date, cfg)
        assert flags_manager.is_timelapse_done(station, date, cfg) is True

    def test_mark_done_clears_deferred(self, station, date, cfg):
        flags_manager.mark_timelapse_deferred(station, date, cfg)
        assert flags_manager.is_timelapse_deferred(station, date, cfg) is True

        flags_manager.mark_timelapse_done(station, date, cfg)
        assert flags_manager.is_timelapse_done(station, date, cfg) is True
        assert flags_manager.is_timelapse_deferred(station, date, cfg) is False


class TestTimelapseDeferred:
    def test_mark_and_query(self, station, date, cfg):
        assert flags_manager.is_timelapse_deferred(station, date, cfg) is False
        flags_manager.mark_timelapse_deferred(station, date, cfg)
        assert flags_manager.is_timelapse_deferred(station, date, cfg) is True


# -- Concurrent operations ---------------------------------------------------

class TestConcurrency:
    def test_parallel_marks_do_not_corrupt_state(self, station, date, cfg):
        """10 threads each marking a different chunk must not lose any entry."""
        chunk_names = [f"RO000H_20260322_21{i:02d}00_color.mkv" for i in range(10)]
        errors: list[Exception] = []

        def mark_one(name: str):
            try:
                flags_manager.mark_ready(station, date, name, cfg)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=mark_one, args=(n,)) for n in chunk_names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        state = flags_manager.load(station, date, cfg)
        assert len(state["chunks"]) == 10
        for name in chunk_names:
            assert state["chunks"][name]["ready"] is True

    def test_parallel_mixed_operations(self, station, date, cfg):
        """Mixed mark/lock/unlock from multiple threads must not crash."""
        chunk = "RO000H_20260322_210000_color.mkv"
        errors: list[Exception] = []

        def worker(op: str):
            try:
                if op == "ready":
                    flags_manager.mark_ready(station, date, chunk, cfg)
                elif op == "lock":
                    flags_manager.lock_chunk(station, date, chunk,
                                             {"lock_type": "detection"}, cfg)
                elif op == "unlock":
                    flags_manager.unlock_chunk(station, date, chunk, cfg)
                elif op == "stacked":
                    flags_manager.mark_stacked(station, date, chunk, cfg)
                elif op == "uploaded":
                    flags_manager.mark_chunk_uploaded(station, date, chunk, cfg)
            except Exception as exc:
                errors.append(exc)

        ops = ["ready", "lock", "stacked", "uploaded", "unlock",
               "ready", "lock", "stacked", "uploaded", "unlock"]
        threads = [threading.Thread(target=worker, args=(op,)) for op in ops]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        # State should load cleanly regardless of operation order
        state = flags_manager.load(station, date, cfg)
        assert chunk in state["chunks"]


# -- make_state_json fixture integration -------------------------------------

class TestMakeStateJsonFixture:
    def test_pre_populated_state_loads(self, make_state_json, station, date, cfg, chunk):
        """The conftest make_state_json fixture should produce loadable state."""
        state = flags_manager._empty_state(station, date)
        state["chunks"][chunk] = flags_manager._empty_chunk()
        state["chunks"][chunk]["ready"] = True
        state["chunks"][chunk]["lock"] = {"lock_type": "detection"}
        make_state_json(station, date, state, cfg)

        loaded = flags_manager.load(station, date, cfg)
        assert loaded["chunks"][chunk]["ready"] is True
        assert loaded["chunks"][chunk]["lock"]["lock_type"] == "detection"


# -- mark_ready_batch --------------------------------------------------------

class TestMarkReadyBatch:

    def test_marks_multiple_chunks(self, tmp_path):
        cfg = {"videocapture_path": str(tmp_path / "color_capture")}
        night = tmp_path / "color_capture" / "RO000H" / "20260315"
        night.mkdir(parents=True)
        names = ["RO000H_20260315_210000_color.mkv", "RO000H_20260315_210020_color.mkv"]
        flags_manager.mark_ready_batch("RO000H", "20260315", names, cfg)
        state = flags_manager.load("RO000H", "20260315", cfg)
        for name in names:
            assert state["chunks"][name]["ready"] is True

    def test_empty_list_is_noop(self, tmp_path):
        cfg = {"videocapture_path": str(tmp_path / "color_capture")}
        night = tmp_path / "color_capture" / "RO000H" / "20260315"
        night.mkdir(parents=True)
        flags_manager.mark_ready_batch("RO000H", "20260315", [], cfg)
        state = flags_manager.load("RO000H", "20260315", cfg)
        assert state["chunks"] == {}

    def test_preserves_existing_chunks(self, tmp_path):
        cfg = {"videocapture_path": str(tmp_path / "color_capture")}
        night = tmp_path / "color_capture" / "RO000H" / "20260315"
        night.mkdir(parents=True)
        flags_manager.mark_ready("RO000H", "20260315", "existing.mkv", cfg)
        flags_manager.mark_ready_batch("RO000H", "20260315", ["new.mkv"], cfg)
        state = flags_manager.load("RO000H", "20260315", cfg)
        assert state["chunks"]["existing.mkv"]["ready"] is True
        assert state["chunks"]["new.mkv"]["ready"] is True
