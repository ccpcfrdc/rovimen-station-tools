# station-cam

Boot-time management for a station's XiongMai (Sofia/DVRIP) cameras. Two systemd
services keep every camera correctly **addressed** and **configured** with no
manual steps, and with **no dependence on the site's DHCP server or IP plan**.

- `cam-net.service` → `cam-net.sh` — brings up the private camera network
  (alias + NAT + per-camera NTP) at boot.
- `cam-enforce.service` → `cam_enforce.py` — at boot, finds each camera by **MAC**
  via a Sofia UDP broadcast (`:34569`, subnet-agnostic), pins its static IP, and
  re-applies its capture profile, writing only the fields that drifted.

Everything is driven by one file: `/etc/rovimen-cam/config.json`.

## Why a dedicated private network (not the site LAN + DHCP)

Cameras live on a private subnet (e.g. `10.42.0.0/24`) served by an **alias on the
station NIC**, with **static IPs** — not DHCP addresses from the site router:

1. **Stable addressing.** DHCP leases change on reboot; a camera that comes back
   on a new IP breaks RMS (`.config` still points at the old address) and capture
   is silently lost. Static IPs on our own subnet never change.
2. **Portable across sites.** The subnet is defined by the *station*, not the
   site router. Move the station anywhere and the camera IPs — and the RMS
   `.config` — stay identical. Nothing to reconfigure on arrival.
3. **Resilient to firmware drift.** XM cameras occasionally reset to DHCP or a
   random subnet. `cam_enforce` finds them by MAC over a layer-2 broadcast and
   pins them back automatically at every boot.
4. **Isolation & security.** XM firmware is insecure. On a private subnet with no
   gateway the cameras cannot reach the internet (NAT is added only for NTP) and
   are not exposed on the site LAN. The station is the only bridge.
5. **Single source of truth.** One `config.json` defines the cameras and the
   network — no dependency on the site's DHCP server, address plan, or router.

Trade-off: to view a camera from another machine you tunnel through the station
(`ssh -N -L 5540:10.42.0.10:554 station`), the same isolation model GMN/ROVIMEN
stations already use.

## Layout

```
station-cam/
├── install.sh                    # copies everything to system locations
├── requirements.txt              # venv deps (tiny; dvrip is vendored)
├── config/
│   ├── profiles.json             # capture profiles — installs to /etc, refreshed each install
│   ├── config.example.json       # station seed (cameras/network placeholders)
│   └── dashboard.example.json    # dashboard settings seed (bind/port/stream)
├── scripts/                      # cam_enforce.py, cam-net.sh, cam_ip.py, cam_find.py, cam_profiles_update.py,
│                                 #   cam_recovery_test.py, cam_help.py, cam_dashboard.py, hls.min.js, dvrip.py (vendored)
└── services/                     # cam-net.service, cam-enforce.service, cam-sync.{service,path}, cam-dashboard.service
```

## Install (as a normal user with sudo — no root login needed)

```bash
bash install.sh
sudo $EDITOR /etc/rovimen-cam/config.json     # set your camera MACs + IPs
sudo systemctl start cam-net cam-enforce
```

The installer auto-detects the network interface the camera alias lives on — the
NIC carrying the default route (the station uplink, where `cam-net` also adds the
alias and NAT). This makes it portable across hosts (`eno1`, `eth0`, `enp3s0`, …)
with no editing. Override it only when the camera network must sit on a *different*
NIC than the uplink:

```bash
bash install.sh --iface eth1        # pin the alias on eth1 instead
```

The chosen interface is written to `config.json` (`"iface"`), the single source of
truth that both `cam-net` and the netplan pin read.

Installs to:

| From | To |
|------|----|
| `scripts/*` | `/usr/local/lib/rovimen-cam/` (+ isolated `.venv`) |
| `config/profiles.json` | `/etc/rovimen-cam/profiles.json` (delivered, refreshed each install) |
| CLI wrappers | `/usr/local/bin/cam-{net,enforce,sync,profiles-update,reboot,find,ip,recovery-test,help,dashboard}` |
| `config/config.example.json` | `/etc/rovimen-cam/config.json` (station cameras, only if absent) |
| `services/*` | `/etc/systemd/system/` (run as root) |
| generated | `/etc/netplan/99-rovimen-cam.yaml` — pins the camera alias so systemd-networkd keeps it across DHCP renews / networkd restarts (Ubuntu/netplan only) |

