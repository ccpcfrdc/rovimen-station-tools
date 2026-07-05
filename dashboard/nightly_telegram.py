#!/usr/bin/env python3
"""nightly_telegram.py -- VPS-side nightly meteor summary -> Telegram.

Replaces the laptop SSH-fan-out bot (``nightly_summary/run_summary.py``).
Instead of SSH-ing to every station over Tailscale (which breaks the moment the
tailnet ACL, an IP, or a jump host changes -- the exact failure that took the
"Meteori Romania" channel down), this reads the fleet-wide detection index
(:mod:`detection_db`) and the storage-box archive (:data:`cache_store.ARCHIVE_PATH`)
that the dashboard already maintains, and posts a per-night summary for the
Romania (``RO*``) cameras to the Telegram channel.

Because it only touches local files on the VPS, it has no station connectivity
dependency at all. It runs once per night from ``rovimen-nightly-summary.timer``
and is idempotent per night via a sent-marker file.

Media policy: per-camera whole-night meteor STACK image (radiants plot dropped),
plus the single brightest clip of the night (best-effort). Every media step is
guarded so a missing/broken file never blocks the text summary.

Secrets and paths come from the environment (same ``/etc/rovimen-dashboard.env``
the dashboard and index-poller already use):

    ROVIMEN_TELEGRAM_BOT_TOKEN     Telegram bot token (required to post)
    ROVIMEN_TELEGRAM_RO_CHAT_ID    "Meteori Romania" channel chat id (required)
    ROVIMEN_TELEGRAM_CHAT_ID       optional operator echo chat id
    ROVIMEN_OPENAI_API_KEY         optional; without it the prose blurb is skipped
    ROVIMEN_OPENAI_MODEL           default "gpt-4o-mini"
    ROVIMEN_CONFIG                 dashboard_config.yaml path (falls back to
                                   ROVIMEN_DASHBOARD_CONFIG then the sibling file)
    ROVIMEN_NIGHTLY_STALE_HOURS    station-freshness window, default 30
    ROVIMEN_NIGHTLY_MAX_STACKS     cap on stack images posted, default 15
    ROVIMEN_NIGHTLY_SEND_CLIP      "1"/"0" post the brightest clip, default "1"
    ROVIMEN_NIGHTLY_STATE_DIR      sent-marker dir, default <detections.db dir>/nightly_telegram_state

Usage:
    python nightly_telegram.py [YYYYMMDD] [--dry-run] [--force] [--no-clip]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

import cache_store
import detection_db

logger = logging.getLogger("nightly_telegram")

# A meteor brighter (more negative) than this is a "fireball".
FIREBALL_THRESHOLD = 0.0

TELEGRAM_API = "https://api.telegram.org"
OPENAI_API = "https://api.openai.com/v1/chat/completions"


# ---------------------------------------------------------------------------
# Environment / configuration
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _config_path() -> Path:
    for var in ("ROVIMEN_CONFIG", "ROVIMEN_DASHBOARD_CONFIG"):
        val = _env(var)
        if val:
            return Path(val)
    return Path(__file__).resolve().parent / "dashboard_config.yaml"


def _state_dir() -> Path:
    val = _env("ROVIMEN_NIGHTLY_STATE_DIR")
    if val:
        return Path(val)
    # Next to the detection DB (i.e. /opt/rovimen), which is writable by the
    # service and honours ROVIMEN_DETECTIONS_DB rather than hardcoding a path.
    return detection_db.DB_PATH.parent / "nightly_telegram_state"


def default_night_id() -> str:
    """Evening date (YYYYMMDD) of the most recently completed observing night.

    The timer fires in the morning (UTC). Before ~noon UTC the night that just
    ended started "yesterday"; after noon it started "today". This matches the
    ``date`` the detection index stamps on a session (derived from the archive
    ``<cam>/<YYYYMMDD>/`` night dir, i.e. the evening date).
    """
    now = datetime.now(timezone.utc)
    if now.hour < 12:
        return (now - timedelta(days=1)).strftime("%Y%m%d")
    return now.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# Station registry
# ---------------------------------------------------------------------------

def load_ro_stations(config_path: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Return (station -> [RO cam codes], cam code -> location label).

    Includes RO cameras from every ``active`` station in the registry. The
    registry is the single source of truth: a decommissioned camera removed
    from ``dashboard_config.yaml`` simply stops appearing here.
    """
    config = cache_store.load_config(config_path)
    station_cams: dict[str, list[str]] = {}
    cam_location: dict[str, str] = {}
    for host_key, st in config.stations.items():
        if getattr(st, "status", "active") != "active":
            continue
        ro = [c.code for c in st.cameras if c.code.upper().startswith("RO")]
        if not ro:
            continue
        station_cams[host_key] = ro
        location = (st.location_name or st.label or host_key)
        for code in ro:
            cam_location[code] = location
    return station_cams, cam_location


