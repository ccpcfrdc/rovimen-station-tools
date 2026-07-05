#!/usr/bin/env bash
# rovimen_install.sh — Interactive station installer for ROVIMEN meteor systems
#
# Run as the target user (not root). Uses sudo internally when needed.
# Usage: bash rovimen_install.sh
#
# Phases:
#   0 — Preflight checks + copy scripts
#   1 — Hardware probe
#   2 — Capture drive selection
#   3 — Camera discovery (network scan)
#   4 — RMS path mapping
#   5 — Config review & edit
#   6 — Installation (write config, install services)
#   7 — Summary

set -euo pipefail

# ── Terminal colours ──────────────────────────────────────────────────────────
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLU='\033[0;34m'
CYN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

hdr()  { echo; echo -e "${BOLD}${BLU}══════════════════════════════════════════${NC}"; \
          echo -e "${BOLD}${BLU}  $*${NC}"; \
          echo -e "${BOLD}${BLU}══════════════════════════════════════════${NC}"; echo; }
info() { echo -e "${GRN}  ✓${NC}  $*"; }
warn() { echo -e "${YLW}  ⚠${NC}  $*"; }
err()  { echo -e "${RED}  ✗${NC}  $*" >&2; }
ask()  { echo -en "${CYN}  ?${NC}  $*" >&2; }
dim()  { echo -e "${DIM}     $*${NC}"; }

die() { err "$*"; exit 1; }

# ── Globals (filled during phases) ───────────────────────────────────────────
INSTALL_USER=$(whoami)
HOME_DIR="$HOME"
SCRIPTS_DIR="$HOME_DIR/rovimen_scripts"
REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VENV_PYTHON=""   # set in phase0_preflight via auto-detection

# Camera data (parallel arrays)
CAM_IPS=()
CAM_CODES=()
CAM_ROTATE=()
CAM_RMS_PATHS=()
CAM_AZ=()
CAM_ALT=()

# Station coordinates (pre-read from RMS configs)
RMS_LAT=""
RMS_LON=""
RMS_ELEV=""

# Config values set during Phase 5
CAPTURE_PATH=""
VAAPI=""
VAAPI_DEVICE=""
VAAPI_DRIVER=""
COMPRESSION_LEVEL=""
ENCODE_WORKERS=""
RETENTION_COLOR_DAYS=""
RETENTION_LOCKED_DAYS=7
RETENTION_STACKS_DAYS=7
RETENTION_TIMELAPSE_DAYS=7
SERVICE_STACKER_ENABLED=true
SERVICE_REENCODE_ENABLED=true
SERVICE_DETECTION_LOCK_ENABLED=true
SERVICE_TIMELAPSE_BUILD_ENABLED=true
SERVICE_STATION_API_ENABLED=true
SERVICE_ARCHIVE_UPLOAD_ENABLED=false
ARCHIVE_HOST="100.64.0.1"
ARCHIVE_PORT=22
ARCHIVE_USER="gmn"
ARCHIVE_BASE_PATH="/srv/rovimen/archive"
ARCHIVE_UPLOAD_METEORS=true
ARCHIVE_UPLOAD_TIMELAPSES=true
ARCHIVE_UPLOAD_STACKS=false
ARCHIVE_INTERVAL_MINUTES=20
ARCHIVE_SSH_KEY="$HOME/.ssh/rovimen_archive_id_ed25519"
DETECTION_PRE_SECONDS=3
DETECTION_POST_SECONDS=12
OVERLAY_ENABLED=true
OVERLAY_STYLE="cinema"
OVERLAY_NETWORK="ROVIMEN"
OVERLAY_COORDS=""
OVERLAY_SHOW_NETWORK=true
OVERLAY_SHOW_LOGO=true
OVERLAY_SHOW_TIMESTAMP=true
OVERLAY_SHOW_STATION=true
OVERLAY_SHOW_COORDS=true
OVERLAY_SHOW_POINTING=true
OVERLAY_TEXT_OPACITY="0.8"
OVERLAY_LOGO_OPACITY="0.8"
STATION_LABEL=""
KEEP_CONFIG=false
VPS_HOST=""
UPDATE_CHANNEL="main"

# Overwrite the hardcoded defaults above with values from config_defaults.json
# once it has been extracted to $SCRIPTS_DIR (called at end of phase0_preflight).
_load_defaults_from_json() {
    local defaults="$SCRIPTS_DIR/config_defaults.json"
    [ -f "$defaults" ] || return 0
    local vals
    vals=$(python3 - "$defaults" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
def b(v): return 'true' if v else 'false'
a   = d.get('archive',   {})
det = d.get('detection', {})
ov  = d.get('overlay',   {})
ret = d.get('retention', {})
svc = d.get('services',  {})
lines = [
    f"VPS_HOST={d.get('vps_host') or ''}",
    f"UPDATE_CHANNEL={d.get('update_channel', 'main')}",
    f"ARCHIVE_HOST={a.get('host', '')}",
    f"ARCHIVE_PORT={a.get('port', 22)}",
    f"ARCHIVE_BASE_PATH={a.get('base_path', '')}",
    f"ARCHIVE_UPLOAD_METEORS={b(a.get('upload_meteors', True))}",
    f"ARCHIVE_UPLOAD_TIMELAPSES={b(a.get('upload_timelapses', True))}",
    f"ARCHIVE_UPLOAD_STACKS={b(a.get('upload_stacks', False))}",
    f"ARCHIVE_INTERVAL_MINUTES={a.get('interval_minutes', 20)}",
    f"DETECTION_PRE_SECONDS={det.get('pre_seconds', 3)}",
    f"DETECTION_POST_SECONDS={det.get('post_seconds', 12)}",
    f"OVERLAY_ENABLED={b(ov.get('enabled', True))}",
    f"OVERLAY_STYLE={ov.get('style', 'cinema')}",
    f"OVERLAY_NETWORK={ov.get('network', 'ROVIMEN')}",
    f"OVERLAY_TEXT_OPACITY={ov.get('text_opacity', 0.8)}",
    f"OVERLAY_LOGO_OPACITY={ov.get('logo_opacity', 0.8)}",
    f"OVERLAY_SHOW_NETWORK={b(ov.get('show_network', True))}",
    f"OVERLAY_SHOW_LOGO={b(ov.get('show_logo', True))}",
    f"OVERLAY_SHOW_TIMESTAMP={b(ov.get('show_timestamp', True))}",
    f"OVERLAY_SHOW_STATION={b(ov.get('show_station', True))}",
    f"OVERLAY_SHOW_COORDS={b(ov.get('show_coords', True))}",
    f"OVERLAY_SHOW_POINTING={b(ov.get('show_pointing', True))}",
    f"RETENTION_LOCKED_DAYS={ret.get('locked_days', 7)}",
    f"RETENTION_STACKS_DAYS={ret.get('stacks_days', 7)}",
    f"RETENTION_TIMELAPSE_DAYS={ret.get('timelapse_days', 7)}",
    f"SERVICE_STACKER_ENABLED={b(svc.get('stacker', {}).get('enabled', True))}",
    f"SERVICE_STACKER_REALTIME={b(svc.get('stacker', {}).get('realtime', True))}",
    f"SERVICE_REENCODE_ENABLED={b(svc.get('reencode', {}).get('enabled', True))}",
    f"SERVICE_DETECTION_LOCK_ENABLED={b(svc.get('detection_lock', {}).get('enabled', True))}",
    f"SERVICE_TIMELAPSE_BUILD_ENABLED={b(svc.get('timelapse_build', {}).get('enabled', True))}",
    f"SERVICE_ARCHIVE_UPLOAD_ENABLED={b(svc.get('archive_upload', {}).get('enabled', False))}",
]
print('\n'.join(lines))
PYEOF
    ) || return 0
    eval "$vals"
}

# ── Utilities ─────────────────────────────────────────────────────────────────

# Prompt with default; echoes chosen value to stdout
prompt() {
    local msg="$1" default="${2:-}"
    if [ -n "$default" ]; then
        ask "${msg} [${DIM}${default}${NC}${CYN}]: "
    else
        ask "${msg}: "
    fi
    local val
    IFS= read -r val || true
    echo "${val:-$default}"
}

# y/n prompt; returns 0 for yes, 1 for no. Blank input uses default if provided.
confirm() {
    local msg="$1" default="${2:-}"
    local suffix
    if [ "$default" = "y" ]; then suffix="(Y/n)";
    elif [ "$default" = "n" ]; then suffix="(y/N)";
    else suffix="(y/n)"; fi
    local val
    while true; do
        ask "${msg} ${suffix}: "
        IFS= read -r val || true
        case "$val" in
            y|Y|yes|Yes|YES) return 0 ;;
            n|N|no|No|NO)    return 1 ;;
            '') if [ "$default" = "y" ]; then return 0;
                elif [ "$default" = "n" ]; then return 1;
                else echo -e "  ${YLW}Please type y or n.${NC}"; fi ;;
            *) echo -e "  ${YLW}Please type y or n.${NC}" ;;
        esac
    done
}

# Ensure spinner is always cleaned up on exit
_spinner_pid=""
trap 'stop_spinner' EXIT

start_spinner() {
    local msg="$1"
    (
        local chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
        local i=0
        while true; do
            printf "\r${CYN}  %s${NC}  %s  " "${chars:$((i % ${#chars})):1}" "$msg"
            sleep 0.1
            i=$((i+1))
        done
    ) &
    _spinner_pid=$!
}

stop_spinner() {
    if [ -n "$_spinner_pid" ]; then
        kill "$_spinner_pid" 2>/dev/null || true
        wait "$_spinner_pid" 2>/dev/null || true
        _spinner_pid=""
        printf "\r\033[K"
    fi
}

# Convert decimal degrees to "DD MMN  DDD MME" overlay format
dec_to_dms_overlay() {
    local lat="$1" lon="$2"
    python3 -c "
lat, lon = float('$lat'), float('$lon')
ns = 'N' if lat >= 0 else 'S'
ew = 'E' if lon >= 0 else 'W'
lat, lon = abs(lat), abs(lon)
lat_d, lat_m = int(lat), int((lat % 1) * 60)
lon_d, lon_m = int(lon), int((lon % 1) * 60)
print(f'{lat_d:02d} {lat_m:02d}{ns}  {lon_d:03d} {lon_m:02d}{ew}')
"
}

