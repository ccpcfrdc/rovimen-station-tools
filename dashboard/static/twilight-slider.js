/*
 * twilight-slider.js
 * ──────────────────
 * Shared helpers for the noon-anchored 24h time slider used in two places:
 *   1) dashboard.html   — Video DB tab (#vdb-slider-start / #vdb-slider-end)
 *      drawn via dashboard-vdb.js's vdbDrawTwilightMarkers / vdbClampToTwilight.
 *   2) events.html      — multi-station event filter (#ev-slider-start / #ev-slider-end)
 *      drawn via events.html's evDrawTwilightMarkers / evClampToTwilight.
 *
 * Both surfaces previously carried ~200 lines of near-identical implementation;
 * bug fixes had to be applied twice and only sometimes propagated. The renderer
 * here is the canonical implementation. Page-specific code still owns the
 * fetch + slider IDs, but delegates marker drawing + clamp math to these
 * helpers so any future tweak (e.g. nautical vs civil twilight) lands in one
 * place.
 *
 * Exposed (on window so classic non-module bundles + inline scripts can call):
 *   utcMinToSliderVal(utcMin)
 *   minutesOfDayToSliderVal(min, isEnd)
 *   sliderValToHHMM(v)
 *   parseHHMM(str)
 *   drawTwilightOnWrap(wrapEl, tw)
 *   computeTwilightClampRange(tw)
 *   mountTwilightSlider(host, opts)   // legacy factory shape; see below
 */

