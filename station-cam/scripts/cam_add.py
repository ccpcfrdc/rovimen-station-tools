#!/usr/bin/env python3
"""cam-add — add a camera to the station config and enforce it.

Scriptable:  cam-add <mac> <ip> [profile] [gw] [mask]
Interactive: cam-add                 (scan for cameras not in config, prompt)

Appends the entry to /etc/rovimen-cam/config.json (never clobbers an existing
camera), then runs cam-enforce to pin the static IP and apply the profile.
gw/mask default to the network's alias_ip / 255.255.255.0; profile defaults to
the first delivered profile.
"""

import ipaddress
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
import cam_enforce as ce  # noqa: E402

CFG = os.environ.get("ROVIMEN_CAM_CONFIG", "/etc/rovimen-cam/config.json")
PROFILES = os.environ.get("ROVIMEN_CAM_PROFILES", "/etc/rovimen-cam/profiles.json")
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def load(p):
    return json.load(open(p)) if os.path.exists(p) else {}


def save_cfg(cfg):
    with open(CFG, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def next_free_ip(cfg):
    net = cfg.get("network", {})
    sub = net.get("subnet")
    used = {c.get("ip") for c in cfg.get("cameras", {}).values()} | {net.get("alias_ip")}
    try:
        for h in ipaddress.ip_network(sub, strict=False).hosts():
            if str(h) not in used:
                return str(h)
    except (ValueError, TypeError):
        pass
    return ""


def add_one(cfg, profiles, mac, ip, profile=None, gw=None, mask=None):
    mac = mac.lower()
    if not MAC_RE.match(mac):
        print(f"bad MAC: {mac}")
        return False
    net = cfg.get("network", {})
    gw = gw or net.get("alias_ip", "")
    mask = mask or "255.255.255.0"
    profile = profile or (next(iter(profiles)) if profiles else "rovimen")
    if profiles and profile not in profiles:
        print(f"unknown profile '{profile}' (have: {', '.join(profiles)})")
        return False
    cams = cfg.setdefault("cameras", {})
    if mac in cams:
        print(f"{mac} already in config -> {cams[mac].get('ip')}")
        return False
    if any(c.get("ip") == ip for c in cams.values()):
        print(f"IP {ip} already used by another camera")
        return False
    cams[mac] = {"ip": ip, "gw": gw, "mask": mask, "profile": profile}
    print(f"added {mac} -> {ip} (gw {gw}, mask {mask}, profile {profile})")
    return True


def interactive(cfg, profiles):
    known = set(cfg.get("cameras", {}))
    new = {m: d for m, d in ce.discover().items() if m not in known}
    if not new:
        print("no new cameras found (all known, or none answered the broadcast)")
        return False
    dprof = next(iter(profiles)) if profiles else "rovimen"
    added = False
    for mac, d in new.items():
        print(f"\nfound {mac} at {d.get('ip')}")
        if input("  add it? [Y/n] ").strip().lower() in ("n", "no"):
            continue
        sug = next_free_ip(cfg)
        ip = input(f"  static IP [{sug}]: ").strip() or sug
        prof = input(f"  profile [{dprof}]: ").strip() or dprof
        added |= add_one(cfg, profiles, mac, ip, prof)
    return added


def main():
    cfg = load(CFG)
    profiles = load(PROFILES)
    args = sys.argv[1:]
    if not args:
        changed = interactive(cfg, profiles)
    elif len(args) >= 2:
        changed = add_one(cfg, profiles, *args[:5])
    else:
        print(__doc__)
        return 1
    if changed:
        save_cfg(cfg)
        print("running cam-enforce...")
        subprocess.run([sys.executable, os.path.join(_HERE, "cam_enforce.py")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
