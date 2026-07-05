# Camera and POE Switch Network Setup

## Default Credentials

| Device | Username | Password | Notes |
|--------|----------|----------|-------|
| Camera (XMeye/NetSurveillance) | admin | *(empty)* | Default factory password is blank |
| POE Switch | admin | admin | Check manufacturer documentation |
| EliteDesk SSH | gmn | *(ssh key)* | Password auth disabled, use SSH key |

**IMPORTANT**: Change default passwords on production systems!

---

## Network Topology

```
                                    ┌─────────────────┐
                                    │   Main Router   │
                                    │  192.168.1.1    │
                                    └────────┬────────┘
                                             │
                         ┌───────────────────┼───────────────────┐
                         │                   │                   │
                         │ WiFi              │ LAN               │
                         ▼                   ▼                   ▼
                 ┌───────────────┐   ┌───────────────┐   Other devices
                 │   EliteDesk   │   │  POE Switch   │
                 │ (gmnro001)    │   │  10.0.0.10    │
                 │               │   │               │
                 │ WiFi:         │   └───────┬───────┘
                 │ 192.168.1.197 │           │
                 │               │           │ POE
                 │ Ethernet:     │◄──────────┤
                 │ 10.0.0.100    │           │
                 └───────────────┘           ▼
                                     ┌───────────────┐
                                     │   Camera 1    │
                                     │  10.0.0.80    │
                                     └───────────────┘
```

## IP Addressing Scheme

| Device | IP Address | Subnet | Notes |
|--------|------------|--------|-------|
| EliteDesk (WiFi) | 192.168.1.197 | 192.168.1.0/24 | For internet/Tailscale |
| EliteDesk (Ethernet) | 10.0.0.100 | 10.0.0.0/24 | Camera network |
| POE Switch | 10.0.0.10 | 10.0.0.0/24 | Switch admin interface |
| Camera 1 | 10.0.0.80 | 10.0.0.0/24 | DHCP assigned |
| Camera 2 | 10.0.0.81 | 10.0.0.0/24 | DHCP assigned |
| Camera 3 | 10.0.0.82 | 10.0.0.0/24 | DHCP assigned |
| ... | 10.0.0.xx | 10.0.0.0/24 | DHCP range: 50-99 |

## EliteDesk Network Configuration

### Ethernet Config (Netplan)

File: `/etc/netplan/01-ethernet.yaml`

```yaml
network:
  version: 2
  ethernets:
    eno1:
      addresses:
        - 10.0.0.100/24
      optional: true
```

Apply changes:
```bash
sudo netplan apply
```

### DHCP Server (dnsmasq)

The EliteDesk runs a DHCP server to assign IPs to cameras.

File: `/etc/dnsmasq.d/camera.conf`

```conf
interface=eno1
bind-interfaces
dhcp-range=10.0.0.50,10.0.0.99,12h
```

**Commands:**
```bash
# Check status
sudo systemctl status dnsmasq

# View leases (see which cameras got IPs)
cat /var/lib/misc/dnsmasq.leases

# Restart after config changes
sudo systemctl restart dnsmasq
```

**DHCP Leases** are persistent by MAC address - cameras will get the same IP on every boot.

---

## Adding a New Camera

### Step 1: Connect Physically

1. Connect camera to POE switch via ethernet
2. Wait 30-60 seconds for camera to boot and request DHCP

### Step 2: Find Camera IP

```bash
# Check DHCP leases
cat /var/lib/misc/dnsmasq.leases

# Example output:
# 1767520437 00:12:43:3b:b1:9f 10.0.0.80 LocalHost 01:00:12:43:3b:b1:9f
#            ^^ MAC address    ^^ IP     ^^ hostname
```

Or scan the network:
```bash
# ARP table
ip neigh show dev eno1

# Ping sweep
for ip in 10.0.0.{50..99}; do ping -c1 -W1 $ip &>/dev/null && echo "Found: $ip"; done
```

### Step 3: Test Camera Connection

```bash
# Check common camera ports
for port in 80 554 34567; do
    timeout 1 bash -c "echo > /dev/tcp/10.0.0.XX/$port" 2>/dev/null && echo "Port $port: OPEN"
done

# Test RTSP stream
ffprobe -v quiet rtsp://10.0.0.XX:554/user=admin\&password=\&channel=1\&stream=0.sdp
```

### Step 4: Configure Camera via RMS

```bash
cd ~/RMS
source venv/bin/activate

# Disable OSD (on-screen text overlay)
python -m Utils.CameraControl SetOSD off

# Set camera time from system
python -m Utils.CameraControl CameraTime set

# View all camera settings
python -m Utils.CameraControl GetSettings
```

### Step 5: Update RMS Config

Edit `~/RMS/.config`:

```ini
[Capture]
device: rtsp://10.0.0.XX:554/user=admin&password=&channel=1&stream=0.sdp
camera_ip: 10.0.0.XX
width: 1920
height: 1080
gst_decoder: avdec_h265
```

---

## Camera Control Commands

All commands run from `~/RMS` with venv activated:

```bash
cd ~/RMS && source venv/bin/activate
```

### OSD (On-Screen Display)

```bash
# Disable timestamp/channel text overlay
python -m Utils.CameraControl SetOSD off

# Enable (if needed)
python -m Utils.CameraControl SetOSD on
```

### Time Sync

```bash
# Set camera time from system clock
python -m Utils.CameraControl CameraTime set
```

### Camera Parameters

