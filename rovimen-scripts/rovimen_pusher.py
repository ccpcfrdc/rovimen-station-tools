#!/usr/bin/env python3
"""rovimen_pusher.py — station-side push agent (reversed-HTTP data plane).

Runs on each ROVIMEN station and POSTs the station's own state to the VPS ingest
API (``/api/ingest/v1/<host_key>/*``). This is the station-side publisher half of
the design in ``docs/reversed_http_push_design.md`` (§2 ingest, §2.3 idempotency
envelope, §2.4 payload schemas).

It also runs the **command worker** (§3): an outbound long-poll of
``GET /api/fleet/<host_key>/commands``. Each returned command is ed25519-**verified**
against the server public key baked into the deploy bundle (``command_verify.py``);
a command that does not verify, whose ``not_after`` has passed, or whose ``type`` is
not in the strict allowlist is refused and never dispatched. Verified commands are
dispatched to the **same local station_api actions** the VPS used to call over
:7779 (restart a service, reboot, patch settings, lock a clip, trigger an upload)
— never eval/exec of an arbitrary payload — and the result is POSTed to the ack
endpoint. The worker is **fail-closed**: with no public key configured, or no
station key, it does not run.

Design properties honoured:

  * **Purely additive.** Reads the station's *existing* local data sources — the
    same ones the VPS poller reads today — by calling ``station_api.py`` on
    ``127.0.0.1:7779`` (``/api/status``, ``/api/vitals``, ``/api/timelapses``) and
    the local detection index (``/api/detections-index``). It does not touch RMS,
    capture, upload, or ``station_api.py``; it only reads. If the pusher dies the
    station is unaffected.
  * **Outbound-only.** Pure outbound HTTPS POSTs to the VPS edge. No inbound port.
  * **Opt-in per station.** Starts only if the ingest key file exists (see
    ``ingest.key`` under ``~/.config/rovimen/``). Absent key -> clean exit 0, so
    the systemd unit can be enabled fleet-wide while staying dormant until a
    station is enrolled in the pilot.
  * **Idempotent envelope.** Every body carries a monotonic per-station ``seq``
    (persisted across restarts) and ``sent_at`` (§2.3). Detections also dedup on
    their natural PK server-side.
  * **Robust.** Retries with backoff+jitter, honours ``Retry-After`` on 429,
    buffers heartbeat/vitals/media on a bounded on-disk spool when ingest is
    down, and never raises out of the main loop.

Config (mirrors how other station scripts read config, no hardcoded paths):

  * ``config.json`` (``~/rovimen_scripts/config.json``) — read for ``host_key``
    and, as a fallback, ``vps_host``. ``host_key`` may also come from the
    ``ROVIMEN_HOST_KEY`` env var or the key filename.
  * Ingest base URL: ``ROVIMEN_INGEST_BASE_URL`` env var, else config
    ``push.ingest_base_url``, else derived from ``vps_host``.
  * Ingest key: ``ROVIMEN_INGEST_KEY`` env var, else the first line of
    ``~/.config/rovimen/ingest.key`` (mode 0600). If neither is present the agent
    exits 0 (opt-in gate).

Run: ``python rovimen_pusher.py`` (long-running daemon, systemd ``Restart=always``).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("rovimen_pusher")

AGENT_VERSION = "1.0.0"

BASE = Path.home()
SCRIPTS_DIR = BASE / "rovimen_scripts"
CONFIG_PATH = SCRIPTS_DIR / "config.json"

# Where the local station_api serves the same data the VPS poller reads.
STATION_API_BASE = "http://127.0.0.1:7779"

# Opt-in gate + durable state live under the operator's XDG config dir.
CONFIG_DIR = BASE / ".config" / "rovimen"
KEY_PATH = CONFIG_DIR / "ingest.key"
STATE_PATH = CONFIG_DIR / "pusher_state.json"
SPOOL_PATH = CONFIG_DIR / "pusher_spool.db"

# Cadences (seconds) — §2.1.
HEARTBEAT_INTERVAL = 45          # 30-60 s window
VITALS_INTERVAL = 30
DETECTIONS_INTERVAL = 300        # matches index_poller 5-min cadence
MEDIA_INTERVAL = 900             # dawn-process pointers change slowly
LIVE_THUMB_INTERVAL = 45         # §9 — newest FF maxpixel, 30-60 s window

# Detection hot window — matches index_poller's 14-day rolling overlap (§2.4.3).
DETECTION_WINDOW_DAYS = 14

# Locked-clip pointer window — how many recent night dirs per camera to scan for
# locked chunks. Locked clips are sparse (a handful per night) and only the
# recent ones matter to the live-feed / footage views, so a short window keeps
# the per-cycle station_api load bounded regardless of archive depth.
CLIP_WINDOW_NIGHTS = 3

# Networking.
HTTP_TIMEOUT = 20
MAX_BACKOFF = 300.0
BASE_BACKOFF = 2.0

# Spool bounds (§7 — bounded local buffer).
SPOOL_MAX_ROWS = 5000


# ──────────────────────────────────────────────────────────────────────────
# Config / identity
# ──────────────────────────────────────────────────────────────────────────


def _load_config() -> dict:
    """Read config.json if present; tolerate a missing/broken file."""
    try:
        return json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not parse %s: %s", CONFIG_PATH, exc)
        return {}


def resolve_host_key(cfg: dict, key_path: Path = KEY_PATH) -> str | None:
    """Determine this station's host_key.

    Priority: ROVIMEN_HOST_KEY env -> config['host_key'] -> config['push']['host_key']
    -> the ``<host_key>.`` prefix of the ingest key filename if named that way.
    Returns None if it cannot be determined.
    """
    env = os.environ.get("ROVIMEN_HOST_KEY")
    if env:
        return env.strip()
    hk = cfg.get("host_key")
    if isinstance(hk, str) and hk.strip():
        return hk.strip()
    push = cfg.get("push")
    if isinstance(push, dict):
        hk = push.get("host_key")
        if isinstance(hk, str) and hk.strip():
            return hk.strip()
    return None


def resolve_ingest_key(key_path: Path = KEY_PATH) -> str | None:
    """Read the per-station ingest key (opt-in gate).

    ROVIMEN_INGEST_KEY env wins (useful for tests / containers); otherwise the
    first non-empty line of the key file. Returns None if neither is present.
    """
    env = os.environ.get("ROVIMEN_INGEST_KEY")
    if env and env.strip():
        return env.strip()
    try:
        for line in key_path.read_text().splitlines():
            line = line.strip()
            if line:
                return line
    except FileNotFoundError:
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not read ingest key %s: %s", key_path, exc)
        return None
    return None


def resolve_live_thumb_interval(cfg: dict) -> int:
    """Live-thumbnail push cadence in seconds (§9, configurable, default 45).

    Priority: ROVIMEN_LIVE_THUMB_INTERVAL env -> config['push']['live_thumb_interval']
    -> LIVE_THUMB_INTERVAL default. Clamped to a sane 10-300 s floor/ceiling."""
    raw: object = None
    env = os.environ.get("ROVIMEN_LIVE_THUMB_INTERVAL")
    if env and env.strip():
        raw = env.strip()
    else:
        push = cfg.get("push")
        if isinstance(push, dict):
            raw = push.get("live_thumb_interval")
    if raw is None:
        return LIVE_THUMB_INTERVAL
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning("invalid live_thumb_interval %r — using default %d",
                       raw, LIVE_THUMB_INTERVAL)
        return LIVE_THUMB_INTERVAL
    return max(10, min(val, 300))


def resolve_base_url(cfg: dict) -> str | None:
    """Determine the ingest base URL.

    Priority: ROVIMEN_INGEST_BASE_URL env -> config['push']['ingest_base_url']
    -> derived https://<vps_host> from config. Returns None if undeterminable.
    """
    env = os.environ.get("ROVIMEN_INGEST_BASE_URL")
    if env and env.strip():
        return env.strip().rstrip("/")
    push = cfg.get("push")
    if isinstance(push, dict):
        url = push.get("ingest_base_url")
        if isinstance(url, str) and url.strip():
            return url.strip().rstrip("/")
    vps = cfg.get("vps_host")
    if isinstance(vps, str) and vps.strip():
        return f"https://{vps.strip()}".rstrip("/")
    return None


# ──────────────────────────────────────────────────────────────────────────
# seq persistence (§2.3)
# ──────────────────────────────────────────────────────────────────────────


class SeqCounter:
    """Monotonic per-station sequence number persisted to a small state file.

    ``next()`` returns a strictly increasing int and durably records it so seq
    keeps climbing across restarts. A corrupt/absent state file starts at 0.
    """

    def __init__(self, state_path: Path = STATE_PATH) -> None:
        self._state_path = state_path
        self._lock = threading.Lock()
        self._seq = self._read()

    def _read(self) -> int:
        try:
            data = json.loads(self._state_path.read_text())
            val = int(data.get("last_seq", 0))
            return max(val, 0)
        except Exception:
            return 0

    @property
    def current(self) -> int:
        return self._seq

    def next(self) -> int:
        with self._lock:
            self._seq += 1
            self._persist(self._seq)
            return self._seq

    def _persist(self, seq: int) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_seq": seq}))
            tmp.replace(self._state_path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not persist seq state: %s", exc)


# ──────────────────────────────────────────────────────────────────────────
# Envelope + payload construction (§2.4)
# ──────────────────────────────────────────────────────────────────────────


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_envelope(seq: int, payload: dict, sent_at: str | None = None) -> dict:
    """Wrap a payload in the common ingest envelope (§2.4)."""
    return {
        "seq": seq,
        "sent_at": sent_at or _now_rfc3339(),
        "agent_version": AGENT_VERSION,
        "payload": payload,
    }


def build_heartbeat_payload(status: dict) -> dict:
    """Project /api/status into the heartbeat payload (§2.4.1).

    Only the fields the SSE diff layer keys on are forwarded, so the pushed body
    matches what the poller feeds ``StationCache.set_status`` today.
    """
    return {
        "online": bool(status.get("online", True)),
        "services": status.get("services", {}),
        "cameras": status.get("cameras", []),
        "disk": status.get("disk", {}),
        "storage": status.get("storage", {}),
        "extra_disks": status.get("extra_disks", []),
        "rms_running": status.get("rms_running"),
        "color_capture_running": status.get("color_capture_running"),
        "network_name": status.get("network_name"),
    }


def build_vitals_payload(vitals: dict) -> dict:
    """Project /api/vitals into the vitals payload (§2.4.2)."""
    return {
        "cpu_pct": vitals.get("cpu_pct"),
        "ram_pct": vitals.get("ram_pct"),
        "ram_used_mb": vitals.get("ram_used_mb"),
        "ram_total_mb": vitals.get("ram_total_mb"),
        "temp_c": vitals.get("temp_c"),
        "total_cores": vitals.get("total_cores"),
        "cores": vitals.get("cores", []),
        "disk_write_mbps": vitals.get("disk_write_mbps"),
        "top_procs": vitals.get("top_procs", []),
    }


def build_detections_payload(window_since: str, detections: list[dict]) -> dict:
    """Wrap detection-index rows into the detections payload (§2.4.3).

    ``detections`` come straight from the local index (columns already match
    ``detection_db.DETECTION_COLS`` minus the VPS-only ``source``), so ingest is a
    pass-through to ``upsert_detections``.
    """
    return {"window_since": window_since, "detections": detections}


def build_clip_pointers(chunks_by_cam_date: dict[tuple[str, str], list[dict]]) -> list[dict]:
    """Flatten locked chunks into clip pointers (§2.4.5).

    ``chunks_by_cam_date`` maps ``(cam, date)`` to the ``/api/chunks`` chunk
    dicts (already filtered to locked chunks). Each chunk carries ``filename``,
    ``locked``, ``lock_type`` and ``meteor_time``; we emit only the fields the
    ingest ``ClipPointer`` schema keys on plus ``lock_type`` (preserved verbatim
    in the pointer's ``extra`` blob server-side). Idempotent by construction: the
    pointers dedup on ``(cam, date, kind, filename)`` in ``media_pointers``, so
    re-scanning the same night is a no-op on identity.
    """
    clips: list[dict] = []
    for (cam, date), chunks in chunks_by_cam_date.items():
        for c in chunks or []:
            filename = c.get("filename")
            if not filename or not c.get("locked"):
                continue
            clips.append({
                "cam": cam,
                "date": date,
                "filename": filename,
                "locked": True,
                "meteor_time": c.get("meteor_time"),
                "lock_type": c.get("lock_type"),
            })
    return clips


def build_media_payload(
    timelapses_raw: dict,
    chunks_by_cam_date: dict[tuple[str, str], list[dict]] | None = None,
) -> dict:
    """Project /api/timelapses (+ locked chunks) into the media payload (§2.4.5).

    ``/api/timelapses`` returns ``{cam: [{date, filename, night_stack}, ...]}``.
    Flatten into the ``timelapses`` / ``nightstacks`` pointer lists. Locked-clip
    pointers come from ``chunks_by_cam_date`` (recent locked chunks per camera,
    fetched from ``/api/chunks/<cam>/<date>?locked_only=1``). The media *bytes*
    still reach the VPS via the independent SFTP path (§2.4.5) — these are only
    pointers telling the dashboard which locked clips exist, replacing the
    request-time :7779 fan-out.
    """
    timelapses: list[dict] = []
    nightstacks: list[dict] = []
    for cam, entries in (timelapses_raw or {}).items():
        for e in entries or []:
            date = e.get("date")
            filename = e.get("filename")
            night_stack = e.get("night_stack")
            if filename:
                timelapses.append({
                    "cam": cam,
                    "date": date,
                    "filename": filename,
                    "night_stack": night_stack,
                })
            if night_stack:
                nightstacks.append({
                    "cam": cam,
                    "date": date,
                    "filename": night_stack,
                })
    clips = build_clip_pointers(chunks_by_cam_date or {})
    return {"timelapses": timelapses, "nightstacks": nightstacks, "clips": clips}


def build_live_thumb_payload(webp: bytes, ff_timestamp: str | None) -> dict:
    """Wrap a WebP maxpixel into the live-thumbnail payload (§9).

    The image is tiny (few KB, newest FF only, no archival value) so the bytes
    ride the JSON envelope base64-encoded rather than taking the bytes→box /
    pointer→VPS split used for archival media (§9). ``content_type`` and
    ``ff_timestamp`` let the VPS serve it back verbatim to the existing consumer.
    """
    return {
        "content_type": "image/webp",
        "ff_timestamp": ff_timestamp,
        "data_b64": base64.b64encode(webp).decode("ascii"),
    }


def _window_since(days: int = DETECTION_WINDOW_DAYS) -> str:
    from datetime import timedelta
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.strftime("%Y%m%d")


# ──────────────────────────────────────────────────────────────────────────
# Local data sources (read-only; reuse station_api :7779)
# ──────────────────────────────────────────────────────────────────────────


def _get_json(url: str, timeout: int = HTTP_TIMEOUT) -> dict | None:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost)
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("local GET %s failed: %s", url, exc)
        return None


def fetch_status() -> dict | None:
    return _get_json(f"{STATION_API_BASE}/api/status")


def fetch_vitals() -> dict | None:
    return _get_json(f"{STATION_API_BASE}/api/vitals")


def fetch_timelapses() -> dict | None:
    return _get_json(f"{STATION_API_BASE}/api/timelapses")


def fetch_detections(since: str) -> list[dict]:
    data = _get_json(f"{STATION_API_BASE}/api/detections-index?since={since}")
    if not data or not data.get("ready"):
        return []
    return data.get("detections", [])


def fetch_camera_codes() -> list[str]:
    """Return the station's camera codes via /api/rms_cameras (reliable, config-
    derived), used to drive per-cam clip and live-thumbnail pushes."""
    data = _get_json(f"{STATION_API_BASE}/api/rms_cameras")
    if not isinstance(data, list):
        return []
    codes: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            code = entry.get("code")
            if isinstance(code, str) and code.strip():
                codes.append(code.strip())
    return codes


def fetch_nights(cam: str) -> list[str]:
    """Return night-date dirs (YYYYMMDD, newest first) that hold clips for cam."""
    data = _get_json(f"{STATION_API_BASE}/api/nights/{cam}")
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, str)]


def fetch_locked_chunks(cam: str, date: str) -> list[dict]:
    """Return locked chunk dicts for one cam/night via /api/chunks?locked_only=1.

    /api/chunks returns ``{"morning_done": bool, "chunks": [...]}`` (or a bare
    list on very old stations); normalise both. Only locked chunks are requested,
    but we defensively re-filter on ``locked``."""
    data = _get_json(f"{STATION_API_BASE}/api/chunks/{cam}/{date}?locked_only=1")
    if isinstance(data, dict):
        chunks = data.get("chunks", [])
    elif isinstance(data, list):
        chunks = data
    else:
        return []
    return [c for c in chunks if isinstance(c, dict) and c.get("locked")]


def fetch_locked_clips(nights: int = CLIP_WINDOW_NIGHTS) -> dict[tuple[str, str], list[dict]]:
    """Enumerate recent locked clips across all cameras.

    For each camera code, scan its most recent ``nights`` night dirs for locked
    chunks. Returns ``{(cam, date): [chunk, ...]}`` suitable for
    :func:`build_clip_pointers`."""
    out: dict[tuple[str, str], list[dict]] = {}
    for cam in fetch_camera_codes():
        for date in fetch_nights(cam)[:nights]:
            locked = fetch_locked_chunks(cam, date)
            if locked:
                out[(cam, date)] = locked
    return out


def fetch_live_thumb(cam: str) -> tuple[bytes, str | None] | None:
    """Fetch the newest FF maxpixel WebP for ``cam`` from the local station_api.

    Returns ``(webp_bytes, ff_timestamp)`` or None if unavailable (404/503/error).
    ``ff_timestamp`` comes from the ``X-FF-Timestamp`` response header so the
    dashboard can show a freshness indicator without decoding the image.
    """
    url = f"{STATION_API_BASE}/api/latest_ff_maxpixel/{cam}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (localhost)
            if not (200 <= resp.status < 300):
                return None
            data = resp.read()
            ts = resp.headers.get("X-FF-Timestamp") if resp.headers else None
            return (data, ts) if data else None
    except urllib.error.HTTPError as exc:
        # 404 (no FF yet) / 503 (decode failed) are normal — camera may be idle.
        logger.debug("live_thumb GET %s -> HTTP %s", url, exc.code)
        return None
    except Exception as exc:
        logger.warning("live_thumb GET %s failed: %s", url, exc)
        return None


# ──────────────────────────────────────────────────────────────────────────
# On-disk spool for buffering when ingest is down (§7)
# ──────────────────────────────────────────────────────────────────────────


class Spool:
    """Bounded on-disk FIFO of pending POSTs, keyed by kind.

    Heartbeat/vitals are last-value: only the newest of each kind is retained
    (a backlog collapses to one POST, per §7). Media pointers likewise coalesce.
    Detection batches are re-derived from the local index on reconnect rather
    than spooled, so this spool never grows unbounded from detections.
    """

    COALESCE_KINDS = {"heartbeat", "vitals", "media"}

    def __init__(self, path: Path = SPOOL_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS spool ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, endpoint TEXT, "
            "body TEXT, created_at TEXT)"
        )
        self._con.commit()

    def add(self, kind: str, endpoint: str, body: dict) -> None:
        cur = self._con.cursor()
        if kind in self.COALESCE_KINDS:
            cur.execute("DELETE FROM spool WHERE kind=?", (kind,))
        cur.execute(
            "INSERT INTO spool (kind, endpoint, body, created_at) VALUES (?,?,?,?)",
            (kind, endpoint, json.dumps(body), _now_rfc3339()),
        )
        # Enforce bound: drop oldest beyond SPOOL_MAX_ROWS.
        cur.execute(
            "DELETE FROM spool WHERE id NOT IN "
            "(SELECT id FROM spool ORDER BY id DESC LIMIT ?)",
            (SPOOL_MAX_ROWS,),
        )
        self._con.commit()

    def pending(self) -> list[tuple[int, str, dict]]:
        rows = self._con.execute(
            "SELECT id, endpoint, body FROM spool ORDER BY id ASC"
        ).fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def remove(self, row_id: int) -> None:
        self._con.execute("DELETE FROM spool WHERE id=?", (row_id,))
        self._con.commit()

    def count(self) -> int:
        return int(self._con.execute("SELECT COUNT(*) FROM spool").fetchone()[0])

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # pragma: no cover
            pass


# ──────────────────────────────────────────────────────────────────────────
# HTTP POST with retry/backoff
# ──────────────────────────────────────────────────────────────────────────


class PostResult:
    __slots__ = ("ok", "status", "retry_after")

    def __init__(self, ok: bool, status: int | None = None,
                 retry_after: float | None = None) -> None:
        self.ok = ok
        self.status = status
        self.retry_after = retry_after


def post_json(url: str, key: str, body: dict, timeout: int = HTTP_TIMEOUT) -> PostResult:
    """Single POST attempt. Returns PostResult; never raises.

    2xx -> ok. 429 -> not ok, carries Retry-After. Other errors -> not ok.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Station-Key", key)
    req.add_header("User-Agent", f"rovimen-pusher/{AGENT_VERSION}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return PostResult(ok=200 <= resp.status < 300, status=resp.status)
    except urllib.error.HTTPError as exc:
        retry_after = None
        if exc.code == 429:
            ra = exc.headers.get("Retry-After") if exc.headers else None
            try:
                retry_after = float(ra) if ra is not None else None
            except (TypeError, ValueError):
                retry_after = None
        logger.warning("POST %s -> HTTP %s", url, exc.code)
        return PostResult(ok=False, status=exc.code, retry_after=retry_after)
    except Exception as exc:
        logger.warning("POST %s failed: %s", url, exc)
        return PostResult(ok=False, status=None)


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at MAX_BACKOFF."""
    raw = min(BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)
    return raw / 2 + random.uniform(0, raw / 2)


# ──────────────────────────────────────────────────────────────────────────
# Publisher
# ──────────────────────────────────────────────────────────────────────────


class Publisher:
    """Drives the periodic POSTs and the spool drain."""

    def __init__(self, base_url: str, host_key: str, key: str,
                 seq: SeqCounter, spool: Spool) -> None:
        self._base = base_url.rstrip("/")
        self._host_key = host_key
        self._key = key
        self._seq = seq
        self._spool = spool
        self._stop = threading.Event()

    def _url(self, endpoint: str) -> str:
        return f"{self._base}/api/ingest/v1/{self._host_key}/{endpoint}"

    def stop(self) -> None:
        self._stop.set()

    def _send(self, kind: str, endpoint: str, payload: dict) -> bool:
        """Build an enveloped body, POST it, spool it on failure. Returns success."""
        body = build_envelope(self._seq.next(), payload)
        result = post_json(self._url(endpoint), self._key, body)
        if result.ok:
            return True
        if kind != "detections":
            # Buffer everything except detections (those re-derive from the index).
            self._spool.add(kind, endpoint, body)
        if result.retry_after:
            self._stop.wait(min(result.retry_after, MAX_BACKOFF))
        return False

    def drain_spool(self) -> None:
        """Attempt to flush buffered POSTs; stop at the first failure."""
        for row_id, endpoint, body in self._spool.pending():
            if self._stop.is_set():
                return
            result = post_json(self._url(endpoint), self._key, body)
            if result.ok:
                self._spool.remove(row_id)
            else:
                if result.retry_after:
                    self._stop.wait(min(result.retry_after, MAX_BACKOFF))
                return

    def push_heartbeat(self) -> bool:
        status = fetch_status()
        if status is None:
            return False
        return self._send("heartbeat", "heartbeat", build_heartbeat_payload(status))

    def push_vitals(self) -> bool:
        vitals = fetch_vitals()
        if vitals is None:
            return False
        return self._send("vitals", "vitals", build_vitals_payload(vitals))

    def push_detections(self) -> bool:
        since = _window_since()
        detections = fetch_detections(since)
        if not detections:
            return True  # nothing to send is success
        return self._send("detections", "detections",
                          build_detections_payload(since, detections))

    def push_media(self) -> bool:
        tl = fetch_timelapses()
        if tl is None:
            return False
        # Locked-clip pointers ride the same media POST (§2.4.5). A failed clip
        # scan (station_api hiccup) still lets timelapse/nightstack pointers
        # through — clips just default to empty that cycle and refresh next time.
        try:
            clips = fetch_locked_clips()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("locked-clip scan failed: %s", exc)
            clips = {}
        return self._send("media", "media", build_media_payload(tl, clips))

    def push_live_thumbs(self) -> bool:
        """Push the newest BW maxpixel WebP for each camera (§9).

        Ephemeral: the newest FF only, no archival value, so the small WebP bytes
        go to the VPS live_thumb ingest (base64 in the envelope) rather than the
        bytes→box path used for archival media. Not spooled — a stale live thumb
        is worthless, so a failed push is simply retried on the next cycle.
        Returns True if every camera with a thumbnail pushed successfully (a
        camera with no current FF is not a failure)."""
        ok_all = True
        for cam in fetch_camera_codes():
            fetched = fetch_live_thumb(cam)
            if fetched is None:
                continue  # no FF yet / decode failed — not a push failure
            webp, ts = fetched
            body = build_envelope(self._seq.next(), build_live_thumb_payload(webp, ts))
            result = post_json(self._url(f"live_thumb/{cam}"), self._key, body)
            if not result.ok:
                ok_all = False
                if result.retry_after:
                    self._stop.wait(min(result.retry_after, MAX_BACKOFF))
        return ok_all


# ──────────────────────────────────────────────────────────────────────────
# Command worker (§3) — outbound long-poll, verify, dispatch, ack
# ──────────────────────────────────────────────────────────────────────────

# Long-poll cadence. The server holds a poll up to ~25 s; on a network error we
# back off. On return (with or without work) we immediately re-poll.
COMMAND_POLL_WAIT = 25           # ?wait= server hold, seconds
COMMAND_POLL_TIMEOUT = 40        # client read timeout > server hold
COMMAND_IDLE_SLEEP = 1.0         # brief pause between empty polls

# Local action dispatch targets on the station's own station_api (:7779). These
# are the SAME operations the VPS used to POST to over the network — the command
# worker just triggers them locally instead. NEVER an arbitrary shell string.
_ACTION_TIMEOUT = 60


def _http_get_commands(url: str, key: str, timeout: int = COMMAND_POLL_TIMEOUT) -> tuple[int | None, dict | None]:
    """GET the command queue. Returns (status, json) or (None, None) on error."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Station-Key", key)
    req.add_header("User-Agent", f"rovimen-pusher/{AGENT_VERSION}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body
    except urllib.error.HTTPError as exc:
        logger.warning("command poll %s -> HTTP %s", url, exc.code)
        return exc.code, None
    except Exception as exc:
        logger.warning("command poll %s failed: %s", url, exc)
        return None, None


def _local_post(path: str, body: dict | None = None, timeout: int = _ACTION_TIMEOUT) -> tuple[bool, str]:
    """POST to the local station_api (:7779). Returns (ok, detail)."""
    url = f"{STATION_API_BASE}{path}"
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost)
            txt = resp.read().decode("utf-8", "replace")[:500]
            return (200 <= resp.status < 300), txt
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = str(exc)
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:
        return False, f"local action failed: {exc}"


