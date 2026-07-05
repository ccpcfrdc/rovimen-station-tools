"""Cloudflare Access JWT verification.

When the dashboard is fronted by a Cloudflare Tunnel + Cloudflare Access,
every request from the public hostname carries a signed JWT in the
``Cf-Access-Jwt-Assertion`` header (also available as the
``CF_Authorization`` cookie). Trusting only the ``Cf-Access-Authenticated-
User-Email`` header would be unsafe — anyone who reaches Flask outside the
tunnel (e.g. over Tailscale, or via a misconfigured network) can spoof it.

This module verifies the JWT cryptographically against Cloudflare's JWKS so
the email claim becomes a hard identity, and exposes a single
``verify_request_token()`` helper the Flask app uses inside its
``before_request`` hook.

Configuration (env on the VPS):

* ``ROVIMEN_CF_TEAM_DOMAIN`` — e.g. ``rovimen.cloudflareaccess.com``
* ``ROVIMEN_CF_AUD``        — the Application Audience (AUD) tag shown in
                              the Cloudflare Access app settings.

When either is unset, ``is_enabled()`` returns False and the rest of the
app falls back to the existing password-based ``/login``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import jwt
import requests

logger = logging.getLogger(__name__)

_JWKS_TTL_SECONDS = 30 * 60
_JWKS_FETCH_TIMEOUT = 5.0


@dataclass(frozen=True)
class CFAccessConfig:
    team_domain: str
    audience: str

    @property
    def jwks_url(self) -> str:
        return f"https://{self.team_domain}/cdn-cgi/access/certs"

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"


def load_config() -> CFAccessConfig | None:
    team = os.environ.get("ROVIMEN_CF_TEAM_DOMAIN", "").strip()
    aud = os.environ.get("ROVIMEN_CF_AUD", "").strip()
    if not team or not aud:
        return None
    return CFAccessConfig(team_domain=team, audience=aud)


class _JWKSCache:
    """Fetches and caches Cloudflare's signing keys.

    JWKS rotates infrequently. We refresh every 30 min, and on any
    signature-verification failure (KeyID may have rotated mid-cache).
    """

    def __init__(self, jwks_url: str) -> None:
        self._url = jwks_url
        self._lock = threading.Lock()
        self._client: jwt.PyJWKClient | None = None
        self._fetched_at: float = 0.0

    def get_signing_key(self, token: str) -> Any:
        with self._lock:
            now = time.time()
            if self._client is None or (now - self._fetched_at) > _JWKS_TTL_SECONDS:
                self._client = jwt.PyJWKClient(
                    self._url, cache_keys=True, lifespan=_JWKS_TTL_SECONDS
                )
                self._fetched_at = now
            client = self._client
        return client.get_signing_key_from_jwt(token).key


_jwks_cache: _JWKSCache | None = None
_jwks_lock = threading.Lock()


def _get_jwks(cfg: CFAccessConfig) -> _JWKSCache:
    global _jwks_cache
    with _jwks_lock:
        if _jwks_cache is None or _jwks_cache._url != cfg.jwks_url:
            _jwks_cache = _JWKSCache(cfg.jwks_url)
        return _jwks_cache


class CFAccessVerifyError(Exception):
    pass


def extract_token(headers: Any, cookies: Any) -> str | None:
    """Pull the CF Access JWT from the request, header first then cookie.

    ``headers`` and ``cookies`` are Flask's ``request.headers`` and
    ``request.cookies`` (or anything with ``.get()``).
    """
    token = headers.get("Cf-Access-Jwt-Assertion", "") or ""
    if not token:
        token = cookies.get("CF_Authorization", "") or ""
    return token.strip() or None


def verify_token(token: str, cfg: CFAccessConfig) -> dict[str, Any]:
    """Verify a CF Access JWT and return its decoded claims.

    Raises ``CFAccessVerifyError`` on any failure (bad signature, wrong
    audience, expired token, missing email claim, network error fetching
    JWKS, etc.). The caller must treat the error as auth failure.
    """
    try:
        signing_key = _get_jwks(cfg).get_signing_key(token)
    except (requests.RequestException, jwt.PyJWKClientError) as exc:
        raise CFAccessVerifyError(f"JWKS fetch failed: {exc}") from exc

    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "ES256"],
            audience=cfg.audience,
            issuer=cfg.issuer,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.InvalidTokenError as exc:
        raise CFAccessVerifyError(f"JWT verification failed: {exc}") from exc

    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise CFAccessVerifyError("JWT missing email claim")
    claims["email"] = email
    return claims


def verify_request_token(headers: Any, cookies: Any) -> dict[str, Any] | None:
    """High-level entry point used by Flask's before_request hook.

    Returns the verified claims dict if a valid CF Access JWT is present
    and Cloudflare integration is enabled, else None. Errors during
    verification are *not* propagated — they downgrade to None and are
    logged. The caller decides whether to reject (strict mode) or fall
    through to the password login (optional mode).
    """
    cfg = load_config()
    if cfg is None:
        return None
    token = extract_token(headers, cookies)
    if not token:
        return None
    try:
        return verify_token(token, cfg)
    except CFAccessVerifyError as exc:
        logger.warning("CF Access JWT rejected: %s", exc)
        return None
