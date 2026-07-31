#!/usr/bin/env python3
"""Boot enforcer for XiongMai (Sofia/DVRIP) cameras.

At boot: find each configured camera by MAC via a Sofia UDP broadcast (:34569,
subnet-agnostic), pin its static IP, then re-apply its capture profile — writing
only the fields that drifted. Runs as root (systemd) so it can raise a temporary
alias to reach a camera that landed on a foreign subnet.

Config: $ROVIMEN_CAM_CONFIG, else /etc/rovimen-cam/config.json.
Run manually with: sudo cam-enforce
"""

import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)  # vendored dvrip.py lives next to this script
from dvrip import DVRIPCam  # noqa: E402

CFG_PATH = os.environ.get("ROVIMEN_CAM_CONFIG", "/etc/rovimen-cam/config.json")
# Profiles are delivered by the repo (refreshed by install.sh) and never edited
# locally — always read them from the installed canonical file next to this script.
PROFILES_PATH = os.environ.get("ROVIMEN_CAM_PROFILES", "/etc/rovimen-cam/profiles.json")
DVR_PORT, DISC_PORT = 34567, 34569
WAIT_ROUNDS, WAIT_SLEEP = 6, 20


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_cfg():
    if not os.path.exists(CFG_PATH):
        log(f"config missing: {CFG_PATH}")
        sys.exit(2)
    with open(CFG_PATH) as f:
        cfg = json.load(f)
    # Station config (cameras/network) is local; profiles come from the delivered
    # canonical file so the station always tracks the repo without local edits.
    if os.path.exists(PROFILES_PATH):
        with open(PROFILES_PATH) as f:
            cfg["profiles"] = json.load(f)
    return cfg


def ip_to_xm(ip):
    o = [int(x) for x in ip.split(".")]
    return "0x%02X%02X%02X%02X" % (o[3], o[2], o[1], o[0])


def xm_to_ip(h):
    if not h:
        return "0.0.0.0"
    v = int(h, 16)
    return ".".join(str((v >> (8 * i)) & 0xFF) for i in range(4))


def sh(*a):
    subprocess.run(a, check=False)


def _hexbool(v):
    """Truthiness of an XM field that may be a hex string ('0x00000001'), an int,
    or a bool. Unparseable/missing -> False."""
    if isinstance(v, str):
        try:
            return int(v, 16) != 0
        except ValueError:
            return False
    return bool(v)


def tcp(ip, port=DVR_PORT):
    s = socket.socket()
    s.settimeout(0.7)
    try:
        return s.connect_ex((ip, port)) == 0
    finally:
        s.close()


def login(ip, t=8):
    c = DVRIPCam(ip, port=DVR_PORT, user="admin", password="")
    c.timeout = t
    return c if c.login() else None


def mac_of(ip):
    c = login(ip, 6)
    if not c:
        return None
    nc = c.get_info("NetWork.NetCommon")
    c.close()
    return (nc.get("MAC", "").lower() if nc else None)


def discover(timeout=2):
    """Sofia UDP broadcast (:34569) -> {mac: {ip}}. Subnet-agnostic."""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.bind(("", DISC_PORT))
    except OSError as e:
        log(f"bind 34569: {e}")
        return found
    s.settimeout(1)
    s.sendto(struct.pack("BBHIIHHI", 255, 0, 0, 0, 0, 0, 1530, 0), ("255.255.255.255", DISC_PORT))
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            data, _ = s.recvfrom(8192)
        except socket.timeout:
            break
        if len(data) < 20:
            continue
        *_, msg, leng = struct.unpack("BBHIIHHI", data[:20])
        if msg == 1531 and leng > 0:
            try:
                body = json.loads(data[20:20 + leng].replace(b"\x00", b""))
            except Exception:
                continue
            nc = body.get("NetWork.NetCommon", {})
            mac = (nc.get("MAC") or "").lower()
            if mac:
                found[mac] = {"ip": xm_to_ip(nc.get("HostIP")),
                              "mask": xm_to_ip(nc.get("Submask"))}
    s.close()
    return found