## Uninstall

```bash
bash uninstall.sh          # remove tooling + services, keep /etc/rovimen-cam config
bash uninstall.sh --purge  # also remove the station config
```
Stops and disables the services, brings the camera network down (alias + NAT),
and removes the CLI wrappers, the library, and the systemd units. The cameras
keep their static IP and profile (stored in their own flash) — they are just no
longer reachable from this host once the alias is gone.

## Dependencies

`dvrip.py` (the Sofia/DVRIP client) is **vendored** and uses only the standard
library — nothing to pull. `install.sh` still creates an isolated `.venv` (with
`pydantic`, best-effort) so the services never depend on the system Python.

**Supported systems** — the same ones ROVIMEN/RMS runs on: Ubuntu 24.04 LTS
(Desktop/Server), Debian 11/12, and Raspberry Pi OS 64-bit. All are Debian-based,
so `install.sh` best-effort `apt-get`s the few things it needs when they are
missing: `python3-venv` (not preinstalled on Server/Debian/Pi OS — needed for the
`.venv`), `iproute2` and `iptables` (for the camera network), and it flags
`ffmpeg` if absent (needed only by `cam-dashboard`). The Python is stdlib-only and
runs on 3.9+, so no version pin is required.

## Usage

```bash
cam-help                     # full command reference (a mini man page)
sudo cam-find                # scan the LAN, tag known cameras by MAC
sudo cam-enforce             # re-assert IPs + profiles now (discovery + IP + profile)
sudo cam-sync                # push config profiles to the cameras (no discovery/IP)
sudo cam-reboot              # reboot every camera in config
sudo cam-net status          # show alias / NAT / configured cameras
sudo cam-ip 10.42.0.10 show  # inspect one camera's network config
```

Encoding changes (resolution / CBR / compression) only take effect after a camera
reboot, so `cam-sync` and `cam-enforce` **reboot a camera automatically when they
change its encoding** — live-applied fields (gain, exposure, OSD, colour) do not
trigger a reboot. Use `cam-reboot` to force a reboot regardless.

## Profiles vs station config (two separate things, on purpose)

Both live in `/etc/rovimen-cam/`, but they are handled differently:

- **Capture profiles** (`/etc/rovimen-cam/profiles.json`) are **delivered by this
  repo** — `install.sh` overwrites them on every run, so you never hand-edit them on a
  station; they always track the repo. This is the curated end-state of the GMN `init`
  profile plus the ROVIMEN tuning (colour, CBR, IR-cut, …), version-controlled in one
  file (`config/profiles.json`).
- **Station config** (`/etc/rovimen-cam/config.json`) holds only what is specific to
  this station: `cameras`, `network`, `iface`, `ntp`. `install.sh` seeds it once and
  **never overwrites** it. You set the cameras once.

### Auto-apply (opt-in)
Enable a path unit that runs `cam-sync` whenever the station config changes:
```bash
sudo systemctl enable --now cam-sync.path
```

## Changing a capture profile and rolling it out

The agreed capture profile lives in one version-controlled file,
`config/profiles.json`, and is delivered to every station from the repo. Changing
it is a plain git flow — the diff records exactly what changed, when, and by whom.

**1. Edit the canonical profile in the repo** (a clone on your workstation):
```bash
cd <repo>/station-cam
$EDITOR config/profiles.json        # change a field, or add a whole new named profile
python3 -m json.tool config/profiles.json >/dev/null && echo "JSON valid"
```

**2. Commit and push** (to your fork, or open a PR upstream once agreed):
```bash
git add config/profiles.json
git commit -m "profiles: <what changed and why>"
git push <remote> HEAD:<branch>
```

**3. Roll out to each station** — pull, refresh, apply:
```bash
cd ~/rovimen-station-tools && git pull && cd station-cam && bash install.sh
sudo cam-profiles-update --apply
```
- `install.sh` copies the new `config/profiles.json` to `/etc/rovimen-cam/profiles.json`.
- `cam-profiles-update --apply` lists the delivered profiles and runs `cam-sync`,
  which writes only the changed fields to each camera (rebooting a camera only if
  its **encoding** changed).

