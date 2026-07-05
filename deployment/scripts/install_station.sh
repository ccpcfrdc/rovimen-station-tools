#!/bin/bash
# ============================================================================
# GMN METEOR STATION - MASTER INSTALL SCRIPT
# ============================================================================
# Target Hardware: HP EliteDesk 800 G4 (or similar)
# Target OS: Ubuntu Server 24.04 LTS
#
# This script prepares a "Golden Master" image for cloning to multiple stations.
# Run this ONCE on the first station, then clone the drive to others.
#
# Usage: ./install_station.sh
# ============================================================================

set -e  # Exit on any error

# --- Configuration ---
# RAM disk size: 6G for 1-2 cameras, 8G for 4-6 cameras
RAM_DISK_SIZE="8G"
RAM_DISK_MOUNT="/mnt/ramdisk"
SWAP_SIZE="16G"
SWAP_FILE="/swapfile"
LOG_FILE="/tmp/install_station_$(date +%Y%m%d_%H%M%S).log"

# --- Colors for output ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# --- Helper Functions ---
log() {
    echo -e "${GREEN}[$(date '+%H:%M:%S')]${NC} $1" | tee -a "$LOG_FILE"
}

warn() {
    echo -e "${YELLOW}[$(date '+%H:%M:%S')] WARNING:${NC} $1" | tee -a "$LOG_FILE"
}

error() {
    echo -e "${RED}[$(date '+%H:%M:%S')] ERROR:${NC} $1" | tee -a "$LOG_FILE"
    exit 1
}

check_root() {
    if [[ $EUID -eq 0 ]]; then
        error "Do not run this script as root. Run as normal user (sudo will be used where needed)."
    fi
}

check_ubuntu() {
    if ! grep -q "Ubuntu" /etc/os-release 2>/dev/null; then
        warn "This script is designed for Ubuntu. Proceed with caution."
    fi
}

# --- Installation Steps ---

step_1_system_update() {
    log ">>> [1/11] Updating System & Installing Base Tools..."

    sudo apt update || error "apt update failed"
    sudo apt upgrade -y || error "apt upgrade failed"

    sudo apt install -y \
        git \
        curl \
        wget \
        htop \
        vim \
        chrony \
        python3-pip \
        python3-venv \
        python3-dev \
        ffmpeg \
        libatlas-base-dev \
        imagemagick \
        jp2a \
        cockpit \
        build-essential \
        libffi-dev \
        libssl-dev \
        watchdog \
        pkg-config \
        libcairo2-dev \
        libgirepository1.0-dev \
        gir1.2-gtk-3.0 \
        cmake \
        nmap \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
        gstreamer1.0-tools \
        || error "Failed to install base packages"

    log "Base packages installed successfully."
}

step_2_time_sync() {
    log ">>> [2/11] Configuring Chrony (Time Sync)..."

    # Force immediate sync
    sudo chronyc makestep || warn "chronyc makestep failed (may be normal on first run)"
    sudo systemctl enable --now chrony || error "Failed to enable chrony"

    # Verify time sync
    sleep 2
    if chronyc tracking | grep -q "Leap status.*Normal"; then
        log "Time sync configured and working."
    else
        warn "Time sync may need manual verification: run 'chronyc tracking'"
    fi
}

step_3_swap() {
    log ">>> [3/11] Setting up ${SWAP_SIZE} Swap File..."

    if [ -f "$SWAP_FILE" ]; then
        log "Swap file already exists."
    else
        sudo fallocate -l "$SWAP_SIZE" "$SWAP_FILE" || sudo dd if=/dev/zero of="$SWAP_FILE" bs=1G count=16 status=progress
        sudo chmod 600 "$SWAP_FILE"
        sudo mkswap "$SWAP_FILE"
        log "Swap file created."
    fi

    # Add to fstab if not present
    if ! grep -q "$SWAP_FILE" /etc/fstab; then
        echo "$SWAP_FILE none swap sw 0 0" | sudo tee -a /etc/fstab
        log "Added swap to /etc/fstab"
    fi

    sudo swapon -a 2>/dev/null || true
    log "Swap status:"
    swapon --show | tee -a "$LOG_FILE"
}

step_4_ramdisk() {
    log ">>> [4/11] Setting up ${RAM_DISK_SIZE} RAM Disk..."

    sudo mkdir -p "$RAM_DISK_MOUNT"

    if grep -q "$RAM_DISK_MOUNT" /etc/fstab; then
        log "RAM Disk already configured in fstab."
    else
        echo "tmpfs $RAM_DISK_MOUNT tmpfs nodev,nosuid,noatime,size=$RAM_DISK_SIZE 0 0" | sudo tee -a /etc/fstab
        log "Added RAM Disk entry to /etc/fstab"
    fi

    sudo mount -a || error "Failed to mount RAM disk"

    # Verify
    if mountpoint -q "$RAM_DISK_MOUNT"; then
        log "RAM Disk mounted successfully:"
        df -h "$RAM_DISK_MOUNT" | tee -a "$LOG_FILE"
    else
        error "RAM Disk mount verification failed"
    fi
}

