import { fmtBytes, fmtDate, parseFn, fetchJson, _modalOpen, _modalClose, escHtml, state, showerFullName, LOCK_ICON, UNLOCK_ICON } from './dashboard-common.js';
/*
 * dashboard-rms.js
 * ────────────────
 * RMS / Detection tab — sky-dome thumb, calendar night-picker, shower/sort filter, locked-clip grid, processing plots, plot zoom modal helpers. Loaded by dashboard.html before dashboard-station.js.
 *
 * Depends on dashboard-common.js (escHtml, fmtBytes, fmtDate, parseFn, state.STATIONS_META,
 * state.VDB_CAMERAS, state.statusData, state.vitalsData, state.tlData, state.activeStation, state.activeTab, state.IS_ADMIN,
 * state.AUTH_USER, state.USER_ROLE, state.USER_STATIONS, _modalOpen, _modalClose) and lexical bindings
 * declared at the top level of dashboard-station.js (classic non-module scripts share
 * the global lexical environment, so top-level `let`/`const` are visible across bundles).
 */

/* ─────────────────────────────────────────
   RMS tab render
───────────────────────────────────────── */
window.rmsState = { station: null, selectedCamera: null };

// One-shot scroll target consumed by fetchRMSAllNights once the night
// sections are rendered. Used by the ?date= URL param so a deep-link from
// the overview's GMN popup (or anywhere else) lands the user on the
// requested night instead of the top of the list.
window._rmsPendingScrollDate = null;

// /api/sky_dome returns 503 when no platepar/stack pair is available (e.g.
// brand-new station, all cams offline). Hide the card in that case rather
// than leaving a broken-image icon at the top of the RMS pane.
function skyDomeOnError(host) {
  const card = document.getElementById(`sky-dome-card-${host}`);
  if (card) card.style.display = 'none';
}

function skyDomeOpenModal(host) {
  const img = document.getElementById(`sky-dome-img-${host}`);
  if (!img || !img.src) return;
  const modal = document.getElementById('cam-modal');
  if (modal && typeof rmsPlotExpand === 'function') {
    rmsPlotExpand(modal, img.src, `${host} — sky coverage`);
    return;
  }
  window.open(img.src, '_blank', 'noopener');
}

function renderRMS(host) {
  const el      = document.getElementById('pane-rms');
  const cameras = state.VDB_CAMERAS[host] || [];
  if (!cameras.length) {
    el.innerHTML = '<div class="no-data">No cameras configured for this station</div>';
    return;
  }

  if (window.rmsState.station !== host) {
    window.rmsState = { station: host, selectedCamera: cameras[0] };
  }
  if (!window.rmsState.selectedCamera || !cameras.includes(window.rmsState.selectedCamera)) {
    window.rmsState.selectedCamera = cameras[0];
  }
  const cam = window.rmsState.selectedCamera;

  const camBtns = cameras.map(c =>
    `<button class="cam-btn${c === cam ? ' active' : ''}" onclick="rmsSelectCamera('${host}','${c}')">${c}</button>`
  ).join('');

  el.dataset.host = host;
  // The live sky-dome lives on the Video DB tab only — keeps the
  // RMS / Detection pane focused on per-night locked clips + processing
  // plots without duplicating the dome image.
  el.innerHTML = `
    <div class="card sky-dome-card" id="sky-dome-card-${host}" style="padding:10px 14px;margin-bottom:12px">
      <div class="card-title" style="margin-bottom:8px">Sky coverage</div>
      <div class="sky-dome-wrap">
        <img class="sky-dome-img" id="sky-dome-img-${host}"
             src="/api/sky_dome/${encodeURIComponent(host)}.png"
             alt="Sky coverage for ${host}"
             loading="lazy" decoding="async"
             onerror="skyDomeOnError('${host}')"
             onclick="skyDomeOpenModal('${host}')">
      </div>
    </div>
    <div class="card" style="padding:10px 14px;margin-bottom:12px">
      <div class="card-title" style="margin-bottom:10px">Locked Clips &amp; RMS Plots</div>
      <div class="cam-btns">${camBtns}</div>
    </div>
    <div class="rms-section" id="rms-sec-${host}-${cam}">
      <div class="rms-section-header" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <span class="rms-meteor-count" id="rms-count-${host}-${cam}"></span>
        <div class="rms-cal-wrap" id="rms-cal-wrap-${host}-${cam}">
          <button class="rms-cal-btn" id="rms-cal-btn-${host}-${cam}"
                  onclick="rmsCalToggle('${host}','${cam}', event)" type="button">
            <span class="rms-cal-icon">&#128197;</span>
            <span id="rms-cal-label-${host}-${cam}">All nights</span>
            <span class="rms-cal-chev">&#9662;</span>
          </button>
          <div class="rms-cal-pop" id="rms-cal-pop-${host}-${cam}" hidden></div>
        </div>
        <div class="vdb-zoom-row" style="margin-left:auto">
          <span title="Smaller">&#9723;</span>
          <input type="range" class="rms-zoom" id="rms-zoom-${host}-${cam}"
                 min="120" max="480" value="200"
                 oninput="rmsUpdateZoom('${host}','${cam}',this.value)" title="Thumbnail size">
          <span title="Larger">&#9724;</span>
        </div>
      </div>
      <div class="rms-filter-row" id="rms-filter-${host}-${cam}">
        <label class="rms-flt-label">Shower
          <span class="rms-flt-btnrow" id="rms-shower-btns-${host}-${cam}">
            <button class="rms-flt-btn active" data-shower="all"
                    onclick="rmsSelectShower('${host}','${cam}','all')">All</button>
          </span>
        </label>
        <label class="rms-flt-label">Sort
          <select class="rms-flt-select" id="rms-sort-${host}-${cam}"
                  onchange="rmsApplyFilter('${host}','${cam}')">
            <option value="date-desc">Newest first</option>
            <option value="date-asc">Oldest first</option>
            <option value="mag-asc">Brightest first</option>
            <option value="mag-desc">Faintest first</option>
          </select>
        </label>
        <button class="rms-flt-clear" onclick="rmsClearFilter('${host}','${cam}')" title="Clear all filters">Reset</button>
        <span class="rms-flt-summary" id="rms-flt-summary-${host}-${cam}"></span>
      </div>
      <div class="rms-chunk-area" id="rms-chunks-${host}-${cam}">
        <div class="offline">Loading…</div>
      </div>
    </div>`;

  fetchRMSAllNights(host, cam);
}

