#!/usr/bin/env bash
# updater.sh — Station script auto-updater (codename: hivemind)
#
# Pulls the latest station scripts from the VPS bundle (which CI keeps in sync
# with this repo).  Safe: preserves config.json, backs up before
# updating, restarts affected services.
#
# CI pipeline:
#   main branch push        → deploy-dashboard.yml   → VPS /opt/rovimen/station-bundle/
#   development branch push → deploy-dev-bundle.yml  → VPS /opt/rovimen/station-bundle-dev/
#
# Channel selection (set in config.json):
#   "update_channel": "main"  (default) — stable releases
#   "update_channel": "dev"             — development builds
#
# Install as a systemd timer (recommended):
#   bash updater.sh --install-timer
#
# Or install as a cron job:
#   bash updater.sh --install-cron
#
# Run manually:
#   bash ~/rovimen_scripts/updater.sh

set -euo pipefail

VPS_USER="root"
LOCAL_DIR="$HOME/rovimen_scripts"

# Read vps_host and update_channel from config.json
_read_cfg() {
python3 -c "
import json, sys
try:
    cfg = json.load(open('$LOCAL_DIR/config.json'))
    print(cfg.get('vps_host') or '')
    print(cfg.get('update_channel', 'main'))
except Exception:
    print('')
    print('main')
" 2>/dev/null || { echo ''; echo 'main'; }
}
{ read -r VPS_HOST; read -r UPDATE_CHANNEL; } <<< "$(_read_cfg)"

if [ -z "$VPS_HOST" ]; then
    echo "$(ts) [updater] No vps_host configured — skipping update"
    exit 0
fi

if [ "$UPDATE_CHANNEL" = "dev" ]; then
    BUNDLE_PATH="/opt/rovimen/station-bundle-dev"
else
    BUNDLE_PATH="/opt/rovimen/station-bundle"
    UPDATE_CHANNEL="main"
fi
VERSION_FILE="$LOCAL_DIR/.version"
BACKUP_DIR="$HOME/rovimen_scripts.bak"
LOG_DIR="$HOME/logs"
LOG_FILE="$LOG_DIR/updater.log"

SERVICES=(
    color-capture
    rovimen-station-api
)

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG_FILE"; }

mkdir -p "$LOG_DIR"

# ── Sudoers reconciliation ─────────────────────────────────────────────────────
# Keeps /etc/sudoers.d/rovimen up to date so all station management operations
# (service control, cron installs, service file updates, reboot) work without
# password prompts, even when running unattended.  Safe to run on every update.

