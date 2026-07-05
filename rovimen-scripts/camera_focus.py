#!/usr/bin/env python3
"""Camera Focus Web UI — MJPEG streaming server for adjusting camera focus.

Zero external dependencies: Python 3 stdlib + ffmpeg only.
Run on any GMN station, access from phone via Tailscale.

Usage:
    python3 camera_focus.py [--port 8080] [--subnet 192.168.1] [--config ~/rovimen_scripts/config.json]
"""

import argparse
import json
import os
import re
import signal
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

SUBNET = "192.168.1"

_PALETTE = [
    "#FF9800", "#2196F3", "#4CAF50", "#E91E63",
    "#9C27B0", "#00BCD4", "#FF5722", "#8BC34A",
]

_DEFAULT_CONFIG = Path.home() / "rovimen_scripts" / "config.json"


def load_cameras(config_path: Path) -> dict:
    """Load camera entries from config.json.

    Returns dict: cam_id -> {"ip": str, "color": str}
    Falls back to empty dict if config is missing or has no stations.
    """
    try:
        cfg = json.loads(config_path.read_text())
    except Exception as e:
        print(f"[config] Could not load {config_path}: {e}")
        return {}

    cameras = {}
    for i, (cam_id, station) in enumerate(cfg.get("stations", {}).items()):
        rtsp = station.get("camera_rtsp", "")
        m = re.search(r"rtsp://[^@]*@([\d.]+):", rtsp)
        if not m:
            print(f"[config] No IP found in camera_rtsp for {cam_id}, skipping")
            continue
        cameras[cam_id] = {
            "ip": m.group(1),
            "color": _PALETTE[i % len(_PALETTE)],
        }

    print(f"[config] Loaded {len(cameras)} camera(s): {', '.join(cameras)}")
    return cameras


class SingleStream:
    """Manages one ffmpeg process for a single camera IP.

    A reader thread parses JPEG frames and broadcasts them to all connected
    HTTP clients via a Condition variable.
    """

    def __init__(self, ip, fps=10):
        self._ip = ip
        self._fps = fps
        self._lock = threading.Lock()
        self._proc = None
        self._reader_thread = None
        self._frame = None          # latest JPEG bytes
        self._frame_id = 0          # monotonic frame counter
        self._frame_cond = threading.Condition()  # notifies waiting clients
        self._clients = 0
        self._running = False

    @property
    def ip(self):
        return self._ip

    @property
    def clients(self):
        return self._clients

    @property
    def running(self):
        return self._running

    def start(self):
        """Start the ffmpeg process."""
        with self._lock:
            if self._running:
                return
            self._start()

    def subscribe(self):
        """Subscribe to frames. Returns a generator yielding JPEG bytes."""
        with self._lock:
            if not self._running:
                self._start()
            self._clients += 1

        last_id = 0
        try:
            while self._running:
                with self._frame_cond:
                    self._frame_cond.wait(timeout=2.0)
                    if self._frame is None or self._frame_id == last_id:
                        if not self._running:
                            break
                        continue
                    frame = self._frame
                    last_id = self._frame_id
                yield frame
        finally:
            with self._lock:
                self._clients -= 1

    def stop(self):
        """Stop the ffmpeg process."""
        with self._lock:
            self._stop()

    def _start(self):
        """Start ffmpeg. Caller must hold self._lock."""
        self._stop()
        rtsp_url = (
            f"rtsp://admin:@{self._ip}:554/"
            "user=admin&password=&channel=1&stream=0.sdp"
        )
        cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-i", rtsp_url,
            "-f", "image2pipe",
            "-c:v", "mjpeg",
            "-q:v", "5",
            "-r", str(self._fps),
            "pipe:1",
        ]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._running = True
        self._frame = None
        self._frame_id = 0
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True
        )
        self._reader_thread.start()
        print(f"[stream] started ffmpeg for {self._ip} @ {self._fps}fps")

    def _stop(self):
        """Stop ffmpeg. Caller must hold self._lock."""
        self._running = False
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=3)
            except Exception:
                pass
            self._proc = None
        with self._frame_cond:
            self._frame_cond.notify_all()
        print(f"[stream] stopped ffmpeg for {self._ip}")

    def _reader_loop(self):
        """Background thread: read ffmpeg stdout, parse JPEGs, broadcast."""
        proc = self._proc
        buf = bytearray()
        while self._running:
            try:
                chunk = proc.stdout.read(4096)
            except Exception:
                break
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                soi = buf.find(b'\xff\xd8')
                if soi == -1:
                    buf.clear()
                    break
                if soi > 0:
                    del buf[:soi]
                eoi = buf.find(b'\xff\xd9', 2)
                if eoi == -1:
                    break
                frame = bytes(buf[:eoi + 2])
                del buf[:eoi + 2]
                with self._frame_cond:
                    self._frame = frame
                    self._frame_id += 1
                    self._frame_cond.notify_all()
        self._running = False
        with self._frame_cond:
            self._frame_cond.notify_all()


