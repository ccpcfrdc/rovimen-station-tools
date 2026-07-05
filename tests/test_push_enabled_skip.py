"""Tests for the per-station push_enabled poll opt-out.

push_enabled is the poll→push cutover switch (reversed-HTTP push rollout).
A station with push_enabled=True is fed by the ingest API, so the dashboard's
server-side pollers must SKIP it:

  * station_client.start_polling status + vitals fan-out
  * index_poller._load_stations (detection-index poll)

Everything defaults to False, so normal stations keep polling exactly as
today — the flag is a conservative, additive per-station opt-out.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import station_client
import index_poller
from models import CameraConfig, StationConfig, DashboardConfig


# ── Helpers ─────────────────────────────────────────────────────────────────

def _capture_poll_targets(config, cache):
    """Run start_polling but capture the poll_status/poll_vitals closures
    instead of spawning real threads. Returns (poll_status_fn, poll_vitals_fn).
    """
    tunnels = MagicMock()
    ran_target: list = []

    def capture_thread(*args, **kwargs):
        target = kwargs.get("target") or (args[0] if args else None)
        ran_target.append(target)
        return MagicMock()

    with patch("station_client.threading") as mock_threading:
        mock_threading.Thread.side_effect = capture_thread
        station_client.start_polling(config, tunnels, cache)

    assert len(ran_target) == 2, "expected poll_status and poll_vitals threads"
    return ran_target[0], ran_target[1]


def _run_once(poll_fn) -> None:
    """Drive a poll closure through exactly one loop iteration by making
    time.sleep raise to break out of the `while True` loop."""
    def fake_sleep(_seconds):
        raise StopIteration("done")

    with patch("station_client.time") as mock_time:
        mock_time.sleep.side_effect = fake_sleep
        with pytest.raises(StopIteration):
            poll_fn()


# ── station_client status/vitals poll skip ────────────────────────────────────

class TestStatusVitalsPollSkip:
    def _config(self) -> DashboardConfig:
        return DashboardConfig(
            stations={
                "normal": StationConfig(ip="100.0.0.1", label="Normal"),
                "pusher": StationConfig(
                    ip="100.0.0.2", label="Pusher", push_enabled=True
                ),
            }
        )

    def test_status_poll_skips_push_enabled_station(self):
        config = self._config()
        cache = MagicMock()
        cache.get_status.return_value = None
        polled: list[str] = []

        def fake_get(cfg, tunnels, key, endpoint, timeout):
            polled.append(key)
            return {"online": True}

        poll_status_fn, _ = _capture_poll_targets(config, cache)
        with patch("station_client.station_get_status", side_effect=fake_get):
            _run_once(poll_status_fn)

        assert "normal" in polled, "normal station must still be polled"
        assert "pusher" not in polled, "push_enabled station must be skipped"
        # The push station's cache entry is left untouched (ingest owns it).
        set_keys = [c.args[0] for c in cache.set_status.call_args_list]
        assert "pusher" not in set_keys

    def test_vitals_poll_skips_push_enabled_station(self):
        config = self._config()
        cache = MagicMock()
        polled: list[str] = []

        def fake_get(cfg, tunnels, key, endpoint, timeout):
            polled.append(key)
            return {"cpu": 1}

        _, poll_vitals_fn = _capture_poll_targets(config, cache)
        with patch("station_client.station_get_status", side_effect=fake_get):
            _run_once(poll_vitals_fn)

        assert "normal" in polled
        assert "pusher" not in polled
        set_keys = [c.args[0] for c in cache.set_vitals.call_args_list]
        assert "pusher" not in set_keys

    def test_normal_station_polled_when_no_flag(self):
        """Default False: a station without push_enabled is polled as today."""
        config = DashboardConfig(
            stations={"normal": StationConfig(ip="100.0.0.1", label="Normal")}
        )
        cache = MagicMock()
        cache.get_status.return_value = None
        polled: list[str] = []

        def fake_get(cfg, tunnels, key, endpoint, timeout):
            polled.append(key)
            return {"online": True}

        poll_status_fn, _ = _capture_poll_targets(config, cache)
        with patch("station_client.station_get_status", side_effect=fake_get):
            _run_once(poll_status_fn)

        assert polled == ["normal"]


# ── index_poller detection-index poll skip ────────────────────────────────────

class TestIndexPollerSkip:
    def _write_config(self, tmp_path, pusher_flag: bool):
        import yaml
        cfg = {
            "station_api_port": 7779,
            "stations": {
                "normal": {
                    "ip": "100.0.0.1",
                    "label": "Normal",
                    "cameras": [{"code": "RO000A", "cam_ip": "192.168.1.10"}],
                },
                "pusher": {
                    "ip": "100.0.0.2",
                    "label": "Pusher",
                    "push_enabled": pusher_flag,
                    "cameras": [{"code": "RO000B", "cam_ip": "192.168.1.11"}],
                },
            },
        }
        path = tmp_path / "dashboard_config.yaml"
        path.write_text(yaml.safe_dump(cfg))
        return path

    def test_load_stations_skips_push_enabled(self, tmp_path):
        path = self._write_config(tmp_path, pusher_flag=True)
        with patch.object(index_poller, "CONFIG_PATH", path):
            stations = index_poller._load_stations()
        assert "normal" in stations
        assert "pusher" not in stations, "push_enabled station must not be polled"

    def test_load_stations_includes_when_flag_false(self, tmp_path):
        path = self._write_config(tmp_path, pusher_flag=False)
        with patch.object(index_poller, "CONFIG_PATH", path):
            stations = index_poller._load_stations()
        assert "normal" in stations
        assert "pusher" in stations, "default/False keeps the station polled"


# ── Round-trip through _config_to_dict semantics ──────────────────────────────

class TestPushEnabledRoundTrip:
    def test_flag_round_trips_through_config_dict(self):
        st = StationConfig(
            ip="100.0.0.2",
            label="Pusher",
            cameras=[CameraConfig(code="RO000B", cam_ip="192.168.1.11")],
            push_enabled=True,
        )
        # Mirror the _config_to_dict per-station emission for push_enabled.
        d = {
            "ip": st.ip,
            "label": st.label,
            "ssh_user": st.ssh_user,
            "proxy_media": st.proxy_media,
            "jump_hosts": st.jump_hosts,
            "lat": st.lat,
            "lon": st.lon,
            "show_on_map": st.show_on_map,
            "public_tabs": st.public_tabs,
            "public": st.public,
            "location_name": st.location_name,
            "status": st.status,
            "push_enabled": st.push_enabled,
            "cameras": [{"code": c.code, "cam_ip": c.cam_ip} for c in st.cameras],
        }
        restored = StationConfig.model_validate(d)
        assert restored.push_enabled is True

    def test_default_false_round_trips(self):
        st = StationConfig(ip="100.0.0.1", label="Normal")
        d = {"ip": st.ip, "label": st.label, "push_enabled": st.push_enabled}
        assert StationConfig.model_validate(d).push_enabled is False
