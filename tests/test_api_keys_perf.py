"""Tests for api_keys.py validation performance — P2 #15.

P2 #15: validate() walks all keys with pbkdf2 on cache miss.
        First request with N keys is O(N * pbkdf2_iterations).
"""

from __future__ import annotations

import hashlib
import secrets
import time

import pytest


class TestApiKeyValidationCost:
    """Document the O(N) cost of key validation without cache.

    The real api_keys module uses werkzeug's check_password_hash (pbkdf2:sha256,
    600000 iterations). We simulate the cost model here to demonstrate scaling."""

    @staticmethod
    def _simulate_validate(raw_key: str, keys: list[dict]) -> dict | None:
        """Simulate the validate() loop: iterate all keys, compare each."""
        for k in keys:
            if k.get("disabled"):
                continue
            if k.get("secret_hash"):
                # In production: check_password_hash(k['secret_hash'], raw_key)
                # Simulated with a fast hash for test speed
                if hashlib.sha256(raw_key.encode()).hexdigest() == k["secret_hash"]:
                    return k
            elif k.get("secret"):
                if secrets.compare_digest(k["secret"], raw_key):
                    return k
        return None

    def test_correct_key_found(self):
        raw = "test-api-key-123"
        h = hashlib.sha256(raw.encode()).hexdigest()
        keys = [
            {"id": "key1", "secret_hash": hashlib.sha256(b"wrong").hexdigest()},
            {"id": "key2", "secret_hash": h},
        ]
        result = self._simulate_validate(raw, keys)
        assert result is not None
        assert result["id"] == "key2"

    def test_wrong_key_walks_all(self):
        """A wrong key must check every entry — O(N)."""
        keys = [
            {"id": f"key{i}", "secret_hash": hashlib.sha256(f"key{i}".encode()).hexdigest()}
            for i in range(100)
        ]
        result = self._simulate_validate("totally-wrong", keys)
        assert result is None

    def test_disabled_keys_skipped(self):
        raw = "test-key"
        h = hashlib.sha256(raw.encode()).hexdigest()
        keys = [
            {"id": "disabled1", "secret_hash": h, "disabled": True},
            {"id": "active1", "secret_hash": h},
        ]
        result = self._simulate_validate(raw, keys)
        assert result["id"] == "active1"

    def test_plaintext_comparison_constant_time(self):
        """Plaintext keys use secrets.compare_digest (constant-time)."""
        raw = "plaintext-key-abc"
        keys = [{"id": "plain1", "secret": raw}]
        result = self._simulate_validate(raw, keys)
        assert result["id"] == "plain1"

    def test_scaling_cost_documented(self):
        """With N hashed keys, the first request for a new consumer does N comparisons.
        With pbkdf2 at 600k iterations, each comparison costs ~150-300ms.
        N=10 keys → 1.5-3.0s for a single validation on cache miss.
        N=100 keys → 15-30s (unacceptable).

        This test documents the scaling concern. The LRU cache (256 entries)
        mitigates it after warmup, but cold-start or cache-busting attacks
        can trigger the slow path."""
        n_keys = 50
        keys = [
            {"id": f"key{i}", "secret_hash": hashlib.sha256(f"secret{i}".encode()).hexdigest()}
            for i in range(n_keys)
        ]

        t0 = time.monotonic()
        self._simulate_validate("wrong-key", keys)
        elapsed = time.monotonic() - t0

        # With sha256 (fast), this should be < 10ms for 50 keys.
        # With pbkdf2 (600k rounds), this would be 50 * ~200ms = ~10s.
        assert elapsed < 1.0
