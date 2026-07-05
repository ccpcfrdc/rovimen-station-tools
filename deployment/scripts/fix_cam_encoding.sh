#!/bin/bash
# Fix camera encoding settings after reboot.
# Reads camera IPs from rovimen_scripts/config.json — no hardcoded IPs.
#
# Applied per camera:
#   - BitRateControl: CBR, BitRate: 8192, Quality: 6
#   - DayNightColor: 1 (always color, prevents auto day/night switch to B&W)
#   - IrcutSwap: 1 (correct IR cut orientation for these cameras)
#   - EsShutter: 0x0 (auto electronic shutter, prevents frame throttling at night)
#   - WhiteBalance: 0x2 (consistent white balance mode across cameras)
#   - DncThr: 50 (day/night threshold, consistent across cameras)
#   - ClearFog: disabled, level 30 (consistent across cameras)
#
# These settings can revert after a camera reboot (firmware behaviour).
# Run this script from cron at boot or after a known camera reboot window.
#
# Usage: bash fix_cam_encoding.sh
# Log:   ~/fix_cam_encoding.log

CONFIG="$HOME/rovimen_scripts/config.json"
LOG="$HOME/fix_cam_encoding.log"

VENV_PYTHON=$(grep -h 'ExecStart' /etc/systemd/system/rms-cam*.service 2>/dev/null \
    | grep -oP '[^\s]+/bin/python3?' | head -1)
if [ -z "$VENV_PYTHON" ]; then
    echo "$(date): ERROR: RMS venv not found (no rms-cam*.service with ExecStart)" >> "$LOG"
    exit 1
fi

wait_for_camera() {
    local ip=$1
    local attempts=0
    while [ $attempts -lt 60 ]; do
        timeout 2 bash -c "echo >/dev/tcp/$ip/34567" 2>/dev/null && return 0
        sleep 5
        attempts=$((attempts + 1))
    done
    return 1
}

fix_camera() {
    local ip=$1
    local station=$2
    echo "$(date): [$station] Fixing $ip" >> "$LOG"

    wait_for_camera "$ip" || {
        echo "$(date): [$station] $ip not reachable after 5 min, skipping" >> "$LOG"
        return 1
    }
    sleep 10

    "$VENV_PYTHON" - "$ip" "$station" >> "$LOG" 2>&1 <<'PYEOF'
import sys, time
from dvrip import DVRIPCam

ip, station = sys.argv[1], sys.argv[2]

# Retry up to 5 times — cameras can accept TCP before DVRIP is ready,
# causing get_info to return None or throw on the first attempt.
for attempt in range(5):
    try:
        cam = DVRIPCam(ip)
        cam.login()

        # Read ALL data first, before any writes.
        # set_info calls can cause the camera to drop the DVRIP connection,
        # so we collect everything we need upfront in a single session.
        enc = cam.get_info("Simplify.Encode")
        cam_cfg = cam.get_info("Camera")

        if enc is None or cam_cfg is None:
            raise RuntimeError("get_info returned None (camera not ready)")

        break  # success
    except Exception as e:
        try:
            cam.close()
        except Exception:
            pass
        if attempt < 4:
            print(f"[{station}] {ip}: attempt {attempt+1} failed ({e}), retrying in 30s")
            time.sleep(30)
        else:
            print(f"[{station}] {ip}: ERROR after 5 attempts: {e}")
            sys.exit(1)

changes = []

# --- CBR encoding ---
v = enc[0]["MainFormat"]["Video"]
if v["BitRateControl"] != "CBR" or v["BitRate"] != 8192 or v["Quality"] != 6:
    cam.set_info("Simplify.Encode.[0].MainFormat.Video.BitRateControl", "CBR")
    cam.set_info("Simplify.Encode.[0].MainFormat.Video.BitRate", 8192)
    cam.set_info("Simplify.Encode.[0].MainFormat.Video.Quality", 6)
    changes.append("set CBR 8192 Q6")
else:
    changes.append("CBR OK")

# --- DayNightColor, IrcutSwap, EsShutter, WhiteBalance, DncThr ---
params = cam_cfg.get("Param", [{}])
p = params[0] if params else {}
if p.get("DayNightColor") != 1:
    cam.set_info("Camera.Param.[0].DayNightColor", 1)
    changes.append("set DayNightColor=1")
else:
    changes.append("DayNightColor OK")
if p.get("IrcutSwap") != 1:
    cam.set_info("Camera.Param.[0].IrcutSwap", 1)
    changes.append("set IrcutSwap=1")
else:
    changes.append("IrcutSwap OK")
if p.get("EsShutter") != "0x00000000":
    cam.set_info("Camera.Param.[0].EsShutter", "0x00000000")
    changes.append("set EsShutter=0x0")
else:
    changes.append("EsShutter OK")
if p.get("WhiteBalance") != "0x00000002":
    cam.set_info("Camera.Param.[0].WhiteBalance", "0x00000002")
    changes.append("set WhiteBalance=0x2")
else:
    changes.append("WhiteBalance OK")
if p.get("DncThr") != 50:
    cam.set_info("Camera.Param.[0].DncThr", 50)
    changes.append("set DncThr=50")
else:
    changes.append("DncThr OK")

# --- ClearFog ---
clearfog = cam_cfg.get("ClearFog", [{}])
cf = clearfog[0] if clearfog else {}
if cf.get("enable") not in (False, 0) or cf.get("level") != 30:
    cam.set_info("Camera.ClearFog.[0].enable", False)
    cam.set_info("Camera.ClearFog.[0].level", 30)
    changes.append("set ClearFog=off/30")
else:
    changes.append("ClearFog OK")

try:
    cam.close()
except Exception:
    pass

print(f"[{station}] {ip}: {', '.join(changes)}")
PYEOF

    echo "$(date): [$station] done" >> "$LOG"
}

echo "$(date): Starting fix_cam_encoding (config: $CONFIG)" >> "$LOG"

if [ ! -f "$CONFIG" ]; then
    echo "$(date): ERROR: config.json not found at $CONFIG" >> "$LOG"
    exit 1
fi

# Extract station IDs and IPs from config.json RTSP URLs.
# RTSP format: rtsp://admin:@<IP>:554/...
while IFS=' ' read -r ip station; do
    fix_camera "$ip" "$station"
    sleep 30   # give the camera a breather between stations
done < <("$VENV_PYTHON" -c "
import json, sys
with open('$CONFIG') as f:
    cfg = json.load(f)
for station, info in cfg['stations'].items():
    rtsp = info.get('camera_rtsp', '')
    try:
        ip = rtsp.split('@')[1].split(':')[0]
        print(ip, station)
    except Exception:
        print(f'ERROR: could not parse IP for {station}: {rtsp}', file=sys.stderr)
")

echo "$(date): All done" >> "$LOG"
