"""Cache infrastructure -- thumbnail cache, disk persistence, archive index."""

import io
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from models import DashboardConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bounded per-process LRU cache
# ---------------------------------------------------------------------------
#
# WHY: every gunicorn worker is a full process with its OWN in-heap caches.
# The Redis backend (#637) shares status/vitals/rate-limit/live-thumb across
# workers, but NOT the prefetch dedup sets, the platepar/timelapse SWR caches,
# etc. Those were bounded only by age (14-day prune in the prefetch loop) or
# not at all, so a single worker's RSS climbed with uptime × cameras × nights.
# Two workers under load spiked the ~15 GB dev box into the OOM killer, which
# is why GUNICORN_WORKERS>1 was not deployable.
#
# This is a drop-in replacement for the plain ``dict`` those caches used: it
# subclasses ``OrderedDict`` so ``cache[k] = v`` / ``.get`` / ``.pop`` /
# ``len`` / iteration are unchanged, but every insert moves the key to the MRU
# end and, once the cap is exceeded, evicts the least-recently-used entry.
# With a generous cap (default 512 — the "cache sizes" log showed tens of
# entries in normal operation) nothing user-visible changes at current scale;
# the cap only bites when a cache would otherwise grow without bound.
#
# NOT thread-safe on its own: callers already serialise access under their
# existing per-cache lock (``_prefetched_lock``, ``_rms_plots_list_cache_lock``,
# the platepar/timelapse locks, …). Eviction happens inside ``__setitem__`` so
# it runs under that same held lock — do not add a second lock here or you
# risk lock-ordering surprises with the caller's.

def _cache_cap(env_var: str, default: int) -> int:
    """Read a cache cap from ``env_var``, falling back to ``default``.

    A non-positive or unparseable value disables the cap (cap of 0 means
    "unbounded"), matching the disk-cache "0 == disabled" convention used
    elsewhere. Caps are read once at construction so a worker's bound is
    fixed for its lifetime.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r; using default %d",
                       env_var, raw, default)
        return default
    return max(0, val)


class BoundedCache(OrderedDict):
    """OrderedDict with a hard entry cap and LRU eviction on insert.

    ``maxsize <= 0`` disables eviction (behaves like a plain dict). Reads via
    ``get``/``__getitem__`` do NOT promote (kept cheap and lock-light); recency
    is defined by insert order, which is what all our callers do on every
    refresh anyway (they re-``__setitem__`` the entry each time they touch it).
    """

    def __init__(self, maxsize: int, *args: Any, **kwargs: Any) -> None:
        self._maxsize = maxsize
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            # Refresh recency: move an existing key to the MRU end so it isn't
            # the next thing evicted.
            self.move_to_end(key)
        super().__setitem__(key, value)
        if self._maxsize > 0:
            while len(self) > self._maxsize:
                self.popitem(last=False)  # drop the LRU (oldest-inserted) entry


# ---------------------------------------------------------------------------
# Thumbnail cache
# ---------------------------------------------------------------------------

# Hot tier: local NVMe SSD for the last few nights.  94.7% of thumbnail
# requests hit the last 3 days, so a short retention keeps the SSD small
# while nginx X-Accel-Redirect serves them at <1 ms.
THUMB_CACHE_DIR = Path(os.environ.get("THUMB_CACHE_DIR", "/opt/rovimen/thumb_cache"))
THUMB_CACHE_DAYS = 3

# When nginx is in front, use X-Accel-Redirect so nginx serves cached files
# directly from disk without going through Python.
NGINX_ACCEL = os.environ.get("NGINX_ACCEL") == "1"

# Refuse to write to the thumbnail cache when free space drops below this
# threshold. The cache lives on NVMe; if it fills, every subsequent write
# 500s the worker and we lose the request entirely. Better to serve the
# bytes once and skip the cache promotion than to crash on disk-full.
_THUMB_CACHE_MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MB
_thumb_cache_full_warn_lock = threading.Lock()
_thumb_cache_full_last_warn: float = 0.0


def _thumb_cache_has_space() -> bool:
    """Return True if THUMB_CACHE_DIR has enough free space to write a thumb.

    Logs a WARN at most once per hour when the cache is full so we notice
    the problem without flooding logs on every request.
    """
    global _thumb_cache_full_last_warn
    try:
        free = shutil.disk_usage(THUMB_CACHE_DIR).free
    except OSError:
        # If we can't stat the cache (e.g. SSHFS dropped), assume it's fine
        # and let the write attempt surface a clearer error.
        return True
    if free >= _THUMB_CACHE_MIN_FREE_BYTES:
        return True
    now = time.monotonic()
    with _thumb_cache_full_warn_lock:
        if now - _thumb_cache_full_last_warn > 3600:
            logger.warning(
                "Thumb cache low on space (%.1f MB free); skipping writes",
                free / (1024 * 1024),
            )
            _thumb_cache_full_last_warn = now
    return False


def _thumb_cache_path(host_key: str, cam: str, date: str, filename: str) -> Path:
    return THUMB_CACHE_DIR / host_key / cam / date / filename


def _thumb_archive_path(host_key: str, cam: str, date: str, filename: str) -> Path | None:
    if THUMB_ARCHIVE_DIR is None:
        return None
    return THUMB_ARCHIVE_DIR / host_key / cam / date / filename


def _downsample_thumbnail_bytes(data: bytes, max_dim: int = 480) -> bytes:
    """Re-encode an image to fit within a ``max_dim``-px bounding box,
    preserving aspect ratio. WebP-encoded output at quality 80, method 4
    (size/speed knee). Returns the input unchanged when it's already
    within bounds OR when decode fails (caller must remain robust to
    pass-through). Caller is responsible for only invoking this on
    paths that are conceptually thumbnails -- full-resolution endpoints
    (e.g. /fullstack/...) must NOT call this.

    Context: some stations send a 480x300 thumbnail (~5 KB) while others
    send a 1280x720 full stack (~230 KB). Without this clamp the VPS
    cache fills with full-resolution stacks and exhausts the volume.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.width <= max_dim and img.height <= max_dim:
            return data
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=80, method=4)
        return buf.getvalue()
    except Exception:
        return data


