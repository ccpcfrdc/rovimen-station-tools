"""Station-side color calibration.

Per-chunk adaptive white balance targeting a universal aesthetic night-sky
colour. Rationale: the raw IR-cut-removed sensor sees strong magenta bias;
pure gray-world correction drives the sky to neutral grey, which looks dull
and unnatural. Targeting a mildly blue-magenta sky colour matches what a
clean-IR sensor would see and matches what the human eye expects at night.

Per-camera differences (sensor variant, scene, light pollution) are absorbed
by the adaptive step: each chunk's own sky pixels drive the gains, so the
same target produces a consistent look across the fleet.

``color_post`` in ``station_configs/<host>/config.json`` can override the
universal target + gamma on a per-camera basis (rare — IMX662 cameras on
mixed fleets, or heavy-light-pollution outliers).

Usage (numpy)::

    gr, gg, gb, gamma = resolve_calibration_from_frame(reference, station_cfg)
    out = apply_calibration_np(image, gr, gg, gb, gamma, rotate=False)

Usage (ffmpeg)::

    gr, gg, gb, gamma = resolve_calibration_from_mkv(mkv, station_cfg)
    vf = build_ffmpeg_filter(gr, gg, gb, gamma, rotate=False)
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


# Universal target for the calibrated night sky, as an R:G:B ratio with G
# normalised to 1.0. (1, 1, 1) is true gray-world: the adaptive step pulls
# each camera's mid-luma sky pixels to neutral grey, regardless of how much
# IR cast the sensor has. The previous (1.44, 1.00, 1.75) target was the raw
# IR-contaminated sky ratio itself, which caused the adaptive gains to be
# near-identity and let the magenta cast survive into maxpixel stacks.
FLEET_TARGET_RGB: tuple[float, float, float] = (1.00, 1.00, 1.00)

# Universal tone-curve gamma applied after gains. <1 = midtone lift.
FLEET_GAMMA: float = 0.75

# Highlight-protect knee — smoothstep fade of per-channel gains toward unity
# between knee_low and knee_high luma. Lowering the knee preserves more of
# the bright meteor trail / star core; raising it lets the gain bite harder
# into the highlights (useful when a station's WB drift contaminates them).
FLEET_HIGHLIGHT_KNEE_LOW:  float = 0.65
FLEET_HIGHLIGHT_KNEE_HIGH: float = 0.95

# Linear contrast multiplier applied after gamma. 1.0 = identity. 1.10 lifts
# contrast by 10 % around 0.5-luma pivot. Stations with a bright sky floor
# benefit from a small bump (~1.05–1.10).
FLEET_CONTRAST: float = 1.0

# Default ``color_post`` when a camera doesn't override anything. Identity-ish
# wrt the adaptive base — the actual aesthetic is baked into FLEET_TARGET_RGB,
# not into multipliers.
FLEET_DEFAULT_COLOR_POST: dict[str, float] = {
    'target_r':   FLEET_TARGET_RGB[0],
    'target_g':   FLEET_TARGET_RGB[1],
    'target_b':   FLEET_TARGET_RGB[2],
    'gamma':      FLEET_GAMMA,
    'contrast':   FLEET_CONTRAST,
    'knee_low':   FLEET_HIGHLIGHT_KNEE_LOW,
    'knee_high':  FLEET_HIGHLIGHT_KNEE_HIGH,
}


def derive_adaptive_gains(
    image: np.ndarray,
    target_rgb: tuple[float, float, float] = FLEET_TARGET_RGB,
    percentile_low: float = 10.0,
    percentile_high: float = 85.0,
    gain_ceiling: float = 1.0,
) -> tuple[float, float, float]:
    """Derive per-channel gains that drive the image's sky pixels toward
    ``target_rgb`` (an R:G:B ratio).

    Sky pixels are selected by excluding the darkest ``percentile_low``%
    (noise floor) and the brightest ``100 - percentile_high``% (stars, trails,
    horizon lights). The mean of the remaining pixels per channel is mapped
    onto the target ratio.

    With ``gain_ceiling=1.0`` (default) the final gains are rescaled so the
    largest equals 1.0 — attenuation only, no headroom overflow.
    """
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f'expected HxWx3 RGB, got shape {image.shape}')

    luma = image.astype(np.float32).mean(axis=2)
    lo = np.percentile(luma, percentile_low)
    hi = np.percentile(luma, percentile_high)
    mask = (luma >= lo) & (luma <= hi)
    pixels = image[mask].astype(np.float32)
    if pixels.size == 0:
        pixels = image.reshape(-1, 3).astype(np.float32)

    mean_r = float(pixels[:, 0].mean()) or 1.0
    mean_g = float(pixels[:, 1].mean()) or 1.0
    mean_b = float(pixels[:, 2].mean()) or 1.0

    tr, tg, tb = target_rgb
    gr, gg, gb = tr / mean_r, tg / mean_g, tb / mean_b

    # Target is a ratio, not a brightness; rescale so max gain == ceiling.
    # (Without this the absolute gains would follow target/mean which is tiny.)
    if gain_ceiling and gain_ceiling > 0:
        mx = max(gr, gg, gb)
        if mx > 0:
            s = gain_ceiling / mx
            gr, gg, gb = gr * s, gg * s, gb * s

    return gr, gg, gb


def resolve_color_post(station_cfg: dict) -> dict:
    """Return the camera's color_post merged over the fleet default, so the
    caller never has to check for absence."""
    return {**FLEET_DEFAULT_COLOR_POST, **(station_cfg.get('color_post') or {})}


def resolve_calibration_from_frame(
    reference: np.ndarray,
    station_cfg: dict,
) -> tuple[float, float, float, float]:
    """Derive adaptive gains for ``reference`` using the camera's target
    (falls back to fleet default), return ``(gain_r, gain_g, gain_b, gamma)``.
    Ready to hand to ``apply_calibration_np`` or ``build_ffmpeg_filter``.
    """
    post = resolve_color_post(station_cfg)
    target = (float(post['target_r']), float(post['target_g']), float(post['target_b']))
    gr, gg, gb = derive_adaptive_gains(reference, target_rgb=target)
    return gr, gg, gb, float(post['gamma'])


def _apply_gains_highlight_protect(
    image: np.ndarray,
    gr: float,
    gg: float,
    gb: float,
    knee_low: float = FLEET_HIGHLIGHT_KNEE_LOW,
    knee_high: float = FLEET_HIGHLIGHT_KNEE_HIGH,
) -> np.ndarray:
    """Apply per-channel gains with smoothstep fade toward unity in highlights
    so bright stars and meteor trails don't pick up a tint."""
    out = image.astype(np.float32)
    # Compute luma reference from a separate array — Python 3.14's JIT
    # miscompiles the in-place ``out *= factor`` below when ``img01`` is
    # derived from ``out`` (both alias the same base buffer to the JIT),
    # silently replacing ``out`` values with the 0–1 normalised scale.
    img01 = image.astype(np.float32) / 255.0
    luma = 0.2126 * img01[:, :, 0] + 0.7152 * img01[:, :, 1] + 0.0722 * img01[:, :, 2]
    t = np.clip((luma - knee_low) / max(knee_high - knee_low, 1e-6), 0.0, 1.0)
    w = 1.0 - (t * t * (3.0 - 2.0 * t))  # smoothstep fade-out
    out[:, :, 0] *= 1.0 + w * (gr - 1.0)
    out[:, :, 1] *= 1.0 + w * (gg - 1.0)
    out[:, :, 2] *= 1.0 + w * (gb - 1.0)
    return np.clip(out, 0, 255).astype(np.uint8)


