#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-auth-oauthlib>=1.2.0",
#     "google-api-python-client>=2.130.0",
# ]
# ///
"""One-shot OAuth setup for the dashboard's YouTube uploader.

Runs the standard "installed app" flow:
  1. Open this script's local web server on http://localhost:8765
  2. Open your browser to Google's consent screen
  3. After you approve, Google redirects to localhost with a one-time code
  4. The script swaps the code for an access + refresh token and writes
     youtube_token.json next to the client_secret JSON.

You then scp BOTH files to /opt/rovimen/ (and /opt/rovimen-dev/ if you
want to test on dev) so the dashboard's compilation builder can use the
refresh token to videos.insert without further interaction.

Usage:
    uv run tools/youtube_oauth_setup.py [path/to/client_secret.json]

If no path is supplied, the script searches ~/Downloads/ for a file
named client_secret_*.json and uses the most recent one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

# YouTube Data API v3 scopes:
#   youtube.upload   — videos.insert (the actual upload)
#   youtube.readonly — channels.list(mine=true), so the whoami helper can
#                      tell you which channel uploads will land on before
#                      you fire a real one.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

# Must match the redirect URI registered on the OAuth client. For Desktop
# clients Google allows http://localhost on any port, so we hard-code
# 8765 — easy to remember + unlikely to collide.
LOCAL_PORT = 8765


def find_client_secret() -> Path:
    if len(sys.argv) > 1:
        p = Path(sys.argv[1]).expanduser().resolve()
        if not p.exists():
            sys.exit(f"client_secret file not found: {p}")
        return p
    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        sys.exit(
            "Pass the path to client_secret_*.json as the first argument "
            "(or drop it in ~/Downloads/)."
        )
    candidates = sorted(
        downloads.glob("client_secret_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        sys.exit(
            "No client_secret_*.json found in ~/Downloads/. Pass the path "
            "explicitly: uv run tools/youtube_oauth_setup.py /path/to/file.json"
        )
    print(f"Using {candidates[0]}")
    return candidates[0]


def main() -> int:
    secret = find_client_secret()
    out_dir = secret.parent
    out_path = out_dir / "youtube_token.json"

    print()
    print("=" * 70)
    print("YouTube OAuth setup")
    print(f"  Client secret: {secret}")
    print(f"  Token output:  {out_path}")
    print(f"  Scope:         {SCOPES[0]}")
    print(f"  Local port:    {LOCAL_PORT}")
    print("=" * 70)
    print()
    print("A browser tab will open on Google's consent screen. After you")
    print("approve, you'll be redirected to a localhost page that says")
    print("'The authentication flow has completed' — that's success.")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=LOCAL_PORT,
        authorization_prompt_message="Please visit this URL: {url}",
        success_message=(
            "Authorization complete — you can close this tab. "
            "Return to the terminal."
        ),
        open_browser=True,
    )

    if not creds or not creds.refresh_token:
        sys.exit(
            "OAuth flow finished but no refresh_token was issued. This "
            "usually happens if you've previously authorized the same "
            "client. Revoke at https://myaccount.google.com/permissions "
            "and re-run."
        )

    payload = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "scopes": creds.scopes,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    os.chmod(out_path, 0o600)
    print()
    print(f"Wrote {out_path} (mode 0600)")
    print()
    print("Now upload BOTH files to the dashboard's VPS:")
    print()
    print(
        f"  scp {secret} {out_path} \\\n"
        f"      root@100.64.0.1:/opt/rovimen/"
    )
    print(
        "  ssh root@100.64.0.1 'mv /opt/rovimen/$(basename "
        f"{secret}) /opt/rovimen/youtube_client_secret.json && "
        "chmod 600 /opt/rovimen/youtube_*.json && "
        "cp /opt/rovimen/youtube_*.json /opt/rovimen-dev/ && "
        "chmod 600 /opt/rovimen-dev/youtube_*.json'"
    )
    print()
    print("Once the files are in place, deploy Phase 2 of the dashboard")
    print("and the cart's 'Build now' button will videos.insert as you go.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
