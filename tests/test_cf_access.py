"""Tests for dashboard/cf_access.py — Cloudflare Access JWT verification.

cf_access.py is the sole auth-bypass path when the dashboard runs behind a
Cloudflare Tunnel. A bug here lets attackers impersonate any user without a
password, so it warrants its own focused test module.

All network I/O is mocked: tests never reach the internet. The JWT fixtures
are signed with a throwaway RSA key generated once per session.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

import cf_access


# ── Key generation (session-scoped: one key pair for the whole test run) ──


@pytest.fixture(scope="session")
def rsa_key_pair():
    """Generate a throwaway RSA-2048 key pair for signing test tokens."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture
def cfg():
    return cf_access.CFAccessConfig(
        team_domain="rovimen.cloudflareaccess.com",
        audience="test-audience-tag",
    )


def _make_token(
    private_pem: bytes,
    *,
    audience: str = "test-audience-tag",
    issuer: str = "https://rovimen.cloudflareaccess.com",
    email: str = "alice@example.com",
    expired: bool = False,
) -> str:
    """Build a signed RS256 JWT for testing."""
    now = int(time.time())
    exp = (now - 3600) if expired else (now + 3600)
    payload = {
        "iss": issuer,
        "aud": audience,
        "email": email,
        "iat": now,
        "exp": exp,
        "sub": "test-subject",
    }
    return jwt.encode(payload, private_pem, algorithm="RS256")


def _signing_key_mock(public_pem: bytes):
    """Return a mock _JWKSCache whose get_signing_key yields the test public key."""
    key_obj = jwt.algorithms.RSAAlgorithm.from_jwk(
        jwt.algorithms.RSAAlgorithm.to_jwk(
            serialization.load_pem_public_key(public_pem)
        )
    )
    mock_cache = MagicMock(spec=cf_access._JWKSCache)
    mock_cache.get_signing_key.return_value = key_obj
    return mock_cache


# ── verify_token: happy path ───────────────────────────────────────────────


class TestVerifyTokenValid:
    def test_valid_token_returns_claims(self, rsa_key_pair, cfg):
        private_pem, public_pem = rsa_key_pair
        token = _make_token(private_pem, audience=cfg.audience, issuer=cfg.issuer)
        mock_cache = _signing_key_mock(public_pem)

        with patch("cf_access._get_jwks", return_value=mock_cache):
            claims = cf_access.verify_token(token, cfg)

        assert claims["email"] == "alice@example.com"
        assert "exp" in claims
        assert "iat" in claims

    def test_email_normalised_to_lowercase(self, rsa_key_pair, cfg):
        private_pem, public_pem = rsa_key_pair
        token = _make_token(
            private_pem,
            audience=cfg.audience,
            issuer=cfg.issuer,
            email="Alice@Example.COM",
        )
        mock_cache = _signing_key_mock(public_pem)

        with patch("cf_access._get_jwks", return_value=mock_cache):
            claims = cf_access.verify_token(token, cfg)

        assert claims["email"] == "alice@example.com"


# ── verify_token: rejection cases ─────────────────────────────────────────


class TestVerifyTokenRejected:
    def test_expired_token_raises(self, rsa_key_pair, cfg):
        private_pem, public_pem = rsa_key_pair
        token = _make_token(
            private_pem, audience=cfg.audience, issuer=cfg.issuer, expired=True
        )
        mock_cache = _signing_key_mock(public_pem)

        with patch("cf_access._get_jwks", return_value=mock_cache):
            with pytest.raises(cf_access.CFAccessVerifyError, match="JWT verification failed"):
                cf_access.verify_token(token, cfg)

    def test_wrong_audience_raises(self, rsa_key_pair, cfg):
        private_pem, public_pem = rsa_key_pair
        token = _make_token(
            private_pem,
            audience="wrong-audience",
            issuer=cfg.issuer,
        )
        mock_cache = _signing_key_mock(public_pem)

        with patch("cf_access._get_jwks", return_value=mock_cache):
            with pytest.raises(cf_access.CFAccessVerifyError, match="JWT verification failed"):
                cf_access.verify_token(token, cfg)

    def test_wrong_issuer_raises(self, rsa_key_pair, cfg):
        private_pem, public_pem = rsa_key_pair
        token = _make_token(
            private_pem,
            audience=cfg.audience,
            issuer="https://evil.cloudflareaccess.com",
        )
        mock_cache = _signing_key_mock(public_pem)

        with patch("cf_access._get_jwks", return_value=mock_cache):
            with pytest.raises(cf_access.CFAccessVerifyError, match="JWT verification failed"):
                cf_access.verify_token(token, cfg)

    def test_missing_email_claim_raises(self, rsa_key_pair, cfg):
        """A structurally valid token that lacks the email claim must be rejected."""
        private_pem, public_pem = rsa_key_pair
        now = int(time.time())
        payload = {
            "iss": cfg.issuer,
            "aud": cfg.audience,
            "iat": now,
            "exp": now + 3600,
            "sub": "no-email",
            # deliberately no "email" key
        }
        token = jwt.encode(payload, private_pem, algorithm="RS256")
        mock_cache = _signing_key_mock(public_pem)

        with patch("cf_access._get_jwks", return_value=mock_cache):
            with pytest.raises(cf_access.CFAccessVerifyError, match="missing email claim"):
                cf_access.verify_token(token, cfg)

    def test_garbage_token_raises(self, cfg):
        mock_cache = MagicMock(spec=cf_access._JWKSCache)
        mock_cache.get_signing_key.side_effect = jwt.PyJWKClientError("no matching key")

        with patch("cf_access._get_jwks", return_value=mock_cache):
            with pytest.raises(cf_access.CFAccessVerifyError, match="JWKS fetch failed"):
                cf_access.verify_token("not.a.jwt", cfg)


