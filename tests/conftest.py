"""Shared test fixtures for the ROVIMEN test suite."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Add module directories to sys.path so tests can import station scripts
# and dashboard modules directly, matching how they run in production.
REPO_ROOT = Path(__file__).resolve().parent.parent
STATION_SCRIPTS = REPO_ROOT / "rovimen-scripts"
DASHBOARD = REPO_ROOT / "dashboard"
TOOLS = REPO_ROOT / "tools"

for p in (STATION_SCRIPTS, DASHBOARD, TOOLS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# create_app() spawns a dozen forever-sleeping daemon workers (status/vitals
# pollers, tunnel watchdog, thumbnail prefetch, sky-dome scheduler, GMN/MDC and
# archive-index refreshers). Under pytest those threads are never joined, so
# every create_app-based test leaked them and they piled up across the ~1400-
# test suite — starving the 2-core CI runner until an unlucky test tripped the
# per-test pytest-timeout signal. Tests exercise the WSGI request surface, not
# the pollers, so disable the background workers for the whole session. Set
# before any dashboard import so the module-level default is read correctly.
os.environ.setdefault("ROVIMEN_DISABLE_BACKGROUND", "1")


@pytest.fixture
def tmp_capture_dir(tmp_path):
    """Create a temporary capture directory structure and return a config dict."""
    capture = tmp_path / "color_capture"
    capture.mkdir()
    cfg = {"videocapture_path": str(capture)}
    return capture, cfg


@pytest.fixture
def sample_config(tmp_path):
    """Minimal station config dict pointing at tmp_path."""
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "segment_duration": 20,
        "compression_level": 2,
        "stations": {
            "RO000H": {
                "rms_data_path": str(tmp_path / "rms_data"),
                "rotate": False,
            }
        },
        "services": {
            "stacker": {"enabled": True},
            "reencode": {"enabled": True},
            "detection_lock": {"enabled": True},
            "archive_upload": {"enabled": True},
            "timelapse_build": {"enabled": True},
        },
        "archive": {
            "enabled": True,
            "host": "10.0.0.1",
            "port": 22,
            "user": "rovimen",
            "base_path": "/rovimen",
        },
        "overlay": {
            "enabled": True,
            "font": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "font_size": 19,
            "text_opacity": 0.4,
            "network": "ROVIMEN",
            "coords": "45.0N 25.0E",
            "style": "standard",
        },
    }


@pytest.fixture
def make_state_json(tmp_path):
    """Factory fixture: create a state.json in the expected directory."""
    def _make(station_id: str, date_str: str, state: dict, cfg: dict):
        root = Path(cfg.get("videocapture_path", str(tmp_path / "color_capture")))
        night_dir = root / station_id / date_str
        night_dir.mkdir(parents=True, exist_ok=True)
        (night_dir / "state.json").write_text(json.dumps(state, indent=2))
        return night_dir
    return _make
