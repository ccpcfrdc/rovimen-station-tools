from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import threading

import numpy as np

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0
GRID_SPACING_KM = 10.0
MIN_ALT_DEG = 3.0
MAX_DIST_KM = 600.0
CACHE_TTL_S = 300.0

ALTITUDE_SLICES_DOUBLE = [25.0, 70.0, 100.0]
VOLUME_SLICES = [25.0, 40.0, 55.0, 70.0, 85.0, 100.0, 115.0, 130.0]
# Sporadic meteor flux for video cameras with ~+4.5 limiting magnitude.
# Derived from ZHR ~10/hr over ~30,000 km^2 effective area (Rendtel 2006),
# scaled from +6.5 to +4.5 by the population index: 10^(0.4 * 2) ≈ 6.3x fewer.
SPORADIC_FLUX = 5.3e-5
LIMITING_MAG = 4.5

_cache_lock = threading.Lock()
_cache: tuple[str, dict] | None = None  # (platepar_hash, result)


def _platepar_hash(platepars: dict[str, dict]) -> str:
    raw = json.dumps(platepars, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


def _deg2rad(d: float) -> float:
    return d * math.pi / 180.0


def _rad2deg(r: float) -> float:
    return r * 180.0 / math.pi


def _destination(lat0: float, lon0: float, bearing_deg: float, dist_km: float) -> tuple[float, float]:
    d = dist_km / EARTH_RADIUS_KM
    lat0r = _deg2rad(lat0)
    lon0r = _deg2rad(lon0)
    az = _deg2rad(bearing_deg)
    lat2 = math.asin(
        math.sin(lat0r) * math.cos(d) + math.cos(lat0r) * math.sin(d) * math.cos(az)
    )
    lon2 = lon0r + math.atan2(
        math.sin(az) * math.sin(d) * math.cos(lat0r),
        math.cos(d) - math.sin(lat0r) * math.sin(lat2),
    )
    return _rad2deg(lat2), _rad2deg(lon2)


def _fov_corners_at_altitude(
    clat: float,
    clon: float,
    az_centre: float,
    alt_centre: float,
    fov_h: float,
    fov_v: float,
    altitude_km: float,
) -> list[tuple[float, float]]:
    corners: list[tuple[float, float]] = []
    for dh, dv in [(-0.5, 0.5), (0.5, 0.5), (0.5, -0.5), (-0.5, -0.5)]:
        az = az_centre + dh * fov_h
        alt = alt_centre + dv * fov_v
        if alt < MIN_ALT_DEG:
            alt = MIN_ALT_DEG
        dist_km = altitude_km / math.tan(_deg2rad(alt))
        dist_km = min(dist_km, MAX_DIST_KM)
        lat, lon = _destination(clat, clon, az, dist_km)
        corners.append((lat, lon))
    return corners


def _build_camera_index(
    platepars: dict[str, dict], config: dict, romania_only: bool = False,
) -> tuple[dict[str, str], list[str]]:
    cam_to_station: dict[str, str] = {}
    stations_list: list[str] = []
    for station_id, sdata in config.get("stations", {}).items():
        if romania_only and not station_id.startswith("gmnro"):
            continue
        stations_list.append(station_id)
        for cam in sdata.get("cameras", []):
            code = cam.get("code")
            if code and code in platepars:
                cam_to_station[code] = station_id
    return cam_to_station, stations_list


def _grid_bounds(
    platepars: dict[str, dict], altitude_km: float
) -> tuple[float, float, float, float] | None:
    all_lats: list[float] = []
    all_lons: list[float] = []
    for pp in platepars.values():
        corners = _fov_corners_at_altitude(
            float(pp["lat"]),
            float(pp["lon"]),
            float(pp["az_centre"]),
            float(pp["alt_centre"]),
            float(pp["fov_h"]),
            float(pp["fov_v"]),
            altitude_km,
        )
        if corners:
            for lat, lon in corners:
                all_lats.append(lat)
                all_lons.append(lon)
    if not all_lats:
        return None
    margin = 0.5
    return (
        min(all_lats) - margin,
        max(all_lats) + margin,
        min(all_lons) - margin,
        max(all_lons) + margin,
    )


def _make_grid(
    lat_min: float, lat_max: float, lon_min: float, lon_max: float
) -> tuple[np.ndarray, np.ndarray, float]:
    mid_lat = (lat_min + lat_max) / 2.0
    dlat = GRID_SPACING_KM / (EARTH_RADIUS_KM * math.pi / 180.0)
    dlon = GRID_SPACING_KM / (EARTH_RADIUS_KM * math.pi / 180.0 * math.cos(_deg2rad(mid_lat)))
    lats = np.arange(lat_min, lat_max, dlat)
    lons = np.arange(lon_min, lon_max, dlon)
    grid_lon, grid_lat = np.meshgrid(lons, lats)
    cell_area_km2 = GRID_SPACING_KM * GRID_SPACING_KM
    return grid_lat, grid_lon, cell_area_km2


def _fov_polygon(
    clat: float, clon: float,
    az_centre: float, alt_centre: float,
    fov_h: float, fov_v: float,
    altitude_km: float,
    n_edge: int = 8,
) -> list[tuple[float, float]]:
    """Sample the FOV boundary as a polygon of ground points at altitude_km."""
    points: list[tuple[float, float]] = []
    edges = [
        [(-0.5, -0.5 + i / n_edge) for i in range(n_edge)],
        [(-0.5 + i / n_edge, 0.5) for i in range(n_edge)],
        [(0.5, 0.5 - i / n_edge) for i in range(n_edge)],
        [(0.5 - i / n_edge, -0.5) for i in range(n_edge)],
    ]
    for edge in edges:
        for dh_frac, dv_frac in edge:
            az = az_centre + dh_frac * fov_h
            alt = alt_centre + dv_frac * fov_v
            if alt < MIN_ALT_DEG:
                alt = MIN_ALT_DEG
            dist_km = altitude_km / math.tan(_deg2rad(alt))
            dist_km = min(dist_km, MAX_DIST_KM)
            lat, lon = _destination(clat, clon, az, dist_km)
            points.append((lat, lon))
    return points


def _point_in_polygon_vectorized(
    grid_lat: np.ndarray,
    grid_lon: np.ndarray,
    polygon: list[tuple[float, float]],
) -> np.ndarray:
    """Ray-casting point-in-polygon test, vectorized over the grid."""
    n = len(polygon)
    inside = np.zeros(grid_lat.shape, dtype=bool)
    for i in range(n):
        y1, x1 = polygon[i]
        y2, x2 = polygon[(i + 1) % n]
        cond = ((y1 > grid_lat) != (y2 > grid_lat)) & \
               (grid_lon < (x2 - x1) * (grid_lat - y1) / (y2 - y1 + 1e-30) + x1)
        inside ^= cond
    return inside


def _coverage_at_altitude(
    platepars: dict[str, dict],
    cam_to_station: dict[str, str],
    altitude_km: float,
    grid: tuple[np.ndarray, np.ndarray, float] | None = None,
) -> tuple[float, float, dict[str, float]]:
    if grid is not None:
        grid_lat, grid_lon, cell_area = grid
    else:
        bounds = _grid_bounds(platepars, altitude_km)
        if bounds is None:
            return 0.0, 0.0, {}
        lat_min, lat_max, lon_min, lon_max = bounds
        grid_lat, grid_lon, cell_area = _make_grid(lat_min, lat_max, lon_min, lon_max)

    station_masks: dict[str, np.ndarray] = {}

    for cam_code, pp in platepars.items():
        station_id = cam_to_station.get(cam_code)
        if station_id is None:
            continue
        fov_h = float(pp["fov_h"])
        fov_v = float(pp["fov_v"])
        if fov_h <= 0 or fov_v <= 0:
            logger.warning("network_stats: skipping %s (fov_h=%.1f, fov_v=%.1f)", cam_code, fov_h, fov_v)
            continue
        poly = _fov_polygon(
            float(pp["lat"]), float(pp["lon"]),
            float(pp["az_centre"]), float(pp["alt_centre"]),
            fov_h, fov_v, altitude_km,
        )
        mask = _point_in_polygon_vectorized(grid_lat, grid_lon, poly)
        if station_id in station_masks:
            station_masks[station_id] = station_masks[station_id] | mask
        else:
            station_masks[station_id] = mask.copy()

    if not station_masks:
        return 0.0, 0.0, {}

    any_coverage = np.zeros_like(grid_lat, dtype=bool)
    station_count = np.zeros_like(grid_lat, dtype=np.int32)
    for sid, smask in station_masks.items():
        any_coverage |= smask
        station_count += smask.astype(np.int32)

    total_area = float(np.sum(any_coverage)) * cell_area
    double_area = float(np.sum(station_count >= 2)) * cell_area

    per_station_area: dict[str, float] = {}
    for sid, smask in station_masks.items():
        per_station_area[sid] = float(np.sum(smask)) * cell_area

    return total_area, double_area, per_station_area


def _atmospheric_volume(
    platepars: dict[str, dict],
    cam_to_station: dict[str, str],
    grid: tuple[np.ndarray, np.ndarray, float] | None = None,
) -> float:
    areas: list[tuple[float, float]] = []
    for alt_km in VOLUME_SLICES:
        total_area, _, _ = _coverage_at_altitude(platepars, cam_to_station, alt_km, grid=grid)
        areas.append((alt_km, total_area))

    volume = 0.0
    for i in range(len(areas) - 1):
        h0, a0 = areas[i]
        h1, a1 = areas[i + 1]
        dh = h1 - h0
        volume += 0.5 * (a0 + a1) * dh
    return volume


def compute_network_stats(platepars: dict[str, dict], config: dict) -> dict:
    t0 = time.monotonic()

    cam_to_station, stations_list = _build_camera_index(platepars, config, romania_only=True)
    valid_cams = [c for c in cam_to_station if c in platepars]

    all_config_cams = set()
    for station_id, sdata in config.get("stations", {}).items():
        if not station_id.startswith("gmnro"):
            continue
        for cam in sdata.get("cameras", []):
            if cam.get("code"):
                all_config_cams.add(cam["code"])
    missing = all_config_cams - set(platepars.keys())
    if missing:
        logger.warning("network_stats: %d cameras without platepars: %s",
                        len(missing), ", ".join(sorted(missing)))

    if not valid_cams:
        logger.warning("network_stats: no valid cameras with platepars")
        return _empty_result(config)

    # Build grid once from the widest altitude (100 km) and reuse for all slices
    bounds = _grid_bounds(platepars, 100.0)
    if bounds is None:
        return _empty_result(config)
    grid = _make_grid(*bounds)

    total_area_100, double_area_100, per_station_100 = _coverage_at_altitude(
        platepars, cam_to_station, 100.0, grid=grid
    )

    double_by_altitude: dict[str, float] = {}
    for alt_km in ALTITUDE_SLICES_DOUBLE:
        if alt_km == 100.0:
            double_by_altitude[f"{int(alt_km)}km"] = double_area_100
        else:
            _, dbl, _ = _coverage_at_altitude(platepars, cam_to_station, alt_km, grid=grid)
            double_by_altitude[f"{int(alt_km)}km"] = dbl

    volume = _atmospheric_volume(platepars, cam_to_station, grid=grid)

    expected_flux_per_hour = SPORADIC_FLUX * double_area_100

    stations_config = {k: v for k, v in config.get("stations", {}).items() if k.startswith("gmnro")}
    total_cameras = sum(
        len(sdata.get("cameras", [])) for sdata in stations_config.values()
    )
    cameras_with_platepar = len(valid_cams)

    per_station: dict[str, dict] = {}
    for station_id, sdata in stations_config.items():
        cam_codes = [c.get("code") for c in sdata.get("cameras", [])]
        n_cams = len(cam_codes)
        area = per_station_100.get(station_id, 0.0)
        per_station[station_id] = {
            "label": sdata.get("label", station_id),
            "cameras": n_cams,
            "cameras_with_platepar": sum(1 for c in cam_codes if c in platepars),
            "covered_area_km2": round(area, 1),
        }

    elapsed = time.monotonic() - t0
    logger.info("network_stats: computed in %.2fs", elapsed)

    return {
        "total_covered_area_km2": round(total_area_100, 1),
        "double_station_coverage_km2": {k: round(v, 1) for k, v in double_by_altitude.items()},
        "atmospheric_volume_km3": round(volume, 1),
        "expected_meteoric_flux_per_hour": round(expected_flux_per_hour, 1),
        "network_summary": {
            "total_stations": len(stations_config),
            "total_cameras": total_cameras,
            "cameras_with_platepar": cameras_with_platepar,
        },
        "per_station": per_station,
        "computation_time_s": round(elapsed, 2),
    }


def _empty_result(config: dict) -> dict:
    stations_config = config.get("stations", {})
    total_cameras = sum(
        len(sdata.get("cameras", [])) for sdata in stations_config.values()
    )
    per_station = {}
    for station_id, sdata in stations_config.items():
        per_station[station_id] = {
            "label": sdata.get("label", station_id),
            "cameras": len(sdata.get("cameras", [])),
            "cameras_with_platepar": 0,
            "covered_area_km2": 0.0,
        }
    return {
        "total_covered_area_km2": 0.0,
        "double_station_coverage_km2": {f"{int(a)}km": 0.0 for a in ALTITUDE_SLICES_DOUBLE},
        "atmospheric_volume_km3": 0.0,
        "expected_meteoric_flux_per_hour": 0.0,
        "network_summary": {
            "total_stations": len(stations_config),
            "total_cameras": total_cameras,
            "cameras_with_platepar": 0,
        },
        "per_station": per_station,
        "computation_time_s": 0.0,
    }


_computing = False

def compute_coverage_grid(
    platepars: dict[str, dict], config: dict, altitude_km: float = 100.0
) -> list[dict]:
    """Return grid cells with their station count for map overlay rendering."""
    cam_to_station, _ = _build_camera_index(platepars, config)
    valid_cams = [c for c in cam_to_station if c in platepars]
    if not valid_cams:
        return []

    bounds = _grid_bounds(platepars, altitude_km)
    if bounds is None:
        return []
    grid_lat, grid_lon, cell_area = _make_grid(*bounds)

    station_masks: dict[str, np.ndarray] = {}
    for cam_code, pp in platepars.items():
        station_id = cam_to_station.get(cam_code)
        if station_id is None:
            continue
        fov_h = float(pp["fov_h"])
        fov_v = float(pp["fov_v"])
        if fov_h <= 0 or fov_v <= 0:
            continue
        poly = _fov_polygon(
            float(pp["lat"]), float(pp["lon"]),
            float(pp["az_centre"]), float(pp["alt_centre"]),
            fov_h, fov_v, altitude_km,
        )
        mask = _point_in_polygon_vectorized(grid_lat, grid_lon, poly)
        if station_id in station_masks:
            station_masks[station_id] = station_masks[station_id] | mask
        else:
            station_masks[station_id] = mask.copy()

    if not station_masks:
        return []

    station_count = np.zeros_like(grid_lat, dtype=np.int32)
    for smask in station_masks.values():
        station_count += smask.astype(np.int32)

    cells = []
    indices = np.argwhere(station_count >= 1)
    for r, c in indices:
        cells.append({
            "lat": round(float(grid_lat[r, c]), 4),
            "lon": round(float(grid_lon[r, c]), 4),
            "n": int(station_count[r, c]),
        })
    return cells


_coverage_cache_lock = threading.Lock()
_coverage_cache: dict[str, list[dict]] = {}  # key: "hash:alt" -> cells


def get_coverage_grid(
    platepars: dict[str, dict], config: dict, altitude_km: float = 100.0
) -> list[dict]:
    h = _platepar_hash(platepars)
    key = f"{h}:{altitude_km}"
    with _coverage_cache_lock:
        if key in _coverage_cache:
            return _coverage_cache[key]
    result = compute_coverage_grid(platepars, config, altitude_km)
    with _coverage_cache_lock:
        _coverage_cache.clear()
        _coverage_cache[key] = result
    return result


_computing = False

def get_network_stats(platepars: dict[str, dict], config: dict) -> dict:
    global _cache, _computing
    h = _platepar_hash(platepars)
    with _cache_lock:
        if _cache and _cache[0] == h:
            return _cache[1]
        if _computing and _cache:
            return _cache[1]
        _computing = True
    try:
        result = compute_network_stats(platepars, config)
        with _cache_lock:
            _cache = (h, result)
        return result
    finally:
        with _cache_lock:
            _computing = False


_precompute_config: dict | None = None


def register_precompute(config: dict) -> None:
    """Register config and subscribe to platepar changes for background precomputation."""
    global _precompute_config
    _precompute_config = config
    import platepar_store
    platepar_store.on_change(_on_platepar_change)
    threading.Thread(target=_precompute_all, daemon=True).start()


def _on_platepar_change() -> None:
    threading.Thread(target=_precompute_all, daemon=True).start()


def _precompute_all() -> None:
    if not _precompute_config:
        return
    import platepar_store
    pp = platepar_store.get_all()
    if not pp:
        return
    logger.info("network_stats: precomputing (platepar change detected, %d cameras)", len(pp))
    get_network_stats(pp, _precompute_config)
    for alt in ALTITUDE_SLICES_DOUBLE:
        get_coverage_grid(pp, _precompute_config, alt)
