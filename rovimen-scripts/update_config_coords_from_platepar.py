#!/usr/bin/env python3
"""update_config_coords_from_platepar.py — Update config.json with platepar data.

Reads az_centre / alt_centre per camera and station lat/lon/elev from
~/RMS_cam*/platepar_cmn2010.cal and writes them into config.json.

Run after completing platepar calibration for one or more cameras.

Usage:
    python3 update_config_coords_from_platepar.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPTS_DIR / "config.json"


def load_platepars(home: Path) -> dict[str, dict]:
    """Return {station_code: platepar_dict} for all found platepars."""
    result = {}
    for platepar_path in sorted(home.glob("RMS_cam*/platepar_cmn2010.cal")):
        try:
            data = json.loads(platepar_path.read_text())
        except Exception as e:
            print(f"  WARN: could not read {platepar_path}: {e}")
            continue
        code = data.get("station_code") or data.get("stationID")
        if not code:
            print(f"  WARN: no station_code in {platepar_path} — skipping")
            continue
        result[code] = data
        print(f"  Found platepar: {platepar_path.parent.name} → {code}"
              f"  (az={data.get('az_centre', '?'):.1f}  alt={data.get('alt_centre', '?'):.1f})")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found", file=sys.stderr)
        sys.exit(1)

    config = json.loads(CONFIG_PATH.read_text())
    home = Path.home()

    print("Scanning for platepars...")
    platepars = load_platepars(home)

    if not platepars:
        print("No platepars found — nothing to update.")
        sys.exit(0)

    changes: list[str] = []

    # Update station-level lat/lon/elev from the first platepar found
    first = next(iter(platepars.values()))
    for field, key in [("latitude", "lat"), ("longitude", "lon"), ("elevation", "elev")]:
        val = first.get(key)
        if val is not None:
            old = config.get(field)
            if old != val:
                changes.append(f"  {field}: {old} → {val}")
                config[field] = val

    # Update az/alt per camera in the stations block
    stations = config.get("stations", {})
    for code, platepar in platepars.items():
        if code not in stations:
            print(f"  WARN: {code} not in config.json stations — skipping")
            continue
        for field, key in [("az", "az_centre"), ("alt", "alt_centre")]:
            val = platepar.get(key)
            if val is not None:
                val = round(val, 1)
                old = stations[code].get(field)
                if old != val:
                    changes.append(f"  stations.{code}.{field}: {old} → {val}")
                    stations[code][field] = val

    if not changes:
        print("config.json already up to date — nothing to change.")
        sys.exit(0)

    print("\nChanges:")
    for c in changes:
        print(c)

    if args.dry_run:
        print("\n[dry-run] no changes written.")
        sys.exit(0)

    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=4))
    os.replace(tmp, CONFIG_PATH)
    print(f"\nUpdated {CONFIG_PATH}")


if __name__ == "__main__":
    main()
