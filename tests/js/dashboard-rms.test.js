import { describe, it, expect } from 'vitest';

import * as ctx from '../../dashboard/static/dashboard-rms.js';

describe('_parseRmsPlotImageUrl', () => {
  const f = ctx._parseRmsPlotImageUrl;

  it('parses a valid plot_image URL', () => {
    const r = f('/api/rms/plot_image/gmn0004/RO000T/20260530/detection_map.png');
    expect(r).toEqual({
      host: 'gmn0004',
      cam: 'RO000T',
      date: '20260530',
      filename: 'detection_map.png',
    });
  });
  it('handles URL-encoded filenames', () => {
    const r = f('/api/rms/plot_image/gmn0004/RO000T/20260530/file%20name.png');
    expect(r).toEqual({
      host: 'gmn0004',
      cam: 'RO000T',
      date: '20260530',
      filename: 'file name.png',
    });
  });
  it('parses absolute URL', () => {
    const r = f('http://localhost/api/rms/plot_image/gmn0004/RO000T/20260530/map.png');
    expect(r).toEqual({
      host: 'gmn0004',
      cam: 'RO000T',
      date: '20260530',
      filename: 'map.png',
    });
  });
  it('returns null for non-plot URL', () => {
    expect(f('/api/rms/chunks/gmn0004/RO000T/20260530')).toBeNull();
  });
  it('returns null for truncated plot URL', () => {
    expect(f('/api/rms/plot_image/gmn0004/RO000T')).toBeNull();
  });
  it('returns null for empty string', () => {
    expect(f('')).toBeNull();
  });
  it('returns null for garbage', () => {
    expect(f('not-a-url-at-all')).toBeNull();
  });
});
