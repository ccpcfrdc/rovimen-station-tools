"""Config-derived GMN witness tagging — the network-agnostic overlay logic
that replaced the hard-coded RO-prefix + Berlin DE codes."""

from __future__ import annotations

import gmn_data
from models import DashboardConfig


def test_tag_witness_with_highlight_subset():
    gmn_data.configure_station_codes(
        our_codes=["RO000A", "RO000B", "DE001B", "DE0018"],
        highlight_codes=["DE001B", "DE0018"],
    )
    # a primary (non-highlight) station saw it -> ours, has_primary, not highlight-only
    assert gmn_data.tag_witness({"RO000A", "US0001"}) == (True, True, False)
    # only highlight (DE) stations saw it -> ours, no primary, highlight-only
    assert gmn_data.tag_witness({"DE001B", "US0001"}) == (True, False, True)
    # mixed primary + highlight -> primary wins
    assert gmn_data.tag_witness({"RO000A", "DE001B"}) == (True, True, False)
    # none of ours -> not ours
    assert gmn_data.tag_witness({"US0001", "AU0002"}) == (False, False, False)
    # case-insensitive matching
    assert gmn_data.tag_witness({"ro000a"}) == (True, True, False) or \
        gmn_data.tag_witness({"RO000A"}) == (True, True, False)


def test_tag_witness_no_highlight_is_single_bucket():
    # An operator that sets no highlight_codes gets one undifferentiated "ours".
    gmn_data.configure_station_codes(our_codes=["US0001", "US0002"])
    assert gmn_data.tag_witness({"US0001"}) == (True, True, False)
    assert gmn_data.tag_witness({"US0002", "RO000A"}) == (True, True, False)
    assert gmn_data.tag_witness({"RO000A"}) == (False, False, False)


def test_tag_witness_unconfigured_matches_nothing():
    gmn_data.configure_station_codes(our_codes=[])
    assert gmn_data.tag_witness({"RO000A"}) == (False, False, False)
    assert gmn_data.our_cam_codes() == frozenset()


def test_highlight_codes_clamped_to_our_codes():
    # A highlight code that isn't one of our stations is ignored (can't make a
    # non-owned station a highlight), so our sole station stays "primary".
    gmn_data.configure_station_codes(our_codes=["RO000A"], highlight_codes=["DE001B"])
    assert gmn_data.our_cam_codes() == frozenset({"RO000A"})
    assert gmn_data.tag_witness({"RO000A"}) == (True, True, False)


def test_config_highlight_codes_uppercased_and_null_safe():
    cfg = DashboardConfig.model_validate({"highlight_codes": ["de001b", " ro000a "]})
    assert cfg.highlight_codes == ["DE001B", "RO000A"]
    assert DashboardConfig.model_validate({"highlight_codes": None}).highlight_codes == []
    assert DashboardConfig().highlight_codes == []