# ── JWKS fetch failure: fail-safe (returns None, does not authenticate) ───


class TestJwksFetchFailure:
    def test_network_error_returns_none(self, monkeypatch):
        """A requests.RequestException during JWKS fetch must not authenticate."""
        import requests

        monkeypatch.setenv("ROVIMEN_CF_TEAM_DOMAIN", "rovimen.cloudflareaccess.com")
        monkeypatch.setenv("ROVIMEN_CF_AUD", "test-audience-tag")

        mock_cache = MagicMock(spec=cf_access._JWKSCache)
        mock_cache.get_signing_key.side_effect = requests.RequestException("timeout")

        # Reset module-level cache so load_config() picks up monkeypatched env
        with patch("cf_access._get_jwks", return_value=mock_cache):
            result = cf_access.verify_request_token(
                headers={"Cf-Access-Jwt-Assertion": "some.token.here"},
                cookies={},
            )

        assert result is None, "Network error during JWKS fetch must not authenticate"

    def test_jwks_client_error_returns_none(self, monkeypatch):
        """A PyJWKClientError (e.g. key not in JWKS) must not authenticate."""
        monkeypatch.setenv("ROVIMEN_CF_TEAM_DOMAIN", "rovimen.cloudflareaccess.com")
        monkeypatch.setenv("ROVIMEN_CF_AUD", "test-audience-tag")

        mock_cache = MagicMock(spec=cf_access._JWKSCache)
        mock_cache.get_signing_key.side_effect = jwt.PyJWKClientError("key not found")

        with patch("cf_access._get_jwks", return_value=mock_cache):
            result = cf_access.verify_request_token(
                headers={"Cf-Access-Jwt-Assertion": "some.token.here"},
                cookies={},
            )

        assert result is None


# ── verify_request_token: integration glue ────────────────────────────────


class TestVerifyRequestToken:
    def test_no_env_vars_returns_none(self, monkeypatch):
        """When CF integration is not configured, verify_request_token returns None."""
        monkeypatch.delenv("ROVIMEN_CF_TEAM_DOMAIN", raising=False)
        monkeypatch.delenv("ROVIMEN_CF_AUD", raising=False)

        result = cf_access.verify_request_token(
            headers={"Cf-Access-Jwt-Assertion": "whatever"},
            cookies={},
        )
        assert result is None

    def test_no_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("ROVIMEN_CF_TEAM_DOMAIN", "rovimen.cloudflareaccess.com")
        monkeypatch.setenv("ROVIMEN_CF_AUD", "test-audience-tag")

        result = cf_access.verify_request_token(headers={}, cookies={})
        assert result is None

    def test_token_from_cookie_accepted(self, rsa_key_pair, monkeypatch):
        private_pem, public_pem = rsa_key_pair
        monkeypatch.setenv("ROVIMEN_CF_TEAM_DOMAIN", "rovimen.cloudflareaccess.com")
        monkeypatch.setenv("ROVIMEN_CF_AUD", "test-audience-tag")

        cfg = cf_access.CFAccessConfig(
            team_domain="rovimen.cloudflareaccess.com",
            audience="test-audience-tag",
        )
        token = _make_token(private_pem, audience=cfg.audience, issuer=cfg.issuer)
        mock_cache = _signing_key_mock(public_pem)

        with patch("cf_access._get_jwks", return_value=mock_cache):
            result = cf_access.verify_request_token(
                headers={},
                cookies={"CF_Authorization": token},
            )

        assert result is not None
        assert result["email"] == "alice@example.com"


# ── extract_token ──────────────────────────────────────────────────────────


class TestExtractToken:
    def test_header_takes_precedence_over_cookie(self):
        result = cf_access.extract_token(
            headers={"Cf-Access-Jwt-Assertion": "header-token"},
            cookies={"CF_Authorization": "cookie-token"},
        )
        assert result == "header-token"

    def test_falls_back_to_cookie(self):
        result = cf_access.extract_token(
            headers={},
            cookies={"CF_Authorization": "cookie-token"},
        )
        assert result == "cookie-token"

    def test_returns_none_when_absent(self):
        result = cf_access.extract_token(headers={}, cookies={})
        assert result is None

    def test_whitespace_only_treated_as_absent(self):
        result = cf_access.extract_token(
            headers={"Cf-Access-Jwt-Assertion": "   "},
            cookies={},
        )
        assert result is None
