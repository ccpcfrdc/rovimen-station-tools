import { fmtBytes, fmtDate, parseFn, fetchJson, _modalOpen, _modalClose, _closeAllOpenModals, escHtml, state, _toast, LOCK_ICON, UNLOCK_ICON, canToggleLock } from './dashboard-common.js';
import { VideoModal } from './dashboard-video-modal.js';
import { MultiDetModal } from './dashboard-multi-det-modal.js';
/*
 * dashboard-vdb.js
 * ────────────────
 * Video Database tab — chunks, all-cams filmstrip, twilight density bar, video modal + trim slider, stitch with adjacent chunks, lock toggle, synced multi-cam modal. Loaded by dashboard.html before dashboard-station.js.
 *
 * Depends on dashboard-common.js (escHtml, fmtBytes, fmtDate, parseFn, state.STATIONS_META,
 * state.VDB_CAMERAS, state.statusData, state.vitalsData, state.tlData, state.activeStation, state.activeTab, state.IS_ADMIN,
 * state.AUTH_USER, state.USER_ROLE, state.USER_STATIONS, _modalOpen, _modalClose) and lexical bindings
 * declared at the top level of dashboard-station.js (classic non-module scripts share
 * the global lexical environment, so top-level `let`/`const` are visible across bundles).
 */

/* ─────────────────────────────────────────
   Video Database
───────────────────────────────────────── */
let vdbAllChunks       = [];
window._vdbVisibleChunks  = [];   // ordered list shown in grid (for ←/→ nav)
let _vdbCurrentIdx     = -1;   // index of open video in window._vdbVisibleChunks
window._stackCurrentIdx   = -1;   // index of open stack in window._vdbVisibleChunks
window._stackModalCtx     = null; // {station, camera, date} for stack nav
window._vdbAzimuths    = {};   // camCode → az_centre degrees (window-scoped for cross-module access)
let vdbSliderTimer = null;
let vdbPollTimer   = null;
let vdbArchivedFiles = new Set();  // filenames in permanent archive
let vdbCachedFiles   = new Set();  // filenames cached on VPS (24h)
window.vdbSortNewest  = true;         // true = newest first (window-scoped for cross-module access)

window.vdbLoadedStation = null;
let _modalOpenedFromRms = false;  // true when the clip modal was opened from the RMS tab

// Shared VideoModal instance for the VDB clip viewer.
// Initialised lazily on first use so the container is guaranteed to exist in the DOM.
let _vdbModal = null;
function _getVdbModal() {
  if (!_vdbModal) {
    _vdbModal = new VideoModal(
      document.getElementById('vdb-modal-container'),
      { trim: true, stitch: true, nav: true }
    );
    if (!window._VideoModalCloseAll) {
      window._VideoModalCloseAll = () => VideoModal.closeAll();
    }
  }
  return _vdbModal;
}

// MultiDetModal instance for the All Cams synced viewer.
let _vdbSyncModal = null;
function _getVdbSyncModal() {
  if (!_vdbSyncModal) {
    _vdbSyncModal = new MultiDetModal(
      document.getElementById('vdb-sync-modal-container')
    );
  }
  return _vdbSyncModal;
}

/** Simple toast notification (no external dependency). */

// All-cams view mode (sessionStorage-scoped per host: 'single' | 'all_cams').
// In 'all_cams' the camera selector is hidden and we render a per-cam stack
// of chunk grids for the selected date; clicks open a synced multi-cam modal.
let vdbViewMode = 'single';
// Per-(host, cam, date) cache of chunk lists fetched for the all-cams view.
// Keyed `<host>|<cam>|<date>` → array of chunk records as returned by
// /api/videodb/chunks. Used both to render the per-cam grids and to find
// the ±300 s window neighbours when opening the synced modal.
let _vdbChunksByCam = {};

function vdbLoadViewMode(host) {
  try { return sessionStorage.getItem(`vdb_view_mode_${host}`) || 'single'; }
  catch (e) { return 'single'; }
}

function vdbSaveViewMode(host, mode) {
  try { sessionStorage.setItem(`vdb_view_mode_${host}`, mode); }
  catch (e) { /* ignore */ }
}

function vdbLoadAllCamsSort(host) {
  try { return sessionStorage.getItem(`vdb_allcams_sort_${host}`) !== 'asc'; }
  catch (e) { return true; }
}

/** Resolve the backend host key for a camera (handles merged stations). */
function hostForCamera(displayHost, camCode) {
  const meta = state.STATIONS_META[displayHost];
  if (!meta) return displayHost;
  const cam = meta.cameras.find(c => c.code === camCode);
  return (cam && cam._host) || displayHost;
}

/** Read #vdb-night <select> and return compact YYYYMMDD. */
function vdbGetNight() {
  return document.getElementById('vdb-night').value || '';
}

/** Set #vdb-night <select> value from compact YYYYMMDD. */
function vdbSetNight(yyyymmdd) {
  document.getElementById('vdb-night').value = yyyymmdd || '';
}

function vdbIsTonight() {
  const now  = new Date();
  const utcH = now.getUTCHours();
  const base = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(),
                                  now.getUTCDate() + (utcH >= 12 ? 0 : -1)));
  const tonight = `${base.getUTCFullYear()}${String(base.getUTCMonth()+1).padStart(2,'0')}${String(base.getUTCDate()).padStart(2,'0')}`;
  return vdbGetNight() === tonight;
}

function vdbStopPoll() {
  if (vdbPollTimer) { clearInterval(vdbPollTimer); vdbPollTimer = null; }
  const badge = document.getElementById('vdb-live-badge');
  if (badge) badge.style.display = 'none';
}

function vdbStartPoll() {
  vdbStopPoll();
  const isLive = vdbIsTonight();
  const badge = document.getElementById('vdb-live-badge');
  if (badge) badge.style.display = isLive ? 'inline' : 'none';
  // 60 s for live (was 30 s — chunks land every few minutes at most, faster
  // polling just wastes the ~300 KB chunks payload), 120 s for historical
  // (was 60 s — backed by ETag/304 anyway, but no need to wake up the tab).
  vdbPollTimer = setInterval(vdbPoll, isLive ? 60000 : 120000);
}

async function vdbPoll() {
  if (document.hidden) return;
  const station = state.activeStation;
  const camera  = vdbGetSelectedCamera();
  const date    = vdbGetNight();
  if (!station || !camera || !date) return;
  try {
    const lockedOnly = document.getElementById('vdb-locked-only').checked ? '1' : '0';
    const pollHost = hostForCamera(station, camera);
    const [chunksResp, cacheResp] = await Promise.all([
      fetch(`/api/videodb/chunks/${pollHost}/${camera}/${date}?locked_only=${lockedOnly}`),
      fetch(`/api/cached-files/${camera}/${date}`).catch(() => ({ok:false})),
    ]);
    if (!chunksResp.ok) {
      if (chunksResp.status === 401) {
        _toast('Session expired, please reload', 'error');
        vdbStopPoll();
      } else if (chunksResp.status === 403) {
        _toast('You do not have access to this station', 'error');
        vdbStopPoll();
      }
      return;
    }
    const fresh = await chunksResp.json();
    let changed = fresh.length !== vdbAllChunks.length;
    if (cacheResp.ok) {
      const cached = new Set(await cacheResp.json());
      if (cached.size !== vdbCachedFiles.size) { vdbCachedFiles = cached; changed = true; }
    }
    if (changed) {
      vdbAllChunks = fresh;
      vdbRedrawCanvas();
      vdbRenderResults();
    }
  } catch(e) { /* ignore poll errors silently */ }
}

