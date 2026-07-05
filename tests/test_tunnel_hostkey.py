"""Security test for SSH tunnel host-key pinning (H5).

``TunnelManager._establish_tunnel`` must build its ``ssh`` command with an
explicit ``UserKnownHostsFile`` (the file ``rovimen_dashboard`` populates at
startup), ``StrictHostKeyChecking=accept-new``, and ``BatchMode=yes`` so a
man-in-the-middle on the jump path can't be silently accepted and a
missing/changed key fails fast instead of hanging under systemd.

The test intercepts ``subprocess.Popen`` so no real ssh process is spawned;
it captures the argv the manager would have run and asserts the options.
"""

from __future__ import annotations

import subprocess

import pytest

import tunnels
from models import DashboardConfig, StationConfig


class _FakeProc:
    """Stand-in for a live ssh Popen object: poll() returns None (alive)."""

    returncode = None

    def poll(self):
        return None

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def tunnel_config() -> DashboardConfig:
    station = StationConfig(
        ip="100.64.0.2", label="Dragsina", ssh_user="alex",
        jump_hosts=["gmn0000"],
    )
    jump = StationConfig(ip="100.64.0.3", label="Berlin", ssh_user="gmn")
    return DashboardConfig(stations={"dragsina": station, "gmn0000": jump})


def _capture_ssh_cmd(monkeypatch, config) -> list[str]:
    """Run _establish_tunnel with Popen + sleep stubbed out and return the
    captured ssh argv."""
    captured: dict[str, list[str]] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(tunnels.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnels.time, "sleep", lambda *_a, **_k: None)
    # Pin the known_hosts path so the assertion is deterministic and doesn't
    # depend on the deployment env / ROVIMEN_KNOWN_HOSTS.
    monkeypatch.setattr(
        tunnels, "_known_hosts_path", lambda: "/opt/rovimen/known_hosts"
    )

    mgr = tunnels.TunnelManager(config)
    mgr._establish_tunnel("dragsina")
    return captured["cmd"]


class TestTunnelHostKeyPinning:
    def test_known_hosts_file_pinned(self, monkeypatch, tunnel_config) -> None:
        cmd = _capture_ssh_cmd(monkeypatch, tunnel_config)
        assert "UserKnownHostsFile=/opt/rovimen/known_hosts" in cmd

    def test_strict_host_key_checking_accept_new(
        self, monkeypatch, tunnel_config
    ) -> None:
        """accept-new (not 'yes') so first contact with a fresh jump host
        works, while any later key change is refused."""
        cmd = _capture_ssh_cmd(monkeypatch, tunnel_config)
        assert "StrictHostKeyChecking=accept-new" in cmd
        assert "StrictHostKeyChecking=no" not in cmd

    def test_batch_mode_enabled(self, monkeypatch, tunnel_config) -> None:
        """BatchMode=yes makes a missing/changed key fail fast instead of
        blocking on an interactive prompt the daemon cannot answer."""
        cmd = _capture_ssh_cmd(monkeypatch, tunnel_config)
        assert "BatchMode=yes" in cmd

    def test_options_are_o_prefixed(self, monkeypatch, tunnel_config) -> None:
        """Each host-key option must be a value to a preceding ``-o`` so ssh
        actually parses it."""
        cmd = _capture_ssh_cmd(monkeypatch, tunnel_config)
        for opt in (
            "UserKnownHostsFile=/opt/rovimen/known_hosts",
            "StrictHostKeyChecking=accept-new",
            "BatchMode=yes",
        ):
            idx = cmd.index(opt)
            assert cmd[idx - 1] == "-o", f"{opt} is not preceded by -o"

    def test_tunnel_registered_after_establish(
        self, monkeypatch, tunnel_config
    ) -> None:
        """Sanity: with the faked (alive) proc the tunnel is recorded in the
        manager's table, proving the new host-key options didn't break the
        establish path. (We assert on ``_tunnels`` rather than
        ``get_api_base`` because the latter does a real socket connect to the
        local forward port, which doesn't exist for a faked ssh proc.)"""
        monkeypatch.setattr(
            tunnels.subprocess, "Popen", lambda cmd, **kw: _FakeProc()
        )
        monkeypatch.setattr(tunnels.time, "sleep", lambda *_a, **_k: None)
        monkeypatch.setattr(
            tunnels, "_known_hosts_path", lambda: "/opt/rovimen/known_hosts"
        )
        mgr = tunnels.TunnelManager(tunnel_config)
        mgr._establish_tunnel("dragsina")
        assert "dragsina" in mgr._tunnels
        assert mgr._tunnels["dragsina"]["jump_host"] == "gmn0000"
        assert mgr._tunnels["dragsina"]["local_port"] > 0


