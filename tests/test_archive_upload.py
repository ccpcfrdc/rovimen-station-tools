"""Tests for archive_upload.py — station-side archive uploader."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import archive_upload
from archive_upload import _Uploader, _ScanResults, _is_locked, _ssh_opts, rsync_batch, rsync_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mkv(parent: Path, name: str, size: int = 1024) -> Path:
    """Create a dummy MKV file."""
    f = parent / name
    f.write_bytes(b'\x00' * size)
    return f


def _make_state(night_dir: Path, state: dict) -> Path:
    """Write state.json inside a night dir."""
    sj = night_dir / 'state.json'
    sj.write_text(json.dumps(state, indent=2))
    return sj


def _archive_cfg(tmp_path: Path, *, enabled: bool = True,
                 stations: dict | None = None, **overrides) -> dict:
    """Build a minimal config dict with an archive block."""
    capture = tmp_path / 'color_capture'
    capture.mkdir(exist_ok=True)
    cfg = {
        'videocapture_path': str(capture),
        'stations': stations or {'RO000H': {}},
        'archive': {
            'enabled': enabled,
            'host': '10.0.0.1',
            'port': 2222,
            'user': 'rovimen',
            'base_path': '/mnt/archive',
            'upload_meteors': True,
            'upload_timelapses': True,
            'upload_stacks': False,
            **overrides,
        },
    }
    return cfg


# ---------------------------------------------------------------------------
# _is_locked
# ---------------------------------------------------------------------------

class TestIsLocked:

    def test_locked_via_state_json(self, tmp_path):
        """When state.json records a lock, _is_locked returns True."""
        cfg = {'videocapture_path': str(tmp_path / 'color_capture')}
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {mkv.name: {'lock': {'lock_type': 'detection'}}},
        })
        assert _is_locked(mkv, 'RO000H', '20260315', cfg) is True

    def test_not_locked_via_state_json(self, tmp_path):
        """When state.json records lock=None, _is_locked returns False (no sidecar)."""
        cfg = {'videocapture_path': str(tmp_path / 'color_capture')}
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {mkv.name: {'lock': None}},
        })
        assert _is_locked(mkv, 'RO000H', '20260315', cfg) is False

    def test_fallback_to_sidecar(self, tmp_path):
        """When state.json is missing, falls back to .locked sidecar."""
        cfg = {'videocapture_path': str(tmp_path / 'color_capture')}
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        # No state.json — create .locked sidecar
        (night / (mkv.name + '.locked')).write_text('{}')
        assert _is_locked(mkv, 'RO000H', '20260315', cfg) is True

    def test_fallback_no_sidecar(self, tmp_path):
        """When neither state.json nor sidecar exist, returns False."""
        cfg = {'videocapture_path': str(tmp_path / 'color_capture')}
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        assert _is_locked(mkv, 'RO000H', '20260315', cfg) is False

    def test_state_json_chunk_not_listed(self, tmp_path):
        """Chunk not in state.json falls through to sidecar check."""
        cfg = {'videocapture_path': str(tmp_path / 'color_capture')}
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {},  # chunk not listed
        })
        # No sidecar either
        assert _is_locked(mkv, 'RO000H', '20260315', cfg) is False


# ---------------------------------------------------------------------------
# _ssh_opts
# ---------------------------------------------------------------------------

class TestSshOpts:

    def test_basic(self):
        opts = _ssh_opts(22, '')
        assert '-p' in opts
        assert '22' in opts
        assert '-o' in opts
        assert 'BatchMode=yes' in opts
        # No -i when ssh_key is empty
        assert '-i' not in opts

    def test_with_ssh_key(self):
        opts = _ssh_opts(2222, '/path/to/key')
        assert '-i' in opts
        assert '/path/to/key' in opts
        assert '2222' in opts

    def test_connect_timeout(self):
        opts = _ssh_opts(22, '')
        assert 'ConnectTimeout=10' in opts

    def test_server_alive(self):
        opts = _ssh_opts(22, '')
        assert 'ServerAliveInterval=15' in opts
        assert 'ServerAliveCountMax=3' in opts

    def test_controlmaster_options(self):
        opts = _ssh_opts(22, '')
        assert 'ControlMaster=auto' in opts
        assert 'ControlPath=/tmp/rovimen-ssh-%r@%h:%p' in opts
        assert 'ControlPersist=300' in opts


# ---------------------------------------------------------------------------
# _Uploader.__init__
# ---------------------------------------------------------------------------

class TestUploaderInit:

    def test_enabled_defaults_false(self, tmp_path):
        """When archive block is missing, enabled defaults to False."""
        cfg = {'videocapture_path': str(tmp_path)}
        up = _Uploader(cfg)
        assert up.enabled is False

    def test_enabled_from_config(self, tmp_path):
        cfg = _archive_cfg(tmp_path, enabled=True)
        up = _Uploader(cfg)
        assert up.enabled is True

    def test_config_values(self, tmp_path):
        cfg = _archive_cfg(tmp_path, host='vps.example.com', port=2222,
                           user='deployer', base_path='/data/rovimen',
                           ssh_key='/keys/id_rsa')
        up = _Uploader(cfg)
        assert up.host == 'vps.example.com'
        assert up.port == 2222
        assert up.user == 'deployer'
        assert up.base_path == '/data/rovimen'
        assert up.ssh_key == '/keys/id_rsa'

    def test_upload_flags_defaults(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        assert up.upload_meteors is True
        assert up.upload_timelapses is True
        assert up.upload_stacks is False

    def test_videocapture_path_fallback_color_video(self, tmp_path):
        cfg = {
            'color_video_path': str(tmp_path / 'cv'),
            'archive': {'enabled': True},
            'stations': {},
        }
        up = _Uploader(cfg)
        assert up.videocapture_path == tmp_path / 'cv'

    def test_videocapture_path_fallback_reenc(self, tmp_path):
        cfg = {
            'reenc_path': str(tmp_path / 'reenc'),
            'archive': {'enabled': True},
            'stations': {},
        }
        up = _Uploader(cfg)
        assert up.videocapture_path == tmp_path / 'reenc'

    def test_videocapture_path_fallback_color_capture(self, tmp_path):
        cfg = {
            'color_capture_path': str(tmp_path / 'cc'),
            'archive': {'enabled': True},
            'stations': {},
        }
        up = _Uploader(cfg)
        assert up.videocapture_path == tmp_path / 'cc'

    def test_videocapture_path_fallback_ssd(self, tmp_path):
        cfg = {
            'ssd_color_path': str(tmp_path / 'ssd'),
            'archive': {'enabled': True},
            'stations': {},
        }
        up = _Uploader(cfg)
        assert up.videocapture_path == tmp_path / 'ssd'

    def test_videocapture_path_ultimate_fallback(self):
        cfg = {'archive': {'enabled': True}, 'stations': {}}
        up = _Uploader(cfg)
        assert up.videocapture_path == Path.home() / 'color_capture'


# ---------------------------------------------------------------------------
# _Uploader._mark_uploaded
# ---------------------------------------------------------------------------

class TestMarkUploaded:

    def test_color_mkv(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        mkv = Path('/tmp/RO000H_20260315_210000_color.mkv')
        with patch('flags_manager.mark_chunk_uploaded') as mock:
            up._mark_uploaded(mkv, 'RO000H', '20260315')
            mock.assert_called_once_with('RO000H', '20260315', mkv.name, cfg)

    def test_night_stack_webp(self, tmp_path):
        """_night_stack.webp dispatches to mark_night_stack_uploaded."""
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        ns = Path('/tmp/RO000H_20260315_night_stack.webp')
        with patch('flags_manager.mark_night_stack_uploaded') as mock:
            up._mark_uploaded(ns, 'RO000H', '20260315')
            mock.assert_called_once_with('RO000H', '20260315', cfg)

    def test_chunk_stack_webp(self, tmp_path):
        """Regular _stack.webp dispatches to mark_chunk_stack_uploaded."""
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        stack = Path('/tmp/RO000H_20260315_210000_stack.webp')
        with patch('flags_manager.mark_chunk_stack_uploaded') as mock:
            up._mark_uploaded(stack, 'RO000H', '20260315')
            chunk_name = 'RO000H_20260315_210000_color.mkv'
            mock.assert_called_once_with('RO000H', '20260315', chunk_name, cfg)

    def test_timelapse_mp4(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        tl = Path('/tmp/RO000H_20260315_timelapse.mp4')
        with patch('flags_manager.mark_timelapse_uploaded') as mock:
            up._mark_uploaded(tl, 'RO000H', '20260315')
            mock.assert_called_once_with('RO000H', '20260315', cfg)

    def test_night_stack_checked_before_regular_stack(self, tmp_path):
        """Night stack suffix _night_stack.webp ends with _stack.webp too.

        Ensure the night stack branch is hit, not the regular stack branch.
        """
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        ns = Path('/tmp/RO000H_20260315_night_stack.webp')
        with patch('flags_manager.mark_night_stack_uploaded') as ns_mock, \
             patch('flags_manager.mark_chunk_stack_uploaded') as cs_mock:
            up._mark_uploaded(ns, 'RO000H', '20260315')
            ns_mock.assert_called_once()
            cs_mock.assert_not_called()

    def test_oserror_is_logged_not_raised(self, tmp_path):
        """OSError in flags_manager is caught and logged."""
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        mkv = Path('/tmp/RO000H_20260315_210000_color.mkv')
        with patch('flags_manager.mark_chunk_uploaded', side_effect=OSError('disk full')):
            # Should not raise
            up._mark_uploaded(mkv, 'RO000H', '20260315')


# ---------------------------------------------------------------------------
# _Uploader._fmt_size
# ---------------------------------------------------------------------------

class TestFmtSize:

    def test_gb_range(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        assert up._fmt_size(2 * 1024 ** 3) == '2.0 GB'
        assert up._fmt_size(int(1.5 * 1024 ** 3)) == '1.5 GB'

    def test_mb_range(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        assert up._fmt_size(150 * 1024 ** 2) == '150 MB'
        assert up._fmt_size(1 * 1024 ** 2) == '1 MB'

    def test_kb_range(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        assert up._fmt_size(500 * 1024) == '500 KB'
        assert up._fmt_size(1024) == '1 KB'

    def test_boundary_gb(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        # Exactly 1 GB
        assert up._fmt_size(1024 ** 3) == '1.0 GB'

    def test_boundary_mb(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        # Exactly 1 MB
        assert up._fmt_size(1024 ** 2) == '1 MB'


# ---------------------------------------------------------------------------
# _Uploader._scan_locked_mkvs
# ---------------------------------------------------------------------------

class TestScanLockedMkvs:

    def test_returns_locked_reencoded_not_uploaded(self, tmp_path):
        """MKV that is locked + reencoded + not uploaded should appear in results."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {
                mkv.name: {
                    'lock': {'lock_type': 'detection'},
                    'reencoded': True,
                    'uploaded': False,
                    'stack_uploaded': False,
                },
            },
        })
        up = _Uploader(cfg)
        results = up._scan_locked_mkvs()
        assert len(results) == 1
        assert results[0][0] == mkv
        assert results[0][1] == 'RO000H'
        assert results[0][2] == '20260315'

    def test_skips_already_uploaded(self, tmp_path):
        """MKV already marked as uploaded should not appear."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {mkv.name: {
                'lock': {'lock_type': 'detection'},
                'reencoded': True,
                'uploaded': True,
            }},
        })
        up = _Uploader(cfg)
        assert up._scan_locked_mkvs() == []

    def test_skips_unlocked(self, tmp_path):
        """MKV with lock=None should not appear."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {mkv.name: {
                'lock': None,
                'reencoded': True,
                'uploaded': False,
            }},
        })
        up = _Uploader(cfg)
        assert up._scan_locked_mkvs() == []

    def test_skips_not_reencoded(self, tmp_path):
        """MKV that is locked but not reencoded should not appear."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {mkv.name: {
                'lock': {'lock_type': 'detection'},
                'reencoded': False,
                'uploaded': False,
            }},
        })
        up = _Uploader(cfg)
        assert up._scan_locked_mkvs() == []

    def test_skips_non_8digit_dirs(self, tmp_path):
        """Directories that don't look like YYYYMMDD dates are skipped."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        bad_dir = capture / 'RO000H' / 'notes'
        bad_dir.mkdir(parents=True)
        _make_mkv(bad_dir, 'RO000H_20260315_210000_color.mkv')
        up = _Uploader(cfg)
        assert up._scan_locked_mkvs() == []

    def test_skips_nonexistent_station_dir(self, tmp_path):
        """Station dir not on disk is silently skipped."""
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        assert up._scan_locked_mkvs() == []

    def test_multiple_stations_and_dates(self, tmp_path):
        """Scan across multiple stations and dates."""
        cfg = _archive_cfg(tmp_path, stations={'RO000H': {}, 'RO000J': {}})
        capture = tmp_path / 'color_capture'
        for station, date in [('RO000H', '20260315'), ('RO000J', '20260316')]:
            night = capture / station / date
            night.mkdir(parents=True)
            mkv = _make_mkv(night, f'{station}_{date}_210000_color.mkv')
            _make_state(night, {
                'station': station, 'date': date,
                'chunks': {mkv.name: {
                    'lock': {'lock_type': 'detection'},
                    'reencoded': True,
                    'uploaded': False,
                }},
            })
        up = _Uploader(cfg)
        results = up._scan_locked_mkvs()
        assert len(results) == 2
        stations_found = {r[1] for r in results}
        assert stations_found == {'RO000H', 'RO000J'}