export function _vdbAzCompass(deg) {
  const dirs = ['N','NE','E','SE','S','SW','W','NW'];
  return dirs[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
}

async function vdbLoadAzimuths(host) {
  try {
    const r = await window.fetchOnce(`tab:platepar:${host}`, `/api/platepar/${host}`);
    if (!r.ok) return;
    const pp = await r.json();
    window._vdbAzimuths = {};
    for (const [cam, d] of Object.entries(pp)) {
      if (d && d.az_centre != null) window._vdbAzimuths[cam] = d.az_centre;
    }
    vdbUpdateAzLabels();
    if (host === state.activeStation) window.renderTimelapses(host);
  } catch(e) {
    if (window._isAbort?.(e)) return;
  }
}

function vdbUpdateAzLabels() {
  const cam = vdbGetSelectedCamera();
  const lbl = document.getElementById('vdb-cam-pointing');
  if (!lbl) return;
  const az = window._vdbAzimuths[cam];
  lbl.textContent = az != null ? `${_vdbAzCompass(az)} · ${Math.round(az)}°` : '';
}

function vdbInit() {
  window.vdbLoadedStation = state.activeStation;
  const host = state.activeStation;
  const vdbHeader = document.querySelector('.vdb-sticky-header');

  // Ensure a banner placeholder exists just before the VDB sticky header
  let banner = document.getElementById('vdb-access-banner');
  if (!banner && vdbHeader) {
    banner = document.createElement('div');
    banner.id = 'vdb-access-banner';
    vdbHeader.parentNode.insertBefore(banner, vdbHeader);
  }

  if (window.canAccessTab('videodb', host)) {
    if (banner) banner.innerHTML = '';
    if (vdbHeader) vdbHeader.style.display = '';
    vdbViewMode = vdbLoadViewMode(host);
    window.vdbAllCamsSortDesc = vdbLoadAllCamsSort(host);
    // Mirror the per-host sort onto the single-cam state so flipping the
    // direction in either view stays consistent within a host (P1-34).
    window.vdbSortNewest = window.vdbAllCamsSortDesc;
    const _sortBtn0 = document.getElementById('vdb-sort-btn');
    if (_sortBtn0) _sortBtn0.innerHTML = window.vdbSortNewest ? 'Newest &#9660;' : 'Oldest &#9650;';
    vdbEnsureViewToggle(host);
    vdbApplyViewModeUI();
    vdbPopulateCameras();
    vdbOnCameraChange();
  } else {
    if (banner) banner.innerHTML = window._accessBanner('Video Database');
    if (vdbHeader) vdbHeader.style.display = 'none';
  }
}

/** Inject the Single / All cams segmented toggle into the sticky header
 *  (idempotent — re-running just rewires onclick / active classes). */
function vdbEnsureViewToggle(host) {
  const filterRight = document.querySelector('#pane-videodb .vdb-filter-right');
  if (!filterRight) return;
  let toggle = document.getElementById('vdb-view-toggle');
  if (!toggle) {
    toggle = document.createElement('div');
    toggle.id = 'vdb-view-toggle';
    toggle.className = 'vdb-view-toggle';
    // Prepend so it sits visually before night-select + locked-only on the
    // right-hand cluster (matches the order: view-mode → date → filters).
    filterRight.insertBefore(toggle, filterRight.firstChild);
  }
  const isMobile = window.innerWidth < 768;
  toggle.innerHTML = `
    <button class="vdb-toggle-btn ${vdbViewMode === 'single' ? 'active' : ''}"
            onclick="vdbSwitchView('${host}','single')">Single cam</button>
    <button class="vdb-toggle-btn ${vdbViewMode === 'all_cams' ? 'active' : ''}"
            onclick="vdbSwitchView('${host}','all_cams')"
            style="${isMobile ? 'display:none' : ''}">All cams</button>`;

  // All-cams chunk-order toggle (only meaningful + visible in all-cams mode).
  // Lives as a sibling to the view toggle so they cluster visually.
  let sort = document.getElementById('vdb-allcams-sort');
  if (!sort) {
    sort = document.createElement('div');
    sort.id = 'vdb-allcams-sort';
    sort.className = 'vdb-view-toggle';
    sort.style.marginLeft = '6px';
    toggle.insertAdjacentElement('afterend', sort);
  }
  sort.innerHTML = `
    <button class="vdb-toggle-btn ${window.vdbAllCamsSortDesc ? 'active' : ''}"
            title="Newest clips first" onclick="vdbSetAllCamsSort('${host}',true)">Newest first</button>
    <button class="vdb-toggle-btn ${!window.vdbAllCamsSortDesc ? 'active' : ''}"
            title="Oldest clips first" onclick="vdbSetAllCamsSort('${host}',false)">Oldest first</button>`;
  sort.style.display = vdbViewMode === 'all_cams' ? '' : 'none';
}

/** Persist + apply the All-cams chunk sort direction. Re-renders each
 *  per-camera filmstrip from its cached chunk list (no re-fetch). */
function vdbSetAllCamsSort(host, desc) {
  if (window.vdbAllCamsSortDesc === desc) return;
  window.vdbAllCamsSortDesc = desc;
  // Mirror onto the single-cam sort state + the legacy "Newest first" pill
  // header so the two views never disagree (P1-34).
  window.vdbSortNewest = desc;
  const singleBtn = document.getElementById('vdb-sort-btn');
  if (singleBtn) singleBtn.innerHTML = window.vdbSortNewest ? 'Newest &#9660;' : 'Oldest &#9650;';
  try { sessionStorage.setItem(`vdb_allcams_sort_${host}`, desc ? 'desc' : 'asc'); } catch (e) { /* ignore */ }
  vdbEnsureViewToggle(host);
  if (vdbViewMode !== 'all_cams') return;
  const date = vdbGetNight();
  if (!date) return;
  const cams = state.VDB_CAMERAS[host] || [];
  _vdbAllCamsRenderTimeline(host, cams, date);
}

/** Show / hide the per-cam selector + sort + locked-only based on view mode.
 *  The shared date picker stays visible in either mode. */
function vdbApplyViewModeUI() {
  const camSel    = document.getElementById('vdb-camera');
  const camPtg    = document.getElementById('vdb-cam-pointing');
  const lockedLbl = document.getElementById('vdb-locked-only')?.closest('.vdb-toggle-label');
  const sortBtn   = document.getElementById('vdb-sort-btn');
  const allCams   = vdbViewMode === 'all_cams';
  // The camera selector and its pointing label only matter in single-cam mode.
  if (camSel) camSel.style.display = allCams ? 'none' : '';
  if (camPtg) camPtg.style.display = allCams ? 'none' : '';
  // "Locked only" and the sort button operate against vdbAllChunks for the
  // single active camera. In all-cams mode we render per-cam grids straight
  // from the raw chunk lists, so hide them to avoid stale UI controls.
  if (lockedLbl) lockedLbl.style.display = allCams ? 'none' : '';
  if (sortBtn)   sortBtn.style.display   = allCams ? 'none' : '';
  // The cam-label on the camera filter row would otherwise reserve space
  // when the buttons are hidden — hide its <label> too.
  const camLabel = document.querySelector('#pane-videodb .vdb-filter-group');
  if (camLabel) camLabel.style.display = allCams ? 'none' : '';
}

function vdbSwitchView(host, mode) {
  if (vdbViewMode === mode) return;
  vdbViewMode = mode;
  vdbSaveViewMode(host, mode);
  vdbEnsureViewToggle(host);
  vdbApplyViewModeUI();
  // Stop the single-cam poll when leaving single mode — it polls one camera
  // anyway and would just churn against the now-hidden selector.
  vdbStopPoll();
  // Re-fetch and re-render under the new mode using the current date.
  if (mode === 'all_cams') {
    vdbRenderAllCams(host);
  } else {
    // Going back to single-cam: clear the all-cams DOM and re-run the
    // normal single-camera fetch path so polling resumes.
    document.getElementById('vdb-results').innerHTML = '';
    vdbOnCameraChange();
  }
}

function vdbGetSelectedCamera() {
  const container = document.getElementById('vdb-camera');
  if (!container) return null;
  const sel = container.querySelector('select');
  if (sel) return sel.value;
  const active = container.querySelector('.cam-btn.active');
  return active ? active.dataset.cam : null;
}

function vdbPopulateCameras() {
  const station = state.activeStation;
  const container = document.getElementById('vdb-camera');
  container.innerHTML = '';
  const cams = state.VDB_CAMERAS[station] || [];
  if (window.innerWidth <= 540) {
    const sel = document.createElement('select');
    cams.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c; opt.textContent = c;
      sel.appendChild(opt);
    });
    sel.onchange = () => vdbOnCameraChange();
    container.appendChild(sel);
  } else {
    cams.forEach((c, i) => {
      const btn = document.createElement('button');
      btn.className = 'cam-btn' + (i === 0 ? ' active' : '');
      btn.dataset.cam = c;
      btn.textContent = c;
      btn.onclick = () => {
        container.querySelectorAll('.cam-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        vdbOnCameraChange();
      };
      container.appendChild(btn);
    });
  }
}

async function vdbOnCameraChange() {
  // Close any clip modal still open from the previous camera so the
  // new camera's results aren't hidden behind a stale player.
  _closeAllOpenModals();
  vdbStopPoll();
  const station  = state.activeStation;
  const camera   = vdbGetSelectedCamera();
  const apiHost  = hostForCamera(station, camera);
  const nightSel = document.getElementById('vdb-night');
  const statusEl = document.getElementById('vdb-status');
  nightSel.innerHTML = '';
  statusEl.textContent = 'Loading\u2026';
  vdbUpdateAzLabels();
  document.getElementById('vdb-results').innerHTML  = '';
  vdbAllChunks = [];
  if (!camera) { statusEl.textContent = ''; return; }
  try {
    const nights = await fetchJson(`/api/videodb/nights/${apiHost}/${camera}`);
    if (!nights.length) {
      statusEl.textContent = 'No nights available';
      return;
    }
    // Populate dropdown — nights are newest-first
    const toDisplay = d => `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`;
    nightSel.innerHTML = nights.map(d => `<option value="${escHtml(d)}">${escHtml(toDisplay(d))}</option>`).join('');
    // Pick the newest night that actually has chunks. The /api/videodb/nights
    // endpoint can return placeholder dirs (e.g. the upcoming night's stub
    // before sunset), so naively picking nights[0] strands the user on
    // "Loading..." / "no clips found" — see RO000H @ 2026-05-23 reported by
    // alex during the gmn0008 audit.
    let chosen = nights[0];
    const candidates = nights.slice(0, 5);
    const probes = await Promise.allSettled(
      candidates.map(n =>
        fetchJson(`/api/videodb/chunks/${apiHost}/${camera}/${n}?locked_only=0`)
      )
    );
    const firstGood = probes.findIndex(r => {
      if (r.status !== 'fulfilled') return false;
      const v = r.value;
      return Array.isArray(v) ? v.length > 0 : (v?.chunks?.length || 0) > 0;
    });
    if (firstGood >= 0) chosen = candidates[firstGood];
    vdbSetNight(chosen);
    statusEl.textContent = '';
    vdbOnNightChange();
  } catch(e) {
    statusEl.textContent = 'Error loading nights';
  }
}