def apply_calibration_np(
    image: np.ndarray,
    gr: float,
    gg: float,
    gb: float,
    gamma: float,
    rotate: bool = False,
    contrast: float = FLEET_CONTRAST,
    knee_low: float = FLEET_HIGHLIGHT_KNEE_LOW,
    knee_high: float = FLEET_HIGHLIGHT_KNEE_HIGH,
) -> np.ndarray:
    """Apply gains (with highlight-protect), gamma, optional contrast bump,
    and optional 180° rotation.

    Gamma uses the numpy convention ``(x/255)^gamma``: gamma < 1 lifts midtones.
    Contrast is a linear multiplier around the 0.5-luma pivot: 1.0 = identity,
    1.10 = +10 % contrast.
    """
    out = _apply_gains_highlight_protect(image, gr, gg, gb, knee_low, knee_high)
    if gamma and abs(gamma - 1.0) > 1e-3:
        f = (out.astype(np.float32) / 255.0) ** float(gamma)
        out = (np.clip(f, 0, 1) * 255).astype(np.uint8)
    if contrast and abs(contrast - 1.0) > 1e-3:
        f = out.astype(np.float32) / 255.0
        f = (f - 0.5) * float(contrast) + 0.5
        out = (np.clip(f, 0, 1) * 255).astype(np.uint8)
    if rotate:
        out = np.rot90(out, k=2)
    return out