class StreamPool:
    """Pool of SingleStream instances, one per camera IP.

    Creates streams on demand, auto-stops idle streams (0 clients) after
    a timeout. Enforces a max concurrent stream limit.
    """

    def __init__(self, max_streams=6, idle_timeout=10):
        self._streams = {}          # ip -> SingleStream
        self._lock = threading.Lock()
        self._max_streams = max_streams
        self._idle_timeout = idle_timeout
        self._idle_timers = {}      # ip -> time when clients hit 0
        # Cleanup thread
        self._cleanup_running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True
        )
        self._cleanup_thread.start()

    def subscribe(self, ip, fps=10):
        """Get-or-create a stream for the IP and subscribe to its frames."""
        with self._lock:
            # Cancel any pending idle timer for this IP
            self._idle_timers.pop(ip, None)

            stream = self._streams.get(ip)
            if stream is None or not stream.running:
                # Check capacity
                active = sum(1 for s in self._streams.values() if s.running)
                if active >= self._max_streams:
                    # Try to evict an idle stream
                    evicted = False
                    for eip, es in list(self._streams.items()):
                        if es.clients == 0:
                            es.stop()
                            del self._streams[eip]
                            self._idle_timers.pop(eip, None)
                            evicted = True
                            break
                    if not evicted:
                        raise RuntimeError(
                            f"Max {self._max_streams} concurrent streams reached"
                        )
                stream = SingleStream(ip, fps=fps)
                self._streams[ip] = stream

        return stream.subscribe()

    def stop_all(self):
        """Stop all active streams."""
        with self._lock:
            for stream in self._streams.values():
                stream.stop()
            self._streams.clear()
            self._idle_timers.clear()
        print("[pool] stopped all streams")

    def stop_one(self, ip):
        """Stop a specific stream by IP."""
        with self._lock:
            stream = self._streams.pop(ip, None)
            self._idle_timers.pop(ip, None)
        if stream:
            stream.stop()

    def _cleanup_loop(self):
        """Periodically check for idle streams and stop them."""
        while self._cleanup_running:
            time.sleep(2)
            now = time.monotonic()
            to_stop = []
            with self._lock:
                for ip, stream in list(self._streams.items()):
                    if stream.clients == 0:
                        if ip not in self._idle_timers:
                            self._idle_timers[ip] = now
                        elif now - self._idle_timers[ip] > self._idle_timeout:
                            to_stop.append(ip)
                    else:
                        # Has clients — clear any idle timer
                        self._idle_timers.pop(ip, None)
                for ip in to_stop:
                    stream = self._streams.pop(ip, None)
                    self._idle_timers.pop(ip, None)
                    if stream:
                        stream.stop()
                        print(f"[pool] auto-stopped idle stream {ip}")

    def shutdown(self):
        """Shut down the pool and all streams."""
        self._cleanup_running = False
        self.stop_all()


# Global stream pool — shared by all HTTP handlers
stream_pool = StreamPool(max_streams=6)


