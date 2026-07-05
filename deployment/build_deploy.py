#!/usr/bin/env python3
"""build_deploy.py — Builds the self-extracting rovimen-deploy.sh installer.

Bundles rovimen_install.sh + all station scripts + service files + assets into a
single portable bash script that can be run on any GMN station with:
    bash rovimen-deploy.sh

Usage:
    cd rovimen-station-tools
    python deployment/build_deploy.py        # writes rovimen-deploy.sh
    python deployment/build_deploy.py --check # dry-run: list what would be bundled
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
SRC = REPO_ROOT / "rovimen-scripts"
OUTPUT = REPO_ROOT / "rovimen-deploy.sh"

# Scripts copied to ~/rovimen_scripts/ on the station.
# Must match the skip-list in rovimen_install.sh phase0_preflight().
SCRIPTS: list[str] = [
    "celestial_dome.py",
    "color_calibration.py",
    "color_capture.py",
    "dawn_process.py",
    "encoder.py",
    "stacker.py",
    "station_api.py",
    "janitor_storage_watchdog.py",
    "overlay.py",
    "rovimen_lock.py",
    "detection_lock.py",
    "archive_upload.py",
    "camera_focus.py",
    "timelapse_build.py",
    "unlock_old_locks.py",
    "updater.sh",
    "hardware_assessment.sh",
    "flags_manager.py",
    "reboot_guard.sh",
    "config_migrate.py",
    "config_defaults.json",
    "build_gstreamer_1223.sh",
    "fix_cam_encoding.sh",
    "toggle_rovimen.sh",
    "update_config_coords_from_platepar.py",
    "detection_indexer.py",
    "rovimen_pusher.py",
    "command_verify.py",
]

# Optional files shipped only if present in rovimen-scripts/. The command-channel
# server public key (docs/reversed_http_push_design.md §3, §8): it ships *in the
# signed deploy bundle* rather than being fetched from the VPS, avoiding the
# chicken-and-egg of distributing a trust anchor over the very channel we are
# hardening. Absent -> the station's command worker fails closed (never acts).
OPTIONAL_SCRIPTS: list[str] = [
    "command_pubkey.pem",
]

# Service files installed to /etc/systemd/system/ (path/user substituted by installer).
# User-level services (e.g. archive-upload.service) go to ~/.config/systemd/user/.
SERVICES: list[str] = [
    "color-capture.service",
    "rovimen-station-api.service",
    "camera-focus.service",
    "archive-upload.service",
    "detection-indexer.service",
    "rovimen-pusher.service",
]

# Assets copied to ~/rovimen_scripts/assets/ and ~/rovimen_scripts/fonts/.
ASSETS: list[tuple[str, str]] = [
    ("assets/astromania_text.png",   "assets/astromania_text.png"),
    ("assets/gmn_sphere.png",        "assets/gmn_sphere.png"),
    ("assets/ab1.png",               "assets/ab1.png"),
    ("fonts/VCR_OSD_MONO_1.001.ttf", "fonts/VCR_OSD_MONO_1.001.ttf"),
]

INSTALLER = "rovimen_install.sh"
DASHBOARD_CONFIG = REPO_ROOT / "dashboard" / "dashboard_config.yaml"
STATION_CONFIGS_DIR = REPO_ROOT / "station_configs"


def _build_rotate_lookup() -> dict[str, bool]:
    """Build camera_code -> rotate lookup from dashboard_config.yaml.

    Returns a dict mapping e.g. 'RO000M' -> False, 'RO000H' -> True.
    """
    if not DASHBOARD_CONFIG.exists():
        return {}
    raw = yaml.safe_load(DASHBOARD_CONFIG.read_text())
    lookup: dict[str, bool] = {}
    for _host_key, station in (raw.get("stations") or {}).items():
        for cam in station.get("cameras") or []:
            code = cam.get("code")
            if code:
                lookup[code] = cam.get("rotate", False)
    return lookup


def sync_rotate_flags() -> int:
    """Sync the rotate flag in every station_configs/*/config.json to match dashboard_config.yaml.

    Returns the number of cameras whose flags were updated.
    """
    rotate_lookup = _build_rotate_lookup()
    if not rotate_lookup:
        print("Warning: no cameras found in dashboard_config.yaml", file=sys.stderr)
        return 0

    updated = 0
    for config_path in sorted(STATION_CONFIGS_DIR.glob("*/config.json")):
        with open(config_path) as f:
            cfg = json.load(f)

        stations = cfg.get("stations", {})
        changed = False
        for cam_code, cam_cfg in stations.items():
            if cam_code not in rotate_lookup:
                continue
            expected = rotate_lookup[cam_code]
            current = cam_cfg.get("rotate")
            if current != expected:
                cam_cfg["rotate"] = expected
                changed = True
                updated += 1
                print(f"  {config_path.parent.name}/{cam_code}: rotate {current} → {expected}")

        if changed:
            with open(config_path, "w") as f:
                json.dump(cfg, f, indent=4)
                f.write("\n")

    return updated


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _section(name: str, path: Path) -> str:
    return f"### {name} ###\n{_b64(path)}\n"


def _git_version() -> str:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, text=True,
        ).strip()
        return sha
    except Exception:
        return "unknown"


