"""Regression guard for the create_app() restart deadlock.

Background: starting the pollers / prefetch loop / tunnel init / archive-index
build INLINE in ``create_app()`` meant those threads began contending for the
import lock, the logging lock and the SSHFS+SQLite handles WHILE the boot thread
was still finishing its own lazy imports and route registration. Across a
``systemctl restart`` (stop overlapping start) that fork-after-threads race
probabilistically wedged the gunicorn worker in ``futex_wait`` at ~20 MB RSS —
``create_app()`` never returned, so the worker never bound its socket and never
served (curl hung → 000).

The fix moves every background start off the boot-critical path into a single
idempotent ``app._start_background_workers()`` closure, triggered AFTER the
worker is serving (a short ``threading.Timer`` fallback in ``create_app`` plus
the gunicorn ``post_worker_init`` hook). These tests pin the invariants:

  1. ``create_app()`` returns promptly and the returned app is serving-ready
     (routes registered, answers a trivial request) WITHOUT the background
     workers having started.
  2. The deferred hook, when background work is enabled, is invoked exactly once
     and is idempotent (double-trigger is a no-op).
  3. The ``ROVIMEN_DISABLE_BACKGROUND`` gate still fully skips background work.

Reuses the minimal-app fixture pattern from ``test_background_worker_gate.py``.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import yaml


def _minimal_app_env() -> Path:
    """Write a minimal users.yaml + dashboard_config.yaml and point the
    dashboard's env at throwaway paths. Returns the config path."""
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_defer_"))
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
    os.environ.setdefault("ROVIMEN_SECRET_KEY", "defer-test-secret")
    return cfg_path


def _build_app():
    cfg_path = _minimal_app_env()
    from rovimen_dashboard import create_app, load_config

    return create_app(load_config(cfg_path), cfg_path)


def test_create_app_returns_serving_ready_without_background(monkeypatch):
    """create_app() must return an app whose request surface works, and must
    NOT have started background workers on the boot path.

    The session-wide ROVIMEN_DISABLE_BACKGROUND=1 (conftest) means the timer is
    never armed and the hook is a no-op, so this proves the app is complete and
    serving-ready with zero background work — which is exactly the boot-critical
    state the worker must reach before anything else runs."""
    assert os.environ.get("ROVIMEN_DISABLE_BACKGROUND") == "1"

    started = time.monotonic()
    app = _build_app()
    elapsed = time.monotonic() - started

    # Construction must be prompt — no synchronous poll/tunnel/SSHFS on boot.
    assert elapsed < 10.0, f"create_app took {elapsed:.1f}s (boot path blocked?)"

    # The deferred hook is always installed, regardless of the gate.
    assert callable(getattr(app, "_start_background_workers", None))

    # Routes are registered → the app is serving-ready. /login is a public
    # path (no session required) so a trivial request completes without hanging.
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/login" in rules
    client = app.test_client()
    resp = client.get("/login")
    assert resp.status_code in (200, 302, 303)


def test_deferred_hook_runs_once_and_is_idempotent(monkeypatch):
    """With background work enabled, _start_background_workers() must fan out to
    the individual start_* functions exactly once even when triggered twice
    (timer + gunicorn post_worker_init both fire in production)."""
    monkeypatch.setenv("ROVIMEN_DISABLE_BACKGROUND", "0")

    import rovimen_dashboard as rd

    calls: list[str] = []

    # Patch every start_* / spawn site the closure touches so nothing real
    # (SSH tunnels, pollers, SSHFS scans, timers) runs during the test.
    monkeypatch.setattr(rd, "start_polling", lambda *a, **k: calls.append("polling"))
    monkeypatch.setattr(rd, "_start_gmn_poller", lambda *a, **k: calls.append("gmn"))
    monkeypatch.setattr(rd, "_disk_cache_load_into", lambda *a, **k: 0)

    class _FakeThread:
        def __init__(self, *a, **k):
            calls.append("thread:" + str(k.get("name", (k.get("target") or "").__class__.__name__)))

        def start(self):
            pass

    class _FakeTimer:
        # The create_app end-of-boot timer must NOT fire during the test; we
        # trigger the hook ourselves. Record that it was armed, then no-op.
        def __init__(self, interval, fn, *a, **k):
            self._fn = fn
            calls.append("timer-armed")

        def start(self):
            pass

    monkeypatch.setattr(rd.threading, "Thread", _FakeThread)
    monkeypatch.setattr(rd.threading, "Timer", _FakeTimer)

    # sky-dome scheduler + mdc poller are imported inside create_app / the
    # closure; patch the source module attrs so the imports resolve to stubs.
    import routes.sky_dome as sky_dome
    monkeypatch.setattr(sky_dome, "start_dome_scheduler", lambda *a, **k: calls.append("dome"))
    import mdc_poller
    monkeypatch.setattr(mdc_poller, "start_mdc_poller", lambda *a, **k: calls.append("mdc"))

    # tunnels is a real TunnelManager instance created inside create_app;
    # start_watchdog()/start_all() spawn threads in the tunnels module's own
    # namespace (unaffected by the rd.threading patch), so stub them on the
    # class to keep the test from launching real SSH machinery.
    import tunnels as tunnels_mod
    monkeypatch.setattr(
        tunnels_mod.TunnelManager, "start_watchdog",
        lambda self: calls.append("watchdog"),
    )
    monkeypatch.setattr(
        tunnels_mod.TunnelManager, "start_all",
        lambda self: calls.append("start_all"),
    )

    app = _build_app()

    # Boot path must have armed the fallback timer but NOT started any worker.
    assert "timer-armed" in calls
    assert "polling" not in calls, "background work leaked onto the boot path"

    calls.clear()
    app._start_background_workers()
    first = list(calls)
    assert "polling" in first and "gmn" in first and "mdc" in first and "dome" in first

    # Second trigger (the other of timer / post_worker_init) is a no-op.
    calls.clear()
    app._start_background_workers()
    assert calls == [], f"deferred hook ran twice: {calls}"


def test_disabled_gate_skips_deferred_hook(monkeypatch):
    """ROVIMEN_DISABLE_BACKGROUND=1 must make the deferred hook a no-op even
    when called directly (tests / E2E launcher rely on this)."""
    monkeypatch.setenv("ROVIMEN_DISABLE_BACKGROUND", "1")

    import rovimen_dashboard as rd

    calls: list[str] = []
    monkeypatch.setattr(rd, "start_polling", lambda *a, **k: calls.append("polling"))
    monkeypatch.setattr(rd, "_start_gmn_poller", lambda *a, **k: calls.append("gmn"))

    app = _build_app()
    app._start_background_workers()
    assert calls == [], "background work ran despite ROVIMEN_DISABLE_BACKGROUND=1"
