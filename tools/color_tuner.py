#!/usr/bin/env python3
"""color_tuner.py — per-camera colour calibration tuner.

Loads a source MKV chunk, derives the adaptive base gains, and opens a
matplotlib window with four sliders (R / G / B gain multipliers + gamma)
that directly preview the calibrated maxpixel + mid-frame.

"Save" writes the tuned values as ``color_post`` into the camera's entry in
``station_configs/<host>/config.json`` (adaptive stays the base; color_post
is the aesthetic layer). "Push" optionally PATCHes the live station's
``config.json`` via SSH so the change takes effect without waiting for the
next bundle deploy.

Usage::

    uv run python tools/color_tuner.py \\
        --mkv downloads/2026-04-19/RO0003_orientation/RO0003_20260418_231610_color.mkv \\
        --station gmn0005 --cam RO0003

Add ``--push`` to SSH into the station and apply the new values immediately.
Add ``--no-save`` to preview only (skip writing to station_configs).
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import yaml
from matplotlib.widgets import Button, Slider

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'rovimen-scripts'))
import color_calibration as cc  # noqa: E402

logger = logging.getLogger(__name__)


def _load_frames(mkv: Path, stride: int = 5) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Decode subsampled frames from the MKV, return (maxpx, avgpx, midframe)."""
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=width,height', '-of', 'json', str(mkv)],
        capture_output=True, text=True, check=True,
    )
    meta = json.loads(probe.stdout)['streams'][0]
    w, h = int(meta['width']), int(meta['height'])
    raw = subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(mkv),
         '-vf', f'select=not(mod(n\\,{stride})),format=rgb24',
         '-vsync', 'vfr', '-f', 'rawvideo', '-'],
        capture_output=True, check=True,
    ).stdout
    fb = w * h * 3
    n = len(raw) // fb
    if n == 0:
        raise RuntimeError(f'no frames decoded from {mkv}')
    frames = np.frombuffer(raw[:n * fb], dtype=np.uint8).reshape(n, h, w, 3)
    return (
        frames.max(axis=0).astype(np.uint8),
        frames.mean(axis=0).clip(0, 255).astype(np.uint8),
        frames[n // 2].copy(),
    )


def _load_station_config(host: str) -> tuple[Path, dict]:
    path = REPO_ROOT / 'station_configs' / host / 'config.json'
    if not path.exists():
        raise FileNotFoundError(f'station config not found: {path}')
    return path, json.loads(path.read_text())


def _save_color_post(host: str, cam: str, color_post: dict) -> Path:
    """Write color_post into station_configs/<host>/config.json (preserves rest)."""
    path, cfg = _load_station_config(host)
    stations = cfg.setdefault('stations', {})
    cam_cfg = stations.setdefault(cam, {})
    cam_cfg['color_post'] = {
        k: round(float(v), 4) for k, v in color_post.items()
    }
    path.write_text(json.dumps(cfg, indent=4) + '\n')
    return path


def _push_to_station(host: str, cam: str, color_post: dict, dashboard_cfg_path: Path) -> None:
    """Push color_post via SSH → station's local station API.

    Uses ``dashboard_config.yaml`` to resolve IP, ssh_user, and jump_hosts.
    Requires SSH keys set up to the station (password auth isn't handled here;
    use ssh-agent or sshpass externally if needed).
    """
    cfg = yaml.safe_load(dashboard_cfg_path.read_text())
    st = cfg['stations'].get(host)
    if not st:
        raise ValueError(f'host {host} not in dashboard_config.yaml')
    ssh_user = st.get('ssh_user', 'gmn')
    ip = st['ip']
    jumps = st.get('jump_hosts') or []
    jump_args: list[str] = []
    if jumps:
        # Resolve jump hosts through dashboard_config
        jump_specs = []
        for j in jumps:
            jst = cfg['stations'].get(j)
            if jst:
                jump_specs.append(f"{jst.get('ssh_user', 'gmn')}@{jst['ip']}")
        if jump_specs:
            jump_args = ['-J', ','.join(jump_specs)]

    payload = {'stations': {cam: {'color_post': {
        k: round(float(v), 4) for k, v in color_post.items()
    }}}}
    remote_cmd = (
        f"curl -s -X PATCH http://localhost:7779/api/settings "
        f"-H 'Content-Type: application/json' "
        f"-d '{json.dumps(payload)}'"
    )
    ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', *jump_args,
               f'{ssh_user}@{ip}', remote_cmd]
    print(f'[push] {" ".join(ssh_cmd)}')
    result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError(f'SSH push failed: {result.stderr.strip()}')
    print(f'[push] station response: {result.stdout.strip()}')