function rmsSelectCamera(host, cam) {
  window.rmsState.selectedCamera = cam;
  renderRMS(host);
}

function rmsJumpToNight(host, cam, date) {
  if (!date) return;
  // Each night section has id=`rms-night-sec-<host>-<cam>-<date>` (set by
  // renderRMSAllNights). Scroll it into view.
  const sec = document.getElementById(`rms-night-sec-${host}-${cam}-${date}`);
  if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

let _rmsAllChunks = [];
let _rmsDetections = {};  // chunk prefix (STATION_DATE_TIME) → RMS detection metadata
let _rmsResults = [];     // [{date, chunks}] from last fetch — cached for re-filter without refetch
let _rmsVisibleChunks = []; // current filtered+sorted set (modal nav cycles through this)

async function fetchRMSAllNights(host, cam) {
  const el = document.getElementById(`rms-chunks-${host}-${cam}`);
  if (!el) return;
  el.innerHTML = '<div class="offline">Loading…</div>';
  // Clear stale detection metadata from a previous camera so it doesn't
  // persist if this fetch fails or returns different data.
  _rmsDetections = {};
  const apiHost = window.hostForCamera(host, cam);
  try {
    const resp = await fetch(`/api/videodb/rmsnights/${apiHost}/${cam}`);
    if (!resp.ok) throw new Error(resp.status === 401 ? 'Session expired — please refresh' : resp.status === 403 ? 'You do not have access to this station' : `Server error (${resp.status})`);
    const nights = await resp.json();
    if (!nights.length) {
      el.innerHTML = '<div class="no-data">No recordings found</div>';
      return;
    }
    const [results, detByNight] = await Promise.all([
      Promise.all(nights.map(date =>
        fetchJson(`/api/videodb/chunks/${apiHost}/${cam}/${date}?locked_only=1`)
          .then(raw => ({ date, chunks: Array.isArray(raw) ? raw : (raw.chunks || []) }))
          .catch(() => ({ date, chunks: [] }))
      )),
      // Fetch RMS detection metadata (mag, shower, radiant) for all nights
      Promise.all(nights.map(date =>
        fetch(`/api/videodb/rms-detections/${apiHost}/${cam}/${date}`)
          .then(r => r.ok ? r.json() : null)
          .catch(() => null)
      )).then(arr => {
        const m = {};
        arr.forEach((data, i) => { if (data) m[nights[i]] = data; });
        return m;
      }),
    ]);
    // Build detection lookup: time_utc (second-precision) → detection
    _rmsDetections = {};
    for (const [date, data] of Object.entries(detByNight)) {
      if (!data?.detections) continue;
      for (const det of data.detections) {
        if (det.time_utc) _rmsDetections[det.time_utc] = det;
      }
    }
    // Build flat nav list with per-chunk date
    _rmsAllChunks = [];
    results.forEach(({ date, chunks }) =>
      chunks.forEach(c => _rmsAllChunks.push({ ...c, _date: date }))
    );
    _rmsVisibleChunks = _rmsAllChunks;
    const total = _rmsAllChunks.length;
    const countEl = document.getElementById(`rms-count-${host}-${cam}`);
    if (countEl) countEl.textContent = `${nights.length} night${nights.length !== 1 ? 's' : ''} \u00b7 ${total} clip${total !== 1 ? 's' : ''}`;
    // Set min/max on the date picker (calendar-style) to the range of
    // available nights — native browser calendar will grey out everything
    // outside and only allow dates that actually exist in this station's
    // archive.
    const nightSel = document.getElementById(`rms-night-${host}-${cam}`);
    if (nightSel && nights.length) {
      const toIso = d => `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`;
      const sorted = [...nights].sort();
      nightSel.min = toIso(sorted[0]);
      nightSel.max = toIso(sorted[sorted.length-1]);
    }
    _rmsResults = results;
    _rmsCalSelected = new Set();   // reset night picker on camera switch
    _rmsCalViewMonth = null;
    _rmsCalRefreshAvailable();
    _rmsCalUpdateLabel(host, cam);
    _rmsPopulateShowerOptions(host, cam);
    renderRMSAllNights(host, cam, results);
    results.forEach(({ date }) =>
      fetchRMSPlots(host, cam, date, `rms-plots-${host}-${cam}-${date}`)
    );
    // Consume a pending ?date= deep-link if one is set for this camera.
    // Scrolling happens on the next frame so the just-rendered DOM has
    // committed layout (otherwise scrollIntoView() snaps to the wrong y).
    if (window._rmsPendingScrollDate) {
      const targetDate = window._rmsPendingScrollDate;
      window._rmsPendingScrollDate = null;
      requestAnimationFrame(() => rmsJumpToNight(host, cam, targetDate));
    }
  } catch(e) {
    el.innerHTML = `<div class="offline">Error: ${escHtml(e?.message || e)}</div>`;
  }
}

/** Look up RMS detection metadata for a chunk using its meteor_time.
 *  Detections are keyed by time_utc (second-precision); chunk.meteor_time
 *  has sub-second precision. Match to the same second. */
function _lookupDetection(filename, meteorTime) {
  if (!meteorTime) return null;
  // meteor_time: "2026-04-14T01:05:41.947816" → truncate to second: "2026-04-14T01:05:41"
  const key = meteorTime.slice(0, 19);
  return _rmsDetections[key] || null;
}

/** Format detection metadata as compact HTML for a card.
 *  e.g. "+1.3m SPO 0.4s 10.3°/s" */
function _detMetaHtml(det) {
  if (!det) return '';
  const parts = [];
  if (det.mag_apparent != null) {
    const mag = det.mag_apparent;
    const cls = mag < 0 ? 'det-mag-fireball' : mag < 1 ? 'det-mag-bright' : '';
    parts.push(`<span class="det-mag ${cls}">${mag >= 0 ? '+' : ''}${mag.toFixed(1)}m</span>`);
  }
  if (det.shower) {
    const cls = det.shower !== 'SPO' ? 'det-shower' : 'det-spo';
    parts.push(`<span class="${cls}" title="${showerFullName(det.shower)}">${det.shower}</span>`);
  }
  if (det.duration_s != null) {
    parts.push(`<span class="det-dur">${det.duration_s.toFixed(1)}s</span>`);
  }
  if (det.angular_velocity != null) {
    parts.push(`<span class="det-angvel">${det.angular_velocity.toFixed(1)}\u00b0/s</span>`);
  }
  if (!parts.length) return '';
  return `<div class="det-meta">${parts.join(' ')}</div>`;
}

// ── RMS card filter (per-camera shower / sort buttons) ────────────────────

// Returns { all: true, set: null } when "All" is selected (no filter),
// or { all: false, set: Set<shower-code> } when one or more specific showers are picked.
function _rmsActiveShower(host, cam) {
  const row = document.getElementById(`rms-shower-btns-${host}-${cam}`);
  if (!row) return { all: true, set: null };
  const active = [...row.querySelectorAll('.rms-flt-btn.active')]
    .map(b => b.dataset.shower);
  if (!active.length || active.includes('all')) return { all: true, set: null };
  return { all: false, set: new Set(active) };
}

function _rmsActiveSort(host, cam) {
  const sel = document.getElementById(`rms-sort-${host}-${cam}`);
  return sel?.value || 'date-desc';
}

function _rmsReadFilter(host, cam) {
  return {
    shower: _rmsActiveShower(host, cam),
    sort: _rmsActiveSort(host, cam),
    nights: _rmsCalSelected,
  };
}

// ── Calendar night picker (multi-select with click/drag) ──────────────────────

let _rmsCalSelected = new Set();   // YYYYMMDD strings; empty = all nights
let _rmsCalViewMonth = null;       // 'YYYY-MM'
let _rmsCalDragMode = null;        // 'add' | 'remove' | null
let _rmsCalDragOriginal = null;    // Set snapshot at drag start (for cancel)
let _rmsCalAvailable = new Set();  // YYYYMMDD strings that have data

function _rmsCalAvailableMonths() {
  const months = new Set();
  for (const d of _rmsCalAvailable) months.add(`${d.slice(0,4)}-${d.slice(4,6)}`);
  return [...months].sort();
}

function _rmsCalUpdateLabel(host, cam) {
  const lbl = document.getElementById(`rms-cal-label-${host}-${cam}`);
  if (!lbl) return;
  const n = _rmsCalSelected.size;
  lbl.textContent = n === 0 ? 'All nights' : (n === 1 ? '1 night' : `${n} nights`);
  const btn = document.getElementById(`rms-cal-btn-${host}-${cam}`);
  if (btn) btn.classList.toggle('active', n > 0);
}

function _rmsCalRefreshAvailable() {
  _rmsCalAvailable = new Set();
  for (const night of _rmsResults || []) {
    if (night.chunks && night.chunks.length) _rmsCalAvailable.add(night.date);
  }
}

function _rmsCalBuildMonth(host, cam) {
  const [y, m] = _rmsCalViewMonth.split('-').map(Number);
  // Cells: leading blanks (Mon=1) + 28-31 days + trailing blanks
  const firstDow = (new Date(Date.UTC(y, m-1, 1)).getUTCDay() + 6) % 7; // Mon=0
  const lastDay = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const cells = [];
  for (let i = 0; i < firstDow; i++) cells.push(null);
  for (let d = 1; d <= lastDay; d++) {
    const ymd = `${y}${String(m).padStart(2,'0')}${String(d).padStart(2,'0')}`;
    cells.push(ymd);
  }
  while (cells.length % 7 !== 0) cells.push(null);
  const months = _rmsCalAvailableMonths();
  const idx = months.indexOf(_rmsCalViewMonth);
  const prevMonth = idx > 0 ? months[idx-1] : null;
  const nextMonth = (idx >= 0 && idx < months.length-1) ? months[idx+1] : null;
  const monthLabel = new Date(Date.UTC(y, m-1, 1))
    .toLocaleString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });
  const dows = ['Mo','Tu','We','Th','Fr','Sa','Su'];
  const grid = cells.map(ymd => {
    if (!ymd) return '<span class="rms-cal-cell rms-cal-empty"></span>';
    const day = parseInt(ymd.slice(6,8));
    const has = _rmsCalAvailable.has(ymd);
    const sel = _rmsCalSelected.has(ymd);
    const cls = ['rms-cal-cell'];
    if (!has) cls.push('rms-cal-disabled');
    if (sel) cls.push('rms-cal-selected');
    return `<span class="${cls.join(' ')}" data-ymd="${ymd}"
              ${has ? `onmousedown="rmsCalDayDown('${host}','${cam}','${ymd}',event)"
                       onmouseenter="rmsCalDayEnter('${host}','${cam}','${ymd}')"` : ''}
            >${day}</span>`;
  }).join('');
  const navLeft  = prevMonth ? `onclick="rmsCalNav('${host}','${cam}','${prevMonth}')"` : 'disabled';
  const navRight = nextMonth ? `onclick="rmsCalNav('${host}','${cam}','${nextMonth}')"` : 'disabled';
  const selN = _rmsCalSelected.size;
  return `
    <div class="rms-cal-head">
      <button type="button" class="rms-cal-nav" ${navLeft}>&#9664;</button>
      <span class="rms-cal-month">${monthLabel}</span>
      <button type="button" class="rms-cal-nav" ${navRight}>&#9654;</button>
    </div>
    <div class="rms-cal-dows">${dows.map(d => `<span>${d}</span>`).join('')}</div>
    <div class="rms-cal-grid">${grid}</div>
    <div class="rms-cal-foot">
      <span class="rms-cal-count">${selN === 0 ? 'No nights selected (= all)' : `${selN} selected`}</span>
      <button type="button" class="rms-cal-action" onclick="rmsCalClearSelection('${host}','${cam}')">Clear</button>
      <button type="button" class="rms-cal-action rms-cal-go" onclick="rmsCalGo('${host}','${cam}')">Go</button>
    </div>`;
}

