#!/usr/bin/env python3
"""cam-help — reference for the station-cam commands (a mini man page)."""

import json
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
CFG = os.environ.get("ROVIMEN_CAM_CONFIG", "/etc/rovimen-cam/config.json")
PROFILES = os.environ.get("ROVIMEN_CAM_PROFILES", "/etc/rovimen-cam/profiles.json")

COMMANDS = [
    ("cam-net up|down|status|cam",
     "Private camera network. 'up' raises the alias + NAT + per-camera NTP;",
     "'down' removes them; 'status' shows alias/NAT/cameras; 'cam' re-applies NTP only.",
     "Runs as root (systemd). Reads network + cameras from the config."),
    ("cam-enforce",
     "Discover each camera by MAC (Sofia broadcast :34569), pin its static IP",
     "(raising a temporary alias if it drifted to another subnet), then apply its",
     "capture profile. This is what the boot service runs. No parameters."),
    ("cam-sync",
     "Push the delivered profiles to the cameras at their configured IPs, without",
     "discovery/IP-enforcement. Only changed fields are written; a camera is",
     "rebooted only when its encoding changed. No parameters."),
    ("cam-profiles-update [--apply]",
     "List the capture profiles delivered by the repo (read from profiles.json).",
     "With --apply, also run cam-sync to push them to the cameras. Never edits the",
     "local config."),
    ("cam-reboot",
     "Reboot every camera listed in the config. No parameters."),
    ("cam-add <mac> <ip> [profile] [gw] [mask]",
     "Add a camera to config.json (gw/mask default to the network; profile to the",
     "first delivered one) and run cam-enforce. With no arguments it scans for",
     "cameras not yet in the config and prompts (interactive onboarding)."),
    ("cam-find [interface|prefix]",
     "Scan for XM cameras on :34567 and tag the ones known in the config by MAC.",
     "Default: every /24 the station has an address on (main LAN + camera alias if",
     "up). Pass an interface (eno1) or prefix (10.42.0) to scan just one subnet."),
    ("cam-ip <current_ip> show|dhcp|static <new_ip> [gw] [mask]",
     "Inspect or change one camera's network config. 'show' prints NetCommon+DHCP;",
     "'dhcp' switches it to DHCP; 'static <ip> [gw] [mask]' sets a static IP",
     "(gw defaults to .1, mask to 255.255.255.0). Connects to <current_ip>."),
    ("cam-recovery-test dhcp|random-subnet|profiles [--yes]",
     "DESTRUCTIVE chaos test: break a camera (DHCP, random subnet, or scrambled",
     "capture profile), then verify the tooling recovers it. Prints the plan",
     "without --yes. Reboots cameras; takes a few minutes."),
    ("cam-dashboard",
     "Local web dashboard: a table of cameras with Play (HLS), Config, Rotate 180°",
     "and Reboot per camera, an Orientation column, and 'Toate camerele' (all cameras",
     "in one grid). systemd service, disabled by default (systemctl enable --now",
     "cam-dashboard). Config: /etc/rovimen-cam/dashboard.json."),
    ("cam-health [--reboot] [--json]",
     "Video-freeze watchdog. Reads RMS's own output (FF files) — never opens a",
     "second RTSP stream, so the RMS/GMN capture is untouched. A camera whose RMS",
     "is capturing but whose frames stalled is FROZEN; --reboot power-cycles it",
     "(cooldown-limited). Opt-in timer: systemctl enable --now cam-health.timer."),
    ("cam-help",
     "This reference.", "", ""),
]

FILES = [
    ("/etc/rovimen-cam/config.json", "station config — cameras + network (edit this; never overwritten)"),
    ("/etc/rovimen-cam/profiles.json", "capture profiles — delivered by the repo, refreshed each install"),
    ("/etc/rovimen-cam/dashboard.json", "dashboard settings (bind/port/stream) — station-local, seeded once"),
    ("/usr/local/lib/rovimen-cam/", "installed scripts + vendored dvrip + .venv"),
]

SERVICES = [
    ("cam-net.service", "boot: raise the private camera network"),
    ("cam-enforce.service", "boot: enforce IPs + profiles (after cam-net)"),
    ("cam-sync.path", "opt-in: run cam-sync when the config changes"),
    ("cam-dashboard.service", "opt-in: local web view of the cameras (HLS)"),
    ("cam-health.timer", "opt-in: periodic video-freeze watchdog (reboots frozen cameras)"),
]

FLOWS = [
    ("Add a camera",
     "sudo $EDITOR /etc/rovimen-cam/config.json   # add <MAC>: {ip, gw, mask, profile}",
     "sudo cam-enforce"),
    ("A profile changed in the repo",
     "cd <repo>/station-cam && git pull && bash install.sh",
     "sudo cam-profiles-update --apply"),
    ("Recover a lost camera now",
     "sudo cam-net up && sudo cam-enforce", ""),
    ("Fix an upside-down camera (in software)",
     'add  "rotate180": true  to that camera in config.json  (or "mirror"/"flip")',
     "sudo cam-sync"),
]

# Optional per-camera fields in config.json (besides ip/gw/mask/profile).
CAM_FIELDS = [
    ("profile", 'capture profile name from profiles.json (e.g. "rovimen")'),
    ("rotate180", "true = correct an upside-down mount (image flip + mirror), in software"),
    ("mirror", "true = horizontal flip only (overrides rotate180 on that axis)"),
    ("flip", "true = vertical flip only (overrides rotate180 on that axis)"),
]


def section(title):
    print(f"\n\033[1m{title}\033[0m" if sys.stdout.isatty() else f"\n{title}")


def main():
    print("station-cam — XiongMai camera management for a meteor station")
    section("COMMANDS")
    for name, *lines in COMMANDS:
        print(f"  {name}")
        for ln in lines:
            if ln:
                print(f"      {ln}")
    section("FILES")
    for path, desc in FILES:
        print(f"  {path}\n      {desc}")
    section("SERVICES (systemctl status/enable ...)")
    for name, desc in SERVICES:
        print(f"  {name:22} {desc}")
    section("COMMON FLOWS")
    for title, *cmds in FLOWS:
        print(f"  {title}:")
        for cmd in cmds:
            if cmd:
                print(f"      {cmd}")

    section("PER-CAMERA CONFIG FIELDS (config.json)")
    for name, desc in CAM_FIELDS:
        print(f"  {name:12} {desc}")
    print("  orientation is enforced at every boot and re-applied after a firmware reset")

    section("CURRENT CONFIG")
    try:
        cams = json.load(open(CFG)).get("cameras", {}) if os.path.exists(CFG) else {}
        profs = json.load(open(PROFILES)) if os.path.exists(PROFILES) else {}
        print(f"  cameras ({CFG}):")
        for mac, t in cams.items() or [("(none)", {})]:
            print(f"      {mac}  ->  {t.get('ip', '?')}  profile={t.get('profile', '?')}")
        print(f"  delivered profiles: {', '.join(profs) or '(none)'}")
    except Exception as e:
        print(f"  (could not read config: {e})")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