```
edit config/profiles.json (repo)  ──commit+push──▶  repo
                                                     │  git pull + install.sh
                                                     ▼
                              /etc/rovimen-cam/profiles.json (station, read live)
                                                     │  cam-profiles-update --apply
                                                     ▼
                                                  cameras
```

**Notes**
- **Never edit `/etc/rovimen-cam/profiles.json` on a station** — `install.sh`
  overwrites it on the next pull. The source of truth is always `config/profiles.json`
  in the repo.
- `config.json` (the station's cameras) is **not touched** by a profile change.
- **Roll out to one station first**, verify (`cam-sync` shows what changed, or
  `cam-recovery-test profiles --yes` validates the whole apply), then do the rest.
- To switch a single camera to a different named profile, change its `profile` field
  in that station's `config.json` and run `sudo cam-sync`.

## Live dashboard (optional)

`cam-dashboard` serves a local web page with a grid of live players for the
cameras. It transcodes each camera's RTSP to HLS on demand with ffmpeg
(H.264 copy — near-zero CPU) and stops a stream once it is unwatched. Pure stdlib
plus a vendored `hls.js`; no extra deps.

It is a systemd service, **disabled by default**:
```bash
sudo systemctl enable --now cam-dashboard
```
The page is a table of the cameras with **Play** (live HLS in a modal), **Config**
(network/encode/param read live over Sofia), **Rotate 180°** and **Reboot** per
camera, plus an **Orientation** column (normal / 180° / flip / mirror) and a **Toate
camerele** button that shows every camera in one grid. Rotate 180° flips the image
live *and* writes `rotate180` into `config.json`, so `cam-enforce` keeps it.
Configure it via `/etc/rovimen-cam/dashboard.json` (station-local, seeded once):

| key | meaning | default |
|-----|---------|---------|
| `bind` | IP (`0.0.0.0`, `127.0.0.1`, tailnet IP) or interface name (`eno1`, `tailscale0`) | `0.0.0.0` |
| `port` | listen port | `8080` |
| `stream` | `0` main, `1` substream. The meteor profile turns the substream **off**, so use `0`; a 2nd main pull is only bandwidth, not a 2nd encode | `0` |
| `idle_timeout` | seconds before an unwatched stream's ffmpeg is stopped | `30` |

There is **no authentication** — restrict exposure with `bind` (localhost or a
tailnet interface) or reach it through your SSH/Cloudflare tunnel:
```bash
ssh -N -L 8080:127.0.0.1:8080 station    # then open http://127.0.0.1:8080
```

## Add a camera

Easiest — `cam-add` writes the config entry and enforces it for you:
```bash
sudo cam-add                       # scan for cameras not in config, prompt for IP/profile
sudo cam-add aa:bb:cc:dd:ee:ff 10.42.0.11 rovimen   # or scriptable
```
Or by hand: add one entry under `cameras` in `/etc/rovimen-cam/config.json`
(`"<MAC>": {"ip": "10.42.0.11", "gw": "10.42.0.1", "mask": "255.255.255.0", "profile": "rovimen"}`)
then `sudo cam-enforce`. Either way, all the tools pick it up automatically.

### Image orientation (per camera)

A camera mounted **upside-down** can be corrected in software — no roof visit. Add
an optional orientation field to that camera's entry in `config.json` and
`cam-enforce`/`cam-sync` will apply it (and re-apply it after any firmware reset):

| field | meaning |
|-------|---------|
| `"rotate180": true` | 180° — the upside-down mount case (sets both mirror + flip) |
| `"mirror": true` | horizontal flip only (left↔right) |
| `"flip": true` | vertical flip only (up↕down) |

```json
"00:12:43:3b:71:41": {"ip": "10.42.0.3", "gw": "10.42.0.1", "mask": "255.255.255.0",
                      "profile": "rovimen", "rotate180": true}
```

The field is **optional**: cameras without it are left exactly as they are (the
enforcer never resets an orientation you set by hand). `mirror`/`flip` override the
`rotate180` shorthand if you need a single-axis flip. Orientation lives in the
camera's own flash, applies live (no reboot), and is unaffected by profile changes.