def _rms_plot_cache_path(host_key: str, cam: str, date: str, filename: str) -> Path:
    """Disk cache for RMS plot images (captured_stack, meteor_stack, radiants, ff_intervals).

    Lives at THUMB_CACHE_DIR/<host>/rms_plots/<cam>/<date>/<filename> so it falls
    under the same prune. Same path used by api_rms_plot_image_proxy.
    """
    return THUMB_CACHE_DIR / host_key / "rms_plots" / cam / date / filename


def _rms_plots_list_cache_path(host_key: str, cam: str, date: str) -> Path:
    """JSON cache for the LIST of available plot files per (host, cam, date).

    Persisted to disk so it survives dashboard restarts and is servable when
    the station goes offline.
    """
    return THUMB_CACHE_DIR / host_key / "rms_plots" / cam / date / "_plots.json"


def _prune_thumb_cache() -> None:
    """Hourly prune of the SSD hot cache.  Expired night-dirs (older than
    THUMB_CACHE_DAYS) are MOVED to the cold-tier storage box rather than
    deleted, so historical thumbnails remain available via the cold lookup
    in ``_serve_thumbnail``.

    If THUMB_ARCHIVE_DIR is unset the old behaviour (plain delete) is
    preserved.  The cold tier has unlimited retention.
    """
    while True:
        time.sleep(3600)
        try:
            cutoff_date = (
                datetime.now(timezone.utc) - timedelta(days=THUMB_CACHE_DAYS)
            ).strftime("%Y%m%d")
            pattern = "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"
            promoted = 0
            for d in THUMB_CACHE_DIR.rglob(pattern):
                try:
                    if not d.is_dir():
                        continue
                    if d.name >= cutoff_date:
                        continue
                    if THUMB_ARCHIVE_DIR is not None:
                        promoted += _promote_dir_to_archive(d)
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    continue
            if promoted:
                logger.info("thumb prune: promoted %d files to cold tier", promoted)
        except Exception:
            logger.exception("thumb cache prune error")


def _promote_dir_to_archive(src_dir: Path) -> int:
    """Copy all files from an SSD night-dir to the matching cold-tier path.
    Skips files that already exist on the cold tier.  Returns count of
    files copied."""
    rel = src_dir.relative_to(THUMB_CACHE_DIR)
    dst_dir = THUMB_ARCHIVE_DIR / rel
    count = 0
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    for f in src_dir.iterdir():
        if not f.is_file():
            continue
        dst = dst_dir / f.name
        if dst.exists():
            continue
        try:
            tmp = dst.with_suffix(dst.suffix + ".part")
            shutil.copy2(f, tmp)
            tmp.replace(dst)
            count += 1
        except OSError:
            continue
    return count


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------

