#!/bin/bash
# Triggered daily at 04:00 local. Waits for all RMS cameras to finish
# processing, uploads archives to GMN, extracts meteor clips, then reboots.
#
# Key difference from the naive pgrep approach: RMS with
# reboot_after_processing=false stays alive after processing (waiting for
# the next night). We detect processing completion by checking for the
# "Processed with RMS" marker in FTPdetectinfo files instead.

# Config — set CAMERAS to your station's camera codes (override via env).
STATION_HOME="${STATION_HOME:-$HOME}"
LOG="${STATION_HOME}/reboot_after_all_cameras.log"
CAMERAS="${CAMERAS:-XX0001 XX0002}"
RMS_BASE="${STATION_HOME}/RMS_data"

# Determine the night we're processing (yesterday's date)
NIGHT=$(date -d "yesterday" +%Y%m%d)

echo "$(date): Script triggered for night $NIGHT, waiting for processing..." >> $LOG

# Wait up to 2 hours for at least one capture dir to appear
WAITED=0
while true; do
    FOUND=0
    for cam in $CAMERAS; do
        if ls -d ${RMS_BASE}/${cam}/CapturedFiles/${cam}_${NIGHT}_* >/dev/null 2>&1; then
            FOUND=1
            break
        fi
    done
    if [ $FOUND -eq 1 ]; then
        break
    fi
    sleep 300
    WAITED=$((WAITED + 300))
    if [ $WAITED -ge 7200 ]; then
        echo "$(date): No capture dirs for $NIGHT after 2h, aborting" >> $LOG
        exit 0
    fi
done
echo "$(date): Capture dirs found for $NIGHT" >> $LOG

# Wait for ALL cameras to finish processing.
# A camera is "done" when its ArchivedFiles has a directory for this night
# containing an FTPdetectinfo with the "Processed with RMS" marker.
# Timeout: 10 hours (processing 5 cameras on Pi can take a while).
WAITED=0
while true; do
    ALL_DONE=true
    for cam in $CAMERAS; do
        ARCHIVE_DIR=$(ls -d ${RMS_BASE}/${cam}/ArchivedFiles/${cam}_${NIGHT}_* 2>/dev/null | head -1)
        if [ -z "$ARCHIVE_DIR" ]; then
            ALL_DONE=false
            break
        fi
        if ! grep -q "Processed with RMS" "$ARCHIVE_DIR"/FTPdetectinfo_*.txt 2>/dev/null; then
            ALL_DONE=false
            break
        fi
    done

    if $ALL_DONE; then
        break
    fi

    sleep 60
    WAITED=$((WAITED + 60))
    if [ $WAITED -ge 36000 ]; then
        echo "$(date): Not all cameras processed after 10h, aborting" >> $LOG
        exit 0
    fi
done
echo "$(date): All cameras processed for $NIGHT" >> $LOG

# Safety: only reboot between 05:00 and 14:00 local time
HOUR=$(date +%H)
if [ "$HOUR" -lt 5 ] || [ "$HOUR" -ge 14 ]; then
    echo "$(date): Outside reboot window (05:00-14:00), aborting" >> $LOG
    exit 0
fi

# Stop RMS services so they don't interfere with upload/extraction
echo "$(date): Stopping RMS services..." >> $LOG
for cam in $CAMERAS; do
    systemctl stop gmn-capture-${cam}.service 2>/dev/null
done
sleep 5
echo "$(date): RMS services stopped" >> $LOG

# Upload archives to GMN server
echo "$(date): Uploading archives to GMN..." >> $LOG
"${STATION_HOME}/vRMS/bin/python" "${STATION_HOME}/scripts/gmn_upload_archives.py" \
    --station-root "${STATION_HOME}/source/Stations" \
    --cameras "$(echo "$CAMERAS" | tr ' ' ',')" \
    --ssh-key "${STATION_HOME}/.ssh/id_rsa" \
    --log "${RMS_BASE}/upload_after_processing.log" \
    >> $LOG 2>&1 || echo "$(date): Upload had errors (see upload_after_processing.log)" >> $LOG
echo "$(date): Upload step done" >> $LOG

# Extract meteor clips before rebooting
echo "$(date): Extracting meteor clips..." >> $LOG
"${STATION_HOME}/vRMS/bin/python" "${STATION_HOME}/meteor_detector/extract_meteor_clips.py" \
    -c "${STATION_HOME}/meteor_detector/config.json" >> "${STATION_HOME}/meteor_clips/extract.log" 2>&1
echo "$(date): Clip extraction done" >> $LOG

echo "$(date): Rebooting" >> $LOG
/sbin/reboot
