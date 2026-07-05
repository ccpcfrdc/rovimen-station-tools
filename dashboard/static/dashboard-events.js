import { escHtml, fetchJson, state, _toast } from './dashboard-common.js';
import { VideoModal } from './dashboard-video-modal.js';
import { MultiDetModal } from './dashboard-multi-det-modal.js';
import { gridInit, gridOnNightChange } from './dashboard-grid.js';
// ── Helpers ────────────────────────────────────────────────────────────────────

function fmtDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
  return d.toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
  return d.toISOString().slice(11, 19) + ' UTC';
}

function _fallbackDetectionOffset(filename, meteorTimeIso) {
  if (!filename || !meteorTimeIso) return 0;
  const fnM = filename.match(/_\d{8}_(\d{6})/);
  if (!fnM) return 0;
  const tp = fnM[1];
  const chkS = +tp.slice(0,2)*3600 + +tp.slice(2,4)*60 + +tp.slice(4,6);
  const d = new Date(meteorTimeIso + (meteorTimeIso.endsWith('Z') ? '' : 'Z'));
  if (isNaN(d)) return 0;
  const detS = d.getUTCHours()*3600 + d.getUTCMinutes()*60 + d.getUTCSeconds() + d.getUTCMilliseconds()/1000;
  let diff = detS - chkS;
  if (diff < -43200) diff += 86400;
  if (diff < 0) diff = 0;
  return Math.round(diff * 100) / 100;
}

// Active IntersectionObserver for the detections grid (Tab 1). Stored at module
// scope so evRerender() / a fresh renderDetections() call can disconnect a
// stale observer before building a new one.
let _detectionsIO = null;

// ── Night selector ─────────────────────────────────────────────────────────────

const datePicker = document.getElementById('ev-date');
datePicker.addEventListener('change', () => { loadDetections(); gridOnNightChange(); });

async function populateNightSelector() {
  try {
    const nights = await fetchJson('/api/detections/nights');
    if (!nights.length) return;
    const fmt = d => `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`;
    datePicker.innerHTML = nights.map(d => `<option value="${d}">${fmt(d)}</option>`).join('');
    datePicker.value = nights[0];
    loadDetections();
  } catch(e) {
    console.error('Failed to load nights', e);
  }
}

// ── Time slider helpers ───────────────────────────────────────────────────

function evSliderToFmt(v) {
  const m = (parseInt(v) + 720) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
}

// Parse "HH:MM" (or "HHMM" / "H:MM") \u2192 minutes-of-day, or null if invalid.
function evParseHHMM(str) {
  const s = String(str).trim().replace(/\s/g, '');
  let m = /^(\d{1,2}):(\d{2})$/.exec(s) || /^(\d{1,2})(\d{2})$/.exec(s);
  if (!m) return null;
  const h = parseInt(m[1]); const mn = parseInt(m[2]);
  if (h < 0 || h > 24 || mn < 0 || mn > 59 || (h === 24 && mn !== 0)) return null;
  return h * 60 + mn;
}

// minutes-of-day \u2192 slider value (slider is noon-anchored: 0 = noon, 720 = midnight, 1440 = next noon).
function evMinutesToSliderVal(min, isEnd) {
  if (min === 720) return isEnd ? 1440 : 0;  // 12:00 \u2014 pick the matching end
  return ((min - 720) + 1440) % 1440;
}

// Sync the editable inputs to the current slider values (without clobbering an input being edited).
function evUpdateTimeInputs() {
  const sV = parseInt(document.getElementById('ev-slider-start').value);
  const eV = parseInt(document.getElementById('ev-slider-end').value);
  const sIn = document.getElementById('ev-time-start');
  const eIn = document.getElementById('ev-time-end');
  if (sIn && document.activeElement !== sIn) { sIn.value = evSliderToFmt(sV); sIn.classList.remove('invalid'); }
  if (eIn && document.activeElement !== eIn) { eIn.value = evSliderToFmt(eV); eIn.classList.remove('invalid'); }
}

function evTimeInputChange(which) {
  const inEl = document.getElementById(which === 'start' ? 'ev-time-start' : 'ev-time-end');
  const slEl = document.getElementById(which === 'start' ? 'ev-slider-start' : 'ev-slider-end');
  const min = evParseHHMM(inEl.value);
  if (min === null) { inEl.classList.add('invalid'); return; }
  inEl.classList.remove('invalid');
  slEl.value = evMinutesToSliderVal(min, which === 'end');
  evSliderInput(which);  // clamps + re-renders + writes back the formatted value
}

let _evSliderTimer = null;
function evSliderInput(which) {
  const sEl = document.getElementById('ev-slider-start');
  const eEl = document.getElementById('ev-slider-end');
  if (which === 'start' && parseInt(sEl.value) > parseInt(eEl.value)) sEl.value = eEl.value;
  if (which === 'end' && parseInt(eEl.value) < parseInt(sEl.value)) eEl.value = sEl.value;
  evUpdateTimeInputs();
  clearTimeout(_evSliderTimer);
  _evSliderTimer = setTimeout(evRerender, 150);
}

// ── Azimuth / pointing filter ─────────────────────────────────────────────────

const EV_AZ_HALF = 50; // ±50° = 100° window
const _EV_COMPASS_AZ = {N: 0, E: 90, S: 180, W: 270};
let _camAzimuths = {};  // camCode → az_centre degrees (populated at init)
let _evAzActive = false;
let _evAzCenter = 0;
let _evStationsCache = null;

async function _evGetStations() {
  if (_evStationsCache) return _evStationsCache;
  _evStationsCache = await fetchJson('/api/stations');
  return _evStationsCache;
}

async function evLoadPlatepars() {
  // Mark compass as loading until platepars arrive
  const compassEl = document.querySelector('.ev-compass');
  if (compassEl) compassEl.classList.add('loading');
  try {
    const stations = await _evGetStations();
    await Promise.all(Object.keys(stations).map(async hk => {
      try {
        const r = await fetch(`/api/platepar/${hk}`);
        if (!r.ok) return;
        const pp = await r.json();
        for (const [cam, d] of Object.entries(pp)) {
          if (d && d.az_centre != null) _camAzimuths[cam] = d.az_centre;
        }
      } catch(e) {}
    }));
  } catch(e) {}
  // Remove loading state from compass
  if (compassEl) compassEl.classList.remove('loading');
  // Re-render now that azimuths are available (especially if user toggled a
  // compass direction while platepars were still loading)
  if (_data) {
    renderDetections(_data);
    renderEvents(_data);
  }
}

