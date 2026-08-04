#!/usr/bin/env bash
# station-cam uninstaller. Run as a normal user with sudo rights.
#   bash uninstall.sh          # remove tooling + services, KEEP /etc/rovimen-cam config
#   bash uninstall.sh --purge  # also remove /etc/rovimen-cam (station config)
#
# Note: this removes the station-side tooling only. The cameras keep their static
# IP and capture profile (those live in the camera's own flash) — but with the
# alias/NAT gone they are no longer reachable from this host.
set -e

LIB=/usr/local/lib/rovimen-cam
ETC=/etc/rovimen-cam
BIN=/usr/local/bin

PURGE=0; YES=0
for a in "$@"; do
    case "$a" in
        --purge) PURGE=1 ;;
        -y|--yes) YES=1 ;;
        *) echo "unknown arg: $a  (use: uninstall.sh [--purge] [-y])"; exit 2 ;;
    esac
done

if [ "$YES" -ne 1 ]; then
    echo "This removes the station-cam tooling and services from this host."
    [ "$PURGE" -eq 1 ] && echo "It will ALSO delete $ETC (station config)."
    printf "Are you sure? [y/N] "
    read -r ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *) echo "aborted"; exit 0 ;;
    esac
fi

echo "== stop + disable services =="
for s in cam-health.timer cam-health cam-dashboard cam-sync.path cam-sync cam-enforce cam-net; do
    sudo systemctl disable --now "$s" 2>/dev/null || true
done

echo "== bring the camera network down (remove alias + NAT) =="
if [ -x "$BIN/cam-net" ]; then sudo "$BIN/cam-net" down 2>/dev/null || true; fi

echo "== remove systemd units =="
sudo rm -f /etc/systemd/system/cam-net.service \
           /etc/systemd/system/cam-enforce.service \
           /etc/systemd/system/cam-sync.service \
           /etc/systemd/system/cam-sync.path \
           /etc/systemd/system/cam-dashboard.service \
           /etc/systemd/system/cam-health.service \
           /etc/systemd/system/cam-health.timer
sudo systemctl daemon-reload

echo "== remove CLI wrappers =="
sudo rm -f "$BIN"/cam-net "$BIN"/cam-enforce "$BIN"/cam-sync "$BIN"/cam-profiles-update \
           "$BIN"/cam-reboot "$BIN"/cam-add "$BIN"/cam-find "$BIN"/cam-ip \
           "$BIN"/cam-recovery-test "$BIN"/cam-help "$BIN"/cam-dashboard "$BIN"/cam-health \
           "$BIN"/findcam "$BIN"/camip

echo "== remove the netplan camera-alias pin =="
if [ -f /etc/netplan/99-rovimen-cam.yaml ]; then
    sudo rm -f /etc/netplan/99-rovimen-cam.yaml
    if command -v netplan >/dev/null 2>&1; then sudo netplan apply 2>/dev/null || true; fi
fi

echo "== remove library + runtime =="
sudo rm -rf "$LIB" /dev/shm/rovimen-cam-hls /var/lib/rovimen-cam

if [ "$PURGE" -eq 1 ]; then
    sudo rm -rf "$ETC"
    echo "== purged $ETC (station config removed) =="
else
    echo "== kept $ETC (station config); pass --purge to remove it too =="
fi
echo "== done =="