function _rmsCalRender(host, cam) {
  const pop = document.getElementById(`rms-cal-pop-${host}-${cam}`);
  if (!pop) return;
  pop.innerHTML = _rmsCalBuildMonth(host, cam);
}

let _rmsCalEscHandler = null;  // stored reference for cleanup

function _rmsCalClose(pop) {
  if (!pop || pop.hidden) return;
  pop.hidden = true;
  if (_rmsCalEscHandler) {
    document.removeEventListener('keydown', _rmsCalEscHandler);
    _rmsCalEscHandler = null;
  }
}

function rmsCalToggle(host, cam, ev) {
  if (ev) ev.stopPropagation();
  const pop = document.getElementById(`rms-cal-pop-${host}-${cam}`);
  if (!pop) return;
  if (!pop.hidden) { _rmsCalClose(pop); return; }
  _rmsCalRefreshAvailable();
  if (!_rmsCalAvailable.size) {
    pop.innerHTML = '<div class="rms-cal-empty-msg">No nights with data yet</div>';
    pop.hidden = false;
    return;
  }
  // Default month: most recent night, or month containing first selected night
  if (!_rmsCalViewMonth || !_rmsCalAvailableMonths().includes(_rmsCalViewMonth)) {
    const months = _rmsCalAvailableMonths();
    const seedNight = _rmsCalSelected.size ? [..._rmsCalSelected].sort().pop() : [..._rmsCalAvailable].sort().pop();
    _rmsCalViewMonth = `${seedNight.slice(0,4)}-${seedNight.slice(4,6)}`;
    if (!months.includes(_rmsCalViewMonth)) _rmsCalViewMonth = months[months.length-1];
  }
  _rmsCalRender(host, cam);
  pop.hidden = false;
  // Escape key closes the popup
  if (_rmsCalEscHandler) document.removeEventListener('keydown', _rmsCalEscHandler);
  _rmsCalEscHandler = (e) => {
    if (e.key === 'Escape') { _rmsCalClose(pop); }
  };
  document.addEventListener('keydown', _rmsCalEscHandler);
}