_ensure_sudoers() {
    local sudoers_file="/etc/sudoers.d/rovimen"
    local _systemctl _cp _chmod _rm _tee _reboot
    _systemctl=$(command -v systemctl)
    _cp=$(command -v cp)
    _chmod=$(command -v chmod)
    _rm=$(command -v rm)
    _tee=$(command -v tee)
    _reboot=$(command -v reboot 2>/dev/null || echo /sbin/reboot)
    local U
    U=$(whoami)

    local desired
    desired=$(cat <<EOF
# ROVIMEN — passwordless operations for station management scripts
# systemctl: all service control + daemon-reload
$U ALL=(root) NOPASSWD: $_systemctl
# cron.d file management (toggle_rovimen.sh)
$U ALL=(root) NOPASSWD: $_cp /tmp/rovimen_cron_dawn /etc/cron.d/rovimen-dawn
$U ALL=(root) NOPASSWD: $_cp /tmp/rovimen_cron_janitor /etc/cron.d/rovimen-janitor
$U ALL=(root) NOPASSWD: $_cp /tmp/rovimen_cron_reboot /etc/cron.d/rovimen-reboot
$U ALL=(root) NOPASSWD: $_chmod 644 /etc/cron.d/rovimen-dawn /etc/cron.d/rovimen-janitor /etc/cron.d/rovimen-reboot
$U ALL=(root) NOPASSWD: $_rm /etc/cron.d/rovimen-dawn
$U ALL=(root) NOPASSWD: $_rm /etc/cron.d/rovimen-janitor
$U ALL=(root) NOPASSWD: $_rm /etc/cron.d/rovimen-reboot
$U ALL=(root) NOPASSWD: $_rm /etc/cron.d/daily-reboot
# systemd service file updates (updater.sh — wildcard covers new services)
$U ALL=(root) NOPASSWD: $_tee /etc/systemd/system/rovimen-updater.service
$U ALL=(root) NOPASSWD: $_tee /etc/systemd/system/rovimen-updater.timer
$U ALL=(root) NOPASSWD: $_cp * /etc/systemd/system/*.service
# Self-update this sudoers file (updater.sh)
$U ALL=(root) NOPASSWD: $_tee $sudoers_file
# Station reboot (station_api.py)
$U ALL=(root) NOPASSWD: $_reboot
EOF
)

    local current
    current=$(sudo -n cat "$sudoers_file" 2>/dev/null || true)
    if [ "$current" = "$desired" ]; then
        log "Sudoers already current"
        return
    fi

    if echo "$desired" | sudo -n tee "$sudoers_file" > /dev/null 2>&1; then
        sudo -n chmod 440 "$sudoers_file" 2>/dev/null || true
        # Remove old narrower rule if present
        sudo -n rm -f /etc/sudoers.d/rovimen-restart 2>/dev/null || true
        log "Sudoers updated"
    else
        log "WARN: Could not update sudoers (sudo -n failed) — run 'sudo bash ~/rovimen_scripts/updater.sh --install-sudoers' manually"
    fi
}

# ── Install helpers ────────────────────────────────────────────────────────────

install_timer() {
    local unit_dir="/etc/systemd/system"
    local service_file="$unit_dir/rovimen-updater.service"
    local timer_file="$unit_dir/rovimen-updater.timer"
    local script="$LOCAL_DIR/updater.sh"

    INSTALL_USER=$(whoami)

    sudo tee "$service_file" > /dev/null <<EOF
[Unit]
Description=ROVIMEN station script updater
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$INSTALL_USER
ExecStart=/usr/bin/bash $script
StandardOutput=journal
StandardError=journal
EOF

    sudo tee "$timer_file" > /dev/null <<EOF
[Unit]
Description=Run ROVIMEN updater three times daily in safe window (12:30, 13:15, 14:00 UTC)

[Timer]
OnCalendar=*-*-* 12:30:00
OnCalendar=*-*-* 13:15:00
OnCalendar=*-*-* 14:00:00
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable --now rovimen-updater.timer
    echo "Installed and enabled rovimen-updater.timer (fires at 12:30, 13:15, 14:00 UTC)"
    systemctl status rovimen-updater.timer --no-pager
}

install_cron() {
    local script="$LOCAL_DIR/updater.sh"
    # Three attempts in the safe daytime window (12:30, 13:15, 14:00 UTC)
    local cron_entries=(
        "30 12 * * * /usr/bin/bash $script >> $LOG_FILE 2>&1"
        "15 13 * * * /usr/bin/bash $script >> $LOG_FILE 2>&1"
        "0  14 * * * /usr/bin/bash $script >> $LOG_FILE 2>&1"
    )
    if crontab -l 2>/dev/null | grep -qF "updater.sh"; then
        echo "Cron entries already exist."
    else
        local tmp
        tmp=$(crontab -l 2>/dev/null || true)
        for entry in "${cron_entries[@]}"; do
            tmp="${tmp}"$'\n'"${entry}"
        done
        echo "$tmp" | crontab -
        echo "Cron jobs installed: fires at 12:30, 13:15, 14:00 UTC."
    fi
}

if [[ "${1:-}" == "--install-timer"   ]]; then install_timer;    exit 0; fi
if [[ "${1:-}" == "--install-cron"    ]]; then install_cron;     exit 0; fi
if [[ "${1:-}" == "--install-sudoers" ]]; then _ensure_sudoers;  exit 0; fi

check_version() {
    local local_ver remote_ver
    local_ver=$(cat "$VERSION_FILE" 2>/dev/null || echo "none")
    remote_ver=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$VPS_USER@$VPS_HOST" \
        "cat $BUNDLE_PATH/.version 2>/dev/null || echo 'unknown'" 2>/dev/null || echo 'unreachable')
    echo "Channel:  $UPDATE_CHANNEL"
    echo "Local:    $local_ver"
    echo "Remote:   $remote_ver"
    if [ "$remote_ver" = "unreachable" ] || [ "$remote_ver" = "unknown" ]; then
        echo "Status:   VPS not reachable"
    elif [ "$local_ver" = "$remote_ver" ]; then
        echo "Status:   up to date"
    else
        echo "Status:   update available"
    fi
}
if [[ "${1:-}" == "--check" ]]; then check_version; exit 0; fi

# ── Main update logic ──────────────────────────────────────────────────────────

# Safety guard — skip entirely if morning processing is running
if pgrep -f dawn_process.py > /dev/null 2>&1; then
    log "SKIP: morning processing is running — deferring update"
    exit 0
fi

# Note whether ffmpeg capture is active — scripts/config will still be updated,
# but color-capture will not be restarted mid-segment to avoid recording gaps.
_ffmpeg_active=false
if pgrep -f 'ffmpeg.*_color\.mkv' > /dev/null 2>&1; then
    log "NOTE: ffmpeg capture is active — scripts will be updated but color-capture restart will be skipped"
    _ffmpeg_active=true
fi

log "Starting update check (channel: $UPDATE_CHANNEL)..."

# VPS reachable?
if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$VPS_USER@$VPS_HOST" true 2>/dev/null; then
    log "WARN: VPS unreachable at $VPS_HOST — skipping update"
    exit 0
fi

# Emergency freeze — if FREEZE file exists on VPS, abort update
if ssh -o ConnectTimeout=8 -o BatchMode=yes "$VPS_USER@$VPS_HOST" \
    "test -f $BUNDLE_PATH/FREEZE" 2>/dev/null; then
    log "WARN: Update frozen by VPS — skipping (remove $BUNDLE_PATH/FREEZE to unfreeze)"
    exit 0
fi

# Compare versions
REMOTE_VERSION=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$VPS_USER@$VPS_HOST" \
    "cat $BUNDLE_PATH/.version 2>/dev/null || echo 'unknown'")

if [ "$REMOTE_VERSION" = "unknown" ]; then
    log "WARN: No version file on VPS — bundle not deployed yet"
    exit 0
fi

LOCAL_VERSION=$(cat "$VERSION_FILE" 2>/dev/null || echo "none")
log "Local: $LOCAL_VERSION | Remote: $REMOTE_VERSION"

if [ "$LOCAL_VERSION" = "$REMOTE_VERSION" ]; then
    log "Already up to date."
    exit 0
fi

log "New version available: $REMOTE_VERSION — updating..."

# Backup — copy first, then atomically replace to avoid losing backup on interruption
if [ -d "$LOCAL_DIR" ]; then
    cp -a "$LOCAL_DIR" "${BACKUP_DIR}.new"
    rm -rf "$BACKUP_DIR"
    mv "${BACKUP_DIR}.new" "$BACKUP_DIR"
    log "Backed up to $BACKUP_DIR"
fi

# Download — never overwrite config.json
if ! rsync -az --timeout=60 \
    -e "ssh -o ConnectTimeout=10 -o BatchMode=yes" \
    --exclude='config.json' \
    --exclude='*.log' \
    --exclude='hardware.json' \
    "$VPS_USER@$VPS_HOST:$BUNDLE_PATH/" "$LOCAL_DIR/"; then
    log "ERROR: rsync failed — restoring backup"
    rm -rf "$LOCAL_DIR"
    mv "$BACKUP_DIR" "$LOCAL_DIR"
    exit 1
fi

log "Scripts updated to $REMOTE_VERSION"

# Update service files if they have changed
svc_reload=false
for svc_src in "$LOCAL_DIR"/*.service; do
    [ -f "$svc_src" ] || continue
    svc_name=$(basename "$svc_src")
    svc_dst="/etc/systemd/system/$svc_name"
    if [ -f "$svc_dst" ]; then
        if ! diff -q "$svc_src" "$svc_dst" > /dev/null 2>&1; then
            log "Updating service file: $svc_name"
            if sudo cp "$svc_src" "$svc_dst" 2>/dev/null; then
                svc_reload=true
            else
                log "WARN: Failed to update $svc_name (no sudo?)"
            fi
        fi
    fi
done
if $svc_reload; then
    sudo systemctl daemon-reload 2>/dev/null && log "systemd daemon reloaded" || log "WARN: daemon-reload failed"
fi

# Migrate config — add any new default fields, preserve existing values
if command -v python3 > /dev/null 2>&1; then
    cp "$LOCAL_DIR/config.json" "$LOCAL_DIR/config.json.bak" 2>/dev/null || true
    python3 "$LOCAL_DIR/config_migrate.py" 2>&1 | while IFS= read -r line; do log "$line"; done || \
        log "WARN: config migration failed (non-fatal)"
fi

# Reconcile cron files — install any that are missing or outdated
if [ -f "$LOCAL_DIR/toggle_rovimen.sh" ]; then
    bash "$LOCAL_DIR/toggle_rovimen.sh" --ensure-crons 2>&1 | while IFS= read -r line; do log "$line"; done || log "WARN: cron reconciliation failed (non-fatal)"
fi

# Reconcile sudoers — keep NOPASSWD rules current as new services are added
_ensure_sudoers

# Restart running services
# Only restart services that are both active AND enabled — if a service was
# intentionally disabled (toggle_rovimen off, dashboard toggle off), skip it.
restarted=()
for svc in "${SERVICES[@]}"; do
    if ! systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        log "SKIP restart: $svc is disabled — not restarting"
        continue
    fi
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        if [[ "$_ffmpeg_active" == "true" && "$svc" == "color-capture" ]]; then
            log "SKIP restart: color-capture skipped while ffmpeg is capturing"
            continue
        fi
        if sudo systemctl restart "$svc" 2>/dev/null; then
            restarted+=("$svc")
            log "Restarted $svc"
        else
            log "WARN: Failed to restart $svc"
        fi
    fi
done

if [ ${#restarted[@]} -eq 0 ]; then
    log "No running services to restart."
else
    log "Done. Restarted: ${restarted[*]}"
    sleep 5
    failed=()
    for svc in "${restarted[@]}"; do
        if ! systemctl is-active --quiet "$svc" 2>/dev/null; then
            log "WARN: $svc is not active after restart"
            failed+=("$svc")
        fi
    done
    if [ ${#failed[@]} -gt 0 ]; then
        log "ERROR: ${#failed[@]} service(s) failed health check after update: ${failed[*]}"
    else
        log "Health check passed — all restarted services are active"
    fi
fi
