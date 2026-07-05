"""Tests for rovimen_lock.py — MKV locking via JSON sidecar files."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import rovimen_lock


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def mkv(tmp_path) -> Path:
    """Create a dummy MKV file and return its path."""
    p = tmp_path / "RO000H_20260322_210000_color.mkv"
    p.write_bytes(b"\x1a\x45\xdf\xa3")  # minimal Matroska header bytes
    return p


@pytest.fixture
def detection_time() -> datetime:
    return datetime(2026, 3, 22, 21, 5, 0)


@pytest.fixture
def meteor_time() -> datetime:
    return datetime(2026, 3, 22, 21, 5, 3, 456000)


# -- _lock_path --------------------------------------------------------------

class TestLockPath:
    def test_appends_locked_suffix(self, mkv):
        lp = rovimen_lock._lock_path(mkv)
        assert lp == mkv.with_suffix(".mkv.locked")
        assert lp.name == "RO000H_20260322_210000_color.mkv.locked"


# -- lock --------------------------------------------------------------------

class TestLock:
    def test_creates_sidecar_with_correct_json(self, mkv, detection_time):
        rovimen_lock.lock(mkv, "detection", detection_time=detection_time)
        lp = mkv.with_suffix(".mkv.locked")
        assert lp.exists()
        data = json.loads(lp.read_text())
        assert data["lock_type"] == "detection"
        assert data["detection_time"] == "20260322_210500"

    def test_meteor_time_stored_as_iso(self, mkv, detection_time, meteor_time):
        rovimen_lock.lock(mkv, "detection",
                          detection_time=detection_time,
                          meteor_time=meteor_time)
        data = json.loads(mkv.with_suffix(".mkv.locked").read_text())
        assert data["meteor_time"] == "2026-03-22T21:05:03.456000"

    def test_meteor_time_absent_when_not_provided(self, mkv, detection_time):
        rovimen_lock.lock(mkv, "detection", detection_time=detection_time)
        data = json.loads(mkv.with_suffix(".mkv.locked").read_text())
        assert "meteor_time" not in data

    def test_detection_time_none_stores_null(self, mkv):
        rovimen_lock.lock(mkv, "manual")
        data = json.loads(mkv.with_suffix(".mkv.locked").read_text())
        assert data["detection_time"] is None

    def test_idempotent_no_overwrite(self, mkv, detection_time):
        """Second lock() call is a no-op -- original metadata preserved."""
        rovimen_lock.lock(mkv, "detection", detection_time=detection_time)
        original_content = mkv.with_suffix(".mkv.locked").read_text()

        # Try to lock again with different metadata
        new_time = datetime(2026, 3, 22, 22, 0, 0)
        rovimen_lock.lock(mkv, "manual", detection_time=new_time)

        assert mkv.with_suffix(".mkv.locked").read_text() == original_content

    def test_lock_type_variants(self, mkv):
        """Various lock_type values should all work."""
        for lock_type in ("detection", "manual", "eon_pass", "fireball"):
            rovimen_lock.unlock(mkv)
            rovimen_lock.lock(mkv, lock_type)
            data = json.loads(mkv.with_suffix(".mkv.locked").read_text())
            assert data["lock_type"] == lock_type


# -- unlock ------------------------------------------------------------------

class TestUnlock:
    def test_removes_sidecar(self, mkv, detection_time):
        rovimen_lock.lock(mkv, "detection", detection_time=detection_time)
        assert mkv.with_suffix(".mkv.locked").exists()
        rovimen_lock.unlock(mkv)
        assert not mkv.with_suffix(".mkv.locked").exists()

    def test_safe_on_nonexistent(self, mkv):
        """Unlocking when no sidecar exists should not raise."""
        rovimen_lock.unlock(mkv)  # no error

    def test_safe_on_nonexistent_mkv(self, tmp_path):
        """Unlocking a path where neither MKV nor sidecar exist should not raise."""
        ghost = tmp_path / "ghost.mkv"
        rovimen_lock.unlock(ghost)


# -- is_locked ---------------------------------------------------------------

class TestIsLocked:
    def test_true_when_locked(self, mkv):
        rovimen_lock.lock(mkv, "detection")
        assert rovimen_lock.is_locked(mkv) is True

    def test_false_when_not_locked(self, mkv):
        assert rovimen_lock.is_locked(mkv) is False

    def test_false_after_unlock(self, mkv):
        rovimen_lock.lock(mkv, "detection")
        rovimen_lock.unlock(mkv)
        assert rovimen_lock.is_locked(mkv) is False


# -- get_lock_info -----------------------------------------------------------

class TestGetLockInfo:
    def test_returns_metadata(self, mkv, detection_time, meteor_time):
        rovimen_lock.lock(mkv, "detection",
                          detection_time=detection_time,
                          meteor_time=meteor_time)
        info = rovimen_lock.get_lock_info(mkv)
        assert info is not None
        assert info["lock_type"] == "detection"
        assert info["detection_time"] == "20260322_210500"
        assert info["meteor_time"] == "2026-03-22T21:05:03.456000"

    def test_returns_none_when_not_locked(self, mkv):
        assert rovimen_lock.get_lock_info(mkv) is None

    def test_returns_none_on_corrupted_json(self, mkv):
        lp = mkv.with_suffix(".mkv.locked")
        lp.write_text("{{{bad json")
        assert rovimen_lock.get_lock_info(mkv) is None

    def test_returns_none_on_binary_garbage(self, mkv):
        lp = mkv.with_suffix(".mkv.locked")
        lp.write_bytes(b"\x00\xff\xfe")
        assert rovimen_lock.get_lock_info(mkv) is None

    def test_missing_fields_return_none_values(self, mkv):
        """A .locked file with only lock_type should still parse without error."""
        lp = mkv.with_suffix(".mkv.locked")
        lp.write_text(json.dumps({"lock_type": "manual"}))
        info = rovimen_lock.get_lock_info(mkv)
        assert info["lock_type"] == "manual"
        assert info["detection_time"] is None
        assert info["meteor_time"] is None

    def test_meteor_time_field_optional(self, mkv, detection_time):
        """Lock without meteor_time should return None for that field."""
        rovimen_lock.lock(mkv, "detection", detection_time=detection_time)
        info = rovimen_lock.get_lock_info(mkv)
        assert info["meteor_time"] is None


# -- get_lock_type -----------------------------------------------------------

class TestGetLockType:
    def test_returns_type_string(self, mkv):
        rovimen_lock.lock(mkv, "detection")
        assert rovimen_lock.get_lock_type(mkv) == "detection"

    def test_returns_none_when_not_locked(self, mkv):
        assert rovimen_lock.get_lock_type(mkv) is None

    def test_returns_none_on_corrupted_sidecar(self, mkv):
        mkv.with_suffix(".mkv.locked").write_text("not json")
        assert rovimen_lock.get_lock_type(mkv) is None


# -- relock ------------------------------------------------------------------

class TestRelock:
    def test_replaces_metadata(self, mkv, detection_time, meteor_time):
        rovimen_lock.lock(mkv, "detection", detection_time=detection_time)
        new_meteor = datetime(2026, 3, 22, 21, 5, 3, 789000)
        rovimen_lock.relock(mkv, "eon_pass",
                            detection_time=detection_time,
                            meteor_time=new_meteor)
        info = rovimen_lock.get_lock_info(mkv)
        assert info["lock_type"] == "eon_pass"
        assert info["meteor_time"] == "2026-03-22T21:05:03.789000"

    def test_relock_on_unlocked_file(self, mkv, detection_time):
        """Relock should work even if the file was not previously locked."""
        rovimen_lock.relock(mkv, "manual", detection_time=detection_time)
        assert rovimen_lock.is_locked(mkv) is True
        assert rovimen_lock.get_lock_type(mkv) == "manual"

    def test_relock_overwrites_different_type(self, mkv):
        rovimen_lock.lock(mkv, "detection")
        rovimen_lock.relock(mkv, "fireball")
        assert rovimen_lock.get_lock_type(mkv) == "fireball"


# -- detection_time formatting -----------------------------------------------

class TestDetectionTimeFormat:
    def test_format_is_yyyymmdd_hhmmss(self, mkv):
        dt = datetime(2026, 1, 5, 3, 9, 7)
        rovimen_lock.lock(mkv, "detection", detection_time=dt)
        data = json.loads(mkv.with_suffix(".mkv.locked").read_text())
        assert data["detection_time"] == "20260105_030907"
