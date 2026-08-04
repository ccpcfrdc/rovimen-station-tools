#!/usr/bin/env python3
"""Video-freeze watchdog for the station's cameras, driven by RMS output.

The reliable, zero-cost signal that a camera's *video* pipeline is alive is
whether RMS — the meteor-capture process that already holds the camera's RTSP
stream — keeps producing frames. We NEVER open a second RTSP session (that could
disturb the capture RMS/GMN depend on); we only *read* what RMS already writes:
the FF files that land in its CapturedFiles directory every ~256 frames
(~10 s at 25 fps). A camera whose control plane is alive but whose video pipeline
has wedged (the classic XM freeze) shows up as "RMS running, FF files no longer
advancing".

Per camera we classify:
  OK       — RMS is capturing and FF files are advancing
  FROZEN   — RMS is capturing but no fresh FF for `stale_after_s`
  STARTING — RMS just began capturing; still inside the startup grace window
  IDLE     — RMS is not capturing this camera right now (nothing to judge)
  NO-RMS   — no RMS station maps to this camera's IP
  UNKNOWN  — mapped, capturing, but the data dir can't be read

With --reboot a FROZEN camera is power-cycled over DVRIP (the same path as
cam-reboot) with a cooldown so a wedged camera is nudged, not hammered. A reboot
only fires on positive evidence (RMS running + frames stalled), so a station with
RMS stopped is never rebooted.

Config: an optional "rms" block in config.json (every key has a default):
  "rms": {
    "user": "rms",
    "config_globs": ["~/source/Stations/*/.config", "~/source/RMS/.config"],
    "stale_after_s": 60, "startup_grace_s": 120,
    "reboot": {"cooldown_s": 900, "max_per_hour": 2}
  }

Run:  sudo cam-health            # report only (exit 1 if any camera is FROZEN)
      sudo cam-health --reboot   # report + auto-reboot frozen (used by the timer)
      sudo cam-health --json     # machine-readable status
"""

import glob
import json
import os
import pwd
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)  # vendored dvrip.py + cam_enforce live next to this script
from cam_enforce import login  # noqa: E402  (reuse the exact DVRIP login path)

CFG_PATH = os.environ.get("ROVIMEN_CAM_CONFIG", "/etc/rovimen-cam/config.json")
STATE_PATH = "/var/lib/rovimen-cam/cam-health.json"

DEFAULTS = {
    "user": "rms",
    "config_globs": ["~/source/Stations/*/.config", "~/source/RMS/.config"],
    "stale_after_s": 60,
    "startup_grace_s": 120,
    "reboot": {"cooldown_s": 900, "max_per_hour": 2},
}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_cfg():
    if not os.path.exists(CFG_PATH):
        log(f"config missing: {CFG_PATH}")
        sys.exit(2)
    with open(CFG_PATH) as f:
        return json.load(f)


def rms_settings(cfg):
    s = dict(DEFAULTS)
    s.update(cfg.get("rms", {}) or {})
    r = dict(DEFAULTS["reboot"])
    r.update((cfg.get("rms", {}) or {}).get("reboot", {}) or {})
    s["reboot"] = r
    return s


def home_of(user):
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return os.path.expanduser("~" + user)


def expand(path, home):
    if path.startswith("~"):
        return home + path[1:]
    return path


# ---- RMS station discovery ------------------------------------------------

