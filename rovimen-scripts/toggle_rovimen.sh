#!/usr/bin/env bash
# toggle_rovimen.sh — Enable, disable, restart, or check ROVIMEN services and crons.
#
# Usage:
#   toggle_rovimen.sh on       — enable + start all services, install crons
#   toggle_rovimen.sh off      — stop + disable all services, remove crons
#   toggle_rovimen.sh restart  — restart currently running services (no cron changes)
#   toggle_rovimen.sh status   — show service and cron state

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_USER=$(whoami)
CONFIG="$SCRIPTS_DIR/config.json"

# ── Terminal colours ──────────────────────────────────────────────────────────
GRN='\033[0;32m'; YLW='\033[1;33m'; RED='\033[0;31m'; BLU='\033[0;34m'
BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

info() { echo -e "${GRN}  ✓${NC}  $*"; }
warn() { echo -e "${YLW}  ⚠${NC}  $*"; }
err()  { echo -e "${RED}  ✗${NC}  $*" >&2; }
dim()  { echo -e "${DIM}     $*${NC}"; }

# ── Services ──────────────────────────────────────────────────────────────────
SERVICES=(color-capture rovimen-station-api)
[ -f /etc/systemd/system/camera-focus.service ] && SERVICES+=(camera-focus)
[ -f /etc/systemd/system/detection-indexer.service ] && SERVICES+=(detection-indexer)
# station-api is never stopped by 'off' — dashboard must stay reachable
STOPPABLE_SERVICES=(color-capture)
[ -f /etc/systemd/system/camera-focus.service ] && STOPPABLE_SERVICES+=(camera-focus)
[ -f /etc/systemd/system/detection-indexer.service ] && STOPPABLE_SERVICES+=(detection-indexer)

# ── Cron files ────────────────────────────────────────────────────────────────
CRON_FILES=(rovimen-dawn rovimen-janitor rovimen-reboot)

# ── Venv detection (authoritative: read from RMS service file) ────────────────
_venv_python() {
    local venv
    # Try system-level RMS service files first, then user-level
    venv=$(grep -h 'ExecStart' /etc/systemd/system/rms-cam*.service 2>/dev/null \
        | grep -oP '/[^\s]+/bin/python3?' | head -1 || true)
    if [[ -z "$venv" ]]; then
        venv=$(grep -h 'ExecStart' "$HOME/.config/systemd/user"/rms-cam*.service 2>/dev/null \
            | grep -oP '/[^\s]+/bin/python3?' | head -1 || true)
    fi
    # Fall back to known venv locations
    if [[ -z "$venv" ]]; then
        for candidate in "$HOME/vRMS/bin/python3" "$HOME/RMS/venv/bin/python3" "$HOME/RMS/venv/bin/python"; do
            if [[ -x "$candidate" ]]; then venv="$candidate"; break; fi
        done
    fi
    echo "${venv:-python3}"
}

