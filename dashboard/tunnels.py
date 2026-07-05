"""SSH tunnel management for cross-tailnet station access."""

import logging
import socket
import subprocess
import threading
import time
from typing import Any

from models import DashboardConfig

logger = logging.getLogger(__name__)


def _evict(base_url: str) -> None:
    """Evict a pooled HTTP session after a tunnel port changes.

    Lazy import to avoid circular dependency with rovimen_dashboard.
    """
    from station_client import _evict_station_session

    _evict_station_session(base_url)


def _known_hosts_path() -> str:
    """Path to the pinned known_hosts file used for tunnel host-key checking.

    Lazy import (like ``_evict``) to avoid a circular dependency with
    ``rovimen_dashboard``, which is the single source of truth for the path
    and the module that populates the file at startup via
    ``_ensure_known_hosts``.
    """
    from rovimen_dashboard import KNOWN_HOSTS_PATH

    return str(KNOWN_HOSTS_PATH)


class TunnelManager:
    """Manages SSH tunnels for stations that need jump hosts."""

    # Exponential backoff cap: when a tunnel keeps dying we stop hammering
    # the jump host. 8 min is long enough that gmn0006-style upstream
    # blackouts don't fill the journal with reconnect spam, short enough
    # that a real recovery is picked up within one operational window.
    _BACKOFF_CAP_S = 480.0
    # A tunnel must stay alive at least this long before we count it as a
    # "good" connection that resets the failure streak. Below this it's
    # treated as an instant-death.
    _TUNNEL_HEALTHY_S = 60.0

    def __init__(self, config: DashboardConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        # host_key -> {"local_port": int, "process": Popen, "jump_host": str,
        #              "established_at": float}
        self._tunnels: dict[str, dict[str, Any]] = {}
        # Per-host pending-reconnect flags + a shared wakeup Event the
        # watchdog blocks on. A request-thread cache miss flips the flag
        # and pings the wakeup so the watchdog reconnects immediately
        # instead of waiting up to 30 s for the next periodic sweep.
        # Reconnects are NEVER run inline on the request thread (B1 fix)
        # — the 2 s sleep in _establish_tunnel used to block every miss.
        self._reconnect_pending: dict[str, bool] = {}
        # Exponential-backoff state. _fail_streak counts consecutive
        # tunnels that died within _TUNNEL_HEALTHY_S; _backoff_until is
        # monotonic time before which the watchdog skips reconnect
        # attempts entirely. Pre-existing churn (every 30 s, indefinitely)
        # filled the journal and wedged the jump host on durable
        # upstream-blackout cases (gmn0006 via gmn0002).
        self._fail_streak: dict[str, int] = {}
        self._backoff_until: dict[str, float] = {}
        self._wakeup = threading.Event()
        for host_key, station in config.stations.items():
            if station.jump_hosts:
                self._reconnect_pending[host_key] = False
                self._fail_streak[host_key] = 0
                self._backoff_until[host_key] = 0.0

    def needs_tunnel(self, host_key: str) -> bool:
        station = self._config.stations.get(host_key)
        return station is not None and len(station.jump_hosts) > 0

    def get_api_base(self, host_key: str) -> str | None:
        """Return the base URL to reach a station's API, or None when no
        tunnel is live for a tunnel-only station.

        This is on the request hot path; it MUST be non-blocking. If the
        tunnel is down we kick the watchdog (via _reconnect_events) and
        return None so the caller fails fast — Romanian stations behind
        a jump host don't have a routable direct IP, so retrying the
        upstream IP would just stall TCP connect for the request timeout.
        """
        with self._lock:
            tunnel = self._tunnels.get(host_key)
            if tunnel and self._is_alive(tunnel):
                return f"http://127.0.0.1:{tunnel['local_port']}"

        if self.needs_tunnel(host_key):
            with self._lock:
                if host_key in self._reconnect_pending:
                    self._reconnect_pending[host_key] = True
            self._wakeup.set()
            return None

        station = self._config.stations[host_key]
        return f"http://{station.ip}:{self._config.station_api_port}"

    def start_all(self) -> None:
        for host_key in self._config.stations:
            if self.needs_tunnel(host_key):
                self._establish_tunnel(host_key)

    def start_watchdog(self) -> None:
        def watchdog() -> None:
            # Sole owner of _establish_tunnel — no request thread should
            # ever call it directly (B1 fix). Periodic sweep every 30 s,
            # but the _wakeup Event lets get_api_base prod us instantly.
            while True:
                self._wakeup.wait(timeout=30)
                self._wakeup.clear()
                now_mono = time.monotonic()
                for host_key in self._config.stations:
                    if not self.needs_tunnel(host_key):
                        continue
                    with self._lock:
                        tunnel = self._tunnels.get(host_key)
                        pending = self._reconnect_pending.get(host_key, False)
                        backoff_until = self._backoff_until.get(host_key, 0.0)
                    if tunnel and self._is_alive(tunnel):
                        if pending:
                            with self._lock:
                                self._reconnect_pending[host_key] = False
                        # A tunnel that's lived past _TUNNEL_HEALTHY_S is
                        # the signal we've recovered from any prior
                        # flapping; clear the backoff streak so the next
                        # eventual drop is treated fresh.
                        age = now_mono - tunnel.get("established_at", 0.0)
                        if age >= self._TUNNEL_HEALTHY_S:
                            self._note_success(host_key)
                        continue
                    # Skip reconnect if we're inside the backoff window —
                    # the upstream is durably broken, more attempts won't
                    # fix it and just spam ssh against the jump host.
                    if now_mono < backoff_until:
                        continue
                    logger.warning("Tunnel down for %s, reconnecting...", host_key)
                    self._establish_tunnel(host_key)
                    with self._lock:
                        self._reconnect_pending[host_key] = False

        threading.Thread(target=watchdog, daemon=True).start()

    def _note_failure(self, host_key: str) -> None:
        """Record a tunnel failure: instant death after spawn, or a
        process that died within _TUNNEL_HEALTHY_S. Doubles the backoff
        each consecutive failure (30 s, 60 s, 120 s, …, capped at
        _BACKOFF_CAP_S).
        """
        with self._lock:
            streak = self._fail_streak.get(host_key, 0) + 1
            self._fail_streak[host_key] = streak
            delay = min(self._BACKOFF_CAP_S, 30.0 * (2 ** (streak - 1)))
            self._backoff_until[host_key] = time.monotonic() + delay
        logger.warning(
            "Tunnel for %s failed (streak=%d), backing off %.0fs",
            host_key, streak, delay,
        )

    def _note_success(self, host_key: str) -> None:
        with self._lock:
            self._fail_streak[host_key] = 0
            self._backoff_until[host_key] = 0.0

    def _is_alive(self, tunnel: dict[str, Any]) -> bool:
        proc = tunnel.get("process")
        if proc is None or proc.poll() is not None:
            return False
        try:
            s = socket.create_connection(("127.0.0.1", tunnel["local_port"]), timeout=2)
            s.close()
            return True
        except OSError:
            return False

    def _tunnel_just_died(self, host_key: str, tunnel: dict[str, Any]) -> bool:
        """True iff this tunnel exists but is now dead AND it died within
        _TUNNEL_HEALTHY_S of being established. Used to decide whether the
        latest reconnect should count toward the backoff streak — a tunnel
        that ran fine for hours before going down is not the same failure
        mode as one that ssh refuses to keep open.
        """
        established_at = tunnel.get("established_at", 0.0)
        if self._is_alive(tunnel):
            return False
        return (time.monotonic() - established_at) < self._TUNNEL_HEALTHY_S

    def _establish_tunnel(self, host_key: str) -> None:
        station = self._config.stations[host_key]
        api_port = self._config.station_api_port

        # Kill existing tunnel
        with self._lock:
            old = self._tunnels.pop(host_key, None)
        # If the previous tunnel was short-lived, count it toward the
        # failure streak before kicking off a new one. (A long-lived
        # tunnel that just went down is not a failure — the upstream
        # was working until recently; one fresh attempt is fine.)
        if old and self._tunnel_just_died(host_key, old):
            self._note_failure(host_key)
        if old and old.get("process"):
            try:
                old["process"].kill()
                old["process"].wait(timeout=5)
            except Exception:
                pass
            old_port = old.get("local_port")
            if old_port is not None:
                _evict(f"http://127.0.0.1:{old_port}")

        # Try each jump host in order
        for jump_key in station.jump_hosts:
            jump = self._config.stations.get(jump_key)
            if jump is None:
                logger.error("Jump host '%s' not found in config", jump_key)
                continue

            local_port = self._find_free_port()
            cmd = [
                "ssh", "-N",
                "-L", f"{local_port}:{station.ip}:{api_port}",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes",
                # Host-key pinning (H5). Without these the tunnel ssh ran with
                # the user's default UserKnownHostsFile and interactive
                # StrictHostKeyChecking, so a man-in-the-middle on the jump
                # path could have been silently accepted (or the connection
                # could block forever on a host-key prompt under systemd).
                # We pin against the file rovimen_dashboard._ensure_known_hosts
                # populates at startup. ``accept-new`` records a key on genuine
                # first contact (so a freshly-added jump host doesn't need a
                # manual keyscan) but, once pinned, refuses any subsequent key
                # change. ``BatchMode=yes`` makes a missing/changed key fail
                # fast instead of hanging on a prompt the daemon can't answer.
                "-o", f"UserKnownHostsFile={_known_hosts_path()}",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "BatchMode=yes",
                f"{jump.ssh_user}@{jump.ip}",
            ]
            logger.info(
                "Opening tunnel for %s via %s (%s) on local port %d",
                host_key, jump_key, jump.ip, local_port,
            )
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                # Wait briefly for tunnel to establish
                time.sleep(2)
                if proc.poll() is not None:
                    logger.warning(
                        "Tunnel via %s failed (exit %s)",
                        jump_key, proc.returncode,
                    )
                    continue

                with self._lock:
                    self._tunnels[host_key] = {
                        "local_port": local_port,
                        "process": proc,
                        "jump_host": jump_key,
                        "established_at": time.monotonic(),
                    }
                logger.info(
                    "Tunnel established for %s via %s -> 127.0.0.1:%d",
                    host_key, jump_key, local_port,
                )
                # Streak is only cleared once the watchdog observes this
                # tunnel surviving past _TUNNEL_HEALTHY_S — see the
                # watchdog loop. Resetting it here would let a flapping
                # ssh process spawn-and-die forever without backoff.
                return
            except Exception:
                logger.exception("Failed to start tunnel via %s", jump_key)

        # All jump hosts failed — count as an immediate failure so the
        # watchdog applies the same backoff schedule and doesn't loop.
        self._note_failure(host_key)
        logger.error("All jump hosts exhausted for %s", host_key)

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def shutdown(self) -> None:
        with self._lock:
            for tunnel in self._tunnels.values():
                proc = tunnel.get("process")
                if proc:
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        pass
            self._tunnels.clear()


class _TunnelDown(Exception):
    """Raised internally when a tunnel-only station has no live tunnel.

    Caller paths convert this to the same "offline" response they would
    produce on a network error — the dashboard never blocks waiting for
    the watchdog to reconnect the tunnel.
    """
