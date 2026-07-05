#!/usr/bin/env python3
"""rovimen_dashboard.py — Real-time web dashboard for ROVIMEN GMN stations.

Proxies data from station REST APIs (rovimen_station_api.py on port 7779).
Station config is loaded from dashboard_config.yaml — no hardcoded IPs or paths.
Auto-deployed from GitHub via CI.

For stations on unreachable networks, SSH tunnels are established through
jump hosts (other stations) with automatic failover.

Usage:
    python3 rovimen_dashboard.py [--port 7777] [--config dashboard_config.yaml]
    Open: http://localhost:7777
"""

import argparse
import copy
from dataclasses import dataclass
import fcntl
import functools
from contextlib import contextmanager
import hashlib
import io
import json
import logging
import math
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (
    Flask,
    Response,
    abort,
    after_this_request,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    send_file,
    send_from_directory,
    url_for,
)
from pydantic import BaseModel, Field

import cf_access
import gmn_data
from http_caching import _json_cached_payload_tag, _json_cached  # noqa: F401
from route_helpers import COLOR_METEOR_STACK_FILENAME, compute_detection_offset, media_url
import security

# tools/celestial_dome.py ships the per-station hemispheric reprojection used
# by /api/sky_dome. The deploy workflow drops the file next to the dashboard
# modules on the VPS so plain `from celestial_dome import ...` works there;
# in a local checkout the tool still lives under tools/, so add that to
# sys.path as a fallback.
import sys as _sys
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if _TOOLS_DIR.is_dir() and str(_TOOLS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TOOLS_DIR))

logger = logging.getLogger(__name__)

from astronomy import (  # noqa: E402
    BUILD_VERSION,
    _compute_build_version,
    _compute_twilight,
    _compute_moon_phase,
    _compute_moon_rise_set,
)


# ---------------------------------------------------------------------------
# Cache infrastructure — extracted to dashboard/cache_store.py
# ---------------------------------------------------------------------------

from cache_store import (  # noqa: F401,E402
    BoundedCache,
    _cache_cap,
    THUMB_CACHE_DIR,
    THUMB_CACHE_DAYS,
    NGINX_ACCEL,
    _THUMB_CACHE_MIN_FREE_BYTES,
    _thumb_cache_has_space,
    _thumb_cache_path,
    _downsample_thumbnail_bytes,
    _rms_plot_cache_path,
    _rms_plots_list_cache_path,
    _prune_thumb_cache,
    load_config,
    ARCHIVE_PATH,
    CACHE_PATH,
    CACHE_TTL_HOURS,
    _CACHE_REFRESH_EXECUTOR,
    _CACHE_REFRESH_LOCK,
    _CACHE_REFRESH_INFLIGHT,
    _disk_cache_dir,
    _disk_cache_path,
    _disk_cache_write,
    _disk_cache_load_into,
    _kick_cache_refresh,
    _drop_future_dates,
    _NightIndex,
    _ArchiveIndex,
)


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------
#
# The Pydantic dataclasses (CameraConfig / StationConfig / DashboardConfig /
# UserConfig) now live in ``dashboard/models.py`` so that public_api.py and
# any future extracted sub-modules can pull them in without depending on this
# god-module. They are re-exported here so external callers keep working.

from models import (  # noqa: E402  (kept next to the section they document)
    CameraConfig,
    DashboardConfig,
    StationConfig,
    UserConfig,
)


# ---------------------------------------------------------------------------
# Pooled HTTP sessions, station API client, polling cache
# ---------------------------------------------------------------------------
# Extracted to ``station_client.py`` — re-exported here for backward compat.
from station_client import (  # noqa: F401,E402
    _station_sessions,
    _station_sessions_lock,
    _get_station_session,
    _session_for_url,
    _evict_station_session,
    _streaming_proxy,
    _with_sshfs_timeout,
    _sshfs_leak_count,
    _SSHFS_CIRCUIT_BREAKER_THRESHOLD,
    station_url,
    station_get_status,
    station_get_raw,
    _status_diff_signature,
    _STATUS_DIFF_FIELDS,
    _MAX_STATUS_LISTENERS,
    _STATUS_LISTENER_QUEUE_MAX,
    StationCache,
    start_polling,
)



# ── SSH known-hosts file for the live-stream pipeline ─────────────────────
# Auto-populated at startup via ``_ensure_known_hosts(config)`` which runs
# ``ssh-keyscan`` for every station + jump-host IP in the config. A station
# that declares ``ssh_host_key_fingerprints`` is verified against those pins
# before its key is trusted (H5 — closes the first-contact TOFU window); an
# unpinned host keeps accept-new/TOFU but is logged as a WARNING so the gap
# stays visible until it's pinned.
KNOWN_HOSTS_PATH = Path(
    os.environ.get("ROVIMEN_KNOWN_HOSTS", "/opt/rovimen/known_hosts")
)


