// Detection grid view (issue #313): rows = time buckets, columns = cameras.
// A row with many filled cells is a multi-station coincidence -- the whole
// point of the view is making those jump out without cross-referencing
// per-station pages.
//
// This module is loaded by events.html and drives the "Grid" tab. The heavy
// lifting (fan-out across stations) happens server-side in
// /api/detections/grid/<date>; this file only requests, filters and paints.
//
// Pure helpers (_gridParseHHMM, _gridAgeLabel, _gridFilterColumns) are exported
// so they can be unit-tested in isolation (tests/js/dashboard-grid.test.js).

import { escHtml } from './dashboard-common.js';

// ── Pure helpers (exported for unit tests) ──────────────────────────────────

/** Parse "HH:MM" (or "HHMM" / "H:MM") -> minutes-of-day, or null if invalid. */
export function _gridParseHHMM(str) {
  if (str == null) return null;
  const s = String(str).trim().replace(/\s/g, '');
  if (!s) return null;
  const m = /^(\d{1,2}):(\d{2})$/.exec(s) || /^(\d{1,2})(\d{2})$/.exec(s);
  if (!m) return null;
  const h = parseInt(m[1], 10);
  const mn = parseInt(m[2], 10);
  if (h < 0 || h > 24 || mn < 0 || mn > 59 || (h === 24 && mn !== 0)) return null;
  return h * 60 + mn;
}

/** Human "age" of an ISO timestamp relative to `now` (default: real now).
 *  Returns e.g. "2h ago", "1d ago", "just now". `now` is injectable for tests. */
export function _gridAgeLabel(iso, now = Date.now()) {
  if (!iso) return '';
  const t = new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).getTime();
  if (!Number.isFinite(t)) return '';
  let secs = Math.floor((now - t) / 1000);
  if (secs < 0) secs = 0;
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** Keep only columns that have at least one cell across the supplied rows.
 *  Empty cameras (no detections all night) would otherwise produce a wall of
 *  blank columns. Preserves the server-provided column order. */
export function _gridFilterColumns(columns, rows) {
  const seen = new Set();
  for (const row of rows || []) {
    for (const cam of Object.keys(row.cells || {})) seen.add(cam);
  }
  return (columns || []).filter(c => seen.has(c.cam));
}

// ── State ───────────────────────────────────────────────────────────────────

let _gridData = null;          // last fetched { date, bucket_min, columns, rows }
let _gridInited = false;
let _gridMode = 'footage';     // 'footage' (all continuous chunks) | 'detections' (locked only)
const FOOTAGE_MAX_MIN = 5;     // footage shows every clip, so cap the window (5 min ≈ 15 clips/cam)

// ── Rendering ───────────────────────────────────────────────────────────────

function _gridStackUrl(date, cell) {
  return cell.stack
    ? `/stack/${cell.host_key}/${cell.cam}/${date}/${cell.stack}`
    : null;
}

function _gridCellHtml(date, cam, cells) {
  if (!cells || !cells.length) {
    return `<td class="grid-cell grid-cell-empty"></td>`;
  }
  const multi = cells.some(c => c.multi);
  const inner = cells.map(c => {
    const stackUrl = _gridStackUrl(date, c);
    const offset = c.detection_offset_s || 0;
    const timeLabel = escHtml((c.meteor_time || '').slice(11, 19));
    const thumb = stackUrl
      ? `<img src="${escHtml(stackUrl)}" loading="lazy" alt="${escHtml(cam)} ${timeLabel}">`
      : `<div class="grid-thumb-noimg">no img</div>`;
    // The click is wired via a single delegated listener on #grid-main (see
    // gridInit). Values ride on HTML-escaped data-* attributes instead of an
    // inline onclick, so nothing is interpolated into executable code.
    return `<div class="grid-thumb${c.multi ? ' multi' : ''}" title="${escHtml(cam)} ${timeLabel} UTC"`
      + ` data-host="${escHtml(c.host_key)}" data-cam="${escHtml(c.cam)}" data-date="${escHtml(date)}"`
      + ` data-fn="${escHtml(c.filename || '')}" data-offset="${escHtml(offset)}" data-mtime="${escHtml(c.meteor_time || '')}">
      ${thumb}
      <span class="grid-thumb-time">${timeLabel}</span>
    </div>`;
  }).join('');
  return `<td class="grid-cell${multi ? ' grid-cell-multi' : ''}">
    <div class="grid-cell-inner">${inner}</div>
  </td>`;
}