PREAMBLE = """\
#!/usr/bin/env bash
# rovimen-deploy.sh — Self-extracting ROVIMEN station installer
# Generated: {date}  rev: {rev}
#
# Run as the station user (gmn, raul, thor, ...):
#   bash rovimen-deploy.sh
#
# What it installs:
#   - ~/rovimen_scripts/  (all station Python scripts + assets)
#   - /etc/systemd/system/  (color-capture, rovimen-station-api, camera-focus)
#   - /etc/cron.d/  (rovimen-morning, rovimen-janitor)
#   - Sudoers rule for passwordless service restarts
#
# Existing ~/rovimen_scripts/config.json is preserved (you will be asked).

set -euo pipefail

# ── Self-extraction ───────────────────────────────────────────────────────────
_extract() {{
    local name="$1" dest="$2"
    awk -v name="$name" '
        $0 == "### " name " ###" {{ found=1; next }}
        found && /^### .* ###$/ {{ exit }}
        found {{ print }}
    ' "$0" | base64 -d > "$dest"
}}

PAYLOAD_DIR=$(mktemp -d)
trap 'rm -rf "$PAYLOAD_DIR"' EXIT

mkdir -p "$PAYLOAD_DIR/assets" "$PAYLOAD_DIR/fonts"

# Extract installer
_extract "rovimen_install.sh" "$PAYLOAD_DIR/rovimen_install.sh"
chmod +x "$PAYLOAD_DIR/rovimen_install.sh"

# Extract scripts
{extract_scripts}
chmod +x "$PAYLOAD_DIR"/*.sh 2>/dev/null || true

# Extract service files
{extract_services}

# Extract assets
{extract_assets}

# ── Run installer ─────────────────────────────────────────────────────────────
echo "rovimen-deploy.sh  rev:{rev}  $(date -u '+%Y-%m-%d %H:%M UTC')"
echo ""
bash "$PAYLOAD_DIR/rovimen_install.sh"

exit 0
# ══════════════════════════════════════════════════════════════════════════════
# PAYLOAD — base64-encoded files (do not edit below this line)
# ══════════════════════════════════════════════════════════════════════════════
"""


def build(check: bool = False) -> None:
    # Sync rotate flags from dashboard_config.yaml into station_configs before bundling.
    n = sync_rotate_flags()
    if n:
        print(f"Synced {n} rotate flag(s) from dashboard_config.yaml → station_configs/")

    missing = []

    installer_path = REPO_ROOT / "deployment" / INSTALLER
    if not installer_path.exists():
        missing.append(str(installer_path))

    script_paths: list[tuple[str, Path]] = []
    for name in SCRIPTS:
        p = SRC / name
        if p.exists():
            script_paths.append((name, p))
        else:
            missing.append(str(p))

    # Optional files: bundled if present, silently skipped if not (e.g. the
    # command-channel server public key ships only once the keypair is minted).
    for name in OPTIONAL_SCRIPTS:
        p = SRC / name
        if p.exists():
            script_paths.append((name, p))

    service_paths: list[tuple[str, Path]] = []
    for name in SERVICES:
        p = SRC / name
        if p.exists():
            service_paths.append((name, p))
        else:
            missing.append(str(p))

    asset_paths: list[tuple[str, str, Path]] = []
    for src_rel, dest_rel in ASSETS:
        p = SRC / src_rel
        if p.exists():
            asset_paths.append((src_rel, dest_rel, p))
        else:
            missing.append(str(p))

    if missing:
        print("Missing files:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)

    if check:
        print(f"Installer:  {installer_path}")
        print(f"\nScripts ({len(script_paths)}):")
        for name, p in script_paths:
            print(f"  {p.stat().st_size:>8} B  {name}")
        print(f"\nServices ({len(service_paths)}):")
        for name, p in service_paths:
            print(f"  {p.stat().st_size:>8} B  {name}")
        print(f"\nAssets ({len(asset_paths)}):")
        for src_rel, dest_rel, p in asset_paths:
            print(f"  {p.stat().st_size:>8} B  {src_rel} → {dest_rel}")
        print(f"\nOutput: {OUTPUT}")
        return

    rev = _git_version()
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    extract_scripts = "\n".join(
        f'_extract "{name}" "$PAYLOAD_DIR/{name}"'
        for name, _ in script_paths
    )
    extract_services = "\n".join(
        f'_extract "{name}" "$PAYLOAD_DIR/{name}"'
        for name, _ in service_paths
    )
    extract_assets = "\n".join(
        f'_extract "{src_rel}" "$PAYLOAD_DIR/{dest_rel}"'
        for src_rel, dest_rel, _ in asset_paths
    )

    preamble = PREAMBLE.format(
        date=date,
        rev=rev,
        extract_scripts=extract_scripts,
        extract_services=extract_services,
        extract_assets=extract_assets,
    )

    sections: list[str] = []
    sections.append(_section(INSTALLER, installer_path))
    for name, p in script_paths:
        sections.append(_section(name, p))
    for name, p in service_paths:
        sections.append(_section(name, p))
    for src_rel, _dest_rel, p in asset_paths:
        sections.append(_section(src_rel, p))

    output = preamble + "\n".join(sections)
    OUTPUT.write_text(output)
    OUTPUT.chmod(0o755)

    size_kb = OUTPUT.stat().st_size // 1024
    print(f"Built {OUTPUT}  ({size_kb} KB)  rev:{rev}")
    print(f"  {len(script_paths)} scripts, {len(service_paths)} services, {len(asset_paths)} assets")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Dry-run: list files only")
    parser.add_argument(
        "--sync-rotate", action="store_true",
        help="Sync rotate flags from dashboard_config.yaml into station_configs/ and exit",
    )
    args = parser.parse_args()
    if args.sync_rotate:
        n = sync_rotate_flags()
        print(f"Done — {n} flag(s) updated.")
        return
    build(check=args.check)


if __name__ == "__main__":
    main()
