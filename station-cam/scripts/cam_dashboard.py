#!/usr/bin/env python3
"""cam-dashboard — local per-station web view of the cameras.

A table of the configured cameras with per-camera actions: Play (opens a modal
with a live HLS player), Config (shows the camera's network/encode/param read
live over Sofia), and Reboot. Streams are transcoded to HLS on demand with
ffmpeg (H.264 copy — near-zero CPU) and stopped once unwatched. stdlib only
(+ vendored dvrip.py and hls.js).

Settings live in their own file, /etc/rovimen-cam/dashboard.json (station-local):
  bind          IP ("0.0.0.0", "127.0.0.1", "100.64.0.5") or interface name
                ("eno1", "tailscale0"); default "0.0.0.0"
  port          default 8080
  stream        0 = main, 1 = substream (default 0; the meteor profile turns the
                substream OFF, and a 2nd main-stream pull is only ~bandwidth)
  idle_timeout  seconds before an unwatched stream's ffmpeg is stopped (default 30)

No authentication — restrict exposure via `bind` or an SSH/Cloudflare tunnel.
"""

import fcntl
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
from dvrip import DVRIPCam  # noqa: E402

CFG = os.environ.get("ROVIMEN_CAM_CONFIG", "/etc/rovimen-cam/config.json")
DASH_CFG = os.environ.get("ROVIMEN_CAM_DASHBOARD", "/etc/rovimen-cam/dashboard.json")
HLS_ROOT = "/dev/shm/rovimen-cam-hls"

with open(CFG) as f:
    _cfg = json.load(f)               # cameras (station config)
_dash = {}                            # dashboard settings (own file, station-local)
if os.path.exists(DASH_CFG):
    with open(DASH_CFG) as f:
        _dash = json.load(f)
PORT = int(_dash.get("port", 8080))
STREAM = int(_dash.get("stream", 0))
IDLE = int(_dash.get("idle_timeout", 30))
USER = _dash.get("user", "admin")
PW = _dash.get("password", "")


