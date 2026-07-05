"""ROVIMEN sky-coverage computation.

Estimates the fraction of Romanian territory that is covered by the network's
cameras, based on each camera's last-known platepar (pointing + FOV).

Method
------
For each camera we project its rectangular FOV boundary onto the ground at a
given altitude (default METEOR_ALT_KM = 75 km for meteors, BOLIDE_ALT_KM = 40 km
for fireballs) using the haversine bearing formula.  The result is a Shapely
polygon.  We union all polygons, intersect with a simplified Romania boundary,
and return:

    coverage_pct      = union_area / romania_area * 100
    dual_coverage_pct = area_seen_by_2+_cameras / romania_area * 100

FOV edges closer than MIN_ALT_DEG to the horizon are clipped so the projection
distance doesn't blow up near the horizon.

Results are cached for CACHE_TTL_S seconds.
"""

from __future__ import annotations

import logging
import math
import time
import threading
from typing import Any

from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

METEOR_ALT_KM  = 75.0    # nominal meteor ablation altitude (peak brightness ~70-90 km)
BOLIDE_ALT_KM  = 40.0    # typical fireball/bolide altitude (most active 30–60 km)
MAX_DIST_KM    = 380.0   # hard cap on ground projection distance
MIN_ALT_DEG    = 4.0     # clip FOV edges below this elevation
BOUNDARY_PTS   = 20      # samples per FOV edge (80 total boundary points)
EARTH_RADIUS   = 6371.0  # km
CACHE_TTL_S    = 3600.0  # 1 hour

# ── Romania boundary (simplified, ~50 vertices, counterclockwise) ──────────
#
# Traced from publicly available geographic data.  Accuracy is ±20 km on most
# segments, which is sufficient for a coverage percentage at this scale.
# Coordinates are (longitude, latitude) as required by Shapely.

_ROMANIA_COORDS: list[tuple[float, float]] = [
    # NW — Satu Mare
    (22.88, 47.77),
    # N border — Ukraine (going east)
    (23.59, 48.00),
    (24.26, 47.93),
    (25.10, 47.73),
    (26.10, 47.67),
    # NE border — Moldova / Prut river (going south)
    (26.66, 47.75),
    (27.29, 47.63),
    (27.60, 47.16),
    (28.11, 46.97),
    (28.20, 46.44),
    # E — Danube mouth / delta (traced north along coast, then south)
    (28.03, 45.43),   # Galați
    (28.46, 45.27),   # Isaccea
    (29.00, 45.18),   # Tulcea
    (29.66, 45.16),   # Sulina — easternmost (north arm, reached first)
    (29.60, 44.92),   # Sfântu Gheorghe — south arm (traced south along coast)
    # SE — Black Sea coast (continuing south)
    (29.50, 44.58),
    (28.80, 44.30),
    (28.66, 44.18),   # Constanța
    (28.59, 43.81),   # Mangalia
    (28.58, 43.74),   # Vama Veche — border with Bulgaria
    # S — Bulgaria border (going west)
    (27.95, 43.90),
    (27.34, 44.12),
    (27.20, 44.13),
    # S — Danube (going west)
    (26.64, 44.07),   # Oltenița
    (25.97, 43.90),   # Giurgiu
    (25.51, 43.78),
    (24.86, 43.75),   # Turnu Măgurele
    (24.51, 43.79),   # Corabia
    (23.86, 43.94),
    (23.05, 44.02),   # Calafat
    (22.65, 44.64),   # Turnu Severin (Drobeta)
    (22.40, 44.73),
    # SW — Serbia (Danube gorge)
    (21.67, 44.86),   # Moldova Veche
    # W — Serbia / Hungary border (going north)
    (21.36, 44.96),
    (21.18, 45.27),
    (21.22, 45.75),   # Timișoara
    (21.07, 46.33),
    (21.23, 46.63),
    (21.94, 47.07),   # Oradea
    (22.38, 47.58),
    (22.88, 47.77),   # close polygon
]

_ROMANIA: Polygon = Polygon(_ROMANIA_COORDS).buffer(0)   # buffer(0) fixes minor self-intersections
_ROMANIA_AREA: float = _ROMANIA.area   # in square-degrees (used as denominator only)

# ── Cache ──────────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
# (expires_mono, pct_90, dual_90, pct_40, dual_40)
_cache: tuple[float, float, float, float, float] | None = None