step_5_watchdog() {
    log ">>> [5/11] Configuring Hardware Watchdog (Auto-Recovery)..."

    # Load Intel TCO watchdog module (for HP EliteDesk and similar Intel systems)
    sudo modprobe iTCO_wdt 2>/dev/null || warn "iTCO_wdt module not available (may use different watchdog)"

    # Make watchdog module load on boot
    echo "iTCO_wdt" | sudo tee /etc/modules-load.d/watchdog.conf > /dev/null

    # Verify watchdog device exists
    if [ ! -e /dev/watchdog ]; then
        warn "No /dev/watchdog device found. Hardware watchdog may not be supported."
    fi

    # Configure watchdog
    sudo tee /etc/watchdog.conf > /dev/null <<EOF
# Watchdog configuration for meteor station
watchdog-device = /dev/watchdog
watchdog-timeout = 60
interval = 30
max-load-1 = 24
min-memory = 1
EOF

    # Enable and start watchdog
    sudo systemctl enable watchdog || warn "Failed to enable watchdog service"
    sudo systemctl start watchdog || warn "Failed to start watchdog (may need reboot)"

    # Verify watchdog is running
    if systemctl is-active --quiet watchdog; then
        log "Watchdog configured and running - system will auto-reboot if hung for 60 seconds."
    else
        warn "Watchdog service not running. May need reboot to activate hardware watchdog."
    fi
}

step_6_tailscale() {
    log ">>> [6/11] Installing Tailscale..."

    if command -v tailscale &> /dev/null; then
        log "Tailscale already installed."
    else
        curl -fsSL https://tailscale.com/install.sh | sh || error "Tailscale installation failed"
        log "Tailscale installed. DO NOT run 'tailscale up' until after cloning!"
    fi

    # Ensure tailscaled service is enabled (will start on boot)
    sudo systemctl enable tailscaled || warn "Failed to enable tailscaled"
}

step_7_clone_rms() {
    log ">>> [7/11] Cloning RMS Repository..."

    cd ~

    if [ -d "RMS" ]; then
        log "RMS directory exists. Pulling latest changes..."
        cd RMS
        git pull || warn "git pull failed - check network connection"
        cd ~
    else
        git clone https://github.com/CroatianMeteorNetwork/RMS.git || error "Failed to clone RMS repository"
        log "RMS cloned successfully."
    fi
}

step_8_python_venv() {
    log ">>> [8/11] Setting up Python Virtual Environment..."
    log "This step may take 10-15 minutes (compiling numpy/scipy)..."

    cd ~/RMS

    # Clean slate
    if [ -d "venv" ]; then
        log "Removing existing venv..."
        rm -rf venv
    fi

    python3 -m venv venv || error "Failed to create virtual environment"
    source venv/bin/activate || error "Failed to activate virtual environment"

    pip install --upgrade pip wheel setuptools || error "Failed to upgrade pip"

    # Install requirements with progress
    pip install -r requirements.txt || error "Failed to install Python requirements"

    deactivate
    log "Python virtual environment setup complete."
}

