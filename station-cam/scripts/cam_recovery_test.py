#!/usr/bin/env python3
"""Chaos/recovery test for the station-cam enforcer.

DESTRUCTIVE: deliberately breaks each configured camera (network or capture
profile), then verifies the tooling recovers it. Run on the station as root,
with cam-net already up (alias reachable).

Modes:
  cam-recovery-test dhcp --yes            # cameras -> DHCP, reboot, recover, reboot, verify
  cam-recovery-test random-subnet --yes   # camera -> random subnet, recover, verify
  cam-recovery-test profiles --yes        # scramble capture profile, cam-sync, verify

Without --yes it only prints the plan.
"""

import os
import random
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
import cam_enforce as ce  # noqa: E402


def wait_for_mac(mac, timeout=200):
    """Poll the Sofia broadcast until `mac` answers; return its current IP."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        devs = ce.discover()
        if mac in devs and devs[mac]["ip"] not in ("", "0.0.0.0"):
            return devs[mac]["ip"]
        time.sleep(5)
    return None


def wait_for_up(ip, timeout=200, settle=10):
    """Wait until the camera answers on :34567 (fully booted, cam-sync-able).
    The `settle` sleep lets a just-issued reboot actually start before we poll,
    so we don't catch the camera in the brief window before it goes down."""
    time.sleep(settle)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ce.tcp(ip):
            return True
        time.sleep(5)
    return False


def break_to_dhcp(ip):
    c = ce.login(ip)
    if not c:
        return False
    dh = c.get_info("NetWork.NetDHCP")
    (dh[0] if isinstance(dh, list) else dh)["Enable"] = 1
    c.set_info("NetWork.NetDHCP", dh)
    c.reboot()
    c.close()
    return True


def break_to_random_subnet(ip):
    """Move the camera to an unlikely private subnet and reboot it."""
    net = f"172.{random.randint(16, 31)}.{random.randint(2, 250)}"
    new_ip = f"{net}.{random.randint(10, 240)}"
    c = ce.login(ip)
    if not c:
        return None
    dh = c.get_info("NetWork.NetDHCP")
    (dh[0] if isinstance(dh, list) else dh)["Enable"] = 0
    c.set_info("NetWork.NetDHCP", dh)
    nc = c.get_info("NetWork.NetCommon")
    nc["HostIP"] = ce.ip_to_xm(new_ip)
    nc["GateWay"] = ce.ip_to_xm(net + ".1")
    nc["Submask"] = ce.ip_to_xm("255.255.255.0")
    c.set_info("NetWork.NetCommon", nc)
    try:
        c.reboot()
    except Exception:
        pass
    c.close()
    return new_ip


def verify(mac, tgt):
    if not ce.tcp(tgt["ip"]):
        return False, f"unreachable at {tgt['ip']}"
    got = ce.mac_of(tgt["ip"])
    if got != mac:
        return False, f"MAC mismatch (got {got})"
    return True, f"reachable at {tgt['ip']}, MAC OK"


def break_profile(ip):
    """Scramble the camera's capture settings away from any sane profile."""
    c = ce.login(ip)
    if not c:
        return False
    enc = c.get_info("Simplify.Encode")
    v = enc[0]["MainFormat"]["Video"]
    v["Resolution"] = "3M" if v.get("Resolution") != "3M" else "1080P"
    v["BitRate"] = 1024
    v["BitRateControl"] = "VBR"
    c.set_info("Simplify.Encode", enc)      # staged in flash (needs reboot to apply)
    c.close()
    time.sleep(3)
    c = ce.login(ip)
    if not c:
        return False
    c.set_info("Camera.Param.[0].DayNightColor", "0x00000002")
    c.set_info("Camera.Param.[0].GainParam", {"AutoGain": 1, "Gain": 20})
    c.set_info("Camera.Param.[0].ExposureParam",
               {"LeastTime": "0x00000100", "Level": 0, "MostTime": "0x00000400"})
    w = c.get_info("AVEnc.VideoWidget")
    for it in (w if isinstance(w, list) else [w]):
        for a in ("TimeTitleAttribute", "ChannelTitleAttribute"):
            if isinstance(it.get(a), dict):
                it[a]["EncodeBlend"] = True
                it[a]["PreviewBlend"] = True
    c.set_info("AVEnc.VideoWidget", w)
    c.close()
    return True


