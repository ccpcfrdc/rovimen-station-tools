"""YouTube Data API v3 uploader for the dashboard's compilation builder.

Loads a refresh-token JSON written by `tools/youtube_oauth_setup.py`
(client_id + client_secret + refresh_token + token_uri + scopes) plus
the OAuth client_secret JSON, then uses google-auth + the YouTube
Data API to push compiled MP4s.

Imports are lazy so the dashboard module loads cleanly even when the
google-auth + googleapiclient libs aren't installed yet (e.g. on a
fresh dev VM). Any code path that calls `upload_video` will surface a
clear error if the libs are missing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Paths configurable via env, default to /opt/rovimen layout.
TOKEN_PATH = Path(os.environ.get("ROVIMEN_YOUTUBE_TOKEN", "/opt/rovimen/youtube_token.json"))
CLIENT_SECRET_PATH = Path(os.environ.get("ROVIMEN_YOUTUBE_CLIENT_SECRET", "/opt/rovimen/youtube_client_secret.json"))

YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"
DEFAULT_CATEGORY_ID = "28"  # Science & Technology — closest fit for meteor footage
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
ALLOWED_PRIVACY = {"public", "unlisted", "private"}


def _libs_importable() -> bool:
    try:
        import google.auth  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
        from googleapiclient.http import MediaFileUpload  # noqa: F401
        return True
    except Exception:
        return False


def is_configured() -> dict:
    """Return a dict the dashboard exposes via /api/youtube/status.

    Keys:
      configured: token+client_secret files present
      libs_ok:    google-auth + googleapiclient importable
      ready:      both of the above
      reason:     human-readable when not ready
    """
    libs_ok = _libs_importable()
    have_token = TOKEN_PATH.exists()
    have_secret = CLIENT_SECRET_PATH.exists()
    configured = have_token and have_secret
    # Reasons are returned to the browser, so they must not leak the
    # server-side credential paths (H7). Describe *what* is missing, not where.
    if not have_token:
        reason = "Missing YouTube OAuth token file"
    elif not have_secret:
        reason = "Missing YouTube client-secret file"
    elif not libs_ok:
        reason = "Python libs not installed (google-api-python-client, google-auth)"
    else:
        reason = ""
    return {
        "configured": configured,
        "libs_ok": libs_ok,
        "ready": configured and libs_ok,
        "reason": reason,
    }


def _build_credentials():
    """Construct a google.oauth2.credentials.Credentials from the saved
    refresh-token JSON. Raises RuntimeError if libs/files are missing."""
    if not _libs_importable():
        raise RuntimeError(
            "YouTube uploader libs not installed. "
            "Run: uv pip install --python /opt/rovimen/venv/bin/python "
            "google-api-python-client google-auth google-auth-httplib2"
        )
    if not TOKEN_PATH.exists():
        raise RuntimeError(f"YouTube token file not found at {TOKEN_PATH}")

    from google.oauth2.credentials import Credentials

    payload = json.loads(TOKEN_PATH.read_text())
    return Credentials(
        token=None,
        refresh_token=payload["refresh_token"],
        token_uri=payload.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=payload["client_id"],
        client_secret=payload["client_secret"],
        scopes=payload.get("scopes", SCOPES),
    )


def _build_youtube_service():
    """Return an authorised YouTube Data API v3 client. Refreshes the
    access token transparently on first call."""
    from google.auth.transport.requests import Request as AuthRequest
    from googleapiclient.discovery import build

    creds = _build_credentials()
    if not creds.valid:
        creds.refresh(AuthRequest())
    return build(
        YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION,
        credentials=creds, cache_discovery=False,
    )


def upload_video(
    filepath: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy: str = "unlisted",
    category_id: str = DEFAULT_CATEGORY_ID,
    progress_cb: Any = None,
) -> dict:
    """Push an MP4 to YouTube and return {video_id, video_url}.

    Uses chunked resumable upload (1 MB chunks) so the call doesn't
    block forever on slow networks and `progress_cb` receives a float
    in [0, 1] after each chunk. Caller surfaces this in the manifest's
    progress field.

    Raises:
      RuntimeError on missing libs/files,
      googleapiclient.errors.HttpError on API rejection (auth, quota,
        invalid metadata, etc.).
    """
    from googleapiclient.http import MediaFileUpload

    if privacy not in ALLOWED_PRIVACY:
        raise ValueError(f"privacy must be one of {ALLOWED_PRIVACY}, got {privacy!r}")
    file_path = Path(filepath)
    if not file_path.exists():
        raise FileNotFoundError(filepath)

    youtube = _build_youtube_service()

    body = {
        "snippet": {
            "title": title[:100],  # YouTube hard limit
            "description": description[:5000],
            "tags": [t[:30] for t in (tags or [])][:30],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    # 1 MB chunks — small enough that progress updates feel live, big
    # enough that overhead per chunk stays low. resumable=True is what
    # makes the call non-blocking-per-chunk.
    media = MediaFileUpload(
        str(file_path),
        mimetype="video/mp4",
        chunksize=1024 * 1024,
        resumable=True,
    )

    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    max_retries = 20
    max_duration = 7200
    start = time.monotonic()
    retry = 0
    response = None
    while response is None:
        if time.monotonic() - start > max_duration:
            raise RuntimeError("YouTube upload timed out after 2 hours")
        try:
            status, response = request.next_chunk()
        except Exception as exc:
            retry += 1
            if retry >= max_retries:
                raise RuntimeError(
                    f"YouTube upload failed after {max_retries} retries"
                ) from exc
            backoff = min(2 ** retry, 60)
            logger.warning("YouTube chunk upload error (retry %d/%d, backoff %ds): %s",
                           retry, max_retries, backoff, exc)
            time.sleep(backoff)
            continue
        if status and progress_cb is not None:
            try:
                progress_cb(float(status.progress()))
            except Exception:
                pass

    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"YouTube did not return a video id: {response!r}")

    return {
        "video_id": video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "raw": response,
    }
