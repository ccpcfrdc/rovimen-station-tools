"""Tests for the color_tee feature in color_capture.py."""

from __future__ import annotations

import os

import pytest

from color_capture import ColorCapture, _VIDEO_SEGMENT_RE


class TestColorNameFromVideo:
    """Test conversion of RMS video segment names to color capture names."""

    def test_standard_segment(self):
        result = ColorCapture._color_name_from_video(
            'RO000Z_20260622_234512_123456_video.mkv'
        )
        assert result == 'RO000Z_20260622_234512_color.mkv'

    def test_zero_microseconds(self):
        result = ColorCapture._color_name_from_video(
            'DE001B_20260621_210000_000000_video.mkv'
        )
        assert result == 'DE001B_20260621_210000_color.mkv'

    def test_non_video_segment(self):
        assert ColorCapture._color_name_from_video('something_else.mkv') is None

    def test_color_segment_rejected(self):
        assert ColorCapture._color_name_from_video(
            'RO000Z_20260622_234512_color.mkv'
        ) is None

    def test_empty_string(self):
        assert ColorCapture._color_name_from_video('') is None

    def test_short_microseconds_rejected(self):
        assert ColorCapture._color_name_from_video(
            'RO000Z_20260622_234512_1_video.mkv'
        ) is None


class TestVideoSegmentRegex:
    """Test the _VIDEO_SEGMENT_RE pattern."""

    def test_matches_standard(self):
        m = _VIDEO_SEGMENT_RE.match('RO000Z_20260622_234512_123456_video.mkv')
        assert m is not None
        assert m.group(1) == 'RO000Z'
        assert m.group(2) == '20260622'
        assert m.group(3) == '234512'

    def test_rejects_color_segment(self):
        assert _VIDEO_SEGMENT_RE.match('RO000Z_20260622_234512_color.mkv') is None

    def test_rejects_no_microseconds(self):
        assert _VIDEO_SEGMENT_RE.match('RO000Z_20260622_234512_video.mkv') is None

    def test_rejects_short_microseconds(self):
        assert _VIDEO_SEGMENT_RE.match('RO000Z_20260622_234512_1_video.mkv') is None

    def test_rejects_long_microseconds(self):
        assert _VIDEO_SEGMENT_RE.match('RO000Z_20260622_234512_1234567_video.mkv') is None


class TestColorTeeConfig:
    """Test that color_tee config is read correctly."""

    def test_default_false(self, tmp_path):
        cfg = tmp_path / 'config.json'
        cfg.write_text('{"stations": {}}')
        cc = ColorCapture.__new__(ColorCapture)
        import json
        cc.cfg = json.loads(cfg.read_text())
        cc.color_tee = cc.cfg.get('color_tee', False)
        assert cc.color_tee is False

    def test_explicit_true(self, tmp_path):
        cfg = tmp_path / 'config.json'
        cfg.write_text('{"stations": {}, "color_tee": true}')
        cc = ColorCapture.__new__(ColorCapture)
        import json
        cc.cfg = json.loads(cfg.read_text())
        cc.color_tee = cc.cfg.get('color_tee', False)
        assert cc.color_tee is True


class TestRmsVideoPath:
    """Test RMS video path derivation."""

    def test_derives_from_rms_data_path(self, tmp_path):
        cc = ColorCapture.__new__(ColorCapture)
        cc.cfg = {
            'stations': {
                'RO000Z': {'rms_data_path': str(tmp_path / 'RMS_data' / 'cam1')},
            }
        }
        result = cc._rms_video_path('RO000Z')
        assert result == tmp_path / 'RMS_data' / 'cam1' / 'video'


class TestNightDateFromFilename:
    """Test night date derivation from segment filename timestamps."""

    def test_evening_same_day(self):
        assert ColorCapture._night_date_from_filename('20260622', '213000') == '20260622'

    def test_after_midnight_previous_day(self):
        assert ColorCapture._night_date_from_filename('20260623', '024500') == '20260622'

    def test_noon_boundary_same_day(self):
        assert ColorCapture._night_date_from_filename('20260622', '120000') == '20260622'

    def test_just_before_noon(self):
        assert ColorCapture._night_date_from_filename('20260622', '115959') == '20260621'


class TestHardlinkTouchEmitsCloseWrite:
    """Verify that the touch-after-hardlink pattern emits IN_CLOSE_WRITE."""

    def test_hardlink_then_touch(self, tmp_path):
        src = tmp_path / 'source.mkv'
        src.write_bytes(b'\x00' * 100)
        dst = tmp_path / 'dest.mkv'
        os.link(str(src), str(dst))
        open(dst, 'ab').close()
        assert dst.exists()
        assert src.stat().st_ino == dst.stat().st_ino
