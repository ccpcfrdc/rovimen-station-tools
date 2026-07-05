"""GMN (Global Meteor Network) data fetching and background poller."""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

import gmn_data

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GMN public REST API integration (background poller + per-date cache)
# ---------------------------------------------------------------------------
#
# Upstream is the Datasette-backed REST API documented at
# https://gmn-python-api.readthedocs.io/en/latest/rest_api.html. It exposes a
# `meteor_summary` table with ~85 columns of computed trajectory parameters
# (begin/end lat-lon-alt, peak abs mag, IAU shower code, geocentric velocity,
# participating stations, ...). Auth: none. Hard limits per upstream docs:
# - 1000 rows max per page
# - queries running >3 s are blocked
# - response cache 1 h server-side
#
# Architecture: a daemon poller refreshes the per-date cache on a slow tick
# (default 2 h). User-facing endpoints NEVER call upstream — they only read
# the prebuilt cache. This keeps user requests instant even when upstream is
# slow or down, and keeps our outbound traffic bounded.
#
# `participating_stations` comes back as a comma-separated string (e.g.
# "RO000A,US0008"). Each event is tagged at fetch time with `has_ro`
# (≥1 station starts with "RO") vs `de_only` (only DE* witnesses, no RO).
# RO-witnessed events drive the monthly counter; DE-only events are still
# stored so the frontend can plot them as a secondary, muted overlay.
# Upstream notes (2026-05-22):
# The documented `/gmn_rest_api/meteor_summary` proxy is currently broken on
# their server — it generates a malformed SQL JOIN and 400s on every query.
# The underlying Datasette at `/gmn_data_store.json?sql=...` works fine and
# is what we drive directly. A single JOIN-with-GROUP_BY query also exceeds
# their 3 s SQL budget, so we issue 3 simple indexed queries per night
# (meteor list, participating_station for those IDs, shower lookup) and
# join in Python.
_GMN_SQL_URL = "https://explore.globalmeteornetwork.org/gmn_data_store.json"
_GMN_POLL_INTERVAL_S = 2 * 3600          # 2 h between full passes
_GMN_RECENT_WINDOW_DAYS = 5              # dates the poller refreshes every tick
_GMN_BACKFILL_WINDOW_DAYS = 32
# Upstream rate-limited us hard when we ran 3-6 parallel workers — every
# query stalled to the full timeout for ~10 min afterwards. Serial-only
# with throttling between requests is the only safe pattern: ~150 queries
# per backfill at 1 q/s = ~3 min, well under any reasonable rate limit.
_GMN_BACKFILL_WORKERS = 1                # serial; do NOT raise without backoff
_GMN_THROTTLE_S = 1.0                    # min seconds between consecutive queries
_GMN_HTTP_TIMEOUT = 30                   # round-trip + queueing dominate even though
                                         # the underlying SQL budget is 3 s.
_GMN_BATCH = 150                         # event IDs per IN(...) clause
# "Our" station codes are supplied by the dashboard config at startup (see
# gmn_data.configure_station_codes). Events are tagged has_ro/de_only through
# gmn_data.tag_witness, so the poller carries no hard-coded RO/DE prefixes.
# Shower lookup is small (~1000 rows) and immutable; cache forever in-process.
_gmn_shower_cache: dict[int, str] = {}
_gmn_shower_cache_lock = threading.Lock()
# {date_str (YYYY-MM-DD): payload}.
# payload = {date, total_upstream, events, has_ro_count, de_only_count,
#            last_fetched_utc, error?}
_gmn_cache: dict[str, dict[str, Any]] = {}
_gmn_cache_lock = threading.Lock()
# Set once the first backfill pass has finished, so the endpoint can answer
# "still warming up" honestly for very early requests.
_gmn_initial_pass_done = threading.Event()


