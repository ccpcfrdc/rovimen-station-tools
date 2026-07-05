"""Shared Pydantic configuration / user dataclasses for the dashboard.

These models are deliberately kept dependency-free (only ``pydantic``) so that
they can be imported by ``rovimen_dashboard.py``, ``public_api.py`` and any
future extracted sub-modules without circular-import grief.

Moving forward, any new dataclass that is consumed by more than one of the
dashboard sub-modules should live here too.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class CameraConfig(BaseModel):
    code: str
    cam_ip: str
    rotate: bool = False
    label: str = ""
    az: float | None = None   # pointing azimuth degrees (0=N, 90=E, 180=S, 270=W)
    alt: float | None = None  # pointing elevation degrees above horizon
    rtsp_url: str | None = None

    @field_validator("cam_ip")
    @classmethod
    def _validate_cam_ip(cls, v: str) -> str:
        import re
        v = v.strip()
        if v and not re.match(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$", v):
            raise ValueError(f"cam_ip must be a dotted-quad IPv4 address, got {v!r}")
        return v


class StationConfig(BaseModel):
    ip: str
    label: str
    ssh_user: str = "gmn"
    cameras: list[CameraConfig] = Field(default_factory=list)
    jump_hosts: list[str] = Field(default_factory=list)
    # Pinned SSH host-key fingerprint(s) for this station's IP (H5). When set,
    # `_ensure_known_hosts` refuses to trust an ssh-keyscan result unless its
    # fingerprint matches one of these values, closing the first-contact TOFU
    # MITM window. Fingerprints are the base64 SHA256 form ssh-keygen emits
    # (e.g. "SHA256:abc123..."), one per host key type, and MUST be verified
    # out-of-band before pinning (see tools/pin_host_keys.py). Left empty for
    # already-deployed stations, which stay on accept-new (a WARNING is logged
    # so unpinned hosts remain visible) — this keeps the fleet backward
    # compatible while the pins are rolled out station by station.
    ssh_host_key_fingerprints: list[str] = Field(default_factory=list)
    proxy_media: bool = False  # proxy all media through dashboard (for cross-tailnet stations)
    lat: float | None = None
    lon: float | None = None
    show_on_map: bool = True
    public_tabs: list[str] = Field(default_factory=list)  # 'videodb', 'archive' visible to guests
    # Controls visibility on the public /api/public/v1/* + /media/v1/*
    # read-only surface. Default true so every new station is visible
    # automatically. Set to false for stations still in commissioning or
    # explicitly opted out. Non-public stations still participate internally
    # (overview, events, GMN cross-join) but are never returned by public
    # endpoints and their media is not served via /media/v1/.
    public: bool = True
    # Human-readable location surfaced on the public API. Falls back to
    # `label` when empty. Free-form (e.g. "Bistrița, Bistrița-Năsăud").
    location_name: str = ""
    # Operational status: "active" (default) or "commissioning". Stations
    # in commissioning state show orange markers on the overview map instead
    # of the normal green/red online/offline indicators.
    status: str = "active"
    # Migration primitive for the reversed-HTTP push rollout. When True, the
    # dashboard stops server-side POLLING this station (status/vitals in
    # station_client + the detection-index poll in index_poller) and relies
    # entirely on data the station PUSHES into the shared StationCache /
    # detections.db via the ingest API. Default False keeps every station on
    # the existing poll path — flip a single station to True to cut it over to
    # push individually. Fail-safe: a push_enabled station that stops pushing
    # simply shows its last-known cache entry (the ingest owns freshness); the
    # dashboard never fabricates status for it.
    push_enabled: bool = False


# Stable page keys that CAN be exposed to anonymous visitors. Each maps to
# one or more ``@public_route(page=...)`` views. This is the *eligible* set
# — the closed vocabulary the runtime ``public_pages`` toggle draws from.
# A key here does NOT make a page public on its own; it only means the page
# is allowed to appear in ``public_pages``. Sensitive pages (admin, config,
# social, network, station-write) are deliberately absent and have no
# ``page=`` tag, so config can never expose them. "highlights" is eligible:
# its page + data route serve curated, GMN-confirmed top-10 clips carrying
# no admin fields, so an operator may opt it into ``public_pages``.
PUBLIC_PAGE_KEYS: frozenset[str] = frozenset(
    {"overview", "station", "events", "live", "showers", "highlights"}
)


class DashboardConfig(BaseModel):
    station_api_port: int = 7779
    correlation_window_s: int = 1
    stations: dict[str, StationConfig] = Field(default_factory=dict)
    # Runtime on/off switch for which public-capable pages anonymous visitors
    # may reach. Each entry is a stable page key (see PUBLIC_PAGE_KEYS) that
    # maps to one or more ``@public_route(page=...)`` views. The auth gate
    # exposes a page to anon ONLY IF (a) its route is page-tagged eligible AND
    # (b) its key appears here. Removing a key re-gates that page on the next
    # restart — no code deploy. FAIL-CLOSED: unknown keys are dropped (they
    # can never widen access), and a page-tagged route whose key is absent
    # stays login-gated. Default = all eligible pages on (preserves the
    # existing public surface from PR #628 out of the box).
    public_pages: list[str] = Field(default_factory=lambda: sorted(PUBLIC_PAGE_KEYS))

    # Optional subset of YOUR own camera codes to treat as a "highlight"
    # overlay: events witnessed ONLY by these stations are tagged ``de_only``
    # (a muted secondary marker), while events with >=1 non-highlight station
    # are the headline set. Empty (the default) = one undifferentiated "ours",
    # which is what most networks want. ROVIMEN sets its Berlin DE codes here.
    highlight_codes: list[str] = Field(default_factory=list)

    @field_validator("highlight_codes", mode="before")
    @classmethod
    def _coerce_highlight_codes(cls, v: object) -> object:
        # A bare/null key means "no highlight". Upper-case and drop blanks so
        # matching against GMN station codes is case-insensitive.
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [str(c).strip().upper() for c in v if str(c).strip()]
        return v

    @field_validator("public_pages", mode="before")
    @classmethod
    def _coerce_null_public_pages(cls, v: object) -> object:
        # A bare ``public_pages:`` YAML key (null) means "none enabled" —
        # fail-closed, not "fall back to default".
        return v if v is not None else []

    @field_validator("public_pages")
    @classmethod
    def _drop_unknown_public_pages(cls, v: list[str]) -> list[str]:
        # Silently drop keys outside the eligible vocabulary. An operator
        # typo or a stale/renamed key can therefore never expose anything —
        # it just resolves to "not enabled". De-duplicate while preserving
        # a stable order for readable round-trips.
        seen: set[str] = set()
        out: list[str] = []
        for key in v:
            if key in PUBLIC_PAGE_KEYS and key not in seen:
                seen.add(key)
                out.append(key)
        return out


class UserConfig(BaseModel):
    display_name: str = ""
    password_hash: str = ""  # empty when the user only signs in via Cloudflare Access
    role: str  # 'admin' | 'host' | 'visitor' | 'press'
    stations: list[str] = Field(default_factory=list)

    @field_validator("stations", mode="before")
    @classmethod
    def _coerce_null_stations(cls, v: object) -> list[str]:
        return v if v is not None else []
    # Werkzeug password hash of the one-time reset token. The plaintext
    # token is shown to the admin once at issuance and never stored —
    # leaking users.yaml no longer leaks an active account-takeover code.
    reset_token: str | None = None
    reset_token_expiry: str | None = None  # ISO8601 UTC
    # Optional Cloudflare Access identity. When set, a verified JWT whose
    # `email` claim matches this value auto-logs the user in (no password).
    # Lookup is case-insensitive — emails are normalised to lowercase before
    # comparison.
    email: str | None = None
    # TOTP MFA. `totp_secret` holds the base32 shared secret once the user
    # has enrolled. `require_totp` defaults to True — every new account
    # is forced to enrol an authenticator on first login. Existing rows
    # without the field pick up the default through Pydantic validation,
    # so legacy users are automatically opted in next time they sign in.
    totp_secret: str | None = None
    require_totp: bool = True
    # Optional one-click magic-login token. Stores a Werkzeug hash of a
    # high-entropy token (plaintext shown to the issuing admin once and
    # delivered out-of-band as a /l/<token> link). A hit on that route
    # establishes a normal 7-day session WITHOUT a password or TOTP — the
    # link itself is the bearer credential. Gated by `expires_at` (the
    # account-level expiry), audit-logged, and scoped by `role`/`stations`.
    # Set to None to revoke. Only minted for `host`-role accounts so a
    # leaked link can never confer admin.
    magic_token: str | None = None
    # Optional account-level expiry (ISO 8601 UTC, e.g. "2026-05-25T18:30:00+00:00").
    # When set and past, _check_credentials() refuses authentication for the
    # account entirely — distinct from reset_token_expiry, which only gates
    # the initial first-login token. Used for tester accounts.
    expires_at: str | None = None
    # Monotonic session-invalidation counter. Stamped into the session at
    # login and re-checked on every authenticated request: an admin bumps it
    # whenever they change this account's role, stations, password, or expiry,
    # so the long-lived (90-day) session cookie can't keep granting the *old*
    # privilege level after a demotion/de-scope. A mismatch forces the live
    # role/stations to be re-read from users.yaml (or the session invalidated).
    # Starts at 0 for legacy rows via the default.
    session_epoch: int = 0