function rmsCalNav(host, cam, monthYM) {
  _rmsCalViewMonth = monthYM;
  _rmsCalRender(host, cam);
}

let _rmsCalMouseUpHandler = null;  // stored reference for cleanup

function rmsCalDayDown(host, cam, ymd, ev) {
  ev.preventDefault();
  // Clean up any prior mouseup listener that leaked (e.g. popup closed mid-drag)
  if (_rmsCalMouseUpHandler) {
    document.removeEventListener('mouseup', _rmsCalMouseUpHandler);
    _rmsCalMouseUpHandler = null;
  }
  _rmsCalDragOriginal = new Set(_rmsCalSelected);
  _rmsCalDragMode = _rmsCalSelected.has(ymd) ? 'remove' : 'add';
  if (_rmsCalDragMode === 'add') _rmsCalSelected.add(ymd);
  else _rmsCalSelected.delete(ymd);
  _rmsCalRender(host, cam);
  // Global mouseup ends the drag
  _rmsCalMouseUpHandler = () => {
    _rmsCalDragMode = null;
    _rmsCalDragOriginal = null;
    if (_rmsCalMouseUpHandler) {
      document.removeEventListener('mouseup', _rmsCalMouseUpHandler);
      _rmsCalMouseUpHandler = null;
    }
  };
  document.addEventListener('mouseup', _rmsCalMouseUpHandler);
}