function _azCompass(deg) {
  const dirs = ['N','NE','E','SE','S','SW','W','NW'];
  return dirs[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
}

function evInAzWindow(camCode) {
  if (!_evAzActive) return true;
  const az = _camAzimuths[camCode];
  if (az == null) return true; // unknown camera — show it
  return [...document.querySelectorAll('.ev-compass-slice.active')].some(s => {
    const center = _EV_COMPASS_AZ[s.dataset.dir];
    let diff = Math.abs(((az - center) + 360) % 360);
    if (diff > 180) diff = 360 - diff;
    return diff <= EV_AZ_HALF;
  });
}

function _azDiff(a, b) {
  const d = Math.abs(((a - b) + 360) % 360);
  return d > 180 ? 360 - d : d;
}

/** Circular mean of an array of azimuth degrees. */
function _azMean(azimuths) {
  const s = azimuths.reduce((a, v) => a + Math.sin(v * Math.PI / 180), 0);
  const c = azimuths.reduce((a, v) => a + Math.cos(v * Math.PI / 180), 0);
  return (Math.atan2(s, c) * 180 / Math.PI + 360) % 360;
}

/**
 * From a list of witnesses, return the largest subset where all cameras
 * are within 90° of each other (using best-anchor greedy), plus any
 * witnesses whose camera azimuth is unknown (always kept).
 * Returns null if the result spans < 2 stations.
 */
function evAzCluster(witnesses) {
  if (Object.keys(_camAzimuths).length === 0) return witnesses; // not loaded yet
  const known = witnesses.filter(w => _camAzimuths[w.cam] != null);
  const unknown = witnesses.filter(w => _camAzimuths[w.cam] == null);
  if (known.length === 0) return witnesses;

  let best = [];
  for (const anchor of known) {
    const az0 = _camAzimuths[anchor.cam];
    const cluster = known.filter(w => _azDiff(_camAzimuths[w.cam], az0) <= 90);
    if (cluster.length > best.length) best = cluster;
  }
  const result = [...best, ...unknown];
  const stations = new Set(result.map(w => w.host_key));
  return stations.size >= 2 ? result : null; // null = not a valid multi-station event
}

function evCompassClick(dir) {
  if (Object.keys(_camAzimuths).length === 0) {
    _toast('Platepars still loading — compass filter not ready yet', 'info');
    return;
  }
  document.querySelector(`.ev-compass-slice[data-dir="${dir}"]`).classList.toggle('active');
  _evAzActive = document.querySelectorAll('.ev-compass-slice.active').length > 0;
  evRerender();
}

function evRerender() {
  if (!_data) return;
  renderDetections(_data);
  renderEvents(_data);
}

/** Convert UTC minutes-from-midnight to slider value (0=12:00, 720=00:00, 1440=12:00+1).
 *  Delegates to the shared twilight-slider helper so VDB + events stay in sync. */
function evUtcMinToSlider(utcMin) { return window.twilightSlider.utcMinToSliderVal(utcMin); }

/** Paint the time slider track with a 24h sky gradient + dim mask outside RMS.
 *  Renderer lives in twilight-slider.js (A-3 dedupe). */
function evDrawTwilightMarkers(tw) {
  const wrap = document.querySelector('.ev-slider-wrap');
  window.twilightSlider.drawTwilightOnWrap(wrap, tw);
}

async function evClampToTwilight(dateYMD) {
  try {
    const stations = await _evGetStations();
    const roStation = Object.keys(stations).find(k => k.startsWith('gmnro')) || Object.keys(stations)[0];
    if (!roStation) return;
    const r = await fetch(`/api/twilight/${roStation}/${dateYMD}`);
    if (!r.ok) return;
    const tw = await r.json();
    const sEl = document.getElementById('ev-slider-start');
    const eEl = document.getElementById('ev-slider-end');
    // Civil twilight (-6°) clamp range from the shared helper.
    const { startVal, endVal } = window.twilightSlider.computeTwilightClampRange(tw);
    sEl.value = startVal;
    eEl.value = endVal;
    evUpdateTimeInputs();
    evDrawTwilightMarkers(tw);
  } catch (e) { /* keep defaults */ }
}

/** Check if an ISO meteor_time falls within the current slider window. */
function evInTimeWindow(meteorTime) {
  const sVal = parseInt(document.getElementById('ev-slider-start').value);
  const eVal = parseInt(document.getElementById('ev-slider-end').value);
  if (sVal === 0 && eVal === 1440) return true;
  if (!meteorTime) return true;
  const d = new Date(meteorTime + (meteorTime.endsWith('Z') ? '' : 'Z'));
  const utcMin = d.getUTCHours() * 60 + d.getUTCMinutes();
  const sliderVal = ((utcMin - 720) + 1440) % 1440;
  return sliderVal >= sVal && sliderVal <= eVal;
}

// ── Zoom slider ───────────────────────────────────────────────────────────

function evUpdateZoom(val) {
  document.documentElement.style.setProperty('--ev-card-w', val + 'px');
  document.documentElement.style.setProperty('--ev-card-h', Math.round(val * 0.745) + 'px');
  // Keep the two sliders (global filter bar + Sort&Filter toolbar) in sync
  for (const id of ['ev-zoom', 'sf-zoom']) {
    const el = document.getElementById(id);
    if (el && el.value !== String(val)) el.value = val;
  }
}

// ── Tab switching ──────────────────────────────────────────────────────────────

// state.activeTab is declared in dashboard-common.js (shared across pages);
// assigning without `let` mutates that binding.
state.activeTab = 'detections';
function switchTab(name) {
  state.activeTab = name;
  document.querySelectorAll('.ev-tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.ev-pane').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-btn-' + name).classList.add('active');
  document.getElementById('pane-' + name).classList.add('active');
  // The global filter bar (Night/Time/Pointing) belongs to tabs 1–2.
  // Sort & Filter and Grid have their own toolbars inside the pane.
  // Grid still uses the shared Night selector, so keep that group visible but
  // hide the Time slider + Pointing compass (the grid has its own time filter).
  const globalBar = document.querySelector('.ev-filter-bar');
  if (globalBar) globalBar.style.display = (name === 'sortfilter') ? 'none' : '';
  const timeGroup = document.querySelector('.ev-filter-group--time');
  const pointingGroup = document.querySelector('.ev-filter-group:has(.ev-compass)');
  const isGrid = name === 'grid';
  if (timeGroup) timeGroup.style.display = isGrid ? 'none' : '';
  if (pointingGroup) pointingGroup.style.display = isGrid ? 'none' : '';
  if (name === 'sortfilter') sfInit();
  if (name === 'grid') gridInit();
}

// ── Data loading ───────────────────────────────────────────────────────────────

let _data = null;
let _stationsInfo = {};

async function loadDetections() {
  const date = datePicker.value;
  if (!date) return;

  document.getElementById('detections-main').innerHTML =
    '<div class="ev-loading"><div class="ev-spinner"></div> Loading detections…</div>';
  document.getElementById('events-main').innerHTML =
    '<div class="ev-loading"><div class="ev-spinner"></div> Loading events…</div>';
  // Clamp time sliders to twilight
  evClampToTwilight(date);

  try {
    const r = await fetch(`/api/detections/${date}`);
    if (!r.ok) throw new Error(r.statusText);
    _data = await r.json();
    renderDetections(_data);
    renderEvents(_data);
  } catch(e) {
    const msg = `<div class="ev-empty">Failed to load detections: ${e.message}</div>`;
    document.getElementById('detections-main').innerHTML = msg;
    document.getElementById('events-main').innerHTML = msg;
  }
}

// ── Tab 1: All Detections ──────────────────────────────────────────────────────

function renderDetections(data) {
  const el = document.getElementById('detections-main');
  const byStation = data.by_station || {};
  const dateStr = data.date || '';

  // Disconnect any pagination observer from a previous render — otherwise it
  // would keep firing against orphaned sentinels (or worse, the wrong ones).
  if (_detectionsIO) {
    _detectionsIO.disconnect();
    _detectionsIO = null;
  }

  if (Object.keys(byStation).length === 0) {
    el.innerHTML = '<div class="ev-empty">No stations available.</div>';
    return;
  }

  // Build O(1) lookups for "is this (filename, cam) part of a multi-station event?".
  const multiSet    = new Set();
  const filenameToEv = new Map();  // 'filename:cam' → event object
  for (const ev of (data.events || [])) {
    if (!ev || ev.witness_count <= 1) continue;
    for (const w of (ev.witnesses || [])) {
      const key = w.filename + ':' + w.cam;
      multiSet.add(key);
      filenameToEv.set(key, ev);
    }
  }

  // First pass: render station + camera scaffolding, and collect card HTML
  // strings into a flat array so the IntersectionObserver can stream them
  // into their target .ev-cards containers without parsing the whole night
  // up front.
  let scaffoldHtml = '';
  const cardHtml = [];   // ordered: { containerIdx, html }
  const containerSelectors = []; // one entry per .ev-cards container
  let containerCounter = 0;

  const stationOrder = Object.entries(byStation).sort((a, b) => a[0].localeCompare(b[0]));
  for (const [hostKey, st] of stationOrder) {
    const cameras = st.cameras || {};
    const filteredCameras = {};
    for (const [cam, chunks] of Object.entries(cameras)) {
      if (!evInAzWindow(cam)) continue;
      filteredCameras[cam] = chunks.filter(c => evInTimeWindow(c.meteor_time));
    }
    const totalDetections = Object.values(filteredCameras).reduce((s, c) => s + c.length, 0);

    scaffoldHtml += `<div class="ev-station">`;
    scaffoldHtml += `<div class="ev-station-hdr">
      <span class="ev-station-name${st.online ? '' : ' offline'}">${escHtml(st.label)}</span>
      <span class="ev-station-key">${escHtml(hostKey)}</span>
      ${totalDetections > 0 ? `<span class="ev-cam-count">${totalDetections} detection${totalDetections !== 1 ? 's' : ''}</span>` : ''}
    </div>`;

    if (!st.online) {
      scaffoldHtml += `<div class="ev-offline-msg">Station offline — showing archived detections</div>`;
    }
    for (const [camCode, chunks] of Object.entries(filteredCameras)) {
      scaffoldHtml += `<div class="ev-cam-section">`;
      scaffoldHtml += `<div class="ev-cam-label">
        ${escHtml(camCode)}
        ${chunks.length > 0 ? `<span class="ev-cam-count">${chunks.length}</span>` : ''}
      </div>`;

      if (chunks.length === 0) {
        scaffoldHtml += `<div class="ev-no-detections">No detections</div>`;
      } else {
        const containerIdx = containerCounter++;
        const sel = `ev-cards-${containerIdx}`;
        containerSelectors.push(sel);
        scaffoldHtml += `<div class="ev-cards" data-cards-id="${sel}"></div>`;

        for (const chunk of chunks) {
          const mt = fmtTime(chunk.meteor_time);
          const stackUrl = chunk.stack
            ? `/stack/${hostKey}/${camCode}/${dateStr}/${chunk.stack}`
            : null;
          const isMulti = multiSet.has(chunk.filename + ':' + camCode);
          const dlUrl = `/download/${hostKey}/${camCode}/${dateStr}/${chunk.filename}`;
          const dlFilename = chunk.filename;
          const parentEv = filenameToEv.get(chunk.filename + ':' + camCode) || null;
          _evCardData.set(`${camCode}:${dateStr}:${chunk.filename}`, {
            stackUrl: stackUrl || null,
            detection: chunk.rms ? {
              mag_apparent:     chunk.rms.mag_apparent,
              shower:           chunk.rms.shower,
              duration_s:       chunk.rms.duration_s,
              angular_velocity: chunk.rms.angular_velocity,
              time_utc:         chunk.meteor_time,
              camera:           camCode,
            } : null,
            parentEvent:     parentEv,
            parentEventDate: dateStr,
          });
          const cardMeta = escHtml(JSON.stringify({
            hostKey, camCode, dateStr,
            filename: chunk.filename,
            offsetS: chunk.detection_offset_s || 0,
            meteorTime: chunk.meteor_time,
            dlUrl, dlFilename,
          }));
          let card = `<div class="ev-card ev-card-trigger" data-card="${cardMeta}">`;
          if (stackUrl) {
            card += `<img src="${stackUrl}" loading="lazy" alt="${escHtml(camCode)} ${mt}">`;
          } else {
            card += `<div class="ev-card-no-img">No image</div>`;
          }
          card += `<div class="ev-card-overlay">${mt}</div>`;
          if (isMulti) card += `<div class="ev-card-multi">MULTI</div>`;
          // Download stack / clip are operator-only. For an anonymous public
          // visitor the underlying endpoints are gated, so painting the hover
          // buttons would just be dead controls — omit them entirely. The card
          // itself still opens the video modal (public playback).
          if (state.AUTH_USER) {
            if (stackUrl) card += `<a class="ev-card-dl ev-card-dl-stack ev-card-dl-stack-trigger" title="Download stack" href="${escHtml(stackUrl)}" download>&#9633;</a>`;
            card += `<button class="ev-card-dl ev-card-dl-clip-trigger" title="Download clip">&#10515;</button>`;
          }
          card += `</div>`;
          cardHtml.push({ containerIdx, html: card });
        }
      }
      scaffoldHtml += `</div>`;
    }
    scaffoldHtml += `</div>`;
  }

  if (!scaffoldHtml) {
    el.innerHTML = '<div class="ev-empty">No detections found.</div>';
    return;
  }

  el.innerHTML = scaffoldHtml + '<div class="ev-load-sentinel"></div>';
  // Single delegated listener covers all ev-card-trigger cards, including
  // those inserted lazily via insertAdjacentHTML. All server-controlled
  // strings come from data-card (JSON, HTML-escaped at build time).
  el.addEventListener('click', _evCardClick);

  // Resolve container element handles once after scaffolding lands in the DOM.
  const containerEls = containerSelectors.map(sel =>
    el.querySelector(`[data-cards-id="${sel}"]`)
  );
  const sentinel = el.querySelector('.ev-load-sentinel');

  // Group consecutive cards by container so each batch produces minimal
  // insertAdjacentHTML calls.
  const BATCH = 100;
  let rendered = 0;
  function renderBatch() {
    if (rendered >= cardHtml.length) return false;
    const end = Math.min(rendered + BATCH, cardHtml.length);
    let i = rendered;
    while (i < end) {
      const containerIdx = cardHtml[i].containerIdx;
      let chunkStr = '';
      let j = i;
      while (j < end && cardHtml[j].containerIdx === containerIdx) {
        chunkStr += cardHtml[j].html;
        j++;
      }
      const target = containerEls[containerIdx];
      if (target) target.insertAdjacentHTML('beforeend', chunkStr);
      i = j;
    }
    rendered = end;
    return rendered < cardHtml.length;
  }

  // First batch renders synchronously so the user sees content immediately.
  const more = renderBatch();
  if (!more) {
    sentinel.remove();
    return;
  }

  _detectionsIO = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      let hasMore = true;
      while (hasMore) hasMore = renderBatch();
      _detectionsIO.disconnect();
      _detectionsIO = null;
      sentinel.remove();
    }
  }, { root: null, rootMargin: '400px' });
  _detectionsIO.observe(sentinel);
}

