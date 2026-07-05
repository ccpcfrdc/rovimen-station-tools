# Network Setup

How a multi-station meteor camera network is composed, how the pieces reach
each other, and how to configure Tailscale so the dashboard can talk to every
station without exposing anything to the public internet.

This reflects a real deployment (15+ stations across two countries, multiple
operators, consumer ISPs, CGNAT, no port forwarding anywhere). Adapt names and
addresses to your own network.

---

## 1. Network composition

Three layers, strictly separated:

```
                        PUBLIC INTERNET
                              |
                     +--------+--------+
                     |   VPS (cloud)   |   dashboard :7777 (behind nginx/TLS)
                     |  dashboard +    |   public API /api/public/v1/*
                     |  media archive  |   media /media/v1/*
                     +--------+--------+
                              |
                    TAILSCALE OVERLAY (tailnet)
                              |
        +---------------------+---------------------+
        |                     |                     |
 +------+------+       +------+------+       +------+------+
 | station A   |       | station B   |       | station N   |
 | station API |       | station API |       | station API |
 |   :7779     |       |   :7779     |       |   :7779     |
 +------+------+       +------+------+       +------+------+
        |                     |                     |
   CAMERA LAN            CAMERA LAN            CAMERA LAN
  (192.168.x.0/24,      (isolated, static     (cameras have NO
   RTSP :554 only)       IPs, no internet)     default gateway)
```

- **Camera LAN** — each station PC has a second NIC (or a dedicated switch
  segment) with the IP cameras on static addresses. Cameras speak RTSP (:554)
  to the station PC only. Give cameras **no default gateway / no DNS** so they
  physically cannot reach the internet — IP camera firmware should never be
  internet-facing.
- **Tailnet** — every station PC and the VPS join one Tailscale network. All
  dashboard↔station traffic (API proxying, media pulls, SSH) runs over it.
  Works through CGNAT and consumer routers with zero port forwarding.
- **Public internet** — only the VPS is reachable publicly, and only through
  nginx/TLS in front of the dashboard. Stations are never exposed.

## 2. Ports

| Port | Where | What | Exposure |
|------|-------|------|----------|
| 554  | cameras | RTSP video | camera LAN only |
| 8554 | station (optional) | local RTSP relay (MediaMTX) — one camera pull shared by capture + colour capture | localhost only |
| 7779 | station | station REST API (`rovimen_station_api.py`) | tailnet only |
| 7777 | VPS | dashboard (Flask/gunicorn) | behind nginx/TLS |
| 22   | stations + VPS | SSH (key-only) | tailnet only |

Recommended firewall on stations (UFW): default deny incoming, then
`ufw allow in on tailscale0` — the station API and SSH become reachable from
the tailnet only, and nothing listens on the home LAN side.

## 3. Tailscale setup

### Tags and ACLs

Use tags, not user identities, for machines. A minimal ACL policy:

```jsonc
{
  "tagOwners": {
    "tag:station":   ["autogroup:admin"],
    "tag:dashboard": ["autogroup:admin"]
  },
  "acls": [
    // dashboard -> every station: API + SSH
    { "action": "accept",
      "src":    ["tag:dashboard"],
      "dst":    ["tag:station:7779", "tag:station:22"] },

    // admins -> everything
    { "action": "accept",
      "src":    ["autogroup:admin"],
      "dst":    ["*:*"] }
  ],
  "ssh": [
    { "action": "check",
      "src":    ["autogroup:admin"],
      "dst":    ["tag:station", "tag:dashboard"],
      "users":  ["autogroup:nonroot"] }
  ]
}
```

Deliberately absent: any `station -> station` rule. Stations never need to
talk to each other, and denying it means one compromised station cannot pivot
across the fleet. Add rules per *need*, not per convenience — this is the
single most valuable property of the ACL.

Join a station with its tag:

```
sudo tailscale up --authkey tskey-auth-PASTE_YOURS --advertise-tags=tag:station
```