def verify_profile(ip, prof):
    """Read the camera back and check it matches the delivered profile."""
    c = ce.login(ip)
    if not c:
        return False, "unreachable"
    bad = []
    enc = c.get_info("Simplify.Encode")[0]["MainFormat"]["Video"]
    for k, val in prof.get("encode", {}).items():
        if enc.get(k) != val:
            bad.append(f"enc.{k}={enc.get(k)}")
    p = (c.get_info("Camera").get("Param") or [{}])[0]
    for k, val in prof.get("param", {}).items():
        if p.get(k) != val:
            bad.append(f"{k}={p.get(k)}")
    exp = prof.get("exposure") or {}
    if exp and (p.get("ExposureParam") or {}).get("MostTime") != exp.get("MostTime"):
        bad.append("exposure")
    g = prof.get("gain") or {}
    if g and (p.get("GainParam") or {}).get("Gain") != g.get("Gain"):
        bad.append(f"gain={(p.get('GainParam') or {}).get('Gain')}")
    c.close()
    return (not bad), ("profile matches" if not bad else "; ".join(bad))


def report(results):
    print("\n=== RESULT ===")
    ok = True
    for mac, (passed, msg) in results.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {mac}: {msg}")
        ok = ok and passed
    print("=== ALL PASS ===" if ok else "=== FAILURES ===")
    return 0 if ok else 1


def run_dhcp(cfg, cameras):
    print("STEP 1: set each camera to DHCP and reboot")
    for mac, tgt in cameras.items():
        print(f"  [{mac}] {tgt['ip']} -> DHCP + reboot:", break_to_dhcp(tgt["ip"]))

    print("STEP 2: wait for cameras to come back, then cam-find")
    for mac in cameras:
        landed = wait_for_mac(mac)
        print(f"  [{mac}] reappeared at {landed}")
    ce.reboot_cameras  # noqa: B018  (keep import warm; discover already used)
    print("  (cam-find view via broadcast:)")
    for m, d in ce.discover().items():
        print(f"    {m}  {d['ip']}")

    print("STEP 3: apply modifications (cam-enforce): recover IP + profile")
    ce.enforce_all(cfg)

    print("STEP 4: reboot cameras again")
    ce.reboot_cameras(cfg)
    for mac in cameras:
        wait_for_mac(mac)

    print("STEP 5: verify")
    return {mac: verify(mac, tgt) for mac, tgt in cameras.items()}


def run_random(cfg, cameras):
    print("STEP 1: move each camera to a random subnet and reboot")
    for mac, tgt in cameras.items():
        landed = break_to_random_subnet(tgt["ip"])
        print(f"  [{mac}] {tgt['ip']} -> {landed} + reboot")

    print("STEP 2: wait for cameras (broadcast reaches any subnet)")
    for mac in cameras:
        print(f"  [{mac}] answers broadcast at {wait_for_mac(mac)}")

    print("STEP 3: recover (cam-enforce): broadcast -> temp alias -> static IP -> profile")
    ce.enforce_all(cfg)

    print("STEP 4: verify")
    return {mac: verify(mac, tgt) for mac, tgt in cameras.items()}


def run_profiles(cfg, cameras):
    profiles = cfg.get("profiles", {})
    print("STEP 1: scramble each camera's capture profile")
    for mac, tgt in cameras.items():
        print(f"  [{mac}] scramble:", break_profile(tgt["ip"]))

    print("STEP 2: restore (cam-sync): apply the delivered profiles")
    ce.sync_profiles(cfg)

    print("STEP 3: wait for cameras to finish rebooting (cam-sync reboots on encoding)")
    for tgt in cameras.values():
        wait_for_up(tgt["ip"])

    print("STEP 4: verify against the delivered profile")
    return {mac: verify_profile(tgt["ip"], ce._profile_for(profiles, tgt))
            for mac, tgt in cameras.items()}


def main():
    args = sys.argv[1:]
    mode = args[0] if args else ""
    if mode not in ("dhcp", "random-subnet", "profiles"):
        print(__doc__)
        return 1

    cfg = ce.load_cfg()
    cameras = cfg.get("cameras", {})
    if not cameras:
        print("no cameras in config")
        return 2

    print(f"MODE: {mode}   cameras: {list(cameras)}")
    if "--yes" not in args:
        print("\nThis is DESTRUCTIVE (breaks the camera's network or profile, reboots it).")
        print("Re-run with --yes to actually do it, e.g.:")
        print(f"  sudo cam-recovery-test {mode} --yes")
        return 0
    if not ce.tcp(next(iter(cameras.values()))["ip"]):
        print("WARN: first camera not reachable at its IP — is cam-net up? (sudo cam-net up)")

    runner = {"dhcp": run_dhcp, "random-subnet": run_random, "profiles": run_profiles}[mode]
    return report(runner(cfg, cameras))


if __name__ == "__main__":
    sys.exit(main())
