"""Path-traversal tests for route_helpers.validate_media_params and
public_api._resolve_media_path.

The two functions form the input-validation + resolve-and-confirm chain for
every media URL the public API serves. A bypass would let an unauthenticated
caller read arbitrary files from the VPS filesystem. These tests lock in the
rejection behaviour and guard against future regex weakening.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))


# ── validate_media_params (route_helpers) ─────────────────────────────────

import route_helpers  # noqa: E402  (sys.path must be set first)


class _FakeConfig:
    """Minimal stand-in for the DashboardConfig object."""

    def __init__(self, host_keys: list[str]):
        self.stations = {k: object() for k in host_keys}


def _fake_config(host_keys: list[str] | None = None) -> _FakeConfig:
    return _FakeConfig(host_keys or ["goodhost"])


class TestValidateMediaParams:
    """route_helpers.validate_media_params raises HTTP 400/404 on bad inputs."""

    def _call(
        self,
        host_key: str = "goodhost",
        station_code: str = "RO000A",
        date: str = "20260101",
        filename: str = "RO000A_20260101_010203_color.mkv",
        ext_re: str = r".*\.mkv$",
        config=None,
    ):
        cfg = config or _fake_config()
        with Flask(__name__).test_request_context("/"):
            route_helpers.validate_media_params(
                cfg, host_key, station_code, date, filename, ext_re
            )

    def test_valid_params_pass(self):
        self._call()  # must not raise

    def test_unknown_host_raises_404(self):
        with pytest.raises(Exception) as exc_info:
            self._call(host_key="unknownhost")
        assert exc_info.value.code == 404

    def test_dotdot_in_station_code_raises_400(self):
        with pytest.raises(Exception) as exc_info:
            self._call(station_code="../evil")
        assert exc_info.value.code == 400

    def test_dotdot_in_date_raises_400(self):
        with pytest.raises(Exception) as exc_info:
            self._call(date="../etc")
        assert exc_info.value.code == 400

    def test_dotdot_in_filename_raises_400(self):
        with pytest.raises(Exception) as exc_info:
            self._call(filename="../etc/passwd")
        assert exc_info.value.code == 400

    def test_slash_in_station_code_raises_400(self):
        with pytest.raises(Exception) as exc_info:
            self._call(station_code="RO000A/extra")
        assert exc_info.value.code == 400

    def test_slash_in_date_raises_400(self):
        with pytest.raises(Exception) as exc_info:
            self._call(date="2026/01/01")
        assert exc_info.value.code == 400

    def test_slash_in_filename_raises_400(self):
        with pytest.raises(Exception) as exc_info:
            self._call(filename="sub/dir/file.mkv")
        assert exc_info.value.code == 400

    def test_non_alphanumeric_station_code_raises_400(self):
        with pytest.raises(Exception) as exc_info:
            self._call(station_code="RO!000A")
        assert exc_info.value.code == 400

    def test_non_8digit_date_raises_400(self):
        with pytest.raises(Exception) as exc_info:
            self._call(date="20260101X")
        assert exc_info.value.code == 400

    def test_filename_not_matching_ext_re_raises_400(self):
        with pytest.raises(Exception) as exc_info:
            self._call(
                filename="RO000A_20260101_010203_color.mkv",
                ext_re=r".*\.mp4$",
            )
        assert exc_info.value.code == 400


# ── _resolve_media_path (public_api) ──────────────────────────────────────


class TestResolveMediaPath:
    """public_api._resolve_media_path must reject traversal and honour
    ARCHIVE_PATH containment, and only return a value for files that exist."""

    @pytest.fixture(autouse=True)
    def _patch_archive(self, tmp_path):
        """Point ARCHIVE_PATH at a temp directory for isolation."""
        import public_api
        import rovimen_dashboard as rd
        self._archive = tmp_path / "archive"
        self._archive.mkdir()
        with patch.object(rd, "ARCHIVE_PATH", self._archive):
            yield

    def _resolve(
        self,
        camera: str = "RO000A",
        date_iso: str = "2026-01-01",
        filename: str = "RO000A_20260101_010203_color.mkv",
        subdir: str = "meteors",
    ):
        import public_api
        return public_api._resolve_media_path(camera, date_iso, filename, subdir)

    def _plant_file(
        self,
        camera: str = "RO000A",
        date_compact: str = "20260101",
        subdir: str = "meteors",
        filename: str = "RO000A_20260101_010203_color.mkv",
    ) -> Path:
        """Create a real file inside the fake archive so is_file() passes."""
        p = self._archive / camera / date_compact / subdir / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake video data")
        return p

    def test_valid_path_resolves(self):
        self._plant_file()
        result = self._resolve()
        assert result is not None
        assert result.is_file()

    def test_file_not_exist_returns_none(self):
        # No file planted — should return None
        result = self._resolve()
        assert result is None

    def test_dotdot_in_camera_rejected(self):
        result = self._resolve(camera="../etc")
        assert result is None

    def test_dotdot_in_date_rejected(self):
        result = self._resolve(date_iso="../etc/passwd")
        assert result is None

    def test_dotdot_in_filename_rejected(self):
        result = self._resolve(filename="../../etc/passwd")
        assert result is None

    def test_encoded_slash_in_filename_rejected(self):
        # URL-decoded slashes — the regex should reject these
        result = self._resolve(filename="sub%2Fdir%2Ffile.mkv")
        assert result is None

    def test_null_byte_in_filename_rejected(self):
        result = self._resolve(filename="file\x00.mkv")
        assert result is None

    def test_unknown_subdir_rejected(self):
        result = self._resolve(subdir="../../etc")
        assert result is None

    def test_sibling_prefix_path_rejected(self, tmp_path):
        """A sibling directory named 'rovimen-evil' must not match startswith
        on 'rovimen'. Path.is_relative_to is used — not string prefix compare."""
        import rovimen_dashboard as rd

        sibling = tmp_path / "archive-evil"
        sibling.mkdir()
        evil_file = sibling / "RO000A" / "20260101" / "meteors" / "RO000A_20260101_010203_color.mkv"
        evil_file.parent.mkdir(parents=True, exist_ok=True)
        evil_file.write_bytes(b"evil")

        # The camera regex only allows alphanumeric so we can't inject the
        # sibling path via camera= directly. Instead verify the archive root
        # check catches any future regex gap: plant a symlink from inside the
        # archive pointing outside.
        link_target = evil_file
        link_inside = self._archive / "RO000A" / "20260101" / "meteors"
        link_inside.mkdir(parents=True, exist_ok=True)
        symlink = link_inside / "RO000A_20260101_010203_color.mkv"
        symlink.symlink_to(link_target)

        # resolve() follows symlinks; is_relative_to must fail for the outside path
        import public_api
        result = public_api._resolve_media_path(
            "RO000A", "2026-01-01", "RO000A_20260101_010203_color.mkv", "meteors"
        )
        # The resolved path exits the archive root — must be rejected
        assert result is None

    def test_valid_camera_code_variants(self):
        """All known camera code formats pass the regex."""
        for code in ("RO000A", "DE001B", "RO0017", "TST001"):
            self._plant_file(camera=code, filename=f"{code}_20260101_010203_color.mkv")
            result = self._resolve(camera=code, filename=f"{code}_20260101_010203_color.mkv")
            assert result is not None, f"Expected {code!r} to resolve"

    def test_invalid_date_formats_rejected(self):
        for bad_date in ("2026-1-1", "20260101", "2026/01/01", "notadate"):
            result = self._resolve(date_iso=bad_date)
            assert result is None, f"Expected {bad_date!r} to be rejected"
