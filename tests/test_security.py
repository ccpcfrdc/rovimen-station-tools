"""Tests for dashboard/security.py — TOTP, login throttle, headers, helpers."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from unittest.mock import patch

import pyotp
import pytest
from flask import Flask

import security


# ── Helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_login_failures():
    """Ensure the module-level login-failure dict is empty between tests."""
    security._login_failures.clear()
    yield
    security._login_failures.clear()


def _make_app() -> Flask:
    """Minimal Flask app for header / response testing."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.route("/test")
    def _test_view():
        return "ok"

    app.after_request(security.add_security_headers)
    return app


# ── TOTP ──────────────────────────────────────────────────────────────────


class TestGenerateTotpSecret:
    def test_returns_base32_string(self):
        secret = security.generate_totp_secret()
        assert isinstance(secret, str)
        assert len(secret) > 0
        # base32 alphabet: A-Z, 2-7, optional padding =
        import re
        assert re.fullmatch(r"[A-Z2-7=]+", secret), f"not valid base32: {secret}"


class TestTotpProvisioningUri:
    def test_contains_username(self):
        secret = security.generate_totp_secret()
        uri = security.totp_provisioning_uri(secret, "alex")
        assert "otpauth://totp/" in uri
        assert "alex" in uri

    def test_contains_issuer(self):
        secret = security.generate_totp_secret()
        uri = security.totp_provisioning_uri(secret, "alice")
        assert "issuer=" in uri.lower() or "issuer=" in uri

    def test_custom_issuer_from_env(self, monkeypatch):
        monkeypatch.setenv("ROVIMEN_TOTP_ISSUER", "TestIssuer")
        secret = security.generate_totp_secret()
        uri = security.totp_provisioning_uri(secret, "bob")
        assert "TestIssuer" in uri


class TestTotpQrDataUri:
    def test_returns_data_uri(self):
        secret = security.generate_totp_secret()
        prov = security.totp_provisioning_uri(secret, "testuser")
        data_uri = security.totp_qr_data_uri(prov)
        assert data_uri.startswith("data:image/png;base64,")
        # The base64 payload should be non-trivial
        payload = data_uri.split(",", 1)[1]
        assert len(payload) > 100


class TestVerifyTotp:
    def test_valid_code(self):
        secret = security.generate_totp_secret()
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert security.verify_totp(secret, code) is True

    def test_empty_secret(self):
        assert security.verify_totp("", "123456") is False

    def test_empty_code(self):
        secret = security.generate_totp_secret()
        assert security.verify_totp(secret, "") is False

    def test_wrong_code(self):
        secret = security.generate_totp_secret()
        assert security.verify_totp(secret, "000000") is False

    def test_none_secret(self):
        assert security.verify_totp(None, "123456") is False

    def test_none_code(self):
        secret = security.generate_totp_secret()
        assert security.verify_totp(secret, None) is False

    def test_code_with_whitespace(self):
        secret = security.generate_totp_secret()
        totp = pyotp.TOTP(secret)
        code = f"  {totp.now()}  "
        assert security.verify_totp(secret, code) is True


# ── Login throttle ───────────────────────────────────────────────────────


class TestIsUserLocked:
    def test_fresh_user_not_locked(self):
        assert security.is_user_locked("alice") is False

    def test_empty_username_returns_false(self):
        assert security.is_user_locked("") is False

    def test_none_username_returns_false(self):
        assert security.is_user_locked(None) is False


class TestRecordLoginFailure:
    def test_nine_failures_not_locked(self):
        for _ in range(9):
            security.record_login_failure("bob")
        assert security.is_user_locked("bob") is False

    def test_ten_failures_locked(self):
        for _ in range(10):
            security.record_login_failure("bob")
        assert security.is_user_locked("bob") is True

    def test_returns_true_only_on_threshold(self):
        results = []
        for _ in range(12):
            results.append(security.record_login_failure("charlie"))
        # Only the 10th failure (index 9) should return True
        assert results[9] is True
        assert all(r is False for r in results[:9])
        assert all(r is False for r in results[10:])

    def test_empty_username_returns_false(self):
        assert security.record_login_failure("") is False