function rmsCalDayEnter(host, cam, ymd) {
  if (!_rmsCalDragMode) return;
  if (_rmsCalDragMode === 'add') _rmsCalSelected.add(ymd);
  else _rmsCalSelected.delete(ymd);
  _rmsCalRender(host, cam);
}

function rmsCalClearSelection(host, cam) {
  _rmsCalSelected.clear();
  _rmsCalRender(host, cam);
}

function rmsCalGo(host, cam) {
  const pop = document.getElementById(`rms-cal-pop-${host}-${cam}`);
  _rmsCalClose(pop);
  _rmsCalUpdateLabel(host, cam);
  rmsApplyFilter(host, cam);
}

// Close popup when clicking outside
document.addEventListener('click', (e) => {
  document.querySelectorAll('.rms-cal-pop').forEach(pop => {
    if (pop.hidden) return;
    if (!pop.parentElement.contains(e.target)) _rmsCalClose(pop);
  });
});

function _rmsPopulateShowerOptions(host, cam) {
  const row = document.getElementById(`rms-shower-btns-${host}-${cam}`);
  if (!row) return;
  const showers = new Set();
  for (const det of Object.values(_rmsDetections)) {
    if (det && det.shower) showers.add(det.shower);
  }
  // Preserve any currently-active picks that are still observed; if "All" was
  // active or nothing survives, fall back to "All".
  const prevActive = new Set(
    [...row.querySelectorAll('.rms-flt-btn.active')].map(b => b.dataset.shower)
  );
  const allWasActive = prevActive.size === 0 || prevActive.has('all');
  const survivors = [...prevActive].filter(s => s !== 'all' && showers.has(s));
  const useAll = allWasActive || survivors.length === 0;
  const activeSet = useAll ? new Set(['all']) : new Set(survivors);
  const ordered = ['all', ...[...showers].sort()];
  row.innerHTML = ordered.map(s =>
    `<button class="rms-flt-btn${activeSet.has(s) ? ' active' : ''}" data-shower="${escHtml(s)}"
             onclick="rmsSelectShower('${escHtml(host)}','${escHtml(cam)}','${escHtml(s)}')">${s === 'all' ? 'All' : escHtml(s)}</button>`
  ).join('');
}

function rmsSelectShower(host, cam, shower) {
  const row = document.getElementById(`rms-shower-btns-${host}-${cam}`);
  if (!row) return;
  const allBtn = row.querySelector('[data-shower="all"]');
  if (shower === 'all') {
    // Clear all specific picks and activate "All" exclusively
    row.querySelectorAll('.rms-flt-btn').forEach(b => b.classList.remove('active'));
    if (allBtn) allBtn.classList.add('active');
  } else {
    const btn = row.querySelector(`[data-shower="${shower}"]`);
    if (!btn) return;
    btn.classList.toggle('active');
    if (allBtn) allBtn.classList.remove('active');
    // If the user toggled the last specific pick off, fall back to "All"
    if (!row.querySelector('.rms-flt-btn.active') && allBtn) {
      allBtn.classList.add('active');
    }
  }
  rmsApplyFilter(host, cam);
}

function rmsClearFilter(host, cam) {
  rmsSelectShower(host, cam, 'all');
  const sortEl = document.getElementById(`rms-sort-${host}-${cam}`);
  if (sortEl) sortEl.value = 'date-desc';
  _rmsCalSelected = new Set();
  _rmsCalUpdateLabel(host, cam);
  rmsApplyFilter(host, cam);
}

function rmsApplyFilter(host, cam) {
  if (!_rmsResults.length) return;
  renderRMSAllNights(host, cam, _rmsResults);
}

function _rmsRenderCardHtml(host, cam, c, date, idx, opts) {
  const isManual = c.lock_type === 'manual';
  const cardClass = isManual ? 'vdb-chunk-locked-manual' : 'vdb-chunk-locked-detection';
  const lockBtnClass = isManual ? 'vdb-lock-btn--manual' : 'vdb-lock-btn--detection';
  const lockTitle = isManual ? 'Manual lock' : 'Detection lock';
  const lockIcon = isManual ? UNLOCK_ICON : LOCK_ICON;
  const isArchive = c.source === 'archive';
  const archiveImgSrc = isArchive && c.stack
    ? `/api/archive/file/${cam}/${date}/${c.stack_subdir || 'meteors'}/${encodeURIComponent(c.stack)}`
    : '';
  const thumbUrl = c.stack
    ? (archiveImgSrc || `/stack/${host}/${cam}/${date}/${encodeURIComponent(c.stack)}`)
    : null;
  const imgHtml = thumbUrl
    ? `<img src="${thumbUrl}" loading="lazy" decoding="async" alt="${c.time}" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
    : '';
  const stackBtn = thumbUrl
    ? `<a class="vdb-dl-btn" href="#" onclick="event.preventDefault();event.stopPropagation();rmsOpenModal(${idx},true)">&#8863; Stack</a>`
    : '';
  const det = _lookupDetection(c.filename, c.meteor_time);
  const detHtml = _detMetaHtml(det);
  const dateLine = opts && opts.showDate
    ? `<div class="vdb-chunk-size" style="color:var(--muted)">${fmtDate(date)}</div>`
    : '';
  return `<div class="vdb-chunk-card ${cardClass}" onclick="rmsOpenModal(${idx})">
    <button class="vdb-lock-btn ${lockBtnClass}" title="${lockTitle}" disabled>${lockIcon}</button>
    ${imgHtml}
    <div class="vdb-chunk-nostack" style="${thumbUrl ? 'display:none' : ''}">&#9654;</div>
    <div class="vdb-chunk-meta">
      <div class="vdb-chunk-time">${c.time} UTC</div>
      ${detHtml}
      ${dateLine}
      <div class="vdb-chunk-size">${c.size_mb} MB</div>
      ${stackBtn}
    </div>
  </div>`;
}