step_9_rms_service() {
    log ">>> [9/11] Creating RMS Systemd Service..."

    # Get the actual username (not root)
    RMS_USER=$(whoami)
    RMS_HOME=$(eval echo ~$RMS_USER)

    sudo tee /etc/systemd/system/rms.service > /dev/null <<EOF
[Unit]
Description=RMS Meteor Detection
After=network.target

[Service]
Type=simple
User=$RMS_USER
WorkingDirectory=$RMS_HOME/RMS
ExecStart=$RMS_HOME/RMS/venv/bin/python -m RMS.StartCapture
Restart=always
RestartSec=30
# Ensure clean shutdown
ExecStop=/bin/kill -SIGTERM \$MAINPID
TimeoutStopSec=60
# Prevent duplicate processes
KillMode=control-group

# Environment
Environment="PATH=$RMS_HOME/RMS/venv/bin:/usr/local/bin:/usr/bin:/bin"
Environment="HOME=$RMS_HOME"

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    # Enable but don't start - needs camera config first
    sudo systemctl enable rms.service || warn "Failed to enable RMS service"

    # Setup log rotation for RMS logs
    RMS_USER=$(whoami)
    RMS_HOME=$(eval echo ~$RMS_USER)
    sudo tee /etc/logrotate.d/rms > /dev/null <<EOF
${RMS_HOME}/RMS_data/logs/*.log {
    weekly
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    create 644 ${RMS_USER} ${RMS_USER}
}
EOF

    log "RMS service created (disabled until camera configured)."
    log "To start: sudo systemctl start rms"
    log "To check: sudo systemctl status rms"
}

step_10_camera_network() {
    log ">>> [10/11] Setting up Camera Network (Ethernet + DHCP)..."

    # Configure ethernet interface for camera subnet
    ETHERNET_IF=$(ip link show | grep -E '^[0-9]+: (eno|enp|eth)' | head -1 | cut -d: -f2 | tr -d ' ')

    if [ -z "$ETHERNET_IF" ]; then
        warn "No ethernet interface found. Skipping camera network setup."
        return
    fi

    log "Found ethernet interface: $ETHERNET_IF"

    # Create netplan config for ethernet
    sudo tee /etc/netplan/01-ethernet.yaml > /dev/null <<EOF
network:
  version: 2
  ethernets:
    $ETHERNET_IF:
      addresses:
        - 10.0.0.100/24
      optional: true
EOF
    sudo chmod 600 /etc/netplan/01-ethernet.yaml
    sudo netplan apply || warn "Failed to apply netplan"

    # Install and configure dnsmasq as DHCP server for cameras
    sudo apt install -y dnsmasq > /dev/null 2>&1 || warn "Failed to install dnsmasq"

    sudo tee /etc/dnsmasq.d/camera.conf > /dev/null <<EOF
# DHCP server for POE cameras
interface=$ETHERNET_IF
bind-interfaces
dhcp-range=10.0.0.50,10.0.0.99,12h
EOF

    sudo systemctl enable dnsmasq || warn "Failed to enable dnsmasq"
    sudo systemctl restart dnsmasq || warn "Failed to start dnsmasq"

    log "Camera network configured:"
    log "  - Ethernet IP: 10.0.0.100/24"
    log "  - DHCP range: 10.0.0.50-99"
    log "  - Cameras will auto-get IPs when connected to POE switch"
}

step_11_cockpit() {
    log ">>> [11/11] Enabling Cockpit Web Dashboard..."

    sudo systemctl enable --now cockpit.socket || warn "Failed to enable Cockpit"

    LOCAL_IP=$(hostname -I | awk '{print $1}')
    log "Cockpit dashboard available at: https://${LOCAL_IP}:9090"
}

print_summary() {
    echo ""
    echo "============================================================================"
    echo -e "${GREEN}INSTALLATION COMPLETE!${NC}"
    echo "============================================================================"
    echo ""
    echo "Installation log saved to: $LOG_FILE"
    echo ""
    echo "VERIFICATION COMMANDS:"
    echo "  1. RAM Disk:      df -h /mnt/ramdisk"
    echo "  2. Swap:          swapon --show"
    echo "  3. Time Sync:     chronyc tracking"
    echo "  4. Watchdog:      sudo systemctl status watchdog"
    echo "  5. RMS Service:   sudo systemctl status rms"
    echo "  6. Camera DHCP:   cat /var/lib/misc/dnsmasq.leases"
    echo "  7. Python:        source ~/RMS/venv/bin/activate && python3 -c \"import cv2; print('OK')\""
    echo ""
    echo "AUTO-RECOVERY FEATURES:"
    echo "  - Watchdog: Reboots system if hung for 60 seconds"
    echo "  - Swap: 16GB safety net prevents OOM crashes"
    echo "  - RMS Service: Auto-restarts if crashed (30 sec delay)"
    echo "  - Services: chrony, tailscaled, cockpit, rms, dnsmasq start on boot"
    echo ""
    echo "CAMERA NETWORK:"
    echo "  - Ethernet IP: 10.0.0.100/24"
    echo "  - Camera DHCP range: 10.0.0.50-99"
    echo "  - Connect cameras to POE switch, they auto-get IPs"
    echo "  - View camera IPs: cat /var/lib/misc/dnsmasq.leases"
    echo ""
    echo "NEXT STEPS:"
    echo "  1. Verify all checks above pass"
    echo "  2. Run the neutralization script: ./neutralize_for_clone.sh"
    echo "  3. Shut down and clone drive with Clonezilla"
    echo "  4. On each cloned station:"
    echo "     - Run: ./post_clone_setup.sh"
    echo "     - Configure RMS camera settings"
    echo ""
    echo "============================================================================"
}

# --- Main Execution ---
main() {
    echo "============================================================================"
    echo "GMN METEOR STATION - MASTER INSTALL SCRIPT"
    echo "============================================================================"
    echo ""

    check_root
    check_ubuntu

    log "Starting installation... Log file: $LOG_FILE"
    log "Hardware: $(cat /sys/class/dmi/id/product_name 2>/dev/null || echo 'Unknown')"
    log "OS: $(lsb_release -ds 2>/dev/null || cat /etc/os-release | grep PRETTY_NAME | cut -d'"' -f2)"
    echo ""

    step_1_system_update
    step_2_time_sync
    step_3_swap
    step_4_ramdisk
    step_5_watchdog
    step_6_tailscale
    step_7_clone_rms
    step_8_python_venv
    step_9_rms_service
    step_10_camera_network
    step_11_cockpit

    print_summary
}

main "$@"
