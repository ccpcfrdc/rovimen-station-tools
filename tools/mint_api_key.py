#!/usr/bin/env python3
"""Mint a new API key for the ROVIMEN public API.

Usage:
    python3 tools/mint_api_key.py --id <slug> --label "<human description>"
                                  [--path /opt/rovimen/api_keys.yaml]
                                  [--rate-limit "240/minute;5000/hour"]

The script appends one entry to the YAML file (creating it if absent),
prints the freshly-minted secret to stdout exactly once, and exits.
Hand the printed secret to the consumer over a secure channel — it is
NOT recoverable afterwards.

To list existing (redacted) keys:
    python3 tools/mint_api_key.py --list

To disable a key (without deleting its row, so the id stays reserved):
    python3 tools/mint_api_key.py --disable <id>

The script is operator-only: no daemons, no web UI. It writes the file
with mode 0600. Run it as the same user that owns ``api_keys.yaml`` on
the target host (typically ``root`` on the VPS), then ``chown`` to the
dashboard user if needed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# When run from the repo root the dashboard module is two levels up.
# Insert it onto sys.path so the script works without an installed
# package context.
# Path resolution works both ways the dashboard ships:
# - Repo checkout: dashboard/ is a sibling of tools/.
# - VPS deploy: rsync flattens dashboard/*.py to the same directory as
#   this script (the deploy workflow copies tools/<script>.py next to
#   the dashboard modules), so the script's own dir contains api_keys.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_DASHBOARD_DIR = _SCRIPT_DIR.parent / "dashboard"
for candidate in (_SCRIPT_DIR, _REPO_DASHBOARD_DIR):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import api_keys  # noqa: E402  — sys.path injection above


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mint or manage ROVIMEN public-API keys.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=api_keys.DEFAULT_KEYS_PATH,
        help="Path to api_keys.yaml (default from ROVIMEN_API_KEYS_PATH or "
             "/opt/rovimen/api_keys.yaml).",
    )
    parser.add_argument(
        "--id",
        help="Short stable identifier for the key (e.g. 'astromania-prod'). "
             "Required for --mint.",
    )
    parser.add_argument(
        "--label",
        default="",
        help="Human-readable description of who/what is using this key.",
    )
    parser.add_argument(
        "--rate-limit",
        default=None,
        help="Optional per-key Flask-Limiter override, e.g. '240/minute;5000/hour'.",
    )
    parser.add_argument("--list", action="store_true", help="List existing keys (no secrets printed).")
    parser.add_argument("--disable", metavar="ID", help="Disable the named key in place.")
    args = parser.parse_args()

    if args.list:
        rows = api_keys.list_keys(args.path)
        if not rows:
            print(f"(no keys in {args.path})")
            return 0
        for r in rows:
            flag = " [DISABLED]" if r["disabled"] else ""
            override = f"  rate-limit={r['rate_limit_override']}" if r["rate_limit_override"] else ""
            print(f"{r['id']:32s}  created={r['created_at'] or '?'}  label={r['label']!r}{flag}{override}")
        return 0

    if args.disable:
        if api_keys.disable_key(args.disable, path=args.path):
            print(f"disabled: {args.disable}")
            return 0
        print(f"not found: {args.disable}", file=sys.stderr)
        return 1

    if not args.id:
        parser.error("--id is required when minting a new key (or use --list / --disable)")

    try:
        new, plaintext = api_keys.add_key(
            id=args.id,
            label=args.label,
            path=args.path,
            rate_limit_override=args.rate_limit,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Print the secret to stdout exactly once, plus a copy-pasteable
    # header line the consumer can drop into their HTTP client config.
    # The stored row only carries the hash — plaintext is the operator's
    # last chance to copy it.
    print(f"id:         {new.id}")
    print(f"label:      {new.label}")
    print(f"created_at: {new.created_at}")
    print(f"secret:     {plaintext}")
    print()
    print("Use it like:")
    print(f"  curl -H 'X-API-Key: {plaintext}' https://<DASHBOARD_HOST>/api/public/v1/stations")
    print(f"  curl -H 'Authorization: Bearer {plaintext}' https://<DASHBOARD_HOST>/api/public/v1/stations")
    print()
    print(f"Saved to: {args.path} (hashed at rest)")
    print("This plaintext secret will NOT be shown again. Hand it to the consumer now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