# ── Overlay preview helpers ───────────────────────────────────────────────────

# Print a box row: left-aligned left content, right-aligned right content, padded to 56 chars
_overlay_row() {
    local left="${1:-}" right="${2:-}" IW=56
    local pad=$(( IW - ${#left} - ${#right} ))
    [ $pad -lt 0 ] && pad=0
    printf "  │ %s%*s%s │\n" "$left" "$pad" '' "$right"
}

_overlay_preview() {
    local hr; hr=$(printf '─%.0s' $(seq 1 58))
    local empty; empty=$(printf '%58s' '')
    local ts_str="2026-03-23  21:34:15 UTC"
    local net_str="${OVERLAY_NETWORK:-ROVIMEN}"
    local sta_str="${CAM_CODES[0]:-RO000X}"
    local coords_str="${OVERLAY_COORDS:-44 48N  026 41E}"
    local az_str="ALT 44.0  AZ 226.0"

    echo -e "\n  ${BOLD}Overlay preview (${OVERLAY_STYLE:-cinema} style):${NC}"
    echo "  ┌${hr}┐"

    if [ "${OVERLAY_STYLE:-cinema}" = "cinema" ]; then
        echo "  │${empty}│"
        printf "  │%25s[VIDEO]%26s│\n" '' ''
        echo "  │${empty}│"
        echo "  ├${hr}┤"

        local r1_left=""
        [ "${OVERLAY_SHOW_LOGO:-true}"     = "true" ] && r1_left="[LOGO] "
        [ "${OVERLAY_SHOW_NETWORK:-true}"  = "true" ] && r1_left="${r1_left}${net_str}"
        local r1_right=""
        [ "${OVERLAY_SHOW_POINTING:-true}" = "true" ] && r1_right="${az_str}"
        _overlay_row "$r1_left" "$r1_right"

        local r2_left=""
        [ "${OVERLAY_SHOW_TIMESTAMP:-true}" = "true" ] && r2_left="${ts_str}"
        local r2_right=""
        [ "${OVERLAY_SHOW_STATION:-true}" = "true" ] && r2_right="${sta_str}"
        [ "${OVERLAY_SHOW_COORDS:-true}"  = "true" ] && r2_right="${r2_right}  ${coords_str}"
        _overlay_row "$r2_left" "$r2_right"
    else
        local top_left=""
        [ "${OVERLAY_SHOW_NETWORK:-true}" = "true" ] && top_left="${net_str}"
        _overlay_row "$top_left" ""
        echo "  │${empty}│"
        printf "  │%25s[VIDEO]%26s│\n" '' ''
        echo "  │${empty}│"
        [ "${OVERLAY_SHOW_STATION:-true}"   = "true" ] && _overlay_row "" "$sta_str"
        [ "${OVERLAY_SHOW_COORDS:-true}"    = "true" ] && _overlay_row "" "$coords_str"
        [ "${OVERLAY_SHOW_POINTING:-true}"  = "true" ] && _overlay_row "" "$az_str"
        [ "${OVERLAY_SHOW_TIMESTAMP:-true}" = "true" ] && _overlay_row "" "$ts_str"
    fi

    echo "  └${hr}┘"
    echo
}

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 0 — PREFLIGHT
# ═════════════════════════════════════════════════════════════════════════════
phase0_preflight() {
    hdr "Phase 0 — Preflight"

    # Must not be root
    if [ "$EUID" -eq 0 ]; then
        die "Run as the target user, not root. The script uses sudo internally."
    fi

    # Check sudo works
    info "Checking sudo access..."
    if ! sudo -n true 2>/dev/null; then
        warn "sudo requires a password — you may be prompted during installation."
    fi

    # Required binaries — auto-install via apt-get if missing
    local missing=0
    declare -A CMD_PKG=([python3]=python3 [ffmpeg]=ffmpeg [nc]=netcat-openbsd [rsync]=rsync)
    for cmd in python3 ffmpeg nc rsync; do
        if command -v "$cmd" &>/dev/null; then
            info "$cmd found"
        else
            local pkg="${CMD_PKG[$cmd]}"
            warn "$cmd not found — installing $pkg..."
            if sudo apt-get install -y "$pkg" &>/dev/null; then
                info "$cmd installed"
            else
                err "Failed to install $pkg — please install it manually"
                missing=1
            fi
        fi
    done

    # RMS venv — check common locations, fall back to prompting
    local venv_found=""
    for candidate in "$HOME_DIR/RMS/venv" "$HOME_DIR/vRMS" "$HOME_DIR/rms_venv"; do
        if [ -f "$candidate/bin/python" ]; then
            venv_found="$candidate"
            break
        fi
    done
    if [ -n "$venv_found" ]; then
        info "RMS venv found at $venv_found"
    else
        warn "RMS venv not found in standard locations (~/RMS/venv, ~/vRMS, ~/rms_venv)"
        local custom_venv
        custom_venv=$(prompt "Enter path to RMS venv (or press Enter to abort)" "")
        if [ -z "$custom_venv" ] || [ ! -f "$custom_venv/bin/python" ]; then
            err "No valid RMS venv found — install RMS first, then re-run."
            missing=1
        else
            venv_found="$custom_venv"
            info "Using venv at $venv_found"
        fi
    fi
    VENV_PYTHON="${venv_found}/bin/python"

    [ "$missing" -eq 1 ] && die "Fix missing dependencies before continuing."

    # Check Python packages in RMS venv
    local venv_pip="${venv_found}/bin/pip"
    info "Checking Python packages in RMS venv..."
    for pkg in flask inotify psutil; do
        if "$VENV_PYTHON" -c "import $pkg" 2>/dev/null; then
            info "  $pkg found"
        else
            warn "  $pkg not found — installing..."
            "$venv_pip" install "$pkg" --quiet || die "Failed to install $pkg"
            info "  $pkg installed"
        fi
    done

    # Create scripts dir and copy scripts from repo
    info "Creating $SCRIPTS_DIR ..."
    mkdir -p "$SCRIPTS_DIR"

    info "Copying scripts from repo..."
    # Copy .py and .sh files (not configs, templates, or static assets)
    local copied=0
    for f in "$REPO_DIR"/*.py "$REPO_DIR"/*.sh; do
        [ -f "$f" ] || continue
        local fname
        fname=$(basename "$f")
        # Skip cloud-only, local-only, and dev/test files
        [[ "$fname" == "rovimen_dashboard.py" ]] && continue  # cloud dashboard
        [[ "$fname" == "install.sh" ]] && continue
        [[ "$fname" == "rovimen_install.sh" ]] && continue
        [[ "$fname" == "deploy_remote.sh" ]] && continue      # local deploy tool
        [[ "$fname" == "generate_plots.py" ]] && continue     # desktop analytics
        [[ "$fname" == "live_detector.py" ]] && continue      # experimental, not deployed
        [[ "$fname" == "shower_association.py" ]] && continue  # desktop analytics
        [[ "$fname" == "test_system.py" ]] && continue        # dev test
        [[ "$fname" == "test_color_capture_timestamps.sh" ]] && continue  # dev test
        [[ "$fname" == "rovimen_tineye.py" ]] && continue          # not yet deployed
        cp "$f" "$SCRIPTS_DIR/$fname"
        copied=$((copied+1))
    done
    info "Copied $copied script files to $SCRIPTS_DIR"
    chmod +x "$SCRIPTS_DIR"/*.sh 2>/dev/null || true

    # Copy config_defaults.json (used by config_migrate.py to fill missing fields)
    if [ -f "$REPO_DIR/config_defaults.json" ]; then
        cp "$REPO_DIR/config_defaults.json" "$SCRIPTS_DIR/config_defaults.json"
        info "Copied config_defaults.json"
    fi

    # Load installer defaults from config_defaults.json so prompts reflect
    # the canonical defaults rather than hardcoded fallbacks in this script.
    _load_defaults_from_json

    # Copy assets dir if present
    if [ -d "$REPO_DIR/assets" ]; then
        cp -r "$REPO_DIR/assets" "$SCRIPTS_DIR/"
        info "Copied assets/"
    fi

    # Copy fonts dir if present
    if [ -d "$REPO_DIR/fonts" ]; then
        cp -r "$REPO_DIR/fonts" "$SCRIPTS_DIR/"
        info "Copied fonts/"
    fi
}

# ── Detect existing config and ask whether to keep it ────────────────────────
check_existing_config() {
    local cfg="$SCRIPTS_DIR/config.json"
    [ -f "$cfg" ] || return 0

    echo
    warn "Existing config found: $cfg"
    echo -e "  ${DIM}$(python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2))" "$cfg" | head -30 | sed 's/^/    /')${NC}"
    echo -e "  ${DIM}  ...${NC}"
    echo

    if confirm "Keep existing config? (No = reconfigure from scratch)" "y"; then
        KEEP_CONFIG=true
        # Read paths from existing config so Phase 6 can still create directories
        CAPTURE_PATH=$(python3 -c "
import json
c = json.load(open('$cfg'))
print(c.get('videocapture_path') or c.get('color_capture_path') or '$HOME_DIR/color_capture')
")
        info "Keeping existing config — skipping camera/drive setup phases."
    else
        KEEP_CONFIG=false
        info "Reconfiguring from scratch."
    fi
}

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1 — HARDWARE PROBE
# ═════════════════════════════════════════════════════════════════════════════
phase1_hardware_probe() {
    hdr "Phase 1 — Hardware Probe"

    local probe_sh="$SCRIPTS_DIR/hardware_assessment.sh"
    if [ ! -f "$probe_sh" ]; then
        die "hardware_assessment.sh not found in $SCRIPTS_DIR — was Phase 0 successful?"
    fi

    info "Running hardware probe..."
    bash "$probe_sh"

    local hw="$SCRIPTS_DIR/hardware.json"
    [ -f "$hw" ] || die "hardware.json not written by probe"

    # Parse probe output
    local cpu_model cpu_cores ram_gb disk_gb capture_disk_gb vaapi vaapi_driver vaapi_h264
    cpu_model=$(python3 -c "import json; d=json.load(open('$hw')); print(d['cpu_model'])")
    cpu_cores=$(python3 -c "import json; d=json.load(open('$hw')); print(d['cpu_cores'])")
    ram_gb=$(python3 -c "import json; d=json.load(open('$hw')); print(d['ram_gb'])")
    disk_gb=$(python3 -c "import json; d=json.load(open('$hw')); print(d['disk_gb'])")
    capture_disk_gb=$(python3 -c "import json; d=json.load(open('$hw')); print(d['capture_disk_gb'])")
    vaapi=$(python3 -c "import json; d=json.load(open('$hw')); print(d['vaapi'])")
    vaapi_driver=$(python3 -c "import json; d=json.load(open('$hw')); print(d.get('vaapi_driver',''))")
    vaapi_h264=$(python3 -c "import json; d=json.load(open('$hw')); print(d['vaapi_h264_encode'])")

    # Read suggested values (used as defaults later)
    COMPRESSION_LEVEL=$(python3 -c "import json; d=json.load(open('$hw')); print(d['suggested']['compression_level'])")
    ENCODE_WORKERS=$(python3 -c "import json; d=json.load(open('$hw')); print(d['suggested']['encode_workers'])")
    RETENTION_COLOR_DAYS=$(python3 -c "import json; d=json.load(open('$hw')); print(d['suggested']['retention_color_days'])")
    VAAPI="$vaapi"
    VAAPI_DEVICE=$(python3 -c "import json; d=json.load(open('$hw')); print(d.get('vaapi_device',''))")
    VAAPI_DRIVER="$vaapi_driver"

    echo
    echo -e "  ${BOLD}CPU:${NC}    $cpu_model · $cpu_cores cores"
    echo -e "  ${BOLD}RAM:${NC}    $ram_gb GB"
    echo -e "  ${BOLD}Disk:${NC}   $disk_gb GB home / $capture_disk_gb GB capture"
    if [ "$vaapi" = "True" ]; then
        local h264_mark=""
        [ "$vaapi_h264" = "True" ] && h264_mark="${GRN}✓ H.264 encode${NC}"
        echo -e "  ${BOLD}VAAPI:${NC}  $vaapi_driver $h264_mark"
    else
        echo -e "  ${BOLD}VAAPI:${NC}  ${YLW}not detected — will use CPU encoding${NC}"
    fi
    echo
}

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2 — CAPTURE DRIVE SELECTION
# ═════════════════════════════════════════════════════════════════════════════
phase2_capture_drive() {
    hdr "Phase 2 — Capture Drive Selection"

    echo -e "  ${DIM}Mounted filesystems:${NC}"
    echo

    # Collect eligible mounts (exclude tmpfs, loop, squashfs, boot, efi, snap)
    local mounts=()
    while IFS= read -r line; do
        mounts+=("$line")
    done < <(df -h --output=source,size,avail,target 2>/dev/null | tail -n +2 | \
        awk '$1 !~ /^(tmpfs|devtmpfs|udev|overlay|none)/ && $4 !~ /^\/(boot|snap|run|sys|proc|dev|^\/)/ && $1 !~ /loop/ && $4 != "/" {print $1, $2, $3" free", $4}' | \
        grep -v "^$")

    if [ ${#mounts[@]} -eq 0 ]; then
        warn "Could not list filesystems — defaulting to home disk"
        CAPTURE_PATH="$HOME_DIR/color_capture"
        info "Capture path: $CAPTURE_PATH"
        return
    fi

    local i=1
    for m in "${mounts[@]}"; do
        printf "  ${BOLD}%2d)${NC}  %s\n" "$i" "$m"
        i=$((i+1))
    done
    echo
    echo -e "  ${BOLD} 0)${NC}  Use home disk (${GRN}~/color_capture${NC})"
    echo

    local choice
    choice=$(prompt "Select capture drive [0]" "0")

    if [ "$choice" -eq 0 ] 2>/dev/null || [ -z "$choice" ]; then
        CAPTURE_PATH="$HOME_DIR/color_capture"
    else
        local idx=$((choice - 1))
        if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#mounts[@]}" ]; then
            local mountpt
            mountpt=$(echo "${mounts[$idx]}" | awk '{print $NF}')
            # Strip trailing slash but guard against root mount "/" becoming empty
            mountpt="${mountpt%/}"
            [ -z "$mountpt" ] && mountpt="/"
            CAPTURE_PATH="${mountpt}/color_capture"
        else
            warn "Invalid selection — using home disk"
            CAPTURE_PATH="$HOME_DIR/color_capture"
        fi
    fi

    info "Capture path set to: $CAPTURE_PATH"
}

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 3 — CAMERA DISCOVERY
# ═════════════════════════════════════════════════════════════════════════════
phase3_camera_discovery() {
    hdr "Phase 3 — Camera Discovery"

    # Pre-fill camera codes, coordinates, and pointing angles from RMS configs + platepars
    declare -A rms_ip_to_code   # ip    → station code
    declare -A rms_code_to_az   # code  → az_centre
    declare -A rms_code_to_alt  # code  → alt_centre
    local first_cfg=true rms_found=0

    # Support gmn-style (RMS_cam*/), raul-style (source/Stations/*/), and luci-style (source/RMS/) layouts
    for cfg in "$HOME_DIR"/RMS_cam*/.config "$HOME_DIR"/source/Stations/*/.config "$HOME_DIR"/source/RMS/.config; do
        [ -f "$cfg" ] || continue
        local ip code
        ip=$(grep -m1 '^camera_ip:' "$cfg" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]') || true
        # Fallback: extract IP from 'device: rtsp://IP:port/...' (GStreamer-style configs)
        if [ -z "$ip" ]; then
            ip=$(grep -m1 '^device:' "$cfg" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1) || true
        fi
        code=$(grep -m1 '^stationID:' "$cfg" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]') || true
        [ -n "$ip" ] && [ -n "$code" ] && rms_ip_to_code["$ip"]="$code" && rms_found=$((rms_found+1))

        # Station coords — read once from the first config (same for all cameras)
        if [ "$first_cfg" = true ] && [ -n "$code" ]; then
            RMS_LAT=$(grep -m1 '^latitude:'  "$cfg" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
            RMS_LON=$(grep -m1 '^longitude:' "$cfg" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
            RMS_ELEV=$(grep -m1 '^elevation:' "$cfg" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
            first_cfg=false
        fi

        # Pointing angles from platepar (same directory as .config)
        local platepar
        platepar="$(dirname "$cfg")/platepar_cmn2010.cal"
        if [ -f "$platepar" ] && [ -n "$code" ]; then
            local az alt
            az=$(python3  -c "import json; p=json.load(open('$platepar')); print(round(p['az_centre'],  1))" 2>/dev/null || true)
            alt=$(python3 -c "import json; p=json.load(open('$platepar')); print(round(p['alt_centre'], 1))" 2>/dev/null || true)
            [ -n "$az"  ] && rms_code_to_az["$code"]="$az"
            [ -n "$alt" ] && rms_code_to_alt["$code"]="$alt"
        fi
    done

    if [ "$rms_found" -eq 0 ]; then
        warn "No RMS configs found in standard locations (~/RMS_cam*/.config, ~/source/Stations/*/.config)."
        local manual_parent
        manual_parent=$(prompt "  Enter parent directory containing RMS_cam* folders (or Enter to skip)" "$HOME_DIR")
        if [ -n "$manual_parent" ]; then
            manual_parent="${manual_parent/#\~/$HOME_DIR}"
            local _load_rms_cfg
            _load_rms_cfg() {
                local cfg="$1"
                [ -f "$cfg" ] || return
                local ip code
                ip=$(grep -m1 '^camera_ip:' "$cfg" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]') || true
                if [ -z "$ip" ]; then
                    ip=$(grep -m1 '^device:' "$cfg" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1) || true
                fi
                code=$(grep -m1 '^stationID:' "$cfg" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]') || true
                [ -n "$ip" ] && [ -n "$code" ] || { warn "  Could not read camera_ip/stationID from $cfg"; return; }
                rms_ip_to_code["$ip"]="$code"
                rms_found=$((rms_found+1))
                if [ "$first_cfg" = true ]; then
                    RMS_LAT=$(grep -m1 '^latitude:'  "$cfg" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
                    RMS_LON=$(grep -m1 '^longitude:' "$cfg" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
                    RMS_ELEV=$(grep -m1 '^elevation:' "$cfg" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
                    first_cfg=false
                fi
                local platepar
                platepar="$(dirname "$cfg")/platepar_cmn2010.cal"
                if [ -f "$platepar" ]; then
                    local az alt
                    az=$(python3  -c "import json; p=json.load(open('$platepar')); print(round(p['az_centre'],  1))" 2>/dev/null || true)
                    alt=$(python3 -c "import json; p=json.load(open('$platepar')); print(round(p['alt_centre'], 1))" 2>/dev/null || true)
                    [ -n "$az"  ] && rms_code_to_az["$code"]="$az"
                    [ -n "$alt" ] && rms_code_to_alt["$code"]="$alt"
                fi
                info "  Loaded: $ip → $code"
            }
            for cfg in "$manual_parent"/RMS_cam*/.config \
                       "$manual_parent"/source/Stations/*/.config \
                       "$manual_parent"/source/RMS/.config; do
                [ -f "$cfg" ] && _load_rms_cfg "$cfg"
            done
            if [ "$rms_found" -eq 0 ]; then
                warn "  No RMS configs found under $manual_parent"
            fi
        fi
        echo
    fi

    if [ "$rms_found" -gt 0 ]; then
        info "Pre-loaded from RMS configs:"
        [ -n "$RMS_LAT" ] && dim "  Coordinates: $RMS_LAT, $RMS_LON (elev ${RMS_ELEV}m)"
        for ip in "${!rms_ip_to_code[@]}"; do
            local c="${rms_ip_to_code[$ip]}"
            local az_str="${rms_code_to_az[$c]:-not found}"
            local alt_str="${rms_code_to_alt[$c]:-not found}"
            dim "  $ip → $c  (az=$az_str  alt=$alt_str)"
        done
        echo
    fi

    # Detect local subnet from routing table
    local subnet=""
    subnet=$(ip route 2>/dev/null | awk '/proto kernel/ && /src/ {print $1}' | \
             grep -E '^192\.168\.' | head -1 || true)

    if [ -z "$subnet" ]; then
        subnet=$(ip route 2>/dev/null | awk '/proto kernel/ && /src/ {print $1}' | \
                 grep -v '^169\.' | head -1 || true)
    fi

    local scan_base=""
    if [ -n "$subnet" ]; then
        # e.g. 192.168.1.0/24 → 192.168.1
        scan_base=$(echo "$subnet" | sed 's|\.[0-9]*/.*||')
        info "Detected subnet: $subnet (scanning ${scan_base}.1–254 on port 554)"
    else
        warn "Could not auto-detect subnet."
        local manual_base
        manual_base=$(prompt "Enter subnet base to scan (e.g. 192.168.1)" "192.168.1")
        scan_base="$manual_base"
    fi

    echo
    start_spinner "Scanning for cameras on port 554..."

    local discovered_ips=()
    while IFS= read -r ip; do
        discovered_ips+=("$ip")
    done < <(
        printf '%s\n' "${scan_base}."{1..254} | \
        xargs -P 50 -I{} bash -c \
            'nc -z -w1 "$1" 554 2>/dev/null && echo "$1"' _ {} 2>/dev/null | \
        sort -t. -k4 -n || true
    )

    stop_spinner

    if [ ${#discovered_ips[@]} -eq 0 ]; then
        warn "No cameras found on ${scan_base}.x:554"
        # Fall back to IPs pre-loaded from RMS configs
        if [ ${#rms_ip_to_code[@]} -gt 0 ]; then
            warn "Using IPs from RMS configs as fallback (cameras may be on a different subnet)"
            for ip in "${!rms_ip_to_code[@]}"; do
                discovered_ips+=("$ip")
            done
            # Sort by last octet for consistent ordering
            IFS=$'\n' discovered_ips=($(printf '%s\n' "${discovered_ips[@]}" | sort -t. -k4 -n))
            unset IFS
        fi
    else
        info "Found ${#discovered_ips[@]} device(s) with port 554 open:"
        echo
        local i=1
        for ip in "${discovered_ips[@]}"; do
            echo -e "    ${BOLD}${i})${NC}  $ip"
            i=$((i+1))
        done
        echo
    fi

    # Collect camera config for each discovered IP
    for ip in "${discovered_ips[@]}"; do
        echo -e "  ${BOLD}Camera at $ip${NC}"
        local suggested_code="${rms_ip_to_code[$ip]:-}"
        local code
        local skip_key="skip"
        [ -z "$suggested_code" ] && skip_key="/"
        while true; do
            code=$(prompt "    GMN camera code (e.g. RO000H) or type '$skip_key' to skip" "$suggested_code")
            [ "$code" = "skip" ] || [ "$code" = "/" ] && break
            [ -n "$code" ] && break
            echo -e "  ${YLW}  Enter a camera code or type '$skip_key' to exclude this IP.${NC}"
        done
        if [ "$code" = "skip" ] || [ "$code" = "/" ]; then
            dim "    Skipped $ip"
            continue
        fi
        code="${code^^}"  # uppercase

        local rotate="false"
        if confirm "    Mounted upside-down?" "n"; then
            rotate="true"
        fi

        CAM_IPS+=("$ip")
        CAM_CODES+=("$code")
        CAM_ROTATE+=("$rotate")
        CAM_AZ+=("${rms_code_to_az[$code]:-}")
        CAM_ALT+=("${rms_code_to_alt[$code]:-}")
        info "    Added $code @ $ip (rotate=$rotate)"
        echo
    done

    # Manual camera entry
    while true; do
        echo
        local manual_ip
        manual_ip=$(prompt "Any undiscovered camera to add? Enter IP (or Enter to skip)" "")
        [ -z "$manual_ip" ] && break

        local code
        code=$(prompt "    GMN camera code for $manual_ip" "")
        [ -z "$code" ] && continue
        code="${code^^}"

        local rotate="false"
        if confirm "    Mounted upside-down?" "n"; then
            rotate="true"
        fi

        CAM_IPS+=("$manual_ip")
        CAM_CODES+=("$code")
        CAM_ROTATE+=("$rotate")
        CAM_AZ+=("${rms_code_to_az[$code]:-}")
        CAM_ALT+=("${rms_code_to_alt[$code]:-}")
        info "    Added $code @ $manual_ip (rotate=$rotate)"
    done

    if [ ${#CAM_CODES[@]} -eq 0 ]; then
        die "No cameras configured. At least one camera is required."
    fi

    info "Total cameras configured: ${#CAM_CODES[@]}"
}

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 4 — RMS PATH MAPPING
# ═════════════════════════════════════════════════════════════════════════════
phase4_rms_mapping() {
    hdr "Phase 4 — RMS Path Mapping"

    local rms_data_root="$HOME_DIR/RMS_data"

    # Universal RMS path discovery: search ~/RMS_data/ up to depth 2 for any
    # CapturedFiles dir containing station-named subdirs. Works for all layouts:
    #   gmn-style:   ~/RMS_data/cam1/CapturedFiles/RO000H_DATE/
    #   raul-style:  ~/RMS_data/RO0003/CapturedFiles/RO0003_DATE/
    #   luci-style:  ~/RMS_data/CapturedFiles/RO000R_DATE/
    declare -A rms_code_to_datapath  # station_code → rms_data_path
    local rms_cam_dirs=()
    if [ -d "$rms_data_root" ]; then
        while IFS= read -r captured_dir; do
            local latest station_code
            latest=$(find "$captured_dir" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort | tail -1 || true)
            [ -z "$latest" ] && continue
            station_code=$(basename "$latest" | cut -d_ -f1)
            # Only accept valid station codes (e.g. RO000H, RO0003)
            [[ "$station_code" =~ ^[A-Z]{2}[0-9]{3,4}[A-Z]?$ ]] || continue
            local data_path
            data_path=$(dirname "$captured_dir")
            rms_code_to_datapath["$station_code"]="$data_path"
            rms_cam_dirs+=("$data_path ($station_code)")
        done < <(find "$rms_data_root" -maxdepth 2 -mindepth 1 -type d -name 'CapturedFiles' | sort)
    fi

    if [ ${#rms_cam_dirs[@]} -gt 0 ]; then
        info "Found RMS data paths:"
        for entry in "${rms_cam_dirs[@]}"; do
            dim "  $entry"
        done
    else
        warn "No RMS CapturedFiles found under $rms_data_root"
    fi
    echo

    for idx in "${!CAM_CODES[@]}"; do
        local code="${CAM_CODES[$idx]}"

        local auto_path="${rms_code_to_datapath[$code]:-}"

        # Fall back to sequential default if no match found
        local default_path="${auto_path:-${rms_data_root}/cam$((idx+1))}"

        echo -e "  ${BOLD}RMS data path for $code${NC}"
        if [ -n "$auto_path" ]; then
            dim "    Auto-detected: $auto_path"
        fi
        if [ ${#rms_cam_dirs[@]} -gt 0 ]; then
            echo -e "  ${DIM}  (1–${#rms_cam_dirs[@]} to pick from list, or type full path)${NC}"
        fi

        local rms_path
        rms_path=$(prompt "    Path" "$default_path")

        # If user entered a number, look up from list
        if [[ "$rms_path" =~ ^[0-9]+$ ]] && [ "$rms_path" -ge 1 ] && \
           [ "$rms_path" -le "${#rms_cam_dirs[@]}" ] 2>/dev/null; then
            rms_path="${rms_cam_dirs[$((rms_path-1))]}"
        fi

        CAM_RMS_PATHS+=("$rms_path")
        info "    $code → $rms_path"
        echo
    done
}

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 5 — CONFIG REVIEW & EDIT
# ═════════════════════════════════════════════════════════════════════════════

# Helper: convert bool to y/n default for confirm()
_yn() { [ "${1:-}" = true ] && echo y || echo n; }

# ── Sub-section: station info ─────────────────────────────────────────────────
_cfg_station_info() {
    echo -e "\n  ${BOLD}Station Info${NC}"
    STATION_LABEL=$(prompt "  Station label / location" "$STATION_LABEL")
    OVERLAY_NETWORK=$(prompt "  Network name" "$OVERLAY_NETWORK")
    local lat lon
    lat=$(prompt "  GPS latitude (decimal, e.g. 44.8)" "$RMS_LAT")
    lon=$(prompt "  GPS longitude (decimal, e.g. 26.7)" "$RMS_LON")
    if [ -n "$lat" ] && [ -n "$lon" ]; then
        OVERLAY_COORDS=$(dec_to_dms_overlay "$lat" "$lon")
        dim "  Formatted: $OVERLAY_COORDS"
    else
        OVERLAY_COORDS=$(prompt "  Coords string (e.g. 44 48N  26 41E)" "$OVERLAY_COORDS")
    fi
    echo
}
_sum_station_info() {
    echo -e "${DIM}${STATION_LABEL:-—}  |  ${OVERLAY_NETWORK}  |  ${OVERLAY_COORDS:-no coords}${NC}"
}

# ── Sub-section: services ─────────────────────────────────────────────────────
_cfg_services() {
    echo -e "\n  ${BOLD}Services${NC}"
    dim "  These control which pipeline stages run each morning after RMS finishes."
    echo

    dim "  Marks clips that contain confirmed RMS meteor detections."
    dim "  Required for locked clip retention and quality encoding."
    if confirm "  Enable detection lock?" "$(_yn "$SERVICE_DETECTION_LOCK_ENABLED")"; then
        SERVICE_DETECTION_LOCK_ENABLED=true
    else
        SERVICE_DETECTION_LOCK_ENABLED=false
    fi
    echo

    dim "  Builds a composite stack image from all frames in each clip."
    dim "  Required for timelapse build — disabling this also disables timelapse."
    if confirm "  Enable stacker?" "$(_yn "$SERVICE_STACKER_ENABLED")"; then
        SERVICE_STACKER_ENABLED=true
    else
        SERVICE_STACKER_ENABLED=false
    fi

    if [ "$SERVICE_STACKER_ENABLED" = true ]; then
        dim "  Stack in real-time as each clip closes (during the night)."
        dim "  If disabled, stacking runs only during the morning sweep."
        if confirm "  Enable real-time stacking?" "$(_yn "$SERVICE_STACKER_REALTIME")"; then
            SERVICE_STACKER_REALTIME=true
        else
            SERVICE_STACKER_REALTIME=false
        fi
    else
        SERVICE_STACKER_REALTIME=false
    fi
    echo

    if [ "$SERVICE_STACKER_ENABLED" = true ]; then
        dim "  Assembles a nightly timelapse video from the per-clip stack frames."
        if confirm "  Enable timelapse build?" "$(_yn "$SERVICE_TIMELAPSE_BUILD_ENABLED")"; then
            SERVICE_TIMELAPSE_BUILD_ENABLED=true
        else
            SERVICE_TIMELAPSE_BUILD_ENABLED=false
        fi
    else
        SERVICE_TIMELAPSE_BUILD_ENABLED=false
        dim "  Timelapse build disabled (requires stacker)."
    fi
    echo

    dim "  Re-encodes raw color clips at the configured quality level to save disk space."
    dim "  Detected meteor clips are always encoded at the highest quality regardless."
    dim "  Important: overlay burn-in, color calibration, calibration frame subtraction,"
    dim "  and video rotation are applied during encoding — these features are ONLY"
    dim "  available when the encoder is enabled."
    if confirm "  Enable encoder?" "$(_yn "$SERVICE_REENCODE_ENABLED")"; then
        SERVICE_REENCODE_ENABLED=true
    else
        SERVICE_REENCODE_ENABLED=false
    fi
    echo

    dim "  Burns metadata text and logo onto encoded video clips."
    if [ "$SERVICE_REENCODE_ENABLED" = false ] && [ "$SERVICE_STACKER_ENABLED" = false ]; then
        OVERLAY_ENABLED=false
        dim "  Overlay disabled (requires encoder or stacker to be enabled)."
    else
        if confirm "  Enable overlay?" "$(_yn "$OVERLAY_ENABLED")"; then
            OVERLAY_ENABLED=true
        else
            OVERLAY_ENABLED=false
        fi
    fi
    echo

    dim "  Exposes a local REST API used by the ROVIMEN dashboard to monitor this station."
    dim "  If disabled, this camera will not be reachable from the dashboard."
    if confirm "  Enable station API?" "$(_yn "$SERVICE_STATION_API_ENABLED")"; then
        SERVICE_STATION_API_ENABLED=true
    else
        SERVICE_STATION_API_ENABLED=false
    fi
    echo

    if confirm "  Enable archive upload (rsync to VPS)?" "$(_yn "$SERVICE_ARCHIVE_UPLOAD_ENABLED")"; then
        SERVICE_ARCHIVE_UPLOAD_ENABLED=true
        ARCHIVE_HOST=$(prompt "  Archive host (Tailscale IP or hostname)" "$ARCHIVE_HOST")
        ARCHIVE_PORT=$(prompt "  Archive SSH port" "$ARCHIVE_PORT")
        ARCHIVE_USER=$(prompt "  Archive SSH user" "$INSTALL_USER")
        ARCHIVE_BASE_PATH=$(prompt "  Archive base path" "$ARCHIVE_BASE_PATH")
        ARCHIVE_INTERVAL_MINUTES=$(prompt "  Upload interval (minutes)" "$ARCHIVE_INTERVAL_MINUTES")
        if confirm "  Upload meteor clips?"  "$(_yn "$ARCHIVE_UPLOAD_METEORS")";    then ARCHIVE_UPLOAD_METEORS=true;    else ARCHIVE_UPLOAD_METEORS=false;    fi
        if confirm "  Upload timelapses?"    "$(_yn "$ARCHIVE_UPLOAD_TIMELAPSES")"; then ARCHIVE_UPLOAD_TIMELAPSES=true; else ARCHIVE_UPLOAD_TIMELAPSES=false; fi
        if confirm "  Upload stacks?"        "$(_yn "$ARCHIVE_UPLOAD_STACKS")";     then ARCHIVE_UPLOAD_STACKS=true;     else ARCHIVE_UPLOAD_STACKS=false;     fi
    else
        SERVICE_ARCHIVE_UPLOAD_ENABLED=false
    fi
    echo
}
_svc() { [ "${1:-}" = true ] && echo on || echo off; }
_sum_services() {
    echo -e "${DIM}lock:$(_svc "$SERVICE_DETECTION_LOCK_ENABLED")  stacker:$(_svc "$SERVICE_STACKER_ENABLED")(rt:$(_svc "$SERVICE_STACKER_REALTIME"))  timelapse:$(_svc "$SERVICE_TIMELAPSE_BUILD_ENABLED")  encoder:$(_svc "$SERVICE_REENCODE_ENABLED")  overlay:$(_svc "$OVERLAY_ENABLED")  api:$(_svc "$SERVICE_STATION_API_ENABLED")  archive:$(_svc "$SERVICE_ARCHIVE_UPLOAD_ENABLED")${NC}"
}

# ── Sub-section: encoder ──────────────────────────────────────────────────────
_cfg_encoder() {
    if [ "$SERVICE_REENCODE_ENABLED" != true ]; then
        COMPRESSION_LEVEL=1
        return
    fi
    echo -e "\n  ${BOLD}Encoder${NC}"
    if [ "$VAAPI" = "True" ]; then
        dim "  VAAPI detected ($VAAPI_DRIVER) — hardware encoding available"
    else
        dim "  No VAAPI — CPU encoding will be used (libx264)"
    fi
    dim "  Controls the trade-off between file size and video quality for color clips."
    dim "  Lower levels preserve more detail but use more disk space."
    dim "  Higher levels shrink files further at the cost of some visual quality."
    dim "  This setting applies to unlocked clips only — clips with confirmed meteor"
    dim "  detections are always encoded at level 1 (near-lossless) regardless."
    dim ""
    dim "    1 — QP19 / CRF20  — near-lossless, ~85% of raw size"
    dim "    2 — QP20 / CRF21  — very slight quality loss, ~70% of raw size (default)"
    dim "    3 — QP21 / CRF22  — moderate compression, ~60% of raw size"
    dim "    4 — QP22 / CRF23  — strongest compression, ~50% of raw size"
    dim "  Actual sizes vary depending on scene content (cloud cover, star density, etc)."
    dim ""
    dim "  For most stations level 2 is recommended. Use level 1 if disk is plentiful"
    dim "  or level 3–4 on constrained storage."
    COMPRESSION_LEVEL=$(prompt "  Compression level (1–4)" "$COMPRESSION_LEVEL")
    echo
}
_sum_encoder() {
    local crf=""
    [ -n "$COMPRESSION_LEVEL" ] && crf="  (CRF$(( COMPRESSION_LEVEL + 19 )))"
    echo -e "${DIM}level ${COMPRESSION_LEVEL:-?}${crf}${NC}"
}

# ── Sub-section: retention ────────────────────────────────────────────────────
_cfg_retention() {
    echo -e "\n  ${BOLD}Retention (days)${NC}"
    RETENTION_COLOR_DAYS=$(prompt     "  Unlocked color clips" "$RETENTION_COLOR_DAYS")
    RETENTION_LOCKED_DAYS=$(prompt    "  Locked clips"         "$RETENTION_LOCKED_DAYS")
    RETENTION_STACKS_DAYS=$(prompt    "  Stacks"               "$RETENTION_STACKS_DAYS")
    RETENTION_TIMELAPSE_DAYS=$(prompt "  Timelapses"           "$RETENTION_TIMELAPSE_DAYS")
    echo
}
_sum_retention() {
    echo -e "${DIM}color:${RETENTION_COLOR_DAYS}d  locked:${RETENTION_LOCKED_DAYS}d  stacks:${RETENTION_STACKS_DAYS}d  timelapse:${RETENTION_TIMELAPSE_DAYS}d${NC}"
}

# ── Sub-section: overlay (internal menu loop) ─────────────────────────────────
_cfg_overlay() {
    [ "$OVERLAY_ENABLED" != true ] && return
    echo -e "\n  ${BOLD}Overlay${NC}"

    while true; do
        _overlay_preview
        echo -e "  ${BOLD}Overlay settings:${NC}"
        echo -e "  1)  Style              ${DIM}${OVERLAY_STYLE}${NC}"
        echo -e "  2)  Network name       ${DIM}$( [ "$OVERLAY_SHOW_NETWORK"   = true ] && echo on || echo off)${NC}"
        echo -e "  3)  Logo               ${DIM}$( [ "$OVERLAY_SHOW_LOGO"      = true ] && echo on || echo off)${NC}"
        echo -e "  4)  Timestamp          ${DIM}$( [ "$OVERLAY_SHOW_TIMESTAMP" = true ] && echo on || echo off)${NC}"
        echo -e "  5)  Station ID         ${DIM}$( [ "$OVERLAY_SHOW_STATION"   = true ] && echo on || echo off)${NC}"
        echo -e "  6)  Coordinates        ${DIM}$( [ "$OVERLAY_SHOW_COORDS"    = true ] && echo on || echo off)${NC}"
        echo -e "  7)  Pointing (az/alt)  ${DIM}$( [ "$OVERLAY_SHOW_POINTING"  = true ] && echo on || echo off)${NC}"
        echo -e "  8)  Text opacity       ${DIM}${OVERLAY_TEXT_OPACITY}${NC}"
        echo -e "  9)  Logo opacity       ${DIM}${OVERLAY_LOGO_OPACITY}${NC}"
        echo

        local choice
        choice=$(prompt "  Enter number to change, or Enter to continue" "")
        [ -z "$choice" ] && break

        case "$choice" in
            1) dim "  cinema: black bar at bottom.  standard: corners."
               local s; s=$(prompt "  Style (cinema/standard)" "$OVERLAY_STYLE"); OVERLAY_STYLE="$s" ;;
            2) if confirm "  Show network name?" "$(_yn "$OVERLAY_SHOW_NETWORK")";   then OVERLAY_SHOW_NETWORK=true;   else OVERLAY_SHOW_NETWORK=false;   fi ;;
            3) if confirm "  Show logo?"         "$(_yn "$OVERLAY_SHOW_LOGO")";      then OVERLAY_SHOW_LOGO=true;      else OVERLAY_SHOW_LOGO=false;      fi ;;
            4) if confirm "  Show timestamp?"    "$(_yn "$OVERLAY_SHOW_TIMESTAMP")"; then OVERLAY_SHOW_TIMESTAMP=true; else OVERLAY_SHOW_TIMESTAMP=false; fi ;;
            5) if confirm "  Show station ID?"   "$(_yn "$OVERLAY_SHOW_STATION")";   then OVERLAY_SHOW_STATION=true;   else OVERLAY_SHOW_STATION=false;   fi ;;
            6) if confirm "  Show coordinates?"  "$(_yn "$OVERLAY_SHOW_COORDS")";    then OVERLAY_SHOW_COORDS=true;    else OVERLAY_SHOW_COORDS=false;     fi ;;
            7) if confirm "  Show pointing?"     "$(_yn "$OVERLAY_SHOW_POINTING")";  then OVERLAY_SHOW_POINTING=true;  else OVERLAY_SHOW_POINTING=false;   fi ;;
            8) OVERLAY_TEXT_OPACITY=$(prompt "  Text opacity (0.0–1.0)" "$OVERLAY_TEXT_OPACITY") ;;
            9) OVERLAY_LOGO_OPACITY=$(prompt "  Logo opacity (0.0–1.0)" "$OVERLAY_LOGO_OPACITY") ;;
            *) warn "  Invalid choice" ;;
        esac
    done
    echo
}
_sum_overlay() {
    [ "$OVERLAY_ENABLED" != true ] && { echo -e "${DIM}disabled${NC}"; return; }
    local parts=""
    [ "$OVERLAY_SHOW_NETWORK"   = true ] && parts="${parts}network "
    [ "$OVERLAY_SHOW_LOGO"      = true ] && parts="${parts}logo "
    [ "$OVERLAY_SHOW_TIMESTAMP" = true ] && parts="${parts}timestamp "
    [ "$OVERLAY_SHOW_STATION"   = true ] && parts="${parts}station "
    [ "$OVERLAY_SHOW_COORDS"    = true ] && parts="${parts}coords "
    [ "$OVERLAY_SHOW_POINTING"  = true ] && parts="${parts}pointing"
    echo -e "${DIM}${OVERLAY_STYLE}  |  ${parts:-none}${NC}"
}

# ── Sub-section: VPS host ─────────────────────────────────────────────────────
_cfg_vps_host() {
    echo -e "\n  ${BOLD}Auto-updater VPS Host${NC}"
    dim "  Tailscale IP of the VPS that serves script bundles."
    dim "  Leave blank to disable auto-updates on this station."
    VPS_HOST=$(prompt "  VPS host IP (or leave blank to skip)" "$VPS_HOST")
    echo
}
_sum_vps_host() {
    [ -n "$VPS_HOST" ] && echo -e "${DIM}${VPS_HOST}${NC}" || echo -e "${DIM}(disabled)${NC}"
}

# ── Sub-section: update channel ───────────────────────────────────────────────
_cfg_update_channel() {
    if [ -z "$VPS_HOST" ]; then
        dim "  (Skipping update channel — no VPS host configured)"
        return
    fi
    echo -e "\n  ${BOLD}Update Channel${NC}"
    dim "  Controls which branch the auto-updater pulls from."
    dim "  main: stable releases. dev: for admins and testing only."
    UPDATE_CHANNEL=$(prompt "  Update channel (main/dev)" "$UPDATE_CHANNEL")
    [ "$UPDATE_CHANNEL" = "dev" ] && warn "  dev channel selected — for admins and testing only!"
    echo
}
_sum_update_channel() {
    [ -n "$VPS_HOST" ] && echo -e "${DIM}${UPDATE_CHANNEL}${NC}" || echo -e "${DIM}n/a${NC}"
}

# ── Main phase ────────────────────────────────────────────────────────────────
phase5_config_review() {
    hdr "Phase 5 — Config Review & Edit"

    # First pass — run all sections sequentially
    _cfg_station_info
    _cfg_services
    _cfg_encoder
    _cfg_retention
    _cfg_overlay
    _cfg_vps_host
    _cfg_update_channel

    # Review menu — revisit any section before confirming
    while true; do
        echo
        echo -e "  ${BOLD}Review configuration:${NC}"
        echo -e "  ${DIM}  Pick a section to edit, or press Enter to continue.${NC}"
        echo
        echo -e "  1)  Station info     $(_sum_station_info)"
        echo -e "  2)  Services         $(_sum_services)"
        [ "$SERVICE_REENCODE_ENABLED" = true ] && \
            echo -e "  3)  Encoder          $(_sum_encoder)"
        echo -e "  4)  Retention        $(_sum_retention)"
        [ "$OVERLAY_ENABLED" = true ] && \
            echo -e "  5)  Overlay          $(_sum_overlay)"
        echo -e "  6)  VPS host         $(_sum_vps_host)"
        echo -e "  7)  Update channel   $(_sum_update_channel)"
        echo

        local choice
        choice=$(prompt "  Section to edit (or Enter to continue)" "")
        [ -z "$choice" ] && break

        case "$choice" in
            1) _cfg_station_info ;;
            2) _cfg_services ;;
            3) _cfg_encoder ;;
            4) _cfg_retention ;;
            5) _cfg_overlay ;;
            6) _cfg_vps_host; _cfg_update_channel ;;
            7) _cfg_update_channel ;;
            *) warn "  Invalid choice" ;;
        esac
    done

    # Show proposed config
    echo
    echo -e "  ${BOLD}Proposed configuration:${NC}"
    echo -e "  ${DIM}──────────────────────────────────────────${NC}"
    _build_config_json | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin), indent=4))" | sed 's/^/    /'
    echo
    echo -e "  ${DIM}──────────────────────────────────────────${NC}"
    echo

    if ! confirm "Confirm and proceed with installation?" "y"; then
        die "Installation cancelled by user."
    fi
}