function renderRMSAllNights(host, cam, results) {
  const el = document.getElementById(`rms-chunks-${host}-${cam}`);
  if (!el) return;
  const zoom = document.getElementById(`rms-zoom-${host}-${cam}`)?.value || 200;
  const flt = _rmsReadFilter(host, cam);
  const summaryEl = document.getElementById(`rms-flt-summary-${host}-${cam}`);

  // Flatten with date annotation; apply filter; collect totals.
  let flat = [];
  let total = 0;
  for (const night of results) {
    if (flt.nights.size && !flt.nights.has(night.date)) {
      // Still count toward total so the summary reflects the filter ratio
      total += night.chunks.length;
      continue;
    }
    for (const c of night.chunks) {
      total++;
      const det = _lookupDetection(c.filename, c.meteor_time);
      if (!flt.shower.all && (!det || !flt.shower.set.has(det.shower))) continue;
      flat.push({ chunk: c, date: night.date, det });
    }
  }

  // Sort
  if (flt.sort === 'mag-asc' || flt.sort === 'mag-desc') {
    flat.sort((a, b) => {
      const am = a.det?.mag_apparent;
      const bm = b.det?.mag_apparent;
      if (am == null && bm == null) return 0;
      if (am == null) return 1;   // unknown mag → end
      if (bm == null) return -1;
      return flt.sort === 'mag-asc' ? am - bm : bm - am;
    });
  } else if (flt.sort === 'date-asc') {
    flat.sort((a, b) => (a.date + a.chunk.time).localeCompare(b.date + b.chunk.time));
  } else {
    // date-desc — already in fetch order (newest first per night, nights newest first)
    flat.sort((a, b) => (b.date + b.chunk.time).localeCompare(a.date + a.chunk.time));
  }

  // Build _rmsVisibleChunks for modal nav (annotate with _date for rmsOpenModal).
  _rmsVisibleChunks = flat.map(({ chunk, date }) => ({ ...chunk, _date: date }));

  // Filter summary
  if (summaryEl) {
    if (!flt.shower.all || flt.nights.size || flat.length !== total) {
      summaryEl.textContent = `${flat.length} of ${total} clip${total !== 1 ? 's' : ''}`;
    } else {
      summaryEl.textContent = '';
    }
  }

  if (!flat.length) {
    el.innerHTML = '<div class="no-data" style="padding:8px 0">No clips match the current filter</div>';
    return;
  }

  let html = '';
  if (flt.sort.startsWith('date-')) {
    // Group by date (matching original layout)
    const byDate = new Map();
    flat.forEach((row, idx) => {
      if (!byDate.has(row.date)) byDate.set(row.date, []);
      byDate.get(row.date).push({ row, idx });
    });
    const datesInOrder = [...byDate.keys()];
    datesInOrder.forEach((date, nightIdx) => {
      const entries = byDate.get(date);
      const sep = nightIdx > 0 ? '<hr style="border:none;border-top:1px solid var(--border);margin:16px 0">' : '';
      const dateLabel = `<div style="font-size:13px;font-weight:600;color:var(--text);margin-bottom:8px">${fmtDate(date)}</div>`;
      const cards = entries.map(({ row, idx }) =>
        _rmsRenderCardHtml(host, cam, row.chunk, date, idx, { showDate: false })
      ).join('');
      const grid = `<div class="vdb-chunk-grid" style="grid-template-columns:repeat(auto-fill,minmax(${zoom}px,1fr))">${cards}</div>`;
      html += `<div id="rms-night-sec-${host}-${cam}-${date}" style="scroll-margin-top:80px">${sep}${dateLabel}${grid}<div id="rms-plots-${host}-${cam}-${date}"></div></div>`;
    });
  } else {
    // Flat sorted view — one grid, date shown on each card
    const cards = flat.map((row, idx) =>
      _rmsRenderCardHtml(host, cam, row.chunk, row.date, idx, { showDate: true })
    ).join('');
    html = `<div class="vdb-chunk-grid" style="grid-template-columns:repeat(auto-fill,minmax(${zoom}px,1fr))">${cards}</div>`;
    // Hidden anchors so plot fetches still have a place to land (one per night)
    for (const date of new Set(flat.map(r => r.date))) {
      html += `<div id="rms-plots-${host}-${cam}-${date}" style="display:none"></div>`;
    }
  }
  el.innerHTML = html;
}

function rmsOpenModal(idx, startInStack = false) {
  // Cycle through whatever the user is currently looking at: the filtered+
  // sorted list (built by renderRMSAllNights), falling back to the full
  // fetch order if filters haven't been applied yet.
  const list = _rmsVisibleChunks.length ? _rmsVisibleChunks : _rmsAllChunks;
  const c = list[idx];
  if (!c) return;
  const host = window.rmsState.station;
  const cam  = window.rmsState.selectedCamera;
  window._vdbVisibleChunks = list;
  window._modalOpenedFromRms = true;
  const detOff = c.source === 'archive' ? null : (c.lock_type === 'detection' ? (c.detection_offset_s ?? null) : null);
  const videoSrc = c.source === 'archive'
    ? `/api/archive/file/${cam}/${c._date}/meteors/${encodeURIComponent(c.filename)}`
    : '';
  window.vdbOpenModal(host, cam, c._date, c.filename, c.time, c.size_mb, detOff, idx, videoSrc, startInStack);
}