def build_ffmpeg_filter(
    gr: float,
    gg: float,
    gb: float,
    gamma: float,
    rotate: bool = False,
    contrast: float = FLEET_CONTRAST,
) -> str:
    """Filter chain for ffmpeg: per-channel gain → gamma → contrast → optional
    180° flip.

    Note on gamma: ffmpeg's ``eq=gamma=G`` uses ``pow(val/255, 1/G) * 255``,
    the *inverse* of the numpy convention. We invert here so the semantics
    match ``apply_calibration_np``.

    ffmpeg ``eq=contrast=X`` already uses 1.0 = identity, so the value is
    passed through unchanged. Combining gamma and contrast into a single
    ``eq=`` filter (instead of chaining two) is cheaper.
    """
    parts = [f'colorchannelmixer=rr={gr:.4f}:gg={gg:.4f}:bb={gb:.4f}']
    eq_terms: list[str] = []
    if gamma and abs(gamma - 1.0) > 1e-3:
        eq_terms.append(f'gamma={1.0 / float(gamma):.4f}')
    if contrast and abs(contrast - 1.0) > 1e-3:
        eq_terms.append(f'contrast={float(contrast):.4f}')
    if eq_terms:
        parts.append('eq=' + ':'.join(eq_terms))
    if rotate:
        parts.append('vflip,hflip')  # 180°
    return ','.join(parts)


def sample_avg_frame(mkv: Path, n_samples: int = 5, full_range: bool = False) -> np.ndarray | None:
    """Decode up to ``n_samples`` evenly-spaced frames from an MKV and return
    their per-pixel mean. Used by encoders that don't stream all frames
    through numpy to build a sky reference for adaptive derivation.
    """
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=width,height,nb_read_frames,duration',
         '-count_frames', '-of', 'json', str(mkv)],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        logger.warning('ffprobe failed for %s', mkv)
        return None
    try:
        meta = json.loads(probe.stdout)['streams'][0]
    except (KeyError, IndexError, json.JSONDecodeError):
        return None
    w, h = int(meta['width']), int(meta['height'])
    nb = int(meta.get('nb_read_frames') or 0)
    if nb <= 0:
        try:
            nb = max(1, int(float(meta.get('duration') or 0) * 25))
        except ValueError:
            nb = 500

    stride = max(1, nb // max(1, n_samples))
    # Force full-range interpretation for cameras that mis-tag full-range data
    # as limited (color_range=tv) — otherwise the reference frame is crushed and
    # the derived gains are wrong. No-op for already-full (pc) streams.
    range_prefix = 'scale=in_range=full:out_range=full,' if full_range else ''
    raw = subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(mkv),
         '-vf', f'{range_prefix}select=not(mod(n\\,{stride})),format=rgb24',
         '-vsync', 'vfr', '-f', 'rawvideo', '-'],
        capture_output=True, check=False,
    )
    if raw.returncode != 0 or not raw.stdout:
        return None
    frame_bytes = w * h * 3
    got = len(raw.stdout) // frame_bytes
    if got == 0:
        return None
    frames = np.frombuffer(raw.stdout[:got * frame_bytes], dtype=np.uint8).reshape(got, h, w, 3)
    return frames.mean(axis=0).astype(np.uint8)


def resolve_calibration_from_mkv(
    mkv: Path,
    station_cfg: dict,
    full_range: bool = False,
) -> tuple[float, float, float, float] | None:
    """Encoder-side helper: sample the MKV, derive adaptive gains, return
    ``(gain_r, gain_g, gain_b, gamma)``. Returns None if sampling failed.

    ``full_range`` forces full-range YUV interpretation for cameras that flag
    full-range data as limited (see ``sample_avg_frame``)."""
    ref = sample_avg_frame(mkv, n_samples=5, full_range=full_range)
    if ref is None:
        return None
    return resolve_calibration_from_frame(ref, station_cfg)