// ── Tab 2: Multi-Station Events ────────────────────────────────────────────────

function renderEvents(data) {
  const el = document.getElementById('events-main');
  // Build display events: apply azimuth clustering + user filters
  const displayEvents = [];
  for (const ev of (data.events || [])) {
    if (ev.witness_count <= 1) continue;
    if (!evInTimeWindow(ev.event_time)) continue;
    const clustered = evAzCluster(ev.witnesses);
    if (!clustered) continue; // pointing-inconsistent — discard
    if (!clustered.some(w => evInAzWindow(w.cam))) continue; // user pointing filter
    displayEvents.push({...ev, witnesses: clustered, witness_count: clustered.length});
  }
  const dateStr = data.date || '';

  if (displayEvents.length === 0) {
    el.innerHTML = `<div class="ev-empty">No multi-station events found for this night.<br>
      <span style="font-size:12px">Events require detections from at least 2 different stations within ${data.correlation_window_s || 1}s of each other, pointing in a similar direction.</span></div>`;
    return;
  }

  // Sort newest first
  displayEvents.sort((a, b) => b.event_time.localeCompare(a.event_time));

  let html = `<div class="ev-event-list">`;
  for (const ev of displayEvents) {
    const stationSet = new Set(ev.witnesses.map(w => w.host_key));
    const stationCount = stationSet.size;
    const camCount = ev.witness_count;
    const timeLabel = fmtDateTime(ev.event_time);

    // Compute mean pointing direction from witnesses with known azimuth
    const knownAz = ev.witnesses.map(w => _camAzimuths[w.cam]).filter(a => a != null);
    const azBadge = knownAz.length > 0
      ? `<span class="ev-badge az" title="${Math.round(_azMean(knownAz))}°">${_azCompass(_azMean(knownAz))}</span>`
      : '';

    const evJson = JSON.stringify(ev).replace(/"/g, '&quot;');
    html += `<div class="ev-event" onclick="openEventModal(${evJson}, '${dateStr}')">`;
    html += `<div class="ev-event-hdr">
      <span class="ev-event-time">${timeLabel}</span>
      <div class="ev-event-badges">
        <span class="ev-badge cameras">${camCount} camera${camCount !== 1 ? 's' : ''}</span>
        <span class="ev-badge stations">${stationCount} station${stationCount !== 1 ? 's' : ''}</span>
        ${azBadge}
      </div>
    </div>`;
    html += `<div class="ev-event-stacks">`;
    for (const w of ev.witnesses) {
      const stackUrl = w.stack
        ? `/stack/${w.host_key}/${w.cam}/${dateStr}/${w.stack}`
        : null;
      html += `<div>`;
      if (stackUrl) {
        html += `<img class="ev-event-thumb" src="${stackUrl}" loading="lazy" alt="${escHtml(w.cam)}">`;
      }
      html += `<div class="ev-event-thumb-info">${escHtml(w.cam)} — <span style="color:var(--text)">${escHtml(w.station_label)}</span></div>`;
      html += `</div>`;
    }
    html += `</div></div>`;
  }
  html += `</div>`;
  el.innerHTML = html;
}

// ── Tab 3: Sort & Filter (date-range, flat chronological, sortable) ───────────

let _sfData = null;            // last fetched flat detections list (with date)
let _sfSelectedShowers = new Set();
let _sfInited = false;

// Pre-select shower from URL param (?shower=PER) — set before sfInit() runs.
const _urlShower = new URLSearchParams(location.search).get('shower')?.toUpperCase();
if (_urlShower) _sfSelectedShowers.add(_urlShower);

function sfInit() {
  if (_sfInited) return;
  _sfInited = true;
  // Default range: last 7 days (today inclusive)
  const today = new Date();
  const fmt = d => d.toISOString().slice(0, 10);
  const from = new Date(today); from.setUTCDate(today.getUTCDate() - 6);
  document.getElementById('sf-from').value = fmt(from);
  document.getElementById('sf-to').value = fmt(today);
  sfReload();
}

function sfDateInputToYYYYMMDD(v) {
  // <input type=date> gives YYYY-MM-DD; backend wants YYYYMMDD.
  return (v || '').replaceAll('-', '');
}

async function sfReload() {
  const fromEl = document.getElementById('sf-from');
  const toEl = document.getElementById('sf-to');
  const main = document.getElementById('sortfilter-main');
  const from = sfDateInputToYYYYMMDD(fromEl.value);
  const to = sfDateInputToYYYYMMDD(toEl.value);
  if (!from || !to) {
    main.innerHTML = '<div class="ev-empty">Pick a from and to date.</div>';
    return;
  }
  if (from > to) {
    main.innerHTML = '<div class="ev-empty">From date must be on or before To date.</div>';
    return;
  }
  main.innerHTML = '<div class="ev-loading"><div class="ev-spinner"></div> Loading detections…</div>';
  try {
    const r = await fetch(`/api/detections/range?from=${from}&to=${to}`);
    if (!r.ok) {
      const txt = await r.text().catch(() => '');
      throw new Error(txt || r.statusText);
    }
    const payload = await r.json();
    _sfData = payload.detections || [];
    _sfBuildShowerChips();
    sfRender();
  } catch(e) {
    _sfData = null;
    main.innerHTML = `<div class="ev-empty">Failed to load: ${e.message}</div>`;
  }
}

function _sfBuildShowerChips() {
  const counts = {};
  for (const d of _sfData || []) {
    const s = (d.rms && d.rms.shower) || null;
    if (s) counts[s] = (counts[s] || 0) + 1;
  }
  // Drop any selections no longer present
  for (const s of [..._sfSelectedShowers]) if (!(s in counts)) _sfSelectedShowers.delete(s);
  const entries = Object.entries(counts).sort((a, b) => {
    // SPO last, others alphabetical
    if (a[0] === 'SPO') return 1;
    if (b[0] === 'SPO') return -1;
    return a[0].localeCompare(b[0]);
  });
  const wrap = document.getElementById('sf-shower-chips');
  if (!entries.length) {
    wrap.innerHTML = '<span style="font-size:11px;color:var(--muted)">No RMS-detected meteors in this range.</span>';
    return;
  }
  let html = '';
  for (const [s, n] of entries) {
    const active = _sfSelectedShowers.has(s) ? ' active' : '';
    html += `<span class="sf-chip${active}" data-shower="${s}" onclick="sfToggleShower('${s}')">${s}<span class="sf-chip-count">${n}</span></span>`;
  }
  wrap.innerHTML = html;
}

function sfToggleShower(s) {
  if (_sfSelectedShowers.has(s)) _sfSelectedShowers.delete(s);
  else _sfSelectedShowers.add(s);
  // Refresh chip visuals + cards
  _sfBuildShowerChips();
  sfRender();
}

function _sfPassesShowerFilter(d) {
  if (_sfSelectedShowers.size === 0) return true;
  const s = d.rms && d.rms.shower;
  return s && _sfSelectedShowers.has(s);
}

function sfRender() {
  const main = document.getElementById('sortfilter-main');
  if (!_sfData) {
    main.innerHTML = '<div class="ev-empty">No data loaded.</div>';
    return;
  }
  const detected = [];
  const undetected = [];
  for (const d of _sfData) {
    if (d.rms && (d.rms.shower || d.rms.mag_apparent != null)) detected.push(d);
    else undetected.push(d);
  }
  const detectedFiltered = detected.filter(_sfPassesShowerFilter);

  const sortMode = document.getElementById('sf-sort').value;
  const cmpTimeAsc = (a, b) => (a.meteor_time || '').localeCompare(b.meteor_time || '');
  const cmpTimeDesc = (a, b) => (b.meteor_time || '').localeCompare(a.meteor_time || '');
  const magOf = d => (d.rms && d.rms.mag_apparent != null) ? d.rms.mag_apparent : null;
  const durOf = d => (d.rms && d.rms.duration_s != null) ? d.rms.duration_s : null;
  const cmpMagBright = (a, b) => {
    const ma = magOf(a), mb = magOf(b);
    if (ma == null && mb == null) return cmpTimeDesc(a, b);
    if (ma == null) return 1;
    if (mb == null) return -1;
    return ma - mb; // smaller = brighter
  };
  const cmpMagFaint = (a, b) => -cmpMagBright(a, b);
  const cmpDurDesc = (a, b) => {
    const da = durOf(a), db = durOf(b);
    if (da == null && db == null) return cmpTimeDesc(a, b);
    if (da == null) return 1;
    if (db == null) return -1;
    return db - da;
  };
  const cmpShower = (a, b) => {
    const sa = (a.rms && a.rms.shower) || 'ZZZ';
    const sb = (b.rms && b.rms.shower) || 'ZZZ';
    if (sa === sb) return cmpTimeDesc(a, b);
    return sa.localeCompare(sb);
  };
  const cmp = ({
    time_desc: cmpTimeDesc, time_asc: cmpTimeAsc,
    mag_bright: cmpMagBright, mag_faint: cmpMagFaint,
    duration_desc: cmpDurDesc, shower: cmpShower,
  })[sortMode] || cmpTimeDesc;
  detectedFiltered.sort(cmp);
  // Undetected always chronological newest-first.
  undetected.sort(cmpTimeDesc);

  let html = '';
  html += `<div class="sf-section-hdr">
    <span class="sf-section-title">RMS-detected</span>
    <span class="sf-section-sub">${detectedFiltered.length} of ${detected.length}${_sfSelectedShowers.size ? ' shown' : ''}</span>
  </div>`;
  html += '<div class="sf-cards">';
  if (!detectedFiltered.length) {
    html += `<div class="ev-empty" style="padding:0">No RMS detections match the current filter.</div>`;
  } else {
    for (const d of detectedFiltered) html += _sfCardHtml(d, true);
  }
  html += '</div>';

  html += `<div class="sf-section-hdr">
    <span class="sf-section-title">Locked clips without metadata</span>
    <span class="sf-section-sub">${undetected.length} clip${undetected.length === 1 ? '' : 's'} · chronological</span>
  </div>`;
  html += '<div class="sf-cards">';
  if (!undetected.length) {
    html += `<div class="ev-empty" style="padding:0">All locked clips in this range have detection metadata.</div>`;
  } else {
    for (const d of undetected) html += _sfCardHtml(d, false);
  }
  html += '</div>';

  main.innerHTML = html;
}

function _sfCardHtml(d, hasMeta) {
  const stackUrl = d.stack ? `/stack/${d.host_key}/${d.cam}/${d.date}/${d.stack}` : null;
  const dt = d.meteor_time ? new Date(d.meteor_time + (d.meteor_time.endsWith('Z') ? '' : 'Z')) : null;
  const dateLabel = dt ? dt.toISOString().slice(5, 10) + ' ' + dt.toISOString().slice(11, 19) : '';
  const shower = (d.rms && d.rms.shower) || null;
  const showerCls = shower === 'SPO' ? ' spo' : '';
  const mag = (d.rms && d.rms.mag_apparent != null) ? d.rms.mag_apparent : null;
  const dur = (d.rms && d.rms.duration_s != null) ? d.rms.duration_s : null;
  const angVel = (d.rms && d.rms.angular_velocity != null) ? d.rms.angular_velocity : null;
  const magStr = mag != null ? `${mag >= 0 ? '+' : ''}${mag.toFixed(1)}m` : '';
  const durStr = dur != null ? `${dur.toFixed(1)}s` : '';
  const velStr = angVel != null ? `${angVel.toFixed(0)}°/s` : '';
  const stationCam = `${d.station_label || d.host_key} · ${d.cam}`;
  const filenameSafe = (d.filename || '').replace(/'/g, "\\'");
  _evCardData.set(`${d.cam}:${d.date}:${d.filename}`, {
    stackUrl: stackUrl || null,
    detection: d.rms ? {
      mag_apparent:     d.rms.mag_apparent,
      shower:           d.rms.shower || d.shower,
      duration_s:       d.rms.duration_s,
      angular_velocity: d.rms.angular_velocity,
      time_utc:         d.meteor_time,
      station:          d.station_label || d.host_key,
      camera:           d.cam,
    } : null,
  });
  const onClick = `openSingleVideo('${d.host_key}','${d.cam}','${d.date}','${filenameSafe}',${d.detection_offset_s || 0},'${d.meteor_time}')`;
  const cartKey = `${d.host_key}|${d.cam}|${d.date}|${d.filename}`;
  const inCart = compCartHas(cartKey);
  // Compilation cart + per-card download/stack are operator-only. Anonymous
  // visitors see the card (opens public playback) but none of the operator
  // controls, whose endpoints are gated server-side anyway.
  const _op = !!state.AUTH_USER;
  const cartBtn = _op ? `<button class="sf-cart-toggle${inCart ? ' in-cart' : ''}" `
    + `onclick="event.stopPropagation();compCartToggle(${JSON.stringify(d).replace(/"/g, '&quot;')})" `
    + `title="${inCart ? 'Remove from compilation' : 'Add to compilation'}">${inCart ? '✓' : '+'}</button>` : '';
  const sfDlUrl = `/download/${d.host_key}/${d.cam}/${d.date}/${d.filename}`;
  const sfDlBtn = _op ? (`<button class="ev-card-dl" style="top:auto;bottom:26px;opacity:0" `
    + `onclick="event.stopPropagation();evDownloadClip('${sfDlUrl}','${d.filename}',event)" `
    + `title="Download clip">&#10515;</button>`
    + (stackUrl ? `<a class="ev-card-dl ev-card-dl-stack" style="top:auto;bottom:26px;left:28px;opacity:0" `
    + `href="${stackUrl}" download onclick="event.stopPropagation()" title="Download stack">&#9633;</a>` : '')) : '';
  let html = `<div class="sf-card${inCart ? ' in-cart' : ''}" onclick="${onClick}" title="${stationCam}">`;
  html += cartBtn;
  html += `<div class="sf-card-img">`;
  if (stackUrl) {
    html += `<img src="${stackUrl}" loading="lazy" alt="${d.cam} ${dateLabel}">`;
  } else {
    html += `<div class="sf-card-no-img">no image</div>`;
  }
  if (shower && hasMeta) html += `<div class="sf-card-shower${showerCls}">${shower}</div>`;
  html += `<div class="sf-card-time">${dateLabel}</div>`;
  html += sfDlBtn;
  html += `</div>`;
  html += `<div class="sf-card-meta">`;
  html += `<div class="sf-card-meta-line1" title="${stationCam}">${d.cam}</div>`;
  if (hasMeta) {
    const parts = [magStr, durStr, velStr].filter(Boolean);
    html += `<div class="sf-card-meta-line2">${parts.join(' · ') || '—'}</div>`;
  } else {
    html += `<div class="sf-card-meta-line2">${d.lock_type === 'manual' ? 'manual' : 'locked'}</div>`;
  }
  html += `</div>`;
  html += `</div>`;
  return html;
}

// ── Delegated click handler for ev-card-trigger cards ─────────────────────────
// All server-controlled strings (filename, meteorTime, dlUrl, dlFilename)
// are read from data-card (JSON, HTML-escaped at render time) so they never
// appear in an onclick attribute context.

function _evCardClick(e) {
  const dlStack = e.target.closest('.ev-card-dl-stack-trigger');
  if (dlStack) {
    e.stopPropagation();
    return; // native <a download> handles it
  }
  const dlClip = e.target.closest('.ev-card-dl-clip-trigger');
  if (dlClip) {
    e.stopPropagation();
    const card = dlClip.closest('.ev-card-trigger');
    if (!card) return;
    const d = JSON.parse(card.dataset.card || '{}');
    evDownloadClip(d.dlUrl, d.dlFilename, e);
    return;
  }
  const card = e.target.closest('.ev-card-trigger');
  if (card) {
    const d = JSON.parse(card.dataset.card || '{}');
    openSingleVideo(d.hostKey, d.camCode, d.dateStr, d.filename, d.offsetS, d.meteorTime);
  }
}

// ── Modal: single video ────────────────────────────────────────────────────────

function openSingleVideo(hostKey, cam, date, filename, offsetS, meteorTimeIso) {
  const resolved = offsetS || _fallbackDetectionOffset(filename, meteorTimeIso);
  const extra = _evCardData.get(`${cam}:${date}:${filename}`) || {};
  const src = `/api/cached-video/${hostKey}/${cam}/${date}/${encodeURIComponent(filename)}?format=mp4`;
  const title = [cam, meteorTimeIso ? fmtDateTime(meteorTimeIso) : date].filter(Boolean).join(' · ');

  let switchToMulti = null;
  if (extra.parentEvent) {
    const ev  = extra.parentEvent;
    const dt  = extra.parentEventDate;
    const cnt = ev.witness_count || (ev.witnesses?.length ?? 0);
    switchToMulti = {
      label:   `All stations (${cnt} cameras) →`,
      onClick: () => { VideoModal.closeAll(); openEventModal(ev, dt); },
    };
  }

  _evSingleModal.open({
    src,
    title,
    detOffset:     resolved || null,
    trimStart:     0,
    station:       hostKey,
    camera:        cam,
    date,
    filename,
    stack:         extra.stackUrl ? { url: extra.stackUrl } : null,
    detection:     extra.detection || null,
    download:      { onClick: () => evDownloadClip(`/shortclip/${hostKey}/${cam}/${date}/${encodeURIComponent(filename)}`, filename, null) },
    switchToMulti,
  });
}

// ── Modal ─────────────────────────────────────────────────────────────────────

// Extra card data keyed by "cam:date:filename" — populated at card-build time
const _evCardData = new Map();

const _evModal = new MultiDetModal(
  document.getElementById('ev-modal-container')
);
const _evSingleModal = new VideoModal(
  document.getElementById('ev-single-modal-container'),
  { trim: true, nav: false }
);
if (!window._VideoModalCloseAll) window._VideoModalCloseAll = () => {
  VideoModal.closeAll();
  MultiDetModal.closeAll();
};

function openEventModal(ev, date) {
  const videos = ev.witnesses.map(w => {
    const offset = w.detection_offset_s || _fallbackDetectionOffset(w.filename, w.meteor_time);
    const stackUrl = w.stack
      ? `/stack/${w.host_key}/${w.cam}/${date}/${w.stack}`
      : null;
    return {
      cam:         w.cam,
      station:     w.host_key,
      date,
      url:         `/api/cached-video/${w.host_key}/${w.cam}/${date}/${encodeURIComponent(w.filename)}?format=mp4`,
      offset,
      label:       w.station_label,
      downloadUrl: `/shortclip/${w.host_key}/${w.cam}/${date}/${encodeURIComponent(w.filename)}`,
      stackUrl,
      filename:    w.filename,
    };
  });

  _evModal.open({ title: fmtDateTime(ev.event_time), videos, windowSec: 5 });
}

function closeModal() {
  _evModal.close();
}

// ── Compilation cart ──────────────────────────────────────────────────────────

const COMP_CART_KEY = 'rovimen_compilation_cart';
let _compCart = { clips: [] };
let _compIsAdmin = false;
let _compPollTimer = null;
let _compActiveBuildId = null;

function _compCartKey(d) {
  return `${d.host_key}|${d.cam}|${d.date}|${d.filename}`;
}

function compCartHas(key) {
  return _compCart.clips.some(c => _compCartKey(c) === key);
}

function compCartLoad() {
  try {
    const raw = localStorage.getItem(COMP_CART_KEY);
    if (raw) _compCart = JSON.parse(raw) || { clips: [] };
  } catch (e) { _compCart = { clips: [] }; }
  if (!Array.isArray(_compCart.clips)) _compCart.clips = [];
}

function compCartSave() {
  try {
    localStorage.setItem(COMP_CART_KEY, JSON.stringify(_compCart));
  } catch (e) { /* quota */ }
}

function _compClipFromDetection(d) {
  const stationLabel = d.station_label || d.host_key;
  const dt = d.meteor_time ? new Date(d.meteor_time + (d.meteor_time.endsWith('Z') ? '' : 'Z')) : null;
  const dateLabel = dt ? dt.toISOString().slice(5, 19).replace('T', ' ') : '';
  const mag = (d.rms && d.rms.mag_apparent != null) ? d.rms.mag_apparent : null;
  const shower = (d.rms && d.rms.shower) || null;
  const magStr = mag != null ? `${mag >= 0 ? '+' : ''}${mag.toFixed(1)}m` : '';
  const labelParts = [magStr, shower].filter(Boolean);
  const label = `${labelParts.join(' ')} · ${stationLabel} ${d.cam} · ${dateLabel}`.trim();
  return {
    host_key: d.host_key,
    cam: d.cam,
    date: d.date,
    filename: d.filename,
    detection_offset_s: d.detection_offset_s ?? null,
    pre: 2,
    post: 5,
    label,
    stack: d.stack || null,
  };
}

function compCartToggle(d) {
  const key = _compCartKey(d);
  const idx = _compCart.clips.findIndex(c => _compCartKey(c) === key);
  if (idx >= 0) _compCart.clips.splice(idx, 1);
  else _compCart.clips.push(_compClipFromDetection(d));
  compCartSave();
  compCartUpdateFab();
  compCartRender();
  // Cheap re-render of the SF tab so the badge state flips for that card
  if (typeof sfRender === 'function' && document.getElementById('pane-sortfilter')?.classList.contains('active')) {
    sfRender();
  }
}

function compCartRemove(idx) {
  _compCart.clips.splice(idx, 1);
  compCartSave();
  compCartUpdateFab();
  compCartRender();
  if (typeof sfRender === 'function' && document.getElementById('pane-sortfilter')?.classList.contains('active')) {
    sfRender();
  }
}

function compCartSetTrim(idx, field, val) {
  const clip = _compCart.clips[idx];
  if (!clip) return;
  const n = parseFloat(val);
  if (!isFinite(n) || n < 0) return;
  clip[field] = field === 'pre' ? Math.min(60, n) : Math.min(300, Math.max(0.5, n));
  compCartSave();
  compCartRenderFootSummary();
}

function compCartQuick(idx, pre, post) {
  const clip = _compCart.clips[idx];
  if (!clip) return;
  clip.pre = pre;
  clip.post = post;
  compCartSave();
  compCartRender();
}

function compCartClear() {
  if (!_compCart.clips.length) return;
  if (!confirm(`Clear all ${_compCart.clips.length} clip(s) from the cart?`)) return;
  _compCart.clips = [];
  compCartSave();
  compCartUpdateFab();
  compCartRender();
  if (typeof sfRender === 'function' && document.getElementById('pane-sortfilter')?.classList.contains('active')) {
    sfRender();
  }
}

function compCartUpdateFab() {
  const fab = document.getElementById('comp-fab');
  const cnt = document.getElementById('comp-fab-count');
  if (!fab || !cnt) return;
  cnt.textContent = String(_compCart.clips.length);
  fab.classList.toggle('empty', _compCart.clips.length === 0);
  fab.style.display = _compIsAdmin ? '' : 'none';
}

function compCartToggleDrawer() {
  const drawer = document.getElementById('comp-drawer');
  if (!drawer) return;
  const willOpen = !drawer.classList.contains('open');
  drawer.classList.toggle('open', willOpen);
  drawer.setAttribute('aria-hidden', willOpen ? 'false' : 'true');
  if (willOpen) compCartRender();
}

function compCartRenderFootSummary() {
  const sum = document.getElementById('comp-summary');
  if (!sum) return;
  const total = _compCart.clips.reduce((acc, c) => acc + (c.pre || 0) + (c.post || 0), 0);
  sum.textContent = `${_compCart.clips.length} clip${_compCart.clips.length !== 1 ? 's' : ''} · ~${total.toFixed(0)}s total`;
}

function compCartRender() {
  const body = document.getElementById('comp-drawer-body');
  if (!body) return;
  if (!_compCart.clips.length) {
    body.innerHTML = `<div class="comp-drawer-empty">
      Pick clips with the <strong>+</strong> button on each card in the Sort &amp; Filter view.<br><br>
      Each row gets its own pre / post seconds and the cart persists across reloads.
    </div>`;
    compCartRenderFootSummary();
    return;
  }
  const rows = _compCart.clips.map((c, i) => {
    const stackUrl = c.stack ? `/stack/${c.host_key}/${c.cam}/${c.date}/${encodeURIComponent(c.stack)}` : '';
    const thumb = stackUrl
      ? `<div class="comp-row-thumb" style="background-image:url('${stackUrl}')"></div>`
      : `<div class="comp-row-thumb"></div>`;
    return `<div class="comp-row" draggable="true" data-idx="${i}"
       ondragstart="compCartDragStart(event,${i})"
       ondragover="compCartDragOver(event)"
       ondrop="compCartDrop(event,${i})"
       ondragend="compCartDragEnd(event)">
      ${thumb}
      <div class="comp-row-body">
        <div class="comp-row-meta">${c.label || `${c.cam} · ${c.date}`}</div>
        <div class="comp-row-sub">${c.filename}</div>
        <div class="comp-row-trim">
          <span>pre</span>
          <input type="number" min="0" max="60" step="0.5" value="${c.pre}" onchange="compCartSetTrim(${i},'pre',this.value)">
          <span>post</span>
          <input type="number" min="0.5" max="300" step="0.5" value="${c.post}" onchange="compCartSetTrim(${i},'post',this.value)">
        </div>
        <div class="comp-quick">
          <button onclick="compCartQuick(${i},2,5)">Std</button>
          <button onclick="compCartQuick(${i},5,30)">+train</button>
          <button onclick="compCartQuick(${i},3,15)">+fireball</button>
        </div>
        <button class="comp-row-trim-toggle" onclick="compRowToggleTrim(${i},this)">&#9662; Trim visually</button>
      </div>
      <button class="comp-row-rm" onclick="compCartRemove(${i})" title="Remove">×</button>
    </div>`;
  }).join('');
  body.innerHTML = rows;
  compCartRenderFootSummary();
}

/* ─── Visual trim: per-row collapsible panel ─────────────────────────────────
   Loads the source video for the clicked clip and shows a live preview plus
   two range thumbs (start / end) anchored to the meteor's detection offset.
   Dragging either thumb seeks the player; values feed back into the cart's
   pre/post fields so the build pipeline sees them. */
function compRowToggleTrim(idx, btn) {
  const row = btn.closest('.comp-row');
  if (!row) return;
  const existing = row.querySelector('.comp-row-trim-visual');
  if (existing) {
    existing.remove();
    btn.innerHTML = '&#9662; Trim visually';
    return;
  }
  btn.innerHTML = '&#9652; Hide trim';
  const c = _compCart.clips[idx];
  const apiHost = c.host_key;
  const url = `/video/${encodeURIComponent(apiHost)}/${encodeURIComponent(c.cam)}/${encodeURIComponent(c.date)}/${encodeURIComponent(c.filename)}`;
  // Meteor center inside the chunk. Falls back to 10 s (typical 20 s chunk
  // midpoint) if the cart row was added before detection_offset_s was
  // captured, so the panel still works.
  const center = (c.detection_offset_s != null) ? c.detection_offset_s : 10.0;
  const initStart = Math.max(0, center - (c.pre ?? 2));
  const initEnd   = center + (c.post ?? 5);

  const panel = document.createElement('div');
  panel.className = 'comp-row-trim-visual';
  panel.innerHTML = `
    <video class="comp-trim-video" preload="metadata" muted playsinline></video>
    <div class="comp-trim-bar">
      <span>Start</span>
      <input type="range" class="comp-trim-start" min="0" max="20" step="0.1" value="${initStart.toFixed(1)}">
      <span class="comp-trim-val comp-trim-start-val">${initStart.toFixed(1)}s</span>
    </div>
    <div class="comp-trim-bar">
      <span>End</span>
      <input type="range" class="comp-trim-end" min="0" max="20" step="0.1" value="${initEnd.toFixed(1)}">
      <span class="comp-trim-val comp-trim-end-val">${initEnd.toFixed(1)}s</span>
    </div>
    <div class="comp-trim-meta">Meteor at <b>${center.toFixed(2)}s</b> in clip &middot; ffmpeg will keep <b>[start &rarr; end]</b> &middot; current pre/post: <b class="comp-trim-pp">${(c.pre ?? 0).toFixed(2)}s / ${(c.post ?? 0).toFixed(2)}s</b></div>
  `;
  row.appendChild(panel);

  const video    = panel.querySelector('video');
  const startEl  = panel.querySelector('.comp-trim-start');
  const endEl    = panel.querySelector('.comp-trim-end');
  const startVal = panel.querySelector('.comp-trim-start-val');
  const endVal   = panel.querySelector('.comp-trim-end-val');
  const ppMeta   = panel.querySelector('.comp-trim-pp');
  video.src = url;
  // controls visible only after metadata loads — avoids the "broken video"
  // first-frame flash on slow proxies.
  video.controls = false;
  video.addEventListener('loadedmetadata', () => {
    video.controls = true;
    const dur = Number.isFinite(video.duration) ? video.duration : 20;
    startEl.max = dur.toFixed(1);
    endEl.max   = dur.toFixed(1);
    if (parseFloat(endEl.value) > dur) {
      endEl.value = dur.toFixed(1);
      endVal.textContent = `${dur.toFixed(1)}s`;
    }
    video.currentTime = parseFloat(startEl.value);
  });

  const apply = () => {
    let s = parseFloat(startEl.value);
    let e = parseFloat(endEl.value);
    if (e <= s + 0.5) e = s + 0.5;
    if (s >= e - 0.5) s = e - 0.5;
    startEl.value = s.toFixed(1); endEl.value = e.toFixed(1);
    startVal.textContent = `${s.toFixed(1)}s`;
    endVal.textContent   = `${e.toFixed(1)}s`;
    const newPre  = Math.max(0,   +(center - s).toFixed(2));
    const newPost = Math.max(0.5, +(e - center).toFixed(2));
    _compCart.clips[idx].pre  = newPre;
    _compCart.clips[idx].post = newPost;
    compCartSave();
    // Sync the existing pre/post number inputs in the row header.
    const trimInputs = row.querySelectorAll('.comp-row-trim input[type="number"]');
    if (trimInputs[0]) trimInputs[0].value = newPre;
    if (trimInputs[1]) trimInputs[1].value = newPost;
    if (ppMeta) ppMeta.textContent = `${newPre.toFixed(2)}s / ${newPost.toFixed(2)}s`;
    compCartRenderFootSummary();
  };
  startEl.addEventListener('input', () => {
    apply();
    if (Number.isFinite(video.duration)) video.currentTime = parseFloat(startEl.value);
  });
  endEl.addEventListener('input', () => {
    apply();
    // Seek a hair before the end-cut so the user sees the last frame
    // they'll keep, not the one they're about to drop.
    if (Number.isFinite(video.duration)) {
      const target = Math.max(parseFloat(startEl.value), parseFloat(endEl.value) - 0.5);
      video.currentTime = target;
    }
  });
}

// ── Drag-to-reorder (HTML5 native) ────────────────────────────────────────────
let _compDragIdx = null;
function compCartDragStart(ev, idx) {
  _compDragIdx = idx;
  ev.dataTransfer.effectAllowed = 'move';
  ev.currentTarget.classList.add('dragging');
}
function compCartDragOver(ev) { ev.preventDefault(); ev.dataTransfer.dropEffect = 'move'; }
function compCartDrop(ev, targetIdx) {
  ev.preventDefault();
  if (_compDragIdx === null || _compDragIdx === targetIdx) return;
  const moved = _compCart.clips.splice(_compDragIdx, 1)[0];
  _compCart.clips.splice(targetIdx, 0, moved);
  _compDragIdx = null;
  compCartSave();
  compCartRender();
}
function compCartDragEnd(ev) {
  ev.currentTarget.classList.remove('dragging');
  _compDragIdx = null;
}

// ── Build pipeline ────────────────────────────────────────────────────────────
async function compCartBuild() {
  if (!_compCart.clips.length) return;
  const titleEl = document.getElementById('comp-title');
  const scheduleEl = document.getElementById('comp-schedule');
  const ytEnabledEl = document.getElementById('comp-yt-upload');
  const ytPrivacyEl = document.getElementById('comp-yt-privacy');
  const title = (titleEl?.value || '').trim() || `ROVIMEN Compilation · ${new Date().toISOString().slice(0,10)}`;
  const scheduledFor = scheduleEl?.value ? new Date(scheduleEl.value).toISOString() : null;
  const youtube = ytEnabledEl?.checked
    ? { upload_after_build: true, privacy: ytPrivacyEl?.value || 'unlisted' }
    : { upload_after_build: false };

  // Step 1 — create the manifest
  const buildBtn = document.getElementById('comp-build-btn');
  buildBtn.disabled = true;
  buildBtn.textContent = scheduledFor ? 'Saving…' : 'Submitting…';
  let manifestId = null;
  try {
    const resp = await fetch('/api/compilation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title,
        scheduled_for: scheduledFor,
        clips: _compCart.clips.map(c => ({
          host_key: c.host_key, cam: c.cam, date: c.date, filename: c.filename,
          detection_offset_s: c.detection_offset_s ?? null,
          pre: c.pre, post: c.post, label: c.label,
        })),
        youtube,
      }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const created = await resp.json();
    manifestId = created.id;
  } catch (e) {
    alert(`Failed to create compilation: ${e.message}`);
    buildBtn.disabled = false;
    buildBtn.textContent = 'Build now';
    return;
  }

  // If scheduled, we're done — manifest sits on disk for the cron tick.
  if (scheduledFor) {
    buildBtn.disabled = false;
    buildBtn.textContent = 'Build now';
    alert(`Scheduled: "${title}" will build at ${scheduleEl.value}`);
    return;
  }

  // Step 2 — kick off immediate build, then poll status
  try {
    const resp = await fetch(`/api/compilation/${manifestId}/build`, { method: 'POST' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  } catch (e) {
    alert(`Build kickoff failed: ${e.message}`);
    buildBtn.disabled = false;
    buildBtn.textContent = 'Build now';
    return;
  }

  _compActiveBuildId = manifestId;
  buildBtn.textContent = 'Building…';
  compCartShowProgress({ status: 'building', progress: { phase: 'queued', done: 0, total: _compCart.clips.length }});
  if (_compPollTimer) clearInterval(_compPollTimer);
  _compPollTimer = setInterval(() => compCartPoll(manifestId), 1500);
}

function compCartShowProgress(state) {
  const el = document.getElementById('comp-progress');
  if (!el) return;
  if (!state) { el.style.display = 'none'; return; }
  el.style.display = '';
  const p = state.progress || { phase: '?', done: 0, total: 0 };
  const pct = p.total ? Math.round(100 * p.done / p.total) : 0;
  let body;
  if (state.status === 'error') {
    body = `<div class="comp-progress-error"><strong>Build failed.</strong><br>${
      (state.error || 'Unknown error').replace(/</g, '&lt;')
    }</div>`;
  } else if (state.status === 'done') {
    const dl = `<a href="/api/compilation/${_compActiveBuildId}/download" target="_blank" style="color:var(--blue)">Download MP4</a>`;
    const yt = state.youtube_url
      ? ` · <a href="${state.youtube_url}" target="_blank" style="color:var(--blue)">YouTube</a>`
      : '';
    body = `<strong>Done.</strong> ${dl}${yt}`;
  } else if (state.status === 'uploading') {
    body = `<div>Uploading to YouTube — ${p.done}%</div>
            <div class="comp-progress-bar"><div style="width:${p.done}%"></div></div>`;
  } else {
    body = `<div>${p.phase} — ${p.done}/${p.total} (${pct}%)</div>
            <div class="comp-progress-bar"><div style="width:${pct}%"></div></div>`;
  }
  el.innerHTML = body;
}

let _compYtStatus = null;  // { ready, configured, libs_ok, reason } from /api/youtube/status

function compCartYtToggle() {
  const cb = document.getElementById('comp-yt-upload');
  const sel = document.getElementById('comp-yt-privacy');
  if (sel) sel.disabled = !cb?.checked;
}

async function compCartLoadYtStatus() {
  try {
    const r = await fetch('/api/youtube/status');
    if (!r.ok) return;
    _compYtStatus = await r.json();
  } catch (e) { return; }
  const cb = document.getElementById('comp-yt-upload');
  const lbl = document.getElementById('comp-yt-label');
  const warn = document.getElementById('comp-yt-warn');
  const sel = document.getElementById('comp-yt-privacy');
  if (!cb || !lbl || !warn) return;
  if (_compYtStatus.ready) {
    cb.disabled = false;
    lbl.style.opacity = '1';
    warn.style.display = 'none';
  } else {
    cb.disabled = true;
    cb.checked = false;
    if (sel) sel.disabled = true;
    lbl.style.opacity = '0.5';
    warn.style.display = '';
    warn.textContent = _compYtStatus.reason || 'YouTube not configured';
  }
}

async function compCartPoll(manifestId) {
  try {
    const resp = await fetch(`/api/compilation/${manifestId}/status`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const state = await resp.json();
    compCartShowProgress(state);
    if (state.status === 'done' || state.status === 'error') {
      clearInterval(_compPollTimer);
      _compPollTimer = null;
      const buildBtn = document.getElementById('comp-build-btn');
      if (buildBtn) { buildBtn.disabled = false; buildBtn.textContent = 'Build now'; }
    }
  } catch (e) { /* keep polling */ }
}

async function compCartShowList() {
  // escH is a local alias for the common escHtml, safe to use inside the
  // popup document which shares no JS context with the main window.
  function escH(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  // Reject non-https YouTube URLs to block javascript: and data: hrefs.
  function safeYtUrl(url) {
    if (!url) return null;
    try {
      const u = new URL(url);
      return u.protocol === 'https:' ? url : null;
    } catch { return null; }
  }
  try {
    const resp = await fetch('/api/compilation');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const items = await resp.json();
    const w = window.open('', '_blank', 'width=720,height=600');
    const rows = items.map(m => {
      const safeYt = safeYtUrl(m.youtube_url);
      const ytLink = safeYt
        ? `<a href="${escH(safeYt)}" target="_blank" rel="noopener noreferrer">YouTube</a>` : '';
      // Compilation IDs are dashboard-internal (UUID/integer) — still escaped for defence-in-depth.
      const safeId = escH(m.id);
      const dlLink = m.output_path
        ? `<a href="/api/compilation/${safeId}/download" target="_blank">Download MP4</a>` : '';
      const err = m.error
        ? `<div style="color:#e74c3c;font-size:11px;margin-top:4px">${escH(m.error)}</div>` : '';
      const sched = m.scheduled_for ? ` · scheduled ${escH(m.scheduled_for)}` : '';
      return `<div class="comp-list-item">
        <div class="comp-list-item-title">${escH(m.title || m.id)}</div>
        <div style="font-size:10px;color:#888">${safeId} · ${(m.clips || []).length} clips · ${escH(m.created_at?.slice(0,16) || '')}${sched}</div>
        <span class="comp-status-badge comp-status-${escH(m.status)}">${escH(m.status)}</span>
        <div class="comp-list-item-actions">
          ${dlLink}
          ${ytLink}
        </div>${err}
      </div>`;
    }).join('') || '<p style="text-align:center;color:#888">No compilations yet.</p>';
    w.document.write(`<html><head><title>Past compilations</title>
      <style>body{background:#0d1117;color:#c9d1d9;font-family:system-ui;padding:14px}
      a{color:#58a6ff} .comp-list-item{padding:10px;background:#161b22;border:1px solid #30363d;border-radius:6px;margin-bottom:8px}
      .comp-list-item-title{font-weight:600;margin-bottom:4px}
      .comp-status-badge{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;margin-top:4px}
      .comp-status-draft{background:#444}.comp-status-scheduled{background:#7c3aed;color:#fff}
      .comp-status-building,.comp-status-uploading{background:#f59e0b;color:#000}
      .comp-status-done{background:#10b981;color:#000}.comp-status-error{background:#e74c3c;color:#fff}
      .comp-list-item-actions{margin-top:6px}
      .comp-list-item-actions a{font-size:11px;padding:2px 6px;background:#161b22;border:1px solid #30363d;border-radius:3px;text-decoration:none;color:#c9d1d9;margin-right:6px}
      </style></head><body><h2>Past compilations</h2>${rows}</body></html>`);
    w.document.close();
  } catch (e) {
    alert(`Failed to load list: ${e.message}`);
  }
}

// ── Clip download (fetch-first, format=mp4) ───────────────────────────────────

async function evDownloadClip(url, filename, ev) {
  const btn = ev ? ev.currentTarget : null;
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  try {
    const resp = await fetch(url);
    if (!resp.ok) {
      const msg = resp.status === 404 ? 'Clip no longer available' : 'Download failed';
      _toast(msg, 'error');
      return;
    }
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename.replace(/\.mkv$/, '.mp4');
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  } catch(e) {
    _toast('Download failed', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⬇'; }
  }
}

// ── Auth + init ────────────────────────────────────────────────────────────────

async function _evInit() {
  try {
    const auth = await (await fetch('/api/auth/status')).json();
    // Record the session identity BEFORE any gated fetchJson fires so an
    // anonymous visitor on this public page isn't bounced to /login when the
    // (login-gated) detection feed 401s — fetchJson keys its login redirect
    // off state.AUTH_USER.
    state.AUTH_USER = auth.user || null;
    if (auth.admin) {
      const el = document.getElementById('logo-dd-admin');
      if (el) el.style.display = '';
      const socialEl = document.getElementById('logo-dd-social');
      if (socialEl) socialEl.style.display = '';
      _compIsAdmin = true;
    }
  } catch(e) {}
  compCartLoad();
  compCartUpdateFab();
  if (_compIsAdmin) compCartLoadYtStatus();
  // Kick the data loads only after auth state is known.
  populateNightSelector();
  evLoadPlatepars();
  if (_urlShower) switchTab('sortfilter');
}
_evInit();

// Expose to global scope for inline onclick handlers
window.switchTab = switchTab;
window.closeModal = closeModal;
window.evCompassClick = evCompassClick;
window.compCartBuild = compCartBuild;
window.compCartClear = compCartClear;
window.compCartShowList = compCartShowList;
window.compCartToggleDrawer = compCartToggleDrawer;
window.compCartQuick = compCartQuick;
window.compCartRemove = compCartRemove;
window.compCartSetTrim = compCartSetTrim;
window.compCartYtToggle = compCartYtToggle;
window.openEventModal = openEventModal;
window.openSingleVideo = openSingleVideo;
window.sfToggleShower = sfToggleShower;
window.compRowToggleTrim = compRowToggleTrim;
window.evSliderInput = evSliderInput;
window.evTimeInputChange = evTimeInputChange;
window.evUpdateZoom = evUpdateZoom;
window.sfReload = sfReload;
window.sfRender = sfRender;
window.compCartToggle = compCartToggle;
window.compCartDragStart = compCartDragStart;
window.compCartDragOver = compCartDragOver;
window.compCartDrop = compCartDrop;
window.compCartDragEnd = compCartDragEnd;
window.evDownloadClip = evDownloadClip;