def set_static(cur_ip, tgt):
    c = login(cur_ip)
    if not c:
        return False
    dh = c.get_info("NetWork.NetDHCP")
    (dh[0] if isinstance(dh, list) else dh)["Enable"] = 0
    c.set_info("NetWork.NetDHCP", dh)
    nc = c.get_info("NetWork.NetCommon")
    nc["HostIP"] = ip_to_xm(tgt["ip"])
    nc["GateWay"] = ip_to_xm(tgt["gw"])
    nc["Submask"] = ip_to_xm(tgt["mask"])
    c.set_info("NetWork.NetCommon", nc)
    c.close()
    return True


def temp_alias(cam_ip, mask):
    """Pick a host address + prefix on the camera's OWN subnet so the station is
    on-link with it. Uses the mask the camera reported over Sofia; falls back to
    /24 only when it is missing or unparseable. A /24 guess is wrong when the
    camera sits on a narrower subnet — it would land off-link and never answer."""
    mask = mask if (mask and mask != "0.0.0.0") else "255.255.255.0"
    try:
        net = ipaddress.ip_network(f"{cam_ip}/{mask}", strict=False)
        host = next(str(h) for h in net.hosts() if str(h) != cam_ip)
        return host, net.prefixlen
    except (ValueError, StopIteration):
        base = cam_ip.rsplit(".", 1)[0]
        return (base + (".2" if cam_ip.endswith(".1") else ".1")), 24


def ensure_ip(mac, tgt, devs, iface):
    if tcp(tgt["ip"]) and mac_of(tgt["ip"]) == mac:
        log(f"[{mac}] IP OK {tgt['ip']}")
        return True
    cur = devs.get(mac)
    if not cur or cur["ip"] == "0.0.0.0":
        log(f"[{mac}] no broadcast reply")
        return False
    if cur["ip"] == tgt["ip"] and tcp(tgt["ip"]):
        return True
    log(f"[{mac}] drifted to {cur['ip']} -> {tgt['ip']}")
    if tcp(cur["ip"]):
        ok = set_static(cur["ip"], tgt)
    else:
        temp, plen = temp_alias(cur["ip"], cur.get("mask"))
        log(f"[{mac}] temporary alias {temp}/{plen} on {iface} (camera mask {cur.get('mask')})")
        sh("ip", "addr", "add", f"{temp}/{plen}", "dev", iface)
        try:
            ok = set_static(cur["ip"], tgt)
        finally:
            sh("ip", "addr", "del", f"{temp}/{plen}", "dev", iface)
    time.sleep(6)
    good = tcp(tgt["ip"]) and mac_of(tgt["ip"]) == mac
    log(f"[{mac}] {'IP enforced' if good else 'set sent (may need camera reboot)'} (ok={ok})")
    return good


