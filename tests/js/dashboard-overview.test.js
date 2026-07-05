import { describe, it, expect, beforeEach, beforeAll } from 'vitest';

import { state } from '../../dashboard/static/dashboard-common.js';

// dashboard-overview.js instantiates a shared VideoModal at module load,
// which needs a real mount element. Provide it, then dynamic-import so the
// module side effects don't throw in jsdom. `ctx` is populated in beforeAll,
// so tests must read ctx.<fn> inside the test body (not at describe time).
let ctx;
beforeAll(async () => {
  const mount = document.createElement('div');
  mount.id = 'gmn-clip-modal-container';
  document.body.appendChild(mount);
  // A top-level IIFE binds a wheel handler to this image on import.
  const plotImg = document.createElement('img');
  plotImg.id = 'ov-plot-modal-img';
  document.body.appendChild(plotImg);
  ctx = await import('../../dashboard/static/dashboard-overview.js');
});

describe('state.selectedStations', () => {
  it('is initialised as an empty Set', () => {
    expect(state.selectedStations).toBeInstanceOf(Set);
  });
});

describe('_toggleInSet', () => {
  beforeEach(() => {
    state.selectedStations.clear();
  });

  it('adds a missing key and reports it as now-selected', () => {
    const set = new Set();
    expect(ctx._toggleInSet(set, 'gmn0004')).toBe(true);
    expect(set.has('gmn0004')).toBe(true);
  });

  it('removes a present key and reports it as now-deselected', () => {
    const set = new Set(['gmn0004']);
    expect(ctx._toggleInSet(set, 'gmn0004')).toBe(false);
    expect(set.has('gmn0004')).toBe(false);
  });

  it('toggles back and forth', () => {
    const set = new Set();
    expect(ctx._toggleInSet(set, 'gmnro03')).toBe(true);
    expect(ctx._toggleInSet(set, 'gmnro03')).toBe(false);
    expect(ctx._toggleInSet(set, 'gmnro03')).toBe(true);
    expect([...set]).toEqual(['gmnro03']);
  });

  it('handles multiple independent keys (multi-select)', () => {
    const set = new Set();
    ctx._toggleInSet(set, 'gmn0004');
    ctx._toggleInSet(set, 'gmnro03');
    ctx._toggleInSet(set, 'gmnro07');
    expect(set.size).toBe(3);
    ctx._toggleInSet(set, 'gmnro03'); // deselect the middle one
    expect([...set].sort()).toEqual(['gmn0004', 'gmnro07']);
  });
});

describe('_unionFovIds', () => {
  const ALL = [
    'gmn0004_RO000T', 'gmn0004_RO000U',
    'gmnro03_RO000H', 'gmnro03_RO000J', 'gmnro03_RO000L',
    'gmnro07_RO000W', 'gmnro07_RO000X',
  ];

  it('returns nothing for no selected stations', () => {
    expect(ctx._unionFovIds([], ALL)).toEqual([]);
    expect(ctx._unionFovIds(new Set(), ALL)).toEqual([]);
  });

  it('returns every FOV id for a single selected station', () => {
    expect(ctx._unionFovIds(['gmnro03'], ALL)).toEqual([
      'gmnro03_RO000H', 'gmnro03_RO000J', 'gmnro03_RO000L',
    ]);
  });

  it('returns the union across several selected stations', () => {
    const union = ctx._unionFovIds(['gmn0004', 'gmnro07'], ALL);
    expect(union.sort()).toEqual([
      'gmn0004_RO000T', 'gmn0004_RO000U',
      'gmnro07_RO000W', 'gmnro07_RO000X',
    ]);
  });

  it('accepts a Set of keys', () => {
    expect(ctx._unionFovIds(new Set(['gmn0004']), ALL)).toEqual([
      'gmn0004_RO000T', 'gmn0004_RO000U',
    ]);
  });

  it('does not match a host that is a prefix of another host code', () => {
    // 'gmn0004' must not pull in a hypothetical 'gmn00041_RO0001'
    const ids = ['gmn0004_RO000T', 'gmn00041_RO0001'];
    expect(ctx._unionFovIds(['gmn0004'], ids)).toEqual(['gmn0004_RO000T']);
  });
});
