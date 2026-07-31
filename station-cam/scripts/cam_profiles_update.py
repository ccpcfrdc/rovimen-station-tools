#!/usr/bin/env python3
"""Show the capture profiles delivered by the repo and, with --apply, push them
to the cameras.

Profiles are NOT stored in the station's local config — they live in the
repo-delivered profiles.json (installed next to this script, refreshed by
install.sh on every `git pull`). cam_enforce/cam-sync read them from there, so a
station is always in line with the repo without any local file being edited.

Typical flow when a profile changed in the repo:
    cd <repo>/station-cam && git pull && bash install.sh   # refresh profiles.json
    sudo cam-profiles-update --apply                        # review + cam-sync

Usage:
  cam-profiles-update           # list the delivered profiles
  cam-profiles-update --apply   # ...then push them to the cameras (cam-sync)
"""

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
PROFILES = os.environ.get("ROVIMEN_CAM_PROFILES", "/etc/rovimen-cam/profiles.json")


def summarize(prof):
    enc = prof.get("encode", {})
    dnc = prof.get("param", {}).get("DayNightColor")
    mode = {"0x00000001": "colour", "0x00000002": "mono"}.get(dnc, dnc)
    return (f"{enc.get('Compression')}/{enc.get('Resolution')}/"
            f"{enc.get('BitRateControl')}{enc.get('BitRate')} {mode}")


def main():
    if not os.path.exists(PROFILES):
        print(f"delivered profiles missing: {PROFILES} (run install.sh)")
        return 2
    with open(PROFILES) as f:
        profiles = json.load(f)
    print(f"delivered profiles ({PROFILES}):")
    for name, prof in profiles.items():
        print(f"  - {name:12} {summarize(prof)}")

    if "--apply" in sys.argv[1:]:
        print("\napplying to cameras (cam-sync)...")
        return subprocess.run(
            [sys.executable, os.path.join(_HERE, "cam_enforce.py"), "--sync"]
        ).returncode
    print("\n(dry run — pass --apply to push these to the cameras)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