def grab_snapshot(ip, timeout=8):
    """Grab a single JPEG frame from the camera."""
    rtsp_url = (
        f"rtsp://admin:@{ip}:554/"
        "user=admin&password=&channel=1&stream=0.sdp"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-timeout", "5000000",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-f", "image2pipe",
        "-c:v", "mjpeg",
        "-q:v", "3",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except subprocess.TimeoutExpired:
        pass
    return None


_DVRIP_VENV = Path.home() / "RMS/venv/lib/python3.12/site-packages"


def _dvrip_set_osd(ip: str, show: bool) -> None:
    """Enable or disable camera OSD (channel title + timestamp) via DVRIP.

    Best-effort — all exceptions are swallowed so focus streaming is unaffected.
    Uses the RMS venv dvrip.py if the system dvrip package lacks DVRIPCam.
    """
    import sys
    venv_path = str(_DVRIP_VENV)
    inserted = False
    if venv_path not in sys.path:
        sys.path.insert(0, venv_path)
        inserted = True
    try:
        from dvrip import DVRIPCam  # type: ignore[import]
        cam = DVRIPCam(ip)
        cam.login()
        osd = cam.get_info("AVEnc.VideoWidget")
        if osd is None:
            print(f"[osd] {ip}: get_info returned None, skipping")
            return
        for ch in osd.get("AVEnc.VideoWidget", []):
            if "ChannelTitle" in ch:
                ch["ChannelTitle"]["Show"] = show
            if "TimeTitle" in ch:
                ch["TimeTitle"]["Show"] = show
        cam.set_info("AVEnc.VideoWidget", osd)
        cam.close()
        state = "on" if show else "off"
        print(f"[osd] {ip}: OSD {state}")
    except Exception as e:
        print(f"[osd] {ip}: {e}")
    finally:
        if inserted:
            sys.path.remove(venv_path)


def _get_iface(subnet: str) -> str:
    """Return the local interface that routes to the given subnet, or 'eno1' as fallback."""
    try:
        result = subprocess.run(
            ["ip", "route", "get", f"{subnet}.1"],
            capture_output=True, text=True, timeout=3,
        )
        for token in result.stdout.split():
            if token == "dev":
                idx = result.stdout.split().index("dev")
                return result.stdout.split()[idx + 1]
    except Exception:
        pass
    return "eno1"


def scan_cameras(subnet, cameras: dict):
    """THOROUGH camera scan: entire subnet with RTSP verification.
    Returns dict of ip -> online bool."""
    print(f"[scan] Starting thorough camera scan on {subnet}.0/24...")
    results = {}
    iface = _get_iface(subnet)
    print(f"[scan] Using interface: {iface}")

    # Step 1: ARP scan entire subnet to find ALL devices
    discovered_ips = set()
    try:
        cmd = ["sudo", "arp-scan", "-I", iface, f"{subnet}.0/24"]
        result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and parts[0].startswith(subnet):
                discovered_ips.add(parts[0])
                print(f"[scan] ARP found: {parts[0]}")
    except Exception as e:
        print(f"[scan] ARP scan failed: {e}")

    # Step 2: Full nmap port scan on entire subnet (RTSP + XMEye + HTTP)
    try:
        cmd = [
            "sudo", "nmap",
            "-e", iface,
            "-Pn",  # No ping
            "-p", "554,34567,80,8000",  # RTSP, XMEye, HTTP, alt-HTTP
            "--open",
            "-T4",  # Faster
            f"{subnet}.0/24"
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120, text=True)
        for line in result.stdout.splitlines():
            if "Nmap scan report for" in line:
                parts = line.split()
                if len(parts) >= 5:
                    ip = parts[4].strip('()')
                    if ip.startswith(subnet):
                        discovered_ips.add(ip)
                        print(f"[scan] nmap found: {ip}")
            elif "open" in line.lower() and "554" in line:
                # Found RTSP port
                print(f"[scan]   -> has RTSP port 554")
    except Exception as e:
        print(f"[scan] nmap failed: {e}")

    # Step 3: Test RTSP on all discovered IPs + known stations
    all_ips_to_test = set(discovered_ips)

    # Add all known camera IPs from config
    for cam in cameras.values():
        all_ips_to_test.add(cam["ip"])

    print(f"[scan] Testing RTSP on {len(all_ips_to_test)} IPs...")

    # Parallel RTSP test
    def test_rtsp(ip):
        rtsp_url = f"rtsp://admin:@{ip}:554/user=admin&password=&channel=1&stream=0.sdp"
        cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-timeout", "2000000",  # 2 second timeout
            "-i", rtsp_url,
            "-frames:v", "1",
            "-f", "null",
            "-"
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=3)
            if result.returncode == 0:
                results[ip] = True
                print(f"[scan] ✓ RTSP WORKS: {ip}")
            else:
                results[ip] = False
        except Exception:
            results[ip] = False

    threads = []
    for ip in sorted(all_ips_to_test):
        results[ip] = False  # Default to offline
        t = threading.Thread(target=test_rtsp, args=(ip,), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=5)

    online = sum(1 for v in results.values() if v)
    print(f"[scan] Complete: {online}/{len(results)} cameras responding to RTSP")

    return results