class TestClearLoginFailures:
    def test_clears_counter(self):
        for _ in range(10):
            security.record_login_failure("dave")
        assert security.is_user_locked("dave") is True
        security.clear_login_failures("dave")
        assert security.is_user_locked("dave") is False

    def test_after_clear_can_fail_again(self):
        for _ in range(10):
            security.record_login_failure("eve")
        security.clear_login_failures("eve")
        # Should need another 10 failures to lock again
        for _ in range(9):
            security.record_login_failure("eve")
        assert security.is_user_locked("eve") is False

    def test_clear_empty_username_noop(self):
        # Should not raise
        security.clear_login_failures("")
        security.clear_login_failures(None)


class TestUsernameNormalization:
    def test_case_insensitive(self):
        for _ in range(10):
            security.record_login_failure("Alice")
        assert security.is_user_locked("alice") is True
        assert security.is_user_locked("ALICE") is True
        assert security.is_user_locked("aLiCe") is True

    def test_whitespace_stripped(self):
        for _ in range(10):
            security.record_login_failure("  frank  ")
        assert security.is_user_locked("frank") is True


class TestLockoutExpiry:
    def test_lockout_expires(self):
        for _ in range(10):
            security.record_login_failure("grace")
        assert security.is_user_locked("grace") is True

        # Advance time past lockout window
        future = time.time() + security._LOGIN_LOCKOUT_SECONDS + 1
        with patch.object(security._time, "time", return_value=future):
            assert security.is_user_locked("grace") is False


# ── Security headers ─────────────────────────────────────────────────────


class TestSecurityHeaders:
    def setup_method(self):
        self.app = _make_app()
        self.client = self.app.test_client()

    def test_hsts_header(self):
        resp = self.client.get("/test")
        assert "Strict-Transport-Security" in resp.headers
        assert "max-age=" in resp.headers["Strict-Transport-Security"]

    def test_content_type_options(self):
        resp = self.client.get("/test")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"

    def test_frame_options(self):
        resp = self.client.get("/test")
        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"

    def test_csp_header(self):
        resp = self.client.get("/test")
        assert "Content-Security-Policy" in resp.headers
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src" in csp

    def test_referrer_policy(self):
        resp = self.client.get("/test")
        assert "Referrer-Policy" in resp.headers

    def test_existing_headers_not_overwritten(self):
        """setdefault should not overwrite a pre-existing header."""
        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.route("/custom")
        def _custom():
            from flask import make_response
            resp = make_response("ok")
            resp.headers["X-Frame-Options"] = "DENY"
            return resp

        app.after_request(security.add_security_headers)
        client = app.test_client()
        resp = client.get("/custom")
        assert resp.headers["X-Frame-Options"] == "DENY"


# ── _parse_trusted_origins ───────────────────────────────────────────────


class TestParseTrustedOrigins:
    def test_comma_separated(self):
        result = security._parse_trusted_origins(
            "https://a.example.com,https://b.example.com"
        )
        assert result == frozenset({"https://a.example.com", "https://b.example.com"})

    def test_trailing_slashes_stripped(self):
        result = security._parse_trusted_origins("https://a.example.com/")
        assert "https://a.example.com" in result
        assert "https://a.example.com/" not in result

    def test_empty_entries_filtered(self):
        result = security._parse_trusted_origins(",, https://a.example.com , ,,")
        assert result == frozenset({"https://a.example.com"})

    def test_empty_string(self):
        result = security._parse_trusted_origins("")
        assert result == frozenset()

    def test_whitespace_only(self):
        result = security._parse_trusted_origins("   ,  , ")
        assert result == frozenset()


# ── lock_file_perms ──────────────────────────────────────────────────────


class TestLockFilePerms:
    def test_sets_permissions(self, tmp_path):
        f = tmp_path / "secret.yaml"
        f.write_text("password: hunter2")
        # Make sure it starts with broader permissions
        os.chmod(f, 0o644)
        security.lock_file_perms(f, mode=0o600)
        mode = stat.S_IMODE(f.stat().st_mode)
        assert mode == 0o600

    def test_noop_on_nonexistent(self, tmp_path):
        missing = tmp_path / "does_not_exist.yaml"
        # Should not raise
        security.lock_file_perms(missing)
