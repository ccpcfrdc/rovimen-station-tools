"""Social media report generation — admin-only."""

from __future__ import annotations

import io
import logging
import os
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, session

from auth import require_auth
from cache_store import ARCHIVE_PATH
from rms_parse import parse_ftpdetectinfo, parse_radiants_txt
import detection_db
import gmn_data

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"compilations", "skyfit", ".thumb_cache", ".thumb_cache_dev"}

# In-memory cache: (start_iso, end_iso, cams) → (computed_at_monotonic, metrics_dict)
_weekly_cache: dict[tuple, tuple[float, dict]] = {}
_CACHE_TTL_PAST = 86400.0   # 24 h for completed past weeks
_CACHE_TTL_CURRENT = 3600.0  # 1 h if the week includes today

_MONTHS_RO = [
    "ian", "feb", "mar", "apr", "mai", "iun",
    "iul", "aug", "sep", "oct", "nov", "dec",
]


def _require_admin() -> None:
    if not session.get("user") or session.get("role") != "admin":
        abort(403)


def _fmt_date_range(start: date, end: date) -> str:
    if start.month == end.month and start.year == end.year:
        return f"{start.day}–{end.day} {_MONTHS_RO[end.month - 1]} {end.year}"
    if start.year == end.year:
        return f"{start.day} {_MONTHS_RO[start.month - 1]} – {end.day} {_MONTHS_RO[end.month - 1]} {end.year}"
    return (f"{start.day} {_MONTHS_RO[start.month - 1]} {start.year} – "
            f"{end.day} {_MONTHS_RO[end.month - 1]} {end.year}")


def _fmt_time(iso_str: str | None) -> str:
    if not iso_str:
        return "–"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%H:%M") + " UTC"
    except Exception:
        return iso_str


def _shower_ro(code: str | None) -> str:
    if not code or code.upper() in ("SPO", "SPORADIC", "..."):
        return "Sporadic"
    return code


def _time_from_ff(ff_file: str) -> str | None:
    """Extract ISO timestamp from an FF filename."""
    m = re.match(r"FF_\w+_(\d{8})_(\d{6})", ff_file)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _stack_path(cam: str, date_ymd: str, time_iso: str | None) -> tuple[Path, str] | None:
    """Find the closest stack file to a detection time.

    Stack filenames use the FF-file timestamp (not the radiants.txt time) and
    may also be time-range stacks ({cam}_{YMD}_{HH}_{HH}_stack.webp), so we
    glob the directory and pick the file whose encoded start-time is within
    30 s of the detection.  The date in the filename may differ from date_ymd
    when UTC midnight falls inside the capture night.
    """
    if not time_iso:
        return None
    try:
        dt_target = datetime.fromisoformat(time_iso.replace("Z", ""))
        target_s = dt_target.hour * 3600 + dt_target.minute * 60 + dt_target.second
    except Exception:
        return None

    # {cam}_{YYYYMMDD}_{HHMMSS}[_{HHMMSS}]_stack.webp
    pat = re.compile(rf"^{re.escape(cam)}_\d{{8}}_(\d{{6}})(?:_\d{{6}})?_stack\.webp$")
    best: tuple[Path, str, int] | None = None
    for subdir in ("meteors", "stacks"):
        d = ARCHIVE_PATH / cam / date_ymd / subdir
        try:
            for f in d.glob("*_stack.webp"):
                m = pat.match(f.name)
                if not m:
                    continue
                t = m.group(1)
                file_s = int(t[:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6])
                diff = abs(file_s - target_s)
                diff = min(diff, 86400 - diff)   # handle midnight wrap
                if diff > 30:
                    continue
                if best is None or diff < best[2]:
                    best = (f, subdir, diff)
        except OSError:
            continue
    return (best[0], best[1]) if best else None