# ── Internal: emit config JSON to stdout ─────────────────────────────────────
_build_config_json() {
    python3 - <<PYEOF
import json, sys

home = "$HOME_DIR"
scripts = "$SCRIPTS_DIR"

# Build stations block
cam_ips    = """${CAM_IPS[*]:-}""".split()
cam_codes  = """${CAM_CODES[*]:-}""".split()
cam_rotate = """${CAM_ROTATE[*]:-}""".split()
cam_rms    = """${CAM_RMS_PATHS[*]:-}""".split()
cam_az     = """${CAM_AZ[*]:-}""".split()
cam_alt    = """${CAM_ALT[*]:-}""".split()

# Pad az/alt lists to match camera count (may be shorter if some cameras lack platepars)
while len(cam_az)  < len(cam_codes): cam_az.append("")
while len(cam_alt) < len(cam_codes): cam_alt.append("")

stations = {}
for ip, code, rot, rms, az, alt in zip(cam_ips, cam_codes, cam_rotate, cam_rms, cam_az, cam_alt):
    rtsp = f"rtsp://admin:@{ip}:554/user=admin&password=&channel=1&stream=0.sdp"
    entry = {
        "rms_data_path": rms,
        "camera_rtsp": rtsp,
        "rotate": rot.lower() == "true",
    }
    if az:  entry["az"]  = float(az)
    if alt: entry["alt"] = float(alt)
    stations[code] = entry

rms_lat  = "$RMS_LAT"
rms_lon  = "$RMS_LON"
rms_elev = "$RMS_ELEV"

vaapi_enabled = "$VAAPI".lower() == "true"

cfg = {
    "stations": stations,

    "vps_host": "$VPS_HOST" or None,
    "update_channel": "$UPDATE_CHANNEL",

    "latitude":  float(rms_lat)  if rms_lat  else None,
    "longitude": float(rms_lon)  if rms_lon  else None,
    "elevation": float(rms_elev) if rms_elev else None,

    "videocapture_path": "$CAPTURE_PATH",
    "clips_path":        f"{home}/meteor_clips",
    "log_path":          f"{home}/logs",

    "segment_duration":          20,
    "ff_idle_timeout_minutes":   30,
    "min_disk_gb_free":          1,
    "compression_level":         int("$COMPRESSION_LEVEL"),
    "encode_workers":            int("$ENCODE_WORKERS"),

    "capabilities": {
        "vaapi":        vaapi_enabled,
        "vaapi_driver": "$VAAPI_DRIVER",
        "vaapi_device": "$VAAPI_DEVICE",
    },

    "services": {
        "stacker":         {"enabled": "$SERVICE_STACKER_ENABLED".lower() == "true", "realtime": "$SERVICE_STACKER_REALTIME".lower() == "true"},
        "reencode":        {"enabled": "$SERVICE_REENCODE_ENABLED".lower() == "true"},
        "detection_lock":  {"enabled": "$SERVICE_DETECTION_LOCK_ENABLED".lower() == "true"},
        "timelapse_build": {"enabled": "$SERVICE_TIMELAPSE_BUILD_ENABLED".lower() == "true"},
        "station_api":     {"enabled": "$SERVICE_STATION_API_ENABLED".lower() == "true"},
        "archive_upload":  {"enabled": "$SERVICE_ARCHIVE_UPLOAD_ENABLED".lower() == "true"},
    },

    "archive": {
        "enabled":            "$SERVICE_ARCHIVE_UPLOAD_ENABLED".lower() == "true",
        "host":               "$ARCHIVE_HOST",
        "port":               int("$ARCHIVE_PORT"),
        "user":               "$ARCHIVE_USER",
        "ssh_key":            "$ARCHIVE_SSH_KEY",
        "base_path":          "$ARCHIVE_BASE_PATH",
        "upload_meteors":     "$ARCHIVE_UPLOAD_METEORS".lower() == "true",
        "upload_timelapses":  "$ARCHIVE_UPLOAD_TIMELAPSES".lower() == "true",
        "upload_stacks":      "$ARCHIVE_UPLOAD_STACKS".lower() == "true",
        "interval_minutes":   int("$ARCHIVE_INTERVAL_MINUTES"),
    },

    "detection": {
        "pre_seconds":  int("$DETECTION_PRE_SECONDS"),
        "post_seconds": int("$DETECTION_POST_SECONDS"),
    },

    "retention": {
        "color_days":     int("$RETENTION_COLOR_DAYS"),
        "locked_days":    int("$RETENTION_LOCKED_DAYS"),
        "stacks_days":    int("$RETENTION_STACKS_DAYS"),
        "timelapse_days": int("$RETENTION_TIMELAPSE_DAYS"),
    },

    "disk": {
        "warn_pct":    85,
        "nuclear_pct": 90,
        "extreme_pct": 95,
    },

    "overlay": {
        "enabled":        "$OVERLAY_ENABLED".lower() == "true",
        "style":          "$OVERLAY_STYLE",
        "font":           f"{scripts}/fonts/VCR_OSD_MONO_1.001.ttf",
        "font_size":      19,
        "logo":           f"{scripts}/assets/astromania_text.png",
        "network":        "$OVERLAY_NETWORK",
        "coords":         "$OVERLAY_COORDS",
        "text_opacity":   float("$OVERLAY_TEXT_OPACITY"),
        "logo_opacity":   float("$OVERLAY_LOGO_OPACITY"),
        "show_logo":      "$OVERLAY_SHOW_LOGO".lower()      == "true",
        "show_network":   "$OVERLAY_SHOW_NETWORK".lower()   == "true",
        "show_timestamp": "$OVERLAY_SHOW_TIMESTAMP".lower() == "true",
        "show_station":   "$OVERLAY_SHOW_STATION".lower()   == "true",
        "show_coords":    "$OVERLAY_SHOW_COORDS".lower()    == "true",
        "show_pointing":  "$OVERLAY_SHOW_POINTING".lower()  == "true",
    },
}

print(json.dumps(cfg))
PYEOF
}