class CommandWorker:
    """Outbound long-poll worker for the server->station command channel (§3).

    Fail-closed: constructed only when both a station key and a verifiable server
    public key are present. Each command is verified (signature + not_after +
    allowlisted type) before dispatch; anything else is refused and acked as an
    error so the dashboard shows why.
    """

    # Strict allowlist mirrored from command_store.ALLOWED_TYPES. A command whose
    # type is not here is refused even if its signature is valid.
    ALLOWED_TYPES = frozenset({
        "restart_service", "reboot", "patch_settings", "lock_clip", "trigger_upload",
        "run_updater", "restart_services",
    })

    def __init__(self, base_url: str, host_key: str, key: str, pubkey_raw: bytes,
                 stop_event: threading.Event) -> None:
        self._base = base_url.rstrip("/")
        self._host_key = host_key
        self._key = key
        self._pubkey = pubkey_raw
        self._stop = stop_event
        # Short retention of executed ids so a redelivered command is acked
        # without re-executing (§7 duplicate command execution).
        self._executed: set[str] = set()

    def _poll_url(self) -> str:
        return f"{self._base}/api/fleet/{self._host_key}/commands?wait={COMMAND_POLL_WAIT}"

    def _ack_url(self, cmd_id: str) -> str:
        return f"{self._base}/api/fleet/{self._host_key}/commands/{cmd_id}/ack"

    def _verify(self, cmd: dict) -> tuple[bool, str]:
        """Verify signature, TTL, and type allowlist. Returns (ok, reason)."""
        import command_verify

        cmd_id = cmd.get("id") or ""
        ctype = cmd.get("type") or ""
        args = cmd.get("args") or {}
        issued_at = cmd.get("issued_at") or ""
        not_after = cmd.get("not_after") or ""
        sig = cmd.get("sig") or ""

        msg = command_verify.canonical_message(
            id=cmd_id, station=self._host_key, type=ctype, args=args,
            issued_at=issued_at, not_after=not_after,
        )
        if not command_verify.verify(msg, sig, self._pubkey):
            return False, "signature_invalid"
        if not command_verify.not_expired(not_after):
            return False, "expired"
        if ctype not in self.ALLOWED_TYPES:
            return False, f"disallowed_type:{ctype}"
        return True, "ok"

    def _dispatch(self, cmd: dict) -> tuple[bool, str]:
        """Map a verified, allowlisted command to a local station_api action.

        Only the allowlisted types reach here (verify() gated them). Each maps to
        an existing local operation — never eval/exec of a payload."""
        ctype = cmd["type"]
        args = cmd.get("args") or {}
        if ctype == "restart_service":
            svc = str(args.get("service", ""))
            if not svc:
                return False, "restart_service requires args.service"
            return _local_post(f"/api/restart/{svc}")
        if ctype == "reboot":
            return _local_post("/api/reboot")
        if ctype == "patch_settings":
            patch = args.get("settings") or args.get("patch") or args
            return _local_post("/api/settings", patch)  # station_api PATCHes config
        if ctype == "lock_clip":
            cam = str(args.get("cam", ""))
            date = str(args.get("date", ""))
            filename = str(args.get("filename", ""))
            if not (cam and date and filename):
                return False, "lock_clip requires args.cam/date/filename"
            locked = bool(args.get("locked", True))
            return _local_post(f"/api/lock/{cam}/{date}/{filename}", {"locked": locked})
        if ctype == "trigger_upload":
            return _local_post("/api/archive/test")
        if ctype == "run_updater":
            # A truthy args.check runs the version check (--check) instead of a
            # full pull+restart; both are loopback POSTs to the same local API.
            if args.get("check"):
                return _local_post("/api/updater/check")
            return _local_post("/api/updater/run")
        if ctype == "restart_services":
            # /api/services/restart restarts the fixed capture set AND the
            # station_api itself. Because the API self-restarts, the loopback
            # POST may never receive a response — the socket is reset/times out
            # as the server exits mid-request. That connection drop IS the
            # success signal for this command (the restart is what we asked
            # for), so a URLError here is acked ok rather than as a failure.
            # An HTTP error status still means real failure and is surfaced.
            ok, detail = _local_post("/api/services/restart")
            if not ok and not detail.startswith("HTTP "):
                return True, f"restart in progress (station_api self-restart): {detail}"
            return ok, detail
        return False, f"unhandled_type:{ctype}"

    def _ack(self, cmd_id: str, ok: bool, detail: str) -> None:
        body = {"status": "ok" if ok else "error", "detail": detail[:500]}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self._ack_url(cmd_id), data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Station-Key", self._key)
        req.add_header("User-Agent", f"rovimen-pusher/{AGENT_VERSION}")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
                _ = resp.read()
        except Exception as exc:
            logger.warning("ack %s failed: %s", cmd_id, exc)

    def handle(self, cmd: dict) -> None:
        """Verify → dispatch → ack a single command. Never raises."""
        cmd_id = cmd.get("id") or ""
        if not cmd_id:
            return
        if cmd_id in self._executed:
            # Redelivery: ack without re-executing (idempotent, §7).
            self._ack(cmd_id, True, "already_executed")
            return
        ok, reason = self._verify(cmd)
        if not ok:
            logger.warning("command %s refused: %s", cmd_id, reason)
            self._executed.add(cmd_id)
            self._ack(cmd_id, False, f"refused: {reason}")
            return
        try:
            done, detail = self._dispatch(cmd)
        except Exception as exc:  # never crash the loop
            done, detail = False, f"dispatch raised: {exc}"
        self._executed.add(cmd_id)
        logger.info("command %s (%s) -> %s", cmd_id, cmd.get("type"),
                    "ok" if done else f"error: {detail}")
        self._ack(cmd_id, done, detail)

    def run(self) -> None:
        """Long-poll loop. Honours stop_event; backs off on network error."""
        fail = 0
        while not self._stop.is_set():
            status, body = _http_get_commands(self._poll_url(), self._key)
            if status is None or status >= 500 or status in (401, 403, 404):
                # Transient or misconfig — back off. 401/403/404 usually means the
                # station isn't enrolled for commands yet; slow-poll rather than spin.
                fail += 1
                self._stop.wait(backoff_delay(min(fail, 6)))
                continue
            fail = 0
            commands = (body or {}).get("commands") or []
            for cmd in commands:
                if self._stop.is_set():
                    break
                self.handle(cmd)
            if not commands:
                self._stop.wait(COMMAND_IDLE_SLEEP)