```bash
# View all settings
python -m Utils.CameraControl GetSettings

# View specific settings
python -m Utils.CameraControl GetCameraParams
python -m Utils.CameraControl GetEncodeParams
python -m Utils.CameraControl GetNetConfig

# Set a parameter
python -m Utils.CameraControl SetParam Camera ElecLevel 60
python -m Utils.CameraControl SetParam Encode Video Resolution 1080P
```

### Reboot Camera

```bash
python -m Utils.CameraControl reboot
```

### Color Settings

```bash
# SetColor brightness,contrast,saturation,hue,gain,whitebalance
python -m Utils.CameraControl SetColor 100,50,50,50,0,0
```

---

## POE Switch Configuration

### Access Switch Admin

From EliteDesk:
```bash
# Switch is at 10.0.0.10
curl http://10.0.0.10  # or use browser via SSH tunnel
```

SSH tunnel for browser access:
```bash
# Run on your workstation
ssh -L 8888:10.0.0.10:80 gmn@<elitedesk-ip>
# Then open http://localhost:8888
```

### Recommended Switch Settings

| Setting | Value | Reason |
|---------|-------|--------|
| DHCP | Disabled | EliteDesk provides DHCP |
| IP Mode | Static | Predictable addressing |
| Switch IP | 10.0.0.10 | Within camera subnet |
| Subnet Mask | 255.255.255.0 | /24 network |
| Gateway | 10.0.0.100 | EliteDesk |

---

## RTSP URL Formats

Different cameras use different RTSP URL formats:

### NetSurveillance / XMeye Cameras (Most Common)

```
rtsp://IP:554/user=admin&password=&channel=1&stream=0.sdp     # Main stream
rtsp://IP:554/user=admin&password=&channel=1&stream=1.sdp     # Sub stream
```

### Hikvision

```
rtsp://admin:password@IP:554/Streaming/Channels/101   # Main
rtsp://admin:password@IP:554/Streaming/Channels/102   # Sub
```

### Dahua

```
rtsp://admin:password@IP:554/cam/realmonitor?channel=1&subtype=0   # Main
rtsp://admin:password@IP:554/cam/realmonitor?channel=1&subtype=1   # Sub
```

### Generic

```
rtsp://IP:554/stream1
rtsp://IP:554/h264/ch1/main/av_stream
rtsp://IP:554/live/ch00_0
```

### Test RTSP URL

```bash
ffprobe -v quiet -print_format json -show_streams "rtsp://10.0.0.80:554/..."
```

---

## Viewing Live Camera Feed

### From EliteDesk (SSH)

```bash
# Capture single frame
ffmpeg -i "rtsp://10.0.0.80:554/user=admin&password=&channel=1&stream=0.sdp" \
    -vframes 1 -q:v 2 /tmp/snapshot.jpg

# View with ffplay (requires X11)
ffplay "rtsp://10.0.0.80:554/user=admin&password=&channel=1&stream=0.sdp"
```

### From Workstation (via Tunnel)

```bash
# Create RTSP tunnel
ssh -f -N -L 8554:10.0.0.80:554 gmn@<elitedesk-ip>

# View with VLC
vlc rtsp://localhost:8554/user=admin\&password=\&channel=1\&stream=0.sdp

# View with ffplay
ffplay rtsp://localhost:8554/user=admin\&password=\&channel=1\&stream=0.sdp
```

---

## Troubleshooting

### Camera Not Getting IP

```bash
# Check dnsmasq is running
sudo systemctl status dnsmasq

# Check ethernet interface is up
ip addr show eno1

# Monitor DHCP requests
sudo tcpdump -i eno1 port 67 or port 68
```

### Camera Not Responding

```bash
# Check if reachable
ping 10.0.0.XX

# Check ARP table
ip neigh show dev eno1

# Scan for devices
sudo nmap -sn -e eno1 10.0.0.0/24
```

### RTSP Stream Not Working

```bash
# Test with ffprobe
ffprobe -v error "rtsp://10.0.0.XX:554/..."

# Check port is open
nc -zv 10.0.0.XX 554

# Try with TCP transport
ffplay -rtsp_transport tcp "rtsp://10.0.0.XX:554/..."
```

### Wrong Video Codec

If RMS complains about codec, check camera encoding:

```bash
python -m Utils.CameraControl GetEncodeParams
```

Update `gst_decoder` in `.config`:
- H.264: `avdec_h264`
- H.265/HEVC: `avdec_h265`

---

## Multi-Camera Setup

For multiple cameras on one station:

1. Each camera needs unique IP (handled by DHCP)
2. Each camera needs separate RMS config section (future feature) or separate RMS instance
3. Update camera_settings.json for each camera

### Camera Registry

| Camera | MAC Address | IP | RTSP Port | Location |
|--------|-------------|-----|-----------|----------|
| CAM01 | 00:12:43:3b:b1:9f | 10.0.0.80 | 554 | TBD |
| CAM02 | TBD | 10.0.0.81 | 554 | TBD |
| CAM03 | TBD | 10.0.0.82 | 554 | TBD |

---

## Quick Reference

```bash
# View camera IPs
cat /var/lib/misc/dnsmasq.leases

# Disable OSD
cd ~/RMS && source venv/bin/activate
python -m Utils.CameraControl SetOSD off

# Test camera stream
ffprobe rtsp://10.0.0.80:554/user=admin\&password=\&channel=1\&stream=0.sdp

# Create viewing tunnel (run on workstation)
ssh -L 8554:10.0.0.80:554 gmn@192.168.1.197
vlc rtsp://localhost:8554/user=admin\&password=\&channel=1\&stream=0.sdp
```