/** Column header: cameras grouped under their station label, shared by both
 *  grid modes. Returns the full `<thead>` markup. */
function _gridHeadHtml(columns) {
  let head = '<thead><tr class="grid-station-row"><th class="grid-corner"></th>';
  let lastStation = null;
  let span = 0;
  const stationCells = [];
  for (const col of columns) {
    if (col.station_label !== lastStation) {
      if (lastStation !== null) stationCells.push({ label: lastStation, span });
      lastStation = col.station_label;
      span = 1;
    } else {
      span += 1;
    }
  }
  if (lastStation !== null) stationCells.push({ label: lastStation, span });
  for (const s of stationCells) {
    head += `<th class="grid-station-hdr" colspan="${s.span}">${escHtml(s.label || '')}</th>`;
  }
  head += '</tr><tr class="grid-cam-row"><th class="grid-corner">time (UTC)</th>';
  for (const col of columns) {
    head += `<th class="grid-cam-hdr" title="${escHtml(col.station_label || '')} — ${escHtml(col.cam)}">${escHtml(col.cam)}</th>`;
  }
  head += '</tr></thead>';
  return head;
}

/** Dispatch to the renderer for the active grid mode. */
function renderGrid(data) {
  if (_gridMode === 'footage') return renderFootageGrid(data);
  return renderDetectionGrid(data);
}

function renderDetectionGrid(data) {
  const el = document.getElementById('grid-main');
  if (!el) return;
  if (!data) {
    el.innerHTML = '<div class="ev-empty">No data loaded.</div>';
    return;
  }
  const date = data.date || '';
  const rows = data.rows || [];
  const columns = _gridFilterColumns(data.columns || [], rows);

  if (!rows.length || !columns.length) {
    el.innerHTML = `<div class="ev-empty">No detections in this window.<br>
      <span style="font-size:12px">Widen the time range or pick a busier night.</span></div>`;
    return;
  }

  const head = _gridHeadHtml(columns);

  // Body: one row per populated time bucket, newest first.
  let body = '<tbody>';
  for (const row of rows) {
    const newest = _gridRowNewest(row);
    const age = newest ? _gridAgeLabel(newest) : '';
    const rowCls = row.has_multi ? ' grid-row-multi' : '';
    body += `<tr class="grid-row${rowCls}">`;
    body += `<th class="grid-time-hdr" title="${row.total} detection${row.total === 1 ? '' : 's'} across ${row.station_count} station${row.station_count === 1 ? '' : 's'}">
      <span class="grid-time">${escHtml(row.label)}</span>
      <span class="grid-age">${escHtml(age)}</span>
    </th>`;
    for (const col of columns) {
      body += _gridCellHtml(date, col.cam, (row.cells || {})[col.cam]);
    }
    body += '</tr>';
  }
  body += '</tbody>';

  el.innerHTML = `<div class="grid-scroll"><table class="grid-table">${head}${body}</table></div>`;
}

// ── Footage mode (all continuous chunks) ────────────────────────────────────

/** One cell = EVERY clip that camera recorded in this (camera, time-bucket)
 *  slot, rendered as a row of thumbnails. The window is capped upstream so a
 *  cell only ever holds a handful of 20 s clips. */
export function _footageCellHtml(date, cam, cell) {
  if (!cell || !cell.chunks || !cell.chunks.length) {
    return `<td class="grid-cell grid-cell-empty"></td>`;
  }
  return `<td class="grid-cell grid-cell-footage${cell.has_detection ? ' grid-cell-detection' : ''}">`
    + `<div class="grid-cell-inner">${_footageStripHtml(date, cam, cell.chunks)}</div></td>`;
}

/** Build the clip thumbnails for a cell: one per 20 s chunk, time-ordered. */
export function _footageStripHtml(date, cam, chunks) {
  return (chunks || []).map(c => {
    const host = c.host_key || '';
    const t = escHtml((c.time || '').slice(0, 8));
    const stackUrl = c.stack ? `/stack/${host}/${cam}/${date}/${c.stack}` : null;
    const thumb = stackUrl
      ? `<img src="${escHtml(stackUrl)}" loading="lazy" alt="${escHtml(cam)} ${t}">`
      : `<div class="grid-thumb-noimg">no img</div>`;
    return `<div class="grid-thumb${c.locked ? ' detection' : ''}" title="${escHtml(cam)} ${t} UTC" `
      + `data-host="${escHtml(host)}" data-cam="${escHtml(cam)}" data-date="${escHtml(date)}" `
      + `data-fn="${escHtml(c.filename || '')}" data-offset="${escHtml(c.detection_offset_s || 0)}" data-mtime="${escHtml(c.meteor_time || '')}">`
      + `${thumb}<span class="grid-thumb-time">${t}</span></div>`;
  }).join('');
}

