"""Regression tests for two undefined-name crashes fixed in this PR.

1. COLOR_METEOR_STACK_FILENAME was referenced in rovimen_dashboard.py but only
   defined as a local inside routes/station_ops.py, causing NameError (and
   silently skipping all RMS-plot prefetch work).  Fix: moved to route_helpers.

2. station_api.py called logger.warning/exception/error without ever defining
   or importing logger, causing NameError at first log call.
   Fix: added import logging + logger = logging.getLogger(__name__).
"""

from __future__ import annotations

import sys
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "dashboard"
STATION_SCRIPTS = REPO_ROOT / "rovimen-scripts"

for _p in (DASHBOARD, STATION_SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class TestColorMeteorStackConstant:
    """COLOR_METEOR_STACK_FILENAME is importable from route_helpers and correct."""

    def test_constant_importable(self) -> None:
        """route_helpers must export COLOR_METEOR_STACK_FILENAME without error."""
        mod = importlib.import_module("route_helpers")
        assert hasattr(mod, "COLOR_METEOR_STACK_FILENAME"), (
            "route_helpers is missing COLOR_METEOR_STACK_FILENAME"
        )

    def test_constant_value_matches_js(self) -> None:
        """Value must match the literal used in all dashboard JS files."""
        mod = importlib.import_module("route_helpers")
        assert mod.COLOR_METEOR_STACK_FILENAME == "__color_meteor_stack__.webp"

    def test_constant_is_string(self) -> None:
        mod = importlib.import_module("route_helpers")
        assert isinstance(mod.COLOR_METEOR_STACK_FILENAME, str)


class TestStationApiLoggerDefined:
    """station_api.py must define logger at module level so log calls don't NameError."""

    def test_logger_attribute_exists(self) -> None:
        """station_api module must expose a logger attribute after import."""
        import logging

        mod = importlib.import_module("station_api")
        assert hasattr(mod, "logger"), (
            "station_api is missing module-level 'logger'"
        )
        assert isinstance(mod.logger, logging.Logger), (
            "station_api.logger must be a logging.Logger instance"
        )

    def test_logger_name(self) -> None:
        """Logger name should be the module name (getLogger(__name__) convention)."""
        mod = importlib.import_module("station_api")
        assert mod.logger.name == "station_api"