def _cam_dirs() -> list[Path]:
    """List camera dirs in ARCHIVE_PATH once, using os.scandir for efficiency."""
    try:
        with os.scandir(ARCHIVE_PATH) as it:
            return sorted(
                Path(e.path) for e in it
                if e.is_dir(follow_symlinks=False)
                and e.name not in _SKIP_DIRS
                and not e.name.startswith(".")
            )
    except OSError:
        return []


def _aggregate_week_from_sshfs(dates: list[date]) -> tuple[dict[str, int], dict | None, dict | None]:
    """Fallback: parse radiants/FTPdetectinfo over SSHFS. Returns (day_counts, brightest, longest)."""
    cam_dirs = _cam_dirs()
    day_counts: dict[str, int] = {d.strftime("%Y%m%d"): 0 for d in dates}
    brightest: dict | None = None
    longest: dict | None = None
    for cam_dir in cam_dirs:
        cam = cam_dir.name
        for d in dates:
            date_ymd = d.strftime("%Y%m%d")
            rms_dir = cam_dir / date_ymd / "rms"
            try:
                if not rms_dir.is_dir():
                    continue
            except OSError:
                continue
            try:
                for f in sorted(rms_dir.glob("*_radiants.txt")):
                    for rec in parse_radiants_txt(f):
                        day_counts[date_ymd] += 1
                        mag = rec.get("mag_apparent") if rec.get("mag_apparent") is not None else rec.get("mag_absolute")
                        if mag is not None and (brightest is None or mag < brightest["mag"]):
                            brightest = {"mag": mag, "shower": rec.get("shower"),
                                         "time_iso": rec.get("time_utc"), "cam": cam,
                                         "date_ymd": date_ymd, "station_count": None}
                for f in sorted(rms_dir.glob("FTPdetectinfo*.txt")):
                    for rec in parse_ftpdetectinfo(f):
                        dur = rec.get("duration_s")
                        if dur is not None and (longest is None or dur > longest["duration_s"]):
                            longest = {"duration_s": dur, "shower": None,
                                       "time_iso": _time_from_ff(rec.get("ff_file", "")),
                                       "cam": cam, "date_ymd": date_ymd}
            except OSError as exc:
                logger.debug("archive scan error %s/%s: %s", cam, date_ymd, exc)
    return day_counts, brightest, longest