/** Transposed footage matrix: cameras down the Y axis, time across the X axis.
 *  The server returns time-bucket rows with per-camera cells; we pivot so each
 *  table row is a camera and each column is a time bucket (earliest left). */
function renderFootageGrid(data) {
  const el = document.getElementById('grid-main');
  if (!el) return;
  if (!data) {
    el.innerHTML = '<div class="ev-empty">No data loaded.</div>';
    return;
  }
  const date = data.date || '';
  const timeBuckets = data.rows || [];                              // X axis (time, ascending)
  const cameras = _gridFilterColumns(data.columns || [], timeBuckets); // Y axis (cameras w/ footage)

  const banner = data.degraded
    ? `<div class="grid-banner">Continuous footage for this night has rotated off the stations
       (kept ~2 days). Showing locked detections only.</div>`
    : '';

  if (!timeBuckets.length || !cameras.length) {
    el.innerHTML = banner + `<div class="ev-empty">No footage in this window.<br>
      <span style="font-size:12px">Pick a recent night and a start time (≤ ${data.max_window_min || 5} min shown).</span></div>`;
    return;
  }

  // Header: corner + one column per time bucket (X axis), sticky at top.
  let head = '<thead><tr><th class="grid-corner">camera \\ time (UTC)</th>';
  for (const tb of timeBuckets) {
    const n = tb.total || 0;
    head += `<th class="grid-cam-hdr" title="${escHtml(tb.label)} UTC — ${n} clip${n === 1 ? '' : 's'} across ${tb.station_count} station${tb.station_count === 1 ? '' : 's'}">${escHtml(tb.label)}</th>`;
  }
  head += '</tr></thead>';

  // Body: one row per camera (Y axis); cells walk the time buckets (X axis).
  let body = '<tbody>';
  for (const camCol of cameras) {
    const cam = camCol.cam;
    const camHasDet = timeBuckets.some(tb => ((tb.cells || {})[cam] || {}).has_detection);
    body += `<tr class="grid-row${camHasDet ? ' grid-row-multi' : ''}">`;
    body += `<th class="grid-time-hdr" title="${escHtml(camCol.station_label || '')} — ${escHtml(cam)}">
      <span class="grid-time">${escHtml(cam)}</span>
      <span class="grid-age">${escHtml(camCol.station_label || '')}</span>
    </th>`;
    for (const tb of timeBuckets) {
      body += _footageCellHtml(date, cam, (tb.cells || {})[cam]);
    }
    body += '</tr>';
  }
  body += '</tbody>';

  el.innerHTML = banner + `<div class="grid-scroll"><table class="grid-table">${head}${body}</table></div>`;
}

/** Update the toolbar hint to match the active grid mode. */
function _gridUpdateHint() {
  const hint = document.querySelector('#pane-grid .grid-hint');
  if (!hint) return;
  hint.textContent = _gridMode === 'footage'
    ? `All footage: cameras down, time across — every clip in the window (capped to ${FOOTAGE_MAX_MIN} min ≈ ${FOOTAGE_MAX_MIN * 3} clips/camera). Set a start time to inspect an event, including cameras that never triggered a detection. Continuous video is kept ~2 days.`
    : 'Detections: each row is a time bucket, each column a camera. Rows with many filled cells are multi-station coincidences — bright meteors, sprites, aurora.';
}

/** Newest meteor_time in a row (for the age label). */
function _gridRowNewest(row) {
  let newest = '';
  for (const cells of Object.values(row.cells || {})) {
    for (const c of cells) {
      if (c.meteor_time && c.meteor_time > newest) newest = c.meteor_time;
    }
  }
  return newest;
}

// ── Data loading ────────────────────────────────────────────────────────────

