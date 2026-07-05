"""Tests for dawn_process.py pipeline logic — P1 #9.

P1 #9: A single stuck camera blocks all cameras until 11:00 UTC deadline.
       Phase 6 (bulk reencode) is permanently skipped if deadline hits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

import flags_manager


STATION_A = "RO000H"
STATION_B = "RO000J"
DATE_STR = "20260322"


@pytest.fixture
def cfg(tmp_path):
    rms_a = tmp_path / "rms_a"
    rms_b = tmp_path / "rms_b"
    rms_a.mkdir()
    rms_b.mkdir()
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "segment_duration": 20,
        "compression_level": 2,
        "stations": {
            STATION_A: {"rms_data_path": str(rms_a), "rotate": False},
            STATION_B: {"rms_data_path": str(rms_b), "rotate": False},
        },
        "services": {
            "stacker": {"enabled": False},
            "reencode": {"enabled": True},
            "detection_lock": {"enabled": True},
            "archive_upload": {"enabled": False},
            "timelapse_build": {"enabled": False},
        },
        "overlay": {"enabled": False},
    }


class TestStuckCameraBlocking:
    """Document: the dawn pipeline waits for ALL cameras to finish RMS
    before processing ANY of them."""

    def test_rms_done_check_gates_all_cameras(self, cfg):
        """If station A is done but station B is not, neither is processed."""
        import dawn_process

        stations = cfg["stations"]

        # Simulate: station A has FTPdetectinfo in ArchivedFiles, station B does not
        rms_a = Path(cfg["stations"][STATION_A]["rms_data_path"])
        archived_a = rms_a / "ArchivedFiles"
        session_a = archived_a / f"{STATION_A}_{DATE_STR}_210000_000000"
        session_a.mkdir(parents=True)
        (session_a / f"FTPdetectinfo_{STATION_A}_{DATE_STR}_210000_000000.txt").write_text(
            "Meteor Count = 0\n"
        )

        # Station B has no session at all — _rms_done returns False
        done_a = dawn_process._rms_done(STATION_A, DATE_STR, str(rms_a))
        done_b = dawn_process._rms_done(STATION_B, DATE_STR,
                                        cfg["stations"][STATION_B]["rms_data_path"])

        assert done_a is True
        assert done_b is False

        # The pipeline checks ALL stations and returns 0 (retry) if any are waiting
        waiting = [
            sid for sid, scfg in stations.items()
            if not dawn_process._rms_done(sid, DATE_STR, scfg.get("rms_data_path", ""))
        ]
        assert STATION_B in waiting
        assert len(waiting) == 1

    def test_decommissioned_camera_excluded_from_wait(self, cfg):
        """A decommissioned camera should not block the pipeline."""
        cfg["stations"][STATION_B]["decommissioned"] = True
        stations = {
            sid: scfg for sid, scfg in cfg["stations"].items()
            if not scfg.get("decommissioned")
        }
        assert STATION_B not in stations
        assert STATION_A in stations


class TestDeadlinePhaseSkipping:
    """Document: past-deadline behaviour skips Phase 6 (bulk reencode)
    and marks morning_done, but the deferred reencode is never retried."""

    def test_morning_done_set_after_deadline_skip(self, tmp_path, cfg):
        """After deadline, morning_done is set even though Phase 6 didn't run."""
        cap = Path(cfg["videocapture_path"])
        night_a = cap / STATION_A / DATE_STR
        night_a.mkdir(parents=True)

        # Manually set morning_done to simulate the deadline path
        flags_manager.mark_morning_done(STATION_A, DATE_STR, cfg)
        assert flags_manager.is_morning_done(STATION_A, DATE_STR, cfg) is True

    def test_morning_done_blocks_next_run(self, tmp_path, cfg):
        """Once morning_done, the dawn pipeline returns 0 without processing."""
        cap = Path(cfg["videocapture_path"])
        night_a = cap / STATION_A / DATE_STR
        night_a.mkdir(parents=True)

        flags_manager.mark_morning_done(STATION_A, DATE_STR, cfg)
        flags_manager.mark_morning_done(STATION_B, DATE_STR, cfg)

        # Both stations are morning_done → pending is empty
        pending = {
            sid: scfg for sid, scfg in cfg["stations"].items()
            if not flags_manager.is_morning_done(sid, DATE_STR, cfg)
        }
        assert len(pending) == 0

    def test_unreencoded_chunks_remain_after_deadline(self, tmp_path, cfg):
        """Chunks that weren't reencoded before the deadline remain unreencoded.
        morning_done is True, so the pipeline won't retry them."""
        cap = Path(cfg["videocapture_path"])
        night_a = cap / STATION_A / DATE_STR
        night_a.mkdir(parents=True)

        chunk_name = f"{STATION_A}_{DATE_STR}_210000_color.mkv"
        (night_a / chunk_name).write_bytes(b"raw-video")
        flags_manager.mark_ready(STATION_A, DATE_STR, chunk_name, cfg)

        # Deadline hits → mark morning_done without reencoding
        flags_manager.mark_morning_done(STATION_A, DATE_STR, cfg)

        state = flags_manager.load(STATION_A, DATE_STR, cfg)
        chunk = state["chunks"].get(chunk_name, {})
        assert chunk.get("reencoded") is False
        assert state.get("morning_done") is True


class TestParallelPhases:
    """Verify _run_encode_locked_all and _run_upload_all invoke all stations."""

    def test_encode_locked_all_calls_both_stations(self, cfg):
        import dawn_process
        cap = Path(cfg["videocapture_path"])
        for sid in [STATION_A, STATION_B]:
            (cap / sid / DATE_STR).mkdir(parents=True)

        with patch("dawn_process.detection_lock") as mock_dl, \
             patch("dawn_process.encoder") as mock_enc:
            dawn_process._run_encode_locked_all(cfg["stations"], DATE_STR, cfg)
            assert mock_dl.process_night.call_count == 2
            assert mock_enc.process_night_locked_only.call_count == 2
            for c in mock_enc.process_night_locked_only.call_args_list:
                assert c[0][2].get('dawn_encode_parallelism') == 1

    def test_upload_all_calls_both_stations(self, cfg):
        import dawn_process
        with patch("dawn_process.archive_upload") as mock_au:
            dawn_process._run_upload_all(cfg["stations"], DATE_STR, cfg)
            assert mock_au.run_night.call_count == 2