def test_known_hosts_path_resolves_to_dashboard_constant() -> None:
    """``_known_hosts_path`` must return the same path rovimen_dashboard
    populates, so the tunnel pins against the file that actually gets keys."""
    from rovimen_dashboard import KNOWN_HOSTS_PATH

    assert tunnels._known_hosts_path() == str(KNOWN_HOSTS_PATH)


# ── Host-key fingerprint PINNING at scan time (H5) ────────────────────────
#
# ``rovimen_dashboard._ensure_known_hosts`` seeds the known_hosts file from
# ssh-keyscan. When a station pins ``ssh_host_key_fingerprints`` the scanned
# key must match a pin or be refused (no accept-new fall-back). An unpinned
# station keeps accept-new (TOFU) but must log a WARNING. These tests stub the
# three subprocess calls the function makes (ssh-keygen -F to check presence,
# ssh-keyscan to fetch, ssh-keygen -lf - to fingerprint).

_GOOD_FP = "SHA256:GOODgoodGOODgoodGOODgoodGOODgoodGOODgoodabc"
_BAD_FP = "SHA256:EVILevilEVILevilEVILevilEVILevilEVILevil999"
_SCAN_LINE = "|1|hashed==|hashed== ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAexample"


def _make_run_stub(scan_fp: str):
    """Return a fake subprocess.run for _ensure_known_hosts.

    - ``ssh-keygen -F <ip> -f <file>`` -> returncode 1 (IP absent, so scanned)
    - ``ssh-keyscan ...``              -> one host-key line
    - ``ssh-keygen -lf -``             -> a line containing ``scan_fp``
    """
    class _R:
        def __init__(self, returncode=0, stdout=""):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        prog = cmd[0]
        if prog == "ssh-keygen" and "-F" in cmd:
            return _R(returncode=1, stdout="")  # not present -> scan it
        if prog == "ssh-keyscan":
            return _R(returncode=0, stdout=_SCAN_LINE + "\n")
        if prog == "ssh-keygen" and "-lf" in cmd:
            return _R(returncode=0, stdout=f"256 {scan_fp} host (ED25519)\n")
        return _R(returncode=0, stdout="")

    return fake_run


def _run_ensure(monkeypatch, tmp_path, pins: list[str], scan_fp: str):
    """Run _ensure_known_hosts against a single pinned/unpinned station and
    return (known_hosts_text, dashboard_module)."""
    import rovimen_dashboard as rd
    from models import DashboardConfig, StationConfig

    kh = tmp_path / "known_hosts"
    monkeypatch.setattr(rd, "KNOWN_HOSTS_PATH", kh)
    monkeypatch.setattr(rd.subprocess, "run", _make_run_stub(scan_fp))

    station = StationConfig(
        ip="100.64.0.2", label="Dragsina", ssh_user="alex",
        ssh_host_key_fingerprints=pins,
    )
    cfg = DashboardConfig(stations={"dragsina": station})
    rd._ensure_known_hosts(cfg)
    text = kh.read_text() if kh.exists() else ""
    return text, rd


class TestEnsureKnownHostsPinning:
    def test_pin_match_is_added(self, monkeypatch, tmp_path) -> None:
        text, _ = _run_ensure(monkeypatch, tmp_path, [_GOOD_FP], _GOOD_FP)
        assert _SCAN_LINE in text

    def test_pin_mismatch_is_refused(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        with caplog.at_level("ERROR"):
            text, _ = _run_ensure(monkeypatch, tmp_path, [_GOOD_FP], _BAD_FP)
        # Scanned key does NOT match the pin -> must not be written.
        assert _SCAN_LINE not in text
        assert any(
            "PIN MISMATCH" in r.getMessage() for r in caplog.records
        ), "a mismatch must be logged at ERROR"

    def test_unpinned_warns_but_accepts(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        with caplog.at_level("WARNING"):
            text, _ = _run_ensure(monkeypatch, tmp_path, [], _GOOD_FP)
        # No pin configured -> accept-new/TOFU preserved (backward compatible).
        assert _SCAN_LINE in text
        assert any(
            "UNPINNED" in r.getMessage() for r in caplog.records
        ), "an unpinned host must be logged at WARNING"