def resolve_bind(b):
    b = (b or "0.0.0.0").strip()
    if b == "0.0.0.0" or all(c.isdigit() or c == "." for c in b):
        return b
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(
            fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", b.encode()[:15]))[20:24])
    except OSError:
        print(f"WARN: interface '{b}' has no IPv4 — binding 0.0.0.0")
        return "0.0.0.0"


BIND = resolve_bind(_dash.get("bind", "0.0.0.0"))


def slug(mac):
    return mac.replace(":", "").lower()


CAMERAS = {slug(mac): {"mac": mac, **t} for mac, t in _cfg.get("cameras", {}).items()}


def rtsp(ip):
    return (f"rtsp://{USER}:{PW}@{ip}:554/user={USER}&password={PW}"
            f"&channel=1&stream={STREAM}.sdp?real_stream")


def cam_login(ip, t=6):
    c = DVRIPCam(ip, port=34567, user=USER, password=PW)
    c.timeout = t
    return c if c.login() else None


def _hexbool(v):
    """Truthiness of an XM field that may be a hex string ('0x00000001'), int or bool."""
    if isinstance(v, str):
        try:
            return int(v, 16) != 0
        except ValueError:
            return False
    return bool(v)


def _orientation(param):
    """Summarise image orientation from Camera.Param.[0] PictureFlip/PictureMirror."""
    p = (param or [{}])
    p = p[0] if isinstance(p, list) else p
    if not isinstance(p, dict):
        return None
    flip, mirror = _hexbool(p.get("PictureFlip")), _hexbool(p.get("PictureMirror"))
    state = ("rotated 180° (upside-down corrected)" if flip and mirror else
             "flipped (vertical)" if flip else
             "mirrored (horizontal)" if mirror else "normal")
    return {"state": state, "flip": flip, "mirror": mirror}


def cam_config(ip):
    if not online(ip):          # fast fail so the endpoint never hangs on a down camera
        return None
    c = cam_login(ip)
    if not c:
        return None
    try:
        param = c.get_info("Camera.Param")
        return {"Orientation": _orientation(param),
                "NetCommon": c.get_info("NetWork.NetCommon"),
                "Encode": c.get_info("Simplify.Encode"),
                "CameraParam": param}
    finally:
        c.close()


def cam_reboot(ip):
    if not online(ip):
        return False
    c = cam_login(ip, 8)
    if not c:
        return False
    try:
        c.reboot()
        return True
    finally:
        c.close()


def cam_orient(ip):
    """Read one camera's current image orientation (or None if unreachable)."""
    if not online(ip):
        return None
    c = cam_login(ip)
    if not c:
        return None
    try:
        return _orientation(c.get_info("Camera.Param"))
    finally:
        c.close()


def _config_set_rotate180(mac, rotate180):
    """Persist the orientation in config.json so cam-enforce keeps it: set
    rotate180=true, or drop the key (back to the 'normal' default) when false."""
    try:
        with open(CFG) as f:
            cfg = json.load(f)
        cam = cfg.get("cameras", {}).get(mac)
        if cam is None:
            return
        if rotate180:
            cam["rotate180"] = True
        else:
            cam.pop("rotate180", None)
        with open(CFG, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
    except Exception as e:
        print(f"config rotate180 update failed for {mac}: {e}", flush=True)


def cam_flip_toggle(ip, mac):
    """Toggle a camera between normal and 180° (flip+mirror), live + in config.
    Returns the new orientation, or None if the camera is unreachable."""
    if not online(ip):
        return None
    c = cam_login(ip)
    if not c:
        return None
    try:
        cur = _orientation(c.get_info("Camera.Param")) or {"flip": False, "mirror": False}
        target = not (cur["flip"] and cur["mirror"])   # go to 180° unless already there
        val = "0x00000001" if target else "0x00000000"
        c.set_info("Camera.Param.[0].PictureFlip", val)
        c.set_info("Camera.Param.[0].PictureMirror", val)
        new = _orientation(c.get_info("Camera.Param"))
    finally:
        c.close()
    _config_set_rotate180(mac, target)
    return new


def online(ip):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex((ip, 34567)) == 0
    finally:
        s.close()


# --- on-demand ffmpeg manager ---
_procs = {}
_lock = threading.Lock()


def ensure_stream(sl):
    cam = CAMERAS.get(sl)
    if not cam:
        return
    with _lock:
        rec = _procs.get(sl)
        if rec and rec["proc"].poll() is None:
            rec["last"] = time.time()
            return
        d = os.path.join(HLS_ROOT, sl)
        os.makedirs(d, exist_ok=True)
        cmd = ["ffmpeg", "-nostdin", "-rtsp_transport", "tcp", "-i", rtsp(cam["ip"]),
               "-an", "-c:v", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "4",
               "-hls_flags", "delete_segments+append_list+omit_endlist",
               "-hls_segment_filename", os.path.join(d, "seg%03d.ts"),
               os.path.join(d, "index.m3u8")]
        logf = open(os.path.join(d, "ffmpeg.log"), "wb")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=logf)
        _procs[sl] = {"proc": proc, "last": time.time()}


def _kill(proc):
    """Stop an ffmpeg AND reap it, so it never lingers as a <defunct> zombie:
    terminate() only sends SIGTERM — without a wait() the exited child is never
    collected by this (PID 1 in the container) parent."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        proc.wait(timeout=1)   # reap the exit status (no-op if already reaped)
    except Exception:
        pass


def stop_stream(sl):
    """Terminate a camera's ffmpeg immediately (called when its player closes)."""
    with _lock:
        rec = _procs.pop(sl, None)
    if rec:
        _kill(rec["proc"])
        return True
    return False


def reaper():
    while True:
        time.sleep(5)
        now = time.time()
        with _lock:
            for sl, rec in list(_procs.items()):
                if now - rec["last"] > IDLE or rec["proc"].poll() is not None:
                    _kill(rec["proc"])
                    _procs.pop(sl, None)


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>station-cam</title><script src="/hls.js"></script>
<style>
 body{background:#111;color:#ddd;font:14px system-ui,sans-serif;margin:0;padding:16px}
 h1{font-size:18px;margin:0 0 12px}
 table{width:100%;border-collapse:collapse}
 th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #2a2a2a}
 th{color:#888;font-weight:600;font-size:12px}
 .on{color:#4ade80}.off{color:#f87171}
 button{background:#2a2a2a;color:#ddd;border:1px solid #3a3a3a;border-radius:6px;padding:5px 10px;cursor:pointer;margin-right:4px}
 button:hover{background:#3a3a3a}
 .modal{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;align-items:center;justify-content:center;z-index:9}
 .modal.show{display:flex}
 .card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;max-width:92vw;max-height:92vh;overflow:auto}
 .hd{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid #2a2a2a}
 .card video{width:800px;max-width:90vw;display:block;background:#000}
 pre{margin:0;padding:14px;font:12px ui-monospace,monospace;white-space:pre-wrap;max-width:80vw}
 .x{background:none;border:none;font-size:18px;color:#ccc}
 .bar{display:flex;align-items:center;gap:12px;margin:0 0 12px}
 .bar h1{margin:0}
 .card.wall{width:96vw;height:92vh;display:flex;flex-direction:column}
 .grid{flex:1;overflow:auto;display:grid;gap:8px;padding:10px;align-content:start;
       grid-template-columns:repeat(auto-fill,minmax(340px,1fr))}
 .cell{background:#000;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden;display:flex;flex-direction:column}
 .cell .cap{font-size:12px;color:#bbb;padding:4px 8px;background:#161616;display:flex;justify-content:space-between}
 .cell video{width:100%;aspect-ratio:16/9;display:block;background:#000;object-fit:contain}
</style></head><body>
<div class=bar><h1>station-cam</h1><button onclick="openWall()">&#9638; Toate camerele</button></div>
<table id=t><thead><tr><th>Camera</th><th>MAC</th><th>Profile</th><th>Orientation</th><th>Status</th><th>Actions</th></tr></thead><tbody></tbody></table>

<div class=modal id=playModal><div class=card>
 <div class=hd><b id=playTitle></b><span><button onclick="flip(curPlay,curPlayIp)">&#8635; 180&deg;</button><button class=x onclick="closePlay()">&times;</button></span></div>
 <video id=player controls autoplay muted playsinline></video>
</div></div>

<div class=modal id=cfgModal><div class=card>
 <div class=hd><b id=cfgTitle></b><button class=x onclick="cfgModal.classList.remove('show')">&times;</button></div>
 <pre id=cfgBody></pre>
</div></div>

<div class=modal id=wallModal><div class="card wall">
 <div class=hd><b>Toate camerele</b><button class=x onclick="closeWall()">&times;</button></div>
 <div id=wallGrid class=grid></div>
</div></div>

<script>
let hls=null, wallHls=[], curPlay=null, curPlayIp='', wallSlugs=[];
// stop a camera's ffmpeg now (survives page unload via sendBeacon)
function stopStream(slug){
 if(!slug)return;
 try{
  if(navigator.sendBeacon){navigator.sendBeacon(`/api/stream/${slug}/stop`);}
  else{fetch(`/api/stream/${slug}/stop`,{method:'POST',keepalive:true});}
 }catch(_){}
}
// attach an HLS stream (or native src) to a <video>; returns the Hls instance or null
function attach(v,slug){
 const src=`/hls/${slug}/index.m3u8`;
 if(window.Hls&&Hls.isSupported()){
  const h=new Hls({liveSyncDurationCount:2,manifestLoadingMaxRetry:8,manifestLoadingRetryDelay:700,levelLoadingMaxRetry:8});
  h.on(Hls.Events.ERROR,(e,d)=>{if(d.fatal){try{h.startLoad();}catch(_){}}});
  h.loadSource(src);h.attachMedia(v);
  return h;
 }
 v.src=src; return null;
}
function play(slug,ip){
 playTitle.textContent=ip;
 curPlay=slug;curPlayIp=ip;
 hls=attach(document.getElementById('player'),slug);
 playModal.classList.add('show');
}
function closePlay(){
 playModal.classList.remove('show');
 const v=document.getElementById('player');v.pause();v.removeAttribute('src');v.load();
 if(hls){hls.destroy();hls=null;}
 stopStream(curPlay);curPlay=null;
}
function openWall(){
 const g=document.getElementById('wallGrid');g.innerHTML='';wallHls=[];wallSlugs=[];
 (window.CAMS||[]).forEach(c=>{
  const cell=document.createElement('div');cell.className='cell';
  const cap=document.createElement('div');cap.className='cap';
  cap.innerHTML=`<span>${c.ip}</span><span class="${c.online?'on':'off'}">${c.online?'online':'offline'}</span>`;
  const v=document.createElement('video');v.muted=true;v.autoplay=true;v.playsInline=true;
  cell.appendChild(cap);cell.appendChild(v);g.appendChild(cell);
  wallSlugs.push(c.slug);
  const h=attach(v,c.slug);if(h)wallHls.push(h);
 });
 wallModal.classList.add('show');
}
function closeWall(){
 wallModal.classList.remove('show');
 wallHls.forEach(h=>{try{h.destroy();}catch(_){}});wallHls=[];
 document.querySelectorAll('#wallGrid video').forEach(v=>{v.pause();v.removeAttribute('src');v.load();});
 document.getElementById('wallGrid').innerHTML='';
 wallSlugs.forEach(stopStream);wallSlugs=[];
}
// tab close / refresh / navigate away: stop whatever is streaming
addEventListener('pagehide',()=>{stopStream(curPlay);wallSlugs.forEach(stopStream);});
function showConfig(slug,ip){
 cfgTitle.textContent=ip+' — config';cfgBody.textContent='loading...';cfgModal.classList.add('show');
 fetch(`/api/camera/${slug}/config`).then(r=>r.json())
  .then(d=>{cfgBody.textContent=JSON.stringify(d,null,2);})
  .catch(e=>{cfgBody.textContent='error: '+e;});
}
function reboot(slug,ip){
 if(!confirm('Reboot '+ip+'?'))return;
 fetch(`/api/camera/${slug}/reboot`,{method:'POST'}).then(r=>r.json())
  .then(d=>alert(d.ok?'reboot sent to '+ip:'failed (unreachable?)'));
}
function orLabel(o){
 if(!o||o.error)return '—';
 if(o.flip&&o.mirror)return '180°';
 if(o.flip)return 'flip';
 if(o.mirror)return 'mirror';
 return 'normal';
}
function loadOrient(slug){
 const cell=document.getElementById('or-'+slug);if(!cell)return;
 fetch(`/api/camera/${slug}/orient`).then(r=>r.json())
  .then(o=>{cell.textContent=orLabel(o);cell.title=(o&&o.state)||'';})
  .catch(()=>{cell.textContent='—';});
}
function flip(slug,ip){
 const cell=document.getElementById('or-'+slug);
 const now=cell?cell.textContent.trim():'';
 const toNormal=(now==='180'||now==='180°');
 const msg=toNormal
  ? 'Camera '+ip+' e ACUM rotita 180 grade.\\nO readuci la NORMAL? (se scrie si in config.json)'
  : 'Camera '+ip+' pare orientata NORMAL.\\nO rotesti 180 grade (upside-down)? (se scrie si in config.json)';
 if(!confirm(msg))return;
 if(cell)cell.textContent='...';
 fetch(`/api/camera/${slug}/flip`,{method:'POST'}).then(r=>r.json()).then(o=>{
  if(o.error){alert('esuat (unreachable?)');return loadOrient(slug);}
  if(cell){cell.textContent=orLabel(o);cell.title=o.state||'';}
 }).catch(()=>{alert('eroare');loadOrient(slug);});
}
fetch('/api/cameras').then(r=>r.json()).then(cams=>{
 window.CAMS=cams;
 const tb=document.querySelector('#t tbody');
 cams.forEach(c=>{
  const tr=document.createElement('tr');
  tr.innerHTML=`<td>${c.ip}</td><td>${c.mac}</td><td>${c.profile||'?'}</td>`+
   `<td id="or-${c.slug}">…</td>`+
   `<td class="${c.online?'on':'off'}">${c.online?'online':'offline'}</td>`+
   `<td><button onclick="play('${c.slug}','${c.ip}')">&#9654; Play</button>`+
   `<button onclick="showConfig('${c.slug}','${c.ip}')">Config</button>`+
   `<button onclick="reboot('${c.slug}','${c.ip}')">Reboot</button></td>`;
  tb.appendChild(tr);
  loadOrient(c.slug);
 });
});
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sendfile(self, fp, ctype):
        if not os.path.exists(fp):
            return self._send(404, "not ready")
        with open(fp, "rb") as f:
            self._send(200, f.read(), ctype)

    def _camera_action(self, path):
        """Return (slug, action) for /api/camera/<slug>/<action>, else (None, None)."""
        rest = path[len("/api/camera/"):].split("/")
        if len(rest) == 2 and rest[0] in CAMERAS:
            return rest[0], rest[1]
        return None, None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self._send(200, PAGE)
        if path == "/hls.js":
            return self._sendfile(os.path.join(_HERE, "hls.min.js"), "application/javascript")
        if path == "/api/cameras":
            data = [{"slug": sl, "mac": c["mac"], "ip": c["ip"],
                     "profile": c.get("profile"), "online": online(c["ip"])}
                    for sl, c in CAMERAS.items()]
            return self._send(200, json.dumps(data), "application/json")
        if path.startswith("/api/camera/"):
            sl, action = self._camera_action(path)
            if sl and action == "config":
                cfg = cam_config(CAMERAS[sl]["ip"])
                return self._send(200, json.dumps(cfg or {"error": "unreachable"}),
                                  "application/json")
            if sl and action == "orient":
                o = cam_orient(CAMERAS[sl]["ip"])
                return self._send(200, json.dumps(o or {"error": "unreachable"}),
                                  "application/json")
            return self._send(404, "unknown camera/action")
        if path.startswith("/hls/"):
            parts = path[len("/hls/"):].split("/", 1)
            if len(parts) != 2 or parts[0] not in CAMERAS:
                return self._send(404, "unknown camera")
            sl, fname = parts
            ensure_stream(sl)
            fp = os.path.join(HLS_ROOT, sl, os.path.basename(fname))
            if fname.endswith(".m3u8"):
                # wait until the playlist actually references a segment, so the
                # first Play gets a playable manifest instead of an empty one
                for _ in range(120):   # up to ~12s for ffmpeg's first segment
                    try:
                        if os.path.exists(fp) and ".ts" in open(fp).read():
                            break
                    except OSError:
                        pass
                    time.sleep(0.1)
                return self._sendfile(fp, "application/vnd.apple.mpegurl")
            if fname.endswith(".ts"):
                return self._sendfile(fp, "video/mp2t")
        return self._send(404, "nope")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/camera/"):
            sl, action = self._camera_action(path)
            if sl and action == "reboot":
                ok = cam_reboot(CAMERAS[sl]["ip"])
                return self._send(200, json.dumps({"ok": ok}), "application/json")
            if sl and action == "flip":
                o = cam_flip_toggle(CAMERAS[sl]["ip"], CAMERAS[sl]["mac"])
                return self._send(200, json.dumps(o or {"error": "unreachable"}),
                                  "application/json")
        if path.startswith("/api/stream/") and path.endswith("/stop"):
            sl = path[len("/api/stream/"):-len("/stop")]
            if sl in CAMERAS:
                stop_stream(sl)
                return self._send(200, json.dumps({"ok": True}), "application/json")
        return self._send(404, "nope")


def main():
    if not CAMERAS:
        print("no cameras in config")
        return 1
    os.makedirs(HLS_ROOT, exist_ok=True)
    threading.Thread(target=reaper, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), H)
    where = "ALL interfaces" if BIND == "0.0.0.0" else BIND
    print(f"cam-dashboard on http://{where}:{PORT}  cameras={list(CAMERAS)}  stream={STREAM}",
          flush=True)
    if BIND == "0.0.0.0":
        print("NOTE: bound on all interfaces with no auth — restrict via "
              "config 'dashboard.bind' (e.g. 127.0.0.1 or tailscale0).", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with _lock:
            for rec in _procs.values():
                _kill(rec["proc"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
