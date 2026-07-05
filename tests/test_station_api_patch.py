"""Tests for station API PATCH /api/settings validation — P2 #14.

P2 #14: The PATCH endpoint does no validation on the incoming payload.
        Arbitrary keys can be injected into config.json.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest


def _deep_merge(base: dict, override: dict) -> dict:
    """Replicate the _deep_merge logic from station_api.py."""
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
    return base


class TestDeepMerge:
    """The _deep_merge function recursively merges with no validation."""

    def test_simple_update(self):
        base = {"segment_duration": 20, "compression_level": 2}
        patch = {"compression_level": 3}
        result = _deep_merge(base, patch)
        assert result["compression_level"] == 3
        assert result["segment_duration"] == 20

    def test_nested_update(self):
        base = {"services": {"stacker": {"enabled": True}}}
        patch = {"services": {"stacker": {"enabled": False}}}
        result = _deep_merge(base, patch)
        assert result["services"]["stacker"]["enabled"] is False

    def test_inject_arbitrary_key(self):
        """Arbitrary keys can be injected — no allow-list enforcement."""
        base = {"segment_duration": 20}
        patch = {"evil_key": "malicious_value"}
        result = _deep_merge(base, patch)
        assert "evil_key" in result

    def test_inject_nested_key(self):
        """Nested injection into stations dict."""
        base = {
            "stations": {
                "RO000H": {"rms_data_path": "/home/gmn/RMS_data"}
            }
        }
        patch = {
            "stations": {
                "EVIL_STATION": {"rms_data_path": "/etc/passwd"}
            }
        }
        result = _deep_merge(base, patch)
        assert "EVIL_STATION" in result["stations"]

    def test_overwrite_critical_field(self):
        """Overwriting rms_data_path could break all detection processing."""
        base = {
            "stations": {
                "RO000H": {"rms_data_path": "/home/gmn/RMS_data", "rotate": False}
            }
        }
        patch = {
            "stations": {
                "RO000H": {"rms_data_path": "/dev/null"}
            }
        }
        result = _deep_merge(base, patch)
        assert result["stations"]["RO000H"]["rms_data_path"] == "/dev/null"
        # rotate is preserved (deep merge, not replace)
        assert result["stations"]["RO000H"]["rotate"] is False

    def test_null_deletes_key(self):
        """Setting a key to None effectively 'deletes' its value."""
        base = {"stations": {"RO000H": {"rms_data_path": "/home/gmn/RMS_data"}}}
        patch = {"stations": {"RO000H": {"rms_data_path": None}}}
        result = _deep_merge(base, patch)
        assert result["stations"]["RO000H"]["rms_data_path"] is None

    def test_replace_dict_with_scalar(self):
        """Replacing a nested dict with a scalar destroys the subtree."""
        base = {"services": {"stacker": {"enabled": True, "threads": 4}}}
        patch = {"services": "disabled"}
        result = _deep_merge(base, patch)
        assert result["services"] == "disabled"

    def test_empty_patch_no_change(self):
        base = {"segment_duration": 20}
        result = _deep_merge(deepcopy(base), {})
        assert result == base

    def test_deeply_nested_injection(self):
        """5 levels deep — still no validation."""
        base = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        patch = {"a": {"b": {"c": {"d": {"e": 999, "injected": True}}}}}
        result = _deep_merge(base, patch)
        assert result["a"]["b"]["c"]["d"]["injected"] is True
