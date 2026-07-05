import { describe, it, expect, vi, beforeEach } from 'vitest';

import * as ctx from '../../dashboard/static/dashboard-common.js';

describe('fmtBytes', () => {
  const f = ctx.fmtBytes;

  it('formats 0 bytes', () => {
    expect(f(0)).toBe('0.0 B');
  });
  it('formats bytes under 1 KB', () => {
    expect(f(512)).toBe('512.0 B');
  });
  it('formats exact 1 KB', () => {
    expect(f(1024)).toBe('1.0 KB');
  });
  it('formats MB', () => {
    expect(f(1048576)).toBe('1.0 MB');
  });
  it('formats GB', () => {
    expect(f(1073741824)).toBe('1.0 GB');
  });
  it('formats TB', () => {
    expect(f(1099511627776)).toBe('1.0 TB');
  });
  it('shows decimal precision', () => {
    expect(f(1536)).toBe('1.5 KB');
  });
  it('returns dash for null', () => {
    expect(f(null)).toBe('—');
  });
  it('returns dash for undefined', () => {
    expect(f(undefined)).toBe('—');
  });
});

describe('fmtDate', () => {
  const f = ctx.fmtDate;

  it('formats YYYYMMDD to YYYY-MM-DD', () => {
    expect(f('20260602')).toBe('2026-06-02');
  });
  it('returns dash for empty string', () => {
    expect(f('')).toBe('—');
  });
  it('returns dash for null', () => {
    expect(f(null)).toBe('—');
  });
  it('returns dash for undefined', () => {
    expect(f(undefined)).toBe('—');
  });
});

describe('parseFn', () => {
  const f = ctx.parseFn;

  it('parses RMS filename with timestamp', () => {
    expect(f('FF_RO000T_20260530_214500_123_0000128.fits')).toBe('2026-05-30 21:45:00 UTC');
  });
  it('returns empty for non-matching filename', () => {
    expect(f('random.txt')).toBe('');
  });
  it('handles filename with extra underscores', () => {
    expect(f('some_prefix_20260101_120000_suffix')).toBe('2026-01-01 12:00:00 UTC');
  });
});

describe('barColor', () => {
  const f = ctx.barColor;

  it('returns green for low usage', () => {
    expect(f(50)).toBe('var(--green)');
  });
  it('returns yellow for moderate usage', () => {
    expect(f(75)).toBe('var(--yellow)');
  });
  it('returns red for high usage', () => {
    expect(f(90)).toBe('var(--red)');
  });
  it('boundary: 70 is green', () => {
    expect(f(70)).toBe('var(--green)');
  });
  it('boundary: 71 is yellow', () => {
    expect(f(71)).toBe('var(--yellow)');
  });
  it('boundary: 85 is yellow', () => {
    expect(f(85)).toBe('var(--yellow)');
  });
  it('boundary: 86 is red', () => {
    expect(f(86)).toBe('var(--red)');
  });
});

describe('escHtml', () => {
  const f = ctx.escHtml;

  it('escapes ampersand', () => {
    expect(f('a&b')).toBe('a&amp;b');
  });
  it('escapes less-than', () => {
    expect(f('<script>')).toBe('&lt;script&gt;');
  });
  it('escapes double quotes', () => {
    expect(f('"hello"')).toBe('&quot;hello&quot;');
  });
  it('escapes single quotes', () => {
    expect(f("it's")).toBe("it&#39;s");
  });
  it('returns empty for null', () => {
    expect(f(null)).toBe('');
  });
  it('returns empty for undefined', () => {
    expect(f(undefined)).toBe('');
  });
  it('handles string with all special chars', () => {
    expect(f('<a href="x">&\'</a>')).toBe('&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;');
  });
  it('passes through clean strings unchanged', () => {
    expect(f('hello world')).toBe('hello world');
  });

  // XSS: station-controlled filename / label payloads must be neutralised.
  it('neutralises single-quote in filename (breaks inline onclick)', () => {
    const filename = "20260601_foo'onclick='alert(1)'.mp4";
    const escaped = f(filename);
    // Must not contain a raw single quote that could terminate an attribute
    expect(escaped).not.toContain("'");
    // Must contain the safe entity instead
    expect(escaped).toContain('&#39;');
  });
  it('neutralises img onerror XSS payload in filename', () => {
    const filename = '<img src=x onerror=alert(1)>.mp4';
    const escaped = f(filename);
    // The opening tag must be entity-encoded so the browser never parses it
    // as a tag; onerror= may still appear as text, but cannot execute.
    expect(escaped).not.toContain('<img');
    expect(escaped).toContain('&lt;img');
    // Closing > is also encoded, completing the tag-break
    expect(escaped).not.toContain('>');
  });
  it('neutralises script injection in station label', () => {
    const label = '<script>alert(1)</script>';
    const escaped = f(label);
    expect(escaped).not.toContain('<script>');
    expect(escaped).toContain('&lt;script&gt;');
  });
});

