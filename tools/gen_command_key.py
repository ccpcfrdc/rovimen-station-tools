#!/usr/bin/env python3
"""gen_command_key.py — mint the ed25519 command-signing keypair.

The server->station command channel (docs/reversed_http_push_design.md §3) signs
every command with a VPS-held ed25519 private key; stations verify with the
matching public key. This CLI mints that keypair.

  * The **private** PEM (``--private``, default /opt/rovimen/command_signing_key.pem,
    mode 0600, rovimen:rovimen) never leaves the VPS. Point the dashboard at it
    with ``ROVIMEN_COMMAND_SIGNING_KEY``.
  * The **public** PEM is written to ``rovimen-scripts/command_pubkey.pem`` by
    default so it is picked up by ``deployment/build_deploy.py`` and shipped to
    stations *in the signed deploy bundle* — NOT fetched from the VPS at runtime
    (§8 chicken-and-egg: the trust anchor must not ride the channel it secures).
    Commit the public key; never commit the private key.

Usage::

    uv run python tools/gen_command_key.py                 # default paths
    uv run python tools/gen_command_key.py --private /path/priv.pem --public /path/pub.pem
    uv run python tools/gen_command_key.py --public-only --from-private /path/priv.pem

Rotation: mint a new keypair, deploy the new public key in the next bundle to all
stations, then switch the dashboard's ``ROVIMEN_COMMAND_SIGNING_KEY`` to the new
private key. Keep the old public key deployed until every station has the new one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo layout: tools/ and dashboard/ are siblings; import the signer.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import command_signing  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRIVATE = Path("/opt/rovimen/command_signing_key.pem")
DEFAULT_PUBLIC = REPO_ROOT / "rovimen-scripts" / "command_pubkey.pem"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mint the command-signing ed25519 keypair.")
    ap.add_argument("--private", type=Path, default=DEFAULT_PRIVATE,
                    help=f"private PEM output path (default {DEFAULT_PRIVATE})")
    ap.add_argument("--public", type=Path, default=DEFAULT_PUBLIC,
                    help=f"public PEM output path (default {DEFAULT_PUBLIC})")
    ap.add_argument("--public-only", action="store_true",
                    help="only (re)derive the public key from an existing private key")
    ap.add_argument("--from-private", type=Path,
                    help="existing private PEM to derive the public key from "
                         "(with --public-only)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing private key")
    args = ap.parse_args(argv)

    if args.public_only:
        src = args.from_private or args.private
        priv = command_signing.load_private_key(src)
        args.public.parent.mkdir(parents=True, exist_ok=True)
        args.public.write_text(command_signing.public_key_pem(priv))
        print(f"public key written: {args.public}")
        return 0

    if args.private.exists() and not args.force:
        print(f"refusing to overwrite existing private key {args.private} "
              f"(use --force to rotate)", file=sys.stderr)
        return 1

    command_signing.generate_keypair(args.private, args.public)
    print(f"private key (0600, keep on VPS only): {args.private}")
    print(f"public key  (ship in deploy bundle):  {args.public}")
    print("\nNext steps:")
    print(f"  1. Point the dashboard at the private key: "
          f"export ROVIMEN_COMMAND_SIGNING_KEY={args.private}")
    print(f"  2. Commit {args.public.name} and rebuild the deploy bundle so "
          f"stations receive it.")
    print("  3. Do NOT commit the private key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
