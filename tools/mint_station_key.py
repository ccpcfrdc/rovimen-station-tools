#!/usr/bin/env python3
"""Mint a per-station ingest key for the ROVIMEN reversed-HTTP push API.

Each station holds exactly one ingest key that authorises writing its own
telemetry to ``/api/ingest/v1/<station>/*`` (and nothing else). See
``docs/reversed_http_push_design.md`` §2.2.

Usage:
    python3 tools/mint_station_key.py --station gmn0002 --label "Vaslui push agent"
                                      [--id gmn0002-2]
                                      [--path /opt/rovimen/station_keys.yaml]
                                      [--rate-limit "240/minute;5000/hour"]

The script appends one entry to the YAML file (creating it if absent, mode
0600), prints the freshly-minted secret to stdout exactly once, and exits. Hand
the printed secret to the station over a secure channel and drop it into the
station's ``~/.config/rovimen/ingest.key`` (0600) — it is NOT recoverable
afterwards.

To list existing (redacted) keys:
    python3 tools/mint_station_key.py --list

To disable a key (row kept so the id/station stays reserved for audit):
    python3 tools/mint_station_key.py --disable <id>

Operator-only: no daemons, no web UI. Run it as the user that owns
``station_keys.yaml`` on the target host (typically ``root`` on the VPS), then
``chown`` to the dashboard user if needed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Path resolution works both ways the dashboard ships:
# - Repo checkout: dashboard/ is a sibling of tools/.
# - VPS deploy: rsync flattens dashboard/*.py next to this script.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_DASHBOARD_DIR = _SCRIPT_DIR.parent / "dashboard"
for candidate in (_SCRIPT_DIR, _REPO_DASHBOARD_DIR):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import station_keys  # noqa: E402  — sys.path injection above


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mint or manage ROVIMEN per-station ingest keys.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=station_keys.DEFAULT_KEYS_PATH,
        help="Path to station_keys.yaml (default from ROVIMEN_STATION_KEYS_PATH "
             "or /opt/rovimen/station_keys.yaml).",
    )
    parser.add_argument(
        "--station",
        help="host_key the key authorises (e.g. 'gmn0002'). Required for minting.",
    )
    parser.add_argument(
        "--id",
        default=None,
        help="Key id (defaults to the station). Use a distinct id to rotate a "
             "station's key while keeping the old row for audit.",
    )
    parser.add_argument(
        "--label",
        default="",
        help="Human-readable description of the station / agent.",
    )
    parser.add_argument(
        "--rate-limit",
        default=None,
        help="Optional per-key Flask-Limiter override, e.g. '240/minute;5000/hour'.",
    )
    parser.add_argument("--list", action="store_true", help="List existing keys (no secrets).")
    parser.add_argument("--disable", metavar="ID", help="Disable the named key in place.")
    args = parser.parse_args()

    if args.list:
        rows = station_keys.list_keys(args.path)
        if not rows:
            print(f"(no station keys in {args.path})")
            return 0
        for r in rows:
            flag = " [DISABLED]" if r["disabled"] else ""
            override = f"  rate-limit={r['rate_limit_override']}" if r["rate_limit_override"] else ""
            print(
                f"{r['id']:20s} station={r['station']:12s} "
                f"created={r['created_at'] or '?'}  label={r['label']!r}{flag}{override}"
            )
        return 0

    if args.disable:
        if station_keys.disable_key(args.disable, path=args.path):
            print(f"disabled: {args.disable}")
            return 0
        print(f"not found: {args.disable}", file=sys.stderr)
        return 1

    if not args.station:
        parser.error("--station is required when minting (or use --list / --disable)")

    try:
        new, plaintext = station_keys.add_key(
            args.station,
            id=args.id,
            label=args.label,
            path=args.path,
            rate_limit_override=args.rate_limit,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"id:         {new.id}")
    print(f"station:    {new.station}")
    print(f"label:      {new.label}")
    print(f"created_at: {new.created_at}")
    print(f"secret:     {plaintext}")
    print()
    print("On the station, store it at ~/.config/rovimen/ingest.key (mode 0600).")
    print("Use it like:")
    print(
        f"  curl -H 'X-Station-Key: {plaintext}' -X POST "
        f"https://<DASHBOARD_HOST>/api/ingest/v1/{new.station}/heartbeat"
    )
    print()
    print(f"Saved to: {args.path} (hashed at rest)")
    print("This plaintext secret will NOT be shown again. Hand it to the station now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