function rmsUpdateZoom(host, cam, val) {
  const area = document.getElementById(`rms-chunks-${host}-${cam}`);
  if (!area) return;
  area.querySelectorAll('.vdb-chunk-grid').forEach(g => {
    g.style.gridTemplateColumns = `repeat(auto-fill,minmax(${val}px,1fr))`;
  });
  // Also scale plot gallery thumbnails to match
  area.querySelectorAll('.rms-plots-grid').forEach(g => {
    g.style.gridTemplateColumns = `repeat(auto-fill,minmax(${val}px,1fr))`;
  });
}

/* ─────────────────────────────────────────
   RMS Plots
───────────────────────────────────────── */
async function fetchRMSPlots(host, cam, date, containerId) {
  const area = document.getElementById(containerId || `rms-plots-${host}-${cam}`);
  if (!area) return;
  try {
    const rmsApiHost = window.hostForCamera(host, cam);
    const r = await fetch(`/api/rms/plots/${rmsApiHost}/${cam}/${date}`);
    if (!r.ok) { area.innerHTML = ''; return; }
    const plots = await r.json();
    renderRMSPlots(host, cam, date, plots, area);
  } catch(e) {
    area.innerHTML = '';
  }
}

/* The `rotate` flag on a camera describes its sensor orientation. Only
   raw sensor frames inherit it — FF stacks, max/avepixel. Matplotlib
   charts (radiants, fieldsums, calibration plots, FF intervals, observing
   periods) carry axis labels and must stay upright. The colour meteor
   stack is pre-rotated upstream by the station stacker, so it never
   qualifies either. */
function _rmsPlotNeedsRotation(host, cam, filename) {
  const meta = state.STATIONS_META && state.STATIONS_META[host];
  if (!meta) return false;
  const c = (meta.cameras || []).find(x => x.code === cam);
  if (!c || !c.rotate) return false;
  if (filename === '__color_meteor_stack__.webp') return false;
  return /_(stack|maxpixel|avepixel)\./i.test(filename);
}

/* Parse /api/rms/plot_image/<host>/<cam>/<date>/<filename> back into its
   components so the modal can decide whether to rotate. Returns null for
   anything that isn't a plot_image URL (e.g. inline SVG placeholders). */
export function _parseRmsPlotImageUrl(imgUrl) {
  try {
    const u = new URL(imgUrl, location.href);
    const parts = u.pathname.split('/');
    const i = parts.indexOf('plot_image');
    if (i < 0 || parts.length < i + 5) return null;
    return {
      host:     parts[i + 1],
      cam:      parts[i + 2],
      date:     parts[i + 3],
      filename: decodeURIComponent(parts[i + 4]),
    };
  } catch (e) { return null; }
}

function renderRMSPlots(host, cam, date, plots, container) {
  if (!plots.length) { container.innerHTML = ''; return; }
  const rmsApiHost = window.hostForCamera(host, cam);
  // FF-based plots come from the station in native sensor orientation.
  // Apply the .thumb-rotated CSS class when the camera is flagged for
  // rotation — except the colour meteor stack, which the station stacker
  // already orients correctly per its own rotate flag.
  const cards = plots.map(p => {
    const imgUrl = `/api/rms/plot_image/${rmsApiHost}/${cam}/${date}/${encodeURIComponent(p.filename)}`;
    const rotCls = _rmsPlotNeedsRotation(rmsApiHost, cam, p.filename) ? ' class="thumb-rotated"' : '';
    const labelE = escHtml(p.label);
    return `<div class="rms-plot-card" onclick="rmsPlotExpand(this,'${escHtml(imgUrl)}','${labelE}')">
      <img${rotCls} src="${escHtml(imgUrl)}" loading="lazy" decoding="async" alt="${labelE}"
           onerror="this.closest('.rms-plot-card').style.display='none'">
      <div class="rms-plot-label">${labelE}</div>
    </div>`;
  }).join('');
  container.innerHTML = `<details class="rms-plots-section">
    <summary class="rms-plots-title">Processing plots <span style="font-size:11px;color:var(--muted);font-weight:400">${plots.length} plot${plots.length!==1?'s':''}</span></summary>
    <div class="rms-plots-grid">${cards}</div>
  </details>`;
}

/* ── Plot zoom state ── */
window._pz = { scale: 1, tx: 0, ty: 0, dragging: false, sx: 0, sy: 0, stx: 0, sty: 0 };