# ---------------------------------------------------------------------------
# Detection data
# ---------------------------------------------------------------------------

def _mag(row: dict[str, Any]) -> float | None:
    m = row.get("mag_apparent")
    if m is None:
        m = row.get("mag_absolute")
    return m


def collect_night(night_id: str, ro_cams: set[str]) -> dict[str, Any]:
    """Aggregate the night's detections for the RO camera set from the index."""
    rows = detection_db.query_detections([night_id], cam_filter=ro_cams)

    per_cam: dict[str, dict[str, Any]] = {}
    brightest: dict[str, Any] | None = None
    for r in rows:
        cam = r["cam"]
        slot = per_cam.setdefault(cam, {"count": 0, "fireballs": 0, "brightest": None})
        slot["count"] += 1
        m = _mag(r)
        if m is not None:
            if m < FIREBALL_THRESHOLD:
                slot["fireballs"] += 1
            if slot["brightest"] is None or m < slot["brightest"]:
                slot["brightest"] = m
            if brightest is None or m < brightest["mag"]:
                brightest = {
                    "mag": m, "cam": cam, "ff_file": r.get("ff_file"),
                    "date": r.get("date"), "shower": r.get("shower"),
                    "time_utc": r.get("time_utc"),
                }

    return {
        "night_id": night_id,
        "total": len(rows),
        "per_cam": per_cam,
        "brightest": brightest,
    }


def station_last_activity(station_cams: dict[str, list[str]]) -> dict[str, float | None]:
    """Most-recent activity timestamp per station (unix seconds), or None.

    Combines ``poll_state.last_polled`` (updated whenever the VPS polls a
    pull-mode station) with ``MAX(ingested_files.ingested_at)`` for the station's
    cameras (covers push-mode ingest). Read-only; never blocks the poller.
    """
    result: dict[str, float | None] = {hk: None for hk in station_cams}
    db = detection_db.DB_PATH
    if not db.exists():
        return result
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        logger.warning("station_last_activity: cannot open DB: %s", exc)
        return result
    try:
        for hk, cams in station_cams.items():
            ts: float | None = None
            row = con.execute(
                "SELECT last_polled, error_count FROM poll_state WHERE host_key=?",
                (hk,),
            ).fetchone()
            if row and row[0] and (row[1] or 0) == 0:
                ts = row[0]
            if cams:
                ph = ",".join("?" * len(cams))
                r2 = con.execute(
                    f"SELECT MAX(ingested_at) FROM ingested_files WHERE cam IN ({ph})",
                    cams,
                ).fetchone()
                if r2 and r2[0] and (ts is None or r2[0] > ts):
                    ts = r2[0]
            result[hk] = ts
    finally:
        con.close()
    return result


def classify(
    night: dict[str, Any],
    station_cams: dict[str, list[str]],
    stale_hours: float,
) -> dict[str, Any]:
    """Split RO cameras into reported / quiet / offline.

    reported: has detections this night.
    quiet:    0 detections but the station is fresh (up, just no meteors -- clouds).
    offline:  0 detections and the station has not reported recently.

    The online/offline call is best-effort from index freshness; it cannot be
    wrong for the whole fleet at once the way the old single-host SSH probe was.
    """
    per_cam = night["per_cam"]
    last = station_last_activity(station_cams)
    now = time.time()
    stale_s = stale_hours * 3600.0

    reported: list[dict[str, Any]] = []
    quiet: list[str] = []
    offline: list[str] = []

    for hk, cams in station_cams.items():
        has_det = any(per_cam.get(c, {}).get("count", 0) > 0 for c in cams)
        ts = last.get(hk)
        fresh = ts is not None and (now - ts) <= stale_s
        online = has_det or fresh
        for cam in cams:
            slot = per_cam.get(cam)
            if slot and slot["count"] > 0:
                reported.append({"cam": cam, **slot})
            elif online:
                quiet.append(cam)
            else:
                offline.append(cam)

    reported.sort(key=lambda d: d["count"], reverse=True)
    return {"reported": reported, "quiet": sorted(quiet), "offline": sorted(offline)}