# ── Cron content ──────────────────────────────────────────────────────────────
_write_crons() {
    local venv_python
    venv_python=$(_venv_python)

    cat > /tmp/rovimen_cron_dawn <<EOF
# ROVIMEN morning processing — every 15 min from 04:00-10:00 UTC
# Processes post-RMS pipeline: EON lock → stack → timelapse → reencode → upload
*/15 4-10 * * * $INSTALL_USER nice -n 10 ionice -c 3 $venv_python $SCRIPTS_DIR/dawn_process.py >> $HOME/logs/rovimen_dawn.log 2>&1
EOF

    cat > /tmp/rovimen_cron_janitor <<EOF
# ROVIMEN janitor — disk retention + pressure escalation every 10 min
*/10 * * * * $INSTALL_USER $venv_python $SCRIPTS_DIR/janitor_storage_watchdog.py -c $CONFIG >> $HOME/logs/rovimen_janitor.log 2>&1
EOF

    cat > /tmp/rovimen_cron_reboot <<EOF
# ROVIMEN reboot guard — waits for morning_done before rebooting (4h timeout)
0 12 * * * $INSTALL_USER $SCRIPTS_DIR/reboot_guard.sh >> $HOME/logs/rovimen_reboot.log 2>&1
EOF

    # Only write cron.d files (requires sudo) if content has changed
    local need_write=0
    for pair in "dawn:/etc/cron.d/rovimen-dawn" "janitor:/etc/cron.d/rovimen-janitor" "reboot:/etc/cron.d/rovimen-reboot"; do
        local key="${pair%%:*}" dst="${pair##*:}"
        if ! diff -q "/tmp/rovimen_cron_${key}" "$dst" > /dev/null 2>&1; then
            need_write=1
            break
        fi
    done

    local legacy_cron_exists=0
    [ -f /etc/cron.d/daily-reboot ] && legacy_cron_exists=1

    chmod +x "$SCRIPTS_DIR/reboot_guard.sh" 2>/dev/null || true

    if [ "$need_write" -eq 1 ] || [ "$legacy_cron_exists" -eq 1 ]; then
        if sudo -n true 2>/dev/null; then
            sudo cp  /tmp/rovimen_cron_dawn    /etc/cron.d/rovimen-dawn
            sudo cp  /tmp/rovimen_cron_janitor /etc/cron.d/rovimen-janitor
            sudo cp  /tmp/rovimen_cron_reboot  /etc/cron.d/rovimen-reboot
            sudo chmod 644 /etc/cron.d/rovimen-dawn /etc/cron.d/rovimen-janitor /etc/cron.d/rovimen-reboot
            [ "$legacy_cron_exists" -eq 1 ] && sudo rm /etc/cron.d/daily-reboot && info "Removed old /etc/cron.d/daily-reboot"
            info "Cron files updated"
        else
            warn "Cron files need updating but sudo requires a password — run 'toggle_rovimen.sh on' manually to apply"
        fi
    else
        info "Cron files already current"
    fi

    rm /tmp/rovimen_cron_dawn /tmp/rovimen_cron_janitor /tmp/rovimen_cron_reboot

    # User crontab — fix_cam_encoding @reboot + updater
    # Always strip existing rovimen entries first so re-running toggle on is idempotent.
    local existing
    existing=$(crontab -l 2>/dev/null || true)
    existing=$(echo "$existing" | grep -v "fix_cam_encoding" | grep -v "updater.sh" | grep -v "dawn_process" || true)
    existing="${existing}"$'\n'"@reboot sleep 120 && bash $SCRIPTS_DIR/fix_cam_encoding.sh"
    existing="${existing}"$'\n'"@reboot sleep 300 && nice -n 10 ionice -c 3 $venv_python $SCRIPTS_DIR/dawn_process.py >> $HOME/logs/rovimen_dawn.log 2>&1"
    existing="${existing}"$'\n'"30 15 * * * bash $SCRIPTS_DIR/fix_cam_encoding.sh >> $HOME/logs/rovimen_fixcam.log 2>&1"
    existing="${existing}"$'\n'"30 12 * * * /usr/bin/bash $SCRIPTS_DIR/updater.sh >> $HOME/logs/updater.log 2>&1"
    existing="${existing}"$'\n'"15 13 * * * /usr/bin/bash $SCRIPTS_DIR/updater.sh >> $HOME/logs/updater.log 2>&1"
    existing="${existing}"$'\n'"0  14 * * * /usr/bin/bash $SCRIPTS_DIR/updater.sh >> $HOME/logs/updater.log 2>&1"
    echo "$existing" | crontab -
    info "fix_cam_encoding @reboot + updater.sh crontab installed"
}

# ── Remove crons ──────────────────────────────────────────────────────────────
_remove_crons() {
    for f in "${CRON_FILES[@]}"; do
        if [ -f "/etc/cron.d/$f" ]; then
            sudo rm "/etc/cron.d/$f"
            info "Removed /etc/cron.d/$f"
        fi
    done

    local existing
    existing=$(crontab -l 2>/dev/null || true)
    local changed=false
    # fix_cam_encoding is intentionally NOT removed here — it must survive rovimen off
    # because cameras reboot independently of ROVIMEN services. Use 'fixcam off' to remove.
    if echo "$existing" | grep -q "updater.sh"; then
        existing=$(echo "$existing" | grep -v "updater.sh")
        changed=true
        info "Removed updater.sh from crontab"
    fi
    $changed && echo "$existing" | crontab - || true
}

# ── Fix-cam cron management (independent of rovimen on/off) ───────────────────
cmd_fixcam_off() {
    local existing
    existing=$(crontab -l 2>/dev/null || true)
    if echo "$existing" | grep -q "fix_cam_encoding"; then
        echo "$existing" | grep -v "fix_cam_encoding" | crontab -
        warn "fix_cam_encoding crons removed (cameras will not recover encoding after reboot)"
    else
        dim "fix_cam_encoding crons not installed — nothing to remove"
    fi
}

# ── Commands ──────────────────────────────────────────────────────────────────
cmd_on() {
    echo -e "\n${BOLD}${BLU}  ROVIMEN — Enabling${NC}\n"
    for svc in "${SERVICES[@]}"; do
        if [ -f "/etc/systemd/system/${svc}.service" ]; then
            sudo systemctl enable --now "$svc" 2>/dev/null \
                && info "Enabled + started $svc" \
                || warn "Could not start $svc"
        else
            warn "$svc.service not found — skipping"
        fi
    done
    echo
    _write_crons
    echo
    info "ROVIMEN is ON"
    echo
}