Use **pre-authorized, tagged, expiring auth keys** for onboarding, and enable
key expiry for people, disable it for tagged machines (or re-auth on a
schedule you control).

### Cross-tailnet stations (jump hosts)

If some stations live in a different tailnet (a partner operator's network,
a legacy setup), share ONE well-connected station from that tailnet into
yours, then declare it as a jump host in `dashboard_config.yaml`:

```yaml
stations:
  remote-station:
    host: 100.64.0.42        # not directly reachable from the dashboard
    jump_hosts:
      - 100.64.0.7           # shared node; dashboard reaches remote via SSH tunnel
```

The dashboard opens an SSH tunnel through the jump host automatically and
fails over between multiple jump hosts if listed. Prefer moving stations into
one tailnet when you can — jump hosts add a serial dependency.

## 4. Camera LAN conventions

- Static IPs from a plan (e.g. `.65`, `.66`, `.67`… for cameras, station PC
  on `.1`), documented per station. DHCP for cameras eventually bites you.
- Wired ethernet only for cameras. Never WiFi — RTSP does not tolerate
  roaming/retransmits, and diagnosing it wastes nights.
- Watch the link speed: a single 10 Mbps negotiation (bad cable, old switch)
  is saturated by two RTSP pulls and silently drops frames. `ethtool <if>`
  should say 100Mb/s or better. If you need the same camera twice (detection
  + colour capture), pull it ONCE through a local RTSP relay (see
  `rovimen-scripts/setup_rtsp_relay.sh`) instead of opening two camera streams.
- Camera settings that matter: CBR (not VBR), H.264, moderate GOP, noise
  reduction off, and the firmware **AutoReboot scheduled during daytime** —
  the factory default can reboot mid-capture at night (see below).

## 5. Frequent problems

The full catalog with diagnosis walkthroughs lives in
[`known_failure_modes.md`](known_failure_modes.md). The ones that hit every
new deployment:

**Station shows offline on the dashboard, but the PC is up.**
`tailscale status` on the station — if it shows `offline` or only DERP-relayed
connections, restart `tailscaled` and check the auth key/expiry. Then verify
the station API: `curl http://localhost:7779/api/status` locally, and from the
dashboard host `curl http://<tailscale-ip>:7779/api/status`. If local works
and remote doesn't, it's ACLs or the station firewall.

**Nightly frame drops in bursts, same wall-clock time every night/hour.**
Almost always the camera firmware's AutoReboot timer firing during capture,
made worse by unsynchronised camera clocks that drift into the night. Set
AutoReboot to a daytime hour on every camera and sync camera clocks
periodically. Do not start by blaming the network.

**Frame drops all night, no pattern.**
Check the camera link speed (`ethtool`), then whether two processes pull the
same camera (use the RTSP relay), then CPU (a GStreamer decode deficit shows
as steadily growing drops on multi-camera hosts).

**Camera unreachable from the station.**
`ping` the camera IP, then `nc -zv <ip> 554`. If ARP fails, it's cabling/PoE/
switch — not software. Cameras that lose power during a storm sometimes come
back on DHCP; this is why static IPs and a written per-station IP plan matter.

**Dashboard is slow / 503s with several stations.**
One gunicorn worker cannot proxy a fleet. Raise `workers` in
`dashboard/gunicorn.conf.py`, and front the dashboard with nginx for TLS and
static/media caching.

**Uploads to the GMN server fail sporadically for multi-camera stations.**
Stagger `upload_delay` per camera — simultaneous SFTP connections from one IP
can trip the server's fail2ban and get the station banned for hours.

## 6. What stays private

Never commit to a public repo, and never reuse across operators:

- `dashboard/dashboard_config.yaml` (real station IPs, SSH users) — gitignored
  here on purpose; only the `.example.yaml` ships.
- Tailscale auth keys, ACL files with real tailnet names, `users.yaml`,
  `api_keys.yaml`.
- SSH private keys — generated per station, they never leave the station.
- Camera RTSP credentials.
