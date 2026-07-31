#!/usr/bin/env bash
# Private camera network: alias + NAT + per-camera NTP. Config-driven, self-locating.
# Config: $ROVIMEN_CAM_CONFIG, else /etc/rovimen-cam/config.json.
# Runs as root (systemd) or manually via sudo. Usage: cam-net up|down|status|cam
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CFG="${ROVIMEN_CAM_CONFIG:-/etc/rovimen-cam/config.json}"
PYBIN="$DIR/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="python3"

eval "$("$PYBIN" - "$CFG" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); n = d.get("network", {})
print(f'IFACE="{d.get("iface","eno1")}"')
print(f'ALIAS_IP="{n.get("alias_ip","10.42.0.1")}"')
print(f'PREFIX="{n.get("prefix",24)}"')
print(f'CAM_NET="{n.get("subnet","10.42.0.0/24")}"')
print(f'NTP_IP="{d.get("ntp","162.159.200.123")}"')
print('CAM_IPS="%s"' % " ".join(c["ip"] for c in d.get("cameras", {}).values()))
PY
)"
UPLINK="$(ip route show default | awk '{print $5; exit}')"
usage(){ echo "Usage: $0 up|down|status|cam"; exit 1; }; [ $# -ge 1 ] || usage
# swap -A -> -C anywhere it appears (keeps -t nat intact) to test-before-add
ensure(){ local chk=("${@/#-A/-C}"); sudo iptables "${chk[@]}" 2>/dev/null || sudo iptables "$@"; }

net_up(){
  ip addr show dev "$IFACE" | grep -q "inet $ALIAS_IP/" && echo "alias $ALIAS_IP present" \
    || { sudo ip addr add "$ALIAS_IP/$PREFIX" dev "$IFACE"; echo "alias $ALIAS_IP/$PREFIX added"; }
  sudo sysctl -qw net.ipv4.ip_forward=1
  ensure -t nat -A POSTROUTING -s "$CAM_NET" -o "$UPLINK" -j MASQUERADE
  ensure -A FORWARD -s "$CAM_NET" -o "$UPLINK" -j ACCEPT
  ensure -A FORWARD -d "$CAM_NET" -o "$IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  echo "NAT via '$UPLINK' for $CAM_NET"
}
cam_cfg(){
  for ip in $CAM_IPS; do
    CAM_IP="$ip" NTP_IP="$NTP_IP" DVR_DIR="$DIR" timeout 25 "$PYBIN" - <<'PY' || echo "  $ip skip"
import os, sys; sys.path.insert(0, os.environ["DVR_DIR"]); from dvrip import DVRIPCam
c = DVRIPCam(os.environ["CAM_IP"], port=34567, user="admin", password=""); c.timeout = 6
if not c.login(): print("  %s login skip" % os.environ["CAM_IP"]); sys.exit(0)
ntp = c.get_info("NetWork.NetNTP"); ntp["Enable"] = True; ntp.setdefault("Server", {})
ntp["Server"]["Name"] = os.environ["NTP_IP"]; ntp["Server"]["Port"] = 123; ntp["UpdatePeriod"] = 60
print("  %s NTP Ret=" % os.environ["CAM_IP"], c.set_info("NetWork.NetNTP", ntp).get("Ret")); c.close()
PY
  done
}
case "$1" in
  up)   net_up; cam_cfg ;;
  cam)  cam_cfg ;;
  down)
    while sudo iptables -t nat -D POSTROUTING -s "$CAM_NET" -o "$UPLINK" -j MASQUERADE 2>/dev/null; do :; done
    while sudo iptables -D FORWARD -s "$CAM_NET" -o "$UPLINK" -j ACCEPT 2>/dev/null; do :; done
    while sudo iptables -D FORWARD -d "$CAM_NET" -o "$IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null; do :; done
    sudo ip addr del "$ALIAS_IP/$PREFIX" dev "$IFACE" 2>/dev/null || true; echo "removed" ;;
  status)
    echo "== $IFACE =="; ip -br addr show "$IFACE"
    echo "uplink=$UPLINK ip_forward=$(sysctl -n net.ipv4.ip_forward) cams=[$CAM_IPS]"
    sudo iptables -t nat -S POSTROUTING | grep "$CAM_NET" || echo "(no nat)" ;;
  *) usage ;;
esac
