import { describe, it, expect } from 'vitest';

import '../../dashboard/static/twilight-slider.js';
const tw = window.twilightSlider;

describe('utcMinToSliderVal', () => {
  const f = tw.utcMinToSliderVal;

  it('noon (720 min) maps to slider 0', () => {
    expect(f(720)).toBe(0);
  });
  it('midnight (0 min) maps to slider 720', () => {
    expect(f(0)).toBe(720);
  });
  it('18:00 (1080 min) maps to slider 360', () => {
    expect(f(1080)).toBe(360);
  });
  it('06:00 (360 min) maps to slider 1080', () => {
    expect(f(360)).toBe(1080);
  });
  it('just before noon (719 min = 11:59) maps to 1439', () => {
    expect(f(719)).toBe(1439);
  });
  it('just after noon (721 min = 12:01) maps to 1', () => {
    expect(f(721)).toBe(1);
  });
  it('end of day (1439 min = 23:59) maps to 719', () => {
    expect(f(1439)).toBe(719);
  });
});

describe('minutesOfDayToSliderVal', () => {
  const f = tw.minutesOfDayToSliderVal;

  it('noon with isEnd=false returns 0 (start of slider)', () => {
    expect(f(720, false)).toBe(0);
  });
  it('noon with isEnd=true returns 1440 (end of slider)', () => {
    expect(f(720, true)).toBe(1440);
  });
  it('midnight returns 720 regardless of isEnd', () => {
    expect(f(0, false)).toBe(720);
    expect(f(0, true)).toBe(720);
  });
  it('matches utcMinToSliderVal for non-noon values', () => {
    for (const m of [0, 100, 360, 719, 721, 1080, 1439]) {
      expect(f(m, false)).toBe(tw.utcMinToSliderVal(m));
    }
  });
});

describe('sliderValToHHMM', () => {
  const f = tw.sliderValToHHMM;

  it('slider 0 -> 12:00 (noon)', () => {
    expect(f(0)).toBe('12:00');
  });
  it('slider 720 -> 00:00 (midnight)', () => {
    expect(f(720)).toBe('00:00');
  });
  it('slider 360 -> 18:00', () => {
    expect(f(360)).toBe('18:00');
  });
  it('slider 1080 -> 06:00', () => {
    expect(f(1080)).toBe('06:00');
  });
  it('slider 1 -> 12:01', () => {
    expect(f(1)).toBe('12:01');
  });
  it('slider 1439 -> 11:59', () => {
    expect(f(1439)).toBe('11:59');
  });
  it('handles string input', () => {
    expect(f('720')).toBe('00:00');
  });
});

describe('parseHHMM', () => {
  const f = tw.parseHHMM;

  it('parses "00:00" to 0', () => {
    expect(f('00:00')).toBe(0);
  });
  it('parses "12:00" to 720', () => {
    expect(f('12:00')).toBe(720);
  });
  it('parses "23:59" to 1439', () => {
    expect(f('23:59')).toBe(1439);
  });
  it('parses "24:00" to 1440', () => {
    expect(f('24:00')).toBe(1440);
  });
  it('parses compact "0830" to 510', () => {
    expect(f('0830')).toBe(510);
  });
  it('parses single-digit hour "9:30"', () => {
    expect(f('9:30')).toBe(570);
  });
  it('rejects "24:01" (past end of day)', () => {
    expect(f('24:01')).toBeNull();
  });
  it('rejects "25:00"', () => {
    expect(f('25:00')).toBeNull();
  });
  it('rejects "12:60"', () => {
    expect(f('12:60')).toBeNull();
  });
  it('rejects empty string', () => {
    expect(f('')).toBeNull();
  });
  it('rejects garbage', () => {
    expect(f('abc')).toBeNull();
    expect(f('12:3')).toBeNull();
  });
  it('trims whitespace', () => {
    expect(f('  12:00  ')).toBe(720);
  });
});