def ensure_profile(ip, prof, allow_reboot=True, orient=None):
    c = login(ip)
    if not c:
        log(f"[{ip}] profile: login failed")
        return
    ch = []
    reboot_needed = False
    enc = c.get_info("Simplify.Encode")
    v = enc[0]["MainFormat"]["Video"]
    tgt_enc = prof.get("encode", {})
    diff = any(v.get(k) != val for k, val in tgt_enc.items())
    extra = enc[0].get("ExtraFormat")
    want_sub = prof.get("substream", False)
    sub_on = bool(extra) and extra.get("VideoEnable") not in (0, False, None)
    if diff or (sub_on != want_sub):
        v.update(tgt_enc)
        if extra:
            extra["VideoEnable"] = bool(want_sub)
        c.set_info("Simplify.Encode", enc)
        ch.append(f"Encode {tgt_enc.get('Resolution')}/{tgt_enc.get('BitRateControl')}/{tgt_enc.get('BitRate')}")
        reboot_needed = True  # encoding (res/CBR/compression) only applies after a reboot
        c.close()
        time.sleep(3)
        c = login(ip)
        if not c:
            log(f"[{ip}] reconnect failed after Encode")
            return
    cam = c.get_info("Camera")
    p = (cam.get("Param") or [{}])[0]
    for k, val in prof.get("param", {}).items():
        if p.get(k) != val:
            c.set_info(f"Camera.Param.[0].{k}", val)
            ch.append(f"{k}={val}")
    exp = prof.get("exposure")
    if exp and (p.get("ExposureParam") or {}).get("MostTime") != exp.get("MostTime"):
        c.set_info("Camera.Param.[0].ExposureParam", exp)
        ch.append("Exposure")
    g = prof.get("gain")
    if g:
        cur = p.get("GainParam") or {}
        if cur.get("AutoGain") != g.get("AutoGain") or cur.get("Gain") != g.get("Gain"):
            c.set_info("Camera.Param.[0].GainParam", g)
            ch.append("Gain")
    cf = prof.get("clearfog")
    if cf:
        cur = (cam.get("ClearFog") or [{}])[0]
        if bool(cur.get("enable")) != bool(cf.get("enable")) or cur.get("level") != cf.get("level"):
            c.set_info("Camera.ClearFog.[0].enable", bool(cf.get("enable")))
            c.set_info("Camera.ClearFog.[0].level", cf.get("level", 30))
            ch.append("ClearFog")
    style = prof.get("style")
    if style:
        px = (cam.get("ParamEx") or [{}])[0]
        if px.get("Style") != style:
            c.set_info("Camera.ParamEx.[0]", {"Style": style})
            ch.append(f"Style={style}")
    # Per-camera image orientation (from the station config, not the shared profile):
    # a 180° upside-down mount = flip + mirror. On XM firmware these live in
    # Camera.Param.[0] as PictureFlip/PictureMirror, hex-string encoded
    # ("0x00000000"/"0x00000001") — NOT ParamEx.Mirror/Flip (writing there is a
    # no-op). Only write what the config specifies and only if it drifted.
    if orient:
        mirror, flip = orient
        for key, want in (("PictureFlip", flip), ("PictureMirror", mirror)):
            if want is not None and _hexbool(p.get(key)) != bool(want):
                c.set_info(f"Camera.Param.[0].{key}", "0x00000001" if want else "0x00000000")
                ch.append(f"{key}={int(bool(want))}")
    col = prof.get("color")
    if col:
        try:
            vc = c.get_info("AVEnc.VideoColor")
            # Structure varies by firmware: a dict, a list of channels, or a list
            # of lists (channel -> time-segments). Descend to the first dict.
            node = vc
            while isinstance(node, list) and node:
                node = node[0]
            # Colour fields live either under VideoColorParam or directly in the dict.
            target = node.get("VideoColorParam") if isinstance(node, dict) else None
            if not isinstance(target, dict):
                target = node if (isinstance(node, dict) and "Brightness" in node) else None
            if isinstance(target, dict) and any(target.get(k) != val for k, val in col.items()):
                target.update(col)
                c.set_info("AVEnc.VideoColor.[0]", vc[0] if isinstance(vc, list) else vc)
                ch.append("Color")
        except Exception as e:
            log(f"[{ip}] color skip: {e}")
    show = bool(prof.get("osd", False))
    w = c.get_info("AVEnc.VideoWidget")
    wch = False
    for it in (w if isinstance(w, list) else [w]):
        for a in ("TimeTitleAttribute", "ChannelTitleAttribute"):
            if a in it and (bool(it[a].get("EncodeBlend")) != show or bool(it[a].get("PreviewBlend")) != show):
                it[a]["EncodeBlend"] = show
                it[a]["PreviewBlend"] = show
                wch = True
    if wch:
        c.set_info("AVEnc.VideoWidget", w)
        ch.append(f"OSD {'on' if show else 'off'}")
    did_reboot = reboot_needed and allow_reboot
    if did_reboot:
        log(f"[{ip}] encoding changed -> rebooting camera to apply")
        try:
            c.reboot()
        except Exception as e:
            log(f"[{ip}] reboot error: {e}")
    c.close()
    tag = " [rebooted]" if did_reboot else ""
    log(f"[{ip}] profile '{prof.get('_name', '?')}': {', '.join(ch) if ch else 'already OK'}{tag}")


