# ROVIMEN Station Deployment — How It Works

## Overview

There are two distinct deployment targets:
1. **VPS (dashboard + station bundles)** — handled by CI automatically on push
2. **Stations (scripts + services + config)** — handled by `rovimen-deploy.sh`, run manually once per station

---

## Part 1 — CI / VPS Deployment (automatic)

Every push to `development` or `main` triggers a GitHub Actions workflow. Each branch deploys a full dashboard **and** a station bundle to the VPS — `development` to the dev tier, `main` to prod. The two workflows are symmetric:

- **`deploy-dev.yml`** (development branch) → dev dashboard to `/opt/rovimen-dev/` (restarts `rovimen-dashboard-dev.service`, served behind nginx vhost `:17778`) **and** the dev station bundle to `/opt/rovimen/station-bundle-dev/`
- **`deploy-prod.yml`** (main branch) → prod dashboard to `/opt/rovimen/` (restarts `rovimen-dashboard.service`) **and** the main station bundle to `/opt/rovimen/station-bundle/`, then rebuilds `rovimen-deploy.sh` (prod-only step)

The VPS station bundle is what stations pull from when auto-updating. It contains all `.py`/`.sh` scripts plus a `.version` file. Test stations opt into the dev bundle by setting `"update_channel": "dev"` in their `config.json`.

> **Do not tear down the dev tier when syncing branches.** "Sync development and main" is a content operation only. The dev workflow, dev dashboard service, dev deploy directory, and the dev bundle channel must all stay intact.

---

## Part 2 — Building the Deploy Package (manual, done by developer)

The deploy package is a single self-extracting bash script: `rovimen-deploy.sh` in the repo root.

Built with:
```bash
python3 deployment/build_deploy.py
```

What it does:
- Reads `deployment/rovimen_install.sh` (the interactive installer)
- Reads all 23 scripts listed in `SCRIPTS` from `rovimen-scripts/`
- Reads 3 service files (`color-capture`, `rovimen-station-api`, `camera-focus`)
- Reads 2 assets (logo PNG, VCR font)
- Base64-encodes everything and concatenates into a single bash file with an embedded extraction header

The output script is self-contained — copy it to any station and it carries everything with it. No internet access required at install time.

After building, copy to the station:
```bash
scp -i ~/.ssh/<your_station_key> rovimen-deploy.sh <user>@<station_ip>:~/rovimen-deploy.sh
```

---

## Part 3 — Running the Installer on a Station

```bash
bash ~/rovimen-deploy.sh
```

The deploy script extracts all files to a temp dir, then runs `rovimen_install.sh`. The installer is split into phases:

### Phase 0 — Preflight
- Checks OS, Python, sudo access
- Detects VAAPI hardware encoding capability
- Detects existing `~/rovimen_scripts/` — if found with a `config.json`, asks whether to keep it or reconfigure from scratch
- If keeping config: skips phases 2–5, goes straight to file installation

### Phase 1 — Hardware Probe
- Runs `hardware_assessment.sh` to inventory CPU, RAM, disk, GPU
- Writes `~/rovimen_scripts/hardware.json`

### Phase 2 — Capture Drive
- Asks where color video should be stored (default: `/mnt/data/color_capture`)
- Validates the path exists and has enough space

### Phase 3 — Camera Discovery
- Pre-reads all `~/RMS_cam*/.config` files to extract: camera IPs → station codes (e.g. `192.168.1.240 → RO000H`), plus station lat/lon/elevation
- Pre-reads any `platepar_cmn2010.cal` files to extract az/alt pointing angles per camera
- Scans the local subnet for devices with port 554 open (RTSP cameras)
- For each discovered IP, pre-fills the station code from RMS config — user must confirm or override; blank re-prompts, must type `skip` to exclude
- Asks if each camera is mounted upside-down
- Also allows manually adding cameras not found by the scan

### Phase 4 — RMS Path Mapping
- Matches each configured camera code to an `~/RMS_data/camN/` directory
- Handles the case where CapturedFiles don't exist yet (new cameras)

### Phase 5 — Config Review & Edit
Interactive menu loop. Runs all sections once sequentially, then shows a summary and lets the user revisit any section before confirming.

