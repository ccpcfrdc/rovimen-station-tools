"""Tests for dashboard/station_keys.py — per-station ingest key store."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from station_keys import (
    SECRET_PREFIX,
    StationKey,
    _KeysFile,
    _invalidate_cache,
    add_key,
    authorizes,
    disable_key,
    is_required,
    list_keys,
    mint_secret,
    validate,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    _invalidate_cache()
    yield
    _invalidate_cache()


@pytest.fixture
def keys_path(tmp_path: Path) -> Path:
    return tmp_path / "station_keys.yaml"


# ── model ─────────────────────────────────────────────────────────────────


class TestStationKeyModel:
    def test_defaults(self):
        k = StationKey(id="gmn0002", station="gmn0002")
        assert k.station == "gmn0002"
        assert k.secret_hash == ""
        assert k.disabled is False
        assert k.rate_limit_override is None

    def test_keysfile_empty(self):
        assert _KeysFile().keys == []


# ── mint_secret ─────────────────────────────────────────────────────────


class TestMintSecret:
    def test_prefix_and_station_tag(self):
        s = mint_secret("gmn0002")
        assert s.startswith(f"{SECRET_PREFIX}_gmn0002_")

    def test_unique(self):
        assert mint_secret("gmn0002") != mint_secret("gmn0002")

    def test_has_256bit_hex_tail(self):
        s = mint_secret("gmnro10")
        tail = s.rsplit("_", 1)[1]
        assert len(tail) == 64
        int(tail, 16)  # must not raise


# ── add_key ───────────────────────────────────────────────────────────────


class TestAddKey:
    def test_creates_key_bound_to_station(self, keys_path: Path):
        key, plaintext = add_key("gmn0002", label="Vaslui", path=keys_path)
        assert key.id == "gmn0002"
        assert key.station == "gmn0002"
        assert key.label == "Vaslui"
        assert key.secret_hash != ""
        assert plaintext.startswith(f"{SECRET_PREFIX}_gmn0002_")

    def test_id_defaults_to_station(self, keys_path: Path):
        key, _ = add_key("gmnro10", path=keys_path)
        assert key.id == "gmnro10"

    def test_distinct_id_for_rotation(self, keys_path: Path):
        add_key("gmn0002", path=keys_path)
        key, _ = add_key("gmn0002", id="gmn0002-2", path=keys_path)
        assert key.id == "gmn0002-2"
        assert key.station == "gmn0002"

    def test_duplicate_enabled_id_raises(self, keys_path: Path):
        add_key("gmn0002", path=keys_path)
        with pytest.raises(ValueError, match="already exists"):
            add_key("gmn0002", path=keys_path)

    def test_reminting_after_disable_ok(self, keys_path: Path):
        add_key("gmn0002", path=keys_path)
        disable_key("gmn0002", path=keys_path)
        # Same id can be reused once the old row is disabled.
        key, _ = add_key("gmn0002", path=keys_path)
        assert key.station == "gmn0002"

    def test_written_yaml_structure(self, keys_path: Path):
        add_key("gmn0002", label="L", path=keys_path)
        raw = yaml.safe_load(keys_path.read_text())
        row = raw["keys"][0]
        assert row["id"] == "gmn0002"
        assert row["station"] == "gmn0002"
        assert row["secret_hash"] != ""
        # plaintext must not be persisted
        assert row["secret"] == ""

    def test_file_mode_0600(self, keys_path: Path):
        add_key("gmn0002", path=keys_path)
        assert (keys_path.stat().st_mode & 0o777) == 0o600


# ── validate / authorizes ──────────────────────────────────────────────────


class TestValidate:
    def test_valid_secret(self, keys_path: Path):
        _, plaintext = add_key("gmn0002", path=keys_path)
        result = validate(plaintext, path=keys_path)
        assert result is not None
        assert result.station == "gmn0002"

    def test_invalid_secret(self, keys_path: Path):
        add_key("gmn0002", path=keys_path)
        assert validate("rvmn_gmn0002_" + "0" * 64, path=keys_path) is None

    def test_empty_secret(self, keys_path: Path):
        add_key("gmn0002", path=keys_path)
        assert validate("", path=keys_path) is None

    def test_disabled_rejected(self, keys_path: Path):
        _, plaintext = add_key("gmn0002", path=keys_path)
        disable_key("gmn0002", path=keys_path)
        assert validate(plaintext, path=keys_path) is None

    def test_missing_file(self, tmp_path: Path):
        assert validate("anything", path=tmp_path / "none.yaml") is None

    def test_legacy_plaintext_migrates(self, keys_path: Path):
        plaintext = mint_secret("gmn0002")
        raw = {
            "keys": [
                {
                    "id": "gmn0002",
                    "station": "gmn0002",
                    "secret": plaintext,
                    "secret_hash": "",
                    "disabled": False,
                }
            ]
        }
        keys_path.write_text(yaml.safe_dump(raw))
        _invalidate_cache()
        result = validate(plaintext, path=keys_path)
        assert result is not None
        _invalidate_cache()
        migrated = yaml.safe_load(keys_path.read_text())["keys"][0]
        assert migrated["secret_hash"] != ""
        assert migrated["secret"] == ""


class TestAuthorizes:
    def test_authorizes_own_station(self, keys_path: Path):
        _, plaintext = add_key("gmn0002", path=keys_path)
        assert authorizes(plaintext, "gmn0002", path=keys_path) is True

    def test_rejects_other_station(self, keys_path: Path):
        """The core §2.2 property: a valid key cannot write another station."""
        _, plaintext = add_key("gmn0002", path=keys_path)
        assert authorizes(plaintext, "gmnro10", path=keys_path) is False

    def test_rejects_invalid_secret(self, keys_path: Path):
        add_key("gmn0002", path=keys_path)
        assert authorizes("garbage", "gmn0002", path=keys_path) is False


# ── disable / list / is_required ────────────────────────────────────────


class TestDisableAndList:
    def test_disable_found(self, keys_path: Path):
        add_key("gmn0002", path=keys_path)
        assert disable_key("gmn0002", path=keys_path) is True
        raw = yaml.safe_load(keys_path.read_text())
        assert raw["keys"][0]["disabled"] is True

    def test_disable_not_found(self, keys_path: Path):
        add_key("gmn0002", path=keys_path)
        assert disable_key("nope", path=keys_path) is False

    def test_list_redacted(self, keys_path: Path):
        add_key("gmn0002", label="Vaslui", path=keys_path)
        listing = list_keys(path=keys_path)
        entry = listing[0]
        assert entry["id"] == "gmn0002"
        assert entry["station"] == "gmn0002"
        assert "secret" not in entry
        assert "secret_hash" not in entry


class TestIsRequired:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv("ROVIMEN_STATION_KEYS_REQUIRED", raising=False)
        assert is_required() is True

    def test_false_when_zero(self, monkeypatch):
        monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "0")
        assert is_required() is False
