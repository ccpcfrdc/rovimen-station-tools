#!/usr/bin/env bash
# hardware_assessment.sh — Hardware capability probe for ROVIMEN stations
#
# Detects CPU, RAM, disk, and GPU/VAAPI capabilities, then writes
# ~/rovimen_scripts/hardware.json for use by the station API and dashboard.
#
# Run once during initial setup, or re-run after hardware changes:
#   bash hardware_assessment.sh
#
# Called automatically by install.sh during station setup.

set -euo pipefail

OUT_DIR="${HOME}/rovimen_scripts"
OUT_FILE="${OUT_DIR}/hardware.json"

log()  { echo "[probe] $*"; }
warn() { echo "[probe] WARN: $*"; }

mkdir -p "$OUT_DIR"

# ── CPU ──────────────────────────────────────────────────────────────────────
cpu_cores=$(nproc 2>/dev/null || echo 1)
cpu_model=$(grep "model name" /proc/cpuinfo 2>/dev/null | head -1 \
            | cut -d: -f2 | xargs 2>/dev/null | tr -dc '[:print:]' || true)
[ -z "$cpu_model" ] && cpu_model="unknown"
arch=$(uname -m)

# ── RAM ──────────────────────────────────────────────────────────────────────
ram_gb=$(awk '/MemTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)

# ── Disk (home partition) ─────────────────────────────────────────────────────
disk_gb=$(df -BG "$HOME" 2>/dev/null | awk 'NR==2 {gsub("G",""); print $2}' || echo 0)
disk_free_gb=$(df -BG "$HOME" 2>/dev/null | awk 'NR==2 {gsub("G",""); print $4}' || echo 0)

# ── Color capture disk (may differ from home if on external drive) ────────────
color_capture_path=""
if [ -f "${OUT_DIR}/config.json" ] && command -v python3 &>/dev/null; then
    color_capture_path=$(python3 -c "
import json
try:
    c = json.load(open('${OUT_DIR}/config.json'))
    print(c.get('color_capture_path') or c.get('videocapture_path') or '')
except: print('')
" 2>/dev/null || true)
fi
# Fall back to home if path not set or doesn't exist
if [ -z "$color_capture_path" ] || [ ! -e "$color_capture_path" ]; then
    color_capture_path="$HOME"
fi
capture_disk_gb=$(df -BG "$color_capture_path" 2>/dev/null | awk 'NR==2 {gsub("G",""); print $2}' || echo "$disk_gb")
capture_disk_free_gb=$(df -BG "$color_capture_path" 2>/dev/null | awk 'NR==2 {gsub("G",""); print $4}' || echo "$disk_free_gb")

# ── VAAPI ─────────────────────────────────────────────────────────────────────
vaapi=false
vaapi_device=""
vaapi_driver=""
vaapi_h264_encode=false

if [ -e /dev/dri/renderD128 ]; then
    vaapi_device="/dev/dri/renderD128"

    # Prove H.264 encode works by actually doing it (32x32 synthetic test)
    if ffmpeg -hide_banner -loglevel error \
              -vaapi_device "$vaapi_device" \
              -f lavfi -i 'testsrc=duration=0.04:size=32x32:rate=25' \
              -vf 'format=nv12,hwupload' \
              -c:v h264_vaapi -qp 20 \
              -f null - 2>/dev/null; then
        vaapi=true
        vaapi_h264_encode=true

        # Detect driver name via vainfo if available (informational only)
        if command -v vainfo &>/dev/null; then
            vainfo_out=$(vainfo --display drm --device "$vaapi_device" 2>/dev/null || true)
            if echo "$vainfo_out" | grep -q "iHD"; then
                vaapi_driver="iHD"
            elif echo "$vainfo_out" | grep -q "i965"; then
                vaapi_driver="i965"
            fi
        fi
    else
        warn "VAAPI device found but H.264 encode test failed — disabling VAAPI"
    fi
fi

# ── GStreamer ─────────────────────────────────────────────────────────────────
gst_version="unknown"
gst_custom=false

_ver_gt() {
    # Returns true if $1 > $2 (semver, sort -V)
    [ "$(printf '%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ] && [ "$1" != "$2" ]
}

if [ -x "/opt/gst-1.22/bin/gst-launch-1.0" ]; then
    gst_version=$(GST_PLUGIN_PATH="/opt/gst-1.22/lib/x86_64-linux-gnu/gstreamer-1.0" \
        LD_LIBRARY_PATH="/opt/gst-1.22/lib/x86_64-linux-gnu" \
        /opt/gst-1.22/bin/gst-launch-1.0 --version 2>/dev/null \
        | grep -oP '\d+\.\d+\.\d+' | head -1 || echo "unknown")
    gst_custom=true
elif command -v gst-launch-1.0 &>/dev/null; then
    gst_version=$(gst-launch-1.0 --version 2>/dev/null \
        | grep -oP '\d+\.\d+\.\d+' | head -1 || echo "unknown")
fi

if [ "$gst_version" != "unknown" ] && _ver_gt "$gst_version" "1.22.3"; then
    warn "GStreamer $gst_version detected — version > 1.22.3 may cause frame drops with RMS."
    warn "Consider building 1.22.3 from source: bash ~/rovimen_scripts/build_gstreamer_1223.sh"
fi

# ── Suggested settings ────────────────────────────────────────────────────────
# encode_workers: 1 worker per 4 CPU cores, capped at 4
suggested_encode_workers=$(( cpu_cores / 4 ))
[ "$suggested_encode_workers" -lt 1 ] && suggested_encode_workers=1
[ "$suggested_encode_workers" -gt 4 ] && suggested_encode_workers=4

# compression_level: 2 (QP20/CRF21) if VAAPI with H264, else 1 (CRF20 CPU)
if [ "$vaapi_h264_encode" = "true" ]; then
    suggested_compression=2
else
    suggested_compression=1
fi

# color_days: conservative estimate based on disk and assumption of 2 cameras at QP20
# ~30 GB/cam/night at QP20 (conservative — actual depends on sky conditions)
GB_PER_CAM_NIGHT=30
cam_count=2  # default guess; install.sh can pass actual count
if [ -f "${OUT_DIR}/config.json" ] && command -v python3 &>/dev/null; then
    cam_count=$(python3 -c "
import json, sys
try:
    c = json.load(open('${OUT_DIR}/config.json'))
    print(len(c.get('stations', {}) or {}))
except: print(2)
" 2>/dev/null || echo 2)
fi
[ "$cam_count" -lt 1 ] && cam_count=1
# If capture lives on a dedicated disk (different from home), use 85%; else 65%
home_dev=$(df "$HOME" 2>/dev/null | awk 'NR==2 {print $1}')
capture_dev=$(df "$color_capture_path" 2>/dev/null | awk 'NR==2 {print $1}')
if [ "$capture_dev" != "$home_dev" ]; then
    capture_pct=85
else
    capture_pct=65
fi
usable_disk=$(( capture_disk_gb * capture_pct / 100 ))
suggested_color_days=$(( usable_disk / (cam_count * GB_PER_CAM_NIGHT) ))
[ "$suggested_color_days" -lt 1 ] && suggested_color_days=1
[ "$suggested_color_days" -gt 14 ] && suggested_color_days=14

# ── Write JSON ────────────────────────────────────────────────────────────────
cat > "$OUT_FILE" <<EOF
{
    "probed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "arch": "$arch",
    "cpu_model": "$cpu_model",
    "cpu_cores": $cpu_cores,
    "ram_gb": $ram_gb,
    "disk_gb": $disk_gb,
    "disk_free_gb": $disk_free_gb,
    "capture_disk_gb": $capture_disk_gb,
    "capture_disk_free_gb": $capture_disk_free_gb,
    "vaapi": $vaapi,
    "vaapi_device": "$vaapi_device",
    "vaapi_driver": "$vaapi_driver",
    "vaapi_h264_encode": $vaapi_h264_encode,
    "gst_version": "$gst_version",
    "gst_custom_build": $gst_custom,
    "suggested": {
        "encode_workers": $suggested_encode_workers,
        "compression_level": $suggested_compression,
        "capabilities_vaapi": $vaapi_h264_encode,
        "capabilities_vaapi_driver": "$vaapi_driver",
        "capabilities_vaapi_device": "$vaapi_device",
        "retention_color_days": $suggested_color_days
    }
}
EOF

log "Hardware profile written to $OUT_FILE"
log "  CPU:  $cpu_model ($cpu_cores cores)"
log "  RAM:  ${ram_gb} GB"
log "  Disk (home): ${disk_gb} GB total / ${disk_free_gb} GB free"
log "  Disk (capture): ${capture_disk_gb} GB total / ${capture_disk_free_gb} GB free  (${color_capture_path})"
if [ "$vaapi" = "true" ]; then
    log "  VAAPI: $vaapi_device  driver=$vaapi_driver  h264_encode=$vaapi_h264_encode"
else
    log "  VAAPI: not detected"
fi
log "  GStreamer: $gst_version$([ "$gst_custom" = "true" ] && echo " (custom /opt/gst-1.22)" || true)"
log "  Suggested: compression=$suggested_compression  workers=$suggested_encode_workers  color_days=${suggested_color_days}"