async function gridLoad() {
  const el = document.getElementById('grid-main');
  if (!el) return;
  const datePicker = document.getElementById('ev-date');
  const date = datePicker ? datePicker.value : null;
  if (!date) {
    el.innerHTML = '<div class="ev-empty">Pick a night.</div>';
    return;
  }
  const bucket = document.getElementById('grid-bucket')?.value || '1';
  const fromEl = document.getElementById('grid-from');
  const toEl = document.getElementById('grid-to');
  let fromMin = _gridParseHHMM(fromEl?.value);
  let toMin = _gridParseHHMM(toEl?.value);

  // Footage shows every clip, so enforce a window of at most FOOTAGE_MAX_MIN.
  // Default the start to 22:00 if blank, derive/clamp the end, and reflect the
  // clamped values back into the inputs so the user sees what's shown.
  if (_gridMode === 'footage') {
    if (fromMin == null) fromMin = 22 * 60;
    const span = toMin == null ? null : ((toMin - fromMin) + 1440) % 1440;
    if (span == null || span === 0 || span > FOOTAGE_MAX_MIN) {
      toMin = (fromMin + FOOTAGE_MAX_MIN) % 1440;
    }
    if (fromEl) fromEl.value = _gridMinToHHMM(fromMin);
    if (toEl) toEl.value = _gridMinToHHMM(toMin);
  }

  const params = new URLSearchParams({ bucket });
  if (fromMin != null) params.set('from', _gridMinToHHMM(fromMin));
  if (toMin != null) params.set('to', _gridMinToHHMM(toMin));

  _gridUpdateHint();
  const base = _gridMode === 'footage' ? '/api/footage/grid/' : '/api/detections/grid/';
  el.innerHTML = '<div class="ev-loading"><div class="ev-spinner"></div> Loading grid…</div>';
  try {
    const r = await fetch(`${base}${date}?${params.toString()}`);
    if (!r.ok) throw new Error(r.statusText);
    _gridData = await r.json();
    renderGrid(_gridData);
  } catch (e) {
    el.innerHTML = `<div class="ev-empty">Failed to load grid: ${escHtml(e.message)}</div>`;
  }
}

function _gridMinToHHMM(min) {
  return `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`;
}

// ── Init (wired by events.html via the Grid tab) ────────────────────────────

// Called from switchTab('grid') in dashboard-events.js the first time the tab
// is opened, then on every subsequent open (cheap; warm server cache).
export function gridInit() {
  if (!_gridInited) {
    _gridInited = true;
    const fromEl = document.getElementById('grid-from');
    const toEl = document.getElementById('grid-to');
    const bucketEl = document.getElementById('grid-bucket');
    const modeEl = document.getElementById('grid-mode');
    // Each mode has a sensible default column width: footage rows are short
    // (≤5 min) so 1-min columns spread the clips across time; detections span a
    // whole night so 5-min columns keep it compact.
    const applyModeBucket = () => { if (bucketEl) bucketEl.value = _gridMode === 'footage' ? '1' : '5'; };
    if (fromEl) fromEl.addEventListener('change', gridLoad);
    if (toEl) toEl.addEventListener('change', gridLoad);
    if (bucketEl) bucketEl.addEventListener('change', gridLoad);
    if (modeEl) {
      _gridMode = modeEl.value || _gridMode;
      applyModeBucket();
      modeEl.addEventListener('change', () => { _gridMode = modeEl.value; applyModeBucket(); gridLoad(); });
    }
    // Delegated click for grid cells: attached once on the stable #grid-main
    // container so it survives innerHTML re-renders without leaking listeners.
    // Reading raw values from dataset avoids interpolating station data into
    // an inline handler (XSS-safe).
    const gridMain = document.getElementById('grid-main');
    if (gridMain) {
      gridMain.addEventListener('click', (e) => {
        const cell = e.target.closest('.grid-thumb');
        if (!cell || !gridMain.contains(cell)) return;
        const d = cell.dataset;
        if (!d.fn) return;  // a cell with no underlying clip is not playable
        if (typeof window.openSingleVideo === 'function') {
          window.openSingleVideo(d.host, d.cam, d.date, d.fn, Number(d.offset) || 0, d.mtime);
        }
      });
    }
  }
  gridLoad();
}

// Reload when the shared Night selector changes while the Grid tab is active.
export function gridOnNightChange() {
  if (_gridInited && document.getElementById('pane-grid')?.classList.contains('active')) {
    gridLoad();
  }
}

// Expose for inline handlers / the events.js tab switcher.
window.gridInit = gridInit;
window.gridOnNightChange = gridOnNightChange;