def load_config(path: Path) -> DashboardConfig:
    raw = yaml.safe_load(path.read_text())
    return DashboardConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Archive + cache paths
# ---------------------------------------------------------------------------

ARCHIVE_PATH = Path(os.environ.get("ROVIMEN_ARCHIVE_PATH", "/srv/rovimen/archive"))
CACHE_PATH = Path(os.environ.get("ROVIMEN_CACHE_PATH", "/opt/rovimen/cache"))
COMPILATIONS_OUT_PATH = Path(
    os.environ.get("ROVIMEN_COMPILATIONS_OUT_PATH", "/opt/rovimen/compilations")
)
COMPILATIONS_ARCHIVE_PATH = ARCHIVE_PATH / "compilations"
INTRO_PATH = Path(os.environ.get("ROVIMEN_INTRO_PATH", "/opt/rovimen/intro.mp4"))
CACHE_TTL_HOURS = 24

# Cold tier: storage-box thumb archive (unlimited retention).  Populated by
# the hourly prune which MOVEs expired SSD thumbs here instead of deleting.
_thumb_archive_env = os.environ.get("THUMB_ARCHIVE_DIR", "")
THUMB_ARCHIVE_DIR: Path | None = (
    Path(_thumb_archive_env) if _thumb_archive_env
    else ARCHIVE_PATH / ".thumb_cache"
)


# ---------------------------------------------------------------------------
# On-disk persistence for the slow SSHFS-walk caches
# ---------------------------------------------------------------------------
#
# Without this, the dashboard cold-starts on every restart: the first user
# to open the Archive or VideoDB tab pays a 5-20 s SSHFS walk per camera --
# on a 24-cam fleet that's up to 8 min of intermittent slow loads while the
# startup pre-warm catches up. With persistence the in-memory cache is
# rehydrated from JSON the instant the process starts, so users see data
# immediately and a background refresh quietly replaces it when stale.
#
# Disk shape: {"saved_at": wall_clock_seconds, "data": <cache value>}.
# Wall-clock time is used (not monotonic) so age survives process restarts.

_CACHE_REFRESH_EXECUTOR: ThreadPoolExecutor | None = None
_CACHE_REFRESH_LOCK = threading.Lock()
_CACHE_REFRESH_INFLIGHT: dict[str, set[str]] = {}


def _disk_cache_dir(name: str) -> Path:
    return CACHE_PATH / "walk_cache" / name


def _disk_cache_path(name: str, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
    return _disk_cache_dir(name) / f"{safe}.json"


def _disk_cache_write(name: str, key: str, data: Any) -> None:
    try:
        path = _disk_cache_path(name, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"saved_at": time.time(), "data": data}))
        tmp.replace(path)
    except Exception:
        logger.exception("disk-cache write failed for %s/%s", name, key)


def _disk_cache_load_into(
    name: str, target: dict[str, tuple[float, Any]]
) -> int:
    """Rehydrate the in-memory cache `target` from disk. Returns the count
    of entries loaded. The monotonic timestamp is back-dated by the file's
    wall-clock age so the existing fresh/stale TTL logic treats the entry
    exactly as it would have if populated at that moment."""
    base = _disk_cache_dir(name)
    if not base.is_dir():
        return 0
    now_wall = time.time()
    now_mono = time.monotonic()
    loaded = 0
    for f in base.glob("*.json"):
        try:
            blob = json.loads(f.read_text())
            saved_at = float(blob["saved_at"])
            data = blob["data"]
        except Exception:
            continue
        age = max(0.0, now_wall - saved_at)
        target[f.stem] = (now_mono - age, data)
        loaded += 1
    return loaded


def _kick_cache_refresh(name: str, key: str, refresh_fn) -> None:
    """Run refresh_fn() in a background thread, coalescing concurrent
    kicks for the same (name, key) pair. The first request that finds a
    stale entry triggers the refresh; subsequent requests during the
    refresh window keep serving the stale entry rather than piling on.
    """
    global _CACHE_REFRESH_EXECUTOR
    with _CACHE_REFRESH_LOCK:
        if _CACHE_REFRESH_EXECUTOR is None:
            _CACHE_REFRESH_EXECUTOR = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="cache-refresh"
            )
        inflight = _CACHE_REFRESH_INFLIGHT.setdefault(name, set())
        if key in inflight:
            return
        inflight.add(key)

    def _run() -> None:
        try:
            refresh_fn()
        except Exception:
            logger.exception("background cache refresh failed for %s/%s", name, key)
        finally:
            with _CACHE_REFRESH_LOCK:
                _CACHE_REFRESH_INFLIGHT.get(name, set()).discard(key)

    _CACHE_REFRESH_EXECUTOR.submit(_run)


