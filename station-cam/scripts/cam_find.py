#!/usr/bin/env python3
"""Scan for XM cameras and tag the ones known in config.json by MAC.
Self-locating (vendored dvrip).

With no argument it scans every /24 the station has an address on — i.e. the
main LAN and the private camera subnet (if the alias is up) — so cameras are
found wherever they are. Pass an interface (e.g. eno1) or an IP prefix
(e.g. 10.42.0) to scan just one subnet.

Usage: cam-find [interface|prefix]
"""

import fcntl
import json
import os
import socket
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
from dvrip import DVRIPCam  # noqa: E402

CFG = os.environ.get("ROVIMEN_CAM_CONFIG", "/etc/rovimen-cam/config.json")
PORT = 34567

known = {}
if os.path.exists(CFG):
    known = {m.lower(): v.get("ip")
             for m, v in json.load(open(CFG)).get("cameras", {}).items()}


def iface_ip(name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return socket.inet_ntoa(
        fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))[20:24]
    )


def local_prefixes():
    """{'/24 prefix': own_ip} for every non-loopback IPv4 the station holds."""
    out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                         capture_output=True, text=True).stdout
    prefixes = {}
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            ip = parts[parts.index("inet") + 1].split("/")[0]
            if not ip.startswith("127."):
                prefixes[ip.rsplit(".", 1)[0]] = ip
    return prefixes


def xm_ip(h):
    v = int(h, 16)
    return ".".join(str((v >> (8 * i)) & 0xFF) for i in range(4))


def check(ip):
    s = socket.socket()
    s.settimeout(0.6)
    try:
        if s.connect_ex((ip, PORT)) != 0:
            return None
    finally:
        s.close()
    try:
        c = DVRIPCam(ip, port=PORT, user="admin", password="")
        c.timeout = 6
        if not c.login():
            c.close()
            return (ip, "?", "login failed")
        nc = c.get_info("NetWork.NetCommon")
        c.close()
        return (ip, nc.get("MAC", "?"), f"HostIP={xm_ip(nc['HostIP'])}")
    except Exception as e:
        return (ip, "?", f"err:{e}")


# --- pick target subnet(s) ---
if len(sys.argv) > 1:
    arg = sys.argv[1]
    if arg[0].isdigit():
        targets = {arg.rstrip("."): None}
    else:
        ip = iface_ip(arg)
        targets = {ip.rsplit(".", 1)[0]: ip}
        print(f"{arg} -> {ip} (subnet {ip.rsplit('.', 1)[0]}.0/24)")
else:
    targets = local_prefixes()  # main LAN + camera alias (if up)
    print(f"local subnets: {', '.join(sorted(targets)) or '(none)'}")

found = []
for prefix, myip in targets.items():
    print(f"Scanning {prefix}.1-254 on {PORT}...")
    ips = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != myip]
    found += [r for r in ThreadPoolExecutor(max_workers=64).map(check, ips) if r]

if not found:
    print("nothing")
else:
    print("\n=== Found ===")
    for ip, mac, info in found:
        m = mac.lower()
        tag = f"  <== KNOWN (expected {known[m]})" if m in known else "  <== unknown"
        print(f"{ip:16} MAC={mac:18} {info}{tag}")