def build_html(subnet, cameras: dict):
    """Return the HTML page as a string."""
    # camera_data: cam_id -> {ip, color} — passed directly to JS
    camera_data = cameras

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Camera Focus</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: #111; color: #eee; font-family: -apple-system, system-ui, sans-serif;
       display: flex; flex-direction: column; height: 100vh; overflow: hidden; }}
.toolbar {{ padding: 8px; background: #222; display: flex; flex-wrap: wrap; gap: 6px;
            align-items: center; border-bottom: 1px solid #333; flex-shrink: 0; }}
.toolbar label {{ font-size: 12px; color: #aaa; margin-right: 2px; }}
.station-group {{ display: flex; gap: 3px; align-items: center; margin-right: 8px; }}
.station-label {{ font-size: 10px; color: #888; writing-mode: vertical-rl; text-orientation: mixed; }}
.cam-btn {{ padding: 6px 10px; border: 2px solid transparent; border-radius: 6px;
            font-size: 13px; font-weight: 600; cursor: pointer; color: #fff;
            transition: all 0.15s; }}
.cam-btn:active {{ transform: scale(0.95); }}
.cam-btn.active {{ border-color: #fff; box-shadow: 0 0 8px rgba(255,255,255,0.4); }}
.cam-btn.grid-active {{ border-color: #ffeb3b; box-shadow: 0 0 8px rgba(255,235,59,0.5); }}
.cam-btn.online {{ box-shadow: 0 0 6px rgba(76,175,80,0.6); }}
.cam-btn.offline {{ opacity: 0.4; }}
.controls {{ padding: 6px 8px; background: #1a1a1a; display: flex; gap: 6px;
             align-items: center; flex-shrink: 0; flex-wrap: wrap; }}
.ctrl-btn {{ padding: 5px 14px; border: 1px solid #555; border-radius: 4px;
             background: #333; color: #eee; font-size: 13px; cursor: pointer; }}
.ctrl-btn:active {{ background: #555; }}
.ctrl-btn.stop {{ background: #c62828; border-color: #e53935; }}
.ctrl-btn.scan {{ background: #1565c0; border-color: #1976d2; }}
.ctrl-btn.grid-on {{ background: #e65100; border-color: #ff6d00; }}
#custom-ip {{ padding: 5px 8px; border: 1px solid #555; border-radius: 4px;
              background: #222; color: #eee; font-size: 13px; width: 130px; }}
#status {{ font-size: 12px; color: #aaa; margin-left: auto; }}
.feed-container {{ flex: 1; overflow: hidden; display: flex; align-items: center;
                   justify-content: center; position: relative; touch-action: none;
                   background: #000; }}
#feed {{ max-width: 100%; max-height: 100%; transform-origin: 0 0; }}
#placeholder {{ color: #555; font-size: 18px; text-align: center; padding: 40px; }}
.zoom-info {{ position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,0.6);
              color: #aaa; padding: 3px 8px; border-radius: 4px; font-size: 12px; }}
/* Grid mode */
#grid-container {{ display: none; width: 100%; height: 100%;
                   display: grid; gap: 2px; padding: 2px; }}
#grid-container.g1 {{ grid-template-columns: 1fr; }}
#grid-container.g2 {{ grid-template-columns: 1fr 1fr; }}
#grid-container.g3, #grid-container.g4 {{ grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; }}
.grid-cell {{ position: relative; background: #000; overflow: hidden;
              display: flex; align-items: center; justify-content: center; cursor: pointer; }}
.grid-cell img {{ width: 100%; height: 100%; object-fit: contain; }}
.grid-cell .grid-label {{ position: absolute; top: 4px; left: 6px; background: rgba(0,0,0,0.7);
                          color: #fff; padding: 2px 8px; border-radius: 3px; font-size: 12px;
                          font-weight: 600; pointer-events: none; }}
.grid-cell .grid-remove {{ position: absolute; top: 4px; right: 6px; background: rgba(198,40,40,0.8);
                           color: #fff; border: none; border-radius: 3px; padding: 2px 8px;
                           font-size: 12px; cursor: pointer; }}
.grid-cell .grid-remove:hover {{ background: #e53935; }}
</style>
</head>
<body>

<div class="toolbar" id="toolbar"></div>

<div class="controls">
  <button class="ctrl-btn scan" onclick="scanCameras()">Scan</button>
  <button class="ctrl-btn" id="btn-grid" onclick="toggleGrid()">Grid</button>
  <button class="ctrl-btn" id="btn-clear-grid" onclick="clearGrid()" style="display:none">Clear</button>
  <button class="ctrl-btn" id="btn-zoom-in" onclick="zoomIn()">Zoom +</button>
  <button class="ctrl-btn" id="btn-zoom-out" onclick="zoomOut()">Zoom &minus;</button>
  <button class="ctrl-btn" id="btn-reset" onclick="resetZoom()">1:1</button>
  <button class="ctrl-btn" id="btn-flip-h" onclick="toggleFlipH()">Flip H</button>
  <button class="ctrl-btn" id="btn-flip-v" onclick="toggleFlipV()">Flip V</button>
  <button class="ctrl-btn stop" onclick="stopStream()">Stop</button>
  <input id="custom-ip" type="text" placeholder="Custom IP..." onkeydown="if(event.key==='Enter')startCustom()">
  <button class="ctrl-btn" onclick="startCustom()">Go</button>
  <span id="status">Ready</span>
</div>

<div class="feed-container" id="feed-container">
  <img id="feed" style="display:none">
  <div id="placeholder">Tap a camera to start live feed</div>
  <div class="zoom-info" id="zoom-info" style="display:none">1.0x</div>
  <div id="grid-container" style="display:none"></div>
</div>

<script>
const CAMERAS = {json.dumps(camera_data)};
const MAX_GRID = 4;
let currentIP = null;
let scale = 1, translateX = 0, translateY = 0;
let flipH = false, flipV = false;
let pinchStartDist = 0, pinchStartScale = 1;
let panStartX = 0, panStartY = 0, panStartTX = 0, panStartTY = 0;
let isPanning = false;

// Grid mode state
let gridMode = false;
let gridCameras = [];  // list of IPs currently in the grid

// Build toolbar buttons — one per camera, labelled with camera ID
const toolbar = document.getElementById('toolbar');
for (const [camId, data] of Object.entries(CAMERAS)) {{
  const btn = document.createElement('button');
  btn.className = 'cam-btn';
  btn.style.background = data.color;
  btn.dataset.ip = data.ip;
  btn.title = data.ip;
  btn.textContent = camId;
  btn.onclick = () => startStream(data.ip);
  toolbar.appendChild(btn);
}}

function setStatus(msg) {{
  document.getElementById('status').textContent = msg;
}}

function startStream(ip) {{
  if (gridMode) {{
    toggleGridCamera(ip);
    return;
  }}

  // Single mode — update active button
  document.querySelectorAll('.cam-btn').forEach(b => b.classList.remove('active'));
  const btn = document.querySelector(`.cam-btn[data-ip="${{ip}}"]`);
  if (btn) btn.classList.add('active');

  currentIP = ip;
  const feed = document.getElementById('feed');
  const ph = document.getElementById('placeholder');

  // Reset zoom
  resetZoom();

  // Start stream
  feed.src = '/stream/' + ip + '?t=' + Date.now();
  feed.style.display = 'block';
  ph.style.display = 'none';
  document.getElementById('zoom-info').style.display = 'block';
  setStatus('Connecting to ' + ip + '...');

  feed.onload = function() {{
    if (!this._loaded) {{
      setStatus('Streaming ' + ip);
      this._loaded = true;
    }}
  }};
  feed.onerror = function() {{
    setStatus('Stream error — camera offline?');
  }};
}}

function stopStream() {{
  if (gridMode) {{
    clearGrid();
    return;
  }}
  const feed = document.getElementById('feed');
  feed.src = '';
  feed.style.display = 'none';
  document.getElementById('placeholder').style.display = 'block';
  document.getElementById('zoom-info').style.display = 'none';
  document.querySelectorAll('.cam-btn').forEach(b => b.classList.remove('active'));
  currentIP = null;
  setStatus('Stopped');
  fetch('/stop').catch(() => {{}});
}}

function startCustom() {{
  const ip = document.getElementById('custom-ip').value.trim();
  if (ip) startStream(ip);
}}

// --- Grid mode ---

function toggleGrid() {{
  gridMode = !gridMode;
  const btn = document.getElementById('btn-grid');
  const clearBtn = document.getElementById('btn-clear-grid');
  const zoomControls = ['btn-zoom-in', 'btn-zoom-out', 'btn-reset', 'btn-flip-h', 'btn-flip-v'];

  if (gridMode) {{
    btn.classList.add('grid-on');
    btn.textContent = 'Single';
    clearBtn.style.display = '';
    // Hide single feed
    document.getElementById('feed').style.display = 'none';
    document.getElementById('feed').src = '';
    document.getElementById('placeholder').style.display = 'none';
    document.getElementById('zoom-info').style.display = 'none';
    document.getElementById('grid-container').style.display = 'grid';
    // Hide zoom controls in grid mode
    zoomControls.forEach(id => document.getElementById(id).style.display = 'none');
    // Clear single-mode button highlights
    document.querySelectorAll('.cam-btn').forEach(b => b.classList.remove('active'));
    currentIP = null;
    if (gridCameras.length === 0) {{
      setStatus('Grid mode — tap cameras to add (max ' + MAX_GRID + ')');
    }} else {{
      setStatus('Grid: ' + gridCameras.length + ' camera(s)');
    }}
  }} else {{
    btn.classList.remove('grid-on');
    btn.textContent = 'Grid';
    clearBtn.style.display = 'none';
    // Clear grid streams
    clearGridStreams();
    document.getElementById('grid-container').style.display = 'none';
    document.getElementById('placeholder').style.display = 'block';
    // Show zoom controls
    zoomControls.forEach(id => document.getElementById(id).style.display = '');
    // Clear grid button highlights
    document.querySelectorAll('.cam-btn').forEach(b => b.classList.remove('grid-active'));
    setStatus('Ready');
  }}
}}

function toggleGridCamera(ip) {{
  const idx = gridCameras.indexOf(ip);
  if (idx !== -1) {{
    // Remove from grid
    gridCameras.splice(idx, 1);
  }} else {{
    if (gridCameras.length >= MAX_GRID) {{
      setStatus('Max ' + MAX_GRID + ' cameras in grid');
      return;
    }}
    gridCameras.push(ip);
  }}
  updateGridButtons();
  renderGrid();
  setStatus('Grid: ' + gridCameras.length + ' camera(s)');
}}

function updateGridButtons() {{
  document.querySelectorAll('.cam-btn').forEach(b => {{
    if (gridCameras.includes(b.dataset.ip)) {{
      b.classList.add('grid-active');
    }} else {{
      b.classList.remove('grid-active');
    }}
  }});
}}

function renderGrid() {{
  const gc = document.getElementById('grid-container');
  // Remove old cells — this disconnects old MJPEG streams
  gc.innerHTML = '';
  // Set grid class for layout
  gc.className = 'g' + Math.min(gridCameras.length, 4);

  for (const ip of gridCameras) {{
    const cell = document.createElement('div');
    cell.className = 'grid-cell';

    const img = document.createElement('img');
    img.src = '/stream/' + ip + '?fps=5&t=' + Date.now();

    const label = document.createElement('div');
    label.className = 'grid-label';
    label.textContent = '.' + ip.split('.').pop();

    const removeBtn = document.createElement('button');
    removeBtn.className = 'grid-remove';
    removeBtn.textContent = 'X';
    removeBtn.onclick = (e) => {{
      e.stopPropagation();
      toggleGridCamera(ip);
    }};

    cell.appendChild(img);
    cell.appendChild(label);
    cell.appendChild(removeBtn);
    gc.appendChild(cell);
  }}

  if (gridCameras.length === 0) {{
    document.getElementById('placeholder').style.display = 'block';
    gc.style.display = 'none';
    setStatus('Grid mode — tap cameras to add (max ' + MAX_GRID + ')');
  }} else {{
    document.getElementById('placeholder').style.display = 'none';
    gc.style.display = 'grid';
  }}
}}

function clearGrid() {{
  clearGridStreams();
  gridCameras = [];
  updateGridButtons();
  renderGrid();
  setStatus('Grid cleared');
}}

function clearGridStreams() {{
  // Disconnect all grid MJPEG img elements
  document.querySelectorAll('#grid-container img').forEach(img => {{ img.src = ''; }});
}}

// --- Zoom controls (single mode only) ---

function applyTransform() {{
  const feed = document.getElementById('feed');
  feed.style.transformOrigin = '0 0';
  const sx = flipH ? -scale : scale;
  const sy = flipV ? -scale : scale;
  feed.style.transform = `translate(${{translateX}}px, ${{translateY}}px) scale(${{sx}}, ${{sy}})`;
  document.getElementById('zoom-info').textContent = scale.toFixed(1) + 'x';
}}

function toggleFlipH() {{
  flipH = !flipH;
  document.getElementById('btn-flip-h').style.background = flipH ? '#1565c0' : '#333';
  applyTransform();
}}

function toggleFlipV() {{
  flipV = !flipV;
  document.getElementById('btn-flip-v').style.background = flipV ? '#1565c0' : '#333';
  applyTransform();
}}

function zoomIn() {{
  scale = Math.min(scale * 1.5, 10);
  applyTransform();
}}

function zoomOut() {{
  scale = Math.max(scale / 1.5, 0.5);
  applyTransform();
}}

function resetZoom() {{
  scale = 1; translateX = 0; translateY = 0;
  flipH = false; flipV = false;
  document.getElementById('btn-flip-h').style.background = '#333';
  document.getElementById('btn-flip-v').style.background = '#333';
  applyTransform();
}}

// Pinch-to-zoom and pan (single mode only)
const container = document.getElementById('feed-container');

container.addEventListener('touchstart', function(e) {{
  if (gridMode) return;
  if (e.touches.length === 2) {{
    e.preventDefault();
    pinchStartDist = Math.hypot(
      e.touches[1].clientX - e.touches[0].clientX,
      e.touches[1].clientY - e.touches[0].clientY
    );
    pinchStartScale = scale;
  }} else if (e.touches.length === 1 && scale > 1) {{
    isPanning = true;
    panStartX = e.touches[0].clientX;
    panStartY = e.touches[0].clientY;
    panStartTX = translateX;
    panStartTY = translateY;
  }}
}}, {{ passive: false }});

container.addEventListener('touchmove', function(e) {{
  if (gridMode) return;
  if (e.touches.length === 2) {{
    e.preventDefault();
    const dist = Math.hypot(
      e.touches[1].clientX - e.touches[0].clientX,
      e.touches[1].clientY - e.touches[0].clientY
    );
    scale = Math.max(0.5, Math.min(10, pinchStartScale * (dist / pinchStartDist)));
    applyTransform();
  }} else if (e.touches.length === 1 && isPanning) {{
    e.preventDefault();
    translateX = panStartTX + (e.touches[0].clientX - panStartX);
    translateY = panStartTY + (e.touches[0].clientY - panStartY);
    applyTransform();
  }}
}}, {{ passive: false }});

container.addEventListener('touchend', function(e) {{
  isPanning = false;
}});

// Mouse wheel zoom (single mode only)
container.addEventListener('wheel', function(e) {{
  if (gridMode) return;
  e.preventDefault();
  if (e.deltaY < 0) scale = Math.min(scale * 1.15, 10);
  else scale = Math.max(scale / 1.15, 0.5);
  applyTransform();
}}, {{ passive: false }});

// Scan
function scanCameras() {{
  setStatus('Scanning...');
  document.querySelectorAll('.cam-btn').forEach(b => {{
    b.classList.remove('online', 'offline');
  }});
  fetch('/scan')
    .then(r => r.json())
    .then(data => {{
      let online = 0;
      for (const [ip, up] of Object.entries(data)) {{
        const btn = document.querySelector(`.cam-btn[data-ip="${{ip}}"]`);
        if (btn) {{
          btn.classList.add(up ? 'online' : 'offline');
          if (up) online++;
        }}
      }}
      setStatus(online + '/' + Object.keys(data).length + ' cameras online');
    }})
    .catch(() => setStatus('Scan failed'));
}}
</script>
</body>
</html>"""


class FocusHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the camera focus UI."""

    server_version = "CameraFocus/1.0"
    subnet = SUBNET
    cameras: dict = {}   # set by main() after loading config

    def log_message(self, format, *args):
        # Quieter logging — just method and path
        print(f"[{self.client_address[0]}] {args[0]}" if args else "")

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self._serve_html()
        elif path.startswith("/stream/"):
            ip = path[len("/stream/"):]
            self._serve_stream(ip)
        elif path.startswith("/snapshot/"):
            ip = path[len("/snapshot/"):]
            self._serve_snapshot(ip)
        elif path == "/scan":
            self._serve_scan()
        elif path == "/stop":
            self._serve_stop()
        else:
            self.send_error(404)

    def _serve_html(self):
        html = build_html(self.subnet, self.cameras).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html)

    def _serve_stream(self, ip):
        # Validate IP loosely
        if not ip.replace(".", "").isdigit():
            self.send_error(400, "Invalid IP")
            return

        # Parse fps query param (default 10, grid mode uses 5)
        query = urlparse(self.path).query
        fps = 10
        for param in query.split("&"):
            if param.startswith("fps="):
                try:
                    fps = max(1, min(30, int(param[4:])))
                except ValueError:
                    pass

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            for frame in stream_pool.subscribe(ip, fps=fps):
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n".encode())
                self.wfile.write(b"\r\n")
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except RuntimeError as e:
            # Max streams reached
            self.wfile.write(b"--frame\r\n")
            self.wfile.write(b"Content-Type: text/plain\r\n\r\n")
            self.wfile.write(str(e).encode())
            self.wfile.write(b"\r\n")

    def _serve_snapshot(self, ip):
        if not ip.replace(".", "").isdigit():
            self.send_error(400, "Invalid IP")
            return

        jpeg = grab_snapshot(ip)
        if jpeg is None:
            self.send_error(504, "Could not grab frame")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(jpeg)

    def _serve_scan(self):
        results = scan_cameras(self.subnet, self.cameras)
        body = json.dumps(results).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_stop(self):
        stream_pool.stop_all()
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Camera Focus Web UI")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--subnet", default=SUBNET, help=f"Camera subnet (default: {SUBNET})")
    parser.add_argument("--bind", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG), help="Path to config.json")
    osd_group = parser.add_mutually_exclusive_group()
    osd_group.add_argument("--disable-osd", action="store_true", help="Disable OSD on all cameras and exit")
    osd_group.add_argument("--enable-osd", action="store_true", help="Enable OSD on all cameras and exit")
    args = parser.parse_args()

    FocusHandler.subnet = args.subnet
    cameras = load_cameras(Path(args.config))
    if not cameras:
        print(f"[config] No cameras from config — using {args.subnet}.200–208")
        cameras = {
            f"CAM-{200 + i}": {"ip": f"{args.subnet}.{200 + i}", "color": _PALETTE[i % len(_PALETTE)]}
            for i in range(9)
        }
    FocusHandler.cameras = cameras

    # OSD-only mode: disable or enable OSD on all cameras then exit
    cam_ips = [c["ip"] for c in FocusHandler.cameras.values()]
    if args.disable_osd or args.enable_osd:
        show = args.enable_osd
        threads = [threading.Thread(target=_dvrip_set_osd, args=(ip, show)) for ip in cam_ips]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return

    server = ThreadingHTTPServer((args.bind, args.port), FocusHandler)
    print(f"Camera Focus UI running on http://{args.bind}:{args.port}")
    print(f"Camera subnet: {args.subnet}.x")
    print("Press Ctrl+C to stop")

    # Clean shutdown — must not call server.shutdown() from signal handler
    # (it deadlocks: shutdown() waits for serve_forever(), which is blocked
    # in the main thread where the signal handler is also running).
    def shutdown_handler(signum, frame):
        print("\nShutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        server.serve_forever()
    finally:
        stream_pool.shutdown()


if __name__ == "__main__":
    main()
