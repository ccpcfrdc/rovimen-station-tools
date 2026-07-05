# Live Meteor Detection System

Real-time meteor detection for RMS with color video clip capture.

## Overview

This system monitors RMS for new FF files, runs meteor detection immediately (instead of waiting until morning), and saves color video clips when meteors are detected.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    RMS (unmodified)                         │
│  - Captures frames from camera                              │
│  - Creates FF files every ~10 seconds                       │
└──────────────────────┬──────────────────────────────────────┘
                       │ FF file created (inotify)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              Live Meteor Detector                           │
│  ┌─────────────────┐  ┌─────────────────┐                   │
│  │  FF File        │  │  Color Buffer   │                   │
│  │  Watcher        │  │  Manager        │                   │
│  │  (inotify)      │  │  (RAM disk)     │                   │
│  └────────┬────────┘  └────────┬────────┘                   │
│           │                    │                            │
│           ▼                    │                            │
│  ┌─────────────────┐           │                            │
│  │  Detection      │           │                            │
│  │  Runner         │───────────┤                            │
│  │  (RMS algos)    │           │                            │
│  └────────┬────────┘           │                            │
│           │ Meteor found       │                            │
│           ▼                    ▼                            │
│  ┌─────────────────────────────────────┐                    │
│  │  Clip Extractor                     │                    │
│  │  - Extracts T-5s to T+15s           │                    │
│  │  - Saves to disk with metadata      │                    │
│  └─────────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────┘
```

## Features

- **Near real-time detection**: ~15-20 second delay from meteor to alert
- **Color video clips**: 20-second clips (5s before, 15s after detection)
- **Full meteor characteristics**:
  - Detection time (UTC)
  - Duration (seconds)
  - Angular velocity (°/s)
  - Peak magnitude
  - Sky position (RA/Dec, Az/El)
  - Probable shower association
- **RAM disk buffering**: Zero disk I/O during capture
- **JSON metadata**: Machine-readable detection data
- **Systemd service**: Runs as background daemon

## Requirements

### System
- Linux (Ubuntu/Debian recommended)
- RAM disk at `/mnt/ramdisk` (8GB recommended)
- ffmpeg installed

### Python
```bash
pip install inotify numpy
```

### RMS
- Working RMS installation with calibration

## Installation

1. **Copy files to RMS station**:
```bash
scp -r live_meteor_detector/ gmn@gmnro001:~/
```

2. **Install Python dependencies**:
```bash
ssh gmn@gmnro001
pip install --user inotify
```

3. **Create output directory**:
```bash
mkdir -p ~/meteor_clips
```

4. **Edit configuration**:
```bash
nano ~/live_meteor_detector/config.json
```

5. **Test**:
```bash
python3 ~/live_meteor_detector/live_detector.py --test
```

## Usage

### Manual Start
```bash
python3 ~/live_meteor_detector/live_detector.py -c ~/live_meteor_detector/config.json
```

### As Systemd Service
```bash
# Install service
sudo cp live_detector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable live_detector
sudo systemctl start live_detector

# Check status
sudo systemctl status live_detector

# View logs
journalctl -u live_detector -f
```

## Configuration

Edit `config.json`:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `rms_data_path` | Path to RMS_data directory | `~/RMS_data` |
| `rms_source_path` | Path to RMS source | `~/RMS` |
| `ramdisk_path` | RAM disk for buffers | `/mnt/ramdisk` |
| `output_path` | Where to save clips | `~/meteor_clips` |
| `cameras` | Dictionary of camera RTSPs | 4 cameras |
| `buffer_duration_seconds` | Rolling buffer size | 30 |
| `pre_detection_seconds` | Seconds before detection | 5 |
| `post_detection_seconds` | Seconds after detection | 15 |

## Output

### Detection JSON
```json
{
  "timestamp": "2026-01-18T03:45:12.123456",
  "ff_file": "/home/gmn/RMS_data/CapturedFiles/.../FF_xxx.fits",
  "camera_id": "cam1",
  "duration": 0.82,
  "angular_velocity": 15.3,
  "peak_magnitude": 1.2,
  "ra": 45.67,
  "dec": 58.12,
  "probable_shower": "Perseid",
  "clip_path": "/home/gmn/meteor_clips/meteor_cam1_20260118_034512.mp4"
}
```

### Video Clip
- Format: MP4 (H.264/HEVC passthrough)
- Duration: 20 seconds (configurable)
- Filename: `meteor_{camera}_{timestamp}.mp4`

## Troubleshooting

### No detections
- Check RMS is creating FF files: `ls ~/RMS_data/CapturedFiles/*/FF_*.fits`
- Verify calibration exists: `ls ~/RMS_data/platepar*.cal`
- Check logs: `tail -f ~/meteor_clips/live_detector.log`

### Buffer not working
- Verify RAM disk: `df -h /mnt/ramdisk`
- Check ffmpeg is installed: `which ffmpeg`
- Verify RTSP URLs work: `ffprobe rtsp://...`

### inotify errors
- Install inotify: `pip install inotify`
- Increase inotify watches: `echo 65536 | sudo tee /proc/sys/fs/inotify/max_user_watches`

## Data Flow Timeline

```
T-30s: Color buffer starts recording
T-10s: FF file capture begins (256 frames)
T=0:   FF file written to disk
T+2s:  inotify triggers detection
T+5s:  Detection complete, meteor found
T+8s:  Clip extracted from buffer (T-5s to T+15s)
T+10s: JSON metadata saved
T+10s: Alert/notification sent
```

Total latency from meteor to alert: **~15-20 seconds**

## License

MIT License - Part of GMN_RMS project
