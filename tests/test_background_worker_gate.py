"""Regression guard for the create_app background-worker leak.

create_app() starts a dozen forever-sleeping daemon workers (status/vitals
pollers, tunnel watchdog, thumbnail prefetch, sky-dome scheduler, GMN/MDC and
archive-index refreshers, per-route cache-cleanup). Nothing joins them, so
every create_app-based test used to leak the full set; across the ~1400-test
suite those threads piled up and starved the CI runner until an unlucky test
tripped the per-test pytest-timeout signal (the reported flaky timeouts).

``ROVIMEN_DISABLE_BACKGROUND=1`` builds the identical WSGI app — routes and
caches intact — but starts none of those daemons. The session-wide conftest
sets it, so this test simply proves the gate holds: repeated create_app() calls
must not accumulate lingering threads beyond a tiny, non-growing baseline.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

import pytest
import yaml


def test_background_gate_respects_env(monkeypatch):
    import rovimen_dashboard

    monkeypatch.setenv("ROVIMEN_DISABLE_BACKGROUND", "1")
    assert rovimen_dashboard._background_workers_enabled() is False

    monkeypatch.setenv("ROVIMEN_DISABLE_BACKGROUND", "0")
    assert rovimen_dashboard._background_workers_enabled() is True

    # Fail-safe: anything other than "1" leaves the workers enabled (prod).
    monkeypatch.delenv("ROVIMEN_DISABLE_BACKGROUND", raising=False)
    assert rovimen_dashboard._background_workers_enabled() is True
    monkeypatch.setenv("ROVIMEN_DISABLE_BACKGROUND", "true")
    assert rovimen_dashboard._background_workers_enabled() is True


def _minimal_app():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_bg_"))
    (tmp / "users.yaml").write_text(yaml.safe_dump({}))
    cfg = {
        "station_api_port": 7779,
        "stations": {
            "a": {
                "ip": "100.0.0.1",
                "label": "A",
                "cameras": [{"code": "RO000A", "cam_ip": "192.168.1.10"}],
            }
        },
    }
    cfg_path = tmp / "dashboard_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))

    for key, val in {
        "ROVIMEN_USERS_PATH": tmp / "users.yaml",
        "THUMB_CACHE_DIR": tmp / "thumb_cache",
        "ROVIMEN_CACHE_PATH": tmp / "cache",
        "ROVIMEN_KNOWN_HOSTS": tmp / "known_hosts",
        "ROVIMEN_AUDIT_LOG_PATH": tmp / "audit.log",
        "ROVIMEN_ACTIVITY_LOG_PATH": tmp / "activity.log",
        "ROVIMEN_STATION_STATE_DB": tmp / "station_state.db",
        "ROVIMEN_DETECTIONS_DB": tmp / "detections.db",
    }.items():
        os.environ[key] = str(val)
    os.environ.setdefault("ROVIMEN_SECRET_KEY", "bg-test-secret")

    from rovimen_dashboard import create_app, load_config

    return create_app(load_config(cfg_path), cfg_path)


def test_create_app_does_not_leak_background_threads():
    """With the session-wide ROVIMEN_DISABLE_BACKGROUND=1 (see conftest),
    building several apps must not accumulate lingering daemon threads.
    A tiny fixed baseline (e.g. the singleton activity-log writer) is fine;
    what must NOT happen is per-call growth of poller/scheduler threads."""
    assert os.environ.get("ROVIMEN_DISABLE_BACKGROUND") == "1"

    baseline = threading.active_count()
    for _ in range(3):
        _minimal_app()
    time.sleep(0.3)  # let any (unexpected) spawned thread register

    delta = threading.active_count() - baseline
    lingering = [
        t.name for t in threading.enumerate() if t is not threading.main_thread()
    ]
    # Pre-fix this was ~15 per call (45 for three). Allow a small non-growing
    # baseline; the real regression signal is proportional-to-call-count growth.
    assert delta <= 2, f"background threads leaked (delta={delta}): {lingering}"
