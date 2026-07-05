"""Tests for encoder.py idempotency and crash recovery — P1 #8.

P1 #8: If a crash occurs between dst_tmp.rename(mkv) and mark_reencoded(),
       the next run double-encodes (cumulative quality loss) because the chunk
       is now the encoded version but the flag still says reencoded=False.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import flags_manager


STATION = "RO000H"
DATE_STR = "20260322"


@pytest.fixture
def night_dir(tmp_path):
    cap = tmp_path / "color_capture" / STATION / DATE_STR
    cap.mkdir(parents=True)
    return cap


@pytest.fixture
def cfg(tmp_path):
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "segment_duration": 20,
        "compression_level": 2,
        "stations": {
            STATION: {"rms_data_path": str(tmp_path / "rms"), "rotate": False},
        },
        "services": {"reencode": {"enabled": True}},
        "overlay": {"enabled": False},
    }


class TestCrashRecoveryDoubleEncode:
    """Document the crash-between-rename-and-flag scenario."""

    def test_reencoded_false_after_simulated_crash(self, night_dir, cfg):
        """Simulate: file was renamed (encoded) but flag not set.
        Verify the state is inconsistent — reencoded=False for an already-encoded file."""
        chunk_name = f"{STATION}_{DATE_STR}_210000_color.mkv"
        mkv = night_dir / chunk_name
        mkv.write_bytes(b"already-encoded-content")

        # Mark chunk as ready (but NOT reencoded)
        flags_manager.mark_ready(STATION, DATE_STR, chunk_name, cfg)

        state = flags_manager.load(STATION, DATE_STR, cfg)
        chunk = state["chunks"].get(chunk_name, {})
        assert chunk.get("reencoded") is False
        # In reality, the file IS now the encoded version — but the flag
        # says it's not, so the encoder will re-encode it (quality loss).

    def test_mark_reencoded_fixes_state(self, night_dir, cfg):
        """After mark_reencoded, the flag is True and re-encoding is skipped."""
        chunk_name = f"{STATION}_{DATE_STR}_210000_color.mkv"
        mkv = night_dir / chunk_name
        mkv.write_bytes(b"encoded-content")

        flags_manager.mark_ready(STATION, DATE_STR, chunk_name, cfg)
        flags_manager.mark_reencoded(STATION, DATE_STR, chunk_name, cfg)

        state = flags_manager.load(STATION, DATE_STR, cfg)
        assert state["chunks"][chunk_name]["reencoded"] is True

    def test_reencoded_flag_survives_reload(self, night_dir, cfg):
        """The reencoded flag persists across load/save cycles."""
        chunk_name = f"{STATION}_{DATE_STR}_210000_color.mkv"
        (night_dir / chunk_name).write_bytes(b"")

        flags_manager.mark_ready(STATION, DATE_STR, chunk_name, cfg)
        flags_manager.mark_reencoded(STATION, DATE_STR, chunk_name, cfg)

        state1 = flags_manager.load(STATION, DATE_STR, cfg)
        assert state1["chunks"][chunk_name]["reencoded"] is True

        # Load again (simulates process restart)
        state2 = flags_manager.load(STATION, DATE_STR, cfg)
        assert state2["chunks"][chunk_name]["reencoded"] is True

    def test_lock_preserved_through_reencode(self, night_dir, cfg):
        """A locked chunk that gets reencoded keeps its lock info."""
        chunk_name = f"{STATION}_{DATE_STR}_210000_color.mkv"
        (night_dir / chunk_name).write_bytes(b"")

        flags_manager.mark_ready(STATION, DATE_STR, chunk_name, cfg)
        flags_manager.lock_chunk(STATION, DATE_STR, chunk_name,
                                 {"lock_type": "detection", "meteor_time": "2026-03-22T21:00:05"},
                                 cfg)
        flags_manager.mark_reencoded(STATION, DATE_STR, chunk_name, cfg)

        state = flags_manager.load(STATION, DATE_STR, cfg)
        chunk = state["chunks"][chunk_name]
        assert chunk["reencoded"] is True
        assert chunk["lock"] is not None
        assert chunk["lock"]["lock_type"] == "detection"
