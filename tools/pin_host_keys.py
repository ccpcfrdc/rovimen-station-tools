#!/usr/bin/env python3
"""Scan a host's SSH keys and print the fingerprint(s) to pin (H5).

Usage:
    python3 tools/pin_host_keys.py <ip> [<ip> ...] [--port 22]

For each IP it runs ``ssh-keyscan`` and prints the ``SHA256:...``
fingerprint of every host key, ready to paste into a station entry in
``dashboard/dashboard_config.yaml``:

    stations:
      dragsina:
        ip: 100.64.0.2
        ssh_host_key_fingerprints:
        - SHA256:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

Once pinned, the dashboard (``_ensure_known_hosts``) refuses to trust any
scanned key for that IP unless its fingerprint matches a pinned value,
closing the first-contact TOFU MITM window.

**SECURITY**: the fingerprint printed here is whatever the network handed
back RIGHT NOW. If a man-in-the-middle is already on the path, this tool
will happily print the attacker's fingerprint. You MUST verify the
fingerprint out-of-band — e.g. by running ``ssh-keygen -lf
/etc/ssh/ssh_host_ed25519_key.pub`` on the station's own console and
comparing — before committing it to config. Pinning an unverified
fingerprint pins the attacker.

Operator-only: no daemons, no web UI. Read-only (only calls ssh-keyscan).
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def _fingerprints(ip: str, port: int) -> list[tuple[str, str]]:
    """Return ``[(key_type, "SHA256:...")]`` for a host, or [] on failure."""
    try:
        scan = subprocess.run(
            ["ssh-keyscan", "-p", str(port), "-T", "5", ip],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 — operator CLI, report and move on
        print(f"  ssh-keyscan failed for {ip}: {exc}", file=sys.stderr)
        return []

    lines = [
        ln for ln in scan.stdout.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    if not lines:
        print(f"  no host keys returned for {ip}", file=sys.stderr)
        return []

    results: list[tuple[str, str]] = []
    for ln in lines:
        try:
            fp = subprocess.run(
                ["ssh-keygen", "-lf", "-"],
                input=ln, capture_output=True, text=True, timeout=5,
            )
        except Exception:  # noqa: BLE001
            continue
        for out in fp.stdout.splitlines():
            parts = out.split()
            sha = next((t for t in parts if t.startswith("SHA256:")), None)
            ktype = parts[-1].strip("()") if parts else "?"
            if sha:
                results.append((ktype, sha))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan a host and print SSH host-key fingerprints to pin.",
    )
    parser.add_argument("ips", nargs="+", help="Host IP(s) to scan")
    parser.add_argument(
        "--port", type=int, default=22, help="SSH port (default 22)",
    )
    args = parser.parse_args()

    print(
        "# VERIFY every fingerprint OUT-OF-BAND (station console) before "
        "pinning.\n"
        "# A MITM on the path right now would make this tool print the "
        "attacker's key.\n"
        "# Paste the matching lines under the station's "
        "'ssh_host_key_fingerprints:' list.\n"
    )
    any_found = False
    for ip in args.ips:
        fps = _fingerprints(ip, args.port)
        if not fps:
            continue
        any_found = True
        print(f"{ip}:")
        print("  ssh_host_key_fingerprints:")
        for ktype, sha in fps:
            print(f"  - {sha}  # {ktype}")
        print()
    return 0 if any_found else 1


if __name__ == "__main__":
    sys.exit(main())
