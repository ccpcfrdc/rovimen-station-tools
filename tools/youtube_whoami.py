#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-auth>=2.30.0",
#     "google-auth-oauthlib>=1.2.0",
#     "google-api-python-client>=2.130.0",
# ]
# ///
"""Show which YouTube channel the currently-stored token uploads to.

Reads ~/Downloads/youtube_token.json (or path passed as argv[1]) and
calls channels.list(mine=true) — prints the channel id, title, and
subscriber count so you know exactly where videos.insert will land.

Useful before testing an upload, especially if you have a Brand
Account separate from your personal channel."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as AuthRequest
from googleapiclient.discovery import build


def main() -> int:
    token_path = Path(sys.argv[1] if len(sys.argv) > 1 else "~/Downloads/youtube_token.json").expanduser()
    if not token_path.exists():
        sys.exit(f"Token file not found: {token_path}")

    payload = json.loads(token_path.read_text())
    creds = Credentials(
        token=None,
        refresh_token=payload["refresh_token"],
        token_uri=payload.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=payload["client_id"],
        client_secret=payload["client_secret"],
        scopes=payload.get("scopes", []),
    )
    if not creds.valid:
        creds.refresh(AuthRequest())

    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = yt.channels().list(
        part="snippet,statistics",
        mine=True,
    ).execute()
    items = resp.get("items", []) or []
    if not items:
        print("No channels visible to this token. The token may be bound to a")
        print("Google account that has no YouTube channel at all.")
        return 1

    print()
    print("=" * 60)
    print("Channels videos.insert will upload to with the current token:")
    print("=" * 60)
    for ch in items:
        sn = ch.get("snippet", {})
        st = ch.get("statistics", {})
        print(f"  Channel id:  {ch.get('id')}")
        print(f"  Title:       {sn.get('title')}")
        print(f"  Custom URL:  {sn.get('customUrl', '(none)')}")
        print(f"  Subscribers: {st.get('subscriberCount', '?')}")
        print(f"  Videos:      {st.get('videoCount', '?')}")
        print("-" * 60)
    print()
    if len(items) > 1:
        print("Multiple channels in the response — videos.insert defaults to")
        print("the first one. Re-run OAuth after switching to the right channel")
        print("if this isn't what you want.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
