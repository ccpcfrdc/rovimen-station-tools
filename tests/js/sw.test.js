import { describe, it, expect } from 'vitest';

import * as ctx from '../../dashboard/static/sw.js';

describe('isNetworkOnly', () => {
  const f = ctx.isNetworkOnly;

  it('API routes are network-only', () => {
    expect(f('/api/stations')).toBe(true);
    expect(f('/api/status/gmn0004')).toBe(true);
    expect(f('/api/videodb/chunks/gmn0004/RO000T/20260601')).toBe(true);
  });
  it('thumbnail routes are network-only', () => {
    expect(f('/thumbnail/gmn0004/RO000T/123.webp')).toBe(true);
  });
  it('video routes are network-only', () => {
    expect(f('/video/gmn0004/RO000T/clip.mp4')).toBe(true);
  });
  it('timelapse routes are network-only', () => {
    expect(f('/timelapse/gmn0004/RO000T/20260601.mp4')).toBe(true);
  });
  it('stream routes are network-only', () => {
    expect(f('/stream/gmn0004')).toBe(true);
  });
  it('download routes are network-only', () => {
    expect(f('/download/something')).toBe(true);
  });
  it('static assets are NOT network-only', () => {
    expect(f('/static/dashboard.css')).toBe(false);
    expect(f('/static/dashboard-common.js')).toBe(false);
  });
  it('root path is NOT network-only', () => {
    expect(f('/')).toBe(false);
  });
  it('sw.js is NOT network-only', () => {
    expect(f('/sw.js')).toBe(false);
  });
});

describe('isStaticAsset', () => {
  const f = ctx.isStaticAsset;

  it('/static/ paths are static assets', () => {
    expect(f('/static/dashboard.css')).toBe(true);
    expect(f('/static/dashboard-common.js')).toBe(true);
    expect(f('/static/twilight-slider.js')).toBe(true);
  });
  it('/sw.js is a static asset', () => {
    expect(f('/sw.js')).toBe(true);
  });
  it('API paths are NOT static', () => {
    expect(f('/api/stations')).toBe(false);
  });
  it('root is NOT static', () => {
    expect(f('/')).toBe(false);
  });
  it('/static without trailing slash is NOT static', () => {
    expect(f('/static')).toBe(false);
  });
});