# ---------------------------------------------------------------------------
# _Uploader._scan_stacks
# ---------------------------------------------------------------------------

class TestScanStacks:

    def test_finds_unuploaded_stacks(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        stacks = capture / 'RO000H' / '20260315' / 'stacks'
        stacks.mkdir(parents=True)
        webp = stacks / 'RO000H_20260315_210000_stack.webp'
        webp.write_bytes(b'\x00' * 100)
        night = capture / 'RO000H' / '20260315'
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {'RO000H_20260315_210000_color.mkv': {
                'stack_uploaded': False,
            }},
        })
        up = _Uploader(cfg)
        results = up._scan_stacks()
        assert len(results) == 1
        assert results[0][0] == webp

    def test_skips_uploaded_stacks(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        stacks = capture / 'RO000H' / '20260315' / 'stacks'
        stacks.mkdir(parents=True)
        webp = stacks / 'RO000H_20260315_210000_stack.webp'
        webp.write_bytes(b'\x00' * 100)
        night = capture / 'RO000H' / '20260315'
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {'RO000H_20260315_210000_color.mkv': {
                'stack_uploaded': True,
            }},
        })
        up = _Uploader(cfg)
        assert up._scan_stacks() == []


# ---------------------------------------------------------------------------
# _Uploader._scan_timelapses
# ---------------------------------------------------------------------------

