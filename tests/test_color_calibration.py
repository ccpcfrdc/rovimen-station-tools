"""Tests for color_calibration.py -- adaptive white balance and color grading.

All functions under test are pure numpy math; no filesystem or subprocess needed.
"""

from __future__ import annotations

import numpy as np
import pytest

from color_calibration import (
    FLEET_CONTRAST,
    FLEET_DEFAULT_COLOR_POST,
    FLEET_GAMMA,
    FLEET_HIGHLIGHT_KNEE_HIGH,
    FLEET_HIGHLIGHT_KNEE_LOW,
    FLEET_TARGET_RGB,
    _apply_gains_highlight_protect,
    apply_calibration_np,
    build_ffmpeg_filter,
    derive_adaptive_gains,
    resolve_calibration_from_frame,
    resolve_color_post,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uniform_image(r: int, g: int, b: int, h: int = 64, w: int = 64) -> np.ndarray:
    """Create an HxWx3 uint8 image with uniform color."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :, 0] = r
    img[:, :, 1] = g
    img[:, :, 2] = b
    return img


def _gradient_image(h: int = 64, w: int = 64) -> np.ndarray:
    """Create an image with a horizontal luma gradient from 0 to 255."""
    ramp = np.linspace(0, 255, w, dtype=np.uint8)
    row = np.stack([ramp, ramp, ramp], axis=-1)
    return np.tile(row[np.newaxis, :, :], (h, 1, 1))


# ---------------------------------------------------------------------------
# derive_adaptive_gains
# ---------------------------------------------------------------------------

class TestDeriveAdaptiveGains:

    def test_uniform_gray_gives_equal_gains(self):
        """Uniform gray image with gray-world target -> gains near (1, 1, 1)."""
        img = _uniform_image(128, 128, 128)
        gr, gg, gb = derive_adaptive_gains(img, target_rgb=(1.0, 1.0, 1.0))
        assert gr == pytest.approx(1.0, abs=0.01)
        assert gg == pytest.approx(1.0, abs=0.01)
        assert gb == pytest.approx(1.0, abs=0.01)

    def test_red_biased_image_red_gain_lowest(self):
        """Red-heavy image: red gain should be lowest (attenuate red)."""
        img = _uniform_image(200, 100, 100)
        gr, gg, gb = derive_adaptive_gains(img, target_rgb=(1.0, 1.0, 1.0))
        assert gr < gg
        assert gr < gb

    def test_blue_biased_image_blue_gain_lowest(self):
        """Blue-heavy image: blue gain should be lowest (attenuate blue)."""
        img = _uniform_image(100, 100, 200)
        gr, gg, gb = derive_adaptive_gains(img, target_rgb=(1.0, 1.0, 1.0))
        assert gb < gr
        assert gb < gg

    def test_gain_ceiling_one_max_gain_is_one(self):
        """With gain_ceiling=1.0 the largest gain should be exactly 1.0."""
        img = _uniform_image(200, 100, 150)
        gr, gg, gb = derive_adaptive_gains(img, gain_ceiling=1.0)
        assert max(gr, gg, gb) == pytest.approx(1.0, abs=1e-6)

    def test_gain_ceiling_two_scales_up(self):
        """gain_ceiling=2.0 scales all gains up; max should be 2.0."""
        img = _uniform_image(200, 100, 150)
        gr, gg, gb = derive_adaptive_gains(img, gain_ceiling=2.0)
        assert max(gr, gg, gb) == pytest.approx(2.0, abs=1e-6)

    def test_gain_ceiling_ratio_preserved(self):
        """Different ceilings should produce the same ratio between gains."""
        img = _uniform_image(200, 100, 150)
        g1 = derive_adaptive_gains(img, gain_ceiling=1.0)
        g2 = derive_adaptive_gains(img, gain_ceiling=2.0)
        ratio1 = g1[0] / g1[1] if g1[1] else 0
        ratio2 = g2[0] / g2[1] if g2[1] else 0
        assert ratio1 == pytest.approx(ratio2, abs=1e-4)

    def test_all_zeros_no_crash(self):
        """All-black image should not crash (division by zero guard)."""
        img = _uniform_image(0, 0, 0)
        gr, gg, gb = derive_adaptive_gains(img)
        # Gains should be finite (the fallback clips or produces 1.0s)
        assert np.isfinite(gr)
        assert np.isfinite(gg)
        assert np.isfinite(gb)

    def test_wrong_shape_raises(self):
        """Non-HxWx3 input should raise ValueError."""
        img_2d = np.zeros((64, 64), dtype=np.uint8)
        with pytest.raises(ValueError, match='HxWx3'):
            derive_adaptive_gains(img_2d)

    def test_wrong_shape_4_channels_raises(self):
        """RGBA (4-channel) input should raise ValueError."""
        img_rgba = np.zeros((64, 64, 4), dtype=np.uint8)
        with pytest.raises(ValueError, match='HxWx3'):
            derive_adaptive_gains(img_rgba)

    def test_percentile_params_affect_gains(self):
        """Different percentile ranges produce different gains when the
        color composition varies across luma bands."""
        # Build an image where brighter rows are redder and darker rows are
        # bluer. Luma varies across rows so different percentile windows
        # select different subsets with different R/G/B means.
        h, w = 64, 64
        img = np.zeros((h, w, 3), dtype=np.uint8)
        for row in range(h):
            frac = row / (h - 1)          # 0 at top -> 1 at bottom
            luma = int(30 + 200 * frac)   # luma rises top-to-bottom
            # Red rises faster than green/blue -> bright rows are red-biased
            img[row, :, 0] = min(255, int(luma * 1.3))
            img[row, :, 1] = luma
            img[row, :, 2] = max(0, int(luma * 0.7))
        g_narrow = derive_adaptive_gains(img, percentile_low=40.0, percentile_high=60.0)
        g_wide = derive_adaptive_gains(img, percentile_low=5.0, percentile_high=95.0)
        # Narrow window selects mid-luma rows; wide window includes the
        # extremes, shifting the mean channel ratios and thus the gains.
        assert g_narrow != g_wide

    def test_custom_target_rgb(self):
        """Supplying a non-gray target should bias gains accordingly."""
        img = _uniform_image(128, 128, 128)
        # Target with higher blue -> blue gain should be largest
        gr, gg, gb = derive_adaptive_gains(img, target_rgb=(1.0, 1.0, 2.0))
        assert gb > gr
        assert gb > gg


# ---------------------------------------------------------------------------
# resolve_color_post
# ---------------------------------------------------------------------------

class TestResolveColorPost:

    def test_empty_config_returns_fleet_defaults(self):
        """Empty station_cfg returns fleet defaults unchanged."""
        result = resolve_color_post({})
        assert result == FLEET_DEFAULT_COLOR_POST

    def test_none_color_post_returns_fleet_defaults(self):
        """Explicit color_post=None returns fleet defaults."""
        result = resolve_color_post({'color_post': None})
        assert result == FLEET_DEFAULT_COLOR_POST

    def test_gamma_override_merged(self):
        """Station override for gamma is merged, others stay default."""
        result = resolve_color_post({'color_post': {'gamma': 0.9}})
        assert result['gamma'] == 0.9
        assert result['target_r'] == FLEET_TARGET_RGB[0]
        assert result['target_g'] == FLEET_TARGET_RGB[1]
        assert result['target_b'] == FLEET_TARGET_RGB[2]
        assert result['contrast'] == FLEET_CONTRAST

    def test_target_r_override(self):
        """Station override for target_r is merged, others default."""
        result = resolve_color_post({'color_post': {'target_r': 1.5}})
        assert result['target_r'] == 1.5
        assert result['target_g'] == FLEET_TARGET_RGB[1]
        assert result['gamma'] == FLEET_GAMMA

    def test_multiple_overrides(self):
        """Multiple fields overridden at once."""
        result = resolve_color_post({
            'color_post': {'gamma': 0.5, 'contrast': 1.2, 'knee_low': 0.5}
        })
        assert result['gamma'] == 0.5
        assert result['contrast'] == 1.2
        assert result['knee_low'] == 0.5
        assert result['target_r'] == FLEET_TARGET_RGB[0]


# ---------------------------------------------------------------------------
# resolve_calibration_from_frame
# ---------------------------------------------------------------------------

class TestResolveCalibratonFromFrame:

    def test_returns_four_floats(self):
        """Should return (gr, gg, gb, gamma)."""
        img = _uniform_image(128, 128, 128)
        result = resolve_calibration_from_frame(img, {})
        assert len(result) == 4
        assert all(isinstance(v, float) for v in result)

    def test_gamma_from_fleet_default(self):
        """Without station override, gamma comes from fleet default."""
        img = _uniform_image(128, 128, 128)
        _, _, _, gamma = resolve_calibration_from_frame(img, {})
        assert gamma == pytest.approx(FLEET_GAMMA)

    def test_gamma_from_station_override(self):
        """Station color_post gamma overrides fleet default."""
        img = _uniform_image(128, 128, 128)
        cfg = {'color_post': {'gamma': 0.9}}
        _, _, _, gamma = resolve_calibration_from_frame(img, cfg)
        assert gamma == pytest.approx(0.9)

    def test_target_override_affects_gains(self):
        """Custom target_r in station config shifts gain ratios."""
        # Use a non-uniform color so target shift changes the gain ratios
        img = _uniform_image(200, 100, 100)
        gr_def, gg_def, _, _ = resolve_calibration_from_frame(img, {})
        gr_high, gg_high, _, _ = resolve_calibration_from_frame(
            img, {'color_post': {'target_r': 2.0}},
        )
        # With higher target_r, the red-to-green gain ratio should increase
        ratio_default = gr_def / gg_def
        ratio_high = gr_high / gg_high
        assert ratio_high > ratio_default


# ---------------------------------------------------------------------------
# _apply_gains_highlight_protect
# ---------------------------------------------------------------------------

class TestApplyGainsHighlightProtect:

    def test_dark_pixels_get_full_gain(self):
        """Pixels with luma well below knee_low get the full gain applied."""
        img = _uniform_image(50, 50, 50)  # luma ~ 0.196
        # Apply a 2x red gain
        out = _apply_gains_highlight_protect(img, 2.0, 1.0, 1.0, knee_low=0.65, knee_high=0.95)
        # Red channel should be approximately doubled
        assert out[0, 0, 0] == pytest.approx(100, abs=2)
        # Green and blue unchanged
        assert out[0, 0, 1] == pytest.approx(50, abs=1)
        assert out[0, 0, 2] == pytest.approx(50, abs=1)

    def test_bright_pixels_no_gain_adjustment(self):
        """Pixels above knee_high get effectively no gain adjustment (gain -> 1.0)."""
        img = _uniform_image(250, 250, 250)  # luma ~ 0.98
        out = _apply_gains_highlight_protect(img, 2.0, 0.5, 0.5, knee_low=0.65, knee_high=0.95)
        # Gains should have faded to near unity; output should be close to input
        assert out[0, 0, 0] == pytest.approx(250, abs=5)
        assert out[0, 0, 1] == pytest.approx(250, abs=5)
        assert out[0, 0, 2] == pytest.approx(250, abs=5)

    def test_mid_range_interpolated(self):
        """Pixels between knee_low and knee_high get partially attenuated gains."""
        # Choose a pixel whose luma falls right in the middle of the knee range
        # knee_low=0.4, knee_high=0.8, midpoint luma ~ 0.6
        val = int(0.6 * 255)  # ~153
        img = _uniform_image(val, val, val)
        out_full_knee = _apply_gains_highlight_protect(
            img, 2.0, 1.0, 1.0, knee_low=0.4, knee_high=0.8,
        )
        # Red should be boosted but NOT fully doubled (smoothstep partial)
        red_out = int(out_full_knee[0, 0, 0])
        assert val < red_out < min(val * 2, 255)

    def test_unity_gains_identity(self):
        """Gains of (1, 1, 1) should return the image unchanged."""
        img = _uniform_image(120, 80, 200)
        out = _apply_gains_highlight_protect(img, 1.0, 1.0, 1.0)
        np.testing.assert_array_equal(out, img)

    def test_output_dtype_uint8(self):
        """Output should always be uint8 regardless of gain values."""
        img = _uniform_image(200, 200, 200)
        out = _apply_gains_highlight_protect(img, 3.0, 3.0, 3.0)
        assert out.dtype == np.uint8

    def test_output_clipped_to_255(self):
        """Even with extreme gains, output should not exceed 255."""
        img = _uniform_image(200, 200, 200)
        out = _apply_gains_highlight_protect(img, 5.0, 5.0, 5.0, knee_low=0.99, knee_high=1.0)
        assert out.max() <= 255


# ---------------------------------------------------------------------------
# apply_calibration_np
# ---------------------------------------------------------------------------

class TestApplyCalibrationNp:

    def test_identity_returns_original(self):
        """Identity gains + gamma=1 + contrast=1 returns the original image."""
        img = _uniform_image(100, 150, 200)
        out = apply_calibration_np(img, 1.0, 1.0, 1.0, gamma=1.0, contrast=1.0)
        np.testing.assert_array_equal(out, img)

    def test_gamma_below_one_lifts_midtones(self):
        """Gamma < 1 should lift midtone pixel values (make them brighter)."""
        img = _uniform_image(128, 128, 128)
        out = apply_calibration_np(img, 1.0, 1.0, 1.0, gamma=0.5, contrast=1.0)
        # (128/255)^0.5 * 255 ~ 181
        assert out[0, 0, 0] > 128

    def test_gamma_above_one_darkens_midtones(self):
        """Gamma > 1 should darken midtone pixel values."""
        img = _uniform_image(128, 128, 128)
        out = apply_calibration_np(img, 1.0, 1.0, 1.0, gamma=2.0, contrast=1.0)
        # (128/255)^2.0 * 255 ~ 64
        assert out[0, 0, 0] < 128

    def test_rotation_180(self):
        """rotate=True should rotate the image 180 degrees."""
        img = np.zeros((4, 4, 3), dtype=np.uint8)
        img[0, 0] = [255, 0, 0]  # red pixel at top-left
        out = apply_calibration_np(img, 1.0, 1.0, 1.0, gamma=1.0, rotate=True, contrast=1.0)
        # After 180 rotation, top-left goes to bottom-right
        np.testing.assert_array_equal(out[3, 3], [255, 0, 0])
        # Original position should now be black
        np.testing.assert_array_equal(out[0, 0], [0, 0, 0])

    def test_rotation_preserves_shape(self):
        """180 rotation preserves image dimensions."""
        img = _uniform_image(100, 100, 100, h=32, w=64)
        out = apply_calibration_np(img, 1.0, 1.0, 1.0, gamma=1.0, rotate=True, contrast=1.0)
        assert out.shape == img.shape

    def test_contrast_above_one_increases_spread(self):
        """Contrast > 1 should push pixels away from 0.5 midpoint."""
        # Create an image at exactly midpoint
        img = _uniform_image(128, 128, 128)
        out = apply_calibration_np(img, 1.0, 1.0, 1.0, gamma=1.0, contrast=1.5)
        # 128/255 ~ 0.502; after contrast: (0.502-0.5)*1.5+0.5 = 0.503 -> ~128
        # For a value near 0.5, contrast has minimal effect; test with off-center
        img_bright = _uniform_image(200, 200, 200)
        out_bright = apply_calibration_np(img_bright, 1.0, 1.0, 1.0, gamma=1.0, contrast=1.5)
        # 200/255 ~ 0.784; (0.784-0.5)*1.5+0.5 = 0.926 -> ~236
        assert out_bright[0, 0, 0] > 200

    def test_contrast_below_one_decreases_spread(self):
        """Contrast < 1 should pull pixels toward 0.5 midpoint."""
        img = _uniform_image(200, 200, 200)
        out = apply_calibration_np(img, 1.0, 1.0, 1.0, gamma=1.0, contrast=0.5)
        # (200/255-0.5)*0.5+0.5 = 0.642 -> ~164
        assert out[0, 0, 0] < 200

    def test_output_uint8_clipped(self):
        """Output should always be uint8 in [0, 255]."""
        img = _uniform_image(200, 200, 200)
        out = apply_calibration_np(img, 2.0, 2.0, 2.0, gamma=0.5, contrast=2.0,
                                   knee_low=0.99, knee_high=1.0)
        assert out.dtype == np.uint8
        assert out.max() <= 255
        assert out.min() >= 0

    def test_highlight_protection_bright_vs_dark(self):
        """Bright pixels should get less gain adjustment than dark pixels."""
        dark = _uniform_image(30, 30, 30)
        bright = _uniform_image(240, 240, 240)
        gain = 2.0

        out_dark = apply_calibration_np(dark, gain, 1.0, 1.0, gamma=1.0, contrast=1.0)
        out_bright = apply_calibration_np(bright, gain, 1.0, 1.0, gamma=1.0, contrast=1.0)

        # Red channel ratio: dark pixels should be more affected
        dark_ratio = out_dark[0, 0, 0] / max(dark[0, 0, 0], 1)
        bright_ratio = out_bright[0, 0, 0] / max(bright[0, 0, 0], 1)
        assert dark_ratio > bright_ratio

    def test_no_rotation_by_default(self):
        """Default rotate=False should not rotate."""
        img = np.zeros((4, 4, 3), dtype=np.uint8)
        img[0, 0] = [255, 0, 0]
        out = apply_calibration_np(img, 1.0, 1.0, 1.0, gamma=1.0, contrast=1.0)
        np.testing.assert_array_equal(out[0, 0], [255, 0, 0])


# ---------------------------------------------------------------------------
# build_ffmpeg_filter
# ---------------------------------------------------------------------------

class TestBuildFfmpegFilter:

    def test_always_has_colorchannelmixer(self):
        """Filter always starts with colorchannelmixer."""
        filt = build_ffmpeg_filter(1.0, 1.0, 1.0, 1.0)
        assert filt.startswith('colorchannelmixer=')

    def test_non_unity_gains_in_mixer(self):
        """Non-unity gains appear in the colorchannelmixer values."""
        filt = build_ffmpeg_filter(1.2, 0.9, 1.1, 1.0)
        assert 'rr=1.2000' in filt
        assert 'gg=0.9000' in filt
        assert 'bb=1.1000' in filt

    def test_non_unity_gamma_produces_eq(self):
        """Non-unity gamma adds an eq=gamma= filter with inverted value."""
        filt = build_ffmpeg_filter(1.0, 1.0, 1.0, 0.75)
        assert 'eq=' in filt
        # 1/0.75 = 1.3333
        assert 'gamma=1.3333' in filt

    def test_identity_gamma_no_eq(self):
        """Identity gamma (1.0) should NOT produce eq= filter."""
        filt = build_ffmpeg_filter(1.0, 1.0, 1.0, 1.0)
        assert 'eq=' not in filt

    def test_rotation_appends_vflip_hflip(self):
        """rotate=True appends vflip,hflip."""
        filt = build_ffmpeg_filter(1.0, 1.0, 1.0, 1.0, rotate=True)
        assert 'vflip,hflip' in filt

    def test_no_rotation_by_default(self):
        """rotate=False (default) should not include vflip/hflip."""
        filt = build_ffmpeg_filter(1.0, 1.0, 1.0, 1.0)
        assert 'vflip' not in filt
        assert 'hflip' not in filt

    def test_contrast_appends_to_eq(self):
        """Non-unity contrast adds contrast= to eq filter."""
        filt = build_ffmpeg_filter(1.0, 1.0, 1.0, 1.0, contrast=1.15)
        assert 'eq=' in filt
        assert 'contrast=1.1500' in filt

    def test_gamma_and_contrast_combined_in_single_eq(self):
        """Both gamma and contrast should appear in a single eq= filter."""
        filt = build_ffmpeg_filter(1.0, 1.0, 1.0, 0.75, contrast=1.1)
        # There should be exactly one 'eq=' occurrence
        assert filt.count('eq=') == 1
        assert 'gamma=' in filt
        assert 'contrast=' in filt

    def test_identity_contrast_no_contrast_in_eq(self):
        """Identity contrast (1.0) should not include contrast in eq."""
        filt = build_ffmpeg_filter(1.0, 1.0, 1.0, 0.75, contrast=1.0)
        assert 'contrast=' not in filt

    def test_full_chain_order(self):
        """Full chain: colorchannelmixer, then eq, then vflip,hflip."""
        filt = build_ffmpeg_filter(1.2, 0.9, 1.1, 0.75, rotate=True, contrast=1.1)
        parts = filt.split(',')
        # First part is colorchannelmixer
        assert parts[0].startswith('colorchannelmixer=')
        # eq= should come before vflip
        eq_idx = next(i for i, p in enumerate(parts) if p.startswith('eq='))
        vflip_idx = next(i for i, p in enumerate(parts) if p == 'vflip')
        assert eq_idx < vflip_idx

    def test_filter_is_valid_string(self):
        """Filter string should not contain newlines or empty segments."""
        filt = build_ffmpeg_filter(1.5, 0.8, 1.2, 0.75, rotate=True, contrast=1.05)
        assert '\n' not in filt
        assert ',,' not in filt
        # Every comma-separated segment should be non-empty
        for part in filt.split(','):
            assert len(part.strip()) > 0
