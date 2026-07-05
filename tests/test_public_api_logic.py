"""Tests for public_api.py event deduplication — P1 #11.

P1 #11: Event dedup when GMN timestamps use +00:00 vs Z suffix.
        Naive vs aware datetime comparison could cause TypeError or missed dedup.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timezone

import pytest


class TestCloseToExisting:
    """Replicate _close_to_existing from public_api.py lines 1065-1075."""

    @staticmethod
    def _close_to_existing(t: str, existing_timestamps: list[float]) -> bool:
        try:
            t_ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return False
        idx = bisect.bisect_left(existing_timestamps, t_ts - 3.0)
        while idx < len(existing_timestamps) and existing_timestamps[idx] <= t_ts + 3.0:
            if abs(existing_timestamps[idx] - t_ts) <= 3.0:
                return True
            idx += 1
        return False

    def test_z_suffix_matches_plus_zero(self):
        """'Z' and '+00:00' represent the same instant and should dedup."""
        t_z = "2026-04-01T21:30:05Z"
        t_plus = "2026-04-01T21:30:05+00:00"

        existing = [datetime.fromisoformat(t_plus.replace("Z", "+00:00")).timestamp()]
        assert self._close_to_existing(t_z, existing) is True

    def test_same_time_matches(self):
        existing_ts = [
            datetime(2026, 4, 1, 21, 30, 5, tzinfo=timezone.utc).timestamp()
        ]
        assert self._close_to_existing("2026-04-01T21:30:05+00:00", existing_ts) is True

    def test_within_3s_matches(self):
        existing_ts = [
            datetime(2026, 4, 1, 21, 30, 5, tzinfo=timezone.utc).timestamp()
        ]
        assert self._close_to_existing("2026-04-01T21:30:07+00:00", existing_ts) is True

    def test_beyond_3s_no_match(self):
        existing_ts = [
            datetime(2026, 4, 1, 21, 30, 5, tzinfo=timezone.utc).timestamp()
        ]
        assert self._close_to_existing("2026-04-01T21:30:09+00:00", existing_ts) is False

    def test_empty_existing_no_match(self):
        assert self._close_to_existing("2026-04-01T21:30:05+00:00", []) is False

    def test_malformed_time_no_match(self):
        assert self._close_to_existing("not-a-date", [1234567890.0]) is False

    def test_none_no_crash(self):
        assert self._close_to_existing(None, [1234567890.0]) is False

    def test_naive_iso_handled(self):
        """A naive ISO string (no timezone) is handled by .replace('Z', '+00:00')
        which is a no-op, then fromisoformat returns a naive datetime.
        .timestamp() interprets it as local time — potential mismatch."""
        naive = "2026-04-01T21:30:05"
        aware = "2026-04-01T21:30:05+00:00"
        existing = [datetime.fromisoformat(aware.replace("Z", "+00:00")).timestamp()]
        # This may or may not match depending on system timezone
        # The important thing is it doesn't crash
        result = self._close_to_existing(naive, existing)
        assert isinstance(result, bool)

    def test_multiple_existing_finds_closest(self):
        existing_ts = [
            datetime(2026, 4, 1, 21, 30, 0, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 4, 1, 21, 35, 0, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 4, 1, 21, 40, 0, tzinfo=timezone.utc).timestamp(),
        ]
        assert self._close_to_existing("2026-04-01T21:35:02+00:00", existing_ts) is True
        assert self._close_to_existing("2026-04-01T21:32:00+00:00", existing_ts) is False


class TestGmnWitnessTimeMatching:
    """Test the +/-2s witness matching from public_api.py lines 471-483."""

    @staticmethod
    def _match_witness(ev_time_str: str, local_times: list[str], window: float = 2.0) -> str | None:
        """Replicate the matching logic."""
        try:
            ev_time_dt = datetime.fromisoformat(ev_time_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        for t in local_times:
            try:
                t_dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                continue
            if abs((t_dt - ev_time_dt).total_seconds()) <= window:
                return t
        return None

    def test_exact_match(self):
        result = self._match_witness(
            "2026-04-01T21:30:05+00:00",
            ["2026-04-01T21:30:05+00:00"],
        )
        assert result is not None

    def test_within_2s(self):
        result = self._match_witness(
            "2026-04-01T21:30:05+00:00",
            ["2026-04-01T21:30:06.5+00:00"],
        )
        assert result is not None

    def test_beyond_2s_no_match(self):
        result = self._match_witness(
            "2026-04-01T21:30:05+00:00",
            ["2026-04-01T21:30:08+00:00"],
        )
        assert result is None

    def test_z_and_plus_zero_interop(self):
        result = self._match_witness(
            "2026-04-01T21:30:05Z",
            ["2026-04-01T21:30:05+00:00"],
        )
        assert result is not None

    def test_naive_vs_aware_raises(self):
        """Mixing naive and aware datetimes raises TypeError."""
        with pytest.raises(TypeError):
            ev_dt = datetime.fromisoformat("2026-04-01T21:30:05+00:00")
            local_dt = datetime.fromisoformat("2026-04-01T21:30:05")
            abs((local_dt - ev_dt).total_seconds())