cmd_off() {
    local svcs=("${STOPPABLE_SERVICES[@]}")
    [[ "${2:-}" == "--include-api" ]] && svcs=("${SERVICES[@]}")
    echo -e "\n${BOLD}${BLU}  ROVIMEN — Disabling${NC}\n"
    for svc in "${svcs[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null \
                || systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            sudo systemctl stop    "$svc" 2>/dev/null || true
            sudo systemctl disable "$svc" 2>/dev/null || true
            info "Stopped + disabled $svc"
        else
            dim "$svc already inactive"
        fi
    done
    echo
    _remove_crons
    echo
    warn "ROVIMEN is OFF"
    echo
}

cmd_restart() {
    echo -e "\n${BOLD}${BLU}  ROVIMEN — Restarting services${NC}\n"
    local any=false
    for svc in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            sudo systemctl restart "$svc" 2>/dev/null \
                && info "Restarted $svc" \
                || err "Failed to restart $svc"
            any=true
        else
            dim "$svc not running — skipping"
        fi
    done
    $any || warn "No services were running — nothing restarted"
    echo
}

_svc_status() {
    # Returns "active/enabled [system]" or "active/enabled [user]" or "inactive/disabled"
    local svc="$1"
    local active enabled scope
    active=$(systemctl is-active  "$svc" 2>/dev/null || echo "inactive")
    enabled=$(systemctl is-enabled "$svc" 2>/dev/null || echo "disabled")
    if [ "$active" = "active" ]; then
        scope="system"
    else
        # Fallback: check user-level (for stations where services were installed --user)
        local u_active u_enabled
        u_active=$(systemctl --user is-active  "$svc" 2>/dev/null || echo "inactive")
        u_enabled=$(systemctl --user is-enabled "$svc" 2>/dev/null || echo "disabled")
        if [ "$u_active" = "active" ]; then
            active="$u_active"; enabled="$u_enabled"; scope="user"
        else
            scope=""
        fi
    fi
    echo "$active" "$enabled" "$scope"
}

cmd_status() {
    echo -e "\n${BOLD}${BLU}  ROVIMEN — Status${NC}\n"

    echo -e "  ${BOLD}Services:${NC}"
    for svc in "${SERVICES[@]}"; do
        local active enabled scope
        read -r active enabled scope <<< "$(_svc_status "$svc")"
        local scope_tag=""
        [ -n "$scope" ] && scope_tag=" ${DIM}[$scope]${NC}"
        if [ "$active" = "active" ]; then
            echo -e "    ${GRN}●${NC}  $svc — ${GRN}$active${NC} / $enabled${scope_tag}"
        else
            echo -e "    ${YLW}●${NC}  $svc — ${YLW}$active${NC} / $enabled"
        fi
    done

    echo
    echo -e "  ${BOLD}Cron files (/etc/cron.d/):${NC}"
    for f in "${CRON_FILES[@]}"; do
        if [ -f "/etc/cron.d/$f" ]; then
            echo -e "    ${GRN}✓${NC}  $f"
        else
            echo -e "    ${YLW}–${NC}  $f (not installed)"
        fi
    done

    echo
    echo -e "  ${BOLD}User crontab:${NC}"
    local tab
    tab=$(crontab -l 2>/dev/null || true)
    if echo "$tab" | grep -q "fix_cam_encoding"; then
        echo -e "    ${GRN}✓${NC}  fix_cam_encoding @reboot + 15:30 UTC"
    else
        echo -e "    ${YLW}–${NC}  fix_cam_encoding @reboot + 15:30 UTC (not installed)"
    fi
    if echo "$tab" | grep -q "updater.sh"; then
        echo -e "    ${GRN}✓${NC}  updater.sh (12:30, 13:15, 14:00 UTC)"
    else
        echo -e "    ${YLW}–${NC}  updater.sh (not installed)"
    fi
    echo
}

# ── Entry point ───────────────────────────────────────────────────────────────
case "${1:-}" in
    on)             cmd_on ;;
    off)            cmd_off "$@" ;;
    restart)        cmd_restart ;;
    status)         cmd_status ;;
    fixcam-off)     cmd_fixcam_off ;;
    --ensure-crons) _write_crons; info "Crons reconciled" ;;
    *)
        echo -e "Usage: $(basename "$0") ${BOLD}on${NC} | ${BOLD}off${NC} [--include-api] | ${BOLD}restart${NC} | ${BOLD}status${NC} | ${BOLD}fixcam-off${NC} | ${BOLD}--ensure-crons${NC}"
        exit 1
        ;;
esac
