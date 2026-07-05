#!/usr/bin/env python3
"""Storage-box retention janitor (VPS-side).

The Hetzner storage box keeps every uploaded detection clip forever, with no
time-based culling -- so it fills up (it hit 99% on a 1 TB box in June 2026).
Color-video MKVs are the bulk; stacks/metadata are tiny.

This janitor enforces a *value-based* retention policy on the cold tier:

    A clip's color-video MKV is pruned once it is older than RETENTION_DAYS,
    UNLESS the clip is "worth keeping forever" by any of:
      - manually locked by an operator      (lock_type == "manual")
      - part of a multi-station event        (GMN-confirmed OR internally
                                              correlated across >=2 cameras)
      - in the top BRIGHT_PERCENTILE %        (brightest, by peak magnitude)
      - in the top LONG_PERCENTILE %          (longest, by duration)

Only the heavy ``*_color.mkv`` video is ever removed. The stack ``.webp``,
the ``rms/`` analysis, and ``state.json`` are always kept, so the visual
record and all metadata survive forever and the dashboard still shows the
detection (just without the full-resolution clip).

Brightness/duration percentile thresholds are computed across the WHOLE
archive (all ages), so "top 20% brightest" means brightest of all time, not
brightest of the pruned cohort.

SAFETY: dry-run by default. Pass ``--apply`` to actually delete. A date whose
``state.json`` can't be read is skipped entirely (never prune blind). Runs on
the VPS, which owns the read-write sshfs mount, so deletion is a plain
``os.remove`` on the mounted path.

Typical use:
    # see what would be pruned, reclaimable GB, without touching anything
    python storagebox_janitor.py --report /opt/rovimen/janitor_report.json

    # actually prune, capped at 5000 deletions per run as a throttle
    python storagebox_janitor.py --apply --max-deletes 5000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Sibling dashboard modules are imported by bare name (the service puts the
# dashboard dir on sys.path). When run as a standalone script, add our own dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gmn_data  # noqa: E402
from rms_parse import parse_ftpdetectinfo, parse_radiants_txt  # noqa: E402

logger = logging.getLogger("storagebox_janitor")

DEFAULT_ARCHIVE = os.environ.get("ROVIMEN_ARCHIVE_PATH", "/srv/rovimen/archive")
DEFAULT_RETENTION_DAYS = 180          # ~6 months
DEFAULT_BRIGHT_PCT = 20.0             # keep brightest 20%
DEFAULT_LONG_PCT = 20.0               # keep longest 20%
DEFAULT_EVENT_TOLERANCE_S = 3.0       # clip<->GMN event time match window
DEFAULT_CORRELATION_WINDOW_S = 3.0    # internal multi-station coincidence window

# Non-camera top-level dirs on the box that must never be scanned for clips.
_SKIP_DIRS = {"compilations", "skyfit", ".thumb_cache", ".thumb_cache_dev"}
_DATE_DIR_LEN = 8  # YYYYMMDD


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Clip:
    cam: str
    date: str                 # YYYYMMDD
    filename: str             # the *_color.mkv chunk name (state.json key)
    mkv_path: Path
    meteor_time: str | None
    lock_type: str | None
    mag: float | None = None          # peak magnitude (lower = brighter)
    duration_s: float | None = None
    multistation: bool = False
    size_bytes: int = 0

    @property
    def date_obj(self) -> datetime:
        return datetime.strptime(self.date, "%Y%m%d").replace(tzinfo=timezone.utc)


@dataclass
class Stats:
    scanned_clips: int = 0
    scanned_dates: int = 0
    skipped_dates: int = 0
    pruned: int = 0
    pruned_bytes: int = 0
    kept_recent: int = 0
    kept_manual: int = 0
    kept_multistation: int = 0
    kept_bright: int = 0
    kept_long: int = 0
    errors: int = 0
    keep_reasons: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _iso_date(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-ish timestamp to a naive UTC datetime (seconds precision)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace(" ", "T")).replace(tzinfo=None)
    except ValueError:
        return None


def _ff_time_utc(ff_file: str) -> str | None:
    """Extract ``YYYY-MM-DDTHH:MM:SS`` from an FF filename like
    ``FF_RO000H_20260314_023422_123_0123456.fits``."""
    parts = ff_file.split("_")
    if len(parts) < 4 or len(parts[2]) != 8 or len(parts[3]) != 6:
        return None
    d, t = parts[2], parts[3]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}"


def _nearest(target: datetime | None, table: dict[str, datetime],
             values: dict[str, dict], tol_s: float) -> dict | None:
    """Find the metadata dict whose timestamp is closest to ``target`` within
    ``tol_s`` seconds. ``table`` maps key->datetime, ``values`` maps key->dict."""
    if target is None:
        return None
    best_key, best_dt = None, None
    best = tol_s
    for key, dt in table.items():
        diff = abs((dt - target).total_seconds())
        if diff <= best:
            best, best_key, best_dt = diff, key, dt
    return values.get(best_key) if best_key is not None else None


# ---------------------------------------------------------------------------
# Percentiles (stdlib-only, no numpy)
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


# ---------------------------------------------------------------------------
# Archive scanning
# ---------------------------------------------------------------------------

def _read_state(date_dir: Path) -> dict | None:
    state_path = date_dir / "state.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())
    except (OSError, ValueError):
        return None


def _rms_metadata(date_dir: Path) -> tuple[dict[str, datetime], dict[str, dict],
                                           dict[str, datetime], dict[str, dict]]:
    """Return (mag_table, mag_values, dur_table, dur_values) keyed by a string
    key, with parallel key->datetime tables for tolerant time matching."""
    mag_table: dict[str, datetime] = {}
    mag_values: dict[str, dict] = {}
    dur_table: dict[str, datetime] = {}
    dur_values: dict[str, dict] = {}
    rms_dir = date_dir / "rms"
    if not rms_dir.is_dir():
        return mag_table, mag_values, dur_table, dur_values
    try:
        radiants = sorted(rms_dir.glob("*_radiants.txt"))
        ftps = sorted(rms_dir.glob("FTPdetectinfo*.txt"))
    except OSError:
        return mag_table, mag_values, dur_table, dur_values

    for f in radiants:
        for d in parse_radiants_txt(f):
            t = d.get("time_utc")
            dt = _parse_iso(t)
            if t and dt is not None:
                mag_table[t] = dt
                mag_values[t] = d
    for f in ftps:
        for d in parse_ftpdetectinfo(f):
            t = _ff_time_utc(d.get("ff_file", ""))
            dt = _parse_iso(t)
            if t and dt is not None:
                dur_table[t] = dt
                dur_values[t] = d
    return mag_table, mag_values, dur_table, dur_values


def _gmn_station_times(date_iso: str) -> dict[str, list[datetime]]:
    """Map camera-code -> list of GMN multi-station event datetimes for a date."""
    out: dict[str, list[datetime]] = {}
    try:
        payload = gmn_data.events_for_date(date_iso)
    except Exception:
        logger.debug("GMN lookup failed for %s", date_iso, exc_info=True)
        return out
    for ev in payload.get("events") or []:
        if len(ev.get("stations") or []) < 2:
            continue
        dt = _parse_iso(ev.get("time"))
        if dt is None:
            continue
        for sta in ev["stations"]:
            out.setdefault(sta.upper(), []).append(dt)
    return out


def scan_date(cam_dirs: list[Path], date: str, tol_s: float,
              corr_window_s: float, stats: Stats) -> list[Clip]:
    """Scan every camera's clips for a single date, returning enriched Clip
    records (mag/duration/multistation populated). Cross-camera internal
    correlation is done here since we have all cameras for the date at once."""
    date_iso = _iso_date(date)
    gmn_times = _gmn_station_times(date_iso)
    clips: list[Clip] = []

    for cam_dir in cam_dirs:
        cam = cam_dir.name
        date_dir = cam_dir / date
        if not date_dir.is_dir():
            continue
        state = _read_state(date_dir)
        if state is None:
            # Can't know lock status -> never prune blind. Skip this (cam,date).
            stats.skipped_dates += 1
            logger.debug("skip %s/%s: no readable state.json", cam, date)
            continue
        meteors_dir = date_dir / "meteors"
        mag_table, mag_values, dur_table, dur_values = _rms_metadata(date_dir)

        for fname, info in (state.get("chunks") or {}).items():
            if not isinstance(info, dict):
                continue
            lock = info.get("lock")
            if not lock or not fname.endswith("_color.mkv"):
                continue
            if info.get("video_pruned"):
                continue  # already pruned in a prior run
            mkv = meteors_dir / fname
            if not mkv.exists():
                continue
            mt = lock.get("meteor_time") if isinstance(lock, dict) else None
            mt_dt = _parse_iso(mt)
            clip = Clip(
                cam=cam, date=date, filename=fname, mkv_path=mkv,
                meteor_time=mt,
                lock_type=(lock.get("lock_type") if isinstance(lock, dict) else None),
            )
            try:
                clip.size_bytes = mkv.stat().st_size
            except OSError:
                clip.size_bytes = 0

            mag_rec = _nearest(mt_dt, mag_table, mag_values, tol_s)
            if mag_rec:
                clip.mag = mag_rec.get("mag_apparent")
                if clip.mag is None:
                    clip.mag = mag_rec.get("mag_absolute")
            dur_rec = _nearest(mt_dt, dur_table, dur_values, tol_s)
            if dur_rec:
                clip.duration_s = dur_rec.get("duration_s")

            # GMN multi-station confirmation.
            for ev_dt in gmn_times.get(cam.upper(), []):
                if mt_dt is not None and abs((ev_dt - mt_dt).total_seconds()) <= tol_s:
                    clip.multistation = True
                    break

            clips.append(clip)
            stats.scanned_clips += 1

    # Internal multi-station correlation: any two clips from different cameras
    # within corr_window_s of each other => both are multi-station witnesses.
    timed = [c for c in clips if _parse_iso(c.meteor_time) is not None]
    timed.sort(key=lambda c: _parse_iso(c.meteor_time))  # type: ignore[arg-type]
    for i, a in enumerate(timed):
        if a.multistation:
            continue
        ta = _parse_iso(a.meteor_time)
        for b in timed[i + 1:]:
            tb = _parse_iso(b.meteor_time)
            if (tb - ta).total_seconds() > corr_window_s:  # type: ignore[operator]
                break
            if b.cam != a.cam:
                a.multistation = True
                b.multistation = True
    return clips


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------

def run(archive: Path, retention_days: int, bright_pct: float, long_pct: float,
        tol_s: float, corr_window_s: float, apply: bool, max_deletes: int,
        report_path: Path | None) -> Stats:
    stats = Stats()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    cam_dirs = sorted(
        d for d in archive.iterdir()
        if d.is_dir() and d.name not in _SKIP_DIRS and not d.name.startswith(".")
    )

    # Collect the union of all date dirs across cameras.
    all_dates: set[str] = set()
    for d in cam_dirs:
        try:
            for sub in d.iterdir():
                if sub.is_dir() and len(sub.name) == _DATE_DIR_LEN and sub.name.isdigit():
                    all_dates.add(sub.name)
        except OSError:
            stats.errors += 1
    dates_sorted = sorted(all_dates)
    logger.info("scanning %d cameras across %d dates in %s",
                len(cam_dirs), len(dates_sorted), archive)

    # Pass 1: enrich every clip.
    all_clips: list[Clip] = []
    for date in dates_sorted:
        stats.scanned_dates += 1
        all_clips.extend(scan_date(cam_dirs, date, tol_s, corr_window_s, stats))

    # Compute fleet-wide percentile thresholds (over all ages).
    mags = sorted(c.mag for c in all_clips if c.mag is not None)
    durs = sorted((c.duration_s for c in all_clips if c.duration_s is not None),
                  reverse=False)
    # Brightest = smallest magnitudes => keep mag <= P(bright_pct).
    bright_thr = _percentile(mags, bright_pct)
    # Longest = largest durations => keep duration >= P(100 - long_pct).
    long_thr = _percentile(durs, 100.0 - long_pct)
    logger.info("thresholds: brightest<=%.2f mag (n=%d), longest>=%.2f s (n=%d)",
                bright_thr if bright_thr is not None else float("nan"), len(mags),
                long_thr if long_thr is not None else float("nan"), len(durs))

    # Pass 2: decide + (optionally) delete.
    capped = False
    for clip in all_clips:
        if clip.date_obj >= cutoff:
            stats.kept_recent += 1
            continue

        reasons: list[str] = []
        if clip.lock_type == "manual":
            reasons.append("manual")
        if clip.multistation:
            reasons.append("multistation")
        if bright_thr is not None and clip.mag is not None and clip.mag <= bright_thr:
            reasons.append("bright")
        if long_thr is not None and clip.duration_s is not None and clip.duration_s >= long_thr:
            reasons.append("long")

        if reasons:
            for r in reasons:
                stats.keep_reasons[r] = stats.keep_reasons.get(r, 0) + 1
            if "manual" in reasons:
                stats.kept_manual += 1
            if "multistation" in reasons:
                stats.kept_multistation += 1
            if "bright" in reasons:
                stats.kept_bright += 1
            if "long" in reasons:
                stats.kept_long += 1
            continue

        # Prune candidate.
        if max_deletes and stats.pruned >= max_deletes:
            capped = True
            continue

        logger.info("%s prune %s/%s/%s  mag=%s dur=%s  %.1f MB",
                    "APPLY" if apply else "DRY", clip.cam, clip.date, clip.filename,
                    clip.mag, clip.duration_s, clip.size_bytes / 1e6)
        if apply:
            if _prune_clip(clip):
                stats.pruned += 1
                stats.pruned_bytes += clip.size_bytes
            else:
                stats.errors += 1
        else:
            stats.pruned += 1
            stats.pruned_bytes += clip.size_bytes

    if capped:
        logger.warning("max-deletes cap (%d) reached -- more clips remain "
                       "eligible; re-run to continue", max_deletes)

    _log_summary(stats, apply, capped, max_deletes)
    if report_path is not None:
        _write_report(report_path, stats, archive, retention_days,
                      bright_thr, long_thr, apply, capped)
    return stats


def _prune_clip(clip: Clip) -> bool:
    """Delete the MKV from the box and mark it pruned in state.json. The stack
    .webp and rms/ are left untouched. Returns True on success."""
    try:
        clip.mkv_path.unlink()
    except FileNotFoundError:
        pass  # already gone; still mark state below
    except OSError as exc:
        logger.error("failed to delete %s: %s", clip.mkv_path, exc)
        return False

    # Mark pruned in state.json so future runs skip it and the dashboard can
    # tell the clip's full video was archived-out (stack still served).
    state_path = clip.mkv_path.parent.parent / "state.json"
    try:
        state = json.loads(state_path.read_text())
        chunk = (state.get("chunks") or {}).get(clip.filename)
        if isinstance(chunk, dict):
            chunk["video_pruned"] = True
            chunk["video_pruned_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            tmp = state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(state_path)
    except (OSError, ValueError) as exc:
        logger.warning("deleted %s but could not update state.json: %s",
                       clip.mkv_path, exc)
    return True


def _log_summary(stats: Stats, apply: bool, capped: bool, max_deletes: int) -> None:
    logger.info("---- %s summary ----", "APPLY" if apply else "DRY-RUN")
    logger.info("clips scanned:        %d (%d dates, %d skipped)",
                stats.scanned_clips, stats.scanned_dates, stats.skipped_dates)
    logger.info("kept (still recent):  %d", stats.kept_recent)
    logger.info("kept manual:          %d", stats.kept_manual)
    logger.info("kept multi-station:   %d", stats.kept_multistation)
    logger.info("kept bright:          %d", stats.kept_bright)
    logger.info("kept long:            %d", stats.kept_long)
    logger.info("%s:              %d clips, %.2f GB",
                "PRUNED" if apply else "WOULD PRUNE",
                stats.pruned, stats.pruned_bytes / 1e9)
    if stats.errors:
        logger.warning("errors:               %d", stats.errors)


def _write_report(path: Path, stats: Stats, archive: Path, retention_days: int,
                  bright_thr: float | None, long_thr: float | None,
                  apply: bool, capped: bool) -> None:
    report = {
        "ran_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archive": str(archive),
        "applied": apply,
        "capped": capped,
        "retention_days": retention_days,
        "bright_threshold_mag": bright_thr,
        "long_threshold_s": long_thr,
        "scanned_clips": stats.scanned_clips,
        "scanned_dates": stats.scanned_dates,
        "skipped_dates": stats.skipped_dates,
        "kept_recent": stats.kept_recent,
        "kept_manual": stats.kept_manual,
        "kept_multistation": stats.kept_multistation,
        "kept_bright": stats.kept_bright,
        "kept_long": stats.kept_long,
        "pruned": stats.pruned,
        "pruned_gb": round(stats.pruned_bytes / 1e9, 3),
        "errors": stats.errors,
    }
    try:
        path.write_text(json.dumps(report, indent=2))
        logger.info("report written to %s", path)
    except OSError as exc:
        logger.warning("could not write report to %s: %s", path, exc)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--archive", type=Path, default=Path(DEFAULT_ARCHIVE),
                   help=f"archive root (default {DEFAULT_ARCHIVE})")
    p.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                   help=f"prune clips older than N days (default {DEFAULT_RETENTION_DAYS})")
    p.add_argument("--bright-percentile", type=float, default=DEFAULT_BRIGHT_PCT,
                   help=f"keep brightest N%% by magnitude (default {DEFAULT_BRIGHT_PCT})")
    p.add_argument("--long-percentile", type=float, default=DEFAULT_LONG_PCT,
                   help=f"keep longest N%% by duration (default {DEFAULT_LONG_PCT})")
    p.add_argument("--event-tolerance-s", type=float, default=DEFAULT_EVENT_TOLERANCE_S,
                   help="clip<->GMN/RMS time match window (s)")
    p.add_argument("--correlation-window-s", type=float,
                   default=DEFAULT_CORRELATION_WINDOW_S,
                   help="internal multi-station coincidence window (s)")
    p.add_argument("--apply", action="store_true",
                   help="actually delete (default: dry-run, delete nothing)")
    p.add_argument("--max-deletes", type=int, default=0,
                   help="cap deletions per run as a throttle (0 = unlimited)")
    p.add_argument("--report", type=Path, default=None,
                   help="write a JSON summary report to this path")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="log every prune decision at INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # Always show our own INFO summary lines.
    logger.setLevel(logging.INFO)

    if not args.archive.is_dir():
        logger.error("archive path %s is not a directory (is the box mounted?)",
                     args.archive)
        return 2
    run(
        archive=args.archive,
        retention_days=args.retention_days,
        bright_pct=args.bright_percentile,
        long_pct=args.long_percentile,
        tol_s=args.event_tolerance_s,
        corr_window_s=args.correlation_window_s,
        apply=args.apply,
        max_deletes=args.max_deletes,
        report_path=args.report,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