def _aggregate_week(dates: list[date], our_cams: frozenset[str]) -> dict:
    """Aggregate detection stats for the given date list. Returns raw metrics dict.

    Queries detections.db when available; falls back to SSHFS file parsing.
    """
    cache_key = (dates[0].isoformat(), dates[-1].isoformat(), tuple(sorted(our_cams)))
    now = time.monotonic()
    today = date.today()
    ttl = _CACHE_TTL_CURRENT if dates[-1] >= today else _CACHE_TTL_PAST
    cached = _weekly_cache.get(cache_key)
    if cached and (now - cached[0]) < ttl:
        return cached[1]

    total_detections = 0
    multistation = 0
    total_orbits = 0
    clear_nights = 0
    brightest: dict | None = None
    longest: dict | None = None
    month_orbit_cache: dict[tuple[int, int], dict[str, int]] = {}
    day_counts: dict[str, int] = {d.strftime("%Y%m%d"): 0 for d in dates}

    date_strs = [d.strftime("%Y%m%d") for d in dates]

    # ── Detection aggregation: DB first, SSHFS fallback ───────────────
    # Only use the index when it covers the whole requested range, so a
    # partially-populated DB falls back to SSHFS instead of under-reporting.
    if detection_db.covers_dates(date_strs):
        try:
            # No cam filter: match the SSHFS fallback, which scans every camera
            # dir in the archive (incl. decommissioned cams' history). Keeping
            # both paths on the same camera set means toggling DB<->SSHFS never
            # shifts the totals.
            stats = detection_db.aggregate_stats(date_strs)
            day_counts = {**day_counts, **stats["per_day"]}
            br = stats.get("brightest")
            if br:
                mag = br.get("mag_apparent") if br.get("mag_apparent") is not None else br.get("mag_absolute")
                brightest = {"mag": mag, "shower": br.get("shower"),
                             "time_iso": br.get("time_utc"), "cam": br.get("cam"),
                             "date_ymd": br.get("date"), "station_count": None}
            lo = stats.get("longest")
            if lo:
                longest = {"duration_s": lo.get("duration_s"), "shower": lo.get("shower"),
                           "time_iso": lo.get("time_utc"), "cam": lo.get("cam"),
                           "date_ymd": lo.get("date")}
        except Exception as exc:
            logger.warning("detection_db query failed, falling back to SSHFS: %s", exc)
            day_counts, brightest, longest = _aggregate_week_from_sshfs(dates)
    else:
        day_counts, brightest, longest = _aggregate_week_from_sshfs(dates)

    total_detections = sum(day_counts.values())
    clear_nights = sum(1 for c in day_counts.values() if c > 0)

    # ── GMN data ──────────────────────────────────────────────────────
    for d in dates:
        date_str = d.strftime("%Y-%m-%d")
        date_ymd = d.strftime("%Y%m%d")
        try:
            payload = gmn_data.events_for_date(date_str)
            for ev in payload.get("events") or []:
                stations = ev.get("stations") or []
                if len(stations) >= 2 and any(s in our_cams for s in stations):
                    multistation += 1
                    if brightest and brightest.get("time_iso"):
                        ev_time = ev.get("time", "")
                        try:
                            ev_dt = datetime.fromisoformat(ev_time.replace("Z", ""))
                            br_dt = datetime.fromisoformat(brightest["time_iso"].replace("Z", ""))
                            if abs((ev_dt - br_dt).total_seconds()) <= 3:
                                brightest["station_count"] = len(stations)
                        except Exception:
                            pass
        except Exception:
            logger.debug("gmn events failed for %s", date_str)

        try:
            key = (d.year, d.month)
            if key not in month_orbit_cache:
                month_orbit_cache[key] = gmn_data._daily_counts_for_month(d.year, d.month)
            total_orbits += month_orbit_cache[key].get(date_ymd, 0)
        except Exception:
            pass

    result = {
        "total_detections": total_detections,
        "multistation": multistation,
        "clear_nights": clear_nights,
        "total_nights": len(dates),
        "total_orbits": total_orbits,
        "brightest": brightest,
        "longest": longest,
    }
    _weekly_cache[cache_key] = (now, result)
    return result


def _build_text(start: date, end: date, metrics: dict,
                active_stations: int, active_cameras: int) -> str:
    br = metrics.get("brightest")
    lo = metrics.get("longest")
    lines: list[str] = [
        "ROVIMEN | Raport săptămânal",
        _fmt_date_range(start, end),
        "",
        "DETECȚII",
        "",
        f"{metrics['total_detections']} meteori înregistrați",
        f"{metrics['multistation']} evenimente multi-stație confirmate",
        f"{metrics['clear_nights']} nopți cu cer senin din {metrics['total_nights']}",
        "",
        "HIGHLIGHT",
    ]

    if br:
        mag_str = f"{br['mag']:+.1f}"
        sc = br.get("station_count")
        witness = f" • Văzut din {sc} stații" if sc else ""
        lines += [
            "",
            "Cel mai luminos",
            f"Magnitudine {mag_str} • {_shower_ro(br.get('shower'))}",
            f"{_fmt_time(br.get('time_iso'))}{witness}",
        ]
    else:
        lines += ["", "Cel mai luminos", "–"]

    if lo:
        dur_str = f"{lo['duration_s']:.1f}s"
        lines += [
            "",
            "Cel mai lung",
            f"{dur_str} • {_shower_ro(lo.get('shower'))}",
            f"{_fmt_time(lo.get('time_iso'))} • {lo.get('cam', '–')}",
        ]
    else:
        lines += ["", "Cel mai lung", "–"]

    lines += [
        "",
        "ORBITE CALCULATE",
        "",
        f"{metrics['total_orbits']} orbite calculate de serverele GMN",
        "",
        "REȚEAUA",
        "",
        f"{active_stations} stații active • {active_cameras} camere",
        "",
        "dashboard.rovimen.org",
    ]
    return "\n".join(lines)