function _pzLimits(img) {
  return {
    maxTx: Math.max(0, img.offsetWidth  * (window._pz.scale - 1) / 2),
    maxTy: Math.max(0, img.offsetHeight * (window._pz.scale - 1) / 2),
  };
}
function _pzRubber(val, limit) {
  if (Math.abs(val) <= limit) return val;
  return Math.sign(val) * (limit + (Math.abs(val) - limit) * 0.25);
}
function _pzClamp(tx, ty, img) {
  const { maxTx, maxTy } = _pzLimits(img);
  return {
    tx: Math.max(-maxTx, Math.min(maxTx, tx)),
    ty: Math.max(-maxTy, Math.min(maxTy, ty)),
  };
}
function _pzApply(img, transition) {
  // When the modal is showing a rotated RMS plot, the .thumb-rotated class
  // adds rotate(180deg). Setting img.style.transform inline overrides the
  // class — append the rotation as the rightmost (= applied first) op so
  // the rotation happens before scale/translate. That keeps user pan/zoom
  // operating in screen coordinates rather than the rotated image frame.
  const rot = img.classList.contains('thumb-rotated') ? ' rotate(180deg)' : '';
  img.style.transition = transition || '';
  img.style.transform = `translate(${window._pz.tx}px,${window._pz.ty}px) scale(${window._pz.scale})${rot}`;
  img.style.transformOrigin = 'center';
  img.style.cursor = window._pz.scale > 1 ? (window._pz.dragging ? 'grabbing' : 'grab') : 'zoom-in';
}
function _pzBounce(img) {
  const { tx, ty } = _pzClamp(window._pz.tx, window._pz.ty, img);
  if (tx !== window._pz.tx || ty !== window._pz.ty) {
    window._pz.tx = tx; window._pz.ty = ty;
    _pzApply(img, 'transform 0.4s cubic-bezier(0.34,1.56,0.64,1)');
  }
}
function _pzWheel(e) {
  e.preventDefault();
  const img = document.getElementById('cam-modal-img');
  window._pz.scale = Math.max(1, Math.min(10, window._pz.scale * (e.deltaY < 0 ? 1.2 : 0.83)));
  if (window._pz.scale === 1) { window._pz.tx = 0; window._pz.ty = 0; }
  else { const c = _pzClamp(window._pz.tx, window._pz.ty, img); window._pz.tx = c.tx; window._pz.ty = c.ty; }
  _pzApply(img);
}
function _pzDown(e) {
  if (window._pz.scale <= 1) return;
  e.preventDefault();
  window._pz.dragging = true; window._pz.sx = e.clientX; window._pz.sy = e.clientY;
  window._pz.stx = window._pz.tx; window._pz.sty = window._pz.ty;
  const img = document.getElementById('cam-modal-img');
  img.style.transition = '';
  img.style.cursor = 'grabbing';
  document.addEventListener('mousemove', _pzMove);
  document.addEventListener('mouseup',   _pzUp);
}
function _pzMove(e) {
  if (!window._pz.dragging) return;
  const img = document.getElementById('cam-modal-img');
  const rawTx = window._pz.stx + e.clientX - window._pz.sx;
  const rawTy = window._pz.sty + e.clientY - window._pz.sy;
  const { maxTx, maxTy } = _pzLimits(img);
  window._pz.tx = _pzRubber(rawTx, maxTx);
  window._pz.ty = _pzRubber(rawTy, maxTy);
  _pzApply(img);
}
function _pzUp() {
  if (!window._pz.dragging) return;
  window._pz.dragging = false;
  window._pz.justDragged = true;
  setTimeout(() => { window._pz.justDragged = false; }, 50);
  document.removeEventListener('mousemove', _pzMove);
  document.removeEventListener('mouseup',   _pzUp);
  const img = document.getElementById('cam-modal-img');
  if (img) _pzBounce(img);
}

function rmsPlotExpand(card, imgUrl, label) {
  const modal = document.getElementById('cam-modal');
  const img   = document.getElementById('cam-modal-img');
  const title = document.getElementById('cam-modal-title');
  if (!modal || !img) return;
  title.textContent = label;
  img.src = imgUrl;
  const dlBtn = document.getElementById('cam-modal-dl');
  if (dlBtn) { dlBtn.href = imgUrl; dlBtn.style.display = ''; }
  // Mirror the per-card rotation policy onto the shared modal img. The
  // .thumb-rotated class is read by _pzApply when computing the inline
  // transform so zoom/pan compose correctly with the 180° flip.
  const info = _parseRmsPlotImageUrl(imgUrl);
  if (info && _rmsPlotNeedsRotation(info.host, info.cam, info.filename)) {
    img.classList.add('thumb-rotated');
  } else {
    img.classList.remove('thumb-rotated');
  }
  // Reset and enable zoom
  window._pz = { scale: 1, tx: 0, ty: 0, dragging: false, sx: 0, sy: 0, stx: 0, sty: 0 };
  img.style.transform = img.classList.contains('thumb-rotated') ? 'rotate(180deg)' : '';
  img.style.cursor = 'zoom-in';
  modal.dataset.plotZoom = '1';
  modal.classList.add('plot-mode');
  img.removeEventListener('wheel',     _pzWheel);
  img.removeEventListener('mousedown', _pzDown);
  img.addEventListener('wheel',     _pzWheel, { passive: false });
  img.addEventListener('mousedown', _pzDown);
  _modalOpen(modal);
}

// Expose to global scope for inline onclick handlers
window.rmsCalClearSelection = rmsCalClearSelection;
window.rmsCalGo = rmsCalGo;
window.rmsCalNav = rmsCalNav;
window.rmsCalToggle = rmsCalToggle;
window.rmsClearFilter = rmsClearFilter;
window.rmsOpenModal = rmsOpenModal;
window.rmsPlotExpand = rmsPlotExpand;
window.rmsSelectCamera = rmsSelectCamera;
window.rmsSelectShower = rmsSelectShower;
window.skyDomeOpenModal = skyDomeOpenModal;
window.renderRMS = renderRMS;
window._lookupDetection = _lookupDetection;
window._pzDown = _pzDown;
window._pzMove = _pzMove;
window._pzUp = _pzUp;
window._pzWheel = _pzWheel;
window.fetchRMSAllNights = fetchRMSAllNights;
window.rmsCalDayDown = rmsCalDayDown;
window.rmsCalDayEnter = rmsCalDayEnter;
window.rmsApplyFilter = rmsApplyFilter;
window.rmsUpdateZoom = rmsUpdateZoom;
window.skyDomeOnError = skyDomeOnError;