export function vdbNextDay(nightDate) {
  const y = +nightDate.slice(0,4), m = +nightDate.slice(4,6)-1, d = +nightDate.slice(6,8);
  const dt = new Date(Date.UTC(y, m, d+1));
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth()+1).padStart(2,'0')}-${String(dt.getUTCDate()).padStart(2,'0')}`;
}

async function vdbOnNightChange() {
  const station  = state.activeStation;
  const camera   = vdbGetSelectedCamera();
  const apiHost  = hostForCamera(station, camera);
  const date     = vdbGetNight();
  const statusEl = document.getElementById('vdb-status');
  if (!date) return;
  document.getElementById('vdb-midnight-label').textContent = vdbNextDay(date);
  // All-cams mode shares the date picker with single-cam but fans out chunk
  // fetches per-camera. Still kick off the twilight clamp so the shared
  // time-slider markers stay accurate; ignore single-cam-only side effects.
  if (vdbViewMode === 'all_cams') {
    statusEl.textContent = '';
    vdbAllChunks = [];
    document.getElementById('vdb-slider-start').value = 0;
    document.getElementById('vdb-slider-end').value = 1440;
    vdbUpdateTimeLabel();
    vdbRedrawCanvas();
    vdbClampToTwilight(station, date);
    vdbRenderAllCams(station);
    return;
  }
  statusEl.textContent = 'Loading\u2026';
  vdbAllChunks = [];
  vdbArchivedFiles = new Set();
  vdbCachedFiles = new Set();
  // Reset sliders to full range while fetching (twilight will clamp them below)
  document.getElementById('vdb-slider-start').value = 0;
  document.getElementById('vdb-slider-end').value = 1440;
  vdbUpdateTimeLabel();
  vdbRedrawCanvas();
  try {
    const [chunks, archiveResp, cacheResp] = await Promise.all([
      fetchJson(`/api/videodb/chunks/${apiHost}/${camera}/${date}?locked_only=${document.getElementById('vdb-locked-only').checked ? '1' : '0'}`),
      fetch(`/api/archive/indexed/${camera}/${date}`).catch(() => ({ok:false})),
      fetch(`/api/cached-files/${camera}/${date}`).catch(() => ({ok:false})),
      vdbClampToTwilight(station, date),
    ]);
    vdbAllChunks = chunks;
    if (archiveResp.ok) {
      const archived = await archiveResp.json().catch(() => []);
      vdbArchivedFiles = new Set(archived);
    }
    if (cacheResp.ok) {
      const cached = await cacheResp.json().catch(() => []);
      vdbCachedFiles = new Set(cached);
    }
    vdbRedrawCanvas();
    vdbRenderResults();
    vdbStartPoll();
  } catch(e) {
    statusEl.textContent = 'Error: ' + e.message;
  }
}

/**
 * Re-fetch chunks when the "Locked only" toggle flips WITHOUT resetting the
 * user-chosen time-range sliders. `vdbOnNightChange()` clobbers the sliders
 * back to twilight; that's correct for a night change, wrong for a filter
 * toggle.
 */
async function vdbOnLockedOnlyChange() {
  const station  = state.activeStation;
  const camera   = vdbGetSelectedCamera();
  const apiHost  = hostForCamera(station, camera);
  const date     = vdbGetNight();
  const statusEl = document.getElementById('vdb-status');
  if (!date) return;
  // Save current slider positions so the user's chosen window survives the
  // re-fetch round-trip.
  const startEl = document.getElementById('vdb-slider-start');
  const endEl   = document.getElementById('vdb-slider-end');
  const savedStart = startEl ? startEl.value : null;
  const savedEnd   = endEl   ? endEl.value   : null;
  if (vdbViewMode === 'all_cams') {
    vdbRenderAllCams(station);
    return;
  }
  statusEl.textContent = 'Loading…';
  vdbAllChunks = [];
  vdbArchivedFiles = new Set();
  vdbCachedFiles = new Set();
  try {
    const [chunks, archiveResp, cacheResp] = await Promise.all([
      fetchJson(`/api/videodb/chunks/${apiHost}/${camera}/${date}?locked_only=${document.getElementById('vdb-locked-only').checked ? '1' : '0'}`),
      fetch(`/api/archive/indexed/${camera}/${date}`).catch(() => ({ok:false})),
      fetch(`/api/cached-files/${camera}/${date}`).catch(() => ({ok:false})),
    ]);
    vdbAllChunks = chunks;
    if (archiveResp.ok) {
      const archived = await archiveResp.json().catch(() => []);
      vdbArchivedFiles = new Set(archived);
    }
    if (cacheResp.ok) {
      const cached = await cacheResp.json().catch(() => []);
      vdbCachedFiles = new Set(cached);
    }
    // Restore the user's slider range before redrawing.
    if (savedStart != null && startEl) startEl.value = savedStart;
    if (savedEnd   != null && endEl)   endEl.value   = savedEnd;
    vdbUpdateTimeLabel();
    vdbRedrawCanvas();
    vdbRenderResults();
    vdbStartPoll();
  } catch(e) {
    statusEl.textContent = 'Error: ' + e.message;
  }
}

/** Convert UTC minutes-from-midnight to VDB slider value (0=12:00 UTC, 720=00:00 UTC, 1440=12:00+1). */
function utcMinToSlider(utcMin) {
  // Delegates to the shared twilight-slider module so the two consumers
  // (dashboard.html VDB tab + events.html event filter) can't drift.
  return window.twilightSlider.utcMinToSliderVal(utcMin);
}

function vdbDrawTwilightMarkers(tw) {
  // Renderer lives in twilight-slider.js (A-3: dedupe). Page-specific bits
  // (wrap selector, moon icon ID) are resolved by drawTwilightOnWrap.
  const wrap = document.querySelector('.vdb-slider-wrap');
  window.twilightSlider.drawTwilightOnWrap(wrap, tw);
}

/** Fetch twilight for a station+date and clamp VDB sliders to civil sunset..civil sunrise range. */
async function vdbClampToTwilight(host, date) {
  const sEl = document.getElementById('vdb-slider-start');
  const eEl = document.getElementById('vdb-slider-end');
  if (!sEl || !eEl) return;
  try {
    const r = await fetch(`/api/twilight/${host}/${date}`);
    if (!r.ok) return;
    const tw = await r.json();
    // Civil twilight (-6°) range, from the shared helper.
    const { startVal, endVal } = window.twilightSlider.computeTwilightClampRange(tw);
    sEl.value = startVal;
    eEl.value = endVal;
    vdbUpdateTimeLabel();
    vdbDrawTwilightMarkers(tw);
  } catch(e) {
    // On failure, keep full 0..1440 range
  }
}

function vdbSliderInput(which) {
  const sEl = document.getElementById('vdb-slider-start');
  const eEl = document.getElementById('vdb-slider-end');
  if (which === 'start' && parseInt(sEl.value) > parseInt(eEl.value)) sEl.value = eEl.value;
  if (which === 'end'   && parseInt(eEl.value) < parseInt(sEl.value)) eEl.value = sEl.value;
  vdbUpdateTimeLabel();
  vdbRedrawCanvas();
  clearTimeout(vdbSliderTimer);
  vdbSliderTimer = setTimeout(vdbRenderResults, 120);
}

export function vdbSliderToFmt(v) {
  const m = (v + 720) % 1440;
  return `${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;
}

// Parse "HH:MM" (or "HHMM") \u2192 minutes-of-day, or null if invalid.
export function vdbParseHHMM(str) {
  const s = String(str).trim().replace(/\s/g, '');
  let m = /^(\d{1,2}):(\d{2})$/.exec(s) || /^(\d{1,2})(\d{2})$/.exec(s);
  if (!m) return null;
  const h = parseInt(m[1]); const mn = parseInt(m[2]);
  if (h < 0 || h > 24 || mn < 0 || mn > 59 || (h === 24 && mn !== 0)) return null;
  return h * 60 + mn;
}

