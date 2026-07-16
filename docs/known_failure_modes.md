# Known Failure Modes

Catalog of recurring or hard-to-diagnose failures observed across ROVIMEN/GMN stations. Each entry: **symptom → root cause → diagnosis → fix/workaround → status**. Add new entries here when a non-obvious failure burns more than an hour of debugging.

---

## 1. Brașov (gmn0008) — hourly RTSP drops were the firmware AutoReboot misconfigured as hourly

**Symptom.** Both cameras (RO0010 + RO000Y) drop ~800–1700 frames once per hour, in a single ~30–50 s burst aligned to each camera's internal `:00`. Each camera's burst lands at a different UTC minute and that minute drifts +1 min/hour, because the device clocks were unsynchronised. On bad nights the drop is long enough to break the RMS session.

**Root cause.** Camera firmware (Heroseen-style HDIPC, MAC OUI `bc:07:18:*`, `deviceID: H0100011A050…`, Vue-SPA web UI on Boa httpd, `/action/*` JSON API, `getSysConfig.platform = "platform v9.1.7"`) had `/action/getRebootConf` set to `{mode: 1, hour: 15, minute: 0}`. The mode value semantics — recovered by reading the `el-select` options in `/js/app.*.js` (mode `4` is the only one that reveals the `day_week` selector, and the form's default is `mode: 2`):

| `mode` | meaning |
|--|--|
| 0 | disabled |
| **1** | **hourly at `:minute`** (the `hour` field is ignored) |
| 2 | daily at `HH:MM` |
| 4 | weekly on `day_week` at `HH:MM` |

Whoever clicked "the second option" in the dropdown thinking it meant "Daily 15:00" actually set the camera to **`Hourly at :00`** — a full reboot every hour. The ~30–50 s downtime is the firmware power-cycling its services. Memory note "AutoReboot: Everyday 15:00" was wrong; reading the same dropdown label without reading the value is what propagated the misconfiguration across other stations historically.

Drift explanation: without NTP the device clock free-runs. The reboot still fires at camera-local `:00`, which is some constant UTC offset per camera. The observed "+1 min/hour" drift was crystal drift, not a separate phenomenon. Once NTP-locked, the reboot fires at UTC `:00` exactly.

**Fix.**
```bash
curl -u admin:123456 -H "Content-Type: application/json" -X POST \
     -d '{"mode":2,"day_week":0,"hour":15,"minute":0}' \
     http://<cam>/action/setRebootConf
# verify
curl -u admin:123456 http://<cam>/action/getRebootConf
```
Applied 2026-05-23 to both gmn0008 cameras. Verified via re-read: `mode: 1 → 2`. Camera now reboots once per day at 15:00 local instead of every hour at `:00`.

**Diagnosis path (kept because this took 3+ nights to land).**
1. Confirmed burst timing wall-clock-aligned per camera, not session-age driven (RMS restarted at varied times, burst landed at the same camera-local minute).
2. Mined the Vue SPA bundles (`/js/app.*.js`) for cron/schedule/hourly/reboot/task references — found the form's `restarForm.mode` field, the `el-select` value mapping, and the cron predefined map `{"@hourly":"0 * * * *"}`.
3. Enabled NTP daily-noon sync on RO0010 (2026-05-22), kept RO000Y untouched. Next night: RO0010 drops landed at UTC `:00:00 ± 2 s` (4 of 5 drops), RO000Y kept dropping at its drifted UTC `:04–:06`. **NTP did not eliminate the event** — it realigned it to UTC `:00`.
4. `curl -u admin:123456 /action/getRebootConf` → `mode:1`. The mode-value table above falls straight out of the JS.

**What does NOT help (verified empirically 2026-05-21/22, kept for posterity):**
| Attempted fix | Result |
|---|---|
| `media_backend: cv2` (FFmpeg backend) | Worse — single 41 s freeze, +2500 drops/cycle |
| `tcp-timeout=30000000` (30 s) | Catastrophic — 3200+ drops (RMS lingers longer in dead session) |
| `protocols=tcp` only | No effect — already default |
| Proactive RMS restart via systemd timer 5 min before the burst | No effect, adds restart-related drops |
| Larger RAM buffer (`num_raw_frames`) | Cannot help — drops are missing camera frames, not buffer overflow |
| Enable NTP daily-noon sync on RO0010 alone | Realigned drops to UTC `:00` sharp — drops were not eliminated |

**Camera HTTP API notes.** (`admin:123456` below is the XMEye **factory
default** — change it on your own cameras; it is shown here only to document the
firmware API.)
- Base: `http://<cam>/action/<endpoint>` — HTTP basic auth `admin:123456`.
- Useful endpoints: `getDeviceTime` / `setTime`, `getRebootConf` / `setRebootConf`, `getRtspConf`, `getRecSchedule`, `getMotionDetConf`, `getSysConfig`, `getAlarmLog`, `getLogTar` (slow).
- `setTime` JSON body: `{ntp_enable, ntpServer, timeInterval, timeZone, timeZoneMode, onvifSyncTime}`.
- `setRebootConf` JSON body: `{mode, day_week, hour, minute}` — values per the table above.

**Status.** **Resolved 2026-05-23.** Both gmn0008 cameras now scheduled for one reboot per day at 15:00 local. Hourly drops stopped entirely after the next firmware reboot cycle (see timing gotcha below). If you onboard another Heroseen/HDIPC camera (MAC OUI `bc:07:18`), check `getRebootConf` immediately — the factory default appears to be `mode: 1`. Full API reference in [`camera_firmware_heroseen.md`](camera_firmware_heroseen.md).

**Post-fix verification (full-night recap, May 22→23):**

| cam | sessions | intra-session drops (UTC) | longest clean stretch | total FFs |
|--|--|--|--|--|
| RO0010 (NTP-synced) | 1 (no session breaks) | 19:00, 20:00, 20:17, 21:00, 22:00 — **all pre-fix** | 22:00 → 02:03 = **4h 03m** | 2666 |
| RO000Y (drifted baseline) | 1 | 19:04, 20:04, 20:05, 20:16, 21:06, 22:06 — **all pre-fix** | 22:06 → 02:03 = **3h 57m** | 2657 |

Zero drops on either camera from the first post-fix reboot cycle through sunrise. Verified `getRebootConf` post-night still returns `mode:2` on both — the change is sticky across the natural daily reboot.

**Timing gotcha (important).** Setting `mode:2` via the HTTP API does NOT stop the *currently-scheduled* hourly reboot. The firmware re-reads the schedule on each reboot, so the existing `mode:1` cron continues to fire one more time after you POST the change. Concretely: I applied the fix at ~20:40 UTC on both cams; the 21:00 and 22:00 reboots still happened, then 23:00 was finally clean. Plan to apply the fix during daytime if you want to avoid catching one or two more drops that night.

**The 20:17 / 20:16 coincident drop on BOTH cameras** in the table above is unrelated to the camera cron — it's an external network event (router or ISP blip; same wall-clock minute on two independent cameras is not a coincidence with a hourly-aligned cron). Worth noting for future debug: not every drop here is the firmware reboot. Verify timing alignment before blaming the AutoReboot config on a new station.

---

## 2. Brașov (gmn0008) — colour stacker saturating Q6600

**Symptom.** Frame drops correlated 1:1 with the rovimen-scripts `stacker.py` (`ffmpeg -f rawvideo -pix_fmt rgb24 pipe:1`) running. Load average 8–10 (Core 2 Quad saturated, 400% max). Each stacker run consumed ~80% CPU for 44–50 s; RMS compression threads stalled, capture buffer filled, frames dropped. May 7 night: 10–12 k drops per camera over ~5 hours observation window.

**Root cause.** Real-time stacker invocation in `color_capture.py` (`services.stacker.realtime: true` default) launched ffmpeg per finished chunk on a CPU that already runs ~340% with two RMS instances. No headroom. The `nice` value in config (default 10) only changes scheduling priority — it does not cap absolute CPU usage when the box is fully saturated. The `cpu_quota: 50` in `config_defaults.json` is **dead config** — `color_capture.py:404-421` reads only `nice` from `services.stacker`, never `cpu_quota`.

**Diagnosis steps that found it.**
1. Sampled `/proc/<pid>/stat` and `BufferedCapture` log "Buffer fill %" every 30 s overnight; found drops landed exactly during `stacker.py` runtime windows.
2. Load avg correlated 1:1 with stacker activity.

**Fix.** Per-station config patch in `~/rovimen_scripts/config.json`:
```json
"services": {
  "stacker": {
    "enabled": true,
    "realtime": false,
    "nice": 19
  }
}
```
`realtime: false` defers stacking to the morning sweep (single batched pass via `_stack_night_perfile`) after RMS capture finishes — zero contention.
Verified 2026-05-21 night on gmn0008: zero stacker-correlated drops after the change.

**Trade-off.** No live colour-stack previews during capture — dashboard sees yesterday's stacks until morning sweep finishes. RMS-side BW maxpixel previews remain live throughout the night.

**Status.** **Resolved on gmn0008.** Other low-CPU stations (Q6600/N100-class) should adopt the same setting until the proper fix lands.

**Proper fix (TODO PR).** Plumb `services.stacker.cpu_quota` end-to-end: wrap stacker subprocess in `systemd-run --scope -p CPUQuota=50%` or `cpulimit -l 50`. Also expose `ffmpeg_threads` as a real config knob (currently hardcoded auto: `max(1, cpu_count // workers)` in `stacker.py:563`, which on Q6600 means 4 threads — way too aggressive).

---

## 3. Q6600 (and other pre-SSE4.1 CPUs) — RMS ml_filter SIGILL crash loop

**Symptom.** After a Sunday `GRMSUpdater.sh` pull, RMS services crash with `status=4/ILL` (SIGILL — illegal instruction) and core-dump. systemd `Restart=always` relaunches → infinite loop, services rack up dozens of `NRestarts` per hour. Each cycle: detection finishes → ML filter step starts → SIGILL within 1 second. **No nightly archives produced.** Multiple nights of captured FFs sit unprocessed; once `capt_dirs_to_keep` is reached, raw captures rotate out.

**Root cause.** Newer RMS pulls `litert` (a.k.a. `ai-edge-litert`) for the ML filter step (`RMS/MLFilter.py`). litert depends on XNNPACK, which emits **SSE4.1 / AVX / AVX2** intrinsics that the Core 2 Quad (Q6600) does not have. The first inference call traps SIGILL.

**Diagnosis steps that found it.**
1. `journalctl -u rms-capture -e` showed `Main process exited, code=dumped, status=4/ILL` immediately after `MLFilter inference starting...` log line.
2. CPU lacks SSE4.1 (`grep -m1 -E "sse4_1|avx" /proc/cpuinfo` returns empty).

**Workaround.** Disable the ML filter step in the RMS `.config`:
```ini
ml_filter: 0
```
Setting it to `0` (vs the default `0.85` ML-confidence threshold) skips the entire MLFilter call in `Reprocess.py:211`. Detection still runs; only the post-detection ML pruning is skipped. Cost: more false-positive detections leak through to FTPdetectinfo — small impact on data quality. Verified 2026-05-21 on gmn0008: zero SIGILL events after the change, all four backlog nights (May 17–20) reprocessed and archived successfully.

**Proper fix (TODO).** Swap `litert` for `tflite-runtime` (older interpreter, no XNNPACK dependency) in the RMS venv on these stations. Or upstream a runtime feature-flag check in `MLFilter.py` that auto-skips when CPU lacks SSE4.1.

**Stations affected.** gmn0008 (Q6600, confirmed). Likely also any pre-Nehalem Intel and pre-K10 AMD machine running the current RMS prerelease.

**Status.** **Workaround in place** on gmn0008. PR upstream pending.

---

## 4. Dashboard offline because configured jump_host went down

**Symptom.** A station's tile shows `online: false` on `https://dashboard.example.net`. The station itself is healthy (its `rovimen-station-api` on `:7779` responds locally and from other tailnet hosts). Dashboard returns `{"error":"HTTPConnectionPool(host='127.0.0.1', port=NNNN): Connection refused"}` for the station's `/api/status`.

**Root cause.** `dashboard_config.yaml` lists a `jump_hosts: [some_other_station]` for the affected station — and that jump_host station is offline (e.g. ISP outage, storm, power). `TunnelManager._establish_tunnel` (`rovimen_dashboard.py:575`) iterates the jump_hosts list trying to `ssh -L <local>:<station_ip>:7779 gmn@<jump_ip>`; if all jump_hosts fail to SSH, no tunnel is established, and `api_base` returns the dead `http://127.0.0.1:<local_port>` URL.

**Why this was set up unnecessarily.** Historical: the jump-host config was needed when stations lived in a separate tailnet shared into GMN. Once a station is native to (or directly shared into) the GMN tailnet, the VPS can reach it directly over Tailscale — no SSH tunnel needed.

**Diagnosis.**
1. From any GMN-tailnet host: `curl http://<station_ip>:7779/api/status` — if this works, the station is reachable directly and the SSH tunnel is unnecessary.
2. `tailscale status | grep <jump_station>` — if it shows `offline, last seen Nh ago`, that's why the tunnel can't be opened.

**Fix.** In `dashboard_config.yaml`, set the station's `jump_hosts: []` (empty list). `TunnelManager.needs_tunnel()` returns `False` for empty lists, and `get_api_base` falls through to `http://<station.ip>:<api_port>` direct.

**Deploy gotcha.** `.github/workflows/deploy-dashboard.yml` rsync **excludes** `dashboard_config.yaml`. After editing in git, manual `scp` to `/opt/rovimen/dashboard_config.yaml` (prod) **and** `/opt/rovimen-dev/dashboard_config.yaml` (dev) is required, followed by `systemctl restart rovimen-dashboard.service` / `rovimen-dashboard-dev.service`.

**Status.** **Resolved on gmn0008** (2026-05-22): jump_hosts cleared since the station is native to GMN tailnet. Other stations with `jump_hosts` set should be re-evaluated — most are likely no longer needed since cross-tailnet sharing has converged most stations into GMN tailnet directly or as shared peers.

---

## 5. `RMS.Reprocess` force-terminates large `imgdata.tar.bz2` uploads mid-flight

**Symptom.** After `RMS.Reprocess` finishes, only the small `*_metadata.tar.bz2` lands on `gmn.uwo.ca`. The much larger `*_imgdata.tar.bz2` is missing from the `files/` directory on the server. The Reprocess log shows the upload starting, progressing through ~30–60% percent ticks, then:
```
WARNING-UploadManager-line:594 - UploadManager did not stop within the timeout period of 60 seconds.
INFO-UploadManager-line:606 - UploadManager terminated (after forced terminate).
INFO-Reprocess-line:858 - Closing upload manager...
```
The `files_to_upload.inf` queue still contains the imgdata path, so it'll be retried on the next nightly upload, but **the data does not reach UWO until tomorrow morning** — and if anything dies in between, the queue file can be lost.

**Root cause.** `UploadManager.stop()` calls `Thread.join(timeout)` to wait for the worker to exit. The worker only checks `self.exit` **between** uploads; mid-transfer it is blocked inside paramiko. The historical default `timeout=60` is much shorter than a 300–400 MB SFTP transfer at 4–6 MB/s (~60–100 s). Once the timer fires, `Reprocess.py` calls `self.terminate()` which kills the worker mid-write — the file is partially uploaded, never finalized, never retried until the next nightly cycle.

**Diagnosis.**
- Look at `tail -30` of the Reprocess log: if the last upload progress tick is well below 100 %, the upload was killed.
- `ssh <station> "cat ~/<data_dir>/<station>/files_to_upload.inf"` — if it still lists a `*_imgdata.tar.bz2` after Reprocess completed, the upload didn't finish.
- Verify directly on the server: `sftp -i ~/.ssh/id_rsa <stationid_lower>@gmn.uwo.ca` then `ls -l files/<station>_<night>_imgdata.tar.bz2` — the username on gmn.uwo.ca is the **lowercase station ID** (see `UploadManager.py:800` comment).

**Fix.**
- Local fork (`fireball-stdpixel-decontamination` branch): commit `6c9584e0 "increased upload manager timeout to 5 mins"` bumped the default to `5*60`. Bumped further to `30*60` (30 min) on 2026-05-22 to handle slow links / multi-file queues, and the stale "60 seconds" docstring was corrected. See `RMS/UploadManager.py:581`.
- The proper upstream fix is to make `stop()` poll for an in-progress-upload flag and only terminate if the worker is genuinely stuck (idle but unresponsive), with an absolute-max-wait fallback. Not done yet.
- Stations running older RMS (e.g. Berlin Pi on `feature-scikit-image`, commit `0e2c9ad2`, default `timeout=60`) will keep hitting this until they pull the fixed `UploadManager.py`. Patch can be applied independently: `scp RMS/UploadManager.py <station>:<rms_dir>/RMS/` and restart the capture service. No DB migrations or coupled changes.

**Manual recovery if you just hit it.** Push the partial file directly:
```bash
ssh <station> 'sftp -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_rsa <stationid_lower>@gmn.uwo.ca <<<"put <full_path>_imgdata.tar.bz2 files/"'
ssh <station> ': > <data_dir>/<station>/files_to_upload.inf'  # clear queue
```

**Status.** **Local fork patched (2026-05-22).** Stations need individual sync; not auto-deployed.

---

## 6. Camera exposure reverts to daylight defaults → black frames at night

**Symptom.** Live view / RMS frames are nearly black at night even though the camera streams fine. RMS detects ~0 stars.

**Diagnosis.** Read `Camera.Param.[0]` via dvrip. The smoking gun is `ExposureParam.MostTime` capped at ~1 ms (`0x400`) and `GainParam` = `{AutoGain: 0, Gain: 0}`. The sensor never integrates longer than ~1/1000 s and applies no gain — fine in daylight, black at night. Some camera units ship/revert to this.

**Fix.** Set the ROVIMEN night standard via dvrip: `ExposureParam` LeastTime/MostTime `0x9C40` (40 ms = full 1/25 s), `GainParam` `{AutoGain: 1, Gain: 60}`, `ElecLevel` 40, `AeSensitivity` 1. Takes effect immediately. **These revert on camera power-cycle**, so they must be re-asserted by `fix_cam_encoding.sh` — which historically only set EsShutter/WB/encode, NOT exposure (fixed: PR #230 adds exposure/gain enforcement to the boot fixup).

**Status.** Found on gmnro11 (Bacau) 2026-05-30. fix_cam_encoding.sh patched; rolls out fleet-wide on next bundle deploy.

---

## 7. "Scripts deployed" ≠ "pipeline running" — `toggle_rovimen.sh on` never run

**Symptom.** Dashboard shows "Color capture is disabled — stacking, encoding, detection lock not running." RMS + station_api are up and live view works, but no color capture / dawn pipeline.

**Diagnosis.** `color-capture.service` (note the **hyphen**, not `color_capture`) is installed in `/etc/systemd/system/` but **disabled/never started**, and there's no `dawn_process` cron. A station deploy installs the scripts and units but does NOT enable the ROVIMEN pipeline — that's a separate explicit step.

**Fix.** `bash ~/rovimen_scripts/toggle_rovimen.sh on` — enables+starts color-capture/camera-focus/station-api and installs the dawn/janitor/reboot crons. Verify color `*_color.mkv` chunks land in `~/color_capture/<station>/<date>/`. Note `toggle_rovimen.sh on` **rewrites the user crontab** (replaces ad-hoc entries with the standard set).

**Status.** Found on gmnro11 (Bacau) 2026-05-30 — deploy got RMS+live-view up but the pipeline was never toggled on.

---

## 8. Stale RMS capture config drift after a camera codec change (decoder + resolution)

**Symptom.** RMS produces 0 FF files; capture "starts" but no frames. ffprobe shows the camera streaming fine.

**Diagnosis.** Two independent config-vs-reality mismatches in `RMS_camN/.config`:
- `gst_decoder: avdec_h265` while the camera now streams **H.264** (camera was switched H.265→H.264 but the decoder wasn't updated). Decoder can't parse the stream → no frames.
- `width/height` set to `1920/1080` while the camera streams **1280×720**. RMS's pipeline caps never negotiate → stall.

**Fix.** Make `.config` match the actual stream: `gst_decoder: avdec_h264`, `width: 1280`, `height: 720`. Verify the real codec/resolution with `ffprobe -rtsp_transport tcp ... -show_entries stream=codec_name,width,height`. Compare against a known-good identical station.

**Status.** Found on Roit (gmnro001) 2026-05-30. The station last captured ~05-25, when the cameras were still H.265.

---

## 9. Missing GStreamer Python bindings → silent OpenCV fallback (unacceptable timestamps)

**Symptom.** RMS log: `Could not import Gst: Namespace Gst not available. Using OpenCV.` RMS keeps running but on the OpenCV capture backend, whose frame timestamping is too poor for meteor astrometry. `media_backend: gst` in `.config` is silently ignored.

**Diagnosis.** The RMS venv can't `import gi; gi.require_version('Gst','1.0')`. Two causes, both present on the stale original station:
- venv `pyvenv.cfg` has `include-system-site-packages = false` AND
- the system is missing the GI typelibs: `gir1.2-gstreamer-1.0` and `python3-gst-1.0` (only `python3-gi` was installed).

The config comment says gst "reverts to cv2" on error — but it reverts on an *import* failure, silently, with no error surfaced.

**Fix.** `sudo apt-get update && sudo apt-get install -y gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 python3-gst-1.0`. The venv's own pip `PyGObject` then finds the **system typelibs** via the default girepository path — so **keep the venv isolated** (`include-system-site-packages = false`) and verify `venv/bin/python -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None)"`. **Do NOT** set `include-system-site-packages = true` to fix this — see #10.

**Status.** Found on Roit (gmnro001) 2026-05-30.

---

## 10. `include-system-site-packages = true` drags in incompatible system matplotlib

**Symptom.** After flipping the venv to system-site-packages (to "get Gst"), RMS crashes on import with `ModuleNotFoundError: No module named 'matplotlib.tri.triangulation'` from `/usr/lib/python3/dist-packages/mpl_toolkits/...`.

**Diagnosis.** Exposing system site-packages lets the OLD system `mpl_toolkits` shadow the venv's modern matplotlib (the `matplotlib.tri.triangulation` submodule was renamed in newer matplotlib). The system mpl_toolkits then fails against the resolved matplotlib version.

**Fix.** Don't use system-site-packages for this. Install the GI **typelibs** at the system level (apt, see #9); the venv's pip PyGObject finds them regardless of the site-packages flag. Keep the venv isolated so its own matplotlib/numpy/etc. are used.

**Status.** Hit during the Roit remediation 2026-05-30; reverted the flag, kept venv isolated.

---

## 11. Legacy hand-rolled watchdog crash-loops RMS, keyed off a phantom camera

**Symptom.** RMS restarts every ~N minutes (`systemd: Stopping/Started rms-camX`), never surviving long enough to write an FF. Looks like a crash but systemd shows clean `Deactivated successfully` — i.e. something is *commanding* the restart, not RMS dying.

**Diagnosis.** The original/oldest stations have ad-hoc watchdog scripts in `~/` (NOT in `rovimen_scripts/`), e.g. `~/rms_watchdog.sh` (cron `*/5`) and `~/live_detector_watchdog.sh` (cron `*/3`). `rms_watchdog.sh` checks `RMS_data/cam1/CapturedFiles` for FFs in the last 5 min and "restarts ALL RMS" if none — but `cam1` is a **camera that was never installed** (phantom config entry). It can never produce FFs, so the watchdog restarts every camera forever. Compounded by #12 (slow startup), RMS never reaches stable capture.

**Fix.** Remove the stale `~/`-level watchdog crons (`crontab -l | grep -vE 'rms_watchdog|live_detector_watchdog' | crontab -`). The modern dawn/janitor crons in `/etc/cron.d/rovimen-*` replace their function. Find restart commanders by grepping `~`/cron for `systemctl restart rms`.

**Status.** Found on Roit (gmnro001) 2026-05-30 — the single biggest reason it wasn't capturing.

---

## 12. Station's public IP banned at `gmn.uwo.ca` → slow RMS startup + no uploads

**Symptom.** RMS startup hangs ~2 min per launch on `Establishing SSH connection to gmn.uwo.ca` → `IO error with key file: timed out`, `DownloadMask: Remote directory 'files' does not exist`. Uploads never succeed.

**Diagnosis.** From the station, `gmn.uwo.ca` (129.100.18.139) is unreachable on **every** port (22 and 443 both time out) while general internet and `github.com:22` work, and an identical sibling station reaches it fine. DNS resolves. → the station's **public IP is firewall-banned at UWO** (GMN runs fail2ban; repeated failed SSH from a stale/unregistered key gets the IP banned). Confirm with `getent hosts gmn.uwo.ca` (resolves) + `bash -c 'echo >/dev/tcp/github.com/22'` (open) vs `.../gmn.uwo.ca/22` (timeout).

**Fix.** Needs GMN-side action (the GMN server admin): unban the station's public IP and/or re-register its upload key. Local capture/calibration are unaffected — only uploads + the startup delay. Note: the startup delay can interact lethally with an aggressive watchdog (#11).

**Status.** Observed on one of our stations 2026-05-30 (banned after repeated SSH attempts with a stale key). Resolved by a GMN-side unban.

---

## 13. Diagnostic RTSP probes steal the camera's connection slot and confound verification

**Symptom.** RMS appears to capture 0 frames; standalone `gst-launch`/`ffprobe`/python-Gst tests against the camera "fail SETUP" or hang — "proving" the camera/gst is broken. But an identical known-good station behaves the same way under the same standalone test while its RMS captures fine.

**Diagnosis.** These XMEye/cheap cameras allow only a **few simultaneous RTSP connections**. Every manual probe (gst-launch, ffprobe, python appsink) competes with RMS for a slot — disrupting RMS's capture AND giving a false "broken" reading. A standalone `gst-launch rtspsrc ! ... ! fakesink` also unreliably "stalls at SETUP" even on working cameras, so it proves nothing.

**Fix.** **Verify capture via the filesystem** — count `FF_*.fits` in the live `CapturedFiles` dir and read RMS's own `BufferedCapture` log (`Buffer fill … Dropped frames: 0 … Grabbing a new block of 256 frames`). Do NOT open competing RTSP connections to "test" the camera while RMS is meant to be using it. ffmpeg/ffprobe is a fine one-shot codec check only when nothing else is connected.

**Status.** Cost significant time during a station remediation 2026-05-30 — the station was already capturing once the watchdog (#11) was gone; the probes masked it.

---

## 14. SkyFit2 launch gotchas (local workstation)

**Symptoms & fixes.**
- `No module named 'pyqtgraph'` / PyQt5 import fails → run SkyFit2 with the **RMS venv python** (`RMS/venv/bin/python -m Utils.SkyFit2 …`), not bare system `python3`.
- `AttributeError: 'Platepar' object has no attribute 'RA_d'` on load → the platepar is a default/uninitialized one (has `az_centre`/`alt_centre` but no `RA_d`/`dec_d`); some local RMS branches don't compute `RA_d` on read while the station's prerelease RMS does. Workaround: launch SkyFit2 **without** that platepar so it creates a fresh default (the calibration is a fresh fit anyway).
- Two SkyFit2 instances launched **simultaneously** → one dies silently after "Loading platepar templates". Launch them **one at a time** (let the first settle before the second).

**Status.** Observed 2026-05-30 calibrating Roit/Bacau.

---

## 15. Camera resets to DHCP or a drifted subnet → `enforce_camera_ip` can't find it

**Symptom.** After a firmware glitch or power event a camera stops answering at its pinned static IP. `fix_cam_encoding.sh` logs `No camera found … cannot enforce IP` and the station silently loses that camera until someone hunts it down by hand.

**Diagnosis.** `enforce_camera_ip`'s recovery was a unicast `nmap -p 34567` of the **expected `/24`**. If the camera fell back to DHCP on a different subnet (or APIPA `169.254.x`), it isn't in that `/24`, so the scan misses it. Worse, on a multi-camera station the scan took "the first camera on 34567 that isn't at the expected IP" — which can be a *different* camera, so enforcement could rewrite the wrong unit's address.

**Fix.** Identify the camera by **MAC** over a Sofia UDP broadcast (`:34569`, the `SearchXM` probe). Because it's a layer-2 broadcast the camera answers **regardless of its IP subnet**, and the reply's `NetWork.NetCommon.MAC` lets `enforce_camera_ip` pin the exact unit before rewriting its IP. Add an optional `camera_mac` to the station in `config.json` to enable it; the `nmap` scan stays as the fallback when no MAC is configured. Discovery lives in `rovimen-scripts/xm_discovery.py` (`python xm_discovery.py` to list every XM camera on the LAN, `--mac <MAC> --quiet` to resolve one).

**Status.** Added with the MAC-verified enforcement change.

---

## Adding new entries

Keep this catalog focused on failure modes that:
- Recur on multiple stations OR
- Took non-trivial time to root-cause OR
- Have a non-obvious workaround a future debugger would not guess

Anything covered by a short comment in code or a one-line note in the project docs belongs there, not here.
