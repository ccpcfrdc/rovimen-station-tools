#!/usr/bin/env python3
"""station_watchdog.py — Station health monitor with Discord alerts.

Polls all stations every N minutes via their REST APIs. Sends Discord
alerts (color-coded embeds) on state transitions: new problem, resolved.

Runs on the VPS as a systemd service alongside rovimen-dashboard.

Usage:
    python3 station_watchdog.py [--config dashboard_config.yaml] [--watchdog-config watchdog_config.yaml]
"""

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml

from rovimen_dashboard import (
    DashboardConfig,
    TunnelManager,
    load_config,
    station_get_status,
    station_url,
)

logger = logging.getLogger("station_watchdog")

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

EMBED_COLORS = {
    SEVERITY_CRITICAL: 0xFF0000,
    SEVERITY_WARNING: 0xFF8C00,
    SEVERITY_INFO: 0x00CC00,
}

SEVERITY_EMOJI = {
    SEVERITY_CRITICAL: "\U0001f6a8",
    SEVERITY_WARNING: "⚠️",
    SEVERITY_INFO: "✅",
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class WatchdogConfig:
    discord_webhook_url: str = ""
    poll_interval_s: int = 300
    disk_warn_pct: int = 85
    disk_critical_pct: int = 95
    janitor_stale_hours: int = 26
    dawn_deadline_utc: str = "12:00"
    repeat_alert_minutes: int = 120
    resolve_delay_polls: int = 2
    dawn_check_start: str = "08:00"
    dawn_check_end: str = "14:00"


def load_watchdog_config(path: Path) -> WatchdogConfig:
    raw = yaml.safe_load(path.read_text())
    cfg = WatchdogConfig()
    cfg.discord_webhook_url = raw.get("discord_webhook_url", "")
    cfg.poll_interval_s = raw.get("poll_interval_s", 300)
    t = raw.get("thresholds", {})
    cfg.disk_warn_pct = t.get("disk_warn_pct", 85)
    cfg.disk_critical_pct = t.get("disk_critical_pct", 95)
    cfg.janitor_stale_hours = t.get("janitor_stale_hours", 26)
    cfg.dawn_deadline_utc = t.get("dawn_deadline_utc", "12:00")
    c = raw.get("cooldowns", {})
    cfg.repeat_alert_minutes = c.get("repeat_alert_minutes", 120)
    cfg.resolve_delay_polls = c.get("resolve_delay_polls", 2)
    w = raw.get("dawn_check_window_utc", {})
    cfg.dawn_check_start = w.get("start", "08:00")
    cfg.dawn_check_end = w.get("end", "14:00")
    return cfg


# ---------------------------------------------------------------------------
# Alert state tracking
# ---------------------------------------------------------------------------


@dataclass
class AlertEntry:
    alert_key: str
    severity: str
    title: str
    detail: str
    host_key: str
    station_label: str
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_alerted: datetime | None = None
    ok_polls: int = 0
    problem_polls: int = 0
    grace_polls: int = 1


@dataclass
class Notification:
    entry: AlertEntry
    kind: str  # "alert", "resolve", or "info"


class AlertTracker:
    def __init__(self, wcfg: WatchdogConfig) -> None:
        self._wcfg = wcfg
        self._active: dict[str, AlertEntry] = {}
        self._pending: list[Notification] = []
        self._notified_dawn: dict[str, datetime] = {}

    def report_problem(
        self,
        key: str,
        severity: str,
        title: str,
        detail: str,
        host_key: str,
        station_label: str,
        grace_polls: int = 1,
    ) -> None:
        now = datetime.now(timezone.utc)
        existing = self._active.get(key)

        if existing:
            existing.ok_polls = 0
            existing.problem_polls += 1
            existing.severity = severity
            existing.detail = detail
            existing.title = title
            if existing.last_alerted is None and existing.problem_polls >= existing.grace_polls:
                existing.last_alerted = now
                self._pending.append(Notification(existing, "alert"))
            elif existing.last_alerted is not None:
                cooldown = timedelta(minutes=self._wcfg.repeat_alert_minutes)
                if now - existing.last_alerted >= cooldown:
                    existing.last_alerted = now
                    self._pending.append(Notification(existing, "alert"))
            return

        entry = AlertEntry(
            alert_key=key,
            severity=severity,
            title=title,
            detail=detail,
            host_key=host_key,
            station_label=station_label,
            first_seen=now,
            problem_polls=1,
            grace_polls=grace_polls,
        )
        self._active[key] = entry
        if grace_polls <= 1:
            entry.last_alerted = now
            self._pending.append(Notification(entry, "alert"))

    def report_info(self, title: str, detail: str, host_key: str, station_label: str) -> None:
        entry = AlertEntry(
            alert_key="", severity=SEVERITY_INFO,
            title=title, detail=detail,
            host_key=host_key, station_label=station_label,
        )
        self._pending.append(Notification(entry, "info"))

    def report_ok(self, key: str) -> None:
        existing = self._active.get(key)
        if existing is None:
            return
        existing.ok_polls += 1
        if existing.ok_polls >= self._wcfg.resolve_delay_polls:
            del self._active[key]
            self._pending.append(Notification(existing, "resolve"))

    def drain_notifications(self) -> list[Notification]:
        pending = self._pending
        self._pending = []
        return pending

    @property
    def active_count(self) -> int:
        return len(self._active)


# ---------------------------------------------------------------------------
# Discord messaging
# ---------------------------------------------------------------------------

_last_discord_ts: float = 0.0


def send_discord_embed(
    webhook_url: str,
    title: str,
    description: str,
    color: int,
    footer: str = "",
) -> bool:
    global _last_discord_ts
    if not webhook_url:
        logger.warning("No Discord webhook URL — skipping: %s", title)
        return False

    elapsed = time.time() - _last_discord_ts
    if elapsed < 2.0:
        time.sleep(2.0 - elapsed)

    embed: dict[str, Any] = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if footer:
        embed["footer"] = {"text": footer}

    # Tight timeout (P1-16): Discord webhook calls run inside the
    # watchdog poll loop. A black-holed webhook URL with no timeout
    # would hang every cycle indefinitely and silently freeze health
    # checks for the entire fleet. Ten seconds is generous enough for
    # a real slow upstream, short enough to recover within a single
    # poll window. The broad except below already prevents a webhook
    # failure from propagating out of this function.
    try:
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        _last_discord_ts = time.time()
        ok = resp.status_code in (200, 204)
        if not ok:
            logger.error("Discord returned %d: %s", resp.status_code, resp.text[:200])
        return ok
    except requests.Timeout:
        # Distinguished log line so an operator can tell "Discord slow"
        # from any other failure mode without grep'ing stack traces.
        logger.warning("Discord webhook timed out after 10s; skipping: %s", title)
        return False
    except Exception:
        logger.exception("Failed to send Discord embed")
        return False


def dispatch_notifications_batched(webhook_url: str, notifications: list[Notification]) -> int:
    """Group notifications by (host_key, kind) and send one embed per group."""
    from collections import defaultdict

    groups: dict[tuple[str, str], list[Notification]] = defaultdict(list)
    for notif in notifications:
        groups[(notif.entry.host_key, notif.kind)].append(notif)

    sent = 0
    for (host_key, kind), group in groups.items():
        label = group[0].entry.station_label
        if kind == "alert":
            worst = SEVERITY_WARNING
            lines = []
            for notif in group:
                if notif.entry.severity == SEVERITY_CRITICAL:
                    worst = SEVERITY_CRITICAL
                lines.append(f"• {notif.entry.detail}")
            emoji = SEVERITY_EMOJI[worst]
            title = f"{emoji} {worst.upper()}: {label} ({host_key})"
            send_discord_embed(webhook_url, title, "\n".join(lines), EMBED_COLORS[worst],
                               f"ROVIMEN Watchdog | {len(group)} issue(s)")
        elif kind == "info":
            lines = [f"• {notif.entry.detail}" for notif in group]
            title = f"✅ {label} ({host_key})"
            send_discord_embed(webhook_url, title, "\n".join(lines), EMBED_COLORS[SEVERITY_INFO],
                               "ROVIMEN Watchdog")
        else:
            now = datetime.now(timezone.utc)
            lines = []
            for notif in group:
                duration = now - notif.entry.first_seen
                hours, remainder = divmod(int(duration.total_seconds()), 3600)
                minutes = remainder // 60
                dur_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"
                lines.append(f"• ~~{notif.entry.title}~~ ({dur_str})")
            title = f"✅ RESOLVED: {label} ({host_key})"
            send_discord_embed(webhook_url, title, "\n".join(lines), EMBED_COLORS[SEVERITY_INFO],
                               f"ROVIMEN Watchdog | {len(group)} resolved")
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


def check_station(
    host_key: str,
    config: DashboardConfig,
    tunnels: TunnelManager,
    wcfg: WatchdogConfig,
    tracker: AlertTracker,
) -> None:
    station = config.stations[host_key]
    label = station.label

    status = station_get_status(config, tunnels, host_key, "/api/status")

    key_reach = f"{host_key}:unreachable"
    if not status.get("online"):
        tracker.report_problem(
            key_reach, SEVERITY_CRITICAL,
            f"{label} unreachable",
            f"Station API not responding: {status.get('error', 'unknown')}",
            host_key, label,
            grace_polls=3,
        )
        return
    tracker.report_ok(key_reach)

    key_api = f"{host_key}:api_error"
    if "error" in status and status.get("online"):
        tracker.report_problem(
            key_api, SEVERITY_WARNING,
            f"{label} API error",
            f"Status endpoint error: {status['error']}",
            host_key, label,
            grace_polls=3,
        )
    else:
        tracker.report_ok(key_api)

    # Services — values are "active"/"inactive" strings
    services = status.get("services", {})
    down_svcs = []
    for svc_name, svc_state in services.items():
        is_active = svc_state == "active" if isinstance(svc_state, str) else bool(svc_state)
        if not is_active:
            down_svcs.append(svc_name)

    key_svc = f"{host_key}:services_down"
    if down_svcs:
        svcs_str = ", ".join(f"`{s}`" for s in down_svcs)
        tracker.report_problem(
            key_svc, SEVERITY_CRITICAL,
            f"{label} services down",
            f"{len(down_svcs)} service(s) down: {svcs_str}",
            host_key, label,
        )
    else:
        tracker.report_ok(key_svc)

    # Disk usage — main disk from status['disk'] and extra mounts from status['extra_disks']
    disks_to_check: list[tuple[str, int]] = []
    main_disk = status.get("disk", {})
    if isinstance(main_disk, dict) and "pct" in main_disk:
        device = main_disk.get("device", "root")
        disks_to_check.append((device, main_disk["pct"]))
    for ed in status.get("extra_disks", []):
        if isinstance(ed, dict) and "pct" in ed:
            mount = ed.get("mount", ed.get("source", "unknown"))
            disks_to_check.append((mount, ed["pct"]))

    for mount, pct in disks_to_check:
        key_disk = f"{host_key}:disk:{mount}"
        if pct >= wcfg.disk_critical_pct:
            tracker.report_problem(
                key_disk, SEVERITY_CRITICAL,
                f"{label} disk critical",
                f"`{mount}` at {pct}%",
                host_key, label,
            )
        elif pct >= wcfg.disk_warn_pct:
            tracker.report_problem(
                key_disk, SEVERITY_WARNING,
                f"{label} disk warning",
                f"`{mount}` at {pct}%",
                host_key, label,
            )
        else:
            tracker.report_ok(key_disk)

    # Janitor staleness
    _check_janitor(host_key, label, config, tunnels, wcfg, tracker)

    # Dawn completeness (only during the check window)
    _check_dawn(host_key, label, station, config, tunnels, wcfg, tracker)


def _check_janitor(
    host_key: str,
    label: str,
    config: DashboardConfig,
    tunnels: TunnelManager,
    wcfg: WatchdogConfig,
    tracker: AlertTracker,
) -> None:
    key = f"{host_key}:janitor_stale"
    try:
        url = station_url(config, tunnels, host_key, "/api/storagewatch/status")
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return

    last_run = data.get("last_run")
    if not last_run:
        tracker.report_ok(key)
        return

    try:
        last_dt = datetime.fromisoformat(last_run)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        tracker.report_ok(key)
        return

    age = datetime.now(timezone.utc) - last_dt
    if age > timedelta(hours=wcfg.janitor_stale_hours):
        hours = int(age.total_seconds() / 3600)
        tracker.report_problem(
            key, SEVERITY_WARNING,
            f"{label} janitor stale",
            f"Last janitor run {hours}h ago (threshold: {wcfg.janitor_stale_hours}h)",
            host_key, label,
        )
    else:
        tracker.report_ok(key)


def _check_dawn(
    host_key: str,
    label: str,
    station: Any,
    config: DashboardConfig,
    tunnels: TunnelManager,
    wcfg: WatchdogConfig,
    tracker: AlertTracker,
) -> None:
    now_utc = datetime.now(timezone.utc)
    start_h, start_m = map(int, wcfg.dawn_check_start.split(":"))
    end_h, end_m = map(int, wcfg.dawn_check_end.split(":"))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    now_minutes = now_utc.hour * 60 + now_utc.minute

    if not (start_minutes <= now_minutes <= end_minutes):
        return

    yesterday = (now_utc - timedelta(days=1)).strftime("%Y%m%d")
    key = f"{host_key}:dawn:{yesterday}"

    if key in tracker._notified_dawn:
        return

    cutoff = now_utc - timedelta(days=7)
    tracker._notified_dawn = {
        k: v for k, v in tracker._notified_dawn.items() if v > cutoff
    }

    all_done = True
    for cam in station.cameras:
        try:
            url = station_url(config, tunnels, host_key, f"/api/chunks/{cam.code}/{yesterday}")
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            all_done = False
            continue

        morning_done = data.get("morning_done", False) if isinstance(data, dict) else False
        if not morning_done:
            all_done = False

    if all_done and station.cameras:
        tracker._notified_dawn[key] = now_utc
        tracker.report_info(
            f"{label} morning complete",
            f"All {len(station.cameras)} cameras processed for {yesterday}",
            host_key, label,
        )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _run_cycle(
    config: DashboardConfig,
    tunnels: TunnelManager,
    wcfg: WatchdogConfig,
    tracker: AlertTracker,
) -> None:
    for host_key in config.stations:
        try:
            check_station(host_key, config, tunnels, wcfg, tracker)
        except Exception:
            logger.exception("Unexpected error checking %s", host_key)

    notifications = tracker.drain_notifications()
    if notifications:
        try:
            sent = dispatch_notifications_batched(wcfg.discord_webhook_url, notifications)
        except Exception:
            logger.exception("Failed to dispatch notifications")
            sent = 0
    else:
        sent = 0

    logger.info(
        "Cycle complete: %d active alerts, %d issues across %d messages",
        tracker.active_count, len(notifications), sent,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="ROVIMEN Station Health Watchdog")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "dashboard_config.yaml"),
        help="Path to dashboard_config.yaml",
    )
    parser.add_argument(
        "--watchdog-config",
        default=str(Path(__file__).parent / "watchdog_config.yaml"),
        help="Path to watchdog_config.yaml",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(Path(args.config))
    wcfg = load_watchdog_config(Path(args.watchdog_config))

    if not wcfg.discord_webhook_url:
        logger.warning("discord_webhook_url is empty — alerts will only be logged")

    tunnels = TunnelManager(config)
    tunnels.start_all()
    tunnels.start_watchdog()

    tracker = AlertTracker(wcfg)

    shutdown = False

    def _handle_signal(signum: int, frame: Any) -> None:
        nonlocal shutdown
        logger.info("Received signal %d, shutting down", signum)
        shutdown = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "Watchdog started — polling %d stations every %ds",
        len(config.stations), wcfg.poll_interval_s,
    )

    while not shutdown:
        cycle_start = time.monotonic()
        try:
            _run_cycle(config, tunnels, wcfg, tracker)
        except Exception:
            logger.exception("Poll cycle failed")
        elapsed = time.monotonic() - cycle_start
        sleep_time = max(1, wcfg.poll_interval_s - elapsed)
        logger.info("Next poll in %.0fs", sleep_time)

        deadline = time.monotonic() + sleep_time
        while not shutdown and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    tunnels.shutdown()
    logger.info("Watchdog stopped")


if __name__ == "__main__":
    main()
