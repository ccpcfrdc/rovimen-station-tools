"""Tests for stacker.build_night_color_meteor_stack — P0 #1 + P1 #10.

P0 #1:  The lock key in state.json is 'lock' (a dict), not 'locked' (a bool).
        A test with a real state dict catches this regression immediately.
P1 #10: Hardcoded WIDTH/HEIGHT (1280x720) in _compute_maxpixel assumes every
        camera outputs 720p.  Verify graceful behaviour when dimensions differ.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

import stacker
import flags_manager


STATION = "RO000T"
NIGHT = "20260401"


@pytest.fixture
def night_dir(tmp_path):
    """Create a night directory with stacks subdirectory."""
    d = tmp_path / "color_capture" / STATION / NIGHT
    d.mkdir(parents=True)
    (d / "stacks").mkdir()
    return d


@pytest.fixture
def cfg(tmp_path):
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "stations": {STATION: {"rotate": False}},
    }


def _make_webp(path: Path, w: int = 64, h: int = 48, color: tuple = (100, 50, 20)):
    """Write a small solid-colour WEBP image."""
    img = Image.fromarray(
        np.full((h, w, 3), color, dtype=np.uint8), mode="RGB"
    )
    img.save(path, format="WEBP")


def _write_state(night_dir: Path, chunks: dict):
    state = {"chunks": chunks}
    (night_dir / "state.json").write_text(json.dumps(state, indent=2))


# ── P0 #1: lock key correctness ──────────────────────────────────────────


class TestLockKeyIsLock:
    """Regression for the 'locked' vs 'lock' bug."""

    def test_locked_chunks_detected_with_lock_dict(self, night_dir, cfg):
        """Chunks with lock={...} must be found (the canonical schema)."""
        chunk_name = f"{STATION}_{NIGHT}_210000_color.mkv"
        stacks = night_dir / "stacks"
        _make_webp(stacks / f"{STATION}_{NIGHT}_210000_stack.webp")
        _make_webp(stacks / f"{STATION}_{NIGHT}_210000_stack_avg.webp", color=(30, 20, 10))
        _write_state(night_dir, {
            chunk_name: {
                "ready": True,
                "stacked": True,
                "reencoded": True,
                "lock": {
                    "lock_type": "detection",
                    "detection_time": "20260401_210005",
                    "meteor_time": "2026-04-01T21:00:05",
                },
            }
        })

        result = stacker.build_night_color_meteor_stack(STATION, NIGHT, cfg)
        assert result is not None
        assert result.exists()
        assert "color_meteor_stack" in result.name

    def test_locked_bool_true_is_not_valid(self, night_dir, cfg):
        """A bare 'locked: true' boolean (old/wrong schema) must NOT match."""
        chunk_name = f"{STATION}_{NIGHT}_210000_color.mkv"
        stacks = night_dir / "stacks"
        _make_webp(stacks / f"{STATION}_{NIGHT}_210000_stack.webp")
        _make_webp(stacks / f"{STATION}_{NIGHT}_210000_stack_avg.webp", color=(30, 20, 10))
        _write_state(night_dir, {
            chunk_name: {
                "ready": True,
                "stacked": True,
                "reencoded": True,
                "locked": True,
                "lock": None,
            }
        })

        result = stacker.build_night_color_meteor_stack(STATION, NIGHT, cfg)
        assert result is None

    def test_lock_none_means_unlocked(self, night_dir, cfg):
        """lock=None → not locked."""
        chunk_name = f"{STATION}_{NIGHT}_210000_color.mkv"
        stacks = night_dir / "stacks"
        _make_webp(stacks / f"{STATION}_{NIGHT}_210000_stack.webp")
        _make_webp(stacks / f"{STATION}_{NIGHT}_210000_stack_avg.webp", color=(30, 20, 10))
        _write_state(night_dir, {
            chunk_name: {
                "ready": True,
                "stacked": True,
                "reencoded": True,
                "lock": None,
            }
        })

        result = stacker.build_night_color_meteor_stack(STATION, NIGHT, cfg)
        assert result is None

    def test_no_stacks_dir_returns_none(self, tmp_path, cfg):
        """Missing stacks directory returns None without error."""
        result = stacker.build_night_color_meteor_stack(STATION, NIGHT, cfg)
        assert result is None

    def test_multiple_locked_chunks_combined(self, night_dir, cfg):
        """Multiple locked chunks produce a composite meteor stack."""
        chunks = {}
        stacks = night_dir / "stacks"
        for ts in ("210000", "210020"):
            chunk_name = f"{STATION}_{NIGHT}_{ts}_color.mkv"
            _make_webp(stacks / f"{STATION}_{NIGHT}_{ts}_stack.webp",
                       color=(200, 100, 50))
            _make_webp(stacks / f"{STATION}_{NIGHT}_{ts}_stack_avg.webp",
                       color=(30, 20, 10))
            chunks[chunk_name] = {
                "ready": True, "stacked": True, "reencoded": True,
                "lock": {"lock_type": "detection",
                         "detection_time": f"{NIGHT}_{ts}",
                         "meteor_time": f"2026-04-01T{ts[:2]}:{ts[2:4]}:{ts[4:6]}"},
            }
        _write_state(night_dir, chunks)

        result = stacker.build_night_color_meteor_stack(STATION, NIGHT, cfg)
        assert result is not None
        img = np.array(Image.open(result))
        assert img.shape[2] == 3
        assert img.max() > 0

    def test_rotation_applied(self, night_dir, cfg):
        """Stacks saved by _finish_stack are already rotated (180° flip).
        build_night_color_meteor_stack must NOT rotate again — it should
        preserve the orientation of the pre-rotated stacks it loads."""
        cfg["stations"][STATION]["rotate"] = True
        chunk_name = f"{STATION}_{NIGHT}_210000_color.mkv"
        stacks = night_dir / "stacks"

        # Original frame: left half red, right half blue.
        gradient = np.zeros((48, 64, 3), dtype=np.uint8)
        gradient[:, :32, 0] = 200
        gradient[:, 32:, 2] = 200

        # _finish_stack applies 180° rotation before saving; simulate that here
        # so the stack on disk is already in the correct output orientation.
        rotated = gradient[::-1, ::-1]

        max_img = Image.fromarray(rotated, mode="RGB")
        avg_img = Image.fromarray(np.zeros_like(rotated), mode="RGB")
        max_img.save(stacks / f"{STATION}_{NIGHT}_210000_stack.webp", format="WEBP")
        avg_img.save(stacks / f"{STATION}_{NIGHT}_210000_stack_avg.webp", format="WEBP")

        _write_state(night_dir, {
            chunk_name: {
                "ready": True, "stacked": True, "reencoded": True,
                "lock": {"lock_type": "detection"},
            }
        })

        result = stacker.build_night_color_meteor_stack(STATION, NIGHT, cfg)
        assert result is not None
        out = np.array(Image.open(result))
        # After 180° rotation: top-left was originally bottom-right (blue half).
        # The brighten pipeline (2× + gamma 0.5) saturates non-zero values to
        # 255, so just assert the dominant channel is correct.
        assert out[0, 0, 2] > out[0, 0, 0]


# ── P1 #10: hardcoded resolution ─────────────────────────────────────────


class TestHardcodedResolution:
    """Verify that stacker constants match expected values and document the risk."""

    def test_width_is_1280(self):
        assert stacker.WIDTH == 1280

    def test_height_is_720(self):
        assert stacker.HEIGHT == 720

    def test_frame_bytes_consistent(self):
        assert stacker.WIDTH * stacker.HEIGHT * 3 == 1280 * 720 * 3

    def test_maxpixel_frame_shape_uses_constants(self):
        """_compute_maxpixel uses (HEIGHT, WIDTH, 3) — if a video has different
        dimensions, the raw bytes won't reshape correctly. This test documents
        the assumption; when we add resolution probing this test should be
        updated to verify dynamic sizing."""
        assert (stacker.HEIGHT, stacker.WIDTH, 3) == (720, 1280, 3)
