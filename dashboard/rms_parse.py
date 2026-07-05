"""Pure parsers for RMS analysis files in the archive ``rms/`` subdir.

Extracted from ``routes.archive`` so they can be reused by non-Flask code
(e.g. the storage-box retention janitor and the detection index) without
importing the whole dashboard app.  Stdlib-only; no Flask, no dashboard-module
imports.

``parse_radiants_txt`` / ``parse_ftpdetectinfo`` keep their original public
output (extra keys may be added, none removed).  ``parse_session_detections``
is the canonical "one session dir -> merged detection rows" routine shared by
both the station indexer and the VPS bootstrap, so a given detection gets the
SAME identity (real FF filename + meteor number) regardless of which producer
wrote it — that is what keeps the two index sources from double-counting.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from pathlib import Path

# FF_<cam>_<YYYYMMDD>_<HHMMSS>_<mmm>_<frame>.fits  (mmm = milliseconds)
_FF_RE = re.compile(r"^FF_([A-Z0-9]+)_(\d{8})_(\d{6})_(\d{3})_")


def _ff_block_start(ff_file: str) -> datetime | None:
    """UTC datetime of the FF block start encoded in an FF filename."""
    m = _FF_RE.match(ff_file)
    if not m:
        return None
    date_s, time_s, ms_s = m.group(2), m.group(3), m.group(4)
    try:
        return datetime(
            int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8]),
            int(time_s[:2]), int(time_s[2:4]), int(time_s[4:6]),
            int(ms_s) * 1000,  # milliseconds -> microseconds
        )
    except ValueError:
        return None


def _opt(s: str) -> float | None:
    s = s.strip()
    return None if s in ("None", "") else float(s)


def _radiant_rows(path: Path) -> list[dict]:
    """Internal: radiants rows including a ``_begin_dt`` datetime for joining."""
    results: list[dict] = []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return results
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 17:
            continue
        try:
            begin_dt = datetime.strptime(parts[0].strip(), "%Y%m%d %H:%M:%S.%f")
        except ValueError:
            try:
                begin_dt = datetime.strptime(parts[0].strip().split(".")[0], "%Y%m%d %H:%M:%S")
            except ValueError:
                continue
        try:
            shower = parts[3].strip() or "SPO"
            if shower == "...":
                shower = "SPO"
            results.append({
                "_begin_dt": begin_dt,
                "time_utc": begin_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "jd": float(parts[1]),
                "solar_lon": _opt(parts[2]),
                "shower": shower,
                "ra_beg": float(parts[4]),
                "dec_beg": float(parts[5]),
                "ra_end": float(parts[6]),
                "dec_end": float(parts[7]),
                "ra_radiant": _opt(parts[8]),
                "dec_radiant": _opt(parts[9]),
                "mag_apparent": _opt(parts[14]),
                "mag_absolute": _opt(parts[15]),
                "radiant_elev": _opt(parts[16]),
            })
        except (ValueError, IndexError):
            continue
    return results


def parse_radiants_txt(path: Path) -> list[dict]:
    """Parse an RMS ``*_radiants.txt`` file into per-detection dicts.

    Each detection carries ``time_utc`` plus photometry (``mag_apparent`` /
    ``mag_absolute``, lower = brighter) and ``shower``.
    """
    rows = _radiant_rows(path)
    for r in rows:
        r.pop("_begin_dt", None)
    return rows


def _ftp_rows(path: Path) -> list[dict]:
    """Internal: FTPdetectinfo rows including ``_begin_dt`` and kinematics.

    Each row carries the real ``ff_file``, ``meteor_no`` (1-based, within that
    FF), ``duration_s``, ``angular_velocity``, azim/elev begin+end and a
    ``peak_mag`` (brightest segment).
    """
    results: list[dict] = []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return results
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("FF_") and line.endswith(".fits"):
            ff_file = line
            i += 1
            if i < len(lines) and "Recalibrated" in lines[i]:
                i += 1
            if i >= len(lines):
                break
            header = lines[i].strip().split()
            if len(header) < 4:
                i += 1
                continue
            try:
                meteor_no = int(header[1])
                num_segments = int(header[2])
                fps = float(header[3])
            except (ValueError, IndexError):
                i += 1
                continue
            i += 1
            frames: list[dict] = []
            while i < len(lines):
                dl = lines[i].strip()
                if not dl or dl.startswith("-") or dl.startswith("FF_"):
                    break
                fp = dl.split()
                if len(fp) >= 9:
                    try:
                        frames.append({
                            "frame": float(fp[0]),
                            "ra": float(fp[3]), "dec": float(fp[4]),
                            "azim": float(fp[5]), "elev": float(fp[6]),
                            "mag": float(fp[8]),
                        })
                    except ValueError:
                        break
                else:
                    break
                i += 1
            if frames:
                duration_s = (frames[-1]["frame"] - frames[0]["frame"]) / fps if fps > 0 else 0.0
                ang_vel = None
                if duration_s > 0 and len(frames) >= 2:
                    ra0, dec0 = math.radians(frames[0]["ra"]), math.radians(frames[0]["dec"])
                    ra1, dec1 = math.radians(frames[-1]["ra"]), math.radians(frames[-1]["dec"])
                    dlat, dlon = dec1 - dec0, ra1 - ra0
                    a = (math.sin(dlat / 2) ** 2
                         + math.cos(dec0) * math.cos(dec1) * math.sin(dlon / 2) ** 2)
                    ang_dist = math.degrees(2 * math.asin(min(1.0, math.sqrt(a))))
                    ang_vel = round(ang_dist / duration_s, 2)
                begin_dt = _ff_block_start(ff_file)
                if begin_dt is not None and fps > 0:
                    begin_dt = begin_dt + timedelta(seconds=frames[0]["frame"] / fps)
                results.append({
                    "_begin_dt": begin_dt,
                    "ff_file": ff_file,
                    "meteor_no": meteor_no,
                    "duration_s": round(duration_s, 3),
                    "angular_velocity": ang_vel,
                    "num_segments": num_segments,
                    "fps": fps,
                    "peak_mag": min(f["mag"] for f in frames),
                    "azim_beg": frames[0]["azim"],
                    "elev_beg": frames[0]["elev"],
                    "azim_end": frames[-1]["azim"],
                    "elev_end": frames[-1]["elev"],
                })
            continue
        i += 1
    return results


def parse_ftpdetectinfo(path: Path) -> list[dict]:
    """Parse an RMS ``FTPdetectinfo_*.txt`` file into per-detection dicts.

    Each detection carries ``ff_file`` and a computed ``duration_s`` (derived
    from the first/last frame numbers and the camera fps).
    """
    rows = _ftp_rows(path)
    for r in rows:
        r.pop("_begin_dt", None)
    return rows


# Maximum |begin-time| difference (seconds) for a radiants row to be considered
# the same detection as an FTP row.  Both derive from the same meteor so they
# agree to ~1 ms; radiants ``time_utc`` is truncated to whole seconds, hence the
# small but non-zero window.  Two distinct meteors on one camera within this
# window essentially never happens, and the match is nearest-first regardless.
_JOIN_TOLERANCE_S = 2.0


def _filtered_ftp_paths(rms_dir: Path) -> list[Path]:
    out: list[Path] = []
    for f in sorted(rms_dir.glob("FTPdetectinfo_*.txt")):
        n = f.name
        if "_unfiltered" in n or "_backup" in n:
            continue
        out.append(f)
    return out


def parse_session_detections(rms_dir: Path) -> list[dict]:
    """Parse one RMS session dir into merged, identity-stable detection rows.

    Joins radiants (photometry/astrometry) to FTPdetectinfo (kinematics) by
    meteor begin time rather than list position, so kinematic fields are never
    attached to the wrong meteor when the two files differ in count or order.
    Each returned row is keyed by the real ``ff_file`` + ``meteor_no``; FTP-only
    detections keep their FF identity, radiants-only rows (no FTP, very rare)
    get a deterministic ``RAD_<ts>`` placeholder so both producers agree.

    Returned rows do NOT carry ``cam``/``date``/``source`` — the caller adds
    those (it knows the camera and night unambiguously from the path).
    """
    # FTP detections, de-duplicated by (ff_file, meteor_no) across any number of
    # filtered FTPdetectinfo files in the dir (e.g. recalibrated + legacy).
    ftp_by_key: dict[tuple[str, int], dict] = {}
    for f in _filtered_ftp_paths(rms_dir):
        for row in _ftp_rows(f):
            ftp_by_key[(row["ff_file"], row["meteor_no"])] = row
    ftps = list(ftp_by_key.values())

    radiants: list[dict] = []
    for f in sorted(rms_dir.glob("*_radiants.txt")):
        radiants.extend(_radiant_rows(f))

    used = [False] * len(radiants)
    rows: list[dict] = []

    for ftp in ftps:
        match: dict | None = None
        if ftp["_begin_dt"] is not None:
            best_idx, best_dt = -1, _JOIN_TOLERANCE_S + 1.0
            for idx, rad in enumerate(radiants):
                if used[idx]:
                    continue
                diff = abs((ftp["_begin_dt"] - rad["_begin_dt"]).total_seconds())
                if diff < best_dt:
                    best_dt, best_idx = diff, idx
            if best_idx >= 0 and best_dt <= _JOIN_TOLERANCE_S:
                used[best_idx] = True
                match = radiants[best_idx]
        rows.append(_merge_row(ftp, match))

    # Radiants with no FTP partner (rare): keep with a deterministic placeholder.
    for idx, rad in enumerate(radiants):
        if used[idx]:
            continue
        rows.append(_radiant_only_row(rad))

    return rows


def _merge_row(ftp: dict, rad: dict | None) -> dict:
    """One detection from an FTP block, enriched with radiants if matched."""
    if rad is not None:
        time_utc = rad.get("time_utc")
        mag_apparent = rad.get("mag_apparent")
    else:
        bdt = ftp.get("_begin_dt")
        time_utc = bdt.strftime("%Y-%m-%dT%H:%M:%S") if bdt is not None else None
        mag_apparent = ftp.get("peak_mag")
    rad = rad or {}
    return {
        "ff_file": ftp["ff_file"],
        "meteor_no": ftp["meteor_no"],
        "time_utc": time_utc,
        "jd": rad.get("jd"),
        "solar_lon": rad.get("solar_lon"),
        "shower": rad.get("shower"),
        "mag_apparent": mag_apparent,
        "mag_absolute": rad.get("mag_absolute"),
        "duration_s": ftp.get("duration_s"),
        "angular_velocity": ftp.get("angular_velocity"),
        "num_segments": ftp.get("num_segments"),
        "fps": ftp.get("fps"),
        "ra_beg": rad.get("ra_beg"),
        "dec_beg": rad.get("dec_beg"),
        "ra_end": rad.get("ra_end"),
        "dec_end": rad.get("dec_end"),
        "ra_radiant": rad.get("ra_radiant"),
        "dec_radiant": rad.get("dec_radiant"),
        "radiant_elev": rad.get("radiant_elev"),
        "azim_beg": ftp.get("azim_beg"),
        "elev_beg": ftp.get("elev_beg"),
        "azim_end": ftp.get("azim_end"),
        "elev_end": ftp.get("elev_end"),
    }


def _radiant_only_row(rad: dict) -> dict:
    bdt = rad["_begin_dt"]
    ff = f"RAD_{bdt.strftime('%Y%m%d_%H%M%S_%f')}"
    return {
        "ff_file": ff,
        "meteor_no": 1,
        "time_utc": rad.get("time_utc"),
        "jd": rad.get("jd"),
        "solar_lon": rad.get("solar_lon"),
        "shower": rad.get("shower"),
        "mag_apparent": rad.get("mag_apparent"),
        "mag_absolute": rad.get("mag_absolute"),
        "duration_s": None,
        "angular_velocity": None,
        "num_segments": None,
        "fps": None,
        "ra_beg": rad.get("ra_beg"),
        "dec_beg": rad.get("dec_beg"),
        "ra_end": rad.get("ra_end"),
        "dec_end": rad.get("dec_end"),
        "ra_radiant": rad.get("ra_radiant"),
        "dec_radiant": rad.get("dec_radiant"),
        "radiant_elev": rad.get("radiant_elev"),
        "azim_beg": None,
        "elev_beg": None,
        "azim_end": None,
        "elev_end": None,
    }