# ──────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────


def run(publisher: Publisher, stop_event: threading.Event,
        live_thumb_interval: int = LIVE_THUMB_INTERVAL) -> None:
    """Scheduling loop. Each kind fires on its own cadence; spool drained first."""
    last = {"heartbeat": 0.0, "vitals": 0.0, "detections": 0.0,
            "media": 0.0, "live_thumb": 0.0}
    intervals = {
        "heartbeat": HEARTBEAT_INTERVAL,
        "vitals": VITALS_INTERVAL,
        "detections": DETECTIONS_INTERVAL,
        "media": MEDIA_INTERVAL,
        "live_thumb": live_thumb_interval,
    }
    handlers = {
        "heartbeat": publisher.push_heartbeat,
        "vitals": publisher.push_vitals,
        "detections": publisher.push_detections,
        "media": publisher.push_media,
        "live_thumb": publisher.push_live_thumbs,
    }
    fail_streak = 0
    while not stop_event.is_set():
        # Flush buffered POSTs before new ones so ordering roughly holds.
        publisher.drain_spool()
        now = time.monotonic()
        any_fail = False
        for kind, interval in intervals.items():
            if stop_event.is_set():
                break
            if now - last[kind] >= interval:
                try:
                    ok = handlers[kind]()
                except Exception as exc:  # never let a kind crash the loop
                    logger.exception("push %s raised: %s", kind, exc)
                    ok = False
                last[kind] = now
                any_fail = any_fail or not ok
        if any_fail:
            fail_streak += 1
            stop_event.wait(backoff_delay(min(fail_streak, 6)))
        else:
            fail_streak = 0
            stop_event.wait(1.0)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ROVIMEN_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = _load_config()
    key = resolve_ingest_key()
    if not key:
        # Opt-in gate: no key -> station not enrolled in the push pilot. Exit
        # cleanly so the systemd unit can be enabled everywhere yet stay dormant.
        logger.info("no ingest key at %s and ROVIMEN_INGEST_KEY unset — "
                    "push not enabled for this station, exiting", KEY_PATH)
        return 0

    host_key = resolve_host_key(cfg)
    if not host_key:
        logger.error("could not determine host_key (set ROVIMEN_HOST_KEY or "
                     "config.json host_key) — refusing to push")
        return 1

    base_url = resolve_base_url(cfg)
    if not base_url:
        logger.error("could not determine ingest base URL (set "
                     "ROVIMEN_INGEST_BASE_URL or config vps_host) — refusing to push")
        return 1

    logger.info("rovimen-pusher %s starting: host_key=%s base=%s",
                AGENT_VERSION, host_key, base_url)

    seq = SeqCounter()
    spool = Spool()
    publisher = Publisher(base_url, host_key, key, seq, spool)
    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("received signal %s, shutting down", signum)
        stop_event.set()
        publisher.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Command worker (§3) — starts only if a server public key is present
    # (fail-closed). With no pubkey the station never acts on any command.
    cmd_thread: threading.Thread | None = None
    try:
        import command_verify

        pubkey = command_verify.load_public_key_raw()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not load command public key: %s", exc)
        pubkey = None
    if pubkey is not None:
        worker = CommandWorker(base_url, host_key, key, pubkey, stop_event)
        cmd_thread = threading.Thread(target=worker.run, name="command-worker", daemon=True)
        cmd_thread.start()
        logger.info("command worker enabled (server pubkey loaded)")
    else:
        logger.info("command worker disabled: no server public key configured "
                    "(set ROVIMEN_COMMAND_PUBKEY or ship command_pubkey.pem) — "
                    "commands will not be executed (fail-closed)")

    try:
        run(publisher, stop_event,
            live_thumb_interval=resolve_live_thumb_interval(cfg))
    finally:
        stop_event.set()
        if cmd_thread is not None:
            cmd_thread.join(timeout=5)
        spool.close()
    logger.info("rovimen-pusher stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
