# Clonezilla Drive Cloning Guide

## Overview

This guide covers cloning the "Golden Master" drive to create identical stations.

## Prerequisites

- [ ] Golden Master station fully configured and tested
- [ ] Neutralization script run (`./neutralize_for_clone.sh`)
- [ ] Master station powered OFF
- [ ] Clonezilla Live USB (create from https://clonezilla.org/downloads.php)
- [ ] Target drives (same size or larger than source)

## Option A: Clone Drive-to-Drive (Recommended)

Best when you have physical access to drives and a PC with multiple drive slots/docks.

### Step 1: Boot Clonezilla

1. Insert Clonezilla USB into any PC with BOTH drives connected:
   - Source: Master drive (golden image)
   - Target: Empty drive for new station
2. Boot from USB (F12 or F9 for boot menu on HP)
3. Select: `Clonezilla live (Default settings)`
4. Language: English
5. Keymap: Keep default
6. Select: `Start_Clonezilla`

### Step 2: Clone Settings

1. Select: `device-device` (disk/partition to disk/partition)
2. Select: `Beginner` mode
3. Select: `disk_to_local_disk` (local disk to local disk clone)
4. **SOURCE disk**: Select the master drive
   - Usually identified by size and model name
   - NVMe drives: `nvme0n1`
   - SATA drives: `sda`, `sdb`
5. **TARGET disk**: Select the empty drive
   - ⚠️ **VERIFY CAREFULLY** - target will be ERASED
6. Skip checking/repairing: `Skip` (saves time)
7. Action when finished: `Choose reboot/shutdown/etc`

### Step 3: Confirm and Clone

1. Review the clone operation summary
2. Type `y` to confirm (twice)
3. Wait for clone to complete (time depends on data size, typically 5-15 min)
4. Shutdown when complete

### Step 4: Install Cloned Drive

1. Install cloned drive into new station
2. Boot the station
3. Run post-clone setup: `./post_clone_setup.sh`

---

## Option B: Clone to Image File (For Multiple Clones)

Best when creating many clones or want to keep a backup image.

### Step 1: Create Image from Master

1. Boot Clonezilla on master station
2. Select: `device-image`
3. Select: `local_dev` (save to local drive/USB)
4. Connect a large USB drive for storing the image
5. Select the USB drive as destination
6. Select: `Beginner` mode
7. Select: `savedisk` (save local disk as image)
8. Name the image: `gmn_master_YYYYMMDD`
9. Select the master drive as source
10. Compression: `z1p` (parallel gzip - good balance)
11. Skip checking: `Yes`
12. Wait for image creation

### Step 2: Restore Image to Each Target

1. Boot Clonezilla on target station
2. Select: `device-image`
3. Select: `local_dev`
4. Connect the USB drive with the image
5. Select: `Beginner` mode
6. Select: `restoredisk` (restore image to disk)
7. Select the image: `gmn_master_YYYYMMDD`
8. Select target disk (internal drive of new station)
9. Confirm and restore
10. Shutdown, remove USB, boot station
11. Run: `./post_clone_setup.sh`

---

## Drive Identification Reference

### NVMe Drives (Most Likely for EliteDesk 800 G4)

```
/dev/nvme0n1     - First NVMe drive
/dev/nvme0n1p1   - First partition
/dev/nvme0n1p2   - Second partition
```

### SATA Drives

```
/dev/sda         - First SATA drive
/dev/sda1        - First partition
/dev/sdb         - Second SATA drive (USB or dock)
```

### Identify Drives in Clonezilla

Before selecting, note the size and model:
- Master drive size: _____ GB
- Master drive model: _____

---

## Troubleshooting

### "Target disk is smaller than source"
- Target must be equal or larger size
- Solution: Use `expert` mode with `-icds` option to skip size check (only if target is slightly smaller)

### Clone seems stuck
- Large drives take time - 500GB can take 15-30 minutes
- Check for blinking activity LED on drives

### Cloned station won't boot
- Check BIOS boot order
- Ensure UEFI/Legacy mode matches source
- Try regenerating GRUB: boot live USB, mount drive, run `grub-install`

### SSH shows "HOST IDENTIFICATION HAS CHANGED"
- Expected! The cloned station has new SSH keys
- Fix on your laptop: `ssh-keygen -R <station-ip>`

---

## Quick Reference Card

```
CLONE WORKFLOW:
1. Run neutralize_for_clone.sh on master → Shutdown
2. Boot Clonezilla USB
3. device-device → Beginner → disk_to_local_disk
4. Select SOURCE (master) → Select TARGET (new)
5. Confirm → Wait → Shutdown
6. Install drive in new station → Boot
7. Run post_clone_setup.sh → Enter station number
8. Update inventory
```
