#!/usr/bin/env python3
"""index_poller.py — VPS-side persistent detection index poller.

Reads dashboard_config.yaml to discover stations, polls each station's
/api/detections-index endpoint every POLL_INTERVAL seconds, and merges
results into /opt/rovimen/detections.db.

Each established station is polled with a rolling overlap window
(?since=today-REPOLL_DAYS) so late-arriving or reprocessed nights inside the
station's hot window propagate. On first contact (or while a station's index
is not yet ready) it asks for the last BOOTSTRAP_DAYS days. Upserts are
idempotent (keyed on cam/date/ff_file/meteor_no), so re-pulling the overlap
window every cycle never double-counts.

Run as: rovimen-index-poller.service (systemd on VPS)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

import detection_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get(
    "ROVIMEN_DASHBOARD_CONFIG",
    Path(__file__).parent / "dashboard_config.yaml",
))
DB_PATH = detection_db.DB_PATH
POLL_INTERVAL = int(os.environ.get("ROVIMEN_INDEX_POLL_INTERVAL", "300"))   # seconds
BOOTSTRAP_DAYS = int(os.environ.get("ROVIMEN_INDEX_BOOTSTRAP_DAYS", "30"))
# Rolling re-poll window for established stations. Must be >= the station
# indexer's HOT_DAYS (14) so every night the station may still rewrite is
# re-fetched; otherwise reprocessing corrections never reach the VPS.
REPOLL_DAYS = int(os.environ.get("ROVIMEN_INDEX_REPOLL_DAYS", "14"))
REQUEST_TIMEOUT = int(os.environ.get("ROVIMEN_INDEX_REQUEST_TIMEOUT", "60"))
MAX_ERRORS_BEFORE_BACKOFF = 3
BACKOFF_FACTOR = 4   # skip this many cycles after repeated errors

_stop = False


# ── Config ────────────────────────────────────────────────────────────────────

def _load_stations() -> dict[str, dict]:
    """Return {host_key: {ip, port, cameras: [codes]}} from dashboard_config.yaml."""
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
    except Exception as exc:
        logger.error("failed to load config: %s", exc)
        return {}

    port = cfg.get("station_api_port", 7779)
    stations: dict[str, dict] = {}
    for host_key, info in cfg.get("stations", {}).items():
        ip = info.get("ip")
        if not ip:
            continue
        # Skip stations migrated to the reversed-HTTP push path: they push
        # their detection index via the ingest API instead of being polled.
        # Fail-safe — default False, so an absent flag keeps polling as today.
        if info.get("push_enabled", False):
            continue
        cam_codes = [c["code"] for c in info.get("cameras", []) if c.get("code")]
        stations[host_key] = {
            "ip": ip,
            "port": port,
            "cameras": cam_codes,
            "label": info.get("label", host_key),
        }
    return stations


def _utc_today() -> datetime:
    return datetime.now(timezone.utc)


# ── Poll one station ──────────────────────────────────────────────────────────

def _poll_station(host_key: str, info: dict) -> tuple[int, bool]:
    """Poll one station and upsert results into detections.db.

    Returns (rows_upserted, ready). ``ready`` is False when the station's index
    isn't built yet, so the caller keeps using the wider first-contact window.
    Raises on network/parse error so the caller can track error_count.
    """
    state = detection_db.get_poll_state(host_key, path=DB_PATH)
    today = _utc_today()
    if state["since_date"]:
        # Established station: rolling overlap window.
        since_date = (today - timedelta(days=REPOLL_DAYS)).strftime("%Y%m%d")
    else:
        # First contact (or never-ready): wider bootstrap window.
        since_date = (today - timedelta(days=BOOTSTRAP_DAYS)).strftime("%Y%m%d")

    url = f"http://{info['ip']}:{info['port']}/api/detections-index"
    resp = requests.get(url, params={"since": since_date}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    payload = resp.json()
    if not payload.get("ready"):
        logger.debug("%s: index not ready yet", host_key)
        return 0, False

    rows: list[dict] = payload.get("detections") or []
    if not rows:
        return 0, True

    upserted = detection_db.upsert_detections(rows, source="station", path=DB_PATH)
    return upserted, True


# ── Poll cycle ────────────────────────────────────────────────────────────────

_error_counts: dict[str, int] = {}
_skip_cycles: dict[str, int] = {}


def _run_cycle(stations: dict[str, dict]) -> None:
    def _poll_one(host_key: str, info: dict) -> tuple[str, int, bool, Exception | None]:
        if _skip_cycles.get(host_key, 0) > 0:
            _skip_cycles[host_key] -= 1
            return host_key, 0, False, None
        try:
            rows, ready = _poll_station(host_key, info)
            return host_key, rows, ready, None
        except Exception as exc:
            return host_key, 0, False, exc

    today_str = _utc_today().strftime("%Y%m%d")
    with ThreadPoolExecutor(max_workers=min(8, len(stations))) as pool:
        futures = {pool.submit(_poll_one, k, v): k for k, v in stations.items()}
        for future in as_completed(futures):
            host_key, rows, ready, exc = future.result()
            if exc is not None:
                _error_counts[host_key] = _error_counts.get(host_key, 0) + 1
                err_count = _error_counts[host_key]
                if err_count >= MAX_ERRORS_BEFORE_BACKOFF:
                    _skip_cycles[host_key] = BACKOFF_FACTOR
                    logger.warning(
                        "%s: %d consecutive errors, backing off %d cycles — %s",
                        host_key, err_count, BACKOFF_FACTOR, exc,
                    )
                else:
                    logger.debug("%s: poll error: %s", host_key, exc)
                state = detection_db.get_poll_state(host_key, path=DB_PATH)
                detection_db.set_poll_state(
                    host_key,
                    since_date=state.get("since_date") or "",
                    error_count=err_count,
                    path=DB_PATH,
                )
            elif ready:
                # Mark contacted (switches to the rolling window) only once the
                # station's index exists. since_date stores the last successful
                # sync date — used as a "have we synced" flag + for monitoring.
                _error_counts[host_key] = 0
                detection_db.set_poll_state(
                    host_key, since_date=today_str, error_count=0, path=DB_PATH,
                )
                if rows:
                    logger.info("%s: %d rows upserted (window since %dd)", host_key, rows, REPOLL_DAYS)
            else:
                # Not ready yet — leave first-contact state untouched so we keep
                # using the wider bootstrap window next cycle.
                _error_counts[host_key] = 0


# ── Signal handling ───────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _stop
    logger.info("received signal %s, stopping", sig)
    _stop = True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("index_poller starting, DB: %s, interval: %ss", DB_PATH, POLL_INTERVAL)

    # Ensure schema exists
    detection_db.open_db(DB_PATH).close()

    cycle = 0
    while not _stop:
        cycle += 1
        stations = _load_stations()
        if not stations:
            logger.warning("no stations found in config, sleeping")
        else:
            logger.debug("poll cycle %d: %d stations", cycle, len(stations))
            try:
                _run_cycle(stations)
            except Exception as exc:
                logger.error("cycle error: %s", exc)

        # Sleep in 1s increments for prompt SIGTERM response
        for _ in range(POLL_INTERVAL):
            if _stop:
                break
            time.sleep(1)

    logger.info("index_poller stopped after %d cycles", cycle)


if __name__ == "__main__":
    main()
