"""Tests for the storage-box retention janitor.

Builds a synthetic archive tree (state.json + rms/radiants + FTPdetectinfo)
in tmp_path and exercises the keep/prune decision end-to-end. GMN lookups are
monkeypatched so the tests never touch the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import storagebox_janitor as sj


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestPercentile:
    def test_empty(self):
        assert sj._percentile([], 20) is None

    def test_single(self):
        assert sj._percentile([3.0], 20) == 3.0

    def test_interpolated(self):
        vals = [-2.0, 4.5, 5.0, 5.0, 5.0]
        # rank = 0.2 * 4 = 0.8 -> -2 + 0.8*(4.5 - -2) = 3.2
        assert sj._percentile(vals, 20) == pytest.approx(3.2)
        # P80 -> rank 3.2 -> 5.0 + 0.2*(5.0-5.0) = 5.0
        assert sj._percentile(vals, 80) == pytest.approx(5.0)


class TestFfTimeUtc:
    def test_valid(self):
        assert sj._ff_time_utc("FF_RO000H_20260314_023422_123_0001.fits") == \
            "2026-03-14T02:34:22"

    def test_malformed(self):
        assert sj._ff_time_utc("garbage.fits") is None
        assert sj._ff_time_utc("FF_RO000H_2026_02.fits") is None


# ---------------------------------------------------------------------------
# Synthetic archive builder
# ---------------------------------------------------------------------------

def _old_date() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y%m%d")


def _recent_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


class _ArchiveBuilder:
    def __init__(self, root: Path):
        self.root = root
        # (cam, date) -> list of (radiants_row, ftp_block)
        self._rms: dict[tuple[str, str], list[tuple[str, str]]] = {}
        # (cam, date) -> state chunks dict
        self._state: dict[tuple[str, str], dict] = {}

    def add_clip(self, cam, date, hhmmss, mag, last_frame, lock_type):
        fname = f"{cam}_{date}_{hhmmss}_color.mkv"
        date_dir = self.root / cam / date
        meteors = date_dir / "meteors"
        meteors.mkdir(parents=True, exist_ok=True)
        # ~10 MB dummy video
        (meteors / fname).write_bytes(b"\0" * (10 * 1024 * 1024))
        # stack we must never delete
        (meteors / fname.replace("_color.mkv", "_stack.webp")).write_bytes(b"webp")

        iso_d = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        meteor_time = f"{iso_d}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}.000000"
        self._state.setdefault((cam, date), {})[fname] = {
            "lock": {"lock_type": lock_type, "meteor_time": meteor_time}
        }

        # radiants row: 17 comma fields; time at idx 0, shower idx 3, mag idx 14/15
        row = ["0"] * 17
        row[0] = f"{date} {hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"
        row[3] = "SPO"
        row[14] = str(mag)
        row[15] = str(mag)
        radiants_row = ",".join(row)

        # FTPdetectinfo block: fps 25, frames 0..last_frame -> dur = last/25
        ff = f"FF_{cam}_{date}_{hhmmss}_000_0001.fits"
        ftp_block = "\n".join([
            ff,
            "Recalibrated",
            f"{cam} 0001 5 25.0",
            "0.0 0 0 0 0 0 0 0 5.0",
            f"{float(last_frame)} 0 0 0 0 0 0 0 5.0",
        ])
        self._rms.setdefault((cam, date), []).append((radiants_row, ftp_block))

    def write(self):
        for (cam, date), chunks in self._state.items():
            date_dir = self.root / cam / date
            (date_dir / "state.json").write_text(json.dumps({"chunks": chunks}))
            rows = self._rms.get((cam, date), [])
            rms_dir = date_dir / "rms"
            rms_dir.mkdir(parents=True, exist_ok=True)
            (rms_dir / f"{cam}_{date}_radiants.txt").write_text(
                "\n".join(r for r, _ in rows) + "\n")
            (rms_dir / f"FTPdetectinfo_{cam}_{date}.txt").write_text(
                "\n".join(b for _, b in rows) + "\n")


@pytest.fixture
def no_gmn(monkeypatch):
    monkeypatch.setattr(sj.gmn_data, "events_for_date",
                        lambda d: {"events": []})


@pytest.fixture
def archive(tmp_path, no_gmn):
    """8 clips (7 old + 1 recent), all on one camera so internal correlation
    never fires. With this population the top-20% cuts are unambiguous:
      bright (2): K1,K2 ; long (2): L1,L2 ; manual: D ; recent: R.
    Only the two plain dim+short old clips (A, E) are prune candidates."""
    b = _ArchiveBuilder(tmp_path)
    old = _old_date()
    b.add_clip("RO000H", old, "020001", mag=-3.0, last_frame=5,   lock_type="detection")  # K1 bright
    b.add_clip("RO000H", old, "020011", mag=-2.0, last_frame=5,   lock_type="detection")  # K2 bright
    b.add_clip("RO000H", old, "020021", mag=5.5,  last_frame=300, lock_type="detection")  # L1 long
    b.add_clip("RO000H", old, "020031", mag=5.6,  last_frame=250, lock_type="detection")  # L2 long
    b.add_clip("RO000H", old, "020041", mag=5.0,  last_frame=5,   lock_type="manual")     # D  manual
    b.add_clip("RO000H", old, "020051", mag=5.0,  last_frame=5,   lock_type="detection")  # A  PRUNE
    b.add_clip("RO000H", old, "020101", mag=4.8,  last_frame=7,   lock_type="detection")  # E  PRUNE
    # Recent clip: always kept regardless of how dim/short.
    b.add_clip("RO000H", _recent_date(), "030000", mag=6.0, last_frame=1, lock_type="detection")
    b.write()
    return tmp_path


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------

def _run(root, **kw):
    defaults = dict(
        archive=root, retention_days=180, bright_pct=20.0, long_pct=20.0,
        tol_s=3.0, corr_window_s=3.0, apply=False, max_deletes=0, report_path=None,
    )
    defaults.update(kw)
    return sj.run(**defaults)


def test_dry_run_selects_only_plain_old_clips(archive):
    stats = _run(archive)
    # A and E pruned; K1/K2 bright, L1/L2 long, D manual, recent kept.
    assert stats.pruned == 2
    assert stats.kept_bright == 2
    assert stats.kept_long == 2
    assert stats.kept_manual == 1
    assert stats.kept_recent == 1
    # Dry run deletes nothing.
    old = _old_date()
    assert (archive / "RO000H" / old / "meteors" /
            f"RO000H_{old}_020051_color.mkv").exists()


def test_apply_deletes_mkv_keeps_stack_and_marks_state(archive):
    stats = _run(archive, apply=True)
    assert stats.pruned == 2
    old = _old_date()
    md = archive / "RO000H" / old / "meteors"
    # Pruned MKVs gone (A, E).
    assert not (md / f"RO000H_{old}_020051_color.mkv").exists()
    assert not (md / f"RO000H_{old}_020101_color.mkv").exists()
    # Kept MKVs remain.
    assert (md / f"RO000H_{old}_020001_color.mkv").exists()  # bright K1
    assert (md / f"RO000H_{old}_020021_color.mkv").exists()  # long L1
    assert (md / f"RO000H_{old}_020041_color.mkv").exists()  # manual D
    # Stacks for pruned clips are NEVER deleted.
    assert (md / f"RO000H_{old}_020051_stack.webp").exists()
    # state.json marks pruned clips.
    state = json.loads((archive / "RO000H" / old / "state.json").read_text())
    assert state["chunks"][f"RO000H_{old}_020051_color.mkv"]["video_pruned"] is True
    assert "video_pruned" not in state["chunks"][f"RO000H_{old}_020001_color.mkv"]


def test_idempotent_second_run_prunes_nothing(archive):
    _run(archive, apply=True)
    stats2 = _run(archive, apply=True)
    assert stats2.pruned == 0


def test_unreadable_state_skips_date_never_prunes(archive, tmp_path):
    old = _old_date()
    (tmp_path / "RO000H" / old / "state.json").write_text("{ not json")
    stats = _run(archive)
    assert stats.pruned == 0
    assert stats.skipped_dates >= 1


def test_max_deletes_throttle(archive):
    stats = _run(archive, apply=True, max_deletes=1)
    assert stats.pruned == 1  # capped


def test_gmn_multistation_keeps_old_dim_clip(tmp_path, monkeypatch):
    old = _old_date()
    iso = f"{old[:4]}-{old[4:6]}-{old[6:8]}"
    monkeypatch.setattr(sj.gmn_data, "events_for_date", lambda d: {
        "events": [{"stations": ["RO000H", "RO000J"],
                    "time": f"{iso}T02:00:01"}]
    })
    b = _ArchiveBuilder(tmp_path)
    b.add_clip("RO000H", old, "020001", mag=9.0, last_frame=1, lock_type="detection")
    b.write()
    stats = _run(tmp_path)
    assert stats.pruned == 0
    assert stats.kept_multistation == 1


def test_internal_correlation_keeps_coincident_clips(tmp_path, no_gmn):
    old = _old_date()
    b = _ArchiveBuilder(tmp_path)
    # Two cameras, same second -> mutual multi-station witnesses.
    b.add_clip("RO000H", old, "020001", mag=9.0, last_frame=1, lock_type="detection")
    b.add_clip("RO000J", old, "020001", mag=9.0, last_frame=1, lock_type="detection")
    b.write()
    stats = _run(tmp_path)
    assert stats.pruned == 0
    assert stats.kept_multistation == 2
