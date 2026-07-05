import { describe, it, expect, beforeEach, afterEach } from 'vitest';

import { VideoModal } from '../../dashboard/static/dashboard-video-modal.js';
import { MultiDetModal } from '../../dashboard/static/dashboard-multi-det-modal.js';
import { state } from '../../dashboard/static/dashboard-common.js';

// ── Operator-control hiding for anonymous visitors ────────────────────────────
//
// The shared VideoModal / MultiDetModal reveal operator-only actions (Download
// clip, Crop, loop/frame download, Show/Download stack, Download video grid)
// based on the session identity in state.AUTH_USER. An anonymous public visitor
// (state.AUTH_USER == null) must get playback controls only; a logged-in
// operator keeps the full action set. These tests drive the real modules in
// jsdom and assert the computed inline display style on each control.

const _SINGLE_OPTS = {
  src: 'blob:test',
  title: 'clip',
  station: 'pub_station',
  camera: 'PUB001',
  date: '20260101',
  filename: 'PUB001_20260101_010101.mkv',
  // Operator features the caller always requests; the modal decides whether to
  // paint them based on auth state.
  download: { onClick: () => {} },
  stack: { url: '/media/v1/stack/PUB001/2026-01-01/PUB001_20260101_010101_stack.webp' },
  loopDlPath: '/loopclip/pub_station/PUB001/20260101/PUB001_20260101_010101.mkv',
};

function mount() {
  const el = document.createElement('div');
  document.body.appendChild(el);
  return el;
}

const OPERATOR_SELECTORS = [
  '.vm-download-btn',
  '.vm-crop-btn',
  '.vm-loop-dl-wrap',
  '.vm-stack-dl-btn',
  '.vm-stack-show-btn',
];

describe('VideoModal operator-control hiding', () => {
  let mountEl;
  let prevUser;

  beforeEach(() => {
    prevUser = state.AUTH_USER;
    mountEl = mount();
  });

  afterEach(() => {
    state.AUTH_USER = prevUser;
    mountEl.remove();
  });

  it('anonymous visitor: every operator control is hidden, playback remains', () => {
    state.AUTH_USER = null;
    const modal = new VideoModal(mountEl, { trim: true });
    modal.open({ ..._SINGLE_OPTS });
    for (const sel of OPERATOR_SELECTORS) {
      const btn = mountEl.querySelector(sel);
      expect(btn, sel).toBeTruthy();
      expect(btn.style.display, `${sel} must be hidden for anon`).toBe('none');
    }
    // Playback controls stay available.
    expect(mountEl.querySelector('.vm-btn-play').style.display).not.toBe('none');
    expect(mountEl.querySelector('.vm-trackbar')).toBeTruthy();
  });

  it('logged-in operator: download / crop / stack controls are shown', () => {
    state.AUTH_USER = 'alex';
    const modal = new VideoModal(mountEl, { trim: true });
    modal.open({ ..._SINGLE_OPTS });
    // Download clip + Crop + Show stack are visible for an operator.
    expect(mountEl.querySelector('.vm-download-btn').style.display).not.toBe('none');
    expect(mountEl.querySelector('.vm-crop-btn').style.display).not.toBe('none');
    expect(mountEl.querySelector('.vm-stack-show-btn').style.display).not.toBe('none');
  });
});

describe('MultiDetModal download-grid hiding', () => {
  let mountEl;
  let prevUser;

  beforeEach(() => {
    prevUser = state.AUTH_USER;
    mountEl = mount();
  });

  afterEach(() => {
    state.AUTH_USER = prevUser;
    mountEl.remove();
  });

  const _MULTI_OPTS = {
    title: 'event',
    videos: [
      { cam: 'PUB001', station: 'pub_station', date: '20260101',
        filename: 'PUB001_20260101_010101.mkv', url: 'blob:a', offset: 0,
        downloadUrl: '/shortclip/pub_station/PUB001/20260101/x.mkv' },
      { cam: 'PUB002', station: 'pub_station', date: '20260101',
        filename: 'PUB002_20260101_010101.mkv', url: 'blob:b', offset: 0,
        downloadUrl: '/shortclip/pub_station/PUB002/20260101/y.mkv' },
    ],
  };

  it('anonymous visitor: "Download video grid" is hidden', () => {
    state.AUTH_USER = null;
    const modal = new MultiDetModal(mountEl);
    modal.open({ ..._MULTI_OPTS });
    const stitch = mountEl.querySelector('.mdm-stitch-btn');
    expect(stitch).toBeTruthy();
    expect(stitch.style.display).toBe('none');
  });

  it('logged-in operator: "Download video grid" is shown', () => {
    state.AUTH_USER = 'alex';
    const modal = new MultiDetModal(mountEl);
    modal.open({ ..._MULTI_OPTS });
    const stitch = mountEl.querySelector('.mdm-stitch-btn');
    expect(stitch.style.display).not.toBe('none');
  });
});