// ── XSS: YouTube URL allow-list (mirrors compCartShowList safeYtUrl logic) ────
// Any URL that is not https: must be rejected to block javascript:/data: hrefs.
describe('safeYtUrl-equivalent URL validation', () => {
  function safeYtUrl(url) {
    if (!url) return null;
    try {
      const u = new URL(url);
      return u.protocol === 'https:' ? url : null;
    } catch { return null; }
  }

  it('accepts a valid https YouTube URL', () => {
    expect(safeYtUrl('https://www.youtube.com/watch?v=abc123')).toBe('https://www.youtube.com/watch?v=abc123');
  });
  it('rejects a javascript: URL', () => {
    expect(safeYtUrl('javascript:alert(1)')).toBeNull();
  });
  it('rejects a data: URL', () => {
    expect(safeYtUrl('data:text/html,<script>alert(1)</script>')).toBeNull();
  });
  it('rejects an http: URL (non-https)', () => {
    expect(safeYtUrl('http://example.com/video')).toBeNull();
  });
  it('rejects null', () => {
    expect(safeYtUrl(null)).toBeNull();
  });
  it('rejects empty string', () => {
    expect(safeYtUrl('')).toBeNull();
  });
  it('rejects a non-URL string', () => {
    expect(safeYtUrl('not a url')).toBeNull();
  });
});

describe('fetchJson', () => {
  const { fetchJson } = ctx;

  beforeEach(() => {
    vi.restoreAllMocks();
    // Reset location.href spy if set
    delete window.__fetchJsonRedirect;
  });

  function mockFetch({ status = 200, ok = true, contentType = 'application/json', body = '{"ok":true}' } = {}) {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status,
      ok,
      headers: { get: (h) => h === 'content-type' ? contentType : null },
      json: () => Promise.resolve(JSON.parse(body)),
    }));
  }

  it('returns parsed JSON for a 200 application/json response', async () => {
    mockFetch({ body: '{"value":42}' });
    const result = await fetchJson('/api/test');
    expect(result).toEqual({ value: 42 });
  });

  it('passes opts to fetch', async () => {
    mockFetch({ body: '{"saved":true}' });
    await fetchJson('/api/save', { method: 'POST' });
    expect(fetch).toHaveBeenCalledWith('/api/save', { method: 'POST' });
  });

  it('throws on non-ok response (500)', async () => {
    mockFetch({ status: 500, ok: false, contentType: 'text/html', body: '{}' });
    await expect(fetchJson('/api/fail')).rejects.toThrow('HTTP 500');
  });

  it('throws on non-ok response (403)', async () => {
    mockFetch({ status: 403, ok: false, contentType: 'application/json', body: '{"error":"forbidden"}' });
    await expect(fetchJson('/api/private')).rejects.toThrow('HTTP 403');
  });

  it('throws with correct status property on non-ok', async () => {
    mockFetch({ status: 503, ok: false, contentType: 'text/html', body: '{}' });
    const err = await fetchJson('/api/down').catch(e => e);
    expect(err.status).toBe(503);
  });

  it('on 401 with a live session: redirects to /login and throws "Session expired"', async () => {
    // A logged-in visitor whose cookie expired mid-session must be bounced.
    const loc = { href: '', assign: vi.fn() };
    Object.defineProperty(window, 'location', { value: loc, writable: true });
    ctx.state.AUTH_USER = 'alex';           // simulate a live session
    mockFetch({ status: 401, ok: false, contentType: 'text/html', body: '{}' });
    const err = await fetchJson('/api/protected').catch(e => e);
    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(401);
    expect(err.message).toBe('Session expired');
    expect(loc.href).toBe('/login');
    ctx.state.AUTH_USER = null;              // reset for other tests
  });

  it('on 401 with NO session (anon public page): does NOT redirect, throws "Unauthorized"', async () => {
    // Anonymous visitors on a public page (e.g. /events, whose curated
    // detection feed stays login-gated) must NOT be ejected to /login when a
    // gated background fetch 401s — the caller degrades gracefully instead.
    const loc = { href: '', assign: vi.fn() };
    Object.defineProperty(window, 'location', { value: loc, writable: true });
    ctx.state.AUTH_USER = null;              // anonymous
    mockFetch({ status: 401, ok: false, contentType: 'text/html', body: '{}' });
    const err = await fetchJson('/api/detections/nights').catch(e => e);
    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(401);
    expect(err.message).toBe('Unauthorized');
    expect(loc.href).toBe('');               // no redirect
  });

  it('throws on ok response with non-JSON content-type', async () => {
    mockFetch({ status: 200, ok: true, contentType: 'text/html', body: '{}' });
    await expect(fetchJson('/api/oops')).rejects.toThrow('Not JSON');
  });

  it('accepts text/json content-type', async () => {
    mockFetch({ status: 200, ok: true, contentType: 'text/json', body: '{"x":1}' });
    const result = await fetchJson('/api/alt');
    expect(result).toEqual({ x: 1 });
  });
});
