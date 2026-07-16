#!/usr/bin/env python3
"""xm_discovery.py -- LAN discovery of XiongMai (Sofia/DVRIP) cameras.

XM cameras answer a UDP broadcast on port 34569 (the ``SearchXM`` half of the
Sofia protocol, the same one DeviceManager.exe uses). Because the probe is a
layer-2 broadcast, cameras reply **regardless of their IP subnet** -- so this
finds a camera even after it has reset to DHCP or drifted onto an unexpected
subnet, which a unicast port scan of the *expected* ``/24`` cannot.

Each reply carries a JSON ``NetWork.NetCommon`` block, so the camera is matched
by **MAC address** rather than "the first camera that isn't at the expected IP".
That makes ``enforce_camera_ip`` safe on multi-camera stations: it can only ever
reconfigure the unit whose MAC matches the one pinned in ``config.json``.

CLI::

    python xm_discovery.py                 # table of every XM camera found
    python xm_discovery.py --json          # same, as JSON
    python xm_discovery.py --mac <MAC>     # JSON for one camera (exit 1 if absent)
    python xm_discovery.py --mac <MAC> -q  # just its current IP (for shell capture)
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import struct
import sys

from pydantic import BaseModel

logger = logging.getLogger(__name__)

DISCOVERY_PORT = 34569
BROADCAST_ADDR = "255.255.255.255"
# Sofia header: BBHIIHHI = magic, version, type, session, packet, info, msgid, len
_HEADER = struct.Struct("BBHIIHHI")
_MSG_SEARCH_REQ = 1530
_MSG_SEARCH_REPLY = 1531


class XMDevice(BaseModel):
    """A camera that answered the Sofia broadcast."""

    mac: str
    ip: str
    gateway: str = ""
    netmask: str = ""


def build_probe() -> bytes:
    """The 20-byte Sofia device-search request (empty body)."""
    return _HEADER.pack(255, 0, 0, 0, 0, 0, _MSG_SEARCH_REQ, 0)


def xm_hex_to_ip(value: str | None) -> str:
    """Decode an XM hex address (``0x0A01A8C0``, little-endian octets) to dotted."""
    if not value:
        return ""
    packed = int(value, 16)
    return ".".join(str((packed >> (8 * i)) & 0xFF) for i in range(4))


def parse_reply(data: bytes) -> XMDevice | None:
    """Parse one datagram into an :class:`XMDevice`, or ``None`` if it is not a
    well-formed Sofia search reply."""
    if len(data) < _HEADER.size:
        return None
    *_, msgid, length = _HEADER.unpack(data[: _HEADER.size])
    if msgid != _MSG_SEARCH_REPLY or length <= 0:
        return None
    body = data[_HEADER.size : _HEADER.size + length].replace(b"\x00", b"")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    net = payload.get("NetWork.NetCommon", {})
    mac = (net.get("MAC") or "").lower()
    if not mac:
        return None
    return XMDevice(
        mac=mac,
        ip=xm_hex_to_ip(net.get("HostIP")),
        gateway=xm_hex_to_ip(net.get("GateWay")),
        netmask=xm_hex_to_ip(net.get("Submask")),
    )


def discover(timeout: float = 2.0) -> dict[str, XMDevice]:
    """Broadcast a Sofia search and return every camera that replied, keyed by
    lower-case MAC. Duplicated replies collapse onto the same key."""
    found: dict[str, XMDevice] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as exc:
        logger.warning("cannot bind UDP :%d for discovery: %s", DISCOVERY_PORT, exc)
        sock.close()
        return found
    try:
        sock.settimeout(timeout)
        sock.sendto(build_probe(), (BROADCAST_ADDR, DISCOVERY_PORT))
        # Cameras answer within milliseconds; keep reading until the socket goes
        # quiet for a full `timeout` window, then stop.
        while True:
            try:
                data, _addr = sock.recvfrom(8192)
            except socket.timeout:
                break
            device = parse_reply(data)
            if device is not None:
                found[device.mac] = device
    finally:
        sock.close()
    logger.debug("Sofia discovery found %d camera(s)", len(found))
    return found


def find_by_mac(mac: str, timeout: float = 2.0) -> XMDevice | None:
    """Return the camera whose MAC matches ``mac`` (case-insensitive), or None."""
    return discover(timeout=timeout).get(mac.lower())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover XM (Sofia) cameras on the LAN.")
    parser.add_argument("--mac", help="only report the camera with this MAC")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="with --mac, print only the IP"
    )
    parser.add_argument("--timeout", type=float, default=2.0, help="seconds to listen")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    devices = discover(timeout=args.timeout)

    if args.mac:
        device = devices.get(args.mac.lower())
        if device is None:
            return 1
        print(device.ip if args.quiet else device.model_dump_json())
        return 0

    if args.json:
        print(json.dumps([d.model_dump() for d in devices.values()]))
    else:
        for d in devices.values():
            print(f"{d.mac}\t{d.ip}\t{d.gateway}\t{d.netmask}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
