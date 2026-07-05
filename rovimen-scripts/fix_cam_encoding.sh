#!/bin/bash
# Fix camera encoding settings after reboot.
# Reads camera IPs from rovimen_scripts/config.json — no hardcoded IPs.
#
# Applied per camera:
#   - Resolution: 720P (MainFormat) — drift to 3M/1080P causes massive RMS frame drops
#   - BitRateControl: CBR, BitRate: 8192, Quality: 6
#   - DayNightColor: 0x00000001 (always color, prevents auto day/night switch to B&W)
#   - IrcutSwap: 1 (correct IR cut orientation for these cameras)
#   - EsShutter: 0x0 (auto electronic shutter, prevents frame throttling at night)
#   - WhiteBalance: 0x2 (consistent white balance mode across cameras)
#   - DncThr: 50 (day/night threshold, consistent across cameras)
#   - ElecLevel: 40, AeSensitivity: 1 (exposure target / metering)
#   - ExposureParam: LeastTime/MostTime 0x9C40 (40ms = full 1/25s frame; without
#     this the shutter caps at ~1ms and night frames are black)
#   - GainParam: AutoGain 1, Gain 60 (auto-gain must be on, or night is black)
#   - ClearFog: disabled, level 30 (consistent across cameras)
#
# These settings can revert after a camera reboot (firmware behaviour).
# Run this script from cron at boot or after a known camera reboot window.
#
# Usage: bash fix_cam_encoding.sh
# Log:   ~/fix_cam_encoding.log

CONFIG="$HOME/rovimen_scripts/config.json"
LOG="$HOME/fix_cam_encoding.log"

VENV_PYTHON=$(grep -hoP 'ExecStart=\K\S+/bin/python3?' /etc/systemd/system/rms-*.service ~/.config/systemd/user/rms-*.service 2>/dev/null | head -1)
if [ -z "$VENV_PYTHON" ]; then
    echo "$(date): ERROR: RMS venv not found (no rms-*.service with ExecStart)" >> "$LOG"
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