def _build_initial(host: str, cam: str) -> dict:
    """Start from existing color_post if present, else fleet default."""
    try:
        _, cfg = _load_station_config(host)
        existing = cfg.get('stations', {}).get(cam, {}).get('color_post')
        if existing:
            return {**cc.FLEET_DEFAULT_COLOR_POST, **existing}
    except FileNotFoundError:
        pass
    return dict(cc.FLEET_DEFAULT_COLOR_POST)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--mkv', type=Path, required=True, help='path to a source MKV chunk')
    ap.add_argument('--station', required=True, help='host key (e.g. gmn0005)')
    ap.add_argument('--cam', required=True, help='camera code (e.g. RO0003)')
    ap.add_argument('--push', action='store_true', help='SSH the new values to the live station')
    ap.add_argument('--no-save', action='store_true', help='preview only; do not write station_configs')
    ap.add_argument('--dashboard-config', type=Path,
                    default=REPO_ROOT / 'dashboard' / 'dashboard_config.yaml',
                    help='used by --push to resolve ssh_user/ip/jump_hosts')
    args = ap.parse_args()

    if not args.mkv.exists():
        ap.error(f'MKV not found: {args.mkv}')

    print(f'loading {args.mkv.name} …')
    maxpx, avgpx, midframe = _load_frames(args.mkv)
    # Sensor sky means (before any calibration) — printed once, useful context
    luma = avgpx.astype(np.float32).mean(axis=2)
    lo = np.percentile(luma, 10); hi = np.percentile(luma, 85)
    sky = avgpx[(luma >= lo) & (luma <= hi)].astype(np.float32)
    print(f'raw sensor sky mean (masked): '
          f'R={sky[:, 0].mean():.1f} G={sky[:, 1].mean():.1f} B={sky[:, 2].mean():.1f}')

    initial = _build_initial(args.station, args.cam)
    print(f'initial color_post: {initial}')

    def render(post: dict) -> tuple[np.ndarray, np.ndarray, tuple[float, float, float, float]]:
        target = (float(post['target_r']), float(post['target_g']), float(post['target_b']))
        gr, gg, gb = cc.derive_adaptive_gains(avgpx, target_rgb=target)
        gamma = float(post['gamma'])
        return (
            cc.apply_calibration_np(maxpx, gr, gg, gb, gamma, rotate=False),
            cc.apply_calibration_np(midframe, gr, gg, gb, gamma, rotate=False),
            (gr, gg, gb, gamma),
        )

    # --- matplotlib UI ---
    fig = plt.figure(figsize=(14, 8))
    fig.suptitle(f'{args.station} / {args.cam} — tune target sky colour + gamma')
    ax_max = fig.add_axes((0.04, 0.30, 0.45, 0.63))
    ax_mid = fig.add_axes((0.51, 0.30, 0.45, 0.63))
    ax_max.set_title('Maxpixel stack'); ax_max.axis('off')
    ax_mid.set_title('Mid-chunk frame'); ax_mid.axis('off')
    calib_max, calib_mid, initial_gains = render(initial)
    img_max = ax_max.imshow(calib_max)
    img_mid = ax_mid.imshow(calib_mid)

    slider_specs = [
        ('target_r', 'R target  ', 0.5, 3.0),
        ('target_g', 'G target  ', 0.5, 3.0),
        ('target_b', 'B target  ', 0.5, 3.0),
        ('gamma',    'gamma     ', 0.3, 1.5),
    ]
    sliders: dict[str, Slider] = {}
    for i, (key, label, lo, hi) in enumerate(slider_specs):
        axs = fig.add_axes((0.10, 0.22 - 0.04 * i, 0.55, 0.025))
        sliders[key] = Slider(axs, label, lo, hi, valinit=initial[key], valfmt='%.3f')

    status_ax = fig.add_axes((0.70, 0.10, 0.28, 0.12))
    status_ax.axis('off')
    status_txt = status_ax.text(0.0, 0.5, '', fontsize=10, family='monospace', va='center')

    def current_post() -> dict:
        return {k: s.val for k, s in sliders.items()}

    def update(_=None):
        post = current_post()
        m, f, (gr, gg, gb, gamma) = render(post)
        img_max.set_data(m)
        img_mid.set_data(f)
        status_txt.set_text(
            f'derived gains:\n R={gr:.3f} G={gg:.3f} B={gb:.3f}\n gamma={gamma:.3f}'
        )
        fig.canvas.draw_idle()

    for s in sliders.values():
        s.on_changed(update)
    update()

    # --- buttons ---
    def do_save(_event=None):
        if args.no_save:
            print('[save] --no-save was set; skipping')
            return
        path = _save_color_post(args.station, args.cam, current_post())
        print(f'[save] wrote {path}')
        status_txt.set_text(status_txt.get_text() + '\nSAVED')
        fig.canvas.draw_idle()

    def do_push(_event=None):
        try:
            _push_to_station(args.station, args.cam, current_post(), args.dashboard_config)
            status_txt.set_text(status_txt.get_text() + '\nPUSHED')
        except Exception as exc:
            status_txt.set_text(status_txt.get_text() + f'\nPUSH FAILED: {exc}')
        fig.canvas.draw_idle()

    def do_reset(_event=None):
        for key, s in sliders.items():
            s.set_val(cc.FLEET_DEFAULT_COLOR_POST[key])

    save_ax  = fig.add_axes((0.70, 0.03, 0.08, 0.05))
    push_ax  = fig.add_axes((0.80, 0.03, 0.08, 0.05))
    reset_ax = fig.add_axes((0.90, 0.03, 0.08, 0.05))
    btn_save  = Button(save_ax,  'Save')
    btn_push  = Button(push_ax,  'Push')
    btn_reset = Button(reset_ax, 'Reset')
    btn_save.on_clicked(do_save)
    btn_push.on_clicked(do_push)
    btn_reset.on_clicked(do_reset)

    plt.show()
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    sys.exit(main())