def _profile_for(profiles, tgt):
    prof = dict(profiles.get(tgt.get("profile", "default"), {}))
    prof["_name"] = tgt.get("profile")
    return prof


def _orient(tgt):
    """Desired image orientation for one camera, from its config entry. Returns
    (mirror, flip) where None on either means 'not specified — leave as-is', or
    None when the config says nothing about orientation (skip the whole check).

    Optional fields: "rotate180"/"invert" (shorthand — sets both mirror+flip, for a
    camera mounted upside-down), overridable by granular "mirror"/"flip" booleans.
    """
    keys = ("rotate180", "invert", "mirror", "flip")
    if not any(k in tgt for k in keys):
        return None
    base = bool(tgt.get("rotate180") or tgt.get("invert"))
    mirror = bool(tgt["mirror"]) if "mirror" in tgt else (True if base else None)
    flip = bool(tgt["flip"]) if "flip" in tgt else (True if base else None)
    return (mirror, flip)


def sync_profiles(cfg):
    """Apply each camera's profile at its configured IP — no discovery, no IP
    change. Use after editing a profile in config.json to push it to the cameras
    (check-before-write, so only changed fields are sent). Returns exit code."""
    profiles = cfg.get("profiles", {})
    rc = 0
    for mac, tgt in cfg.get("cameras", {}).items():
        if not tcp(tgt["ip"]):
            log(f"[{mac}] {tgt['ip']} unreachable — skip (run cam-enforce first?)")
            rc = 1
            continue
        ensure_profile(tgt["ip"], _profile_for(profiles, tgt), orient=_orient(tgt))
    return rc


def reboot_cameras(cfg):
    """Reboot every camera listed in config, at its configured IP."""
    rc = 0
    for mac, tgt in cfg.get("cameras", {}).items():
        c = login(tgt["ip"], 8)
        if not c:
            log(f"[{mac}] {tgt['ip']} unreachable — skip")
            rc = 1
            continue
        try:
            c.reboot()
            log(f"[{mac}] {tgt['ip']} reboot sent")
        except Exception as e:
            log(f"[{mac}] reboot error: {e}")
            rc = 1
        finally:
            c.close()
    return rc


def enforce_all(cfg):
    """Discover + pin IP + apply profile for every camera in config. Returns 0
    when all cameras were handled, 1 otherwise."""
    iface = cfg.get("iface", "eno1")
    profiles = cfg.get("profiles", {})
    pending = dict(cfg.get("cameras", {}))
    for _ in range(WAIT_ROUNDS):
        devs = discover()
        for mac in list(pending):
            tgt = pending[mac]
            if ensure_ip(mac, tgt, devs, iface):
                ensure_profile(tgt["ip"], _profile_for(profiles, tgt), orient=_orient(tgt))
                del pending[mac]
        if not pending:
            break
        log(f"{len(pending)} pending, retry in {WAIT_SLEEP}s...")
        time.sleep(WAIT_SLEEP)
    return 0 if not pending else 1


def main():
    cfg = load_cfg()
    if "--sync" in sys.argv[1:]:
        # profiles-only: push config profiles to cameras already at their IPs
        sys.exit(sync_profiles(cfg))
    if "--reboot" in sys.argv[1:]:
        sys.exit(reboot_cameras(cfg))
    sys.exit(enforce_all(cfg))


if __name__ == "__main__":
    main()
