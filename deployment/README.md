# GMN Multi-Station Deployment

Deployment system for Global Meteor Network (GMN/RMS) observation stations.

## Overview

This repository contains everything needed to:
1. Set up a "Golden Master" Ubuntu Server installation
2. Clone it to multiple identical stations
3. Configure each station with unique identity

## Hardware

- **Target**: HP EliteDesk 800 G4 (or similar)
- **RAM**: 16GB (6GB allocated to RAM disk)
- **Storage**: NVMe/SATA SSD
- **OS**: Ubuntu Server 24.04 LTS

## Quick Start

### Phase 1: Create Golden Master

```bash
# On the first station (192.168.0.197)
ssh gmn@192.168.0.197

# Download and run install script
# (or copy from this repo)
chmod +x install_station.sh
./install_station.sh

# Verify installation
df -h /mnt/ramdisk          # RAM disk
chronyc tracking            # Time sync
source ~/RMS/venv/bin/activate && python3 -c "import cv2; print('OK')"
```

### Phase 2: Neutralize for Cloning

```bash
# ONLY after verifying everything works
./neutralize_for_clone.sh
# System will shut down automatically
```

### Phase 3: Clone Drive

See [CLONEZILLA_GUIDE.md](docs/CLONEZILLA_GUIDE.md)

### Phase 4: Configure Each Clone

```bash
# On each cloned station after first boot
./post_clone_setup.sh
# Enter station number: 001, 002, 003, etc.
```

## Directory Structure

```
multi-station-deployment/
├── README.md                    # This file
├── scripts/
│   ├── install_station.sh       # Master installation script
│   ├── neutralize_for_clone.sh  # Pre-clone identity wipe
│   └── post_clone_setup.sh      # Per-station setup after clone
├── docs/
│   ├── CLONEZILLA_GUIDE.md      # Drive cloning instructions
│   └── POST_CLONE_CHECKLIST.md  # Verification checklist
└── inventory/
    └── stations.md              # Station registry and IPs
```

## Station Naming Convention

| Station | Hostname     | Purpose |
|---------|--------------|---------|
| 001     | gmn_ro_001   | Primary station (golden master) |
| 002     | gmn_ro_002   | Clone |
| 003     | gmn_ro_003   | Clone |
| ...     | gmn_ro_XXX   | Expandable |

## Network Services

| Service    | Port  | Purpose |
|------------|-------|---------|
| SSH        | 22    | Remote access |
| Cockpit    | 9090  | Web dashboard (HTTPS) |
| Tailscale  | -     | VPN mesh network |

## Key Paths

| Path | Purpose |
|------|---------|
| `/mnt/ramdisk` | 6GB RAM disk for capture (saves SSD) |
| `~/RMS` | RMS software installation |
| `~/RMS/venv` | Python virtual environment |
| `~/RMS/.config` | Station-specific camera config |

## Maintenance Commands

```bash
# Update RMS on a station
cd ~/RMS && git pull
source venv/bin/activate
pip install -r requirements.txt

# Check time sync
chronyc tracking

# Check Tailscale status
tailscale status

# Monitor RAM disk usage
watch df -h /mnt/ramdisk
```

## Expanding to More Stations

1. Clone the golden master drive (Clonezilla)
2. Install in new hardware
3. Boot and run `./post_clone_setup.sh`
4. Enter next station number (004, 005, etc.)
5. Update [inventory/stations.md](inventory/stations.md)

## Troubleshooting

### Station not reachable via SSH
```bash
# Check if online
ping <station-ip>

# If SSH key changed warning
ssh-keygen -R <station-ip>
```

### Time sync issues
```bash
sudo chronyc makestep    # Force immediate sync
chronyc sources -v       # Check time sources
```

### RMS Python issues
```bash
cd ~/RMS
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
