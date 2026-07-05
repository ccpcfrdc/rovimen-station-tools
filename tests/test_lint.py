"""Lint and syntax validation for all ROVIMEN Python modules.

Ensures every .py file compiles without syntax errors, key modules import
cleanly, and expected public APIs exist.
"""

from __future__ import annotations

import importlib
import py_compile
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories to scan for .py files.
SCAN_DIRS = [
    REPO_ROOT / "rovimen-scripts",
    REPO_ROOT / "dashboard",
    REPO_ROOT / "tools",
    REPO_ROOT / "live_wallpaper",
]

# Directories to skip entirely (upstream code, caches).
SKIP_NAMES = {"__pycache__", "RMS", ".venv", ".git"}


def _discover_py_files() -> list[Path]:
    """Return all .py files under SCAN_DIRS, skipping excluded directories."""
    files: list[Path] = []
    for scan_dir in SCAN_DIRS:
        if not scan_dir.is_dir():
            continue
        for py_file in sorted(scan_dir.rglob("*.py")):
            # Skip if any path component is in SKIP_NAMES
            if SKIP_NAMES & set(py_file.relative_to(REPO_ROOT).parts):
                continue
            files.append(py_file)
    return files


ALL_PY_FILES = _discover_py_files()


def _file_id(path: Path) -> str:
    """Short label for parametrize IDs: 'dashboard/models.py'."""
    return str(path.relative_to(REPO_ROOT))


# ---------------------------------------------------------------------------
# 1. Syntax validation
# ---------------------------------------------------------------------------

class TestSyntaxValidation:
    """Every .py file must compile without syntax errors."""

    @pytest.mark.parametrize("py_file", ALL_PY_FILES, ids=_file_id)
    def test_file_compiles(self, py_file: Path) -> None:
        """Compile a single Python file and assert no SyntaxError."""
        py_compile.compile(str(py_file), doraise=True)


# ---------------------------------------------------------------------------
# 2. Import validation
# ---------------------------------------------------------------------------

# (module_name, parent_dir) -- parent_dir ensures sys.path contains the
# correct directory so top-level scripts can be imported by filename stem.
IMPORTABLE_MODULES: list[tuple[str, str]] = [
    # rovimen-scripts
    ("flags_manager", "rovimen-scripts"),
    ("rovimen_lock", "rovimen-scripts"),
    ("overlay", "rovimen-scripts"),
    ("color_calibration", "rovimen-scripts"),
    ("config_migrate", "rovimen-scripts"),
    ("detection_lock", "rovimen-scripts"),
    ("encoder", "rovimen-scripts"),
    ("archive_upload", "rovimen-scripts"),
    ("timelapse_build", "rovimen-scripts"),
    # dashboard
    ("models", "dashboard"),
    ("api_keys", "dashboard"),
    ("coverage", "dashboard"),
    ("usage_stats", "dashboard"),
    ("security", "dashboard"),
    ("route_helpers", "dashboard"),
]


class TestModuleImports:
    """Key modules must import without errors."""

    @pytest.mark.parametrize(
        "mod_name,parent",
        IMPORTABLE_MODULES,
        ids=[m for m, _ in IMPORTABLE_MODULES],
    )
    def test_import(self, mod_name: str, parent: str) -> None:
        parent_path = str(REPO_ROOT / parent)
        if parent_path not in sys.path:
            sys.path.insert(0, parent_path)
        try:
            importlib.import_module(mod_name)
        except Exception as exc:
            # Allow graceful skip for missing system resources (fonts,
            # hardware devices, display servers, etc.)
            msg = str(exc)
            system_errors = (
                "font", "display", "DISPLAY", "device", "Xlib",
                "No module named 'gi'", "wayland", "libGL",
            )
            if any(tok in msg for tok in system_errors):
                pytest.skip(f"Skipped due to missing system resource: {msg}")
            raise


# ---------------------------------------------------------------------------
# 3. Module-level sanity checks -- public API surface
# ---------------------------------------------------------------------------

# Mapping: module name -> list of expected attributes (functions or classes).
PUBLIC_API: dict[str, list[str]] = {
    "flags_manager": [
        "load", "save", "mark_ready", "lock_chunk", "unlock_chunk",
    ],
    "rovimen_lock": [
        "lock", "unlock", "is_locked", "get_lock_info",
    ],
    "overlay": [
        "build_drawtext_annotations", "measure_text_width",
    ],
    "color_calibration": [
        "derive_adaptive_gains", "apply_calibration_np", "build_ffmpeg_filter",
    ],
    "encoder": [
        "process_chunk", "process_night", "_build_cmd", "_cpu_filter_chain",
    ],
    "archive_upload": [
        "rsync_file", "rsync_batch", "run_night", "run_once",
    ],
    "detection_lock": [
        "process_night", "_parse_ftpdetectinfo",
    ],
    "config_migrate": [
        "deep_merge", "migrate",
    ],
    "models": [
        "CameraConfig", "StationConfig", "DashboardConfig", "UserConfig",
    ],
    "api_keys": [
        "validate", "add_key", "disable_key", "mint_secret",
    ],
    "coverage": [
        "compute_coverage_stats", "camera_footprint", "camera_footprints_geojson",
    ],
    "security": [
        "verify_totp", "generate_totp_secret", "add_security_headers",
    ],
    "usage_stats": [
        "compute_usage", "compute_user_activity",
    ],
    "route_helpers": [
        "COLOR_METEOR_STACK_FILENAME",
        "lookup_station",
        "proxy_get",
        "media_url",
    ],
}


def _api_cases() -> list[tuple[str, str]]:
    """Flatten PUBLIC_API into (module, attr) pairs for parametrize."""
    cases = []
    for mod, attrs in PUBLIC_API.items():
        for attr in attrs:
            cases.append((mod, attr))
    return cases


_API_CASES = _api_cases()


class TestPublicAPI:
    """Imported modules expose the expected public functions and classes."""

    @pytest.mark.parametrize(
        "mod_name,attr",
        _API_CASES,
        ids=[f"{m}.{a}" for m, a in _API_CASES],
    )
    def test_has_attribute(self, mod_name: str, attr: str) -> None:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:
            msg = str(exc)
            system_errors = (
                "font", "display", "DISPLAY", "device", "Xlib",
                "No module named 'gi'", "wayland", "libGL",
            )
            if any(tok in msg for tok in system_errors):
                pytest.skip(f"Skipped due to missing system resource: {msg}")
            raise

        assert hasattr(mod, "__name__"), f"{mod_name} missing __name__"
        assert hasattr(mod, attr), (
            f"{mod_name} missing expected attribute '{attr}'"
        )
