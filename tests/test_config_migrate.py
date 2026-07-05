"""Tests for config_migrate.py — merge new default fields into config.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config_migrate


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def config_env(tmp_path, monkeypatch):
    """Set up a temporary config.json and config_defaults.json.

    Returns a helper object with paths and convenience methods.
    """
    config_path = tmp_path / "config.json"
    defaults_path = tmp_path / "config_defaults.json"

    monkeypatch.setattr(config_migrate, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_migrate, "DEFAULTS_PATH", defaults_path)

    class Env:
        cfg = config_path
        defaults = defaults_path

        @staticmethod
        def write_config(data: dict) -> None:
            config_path.write_text(json.dumps(data, indent=4))

        @staticmethod
        def write_defaults(data: dict) -> None:
            defaults_path.write_text(json.dumps(data, indent=4))

        @staticmethod
        def read_config() -> dict:
            return json.loads(config_path.read_text())

    return Env()


# -- deep_merge ---------------------------------------------------------------

class TestDeepMerge:
    def test_missing_key_filled_from_defaults(self):
        base = {"a": 1}
        defaults = {"a": 1, "b": 2}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert merged == {"a": 1, "b": 2}
        assert "b" in added

    def test_existing_key_not_overwritten(self):
        base = {"a": 1, "b": "original"}
        defaults = {"b": "default"}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert merged["b"] == "original"
        assert added == []

    def test_nested_dicts_recursively_merged(self):
        base = {"outer": {"existing": True}}
        defaults = {"outer": {"existing": False, "new_field": 42}}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert merged["outer"]["existing"] is True  # not overwritten
        assert merged["outer"]["new_field"] == 42
        assert "outer.new_field" in added

    def test_stations_key_never_recursed(self):
        base = {"stations": {"RO000H": {"ip": "10.0.0.1"}}}
        defaults = {"stations": {"DEFAULT": {"ip": "0.0.0.0", "port": 7779}}}
        merged, added = config_migrate.deep_merge(base, defaults)
        # stations should be untouched
        assert "DEFAULT" not in merged["stations"]
        assert merged["stations"]["RO000H"] == {"ip": "10.0.0.1"}
        assert added == []

    def test_type_mismatch_logs_warning_and_skips(self, caplog):
        base = {"key": "i_am_a_string"}
        defaults = {"key": {"nested": True}}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert merged["key"] == "i_am_a_string"
        assert added == []
        assert "Type mismatch" in caplog.text

    def test_returns_correct_added_paths(self):
        base = {"a": 1}
        defaults = {"a": 1, "b": 2, "c": {"d": 3}}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert sorted(added) == ["b", "c"]

    def test_empty_defaults_no_changes(self):
        base = {"a": 1, "b": 2}
        merged, added = config_migrate.deep_merge(base, {})
        assert merged == base
        assert added == []

    def test_empty_base_fills_everything(self):
        defaults = {"a": 1, "b": {"c": 3}}
        merged, added = config_migrate.deep_merge({}, defaults)
        assert merged == defaults
        assert sorted(added) == ["a", "b"]

    def test_deeply_nested_merge(self):
        base = {"level1": {"level2": {"existing": True}}}
        defaults = {"level1": {"level2": {"existing": False, "added": "yes"}}}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert merged["level1"]["level2"]["existing"] is True
        assert merged["level1"]["level2"]["added"] == "yes"
        assert "level1.level2.added" in added

    def test_base_not_mutated(self):
        base = {"a": 1}
        original = base.copy()
        config_migrate.deep_merge(base, {"b": 2})
        assert base == original  # base dict unchanged


# -- migrate -------------------------------------------------------------------

class TestMigrate:
    def test_adds_missing_fields(self, config_env):
        config_env.write_config({"existing": True})
        config_env.write_defaults({"existing": True, "new_key": "value"})
        count = config_migrate.migrate(dry_run=False)
        assert count == 1
        result = config_env.read_config()
        assert result["new_key"] == "value"

    def test_preserves_existing_fields(self, config_env):
        config_env.write_config({"keep_me": "original", "also_keep": 42})
        config_env.write_defaults({"keep_me": "default", "also_keep": 0, "add_me": True})
        config_migrate.migrate(dry_run=False)
        result = config_env.read_config()
        assert result["keep_me"] == "original"
        assert result["also_keep"] == 42

    def test_removes_keys_in_removed_keys(self, config_env, monkeypatch):
        monkeypatch.setattr(config_migrate, "REMOVED_KEYS", ["old_key", "stale_key"])
        monkeypatch.setattr(config_migrate, "REMOVED_NESTED_KEYS", {})
        config_env.write_config({"old_key": 1, "stale_key": 2, "good_key": 3})
        config_env.write_defaults({"good_key": 3})
        config_migrate.migrate(dry_run=False)
        result = config_env.read_config()
        assert "old_key" not in result
        assert "stale_key" not in result
        assert result["good_key"] == 3

    def test_removes_nested_keys(self, config_env, monkeypatch):
        monkeypatch.setattr(config_migrate, "REMOVED_KEYS", [])
        monkeypatch.setattr(
            config_migrate, "REMOVED_NESTED_KEYS",
            {"capabilities": ["vaapi", "vaapi_driver"]},
        )
        config_env.write_config({
            "capabilities": {"vaapi": True, "vaapi_driver": "iHD", "keep": "this"},
        })
        config_env.write_defaults({})
        config_migrate.migrate(dry_run=False)
        result = config_env.read_config()
        assert "vaapi" not in result["capabilities"]
        assert "vaapi_driver" not in result["capabilities"]
        assert result["capabilities"]["keep"] == "this"

    def test_removes_empty_parent_after_nested_purge(self, config_env, monkeypatch):
        monkeypatch.setattr(config_migrate, "REMOVED_KEYS", [])
        monkeypatch.setattr(
            config_migrate, "REMOVED_NESTED_KEYS",
            {"capabilities": ["only_child"]},
        )
        config_env.write_config({"capabilities": {"only_child": True}})
        config_env.write_defaults({})
        config_migrate.migrate(dry_run=False)
        result = config_env.read_config()
        assert "capabilities" not in result

    def test_dry_run_does_not_write(self, config_env):
        config_env.write_config({"existing": True})
        config_env.write_defaults({"existing": True, "new_key": "value"})
        count = config_migrate.migrate(dry_run=True)
        assert count == 1
        # File should still have original content
        result = config_env.read_config()
        assert "new_key" not in result

    def test_missing_config_returns_zero(self, config_env):
        # Only write defaults, no config.json
        config_env.write_defaults({"a": 1})
        assert config_migrate.migrate() == 0

    def test_missing_defaults_returns_zero(self, config_env):
        # Only write config, no defaults
        config_env.write_config({"a": 1})
        assert config_migrate.migrate() == 0

    def test_up_to_date_config_returns_zero(self, config_env):
        config_env.write_config({"a": 1, "b": 2})
        config_env.write_defaults({"a": 1, "b": 2})
        count = config_migrate.migrate(dry_run=False)
        assert count == 0

    def test_atomic_write_uses_tmp_file(self, config_env):
        config_env.write_config({"existing": True})
        config_env.write_defaults({"existing": True, "new_key": "value"})
        config_migrate.migrate(dry_run=False)
        # After successful migration, no .tmp file should remain
        tmp_file = config_env.cfg.with_suffix(".json.tmp")
        assert not tmp_file.exists()

    def test_removed_keys_from_actual_list(self, config_env):
        """Verify the real REMOVED_KEYS list purges matching keys."""
        cfg = {"good": True}
        for k in config_migrate.REMOVED_KEYS[:3]:
            cfg[k] = "should_be_removed"
        config_env.write_config(cfg)
        config_env.write_defaults({"good": True})
        config_migrate.migrate(dry_run=False)
        result = config_env.read_config()
        for k in config_migrate.REMOVED_KEYS[:3]:
            assert k not in result
        assert result["good"] is True
