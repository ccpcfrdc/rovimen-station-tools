#!/usr/bin/env python3
"""
Color Calibration for IR-Cut-Removed Meteor Cameras

All XMEye cameras in the GMN network have their IR cut filter physically removed,
causing a strong red/pink tint. The Sony STARVIS sensors (IMX291/307) have ~3x
enhanced NIR sensitivity, making the red channel dominant.

This tool derives per-camera color correction gains from nighttime sky images
(Gray World assumption on sky background) and applies them consistently to
both maxpixel stacks (NumPy) and video clips (ffmpeg).

Usage:
    python color_calibration.py derive      --image stack.jpg --camera RO000A [--preview]
    python color_calibration.py apply-image --input raw.jpg --output corrected.jpg --camera RO000A
    python color_calibration.py apply-video --input raw.mp4 --output corrected.mp4 --camera RO000A
    python color_calibration.py ffmpeg-filter --camera RO000A
    python color_calibration.py list
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

# Config file location (next to this script)
GAINS_FILE = Path(__file__).parent / "color_calibration_gains.json"


# =============================================================================
# Gains persistence
# =============================================================================

def load_gains():
    """Load the gains config file. Returns dict."""
    if GAINS_FILE.exists():
        return json.loads(GAINS_FILE.read_text())
    return {"version": 1, "method": "gray_world_sky_maxgain", "cameras": {}}


def save_gains(config):
    """Save the gains config file."""
    GAINS_FILE.write_text(json.dumps(config, indent=4) + "\n")


def get_camera_gains(camera_id):
    """Get gains for a specific camera. Returns (gain_r, gain_g, gain_b) or None."""
    config = load_gains()
    cam = config.get("cameras", {}).get(camera_id)
    if cam is None:
        return None
    return cam["gain_r"], cam["gain_g"], cam["gain_b"]


# =============================================================================
# Core calibration: Gray World on sky background
# =============================================================================

def derive_gains(
    image,
    percentile_low=10,
    percentile_high=85,
    target_mode="mean",
    gain_ceiling=1.0,
):
    """Derive per-channel color correction gains from a sky image.

    Method:
        1. Compute per-pixel luminance (mean of RGB)
        2. Select sky pixels between percentile_low and percentile_high of luminance
           (excludes dark noise floor and bright stars/meteor trails)
        3. Compute mean R, G, B of selected sky pixels
        4. Compute gains toward a target:
           - mean: mean(R,G,B), traditional Gray World
           - max: max(R,G,B), attenuation-only
           - green: green channel mean
        5. Optional gain ceiling rescales all gains by one common factor so that
           no channel exceeds the requested maximum (default 1.0).

    Args:
        image: numpy array (H, W, 3) uint8 RGB
        percentile_low: lower luminance percentile cutoff (default 10)
        percentile_high: upper luminance percentile cutoff (default 85)

    Returns:
        (gain_r, gain_g, gain_b, sky_mean_rgb) where sky_mean_rgb is (mean_r, mean_g, mean_b)
    """
    # Compute per-pixel luminance as mean of channels
    luminance = image.astype(np.float32).mean(axis=2)

    # Determine percentile thresholds
    lo = np.percentile(luminance, percentile_low)
    hi = np.percentile(luminance, percentile_high)

    # Select sky pixels (between thresholds)
    mask = (luminance >= lo) & (luminance <= hi)
    sky_pixels = image[mask].astype(np.float32)

    if len(sky_pixels) == 0:
        print("Warning: no sky pixels selected, using full image")
        sky_pixels = image.reshape(-1, 3).astype(np.float32)

    mean_r = float(sky_pixels[:, 0].mean())
    mean_g = float(sky_pixels[:, 1].mean())
    mean_b = float(sky_pixels[:, 2].mean())

    if target_mode == "mean":
        target = (mean_r + mean_g + mean_b) / 3.0
    elif target_mode == "max":
        target = max(mean_r, mean_g, mean_b)
    elif target_mode == "green":
        target = mean_g
    else:
        raise ValueError(f"Unknown target_mode={target_mode}")

    gain_r = target / mean_r if mean_r > 0 else 1.0
    gain_g = target / mean_g if mean_g > 0 else 1.0
    gain_b = target / mean_b if mean_b > 0 else 1.0

    if gain_ceiling is not None:
        max_gain = max(gain_r, gain_g, gain_b)
        if max_gain > gain_ceiling > 0:
            scale = gain_ceiling / max_gain
            gain_r *= scale
            gain_g *= scale
            gain_b *= scale

    return gain_r, gain_g, gain_b, (mean_r, mean_g, mean_b)


def apply_gains_to_image(
    image,
    gain_r,
    gain_g,
    gain_b,
    highlight_protect=False,
    knee_low=0.65,
    knee_high=0.95,
):
    """Apply color correction gains to a numpy RGB image.

    Args:
        image: numpy array (H, W, 3) uint8 RGB
        gain_r, gain_g, gain_b: per-channel multipliers

    Returns:
        Corrected numpy array (H, W, 3) uint8
    """
    corrected = image.astype(np.float32)

    if highlight_protect:
        # Fade WB toward unity in highlights to reduce per-channel clipping tint.
        img01 = corrected / 255.0
        luma = 0.2126 * img01[:, :, 0] + 0.7152 * img01[:, :, 1] + 0.0722 * img01[:, :, 2]
        t = np.clip((luma - knee_low) / max(knee_high - knee_low, 1e-6), 0.0, 1.0)
        w = 1.0 - (t * t * (3.0 - 2.0 * t))  # smoothstep fade-out

        corrected[:, :, 0] *= 1.0 + w * (gain_r - 1.0)
        corrected[:, :, 1] *= 1.0 + w * (gain_g - 1.0)
        corrected[:, :, 2] *= 1.0 + w * (gain_b - 1.0)
    else:
        corrected[:, :, 0] *= gain_r
        corrected[:, :, 1] *= gain_g
        corrected[:, :, 2] *= gain_b

    return np.clip(corrected, 0, 255).astype(np.uint8)


# =============================================================================
# ffmpeg filter string builders
# =============================================================================

def build_ffmpeg_lutrgb_filter(gain_r, gain_g, gain_b):
    """Build an ffmpeg lutrgb filter string from gains.

    Returns string like: "lutrgb=r=val*0.620:g=val*1.000:b=val*1.250"
    """
    return f"lutrgb=r=val*{gain_r:.3f}:g=val*{gain_g:.3f}:b=val*{gain_b:.3f}"


def build_ffmpeg_colorchannelmixer_filter(gain_r, gain_g, gain_b):
    """Build a diagonal-only colorchannelmixer filter from gains."""
    return (
        "colorchannelmixer="
        f"rr={gain_r:.4f}:rg=0:rb=0:gr=0:gg={gain_g:.4f}:gb=0:br=0:bg=0:bb={gain_b:.4f}"
    )


def build_ffmpeg_vf_string(camera_id, rotate=True, color_filter="lutrgb"):
    """Build a complete -vf filter string for a camera, combining rotation + color correction.

    Args:
        camera_id: camera identifier to look up gains
        rotate: whether to include 180-degree rotation (True for all except Raul)

    Returns:
        Filter string like "rotate=PI,lutrgb=r=val*0.620:g=val:b=val*1.250"
        or just "rotate=PI" if no gains are stored for this camera.
    """
    parts = []
    if rotate:
        parts.append("rotate=PI")

    gains = get_camera_gains(camera_id)
    if gains is not None:
        gain_r, gain_g, gain_b = gains
        if color_filter == "colorchannelmixer":
            parts.append(build_ffmpeg_colorchannelmixer_filter(gain_r, gain_g, gain_b))
        else:
            parts.append(build_ffmpeg_lutrgb_filter(gain_r, gain_g, gain_b))

    return ",".join(parts) if parts else None


# =============================================================================
# Video processing
# =============================================================================

def correct_video_file(input_path, output_path, camera_id, rotate=True, crf=18,
                       color_filter="colorchannelmixer",
                       highlight_protect=False, knee_low=0.65, knee_high=0.95,
                       gains_override=None):
    """Apply color correction (and optional rotation) to a video file using ffmpeg.

    When highlight_protect is False, uses pure ffmpeg filters (fast).
    When highlight_protect is True, uses Python frame-by-frame processing (slower
    but supports luma-dependent gain blending for natural star/meteor colors).

    Args:
        gains_override: (gain_r, gain_g, gain_b) tuple to use instead of stored gains.
                       Used for adaptive per-clip calibration.

    Returns:
        True if successful
    """
    if highlight_protect:
        return _correct_video_python(input_path, output_path, camera_id,
                                     rotate=rotate, crf=crf,
                                     knee_low=knee_low, knee_high=knee_high,
                                     gains_override=gains_override)

    vf = build_ffmpeg_vf_string(camera_id, rotate=rotate, color_filter=color_filter)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
    ]
    if vf:
        cmd.extend(["-vf", vf])
    cmd.extend([
        "-c:v", "libx264", "-crf", str(crf), "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-an", str(output_path),
    ])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg error: {result.stderr}")
        return False
    return os.path.getsize(output_path) > 1000


def _correct_video_python(input_path, output_path, camera_id, rotate=True, crf=18,
                           knee_low=0.65, knee_high=0.95, gains_override=None):
    """Frame-by-frame video correction with highlight protection via numpy.

    Decodes with ffmpeg, applies gains + highlight-protect per frame in Python,
    pipes corrected frames back to ffmpeg for encoding.
    """
    gains = gains_override or get_camera_gains(camera_id)
    if gains is None:
        print("No gains stored")
        return False

    # Probe dimensions
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(input_path)],
            capture_output=True, text=True, timeout=10,
        )
        streams = json.loads(probe.stdout)
        vs = next(s for s in streams["streams"] if s["codec_type"] == "video")
        w, h = int(vs["width"]), int(vs["height"])
        # Get fps for output
        r_fps = vs.get("r_frame_rate", "25/1")
    except Exception as e:
        print(f"Probe failed: {e}")
        return False

    # Build output vf (rotation only — color is done in Python)
    out_vf = "rotate=PI" if rotate else None

    # Decoder
    decoder = subprocess.Popen(
        ["ffmpeg", "-i", str(input_path), "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-v", "quiet", "-"],
        stdout=subprocess.PIPE,
    )

    # Encoder
    enc_cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{w}x{h}", "-r", r_fps, "-i", "-",
    ]
    if out_vf:
        enc_cmd.extend(["-vf", out_vf])
    enc_cmd.extend([
        "-c:v", "libx264", "-crf", str(crf), "-preset", "fast",
        "-pix_fmt", "yuv420p", "-an", str(output_path),
    ])
    encoder = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE)

    frame_size = w * h * 3
    n_frames = 0
    gain_r, gain_g, gain_b = gains

    try:
        while True:
            raw = decoder.stdout.read(frame_size)
            if len(raw) < frame_size:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
            corrected = apply_gains_to_image(
                frame, gain_r, gain_g, gain_b,
                highlight_protect=True, knee_low=knee_low, knee_high=knee_high,
            )
            encoder.stdin.write(corrected.tobytes())
            n_frames += 1
    except BrokenPipeError:
        pass
    finally:
        decoder.stdout.close()
        decoder.wait()
        encoder.stdin.close()
        encoder.wait()

    print(f"  Processed {n_frames} frames (highlight-protect, knee={knee_low}-{knee_high})")
    return os.path.getsize(output_path) > 1000


def derive_gains_from_video(video_path, percentile_low=10, percentile_high=85,
                            target_mode="mean", gain_ceiling=1.0, sample_frames=50):
    """Derive color correction gains from a video's own sky pixels.

    Samples evenly-spaced frames, builds a maxpixel, and runs derive_gains on it.
    This gives per-clip adaptive calibration that works across twilight, deep night, etc.

    Args:
        video_path: source video file
        sample_frames: number of frames to sample (evenly spaced)

    Returns:
        (gain_r, gain_g, gain_b, sky_mean_rgb) or None on failure
    """
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(video_path)],
            capture_output=True, text=True, timeout=10,
        )
        streams = json.loads(probe.stdout)
        vs = next(s for s in streams["streams"] if s["codec_type"] == "video")
        w, h = int(vs["width"]), int(vs["height"])
    except Exception as e:
        print(f"Probe failed: {e}")
        return None

    # Decode frames, sampling evenly using reservoir approach to avoid holding all in RAM.
    # Use ffmpeg to count frames first (fast), then select which to keep during decode.
    count_cmd = ["ffprobe", "-v", "quiet", "-count_frames", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_read_frames", "-print_format", "csv=p=0",
                 str(video_path)]
    try:
        count_result = subprocess.run(count_cmd, capture_output=True, text=True, timeout=30)
        total_frames = int(count_result.stdout.strip())
    except Exception:
        total_frames = 0

    proc = subprocess.Popen(
        ["ffmpeg", "-i", str(video_path), "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-v", "quiet", "-"],
        stdout=subprocess.PIPE,
    )
    frame_size = w * h * 3

    # Decide which frames to sample (evenly spaced)
    if total_frames > 0:
        step = max(1, total_frames // sample_frames)
        keep_set = set(range(0, total_frames, step))
    else:
        keep_set = None  # keep every Nth frame on the fly

    maxpixel = None
    n = 0
    kept = 0
    while True:
        raw = proc.stdout.read(frame_size)
        if len(raw) < frame_size:
            break
        should_keep = (n in keep_set) if keep_set is not None else (n % max(1, 7) == 0)
        if should_keep and kept < sample_frames:
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
            if maxpixel is None:
                maxpixel = frame.copy()
            else:
                np.maximum(maxpixel, frame, out=maxpixel)
            kept += 1
        n += 1
    proc.wait()

    if n == 0:
        print("No frames decoded")
        return None

    gains = derive_gains(maxpixel, percentile_low=percentile_low,
                         percentile_high=percentile_high,
                         target_mode=target_mode, gain_ceiling=gain_ceiling)
    return gains


def generate_corrected_maxpixel(video_path, output_path, camera_id,
                                highlight_protect=False, knee_low=0.65, knee_high=0.95,
                                gains_override=None, rotate=False):
    """Generate a color-corrected maxpixel stack from a video file.

    Decodes all frames, applies color gains per-frame, then takes per-pixel maximum.

    Args:
        video_path: source video file
        output_path: output JPEG path
        camera_id: camera identifier to look up gains
        highlight_protect: fade WB gains in highlights to preserve star/meteor colors
        knee_low, knee_high: smoothstep knee range for highlight protection
        gains_override: (gain_r, gain_g, gain_b) tuple to use instead of stored gains
        rotate: apply 180-degree rotation during decode

    Returns:
        True if successful
    """
    gains = gains_override or get_camera_gains(camera_id)

    # Probe video dimensions
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(video_path)],
            capture_output=True, text=True, timeout=10,
        )
        streams = json.loads(probe.stdout)
        video_stream = next(s for s in streams["streams"] if s["codec_type"] == "video")
        width = int(video_stream["width"])
        height = int(video_stream["height"])
    except Exception as e:
        print(f"Probe failed: {e}")
        return False

    # Decode frames as raw RGB (with optional rotation)
    try:
        decode_cmd = ["ffmpeg", "-i", str(video_path)]
        if rotate:
            decode_cmd.extend(["-vf", "rotate=PI"])
        decode_cmd.extend(["-f", "rawvideo", "-pix_fmt", "rgb24", "-v", "quiet", "-"])
        proc = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE)
        frame_size = width * height * 3
        maxpixel = None
        n_frames = 0

        while True:
            raw = proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 3))
            if gains is not None:
                frame = apply_gains_to_image(frame, *gains,
                                             highlight_protect=highlight_protect,
                                             knee_low=knee_low, knee_high=knee_high)
            if maxpixel is None:
                maxpixel = frame.copy()
            else:
                np.maximum(maxpixel, frame, out=maxpixel)
            n_frames += 1

        proc.wait()

        if maxpixel is not None:
            Image.fromarray(maxpixel).save(str(output_path), quality=92)
            print(f"  Maxpixel from {n_frames} frames -> {output_path}")
            return os.path.getsize(output_path) > 1000
    except Exception as e:
        print(f"Maxpixel generation failed: {e}")

    return False


# =============================================================================
# Preview / visualization
# =============================================================================

def preview_correction(image_path, gains=None, camera_id=None):
    """Show side-by-side before/after with RGB histograms.

    Args:
        image_path: path to the source image
        gains: (gain_r, gain_g, gain_b) tuple, or None to look up from camera_id
        camera_id: camera identifier (used if gains is None)
    """
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available, skipping preview")
        return

    if gains is None and camera_id:
        gains = get_camera_gains(camera_id)
    if gains is None:
        print("No gains available for preview")
        return

    gain_r, gain_g, gain_b = gains
    img = np.array(Image.open(image_path))
    corrected = apply_gains_to_image(img, gain_r, gain_g, gain_b)

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    # Before
    axes[0, 0].imshow(img)
    axes[0, 0].set_title("Original (IR-cut removed)")
    axes[0, 0].axis("off")

    # After
    axes[0, 1].imshow(corrected)
    axes[0, 1].set_title(f"Corrected (R={gain_r:.3f}, G={gain_g:.3f}, B={gain_b:.3f})")
    axes[0, 1].axis("off")

    # Histogram before
    for ch, color in enumerate(["red", "green", "blue"]):
        axes[1, 0].hist(img[:, :, ch].ravel(), bins=128, range=(0, 256),
                        color=color, alpha=0.5, label=color)
    axes[1, 0].set_title("Original histogram")
    axes[1, 0].legend()
    axes[1, 0].set_xlim(0, 256)

    # Histogram after
    for ch, color in enumerate(["red", "green", "blue"]):
        axes[1, 1].hist(corrected[:, :, ch].ravel(), bins=128, range=(0, 256),
                        color=color, alpha=0.5, label=color)
    axes[1, 1].set_title("Corrected histogram")
    axes[1, 1].legend()
    axes[1, 1].set_xlim(0, 256)

    plt.suptitle(f"Color Calibration: {camera_id or Path(image_path).stem}")
    plt.tight_layout()
    plt.show()


# =============================================================================
# CLI subcommands
# =============================================================================

def cmd_derive(args):
    """Derive color correction gains from a sky image."""
    img = np.array(Image.open(args.image))
    print(f"Image: {args.image} ({img.shape[1]}x{img.shape[0]})")

    gain_r, gain_g, gain_b, (mean_r, mean_g, mean_b) = derive_gains(
        img,
        percentile_low=args.percentile_low,
        percentile_high=args.percentile_high,
        target_mode=args.target_mode,
        gain_ceiling=args.gain_ceiling,
    )

    print(f"Sky mean RGB: ({mean_r:.1f}, {mean_g:.1f}, {mean_b:.1f})")
    print(f"Gains: R={gain_r:.3f}, G={gain_g:.3f}, B={gain_b:.3f}")

    # Sanity check — gains should be reasonable
    for name, g in [("gain_r", gain_r), ("gain_g", gain_g), ("gain_b", gain_b)]:
        if not (0.3 <= g <= 2.5):
            print(f"WARNING: {name}={g:.3f} outside expected range [0.3, 2.5]")

    if args.camera:
        config = load_gains()
        config["cameras"][args.camera] = {
            "gain_r": round(gain_r, 4),
            "gain_g": round(gain_g, 4),
            "gain_b": round(gain_b, 4),
            "target_mode": args.target_mode,
            "gain_ceiling": args.gain_ceiling,
            "source_image": os.path.basename(args.image),
            "derived_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "sky_mean_rgb": [round(mean_r, 1), round(mean_g, 1), round(mean_b, 1)],
        }
        save_gains(config)
        print(f"Saved gains for {args.camera} -> {GAINS_FILE}")

    if args.preview:
        preview_correction(args.image, gains=(gain_r, gain_g, gain_b),
                          camera_id=args.camera)


def cmd_apply_image(args):
    """Apply color correction to a single image."""
    gains = get_camera_gains(args.camera)
    if gains is None:
        print(f"No gains stored for camera {args.camera}")
        sys.exit(1)

    gain_r, gain_g, gain_b = gains
    print(f"Applying gains R={gain_r:.3f}, G={gain_g:.3f}, B={gain_b:.3f} to {args.input}")

    img = np.array(Image.open(args.input).convert("RGB"))
    corrected = apply_gains_to_image(
        img,
        gain_r,
        gain_g,
        gain_b,
        highlight_protect=args.highlight_protect,
        knee_low=args.knee_low,
        knee_high=args.knee_high,
    )
    Image.fromarray(corrected).save(args.output, quality=92)
    print(f"Saved: {args.output}")


def cmd_apply_video(args):
    """Apply color correction to a video file."""
    rotate = not args.no_rotate
    hp = getattr(args, 'highlight_protect', False)
    adaptive = getattr(args, 'adaptive', False)
    gains_override = None

    blue_boost = getattr(args, 'blue_boost', 1.0)

    if adaptive:
        print(f"Adaptive mode: deriving gains from clip itself...")
        result = derive_gains_from_video(args.input, gain_ceiling=args.gain_ceiling)
        if result is None:
            print("Failed to derive adaptive gains")
            sys.exit(1)
        gain_r, gain_g, gain_b, (mr, mg, mb) = result
        print(f"  Sky mean RGB: ({mr:.1f}, {mg:.1f}, {mb:.1f})")
    else:
        gains = get_camera_gains(args.camera)
        if gains is None:
            print(f"No gains stored for camera {args.camera}")
            sys.exit(1)
        gain_r, gain_g, gain_b = gains

    if blue_boost != 1.0:
        gain_b *= blue_boost
    gains_override = (gain_r, gain_g, gain_b)

    print(f"Applying gains R={gain_r:.3f}, G={gain_g:.3f}, B={gain_b:.3f}")
    print(f"  Rotate: {rotate}, CRF: {args.crf}")
    print(f"  Input:  {args.input}")
    print(f"  Output: {args.output}")

    if hp:
        print(f"  Highlight-protect: knee={args.knee_low}-{args.knee_high}")
    elif not adaptive:
        vf = build_ffmpeg_vf_string(args.camera, rotate=rotate, color_filter=args.color_filter)
        print(f"  Filter: -vf \"{vf}\"")

    if correct_video_file(args.input, args.output, args.camera,
                         rotate=rotate, crf=args.crf, color_filter=args.color_filter,
                         highlight_protect=hp or adaptive or blue_boost != 1.0,
                         knee_low=getattr(args, 'knee_low', 0.65),
                         knee_high=getattr(args, 'knee_high', 0.95),
                         gains_override=gains_override):
        input_size = os.path.getsize(args.input) / (1024 * 1024)
        output_size = os.path.getsize(args.output) / (1024 * 1024)
        print(f"  Done! {input_size:.1f}M -> {output_size:.1f}M")
    else:
        print("  FAILED")
        sys.exit(1)


def cmd_ffmpeg_filter(args):
    """Print the ffmpeg -vf filter string for a camera."""
    rotate = not args.no_rotate
    vf = build_ffmpeg_vf_string(args.camera, rotate=rotate, color_filter=args.color_filter)
    if vf is None:
        print(f"No gains stored for camera {args.camera}")
        sys.exit(1)
    print(vf)


def cmd_list(args):
    """List all stored camera calibrations."""
    config = load_gains()
    cameras = config.get("cameras", {})
    if not cameras:
        print("No calibrations stored yet.")
        return

    print(f"{'Camera':<10} {'Gain R':>8} {'Gain G':>8} {'Gain B':>8}  {'Source':<40} {'Derived'}")
    print("-" * 100)
    for cam_id, cam in sorted(cameras.items()):
        print(f"{cam_id:<10} {cam['gain_r']:>8.4f} {cam['gain_g']:>8.4f} {cam['gain_b']:>8.4f}"
              f"  {cam.get('source_image', 'N/A'):<40} {cam.get('derived_at', 'N/A')}")


def cmd_maxpixel(args):
    """Generate a color-corrected maxpixel from a video."""
    hp = getattr(args, 'highlight_protect', False)
    adaptive = getattr(args, 'adaptive', False)
    blue_boost = getattr(args, 'blue_boost', 1.0)
    gains_override = None

    if adaptive:
        print(f"Adaptive mode: deriving gains from clip itself...")
        result = derive_gains_from_video(args.input, gain_ceiling=args.gain_ceiling)
        if result is None:
            print("Failed to derive adaptive gains")
            sys.exit(1)
        gain_r, gain_g, gain_b, (mr, mg, mb) = result
        if blue_boost != 1.0:
            gain_b *= blue_boost
        gains_override = (gain_r, gain_g, gain_b)
        print(f"  Sky mean RGB: ({mr:.1f}, {mg:.1f}, {mb:.1f})")
        print(f"  Adaptive gains: R={gain_r:.3f}, G={gain_g:.3f}, B={gain_b:.3f}")

    print(f"Generating corrected maxpixel from {args.input}" +
          (" (adaptive+highlight-protect)" if adaptive else
           " (highlight-protect)" if hp else ""))
    if generate_corrected_maxpixel(args.input, args.output, args.camera,
                                    highlight_protect=hp or adaptive or blue_boost != 1.0,
                                    knee_low=getattr(args, 'knee_low', 0.65),
                                    knee_high=getattr(args, 'knee_high', 0.95),
                                    gains_override=gains_override):
        size = os.path.getsize(args.output) / 1024
        print(f"Saved: {args.output} ({size:.0f} KB)")
    else:
        print("FAILED")
        sys.exit(1)


# =============================================================================
# CLI entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Color calibration for IR-cut-removed meteor cameras",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # derive
    p = sub.add_parser("derive", help="Derive color correction gains from a sky image")
    p.add_argument("--image", required=True, help="Input image (maxpixel/stack JPEG)")
    p.add_argument("--camera", help="Camera ID to store gains for (e.g. RO000A)")
    p.add_argument("--preview", action="store_true", help="Show matplotlib preview")
    p.add_argument("--percentile-low", type=int, default=10,
                   help="Lower luminance percentile cutoff (default: 10)")
    p.add_argument("--percentile-high", type=int, default=85,
                   help="Upper luminance percentile cutoff (default: 85)")
    p.add_argument("--target-mode", choices=["mean", "max", "green"], default="mean",
                   help="Gray-world target channel mode (default: mean)")
    p.add_argument("--gain-ceiling", type=float, default=1.0,
                   help="Rescale gains so max gain <= ceiling (default: 1.0)")
    p.set_defaults(func=cmd_derive)

    # apply-image
    p = sub.add_parser("apply-image", help="Apply color correction to an image")
    p.add_argument("--input", required=True, help="Input image")
    p.add_argument("--output", required=True, help="Output image")
    p.add_argument("--camera", required=True, help="Camera ID")
    p.add_argument("--highlight-protect", action="store_true",
                   help="Fade WB gains toward highlights to reduce color clipping")
    p.add_argument("--knee-low", type=float, default=0.65,
                   help="Highlight-protection knee start in [0,1] (default: 0.65)")
    p.add_argument("--knee-high", type=float, default=0.95,
                   help="Highlight-protection knee end in [0,1] (default: 0.95)")
    p.set_defaults(func=cmd_apply_image)

    # apply-video
    p = sub.add_parser("apply-video", help="Apply color correction to a video")
    p.add_argument("--input", required=True, help="Input video")
    p.add_argument("--output", required=True, help="Output video")
    p.add_argument("--camera", required=True, help="Camera ID")
    p.add_argument("--no-rotate", action="store_true", help="Skip 180-degree rotation")
    p.add_argument("--crf", type=int, default=18, help="Video quality CRF (default: 18)")
    p.add_argument("--color-filter", choices=["lutrgb", "colorchannelmixer"], default="colorchannelmixer",
                   help="ffmpeg color filter backend (default: colorchannelmixer)")
    p.add_argument("--highlight-protect", action="store_true",
                   help="Python frame-by-frame mode: fade WB in highlights (slower)")
    p.add_argument("--adaptive", action="store_true",
                   help="Derive gains from the clip itself (per-clip calibration)")
    p.add_argument("--gain-ceiling", type=float, default=1.0,
                   help="Max gain for adaptive mode (default: 1.0)")
    p.add_argument("--blue-boost", type=float, default=1.0,
                   help="Multiply blue gain by this factor (default: 1.0)")
    p.add_argument("--knee-low", type=float, default=0.65,
                   help="Highlight-protection knee start in [0,1] (default: 0.65)")
    p.add_argument("--knee-high", type=float, default=0.95,
                   help="Highlight-protection knee end in [0,1] (default: 0.95)")
    p.set_defaults(func=cmd_apply_video)

    # ffmpeg-filter
    p = sub.add_parser("ffmpeg-filter", help="Print ffmpeg -vf filter string")
    p.add_argument("--camera", required=True, help="Camera ID")
    p.add_argument("--no-rotate", action="store_true", help="Skip 180-degree rotation")
    p.add_argument("--color-filter", choices=["lutrgb", "colorchannelmixer"], default="colorchannelmixer",
                   help="ffmpeg color filter backend (default: colorchannelmixer)")
    p.set_defaults(func=cmd_ffmpeg_filter)

    # list
    p = sub.add_parser("list", help="List all stored camera calibrations")
    p.set_defaults(func=cmd_list)

    # maxpixel
    p = sub.add_parser("maxpixel", help="Generate color-corrected maxpixel from video")
    p.add_argument("--input", required=True, help="Input video file")
    p.add_argument("--output", required=True, help="Output JPEG path")
    p.add_argument("--camera", required=True, help="Camera ID")
    p.add_argument("--highlight-protect", action="store_true",
                   help="Fade WB gains in highlights to preserve star/meteor colors")
    p.add_argument("--adaptive", action="store_true",
                   help="Derive gains from the clip itself (per-clip calibration)")
    p.add_argument("--gain-ceiling", type=float, default=1.0,
                   help="Max gain for adaptive mode (default: 1.0)")
    p.add_argument("--blue-boost", type=float, default=1.0,
                   help="Multiply blue gain by this factor (default: 1.0)")
    p.add_argument("--knee-low", type=float, default=0.65,
                   help="Highlight-protection knee start in [0,1] (default: 0.65)")
    p.add_argument("--knee-high", type=float, default=0.95,
                   help="Highlight-protection knee end in [0,1] (default: 0.95)")
    p.set_defaults(func=cmd_maxpixel)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
