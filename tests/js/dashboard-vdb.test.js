import { describe, it, expect } from 'vitest';

import * as ctx from '../../dashboard/static/dashboard-vdb.js';
import { escHtml } from '../../dashboard/static/dashboard-common.js';

// ── XSS: data-* attribute round-trip ──────────────────────────────────────────
// Cards store server-supplied fields as escHtml(JSON.stringify({...})) in a
// data-chunk attribute; the delegated handler recovers them via JSON.parse.
// The round-trip must:
//   a) produce an HTML-safe attribute value (no raw < ' " that break markup)
//   b) recover the original string value (not HTML-entity-encoded) for the
//      JS callee (which receives a plain string, not HTML)
describe('vdb card data-* XSS round-trip', () => {
  function encode(payload) {
    return escHtml(JSON.stringify({ filename: payload, time: '21:30:00' }));
  }
  function decode(attr) {
    // Simulate what the browser does: the HTML parser decodes entities in the
    // attribute value before exposing it via dataset.  We replicate that here
    // using a DOMParser-free approach: JSON.parse on the entity-decoded string.
    const decoded = attr
      .replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'");
    return JSON.parse(decoded);
  }

  it('single-quote filename: attribute value contains no raw single quotes', () => {
    const filename = "night'onload='alert(1)'.mp4";
    const attr = encode(filename);
    expect(attr).not.toContain("'");
  });

  it('single-quote filename: round-trips to original string', () => {
    const filename = "night'onload='alert(1)'.mp4";
    const attr = encode(filename);
    const recovered = decode(attr);
    expect(recovered.filename).toBe(filename);
  });

  it('img onerror filename: attribute value contains no raw angle brackets', () => {
    const filename = '<img src=x onerror=alert(1)>.mp4';
    const attr = encode(filename);
    // < and > are entity-encoded, so the browser sees no HTML tag structure.
    // The string "onerror=" may appear as plain text; what matters is the
    // angle brackets that would make it a live element are gone.
    expect(attr).not.toContain('<img');
    expect(attr).not.toContain('<');
    expect(attr).not.toContain('>');
  });

  it('img onerror filename: round-trips to original string', () => {
    const filename = '<img src=x onerror=alert(1)>.mp4';
    const attr = encode(filename);
    const recovered = decode(attr);
    expect(recovered.filename).toBe(filename);
  });

  it('double-quote filename: attribute value contains no raw double quotes', () => {
    const filename = 'file"name".mp4';
    const attr = encode(filename);
    expect(attr).not.toContain('"');
  });

  it('double-quote filename: round-trips to original string', () => {
    const filename = 'file"name".mp4';
    const attr = encode(filename);
    const recovered = decode(attr);
    expect(recovered.filename).toBe(filename);
  });

  it('clean filename passes through and round-trips unchanged', () => {
    const filename = '20260601_234512.123_RO0017.mp4';
    const attr = encode(filename);
    const recovered = decode(attr);
    expect(recovered.filename).toBe(filename);
  });
});

describe('vdbChunkUtcMs', () => {
  const f = ctx.vdbChunkUtcMs;

  it('afternoon time stays on same calendar day', () => {
    const ms = f('20260601', '21:30:00');
    const d = new Date(ms);
    expect(d.getUTCFullYear()).toBe(2026);
    expect(d.getUTCMonth()).toBe(5);
    expect(d.getUTCDate()).toBe(1);
    expect(d.getUTCHours()).toBe(21);
    expect(d.getUTCMinutes()).toBe(30);
  });
  it('morning time (before noon) rolls to next calendar day', () => {
    const ms = f('20260601', '03:15:00');
    const d = new Date(ms);
    expect(d.getUTCDate()).toBe(2);
    expect(d.getUTCHours()).toBe(3);
    expect(d.getUTCMinutes()).toBe(15);
  });
  it('exactly noon (12:00) stays on same day', () => {
    const ms = f('20260601', '12:00:00');
    const d = new Date(ms);
    expect(d.getUTCDate()).toBe(1);
    expect(d.getUTCHours()).toBe(12);
  });
  it('11:59 (just before noon) rolls to next day', () => {
    const ms = f('20260601', '11:59:00');
    const d = new Date(ms);
    expect(d.getUTCDate()).toBe(2);
  });
  it('returns null for empty ymd', () => {
    expect(f('', '12:00:00')).toBeNull();
  });
  it('returns null for null ymd', () => {
    expect(f(null, '12:00:00')).toBeNull();
  });
  it('returns null for null hms', () => {
    expect(f('20260601', null)).toBeNull();
  });
  it('returns null for short ymd', () => {
    expect(f('2026', '12:00:00')).toBeNull();
  });
  it('returns null for garbage hms', () => {
    expect(f('20260601', 'abc')).toBeNull();
  });
  it('handles missing seconds', () => {
    const ms = f('20260601', '21:30:');
    expect(ms).not.toBeNull();
    const d = new Date(ms);
    expect(d.getUTCHours()).toBe(21);
    expect(d.getUTCSeconds()).toBe(0);
  });
});

