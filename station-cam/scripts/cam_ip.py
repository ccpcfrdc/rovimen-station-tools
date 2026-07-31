#!/usr/bin/env python3
"""Manually set a camera's IP (DHCP or static). Self-locating (vendored dvrip).

Usage:
  cam-ip <current_ip> show
  cam-ip <current_ip> dhcp
  cam-ip <current_ip> static <new_ip> [gateway] [mask]
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
from dvrip import DVRIPCam  # noqa: E402

USER, PW = "admin", ""


def ip_to_xm(ip):
    o = [int(x) for x in ip.split(".")]
    return "0x%02X%02X%02X%02X" % (o[3], o[2], o[1], o[0])  # XM = reversed octets


def usage():
    print("Usage:")
    print("  cam-ip <current_ip> show")
    print("  cam-ip <current_ip> dhcp")
    print("  cam-ip <current_ip> static <new_ip> [gateway] [mask]")
    sys.exit(1)


if len(sys.argv) < 3:
    usage()
cur, mode = sys.argv[1], sys.argv[2]

cam = DVRIPCam(cur, port=34567, user=USER, password=PW)
cam.timeout = 10
if not cam.login():
    print("LOGIN FAILED @", cur)
    sys.exit(1)

if mode == "show":
    print("NetCommon:", json.dumps(cam.get_info("NetWork.NetCommon"), indent=2))
    print("DHCP:", json.dumps(cam.get_info("NetWork.NetDHCP"), indent=2))

elif mode == "dhcp":
    dh = cam.get_info("NetWork.NetDHCP")
    (dh[0] if isinstance(dh, list) else dh)["Enable"] = 1
    print("DHCP on -> Ret", cam.set_info("NetWork.NetDHCP", dh).get("Ret"))
    print("Reboot the camera to pick up a lease.")

elif mode == "static":
    if len(sys.argv) < 4:
        usage()
    ip = sys.argv[3]
    gw = sys.argv[4] if len(sys.argv) > 4 else ip.rsplit(".", 1)[0] + ".1"
    mask = sys.argv[5] if len(sys.argv) > 5 else "255.255.255.0"
    dh = cam.get_info("NetWork.NetDHCP")
    (dh[0] if isinstance(dh, list) else dh)["Enable"] = 0
    print("DHCP off -> Ret", cam.set_info("NetWork.NetDHCP", dh).get("Ret"))
    nc = cam.get_info("NetWork.NetCommon")
    nc["HostIP"], nc["GateWay"], nc["Submask"] = ip_to_xm(ip), ip_to_xm(gw), ip_to_xm(mask)
    print(f"Static {ip} gw {gw} mask {mask} -> Ret", cam.set_info("NetWork.NetCommon", nc).get("Ret"))
    print(f"Camera moves to {ip}. You lose access on {cur}; reconnect at {ip}.")
else:
    usage()
cam.close()