# ── Archive SSH key generation and authorization ──────────────────────────────
_setup_archive_ssh_key() {
    echo
    echo -e "  ${BOLD}Archive SSH Key Setup${NC}"

    # Generate key if it doesn't already exist
    if [ -f "$ARCHIVE_SSH_KEY" ]; then
        info "SSH key already exists: $ARCHIVE_SSH_KEY"
    else
        info "Generating SSH key pair at $ARCHIVE_SSH_KEY ..."
        ssh-keygen -t ed25519 -C "rovimen-archive@$(hostname)" \
            -f "$ARCHIVE_SSH_KEY" -N "" -q
        info "Key generated"
    fi

    local pubkey
    pubkey=$(cat "${ARCHIVE_SSH_KEY}.pub")

    echo
    echo -e "  ${BOLD}${YLW}Action required — authorize this station on the archive host:${NC}"
    echo
    echo -e "  On ${BOLD}${ARCHIVE_USER}@${ARCHIVE_HOST}${NC}, run:"
    echo
    echo -e "  ${DIM}mkdir -p ~/.ssh && chmod 700 ~/.ssh${NC}"
    echo -e "  ${DIM}echo \"${pubkey}\" >> ~/.ssh/authorized_keys${NC}"
    echo -e "  ${DIM}chmod 600 ~/.ssh/authorized_keys${NC}"
    echo
    echo -e "  ${BOLD}Public key:${NC}"
    echo -e "  ${CYN}${pubkey}${NC}"
    echo

    # Wait for user to confirm before testing
    ask "Press Enter once the key is authorized on the archive host (or Ctrl+C to skip test)..."
    IFS= read -r _

    # Test connection
    info "Testing SSH connection to ${ARCHIVE_USER}@${ARCHIVE_HOST}:${ARCHIVE_PORT} ..."
    local ssh_opts=(-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new
                    -i "$ARCHIVE_SSH_KEY" -p "$ARCHIVE_PORT")
    if ssh "${ssh_opts[@]}" "${ARCHIVE_USER}@${ARCHIVE_HOST}" "mkdir -p ${ARCHIVE_BASE_PATH} && echo ok" 2>/dev/null | grep -q ok; then
        info "Connection successful. Archive base path created: ${ARCHIVE_BASE_PATH}"
    else
        warn "SSH test failed. Archive upload will be configured but may not work until the key is authorized."
        warn "Re-test manually: ssh -i $ARCHIVE_SSH_KEY -p $ARCHIVE_PORT ${ARCHIVE_USER}@${ARCHIVE_HOST}"
    fi
    echo
}

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 6 — INSTALLATION
# ═════════════════════════════════════════════════════════════════════════════
phase6_install() {
    hdr "Phase 6 — Installation"

    # 6a — Stop any already-running rovimen services before touching files
    local _running_services=()
    for svc in color-capture rovimen-station-api camera-focus; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            info "Stopping $svc ..."
            sudo systemctl stop "$svc"
            _running_services+=("$svc")
        fi
    done

    # Stop and disable storagewatch if present (replaced by janitor cron)
    if systemctl is-active --quiet rovimen-storagewatch 2>/dev/null; then
        sudo systemctl stop rovimen-storagewatch 2>/dev/null || true
    fi
    if systemctl is-enabled --quiet rovimen-storagewatch 2>/dev/null; then
        sudo systemctl disable rovimen-storagewatch 2>/dev/null || true
        info "Disabled rovimen-storagewatch (replaced by janitor cron)"
    fi

    # Stop and disable nightwatcher if present (replaced by morning cron)
    if systemctl is-active --quiet rovimen-nightwatcher 2>/dev/null; then
        sudo systemctl stop rovimen-nightwatcher 2>/dev/null || true
    fi
    if systemctl is-enabled --quiet rovimen-nightwatcher 2>/dev/null; then
        sudo systemctl disable rovimen-nightwatcher 2>/dev/null || true
        info "Disabled rovimen-nightwatcher (replaced by morning cron)"
    fi

    # 6b — Write config.json (skipped if keeping existing config)
    local cfg_path="$SCRIPTS_DIR/config.json"
    if [ "$KEEP_CONFIG" = "true" ]; then
        info "Keeping existing config.json"
    else
        info "Writing $cfg_path ..."
        _build_config_json | python3 -m json.tool > "$cfg_path"
        info "config.json written"
    fi

    # Create required directories
    # CAPTURE_PATH may be on a root-owned mount — use sudo + chown
    if ! mkdir -p "$CAPTURE_PATH" 2>/dev/null; then
        sudo mkdir -p "$CAPTURE_PATH"
        sudo chown "$INSTALL_USER":"$INSTALL_USER" "$CAPTURE_PATH"
    fi
    mkdir -p "$HOME_DIR/meteor_clips" "$HOME_DIR/logs"
    info "Directories created"

    # 6c — Archive SSH key setup
    if [ "$SERVICE_ARCHIVE_UPLOAD_ENABLED" = "true" ]; then
        _setup_archive_ssh_key
    fi

    # 6d — Install systemd service files
    local venv_python="$VENV_PYTHON"
    local user_home="$HOME_DIR"

    info "Installing systemd services..."
    for svc in "$REPO_DIR"/*.service; do
        [ -f "$svc" ] || continue
        local svc_name
        svc_name=$(basename "$svc")

        # Skip cloud-only, experimental, and obsolete services
        [[ "$svc_name" == *dashboard* ]] && continue
        [[ "$svc_name" == "live_detector.service" ]] && continue
        [[ "$svc_name" == "rovimen-tineye.service" ]] && continue

        # User-level service: archive-upload runs as a user unit
        if [[ "$svc_name" == "archive-upload.service" ]]; then
            if [ "$SERVICE_ARCHIVE_UPLOAD_ENABLED" = "true" ]; then
                local user_unit_dir="$HOME_DIR/.config/systemd/user"
                mkdir -p "$user_unit_dir"
                cp "$svc" "$user_unit_dir/$svc_name"
                info "  Installed $svc_name (user-level)"
                # Enable lingering so user units survive logout
                sudo loginctl enable-linger "$INSTALL_USER" 2>/dev/null || \
                    warn "Could not enable linger for $INSTALL_USER"
            fi
            continue
        fi

        local tmp_svc
        tmp_svc=$(mktemp /tmp/rovimen_svc_XXXXXX.service)

        # Substitute User=, Group=, WorkingDirectory=, ExecStart= venv path
        sed \
            -e "s|User=gmn|User=$INSTALL_USER|g" \
            -e "s|Group=gmn|Group=$INSTALL_USER|g" \
            -e "s|/home/gmn/RMS/venv/bin/python|$venv_python|g" \
            -e "s|/home/gmn/rovimen_scripts|$SCRIPTS_DIR|g" \
            -e "s|/home/gmn/|$user_home/|g" \
            "$svc" > "$tmp_svc"

        sudo cp "$tmp_svc" "/etc/systemd/system/$svc_name"
        rm "$tmp_svc"
        info "  Installed $svc_name"
    done

    # 6e — Sudoers rule (allows station scripts to manage services and cron
    #      files without password prompts, even when running unattended)
    info "Installing sudoers rule for service management..."
    local sudoers_file="/etc/sudoers.d/rovimen"
    # Detect real binary paths (differ between Debian/Ubuntu and RHEL/Rocky)
    local _systemctl _cp _chmod _rm _tee _reboot
    _systemctl=$(command -v systemctl)
    _cp=$(command -v cp)
    _chmod=$(command -v chmod)
    _rm=$(command -v rm)
    _tee=$(command -v tee)
    _reboot=$(command -v reboot || echo /sbin/reboot)
    # Remove old narrower rule if present
    sudo rm -f /etc/sudoers.d/rovimen-restart
    sudo tee "$sudoers_file" > /dev/null <<EOF
# ROVIMEN — passwordless operations for station management scripts
# systemctl: service start/stop/restart/enable/disable + daemon-reload
${INSTALL_USER} ALL=(root) NOPASSWD: ${_systemctl}
# cron.d file management (toggle_rovimen.sh installs/removes cron files)
${INSTALL_USER} ALL=(root) NOPASSWD: ${_cp} /tmp/rovimen_cron_dawn /etc/cron.d/rovimen-dawn
${INSTALL_USER} ALL=(root) NOPASSWD: ${_cp} /tmp/rovimen_cron_janitor /etc/cron.d/rovimen-janitor
${INSTALL_USER} ALL=(root) NOPASSWD: ${_cp} /tmp/rovimen_cron_reboot /etc/cron.d/rovimen-reboot
${INSTALL_USER} ALL=(root) NOPASSWD: ${_chmod} 644 /etc/cron.d/rovimen-dawn /etc/cron.d/rovimen-janitor /etc/cron.d/rovimen-reboot
${INSTALL_USER} ALL=(root) NOPASSWD: ${_rm} /etc/cron.d/rovimen-dawn
${INSTALL_USER} ALL=(root) NOPASSWD: ${_rm} /etc/cron.d/rovimen-janitor
${INSTALL_USER} ALL=(root) NOPASSWD: ${_rm} /etc/cron.d/rovimen-reboot
${INSTALL_USER} ALL=(root) NOPASSWD: ${_rm} /etc/cron.d/daily-reboot
# systemd service file updates (wildcard covers new services added in future)
${INSTALL_USER} ALL=(root) NOPASSWD: ${_tee} /etc/systemd/system/rovimen-updater.service
${INSTALL_USER} ALL=(root) NOPASSWD: ${_tee} /etc/systemd/system/rovimen-updater.timer
${INSTALL_USER} ALL=(root) NOPASSWD: ${_cp} * /etc/systemd/system/*.service
# Self-update this sudoers file (updater.sh keeps it current across versions)
${INSTALL_USER} ALL=(root) NOPASSWD: ${_tee} /etc/sudoers.d/rovimen
# Station reboot (station_api.py /api/reboot)
${INSTALL_USER} ALL=(root) NOPASSWD: ${_reboot}
EOF
    sudo chmod 440 "$sudoers_file"
    info "  Sudoers rule installed at $sudoers_file"

    # 6f — Reload systemd daemon (picks up newly installed service files)
    info "Reloading systemd daemon..."
    sudo systemctl daemon-reload
    # Also reload user-level units if the archive-upload service was installed
    if [ "$SERVICE_ARCHIVE_UPLOAD_ENABLED" = "true" ] && \
       [ -f "$HOME_DIR/.config/systemd/user/archive-upload.service" ]; then
        systemctl --user daemon-reload 2>/dev/null || true
        systemctl --user enable archive-upload.service 2>/dev/null || true
        info "  User-level archive-upload.service enabled"
    fi
    info "  Services registered — run 'toggle_rovimen on' to start"

    # 6g — Install auto-updater cron (only if VPS host is configured)
    if [ -n "$VPS_HOST" ]; then
        info "Installing auto-updater cron..."
        bash "$SCRIPTS_DIR/updater.sh" --install-cron
        info "  Auto-updater cron installed (runs at 12:30, 13:15, 14:00 UTC)"
    else
        info "  Skipping auto-updater cron — no VPS host configured"
    fi

    # 6h — Stamp .version so the updater does not treat a fresh install as stale
    local version_file="$SCRIPTS_DIR/.version"
    local bundle_path
    if [ "$UPDATE_CHANNEL" = "dev" ]; then
        bundle_path="/opt/rovimen/station-bundle-dev"
    else
        bundle_path="/opt/rovimen/station-bundle"
    fi
    local remote_ver=""
    if [ -n "$VPS_HOST" ]; then
        remote_ver=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "root@$VPS_HOST" \
            "cat $bundle_path/.version 2>/dev/null || true" 2>/dev/null || true)
    fi
    if [ -n "$remote_ver" ]; then
        echo "$remote_ver" > "$version_file"
        info "Stamped .version: $remote_ver"
    else
        warn "Could not fetch version from VPS — updater will sync on first run"
    fi

    info "Installation complete"
}

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 6j — GSTREAMER 1.22.3
# ═════════════════════════════════════════════════════════════════════════════
phase_gstreamer() {
    hdr "GStreamer 1.22.3 (optional)"

    # Already installed?
    if [ -x "/opt/gst-1.22/bin/gst-launch-1.0" ]; then
        local installed_ver
        installed_ver=$(GST_PLUGIN_PATH="/opt/gst-1.22/lib/x86_64-linux-gnu/gstreamer-1.0" \
            LD_LIBRARY_PATH="/opt/gst-1.22/lib/x86_64-linux-gnu" \
            /opt/gst-1.22/bin/gst-launch-1.0 --version 2>/dev/null \
            | grep -oP '\d+\.\d+\.\d+' | head -1 || echo "unknown")
        info "GStreamer 1.22.3 already installed at /opt/gst-1.22/ (version: $installed_ver) — skipping."
        return 0
    fi

    # Detect system GStreamer version
    local sys_ver="unknown"
    if command -v gst-launch-1.0 &>/dev/null; then
        sys_ver=$(gst-launch-1.0 --version 2>/dev/null \
            | grep -oP '\d+\.\d+\.\d+' | head -1 || echo "unknown")
    fi

    if [ "$sys_ver" != "unknown" ]; then
        local _is_newer=false
        [ "$(printf '%s\n' "$sys_ver" "1.22.3" | sort -V | tail -1)" = "$sys_ver" ] \
            && [ "$sys_ver" != "1.22.3" ] && _is_newer=true

        if $_is_newer; then
            warn "System GStreamer is $sys_ver — versions above 1.22.3 can cause frame drops with RMS."
            dim  "Building 1.22.3 from source is strongly recommended."
        else
            info "System GStreamer: $sys_ver (≤ 1.22.3 — OK)"
            dim  "You can still build 1.22.3 to /opt/gst-1.22/ for a pinned install."
        fi
    else
        warn "GStreamer not found on system."
    fi

    echo
    if ! confirm "Build GStreamer 1.22.3 from source now? (~20-30 min)" "n"; then
        dim "Skipped. Run manually later: bash $SCRIPTS_DIR/build_gstreamer_1223.sh"
        return 0
    fi

    echo
    warn "This will take 20-30 minutes. It is safe to run in the background."
    warn "Strongly recommend running in a tmux session to survive SSH drops:"
    dim  "  tmux new -s gst_build"
    dim  "  bash $SCRIPTS_DIR/build_gstreamer_1223.sh 2>&1 | tee ~/gst_build.log"
    echo
    if ! confirm "Start the build now in this terminal?" "y"; then
        dim "Skipped. Run manually: bash $SCRIPTS_DIR/build_gstreamer_1223.sh"
        return 0
    fi

    bash "$SCRIPTS_DIR/build_gstreamer_1223.sh"
}

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 7 — SUMMARY
# ═════════════════════════════════════════════════════════════════════════════
phase7_summary() {
    hdr "Phase 7 — Summary"

    # Detect Tailscale IP for access URL
    local ts_ip=""
    ts_ip=$(tailscale ip 2>/dev/null | head -1 || true)
    [ -z "$ts_ip" ] && ts_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

    echo -e "  ${BOLD}Service Status:${NC}"
    for svc in color-capture rovimen-station-api camera-focus; do
        local status
        status=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
        if [ "$status" = "active" ]; then
            echo -e "    ${GRN}●${NC} $svc — ${GRN}active${NC}"
        else
            echo -e "    ${YLW}●${NC} $svc — ${YLW}$status${NC}"
        fi
    done

    echo
    echo -e "  ${BOLD}Paths:${NC}"
    echo -e "    Config:     $SCRIPTS_DIR/config.json"
    echo -e "    Capture:    $CAPTURE_PATH"
    echo -e "    Logs:       $HOME_DIR/logs"
    echo

    echo -e "  ${BOLD}Cameras:${NC}"
    for idx in "${!CAM_CODES[@]}"; do
        local code="${CAM_CODES[$idx]}"
        local ip="${CAM_IPS[$idx]}"
        local rot="${CAM_ROTATE[$idx]}"
        local rms="${CAM_RMS_PATHS[$idx]}"
        echo -e "    ${GRN}$code${NC}  $ip  rotate=$rot  rms=$rms"
    done

    echo
    echo -e "  ${BOLD}Useful commands:${NC}"
    echo -e "    ${DIM}# View live logs:${NC}"
    echo -e "    journalctl -fu color-capture"
    echo -e "    journalctl -fu rovimen-station-api"
    echo
    echo -e "    ${DIM}# Morning pipeline:${NC}"
    echo -e "    tail -f /var/log/rovimen_dawn.log"
    echo
    echo -e "    ${DIM}# Auto-updater:${NC}"
    echo -e "    tail -f ~/logs/updater.log"
    echo -e "    bash $SCRIPTS_DIR/updater.sh          ${DIM}# manual update check${NC}"
    echo
    echo -e "    ${DIM}# Service control:${NC}"
    echo -e "    bash $SCRIPTS_DIR/toggle_rovimen.sh status"
    echo -e "    bash $SCRIPTS_DIR/toggle_rovimen.sh restart"
    echo
    echo -e "    ${DIM}# Check station API:${NC}"
    echo -e "    curl http://localhost:7779/api/status"
    echo
    if [ -n "$ts_ip" ]; then
        echo -e "  ${BOLD}${GRN}Station is running.${NC}"
        echo -e "  Access at: ${CYN}http://${ts_ip}:7779/api/status${NC}"
    fi
    echo
    if [ "$SERVICE_ARCHIVE_UPLOAD_ENABLED" = "true" ]; then
        echo -e "  ${BOLD}Archive:${NC}"
        echo -e "    Target:  ${ARCHIVE_USER}@${ARCHIVE_HOST}:${ARCHIVE_PORT}${ARCHIVE_BASE_PATH}"
        echo -e "    SSH key: $ARCHIVE_SSH_KEY"
        echo
    fi

    echo -e "  ${BOLD}Next steps:${NC}"
    echo -e "    1. Review config:  ${CYN}$SCRIPTS_DIR/config.json${NC}"
    echo -e "    2. Start ROVIMEN:  ${CYN}bash $SCRIPTS_DIR/toggle_rovimen.sh on${NC}"
    echo -e "    3. Calibrate platepars in SkyFit2 for each camera"
    echo -e "    4. Derive color calibration gains"
    echo -e "    5. Add station to dashboard_config.yaml on the cloud VPS"
    echo -e "    6. Add Telegram token/chat_id to config.json if desired"
    echo
    echo -e "  ${GRN}${BOLD}Installation complete!${NC}"
    echo
}

# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════
main() {
    echo
    echo -e "${BOLD}${CYN}╔════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${CYN}║   ROVIMEN Station Installer                ║${NC}"
    echo -e "${BOLD}${CYN}║   Interactive setup — runs as: $INSTALL_USER${NC}"
    echo -e "${BOLD}${CYN}╚════════════════════════════════════════════╝${NC}"
    echo

    phase0_preflight
    check_existing_config
    phase1_hardware_probe
    if [ "$KEEP_CONFIG" = "false" ]; then
        phase2_capture_drive
        phase3_camera_discovery
        phase4_rms_mapping
        phase5_config_review
    fi
    phase6_install
    phase_gstreamer
    phase7_summary
}

main "$@"