describe('_vdbAzCompass', () => {
  const f = ctx._vdbAzCompass;

  it('0 degrees is N', () => {
    expect(f(0)).toBe('N');
  });
  it('45 degrees is NE', () => {
    expect(f(45)).toBe('NE');
  });
  it('90 degrees is E', () => {
    expect(f(90)).toBe('E');
  });
  it('135 degrees is SE', () => {
    expect(f(135)).toBe('SE');
  });
  it('180 degrees is S', () => {
    expect(f(180)).toBe('S');
  });
  it('225 degrees is SW', () => {
    expect(f(225)).toBe('SW');
  });
  it('270 degrees is W', () => {
    expect(f(270)).toBe('W');
  });
  it('315 degrees is NW', () => {
    expect(f(315)).toBe('NW');
  });
  it('360 degrees wraps to N', () => {
    expect(f(360)).toBe('N');
  });
  it('negative degrees wrap correctly', () => {
    expect(f(-90)).toBe('W');
    expect(f(-180)).toBe('S');
  });
  it('boundary: 22 rounds to N', () => {
    expect(f(22)).toBe('N');
  });
  it('boundary: 23 rounds to NE', () => {
    expect(f(23)).toBe('NE');
  });
  it('large angle wraps', () => {
    expect(f(720)).toBe('N');
    expect(f(810)).toBe('E');
  });
});

describe('vdbSliderToFmt', () => {
  const f = ctx.vdbSliderToFmt;

  it('slider 0 -> 12:00 (noon)', () => {
    expect(f(0)).toBe('12:00');
  });
  it('slider 720 -> 00:00 (midnight)', () => {
    expect(f(720)).toBe('00:00');
  });
  it('slider 360 -> 18:00', () => {
    expect(f(360)).toBe('18:00');
  });
  it('slider 1 -> 12:01', () => {
    expect(f(1)).toBe('12:01');
  });
});

describe('vdbParseHHMM', () => {
  const f = ctx.vdbParseHHMM;

  it('parses "14:30" to 870', () => {
    expect(f('14:30')).toBe(870);
  });
  it('parses compact "1430"', () => {
    expect(f('1430')).toBe(870);
  });
  it('rejects invalid', () => {
    expect(f('25:00')).toBeNull();
    expect(f('')).toBeNull();
    expect(f('abc')).toBeNull();
  });
  it('24:00 is valid (end of day)', () => {
    expect(f('24:00')).toBe(1440);
  });
  it('24:01 is invalid', () => {
    expect(f('24:01')).toBeNull();
  });
});

describe('vdbMinutesToSliderVal', () => {
  const f = ctx.vdbMinutesToSliderVal;

  it('noon with isEnd=false returns 0', () => {
    expect(f(720, false)).toBe(0);
  });
  it('noon with isEnd=true returns 1440', () => {
    expect(f(720, true)).toBe(1440);
  });
  it('midnight returns 720', () => {
    expect(f(0, false)).toBe(720);
  });
});

describe('vdbNextDay', () => {
  const f = ctx.vdbNextDay;

  it('normal day rollover', () => {
    expect(f('20260601')).toBe('2026-06-02');
  });
  it('end of month rollover', () => {
    expect(f('20260630')).toBe('2026-07-01');
  });
  it('end of year rollover', () => {
    expect(f('20261231')).toBe('2027-01-01');
  });
  it('february in non-leap year', () => {
    expect(f('20270228')).toBe('2027-03-01');
  });
  it('february in leap year', () => {
    expect(f('20280229')).toBe('2028-03-01');
  });
  it('february 28 in leap year stays in feb', () => {
    expect(f('20280228')).toBe('2028-02-29');
  });
});
