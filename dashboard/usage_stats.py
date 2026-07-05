"""Usage analytics derived from the audit + activity logs.

Two flat JSON-lines files feed every figure here:

* ``audit.log``    — one record per security-relevant event. We read the
  ``login`` / ``magic_login`` events (``result == "ok"``) to derive each
  user's *last login*.
* ``activity.log`` — one record per authenticated, non-poll request
  (see ``security.log_request_activity``). This is the raw material for
  feature-usage counts, per-user *active time*, and the daily-volume
  series.

Everything is best-effort: the logs are append-only text written by a
separate daemon, may be mid-rotation, and individual lines can be
truncated or malformed. Every parse is wrapped so a bad line is skipped
rather than failing the whole report. All timestamps are ISO-8601 UTC as
written by ``security.audit`` / ``log_request_activity``.

The functions are read-only and hold no locks — safe to call from an
admin request thread. Scanning is bounded by ``_MAX_SCAN_BYTES`` (we tail
the file when it is larger) so a runaway log can never make an admin
endpoint hang.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

AUDIT_LOG_PATH = Path(
    os.environ.get("ROVIMEN_AUDIT_LOG_PATH", "/opt/rovimen/audit.log")
)
ACTIVITY_LOG_PATH = Path(
    os.environ.get("ROVIMEN_ACTIVITY_LOG_PATH", "/opt/rovimen/activity.log")
)

# Tail at most this many bytes from each log. The activity log is the high
# volume one; 64 MiB of one-line-per-request is on the order of hundreds of
# thousands of records — far more than any report window needs, and a hard
# ceiling against a pathologically large file blocking the request thread.
_MAX_SCAN_BYTES = 64 * 1024 * 1024

# Consecutive same-user activity records less than this far apart are
# treated as continuous engagement; a longer gap starts a new session and
# the idle stretch in between is NOT counted. Each gap is also capped at
# this value so "active time" approximates wall-clock attention rather than
# raw last-minus-first (which would count a user who left a tab open).
_IDLE_GAP_S = 15 * 60


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iter_log(path: Path, since: datetime) -> Iterator[dict[str, Any]]:
    """Yield JSON records from ``path`` whose ``ts`` is >= ``since``.

    Tails the last ``_MAX_SCAN_BYTES`` when the file is bigger (dropping a
    possibly-partial first line). Malformed lines and records without a
    parseable ``ts`` are skipped silently.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            if size > _MAX_SCAN_BYTES:
                fh.seek(size - _MAX_SCAN_BYTES)
                fh.readline()  # discard the partial line we landed in
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get("ts", ""))
                if ts is None or ts < since:
                    continue
                rec["_ts"] = ts
                yield rec
    except OSError as exc:
        logger.warning("usage_stats: cannot read %s: %s", path, exc)


# ── Feature classification ───────────────────────────────────────────────
#
# Maps a request (method + path) to a human-facing feature label. First
# matching prefix wins, so order matters — most specific first. Unmatched
# paths return None and are excluded from feature stats. Page loads (GET of
# an HTML route) and
# the API calls behind each tab are deliberately bucketed under the same
# label so "Events" counts both the page view and its data fetches.

_FEATURE_RULES: list[tuple[str, str]] = [
    ("/api/sky_dome", "Celestial Dome"),
    ("/api/latest_frame", "Final Data Products"),
    ("/api/latest_stack", "Final Data Products"),
    ("/api/timelapses", "Timelapses"),
    ("/api/detections", "Detections"),
    ("/api/events", "Events"),
    ("/api/compilation", "Clip Compilation"),
    ("/api/compile", "Clip Compilation"),
    ("/api/rms", "RMS / Detection"),
    ("/api/nights", "Video DB"),
    ("/api/chunks", "Video DB"),
    ("/api/archive", "Archive"),
    ("/api/settings", "Station Settings"),
    ("/api/admin", "Admin actions"),
    ("/api/public", "Public API"),
    ("/events", "Events"),
    ("/live_view", "Live View"),
    ("/stream", "Live View"),
    ("/network", "Admin: Network"),
    ("/config", "Admin: Config"),
    ("/admin", "Admin"),
    ("/dashboard", "Station Dashboard"),
    ("/overview", "Overview"),
]


def _classify(path: str) -> str | None:
    if path == "/" or path == "":
        return "Overview"
    for prefix, label in _FEATURE_RULES:
        if path == prefix or path.startswith(prefix):
            return label
    return None


# ── TTL cache for expensive scans ─────────────────────────────────────────

_usage_cache: tuple[float, dict, int] | None = None
_usage_cache_lock = threading.Lock()
_activity_cache: tuple[float, dict, int] | None = None
_activity_cache_lock = threading.Lock()
_USAGE_CACHE_TTL = 60  # seconds


# ── Per-user activity (last login + active time) ──────────────────────────


def _last_logins(since: datetime) -> dict[str, str]:
    """username -> ISO timestamp of most recent successful login."""
    out: dict[str, str] = {}
    for rec in _iter_log(AUDIT_LOG_PATH, since):
        if rec.get("event") not in ("login", "magic_login"):
            continue
        if rec.get("result") != "ok":
            continue
        user = rec.get("username")
        if not user or user == "?":
            continue
        ts = rec["_ts"].isoformat(timespec="seconds")
        # Lines are append-only chronological, so the last write wins.
        out[user] = ts
    return out