// minutes-of-day \u2192 slider value (slider is noon-anchored: 0 = noon, 720 = midnight, 1440 = next noon).
export function vdbMinutesToSliderVal(min, isEnd) {
  if (min === 720) return isEnd ? 1440 : 0;
  return ((min - 720) + 1440) % 1440;
}

function vdbUpdateTimeLabel() {
  const s = parseInt(document.getElementById('vdb-slider-start').value);
  const e = parseInt(document.getElementById('vdb-slider-end').value);
  const sIn = document.getElementById('vdb-time-start');
  const eIn = document.getElementById('vdb-time-end');
  if (sIn && document.activeElement !== sIn) { sIn.value = vdbSliderToFmt(s); sIn.classList.remove('invalid'); }
  if (eIn && document.activeElement !== eIn) { eIn.value = vdbSliderToFmt(e); eIn.classList.remove('invalid'); }
}

function vdbTimeInputChange(which) {
  const inEl = document.getElementById(which === 'start' ? 'vdb-time-start' : 'vdb-time-end');
  const slEl = document.getElementById(which === 'start' ? 'vdb-slider-start' : 'vdb-slider-end');
  const min = vdbParseHHMM(inEl.value);
  if (min === null) { inEl.classList.add('invalid'); return; }
  inEl.classList.remove('invalid');
  slEl.value = vdbMinutesToSliderVal(min, which === 'end');
  vdbSliderInput(which);  // clamps + re-renders + writes back the formatted value
}

function vdbRedrawCanvas() {
  const canvas = document.getElementById('vdb-density-canvas');
  if (!canvas) return;
  const W = canvas.parentElement.clientWidth || 600;
  canvas.width = W;
  const H = 5;
  const ctx = canvas.getContext('2d');
  const s = parseInt(document.getElementById('vdb-slider-start').value);
  const e = parseInt(document.getElementById('vdb-slider-end').value);

  // All chunks — dim marks
  ctx.fillStyle = 'rgba(88,166,255,0.14)';
  const markW = Math.max(2, Math.ceil(W / 720));
  vdbAllChunks.forEach(c => {
    const p = c.time.split(':');
    const clockM = parseInt(p[0]) * 60 + parseInt(p[1]);
    const sliderPos = (clockM - 720 + 1440) % 1440;
    ctx.fillRect(Math.floor((sliderPos / 1440) * W), 0, markW, H);
  });

  // Selection range overlay
  ctx.fillStyle = 'rgba(88,166,255,0.12)';
  ctx.fillRect((s/1440)*W, 0, ((e-s)/1440)*W, H);

  // Filtered chunks — bright marks
  ctx.fillStyle = 'rgba(88,166,255,0.7)';
  vdbFilteredChunks().forEach(c => {
    const p = c.time.split(':');
    const clockM = parseInt(p[0]) * 60 + parseInt(p[1]);
    const sliderPos = (clockM - 720 + 1440) % 1440;
    ctx.fillRect(Math.floor((sliderPos / 1440) * W), 0, markW, H);
  });
}

function vdbFilteredChunks() {
  const s = parseInt(document.getElementById('vdb-slider-start').value);
  const e = parseInt(document.getElementById('vdb-slider-end').value);
  const filtered = vdbAllChunks.filter(c => {
    const p = c.time.split(':');
    const clockM = parseInt(p[0]) * 60 + parseInt(p[1]);
    const sliderPos = (clockM - 720 + 1440) % 1440;
    return sliderPos >= s && sliderPos <= e;
  });
  return window.vdbSortNewest ? filtered.slice().reverse() : filtered;
}

function vdbToggleSort() {
  window.vdbSortNewest = !window.vdbSortNewest;
  // Mirror onto the all-cams sort + persist per host (P1-34 — single & all-cams
  // share one sort direction).
  window.vdbAllCamsSortDesc = window.vdbSortNewest;
  try {
    if (state.activeStation) sessionStorage.setItem(
      `vdb_allcams_sort_${state.activeStation}`, window.vdbAllCamsSortDesc ? 'desc' : 'asc');
  } catch (e) { /* ignore */ }
  const btn = document.getElementById('vdb-sort-btn');
  if (btn) btn.innerHTML = window.vdbSortNewest ? 'Newest &#9660;' : 'Oldest &#9650;';
  // Refresh the all-cams sort segmented control so it matches.
  if (state.activeStation) vdbEnsureViewToggle(state.activeStation);
  vdbRenderResults();
}

/* Convert a (YYYYMMDD, HH:MM:SS) pair into epoch ms. The night folder name
   is the date the night STARTED; clips with HH < 12 belong to the following
   UTC calendar day (rollover at noon UTC — same convention as vdbIsTonight
   and the GMN matcher). Returns null on bad input. */
const VDB_STALE_MS = 5 * 60 * 1000;
export function vdbChunkUtcMs(ymd, hms) {
  if (!ymd || !hms || ymd.length < 8) return null;
  const [hh, mm, ss] = hms.split(':').map(n => parseInt(n, 10));
  if (Number.isNaN(hh) || Number.isNaN(mm)) return null;
  const y = parseInt(ymd.slice(0, 4), 10);
  const mo = parseInt(ymd.slice(4, 6), 10) - 1;
  let d = parseInt(ymd.slice(6, 8), 10);
  if (hh < 12) d += 1;
  return Date.UTC(y, mo, d, hh, mm, ss || 0);
}

/* Returns the index of the single visible clip we should paint stale, or
   -1 if none. A clip qualifies only when:
     1) it's the most-recent clip by time across the visible set, AND
     2) the selected night is tonight (RMS should still be live-capturing), AND
     3) it's more than VDB_STALE_MS behind wall-clock UTC.
   We deliberately don't paint every old clip red — that just means "old",
   not "the pipeline is lagging". */
function vdbStaleIdx(chunks, ymd) {
  if (!chunks.length || !vdbIsTonight()) return -1;
  let latestIdx = -1, latestMs = -Infinity;
  for (let i = 0; i < chunks.length; i++) {
    const cms = vdbChunkUtcMs(ymd, chunks[i].time);
    if (cms != null && cms > latestMs) { latestMs = cms; latestIdx = i; }
  }
  if (latestIdx < 0) return -1;
  return (Date.now() - latestMs) > VDB_STALE_MS ? latestIdx : -1;
}

/* Delegated click handler for the single-cam chunk grid. Reads all
   server-supplied values from data-* attributes (set via escHtml/JSON at
   render time) so no station-controlled string ever lands in an onclick
   attribute context. */
function _vdbGridClick(e) {
  const lockBtn = e.target.closest('.vdb-lock-trigger');
  if (lockBtn) {
    e.stopPropagation();
    const card = lockBtn.closest('.vdb-chunk-card');
    if (!card) return;
    const d = JSON.parse(card.dataset.chunk || '{}');
    vdbToggleLock(d.apiHost, d.camera, d.date, d.filename, lockBtn);
    return;
  }
  const processBtn = e.target.closest('.vdb-process-trigger');
  if (processBtn) {
    e.stopPropagation();
    const card = processBtn.closest('.vdb-chunk-card');
    if (!card) return;
    const d = JSON.parse(card.dataset.chunk || '{}');
    vdbProcessChunk(d.apiHost, d.camera, d.date, d.filename, processBtn);
    return;
  }
  const stackLink = e.target.closest('.vdb-stack-trigger');
  if (stackLink) {
    e.preventDefault();
    e.stopPropagation();
    const card = stackLink.closest('.vdb-chunk-card');
    if (!card) return;
    const d = JSON.parse(card.dataset.chunk || '{}');
    vdbOpenModal(d.apiHost, d.camera, d.date, d.filename, d.time, d.sizeMb, d.detOff, d.idx, d.archiveVideoSrc, true);
    return;
  }
  const card = e.target.closest('.vdb-card-trigger');
  if (card) {
    const d = JSON.parse(card.dataset.chunk || '{}');
    vdbOpenModal(d.apiHost, d.camera, d.date, d.filename, d.time, d.sizeMb, d.detOff, d.idx, d.archiveVideoSrc);
  }
}

