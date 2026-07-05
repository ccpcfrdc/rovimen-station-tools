import { describe, it, expect } from 'vitest';

import {
  _gridParseHHMM,
  _gridAgeLabel,
  _gridFilterColumns,
  _footageCellHtml,
  _footageStripHtml,
} from '../../dashboard/static/dashboard-grid.js';

describe('_gridParseHHMM', () => {
  it('parses HH:MM', () => {
    expect(_gridParseHHMM('21:30')).toBe(21 * 60 + 30);
    expect(_gridParseHHMM('00:00')).toBe(0);
    expect(_gridParseHHMM('24:00')).toBe(24 * 60);
  });
  it('parses HHMM and H:MM', () => {
    expect(_gridParseHHMM('2130')).toBe(21 * 60 + 30);
    expect(_gridParseHHMM('3:05')).toBe(3 * 60 + 5);
  });
  it('trims surrounding whitespace', () => {
    expect(_gridParseHHMM('  21:30  ')).toBe(21 * 60 + 30);
  });
  it('returns null for empty / null / blank', () => {
    expect(_gridParseHHMM('')).toBeNull();
    expect(_gridParseHHMM(null)).toBeNull();
    expect(_gridParseHHMM('   ')).toBeNull();
    expect(_gridParseHHMM(undefined)).toBeNull();
  });
  it('returns null for out-of-range values', () => {
    expect(_gridParseHHMM('25:00')).toBeNull();
    expect(_gridParseHHMM('21:60')).toBeNull();
    expect(_gridParseHHMM('24:01')).toBeNull();
  });
  it('returns null for garbage', () => {
    expect(_gridParseHHMM('abc')).toBeNull();
    expect(_gridParseHHMM('9')).toBeNull();
  });
});

describe('_gridAgeLabel', () => {
  const now = Date.parse('2026-06-16T12:00:00Z');
  it('returns "just now" under a minute', () => {
    expect(_gridAgeLabel('2026-06-16T11:59:30', now)).toBe('just now');
  });
  it('returns minutes', () => {
    expect(_gridAgeLabel('2026-06-16T11:30:00', now)).toBe('30m ago');
  });
  it('returns hours', () => {
    expect(_gridAgeLabel('2026-06-16T10:00:00', now)).toBe('2h ago');
  });
  it('returns days', () => {
    expect(_gridAgeLabel('2026-06-14T12:00:00', now)).toBe('2d ago');
  });
  it('handles a trailing Z without double-appending', () => {
    expect(_gridAgeLabel('2026-06-16T10:00:00Z', now)).toBe('2h ago');
  });
  it('clamps a future timestamp to "just now"', () => {
    expect(_gridAgeLabel('2026-06-16T13:00:00', now)).toBe('just now');
  });
  it('returns empty string for falsy / unparseable input', () => {
    expect(_gridAgeLabel('', now)).toBe('');
    expect(_gridAgeLabel(null, now)).toBe('');
    expect(_gridAgeLabel('not-a-date', now)).toBe('');
  });
});

describe('_gridFilterColumns', () => {
  const columns = [
    { cam: 'RO000A', host_key: 'gmnro01', station_label: 'Ghirdoveni' },
    { cam: 'RO000B', host_key: 'gmnro01', station_label: 'Ghirdoveni' },
    { cam: 'RO000M', host_key: 'gmnro02', station_label: 'Vaslui' },
  ];
  it('keeps only columns with at least one cell, preserving order', () => {
    const rows = [
      { cells: { RO000A: [{}], RO000M: [{}] } },
      { cells: { RO000A: [{}] } },
    ];
    const kept = _gridFilterColumns(columns, rows);
    expect(kept.map(c => c.cam)).toEqual(['RO000A', 'RO000M']);
  });
  it('returns empty array when no rows', () => {
    expect(_gridFilterColumns(columns, [])).toEqual([]);
  });
  it('tolerates missing cells objects', () => {
    expect(_gridFilterColumns(columns, [{}])).toEqual([]);
  });
  it('tolerates null inputs', () => {
    expect(_gridFilterColumns(null, null)).toEqual([]);
  });
  it('handles footage cells (object-valued), not just detection arrays', () => {
    const rows = [{ cells: { RO000B: { count: 3, rep: {}, chunks: [] } } }];
    expect(_gridFilterColumns(columns, rows).map(c => c.cam)).toEqual(['RO000B']);
  });
});