def compute_user_activity(days: int = 90) -> dict[str, dict[str, Any]]:
    """Per-user engagement summary over the trailing ``days`` window.

    Returns ``{username: {last_login, last_seen, requests, sessions,
    active_seconds}}``. ``active_seconds`` sums the gaps between a user's
    consecutive activity records, capping each gap at ``_IDLE_GAP_S`` and
    dropping gaps beyond it (session boundaries) — an attention estimate,
    not exact wall-clock.

    Results are cached for ``_USAGE_CACHE_TTL`` seconds to avoid
    re-scanning the log files on every request.
    """
    global _activity_cache
    now = time.monotonic()
    with _activity_cache_lock:
        if _activity_cache and (now - _activity_cache[0]) < _USAGE_CACHE_TTL:
            if _activity_cache[2] == days:
                return _activity_cache[1]

    result = _compute_user_activity_uncached(days)

    with _activity_cache_lock:
        _activity_cache = (time.monotonic(), result, days)
    return result


def _compute_user_activity_uncached(days: int) -> dict[str, dict[str, Any]]:
    """Inner implementation without caching."""
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    # user -> ordered list of activity timestamps
    stamps: dict[str, list[datetime]] = defaultdict(list)
    for rec in _iter_log(ACTIVITY_LOG_PATH, since):
        user = rec.get("user")
        if not user:
            continue
        stamps[user].append(rec["_ts"])

    last_logins = _last_logins(since)

    out: dict[str, dict[str, Any]] = {}
    # Union of everyone seen in either log so a user who logged in but did
    # nothing still gets a row.
    for user in set(stamps) | set(last_logins):
        ts_list = sorted(stamps.get(user, []))
        active = 0
        sessions = 1 if ts_list else 0
        for prev, cur in zip(ts_list, ts_list[1:]):
            gap = (cur - prev).total_seconds()
            if gap <= _IDLE_GAP_S:
                active += gap
            else:
                sessions += 1
        out[user] = {
            "last_login": last_logins.get(user),
            "last_seen": ts_list[-1].isoformat(timespec="seconds") if ts_list else None,
            "requests": len(ts_list),
            "sessions": sessions,
            "active_seconds": int(active),
        }
    return out


# ── Aggregate usage report ────────────────────────────────────────────────


def compute_usage(days: int = 7) -> dict[str, Any]:
    """Site-wide usage report over the trailing ``days`` window.

    Results are cached for ``_USAGE_CACHE_TTL`` seconds to avoid
    re-scanning the log files on every request.

    Shape::

        {
          "days", "since", "generated_at",
          "totals":   {"requests", "active_users", "sessions"},
          "features": [{"feature", "hits", "users", "last_used"}, ...],
          "users":    [{"user", "last_login", "last_seen", "requests",
                        "sessions", "active_seconds"}, ...],
          "daily":    [{"date", "hits"}, ...]   # oldest -> newest
        }
    """
    global _usage_cache
    now_mono = time.monotonic()
    with _usage_cache_lock:
        if _usage_cache and (now_mono - _usage_cache[0]) < _USAGE_CACHE_TTL:
            if _usage_cache[2] == days:
                return _usage_cache[1]

    result = _compute_usage_uncached(days)

    with _usage_cache_lock:
        _usage_cache = (time.monotonic(), result, days)
    return result


def _compute_usage_uncached(days: int) -> dict[str, Any]:
    """Inner implementation without caching."""
    days = max(1, min(days, 365))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    feat_hits: dict[str, int] = defaultdict(int)
    feat_users: dict[str, set[str]] = defaultdict(set)
    feat_last: dict[str, datetime] = {}
    daily: dict[str, int] = defaultdict(int)
    total_requests = 0

    for rec in _iter_log(ACTIVITY_LOG_PATH, since):
        path = rec.get("path") or ""
        user = rec.get("user") or "?"
        ts = rec["_ts"]
        feat = _classify(path)
        if feat is not None:
            feat_hits[feat] += 1
            feat_users[feat].add(user)
            if feat not in feat_last or ts > feat_last[feat]:
                feat_last[feat] = ts
        daily[ts.date().isoformat()] += 1
        total_requests += 1

    features = sorted(
        (
            {
                "feature": f,
                "hits": feat_hits[f],
                "users": len(feat_users[f]),
                "last_used": feat_last[f].isoformat(timespec="seconds"),
            }
            for f in feat_hits
        ),
        key=lambda d: d["hits"],
        reverse=True,
    )

    activity = compute_user_activity(days=days)
    users = sorted(
        (
            {"user": u, **stats}
            for u, stats in activity.items()
        ),
        key=lambda d: (d.get("last_seen") or ""),
        reverse=True,
    )

    # Dense daily series so the chart has a bar for every day in the window,
    # including zero-traffic days. Anchored on today and walking back so the
    # last bar is always the current day (oldest -> newest).
    daily_series = []
    for i in range(days):
        d = (now.date() - timedelta(days=days - 1 - i)).isoformat()
        daily_series.append({"date": d, "hits": daily.get(d, 0)})

    return {
        "days": days,
        "since": since.isoformat(timespec="seconds"),
        "generated_at": now.isoformat(timespec="seconds"),
        "totals": {
            "requests": total_requests,
            "active_users": len([u for u in activity.values() if u["requests"]]),
            "sessions": sum(u["sessions"] for u in activity.values()),
        },
        "features": features,
        "users": users,
        "daily": daily_series,
    }
