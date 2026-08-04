#!/usr/bin/env bash
# station-cam installer. Run as a normal user WITH sudo rights (no root login):
#   bash install.sh [--iface <name>]
#
# Copies scripts -> /usr/local/lib/rovimen-cam (+ isolated .venv), CLI wrappers ->
# /usr/local/bin, config -> /etc/rovimen-cam, and the two systemd units. Idempotent.
#
#   --iface <name>  interface the camera alias lives on. Default: auto-detected
#                   (the NIC carrying the default route — the station uplink, which
#                   is where cam-net already adds the alias + NAT). Pass it only when
#                   the camera network must sit on a different NIC than eno1/eth0/…
set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
LIB=/usr/local/lib/rovimen-cam
ETC=/etc/rovimen-cam
BIN=/usr/local/bin

IFACE_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --iface) IFACE_ARG="${2:?--iface needs a name, e.g. --iface eth0}"; shift 2 ;;
        --iface=*) IFACE_ARG="${1#*=}"; shift ;;
        -h|--help) echo "usage: install.sh [--iface <name>]"; exit 0 ;;
        *) echo "unknown arg: $1  (use: install.sh [--iface <name>])" >&2; exit 2 ;;
    esac
done

# Primary interface = the NIC with the default route (the uplink cam-net also
# NATs through). Falls back to the first real, up IPv4 iface if no default route.
detect_iface() {
    local i
    i=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
    [ -n "$i" ] && { echo "$i"; return; }
    ip -o -4 addr show up 2>/dev/null \
        | awk '$2!="lo" && $2!~/^(docker|veth|br-|tailscale|virbr|wg|lo)/ {print $2; exit}'
}

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
# cam-health = video-freeze watchdog (RMS-output driven; also an opt-in timer)
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/cam_health.py "$@"\n' "$LIB" "$LIB" \
    | sudo tee "$BIN/cam-health" >/dev/null
sudo chmod 0755 "$BIN/cam-health"

echo "== config -> $ETC/ =="
# profiles.json is delivered by the repo — always refreshed from it
sudo cp "$SRC/config/profiles.json" "$ETC/profiles.json"
echo "  profiles.json (delivered) refreshed"
# config.json + dashboard.json are station-specific — seed once, never overwrite
if [ -f "$ETC/config.json" ]; then
    echo "  kept existing config.json (station cameras)"
    SEEDED_CONFIG=0
else
    sudo cp "$SRC/config/config.example.json" "$ETC/config.json"
    echo "  seeded config.json — set your cameras:  sudo \$EDITOR $ETC/config.json"
    SEEDED_CONFIG=1
fi
if [ -f "$ETC/dashboard.json" ]; then
    echo "  kept existing dashboard.json"
else
    sudo cp "$SRC/config/dashboard.example.json" "$ETC/dashboard.json"
    echo "  seeded dashboard.json (bind/port/stream)"
fi
# remove the old delivered-profiles location from earlier installs
sudo rm -f "$LIB/profiles.json"

echo "== camera-network interface =="
# config.json's "iface" is the single source of truth (cam-net + the netplan pin
# below both read it). Resolve it once here:
#   --iface <name>       -> explicit override (always wins)
#   fresh seed, no flag  -> auto-detect the uplink NIC (default-route iface)
#   existing config      -> leave whatever the station already set
CUR_IFACE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("iface","eno1"))' "$ETC/config.json" 2>/dev/null || echo eno1)
if [ -n "$IFACE_ARG" ]; then
    WANT_IFACE="$IFACE_ARG"
elif [ "$SEEDED_CONFIG" = 1 ]; then
    WANT_IFACE="$(detect_iface)"; [ -n "$WANT_IFACE" ] || WANT_IFACE="$CUR_IFACE"
else
    WANT_IFACE="$CUR_IFACE"
fi
if [ "$WANT_IFACE" != "$CUR_IFACE" ]; then
    python3 - "$ETC/config.json" "$WANT_IFACE" <<'PY' | sudo tee "$ETC/config.json.tmp" >/dev/null
import json, sys
d = json.load(open(sys.argv[1])); d["iface"] = sys.argv[2]
print(json.dumps(d, indent=2))
PY
    sudo mv "$ETC/config.json.tmp" "$ETC/config.json"
    echo "  iface set to '$WANT_IFACE' in config.json"
else
    echo "  iface = '$WANT_IFACE'"
fi
if ! ip link show "$WANT_IFACE" >/dev/null 2>&1; then
    echo "  WARN: interface '$WANT_IFACE' not present on this host — set the right one" >&2
    echo "        with:  bash install.sh --iface <name>   (or edit $ETC/config.json)" >&2
fi

echo "== persistent camera alias (netplan) =="
# Pin the private camera alias in netplan so systemd-networkd keeps it across
# DHCP renews and networkd restarts (e.g. unattended-upgrades running netplan
# apply). Without this, a networkd restart flushes the alias cam-net added and
# the cameras drop off until the next boot. Only on netplan systems (Ubuntu);
# elsewhere cam-net keeps adding the alias at boot as before.
IFACE="$WANT_IFACE"
read -r ALIAS PREFIX <<EOF
$(python3 - "$ETC/config.json" <<'PY'
import json, sys
n = json.load(open(sys.argv[1])).get("network", {})
print(n.get("alias_ip", "10.42.0.1"), n.get("prefix", 24))
PY
)
EOF
NP=/etc/netplan/99-rovimen-cam.yaml
if command -v netplan >/dev/null 2>&1 && ip link show "$IFACE" >/dev/null 2>&1; then
    printf '# station-cam: persist the private camera alias (mirrors config.json).\n# systemd-networkd keeps it across DHCP renews / networkd restarts.\nnetwork:\n  version: 2\n  ethernets:\n    %s:\n      addresses: [%s/%s]\n' \
        "$IFACE" "$ALIAS" "$PREFIX" | sudo tee "$NP" >/dev/null
    sudo chmod 600 "$NP"
    if sudo netplan generate 2>/dev/null; then
        sudo netplan apply 2>/dev/null \
            && echo "  pinned $ALIAS/$PREFIX on $IFACE (survives networkd restarts)" \
            || echo "  WARN: 'netplan apply' failed — cam-net still adds the alias at boot" >&2
    else
        echo "  WARN: netplan rejected $NP — removing it; cam-net adds the alias at boot" >&2
        sudo rm -f "$NP"
    fi
elif command -v netplan >/dev/null 2>&1; then
    echo "  iface '$IFACE' not present yet — re-run:  bash install.sh --iface <name>"
else
    echo "  no netplan here — cam-net adds the camera alias at boot (no change needed)"
fi

echo "== systemd units =="
sudo cp "$SRC"/services/*.service /etc/systemd/system/
[ -n "$(ls "$SRC"/services/*.path 2>/dev/null)" ] && sudo cp "$SRC"/services/*.path /etc/systemd/system/
[ -n "$(ls "$SRC"/services/*.timer 2>/dev/null)" ] && sudo cp "$SRC"/services/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cam-net.service cam-enforce.service
# cam-sync.path (auto-apply profiles on config change) and cam-health.timer
# (video-freeze watchdog) are OPT-IN — enable them per station when wanted.

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
echo "  sudo systemctl enable --now cam-health.timer # (optional) reboot a camera if RMS capture freezes"
