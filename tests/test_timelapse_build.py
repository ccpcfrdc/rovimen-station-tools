"""Tests for timelapse_build.py — per-night timelapse and night-stack builder."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import numpy as np
import pytest
from PIL import Image

import timelapse_build
from timelapse_build import build, build_night_stack, build_timelapse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_webp(path: Path, *, width: int = 64, height: int = 48,
               color: tuple[int, int, int] = (100, 100, 100)) -> Path:
    """Create a small solid-color lossless WebP image."""
    img = Image.new('RGB', (width, height), color)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), format='WEBP', lossless=True)
    return path


def _base_cfg(tmp_path: Path, *, stacker_enabled: bool = True) -> dict:
    """Build a minimal config dict for timelapse_build.build()."""
    capture = tmp_path / 'color_capture'
    capture.mkdir(exist_ok=True)
    return {
        'videocapture_path': str(capture),
        'services': {
            'stacker': {'enabled': stacker_enabled},
        },
    }


# ---------------------------------------------------------------------------
# build_timelapse
# ---------------------------------------------------------------------------

class TestBuildTimelapse:

    def test_no_webps_returns_none(self, tmp_path):
        """Empty stacks dir returns None and logs a warning."""
        stacks = tmp_path / 'stacks'
        stacks.mkdir()
        out = tmp_path / 'out'
        result = build_timelapse('20260315', 'RO000H', stacks, out)
        assert result is None

    @patch('timelapse_build.subprocess.call', return_value=0)
    def test_creates_output_dir(self, mock_call, tmp_path):
        stacks = tmp_path / 'stacks'
        _make_webp(stacks / 'RO000H_20260315_210000_stack.webp')
        out = tmp_path / 'nonexistent_dir'
        assert not out.exists()
        build_timelapse('20260315', 'RO000H', stacks, out)
        assert out.exists()

    @patch('timelapse_build.subprocess.call', return_value=0)
    def test_output_filename_format(self, mock_call, tmp_path):
        stacks = tmp_path / 'stacks'
        _make_webp(stacks / 'RO000H_20260315_210000_stack.webp')
        out = tmp_path / 'out'
        result = build_timelapse('20260315', 'RO000H', stacks, out)
        assert result is not None
        assert result.name == 'RO000H_20260315_timelapse.mp4'
        assert result.parent == out

    @patch('timelapse_build.subprocess.call', return_value=0)
    def test_no_rotate(self, mock_call, tmp_path):
        """Without rotate, no vflip/hflip in ffmpeg command."""
        stacks = tmp_path / 'stacks'
        _make_webp(stacks / 'RO000H_20260315_210000_stack.webp')
        out = tmp_path / 'out'
        build_timelapse('20260315', 'RO000H', stacks, out, rotate=False)
        cmd = mock_call.call_args[0][0]
        assert '-vf' not in cmd

    @patch('timelapse_build.subprocess.call', return_value=0)
    def test_rotate_adds_vflip_hflip(self, mock_call, tmp_path):
        """rotate=True adds vflip,hflip filter."""
        stacks = tmp_path / 'stacks'
        _make_webp(stacks / 'RO000H_20260315_210000_stack.webp')
        out = tmp_path / 'out'
        build_timelapse('20260315', 'RO000H', stacks, out, rotate=True)
        cmd = mock_call.call_args[0][0]
        vf_idx = cmd.index('-vf')
        vf_value = cmd[vf_idx + 1]
        assert 'vflip' in vf_value
        assert 'hflip' in vf_value

    @patch('timelapse_build.subprocess.call', return_value=1)
    def test_ffmpeg_failure_returns_none(self, mock_call, tmp_path):
        """Non-zero ffmpeg exit code returns None."""
        stacks = tmp_path / 'stacks'
        _make_webp(stacks / 'RO000H_20260315_210000_stack.webp')
        out = tmp_path / 'out'
        result = build_timelapse('20260315', 'RO000H', stacks, out)
        assert result is None

    @patch('timelapse_build.subprocess.call', return_value=0)
    def test_ffmpeg_command_structure(self, mock_call, tmp_path):
        """Verify key ffmpeg arguments."""
        stacks = tmp_path / 'stacks'
        _make_webp(stacks / 'RO000H_20260315_210000_stack.webp')
        out = tmp_path / 'out'
        build_timelapse('20260315', 'RO000H', stacks, out)
        cmd = mock_call.call_args[0][0]
        assert cmd[0] == 'ffmpeg'
        assert '-framerate' in cmd
        assert '25' in cmd
        assert '-c:v' in cmd
        assert 'libx264' in cmd
        assert '-y' in cmd


# ---------------------------------------------------------------------------
# build_night_stack
# ---------------------------------------------------------------------------

class TestBuildNightStack:

    def test_filters_to_2000_0200_window(self, tmp_path):
        """Only thumbnails between 20:00 and 02:00 UTC are included."""
        thumbs = tmp_path / 'thumbs'
        # 21:30 -- inside window
        _make_webp(thumbs / 'RO000H_20260315_213000_stack.webp', color=(200, 0, 0))
        # 15:00 -- outside window
        _make_webp(thumbs / 'RO000H_20260315_150000_stack.webp', color=(0, 200, 0))
        # 01:30 -- inside window
        _make_webp(thumbs / 'RO000H_20260316_013000_stack.webp', color=(0, 0, 200))
        out = tmp_path / 'out'
        out.mkdir()
        build_night_stack('20260315', 'RO000H', thumbs, out)
        result = out / 'RO000H_20260315_night_stack.webp'
        assert result.exists()
        # The 15:00 image should not have contributed (green channel)
        arr = np.array(Image.open(result).convert('RGB'))
        # The max of the red channel (200 from 21:30 image) should be present
        assert arr[:, :, 0].max() == 200
        # The green channel max should be 0 (the 15:00 green image was excluded)
        assert arr[:, :, 1].max() == 0

    def test_no_thumbnails_in_window(self, tmp_path):
        """When no thumbnails fall in the dark-sky window, logs warning and returns."""
        thumbs = tmp_path / 'thumbs'
        _make_webp(thumbs / 'RO000H_20260315_120000_stack.webp')
        _make_webp(thumbs / 'RO000H_20260315_150000_stack.webp')
        out = tmp_path / 'out'
        out.mkdir()
        build_night_stack('20260315', 'RO000H', thumbs, out)
        result = out / 'RO000H_20260315_night_stack.webp'
        assert not result.exists()

    def test_no_thumbnails_at_all(self, tmp_path):
        """Empty thumbs dir logs warning, does not crash."""
        thumbs = tmp_path / 'thumbs'
        thumbs.mkdir()
        out = tmp_path / 'out'
        out.mkdir()
        build_night_stack('20260315', 'RO000H', thumbs, out)
        assert not (out / 'RO000H_20260315_night_stack.webp').exists()

    def test_per_pixel_maximum(self, tmp_path):
        """Night stack is the per-pixel maximum across frames."""
        thumbs = tmp_path / 'thumbs'
        # Frame 1: bright red in top-left
        img1 = Image.new('RGB', (4, 4), (0, 0, 0))
        img1.putpixel((0, 0), (255, 0, 0))
        p1 = thumbs / 'RO000H_20260315_210000_stack.webp'
        p1.parent.mkdir(parents=True, exist_ok=True)
        img1.save(str(p1), format='WEBP', lossless=True)

        # Frame 2: bright blue in bottom-right
        img2 = Image.new('RGB', (4, 4), (0, 0, 0))
        img2.putpixel((3, 3), (0, 0, 255))
        p2 = thumbs / 'RO000H_20260315_220000_stack.webp'
        img2.save(str(p2), format='WEBP', lossless=True)

        out = tmp_path / 'out'
        out.mkdir()
        build_night_stack('20260315', 'RO000H', thumbs, out)
        result = np.array(Image.open(out / 'RO000H_20260315_night_stack.webp').convert('RGB'))
        # Both bright pixels should be preserved
        assert result[0, 0, 0] == 255   # red at (0,0)
        assert result[3, 3, 2] == 255   # blue at (3,3)

    def test_output_is_webp(self, tmp_path):
        thumbs = tmp_path / 'thumbs'
        _make_webp(thumbs / 'RO000H_20260315_210000_stack.webp')
        out = tmp_path / 'out'
        out.mkdir()
        build_night_stack('20260315', 'RO000H', thumbs, out)
        result = out / 'RO000H_20260315_night_stack.webp'
        assert result.exists()
        assert result.suffix == '.webp'

    def test_handles_different_sizes(self, tmp_path):
        """Images of different sizes are resized to match the first."""
        thumbs = tmp_path / 'thumbs'
        # First image: 64x48
        _make_webp(thumbs / 'RO000H_20260315_210000_stack.webp',
                    width=64, height=48, color=(100, 0, 0))
        # Second image: 128x96 (different size)
        _make_webp(thumbs / 'RO000H_20260315_220000_stack.webp',
                    width=128, height=96, color=(0, 100, 0))
        out = tmp_path / 'out'
        out.mkdir()
        build_night_stack('20260315', 'RO000H', thumbs, out)
        result = Image.open(out / 'RO000H_20260315_night_stack.webp')
        # Output should match first image dimensions
        assert result.size == (64, 48)

    def test_boundary_2000(self, tmp_path):
        """20:00 (hhmm=2000) is inside the window (>= 2000)."""
        thumbs = tmp_path / 'thumbs'
        _make_webp(thumbs / 'RO000H_20260315_200000_stack.webp', color=(50, 50, 50))
        out = tmp_path / 'out'
        out.mkdir()
        build_night_stack('20260315', 'RO000H', thumbs, out)
        assert (out / 'RO000H_20260315_night_stack.webp').exists()

    def test_boundary_0200(self, tmp_path):
        """02:00 (hhmm=200) is inside the window (<= 200)."""
        thumbs = tmp_path / 'thumbs'
        _make_webp(thumbs / 'RO000H_20260316_020000_stack.webp', color=(50, 50, 50))
        out = tmp_path / 'out'
        out.mkdir()
        build_night_stack('20260315', 'RO000H', thumbs, out)
        assert (out / 'RO000H_20260315_night_stack.webp').exists()

    def test_boundary_0201_excluded(self, tmp_path):
        """02:01 (hhmm=201) is outside the window."""
        thumbs = tmp_path / 'thumbs'
        _make_webp(thumbs / 'RO000H_20260316_020100_stack.webp', color=(50, 50, 50))
        out = tmp_path / 'out'
        out.mkdir()
        build_night_stack('20260315', 'RO000H', thumbs, out)
        assert not (out / 'RO000H_20260315_night_stack.webp').exists()


# ---------------------------------------------------------------------------
# build (orchestrator)
# ---------------------------------------------------------------------------

class TestBuild:

    @patch('timelapse_build.flags_manager')
    def test_stacker_disabled_marks_done(self, mock_fm, tmp_path):
        """When stacker is disabled, marks timelapse_done and returns immediately."""
        cfg = _base_cfg(tmp_path, stacker_enabled=False)
        build('RO000H', '20260315', cfg)
        mock_fm.mark_timelapse_done.assert_called_once_with('RO000H', '20260315', cfg)

    @patch('timelapse_build.flags_manager')
    def test_no_stacks_dir_marks_done(self, mock_fm, tmp_path):
        """When stacks directory doesn't exist, marks done and returns."""
        cfg = _base_cfg(tmp_path)
        # Don't create the stacks dir
        build('RO000H', '20260315', cfg)
        mock_fm.mark_timelapse_done.assert_called_once_with('RO000H', '20260315', cfg)

    @patch('timelapse_build.flags_manager')
    def test_stacks_dir_empty_marks_done(self, mock_fm, tmp_path):
        """When stacks directory exists but has no WebP files, marks done."""
        cfg = _base_cfg(tmp_path)
        stacks = Path(cfg['videocapture_path']) / 'RO000H' / '20260315' / 'stacks'
        stacks.mkdir(parents=True)
        # Empty stacks dir — no webps
        build('RO000H', '20260315', cfg)
        mock_fm.mark_timelapse_done.assert_called_once()

    @patch('timelapse_build.build_night_stack')
    @patch('timelapse_build.build_timelapse')
    @patch('timelapse_build.flags_manager')
    def test_calls_build_timelapse_then_night_stack(self, mock_fm, mock_bt, mock_bns, tmp_path):
        """build() calls build_timelapse, then build_night_stack if thumbs exist."""
        cfg = _base_cfg(tmp_path)
        capture = Path(cfg['videocapture_path'])
        stacks = capture / 'RO000H' / '20260315' / 'stacks'
        thumbs = stacks / 'thumbs'
        stacks.mkdir(parents=True)
        thumbs.mkdir()
        _make_webp(stacks / 'RO000H_20260315_210000_stack.webp')
        mock_bt.return_value = stacks.parent / 'RO000H_20260315_timelapse.mp4'
        build('RO000H', '20260315', cfg)
        mock_bt.assert_called_once_with(
            '20260315', 'RO000H', stacks, stacks.parent,
        )
        mock_bns.assert_called_once_with(
            '20260315', 'RO000H', thumbs, stacks.parent,
        )

    @patch('timelapse_build.build_night_stack')
    @patch('timelapse_build.build_timelapse')
    @patch('timelapse_build.flags_manager')
    def test_marks_done_on_successful_timelapse(self, mock_fm, mock_bt, mock_bns, tmp_path):
        """When build_timelapse succeeds, marks timelapse_done."""
        cfg = _base_cfg(tmp_path)
        capture = Path(cfg['videocapture_path'])
        stacks = capture / 'RO000H' / '20260315' / 'stacks'
        stacks.mkdir(parents=True)
        _make_webp(stacks / 'RO000H_20260315_210000_stack.webp')
        mock_bt.return_value = Path('/some/timelapse.mp4')
        build('RO000H', '20260315', cfg)
        mock_fm.mark_timelapse_done.assert_called_once_with('RO000H', '20260315', cfg)

    @patch('timelapse_build.build_night_stack')
    @patch('timelapse_build.build_timelapse')
    @patch('timelapse_build.flags_manager')
    def test_no_mark_done_on_timelapse_failure(self, mock_fm, mock_bt, mock_bns, tmp_path):
        """When build_timelapse returns None, timelapse_done is not marked."""
        cfg = _base_cfg(tmp_path)
        capture = Path(cfg['videocapture_path'])
        stacks = capture / 'RO000H' / '20260315' / 'stacks'
        stacks.mkdir(parents=True)
        _make_webp(stacks / 'RO000H_20260315_210000_stack.webp')
        mock_bt.return_value = None  # failure
        build('RO000H', '20260315', cfg)
        mock_fm.mark_timelapse_done.assert_not_called()

    @patch('timelapse_build.build_night_stack')
    @patch('timelapse_build.build_timelapse')
    @patch('timelapse_build.flags_manager')
    def test_no_thumbs_dir_skips_night_stack(self, mock_fm, mock_bt, mock_bns, tmp_path):
        """When thumbs/ doesn't exist, build_night_stack is not called."""
        cfg = _base_cfg(tmp_path)
        capture = Path(cfg['videocapture_path'])
        stacks = capture / 'RO000H' / '20260315' / 'stacks'
        stacks.mkdir(parents=True)
        # No thumbs/ subdirectory
        _make_webp(stacks / 'RO000H_20260315_210000_stack.webp')
        mock_bt.return_value = Path('/some/timelapse.mp4')
        build('RO000H', '20260315', cfg)
        mock_bns.assert_not_called()

    @patch('timelapse_build.flags_manager')
    def test_videocapture_path_fallback(self, mock_fm, tmp_path):
        """Config fallback chain for capture path works."""
        cfg = {
            'color_video_path': str(tmp_path / 'cv'),
            'services': {'stacker': {'enabled': False}},
        }
        build('RO000H', '20260315', cfg)
        mock_fm.mark_timelapse_done.assert_called_once()
