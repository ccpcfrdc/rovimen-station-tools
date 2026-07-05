"""Tests for dashboard/api_keys.py — API key store for the public API."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from api_keys import (
    ApiKey,
    _KeysFile,
    _invalidate_cache,
    add_key,
    disable_key,
    is_required,
    list_keys,
    mint_secret,
    validate,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure a fresh cache for every test."""
    _invalidate_cache()
    yield
    _invalidate_cache()


@pytest.fixture
def keys_path(tmp_path: Path) -> Path:
    return tmp_path / "api_keys.yaml"


# ── ApiKey model ─────────────────────────────────────────────────────────


class TestApiKeyModel:
    def test_creation_defaults(self):
        key = ApiKey(id="test-key")
        assert key.id == "test-key"
        assert key.secret_hash == ""
        assert key.secret == ""
        assert key.label == ""
        assert key.created_at == ""
        assert key.disabled is False
        assert key.rate_limit_override is None

    def test_full_creation(self):
        key = ApiKey(
            id="prod",
            secret_hash="pbkdf2:sha256:600000$...",
            label="production key",
            created_at="2026-05-24T13:30:00Z",
            disabled=False,
            rate_limit_override="100/minute",
        )
        assert key.label == "production key"
        assert key.rate_limit_override == "100/minute"


class TestKeysFile:
    def test_empty(self):
        kf = _KeysFile()
        assert kf.keys == []

    def test_with_keys(self):
        kf = _KeysFile(keys=[ApiKey(id="a"), ApiKey(id="b")])
        assert len(kf.keys) == 2


# ── mint_secret ──────────────────────────────────────────────────────────


class TestMintSecret:
    def test_length(self):
        s = mint_secret()
        assert len(s) == 64

    def test_hex(self):
        s = mint_secret()
        int(s, 16)  # must not raise

    def test_unique(self):
        a = mint_secret()
        b = mint_secret()
        assert a != b


# ── add_key ──────────────────────────────────────────────────────────────


class TestAddKey:
    def test_creates_new_key(self, keys_path: Path):
        key, plaintext = add_key("my-app", label="My App", path=keys_path)
        assert key.id == "my-app"
        assert key.label == "My App"
        assert key.secret_hash != ""
        assert key.disabled is False
        assert len(plaintext) == 64

    def test_returns_valid_plaintext(self, keys_path: Path):
        key, plaintext = add_key("v1", path=keys_path)
        result = validate(plaintext, path=keys_path)
        assert result is not None
        assert result.id == "v1"

    def test_duplicate_id_raises(self, keys_path: Path):
        add_key("dup", path=keys_path)
        with pytest.raises(ValueError, match="already exists"):
            add_key("dup", path=keys_path)

    def test_creates_parent_directory(self, tmp_path: Path):
        deep_path = tmp_path / "nested" / "dir" / "api_keys.yaml"
        key, _ = add_key("deep", path=deep_path)
        assert deep_path.exists()
        assert key.id == "deep"

    def test_written_yaml_structure(self, keys_path: Path):
        add_key("struct-test", label="Label", path=keys_path)
        raw = yaml.safe_load(keys_path.read_text())
        assert "keys" in raw
        assert len(raw["keys"]) == 1
        row = raw["keys"][0]
        assert row["id"] == "struct-test"
        assert row["label"] == "Label"
        assert row["secret_hash"] != ""
        assert row["disabled"] is False

    def test_multiple_keys(self, keys_path: Path):
        add_key("k1", path=keys_path)
        add_key("k2", path=keys_path)
        raw = yaml.safe_load(keys_path.read_text())
        assert len(raw["keys"]) == 2

    def test_rate_limit_override(self, keys_path: Path):
        key, _ = add_key("rl", rate_limit_override="10/second", path=keys_path)
        assert key.rate_limit_override == "10/second"


# ── validate ─────────────────────────────────────────────────────────────


class TestValidate:
    def test_valid_secret(self, keys_path: Path):
        _, plaintext = add_key("valid", path=keys_path)
        result = validate(plaintext, path=keys_path)
        assert result is not None
        assert result.id == "valid"

    def test_invalid_secret(self, keys_path: Path):
        add_key("exists", path=keys_path)
        result = validate("0" * 64, path=keys_path)
        assert result is None

    def test_empty_secret(self, keys_path: Path):
        add_key("exists2", path=keys_path)
        assert validate("", path=keys_path) is None

    def test_disabled_key_rejected(self, keys_path: Path):
        _, plaintext = add_key("dis", path=keys_path)
        disable_key("dis", path=keys_path)
        result = validate(plaintext, path=keys_path)
        assert result is None

    def test_missing_file(self, tmp_path: Path):
        result = validate("anything", path=tmp_path / "nonexistent.yaml")
        assert result is None

    def test_legacy_plaintext_validation_and_migration(self, keys_path: Path):
        """A key stored with plaintext 'secret' field should validate and
        be migrated to 'secret_hash' on the spot."""
        plaintext = mint_secret()
        raw = {
            "keys": [
                {
                    "id": "legacy",
                    "secret": plaintext,
                    "secret_hash": "",
                    "label": "legacy key",
                    "created_at": "2026-01-01T00:00:00Z",
                    "disabled": False,
                    "rate_limit_override": None,
                }
            ]
        }
        keys_path.write_text(yaml.safe_dump(raw))
        _invalidate_cache()

        result = validate(plaintext, path=keys_path)
        assert result is not None
        assert result.id == "legacy"

        # After migration the file should have secret_hash set and secret cleared
        _invalidate_cache()
        migrated = yaml.safe_load(keys_path.read_text())
        row = migrated["keys"][0]
        assert row["secret_hash"] != ""
        assert row["secret"] == ""


# ── disable_key ──────────────────────────────────────────────────────────


class TestDisableKey:
    def test_found_and_disabled(self, keys_path: Path):
        add_key("to-disable", path=keys_path)
        assert disable_key("to-disable", path=keys_path) is True
        raw = yaml.safe_load(keys_path.read_text())
        assert raw["keys"][0]["disabled"] is True

    def test_not_found(self, keys_path: Path):
        add_key("other", path=keys_path)
        assert disable_key("missing", path=keys_path) is False


# ── list_keys ────────────────────────────────────────────────────────────


class TestListKeys:
    def test_redacted_listing(self, keys_path: Path):
        add_key("list-me", label="Visible", path=keys_path)
        listing = list_keys(path=keys_path)
        assert len(listing) == 1
        entry = listing[0]
        assert entry["id"] == "list-me"
        assert entry["label"] == "Visible"
        assert "secret" not in entry
        assert "secret_hash" not in entry

    def test_empty_for_missing_file(self, tmp_path: Path):
        listing = list_keys(path=tmp_path / "missing.yaml")
        assert listing == []

    def test_multiple_entries(self, keys_path: Path):
        add_key("a", path=keys_path)
        add_key("b", path=keys_path)
        listing = list_keys(path=keys_path)
        assert len(listing) == 2
        ids = {e["id"] for e in listing}
        assert ids == {"a", "b"}


# ── is_required ──────────────────────────────────────────────────────────


class TestIsRequired:
    def test_defaults_to_true(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ROVIMEN_API_KEYS_REQUIRED", raising=False)
        assert is_required() is True

    def test_false_when_env_zero(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "0")
        assert is_required() is False

    def test_true_when_env_one(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "1")
        assert is_required() is True

    def test_true_for_arbitrary_value(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "yes")
        assert is_required() is True