# ── Geometry ───────────────────────────────────────────────────────────────

def _destination(lat0_deg: float, lon0_deg: float, bearing_deg: float, dist_km: float) -> tuple[float, float]:
    """Return (lat, lon) reached by travelling dist_km on bearing from (lat0, lon0)."""
    d    = dist_km / EARTH_RADIUS
    lat0 = math.radians(lat0_deg)
    lon0 = math.radians(lon0_deg)
    az   = math.radians(bearing_deg)

    lat2 = math.asin(
        math.sin(lat0) * math.cos(d)
        + math.cos(lat0) * math.sin(d) * math.cos(az)
    )
    lon2 = lon0 + math.atan2(
        math.sin(az) * math.sin(d) * math.cos(lat0),
        math.cos(d) - math.sin(lat0) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def _project(lat0: float, lon0: float, az: float, alt: float, H: float) -> tuple[float, float]:
    """Project az/alt direction onto the ground at altitude H km."""
    eff_alt = max(alt, MIN_ALT_DEG)
    dist_km = min(H / math.tan(math.radians(eff_alt)), MAX_DIST_KM)
    return _destination(lat0, lon0, az, dist_km)


def camera_footprint(pp: dict[str, Any], H: float = METEOR_ALT_KM) -> Polygon | None:
    """Return the ground footprint of one camera at altitude H km, or None if platepar is incomplete."""
    try:
        lat   = float(pp["lat"])
        lon   = float(pp["lon"])
        az_c  = float(pp["az_centre"])
        alt_c = float(pp["alt_centre"])
        fov_h = float(pp["fov_h"])
        fov_v = float(pp["fov_v"])
    except (KeyError, TypeError, ValueError):
        return None

    N = BOUNDARY_PTS
    az_l, az_r   = az_c - fov_h / 2, az_c + fov_h / 2
    alt_t, alt_b = alt_c + fov_v / 2, alt_c - fov_v / 2

    coords: list[tuple[float, float]] = []
    edges: list[list[tuple[float, float]]] = [
        [(az_l + i / (N - 1) * fov_h, alt_t) for i in range(N)],   # top
        [(az_r, alt_t - i / (N - 1) * fov_v) for i in range(N)],   # right
        [(az_r - i / (N - 1) * fov_h, alt_b) for i in range(N)],   # bottom
        [(az_l, alt_b + i / (N - 1) * fov_v) for i in range(N)],   # left
    ]
    for edge in edges:
        for az, alt in edge:
            plat, plon = _project(lat, lon, az, alt, H)
            coords.append((plon, plat))   # Shapely: (x=lon, y=lat)

    try:
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        return poly if poly.is_valid and not poly.is_empty else None
    except Exception as exc:
        logger.debug("camera_footprint: %s", exc)
        return None


def _build_footprints(platepars: dict[str, dict], H: float) -> list[Polygon]:
    fps = []
    for pp in platepars.values():
        fp = camera_footprint(pp, H)
        if fp is not None:
            fps.append(fp)
    return fps


def _coverage_from_footprints(footprints: list[Polygon]) -> float:
    """Single-camera coverage: fraction of Romania seen by ≥1 camera."""
    if not footprints:
        return 0.0
    try:
        union        = unary_union(footprints)
        intersection = union.intersection(_ROMANIA)
        return round(min(intersection.area / _ROMANIA_AREA * 100.0, 100.0), 1)
    except Exception as exc:
        logger.error("coverage: geometry failed: %s", exc)
        return 0.0


def _dual_coverage_from_footprints(footprints: list[Polygon]) -> float:
    """Dual-camera coverage: fraction of Romania seen by ≥2 cameras simultaneously."""
    if len(footprints) < 2:
        return 0.0
    overlaps = []
    for i in range(len(footprints)):
        for j in range(i + 1, len(footprints)):
            try:
                inter = footprints[i].intersection(footprints[j])
                if not inter.is_empty:
                    overlaps.append(inter)
            except Exception:
                pass
    if not overlaps:
        return 0.0
    try:
        union        = unary_union(overlaps)
        intersection = union.intersection(_ROMANIA)
        return round(min(intersection.area / _ROMANIA_AREA * 100.0, 100.0), 1)
    except Exception as exc:
        logger.error("dual_coverage: geometry failed: %s", exc)
        return 0.0


def compute_coverage_stats(platepars: dict[str, dict]) -> dict[str, float]:
    """Compute all four coverage values in one pass (meteor + bolide altitudes, single + dual)."""
    fps_90 = _build_footprints(platepars, METEOR_ALT_KM)
    fps_40 = _build_footprints(platepars, BOLIDE_ALT_KM)

    if not fps_90:
        logger.warning("coverage: no valid footprints — returning 0")

    return {
        "coverage_pct":          _coverage_from_footprints(fps_90),
        "dual_coverage_pct":     _dual_coverage_from_footprints(fps_90),
        "coverage_pct_40":       _coverage_from_footprints(fps_40),
        "dual_coverage_pct_40":  _dual_coverage_from_footprints(fps_40),
    }


def get_coverage_stats(platepars: dict[str, dict]) -> dict[str, float]:
    """Cached wrapper around compute_coverage_stats (TTL = CACHE_TTL_S)."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache and now < _cache[0]:
            c = _cache
            return {
                "coverage_pct":         c[1],
                "dual_coverage_pct":    c[2],
                "coverage_pct_40":      c[3],
                "dual_coverage_pct_40": c[4],
            }
    stats = compute_coverage_stats(platepars)
    with _cache_lock:
        _cache = (
            now + CACHE_TTL_S,
            stats["coverage_pct"],
            stats["dual_coverage_pct"],
            stats["coverage_pct_40"],
            stats["dual_coverage_pct_40"],
        )
    logger.info(
        "coverage: recomputed → 90km %.1f%% (dual %.1f%%), 40km %.1f%% (dual %.1f%%)",
        stats["coverage_pct"], stats["dual_coverage_pct"],
        stats["coverage_pct_40"], stats["dual_coverage_pct_40"],
    )
    return stats


def camera_footprints_geojson(
    platepars: dict[str, dict],
    cam_to_station: dict[str, str],
    H: float = METEOR_ALT_KM,
) -> list[dict]:
    """Return GeoJSON-style feature dicts for the FOV map overlay.

    Args:
        platepars:       camera_code -> platepar dict
        cam_to_station:  camera_code (upper) -> host_key, for labelling footprints
        H:               ground altitude in km

    Returns:
        List of features — footprint features first, then cross-station overlap features.
        Each feature has ``geometry`` (GeoJSON Polygon) and ``properties``.
    """
    # Build per-camera footprints, keeping track of which station each belongs to.
    cam_footprints: list[tuple[str, str, Polygon]] = []  # (cam_code, station_id, polygon)
    for cam, pp in platepars.items():
        fp = camera_footprint(pp, H)
        if fp is None:
            continue
        station_id = cam_to_station.get(cam.upper())
        if station_id is None:
            continue
        cam_footprints.append((cam, station_id, fp))

    features: list[dict] = []

    # Per-camera footprint features.
    for cam, station_id, fp in cam_footprints:
        try:
            geom = mapping(fp)
        except Exception:
            continue
        features.append({
            "type":       "Feature",
            "geometry":   geom,
            "properties": {"feature_type": "footprint", "station_id": station_id, "camera": cam},
        })

    # Cross-station overlap features (only cameras from different stations).
    overlaps: list[Polygon] = []
    for i in range(len(cam_footprints)):
        for j in range(i + 1, len(cam_footprints)):
            if cam_footprints[i][1] == cam_footprints[j][1]:
                continue  # same station — skip
            try:
                inter = cam_footprints[i][2].intersection(cam_footprints[j][2])
                if not inter.is_empty:
                    overlaps.append(inter)
            except Exception:
                pass

    if overlaps:
        try:
            merged = unary_union(overlaps)
            # Normalise to a flat list of Polygons so the JS renderer never
            # has to deal with MultiPolygon geometries.
            polys = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
            for poly in polys:
                if poly.is_empty:
                    continue
                features.append({
                    "type":       "Feature",
                    "geometry":   mapping(poly),
                    "properties": {"feature_type": "overlap"},
                })
        except Exception as exc:
            logger.debug("camera_footprints_geojson overlaps: %s", exc)

    return features


# Keep the old single-value function for any callers outside public_api.
def get_coverage_pct(platepars: dict[str, dict]) -> float:
    return get_coverage_stats(platepars)["coverage_pct"]
