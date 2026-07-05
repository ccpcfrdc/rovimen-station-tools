import { describe, it, expect } from 'vitest';

import * as ctx from '../../dashboard/static/dashboard-station.js';

describe('escHtmlSafe', () => {
  const f = ctx.escHtmlSafe;

  it('escapes ampersand', () => {
    expect(f('a&b')).toBe('a&amp;b');
  });
  it('escapes angle brackets', () => {
    expect(f('<div>')).toBe('&lt;div&gt;');
  });
  it('escapes quotes', () => {
    expect(f('"x"')).toBe('&quot;x&quot;');
    expect(f("'x'")).toBe("&#39;x&#39;");
  });
  it('handles numbers', () => {
    expect(f(42)).toBe('42');
  });
  it('handles null gracefully', () => {
    expect(f(null)).toBe('null');
  });
  it('all special chars at once', () => {
    expect(f('&<>"\'')).toBe('&amp;&lt;&gt;&quot;&#39;');
  });
});

describe('_isAbort', () => {
  const f = ctx._isAbort;

  it('returns true for AbortError', () => {
    const e = new Error('aborted');
    e.name = 'AbortError';
    expect(f(e)).toBe(true);
  });
  it('returns false for other errors', () => {
    expect(f(new Error('fail'))).toBe(false);
  });
  it('returns falsy for null', () => {
    expect(f(null)).toBeFalsy();
  });
  it('returns falsy for undefined', () => {
    expect(f(undefined)).toBeFalsy();
  });
});

describe('_domeFmtUTC', () => {
  const f = ctx._domeFmtUTC;

  it('formats epoch to HH:MM:SS UTC', () => {
    const ms = Date.UTC(2026, 5, 2, 14, 30, 45);
    expect(f(ms)).toBe('14:30:45 UTC');
  });
  it('midnight', () => {
    const ms = Date.UTC(2026, 0, 1, 0, 0, 0);
    expect(f(ms)).toBe('00:00:00 UTC');
  });
  it('end of day', () => {
    const ms = Date.UTC(2026, 0, 1, 23, 59, 59);
    expect(f(ms)).toBe('23:59:59 UTC');
  });
});

describe('_domeFmtRelative', () => {
  const f = ctx._domeFmtRelative;

  it('under 60 seconds returns "just now"', () => {
    expect(f(0)).toBe('just now');
    expect(f(30000)).toBe('just now');
    expect(f(59999)).toBe('just now');
  });
  it('minutes: 60s to 59m', () => {
    expect(f(60000)).toBe('1m ago');
    expect(f(120000)).toBe('2m ago');
    expect(f(3540000)).toBe('59m ago');
  });
  it('hours with minutes', () => {
    expect(f(3600000)).toBe('1h ago');
    expect(f(3660000)).toBe('1h 1m ago');
    expect(f(7200000)).toBe('2h ago');
    expect(f(7260000)).toBe('2h 1m ago');
  });
  it('exact hours (no remainder minutes)', () => {
    expect(f(3600000 * 5)).toBe('5h ago');
  });
  it('days with hours', () => {
    expect(f(86400000)).toBe('1d ago');
    expect(f(86400000 + 3600000)).toBe('1d 1h ago');
    expect(f(86400000 * 3 + 3600000 * 12)).toBe('3d 12h ago');
  });
  it('30+ days drops hour granularity', () => {
    expect(f(86400000 * 30)).toBe('30d ago');
    expect(f(86400000 * 47)).toBe('47d ago');
    expect(f(86400000 * 30 + 3600000 * 5)).toBe('30d ago');
  });
  it('negative returns empty', () => {
    expect(f(-1000)).toBe('');
  });
  it('NaN returns empty', () => {
    expect(f(NaN)).toBe('');
  });
  it('Infinity returns empty', () => {
    expect(f(Infinity)).toBe('');
  });
});

describe('fmtCaptureUTC', () => {
  const f = ctx.fmtCaptureUTC;

  it('zero-pads hours and minutes', () => {
    expect(f(1.5)).toBe('01:30');
    expect(f(9)).toBe('09:00');
  });
  it('rounds to the nearest minute', () => {
    // 15.511h = 15h 30.66m -> rounds to 15:31
    expect(f(15.511)).toBe('15:31');
  });
  it('wraps hours past midnight back into [0,24)', () => {
    expect(f(25)).toBe('01:00');
    expect(f(24)).toBe('00:00');
  });
  it('wraps negative hours into [0,24)', () => {
    expect(f(-1)).toBe('23:00');
    expect(f(-0.5)).toBe('23:30');
  });
});

describe('computeCaptureWindow', () => {
  const f = ctx.computeCaptureWindow;
  // Romanian-latitude reference station for all cases below.
  const LAT = 45;
  const LON = 24;

  it('returns null when lat/lon are missing', () => {
    expect(f(null, LON)).toBeNull();
    expect(f(LAT, null)).toBeNull();
    expect(f(undefined, undefined)).toBeNull();
  });

  it('uses civil twilight (-6 degrees), not nautical (-12)', () => {
    // Regression for #495: the old -12 deg model put sunset ~36 min later
    // at lat 45. Civil twilight must give the earlier 15:31 UTC, never 16:07.
    const now = new Date(Date.UTC(2026, 0, 15, 12, 0, 0)); // winter midday
    const w = f(LAT, LON, now);
    expect(ctx.fmtCaptureUTC(w.sunsetUTC)).toBe('15:31');
    // -12 deg would have produced 16:07; assert we are clearly on the -6 side.
    expect(w.sunsetUTC).toBeLessThan(15.6);
  });

  it('is NOT active before sunset (shows "Next ...")', () => {
    const now = new Date(Date.UTC(2026, 0, 15, 12, 0, 0)); // midday, before 15:31 set
    const w = f(LAT, LON, now);
    expect(w.active).toBe(false);
    expect(w.label).toContain('Next RMS capture window:');
    expect(w.label).toContain('&rarr;');
    expect(w.label).not.toContain('Capture window active');
  });

  it('is active inside the window (shows "Capture window active")', () => {
    const now = new Date(Date.UTC(2026, 0, 15, 19, 0, 0)); // evening, after 15:31 set
    const w = f(LAT, LON, now);
    expect(w.active).toBe(true);
    expect(w.label).toContain('Capture window active');
    expect(w.label).toContain('ends');
    expect(w.label).toContain(ctx.fmtCaptureUTC(w.sunriseUTC));
    expect(w.label).not.toContain('Next RMS capture window');
  });

  it('stays active after midnight, before sunrise (wrapping window)', () => {
    const now = new Date(Date.UTC(2026, 0, 16, 2, 0, 0)); // 02:00, before 05:17 rise
    const w = f(LAT, LON, now);
    expect(w.active).toBe(true);
    expect(w.label).toContain('Capture window active');
  });

  it('is NOT active after sunrise (shows "Next ...")', () => {
    const now = new Date(Date.UTC(2026, 0, 16, 8, 0, 0)); // 08:00, after 05:17 rise
    const w = f(LAT, LON, now);
    expect(w.active).toBe(false);
    expect(w.label).toContain('Next RMS capture window:');
  });

  it('returns sunset later than sunrise for a normal night (wraps midnight)', () => {
    const now = new Date(Date.UTC(2026, 0, 15, 19, 0, 0));
    const w = f(LAT, LON, now);
    const wrap = h => ((h % 24) + 24) % 24;
    expect(wrap(w.sunsetUTC)).toBeGreaterThan(wrap(w.sunriseUTC));
  });
});
