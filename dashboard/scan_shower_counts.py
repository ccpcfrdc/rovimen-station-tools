#!/usr/bin/env python3
"""Scan storagebox radiants files and write per-shower detection counts to JSON.

Designed to run as a daily cron job on the VPS (where storagebox is mounted).

Usage:
    python3 scan_shower_counts.py [--year YEAR] [--archive /srv/rovimen/archive]
                                  [--output /opt/rovimen/shower_year_counts.json]
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, date, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _parse_radiants_txt(path: Path) -> list[str]:
    """Return a list of shower codes (one per detection) from a radiants txt file."""
    codes = []
    try:
        text = path.read_text(errors="ignore")
    except OSError as e:
        logger.warning("cannot read %s: %s", path, e)
        return codes
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        shower = parts[3].strip() or "SPO"
        if shower == "...":
            shower = "SPO"
        codes.append(shower)
    return codes


def scan(archive: Path, year: int) -> dict[str, int]:
    year_prefix = str(year)
    counts: dict[str, int] = {}
    files_scanned = 0

    if not archive.exists():
        logger.error("archive path does not exist: %s", archive)
        sys.exit(1)

    for cam_dir in sorted(archive.iterdir()):
        if not cam_dir.is_dir():
            continue
        for date_dir in sorted(cam_dir.iterdir()):
            if not date_dir.is_dir() or not date_dir.name.startswith(year_prefix):
                continue
            rms_dir = date_dir / "rms"
            if not rms_dir.is_dir():
                continue
            for f in rms_dir.glob("*_radiants.txt"):
                for code in _parse_radiants_txt(f):
                    counts[code] = counts.get(code, 0) + 1
                files_scanned += 1

    logger.info("scanned %d radiants files, found %d shower codes for %d", files_scanned, len(counts), year)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan storagebox for per-shower detection counts")
    parser.add_argument("--year", type=int, default=date.today().year)
    parser.add_argument("--archive", type=Path,
                        default=Path(os.environ.get("ROVIMEN_ARCHIVE_PATH", "/srv/rovimen/archive")))
    parser.add_argument("--output", type=Path, default=Path("/opt/rovimen/shower_year_counts.json"))
    args = parser.parse_args()

    logger.info("scanning %s for year %d", args.archive, args.year)
    counts = scan(args.archive, args.year)

    payload = {
        "year": args.year,
        "counts": counts,
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    tmp = args.output.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    tmp.replace(args.output)
    logger.info("wrote %s (%d entries)", args.output, len(counts))


if __name__ == "__main__":
    main()
