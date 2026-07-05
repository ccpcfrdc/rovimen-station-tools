"""overlay.py — Shared OSD annotation utilities.

Used by both coppermind (video encoding) and nightwatcher (maxpixel stacks)
so that both produce visually identical overlays from the same config.

Public API
----------
build_drawtext_annotations(overlay_cfg, station_id, station_cfg, chunk_epoch)
    → (list[str], int)   ffmpeg drawtext filter strings + cinema bar height

annotate_still(src, dst, overlay_cfg, station_id, station_cfg, chunk_epoch)
    → bool               apply overlay to a still image (WebP/PNG) via ffmpeg
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE = Path.home()

try:
    from PIL import Image as _PILImage, ImageDraw as _PILDraw, ImageFont as _PILFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


# ---------------------------------------------------------------------------
# Text measurement helpers
# ---------------------------------------------------------------------------

def measure_text_width(font_path: str, font_size: int, text: str) -> int:
    """Return pixel width of text at the given font. Falls back to monospace estimate."""
    if _HAS_PIL:
        try:
            img  = _PILImage.new('RGB', (4000, 200))
            draw = _PILDraw.Draw(img)
            font = _PILFont.truetype(font_path, font_size)
            bb   = draw.textbbox((0, 0), text, font=font)
            return bb[2] - bb[0]
        except Exception:
            pass
    return int(font_size * 0.65 * len(text))


def measure_text_height(font_path: str, font_size: int, text: str = 'Ag') -> int:
    """Return pixel height of text bounding box at the given font. Falls back to font_size."""
    if _HAS_PIL:
        try:
            img  = _PILImage.new('RGB', (4000, 200))
            draw = _PILDraw.Draw(img)
            font = _PILFont.truetype(font_path, font_size)
            bb   = draw.textbbox((0, 0), text, font=font)
            return bb[3] - bb[1]
        except Exception:
            pass
    return font_size


# ---------------------------------------------------------------------------
# Platepar pointing fallback
# ---------------------------------------------------------------------------

def platepar_pointing(cam: str, cfg: dict) -> tuple[float, float] | None:
    """Read az_centre/alt_centre from the active platepar for *cam*.

    Returns (az, alt) or None if no usable platepar exists.
    """
    candidates: list[Path] = []

    active = _BASE / 'source' / 'Stations' / cam / 'platepar_cmn2010.cal'
    if active.exists():
        candidates.append(active)

    rms_path = cfg.get('stations', {}).get(cam, {}).get('rms_data_path')
    if rms_path:
        rms_dir = Path(rms_path)
        for sub in ('CapturedFiles', 'ArchivedFiles'):
            sub_dir = rms_dir / sub
            if not sub_dir.exists():
                continue
            for session in sub_dir.iterdir():
                if not session.is_dir():
                    continue
                pp = session / 'platepar_cmn2010.cal'
                if pp.exists():
                    candidates.append(pp)

    if not candidates:
        return None

    best = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(best.read_text())
        az = data.get('az_centre')
        alt = data.get('alt_centre')
        if az is not None and alt is not None:
            return float(az), float(alt)
    except Exception:
        pass
    return None


def enrich_station_cfg_pointing(station_id: str, station_cfg: dict,
                                cfg: dict) -> dict:
    """Return station_cfg with az/alt filled from the platepar if missing."""
    if 'az' in station_cfg and 'alt' in station_cfg:
        return station_cfg
    pointing = platepar_pointing(station_id, cfg)
    if pointing:
        logger.debug('%s: az/alt from platepar: az=%.1f alt=%.1f',
                     station_id, pointing[0], pointing[1])
        return {**station_cfg, 'az': pointing[0], 'alt': pointing[1]}
    return station_cfg


# ---------------------------------------------------------------------------
# Drawtext filter builder
# ---------------------------------------------------------------------------

def build_drawtext_annotations(
    overlay_cfg: dict,
    station_id: str,
    station_cfg: dict,
    chunk_epoch: int,
) -> tuple[list[str], int]:
    """Build drawtext filter strings for OSD annotation.

    Returns (filters, bar_h) where bar_h > 0 for cinema style (pad height to add).

    Standard style: elements overlaid directly on video/image.
    Cinema style:   black bar added at bottom; all elements positioned inside it.
      - Left:  network name (+ logo composited alongside in caller)
      - Right: station / coords / az+alt / timestamp stacked top→bottom
    """
    font_path = overlay_cfg.get('font', '')
    font_size = overlay_cfg.get('font_size', 19)
    opacity   = float(overlay_cfg.get('text_opacity', 0.4))
    coords    = overlay_cfg.get('coords', '')
    network   = overlay_cfg.get('network', 'ROVIMEN')
    az        = float(station_cfg.get('az', overlay_cfg.get('az', 0)))
    alt       = float(station_cfg.get('alt', overlay_cfg.get('alt', 0)))
    style     = overlay_cfg.get('style', 'standard')

    show_logo      = overlay_cfg.get('show_logo',      True)  # noqa: F841 (used by caller)
    show_station   = overlay_cfg.get('show_station',   True)
    show_coords    = overlay_cfg.get('show_coords',    True)
    show_pointing  = overlay_cfg.get('show_pointing',  True)
    show_timestamp = overlay_cfg.get('show_timestamp', True)
    show_network   = overlay_cfg.get('show_network',   True)

    MARGIN = 14
    GAP    = 10

    if not Path(font_path).exists():
        return [], 0

    shadow_opacity = opacity * 0.8

    def dt(text_val: str, x_expr: str, y_expr: str) -> str:
        return (
            f'drawtext=fontfile={font_path}'
            f':text={text_val}'
            f':fontsize={font_size}'
            f':fontcolor=white@{opacity:.2f}'
            f':shadowcolor=black@{shadow_opacity:.2f}'
            f':shadowx=2:shadowy=2'
            f':x={x_expr}:y={y_expr}'
        )

    def ts(x_expr: str, y_expr: str) -> str:
        return (
            f'drawtext=fontfile={font_path}'
            f':expansion=strftime'
            f':basetime={chunk_epoch * 1000000}'
            f':text=%Y-%m-%d  %T UTC'
            f':fontsize={font_size}'
            f':fontcolor=white@{opacity:.2f}'
            f':shadowcolor=black@{shadow_opacity:.2f}'
            f':shadowx=2:shadowy=2'
            f':x={x_expr}:y={y_expr}'
        )

    az_alt_text = f'ALT {alt:.1f}  AZ {az:.1f}'

    if style == 'cinema':
        bar_h = 2 * MARGIN + 2 * font_size + 1 * GAP
        bar_h = (bar_h + 15) // 16 * 16  # align to 16px for VAAPI encoder
        y = [f'h-{bar_h}+{MARGIN + i * (font_size + GAP)}' for i in range(2)]
        filters = []
        if show_network:
            filters.append(dt(network, str(MARGIN), y[0]))
        if show_pointing:
            filters.append(dt(az_alt_text, f'w-{MARGIN}-tw', y[0]))
        if show_timestamp:
            filters.append(ts(str(MARGIN), y[1]))
        parts = []
        if show_station: parts.append(station_id)
        if show_coords:  parts.append(coords)
        if parts:
            filters.append(dt('  '.join(parts), f'w-{MARGIN}-tw', y[1]))
        return filters, bar_h

    else:  # standard
        filters = []
        if show_timestamp:
            filters.append(ts(f'w-{MARGIN}-tw', f'h-{MARGIN}-th'))
        if show_pointing:
            filters.append(dt(az_alt_text, f'w-{MARGIN}-tw', f'h-{MARGIN}-2*th-{GAP}'))
        if show_coords:
            filters.append(dt(coords, f'w-{MARGIN}-tw', f'h-{MARGIN}-3*th-{GAP*2}'))
        if show_station:
            filters.append(dt(station_id, f'w-{MARGIN}-tw', f'h-{MARGIN}-4*th-{GAP*3}'))
        if show_network:
            filters.append(dt(network, str(MARGIN), str(MARGIN)))
        return filters, 0


# ---------------------------------------------------------------------------
# Still-image annotation
# ---------------------------------------------------------------------------

def annotate_still(
    src: Path,
    dst: Path,
    overlay_cfg: dict,
    station_id: str,
    station_cfg: dict,
    chunk_epoch: int,
) -> bool:
    """Apply OSD annotation to a still image (WebP/PNG) via ffmpeg.

    Uses the same drawtext filters and layout as coppermind uses for video,
    so stacks and encoded clips look identical. Overwrites dst.

    Returns True on success, False on ffmpeg error.
    """
    filters, bar_h = build_drawtext_annotations(
        overlay_cfg, station_id, station_cfg, chunk_epoch,
    )

    if not filters and bar_h == 0:
        if src != dst:
            import shutil
            shutil.copy2(src, dst)
        return True

    logo_path    = overlay_cfg.get('logo', '')
    logo_opacity = overlay_cfg.get('logo_opacity', 0.8)
    show_logo    = overlay_cfg.get('show_logo', True)
    use_logo     = bool(show_logo and logo_path and Path(logo_path).exists())

    if use_logo:
        font_path = overlay_cfg.get('font', '')
        font_size = overlay_cfg.get('font_size', 19)
        network   = overlay_cfg.get('network', 'ROVIMEN')
        style     = overlay_cfg.get('style', 'standard')
        MARGIN    = 14

        vf_parts = []
        if bar_h > 0:
            vf_parts.append(f'pad=iw:ih+{bar_h}:0:0:black')
        vf_parts.extend(filters)
        cpu_chain = ','.join(vf_parts) if vf_parts else 'null'

        ntw = measure_text_width(font_path, font_size, network)
        logo_size = int(overlay_cfg.get('logo_size', 0))
        logo_h = logo_size if logo_size > 0 else measure_text_height(font_path, font_size) + 1
        logo_x = MARGIN + ntw + 8
        text_center = f'H-{bar_h}+{MARGIN}+{font_size}/2' if style == 'cinema' else f'{MARGIN}+{font_size}/2'
        logo_y = f'({text_center})-{logo_h}/2'

        filter_complex = (
            f'[0:v]{cpu_chain}[vmain];'
            f'[1:v]scale=-2:{logo_h},format=rgba,'
            f'colorchannelmixer=aa={logo_opacity}[logo];'
            f'[vmain][logo]overlay=x={logo_x}:y={logo_y}[out]'
        )
        webp_lossless = ['-quality', '95'] if dst.suffix.lower() == '.webp' else []
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(src),
            '-i', logo_path,
            '-filter_complex', filter_complex,
            '-map', '[out]',
            *webp_lossless,
            str(dst),
        ]
    else:
        vf_parts = []
        if bar_h > 0:
            vf_parts.append(f'pad=iw:ih+{bar_h}:0:0:black')
        vf_parts.extend(filters)
        webp_lossless = ['-quality', '95'] if dst.suffix.lower() == '.webp' else []
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(src),
            '-vf', ','.join(vf_parts),
            *webp_lossless,
            str(dst),
        ]

    # ffmpeg cannot read and write the same file — use a temp file when overwriting.
    # When dst is WebP, ffmpeg may lack libwebp; route through a PNG temp and let
    # Pillow handle the final WebP write.
    inplace = (src.resolve() == dst.resolve())
    is_webp = dst.suffix.lower() == '.webp'

    if is_webp:
        fd, png_tmp = tempfile.mkstemp(suffix='.png', dir=dst.parent)
        os.close(fd)
        cmd[-1] = png_tmp
        result = subprocess.run(cmd, stderr=subprocess.DEVNULL, env={**os.environ, 'TZ': 'UTC'})
        try:
            if result.returncode == 0:
                from PIL import Image as _PILImg
                _PILImg.open(png_tmp).save(str(dst), 'webp', quality=95)
                return True
            return False
        except Exception:
            return False
        finally:
            try:
                os.unlink(png_tmp)
            except OSError:
                pass
    else:
        if inplace:
            fd, tmp = tempfile.mkstemp(suffix=dst.suffix, dir=dst.parent)
            os.close(fd)
            cmd[-1] = tmp
        else:
            tmp = None

        result = subprocess.run(cmd, stderr=subprocess.DEVNULL, env={**os.environ, 'TZ': 'UTC'})
        if result.returncode == 0:
            if inplace:
                os.replace(tmp, dst)
            return True
        else:
            if inplace and tmp and os.path.exists(tmp):
                os.unlink(tmp)
            return False