Sections:
- **Station info** — label, location, network name
- **Services** — per-service on/off toggles with dependency logic:
  - Detection lock (required for locked retention and quality encoding)
  - Stacker (required for timelapse; if off, timelapse is forced off)
  - Timelapse build (only shown if stacker is on)
  - Encoder (if off: encoder section is skipped, compression defaults to 1)
  - Overlay (forced off if both encoder and stacker are off; if off: overlay section hidden)
  - Station API (if disabled: station unreachable from dashboard)
  - Archive upload (rsync to VPS)
- **Encoder** — compression level 1–4 (only shown if encoder is on)
- **Retention** — how many days to keep color clips, stacks, timelapses
- **Overlay** — style (cinema/standard), which components to show, opacity; live ASCII preview in terminal (only shown if overlay is on)
- **Update channel** — `main` (stable) or `development` (testing)

### Phase 6 — File Installation
- Copies all scripts to `~/rovimen_scripts/`
- Writes `config.json` from the values collected in phases 2–5
- Installs service files to `/etc/systemd/system/` with user/path substitution
- Runs `systemctl daemon-reload`
- Disables any legacy `rovimen-nightwatcher` service if present
- Does **not** start any services or install any crons — that is `toggle_rovimen.sh`'s job

### Phase 7 — Summary
- Shows installed services (inactive at this point)
- Shows useful commands
- Shows next steps

---

## Part 4 — Activating the Station

After the installer completes:

```bash
bash ~/rovimen_scripts/toggle_rovimen.sh on
```

This:
- Enables and starts: `color-capture`, `rovimen-station-api`, `camera-focus`
- Installs `/etc/cron.d/rovimen-dawn` — morning pipeline (`dawn_process.py`, every 15 min 04:00–10:00 UTC)
- Installs `/etc/cron.d/rovimen-janitor` — disk watchdog (every 10 min)
- Installs `/etc/cron.d/rovimen-reboot` — reboot guard (daily at 12:00 UTC)
- Installs user crontab entries: `fix_cam_encoding @reboot`, `updater.sh` (12:30, 13:15, 14:00 UTC)

`toggle_rovimen.sh off` reverses all of the above. `restart` restarts running services only. `status` shows current state of all services and crons.

---

## Part 5 — Auto-Updater (ongoing)

Once the station is live, the updater cron runs three times daily. Each run:
1. Skips if `color-capture` is active (capturing) or `dawn_process.py` is running (morning processing)
2. SSHes to the VPS and reads the bundle's `.version` file
3. If newer than local: `rsync` pulls all scripts, excluding `config.json`
4. Runs `config_migrate.py` to add any new config fields while preserving existing values
5. Restarts any currently-running services

The station's `update_channel` in `config.json` controls whether it pulls from the `main` or `development` bundle.

Manual update: `bash ~/rovimen_scripts/updater.sh`

---

## Part 6 — Post-Install Calibration Steps

These are not automated and must be done manually after deployment:

1. **Platepars** — run SkyFit2 for each camera to calibrate pointing angles. After calibration:
   ```bash
   python3 ~/rovimen_scripts/update_config_coords_from_platepar.py
   ```
   This reads `az_centre`/`alt_centre` from each `platepar_cmn2010.cal` and updates `config.json`.

2. **Color calibration gains** — derive per-camera RGB gains and add to `config.json`

3. **Dashboard** — add station entry to `dashboard_config.yaml` on the VPS

4. **Telegram** — add `telegram_token` and `telegram_chat_id` to `config.json` if alerts are wanted

---

## Key Files at a Glance

| File | Purpose |
|------|---------|
| `deployment/build_deploy.py` | Builds `rovimen-deploy.sh` |
| `deployment/rovimen_install.sh` | Interactive installer (embedded in deploy script) |
| `rovimen-deploy.sh` | Self-extracting deploy package (built artefact) |
| `rovimen-scripts/toggle_rovimen.sh` | Start/stop/status all services and crons as a unit |
| `rovimen-scripts/updater.sh` | Pulls latest scripts from VPS bundle |
| `rovimen-scripts/config_migrate.py` | Adds new config fields on update, preserves existing values |
| `rovimen-scripts/dawn_process.py` | Morning pipeline (triggered by cron after RMS finishes) |
| `.github/workflows/deploy-prod.yml` | CI: `main` → prod dashboard (`/opt/rovimen/`) + main bundle + `rovimen-deploy.sh` rebuild |
| `.github/workflows/deploy-dev.yml` | CI: `development` → dev dashboard (`/opt/rovimen-dev/`) + dev bundle |