function vdbRenderResults() {
  // No-op in all-cams mode — vdbRenderAllCams owns the #vdb-results DOM and
  // mutates per-camera grids directly; running the single-cam renderer here
  // would wipe them on every slider input.
  if (vdbViewMode === 'all_cams') return;
  const station   = state.activeStation;
  const camera    = vdbGetSelectedCamera();
  const apiHost   = hostForCamera(station, camera);
  const date      = vdbGetNight();
  const statusEl  = document.getElementById('vdb-status');
  const resultsEl = document.getElementById('vdb-results');
  const zoom      = document.getElementById('vdb-zoom').value;
  if (!date) return;
  const chunks = vdbFilteredChunks();
  window._vdbVisibleChunks = chunks;
  if (!chunks.length) {
    statusEl.textContent = vdbAllChunks.length
      ? `No clips in selected range \u00b7 ${vdbAllChunks.length} total for night`
      : 'No clips found.';
    resultsEl.innerHTML = '';
    return;
  }
  const _staleIdx = vdbStaleIdx(chunks, date);
  statusEl.textContent = `${chunks.length} clip${chunks.length!==1?'s':''} \u00b7 ${fmtDate(date)}`;
  const cards = chunks.map((c, idx) => {
    const isArchive = c.source === 'archive';
    const thumbUrl = c.stack
      ? (isArchive
          ? `/api/archive/file/${camera}/${date}/${c.stack_subdir || 'meteors'}/${encodeURIComponent(c.stack)}`
          : `/stack/${apiHost}/${camera}/${date}/${encodeURIComponent(c.stack)}`)
      : null;
    const imgHtml = thumbUrl
      ? `<img src="${thumbUrl}" loading="lazy" decoding="async" alt="${c.time}"
             onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
      : '';
    const placeholderStyle = thumbUrl ? 'display:none' : '';
    const inArchive = vdbArchivedFiles.has(c.filename);
    const inCache = vdbCachedFiles.has(c.filename);
    const onServer = inArchive || inCache;
    const archiveClass = onServer ? ' vdb-chunk-archived' : '';
    const archiveBadge = inArchive ? '<span class="vdb-archived-badge">ARCHIVED</span>'
                       : inCache ? '<span class="vdb-archived-badge" style="background:rgba(88,166,255,.85)">CACHED</span>' : '';
    const lockClass = c.lock_type === 'manual' ? ' vdb-chunk-locked-manual'
                    : c.lock_type === 'detection' ? ' vdb-chunk-locked-detection' : '';
    const lockIcon  = c.locked ? LOCK_ICON : UNLOCK_ICON;
    const lockTitle = c.lock_type === 'detection' ? 'Detection locked'
                    : c.locked ? 'Remove manual lock' : 'Lock this clip';
    const canLock = canToggleLock(apiHost);
    const lockBtn = c.lock_type === 'detection'
      ? `<button class="vdb-lock-btn vdb-lock-btn--detection" title="${lockTitle}" disabled>${LOCK_ICON}</button>`
      : canLock && c.locked
      ? `<button class="vdb-lock-btn vdb-lock-btn--manual vdb-lock-trigger" title="${lockTitle}"
         >${lockIcon}</button>`
      : canLock
      ? `<button class="vdb-lock-btn vdb-lock-trigger" title="${lockTitle}"
         >${lockIcon}</button>`
      : c.locked ? `<button class="vdb-lock-btn vdb-lock-btn--${c.lock_type || 'manual'}" title="Locked" disabled>${LOCK_ICON}</button>` : '';
    // Real null (not the string 'null'): this rides inside the data-chunk JSON
    // payload and is JSON.parsed back in _vdbGridClick, so it must be a genuine
    // JS null. The old 'null' sentinel only worked when it was interpolated as a
    // JS literal into an inline onclick (removed for XSS hardening).
    const detOff   = c.lock_type === 'detection' ? (c.detection_offset_s ?? null) : null;
    const archiveVideoSrc = isArchive
      ? `/api/archive/file/${camera}/${date}/meteors/${encodeURIComponent(c.filename)}`
      : '';
    const archiveImgSrc = isArchive && c.stack
      ? `/api/archive/file/${camera}/${date}/${c.stack_subdir || 'meteors'}/${encodeURIComponent(c.stack)}`
      : '';
    const cardData = escHtml(JSON.stringify({
      apiHost, camera, date, filename: c.filename, time: c.time,
      sizeMb: c.size_mb, detOff, idx, archiveVideoSrc,
    }));
    const timeClass = idx === _staleIdx ? 'vdb-chunk-time vdb-time-stale' : 'vdb-chunk-time';
    return `<div class="vdb-chunk-card${lockClass}${archiveClass} vdb-card-trigger"
        data-filename="${escHtml(c.filename)}" data-lock-type="${escHtml(c.lock_type || '')}"
        data-chunk="${cardData}">
      ${archiveBadge}${lockBtn}
      ${imgHtml}
      <div class="vdb-chunk-nostack" style="${placeholderStyle}">\u25b6</div>
      <div class="vdb-chunk-meta">
        <div class="${timeClass}">${escHtml(c.time)} UTC</div>
        <div class="vdb-chunk-size">${c.size_mb != null ? c.size_mb + ' MB' : ''}</div>
        <div class="vdb-chunk-btns">
          ${isArchive
            ? `<button class="vdb-process-btn" disabled title="Not available for archived clips">&#128190; Archive</button>`
            : c.reencoded
              ? `<button class="vdb-process-btn vdb-process-btn--done" disabled>&#10003; Processed</button>`
              : state.USER_ROLE === 'visitor'
              ? ''
              : `<button class="vdb-process-btn vdb-process-trigger" title="Encode with overlay &amp; colour calibration"
                   >&#9881; Process</button>`}
          ${thumbUrl ? `<a class="vdb-dl-btn vdb-stack-trigger" href="#">&#8718; Stack</a>` : ''}
        </div>
      </div>
    </div>`;
  }).join('');
  resultsEl.innerHTML = `<div class="card"><div class="vdb-chunk-grid" id="vdb-chunk-grid"
    style="grid-template-columns:repeat(auto-fill,minmax(${zoom}px,1fr))">${cards}</div></div>`;
  // Attach delegated listeners so server-controlled strings never appear in
  // onclick attribute context. All payload comes from data-chunk (JSON, already
  // HTML-escaped at render time) rather than from inline event handlers.
  const grid = resultsEl.querySelector('#vdb-chunk-grid');
  if (grid) {
    grid.addEventListener('click', _vdbGridClick);
  }
}

function vdbUpdateZoom(val) {
  const tpl = `repeat(auto-fill,minmax(${val}px,1fr))`;
  const grid = document.getElementById('vdb-chunk-grid');
  if (grid) grid.style.gridTemplateColumns = tpl;
  document.querySelectorAll('.vdb-cam-grid').forEach(g => {
    g.style.gridTemplateColumns = tpl;
  });
}

/* ─────────────────────────────────────────
   All-cams view: per-camera chunk grids
───────────────────────────────────────── */

/** Build a single chunk-card for the all-cams filmstrip. Clicks open the
 *  synced multi-camera modal. Manual-lock button mirrors the single-cam
 *  view so operators can pin clips for saving from this view too. */
function _vdbAllCamsCardHtml(host, cam, date, chunk) {
  const apiHost  = hostForCamera(host, cam);
  const isArchive = chunk.source === 'archive';
  const thumbUrl = chunk.stack
    ? (isArchive
        ? `/api/archive/file/${cam}/${date}/${chunk.stack_subdir || 'meteors'}/${encodeURIComponent(chunk.stack)}`
        : `/stack/${apiHost}/${cam}/${date}/${encodeURIComponent(chunk.stack)}`)
    : null;
  const imgHtml = thumbUrl
    ? `<img src="${thumbUrl}" loading="lazy" decoding="async" alt="${chunk.time}"
           onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
    : '';
  const placeholderStyle = thumbUrl ? 'display:none' : '';
  const lockClass = chunk.lock_type === 'manual' ? ' vdb-chunk-locked-manual'
                  : chunk.lock_type === 'detection' ? ' vdb-chunk-locked-detection' : '';
  const lockIcon  = chunk.locked ? LOCK_ICON : UNLOCK_ICON;
  const lockTitle = chunk.lock_type === 'detection' ? 'Detection locked'
                  : chunk.locked ? 'Remove manual lock' : 'Lock this clip';
  const canLock = canToggleLock(apiHost);
  const lockBtn = chunk.lock_type === 'detection'
    ? `<button class="vdb-lock-btn vdb-lock-btn--detection" title="${lockTitle}" disabled>${LOCK_ICON}</button>`
    : canLock && chunk.locked
    ? `<button class="vdb-lock-btn vdb-lock-btn--manual vdb-allcam-lock-trigger" title="${lockTitle}"
       >${lockIcon}</button>`
    : canLock
    ? `<button class="vdb-lock-btn vdb-allcam-lock-trigger" title="${lockTitle}"
       >${lockIcon}</button>`
    : chunk.locked
    ? `<button class="vdb-lock-btn vdb-lock-btn--${chunk.lock_type || 'manual'}" title="Locked" disabled>${LOCK_ICON}</button>`
    : '';
  const cardData = escHtml(JSON.stringify({
    host, cam, apiHost, date, filename: chunk.filename, time: chunk.time,
  }));
  return `<div class="vdb-chunk-card${lockClass} vdb-allcam-card-trigger"
      data-filename="${escHtml(chunk.filename)}" data-lock-type="${escHtml(chunk.lock_type || '')}"
      data-allcam="${escHtml(cam)}" data-chunk="${cardData}">
    ${lockBtn}
    ${imgHtml}
    <div class="vdb-chunk-nostack" style="${placeholderStyle}">▶</div>
    <div class="vdb-chunk-meta">
      <div class="vdb-chunk-time">${escHtml(chunk.time)} UTC</div>
      <div class="vdb-chunk-size">${chunk.size_mb != null ? chunk.size_mb + ' MB' : ''}</div>
    </div>
  </div>`;
}

let _vdbCurrentHost = null;

/** Parse a chunk time string (HH:MM:SS) to seconds, adjusted for night wraparound. */
function _allCamsTimeSec(t) {
  if (!t) return 0;
  const [h, m, s] = t.split(':').map(Number);
  const sec = h * 3600 + m * 60 + (s || 0);
  return h < 12 ? sec + 86400 : sec;
}

/** Group chunks from all cameras by timestamp proximity (±windowSec).
 *  Uses the first camera with data as the timeline reference. */
function _vdbGroupByTime(allChunks, cams, windowSec) {
  const refCam = cams.find(c => allChunks[c]?.length);
  if (!refCam) return [];
  return allChunks[refCam].map(refChunk => {
    const refSec = _allCamsTimeSec(refChunk.time);
    const slots = { [refCam]: refChunk };
    for (const cam of cams) {
      if (cam === refCam) continue;
      let best = null, bestDiff = Infinity;
      for (const c of (allChunks[cam] || [])) {
        const d = Math.abs(_allCamsTimeSec(c.time) - refSec);
        if (d < bestDiff) { bestDiff = d; best = c; }
      }
      if (best && bestDiff <= windowSec) slots[cam] = best;
    }
    return { refCam, refChunk, slots };
  });
}

/** Build one time-slot card showing all cameras side by side. */
function _vdbTimeSlotCardHtml(host, cams, date, group) {
  const camCells = cams.map(cam => {
    const chunk = group.slots[cam];
    if (!chunk) {
      return `<div class="vdb-ts-cam"><div class="vdb-ts-cam-lbl">${cam}</div><div class="vdb-ts-empty"></div></div>`;
    }
    const apiHost = hostForCamera(host, cam);
    const thumbUrl = chunk.stack ? `/stack/${apiHost}/${cam}/${date}/${encodeURIComponent(chunk.stack)}` : null;
    const imgHtml = thumbUrl
      ? `<img src="${thumbUrl}" loading="lazy" decoding="async" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
      : '';
    const phStyle = thumbUrl ? 'display:none' : '';
    const lockBadge = chunk.lock_type === 'detection'
      ? `<span class="vdb-ts-badge vdb-ts-badge--det" title="Detection locked">${LOCK_ICON}</span>`
      : chunk.locked
      ? `<span class="vdb-ts-badge vdb-ts-badge--manual" title="Manually locked">${LOCK_ICON}</span>`
      : '';
    return `<div class="vdb-ts-cam">
      <div class="vdb-ts-cam-lbl">${cam}</div>
      <div class="vdb-ts-thumb">
        ${imgHtml}
        <div class="vdb-chunk-nostack" style="${phStyle}">▶</div>
        ${lockBadge}
      </div>
    </div>`;
  }).join('');
  const { refChunk } = group;
  // Encode only the cameras that were actually matched (shown in this card).
  const slotMap = {};
  for (const [cam, chunk] of Object.entries(group.slots)) {
    slotMap[cam] = { f: chunk.filename, t: chunk.time, s: chunk.stack || null };
  }
  return `<div class="vdb-ts-card" data-host="${host}" data-date="${date}" data-slot="${escHtml(JSON.stringify(slotMap))}" onclick="vdbOpenTimeSlot(this)">
    <div class="vdb-ts-cams">
      ${camCells}
      <button class="vdb-dome-btn" title="Sky dome view" onclick="event.stopPropagation();vdbOpenDome(this.closest('.vdb-ts-card'))">🌍</button>
    </div>
    <div class="vdb-ts-meta">
      <span>${refChunk.time}<br><span style="font-weight:400;opacity:.6">UTC</span></span>
    </div>
  </div>`;
}

/** Render the timeline from cached chunks. Safe to call multiple times. */
function _vdbAllCamsRenderTimeline(host, cams, date) {
  const grid = document.getElementById('vdb-allcam-global');
  if (!grid || grid.dataset.host !== host || grid.dataset.date !== date) return;
  const desc = !!window.vdbAllCamsSortDesc;
  const allChunks = {};
  for (const cam of cams) {
    const raw = _vdbChunksByCam[`${host}|${cam}|${date}`] || [];
    allChunks[cam] = raw.slice().sort((a, b) => {
      const cmp = _allCamsTimeSec(a.time) - _allCamsTimeSec(b.time);
      return desc ? -cmp : cmp;
    });
  }
  const groups = _vdbGroupByTime(allChunks, cams, 15);
  if (!groups.length) {
    grid.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:12px">No clips for this night</div>';
    return;
  }
  grid.innerHTML = groups.map(g => _vdbTimeSlotCardHtml(host, cams, date, g)).join('');
}

const _vdbAllCamsPending = new Map();

/** Fetch chunks for one camera, cache them, then render the timeline once all
 *  cameras for this host+date have loaded. */
async function vdbFetchAndRenderCamChunks(host, cam, date) {
  const apiHost = hostForCamera(host, cam);
  try {
    const raw = await fetchJson(`/api/videodb/chunks/${apiHost}/${cam}/${date}?locked_only=0`);
    _vdbChunksByCam[`${host}|${cam}|${date}`] = Array.isArray(raw) ? raw : (raw?.chunks || []);
  } catch (e) {
    _vdbChunksByCam[`${host}|${cam}|${date}`] = [];
  }
  const key = `${host}|${date}`;
  const remaining = (_vdbAllCamsPending.get(key) ?? 1) - 1;
  if (remaining <= 0) {
    _vdbAllCamsPending.delete(key);
    const cams = state.VDB_CAMERAS[host] || [];
    _vdbAllCamsRenderTimeline(host, cams, date);
  } else {
    _vdbAllCamsPending.set(key, remaining);
  }
}

let _vdbResizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(_vdbResizeTimer);
  _vdbResizeTimer = setTimeout(() => {
    if (window.innerWidth < 768 && vdbViewMode === 'all_cams' && _vdbCurrentHost) {
      vdbSwitchView(_vdbCurrentHost, 'single');
    }
    if (_vdbCurrentHost) vdbEnsureViewToggle(_vdbCurrentHost);
  }, 200);
});

function vdbRenderAllCams(host) {
  const cams = state.VDB_CAMERAS[host] || [];
  const container = document.getElementById('vdb-results');
  if (!container) return;
  if (!cams.length) { container.innerHTML = '<div class="no-data">No cameras configured.</div>'; return; }
  const date = vdbGetNight();
  if (!date) { container.innerHTML = '<div class="no-data">No night selected.</div>'; return; }
  _vdbCurrentHost = host;
  container.innerHTML = `<div class="card vdb-allcams-card"><div class="vdb-allcams-scroll"><div class="vdb-allcams-timeline" id="vdb-allcam-global" data-host="${host}" data-date="${date}"><div style="color:var(--muted);font-size:12px;padding:12px">Loading ${cams.length} camera${cams.length !== 1 ? 's' : ''}…</div></div></div></div>`;
  const statusEl = document.getElementById('vdb-status');
  if (statusEl) statusEl.textContent = `${cams.length} camera${cams.length !== 1 ? 's' : ''} · ${fmtDate(date)}`;
  const key = `${host}|${date}`;
  _vdbAllCamsPending.set(key, cams.length);
  for (const cam of cams) vdbFetchAndRenderCamChunks(host, cam, date);
}

/* ─────────────────────────────────────────
   Synced multi-cam modal
───────────────────────────────────────── */

const _VDB_SYNC_WINDOW_S = 300;

function vdbCloseSyncedModal() {
  _getVdbSyncModal().close();
}

async function vdbOpenSyncedModal(host, clickedCam, date, filename, time) {
  const clickedMs = vdbChunkUtcMs(date, time);
  if (clickedMs == null) return;
  const cams = state.VDB_CAMERAS[host] || [];
  if (!cams.length) return;

  // Ensure each cam's chunk list is loaded.
  await Promise.all(cams.map(async cam => {
    const key = `${host}|${cam}|${date}`;
    if (_vdbChunksByCam[key]) return;
    try {
      const apiHost = hostForCamera(host, cam);
      const r = await fetch(`/api/videodb/chunks/${apiHost}/${cam}/${date}?locked_only=0`);
      const raw = await r.json();
      _vdbChunksByCam[key] = Array.isArray(raw) ? raw : (raw?.chunks || []);
    } catch (e) { _vdbChunksByCam[key] = []; }
  }));

  const winMs = _VDB_SYNC_WINDOW_S * 1000;
  const matches = cams.map(cam => {
    const list = _vdbChunksByCam[`${host}|${cam}|${date}`] || [];
    let best = null, bestDelta = Infinity;
    for (const c of list) {
      const ms = vdbChunkUtcMs(date, c.time);
      if (ms == null) continue;
      const delta = Math.abs(ms - clickedMs);
      if (delta <= winMs && delta < bestDelta) { best = c; bestDelta = delta; }
    }
    return { cam, match: best, ms: best ? vdbChunkUtcMs(date, best.time) : null };
  });

  const matched = matches.filter(m => m.ms != null);
  if (!matched.length) return;

  // Reference t=0 = clicked chunk start; offsets = (referenceMs - chunkMs) / 1000.
  // windowSec covers the max spread between matched chunks + one chunk duration.
  const maxSpreadMs = Math.max(...matched.map(m => Math.abs(m.ms - clickedMs)));
  const windowSec   = Math.ceil(maxSpreadMs / 1000) + 22;

  const videos = matches.map(({ cam, match, ms }) => {
    if (!match || ms == null) return null;
    const apiHost = hostForCamera(host, cam);
    const rawSrc = match.source === 'archive'
      ? `/api/archive/file/${cam}/${date}/meteors/${encodeURIComponent(match.filename)}`
      : `/api/cached-video/${apiHost}/${cam}/${date}/${encodeURIComponent(match.filename)}`;
    const url = rawSrc + (rawSrc.includes('?') ? '&' : '?') + 'format=mp4';
    const offset   = (clickedMs - ms) / 1000;
    const stackUrl = match.stack ? `/stack/${apiHost}/${cam}/${date}/${encodeURIComponent(match.stack)}` : null;
    return { cam, station: host, date, url, offset, label: match.time, filename: match.filename, stackUrl };
  });

  _getVdbSyncModal().open({
    title: `All cameras  ·  ${fmtDate(date)}  ·  ${time} UTC`,
    videos: videos.filter(Boolean),
    windowPreSec:  0,
    windowPostSec: windowSec,
    showDetection: false,
  });
}

/** Open the synced modal from an All Cams timeline card, using only the
 *  cameras that were actually matched and displayed in that card. */
async function vdbOpenTimeSlot(el) {
  const host  = el.dataset.host;
  const date  = el.dataset.date;
  const slots = JSON.parse(el.dataset.slot);  // {cam: {f, t, s}}
  const entries = Object.entries(slots);
  if (!entries.length) return;

  const refMs = vdbChunkUtcMs(date, entries[0][1].t);
  if (refMs == null) return;

  // Ensure chunk cache is populated (needed for the modal's stitch/lock features).
  const cams = state.VDB_CAMERAS[host] || [];
  await Promise.all(cams.map(async cam => {
    const key = `${host}|${cam}|${date}`;
    if (_vdbChunksByCam[key]) return;
    try {
      const apiHost = hostForCamera(host, cam);
      const r   = await fetch(`/api/videodb/chunks/${apiHost}/${cam}/${date}?locked_only=0`);
      const raw = await r.json();
      _vdbChunksByCam[key] = Array.isArray(raw) ? raw : (raw?.chunks || []);
    } catch (e) { _vdbChunksByCam[key] = []; }
  }));

  const maxSpreadMs = Math.max(...entries.map(([, s]) => {
    const ms = vdbChunkUtcMs(date, s.t);
    return ms != null ? Math.abs(ms - refMs) : 0;
  }));
  const windowSec = Math.ceil(maxSpreadMs / 1000) + 22;

  const videos = entries.map(([cam, slot]) => {
    const apiHost = hostForCamera(host, cam);
    const chunkMs = vdbChunkUtcMs(date, slot.t);
    const offset  = chunkMs != null ? (refMs - chunkMs) / 1000 : 0;
    const url     = `/api/cached-video/${apiHost}/${cam}/${date}/${encodeURIComponent(slot.f)}?format=mp4`;
    const stackUrl = slot.s ? `/stack/${apiHost}/${cam}/${date}/${encodeURIComponent(slot.s)}` : null;
    return { cam, station: host, date, url, offset, label: slot.t, filename: slot.f, stackUrl };
  });

  _getVdbSyncModal().open({
    title:         `All cameras  ·  ${fmtDate(date)}  ·  ${entries[0][1].t} UTC`,
    videos,
    windowPreSec:  0,
    windowPostSec: windowSec,
    showDetection: false,
  });
}
window.vdbOpenTimeSlot = vdbOpenTimeSlot;

/** Open the All Cams modal for a card and immediately activate dome view. */
async function vdbOpenDome(cardEl) {
  await vdbOpenTimeSlot(cardEl);
  _getVdbSyncModal()._toggleDomeMode();
}
window.vdbOpenDome = vdbOpenDome;


/* ─────────────────────────────────────────
   VDB Select mode
───────────────────────────────────────── */

async function vdbToggleLock(station, camera, date, filename, btn) {
  const card    = btn.closest('.vdb-chunk-card');
  const isLocked = card.classList.contains('vdb-chunk-locked-detection') || card.classList.contains('vdb-chunk-locked-manual');
  btn.disabled  = true;
  try {
    const resp = await fetch(
      `/api/lock/${station}/${camera}/${date}/${encodeURIComponent(filename)}`,
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({locked: !isLocked})}
    );
    if (!resp.ok) {
      const ct = resp.headers.get('content-type') || '';
      if (ct.includes('text/html') || resp.status === 401) {
        throw new Error(`Session expired (${resp.status}). Please refresh the page.`);
      }
      if (resp.status === 403) {
        throw new Error('You do not have access to this station.');
      }
      const body = await resp.text();
      throw new Error(body.length > 200 ? body.slice(0, 200) + '...' : body);
    }
    const data = await resp.json();
    // Update card state in-place
    card.classList.remove('vdb-chunk-locked-detection', 'vdb-chunk-locked-manual');
    if (data.locked) card.classList.add(`vdb-chunk-locked-${data.lock_type}`);
    btn.classList.remove('vdb-lock-btn--detection', 'vdb-lock-btn--manual');
    if (data.locked) btn.classList.add(`vdb-lock-btn--${data.lock_type}`);
    btn.innerHTML = data.lock_type === 'detection' ? LOCK_ICON : data.locked ? LOCK_ICON : UNLOCK_ICON;
    btn.title     = data.locked ? 'Remove manual lock' : 'Lock this clip';
    // Sync vdbAllChunks so filtering stays consistent
    const chunk = vdbAllChunks.find(c => c.filename === filename);
    if (chunk) { chunk.locked = data.locked; chunk.lock_type = data.lock_type; }
  } catch(e) {
    alert('Lock failed: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

function vdbOpenModal(station, camera, date, filename, time, sizeMb, detOffset = null, idx = -1, videoSrc = '', startInStack = false) {
  _modalOpenedFromRms = false;
  _vdbCurrentIdx = idx;

  const rawSrc = videoSrc || `/api/cached-video/${station}/${camera}/${date}/${encodeURIComponent(filename)}`;
  const src = rawSrc + (rawSrc.includes('?') ? '&' : '?') + 'format=mp4';

  const chunkRec = (window._vdbVisibleChunks || []).find(c => c.filename === filename);
  const rmsDet = window._lookupDetection(filename, chunkRec?.meteor_time);

  const det  = (window.settingsData?.[station] || {}).detection || {};
  const pre  = det.pre_seconds  ?? 3;
  const post = det.post_seconds ?? 12;
  const trimStart = detOffset != null ? Math.max(0, detOffset - pre) : 0;
  const trimEnd   = detOffset != null ? Math.min(20, detOffset + post) : undefined;

  const n = (window._vdbVisibleChunks || []).length;
  const arrayDelta_prev = window.vdbSortNewest ? 1 : -1;
  const arrayDelta_next = window.vdbSortNewest ? -1 : 1;
  const stitchEarlierIdx = window.vdbSortNewest ? idx + 1 : idx - 1;
  const stitchLaterIdx   = window.vdbSortNewest ? idx - 1 : idx + 1;
  const navPrevIdx = idx + arrayDelta_prev;
  const navNextIdx = idx + arrayDelta_next;

  const isArchive = Boolean(videoSrc && videoSrc.startsWith('/api/archive/'));
  let stack = null;
  if (chunkRec?.stack && !isArchive) {
    stack = { url: `/fullstack/${station}/${camera}/${date}/${encodeURIComponent(chunkRec.stack)}` };
  }

  _getVdbModal().open({
    src,
    title: `${camera}  ·  ${fmtDate(date)}  ·  ${time} UTC`,
    station, camera, date, filename,
    loopDlPath: isArchive
      ? `/loop_clip_archive/${camera}/${date}/${encodeURIComponent(filename)}`
      : `/loop_clip/${station}/${camera}/${date}/${encodeURIComponent(filename)}`,
    detOffset,
    trimStart,
    trimEnd,
    detection: rmsDet || null,
    stack,
    startInStack,
    download: { onClick: _vdbDownload },
    stitch: {
      hasPrev:    idx >= 0 && stitchEarlierIdx >= 0 && stitchEarlierIdx < n,
      hasNext:    idx >= 0 && stitchLaterIdx   >= 0 && stitchLaterIdx   < n,
      onPrev:     () => vdbStitchAdj(-1),
      onNext:     () => vdbStitchAdj(1),
      onUnstitch: () => vdbUnstitch(),
    },
    nav: {
      hasPrev: idx >= 0 && navPrevIdx >= 0 && navPrevIdx < n,
      hasNext: idx >= 0 && navNextIdx >= 0 && navNextIdx < n,
      onPrev:  () => vdbNavModal(-1),
      onNext:  () => vdbNavModal(1),
    },
  });

  const video = _vdbModal.getVideoEl();
  if (video) {
    video.addEventListener('canplay', () => {
      if (!vdbCachedFiles.has(filename)) {
        vdbCachedFiles.add(filename);
        vdbRenderResults();
      }
    }, { once: true });
  }
}

async function _vdbDownload() {
  const modal = _getVdbModal();
  const ts = modal.getTrimState();
  if (!ts) return;
  const dlBtn = modal._el?.querySelector('.vm-download-btn');
  if (dlBtn) { dlBtn.disabled = true; dlBtn.textContent = 'Preparing…'; }
  try {
    const { station, camera, date, filename, filename2, stitched, stitchAdjIsLater, start, end } = ts;
    let url;
    if (stitched && filename2) {
      const [f1, f2] = stitchAdjIsLater ? [filename, filename2] : [filename2, filename];
      url = `/shortclip_stitch/${station}/${camera}/${date}?f1=${encodeURIComponent(f1)}&f2=${encodeURIComponent(f2)}&ss=${start.toFixed(2)}&t=${(end - start).toFixed(2)}`;
    } else {
      url = `/shortclip/${station}/${camera}/${date}/${encodeURIComponent(filename)}?ss=${start.toFixed(2)}&t=${(end - start).toFixed(2)}`;
    }
    const resp = await fetch(url);
    if (!resp.ok) {
      _toast(resp.status === 404 ? 'Clip no longer available on station' : `Download failed (${resp.status})`, 'error');
      return;
    }
    const cd = resp.headers.get('content-disposition');
    let dlFilename = 'clip.mp4';
    if (cd) { const m = cd.match(/filename="?([^";\n]+)"?/); if (m) dlFilename = m[1]; }
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = dlFilename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  } catch (e) {
    _toast('Download failed: network error', 'error');
  } finally {
    if (dlBtn) { dlBtn.disabled = false; dlBtn.innerHTML = '&#8595; Download clip'; }
  }
}

function vdbCloseModal(e) {
  _getVdbModal().close();
}

function vdbNavModal(delta) {
  if (!window._vdbVisibleChunks.length || _vdbCurrentIdx < 0) return;
  const arrayDelta = _modalOpenedFromRms ? delta : (window.vdbSortNewest ? -delta : delta);
  const newIdx = Math.max(0, Math.min(window._vdbVisibleChunks.length - 1, _vdbCurrentIdx + arrayDelta));
  if (newIdx === _vdbCurrentIdx) return;
  const c = window._vdbVisibleChunks[newIdx];
  const ts = _vdbModal?.getTrimState();
  const station = ts?.station || '';
  const camera  = ts?.camera  || '';
  const date = c._date || ts?.date || '';
  const detOff = c.lock_type === 'detection' ? (c.detection_offset_s ?? null) : null;
  vdbOpenModal(station, camera, date, c.filename, c.time, c.size_mb, detOff, newIdx);
}

async function vdbProcessChunk(host, cam, date, filename, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '\u23f3';
  try {
    const r = await fetch(`/api/encode_chunk/${host}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({camera: cam, date, filename}),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || 'encode returned ok:false');
    const chunk = vdbAllChunks.find(c => c.filename === filename);
    if (chunk) chunk.reencoded = true;
    btn.textContent = '\u2713 Processed';
    btn.className = 'vdb-process-btn vdb-process-btn--done';
    btn.disabled = true;
    btn.onclick = null;
    setTimeout(() => vdbRenderResults(), 800);
  } catch (err) {
    btn.textContent = '\u2717';
    btn.title = err.message || 'Failed';
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
  }
}


function _trimInit() {} // removed — handled by VideoModal

async function vdbStitchAdj(chronoDelta) {
  const arrayDelta = window.vdbSortNewest ? -chronoDelta : chronoDelta;
  const adjIdx = _vdbCurrentIdx + arrayDelta;
  if (adjIdx < 0 || adjIdx >= window._vdbVisibleChunks.length) return;
  const adj = window._vdbVisibleChunks[adjIdx];
  const modal = _getVdbModal();
  const ts = modal.getTrimState();
  if (!ts) return;
  const { station, camera, date, filename } = ts;
  const [f1, f2] = chronoDelta > 0 ? [filename, adj.filename] : [adj.filename, filename];

  modal.updateStitch({ isStitched: true, label: 'Stitching…' });

  const video = modal.getVideoEl();
  const src = `/stitch_video/${station}/${camera}/${date}?f1=${encodeURIComponent(f1)}&f2=${encodeURIComponent(f2)}&format=mp4`;
  video.src = src;
  video.load();
  video.play().catch(() => {});

  const stitchAt = chronoDelta > 0 ? (ts.duration || 20) : 0;
  modal.updateTrim({
    stitched: true,
    filename2: adj.filename,
    stitchAdjIsLater: chronoDelta > 0,
    stitchAt,
    start: Math.max(0, stitchAt - 5),
    end:   stitchAt + 10,
  });

  video.addEventListener('loadedmetadata', () => {
    const dur = isFinite(video.duration) && video.duration > 0 ? video.duration : 40;
    modal.updateTrim({ duration: dur, stitchAt: dur / 2, end: Math.min(dur, stitchAt + 10) });
  }, { once: true });

  modal.updateStitch({ isStitched: true, label: `Stitched with ${chronoDelta > 0 ? 'next' : 'prev'}` });
}

function vdbUnstitch() {
  const modal = _getVdbModal();
  const ts = modal.getTrimState();
  if (!ts) return;
  const { station, camera, date, filename, detOffset, videoSrc } = ts;

  modal.updateTrim({ stitched: false, filename2: null, stitchAt: null });

  const video = modal.getVideoEl();
  const rawSrc = videoSrc || `/api/cached-video/${station}/${camera}/${date}/${encodeURIComponent(filename)}`;
  video.src = rawSrc + (rawSrc.includes('?') ? '&' : '?') + 'format=mp4';
  video.load();
  video.play().catch(() => {});
  video.addEventListener('loadedmetadata', () => {
    const dur = isFinite(video.duration) && video.duration > 0 ? video.duration : 20;
    const det = (window.settingsData?.[station] || {}).detection || {};
    const pre = det.pre_seconds ?? 3;
    const post = det.post_seconds ?? 12;
    modal.updateTrim({
      duration: dur,
      start: detOffset != null ? Math.max(0, detOffset - pre) : 0,
      end:   detOffset != null ? Math.min(dur, detOffset + post) : dur,
    });
  }, { once: true });

  const n = (window._vdbVisibleChunks || []).length;
  const stitchEarlierIdx = window.vdbSortNewest ? _vdbCurrentIdx + 1 : _vdbCurrentIdx - 1;
  const stitchLaterIdx   = window.vdbSortNewest ? _vdbCurrentIdx - 1 : _vdbCurrentIdx + 1;
  modal.updateStitch({
    hasPrev: stitchEarlierIdx >= 0 && stitchEarlierIdx < n,
    hasNext: stitchLaterIdx   >= 0 && stitchLaterIdx   < n,
    isStitched: false,
  });
}

// Expose to global scope for inline onclick handlers
window.vdbCloseModal = vdbCloseModal;
window.vdbNavModal = vdbNavModal;
window.vdbStitchAdj = vdbStitchAdj;
window.vdbToggleSort = vdbToggleSort;
window.vdbUnstitch = vdbUnstitch;
window.vdbOpenSyncedModal = vdbOpenSyncedModal;
window.vdbSetAllCamsSort = vdbSetAllCamsSort;
window.vdbSwitchView = vdbSwitchView;
window.vdbStopPoll = vdbStopPoll;
window.vdbInit = vdbInit;
window.vdbLoadAzimuths = vdbLoadAzimuths;
window.hostForCamera = hostForCamera;
window.vdbCloseSyncedModal = vdbCloseSyncedModal;
window.vdbModalIsOpen = () => !!_vdbModal?._el?.classList.contains('open');
window.vdbPoll = vdbPoll;
window.vdbOpenModal = vdbOpenModal;
window.vdbProcessChunk = vdbProcessChunk;
window.vdbToggleLock = vdbToggleLock;
window.vdbSliderInput = vdbSliderInput;
window.vdbTimeInputChange = vdbTimeInputChange;
window.vdbUpdateZoom = vdbUpdateZoom;
window.vdbOnNightChange = vdbOnNightChange;
window.vdbOnLockedOnlyChange = vdbOnLockedOnlyChange;
window._vdbAzCompass = _vdbAzCompass;
