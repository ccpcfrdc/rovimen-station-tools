"""Admin routes — users CRUD, activity-log health, network-config edits.

Extracted from rovimen_dashboard.py as part of the A-1 refactor
(docs/audit-2026-05-24.md). All routes preserved verbatim — same URLs,
same behaviour, same decorators. Wired in from ``create_app()`` via the
two ``register_*_routes`` entry points below.

The user-CRUD slice only needs module-level helpers from
``rovimen_dashboard`` (``require_admin``, ``_load_users``,
``_update_users``), so its register function takes ``app`` only.

The network-config slice needs the per-app ``config`` + ``tunnels``
instances and three closure-bound helpers from ``create_app`` (atomic
``_save_config``, the YAML serialiser, and the rotate-push), so its
register function takes them as parameters rather than reaching back
into the parent module.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from flask import Flask, abort, jsonify, request
from werkzeug.security import generate_password_hash

import security

from models import CameraConfig, DashboardConfig, StationConfig, UserConfig


# ── SSRF deny-list for /api/admin/autodetect ──────────────────────────────
#
# ``/api/admin/autodetect`` fetches ``http://{ip}:{port}/api/settings`` with an
# admin-supplied IP. Even behind ``@require_admin`` this is an SSRF primitive: a
# hijacked admin session (or CSRF) could pivot the VPS into probing hosts the
# operator never intended — cloud metadata (169.254.169.254), loopback-only
# admin services, or arbitrary RFC1918 hosts on the VPS's own network segment.
#
# The previous guard only blocked 127/0.0.0.0/169.254 via string-octet math and
# left every other range (10/172.16/192.168, multicast, reserved) reachable.
# This validator classifies the IP with stdlib ``ipaddress`` and denies the full
# set of special-use ranges, and constrains ``port`` to the unprivileged range
# with 22/80/443 denied so the endpoint can't be turned into a scanner for SSH /
# web admin panels.
#
# TRADEOFF — the Tailscale CGNAT range (100.64.0.0/10) is *deliberately allowed*.
# The Add-Station UI's IP field is labelled "Enter a Tailscale IP first" and the
# whole point of autodetect is to read a brand-new station's ``/api/settings``
# over the operator's own tailnet before it's committed to ``dashboard_config``.
# Those 100.x hosts are the operator's own machines, reachable only by
# authenticated tailnet members — fetching them is the feature, not the attack.
# Blanket-blocking CGNAT (as a naive "deny all RFC6598" rule would) breaks the
# documented workflow while closing nothing that matters; the dangerous targets
# (metadata, loopback, RFC1918, link-local) are the ones denied above. The
# allowance is an explicit, named flag (``allow_tailscale``) so it can be flipped
# off and unit-tested in isolation.
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_DENIED_PORTS = frozenset({22, 80, 443})
_MIN_PORT = 1024
_MAX_PORT = 65535


def validate_autodetect_target(
    ip: str, port: Any, *, allow_tailscale: bool = True
) -> str | None:
    """Validate an autodetect ``(ip, port)`` against the SSRF deny-list.

    Returns ``None`` when the target is allowed, or a human-readable error
    string when it must be rejected. Kept side-effect free and importable so
    the deny-list can be unit-tested without standing up the Flask app.

    ``allow_tailscale`` keeps the intended station-tailnet use working: when
    ``True`` (the default for the autodetect route) an address in the Tailscale
    CGNAT range is permitted. When ``False`` the CGNAT range is rejected too and
    only ordinary public addresses pass.

    H6 — allowing the Tailscale CGNAT range (100.64.0.0/10) is a DELIBERATE,
    owner-accepted tradeoff, not an oversight: autodetect's entire purpose is
    to read a brand-new station's ``/api/settings`` over the operator's own
    tailnet before it's committed to ``dashboard_config``, and those 100.x
    hosts are the operator's own authenticated tailnet peers. The residual
    risk (an admin session could probe arbitrary tailnet hosts/ports in the
    unprivileged range) is accepted; the compensating control is that every
    autodetect attempt is audit-logged in the route handler (see
    ``api_admin_autodetect``). The dangerous ranges — metadata, loopback,
    RFC1918, link-local, multicast, reserved — remain denied above.
    """
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return "Invalid IP address format"
    if isinstance(addr, ipaddress.IPv6Address):
        # The station fleet is IPv4-only; refuse v6 rather than reason about
        # its (larger) set of special-use ranges.
        return "IP address not allowed"

    in_tailnet = addr in _TAILSCALE_CGNAT
    if in_tailnet:
        # The CGNAT range is the intended target. Permit it when allowed,
        # reject it explicitly otherwise. We check membership directly rather
        # than relying on ``is_private`` because the stdlib classification of
        # 100.64.0.0/10 differs across Python versions (private on 3.12-,
        # un-classified on 3.13+); an explicit check is version-stable.
        if not allow_tailscale:
            return "IP address not allowed"
    elif (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return "IP address not allowed"

    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return "Invalid port"
    if port_i < _MIN_PORT or port_i > _MAX_PORT or port_i in _DENIED_PORTS:
        return "Port not allowed"
    return None


def register_admin_user_routes(app: Flask) -> None:
    """Attach user-CRUD + activity-log health endpoints to ``app``.

    The helpers ``require_admin`` / ``_load_users`` / ``_update_users``
    are imported lazily from ``rovimen_dashboard`` to keep this module
    free of any cyclic import at parse time.
    """
    # Lazy import: this module is loaded *from* rovimen_dashboard, so a
    # top-level ``import rovimen_dashboard`` here would deadlock the
    # interpreter. By the time register_*_routes() runs, the parent
    # module's globals are fully populated.
    from rovimen_dashboard import (
        _load_users,
        _update_users,
        require_admin,
    )

    @app.route("/api/admin/users")
    @require_admin
    def api_admin_users_list():
        users = _load_users()
        result = []
        for username, u in users.items():
            result.append({
                "username": username,
                "display_name": u.display_name,
                "role": u.role,
                "stations": u.stations,
                "expires_at": u.expires_at,
                "has_magic_link": bool(u.magic_token),
            })
        return jsonify(result)

    @app.route("/api/admin/users/activity")
    @require_admin
    def api_admin_users_activity():
        """Per-user last-login + active-time summary for the Users table.

        Derived from the audit + activity logs (read-only). 90-day window
        is plenty to surface a "last login" for an account that signs in
        rarely without scanning the whole log history.
        """
        import usage_stats
        return jsonify(usage_stats.compute_user_activity(days=90))

    @app.route("/api/admin/usage-stats")
    @require_admin
    def api_admin_usage_stats():
        """Site-wide feature-usage report over a trailing day window."""
        import usage_stats
        try:
            days = int(request.args.get("days", "7"))
        except (TypeError, ValueError):
            days = 7
        return jsonify(usage_stats.compute_usage(days=days))

    @app.route("/api/admin/users", methods=["POST"])
    @require_admin
    def api_admin_users_create():
        body = request.get_json(force=True) or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        role     = body.get("role") or "host"
        if not username or not password:
            return jsonify({"error": "username and password required"}), 400
        # Match the floor enforced by /set-password — otherwise admin-side
        # user creation would silently bypass the policy that operators
        # going through self-serve reset are held to.
        if len(password) < 14:
            return jsonify({"error":
                            "password must be at least 14 characters"}), 400
        if role not in ("admin", "host", "visitor", "press"):
            return jsonify({"error": "invalid role"}), 400

        expires_at = body.get("expires_at") or None

        class _Conflict(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username in users:
                raise _Conflict()
            users[username] = UserConfig(
                display_name=body.get("display_name") or "",
                password_hash=generate_password_hash(password),
                role=role,
                stations=body.get("stations") or [],
                expires_at=expires_at,
            )

        try:
            _update_users(_mutate)
        except _Conflict:
            return jsonify({"error": "user already exists"}), 409
        return jsonify({"ok": True})

    @app.route("/api/admin/users/<username>", methods=["PATCH"])
    @require_admin
    def api_admin_users_update(username: str):
        body = request.get_json(force=True) or {}

        # Validate the body once up front — anything we'd otherwise
        # discover deep inside the mutator (and then have to bubble out
        # via exceptions) becomes a plain 400 here.
        if "role" in body and body["role"] not in ("admin", "host", "visitor", "press"):
            return jsonify({"error": "invalid role"}), 400
        new_pw = body.get("password") or ""
        if new_pw and len(new_pw) < 14:
            # Same floor as /set-password and user-create; a PATCH must
            # not be a silent escape hatch around the policy.
            return jsonify({"error":
                            "password must be at least 14 characters"}), 400

        class _NotFound(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NotFound()
            u = users[username]
            # Track whether any privilege-relevant field actually changed so we
            # only bump the session epoch (which force-revalidates live
            # sessions) when role/stations/password/expiry move. A pure
            # display-name edit must not log the user out.
            privilege_changed = False
            if "display_name" in body:
                u.display_name = body["display_name"]
            if "role" in body:
                # A role change invalidates any outstanding magic-login link:
                # the link is a bearer credential issued against the old
                # privilege level, so a host→admin promotion (or any change)
                # must not let a stale token redeem at the new level.
                if body["role"] != u.role:
                    u.magic_token = None
                    privilege_changed = True
                u.role = body["role"]
            if "stations" in body:
                if body["stations"] != u.stations:
                    privilege_changed = True
                u.stations = body["stations"]
            if "expires_at" in body:
                new_exp = body["expires_at"] or None
                if new_exp != u.expires_at:
                    privilege_changed = True
                u.expires_at = new_exp
            if new_pw:
                u.password_hash = generate_password_hash(new_pw)
                privilege_changed = True
            if privilege_changed:
                # Bump the session epoch so the before_request revalidation
                # gate re-reads this account's live role/stations into any
                # outstanding session on its next request — a demotion/de-scope
                # takes effect immediately, not at the user's next
                # /api/auth/status poll (audit H3).
                u.session_epoch = (u.session_epoch or 0) + 1

        try:
            _update_users(_mutate)
        except _NotFound:
            abort(404)
        return jsonify({"ok": True})

    @app.route("/api/admin/users/<username>/reset-password", methods=["POST"])
    @require_admin
    def api_admin_users_reset_password(username: str):
        # Generate a high-entropy one-time token. The plaintext is shown to
        # the issuing admin once (it has to be delivered out-of-band to the
        # user). Only the hash is persisted, so a leak of users.yaml no
        # longer leaks an active account-takeover code — matching how we
        # treat password hashes elsewhere. Verification at /login goes
        # through check_password_hash (constant-time inside).
        token  = secrets.token_urlsafe(32)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

        class _NotFound(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NotFound()
            u = users[username]
            u.reset_token        = generate_password_hash(token)
            u.reset_token_expiry = expiry
            # A password reset is a privilege-relevant change: bump the session
            # epoch so any existing session for this account is force-
            # revalidated against users.yaml on its next authenticated request
            # (audit H3) rather than running on a stale role/station snapshot.
            u.session_epoch = (u.session_epoch or 0) + 1

        try:
            _update_users(_mutate)
        except _NotFound:
            abort(404)
        return jsonify({"ok": True, "token": token})

    @app.route("/api/admin/users/<username>/magic-link", methods=["POST"])
    @require_admin
    def api_admin_users_magic_link(username: str):
        """Mint (or rotate) a one-click magic-login link for a host account.

        Returns the full ``<base>/l/<token>`` URL once — only the hash is
        persisted, mirroring how ``reset-password`` treats its token. Each
        call rotates the token, so re-issuing instantly invalidates the
        previous link. The link is a bearer credential, so we refuse to
        mint one for an ``admin`` account: a leaked link must never be able
        to confer admin. Pair this with ``expires_at`` (set via the user
        PATCH) to bound the link's lifetime.

        Optional JSON body: ``{"days": <int>}`` (account is active for that
        many days from now) or ``{"expires_at": "<ISO8601 UTC>"}``. If both
        are present, ``expires_at`` wins. Omit both to leave the existing
        expiry untouched.
        """
        from public_api import _public_base_url

        body = request.get_json(silent=True) or {}
        new_expiry = body.get("expires_at")
        if not new_expiry and body.get("days") is not None:
            try:
                days = int(body["days"])
            except (TypeError, ValueError):
                return jsonify({"error": "days must be an integer"}), 400
            if days < 1 or days > 3650:
                return jsonify({"error": "days must be between 1 and 3650"}), 400
            new_expiry = (
                datetime.now(timezone.utc) + timedelta(days=days)
            ).isoformat()

        token = secrets.token_urlsafe(32)

        class _NotFound(Exception):
            pass

        class _AdminRefused(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NotFound()
            u = users[username]
            if u.role == "admin":
                raise _AdminRefused()
            u.magic_token = generate_password_hash(token)
            if new_expiry:
                u.expires_at = new_expiry

        try:
            _update_users(_mutate)
        except _NotFound:
            abort(404)
        except _AdminRefused:
            return jsonify(
                {"error": "magic links are not allowed for admin accounts"}), 400

        url = f"{_public_base_url()}/l/{token}"
        return jsonify({"ok": True, "url": url, "token": token})

    @app.route("/api/admin/users/<username>/magic-link", methods=["DELETE"])
    @require_admin
    def api_admin_users_magic_link_revoke(username: str):
        """Revoke a magic-login link by clearing the stored token hash."""
        class _NotFound(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NotFound()
            users[username].magic_token = None

        try:
            _update_users(_mutate)
        except _NotFound:
            abort(404)
        return jsonify({"ok": True})

    @app.route("/api/admin/users/<username>", methods=["DELETE"])
    @require_admin
    def api_admin_users_delete(username: str):
        class _NotFound(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NotFound()
            users.pop(username)

        try:
            _update_users(_mutate)
        except _NotFound:
            abort(404)
        return jsonify({"ok": True})

    @app.route("/api/admin/health/activity_log")
    @require_admin
    def api_admin_activity_log_health():
        """Operator-visibility for the bounded activity-log queue (P1-15).

        Returns the running drop counter and the current queue depth.
        When ``dropped_since_boot`` is non-zero some authenticated
        traffic has been logged with gaps and we should investigate
        either the writer-thread health or the load that's outrunning
        the disk.
        """
        return jsonify(security.activity_log_stats())


def register_admin_network_routes(
    app: Flask,
    config: DashboardConfig,
    *,
    save_config: Callable[[], None],
    config_to_dict: Callable[[], dict[str, Any]],
    require_station: Callable[[str], StationConfig],
    push_rotate_to_station: Callable[..., dict[str, Any]],
    session_for_url,
    config_path: "Path | None" = None,
) -> None:
    """Attach the /api/admin/network-config + autodetect routes.

    The closure-bound helpers (``save_config`` / ``config_to_dict`` /
    ``require_station`` / ``push_rotate_to_station``) are passed in from
    ``create_app()`` rather than re-defined here — keeping the atomic
    write lock and tunnel reference shared with the rest of the dashboard.
    """
    from rovimen_dashboard import require_admin
    import config_sync

    _original_save = save_config

    def save_config():
        _original_save()
        if config_path:
            config_sync.sync_config(config_path)

    @app.route("/api/admin/network-config")
    @require_admin
    def api_admin_network_config_get():
        return jsonify(config_to_dict())

    @app.route("/api/admin/network-config/station/<host_key>", methods=["PATCH"])
    @require_admin
    def api_admin_network_config_station(host_key: str):
        require_station(host_key)
        st = config.stations[host_key]
        body = request.get_json(force=True) or {}
        allowed = {"ip", "label", "ssh_user", "proxy_media", "jump_hosts", "lat", "lon", "show_on_map", "public_tabs", "public", "location_name", "status", "push_enabled"}
        updates = {k: v for k, v in body.items() if k in allowed}
        try:
            config.stations[host_key] = st.model_copy(update=updates)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Validation failed: {exc}"}), 422
        save_config()
        return jsonify({"ok": True})

    @app.route(
        "/api/admin/network-config/station/<host_key>/camera",
        methods=["POST"],
    )
    @require_admin
    def api_admin_network_config_camera_add(host_key: str):
        require_station(host_key)
        st = config.stations[host_key]
        body = request.get_json(force=True) or {}
        code = (body.get("code") or "").strip()
        if not code:
            return jsonify({"ok": False, "error": "code is required"}), 400
        if any(c.code == code for c in st.cameras):
            return jsonify({"ok": False, "error": f"Camera {code} already exists"}), 409
        st.cameras.append(CameraConfig(
            code=code,
            cam_ip=body.get("cam_ip", ""),
            rotate=bool(body.get("rotate", False)),
            az=body.get("az") or None,
            alt=body.get("alt") or None,
        ))
        save_config()
        return jsonify({"ok": True})

    @app.route(
        "/api/admin/network-config/station/<host_key>/camera/<cam_code>",
        methods=["PATCH"],
    )
    @require_admin
    def api_admin_network_config_camera(host_key: str, cam_code: str):
        require_station(host_key)
        st = config.stations[host_key]
        cam = next((c for c in st.cameras if c.code == cam_code), None)
        if not cam:
            abort(404)
        body = request.get_json(force=True) or {}
        rotate_changed = "rotate" in body and bool(body["rotate"]) != bool(cam.rotate)
        cam_allowed = {"cam_ip", "rotate", "az", "alt", "label", "code"}
        cam_updates = {k: v for k, v in body.items() if k in cam_allowed}
        try:
            updated_cam = cam.model_copy(update=cam_updates)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Validation failed: {exc}"}), 422
        idx = next(i for i, c in enumerate(st.cameras) if c.code == cam_code)
        st.cameras[idx] = updated_cam
        save_config()

        # Push rotate change to the station so its local config.json stays in
        # sync with dashboard_config.yaml (the source of truth per PR #46).
        # The station's `code` key in config.json matches the camera code here.
        station_push = None
        if rotate_changed:
            target_code = body.get("code", cam_code)
            station_push = push_rotate_to_station(host_key, target_code, bool(body["rotate"]))
        return jsonify({"ok": True, "station_push": station_push})

    @app.route(
        "/api/admin/network-config/station/<host_key>/camera/<cam_code>",
        methods=["DELETE"],
    )
    @require_admin
    def api_admin_network_config_camera_delete(host_key: str, cam_code: str):
        require_station(host_key)
        st = config.stations[host_key]
        st.cameras = [c for c in st.cameras if c.code != cam_code]
        save_config()
        return jsonify({"ok": True})

    @app.route("/api/admin/sync-rotate-all", methods=["POST"])
    @require_admin
    def api_admin_sync_rotate_all():
        """Push the rotate flag from dashboard_config.yaml to every station's
        live config.json via station API PATCH /api/settings.

        One-shot repair for drift between dashboard config and deployed station
        configs. Returns a per-camera result list."""
        body = request.get_json(silent=True) or {}
        only_host = body.get("host")
        results: list[dict[str, Any]] = []
        for host_key, st in config.stations.items():
            if only_host and host_key != only_host:
                continue
            for cam in st.cameras:
                push = push_rotate_to_station(host_key, cam.code, bool(cam.rotate))
                results.append({
                    "host": host_key,
                    "cam": cam.code,
                    "rotate": bool(cam.rotate),
                    "ok": push["ok"],
                    "error": push.get("error"),
                })
        total = len(results)
        succeeded = sum(1 for r in results if r["ok"])
        return jsonify({"ok": succeeded == total, "total": total, "succeeded": succeeded, "results": results})

    @app.route("/api/admin/autodetect", methods=["POST"])
    @require_admin
    def api_admin_autodetect():
        body = request.get_json(force=True) or {}
        ip = body.get("ip", "")
        port = body.get("port", 7779)
        deny_reason = validate_autodetect_target(ip, port)
        # H6: allowing the tailnet CGNAT range is an accepted tradeoff — make
        # every attempt auditable. Log the requested target + outcome (allowed
        # or the deny reason) with the admin's IP/UA so tailnet-scanning via a
        # hijacked admin session is at least attributable after the fact.
        security.audit_request(
            "admin.autodetect",
            target_ip=str(ip),
            target_port=port,
            allowed=deny_reason is None,
            deny_reason=deny_reason,
        )
        if deny_reason is not None:
            return jsonify({"ok": False, "error": deny_reason}), 400
        port = int(port)
        try:
            autodetect_url = f"http://{ip}:{port}/api/settings"
            resp = session_for_url(autodetect_url).get(autodetect_url, timeout=8)
            resp.raise_for_status()
            settings = resp.json()
        except Exception:
            return jsonify({"ok": False, "error": f"Could not reach station at {ip}:{port}"})

        cameras = []
        ssh_user = "gmn"
        stations_dict = settings.get("stations", {})
        first = True
        for code, sinfo in stations_dict.items():
            rtsp = sinfo.get("camera_rtsp", "")
            m = re.search(r"rtsp://admin:@([\d.]+):", rtsp)
            cam_ip = m.group(1) if m else ""
            rotate = sinfo.get("rotate", False)
            cameras.append({"code": code, "cam_ip": cam_ip, "rotate": rotate})
            if first:
                rms_path = sinfo.get("rms_data_path", "")
                um = re.search(r"/home/([^/]+)/", rms_path)
                if um:
                    ssh_user = um.group(1)
                first = False

        return jsonify({"ok": True, "cameras": cameras, "ssh_user": ssh_user})

    @app.route("/api/admin/network-config/station", methods=["POST"])
    @require_admin
    def api_admin_network_config_station_add():
        body = request.get_json(force=True) or {}
        host = body.get("host", "")
        if not re.match(r"^[a-z0-9_-]+$", host):
            return jsonify({"ok": False, "error": "Invalid host key"}), 400
        if host in config.stations:
            return jsonify({"ok": False, "error": f"Station '{host}' already exists"}), 409

        jump_raw = body.get("jump_hosts", [])
        jump_hosts = [j.strip() for j in jump_raw if j.strip()] if isinstance(jump_raw, list) else []

        cameras = [
            CameraConfig(
                code=c.get("code", ""),
                cam_ip=c.get("cam_ip", ""),
                rotate=bool(c.get("rotate", False)),
            )
            for c in (body.get("cameras") or [])
        ]

        config.stations[host] = StationConfig(
            ip=body.get("ip", ""),
            label=body.get("label", host),
            ssh_user=body.get("ssh_user", "gmn"),
            proxy_media=bool(body.get("proxy_media", False)),
            jump_hosts=jump_hosts,
            cameras=cameras,
            lat=body.get("lat") or None,
            lon=body.get("lon") or None,
            show_on_map=bool(body.get("show_on_map", True)),
        )
        save_config()
        return jsonify({"ok": True})

    @app.route("/api/admin/network-config/station/<host_key>", methods=["DELETE"])
    @require_admin
    def api_admin_network_config_station_delete(host_key: str):
        require_station(host_key)
        config.stations.pop(host_key)
        save_config()
        return jsonify({"ok": True})
