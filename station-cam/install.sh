#!/usr/bin/env bash
# station-cam installer. Run as a normal user WITH sudo rights (no root login):
#   bash install.sh
#
# Copies scripts -> /usr/local/lib/rovimen-cam (+ isolated .venv), CLI wrappers ->
# /usr/local/bin, config -> /etc/rovimen-cam, and the two systemd units. Idempotent.
set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
LIB=/usr/local/lib/rovimen-cam
ETC=/etc/rovimen-cam
BIN=/usr/local/bin

[ -f "$SRC/scripts/dvrip.py" ] || { echo "ERROR: vendored scripts/dvrip.py missing." >&2; exit 1; }

echo "== system dependencies =="
# Every ROVIMEN-supported OS (Ubuntu 24.04 Desktop/Server, Debian 11/12,
# Raspberry Pi OS 64-bit) is Debian-based, so we best-effort apt-get what the
# tooling needs. python3-venv is the key one: it is NOT preinstalled on Server,
# Debian, or Pi OS, and without it `python3 -m venv` (below) aborts the install.
need=""
python3 -c 'import venv, ensurepip' 2>/dev/null || need="$need python3-venv"
command -v ip >/dev/null 2>&1 || need="$need iproute2"
command -v iptables >/dev/null 2>&1 || need="$need iptables"
if [ -n "$need" ]; then
    if command -v apt-get >/dev/null 2>&1; then
        echo "  installing:$need"
        sudo apt-get update -qq && sudo apt-get install -y -q $need
    else
        echo "  WARN: missing:$need — install them with your package manager" >&2
    fi
fi
command -v ffmpeg >/dev/null 2>&1 || \
    echo "  note: ffmpeg not found — needed only for 'cam-dashboard' (sudo apt-get install ffmpeg)"

echo "== library -> $LIB =="
sudo mkdir -p "$LIB" "$ETC"
sudo cp "$SRC"/scripts/*.py "$SRC"/scripts/*.sh "$SRC"/scripts/*.js "$LIB"/
sudo chmod 0755 "$LIB"/*.py "$LIB"/*.sh

echo "== venv -> $LIB/.venv =="
[ -x "$LIB/.venv/bin/python" ] || sudo python3 -m venv "$LIB/.venv"
if ! sudo "$LIB/.venv/bin/pip" install -q --upgrade pip -r "$SRC/requirements.txt"; then
    echo "  WARN: pip install failed (offline?). venv created; scripts run without it" >&2
fi

echo "== CLI wrappers -> $BIN =="
# cam-net is bash + self-locating: a symlink is enough (readlink resolves to $LIB)
sudo ln -sf "$LIB/cam-net.sh" "$BIN/cam-net"
# python CLIs run through the venv interpreter
for name in cam-enforce:cam_enforce cam-find:cam_find cam-ip:cam_ip; do
    cli="${name%%:*}"; script="${name##*:}"
    printf '#!/bin/sh\nexec %s/.venv/bin/python %s/%s.py "$@"\n' "$LIB" "$LIB" "$script" \
        | sudo tee "$BIN/$cli" >/dev/null
    sudo chmod 0755 "$BIN/$cli"
done
sudo rm -f "$BIN/findcam" "$BIN/camip"   # legacy names, renamed to cam-find / cam-ip
# cam-sync = cam-enforce in profiles-only mode (push config profiles to cameras)
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/cam_enforce.py --sync "$@"\n' "$LIB" "$LIB" \
    | sudo tee "$BIN/cam-sync" >/dev/null
sudo chmod 0755 "$BIN/cam-sync"
# cam-profiles-update = refresh live config profiles from the canonical package copy
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/cam_profiles_update.py "$@"\n' "$LIB" "$LIB" \
    | sudo tee "$BIN/cam-profiles-update" >/dev/null
sudo chmod 0755 "$BIN/cam-profiles-update"
# cam-reboot = reboot every camera in config
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/cam_enforce.py --reboot "$@"\n' "$LIB" "$LIB" \
    | sudo tee "$BIN/cam-reboot" >/dev/null
sudo chmod 0755 "$BIN/cam-reboot"
# cam-add = add a camera to config + enforce (scriptable or interactive)
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/cam_add.py "$@"\n' "$LIB" "$LIB" \
    | sudo tee "$BIN/cam-add" >/dev/null
sudo chmod 0755 "$BIN/cam-add"
# cam-recovery-test = DESTRUCTIVE chaos/recovery test (needs --yes)
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/cam_recovery_test.py "$@"\n' "$LIB" "$LIB" \
    | sudo tee "$BIN/cam-recovery-test" >/dev/null
sudo chmod 0755 "$BIN/cam-recovery-test"
# cam-help = command reference (mini man page)
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/cam_help.py "$@"\n' "$LIB" "$LIB" \
    | sudo tee "$BIN/cam-help" >/dev/null
sudo chmod 0755 "$BIN/cam-help"
# cam-dashboard = local web view (also a systemd service; opt-in)
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/cam_dashboard.py "$@"\n' "$LIB" "$LIB" \
    | sudo tee "$BIN/cam-dashboard" >/dev/null
sudo chmod 0755 "$BIN/cam-dashboard"

echo "== config -> $ETC/ =="
# profiles.json is delivered by the repo — always refreshed from it
sudo cp "$SRC/config/profiles.json" "$ETC/profiles.json"
echo "  profiles.json (delivered) refreshed"
# config.json + dashboard.json are station-specific — seed once, never overwrite
if [ -f "$ETC/config.json" ]; then
    echo "  kept existing config.json (station cameras)"
else
    sudo cp "$SRC/config/config.example.json" "$ETC/config.json"
    echo "  seeded config.json — set your cameras:  sudo \$EDITOR $ETC/config.json"
fi
if [ -f "$ETC/dashboard.json" ]; then
    echo "  kept existing dashboard.json"
else
    sudo cp "$SRC/config/dashboard.example.json" "$ETC/dashboard.json"
    echo "  seeded dashboard.json (bind/port/stream)"
fi
# remove the old delivered-profiles location from earlier installs
sudo rm -f "$LIB/profiles.json"

echo "== systemd units =="
sudo cp "$SRC"/services/*.service /etc/systemd/system/
[ -n "$(ls "$SRC"/services/*.path 2>/dev/null)" ] && sudo cp "$SRC"/services/*.path /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cam-net.service cam-enforce.service
# cam-sync.path (auto-apply profiles when config.json changes) is OPT-IN.

echo "== done =="
echo "Run 'cam-help' for the full command reference."
echo "Edit $ETC/config.json (set your camera MACs), then:"
echo "  sudo systemctl start cam-net cam-enforce"
echo "  sudo cam-find            # discover cameras / MACs"
echo "  sudo cam-sync            # push config profiles to the cameras"
echo "  sudo cam-profiles-update --apply   # show delivered profiles + push them to cameras"
echo "  sudo cam-reboot          # reboot every camera in config"
echo "  sudo systemctl enable --now cam-sync.path    # (optional) auto cam-sync on config change"
echo "  sudo systemctl enable --now cam-dashboard    # (optional) local web view of the cameras"