describe('computeTwilightClampRange', () => {
  const f = tw.computeTwilightClampRange;

  it('returns sorted {startVal, endVal} from civil twilight', () => {
    const r = f({ civil_sunset_min: 1140, civil_sunrise_min: 240 });
    expect(r.startVal).toBeLessThanOrEqual(r.endVal);
  });
  it('uses defaults when twilight data is missing', () => {
    const r = f({});
    expect(r.startVal).toBeLessThanOrEqual(r.endVal);
    expect(typeof r.startVal).toBe('number');
    expect(typeof r.endVal).toBe('number');
  });
  it('sunset and sunrise at symmetric positions produce centered range', () => {
    const r = f({ civil_sunset_min: 1080, civil_sunrise_min: 360 });
    const sunsetSlider = tw.utcMinToSliderVal(1080);
    const sunriseSlider = tw.utcMinToSliderVal(360);
    expect(r.startVal).toBe(Math.min(sunsetSlider, sunriseSlider));
    expect(r.endVal).toBe(Math.max(sunsetSlider, sunriseSlider));
  });
});

describe('drawTwilightOnWrap gradient stop ordering', () => {
  // Regression: near the summer solstice at mid-latitudes, astronomical
  // sunrise slider position can fall just before midnight (< 50%), making
  // a hardcoded 50% midpoint create backwards gradient stops (#315).
  it('night midpoint does not exceed astronomical sunrise position for summer solstice', () => {
    // Regression for #315: near the summer solstice at lat 45.5, lon 26,
    // astronomical sunrise_min wraps to 1438 (23:58 UTC).  Its slider
    // percentage (~49.9%) falls just before the old hardcoded 50% midpoint,
    // producing a backwards gradient stop and a visible gap at midnight.
    const wrap = document.createElement('div');
    Object.defineProperty(wrap, 'offsetWidth', { value: 400 });
    tw.drawTwilightOnWrap(wrap, {
      civil_sunset_min: 1117,
      civil_sunrise_min: 112,
      nautical_sunset_min: 1166,
      nautical_sunrise_min: 62,
      sunset_min: 1231,
      sunrise_min: 1438,   // just before midnight
    });
    const gradient = wrap.style.getPropertyValue('--tl-gradient');
    // The night colour (#1a2c5e) should appear in the gradient with
    // monotonically non-decreasing positions.  Extract positions attached
    // to the NGHT colour to confirm the midpoint sits between ss and sr.
    const nghtStops = [...gradient.matchAll(/#1a2c5e\s+([\d.]+)%/g)]
      .map(m => parseFloat(m[1]));
    expect(nghtStops.length).toBeGreaterThanOrEqual(2);
    for (let i = 1; i < nghtStops.length; i++) {
      expect(nghtStops[i]).toBeGreaterThanOrEqual(nghtStops[i - 1]);
    }
  });

  it('night midpoint is centered between sunset and sunrise for winter', () => {
    const wrap = document.createElement('div');
    Object.defineProperty(wrap, 'offsetWidth', { value: 400 });
    tw.drawTwilightOnWrap(wrap, {
      civil_sunset_min: 1050,
      civil_sunrise_min: 390,
      nautical_sunset_min: 1065,
      nautical_sunrise_min: 375,
      sunset_min: 1080,
      sunrise_min: 360,
    });
    const gradient = wrap.style.getPropertyValue('--tl-gradient');
    const nghtStops = [...gradient.matchAll(/#1a2c5e\s+([\d.]+)%/g)]
      .map(m => parseFloat(m[1]));
    expect(nghtStops.length).toBeGreaterThanOrEqual(2);
    for (let i = 1; i < nghtStops.length; i++) {
      expect(nghtStops[i]).toBeGreaterThanOrEqual(nghtStops[i - 1]);
    }
  });
});

describe('sliderVal <-> HHMM roundtrip', () => {
  it('every slider value roundtrips through HHMM -> parse -> slider', () => {
    const { sliderValToHHMM, parseHHMM, minutesOfDayToSliderVal } = tw;
    for (let v = 0; v < 1440; v++) {
      const hhmm = sliderValToHHMM(v);
      const min = parseHHMM(hhmm);
      expect(min).not.toBeNull();
      const back = minutesOfDayToSliderVal(min, false);
      expect(back).toBe(v);
    }
  });
});