class TestScanTimelapses:

    def test_finds_timelapse_mp4(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        tl = night / 'RO000H_20260315_timelapse.mp4'
        tl.write_bytes(b'\x00' * 100)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'timelapse_uploaded': False,
        })
        up = _Uploader(cfg)
        results = up._scan_timelapses()
        assert any(r[0] == tl for r in results)

    def test_finds_night_stack_webp(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        ns = night / 'RO000H_20260315_night_stack.webp'
        ns.write_bytes(b'\x00' * 100)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'night_stack_uploaded': False,
        })
        up = _Uploader(cfg)
        results = up._scan_timelapses()
        assert any(r[0] == ns for r in results)

    def test_skips_uploaded_timelapse(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        tl = night / 'RO000H_20260315_timelapse.mp4'
        tl.write_bytes(b'\x00' * 100)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'timelapse_uploaded': True,
        })
        up = _Uploader(cfg)
        results = up._scan_timelapses()
        timelapse_results = [r for r in results if r[0].name.endswith('_timelapse.mp4')]
        assert timelapse_results == []


# ---------------------------------------------------------------------------
# _Uploader._scan_state_jsons
# ---------------------------------------------------------------------------

class TestScanStateJsons:

    def test_finds_state_jsons(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        _make_state(night, {'station': 'RO000H', 'date': '20260315'})
        up = _Uploader(cfg)
        results = up._scan_state_jsons()
        assert len(results) == 1
        assert results[0][0] == night / 'state.json'
        assert results[0][1] == 'RO000H'
        assert results[0][2] == '20260315'

    def test_skips_non_date_dirs(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        bad_dir = capture / 'RO000H' / 'logs'
        bad_dir.mkdir(parents=True)
        (bad_dir / 'state.json').write_text('{}')
        up = _Uploader(cfg)
        assert up._scan_state_jsons() == []

    def test_skips_missing_state_json(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        # No state.json
        up = _Uploader(cfg)
        assert up._scan_state_jsons() == []


# ---------------------------------------------------------------------------
# rsync_file
# ---------------------------------------------------------------------------

class TestRsyncFile:

    @patch('archive_upload.subprocess.run')
    @patch('archive_upload._ssh_mkdir')
    def test_success(self, mock_mkdir, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        local = tmp_path / 'test.mkv'
        local.write_bytes(b'\x00')
        result = rsync_file(local, '/remote/test.mkv', 'host', 22)
        assert result is True
        mock_mkdir.assert_called_once()
        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == 'rsync'
        assert str(local) in cmd
        assert 'root@host:/remote/test.mkv' in cmd

    @patch('archive_upload.subprocess.run')
    @patch('archive_upload._ssh_mkdir')
    def test_failure(self, mock_mkdir, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stderr='connection refused')
        local = tmp_path / 'test.mkv'
        local.write_bytes(b'\x00')
        result = rsync_file(local, '/remote/test.mkv', 'host', 22)
        assert result is False

    @patch('archive_upload.subprocess.run', side_effect=subprocess.TimeoutExpired('rsync', 300))
    @patch('archive_upload._ssh_mkdir')
    def test_timeout(self, mock_mkdir, mock_run, tmp_path):
        local = tmp_path / 'test.mkv'
        local.write_bytes(b'\x00')
        result = rsync_file(local, '/remote/test.mkv', 'host', 22)
        assert result is False

    @patch('archive_upload.subprocess.run')
    @patch('archive_upload._ssh_mkdir')
    def test_ssh_key_in_command(self, mock_mkdir, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        local = tmp_path / 'test.mkv'
        local.write_bytes(b'\x00')
        rsync_file(local, '/remote/test.mkv', 'host', 22, ssh_key='/key')
        cmd = mock_run.call_args[0][0]
        ssh_e_arg = cmd[cmd.index('-e') + 1]
        assert '-i' in ssh_e_arg
        assert '/key' in ssh_e_arg

    @patch('archive_upload.subprocess.run')
    @patch('archive_upload._ssh_mkdir')
    def test_custom_user_and_port(self, mock_mkdir, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        local = tmp_path / 'test.mkv'
        local.write_bytes(b'\x00')
        rsync_file(local, '/remote/test.mkv', 'host', 2222, user='deploy')
        cmd = mock_run.call_args[0][0]
        assert 'deploy@host:/remote/test.mkv' in cmd
        ssh_e_arg = cmd[cmd.index('-e') + 1]
        assert '-p 2222' in ssh_e_arg


# ---------------------------------------------------------------------------
# rsync_batch
# ---------------------------------------------------------------------------

class TestRsyncBatch:

    @patch('archive_upload.subprocess.run')
    @patch('archive_upload._ssh_mkdir')
    def test_success_returns_all_files(self, mock_mkdir, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        f1 = tmp_path / 'a.mkv'
        f2 = tmp_path / 'b.mkv'
        f1.write_bytes(b'\x00')
        f2.write_bytes(b'\x00')
        result = rsync_batch([f1, f2], tmp_path, '/remote/dir', 'host', 22)
        assert result == [f1, f2]

    @patch('archive_upload.subprocess.run')
    @patch('archive_upload._ssh_mkdir')
    def test_empty_list(self, mock_mkdir, mock_run, tmp_path):
        result = rsync_batch([], tmp_path, '/remote/dir', 'host', 22)
        assert result == []
        mock_run.assert_not_called()

    @patch('archive_upload.rsync_file')
    @patch('archive_upload.subprocess.run')
    @patch('archive_upload._ssh_mkdir')
    def test_batch_failure_falls_back_to_per_file(self, mock_mkdir, mock_run,
                                                   mock_rsync_file, tmp_path):
        """When batch rsync fails, it falls back to per-file uploads."""
        mock_run.return_value = MagicMock(returncode=1, stderr='batch error')
        mock_rsync_file.side_effect = [True, False]  # first succeeds, second fails
        f1 = tmp_path / 'a.mkv'
        f2 = tmp_path / 'b.mkv'
        f1.write_bytes(b'\x00')
        f2.write_bytes(b'\x00')
        result = rsync_batch([f1, f2], tmp_path, '/remote/dir', 'host', 22)
        assert result == [f1]  # only the one that succeeded
        assert mock_rsync_file.call_count == 2

    @patch('archive_upload.subprocess.run',
           side_effect=subprocess.TimeoutExpired('rsync', 300))
    @patch('archive_upload._ssh_mkdir')
    def test_timeout_returns_empty(self, mock_mkdir, mock_run, tmp_path):
        f1 = tmp_path / 'a.mkv'
        f1.write_bytes(b'\x00')
        result = rsync_batch([f1], tmp_path, '/remote/dir', 'host', 22)
        assert result == []

    @patch('archive_upload.subprocess.run')
    @patch('archive_upload._ssh_mkdir')
    def test_files_from_flag_in_command(self, mock_mkdir, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        f1 = tmp_path / 'a.mkv'
        f1.write_bytes(b'\x00')
        rsync_batch([f1], tmp_path, '/remote/dir', 'host', 22)
        cmd = mock_run.call_args[0][0]
        assert '--files-from' in cmd


# ---------------------------------------------------------------------------
# _Uploader._scan_all
# ---------------------------------------------------------------------------

class TestScanAll:

    def test_returns_all_categories(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        stacks_dir = night / 'stacks'
        stacks_dir.mkdir()

        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        webp = stacks_dir / 'RO000H_20260315_210000_stack.webp'
        webp.write_bytes(b'\x00' * 100)
        tl = night / 'RO000H_20260315_timelapse.mp4'
        tl.write_bytes(b'\x00' * 100)
        ns = night / 'RO000H_20260315_night_stack.webp'
        ns.write_bytes(b'\x00' * 100)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {
                mkv.name: {
                    'lock': {'lock_type': 'detection'},
                    'reencoded': True,
                    'uploaded': False,
                    'stack_uploaded': False,
                },
            },
            'timelapse_uploaded': False,
            'night_stack_uploaded': False,
        })

        up = _Uploader(cfg)
        scan = up._scan_all()
        assert len(scan.locked_mkvs) == 1
        assert len(scan.stacks) == 1
        assert len(scan.timelapses) == 2
        assert len(scan.state_jsons) == 1

    def test_empty_tree(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        scan = up._scan_all()
        assert scan.locked_mkvs == []
        assert scan.stacks == []
        assert scan.timelapses == []
        assert scan.state_jsons == []

    def test_matches_individual_scans(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        stacks_dir = night / 'stacks'
        stacks_dir.mkdir()
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        webp = stacks_dir / 'RO000H_20260315_210000_stack.webp'
        webp.write_bytes(b'\x00' * 100)
        tl = night / 'RO000H_20260315_timelapse.mp4'
        tl.write_bytes(b'\x00' * 100)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {
                mkv.name: {
                    'lock': {'lock_type': 'detection'},
                    'reencoded': True,
                    'uploaded': False,
                    'stack_uploaded': False,
                },
            },
            'timelapse_uploaded': False,
        })
        up = _Uploader(cfg)
        scan = up._scan_all()
        assert scan.locked_mkvs == up._scan_locked_mkvs()
        assert scan.stacks == up._scan_stacks()
        assert scan.timelapses == up._scan_timelapses()
        assert scan.state_jsons == up._scan_state_jsons()
