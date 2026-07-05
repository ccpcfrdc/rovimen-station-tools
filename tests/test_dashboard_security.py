"""Tests for dashboard security edge cases — P2 #12, #13.

P2 #12: Magic login with malformed expires_at falls through to non-expired.
P2 #13: Debug endpoint uses non-constant-time comparison.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import pytest


# ── P2 #12: Magic login expires_at parse failure ─────────────────────────


class TestMagicLoginExpiry:
    """The magic_login handler catches ValueError on expires_at parse and
    falls through to the non-expired path. This means a malformed expiry
    string makes the account immortal."""

    @staticmethod
    def _check_expiry(expires_at: str | None) -> str:
        """Replicate the expiry check from magic_login (lines 2860-2870)."""
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at).astimezone(timezone.utc)
                if datetime.now(timezone.utc) > exp:
                    return "expired"
            except ValueError:
                pass  # malformed → falls through to "valid"
        return "valid"

    def test_valid_future_expiry(self):
        assert self._check_expiry("2099-12-31T23:59:59+00:00") == "valid"

    def test_valid_past_expiry(self):
        assert self._check_expiry("2020-01-01T00:00:00+00:00") == "expired"

    def test_none_expiry_is_valid(self):
        assert self._check_expiry(None) == "valid"

    def test_empty_string_is_valid(self):
        """Empty string is falsy, so the if-branch is skipped."""
        assert self._check_expiry("") == "valid"

    def test_malformed_string_falls_through_to_valid(self):
        """A non-ISO string causes ValueError, caught by except, returns 'valid'.
        This is the documented bug — should fail closed to 'expired'."""
        assert self._check_expiry("never") == "valid"

    def test_partial_iso_falls_through(self):
        """A truncated date that fromisoformat can't parse."""
        assert self._check_expiry("2026-13-45") == "valid"

    def test_garbage_bytes_falls_through(self):
        assert self._check_expiry("xyz!@#$%") == "valid"

    def test_safe_alternative_fails_closed(self):
        """Show what the correct behaviour would be: malformed → expired."""
        def _check_expiry_safe(expires_at: str | None) -> str:
            if expires_at:
                try:
                    exp = datetime.fromisoformat(expires_at).astimezone(timezone.utc)
                    if datetime.now(timezone.utc) > exp:
                        return "expired"
                except ValueError:
                    return "expired"  # fail closed
            return "valid"

        assert _check_expiry_safe("never") == "expired"
        assert _check_expiry_safe("2099-12-31T23:59:59+00:00") == "valid"


# ── P2 #13: Debug endpoint constant-time comparison ──────────────────────


class TestDebugTokenComparison:
    """The debug inject endpoint uses `token != expected` (non-constant-time).
    Should use secrets.compare_digest."""

    def test_non_constant_time_comparison(self):
        """Document: string != is not constant-time. Python short-circuits
        on first differing character."""
        expected = "super-secret-debug-token-12345"
        token_wrong = "totally-wrong"
        token_right = expected

        # The current code does:
        assert (token_wrong != expected) is True
        assert (token_right != expected) is False

    def test_constant_time_comparison(self):
        """secrets.compare_digest is the safe alternative."""
        expected = "super-secret-debug-token-12345"
        assert secrets.compare_digest(expected, expected) is True
        assert secrets.compare_digest(expected, "wrong") is False
        assert secrets.compare_digest("", "") is True

    def test_empty_expected_always_rejects(self):
        """When ROVIMEN_DEBUG_TOKEN is unset, expected is '' and the
        `not expected` check rejects before the comparison. This is correct."""
        expected = ""
        token = "anything"
        if not expected or token != expected:
            result = "rejected"
        else:
            result = "accepted"
        assert result == "rejected"

    def test_empty_expected_with_empty_token(self):
        """Even with both empty, `not expected` catches it."""
        expected = ""
        token = ""
        if not expected or token != expected:
            result = "rejected"
        else:
            result = "accepted"
        assert result == "rejected"
