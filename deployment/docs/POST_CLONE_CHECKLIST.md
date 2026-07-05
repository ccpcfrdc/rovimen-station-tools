# Post-Clone Checklist

Run through this checklist for **each cloned station** after first boot.

## Station: gmn_ro_0__

### Pre-Flight Checks

- [ ] Station booted successfully
- [ ] Network cable connected
- [ ] Can ping gateway: `ping 192.168.0.1`

### Identity Setup

- [ ] Run post-clone script:
  ```bash
  cd ~/multi-station-deployment/scripts  # or wherever scripts are located
  ./post_clone_setup.sh
  ```
- [ ] Enter station number when prompted (001, 002, 003, etc.)
- [ ] Tailscale authenticated via web link
- [ ] Hostname verified: `hostname` shows `gmn_ro_0XX`

### Verification Tests

- [ ] **SSH Keys Regenerated**:
  ```bash
  ls -la /etc/ssh/ssh_host_*
  # Should show files with TODAY's date
  ```

- [ ] **Machine ID Unique**:
  ```bash
  cat /etc/machine-id
  # Should be different on each station
  ```

- [ ] **RAM Disk Mounted**:
  ```bash
  df -h /mnt/ramdisk
  # Should show 6.0G size
  ```

- [ ] **Time Sync Working**:
  ```bash
  chronyc tracking
  # "System time" offset should be < 0.001 seconds
  ```

- [ ] **Python Environment**:
  ```bash
  source ~/RMS/venv/bin/activate
  python3 -c "import cv2; print('OpenCV OK')"
  deactivate
  ```

- [ ] **Tailscale Connected**:
  ```bash
  tailscale status
  # Should show this station and others
  ```

- [ ] **Cockpit Dashboard**:
  - Open browser to `https://<station-ip>:9090`
  - Login with gmn credentials

### RMS Configuration

- [ ] Camera connected and detected: `ls /dev/video*`
- [ ] Station config updated in `~/RMS/.config/`
- [ ] GPS coordinates set for this station's location
- [ ] Test capture successful

### Final Steps

- [ ] Update inventory spreadsheet with:
  - Local IP address
  - Tailscale IP address
  - MAC address (`ip link show`)
- [ ] Verify station visible from laptop via Tailscale
- [ ] Label physical machine with station ID

---

**Completed by**: _______________
**Date**: _______________
**Station ID**: gmn_ro_0__