def _fingerprints_for_keyscan_line(line: str) -> list[str]:
    """Return the ``SHA256:...`` fingerprint(s) for a single ssh-keyscan
    output line.

    ``ssh-keygen -lf -`` reads a keyscan line on stdin and prints its
    fingerprint. A line may hash-hostname (``-H``) or not; either form is
    accepted by ssh-keygen. Returns an empty list if the line can't be
    fingerprinted (so the caller treats it as unverifiable, i.e. a mismatch
    when a pin is required)."""
    try:
        r = subprocess.run(
            ["ssh-keygen", "-lf", "-"],
            input=line, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    fps: list[str] = []
    for out in r.stdout.splitlines():
        for tok in out.split():
            if tok.startswith("SHA256:"):
                fps.append(tok)
    return fps


def _ensure_known_hosts(config: "DashboardConfig") -> None:
    """Populate KNOWN_HOSTS_PATH with host keys for every configured station
    (and jump host). Existing keys are preserved; only missing IPs are scanned.
    Runs once at startup in a background thread so it never blocks boot.

    Host-key pinning (H5): if a station declares ``ssh_host_key_fingerprints``,
    an ssh-keyscan result for its IP is trusted ONLY when its fingerprint
    matches one of the pinned values — a mismatch is refused and logged as an
    error, with no fall-back to accept-new (closes the first-contact TOFU MITM
    window). Unpinned IPs keep the accept-new behaviour so the ~15 already-
    deployed stations don't break, but each unpinned scan is logged as a
    WARNING so the gap stays visible."""
    ips: set[str] = set()
    # ip -> set of pinned SHA256 fingerprints (empty set => unpinned).
    pins_by_ip: dict[str, set[str]] = {}

    def _record(st: "StationConfig") -> None:
        ips.add(st.ip)
        if st.ssh_host_key_fingerprints:
            pins_by_ip.setdefault(st.ip, set()).update(
                st.ssh_host_key_fingerprints
            )
        else:
            pins_by_ip.setdefault(st.ip, set())

    for st in config.stations.values():
        _record(st)
        for jk in st.jump_hosts:
            jst = config.stations.get(jk)
            if jst:
                _record(jst)

    KNOWN_HOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not KNOWN_HOSTS_PATH.is_file():
        KNOWN_HOSTS_PATH.touch()

    missing: list[str] = []
    for ip in sorted(ips):
        try:
            r = subprocess.run(
                ["ssh-keygen", "-F", ip, "-f", str(KNOWN_HOSTS_PATH)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0 or not r.stdout.strip():
                missing.append(ip)
        except Exception:
            missing.append(ip)

    if not missing:
        logger.info(
            "known_hosts OK: all %d station/jump IPs present in %s",
            len(ips), KNOWN_HOSTS_PATH,
        )
        return

    logger.info(
        "known_hosts: scanning %d missing IPs: %s",
        len(missing), ", ".join(missing),
    )

    scanned_lines: list[str] = []
    for ip in missing:
        pins = pins_by_ip.get(ip) or set()
        try:
            result = subprocess.run(
                ["ssh-keyscan", "-H", "-T", "5", ip],
                capture_output=True, text=True, timeout=10,
            )
            lines = [
                ln for ln in result.stdout.splitlines()
                if ln.strip() and not ln.startswith("#")
            ]
            if not lines:
                logger.warning("ssh-keyscan returned no keys for %s", ip)
                continue

            if pins:
                # Pinned host: trust ONLY lines whose fingerprint matches a
                # pinned value. A mismatch is a refusal, not accept-new.
                accepted: list[str] = []
                for ln in lines:
                    fps = _fingerprints_for_keyscan_line(ln)
                    if any(fp in pins for fp in fps):
                        accepted.append(ln)
                    else:
                        logger.error(
                            "HOST-KEY PIN MISMATCH for %s: scanned %s does not "
                            "match any pinned fingerprint %s — refusing to "
                            "trust this key (possible MITM)",
                            ip, fps or ["<unparseable>"], sorted(pins),
                        )
                if accepted:
                    scanned_lines.extend(accepted)
                    logger.info(
                        "ssh-keyscan OK (pin verified): %s (%d/%d keys "
                        "matched pin)", ip, len(accepted), len(lines),
                    )
                else:
                    logger.error(
                        "No scanned key for %s matched its pinned fingerprint "
                        "— %s will not be added to known_hosts", ip, ip,
                    )
            else:
                # Unpinned host: keep the existing accept-new (TOFU) behaviour
                # so already-deployed stations keep working, but flag it.
                scanned_lines.extend(lines)
                logger.warning(
                    "ssh-keyscan OK (UNPINNED, TOFU): %s (%d keys) — no "
                    "ssh_host_key_fingerprints configured; first-contact MITM "
                    "is not closed for this host. Pin it via "
                    "tools/pin_host_keys.py.", ip, len(lines),
                )
        except Exception as exc:
            logger.warning("ssh-keyscan failed for %s: %s", ip, exc)

    if scanned_lines:
        with open(KNOWN_HOSTS_PATH, "a") as fh:
            fh.write("\n".join(scanned_lines) + "\n")
        logger.info(
            "known_hosts updated: added %d key lines to %s",
            len(scanned_lines), KNOWN_HOSTS_PATH,
        )
    else:
        logger.error(
            "Could not scan any station host keys — live streaming will be "
            "unavailable until %s is populated", KNOWN_HOSTS_PATH,
        )

# ── Live-stream concurrency caps ──────────────────────────────────────────
# Each ``/stream/<host>/<camera>`` request spawns an ``ssh ... ffmpeg`` pipe
# that pegs ~10–25 % of one VPS core. Without a cap a handful of browser
# reloads (each request opens a fresh stream and orphans the previous one
# until the 30 s read timeout fires) can saturate the VPS and starve the
# rest of the dashboard. We use two layers:
#
# * One semaphore per ``(session_user, host_key, camera_code)`` triple —
#   keeps a single user from stacking up reloads on the same camera.
# * One global semaphore — caps total in-flight live streams across the
#   whole VPS, regardless of user. A second user reaching a different
#   camera takes a different per-user slot but still counts against the
#   global cap.
#
# Acquired with ``blocking=False`` so a refused request is rejected with
# 429 immediately rather than tying up the Flask worker thread. Both
# semaphores are released in a ``finally`` inside the streaming generator.
_LIVE_STREAM_GLOBAL_CAP = int(
    os.environ.get("ROVIMEN_LIVE_STREAM_GLOBAL_CAP", "4")
)
_LIVE_STREAM_PER_TRIPLE_CAP = int(
    os.environ.get("ROVIMEN_LIVE_STREAM_PER_TRIPLE_CAP", "1")
)
_LIVE_STREAM_RETRY_AFTER_S = int(
    os.environ.get("ROVIMEN_LIVE_STREAM_RETRY_AFTER_S", "15")
)
_live_stream_global_sem = threading.BoundedSemaphore(_LIVE_STREAM_GLOBAL_CAP)
_live_stream_per_triple_sems: dict[tuple[str, str, str], threading.BoundedSemaphore] = {}
_live_stream_per_triple_lock = threading.Lock()


def _background_workers_enabled() -> bool:
    """Whether create_app should spawn its long-lived background workers.

    Production (env unset) starts the full set: tunnel init/watchdog, the
    status/vitals pollers, thumbnail prefetch, the sky-dome scheduler and the
    GMN/MDC/archive-index refreshers. Setting ``ROVIMEN_DISABLE_BACKGROUND=1``
    builds the same WSGI app with routes and caches intact but starts none of
    those daemon loops — so a caller that only exercises the request surface
    (unit tests, the Playwright E2E launcher) doesn't leak forever-sleeping
    threads. Fail-safe: any value other than "1" leaves the workers enabled.
    """
    return os.environ.get("ROVIMEN_DISABLE_BACKGROUND", "0") != "1"


def _live_stream_triple_sem(
    user: str, host_key: str, camera_code: str,
) -> threading.BoundedSemaphore:
    """Return (and lazily create) the per-(user, host, camera) semaphore."""
    key = (user, host_key, camera_code)
    with _live_stream_per_triple_lock:
        if len(_live_stream_per_triple_sems) > 1024:
            _live_stream_per_triple_sems.clear()
        sem = _live_stream_per_triple_sems.get(key)
        if sem is None:
            sem = threading.BoundedSemaphore(_LIVE_STREAM_PER_TRIPLE_CAP)
            _live_stream_per_triple_sems[key] = sem
        return sem


# ── Shared validation regexes (defence-in-depth against path traversal) ──
#
# These shapes mirror those in ``dashboard/public_api.py`` for ``/media/v1/*``
# (``_CAMERA_RE``, ``_DATE_COMPACT_RE``, ``_FILENAME_RE``). Any code that
# interpolates user-supplied camera / date / filename / manifest-id values
# into a filesystem path MUST gate on these regexes before the interpolation
# AND verify ``Path.resolve().is_relative_to(<intended-root>)`` afterwards.
# The regex is a cheap pre-filter; the resolve check is the actual safety net.
#
# ``_COMPILATION_MANIFEST_ID_RE`` is the slug shape accepted for caller-supplied
# compilation manifest IDs (``/api/compilation`` POST body ``id``). It mirrors
# the existing internal generator ``{YYYYMMDD}-{slug}`` (dashes, digits,
# lowercase letters) plus dots and underscores so callers can pass arbitrary
# project-shaped slugs. It deliberately rejects ``/``, ``\``, ``..``, and a
# leading dot to prevent both filesystem escape and hidden-output files.
_COMPILATION_MANIFEST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# Clip-side shapes (must stay in sync with public_api._CAMERA_RE etc).
_COMPILATION_CAMERA_RE = re.compile(r"^[A-Z0-9]{1,16}$")
_COMPILATION_DATE_COMPACT_RE = re.compile(r"^\d{8}$")
# Real station filenames include lowercase suffixes like _color, e.g.
# RO000T_20260506_193628_color.mkv (and boundary stitches like
# RO000T_20260506_193628_193648_color.mkv). Kept narrower than the public-API
# ``_FILENAME_RE`` (which also accepts ``.webp``/``.jpg``) because the
# compilation pipeline only ever ffmpeg-concats video.
_COMPILATION_CLIP_FILENAME_RE = re.compile(r"^[A-Za-z0-9_]+\.(mkv|mp4)$")

# In-memory cache for nights_full scans (storagebox stat calls are slow).
# cam -> (timestamp, result). Cam-keyed (fleet-bounded in practice) but each
# entry holds a full per-camera nights list, so LRU-cap it as a hard ceiling
# against renamed/retired camera codes accumulating over deploy uptime.
_nights_full_cache: BoundedCache = BoundedCache(
    _cache_cap("ROVIMEN_NIGHTS_FULL_CACHE_MAX", 256)
)
_NIGHTS_FULL_TTL = 120  # seconds


_archive_idx = _ArchiveIndex()


# ---------------------------------------------------------------------------
# Authentication — extracted to dashboard/auth.py
# ---------------------------------------------------------------------------

from auth import (  # noqa: F401,E402
    USERS_PATH,
    SECRET_KEY_PATH,
    _load_or_create_secret_key,
    _parse_users_file,
    _load_users,
    _invalidate_users_cache,
    _users_lock,
    _backup_users_file,
    _check_user_count,
    _save_users,
    _update_users,
    _check_credentials,
    _session_role,
    _has_station_access,
    _host_keys_for_camera,
    _has_camera_access,
    _find_user_by_email,
    _cf_access_before_request,
    install_session_revalidation,
    require_auth,
    require_admin,
    require_station,
)


# ---------------------------------------------------------------------------
# SSH tunnel manager — extracted to tunnels.py
# ---------------------------------------------------------------------------
from tunnels import TunnelManager, _TunnelDown  # noqa: F401,E402


# ---------------------------------------------------------------------------
# GMN public REST API integration — extracted to gmn_poller.py
# ---------------------------------------------------------------------------
from gmn_poller import (  # noqa: E402,F401
    _GMN_SQL_URL,
    _GMN_POLL_INTERVAL_S,
    _GMN_RECENT_WINDOW_DAYS,
    _GMN_BACKFILL_WINDOW_DAYS,
    _GMN_BACKFILL_WORKERS,
    _GMN_THROTTLE_S,
    _GMN_HTTP_TIMEOUT,
    _GMN_BATCH,
    _gmn_shower_cache,
    _gmn_shower_cache_lock,
    _gmn_cache,
    _gmn_cache_lock,
    _gmn_initial_pass_done,
    _gmn_split_stations,
    _gmn_row_to_event,
    _gmn_request_lock,
    _gmn_last_request_t,
    _gmn_sql,
    _gmn_load_shower_cache,
    _gmn_sql_quote_ids,
    _gmn_fetch_multistation,
    _gmn_fetch_multistation_LEGACY_DATASETTE,
    _gmn_get_cached,
    _gmn_monthly_count,
    _gmn_poller_loop,
    _start_gmn_poller,
    _gmn_sanity_check,
)


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------


def create_app(config: DashboardConfig, config_path: Path | None = None) -> Flask:
    cache = StationCache()
    tunnels = TunnelManager(config)

    # Tell the GMN layer which camera codes are "ours" (plus the optional
    # highlight subset) so witness filtering/tagging is driven by config
    # rather than hard-coded RO/DE prefixes — this is what makes the GMN
    # overlay work for any network's own station set.
    gmn_data.configure_station_codes(
        (cam.code for st in config.stations.values() for cam in st.cameras),
        config.highlight_codes,
    )

    def _media_url(host_key: str, *path_parts: str) -> str:
        return media_url(config, tunnels, host_key, *path_parts)

    # morning_done[(host_key, cam, date)] stores the monotonic timestamp at which
    # dawn_process finished (truthy), or 0.0 if not yet done (falsy).  Using a
    # float lets _thumb_prefetch_loop prune stale entries by age under
    # _prefetched_lock alongside bw_prefetched/color_prefetched — without this,
    # entries for historical nights accumulate indefinitely over deploy uptime.
    # All callers that previously stored/read bool continue to work: 0.0 is
    # falsy, positive monotonic timestamps are truthy.
    # Cap for the per-(host,cam,night) prefetch dedup + morning-done maps.
    # These were bounded only by the 14-day age prune in _thumb_prefetch_loop,
    # so a worker's RSS grew with uptime × cameras × nights before a prune ran.
    # A hard LRU cap makes each map's memory constant regardless of uptime;
    # 512 comfortably covers ~100 cameras × the couple of live nights the loop
    # actually re-warms (the "cache sizes" log showed tens of entries), so at
    # current scale nothing evicts and behaviour is unchanged.
    _PREFETCH_DEDUP_MAX = _cache_cap("ROVIMEN_PREFETCH_DEDUP_MAX", 512)
    morning_done: BoundedCache = BoundedCache(_PREFETCH_DEDUP_MAX)
    # bw_prefetched: nights where BW thumbnails have been queued (skip on next fetch).
    # color_prefetched: nights where color overwrite has been queued after morning_done.
    # Stored as {key: insertion_monotonic_ts}: lets us prune entries older than the
    # retention horizon so the dicts don't grow without bound over deploy uptime,
    # and lets the prefetch worker discard a key on failure so retries are possible.
    # LRU-capped (see _PREFETCH_DEDUP_MAX) as a hard ceiling on top of the age prune.
    bw_prefetched: BoundedCache = BoundedCache(_PREFETCH_DEDUP_MAX)
    color_prefetched: BoundedCache = BoundedCache(_PREFETCH_DEDUP_MAX)
    _prefetched_lock = threading.Lock()
    # 14 days: long enough to dedup the live nightly cycle (current + previous
    # night re-fetched on dashboard tab churn) without retaining state forever.
    _PREFETCHED_MAX_AGE_S = 14 * 24 * 3600.0
    # Simple TTL cache for /api/detections/<date>: {date: (expires_at, payload)}.
    # Already LRU-evicted at 100 in routes.detections; wrap it in BoundedCache
    # as the belt to that suspenders so the size ceiling holds even if the
    # per-route eviction path is ever skipped.
    _DETECTIONS_CACHE_MAX = _cache_cap("ROVIMEN_DETECTIONS_CACHE_MAX", 128)
    _detections_cache: BoundedCache = BoundedCache(_DETECTIONS_CACHE_MAX)
    # Yesterday's detections don't change once the night is archived — cache
    # for much longer than live day. 60 s was barely enough to absorb repeated
    # tab switches; go to 30 min so reloading the Events page is instant.
    _DETECTIONS_TTL = 1800  # seconds

    # ── Cache-while-online, serve-stale-when-offline ──
    # Goal: every panel on the overview / per-station page must keep working
    # when a station goes offline. Pre-fetch aggressively while online; never
    # invalidate on offline; let the user see the last-known-good content.
    #
    # Cache shape: {key: (expires_at, payload)} for online-TTL entries.
    # When the station is offline we return the stale payload regardless of
    # `expires_at`. None means "never seen yet".
    _overview_stacks_cache: dict[tuple[str, str], dict[str, Any]] = {}  # (host, cam) -> entry
    # Lock guards iteration in api_overview_stacks against concurrent
    # __setitem__ from the background refresh thread; without it a
    # `dictionary changed size during iteration` RuntimeError can fire on
    # the request thread when a refresh lands mid-poll.
    _overview_stacks_cache_lock = threading.Lock()
    _overview_stacks_full_ts: float = 0.0  # last time we ran the fan-out
    _OVERVIEW_STACKS_TTL = 120  # seconds — refresh fan-out at most this often
    # host -> (expires, payload). Keyed by host, so its natural ceiling is the
    # fleet size — but a stray/renamed host key could still leak an entry per
    # deploy. LRU-cap it so it can never outgrow a small multiple of the fleet.
    _PLATEPAR_CACHE_MAX = _cache_cap("ROVIMEN_PLATEPAR_CACHE_MAX", 256)
    _platepar_cache: BoundedCache = BoundedCache(_PLATEPAR_CACHE_MAX)
    _PLATEPAR_TTL = 300  # 5 min
    # SWR cache: (fetched_at_monotonic, payload). Entries newer than
    # _TIMELAPSES_FRESH_TTL serve straight from cache; older entries serve
    # stale immediately and trigger a background refresh, coalesced via
    # _timelapses_swr_inflight. The full per-camera SSHFS walk only ever
    # runs in the refresh thread once a cache entry exists.
    # Keyed by host; SWR entries hold a full per-camera timelapse listing, so a
    # leaked entry is comparatively heavy. Cap it (host-keyed -> fleet-sized in
    # practice) so it can't grow unbounded across renamed/retired host keys.
    _TIMELAPSES_CACHE_MAX = _cache_cap("ROVIMEN_TIMELAPSES_CACHE_MAX", 256)
    _timelapses_swr_cache: BoundedCache = BoundedCache(_TIMELAPSES_CACHE_MAX)
    _timelapses_swr_inflight: set[str] = set()
    _timelapses_swr_lock = threading.Lock()
    _TIMELAPSES_FRESH_TTL = 60  # seconds

    # Process-lifetime "last successfully observed" mirrors of status/vitals.
    # Unlike StationCache, these never expire — they're a fallback shown to
    # the user when the live cache is empty (post-restart, post-evict) so
    # the request thread never blocks on a 15-20 s SSH-tunneled fetch.
    # Background pollers (start_polling) refresh the live cache; cold-miss
    # request handlers also kick a one-shot refresh, coalesced via the
    # inflight sets.
    # Host-keyed, never-expiring last-known-good mirrors. Fleet-sized in
    # practice; LRU-capped so renamed/retired host keys can't accumulate the
    # full status/vitals payload forever over deploy uptime.
    _LAST_SEEN_CACHE_MAX = _cache_cap("ROVIMEN_LAST_SEEN_CACHE_MAX", 256)
    _status_last_seen: BoundedCache = BoundedCache(_LAST_SEEN_CACHE_MAX)
    _vitals_last_seen: BoundedCache = BoundedCache(_LAST_SEEN_CACHE_MAX)
    _status_last_seen_lock = threading.Lock()
    _vitals_last_seen_lock = threading.Lock()
    _status_refresh_inflight: set[str] = set()
    _vitals_refresh_inflight: set[str] = set()
    _LAST_SEEN_MAX_AGE = 24 * 3600  # 24 h — older than this, ignore.
    # value: (expiry_mono, plots_list, has_color_meteor_stack_or_None)
    # The 3rd slot caches the result of the per-night HEAD check that
    # probes for the color meteor stack — see api_rms_plots_proxy. None
    # means "not yet probed".
    # (host, cam, date) -> (expiry_mono, plots_list, has_color_meteor_or_None).
    # LRU-capped as a hard ceiling; the per-write TTL-then-age prune in
    # _prefetch_rms_plots_for_night still runs first (it trims expired entries
    # before the cap would bite), so the cap only evicts when live entries alone
    # would exceed it — impossible at current scale (fleet × ~2 nights).
    _RMS_PLOTS_LIST_CACHE_MAX = _cache_cap("ROVIMEN_RMS_PLOTS_LIST_CACHE_MAX", 2048)
    _rms_plots_list_cache: BoundedCache = BoundedCache(_RMS_PLOTS_LIST_CACHE_MAX)
    # Guards iterate+evict in _prefetch_rms_plots_for_night and api_rms_plots_proxy
    # (station_ops) against concurrent RuntimeError on dict resize.
    _rms_plots_list_cache_lock = threading.Lock()
    _RMS_PLOTS_LIST_TTL = 300  # 5 min — list rarely changes during the day

    # Bounded pool for chunk-thumbnail prefetches. Without it, a
    # morning_done transition across N stations × M cameras would spawn
    # 40+ unconstrained threads — each running a serial GET loop that
    # holds an HTTP connection to its station. 8 workers covers two
    # stations' worth of cameras in flight while leaving the rest of
    # the pool free for live requests.
    _prefetch_executor = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="prefetch",
    )

    def _record_status_last_seen(host_key: str, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        with _status_last_seen_lock:
            _status_last_seen[host_key] = (time.time(), data)

    def _record_vitals_last_seen(host_key: str, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        with _vitals_last_seen_lock:
            _vitals_last_seen[host_key] = (time.time(), data)

    def _kick_status_refresh(host_key: str) -> None:
        with _status_last_seen_lock:
            if host_key in _status_refresh_inflight:
                return
            _status_refresh_inflight.add(host_key)

        def _run() -> None:
            try:
                data = station_get_status(
                    config, tunnels, host_key, "/api/status", timeout=8,
                )
                cache.set_status(host_key, data)
                _record_status_last_seen(host_key, data)
            except Exception:
                logger.debug("cold-miss status refresh failed for %s", host_key)
            finally:
                with _status_last_seen_lock:
                    _status_refresh_inflight.discard(host_key)

        threading.Thread(
            target=_run, name=f"status-refresh-{host_key}", daemon=True
        ).start()

    def _kick_vitals_refresh(host_key: str) -> None:
        with _vitals_last_seen_lock:
            if host_key in _vitals_refresh_inflight:
                return
            _vitals_refresh_inflight.add(host_key)

        def _run() -> None:
            try:
                data = station_get_status(
                    config, tunnels, host_key, "/api/vitals", timeout=10,
                )
                cache.set_vitals(host_key, data)
                _record_vitals_last_seen(host_key, data)
            except Exception:
                logger.debug("cold-miss vitals refresh failed for %s", host_key)
            finally:
                with _vitals_last_seen_lock:
                    _vitals_refresh_inflight.discard(host_key)

        threading.Thread(
            target=_run, name=f"vitals-refresh-{host_key}", daemon=True
        ).start()

    def _prefetch_night(
        host_key: str, cam: str, date: str, chunks_data: list, force: bool = False
    ) -> None:
        """Background: fetch and cache thumbnails for a night.

        force=False (BW pass): skip files that are already cached.
        force=True  (color pass, morning_done): overwrite all — replaces stale BW.

        On total failure (no chunks fetched AND at least one was attempted),
        discard the (host, cam, date) key from the appropriate prefetched
        dict so the next loop iteration can retry. Without this, a transient
        network blip during the first attempt permanently sticks the key.
        """
        fetched = 0
        attempted = 0
        for chunk in chunks_data:
            stack = chunk.get("stack")
            if not stack:
                continue
            cache_file = _thumb_cache_path(host_key, cam, date, stack)
            if not force and cache_file.exists():
                continue
            attempted += 1
            try:
                url = _media_url(host_key, "thumbnail", cam, date, stack)
                sess = _session_for_url(url)
                resp = sess.get(url, timeout=10)
                if resp.status_code == 200 and _thumb_cache_has_space():
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_bytes(
                        _downsample_thumbnail_bytes(resp.content)
                    )
                    fetched += 1
            except Exception:
                pass
        if fetched:
            label = "color" if force else "BW"
            logger.info(
                "Prefetched %d %s thumbnails for %s/%s/%s",
                fetched, label, host_key, cam, date,
            )
        if attempted > 0 and fetched == 0:
            # Every fetch failed — release the dedup key so a retry can run.
            key = (host_key, cam, date)
            with _prefetched_lock:
                if force:
                    color_prefetched.pop(key, None)
                else:
                    bw_prefetched.pop(key, None)

    # Per-night bulk archive-thumb prefetch: dedup keys so concurrent loads
    # of the same archive night don't spawn duplicate copy jobs.
    # Stored as {key: insertion_monotonic_ts} for the same reasons as
    # bw_prefetched / color_prefetched (bounded growth + retry-on-failure).
    # LRU-capped alongside bw/color_prefetched — same (host,cam,date) key space,
    # same 14-day age prune, so the hard ceiling reuses _PREFETCH_DEDUP_MAX.
    _archive_thumb_prefetched: BoundedCache = BoundedCache(_PREFETCH_DEDUP_MAX)
    _archive_thumb_prefetch_lock = threading.Lock()
    _sshfs_copy_sem = threading.Semaphore(2)

    def _prefetch_archive_thumbs(host_key: str, cam: str, date: str) -> None:
        """Background bulk copy of ALL stack thumbnails for a (cam, date) from
        the storage-box archive to the local NVMe THUMB_CACHE. After this
        runs every per-thumbnail request hits NVMe directly, served by nginx
        via X-Accel-Redirect.

        Idempotent — already-cached files are skipped — and coalesced via
        ``_archive_thumb_prefetched`` so concurrent loads of the same night
        spawn one copy job, not N. Individual file copies run in parallel
        because SSHFS reads are bandwidth-bound, not CPU-bound, and four
        concurrent transfers fill the storage-box pipe far faster than serial
        ones.
        """
        key = (host_key, cam, date)
        with _archive_thumb_prefetch_lock:
            if key in _archive_thumb_prefetched:
                return
            _archive_thumb_prefetched[key] = time.monotonic()

        # Failure path: drop the key so the next caller can retry. Without
        # this, a transient SSHFS / disk error would permanently block this
        # (host, cam, date) from being re-prefetched until process restart.
        succeeded = False
        try:
            stacks_dir = ARCHIVE_PATH / cam / date / "stacks"
            try:
                if not stacks_dir.is_dir():
                    succeeded = True  # nothing to do is a clean outcome
                    return
                entries = [s for s in stacks_dir.iterdir() if s.suffix.lower() == ".webp"]
            except OSError:
                return  # succeeded stays False -> key discarded

            def _copy_one(src: Path) -> bool:
                cache_file = _thumb_cache_path(host_key, cam, date, src.name)
                if cache_file.exists():
                    return False
                if not _thumb_cache_has_space():
                    return False
                _sshfs_copy_sem.acquire()
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache_file.with_suffix(cache_file.suffix + ".part")
                    shutil.copyfile(src, tmp)
                    tmp.replace(cache_file)
                    return True
                except Exception:
                    return False
                finally:
                    _sshfs_copy_sem.release()

            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="archive-thumb") as pool:
                copied = sum(1 for ok in pool.map(_copy_one, entries) if ok)
            if copied:
                logger.info(
                    "Archive thumb prefetch: %d files %s/%s/%s",
                    copied, host_key, cam, date,
                )
            succeeded = True
        finally:
            if not succeeded:
                with _archive_thumb_prefetch_lock:
                    _archive_thumb_prefetched.pop(key, None)

    app = Flask(
        __name__,
        static_folder=str(Path(__file__).parent / "static"),
        template_folder=str(Path(__file__).parent / "templates"),
    )
    # Persistent secret key: session cookies survive dashboard restarts so
    # every user doesn't get force-logged-out on every redeploy. Env wins
    # for deployments that already feed the key in via systemd; otherwise
    # we materialise one on disk (0600) and reuse it on subsequent boots.
    app.secret_key = _load_or_create_secret_key()

    # Cookie flags, ProxyFix, security headers, 24 h session lifetime.
    # Must run before the limiter so the limiter sees the ProxyFix'd
    # remote_addr and emits accurate per-IP buckets.
    security.configure_hardening(app)
    limiter = security.init_limiter(app)
    app.config["BUILD_VERSION"] = BUILD_VERSION
    app._limiter = limiter  # type: ignore[attr-defined]
    csrf = security.init_csrf(app)
    # CSRF Phase 1 (audit P1-3): reject mutating /api/* calls whose
    # Origin / Sec-Fetch-Site indicate a cross-origin caller. Skips
    # /api/public/v1/* (intentionally cross-origin, API-key gated) and
    # passes through curl/cron (neither header sent). Phase 2 will add
    # a real CSRF token on top.
    security.install_csrf_origin_check(app)

    # When Cloudflare Access fronts the dashboard (env: ROVIMEN_CF_TEAM_DOMAIN
    # + ROVIMEN_CF_AUD), verify the signed JWT on every request and bridge
    # the email claim into a Flask session — users skip the /login form.
    # Dormant when those envs are unset, so dev and Tailscale ops paths
    # keep using the password flow untouched.
    if cf_access.load_config() is not None:
        logger.info("Cloudflare Access auto-login enabled")
        app.before_request(_cf_access_before_request)

    # Privilege-revocation guard (audit H3): re-check each logged-in session
    # against the live users.yaml — refresh role/stations on a session-epoch
    # bump (admin changed role/stations/password/expiry) and clear the session
    # on account deletion or past expiry. Registered BEFORE the auth gate so a
    # cleared session fails closed on the same request.
    install_session_revalidation(app)

    # Global auth gate — always on. Removing the env toggle removes the
    # "one bad rollback re-opens the dashboard" failure mode.
    #
    # ``config.public_pages`` is the runtime on/off switch for which
    # public-capable pages anonymous visitors may reach (see
    # dashboard_config.yaml + models.DashboardConfig). It is read once at
    # startup, so toggling a page requires a service restart (no hot-reload
    # of dashboard_config.yaml for this setting yet).
    security.install_auth_gate(app, public_pages=config.public_pages)

    # Defense in depth: when a request reaches Flask directly (e.g. dev runs
    # without nginx in front), Werkzeug's default static handler sends
    # `Cache-Control: no-cache`. Align it with the nginx setting so behaviour
    # is identical across paths; cache-busting via ?v= still invalidates
    # cleanly because the URL itself changes between builds.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 604800  # 7 days

    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def _csrf_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), 400
        return render_template("login.html",
                               error="Session expired. Please try again."), 400

    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def _json_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), error.code
        return error

    @app.after_request
    def _close_connection(response: Response) -> Response:
        """Force Werkzeug to close the TCP socket after each response.

        Behind nginx with proxy_http_version 1.1 and keep-alive, Werkzeug's
        dev server leaks CLOSE-WAIT sockets when the proxy drops idle
        connections.  Over hours this exhausts the thread pool and the
        server stops accepting new requests.  Setting Connection: close on
        every response tells Werkzeug to tear down the socket immediately.

        In production gunicorn manages connection lifecycle independently and
        nginx relies on HTTP/1.1 keep-alive for upstream reuse.  Injecting
        Connection: close there defeats nginx <-> gunicorn keep-alive and adds
        one extra TCP handshake per request.  Guard to dev-server only."""
        # os.environ check is more reliable than app.debug (debug may be False
        # even in dev if the caller didn't pass --debug explicitly).
        if os.environ.get("WERKZEUG_SERVER_FD") or os.environ.get("WERKZEUG_RUN_MAIN"):
            response.headers["Connection"] = "close"
        return response

    @app.context_processor
    def _inject_build_version() -> dict[str, str]:
        """Make BUILD_VERSION available to every template as `build_version`.

        Templates pass it through `url_for('static', ..., v=build_version)` to
        produce URLs like `/static/dashboard.js?v=<sha>`. Changing the SHA on
        deploy guarantees clients fetch the new asset on next page load — see
        the module-level comment on _compute_build_version for the underlying
        cache-poisoning failure mode this prevents.
        """
        return {"build_version": BUILD_VERSION}

    @app.context_processor
    def _inject_nav_visibility() -> dict[str, object]:
        """Expose ``nav_show(page_key)`` to every template so the shared
        ``<nav>`` can drop tabs a viewer can't actually reach.

        The nav links are hardcoded per template, but which pages are open to
        anonymous visitors is a runtime toggle (``public_pages`` in
        dashboard_config.yaml). Without this, dropping a page from
        ``public_pages`` gates the route but leaves a dead tab that bounces
        anonymous visitors to ``/login`` — the opposite of a clean public
        surface. ``nav_show`` returns True when the viewer is logged in (any
        role sees every tab) OR the page key is currently public. It mirrors
        the same ``public_pages`` set the auth gate freezes at startup, so the
        nav and the gate can never disagree for a given request.
        """
        from flask import session

        authed = bool(session.get("user"))
        public = frozenset(config.public_pages or ())

        def nav_show(page_key: str) -> bool:
            return authed or page_key in public

        return {"nav_show": nav_show}

    def _require_station(host_key: str) -> StationConfig:
        station = config.stations.get(host_key)
        if station is None:
            abort(404)
        return station


    # Note: a former ``_rotate_image_bytes`` helper used PIL to 180-rotate
    # RMS plot bytes on cache miss (~20 ms per image). Rotation moved to the
    # client (``.thumb-rotated`` CSS class) — the compositor does it after
    # first paint, and the NVMe cache now stores raw upstream bytes.

    # ── Authentication — extracted to routes/auth_routes.py ────────────────
    from routes.auth_routes import register_auth_routes
    register_auth_routes(app, config, cache, tunnels)

    # ── Simple page renders (sw.js, index, station, config, admin,
    #    events, highlights, live) — extracted to routes/misc.py ──────────
    from routes.misc import register_misc_routes
    register_misc_routes(app, config, BUILD_VERSION)

    # ── Admin: network config read/write ─────────────────────────────────

    def _camera_to_dict(c: CameraConfig) -> dict:
        d: dict[str, Any] = {
            "code": c.code,
            "cam_ip": c.cam_ip,
            "rotate": c.rotate,
            "label": getattr(c, "label", None) or "",
        }
        # Only emit pointing fields when set — keeps the YAML uncluttered for
        # stations whose cameras haven't been pointed yet, and avoids dropping
        # az/alt on round-trip (P0-2): the overview map's FOV polygons depend
        # on these values being preserved across every admin write.
        if c.az is not None:
            d["az"] = c.az
        if c.alt is not None:
            d["alt"] = c.alt
        if c.rtsp_url is not None:
            d["rtsp_url"] = c.rtsp_url
        return d

    def _config_to_dict() -> dict:
        return {
            "station_api_port": config.station_api_port,
            "correlation_window_s": config.correlation_window_s,
            # Runtime public-page toggle set. Preserved across admin config
            # writes so an operator saving the config from the admin UI can't
            # silently drop the public-page allowlist (which would fail closed
            # to "nothing public" on the next restart).
            "public_pages": config.public_pages,
            # Optional highlight-overlay subset (see models.DashboardConfig).
            # Round-trips so an admin save can't silently drop it.
            "highlight_codes": config.highlight_codes,
            "stations": {
                host: {
                    "ip": st.ip,
                    "label": st.label,
                    "ssh_user": st.ssh_user,
                    "proxy_media": st.proxy_media,
                    "jump_hosts": st.jump_hosts,
                    "lat": st.lat,
                    "lon": st.lon,
                    # Preserve overview-map opt-out (P0-2). Default in the
                    # model is True, so omitting this previously silently
                    # re-enabled stations the operator had hidden.
                    "show_on_map": st.show_on_map,
                    "public_tabs": st.public_tabs,
                    "public": st.public,
                    "location_name": st.location_name,
                    "status": st.status,
                    # Poll→push cutover switch. Round-trips so an admin edit
                    # persists which stations have been migrated to push.
                    "push_enabled": st.push_enabled,
                    # Only emit host-key pins when set, so the ~15 unpinned
                    # stations don't grow an empty list in the on-disk config.
                    **(
                        {"ssh_host_key_fingerprints": st.ssh_host_key_fingerprints}
                        if st.ssh_host_key_fingerprints
                        else {}
                    ),
                    "cameras": [_camera_to_dict(c) for c in st.cameras],
                }
                for host, st in config.stations.items()
            },
        }

    # Sidecar lock file for cross-process coordination on dashboard_config.yaml
    # writes. The lock is independent of the file itself so a half-written
    # config (which yaml.safe_load would reject) can't deadlock the next save.
    _config_lock_path = (
        config_path.with_suffix(config_path.suffix + ".lock")
        if config_path
        else None
    )
    _config_save_thread_lock = threading.Lock()

    def _save_config() -> None:
        """Atomic, locked write of ``dashboard_config.yaml`` (P0-3).

        Uses the same pattern as ``compilation_store`` and ``_save_users``:
        threading.Lock for same-process races, ``fcntl.flock`` on a sidecar
        file for cross-process races, write to a ``.tmp`` sibling then
        ``os.replace`` so a concurrent reader never sees a half-written file.
        """
        if not config_path or _config_lock_path is None:
            return
        raw = _config_to_dict()
        payload = yaml.dump(raw, default_flow_style=False, allow_unicode=True)
        with _config_save_thread_lock:
            _config_lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_config_lock_path, "a+") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                tmp = config_path.with_suffix(config_path.suffix + ".tmp")
                tmp.write_text(payload)
                os.replace(tmp, config_path)

    def _push_rotate_to_station(host_key: str, cam_code: str, rotate: bool, *, timeout: int = 8) -> dict[str, Any]:
        """PATCH /api/settings on the station to set stations.<cam_code>.rotate.

        Returns {ok, error?}. Does not raise — caller may proceed even if station
        is offline; dashboard_config.yaml remains the source of truth and the
        station will pick up the value on its next deploy.
        """
        try:
            url = station_url(config, tunnels, host_key, "/api/settings")
            sess = _session_for_url(url)
            resp = sess.patch(
                url,
                json={"stations": {cam_code: {"rotate": bool(rotate)}}},
                timeout=timeout,
            )
            resp.raise_for_status()
            return {"ok": True}
        except Exception as exc:
            logger.warning("push rotate to %s/%s failed: %s", host_key, cam_code, exc)
            return {"ok": False, "error": str(exc)}

    # Network-config admin routes (/api/admin/network-config/*,
    # /api/admin/sync-rotate-all, /api/admin/autodetect) live in
    # ``dashboard/admin_config.py``. They still close over this app's
    # ``config`` + ``tunnels``, so we pass the four atomic-write / station
    # helpers as parameters rather than re-defining the lock there.
    from admin_config import register_admin_network_routes
    register_admin_network_routes(
        app,
        config,
        save_config=_save_config,
        config_to_dict=_config_to_dict,
        require_station=_require_station,
        push_rotate_to_station=_push_rotate_to_station,
        session_for_url=_session_for_url,
        config_path=config_path,
    )

    # ── Users CRUD + activity-log health ─────────────────────────────────
    # Moved to ``dashboard/admin_config.py`` (A-1 refactor). The handlers
    # only need module-level helpers (require_admin, _load_users,
    # _update_users, security.activity_log_stats), so they extract cleanly
    # without dragging closure state along.
    from admin_config import register_admin_user_routes
    register_admin_user_routes(app)

    # ── Overview / status / SSE — extracted to routes/overview.py ──────────
    # Convert _overview_stacks_full_ts to mutable container so overview module
    # can update it (Python closures can't rebind nonlocal from another module).
    _overview_stacks_full_ts_box = [_overview_stacks_full_ts]
    from routes.overview import register_overview_routes
    register_overview_routes(
        app, config, cache, tunnels,
        overview_stacks_cache=_overview_stacks_cache,
        overview_stacks_cache_lock=_overview_stacks_cache_lock,
        overview_stacks_full_ts=_overview_stacks_full_ts_box,
        overview_stacks_ttl=_OVERVIEW_STACKS_TTL,
        status_last_seen=_status_last_seen,
        vitals_last_seen=_vitals_last_seen,
        status_last_seen_lock=_status_last_seen_lock,
        vitals_last_seen_lock=_vitals_last_seen_lock,
        last_seen_max_age=_LAST_SEEN_MAX_AGE,
        kick_status_refresh=_kick_status_refresh,
        kick_vitals_refresh=_kick_vitals_refresh,
        record_status_last_seen=_record_status_last_seen,
        record_vitals_last_seen=_record_vitals_last_seen,
    )

    # ── Station proxy — extracted to routes/station_proxy.py ───────────────
    # (host, path) -> (expires, payload). Already TTL-then-age evicted at 500 in
    # route_helpers.proxy_get; the BoundedCache wrap is the hard ceiling so the
    # size bound holds regardless of the per-write eviction path.
    _PROXY_CACHE_MAX = _cache_cap("ROVIMEN_PROXY_CACHE_MAX", 512)
    _proxy_cache: BoundedCache = BoundedCache(_PROXY_CACHE_MAX)
    _proxy_cache_lock = threading.Lock()
    from routes.station_proxy import register_station_proxy_routes
    _get_timelapses_swr = register_station_proxy_routes(
        app, config, tunnels, cache,
        timelapses_swr_cache=_timelapses_swr_cache,
        timelapses_swr_inflight=_timelapses_swr_inflight,
        timelapses_swr_lock=_timelapses_swr_lock,
        timelapses_fresh_ttl=_TIMELAPSES_FRESH_TTL,
        platepar_cache=_platepar_cache,
        platepar_ttl=_PLATEPAR_TTL,
        proxy_cache=_proxy_cache,
        proxy_cache_lock=_proxy_cache_lock,
        prefetch_executor=_prefetch_executor,
        archive_idx=_archive_idx,
    )

    # ── Network stats, coverage grid, version, global config PATCH,
    #    network page — extracted to routes/network.py ─────────────────────
    from routes.network import register_network_routes
    register_network_routes(app, config, save_config=_save_config)

    # ── Sky dome — extracted to routes/sky_dome.py ─────────────────────────
    # Only route registration happens on the boot-critical path here; the
    # scheduler thread is started later from _start_background_workers() (see
    # the deferred-startup section near the end of create_app).
    from routes.sky_dome import register_sky_dome_routes, start_dome_scheduler
    register_sky_dome_routes(
        app, config, tunnels, cache,
        platepar_cache=_platepar_cache,
        platepar_ttl=_PLATEPAR_TTL,
    )

    # ── Station operations — extracted to routes/station_ops.py ─────────
    from routes.station_ops import register_station_ops_routes
    register_station_ops_routes(
        app, config, tunnels, cache,
        rms_plots_list_cache=_rms_plots_list_cache,
        rms_plots_list_cache_lock=_rms_plots_list_cache_lock,
        rms_plots_list_ttl=_RMS_PLOTS_LIST_TTL,
        proxy_cache=_proxy_cache,
        proxy_cache_lock=_proxy_cache_lock,
    )



    def _archive_nights(cam_code: str) -> list[str]:
        """Return dates that have at least one .mkv/.mp4 in meteors/.
        Reads directly from the in-memory archive index — O(1), no SSHFS."""
        return _archive_idx.nights_with_meteors(cam_code)
    def _read_archive_locked_chunks(cam_code: str, date: str) -> list[dict]:
        """Return locked-chunk dicts for a (cam, date) from the VPS archive.

        Uses the uploaded state.json (same schema as the station's) to recover
        full lock metadata — lock_type, meteor_time — so the result is
        interchangeable with the station API's /api/chunks response shape.

        Empty list if no archive, or no state.json, or no locked chunks.
        """
        state_path = ARCHIVE_PATH / cam_code / date / "state.json"

        def _read_state():
            if not state_path.exists():
                return None
            return json.loads(state_path.read_text())

        state = _with_sshfs_timeout(_read_state, timeout=5.0, default=None)
        if state is None:
            return []
        ni = _archive_idx.night_files(cam_code, date)
        meteor_names = set(ni.meteor_files) if ni else set()
        stack_names = ni.stack_files if ni else frozenset()
        out: list[dict] = []
        for fname, info in (state.get("chunks") or {}).items():
            if not isinstance(info, dict):
                continue
            lock = info.get("lock")
            if not lock:
                continue
            if fname not in meteor_names:
                continue
            time_str = "00:00:00"
            parts = fname.split("_")
            if len(parts) >= 3 and len(parts[2]) == 6 and parts[2].isdigit():
                t = parts[2]
                time_str = f"{t[:2]}:{t[2:4]}:{t[4:6]}"
            stack_name = fname.replace("_color.mkv", "_stack.webp").replace(".mp4", "_stack.webp")
            if stack_name in stack_names:
                stack_subdir = "stacks"
            elif stack_name in meteor_names:
                stack_subdir = "meteors"
            else:
                stack_subdir = None
            mt_str = lock.get("meteor_time") if isinstance(lock, dict) else None
            det_time_str = lock.get("detection_time") if isinstance(lock, dict) else None
            det_offset = compute_detection_offset(fname, mt_str, det_time_str)
            out.append({
                "filename": fname,
                "time": time_str,
                "stack": stack_name if stack_subdir else None,
                "stack_subdir": stack_subdir,
                "size_mb": None,
                "locked": True,
                "lock_type": lock.get("lock_type") if isinstance(lock, dict) else "detection",
                "detection_offset_s": det_offset,
                "meteor_time": mt_str,
                "reencoded": info.get("reencoded", False),
                "source": "archive",
            })
        out.sort(key=lambda c: c["time"])
        return out

    # Expose to extracted route modules (routes.detections) that need these
    # at request time. The functions close over module-level _archive_idx
    # and station_client helpers, so they can't be trivially imported.
    app._archive_nights = _archive_nights  # type: ignore[attr-defined]
    app._read_archive_locked_chunks = _read_archive_locked_chunks  # type: ignore[attr-defined]


    # ── Media / videodb / stream — extracted to routes/media.py ────────────
    from routes.media import register_media_routes
    register_media_routes(
        app, config, tunnels, cache,
        archive_idx=_archive_idx,
        morning_done=morning_done,
        bw_prefetched=bw_prefetched,
        color_prefetched=color_prefetched,
        prefetched_lock=_prefetched_lock,
        prefetched_max_age_s=_PREFETCHED_MAX_AGE_S,
        prefetch_night=_prefetch_night,
        prefetch_executor=_prefetch_executor,
        prefetch_archive_thumbs=_prefetch_archive_thumbs,
        proxy_cache=_proxy_cache,
        proxy_cache_lock=_proxy_cache_lock,
        live_stream_triple_sem=_live_stream_triple_sem,
        live_stream_global_sem=_live_stream_global_sem,
        live_stream_retry_after_s=_LIVE_STREAM_RETRY_AFTER_S,
    )

    # ── Archive endpoints -- extracted to routes/archive.py ────────────────
    from routes.archive import register_archive_routes
    register_archive_routes(
        app, config, tunnels, cache,
        archive_idx=_archive_idx,
        nights_full_cache=_nights_full_cache,
        nights_full_ttl=_NIGHTS_FULL_TTL,
        prefetch_archive_thumbs=_prefetch_archive_thumbs,
    )

    # ── Detections + live-feed -- extracted to routes/detections.py ────────
    from routes.detections import register_detections_routes
    register_detections_routes(
        app, config, tunnels, cache,
        archive_idx=_archive_idx,
        detections_cache=_detections_cache,
        detections_ttl=_DETECTIONS_TTL,
    )

    # ── Highlights API routes — extracted to routes/highlights.py ────────
    from routes.highlights import register_highlights_routes
    register_highlights_routes(app, config, cache, tunnels)

    # ── Social media report (admin-only weekly post generator) ────────────
    from routes.social import register_social_routes
    register_social_routes(app, config)

    # ── Compilation builder (cart, build pipeline, downloadable MP4) ──────
    # Extracted to dashboard/routes/compilation.py
    from routes.compilation import register_compilation_routes
    register_compilation_routes(app, config, tunnels, cache)

    # ── Startup ───────────────────────────────────────────────────────────

    def _prefetch_rms_plots_for_night(
        host_key: str, cam_code: str, date: str
    ) -> None:
        """Fetch the RMS plots LIST and every plot image into THUMB_CACHE.

        This is what makes the per-station RMS panel and the overview captured
        stack render instantly on cold dashboards, AND keeps them visible when
        the station goes offline (8-day disk TTL covers most outages).
        """
        try:
            plots = station_get_raw(
                config, tunnels, host_key,
                f"/api/rms/plots/{cam_code}/{date}", timeout=10,
            )
        except Exception:
            return
        if not isinstance(plots, list):
            return
        # Persist list JSON
        try:
            p = _rms_plots_list_cache_path(host_key, cam_code, date)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(plots))
            # 3-tuple keeps schema in sync with api_rms_plots_proxy; None
            # for has_color means "not yet probed" so the next route hit
            # will run the HEAD check.
            with _rms_plots_list_cache_lock:
                _rms_plots_list_cache[(host_key, cam_code, date)] = (
                    time.monotonic() + _RMS_PLOTS_LIST_TTL, plots, None,
                )
                if len(_rms_plots_list_cache) > 2000:
                    now_mono = time.monotonic()
                    expired = [k for k, v in _rms_plots_list_cache.items()
                               if v[0] < now_mono]
                    for k in expired:
                        _rms_plots_list_cache.pop(k, None)
                    if len(_rms_plots_list_cache) > 2000:
                        by_age = sorted(_rms_plots_list_cache.items(),
                                        key=lambda x: x[1][0])
                        for k, _ in by_age[:500]:
                            _rms_plots_list_cache.pop(k, None)
        except Exception:
            pass
        # Update the overview captured-stack last-known-good entry
        for entry in plots:
            fn = entry.get("filename", "")
            if "captured_stack" in fn:
                with _overview_stacks_cache_lock:
                    _overview_stacks_cache[(host_key, cam_code)] = {
                        "host": host_key,
                        "cam": cam_code,
                        "date": date,
                        "filename": fn,
                        "label": entry.get("label", "Captured stack"),
                    }
                break
        # Fetch each plot image into THUMB_CACHE so the per-station detail
        # page renders instantly. Cache the raw upstream bytes — rotation
        # for cameras with rotate: true is now applied by the client via
        # the ``.thumb-rotated`` CSS class. Skipping the PIL pass saves
        # ~20 ms per image on cold cache and lets cached bytes serve any
        # future orientation policy without re-encoding.
        for entry in plots:
            fn = entry.get("filename")
            if not fn or not re.match(r"^[\w._-]+\.(jpg|png|webp)$", fn):
                continue
            cache_file = _rms_plot_cache_path(host_key, cam_code, date, fn)
            if cache_file.exists():
                continue
            is_color_meteor = fn == COLOR_METEOR_STACK_FILENAME
            try:
                if is_color_meteor:
                    url = _media_url(host_key, "api", "color-meteor-stack", cam_code, date)
                else:
                    url = _media_url(host_key, "api", "rms", "plot_image",
                                     cam_code, date, fn)
                resp = _session_for_url(url).get(url, timeout=10)
                if resp.status_code != 200:
                    continue
                # Rotation moved client-side to a CSS class; store raw bytes.
                if not _thumb_cache_has_space():
                    continue
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(resp.content)
            except Exception:
                continue

    def _thumb_prefetch_loop() -> None:
        """Background loop: proactively cache everything an offline station
        would otherwise render as missing.

        Runs ONE iteration immediately on startup (so a fresh dashboard
        restart warms the caches within seconds), then every 5 minutes.
        For each ONLINE station × camera × current night we (re-)warm:
          1. Chunk thumbnails (BW immediately, color force-overwrite when
             morning_done flips)
          2. RMS plots list + every plot image (radiants, ff_intervals,
             captured_stack, meteor_stack, color_meteor_stack)
          3. Overview captured-stack last-known-good entry per (host, cam)

        Also refreshes _overview_stacks_cache directly via _refresh_overview_stacks
        so /api/overview/stacks is warm without waiting for a user request.

        Offline stations are skipped here so we don't waste cycles, but the
        previously-cached entries remain on disk + in memory and the API
        routes (api_overview_stacks, api_rms_plots_proxy, api_platepar_proxy,
        api_rms_plot_image_proxy) all serve stale.
        """
        nonlocal _overview_stacks_full_ts
        while True:
            try:
                # Prune stale dedup keys before this iteration so the dicts
                # don't grow without bound over deploy uptime × cameras × nights.
                # A key older than the retention horizon is unreachable in
                # practice (the night has rolled out of the chunks endpoint)
                # but its presence still costs memory and blocks the rare
                # retroactive reprefetch.
                cutoff_mono = time.monotonic() - _PREFETCHED_MAX_AGE_S
                with _prefetched_lock:
                    for stale_key in [
                        k for k, ts in bw_prefetched.items() if ts < cutoff_mono
                    ]:
                        bw_prefetched.pop(stale_key, None)
                    for stale_key in [
                        k for k, ts in color_prefetched.items() if ts < cutoff_mono
                    ]:
                        color_prefetched.pop(stale_key, None)
                    # morning_done is now float-stamped so we can prune it here
                    # alongside its siblings — prevents unbounded growth across
                    # long deploy uptimes with many historical nights.
                    for stale_key in [
                        k for k, ts in morning_done.items() if ts > 0.0 and ts < cutoff_mono
                    ]:
                        morning_done.pop(stale_key, None)
                with _archive_thumb_prefetch_lock:
                    for stale_key in [
                        k for k, ts in _archive_thumb_prefetched.items() if ts < cutoff_mono
                    ]:
                        _archive_thumb_prefetched.pop(stale_key, None)
                _overview_stacks_full_ts_box[0] = time.monotonic()
                app._refresh_overview_stacks()
                now = datetime.now(timezone.utc)
                date_str = (
                    (now - timedelta(days=1)).strftime("%Y%m%d")
                    if now.hour < 12
                    else now.strftime("%Y%m%d")
                )
                for i, (host_key, station) in enumerate(config.stations.items()):
                    if i > 0:
                        time.sleep(2)
                    # Skip only if explicitly known offline. status=None at
                    # cold start (status poller hasn't run yet) — attempt
                    # the fetch; failures are caught downstream silently.
                    status = cache.get_status(host_key)
                    if status is not None and not status.get("online"):
                        continue
                    for cam in station.cameras:
                        cam_code = cam.code
                        key = (host_key, cam_code, date_str)
                        # 1) Chunks + chunk thumbnails (existing behaviour)
                        try:
                            raw = station_get_raw(
                                config, tunnels, host_key,
                                f"/api/chunks/{cam_code}/{date_str}",
                                timeout=10,
                            )
                        except Exception:
                            raw = None
                        if isinstance(raw, dict) and "chunks" in raw:
                            chunks_list = raw["chunks"]
                            was_done = morning_done.get(key, False)
                            is_done = bool(raw.get("morning_done", False))
                            # Store monotonic timestamp so _prefetched_lock pruning
                            # can evict entries for historical nights by age.
                            morning_done[key] = time.monotonic() if is_done else 0.0
                        elif isinstance(raw, list):
                            chunks_list = raw
                            is_done = False
                            was_done = morning_done.get(key, False)
                        else:
                            chunks_list = []
                            is_done = False
                            was_done = morning_done.get(key, False)
                        if chunks_list:
                            now_mono = time.monotonic()
                            with _prefetched_lock:
                                bw_seen = key in bw_prefetched
                                if not bw_seen:
                                    bw_prefetched[key] = now_mono
                                color_seen = key in color_prefetched
                                if is_done and not was_done and not color_seen:
                                    color_prefetched[key] = now_mono
                            if not bw_seen:
                                _prefetch_executor.submit(
                                    _prefetch_night,
                                    host_key, cam_code, date_str, chunks_list,
                                    force=False,
                                )
                            if is_done and not was_done and not color_seen:
                                _prefetch_executor.submit(
                                    _prefetch_night,
                                    host_key, cam_code, date_str, chunks_list,
                                    force=True,
                                )
                        # 2+3) RMS plots list + images, AND the previous night
                        # too — overview shows yesterday's captured stack until
                        # tonight's first detection lands.
                        for d in (date_str, (now - timedelta(days=1)).strftime("%Y%m%d")):
                            _prefetch_executor.submit(
                                _prefetch_rms_plots_for_night,
                                host_key, cam_code, d,
                            )
            except Exception:
                logger.exception("thumb prefetch loop error")
            logger.info(
                "cache sizes: proxy=%d rms_plots=%d overview_stacks=%d "
                "detections=%d bw_prefetched=%d color_prefetched=%d",
                len(_proxy_cache), len(_rms_plots_list_cache),
                len(_overview_stacks_cache), len(_detections_cache),
                len(bw_prefetched), len(color_prefetched),
            )
            time.sleep(300)

    def _prewarm_archive_index() -> None:
        try:
            disk_path = CACHE_PATH / "archive_index.json"
            loaded = _archive_idx.load_from_disk(disk_path)
            if loaded:
                logger.info("archive index: loaded %d nights from disk cache", loaded)
            time.sleep(5)
            count = _archive_idx.build_full()
            logger.info("archive index: built %d nights from SSHFS scan", count)
            _archive_idx.save_to_disk(disk_path)
            cam_codes = [cam.code for st in config.stations.values() for cam in st.cameras]
            for cam_code in cam_codes:
                app._refresh_nights_full(cam_code)  # type: ignore[attr-defined]
                time.sleep(0.5)
            prefetched = 0
            for cam_code in cam_codes:
                nights = _archive_idx.nights(cam_code)
                if not nights:
                    continue
                owners = _host_keys_for_camera(config, cam_code)
                if not owners:
                    continue
                host_key = owners[0]
                for date in nights[:3]:
                    _prefetch_archive_thumbs(host_key, cam_code, date)
                    prefetched += 1
                time.sleep(1)
            if prefetched:
                logger.info("archive thumbs: queued prefetch for %d camera-nights", prefetched)
        except Exception:
            logger.exception("archive index build failed")

    def _archive_index_refresher() -> None:
        ticks = 0
        while True:
            time.sleep(60)
            ticks += 1
            try:
                if ticks % 60 == 0:
                    count = _archive_idx.build_full()
                    logger.info("archive index: full rebuild — %d nights", count)
                else:
                    count = _archive_idx.refresh_recent(days=3)
                if count:
                    disk_path = CACHE_PATH / "archive_index.json"
                    _archive_idx.save_to_disk(disk_path)
                    if ticks % 60 != 0:
                        logger.info("archive index: refreshed %d recent nights", count)
            except Exception:
                logger.exception("archive index refresh failed")

    # ── Deferred background startup ────────────────────────────────────────
    # NOTHING that spawns a thread or blocks on a lock/IO may run between the
    # worker fork and the worker becoming ready-to-serve. Starting the pollers,
    # prefetch loop, tunnel init and archive-index build inline in create_app()
    # meant those threads began contending for the import lock / logging lock /
    # SSHFS+SQLite handles WHILE the boot thread was still finishing its own
    # lazy imports and route registration — a classic fork-after-threads
    # deadlock that probabilistically wedged the worker on `systemctl restart`
    # (worker stuck in futex_wait at ~20 MB RSS, never serving).
    #
    # Instead we collect every background start into one idempotent closure and
    # arm it to run OFF the boot-critical path (a short timer here, plus the
    # gunicorn post_worker_init hook in gunicorn.conf.py). By the time it fires
    # the worker has already returned this app to gunicorn, bound its socket and
    # begun serving — so no lock it touches can block the boot thread.
    _bg_started = threading.Event()
    _bg_start_lock = threading.Lock()

    def _start_background_workers() -> None:
        """Start all long-lived background workers exactly once, off the boot
        path. No-op when ROVIMEN_DISABLE_BACKGROUND=1 (tests, E2E launcher)."""
        if not _background_workers_enabled():
            return
        with _bg_start_lock:
            if _bg_started.is_set():
                return
            _bg_started.set()

        # Rehydrate the SSHFS-walk caches from disk so the dashboard never
        # cold-starts. Entries keep their original age via the wall-clock
        # back-dating in _disk_cache_load_into, so the stale-while-revalidate
        # path in api_archive_nights_full kicks in correctly: users get
        # last-known data instantly, the background refresh runs on demand.
        n_full = _disk_cache_load_into("nights_full", _nights_full_cache)
        if n_full:
            logger.info("Rehydrated nights_full cache from disk: %d entries", n_full)

        # start_all() blocks ~2-10 s per unreachable host (serial, jump-host SSH
        # handshake); it already runs in its own daemon thread so the watchdog
        # and on-demand get_api_base() reconnect without blocking request paths.
        threading.Thread(target=tunnels.start_all, name="tunnel-init", daemon=True).start()
        tunnels.start_watchdog()
        start_polling(config, tunnels, cache)
        threading.Thread(target=_thumb_prefetch_loop, daemon=True).start()
        start_dome_scheduler()
        # GMN public-API poller — backfills 35 days then refreshes the trailing
        # 5 days every 2 h. Sole writer of _gmn_cache; the user-facing routes
        # only read from it.
        _start_gmn_poller()
        # MDC shower reference data — fetched once on startup then weekly.
        from mdc_poller import start_mdc_poller
        start_mdc_poller()
        threading.Thread(target=_prewarm_archive_index, daemon=True).start()
        threading.Thread(target=_archive_index_refresher, daemon=True).start()
        logger.info("background workers started (deferred, off boot path)")

    # Expose the deferred hook so the gunicorn post_worker_init hook (and tests)
    # can trigger it explicitly; the timer below covers the inline-gunicorn and
    # `python rovimen_dashboard.py` launch paths that have no gunicorn hooks.
    app._start_background_workers = _start_background_workers  # type: ignore[attr-defined]

    # Mount the public read-only API. Registered before CSRF-exempt sweep
    # so its /api/public/* routes get exempted too. The timelapses closure
    # is the SWR-cached accessor (not the raw compute) so a misbehaving
    # public consumer can't trigger fresh per-cam SSHFS walks on every hit.
    import public_api
    public_api.register_public_routes(
        app,
        config=config,
        compute_detections_payload=app._compute_detections_payload,  # type: ignore[attr-defined]
        get_timelapses_payload=_get_timelapses_swr,
        station_cache_get_status=cache.get_status,
        limiter=limiter,
    )

    # Mount the reversed-HTTP push ingest API (Phase A — additive; see
    # docs/reversed_http_push_design.md). Stations POST their own telemetry
    # here and it lands in the SAME StationCache + detections.db the existing
    # pollers write, so the dashboard reads either source transparently. The
    # pollers (station_client.py, index_poller.py) are untouched. Registered
    # before the CSRF-exempt sweep so its /api/ingest/* routes are exempted;
    # authenticated per-station key (X-Station-Key), so it's also exempt from
    # the login gate via security._AUTH_GATE_PUBLIC_PREFIXES.
    import ingest_api
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=cache.set_status,
        cache_set_vitals=cache.set_vitals,
        cache_set_live_thumb=cache.set_live_thumb,
        known_stations=lambda: set(config.stations.keys()),
        limiter=limiter,
    )

    # Mount the server->station command channel (reversed-HTTP push §3). Admins
    # enqueue ed25519-signed, allowlisted commands; stations long-poll and ack
    # over the same per-station key as ingest. Registered before the CSRF-exempt
    # sweep so its /api/fleet/* routes are exempted. The station-key GET/ack
    # routes bypass the login gate via _AUTH_GATE_PUBLIC_PREFIXES (they do their
    # own per-station-key auth); the enqueue POST keeps @require_admin, which is
    # the real gate — a signed command can only be issued by an admin session.
    import command_api
    command_api.register_command_routes(
        app,
        known_stations=lambda: set(config.stations.keys()),
        limiter=limiter,
    )

    # Exempt /api/* from CSRF — they're session+SameSite=Lax protected,
    # JSON endpoints called from same-origin JS. Form-based POSTs
    # (/login, /set-password, /totp/*) DO get CSRF checks because they
    # carry a {{ csrf_token() }} hidden input.
    security.exempt_api_routes_from_csrf(app, csrf)

    # Arm the deferred background startup off the boot-critical path. The timer
    # fires from a fresh, short-lived thread ~0.5 s after create_app() has
    # returned this app to the caller (gunicorn/Werkzeug) — by which point the
    # worker has bound its socket and is serving. This is the launch-agnostic
    # trigger: it fires under the plain inline `gunicorn 'rovimen_dashboard:...'`
    # ExecStart and under `python rovimen_dashboard.py`. When gunicorn.conf.py is
    # used, post_worker_init ALSO calls _start_background_workers(), but the
    # idempotency guard makes the double-trigger a no-op. Skipped entirely when
    # background work is disabled so tests don't spawn a stray timer thread.
    if _background_workers_enabled():
        threading.Timer(0.5, _start_background_workers).start()

    return app


def redirect_to(url: str) -> Response:
    return Response(status=302, headers={"Location": url})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _init_and_create_app(config_path: Path | None = None) -> Flask:
    """Shared init for both direct and gunicorn entry points."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if config_path is None:
        config_path = Path(
            os.environ.get(
                "ROVIMEN_CONFIG",
                Path(__file__).parent / "dashboard_config.yaml",
            )
        )
    config = load_config(config_path)
    logger.info(
        "Loaded %d stations: %s",
        len(config.stations),
        ", ".join(config.stations.keys()),
    )
    THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_prune_thumb_cache, daemon=True).start()
    threading.Thread(
        target=_ensure_known_hosts, args=(config,), daemon=True,
    ).start()
    return create_app(config, config_path)


# gunicorn entry point: `gunicorn 'rovimen_dashboard:wsgi_app()'`
def wsgi_app() -> Flask:
    return _init_and_create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="ROVIMEN Dashboard")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "dashboard_config.yaml",
    )
    args = parser.parse_args()
    app = _init_and_create_app(args.config)
    print(f"ROVIMEN Dashboard -> http://localhost:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    import sys

    if "--test-gmn" in sys.argv:
        _gmn_sanity_check()
    main()
