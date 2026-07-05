"""Sync dashboard_config.yaml back to the GitHub repo after admin edits.

When an admin changes the config via the dashboard UI, the YAML is
written to disk by save_config(). This module pushes that updated file
to the repo so the next deploy carries the change, and the repo stays
the single source of truth.

Requires GITHUB_TOKEN env var with repo contents:write scope.
Runs in a background thread so the admin API response isn't blocked.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import base64
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
# Set GITHUB_REPO to the "owner/repo" that holds your dashboard_config.yaml.
REPO = os.environ.get("GITHUB_REPO", "")
CONFIG_PATH_IN_REPO = "dashboard/dashboard_config.yaml"
# IMPORTANT: GITHUB_CONFIG_BRANCH MUST be set per tier in the systemd unit:
#   prod (/etc/rovimen-dashboard.env):     GITHUB_CONFIG_BRANCH=main
#   dev  (/etc/rovimen-dashboard-dev.env): GITHUB_CONFIG_BRANCH=development
# Without this, admin config edits on prod write to the development branch
# and are silently overwritten on the next prod deploy from main.
# The default below is "development" to fail safely (wrong branch = no data
# loss; prod edits just land in dev and raise a visible review question).
BRANCH = os.environ.get("GITHUB_CONFIG_BRANCH", "development")

_API = "https://api.github.com"


def _sync_to_github(local_path: Path) -> None:
    if not GITHUB_TOKEN:
        logger.debug("config_sync: no GITHUB_TOKEN, skipping")
        return

    try:
        content = local_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("config_sync: cannot read %s: %s", local_path, exc)
        return

    url = f"{_API}/repos/{REPO}/contents/{CONFIG_PATH_IN_REPO}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ROVIMEN-Dashboard/1.0",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(
            f"{url}?ref={BRANCH}", headers=headers, method="GET"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            existing = json.loads(resp.read())
        sha = existing.get("sha", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            sha = ""
        else:
            logger.warning("config_sync: GET failed: %s", exc)
            return
    except Exception as exc:
        logger.warning("config_sync: GET failed: %s", exc)
        return

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    body = {
        "message": "chore: sync dashboard_config.yaml from admin UI",
        "content": encoded,
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        logger.info("config_sync: pushed to %s/%s", REPO, BRANCH)
    except Exception as exc:
        logger.warning("config_sync: PUT failed: %s", exc)


def sync_config(local_path: Path) -> None:
    """Push config to GitHub in a background thread (non-blocking)."""
    t = threading.Thread(
        target=_sync_to_github, args=(local_path,), daemon=True
    )
    t.start()