# ---------------------------------------------------------------------------
# Summary text
# ---------------------------------------------------------------------------

def llm_prose(night: dict[str, Any], groups: dict[str, Any],
              cam_location: dict[str, str]) -> str | None:
    api_key = _env("ROVIMEN_OPENAI_API_KEY")
    if not api_key:
        return None
    model = _env("ROVIMEN_OPENAI_MODEL") or "gpt-4o-mini"
    night_fmt = datetime.strptime(night["night_id"], "%Y%m%d").strftime("%B %d, %Y")

    lines: list[str] = []
    for item in groups["reported"]:
        cam = item["cam"]
        loc = cam_location.get(cam, "")
        line = f"- {cam} ({loc}): {item['count']} meteors"
        if item["fireballs"]:
            line += f", {item['fireballs']} fireballs"
        if item["brightest"] is not None:
            line += f", brightest mag {item['brightest']:+.1f}"
        lines.append(line)
    for cam in groups["quiet"]:
        lines.append(f"- {cam} ({cam_location.get(cam, '')}): 0 meteors (clear/no data)")
    for cam in groups["offline"]:
        lines.append(f"- {cam} ({cam_location.get(cam, '')}): offline (not reporting)")

    active_stations = len({
        cam_location.get(i["cam"]) for i in groups["reported"]
    })
    user_prompt = (
        f"Night of {night_fmt}. Total: {night['total']} meteors across "
        f"{active_stations} reporting Romanian stations.\n" + "\n".join(lines)
    )
    try:
        resp = requests.post(
            OPENAI_API,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": (
                        "You are a concise meteor observer assistant for the "
                        "ROVIMEN Romanian camera network. Summarize the night's "
                        "meteor observation results in 2-4 sentences. Mention "
                        "notable activity, fireballs, and any stations that were "
                        "offline. Be factual and direct. Do not use emojis."
                    )},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 220,
                "temperature": 0.7,
            },
            timeout=45,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        logger.warning("OpenAI error %s: %s", resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        logger.warning("OpenAI request failed: %s", exc)
    return None


def build_message(night: dict[str, Any], groups: dict[str, Any],
                  cam_location: dict[str, str], prose: str | None) -> str:
    night_fmt = datetime.strptime(night["night_id"], "%Y%m%d").strftime("%b %d, %Y")
    total_fireballs = sum(i["fireballs"] for i in groups["reported"])

    lines = [f"<b>Nightly Summary: {night_fmt}</b>", ""]
    for item in groups["reported"]:
        cam = item["cam"]
        line = f"  {cam} ({cam_location.get(cam, '')}): <b>{item['count']}</b> meteors"
        if item["brightest"] is not None:
            line += f" (brightest {item['brightest']:+.1f})"
        lines.append(line)
    lines.append("")
    fb = f" | Fireballs: {total_fireballs}" if total_fireballs else ""
    lines.append(f"<b>Total: {night['total']} meteors</b>{fb}")
    if groups["quiet"]:
        lines.append(f"<i>Clear/no meteors: {', '.join(groups['quiet'])}</i>")
    if groups["offline"]:
        lines.append(f"<i>Offline: {', '.join(groups['offline'])}</i>")
    if prose:
        lines += ["", prose]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Media resolution (archive on the storage box)
# ---------------------------------------------------------------------------

def find_night_stack(cam: str, night_id: str) -> Path | None:
    """The per-camera whole-night meteor stack: <cam>/<date>/timelapse/*_night_stack.webp."""
    d = cache_store.ARCHIVE_PATH / cam / night_id / "timelapse"
    exact = d / f"{cam}_{night_id}_night_stack.webp"
    if exact.exists():
        return exact
    try:
        matches = sorted(d.glob(f"{cam}_*_night_stack.webp"))
    except OSError:
        return None
    return matches[0] if matches else None


def find_clip(cam: str, night_id: str, ff_file: str | None) -> Path | None:
    """Best-effort match of a detection's color clip in <cam>/<date>/meteors/."""
    if not ff_file:
        return None
    d = cache_store.ARCHIVE_PATH / cam / night_id / "meteors"
    m = re.search(r"_(\d{8})_(\d{6})", ff_file)
    stamp = f"{m.group(1)}_{m.group(2)}" if m else None
    try:
        clips = sorted(d.glob("*.mkv")) + sorted(d.glob("*.mp4"))
    except OSError:
        return None
    if not clips:
        return None
    if stamp:
        for c in clips:
            if stamp in c.name:
                return c
    return None


def webp_to_jpg(src: Path, dst: Path) -> bool:
    """Telegram sendPhoto is unreliable with WebP; convert to JPEG first."""
    try:
        from PIL import Image
        with Image.open(src) as im:
            im.convert("RGB").save(dst, "JPEG", quality=90)
        return True
    except Exception as exc:
        logger.warning("webp->jpg failed for %s: %s", src, exc)
        return False


def mkv_to_mp4(src: Path, dst: Path) -> bool:
    """Remux to MP4 (H.264) so Telegram plays it inline. Copy streams if possible."""
    if not shutil.which("ffmpeg"):
        return False
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-c", "copy", "-movflags",
             "+faststart", str(dst)],
            capture_output=True, timeout=180,
        )
        if r.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            return True
        # Fall back to a re-encode if stream copy produced nothing usable.
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-c:v", "libx264", "-preset",
             "veryfast", "-movflags", "+faststart", "-an", str(dst)],
            capture_output=True, timeout=300,
        )
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("mkv->mp4 failed for %s: %s", src, exc)
        return False


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class Telegram:
    def __init__(self, token: str, chat_ids: list[str]):
        self.token = token
        self.chat_ids = chat_ids

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API}/bot{self.token}/{method}"

    def send_message(self, text: str) -> None:
        for chat_id in self.chat_ids:
            time.sleep(1.5)
            try:
                requests.post(self._url("sendMessage"), data={
                    "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }, timeout=30)
            except requests.RequestException as exc:
                logger.warning("sendMessage to %s failed: %s", chat_id, exc)

    def send_photo(self, path: Path, caption: str) -> None:
        for chat_id in self.chat_ids:
            time.sleep(1.5)
            try:
                with open(path, "rb") as f:
                    requests.post(self._url("sendPhoto"),
                                  data={"chat_id": chat_id, "caption": caption},
                                  files={"photo": f}, timeout=90)
            except (requests.RequestException, OSError) as exc:
                logger.warning("sendPhoto to %s failed: %s", chat_id, exc)

    def send_video(self, path: Path, caption: str) -> None:
        for chat_id in self.chat_ids:
            time.sleep(1.5)
            try:
                with open(path, "rb") as f:
                    requests.post(self._url("sendVideo"),
                                  data={"chat_id": chat_id, "caption": caption},
                                  files={"video": f}, timeout=180)
            except (requests.RequestException, OSError) as exc:
                logger.warning("sendVideo to %s failed: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def post_media(tg: Telegram, night: dict[str, Any], groups: dict[str, Any],
               send_clip: bool, max_stacks: int, workdir: Path) -> None:
    # Per-camera whole-night meteor stacks (radiants plot dropped).
    for item in groups["reported"][:max_stacks]:
        cam = item["cam"]
        webp = find_night_stack(cam, night["night_id"])
        if not webp:
            continue
        jpg = workdir / f"{cam}_{night['night_id']}_stack.jpg"
        if webp_to_jpg(webp, jpg):
            tg.send_photo(jpg, f"{cam} | Meteor stack")

    # The single brightest clip of the night (best-effort).
    if send_clip and night["brightest"]:
        br = night["brightest"]
        clip = find_clip(br["cam"], night["night_id"], br.get("ff_file"))
        if clip:
            mp4 = clip if clip.suffix == ".mp4" else workdir / f"brightest_{clip.stem}.mp4"
            ok = clip.suffix == ".mp4" or mkv_to_mp4(clip, mp4)
            if ok:
                shower = br.get("shower") or "Sporadic"
                tg.send_video(mp4, f"{br['cam']} | Brightest {br['mag']:+.1f} | {shower}")


def run(night_id: str, *, dry_run: bool, force: bool, send_clip: bool) -> int:
    config_path = _config_path()
    if not config_path.exists():
        logger.error("config not found: %s", config_path)
        return 2

    station_cams, cam_location = load_ro_stations(config_path)
    ro_cams = {c for cams in station_cams.values() for c in cams}
    if not ro_cams:
        logger.error("no RO cameras in registry %s", config_path)
        return 2

    if not detection_db.is_ready():
        logger.error("detection index not ready at %s -- nothing to post",
                     detection_db.DB_PATH)
        return 2

    night = collect_night(night_id, ro_cams)
    stale_hours = float(_env("ROVIMEN_NIGHTLY_STALE_HOURS") or "30")
    groups = classify(night, station_cams, stale_hours)
    prose = llm_prose(night, groups, cam_location)
    message = build_message(night, groups, cam_location, prose)

    logger.info("night %s: %d meteors, %d reported cams, %d quiet, %d offline",
                night_id, night["total"], len(groups["reported"]),
                len(groups["quiet"]), len(groups["offline"]))

    if dry_run:
        print("=== nightly_telegram DRY RUN ===")
        print(message)
        print("--- media ---")
        for item in groups["reported"]:
            s = find_night_stack(item["cam"], night_id)
            print(f"stack {item['cam']}: {s or 'MISSING'}")
        if night["brightest"]:
            br = night["brightest"]
            print(f"brightest clip {br['cam']}: "
                  f"{find_clip(br['cam'], night_id, br.get('ff_file')) or 'MISSING'}")
        return 0

    state_dir = _state_dir()
    marker = state_dir / f"{night_id}.sent"
    if marker.exists() and not force:
        logger.info("already posted for %s (%s); use --force to repost",
                    night_id, marker)
        return 0

    token = _env("ROVIMEN_TELEGRAM_BOT_TOKEN")
    ro_chat = _env("ROVIMEN_TELEGRAM_RO_CHAT_ID")
    if not token or not ro_chat:
        logger.error("ROVIMEN_TELEGRAM_BOT_TOKEN and ROVIMEN_TELEGRAM_RO_CHAT_ID required")
        return 2
    chat_ids = [ro_chat]
    echo = _env("ROVIMEN_TELEGRAM_CHAT_ID")
    if echo and echo != ro_chat:
        chat_ids.append(echo)

    tg = Telegram(token, chat_ids)
    tg.send_message(message)

    max_stacks = int(_env("ROVIMEN_NIGHTLY_MAX_STACKS") or "15")
    with tempfile.TemporaryDirectory(prefix="nightly_tg_") as tmp:
        post_media(tg, night, groups, send_clip, max_stacks, Path(tmp))

    state_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(timezone.utc).isoformat())
    logger.info("posted nightly summary for %s to %d chat(s)", night_id, len(chat_ids))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post the nightly RO meteor summary to Telegram.")
    parser.add_argument("night", nargs="?", help="night id YYYYMMDD (default: last night)")
    parser.add_argument("--dry-run", action="store_true", help="compute and print, do not post")
    parser.add_argument("--force", action="store_true", help="repost even if already sent")
    parser.add_argument("--no-clip", action="store_true", help="skip the brightest-clip video")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    night_id = args.night or default_night_id()
    if not (len(night_id) == 8 and night_id.isdigit()):
        parser.error(f"invalid night id {night_id!r} (expected YYYYMMDD)")

    send_clip = not args.no_clip and _env("ROVIMEN_NIGHTLY_SEND_CLIP", "1") != "0"
    return run(night_id, dry_run=args.dry_run, force=args.force, send_clip=send_clip)


if __name__ == "__main__":
    sys.exit(main())