def _gmn_split_stations(raw: Any) -> list[str]:
    """Normalize participating_stations into a list[str].

    Upstream returns a comma-separated string. Defensive: tolerate already-list
    payloads, None, or other delimiters that have shown up historically (";",
    whitespace).
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if str(s).strip()]
    if not isinstance(raw, str):
        return []
    # Replace any plausible separator with comma, then split.
    cleaned = raw.replace(";", ",").replace("|", ",")
    return [s.strip() for s in cleaned.split(",") if s.strip()]


def _gmn_row_to_event(row: dict[str, Any]) -> dict[str, Any]:
    """Map an upstream meteor_summary row to the slim dashboard shape, tagged
    with ``has_ro`` (>=1 RO* witness) and ``de_only`` (only DE* witnesses)."""
    stations = _gmn_split_stations(row.get("participating_stations"))
    _is_ours, has_ro, de_only = gmn_data.tag_witness({s.upper() for s in stations})
    return {
        "id":             row.get("unique_trajectory_identifier"),
        "time":           row.get("beginning_utc_time"),
        "lat_begin":      row.get("latbeg_n_deg"),
        "lon_begin":      row.get("lonbeg_e_deg"),
        "lat_end":        row.get("latend_n_deg"),
        "lon_end":        row.get("lonend_e_deg"),
        "altitude_begin": row.get("htbeg_km"),
        "altitude_end":   row.get("htend_km"),
        "peak_mag":       row.get("peak_absmag"),
        "shower":         row.get("iau_code"),
        "velocity":       row.get("vgeo_km_s"),
        "stations":       stations,
        "has_ro":         has_ro,
        "de_only":        de_only,
    }


# Global serializer + last-request timestamp so all GMN HTTP calls in the
# process respect _GMN_THROTTLE_S. Concurrent callers wait their turn.
_gmn_request_lock = threading.Lock()
_gmn_last_request_t = 0.0


def _gmn_sql(sql: str) -> list[dict[str, Any]]:
    """Execute a SQL query against the GMN Datasette and return the rows
    (objects shape). Serialized + throttled across the process to stay
    well under upstream's rate limit — they will silently start stalling
    every request to the full timeout if we go too fast.

    Raises on HTTP error / non-JSON response so the caller can decide
    whether to treat it as upstream-down."""
    global _gmn_last_request_t
    with _gmn_request_lock:
        wait = _GMN_THROTTLE_S - (time.monotonic() - _gmn_last_request_t)
        if wait > 0:
            time.sleep(wait)
        _gmn_last_request_t = time.monotonic()
    resp = requests.get(
        _GMN_SQL_URL,
        params={"sql": sql, "_shape": "objects"},
        timeout=_GMN_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict) or not payload.get("ok", True):
        raise ValueError(f"upstream rejected query: {payload.get('error')!r}")
    return payload.get("rows") or []


def _gmn_load_shower_cache() -> None:
    """Pull the full shower table once and cache iau_no -> iau_code. Idempotent."""
    with _gmn_shower_cache_lock:
        if _gmn_shower_cache:
            return
    try:
        rows = _gmn_sql("SELECT iau_no, iau_code FROM shower")
    except (requests.RequestException, ValueError) as exc:
        logger.warning("GMN shower cache load failed: %s", exc)
        return
    with _gmn_shower_cache_lock:
        for r in rows:
            no = r.get("iau_no")
            code = r.get("iau_code")
            if no is not None and code:
                _gmn_shower_cache[int(no)] = str(code)


def _gmn_sql_quote_ids(ids: list[str]) -> str:
    """Quote a list of trajectory IDs for an IN(...) clause. The IDs follow
    a strict ``\\d{14}_[A-Za-z0-9]{5}`` shape so single-quoting is safe;
    still strip any quote characters defensively before joining."""
    cleaned = [i.replace("'", "") for i in ids]
    return ",".join(f"'{i}'" for i in cleaned)


def _gmn_fetch_multistation(date_str: str) -> dict[str, Any]:
    """Fetch all multi-station trajectories for a UTC night.

    Delegates to gmn_data.events_for_date(), which downloads the GMN's
    documented monthly traj_summary text file (~45 MB) once an hour and
    parses it locally. We deliberately moved off the Datasette SQL
    endpoint at ``explore.globalmeteornetwork.org`` because its query
    path went hard-unresponsive in May 2026 — every SQL request, even
    ``SELECT 1``, was timing out at 30 s. The static files are GMN's
    documented public data export and have stayed reliable.

    Output shape is unchanged so the rest of the dashboard / poller is
    not impacted. Never raises — failures surface as an ``error`` field
    on the payload, identical to the old behaviour.
    """
    return gmn_data.events_for_date(date_str)


def _gmn_fetch_multistation_LEGACY_DATASETTE(date_str: str) -> dict[str, Any]:
    """Original Datasette-based fetch — kept (renamed) for the day the
    upstream SQL endpoint becomes useful again. NOT wired into the poller
    today; ``_gmn_fetch_multistation`` above is the live path."""
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return {
            "date": date_str, "total_upstream": 0, "events": [],
            "has_ro_count": 0, "de_only_count": 0,
            "last_fetched_utc": fetched_at,
            "error": f"invalid date {date_str!r}; expected YYYY-MM-DD",
        }

    # Trajectory IDs are stamped with the YYYYMMDD begin date as their
    # prefix — exploit that to filter by date AND station prefix in a
    # single small query against participating_station. Bypasses both the
    # 3-s JOIN time budget and the 1000-row default page cap on meteor.
    yyyymmdd = day.strftime("%Y%m%d")

    # Make sure the shower lookup table is loaded (one-shot per process).
    _gmn_load_shower_cache()

    # Q1: trajectory IDs witnessed by at least one RO* station OR one of
    # OUR specific DE codes on this date. The frontend plots the DE-only
    # ones as a muted secondary overlay, RO drives the headline counter.
    codes = sorted(gmn_data.our_cam_codes())
    if not codes:
        # No stations configured yet — nothing is "ours", so return an empty
        # (but well-formed) payload rather than issuing an invalid IN () query.
        return {
            "date": date_str, "total_upstream": 0, "events": [],
            "has_ro_count": 0, "de_only_count": 0,
            "last_fetched_utc": fetched_at,
        }
    codes_in = ",".join(f"'{c}'" for c in codes)
    id_sql = (
        "SELECT DISTINCT meteor_unique_trajectory_identifier AS id "
        "FROM participating_station "
        f"WHERE station_code IN ({codes_in}) "
        f"AND meteor_unique_trajectory_identifier LIKE '{yyyymmdd}%' "
        "ORDER BY meteor_unique_trajectory_identifier"
    )
    try:
        id_rows = _gmn_sql(id_sql)
    except (requests.RequestException, ValueError) as exc:
        return {
            "date": date_str, "total_upstream": 0, "events": [],
            "has_ro_count": 0, "de_only_count": 0,
            "last_fetched_utc": fetched_at,
            "error": f"upstream id-list fetch failed: {exc}",
        }
    ids = [str(r["id"]) for r in id_rows if r.get("id")]
    if not ids:
        return {
            "date": date_str, "total_upstream": 0, "events": [],
            "has_ro_count": 0, "de_only_count": 0,
            "last_fetched_utc": fetched_at,
        }

    # Q2: meteor details for those IDs (batched IN clause).
    meteors: list[dict[str, Any]] = []
    for start in range(0, len(ids), _GMN_BATCH):
        chunk = ids[start:start + _GMN_BATCH]
        sql = (
            "SELECT unique_trajectory_identifier AS id, beginning_utc_time AS time, "
            "latbeg_n_deg AS lat_begin, lonbeg_e_deg AS lon_begin, "
            "latend_n_deg AS lat_end, lonend_e_deg AS lon_end, "
            "htbeg_km AS altitude_begin, htend_km AS altitude_end, "
            "peak_absmag AS peak_mag, vgeo_km_s AS velocity, "
            "shower_iau_no AS shower_iau_no "
            f"FROM meteor WHERE unique_trajectory_identifier IN ({_gmn_sql_quote_ids(chunk)})"
        )
        try:
            meteors.extend(_gmn_sql(sql))
        except (requests.RequestException, ValueError) as exc:
            return {
                "date": date_str, "total_upstream": len(ids), "events": [],
                "has_ro_count": 0, "de_only_count": 0,
                "last_fetched_utc": fetched_at,
                "error": f"upstream meteor fetch failed: {exc}",
            }

    # Q3: full witness list per meteor (same ID batches).
    stations_by_id: dict[str, list[str]] = {}
    for start in range(0, len(ids), _GMN_BATCH):
        chunk = ids[start:start + _GMN_BATCH]
        sql = (
            "SELECT meteor_unique_trajectory_identifier AS id, station_code "
            "FROM participating_station "
            f"WHERE meteor_unique_trajectory_identifier IN ({_gmn_sql_quote_ids(chunk)})"
        )
        try:
            rows = _gmn_sql(sql)
        except (requests.RequestException, ValueError) as exc:
            return {
                "date": date_str, "total_upstream": len(ids), "events": [],
                "has_ro_count": 0, "de_only_count": 0,
                "last_fetched_utc": fetched_at,
                "error": f"upstream station fetch failed: {exc}",
            }
        for r in rows:
            mid = str(r.get("id") or "")
            code = (r.get("station_code") or "").strip()
            if mid and code:
                stations_by_id.setdefault(mid, []).append(code)

    # Q3: shower lookup already cached. Build the slim event objects.
    events: list[dict[str, Any]] = []
    with _gmn_shower_cache_lock:
        showers = dict(_gmn_shower_cache)
    for m in meteors:
        mid = str(m.get("id") or "")
        st_list = stations_by_id.get(mid, [])
        if len(st_list) < 2:
            continue  # single-station rows aren't of interest
        # Tag via the config-derived code sets (gmn_data.tag_witness):
        # has_ro = a primary (non-highlight) station saw it; de_only = only
        # highlight stations did.
        upper = {s.upper() for s in st_list}
        _is_ours, has_ro, de_only = gmn_data.tag_witness(upper)
        events.append({
            "id":             mid,
            "time":           m.get("time"),
            "lat_begin":      m.get("lat_begin"),
            "lon_begin":      m.get("lon_begin"),
            "lat_end":        m.get("lat_end"),
            "lon_end":        m.get("lon_end"),
            "altitude_begin": m.get("altitude_begin"),
            "altitude_end":   m.get("altitude_end"),
            "peak_mag":       m.get("peak_mag"),
            "shower":         showers.get(int(m["shower_iau_no"])) if m.get("shower_iau_no") not in (None, -1) else None,
            "velocity":       m.get("velocity"),
            "stations":       st_list,
            "has_ro":         has_ro,
            "de_only":        de_only,
        })

    has_ro_count  = sum(1 for e in events if e["has_ro"])
    de_only_count = sum(1 for e in events if e["de_only"])
    return {
        "date":             date_str,
        "total_upstream":   len(meteors),
        "events":           events,
        "has_ro_count":     has_ro_count,
        "de_only_count":    de_only_count,
        "last_fetched_utc": fetched_at,
    }


def _gmn_get_cached(date_str: str) -> dict[str, Any]:
    """Return the cached payload for ``date_str`` or a stub if the poller hasn't
    reached it yet. NEVER calls upstream — that's the poller's job."""
    with _gmn_cache_lock:
        cached = _gmn_cache.get(date_str)
        if cached is not None:
            return cached
    stub: dict[str, Any] = {
        "date":             date_str,
        "total_upstream":   0,
        "events":           [],
        "has_ro_count":     0,
        "de_only_count":    0,
        "last_fetched_utc": None,
    }
    if not _gmn_initial_pass_done.is_set():
        stub["error"] = "GMN cache warming up — try again in a minute."
    return stub


def _gmn_monthly_count(now_utc: datetime | None = None) -> dict[str, Any]:
    """Sum has_ro_count across cached entries that fall in the current UTC
    calendar month. Cheap read off the in-memory cache; safe to call per
    request."""
    now = now_utc or datetime.now(timezone.utc)
    month_prefix = now.strftime("%Y-%m-")
    with _gmn_cache_lock:
        items = list(_gmn_cache.items())
    total = 0
    for date_str, payload in items:
        if date_str.startswith(month_prefix):
            total += int(payload.get("has_ro_count") or 0)
    return {"month": now.strftime("%Y-%m"), "ro_orbit_count": total}


def _gmn_poller_loop(stop_event: threading.Event) -> None:
    """Background thread: warm the cache for the trailing 35 days on startup,
    then refresh the trailing 5 days every _GMN_POLL_INTERVAL_S. Older
    trajectories are immutable once GMN's nightly batch lands, so they don't
    need to be re-fetched."""
    try:
        # Initial backfill — covers the current calendar month + buffer so the
        # monthly counter is meaningful as soon as the first pass finishes.
        # Serial backfill — _gmn_sql throttles to 1 q/s. Concurrent workers
        # tripped the upstream rate limit and stalled every subsequent request
        # to the full timeout for tens of minutes. Each date is 3 queries so
        # a 36-day backfill is ~3 min; users see partial results in the cache
        # progressively as it fills.
        today = datetime.now(timezone.utc).date()
        backfill_dates = [
            (today - timedelta(days=d)).isoformat()
            for d in range(_GMN_BACKFILL_WINDOW_DAYS + 1)
        ]
        logger.info("GMN poller: backfilling %d dates (serial, %.1fs throttle)",
                    len(backfill_dates), _GMN_THROTTLE_S)
        retry_dates: list[str] = []
        for d in backfill_dates:
            if stop_event.is_set():
                return
            # Skip dates whose cached entry is marked pinned (mock events injected
            # via the debug endpoint should survive the next poll cycle).
            with _gmn_cache_lock:
                cur = _gmn_cache.get(d)
                if cur and cur.get("pinned"):
                    continue
            try:
                payload = _gmn_fetch_multistation(d)
            except Exception as exc:
                logger.warning("GMN backfill %s raised: %s", d, exc)
                continue
            with _gmn_cache_lock:
                # Last-second pinned check in case an inject happened during the fetch
                cur = _gmn_cache.get(d)
                if cur and cur.get("pinned"):
                    continue
                _gmn_cache[d] = payload
            if payload.get("error"):
                retry_dates.append(d)

        # One-pass retry for transient upstream failures.
        if retry_dates:
            logger.info("GMN poller: retrying %d errored dates", len(retry_dates))
            for d in retry_dates:
                if stop_event.is_set():
                    return
                with _gmn_cache_lock:
                    cur = _gmn_cache.get(d)
                    if cur and cur.get("pinned"):
                        continue
                try:
                    payload = _gmn_fetch_multistation(d)
                except Exception:
                    continue
                with _gmn_cache_lock:
                    cur = _gmn_cache.get(d)
                    if cur and cur.get("pinned"):
                        continue
                    _gmn_cache[d] = payload

        logger.info("GMN poller: initial backfill done")

        # Steady state — refresh only the trailing window. Older entries persist
        # forever (or until process restart).
        while not stop_event.is_set():
            if stop_event.wait(_GMN_POLL_INTERVAL_S):
                return
            today = datetime.now(timezone.utc).date()
            recent = [
                (today - timedelta(days=d)).isoformat()
                for d in range(_GMN_RECENT_WINDOW_DAYS + 1)
            ]
            logger.info("GMN poller: refreshing %d recent dates", len(recent))
            for d in recent:
                if stop_event.is_set():
                    return
                try:
                    payload = _gmn_fetch_multistation(d)
                except Exception:
                    logger.exception("GMN poller: error fetching %s", d)
                    continue
                with _gmn_cache_lock:
                    cur = _gmn_cache.get(d)
                    if cur and cur.get("pinned"):
                        continue
                    _gmn_cache[d] = payload
    except Exception:
        logger.exception("GMN poller: unhandled error — thread exiting")
    finally:
        # Always unblock callers waiting on the initial pass, even if the
        # backfill raised before reaching steady state. Without this, any
        # code that blocks on _gmn_initial_pass_done.wait() would hang forever.
        _gmn_initial_pass_done.set()


def _start_gmn_poller() -> threading.Event:
    """Spawn the background poller as a daemon thread. Returns a stop event
    the caller can set to halt the loop (mostly useful for tests / ad-hoc
    shutdown)."""
    stop = threading.Event()
    t = threading.Thread(
        target=_gmn_poller_loop, args=(stop,),
        name="gmn-poller", daemon=True,
    )
    t.start()
    return stop


def _gmn_sanity_check() -> None:
    """Stand-alone sanity check: hit the GMN multi-station fetcher for last
    night (today UTC minus 1 day) and print upstream/filtered counts plus the
    first 2 RO-involved events.

    Invoke with ``python rovimen_dashboard.py --test-gmn`` — does NOT start
    the Flask app. Useful for verifying the upstream contract from a dev box
    or from the deploy host before the overview overlay frontend lands.
    """
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    last_night = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    print(f"GMN multi-station sanity check for {last_night} (UTC)")
    payload = _gmn_fetch_multistation(last_night)
    if "error" in payload:
        print(f"  upstream error: {payload['error']}")
    else:
        print("  upstream returned data: yes")
    print(f"  total upstream events:  {payload['total_upstream']}")
    print(f"  multi-station events:   {len(payload['events'])}")
    print(f"  with RO witness:        {payload['has_ro_count']}")
    print(f"  DE-only events:         {payload['de_only_count']}")
    print("  first 2 events:")
    print(json.dumps(payload["events"][:2], indent=2, default=str))
    sys.exit(0 if "error" not in payload else 1)