enforce_camera_ip() {
    local expected_ip=$1
    local station=$2
    local subnet
    subnet=$(echo "$expected_ip" | sed 's/\.[0-9]*$/./')

    if timeout 2 bash -c "echo >/dev/tcp/$expected_ip/34567" 2>/dev/null; then
        echo "$(date): [$station] Camera at $expected_ip — IP OK" >> "$LOG"
        return 0
    fi

    echo "$(date): [$station] Camera not at $expected_ip, scanning ${subnet}0/24 for port 34567..." >> "$LOG"
    local found_ip
    found_ip=$(nmap -p 34567 "${subnet}0/24" --open -oG - 2>/dev/null \
        | grep '34567/open' | grep -v "$expected_ip" \
        | head -1 | awk '{print $2}')

    if [ -z "$found_ip" ]; then
        echo "$(date): [$station] No camera found on subnet — cannot enforce IP" >> "$LOG"
        return 1
    fi

    echo "$(date): [$station] Found camera at $found_ip, changing to $expected_ip..." >> "$LOG"

    local expected_hex
    expected_hex=$("$VENV_PYTHON" -c "
import socket, struct
packed = socket.inet_aton('$expected_ip')
print('0x' + struct.pack('<4B', *packed).hex().upper())
")

    "$VENV_PYTHON" - "$found_ip" "$expected_ip" "$expected_hex" "$station" >> "$LOG" 2>&1 <<'IPEOF'
import sys, time
try:
    from dvrip import DVRIPCam
except ImportError:
    import importlib.util, pathlib
    for p in sorted(pathlib.Path(sys.prefix, "lib").rglob("dvrip.py")):
        if p.parent.name != "dvrip":
            spec = importlib.util.spec_from_file_location("dvrip_flat", str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            DVRIPCam = mod.DVRIPCam
            break
    else:
        raise ImportError("DVRIPCam not found")

found_ip, target_ip, target_hex, station = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
cam = DVRIPCam(found_ip)
if not cam.login():
    print(f"[{station}] Login to {found_ip} failed")
    sys.exit(1)
net = cam.get_info("NetWork.NetCommon")
if net is None:
    print(f"[{station}] Could not read NetCommon from {found_ip}")
    cam.close()
    sys.exit(1)
old_ip = net.get("HostIP", "unknown")
net["HostIP"] = target_hex
cam.set_info("NetWork.NetCommon", net)
cam.close()
print(f"[{station}] Changed IP from {old_ip} ({found_ip}) to {target_hex} ({target_ip})")
IPEOF

    sleep 5
    if timeout 2 bash -c "echo >/dev/tcp/$expected_ip/34567" 2>/dev/null; then
        echo "$(date): [$station] IP enforcement successful — camera now at $expected_ip" >> "$LOG"
        return 0
    else
        echo "$(date): [$station] ERROR: IP enforcement failed — camera not responding at $expected_ip" >> "$LOG"
        return 1
    fi
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
try:
    from dvrip import DVRIPCam
except ImportError:
    import importlib.util, pathlib
    for p in sorted(pathlib.Path(sys.prefix, "lib").rglob("dvrip.py")):
        if p.parent.name != "dvrip":
            spec = importlib.util.spec_from_file_location("dvrip_flat", str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            DVRIPCam = mod.DVRIPCam
            break
    else:
        raise ImportError("DVRIPCam not found")

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

# --- Encoder: Resolution, CBR, BitRate, Quality ---
# Granular sets on Resolution drop the DVRIP session mid-call (tested), so any
# encoder drift is corrected as a single bulk Simplify.Encode write, then we
# reconnect before touching Camera.Param fields.
v = enc[0]["MainFormat"]["Video"]
before = (v.get("Resolution"), v.get("BitRateControl"), v.get("BitRate"), v.get("Quality"))
target = ("720P", "CBR", 8192, 6)

# Sub-stream (ExtraFormat) must stay OFF. RMS and color_capture both pull the MAIN
# stream (stream=0); the sub-stream is unused, and on these cheap cameras a 2nd live
# encoder steals SoC/encoder budget from the main stream -> main-stream frame drops.
# Folded into the same bulk Simplify.Encode write so it costs no extra stream bounce.
extra = enc[0].get("ExtraFormat")
substream_on = bool(extra) and extra.get("VideoEnable") not in (0, False, None)
if substream_on:
    extra["VideoEnable"] = False

if before != target or substream_on:
    v["Resolution"] = "720P"
    v["BitRateControl"] = "CBR"
    v["BitRate"] = 8192
    v["Quality"] = 6
    cam.set_info("Simplify.Encode", enc)
    if before != target:
        changes.append(f"set Encode 720P/CBR/8192/Q6 (was {before})")
    if substream_on:
        changes.append("disabled ExtraFormat sub-stream")
    try:
        cam.close()
    except Exception:
        pass
    time.sleep(3)
    for i in range(12):
        try:
            cam = DVRIPCam(ip)
            cam.login()
            cam_cfg = cam.get_info("Camera")
            if cam_cfg is None:
                raise RuntimeError("Camera get_info None after Encode write")
            break
        except Exception as e:
            if i == 11:
                print(f"[{station}] {ip}: ERROR reconnect after Encode write: {e}")
                sys.exit(1)
            time.sleep(5)
else:
    changes.append("Encode OK (720P/CBR/8192/Q6, sub-stream off)")

# --- DayNightColor, IrcutSwap, EsShutter, WhiteBalance, DncThr ---
params = cam_cfg.get("Param", [{}])
p = params[0] if params else {}
if p.get("DayNightColor") != "0x00000001":
    cam.set_info("Camera.Param.[0].DayNightColor", "0x00000001")
    changes.append("set DayNightColor=0x00000001")
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

# --- Exposure / Gain / ElecLevel ---
# Without these the camera sits in a daylight exposure (MostTime ~1ms, AutoGain
# off, Gain 0) and night frames are black. Lock the shutter to a full 40ms frame
# (1/25s) and let auto-gain ride to Gain=60. These revert on camera power-cycle,
# hence they belong here in the boot fixup alongside EsShutter.
if p.get("ElecLevel") != 40:
    cam.set_info("Camera.Param.[0].ElecLevel", 40)
    changes.append("set ElecLevel=40")
else:
    changes.append("ElecLevel OK")
if p.get("AeSensitivity") != 1:
    cam.set_info("Camera.Param.[0].AeSensitivity", 1)
    changes.append("set AeSensitivity=1")
else:
    changes.append("AeSensitivity OK")
exp = p.get("ExposureParam", {})
exp_target = {"LeastTime": "0x00009C40", "Level": 0, "MostTime": "0x00009C40"}
if (exp.get("LeastTime"), exp.get("MostTime")) != (exp_target["LeastTime"], exp_target["MostTime"]):
    cam.set_info("Camera.Param.[0].ExposureParam", exp_target)
    changes.append("set ExposureParam=40ms")
else:
    changes.append("ExposureParam OK")
gain = p.get("GainParam", {})
if gain.get("AutoGain") != 1 or gain.get("Gain") != 60:
    cam.set_info("Camera.Param.[0].GainParam", {"AutoGain": 1, "Gain": 60})
    changes.append("set GainParam=auto/60")
else:
    changes.append("GainParam OK")

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
    enforce_camera_ip "$ip" "$station"
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
