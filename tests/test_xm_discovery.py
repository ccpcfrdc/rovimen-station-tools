"""Tests for xm_discovery.py -- Sofia (XM/DVRIP) LAN discovery pure-logic.

Covers build_probe, xm_hex_to_ip, parse_reply and the MAC keying of discover().
The socket path is exercised via a fake socket so no real network is touched.
"""

from __future__ import annotations

import json
import struct

import pytest

from xm_discovery import (
    _HEADER,
    _MSG_SEARCH_REPLY,
    XMDevice,
    build_probe,
    discover,
    parse_reply,
    xm_hex_to_ip,
)


def _reply(net: dict[str, str]) -> bytes:
    """Assemble a well-formed Sofia search reply (msgid 1531) around ``net``."""
    body = json.dumps({"NetWork.NetCommon": net}).encode()
    header = _HEADER.pack(255, 0, 0, 0, 0, 0, _MSG_SEARCH_REPLY, len(body))
    return header + body


NETCOMMON = {
    "MAC": "00:12:43:3B:71:C5",
    "HostIP": "0x0A01A8C0",
    "GateWay": "0x0101A8C0",
    "Submask": "0x00FFFFFF",
}


def test_build_probe_is_20_byte_search_request():
    probe = build_probe()
    assert len(probe) == 20
    assert struct.unpack("BBHIIHHI", probe) == (255, 0, 0, 0, 0, 0, 1530, 0)


@pytest.mark.parametrize(
    "hex_value,expected",
    [
        ("0x0A01A8C0", "192.168.1.10"),
        ("0x0101A8C0", "192.168.1.1"),
        ("0x00FFFFFF", "255.255.255.0"),
        ("", ""),
        (None, ""),
    ],
)
def test_xm_hex_to_ip(hex_value, expected):
    assert xm_hex_to_ip(hex_value) == expected


def test_parse_reply_decodes_netcommon():
    device = parse_reply(_reply(NETCOMMON))
    assert device == XMDevice(
        mac="00:12:43:3b:71:c5",  # lower-cased
        ip="192.168.1.10",
        gateway="192.168.1.1",
        netmask="255.255.255.0",
    )


def test_parse_reply_rejects_short_datagram():
    assert parse_reply(b"\x00\x01") is None


def test_parse_reply_rejects_wrong_msgid():
    body = json.dumps({"NetWork.NetCommon": NETCOMMON}).encode()
    not_a_reply = _HEADER.pack(255, 0, 0, 0, 0, 0, 1530, len(body)) + body
    assert parse_reply(not_a_reply) is None


def test_parse_reply_rejects_bad_json():
    header = _HEADER.pack(255, 0, 0, 0, 0, 0, _MSG_SEARCH_REPLY, 5)
    assert parse_reply(header + b"not{}") is None


def test_parse_reply_requires_mac():
    assert parse_reply(_reply({"HostIP": "0x0A01A8C0"})) is None


class _FakeSocket:
    """Minimal datagram socket that replays canned replies then times out."""

    def __init__(self, replies: list[bytes]):
        self._replies = list(replies)

    def setsockopt(self, *_):
        pass

    def bind(self, _addr):
        pass

    def settimeout(self, _t):
        pass

    def sendto(self, _data, _addr):
        pass

    def recvfrom(self, _n):
        if self._replies:
            return self._replies.pop(0), ("10.0.0.1", 34569)
        raise TimeoutError

    def close(self):
        pass


def test_discover_keys_by_mac_and_dedupes(monkeypatch):
    # Same camera answers twice (broadcast echoes) -> one entry.
    replies = [_reply(NETCOMMON), _reply(NETCOMMON)]
    monkeypatch.setattr("xm_discovery.socket.socket", lambda *a, **k: _FakeSocket(replies))
    # TimeoutError is what socket.timeout raises on Python 3.10+.
    monkeypatch.setattr("xm_discovery.socket.timeout", TimeoutError, raising=False)

    found = discover(timeout=1.0)

    assert list(found) == ["00:12:43:3b:71:c5"]
    assert found["00:12:43:3b:71:c5"].ip == "192.168.1.10"
