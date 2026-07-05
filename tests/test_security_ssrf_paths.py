"""Security tests for the SSRF deny-list and compilation path containment.

Covers two hardening fixes:

* ``admin_config.validate_autodetect_target`` — the ``/api/admin/autodetect``
  SSRF guard. Private / loopback / link-local / multicast / reserved targets
  and unsafe ports are rejected; the Tailscale CGNAT range (the intended
  station target) is allowed by default and rejectable on demand.
* ``/api/compilation/<id>/download`` path containment — a manifest whose
  ``output_path`` resolves outside ``COMPILATIONS_OUT_PATH`` must be refused
  (403) before ``send_file`` ever touches the filesystem.

The autodetect tests exercise the real imported validator. The download
containment test replicates the route's inline check faithfully (the same
pattern ``tests/test_dashboard_security.py`` uses for inline route logic) and
pins it against the real ``COMPILATIONS_OUT_PATH`` constant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from admin_config import validate_autodetect_target
from cache_store import COMPILATIONS_OUT_PATH


# ── SSRF deny-list: /api/admin/autodetect ────────────────────────────────


class TestAutodetectSSRFDenyList:
    """``validate_autodetect_target`` returns None to allow, or a reason str
    to reject. The deny-list must block every special-use range plus unsafe
    ports while preserving the intended Tailscale-station use."""

    # --- rejected: special-use address ranges ---

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",          # loopback
            "127.10.20.30",       # loopback (whole /8)
            "10.0.0.1",           # RFC1918 private
            "172.16.5.4",         # RFC1918 private
            "192.168.1.50",       # RFC1918 private
            "169.254.169.254",    # link-local / cloud metadata
            "169.254.0.1",        # link-local
            "224.0.0.1",          # multicast
            "239.255.255.250",    # multicast (SSDP)
            "240.0.0.1",          # reserved
            "0.0.0.0",            # unspecified
            "255.255.255.255",    # reserved/broadcast
        ],
    )
    def test_special_use_ranges_rejected(self, ip: str) -> None:
        assert validate_autodetect_target(ip, 7779) == "IP address not allowed"

    # --- rejected: malformed input ---

    @pytest.mark.parametrize(
        "ip",
        ["", "not-an-ip", "999.1.1.1", "1.2.3", "1.2.3.4.5", "12.34.56.789"],
    )
    def test_malformed_ip_rejected(self, ip: str) -> None:
        assert validate_autodetect_target(ip, 7779) == "Invalid IP address format"

    def test_ipv6_rejected(self) -> None:
        """The fleet is IPv4-only; v6 (incl. ::1 and metadata-equivalents)
        is refused outright rather than partially reasoned about."""
        assert validate_autodetect_target("::1", 7779) == "IP address not allowed"
        assert validate_autodetect_target(
            "fd00::1", 7779
        ) == "IP address not allowed"

    # --- rejected: unsafe ports ---

    @pytest.mark.parametrize("port", [22, 80, 443])
    def test_sensitive_ports_rejected(self, port: int) -> None:
        """A globally-routable host is otherwise fine, but the common
        SSH / HTTP / HTTPS service ports are denied so the endpoint can't be
        a port-scanner for those services."""
        assert validate_autodetect_target("8.8.8.8", port) == "Port not allowed"

    @pytest.mark.parametrize("port", [0, 1, 22, 80, 443, 1023, 65536, 70000, -1])
    def test_out_of_range_or_privileged_ports_rejected(self, port: int) -> None:
        reason = validate_autodetect_target("8.8.8.8", port)
        assert reason in {"Port not allowed", "Invalid port"}

    @pytest.mark.parametrize("port", ["abc", None, "", 12.5j])
    def test_non_integer_port_rejected(self, port) -> None:
        assert validate_autodetect_target("8.8.8.8", port) == "Invalid port"

    # --- allowed: the intended targets ---

    def test_tailscale_cgnat_allowed_by_default(self) -> None:
        """The whole point of autodetect is reading a new station's
        /api/settings over the operator's tailnet. CGNAT 100.64.0.0/10 must
        pass with the default station API port."""
        assert validate_autodetect_target("100.64.0.3", 7779) is None
        assert validate_autodetect_target("100.64.0.1", 7779) is None
        assert validate_autodetect_target("100.64.0.4", 7779) is None

    def test_public_host_allowed(self) -> None:
        """An ordinary globally-routable host on a safe port is allowed.

        (Note: RFC 5737 documentation ranges like 203.0.113.0/24 are
        classified ``is_private`` by stdlib ``ipaddress`` and are therefore
        *correctly* rejected — use real public IPs here.)"""
        assert validate_autodetect_target("8.8.8.8", 7779) is None
        assert validate_autodetect_target("9.9.9.9", 8080) is None

    def test_default_station_api_port_is_in_safe_range(self) -> None:
        """7779 (the station API port) must not be accidentally denied."""
        assert validate_autodetect_target("100.64.0.3", 7779) is None

    # --- the opt-out flag tightens to public-only ---

    def test_tailscale_rejected_when_flag_off(self) -> None:
        """With allow_tailscale=False the CGNAT range is denied too, leaving
        only ordinary public addresses."""
        assert validate_autodetect_target(
            "100.64.0.3", 7779, allow_tailscale=False
        ) == "IP address not allowed"
        # ...but a genuinely public host still passes in that stricter mode.
        assert validate_autodetect_target(
            "8.8.8.8", 7779, allow_tailscale=False
        ) is None

    def test_just_outside_cgnat_is_public(self) -> None:
        """Boundary: 100.63.255.255 and 100.128.0.0 sit just outside
        100.64.0.0/10 and are ordinary public space — allowed regardless of
        the flag, and not mistaken for the tailnet."""
        assert validate_autodetect_target("100.63.255.255", 7779) is None
        assert validate_autodetect_target(
            "100.128.0.0", 7779, allow_tailscale=False
        ) is None


# ── Compilation download path containment ────────────────────────────────


def _download_allowed(output_path: str, root: Path) -> bool:
    """Faithful replica of the containment decision in
    ``routes/compilation.py::api_compilation_download``. Returns True when the
    manifest's output_path resolves inside ``root`` (download proceeds), False
    when it escapes (route aborts 403)."""
    out = Path(output_path)
    try:
        out_resolved = out.resolve()
        out_root = root.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        return out_resolved.is_relative_to(out_root)
    except ValueError:
        return False


class TestCompilationDownloadContainment:
    """A tampered manifest ``output_path`` pointing outside the compilations
    directory must be refused; a legitimate in-root path must pass."""

    def test_in_root_path_allowed(self) -> None:
        good = str(COMPILATIONS_OUT_PATH / "abc123.mp4")
        assert _download_allowed(good, COMPILATIONS_OUT_PATH) is True

    @pytest.mark.parametrize(
        "tampered",
        [
            "/etc/passwd",
            "/opt/rovimen/api_keys.yaml",
            "/opt/rovimen/users.yaml",
            "/root/.ssh/id_rsa",
            "/opt/rovimen/known_hosts",
        ],
    )
    def test_absolute_escape_rejected(self, tampered: str) -> None:
        assert _download_allowed(tampered, COMPILATIONS_OUT_PATH) is False

    def test_dotdot_traversal_rejected(self) -> None:
        """A relative ../ climb that resolves out of the root is refused."""
        evil = str(COMPILATIONS_OUT_PATH / ".." / ".." / "etc" / "passwd")
        assert _download_allowed(evil, COMPILATIONS_OUT_PATH) is False

    def test_sibling_prefix_not_treated_as_contained(self) -> None:
        """A sibling dir that merely shares the root's string prefix
        (``/opt/rovimen/compilations-evil``) must not count as contained —
        this is why the check uses path semantics, not ``startswith``."""
        sibling = str(COMPILATIONS_OUT_PATH.parent / "compilations-evil" / "x.mp4")
        assert _download_allowed(sibling, COMPILATIONS_OUT_PATH) is False

    def test_root_itself_against_tmp(self, tmp_path: Path) -> None:
        """Sanity check against an arbitrary root: a file inside passes, a
        file outside fails — independent of the production constant."""
        inside = str(tmp_path / "comp" / "v.mp4")
        (tmp_path / "comp").mkdir()
        outside = str(tmp_path.parent / "v.mp4")
        assert _download_allowed(inside, tmp_path / "comp") is True
        assert _download_allowed(outside, tmp_path / "comp") is False


# ── H6: autodetect attempts are audit-logged ─────────────────────────────
#
# The CGNAT allowance in ``validate_autodetect_target`` is an accepted
# tradeoff; the compensating control is that the route audit-logs every
# attempt (allowed or denied) via ``security.audit_request``. We register the
# real route on a bare Flask app with the admin gate and network stubbed out,
# then assert the audit call fires with the requested target.


def _make_autodetect_app(monkeypatch, audited: list[dict]):
    """Register the real autodetect route with require_admin bypassed,
    security.audit_request captured, and the outbound HTTP session stubbed."""
    import admin_config
    import rovimen_dashboard
    import security
    from flask import Flask
    from models import DashboardConfig

    # Bypass the admin gate: the decorator is a passthrough in the test.
    monkeypatch.setattr(
        rovimen_dashboard, "require_admin", lambda f: f, raising=False
    )

    def fake_audit_request(event, **fields):
        audited.append({"event": event, **fields})

    monkeypatch.setattr(security, "audit_request", fake_audit_request)

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"stations": {}}

    class _FakeSession:
        def get(self, url, timeout=None):
            return _FakeResp()

    app = Flask(__name__)
    admin_config.register_admin_network_routes(
        app,
        DashboardConfig(stations={}),
        save_config=lambda: None,
        config_to_dict=lambda: {},
        require_station=lambda h: None,
        push_rotate_to_station=lambda *a, **k: {},
        session_for_url=lambda url: _FakeSession(),
    )
    return app


class TestAutodetectAudit:
    def test_allowed_target_is_audited(self, monkeypatch) -> None:
        audited: list[dict] = []
        app = _make_autodetect_app(monkeypatch, audited)
        client = app.test_client()
        resp = client.post(
            "/api/admin/autodetect",
            json={"ip": "100.64.0.3", "port": 7779},
        )
        assert resp.status_code == 200
        assert len(audited) == 1
        rec = audited[0]
        assert rec["event"] == "admin.autodetect"
        assert rec["target_ip"] == "100.64.0.3"
        assert rec["target_port"] == 7779
        assert rec["allowed"] is True

    def test_denied_target_is_audited(self, monkeypatch) -> None:
        audited: list[dict] = []
        app = _make_autodetect_app(monkeypatch, audited)
        client = app.test_client()
        resp = client.post(
            "/api/admin/autodetect",
            json={"ip": "169.254.169.254", "port": 80},
        )
        assert resp.status_code == 400
        assert len(audited) == 1
        rec = audited[0]
        assert rec["event"] == "admin.autodetect"
        assert rec["target_ip"] == "169.254.169.254"
        assert rec["allowed"] is False
        assert rec["deny_reason"]