describe('_footageCellHtml', () => {
  const mkChunk = (over = {}) => ({
    host_key: 'gmnro02', filename: 'RO000M_20260622_220500_color.mkv',
    time: '20:05:00', stack: 'RO000M_20260622_220500_stack.webp',
    locked: false, meteor_time: null, detection_offset_s: 0, ...over,
  });
  const cell = {
    host_key: 'gmnro02', count: 3, has_detection: false,
    chunks: [mkChunk(), mkChunk({ filename: 'b.mkv', time: '20:05:20' }), mkChunk({ filename: 'c.mkv', time: '20:05:40' })],
  };
  it('renders an empty cell when there is no footage', () => {
    expect(_footageCellHtml('20260622', 'RO000M', null))
      .toBe('<td class="grid-cell grid-cell-empty"></td>');
    expect(_footageCellHtml('20260622', 'RO000M', { count: 0, chunks: [] }))
      .toBe('<td class="grid-cell grid-cell-empty"></td>');
  });
  it('renders EVERY clip in the cell (one playable thumb each)', () => {
    const html = _footageCellHtml('20260622', 'RO000M', cell);
    expect(html).toContain('grid-cell-footage');
    expect(html).toContain('/stack/gmnro02/RO000M/20260622/RO000M_20260622_220500_stack.webp');
    expect((html.match(/data-fn="/g) || []).length).toBe(3);   // all 3 clips, no rep/expand
    expect(html).not.toContain('grid-expand');                 // no expand button anymore
  });
  it('marks cells that contain a detection', () => {
    const det = { ...cell, has_detection: true };
    expect(_footageCellHtml('20260622', 'RO000M', det)).toContain('grid-cell-detection');
  });
  it('escapes interpolated filename values (no raw markup)', () => {
    const evil = { ...cell, chunks: [mkChunk({ filename: '"><img src=x onerror=alert(1)>' })] };
    expect(_footageCellHtml('20260622', 'RO000M', evil)).not.toContain('<img src=x onerror=');
  });
});

describe('_footageStripHtml', () => {
  const chunks = [
    { host_key: 'gmnro02', filename: 'a_color.mkv', time: '20:05:00',
      stack: 'a_stack.webp', locked: false, detection_offset_s: 0, meteor_time: null },
    { host_key: 'gmnro02', filename: 'b_color.mkv', time: '20:05:20',
      stack: null, locked: true, detection_offset_s: 7.2, meteor_time: '2026-06-22T20:05:25' },
  ];
  it('renders one thumb per chunk with its filename', () => {
    const html = _footageStripHtml('20260622', 'RO000M', chunks);
    expect(html).toContain('data-fn="a_color.mkv"');
    expect(html).toContain('data-fn="b_color.mkv"');
    // One data-fn attribute per chunk thumb (avoids matching grid-thumb-time etc).
    expect((html.match(/data-fn="/g) || []).length).toBe(2);
  });
  it('flags the locked chunk and falls back to "no img" without a stack', () => {
    const html = _footageStripHtml('20260622', 'RO000M', chunks);
    expect(html).toContain('grid-thumb detection');
    expect(html).toContain('no img');
  });
  it('returns empty string for no chunks', () => {
    expect(_footageStripHtml('20260622', 'RO000M', [])).toBe('');
    expect(_footageStripHtml('20260622', 'RO000M', null)).toBe('');
  });
});