def parse_rms_config(path):
    """Pull stationID / device / data_dir out of an RMS .config (INI-ish,
    'key: value' lines). Returns {} if the file can't be read."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue
                if ":" not in line:
                    continue
                key, _, val = line.partition(":")
                key = key.strip().lower()
                val = val.split(";", 1)[0].strip()  # drop trailing inline comment
                if key in ("stationid", "device", "data_dir"):
                    out[key] = val
    except OSError:
        return {}
    return out


def device_ip(device):
    """Extract the host/IP from an RMS device string like
    'rtsp://10.42.0.10:554/...'. Returns '' if not an rtsp URL."""
    if not device or "://" not in device:
        return ""
    host = device.split("://", 1)[1]
    host = host.split("/", 1)[0]       # strip path
    host = host.split("@", 1)[-1]      # strip creds if any
    host = host.split(":", 1)[0]       # strip port
    return host.strip()


def discover_stations(settings, home):
    """All RMS stations found on disk, keyed by camera IP."""
    by_ip = {}
    for pattern in settings["config_globs"]:
        for path in glob.glob(expand(pattern, home)):
            c = parse_rms_config(path)
            ip = device_ip(c.get("device", ""))
            if not ip:
                continue
            by_ip[ip] = {
                "station": c.get("stationid", "?"),
                "data_dir": expand(c.get("data_dir", "~/RMS_data"), home),
                "config_path": path,
            }
    return by_ip


# ---- capture liveness -----------------------------------------------------

def capture_cmdlines():
    """cmdlines of every process that looks like an RMS StartCapture."""
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "StartCapture" in cmd:
            out.append(cmd)
    return out


def is_capturing(station, config_path, cmdlines):
    """A station is capturing if a StartCapture process names its ID or config."""
    cfg_dir = os.path.dirname(config_path)
    for cmd in cmdlines:
        if station and station != "?" and station in cmd:
            return True
        if config_path and config_path in cmd:
            return True
        if cfg_dir and cfg_dir in cmd:
            return True
    return False


def newest_ff(data_dir, station):
    """(newest FF mtime, newest capture-dir mtime) for this station, or (None, None).
    Raises OSError semantics folded into None so caller can flag UNKNOWN."""
    capdir = os.path.join(data_dir, "CapturedFiles")
    if not os.path.isdir(capdir):
        return None, None, False
    best_ff = None
    best_dir = None
    prefix = (station + "_") if station and station != "?" else ""
    try:
        names = os.listdir(capdir)
    except OSError:
        return None, None, False
    for name in names:
        if prefix and not name.startswith(prefix):
            continue
        d = os.path.join(capdir, name)
        if not os.path.isdir(d):
            continue
        try:
            m = os.path.getmtime(d)
        except OSError:
            continue
        if best_dir is None or m > best_dir:
            best_dir = m
        try:
            for fn in os.listdir(d):
                if fn.startswith("FF_") and fn.endswith((".fits", ".bin")):
                    fm = os.path.getmtime(os.path.join(d, fn))
                    if best_ff is None or fm > best_ff:
                        best_ff = fm
        except OSError:
            continue
    return best_ff, best_dir, True


def classify(cam_ip, stations_by_ip, cmdlines, settings, now):
    """Return (state, detail, reboot_ok) for one of our cameras."""
    st = stations_by_ip.get(cam_ip)
    if not st:
        return "NO-RMS", "nicio stație RMS pe acest IP", False
    if not is_capturing(st["station"], st["config_path"], cmdlines):
        return "IDLE", f"RMS ({st['station']}) nu capturează", False

    best_ff, best_dir, readable = newest_ff(st["data_dir"], st["station"])
    if not readable:
        return "UNKNOWN", f"data_dir inaccesibil ({st['data_dir']})", False

    grace = settings["startup_grace_s"]
    stale = settings["stale_after_s"]
    starting = best_dir is not None and (now - best_dir) < grace

    if best_ff is None:
        if starting:
            return "STARTING", "captură pornită, încă fără FF", False
        return "FROZEN", "captură activă dar niciun FF produs", True

    age = int(now - best_ff)
    if age <= stale:
        return "OK", f"ultimul FF acum {age}s (stație {st['station']})", False
    if starting:
        return "STARTING", f"captură recentă, FF la {age}s", False
    return "FROZEN", f"FF învechit de {age}s (prag {stale}s)", True


# ---- reboot with cooldown -------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"reboots": {}}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        log(f"WARN: could not persist state: {e}")


def reboot_allowed(ip, state, settings, now):
    """Cooldown gate: not too soon after the last reboot, not too many per hour."""
    hist = [t for t in state["reboots"].get(ip, []) if now - t < 3600]
    state["reboots"][ip] = hist  # prune
    if hist and (now - max(hist)) < settings["reboot"]["cooldown_s"]:
        return False, f"cooldown ({int(now - max(hist))}s < {settings['reboot']['cooldown_s']}s)"
    if len(hist) >= settings["reboot"]["max_per_hour"]:
        return False, f"limită {settings['reboot']['max_per_hour']}/oră atinsă"
    return True, ""


def do_reboot(ip, state, now):
    # login() can *raise* (dvrip throws when a camera is unreachable, and a wedged
    # camera's control plane may hang too) — never let that crash the watchdog.
    c = None
    try:
        c = login(ip, 8)
        if not c:
            log(f"[{ip}] reboot skip — DVRIP login refused")
            return False
        c.reboot()
        state["reboots"].setdefault(ip, []).append(now)
        log(f"[{ip}] FROZEN — reboot sent")
        return True
    except Exception as e:
        log(f"[{ip}] reboot skip — DVRIP error: {e}")
        return False
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass


# ---- main -----------------------------------------------------------------

def main():
    args = sys.argv[1:]
    do_reboots = "--reboot" in args
    as_json = "--json" in args

    cfg = load_cfg()
    settings = rms_settings(cfg)
    home = home_of(settings["user"])
    stations_by_ip = discover_stations(settings, home)
    cmdlines = capture_cmdlines()
    now = time.time()

    cameras = cfg.get("cameras", {})
    rows = []
    for mac, tgt in sorted(cameras.items(), key=lambda kv: kv[1].get("ip", "")):
        ip = tgt.get("ip", "?")
        state_s, detail, reboot_ok = classify(ip, stations_by_ip, cmdlines, settings, now)
        rows.append({"mac": mac, "ip": ip, "state": state_s,
                     "detail": detail, "reboot_ok": reboot_ok})

    frozen = [r for r in rows if r["state"] == "FROZEN"]

    if do_reboots and frozen:
        state = load_state()
        for r in frozen:
            ok, why = reboot_allowed(r["ip"], state, settings, now)
            if ok:
                do_reboot(r["ip"], state, now)
            else:
                log(f"[{r['ip']}] FROZEN — reboot held: {why}")
        save_state(state)

    if as_json:
        print(json.dumps({"checked_at": int(now), "cameras": rows}, indent=2))
    else:
        if not rows:
            log("no cameras in config.json")
        for r in rows:
            print(f"  {r['ip']:<14} {r['state']:<8} {r['detail']}")
        if not stations_by_ip:
            log("note: no RMS station configs found — cameras show NO-RMS until RMS is set up")

    sys.exit(1 if frozen else 0)


if __name__ == "__main__":
    main()