(function () {
  'use strict';

  // ── Coordinate conversions ──────────────────────────────────────────────
  // The slider domain is noon-anchored: 0 = 12:00 UTC (noon), 720 = 00:00 UTC
  // (midnight), 1440 = 12:00 UTC next day. UTC minutes-of-day (0..1439) maps
  // onto the slider via ((utcMin - 720) + 1440) % 1440.
  function utcMinToSliderVal(utcMin) {
    return ((utcMin - 720) + 1440) % 1440;
  }

  function minutesOfDayToSliderVal(min, isEnd) {
    if (min === 720) return isEnd ? 1440 : 0;  // 12:00 — pick the matching end
    return ((min - 720) + 1440) % 1440;
  }

  function sliderValToHHMM(v) {
    const m = (parseInt(v, 10) + 720) % 1440;
    return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
  }

  // "HH:MM" or "HHMM" / "H:MM" → minutes-of-day, or null if invalid.
  function parseHHMM(str) {
    const s = String(str).trim().replace(/\s/g, '');
    let m = /^(\d{1,2}):(\d{2})$/.exec(s) || /^(\d{1,2})(\d{2})$/.exec(s);
    if (!m) return null;
    const h = parseInt(m[1], 10);
    const mn = parseInt(m[2], 10);
    if (h < 0 || h > 24 || mn < 0 || mn > 59 || (h === 24 && mn !== 0)) return null;
    return h * 60 + mn;
  }

  // ── Twilight clamp range ────────────────────────────────────────────────
  // Use civil twilight (-6°) as the approximate RMS capture window. Both VDB
  // and events.html used civil — keep that here. Returns {startVal, endVal}
  // in slider coordinates, sorted so start <= end.
  function computeTwilightClampRange(tw) {
    const sunsetSlider  = utcMinToSliderVal(tw.civil_sunset_min  ?? 1050);
    const sunriseSlider = utcMinToSliderVal(tw.civil_sunrise_min ?? 390);
    return {
      startVal: Math.min(sunsetSlider, sunriseSlider),
      endVal:   Math.max(sunsetSlider, sunriseSlider),
    };
  }

  // ── Renderer ────────────────────────────────────────────────────────────
  // Paints the 24h sky gradient + dim mask outside RMS capture window onto
  // the given .vdb-slider-wrap / .ev-slider-wrap element. Reads from tw the
  // server-side twilight payload (civil/nautical/sunset_min, moon_rise/set,
  // illumination, phase_emoji/phase_name) and writes two CSS custom
  // properties: --tl-gradient (the track background) and --moon-gradient
  // (optional). The caller's CSS uses these on ::before / ::after etc.
  function drawTwilightOnWrap(wrap, tw) {
    if (!wrap || !tw) return;

    const p = min => min == null ? null : (utcMinToSliderVal(min) / 1440 * 100).toFixed(2);
    const cs = p(tw.civil_sunset_min);       // DAY → CIV
    const ns = p(tw.nautical_sunset_min);    // CIV → NAUT
    const ss = p(tw.sunset_min);             // NAUT → NGHT
    const sr = p(tw.sunrise_min);            // NGHT → NAUT
    const nr = p(tw.nautical_sunrise_min);   // NAUT → CIV
    const cr = p(tw.civil_sunrise_min);      // CIV → DAY

    // RMS window ends 30 min past civil sunrise.
    const cr30 = cr != null
      ? Math.min(100, parseFloat(cr) + (30 / 1440 * 100)).toFixed(2)
      : cr;

    // 20px blend at every boundary (10px each side).
    const trackW = wrap.offsetWidth || 400;
    const half = (10 / trackW * 100);
    const lo = v => Math.max(0,   parseFloat(v) - half).toFixed(2);
    const hi = v => Math.min(100, parseFloat(v) + half).toFixed(2);

    // Section colours (4 sections: daytime, civil, nautical, night/astro).
    const DAY  = '#87ceeb';  // light sky blue
    const CIV  = '#a8b870';  // yellow-blue blend (civil twilight)
    const NAUT = '#d4c040';  // lighter yellow (nautical twilight)
    const NGHT = '#1a2c5e';  // dark navy (astronomical night)

    // Compute the midpoint of full night dynamically from astronomical
    // sunset (ss) and sunrise (sr) rather than assuming midnight (50%).
    // Near the summer solstice at mid-latitudes the astronomical sunrise
    // slider position can fall just before midnight, making a hardcoded
    // 50% midpoint create a backwards gradient stop and a visible gap.
    const nightMid = (ss != null && sr != null)
      ? ((parseFloat(ss) + parseFloat(sr)) / 2).toFixed(2)
      : '50';

    const sky = [
      `${DAY}  0%`,
      cs != null ? `${DAY}  ${lo(cs)}%` : `${DAY}  20%`,
      cs != null ? `${CIV}  ${hi(cs)}%` : `${CIV}  22%`,
      ns != null ? `${CIV}  ${lo(ns)}%` : `${CIV}  26%`,
      ns != null ? `${NAUT} ${hi(ns)}%` : `${NAUT} 28%`,
      ss != null ? `${NAUT} ${lo(ss)}%` : `${NAUT} 32%`,
      ss != null ? `${NGHT} ${hi(ss)}%` : `${NGHT} 34%`,
      `${NGHT} ${nightMid}%`,
      sr != null ? `${NGHT} ${lo(sr)}%` : `${NGHT} 64%`,
      sr != null ? `${NAUT} ${hi(sr)}%` : `${NAUT} 66%`,
      nr != null ? `${NAUT} ${lo(nr)}%` : `${NAUT} 70%`,
      nr != null ? `${CIV}  ${hi(nr)}%` : `${CIV}  72%`,
      cr != null ? `${CIV}  ${lo(cr)}%` : `${CIV}  74%`,
      cr != null ? `${DAY}  ${hi(cr)}%` : `${DAY}  76%`,
      `${DAY}  100%`,
    ];

    const DIM = 'rgba(13,17,23,0.50)';
    const CLR = 'rgba(13,17,23,0)';
    const mask = [`${DIM} 0%`];
    if (cs   != null) { mask.push(`${DIM} ${cs}%`,   `${CLR} ${cs}%`); }
    if (cr30 != null) { mask.push(`${CLR} ${cr30}%`, `${DIM} ${cr30}%`); }
    mask.push(`${DIM} 100%`);

    wrap.style.setProperty(
      '--tl-gradient',
      `linear-gradient(to right,${mask.join(',')}),linear-gradient(to right,${sky.join(',')})`
    );

    // Moon phase icon — IDs differ per page (vdb-moon-icon / ev-moon-icon).
    // Both pages call this same helper, so we update whichever exists.
    for (const moonIconId of ['vdb-moon-icon', 'ev-moon-icon']) {
      const moonIcon = document.getElementById(moonIconId);
      if (moonIcon && tw.phase_emoji) {
        moonIcon.textContent = tw.phase_emoji;
        moonIcon.title = `${tw.phase_name} — ${Math.round((tw.illumination ?? 0) * 100)}% illuminated`;
      }
    }

    // Moon bar (bottom edge of track).
    const illum = tw.illumination ?? 0;
    const mr = tw.moon_rise_min != null ? p(tw.moon_rise_min) : null;
    const ms = tw.moon_set_min  != null ? p(tw.moon_set_min)  : null;
    if (mr != null && ms != null && illum > 0.02) {
      const moonAlpha = (illum * 0.85 + 0.15).toFixed(2);
      const MC  = `rgba(255,255,255,${moonAlpha})`;
      const OFF = 'rgba(255,255,255,0)';
      const mrF = parseFloat(mr), msF = parseFloat(ms);
      let moonStops;
      if (mrF <= msF) {
        // Moon rises and sets within the same slider span (normal case).
        moonStops = [
          `${OFF} 0%`,
          `${OFF} ${lo(mr)}%`, `${MC} ${hi(mr)}%`,
          `${MC}  ${lo(ms)}%`, `${OFF} ${hi(ms)}%`,
          `${OFF} 100%`,
        ];
      } else {
        // Moon already up at noon, sets before rising again (wraps midnight).
        moonStops = [
          `${MC}  0%`,
          `${MC}  ${lo(ms)}%`, `${OFF} ${hi(ms)}%`,
          `${OFF} ${lo(mr)}%`, `${MC}  ${hi(mr)}%`,
          `${MC}  100%`,
        ];
      }
      wrap.style.setProperty('--moon-gradient', `linear-gradient(to right,${moonStops.join(',')})`);
    } else {
      wrap.style.removeProperty('--moon-gradient');
    }
  }

  // ── Optional factory: legacy mountTwilightSlider() shape ────────────────
  // Some callers may prefer a "host + opts" factory; today both consumers do
  // their own DOM wiring and just want the helpers above. Keep the factory
  // around as a thin convenience so future use-sites can wire a slider in
  // one call.
  function mountTwilightSlider(hostEl, opts) {
    opts = opts || {};
    if (!hostEl) return null;
    return {
      draw: (tw) => drawTwilightOnWrap(hostEl, tw),
      clampRange: (tw) => computeTwilightClampRange(tw),
    };
  }

  // ── Expose ──────────────────────────────────────────────────────────────
  window.twilightSlider = {
    utcMinToSliderVal,
    minutesOfDayToSliderVal,
    sliderValToHHMM,
    parseHHMM,
    drawTwilightOnWrap,
    computeTwilightClampRange,
    mountTwilightSlider,
  };
})();