def register_social_routes(app: Flask, config) -> None:

    def _active_counts() -> tuple[int, int]:
        stations = sum(
            1 for st in config.stations.values()
            if getattr(st, "status", "active") == "active"
        )
        cameras = sum(
            len(st.cameras) for st in config.stations.values()
            if getattr(st, "status", "active") == "active"
        )
        return stations, cameras

    def _our_cams() -> frozenset[str]:
        return frozenset(
            cam.code
            for st in config.stations.values()
            for cam in st.cameras
        )

    def _default_week() -> tuple[date, date]:
        today = date.today()
        last_mon = today - timedelta(days=today.weekday() + 7)
        return last_mon, last_mon + timedelta(days=6)

    def _parse_range() -> tuple[date, date]:
        default_start, default_end = _default_week()
        try:
            start = date.fromisoformat(request.args.get("start", default_start.isoformat()))
            end = date.fromisoformat(request.args.get("end", default_end.isoformat()))
        except ValueError:
            abort(400)
        if (end - start).days > 13:
            abort(400)
        return start, end

    # ── Pages ─────────────────────────────────────────────────────────────

    @app.route("/social")
    @require_auth
    def social_page():
        _require_admin()
        default_start, default_end = _default_week()
        return render_template(
            "social.html",
            default_start=default_start.isoformat(),
            default_end=default_end.isoformat(),
        )

    # ── Data endpoint ──────────────────────────────────────────────────────

    @app.route("/api/social/weekly")
    @require_auth
    def social_weekly():
        _require_admin()
        start, end = _parse_range()
        dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        metrics = _aggregate_week(dates, _our_cams())
        active_stations, active_cameras = _active_counts()
        text = _build_text(start, end, metrics, active_stations, active_cameras)

        # Find stack URL for brightest detection
        stack_url = None
        br = metrics.get("brightest")
        if br and br.get("time_iso") and br.get("cam") and br.get("date_ymd"):
            result = _stack_path(br["cam"], br["date_ymd"], br["time_iso"])
            if result:
                _, subdir = result
                cam, ymd = br["cam"], br["date_ymd"]
                dt = datetime.fromisoformat(br["time_iso"])
                stem = f"{cam}_{dt.strftime('%Y%m%d_%H%M%S')}"
                stack_url = f"/api/archive/{cam}/{ymd}/{subdir}/{stem}_stack.webp"

        return jsonify({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "text": text,
            "metrics": {
                "total_detections": metrics["total_detections"],
                "multistation": metrics["multistation"],
                "clear_nights": metrics["clear_nights"],
                "total_nights": len(dates),
                "total_orbits": metrics["total_orbits"],
                "active_stations": active_stations,
                "active_cameras": active_cameras,
            },
            "stack_url": stack_url,
        })

    # ── ZIP download ───────────────────────────────────────────────────────

    @app.route("/api/social/weekly/zip")
    @require_auth
    def social_weekly_zip():
        _require_admin()
        start, end = _parse_range()
        dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        metrics = _aggregate_week(dates, _our_cams())
        active_stations, active_cameras = _active_counts()
        text = _build_text(start, end, metrics, active_stations, active_cameras)

        buf = io.BytesIO()
        slug = f"{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"rovimen_raport_{slug}.txt", text.encode("utf-8"))

            br = metrics.get("brightest")
            if br and br.get("time_iso") and br.get("cam") and br.get("date_ymd"):
                result = _stack_path(br["cam"], br["date_ymd"], br["time_iso"])
                if result:
                    stack_p, _ = result
                    try:
                        zf.write(stack_p, f"highlight_{slug}.webp")
                    except OSError as e:
                        logger.warning("could not read stack for zip: %s", e)

        buf.seek(0)
        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"rovimen_raport_{slug}.zip",
        )
