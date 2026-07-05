#!/usr/bin/env bash
# reboot_guard.sh — Safe reboot gate for ROVIMEN stations.
#
# Waits until morning_done is True in state.json for all stations (or
# 4-hour timeout), then reboots.  Replaces a bare sudo /sbin/reboot cron entry.
#
# Cron entry (runs as station user, uses sudo for reboot):
#   0 12 * * * gmn /home/gmn/rovimen_scripts/reboot_guard.sh >> /var/log/rovimen_reboot.log 2>&1
#
# How it works:
#   1. Reads state.json for each configured station under videocapture_path.
#   2. Checks that morning_done == true.
#   3. Reboots once all stations are done, or after MAX_WAIT_S (default 4 h).

set -euo pipefail

CONFIG="${ROVIMEN_CONFIG:-$HOME/rovimen_scripts/config.json}"
MAX_WAIT_S=14400   # 4 hours
POLL_S=60

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [reboot-guard] $*"; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Return the night date for the most recently completed night:
#   before 12:00 UTC → use yesterday's date  (morning, last night's data)
#   after  12:00 UTC → use today's date       (night just starting)
night_date() {
    local hour
    hour=$(date -u +%H)
    if [ "$hour" -lt 12 ]; then
        date -u -d 'yesterday' '+%Y%m%d'
    else
        date -u '+%Y%m%d'
    fi
}

# Echo the videocapture_path from config.json.
get_capture_path() {
    python3 - "$CONFIG" <<'PYEOF'
import json, sys, pathlib
cfg = json.load(open(sys.argv[1]))
print(cfg.get('videocapture_path') or cfg.get('reenc_path')
      or str(pathlib.Path.home() / 'color_capture'))
PYEOF
}

# Echo space-separated list of station IDs from config.json.
get_stations() {
    python3 - "$CONFIG" <<'PYEOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
print(' '.join(cfg.get('stations', {}).keys()))
PYEOF
}

# Return 0 if morning_done is True for this station/date.
all_complete() {
    local state_file="$1"
    [ -f "$state_file" ] || return 1
    python3 - "$state_file" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
sys.exit(0 if data.get('morning_done', False) else 1)
PYEOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

log "Reboot guard starting  config=$CONFIG  max_wait=${MAX_WAIT_S}s  poll=${POLL_S}s"

NIGHT=$(night_date)
log "Night date: $NIGHT"

CAP_PATH=$(get_capture_path)
log "videocapture_path: $CAP_PATH"

STATIONS=$(get_stations)
log "Stations: $STATIONS"
[ -z "$STATIONS" ] && { log "No stations in config — skipping reboot"; exit 0; }

elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT_S" ]; do
    all_ok=true
    for station in $STATIONS; do
        state_file="$CAP_PATH/$station/$NIGHT/state.json"
        if ! all_complete "$state_file"; then
            log "  Waiting for $station/$NIGHT (state: $state_file)"
            all_ok=false
        else
            log "  OK: $station/$NIGHT"
        fi
    done

    if $all_ok; then
        log "All processes complete — rebooting"
        sudo /sbin/reboot
        exit 0
    fi

    sleep "$POLL_S"
    elapsed=$((elapsed + POLL_S))
done

log "Timeout after ${MAX_WAIT_S}s — rebooting anyway"
sudo /sbin/reboot