def _drop_future_dates(dates: list[str]) -> list[str]:
    """Filter a list of YYYYMMDD strings to drop any > today's UTC date.

    Captured nights cannot exist in the future. Defensive filter so that any
    stray future-dated directory on disk (clock skew during a restore, a
    manually-created test dir, etc.) never reaches the UI as a selectable
    date. The client also applies a `max=today` attribute on date pickers --
    this is the server-side belt to its client-side suspenders.
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return [d for d in dates if isinstance(d, str) and d <= today]


# ---------------------------------------------------------------------------
# Archive index -- in-memory mirror of the storagebox directory tree
# ---------------------------------------------------------------------------

_CAM_RE = re.compile(r"^[A-Z0-9]+$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{8}$")


@dataclass(slots=True)
class _NightIndex:
    """Pre-scanned file listing for one camera/date in the archive."""
    meteor_files: tuple[str, ...]
    stack_files: frozenset[str]
    timelapse_files: tuple[str, ...]
    state_chunks: dict = None


_ARCHIVE_HOT_DAYS = 3
_ARCHIVE_ONDEMAND_TTL = 120  # seconds


class _ArchiveIndex:
    """Thread-safe in-memory index of the storagebox directory tree.

    Only the last ``_ARCHIVE_HOT_DAYS`` days keep full file listings
    in RAM. Older nights are recorded as date-only entries (no file
    lists) and scanned from SSHFS on demand with a short TTL cache.
    This keeps steady-state memory under ~20 MB instead of ~300 MB.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Hot data: full file listings for recent nights only
        self._data: dict[str, dict[str, _NightIndex]] = {}
        # Lightweight date catalogue: cam -> set of all known date strings
        self._dates: dict[str, set[str]] = {}
        # On-demand cache for older nights: (cam, date) -> (expiry, _NightIndex)
        self._ondemand: dict[tuple[str, str], tuple[float, _NightIndex]] = {}
        # Tracks which dates have meteors (for nights_with_meteors)
        self._has_meteors: dict[str, set[str]] = {}
        self.ready = False

    @staticmethod
    def _hot_cutoff() -> str:
        return (datetime.now(timezone.utc) - timedelta(days=_ARCHIVE_HOT_DAYS)).strftime("%Y%m%d")

    # -- read accessors (called from request threads) ---------------------

    def cameras(self) -> list[str]:
        with self._lock:
            return sorted(
                c for c in self._dates if _CAM_RE.match(c)
            )

    def nights(self, cam: str) -> list[str]:
        with self._lock:
            dates = self._dates.get(cam)
            if not dates:
                return []
            return sorted(dates, reverse=True)

    def nights_with_meteors(self, cam: str) -> list[str]:
        with self._lock:
            dates = self._has_meteors.get(cam)
            if not dates:
                return []
            return sorted(dates, reverse=True)

    def night_files(self, cam: str, date: str) -> _NightIndex | None:
        with self._lock:
            dates = self._data.get(cam)
            if dates:
                ni = dates.get(date)
                if ni is not None:
                    return ni
            # Check on-demand cache
            key = (cam, date)
            cached = self._ondemand.get(key)
            if cached and cached[0] > time.time():
                return cached[1]
        # Not in hot data or on-demand cache — scan from SSHFS with timeout
        from station_client import _with_sshfs_timeout
        night_path = ARCHIVE_PATH / cam / date
        ni = _with_sshfs_timeout(
            lambda p=str(night_path): self._scan_night(p) if Path(p).is_dir() else None,
            timeout=5.0,
        )
        if ni is not None:
            with self._lock:
                self._ondemand[(cam, date)] = (time.time() + _ARCHIVE_ONDEMAND_TTL, ni)
        return ni

    def all_night_files(self, cam: str, date: str) -> set[str]:
        ni = self.night_files(cam, date)
        if ni is None:
            return set()
        return set(ni.meteor_files) | set(ni.stack_files) | set(ni.timelapse_files)

    def is_built(self) -> bool:
        with self._lock:
            return len(self._dates) > 0

    # -- writers (called from background threads) -------------------------

    def build_full(self) -> int:
        """Full scan of ARCHIVE_PATH. Returns count of nights indexed."""
        if not ARCHIVE_PATH.exists():
            return 0
        try:
            with os.scandir(ARCHIVE_PATH) as it:
                cam_entries = [
                    e.name for e in it
                    if e.is_dir(follow_symlinks=False)
                    and _CAM_RE.match(e.name)
                    and e.name != "compilations"
                ]
        except OSError:
            logger.warning("archive index: cannot scan %s", ARCHIVE_PATH)
            return 0

        cutoff = self._hot_cutoff()
        new_data: dict[str, dict[str, _NightIndex]] = {}
        new_dates: dict[str, set[str]] = {}
        new_has_meteors: dict[str, set[str]] = {}
        total = 0

        def _scan_camera(cam: str) -> tuple[
            str, dict[str, _NightIndex], set[str], set[str]
        ]:
            cam_dir = ARCHIVE_PATH / cam
            hot: dict[str, _NightIndex] = {}
            all_dates: set[str] = set()
            meteor_dates: set[str] = set()
            try:
                with os.scandir(cam_dir) as it:
                    for entry in it:
                        if not (entry.is_dir(follow_symlinks=False)
                                and _DATE_RE.match(entry.name)):
                            continue
                        all_dates.add(entry.name)
                        if entry.name >= cutoff:
                            ni = self._scan_night(entry.path)
                            if ni is not None:
                                hot[entry.name] = ni
                                if any(f.endswith((".mkv", ".mp4"))
                                       for f in ni.meteor_files):
                                    meteor_dates.add(entry.name)
                        else:
                            # Older night: just check if meteors/ has files
                            meteors_dir = os.path.join(entry.path, "meteors")
                            try:
                                with os.scandir(meteors_dir) as mit:
                                    for f in mit:
                                        if (f.is_file(follow_symlinks=False)
                                                and f.name.endswith(
                                                    (".mkv", ".mp4"))):
                                            meteor_dates.add(entry.name)
                                            break
                            except OSError:
                                pass
            except OSError:
                pass
            return cam, hot, all_dates, meteor_dates

        with ThreadPoolExecutor(max_workers=4) as pool:
            for cam, hot, all_dates, meteor_dates in pool.map(
                lambda c: _scan_camera(c), cam_entries
            ):
                new_data[cam] = hot
                new_dates[cam] = all_dates
                new_has_meteors[cam] = meteor_dates
                total += len(all_dates)

        with self._lock:
            self._data = new_data
            self._dates = new_dates
            self._has_meteors = new_has_meteors
            self._ondemand.clear()
            if total:
                self.ready = True
        return total

    def refresh_recent(self, days: int = 3) -> int:
        """Re-scan only dates >= cutoff. Also discovers new cameras and dates.
        Parallelized across cameras like build_full. Updates applied atomically
        per batch so readers never see partial state."""
        if not ARCHIVE_PATH.exists():
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
        try:
            with os.scandir(ARCHIVE_PATH) as it:
                cam_entries = [
                    e.name for e in it
                    if e.is_dir(follow_symlinks=False)
                    and _CAM_RE.match(e.name)
                    and e.name != "compilations"
                ]
        except OSError:
            return 0

        def _refresh_cam(cam: str) -> tuple[
            dict[str, _NightIndex], set[str], set[str]
        ]:
            cam_dir = ARCHIVE_PATH / cam
            updates: dict[str, _NightIndex] = {}
            new_dates: set[str] = set()
            meteor_dates: set[str] = set()
            try:
                with os.scandir(cam_dir) as it:
                    for entry in it:
                        if not (entry.is_dir(follow_symlinks=False)
                                and _DATE_RE.match(entry.name)):
                            continue
                        new_dates.add(entry.name)
                        if entry.name < cutoff:
                            continue
                        ni = self._scan_night(entry.path)
                        if ni is not None:
                            updates[entry.name] = ni
                            if any(f.endswith((".mkv", ".mp4"))
                                   for f in ni.meteor_files):
                                meteor_dates.add(entry.name)
            except OSError:
                pass
            return updates, new_dates, meteor_dates

        all_updates: dict[str, tuple[
            dict[str, _NightIndex], set[str], set[str]
        ]] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            future_to_cam = {pool.submit(_refresh_cam, c): c for c in cam_entries}
            for fut in as_completed(future_to_cam):
                cam = future_to_cam[fut]
                try:
                    result = fut.result()
                    all_updates[cam] = result
                except Exception:
                    pass

        count = 0
        hot_cutoff = self._hot_cutoff()
        with self._lock:
            for cam, (updates, new_dates, meteor_dates) in all_updates.items():
                if cam not in self._data:
                    self._data[cam] = {}
                if cam not in self._dates:
                    self._dates[cam] = set()
                if cam not in self._has_meteors:
                    self._has_meteors[cam] = set()
                self._data[cam].update(updates)
                self._dates[cam] |= new_dates
                self._has_meteors[cam] |= meteor_dates
                count += len(updates)
            # Evict stale hot entries
            for cam in list(self._data):
                self._data[cam] = {
                    d: ni for d, ni in self._data[cam].items()
                    if d >= hot_cutoff
                }
            # Evict expired on-demand entries
            now = time.time()
            self._ondemand = {
                k: v for k, v in self._ondemand.items() if v[0] > now
            }
        return count

    # -- internal helpers -------------------------------------------------

    @staticmethod
    def _scan_night(night_path: str) -> _NightIndex | None:
        """Scan meteors/, stacks/, timelapse/ under a single night dir."""
        meteor_files: list[str] = []
        stack_files: list[str] = []
        timelapse_files: list[str] = []
        for subdir, target in (("meteors", meteor_files), ("stacks", stack_files),
                               ("timelapse", timelapse_files)):
            path = os.path.join(night_path, subdir)
            try:
                with os.scandir(path) as it:
                    for f in it:
                        if f.is_file(follow_symlinks=False):
                            target.append(f.name)
            except OSError:
                continue
        if not meteor_files and not stack_files and not timelapse_files:
            return None
        state_chunks: dict | None = None
        state_path = os.path.join(night_path, "state.json")
        try:
            with open(state_path) as fh:
                state_chunks = json.load(fh).get("chunks", {})
        except (OSError, json.JSONDecodeError, KeyError):
            pass
        return _NightIndex(
            meteor_files=tuple(sorted(meteor_files)),
            stack_files=frozenset(stack_files),
            timelapse_files=tuple(sorted(timelapse_files)),
            state_chunks=state_chunks,
        )

    def save_to_disk(self, path: Path) -> None:
        """Persist only hot data + date catalogue for fast startup."""
        data: dict[str, dict[str, dict[str, list[str]]]] = {}
        with self._lock:
            for cam, nights in self._data.items():
                data[cam] = {}
                for date, ni in nights.items():
                    entry = {
                        "m": list(ni.meteor_files),
                        "s": sorted(ni.stack_files),
                        "t": list(ni.timelapse_files),
                    }
                    if ni.state_chunks is not None:
                        entry["c"] = ni.state_chunks
                    data[cam][date] = entry
            # Persist date catalogue and meteor flags as lightweight entries
            for cam, dates in self._dates.items():
                if cam not in data:
                    data[cam] = {}
                meteor_dates = self._has_meteors.get(cam, set())
                for d in dates:
                    if d not in data[cam]:
                        entry: dict[str, list[str]] = {"m": [], "s": [], "t": []}
                        if d in meteor_dates:
                            entry["_meteor"] = []
                        data[cam][d] = entry
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")))
            tmp.rename(path)
        except OSError:
            logger.warning("archive index: failed to save to %s", path)

    def load_from_disk(self, path: Path) -> int:
        """Load index from JSON. Only recent nights get full file lists."""
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        cutoff = self._hot_cutoff()
        new_data: dict[str, dict[str, _NightIndex]] = {}
        new_dates: dict[str, set[str]] = {}
        new_has_meteors: dict[str, set[str]] = {}
        total = 0
        for cam, nights in raw.items():
            new_data[cam] = {}
            new_dates[cam] = set()
            new_has_meteors[cam] = set()
            for date, files in nights.items():
                new_dates[cam].add(date)
                total += 1
                has_meteor_flag = "_meteor" in files
                m_files = files.get("m", ())
                if has_meteor_flag or any(
                    f.endswith((".mkv", ".mp4")) for f in m_files
                ):
                    new_has_meteors[cam].add(date)
                if date >= cutoff and (m_files or files.get("s") or files.get("t")):
                    new_data[cam][date] = _NightIndex(
                        meteor_files=tuple(m_files),
                        stack_files=frozenset(files.get("s", ())),
                        timelapse_files=tuple(files.get("t", ())),
                        state_chunks=files.get("c"),
                    )
        with self._lock:
            self._data = new_data
            self._dates = new_dates
            self._has_meteors = new_has_meteors
            if total:
                self.ready = True
        return total
