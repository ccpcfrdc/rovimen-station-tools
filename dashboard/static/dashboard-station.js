import { clampDateInputsToToday, fmtBytes, fmtDate, barColor, fetchJson, appPanelClose, _modalOpen, _modalClose, _closeAllOpenModals, escHtml, state } from './dashboard-common.js';
/* global renderStationDropdown, updateStationDots, rmsPlotExpand,
          vdbCloseModal, arcModalClose, vdbNavModal */
/*
 * dashboard-station.js
 * ────────────────────
 * Per-station detail page (dashboard.html): tabs, RMS detection grid,
 * Video Database, Archive, Station Admin (settings, sysadmin tab,
 * cron, dawn, logs), services + storage cards, live feed modals.
 *
 * Depends on dashboard-common.js (formatters, app-panel, state.STATIONS_META,
 * globals on `window`).
 */

/* ─────────────────────────────────────────
   askConfirm(message, onConfirm, opts) — in-DOM replacement for the
   blocking window.confirm() used by destructive actions (reboot,
   updater, restart-services, dawn run, restart-service). The native
   confirm() halts timers, SSE streams and live updates while the user
   decides, and on iOS it cannot be styled. This helper renders a card
   styled like .cam-modal/.vdb-modal, keeps the event loop running, and
   exposes Cancel + Confirm buttons that meet the 44 px tap target.

   Signature:
     askConfirm(message, onConfirm, {
       confirmLabel?: string = 'Confirm',
       cancelLabel?:  string = 'Cancel',
       destructive?: boolean = true,   // red Confirm vs neutral blue
       title?:        string,           // optional bold heading line
       onCancel?:     () => void,
     })

   Only one askConfirm card is mounted at a time; opening a second
   replaces the first. Escape / clicking the backdrop cancels.
───────────────────────────────────────── */
function askConfirm(message, onConfirm, opts) {
  opts = opts || {};
  const confirmLabel = opts.confirmLabel || 'Confirm';
  const cancelLabel  = opts.cancelLabel  || 'Cancel';
  const destructive  = opts.destructive !== false;
  const title        = opts.title || '';

  // Remove any prior card so we don't stack overlays.
  document.querySelectorAll('.ask-confirm').forEach(n => n.remove());

  const card = document.createElement('div');
  card.className = 'ask-confirm open';
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'true');

  const titleHtml = title
    ? `<div style="font-weight:600;margin-bottom:6px">${escHtmlSafe(title)}</div>`
    : '';
  card.innerHTML = `
    <div class="ask-confirm-card">
      <div class="ask-confirm-body">
        ${titleHtml}
        <div>${escHtmlSafe(message)}</div>
      </div>
      <div class="ask-confirm-actions">
        <button type="button" class="ask-confirm-btn ask-confirm-cancel">${escHtmlSafe(cancelLabel)}</button>
        <button type="button" class="ask-confirm-btn ask-confirm-ok${destructive ? '' : ' neutral'}">${escHtmlSafe(confirmLabel)}</button>
      </div>
    </div>`;
  document.body.appendChild(card);

  const okBtn     = card.querySelector('.ask-confirm-ok');
  const cancelBtn = card.querySelector('.ask-confirm-cancel');

  function cleanup() {
    document.removeEventListener('keydown', onKey, true);
    card.remove();
  }
  function fireCancel() {
    cleanup();
    try { opts.onCancel && opts.onCancel(); } catch(e) { /* swallow */ }
  }
  function fireOk() {
    cleanup();
    try { onConfirm(); } catch(e) { /* caller logs */ }
  }
  function onKey(e) {
    if (e.key === 'Escape') { e.stopPropagation(); fireCancel(); }
    else if (e.key === 'Enter') { e.stopPropagation(); fireOk(); }
  }

  okBtn.addEventListener('click', fireOk);
  cancelBtn.addEventListener('click', fireCancel);
  card.addEventListener('click', (e) => { if (e.target === card) fireCancel(); });
  document.addEventListener('keydown', onKey, true);

  // Focus Confirm so keyboard users can hit Enter immediately; cancel
  // gets the visual default outline.
  setTimeout(() => { try { okBtn.focus(); } catch(e) {} }, 0);
}

// Minimal HTML escaper used by askConfirm. dashboard-station.js already
// has an escHtml() defined far below, but the helper is declared up here
// at the top of the file so all five confirm() call-sites can rely on
// it regardless of declaration order. Keep this independent so future
// refactors of escHtml() can't accidentally break the confirm flow.
export function escHtmlSafe(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]
  ));
}

/* ─────────────────────────────────────────
   Station-only state. Cross-bundle state (state.STATIONS_META, state.statusData,
   state.vitalsData, state.activeStation, state.activeTab, etc.) lives in dashboard-common.js
   and is read here via the `var` aliases that common.js exposes.
───────────────────────────────────────── */
let rmsTabState   = {};   // `${host}_${cam}` -> { nights, selectedNight }
window.settingsData  = {};   // host -> config object (window-scoped for cross-module access)
window.encStatsData  = {};   // host -> encoding stats from station API
window.hwData        = {};   // host -> hardware profile
let liveActive    = {};   // `${host}_${camCode}` -> bool
let liveAutoStop  = {};   // `${host}_${camCode}` -> { deadline, timeoutId, intervalId }
let _liveRetries  = {};   // `${host}_${camCode}` -> int (error count)

// Live view eats encoder cycles + ifb bandwidth on the camera. While RMS is
// actively capturing meteors (rms-cam* services running), keeping a stream
// open causes frame drops. Auto-stop the feed after 60 s with a visible
// countdown so an opened stream can never be left running overnight.
const LIVE_VIEW_MAX_S = 60;
window._trimState    = {};   // current modal detection trim state
window._trimRaf      = null; // requestAnimationFrame id for playhead

let statusTimer          = null;
let vitalsTimer          = null;
let sysadminVitalsTimer  = null; // 10s fetch-then-reschedule while System Admin tab is active
let sysadminStorageTimer = null; // 30s storage refresh while System Admin tab is active
let dawnProgressTimer    = null;
let dawnProgressState    = {};   // host -> { date: 'YYYYMMDD' }
let updaterLogTimer      = null;
let vdbSkyDomeTimer      = null; // 60s BW sky-dome cache-bust while Video DB tab is active
window.vdbAllCamsSortDesc   = true; // chunk order per row in All cams view; true = newest first.
                                 // P1-34: single-cam window.vdbSortNewest mirrors this value at
                                 // station-switch + toggle time so the two views can't disagree.

// Sky-dome freshness indicator state — captureMs is the newest FF capture
// time across cameras the last server-side render used. Refreshed via
// /api/sky_dome_meta on the same 60 s cadence as the dome image itself.
const _domeFreshness = {};       // host -> {captureMs: number|null}
let _domeFreshTicker = null;     // 1 s "Now" clock ticker

// System Admin tab state
let activeSysAdminTab = {};  // host -> 'health' | 'processes' | 'updatelogs' | 'livestream'
window.vitalsHistory     = {};  // host -> ring buffer of vitals snapshots
window.coreData          = {};  // host -> [{load_pct, freq_ghz}]
window.cronsData         = {};  // host -> [{schedule, command, source}]
window.rmsStatusData     = {};  // host -> {date, cameras: {cam: {processed, uploaded}}}
window.rmsStatusDate     = {};  // host -> 'YYYYMMDD'
window.storagewatchData  = {};  // host -> {last_run, deleted_gb, filesystems}

/* ─────────────────────────────────────────
   Inflight fetch dedupe / abort
   ─────────────────────────────────────────
   Rapid tab-switches used to fire identical fetches in parallel, where the
   older response could clobber the newer one. fetchOnce(key, ...) aborts any
   prior request for `key` before issuing the next. abortAllInflight('tab:')
   is called on tab change so background tab fetches stop holding sockets
   open. Callers must swallow AbortError on the awaited fetch.
───────────────────────────────────────── */
const _inflight = new Map();
function fetchOnce(key, url, options = {}) {
  const prev = _inflight.get(key);
  if (prev) prev.controller.abort();
  const controller = new AbortController();
  const p = fetch(url, { ...options, signal: controller.signal })
    .finally(() => {
      if (_inflight.get(key)?.controller === controller) _inflight.delete(key);
    });
  _inflight.set(key, { controller, promise: p });
  return p;
}
function abortAllInflight(prefix) {
  for (const [k, v] of _inflight) {
    if (!prefix || k.startsWith(prefix)) v.controller.abort();
  }
}
export function _isAbort(e) { return e && e.name === 'AbortError'; }

/* ─────────────────────────────────────────
   Station / Tab switching
───────────────────────────────────────── */
function selectStation(host) {
  if (host === state.activeStation) return;
  // Any modal left open from the previous station (live cam, RMS plot,
  // VDB clip, app panel) must close before we swap content — otherwise
  // a player or plot lingers on top of the new station's pane.
  _closeAllOpenModals();
  state.activeStation = host;
  if (window.renderStationDropdown) renderStationDropdown();
  const _m = state.STATIONS_META[host];
  if (_m) updateNextCapture(_m.lat, _m.lon);
  updateTabVisibility(host);
  window._vdbAzimuths = {};
  window.vdbLoadAzimuths?.(host);

  // Clear per-tab timers so switchTab restarts them fresh for the new station
  if (dawnProgressTimer)   { clearInterval(dawnProgressTimer);   dawnProgressTimer   = null; }
  if (sysadminVitalsTimer) { clearTimeout(sysadminVitalsTimer);  sysadminVitalsTimer = null; }
  if (sysadminStorageTimer){ clearInterval(sysadminStorageTimer);sysadminStorageTimer= null; }
  if (vdbSkyDomeTimer)     { clearInterval(vdbSkyDomeTimer);     vdbSkyDomeTimer     = null; }
  if (_domeFreshTicker)    { clearInterval(_domeFreshTicker);    _domeFreshTicker    = null; }

  // Full tab re-init for the new station (also updates the URL)
  const tabBtn = document.getElementById('tab-btn-' + state.activeTab);
  if (tabBtn) switchTab(state.activeTab, tabBtn);

  // Fetch fresh data for this station if we have none
  if (!state.statusData[host]) fetchStatus(host);
  if (!state.vitalsData[host]) fetchVitals(host);
}

function switchTab(tab, btn, { push = true } = {}) {
  // Any modal left open from the previous tab (RMS plot, VDB clip,
  // archive playback, app panel) must close before we swap panes —
  // otherwise it lingers on top of the new tab's content.
  _closeAllOpenModals();
  if (tab !== 'videodb' && window.vdbStopPoll) window.vdbStopPoll();
  if (tab !== 'settings' && window.logsState?.live) { clearInterval(window.logsState.timer); window.logsState.timer = null; window.logsState.live = false; }
  if (tab !== 'settings' && dawnProgressTimer)   { clearInterval(dawnProgressTimer);   dawnProgressTimer   = null; }
  if (tab !== 'settings' && sysadminVitalsTimer) { clearTimeout(sysadminVitalsTimer);  sysadminVitalsTimer = null; }
  if (tab !== 'settings' && sysadminStorageTimer){ clearInterval(sysadminStorageTimer);sysadminStorageTimer= null; }
  if (tab !== 'videodb' && vdbSkyDomeTimer)      { clearInterval(vdbSkyDomeTimer);     vdbSkyDomeTimer     = null; }
  if (tab !== state.activeTab) abortAllInflight('tab:');
  state.activeTab = tab;
  document.querySelectorAll('.sub-tab').forEach(b => {
    b.classList.remove('active');
    b.setAttribute('aria-selected', 'false');
  });
  btn.classList.add('active');
  btn.setAttribute('aria-selected', 'true');
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-' + tab).classList.add('active');
  if (push) history.pushState({ station: state.activeStation, tab }, '', `/station/${state.activeStation}/${tab}`);
  renderActiveTab();

  // Lazy loads on tab open
  if (tab === 'rms') {
    window.renderRMS?.(state.activeStation);
  }
  if (tab === 'settings') {
    if (!window.settingsData[state.activeStation]) fetchSettings(state.activeStation);
    else {
      renderSysAdmin(state.activeStation);
      fetchDawnProgress(state.activeStation);
    }
    if (!dawnProgressTimer) {
      dawnProgressTimer = setInterval(() => { if (state.activeTab === 'settings') fetchDawnProgress(state.activeStation); }, 10000);
    }
    if (!sysadminVitalsTimer) {
      (function scheduleVitalsPoll() {
        sysadminVitalsTimer = setTimeout(async () => {
          if (state.activeTab === 'settings') {
            await fetchVitalsFast(state.activeStation);
          }
          if (state.activeTab === 'settings') scheduleVitalsPoll();
          else sysadminVitalsTimer = null;
        }, 10000);
      })();
    }
    if (!sysadminStorageTimer) {
      sysadminStorageTimer = setInterval(() => {
        if (state.activeTab === 'settings') fetchStorageForSysAdmin(state.activeStation);
      }, 30000);
    }
  }
  if (tab === 'videodb') {
    if (window.vdbLoadedStation !== state.activeStation && window.vdbInit) window.vdbInit();
    // Idempotent mount — vdbMountSkyDome() bails out if the card is already
    // there for the active station, so re-entering the tab doesn't
    // re-create the DOM node (and date-picker changes don't either, since
    // they only repaint #vdb-results, not the pane root).
    vdbMountSkyDome(state.activeStation);
    domeFetchFreshness(state.activeStation);
    domeEnsureFreshTicker();
    if (!vdbSkyDomeTimer) {
      // Live BW dome — 60 s cache-bust so the newest FF maxpixel rolls in
      // while the user is browsing the Video DB tab. Always shows
      // tonight's sky regardless of which date the user picked below.
      vdbSkyDomeTimer = setInterval(() => {
        if (state.activeTab === 'videodb') vdbRefreshSkyDome(state.activeStation);
      }, 60000);
    }
  }
  if (tab === 'fdp') {
    renderFinalDataProducts(state.activeStation);
    if (!state.tlData[state.activeStation]) fetchTimelapses(state.activeStation);
  }
  if (tab === 'archive') {
    window.archiveInit?.();
  }
}

function renderActiveTab() {
  const host = state.activeStation;
  if (state.activeTab === 'rms')       window.renderRMS?.(host);
  if (state.activeTab === 'settings')  renderSysAdmin(host);
  if (state.activeTab === 'fdp')       renderFinalDataProducts(host);
}

/* ─────────────────────────────────────────
   Vitals card (in-place update)
───────────────────────────────────────── */
function mkVitals(host) {
  const v = state.vitalsData[host];
  // The dashboard backend marks payloads as stale when the station-side probe
  // hasn't responded but a cached value is being served (P1-42). Show a small
  // badge next to the card title so operators don't read months-old vitals as
  // fresh. The flag may live on either the vitals or the status payload —
  // both routes set it — so check both.
  const isStale = !!(v && v.stale) || !!(state.statusData && state.statusData[host] && state.statusData[host].stale);
  const staleBadge = isStale
    ? ` <span style="font-size:10px;font-weight:600;background:rgba(245,166,35,0.18);color:#f5a623;padding:2px 6px;border-radius:3px;margin-left:6px" title="Station hasn't refreshed recently — serving cached telemetry.">stale</span>`
    : '';
  if (!v) return `<div class="card">
    <div class="card-title">Vitals <span style="font-size:10px;color:var(--muted)">(60s refresh)</span>${staleBadge}</div>
    <div class="loading">Loading…</div></div>`;
  // online=false OR the station-side returned online=true but no actual
  // metrics (gmn0007 reports online=true with error="psutil not installed"
  // and null cpu_pct/ram_pct, which used to render "NaN / NaN GB (null%)").
  const noMetrics = v.cpu_pct == null && v.ram_pct == null && v.ram_used_mb == null;
  if (!v.online || noMetrics) return `<div class="card">
    <div class="card-title">Vitals${staleBadge}</div>
    <div class="no-data">Unavailable — ${v.error||'psutil missing?'}</div></div>`;

  const cpuCol = barColor(v.cpu_pct);
  const ramCol = barColor(v.ram_pct);
  const ramUsed  = v.ram_used_mb  != null ? (v.ram_used_mb  / 1024).toFixed(1) : '—';
  const ramTotal = v.ram_total_mb != null ? (v.ram_total_mb / 1024).toFixed(1) : '—';
  const ramPctStr = v.ram_pct != null ? v.ram_pct + '%' : '—';
  const cpuPctStr = v.cpu_pct != null ? v.cpu_pct + '%' : '—';
  const tempC = v.temp_c;
  const tempCol = tempC != null ? (tempC > 75 ? 'var(--red)' : tempC > 60 ? 'var(--yellow)' : 'var(--green)') : 'var(--muted)';
  const tempPct = tempC != null ? Math.min(100, Math.round(tempC)) : 0;
  return `<div class="card" id="vitals-card-${host}">
    <div class="card-title">Vitals <span style="font-size:10px;color:var(--muted)">(60s refresh)</span>${staleBadge}</div>
    <div class="vitals-grid" style="grid-template-columns:1fr 1fr 1fr">
      <div class="vitals-item">
        <div class="vitals-label">
          <span>CPU</span>
          <span id="vit-cpu-val-${host}" class="v-cpu">${cpuPctStr}</span>
        </div>
        <div class="vitals-bar-track" role="progressbar"
             aria-label="CPU usage" aria-valuemin="0" aria-valuemax="100"
             aria-valuenow="${v.cpu_pct ?? 0}">
          <div class="vitals-bar-fill" id="vit-cpu-bar-${host}"
               style="width:${v.cpu_pct ?? 0}%;background:${cpuCol}"></div>
        </div>
      </div>
      <div class="vitals-item">
        <div class="vitals-label">
          <span>RAM</span>
          <span id="vit-ram-val-${host}" class="v-ram">${ramUsed} / ${ramTotal} GB (${ramPctStr})</span>
        </div>
        <div class="vitals-bar-track" role="progressbar"
             aria-label="RAM usage" aria-valuemin="0" aria-valuemax="100"
             aria-valuenow="${v.ram_pct ?? 0}">
          <div class="vitals-bar-fill" id="vit-ram-bar-${host}"
               style="width:${v.ram_pct ?? 0}%;background:${ramCol}"></div>
        </div>
      </div>
      <div class="vitals-item">
        <div class="vitals-label">
          <span>Temp</span>
          <span id="vit-temp-val-${host}" style="color:${tempCol}">${tempC != null ? tempC + '\u00b0C' : '--'}</span>
        </div>
        <div class="vitals-bar-track" role="progressbar"
             aria-label="Temperature (degrees Celsius)" aria-valuemin="0" aria-valuemax="100"
             aria-valuenow="${tempPct}">
          <div class="vitals-bar-fill" id="vit-temp-bar-${host}"
               style="width:${tempPct}%;background:${tempCol}"></div>
        </div>
      </div>
    </div>
  </div>`;
}

function updateVitalsDOM(host) {
  // In-place update — only touch the bar + value elements if card is rendered
  const v = state.vitalsData[host];
  if (!v || !v.online) return;
  // Same fall-through as mkVitals: if metrics are absent, leave the card
  // as-is rather than overwriting "Unavailable" with NaN/null.
  if (v.cpu_pct == null && v.ram_pct == null && v.ram_used_mb == null) return;
  const cpuVal = document.getElementById(`vit-cpu-val-${host}`);
  const cpuBar = document.getElementById(`vit-cpu-bar-${host}`);
  const ramVal = document.getElementById(`vit-ram-val-${host}`);
  const ramBar = document.getElementById(`vit-ram-bar-${host}`);
  if (!cpuVal) return;  // card not rendered yet
  const ramUsed  = v.ram_used_mb  != null ? (v.ram_used_mb  / 1024).toFixed(1) : '—';
  const ramTotal = v.ram_total_mb != null ? (v.ram_total_mb / 1024).toFixed(1) : '—';
  const ramPctStr = v.ram_pct != null ? v.ram_pct + '%' : '—';
  cpuVal.textContent = v.cpu_pct != null ? v.cpu_pct + '%' : '—';
  cpuBar.style.width = (v.cpu_pct ?? 0) + '%';
  cpuBar.style.background = barColor(v.cpu_pct);
  cpuBar.parentElement?.setAttribute('aria-valuenow', String(v.cpu_pct ?? 0));
  ramVal.textContent = `${ramUsed} / ${ramTotal} GB (${ramPctStr})`;
  ramBar.style.width = (v.ram_pct ?? 0) + '%';
  ramBar.style.background = barColor(v.ram_pct);
  ramBar.parentElement?.setAttribute('aria-valuenow', String(v.ram_pct ?? 0));
  // Temperature
  const tempVal = document.getElementById(`vit-temp-val-${host}`);
  const tempBar = document.getElementById(`vit-temp-bar-${host}`);
  if (tempVal && v.temp_c != null) {
    const tempCol = v.temp_c > 75 ? 'var(--red)' : v.temp_c > 60 ? 'var(--yellow)' : 'var(--green)';
    tempVal.textContent = v.temp_c + '\u00b0C';
    tempVal.style.color = tempCol;
    const tempPct = Math.min(100, Math.round(v.temp_c));
    tempBar.style.width = tempPct + '%';
    tempBar.style.background = tempCol;
    tempBar.parentElement?.setAttribute('aria-valuenow', String(tempPct));
  }
}

/* ─────────────────────────────────────────
   Services card
───────────────────────────────────────── */
function mkServices(services) {
  const rovimenMap = {
    'rovimen-nightwatcher': 'Nightwatcher',
    'rovimen-coppermind':   'Coppermind',
    'color-capture':        'Color Capture',
    'rovimen-tineye':       'Tineye',
    'rovimen-station-api':  'Station API',
  };
  // Build rms-cam* -> station code lookup from config
  const camLookup = {};
  if (state.STATIONS_META[state.activeStation]) {
    state.STATIONS_META[state.activeStation].cameras.forEach((c, i) => {
      camLookup[`rms-cam${i + 1}`] = c.code;
    });
    // rms-capture (single-process RMS) — show all camera codes
    if (!camLookup['rms-cam1']) {
      const codes = state.STATIONS_META[state.activeStation].cameras.map(c => c.code).join('/');
      camLookup['rms-capture'] = codes;
    }
  }
  let rms = '', rov = '';
  // Human-readable status text for screen readers — paired with the dot
  // shape so red-green colour-blind users (and SR users in general) get
  // unambiguous state on each service row.
  const dotLabel = { active: 'active', failed: 'failed', inactive: 'inactive', unknown: 'state unknown' };
  for (const [svc, st] of Object.entries(services)) {
    const cls  = ['active','failed','inactive'].includes(st) ? st : 'unknown';
    const code = camLookup[svc] || null;
    const tag  = code ? `<span class="svc-cam">${escHtml(code)}</span>` : '';
    const name = rovimenMap[svc] || svc;
    const nameE = escHtml(name);
    const restartBtn = state.IS_ADMIN && rovimenMap[svc]
      ? `<button class="svc-restart-btn" onclick="event.stopPropagation();window.restartService('${escHtml(state.activeStation)}','${escHtml(svc)}',this)" title="Restart ${nameE}" aria-label="Restart ${nameE}">&#8635;</button>`
      : '';
    const b = `<div class="svc-item s-${cls}">
      <div class="dot dot-${cls}" role="img" aria-label="${nameE} ${dotLabel[cls]}"></div>
      <span class="svc-name">${nameE}</span>${tag}${restartBtn}
    </div>`;
    if (svc.startsWith('rms-cam') || svc === 'rms-capture') rms += b; else rov += b;
  }
  return `<div class="card"><div class="card-title">Services</div>
    <div class="svc-group-label">RMS</div><div class="svc-grid">${rms}</div>
    <div class="svc-group-label">ROVIMEN</div><div class="svc-grid">${rov}</div>
  </div>`;
}

/* ─────────────────────────────────────────
   Storage card
───────────────────────────────────────── */
const DISK_PALETTE = ['var(--blue)','var(--yellow)','var(--purple)','var(--green)'];

function mkStorage(storage, disk, extraDisks) {
  // Build device → accent colour map (main disk first, then extras)
  const devColor = {};
  if (disk?.device) devColor[disk.device] = DISK_PALETTE[0];
  (extraDisks || []).forEach((ed, i) => {
    if (!devColor[ed.source]) devColor[ed.source] = DISK_PALETTE[(i + 1) % DISK_PALETTE.length];
  });
  const accentFor = dev => devColor[dev] || 'var(--muted)';

  // If the station has no usable storage telemetry at all (psutil missing on
  // the station-side probe), an empty grid of "—" reads as "no storage" not
  // "metrics unavailable". Surface that explicitly.
  const haveStorage = storage && Object.values(storage).some(v => v && v.bytes != null);
  const haveDisk    = disk && (disk.used_mb != null || disk.total_mb != null);
  const haveExtras  = Array.isArray(extraDisks) && extraDisks.some(ed => ed && (ed.used_mb != null || ed.total_mb != null));
  if (!haveStorage && !haveDisk && !haveExtras) {
    return `<div class="card"><div class="card-title">Storage</div>
      <div style="padding:10px 12px;border-left:3px solid var(--yellow);background:rgba(245,166,35,0.08);color:var(--text,#e6e6e6);border-radius:4px">
        Disk metrics unavailable on this station.
        <div style="color:var(--muted);font-size:12px;margin-top:4px">
          The station-side probe couldn't read filesystem usage
          (psutil missing or storage probe failing). Other telemetry is still being collected.
        </div>
      </div>
    </div>`;
  }

  const rows_def = [
    {key:'color_capture',   label:'color_capture'},
    {key:'color_timelapse', label:'color_timelapse'},
    {key:'rms',             label:'RMS (CapturedFiles + Archived)'},
  ];
  let rows = '';
  for (const {key, label} of rows_def) {
    const d = storage?.[key];
    if (!d) {
      rows += `<tr><td class="c-folder">${label}</td><td colspan="4" style="color:var(--muted)">—</td></tr>`;
      continue;
    }
    const accent = accentFor(d.device);
    rows += `<tr>
      <td class="c-folder" style="border-left:3px solid ${accent};padding-left:9px;color:${accent}">${label}</td>
      <td class="c-size" style="color:${accent}">${fmtBytes(d.bytes)}</td>
      <td style="color:var(--muted)">${fmtDate(d.oldest)}</td>
      <td style="color:var(--muted)">${fmtDate(d.newest)}</td>
      <td style="color:var(--muted)">${d.days ?? '—'}</td>
    </tr>`;
  }
  // Other row — only on main disk
  if (disk?.other_mb != null) {
    const accent = accentFor(disk.device);
    const otherBytes = disk.other_mb * 1024 * 1024;
    rows += `<tr>
      <td class="c-folder" style="border-left:3px solid ${accent};padding-left:9px;color:var(--muted);font-weight:400">Other (OS + misc)</td>
      <td class="c-size" style="color:var(--muted)">${fmtBytes(otherBytes)}</td>
      <td colspan="3"></td>
    </tr>`;
  }

  // Disk meta row — guard every numeric field. A station can return `disk`
  // as an object with only `device` and null usage (e.g. offline-but-cached
  // shells, or a probe that crashed mid-collect), and the old template
  // rendered "NaN GB / NaN GB · null%" in those cases.
  const fmtDiskRow = (d, label) => {
    if (!d) return '';
    const used   = d.used_mb  != null ? (d.used_mb  / 1024).toFixed(1) : '—';
    const total  = d.total_mb != null ? (d.total_mb / 1024).toFixed(1) : '—';
    const pctStr = d.pct != null ? d.pct + '%' : '—';
    const usage  = barColor(d.pct);
    const accent = accentFor(d.device || d.source);
    const fillPct = d.pct != null ? d.pct : 0;
    return `<div class="disk-section" style="border-left:3px solid ${accent};padding-left:9px">
      <div class="disk-meta">
        <span style="color:${accent};font-weight:700">${label}</span>
        <span style="color:${usage}">${used} GB / ${total} GB &nbsp;·&nbsp; ${pctStr}</span>
      </div>
      <div class="disk-track"><div class="disk-fill" style="width:${fillPct}%;background:${usage}"></div></div>
    </div>`;
  };
  let diskHtml = '';
  if (disk) diskHtml += fmtDiskRow(disk, disk.device || '/home/gmn');
  for (const ed of (extraDisks || [])) {
    diskHtml += fmtDiskRow(ed, `${ed.source} → ${ed.mount}`);
  }
  return `<div class="card"><div class="card-title">Storage</div>
    <table class="stbl"><thead><tr>
      <th>Folder</th><th>Size</th><th>Oldest</th><th>Newest</th><th>Days</th>
    </tr></thead><tbody>${rows}</tbody></table>${diskHtml}
  </div>`;
}

/* ─────────────────────────────────────────
   Camera windows
───────────────────────────────────────── */
function mkCameraWindows(host, cameras, { collapsible = false } = {}) {
  if (!_canUseLiveFeed(host)) return '';  // live feed requires admin or own-station host
  const cams = cameras || state.STATIONS_META[host]?.cameras || [];
  if (!cams.length) return '';
  const cells = cams.map(cam => {
    const key     = `${host}_${cam.code}`;
    const isLive  = !!liveActive[key];
    const btnTxt  = isLive ? '&#9632; Stop'  : '&#9654; Start Live';
    const btnCls  = isLive ? 'cam-live-btn stop' : 'cam-live-btn';
    const bodyHtml = isLive
      ? _liveBodyHTML(host, cam.code, key)
      : `<div class="cam-placeholder">No live feed</div>`;
    return `<div class="cam-window">
      <div class="cam-window-header">
        <span class="cam-code">${escHtml(cam.code)}</span>
        <span class="cam-ip">${escHtml(cam.cam_ip)}</span>
        <button class="${btnCls}" onclick="toggleCamLive('${escHtml(host)}','${escHtml(cam.code)}',this)">${btnTxt}</button>
        <button class="cam-expand-btn" onclick="expandCam('${escHtml(host)}','${escHtml(cam.code)}','${escHtml(cam.cam_ip)}')"
                title="Expand">&#x2922;</button>
      </div>
      <div class="cam-body" id="cam-body-${key}">${bodyHtml}</div>
    </div>`;
  }).join('');
  const grid = `<div class="cam-grid">${cells}</div>`;
  if (!collapsible) {
    return `<div class="card"><div class="card-title">Cameras</div>${grid}</div>`;
  }
  const panelId = `cam-collapse-${host}`;
  return `<div class="card">
    <div class="card-title" style="display:flex;align-items:center;gap:8px">
      Cameras
      <button class="svc-expand-btn" aria-expanded="false"
              onclick="toggleCamPanel('${panelId}',this)">Show ▾</button>
    </div>
    <div id="${panelId}" class="svc-panel">
      <div class="svc-panel-inner" style="display:block;padding:14px 16px">${grid}</div>
    </div>
  </div>`;
}

function mkRebootCard(host) {
  return `<div class="card">
    <div class="card-title">Station</div>
    <button class="settings-save-btn" style="background:var(--red);font-size:12px;padding:6px 18px"
            onclick="rebootStation('${host}', this)">&#8635; Reboot station</button>
  </div>`;
}

function rebootStation(host, btn) {
  askConfirm(
    `Reboot ${host}? The station will be offline for ~1 minute.`,
    async () => {
      const orig = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Rebooting…';
      try {
        const r = await fetch(`/api/reboot/${host}`, { method: 'POST' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        btn.textContent = '✓ Reboot sent';
        setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 10000);
      } catch(e) {
        btn.textContent = '✗ ' + e.message;
        setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 4000);
      }
    },
    { confirmLabel: 'Confirm reboot', title: 'Reboot station' }
  );
}


function liveStreamLoaded(key) {
  const el = document.getElementById(`live-status-${key}`);
  if (el) el.style.display = 'none';
  _liveRetries[key] = 0;
}

function _pollLiveImg(key) {
  const img = document.getElementById(`live-img-${key}`);
  if (!img || !liveActive[key]) return;
  if (img.naturalWidth > 0) { liveStreamLoaded(key); return; }
  img.decode()
    .then(() => { if (liveActive[key]) liveStreamLoaded(key); })
    .catch(() => {});
  setTimeout(() => _pollLiveImg(key), 400);
}

function liveStreamError(img, host, camCode, key) {
  if (!liveActive[key]) return;
  _liveRetries[key] = (_liveRetries[key] || 0) + 1;
  img.style.display = 'none';
  const el = document.getElementById(`live-status-${key}`);

  if (_liveRetries[key] > 3) {
    if (el) el.textContent = 'Stream unavailable';
    fetch(`/stream/${host}/${camCode}`, { signal: AbortSignal.timeout(12000) })
      .then(async r => {
        if (r.ok) return null;
        try { return await r.json(); } catch { return null; }
      })
      .then(data => {
        if (!liveActive[key] || !el) return;
        if (data && (data.detail || data.error))
          el.textContent = data.detail || data.error;
        el.classList.add('error');
      })
      .catch(() => { if (el) el.classList.add('error'); });
    return;
  }

  if (el) {
    el.textContent = `Connecting… (attempt ${_liveRetries[key] + 1}/4)`;
    el.style.display = '';
  }
  setTimeout(() => {
    if (!liveActive[key]) return;
    img.style.display = 'block';
    img.src = `/stream/${host}/${camCode}?t=${Date.now()}`;
  }, 5000);
}

function _isRmsActive(host) {
  // "RMS is running" = any rms-cam*.service or rms-capture.service reported
  // active in the latest /api/status payload. Necessary signal that live view
  // could interfere; not a perfect "actively capturing right now" check (the
  // services stay active 24/7), but the cost of an unnecessary 60 s cap is
  // small and the cost of frame-drops during a real capture night is high.
  const svcs = (state.statusData[host] || {}).services || {};
  return Object.entries(svcs).some(([k, v]) =>
    (k.startsWith('rms-cam') || k === 'rms-capture') && v === 'active'
  );
}

function _clearLiveCountdown(key) {
  const st = liveAutoStop[key];
  if (!st) return;
  if (st.timeoutId)  clearTimeout(st.timeoutId);
  if (st.intervalId) clearInterval(st.intervalId);
  delete liveAutoStop[key];
}

function _startLiveCountdown(host, camCode) {
  const key = `${host}_${camCode}`;
  _clearLiveCountdown(key);
  const deadline = Date.now() + LIVE_VIEW_MAX_S * 1000;
  const tick = () => {
    const el = document.getElementById(`live-cd-${key}`);
    if (!el) { _clearLiveCountdown(key); return; }
    const sLeft = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    el.textContent = sLeft;
  };
  const timeoutId = setTimeout(() => {
    if (!liveActive[key]) return;
    const cell = document.getElementById(`cam-body-${key}`)?.closest('.cam-window');
    const btn  = cell?.querySelector('.cam-live-btn');
    toggleCamLive(host, camCode, btn);
  }, LIVE_VIEW_MAX_S * 1000);
  const intervalId = setInterval(tick, 1000);
  liveAutoStop[key] = { deadline, timeoutId, intervalId };
  tick();
}

function _liveBodyHTML(host, camCode, key) {
  const warn = _isRmsActive(host);
  const overlay = warn ? `
    <div class="cam-live-warn" style="position:absolute;left:0;right:0;bottom:0;
         background:linear-gradient(transparent,rgba(0,0,0,.78));color:#fff;
         padding:18px 10px 6px;font-size:11px;line-height:1.35;
         display:flex;justify-content:space-between;align-items:flex-end;gap:8px;
         pointer-events:none">
      <span><b style="color:var(--yellow,#ffd166)">&#9888; RMS is capturing</b><br>
            <span style="opacity:.85">Live view may cause frame drops</span></span>
      <span style="white-space:nowrap;font-variant-numeric:tabular-nums">
        Auto-stop in <b id="live-cd-${key}">${LIVE_VIEW_MAX_S}</b>s
      </span>
    </div>` : '';
  _liveRetries[key] = 0;
  setTimeout(() => _pollLiveImg(key), 800);
  return `<div style="position:relative;width:100%;height:100%">
    <img id="live-img-${key}" src="/stream/${host}/${camCode}"
         style="width:100%;height:100%;object-fit:cover;display:block"
         onload="liveStreamLoaded('${key}')"
         onerror="liveStreamError(this,'${host}','${camCode}','${key}')"
         alt="${camCode}" decoding="async">
    <div class="live-status" id="live-status-${key}">Connecting&hellip;</div>
    ${overlay}
  </div>`;
}

function toggleCamLive(host, camCode, btn) {
  const key = `${host}_${camCode}`;
  if (liveActive[key]) {
    const img = document.getElementById(`live-img-${key}`);
    if (img) img.src = '';
    liveActive[key] = false;
    delete _liveRetries[key];
    _clearLiveCountdown(key);
  } else {
    liveActive[key] = true;
  }
  const live = liveActive[key];

  // Update button in-place
  if (btn) {
    btn.innerHTML = live ? '&#9632; Stop' : '&#9654; Start Live';
    btn.className = live ? 'cam-live-btn stop' : 'cam-live-btn';
  }

  // Update just the camera body div — works in any container (collapsible or sub-tab)
  const body = document.getElementById(`cam-body-${key}`);
  if (body) {
    body.innerHTML = live
      ? _liveBodyHTML(host, camCode, key)
      : `<div class="cam-placeholder">No live feed</div>`;
    if (live) {
      const img = document.getElementById(`live-img-${key}`);
      if (img) img.addEventListener('load', () => liveStreamLoaded(key));
    }
  }

  // Start the auto-stop countdown only when RMS is actively running. During
  // daytime / RMS-stopped state the live feed is harmless — no cap.
  if (live && _isRmsActive(host)) _startLiveCountdown(host, camCode);
}

/* ─────────────────────────────────────────
   Modal accessibility — shared helpers
   ─────────────────────────────────────────
   _modalOpen / _modalClose / _MODAL_TABBABLE_SEL / _modalTabbables /
   _modalTrapHandler / _closeAllOpenModals all live in dashboard-common.js
   now (which loads first on every page). All non-module <script> tags
   share a single global-script lexical environment, so calls to those
   helpers from this bundle resolve at runtime as plain identifiers; the
   window._modalOpen / window._modalClose mirrors are there for inline
   <script> blocks in templates that prefer the explicit global form.
───────────────────────────────────────── */

function expandCam(host, camCode, camIp) {
  const modal = document.getElementById('cam-modal');
  const img   = document.getElementById('cam-modal-img');
  const title = document.getElementById('cam-modal-title');
  title.textContent = `${camCode}  ·  ${camIp}  ·  LIVE`;
  const dlBtn = document.getElementById('cam-modal-dl');
  if (dlBtn) dlBtn.style.display = 'none';
  // The shared cam-modal-img is reused across live feed + RMS plot modes;
  // make sure any leftover rotation from a previous plot view is cleared
  // before showing the live feed.
  img.classList.remove('thumb-rotated');
  img.src = `/stream/${host}/${camCode}`;
  _modalOpen(modal);
}

function closeCamModal(e) {
  if (e && e.target !== document.getElementById('cam-modal')) return;
  if (window._pz.justDragged) return;
  const modal = document.getElementById('cam-modal');
  const img   = document.getElementById('cam-modal-img');
  img.src = '';
  img.classList.remove('thumb-rotated');
  if (modal.dataset.plotZoom) {
    img.removeEventListener('wheel',     window._pzWheel);
    img.removeEventListener('mousedown', window._pzDown);
    document.removeEventListener('mousemove', window._pzMove);
    document.removeEventListener('mouseup',   window._pzUp);
    img.style.transform = '';
    img.style.transition = '';
    img.style.cursor = '';
    delete modal.dataset.plotZoom;
    modal.classList.remove('plot-mode');
  }
  _modalClose(modal);
}

function stackOpenModal(station, camera, date, stackFile, time, idx = -1, imgSrc = null) {
  window._stackCurrentIdx = idx;
  window._stackModalCtx   = {station, camera, date};
  const url = imgSrc || `/fullstack/${station}/${camera}/${date}/${encodeURIComponent(stackFile)}`;
  document.getElementById('stack-modal-title').textContent = `${camera}  ${time} UTC`;
  document.getElementById('stack-modal-img').src = url;
  document.getElementById('stack-modal-dl').href = url + (imgSrc ? '' : '?download=1');
  document.getElementById('stack-modal-dl').download = stackFile;
  _modalOpen(document.getElementById('stack-modal'));
}

function stackNavModal(delta) {
  if (!window._vdbVisibleChunks.length || !window._stackModalCtx) return;
  const arrayDir = (window.vdbSortNewest ? -delta : delta) > 0 ? 1 : -1;
  let next = window._stackCurrentIdx + arrayDir;
  while (next >= 0 && next < window._vdbVisibleChunks.length) {
    if (window._vdbVisibleChunks[next].stack) break;
    next += arrayDir;
  }
  if (next < 0 || next >= window._vdbVisibleChunks.length) return;
  const c = window._vdbVisibleChunks[next];
  const {station, camera, date} = window._stackModalCtx;
  const archiveImgSrc = c.source === 'archive'
    ? `/api/archive/file/${camera}/${date}/${c.stack_subdir || 'meteors'}/${encodeURIComponent(c.stack)}`
    : null;
  stackOpenModal(station, camera, date, c.stack, c.time, next, archiveImgSrc);
}

function stackCloseModal(e) {
  if (e && e.target !== document.getElementById('stack-modal')) return;
  document.getElementById('stack-modal-img').src = '';
  _modalClose(document.getElementById('stack-modal'));
}

/* ─────────────────────────────────────────
   Timelapses
───────────────────────────────────────── */
function mkTimelapses(host) {
  if (!canAccessTab('timelapse', host)) return window._accessBanner('Timelapses');
  const tl = state.tlData[host];
  if (!tl) return `<div class="card"><div class="card-title">Timelapses</div>
    <div class="loading" id="tl-loading-${host}">Loading…</div></div>`;

  const cams = Object.keys(tl);
  if (!cams.length) return `<div class="card"><div class="card-title">Timelapses</div>
    <div class="no-data">No timelapses available</div></div>`;

  // Build union of all dates
  const dateSet = new Set();
  for (const entries of Object.values(tl)) entries.forEach(e => dateSet.add(e.date));
  const dates = [...dateSet].sort().reverse();

  const toDisplay = d => `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`;
  const firstDate = dates[0];
  const tlVideoUrl  = (cam, e) => e.source === 'archive'
    ? `/api/archive/file/${cam}/${e.date}/timelapse/${e.filename}`
    : `/timelapse/${host}/${cam}/${e.date}/${e.filename}`;
  const tlStackUrl  = (cam, e) => e.night_stack
    ? (e.source === 'archive'
        ? `/api/archive/file/${cam}/${e.date}/timelapse/${e.night_stack}`
        : `/night_stack/${host}/${cam}/${e.date}/${e.night_stack}`)
    : '';
  const tlDlUrl     = (cam, e) => e.source === 'archive'
    ? `/api/archive/file/${cam}/${e.date}/timelapse/${e.filename}`
    : `/timelapse_download/${host}/${cam}/${e.date}/${e.filename}`;
  const players = cams.map(cam => {
    const entry    = tl[cam].find(e => e.date === firstDate) || tl[cam][0];
    const src      = entry ? tlVideoUrl(cam, entry) : '';
    const stackSrc = entry ? tlStackUrl(cam, entry) : '';
    const dlHref   = entry ? tlDlUrl(cam, entry) : '';
    const camAz = window._vdbAzimuths[cam];
    const camPointing = camAz != null ? `<span class="tl-cam-pointing">${window._vdbAzCompass?.(camAz) || ''} · ${Math.round(camAz)}°</span>` : '';
    return `<div class="tl-cam-block">
      <div class="tl-cam-header">
        <span>${cam}${camPointing}</span>
        <a id="tl-dl-${host}-${cam}" class="tl-dl-btn"
           href="${dlHref}" download ${dlHref ? '' : 'hidden'}>&#8595; Download</a>
      </div>
      <video id="tl-${host}-${cam}" class="tl-video" src="${src}"
             ${stackSrc ? `poster="${stackSrc}"` : ''}
             controls preload="metadata"></video>
    </div>`;
  }).join('');

  const tlNightOptions = dates.map(d => `<option value="${d}"${d === firstDate ? ' selected' : ''}>${toDisplay(d)}</option>`).join('');
  return `<div class="card"><div class="card-title">Timelapses</div>
    <div class="tl-controls">
      <label>Night</label>
      <select class="tl-night-sel" onchange="onTlNightChange('${host}', this.value)">
        ${tlNightOptions}
      </select>
    </div>
    <div class="tl-grid">${players}</div>
  </div>`;
}

function onTlNightChange(host, date) {
  const tl = state.tlData[host];
  if (!tl) return;
  for (const [cam, entries] of Object.entries(tl)) {
    const entry = entries.find(e => e.date === date) || null;
    const vid   = document.getElementById(`tl-${host}-${cam}`);
    const dlBtn = document.getElementById(`tl-dl-${host}-${cam}`);
    if (vid) {
      if (entry) {
        const src = entry.source === 'archive'
          ? `/api/archive/file/${cam}/${entry.date}/timelapse/${entry.filename}`
          : `/timelapse/${host}/${cam}/${entry.date}/${entry.filename}`;
        const poster = entry.night_stack
          ? (entry.source === 'archive'
              ? `/api/archive/file/${cam}/${entry.date}/timelapse/${entry.night_stack}`
              : `/night_stack/${host}/${cam}/${entry.date}/${entry.night_stack}`)
          : '';
        vid.poster = poster;
        vid.src    = src;
        vid.load();
      } else {
        vid.poster = '';
        vid.src    = '';
      }
    }
    if (dlBtn) {
      if (entry) {
        dlBtn.href   = entry.source === 'archive'
          ? `/api/archive/file/${cam}/${entry.date}/timelapse/${entry.filename}`
          : `/timelapse_download/${host}/${cam}/${entry.date}/${entry.filename}`;
        dlBtn.hidden = false;
      } else {
        dlBtn.href   = '';
        dlBtn.hidden = true;
      }
    }
  }
}

function renderTimelapses(host) {
  // tl-container is mounted by renderFinalDataProducts on the FDP pane.
  // Guard for the case where the user is on a different tab and the
  // container hasn't been created yet (background fetch still completes).
  const el = document.getElementById('tl-container');
  if (!el) return;
  el.innerHTML = mkTimelapses(host);
}


/* ─────────────────────────────────────────
   Final Data Products tab — nightly outputs only:
   timelapses, nightly sky-dome MP4, per-camera RMS plots, GMN cross-link.
───────────────────────────────────────── */
function mkFdpDomeTimelapse(host) {
  return `<div class="card" id="fdp-dome-tl-card-${host}" style="padding:10px 14px;margin-bottom:12px">
    <div class="card-title" style="margin-bottom:8px">
      Nightly sky dome
      <span style="font-size:10px;color:var(--muted);font-weight:400;margin-left:8px">
        one frame per minute, stitched after sunrise
      </span>
    </div>
    <div id="fdp-dome-tl-body-${host}" style="font-size:12px;color:var(--muted)">Loading...</div>
  </div>`;
}

function mkFdpRmsNightPlotsSkeleton(host) {
  return `<div class="card" id="fdp-rms-plots-${host}" style="padding:10px 14px;margin-bottom:12px">
    <div class="card-title" style="margin-bottom:8px">RMS night plots</div>
    <div style="font-size:12px;color:var(--muted)">Loading RMS night plots...</div>
  </div>`;
}

/* ─────────────────────────────────────────
   Video DB tab — live BW sky-dome card.
   Separate variant (?variant=ff_max) so it always shows tonight's newest
   FF maxpixel coverage, regardless of which date the user picked in the
   VDB filters below. Refreshed every 60 s while the tab is active.
───────────────────────────────────────── */
function mkVdbSkyDome(host) {
  const bust = Date.now();
  return `<div class="card sky-dome-card" id="vdb-sky-dome-card-${host}" style="padding:10px 14px;margin-bottom:12px">
    <div class="card-title" style="margin-bottom:6px">
      Sky right now
      <span style="font-size:10px;color:var(--muted);font-weight:400;margin-left:8px">
        live - refreshes every ~90 seconds - always tonight, regardless of date selector below
      </span>
    </div>
    <div class="sky-dome-wrap">
      <img class="sky-dome-img" id="vdb-sky-dome-img-${host}"
           src="/api/sky_dome/${encodeURIComponent(host)}.png?variant=ff_max&t=${bust}"
           alt="Sky right now for ${host}"
           loading="lazy" decoding="async"
           onerror="vdbSkyDomeOnError('${host}')"
           onclick="vdbSkyDomeOpenModal('${host}')">
    </div>
    ${domeFreshLabelHtml(host, 'vdb')}
  </div>`;
}

function vdbMountSkyDome(host) {
  const pane = document.getElementById('pane-videodb');
  if (!pane) return;
  pane.querySelectorAll('[id^="vdb-sky-dome-card-"]').forEach(node => {
    if (node.id !== `vdb-sky-dome-card-${host}`) node.remove();
  });
  if (document.getElementById(`vdb-sky-dome-card-${host}`)) return;
  const wrap = document.createElement('div');
  wrap.innerHTML = mkVdbSkyDome(host);
  pane.insertBefore(wrap.firstElementChild, pane.firstElementChild);
}

function vdbRefreshSkyDome(host) {
  const img = document.getElementById(`vdb-sky-dome-img-${host}`);
  if (!img) return;
  const card = document.getElementById(`vdb-sky-dome-card-${host}`);
  if (card) card.style.display = '';
  img.src = `/api/sky_dome/${encodeURIComponent(host)}.png?variant=ff_max&t=${Date.now()}`;
  domeFetchFreshness(host);
}

/* ─────────────────────────────────────────
   Sky-dome freshness indicator — shared between the Video DB "Sky right
   now" card and the RMS / Detection "Sky coverage" card. Reads the
   server-side sidecar JSON to learn the newest FF capture time across
   cameras the last render used; colours the label orange > 5 min,
   red > 10 min behind wall-clock. "Now" updates every second.
───────────────────────────────────────── */
function domeFreshLabelHtml(host, side) {
  // side === 'vdb' or 'rms' — two surfaces, identical layout, distinct IDs
  // so we can update whichever is currently mounted.
  return `<div class="sky-dome-fresh" id="${side}-sky-dome-fresh-${host}"
              style="font-size:11px;color:var(--muted);margin-top:8px;display:flex;gap:18px;justify-content:center;flex-wrap:wrap;font-family:'SF Mono',Consolas,monospace">
    <span><span style="color:var(--muted);margin-right:6px">Image:</span><span class="sky-dome-fresh-img">&mdash;</span></span>
    <span style="display:none"><span class="sky-dome-fresh-lag"></span></span>
    <span><span style="color:var(--muted);margin-right:6px">Now:</span><span class="sky-dome-fresh-now">&mdash;</span></span>
  </div>`;
}

export function _domeFmtUTC(ms) {
  return new Date(ms).toISOString().slice(11, 19) + ' UTC';
}

/* Compact "time since" formatter for the live-image lag indicator.
   < 60 s   -> "just now"
   < 60 min -> "12m ago"
   < 24 h   -> "3h 14m ago"
   < 30 d   -> "2d 5h ago"
   >= 30 d  -> "47d ago" (no further granularity needed at that scale) */
export function _domeFmtRelative(ageMs) {
  if (!Number.isFinite(ageMs) || ageMs < 0) return '';
  const s = Math.floor(ageMs / 1000);
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  const mRem = m % 60;
  if (h < 24) return mRem ? `${h}h ${mRem}m ago` : `${h}h ago`;
  const d = Math.floor(h / 24);
  const hRem = h % 24;
  if (d < 30) return hRem ? `${d}d ${hRem}h ago` : `${d}d ago`;
  return `${d}d ago`;
}

async function domeFetchFreshness(host) {
  try {
    const r = await fetch(`/api/sky_dome_meta/${encodeURIComponent(host)}?variant=ff_max`);
    if (!r.ok) { _domeFreshness[host] = null; }
    else {
      const d = await r.json();
      _domeFreshness[host] = (d && d.capture_time)
        ? { captureMs: new Date(d.capture_time).getTime() }
        : null;
    }
  } catch (e) { _domeFreshness[host] = null; }
  domeUpdateFreshnessLabels(host);
}

function domeUpdateFreshnessLabels(host) {
  const now = Date.now();
  const state = _domeFreshness[host];
  let imgText, imgColor, lagText = '';
  const haveCapture = !!(state && Number.isFinite(state.captureMs));
  if (haveCapture) {
    imgText = _domeFmtUTC(state.captureMs);
    const ageMs = Math.max(0, now - state.captureMs);
    const ageS = ageMs / 1000;
    if (ageS > 600) imgColor = '#f85149';        // > 10 min behind: red
    else if (ageS > 300) imgColor = '#f5a623';   // > 5 min behind: orange
    else imgColor = 'var(--text,#e6e6e6)';
    lagText = _domeFmtRelative(ageMs);
  } else {
    imgText = 'unknown';
    imgColor = 'var(--muted)';
  }
  const nowText = _domeFmtUTC(now);
  for (const side of ['vdb', 'rms']) {
    const wrap = document.getElementById(`${side}-sky-dome-fresh-${host}`);
    if (!wrap) continue;
    const imgEl = wrap.querySelector('.sky-dome-fresh-img');
    const nowEl = wrap.querySelector('.sky-dome-fresh-now');
    const lagEl = wrap.querySelector('.sky-dome-fresh-lag');
    if (imgEl) { imgEl.textContent = imgText; imgEl.style.color = imgColor; }
    if (nowEl) {
      nowEl.textContent = nowText;
      // Hide the "Now: HH:MM" segment when we have no capture time — showing
      // a live-ticking clock next to "Image: unknown" reads as "data refresh
      // is working" which is misleading.
      const nowParent = nowEl.parentElement;
      if (nowParent) nowParent.style.display = haveCapture ? '' : 'none';
    }
    if (lagEl) {
      lagEl.textContent = lagText;
      lagEl.style.color = imgColor;
      // Hide entirely (incl. the leading "(") when we have nothing to say —
      // avoids a stray "()" on the unknown-image rendering.
      const parent = lagEl.parentElement;
      if (parent) parent.style.display = lagText ? '' : 'none';
    }
  }
}

function domeEnsureFreshTicker() {
  if (_domeFreshTicker) clearInterval(_domeFreshTicker);
  _domeFreshTicker = setInterval(() => {
    if (state.activeStation) domeUpdateFreshnessLabels(state.activeStation);
  }, 1000);
}

// Browsers throttle setInterval in background tabs (sometimes to a single
// fire per minute, often much slower). Without this, a user who tabs away
// for 5 minutes returns to a sky-dome that is several minutes stale and
// has to wait up to another 60 s for the next interval tick. Fire a
// catch-up refresh + freshness fetch the instant the tab becomes visible
// again while we're on the Video DB pane.
if (typeof document !== 'undefined' && !window._domeVisHookInstalled) {
  window._domeVisHookInstalled = true;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (state.activeTab !== 'videodb' || !state.activeStation) return;
    vdbRefreshSkyDome(state.activeStation);
  });
}

function vdbSkyDomeOnError(host) {
  const card = document.getElementById(`vdb-sky-dome-card-${host}`);
  if (card) card.style.display = 'none';
}

function vdbSkyDomeOpenModal(host) {
  const img = document.getElementById(`vdb-sky-dome-img-${host}`);
  if (!img || !img.src) return;
  const modal = document.getElementById('cam-modal');
  if (modal && typeof rmsPlotExpand === 'function') {
    rmsPlotExpand(modal, img.src, `${host} — sky right now`);
    return;
  }
  window.open(img.src, '_blank', 'noopener');
}

function mkFdpGmnPlotsPointer(host) {
  // GMN plots (radiants, observing periods, calibration fits) are already
  // rendered per-night inside the RMS / Detection tab — duplicating the
  // full grid here would double the request volume on every tab switch.
  // Surface the cross-link instead so operators know where to find them.
  return `<div class="card" style="padding:10px 14px;margin-bottom:12px">
    <div class="card-title" style="margin-bottom:8px">GMN processing plots</div>
    <div style="font-size:12px;color:var(--muted);line-height:1.5">
      Per-night radiants, observing-period charts, calibration fits and FF
      stack mosaics are produced by RMS post-processing and shown under
      <a href="#" onclick="switchTab('rms', document.getElementById('tab-btn-rms'));return false;"
         style="color:var(--blue);text-decoration:none">
        RMS / Detection &rarr; Processing plots
      </a> on each night.
    </div>
  </div>`;
}

function renderFinalDataProducts(host) {
  const el = document.getElementById('pane-fdp');
  if (!el) return;
  if (!canAccessTab('fdp', host)) {
    el.innerHTML = window._accessBanner('Final Data Products');
    return;
  }
  el.innerHTML = [
    `<div id="tl-container"></div>`,
    mkFdpDomeTimelapse(host),
    mkFdpColorMeteorStackSkeleton(host),
    `<div id="fdp-rms-plots-container">${mkFdpRmsNightPlotsSkeleton(host)}</div>`,
    // GMN plots already rendered on RMS / Detection tab — no need to duplicate here.
  ].join('');
  if (state.tlData[host]) renderTimelapses(host);
  fdpLoadDomeTimelapseList(host);
  fdpLoadColorMeteorStacks(host);
  fdpLoadRmsNightPlots(host);
}

/* Fetch the list of available nightly dome MP4s (newest first) and mount
   a <video> for the newest date plus a small dropdown when more than one
   night is available. Branch 3 ships the MP4 builder; until then — or
   whenever no MP4 has been built for this station yet — paint a placeholder. */
let _fdpDomeDates = {};   // host -> ['YYYYMMDD', ...] (newest first)

async function fdpLoadDomeTimelapseList(host) {
  const body = document.getElementById(`fdp-dome-tl-body-${host}`);
  if (!body) return;
  const placeholder = `<div style="color:var(--muted);font-size:12px">Tonight's dome timelapse will appear after dawn.</div>`;
  let dates = [];
  try {
    const r = await fetch(`/api/sky_dome_timelapse/${encodeURIComponent(host)}/dates`);
    if (!r.ok) { body.innerHTML = placeholder; return; }
    const j = await r.json();
    dates = Array.isArray(j && j.dates) ? j.dates : [];
  } catch (e) {
    body.innerHTML = placeholder;
    return;
  }
  if (!dates.length) { body.innerHTML = placeholder; return; }
  _fdpDomeDates[host] = dates;
  fdpRenderDomeTimelapse(host, dates[0]);
}

function fdpRenderDomeTimelapse(host, selectedDate) {
  const body = document.getElementById(`fdp-dome-tl-body-${host}`);
  if (!body) return;
  const dates = _fdpDomeDates[host] || [selectedDate];
  const opts = dates.map(d =>
    `<option value="${d}"${d === selectedDate ? ' selected' : ''}>${d}</option>`
  ).join('');
  const picker = dates.length > 1
    ? `<div style="text-align:right;margin-bottom:6px">
         <label style="font-size:11px;color:var(--muted);margin-right:6px">Night</label>
         <select onchange="fdpRenderDomeTimelapse('${host}', this.value)"
                 style="background:var(--bg);border:1px solid var(--border);color:var(--text);
                        font-family:inherit;font-size:12px;padding:3px 8px;border-radius:4px">
           ${opts}
         </select>
       </div>`
    : '';
  const url = `/api/sky_dome_timelapse/${encodeURIComponent(host)}/${encodeURIComponent(selectedDate)}.mp4`;
  body.innerHTML = `${picker}
    <video controls preload="metadata"
           style="width:100%;max-width:800px;display:block;margin:0 auto;background:#000"
           src="${url}"></video>`;
}

/* ─────────────────────────────────────────
   FDP: per-camera color meteor stacks
   One image per camera for the most recent night that has a color meteor
   stack. Uses the existing /api/rms/plots proxy which injects the
   __color_meteor_stack__.webp synthetic entry when the station has one.
───────────────────────────────────────── */
function mkFdpColorMeteorStackSkeleton(host) {
  return `<div class="card" id="fdp-color-meteor-stack-${host}" style="padding:10px 14px;margin-bottom:12px">
    <div class="card-title" style="margin-bottom:8px">Color meteor stacks</div>
    <div style="font-size:12px;color:var(--muted)">Loading color meteor stacks...</div>
  </div>`;
}

async function fdpLoadColorMeteorStacks(host) {
  const card = document.getElementById(`fdp-color-meteor-stack-${host}`);
  if (!card) return;
  const cameras = state.VDB_CAMERAS[host] || [];
  if (!cameras.length) {
    card.innerHTML = `<div class="card-title" style="margin-bottom:8px">Color meteor stacks</div>
      <div style="font-size:12px;color:var(--muted)">No cameras configured for this station.</div>`;
    return;
  }
  const COLOR_STACK_FN = '__color_meteor_stack__.webp';
  let anyFound = false;
  const sections = [];
  let newestDate = null;
  for (const cam of cameras) {
    try {
      const res = await _fdpLoadCamColorStack(host, cam, COLOR_STACK_FN);
      sections.push(res.html);
      if (res.found) anyFound = true;
      if (res.date && (!newestDate || res.date > newestDate)) newestDate = res.date;
    } catch (e) {
      sections.push(`<div class="fdp-color-stack-cam-row">
        <div class="fdp-night-plots-cam-header">${cam}</div>
        <div style="font-size:12px;color:var(--muted)">No color meteor stack available.</div>
      </div>`);
    }
  }
  if (!anyFound) {
    card.innerHTML = `<div class="card-title" style="margin-bottom:8px">Color meteor stacks</div>
      <div style="font-size:12px;color:var(--muted)">No color meteor stacks available yet. Stacks are built after dawn when meteors are detected.</div>`;
    return;
  }
  let staleLine = '';
  if (newestDate && newestDate.length === 8) {
    const y = +newestDate.slice(0, 4);
    const m = +newestDate.slice(4, 6) - 1;
    const d = +newestDate.slice(6, 8);
    const plotDate = new Date(Date.UTC(y, m, d));
    const today = new Date();
    const todayUtc = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
    const ageDays = Math.floor((todayUtc - plotDate.getTime()) / 86400000);
    const isoDate = `${newestDate.slice(0,4)}-${newestDate.slice(4,6)}-${newestDate.slice(6,8)}`;
    const stale = ageDays > 1;
    const color = stale ? '#f85149' : 'var(--muted)';
    const ageText = ageDays <= 0 ? 'last night'
                   : ageDays === 1 ? '1 day old'
                   : `${ageDays} days old`;
    staleLine = `<div style="font-size:11px;color:${color};font-weight:400;margin-top:2px">from ${isoDate} &middot; ${ageText}</div>`;
  }
  card.innerHTML = `<div class="card-title" style="margin-bottom:8px">Color meteor stacks
      <span style="font-size:10px;color:var(--muted);font-weight:400;margin-left:8px">color video frames with detected meteors &middot; click to enlarge</span>
      ${staleLine}
    </div>${sections.join('')}`;
  card.addEventListener('click', _fdpPlotTileClick);
}

async function _fdpLoadCamColorStack(host, cam, colorStackFn) {
  const apiHost = window.hostForCamera(host, cam);
  const nightsResp = await fetch(`/api/videodb/rmsnights/${apiHost}/${cam}`);
  if (!nightsResp.ok) throw new Error('nights');
  const nights = await nightsResp.json();
  if (!Array.isArray(nights) || !nights.length) {
    return { found: false, date: null, html: `<div class="fdp-color-stack-cam-row">
      <div class="fdp-night-plots-cam-header">${cam}</div>
      <div style="font-size:12px;color:var(--muted)">No color meteor stack available.</div>
    </div>` };
  }
  // Walk nights newest-first until we find one with a color meteor stack.
  for (const candidate of nights) {
    try {
      const r = await fetch(`/api/rms/plots/${apiHost}/${cam}/${candidate}`);
      if (!r.ok) continue;
      const list = await r.json();
      if (!Array.isArray(list)) continue;
      const stackEntry = list.find(p => p && p.filename === colorStackFn);
      if (!stackEntry) continue;
      const imgUrl = `/api/rms/plot_image/${apiHost}/${cam}/${candidate}/${encodeURIComponent(colorStackFn)}`;
      const label = stackEntry.label || 'Meteor stack (color)';
      return { found: true, date: candidate, html: `<div class="fdp-color-stack-cam-row">
        <div class="fdp-night-plots-cam-header">${escHtml(cam)} &middot; ${escHtml(candidate)}</div>
        <div class="fdp-color-stack-grid">
          <div class="fdp-night-plots-tile fdp-plot-tile-trigger" data-img-url="${escHtml(imgUrl)}" data-label="${escHtml(label)}">
            <img src="${escHtml(imgUrl)}" loading="lazy" decoding="async" alt="${escHtml(label)}"
                 onerror="this.closest('.fdp-night-plots-tile').style.display='none'">
            <div class="fdp-night-plots-tile-cap">${escHtml(label)}</div>
          </div>
        </div>
      </div>` };
    } catch (e) { /* try next night */ }
  }
  return { found: false, date: null, html: `<div class="fdp-color-stack-cam-row">
    <div class="fdp-night-plots-cam-header">${cam}</div>
    <div style="font-size:12px;color:var(--muted)">No color meteor stack available.</div>
  </div>` };
}

/* For each camera on this station, find the most-recent night that has
   any plots and render a thumbnail grid of well-known per-night plot
   files. Failures per camera don't abort the rest — they swap that
   section's body for an inline "no plots yet" line. */
const _FDP_RMS_PLOT_SUFFIXES = [
  '_calibration_variation.png',
  '_radiants.png',
  '_ff_density.png',
  '_meteors_count.png',
  '_velocities.png',
  '_photometric_residuals.png',
];

async function fdpLoadRmsNightPlots(host) {
  const card = document.getElementById(`fdp-rms-plots-${host}`);
  if (!card) return;
  const cameras = state.VDB_CAMERAS[host] || [];
  if (!cameras.length) {
    card.innerHTML = `<div class="card-title" style="margin-bottom:8px">RMS night plots</div>
      <div style="font-size:12px;color:var(--muted)">No cameras configured for this station.</div>`;
    return;
  }
  let anySuccess = false;
  const sections = [];
  // Track the most recent night we actually rendered plots for, across all
  // cameras. Used to surface a "from YYYY-MM-DD" staleness subline so the
  // header doesn't silently show week-old plots without context (P1-41).
  let newestDate = null;
  for (const cam of cameras) {
    try {
      const res = await _fdpLoadCamPlots(host, cam);
      sections.push(res.html);
      anySuccess = true;
      if (res.date && (!newestDate || res.date > newestDate)) newestDate = res.date;
    } catch (e) {
      sections.push(`<div class="fdp-night-plots-cam-row">
        <div class="fdp-night-plots-cam-header">${cam}</div>
        <div style="font-size:12px;color:var(--muted)">No plots available yet for this camera.</div>
      </div>`);
    }
  }
  if (!anySuccess) {
    card.innerHTML = `<div class="card-title" style="margin-bottom:8px">RMS night plots</div>
      <div style="font-size:12px;color:var(--muted)">Could not load RMS plots.</div>`;
    return;
  }
  // Build a "from YYYY-MM-DD (N days old)" subline. ymd format from the
  // station API is YYYYMMDD. Compare against today UTC; > 1 day old = red.
  let staleLine = '';
  if (newestDate && newestDate.length === 8) {
    const y = +newestDate.slice(0, 4);
    const m = +newestDate.slice(4, 6) - 1;
    const d = +newestDate.slice(6, 8);
    const plotDate = new Date(Date.UTC(y, m, d));
    const today = new Date();
    const todayUtc = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
    const ageDays = Math.floor((todayUtc - plotDate.getTime()) / 86400000);
    const isoDate = `${newestDate.slice(0,4)}-${newestDate.slice(4,6)}-${newestDate.slice(6,8)}`;
    const stale = ageDays > 1;
    const color = stale ? '#f85149' : 'var(--muted)';
    const ageText = ageDays <= 0 ? 'last night'
                   : ageDays === 1 ? '1 day old'
                   : `${ageDays} days old`;
    staleLine = `<div style="font-size:11px;color:${color};font-weight:400;margin-top:2px">from ${isoDate} &middot; ${ageText}</div>`;
  }
  card.innerHTML = `<div class="card-title" style="margin-bottom:8px">RMS night plots
      <span style="font-size:10px;color:var(--muted);font-weight:400;margin-left:8px">most recent night with content &middot; click any plot to enlarge &middot; scroll wheel to zoom</span>
      ${staleLine}
    </div>${sections.join('')}`;
  card.addEventListener('click', _fdpPlotTileClick);
}

async function _fdpLoadCamPlots(host, cam) {
  const apiHost = window.hostForCamera(host, cam);
  const nightsResp = await fetch(`/api/videodb/rmsnights/${apiHost}/${cam}`);
  if (!nightsResp.ok) throw new Error('nights');
  const nights = await nightsResp.json();
  if (!Array.isArray(nights) || !nights.length) {
    return { date: null, html: `<div class="fdp-night-plots-cam-row">
      <div class="fdp-night-plots-cam-header">${cam}</div>
      <div style="font-size:12px;color:var(--muted)">No plots available yet for this camera.</div>
    </div>` };
  }
  // /api/videodb/rmsnights returns newest-first (future-dropped). Walk
  // forward until we find a night that actually carries plot files.
  let date = null;
  let plots = [];
  for (const candidate of nights) {
    try {
      const r = await fetch(`/api/rms/plots/${apiHost}/${cam}/${candidate}`);
      if (!r.ok) continue;
      const list = await r.json();
      if (Array.isArray(list) && list.length) {
        date = candidate;
        plots = list;
        break;
      }
    } catch (e) { /* try next night */ }
  }
  if (!date) {
    return { date: null, html: `<div class="fdp-night-plots-cam-row">
      <div class="fdp-night-plots-cam-header">${cam}</div>
      <div style="font-size:12px;color:var(--muted)">No plots available yet for this camera.</div>
    </div>` };
  }
  // Show every plot the station returns (matches the RMS / Detection tab) —
  // the previous narrow suffix filter hid most of them. Still skip non-PNG
  // artifacts that aren't viewable inline (none today, but defensive).
  const wanted = plots.filter(p => p && p.filename);
  if (!wanted.length) {
    return { date, html: `<div class="fdp-night-plots-cam-row">
      <div class="fdp-night-plots-cam-header">${cam} &middot; ${date}</div>
      <div style="font-size:12px;color:var(--muted)">No plots available yet for this camera.</div>
    </div>` };
  }
  const tiles = wanted.map(p => {
    const imgUrl = `/api/rms/plot_image/${apiHost}/${cam}/${date}/${encodeURIComponent(p.filename)}`;
    const label = p.label || p.filename;
    return `<div class="fdp-night-plots-tile fdp-plot-tile-trigger" data-img-url="${escHtml(imgUrl)}" data-label="${escHtml(label)}">
      <img src="${escHtml(imgUrl)}" loading="lazy" decoding="async" alt="${escHtml(label)}"
           onerror="this.closest('.fdp-night-plots-tile').style.display='none'">
      <div class="fdp-night-plots-tile-cap">${escHtml(label)}</div>
    </div>`;
  }).join('');
  return { date, html: `<div class="fdp-night-plots-cam-row">
    <div class="fdp-night-plots-cam-header">${cam} &middot; ${date}</div>
    <div class="fdp-night-plots-grid">${tiles}</div>
  </div>` };
}

/* Delegated click handler for FDP plot tile cards. Reads server-supplied
   imgUrl and label from data-* attributes set at render time via escHtml,
   so neither value appears in an onclick attribute context. */
function _fdpPlotTileClick(e) {
  const tile = e.target.closest('.fdp-plot-tile-trigger');
  if (!tile) return;
  fdpOpenRmsPlot(tile.dataset.imgUrl, tile.dataset.label);
}

/* Click-to-enlarge for FDP RMS night plot thumbs. Prefers the shared
   cam-modal (same path RMS / Detection uses via rmsPlotExpand); falls
   back to opening the image URL in a new tab if the modal DOM isn't on
   this template. */
function fdpOpenRmsPlot(imgUrl, label) {
  const modal = document.getElementById('cam-modal');
  const img   = document.getElementById('cam-modal-img');
  const title = document.getElementById('cam-modal-title');
  if (modal && img && title && typeof rmsPlotExpand === 'function') {
    rmsPlotExpand(modal, imgUrl, label);
    return;
  }
  window.open(imgUrl, '_blank', 'noopener');
}



/* ─────────────────────────────────────────
   System Admin tab render
───────────────────────────────────────── */
function svcRow(id, icon, name, desc, checked, onchange = '') {
  return `<div class="svc-row">
    <span class="svc-icon">${icon}</span>
    <div class="svc-text">
      <div class="svc-name">${name}</div>
      <div class="svc-desc">${desc}</div>
    </div>
    <label class="svc-toggle">
      <input type="checkbox" id="cfg-svc-${id}" ${checked ? 'checked' : ''}${onchange ? ` onchange="${onchange}"` : ''}>
      <span class="svc-toggle-track"></span>
    </label>
  </div>`;
}

function toggleSvcPanel(panelId, btn) {
  const panel = document.getElementById(panelId);
  if (!panel) return;
  const open = panel.classList.toggle('open');
  btn.setAttribute('aria-expanded', open);
  btn.textContent = open ? 'Settings ▴' : 'Settings ▾';
}

function toggleCamPanel(panelId, btn) {
  const panel = document.getElementById(panelId);
  if (!panel) return;
  const open = panel.classList.toggle('open');
  btn.setAttribute('aria-expanded', open);
  btn.textContent = open ? 'Hide ▴' : 'Show ▾';
}

function updateEncoderLabels(host, useVaapi) {
  const list = document.getElementById('cfg-quality-list-' + host);
  if (!list) return;
  list.querySelectorAll('label[data-lvl]').forEach(label => {
    const lvl = parseInt(label.dataset.lvl);
    if (lvl === 0) return;
    const qp  = label.dataset.qp;
    const crf = label.dataset.crf;
    const el  = label.querySelector('.enc-label');
    if (!el) return;
    const primary   = useVaapi ? qp : crf;
    const secondary = useVaapi
      ? `<span class="enc-secondary">${crf} CPU</span>`
      : `<span class="enc-secondary">${qp} VAAPI</span>`;
    el.innerHTML = `${primary} ${secondary}`;
  });
}

function renderSuggestions(host, cfg) {
  const hw = window.hwData[host];

  // Hardware profile summary (shown even if no suggestions)
  let hwSummary = '';
  if (!hw || hw.__missing) {
    return `<div class="settings-edit-card" style="border-left:3px solid var(--muted)">
      <div class="settings-edit-title">Hardware Profile</div>
      <div style="font-size:12px;color:var(--muted)">
        No hardware profile found. Run <code>bash rovimen_probe.sh</code> on the station to generate one.
      </div>
    </div>`;
  }

  const caps = cfg.capabilities || {};
  const ret  = cfg.retention    || {};
  const sug  = hw.suggested     || {};

  // Build suggestion list
  const suggestions = [];

  // encode_workers
  const currentWorkers = cfg.encode_workers ?? 1;
  if (sug.encode_workers && sug.encode_workers !== currentWorkers) {
    suggestions.push({
      icon: '🧵',
      text: `${hw.cpu_cores}-core CPU — encode workers set to ${currentWorkers}, suggest ${sug.encode_workers}`,
      patch: { encode_workers: sug.encode_workers },
      label: `Set to ${sug.encode_workers}`,
    });
  }

  // retention.color_days
  const currentColorDays = ret.color_days ?? 2;
  if (sug.retention_color_days && Math.abs(sug.retention_color_days - currentColorDays) >= 1) {
    const dir = sug.retention_color_days > currentColorDays ? 'increase' : 'decrease';
    suggestions.push({
      icon: '💾',
      text: `${hw.disk_gb} GB disk, ~${hw.cpu_cores > 0 ? Object.keys(cfg.stations||{}).length || 2 : 2} cameras — suggest ${dir} unlocked clip retention to ${sug.retention_color_days} days (currently ${currentColorDays})`,
      patch: { retention: { color_days: sug.retention_color_days } },
      label: `Set to ${sug.retention_color_days}d`,
    });
  }

  // compression_level (only suggest if vaapi is being enabled)
  if (hw.vaapi_h264_encode && !caps.vaapi && sug.compression_level !== undefined
      && (cfg.compression_level ?? 0) === 0) {
    suggestions.push({
      icon: '🎬',
      text: `Re-encoding is set to raw copy — with VAAPI enabled, suggest QP 20 (level 2)`,
      patch: { compression_level: 2 },
      label: 'Set QP 20',
    });
  }

  // Hardware summary line
  const vaapiStr = hw.vaapi_h264_encode
    ? `<span style="color:var(--green)">VAAPI H.264 ✓</span> (${hw.vaapi_driver || '?'})`
    : hw.vaapi
      ? `<span style="color:var(--yellow)">VAAPI (no H.264 encode)</span>`
      : `<span style="color:var(--muted)">No VAAPI</span>`;

  hwSummary = `<div style="display:flex;flex-wrap:wrap;gap:16px;font-size:11px;margin-bottom:${suggestions.length ? 10 : 0}px">
    <span><span style="color:var(--muted)">CPU</span> ${hw.cpu_model ? hw.cpu_model.replace(/\(R\)|\(TM\)/g,'').replace(/\s+/g,' ').trim() : '?'} · ${hw.cpu_cores} cores</span>
    <span><span style="color:var(--muted)">RAM</span> ${hw.ram_gb} GB</span>
    <span><span style="color:var(--muted)">Disk</span> ${hw.disk_gb} GB</span>
    <span>${vaapiStr}</span>
    ${hw.probed_at ? `<span style="color:var(--muted);margin-left:auto">probed ${hw.probed_at.slice(0,10)}</span>` : ''}
  </div>`;

  const hwTitle = (label) =>
    `<div class="settings-edit-title">${label}</div>`;

  if (!suggestions.length) {
    return `<div class="settings-edit-card" style="border-left:3px solid var(--green)">
      ${hwTitle('Hardware Profile')}
      ${hwSummary}
      <div style="font-size:12px;color:var(--green);margin-top:4px">✓ Config looks good for this hardware</div>
    </div>`;
  }

  const sugRows = suggestions.map((s, i) => `
    <div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;
                border-bottom:1px solid var(--border);font-size:12px">
      <span style="font-size:15px;flex-shrink:0;width:20px;text-align:center">${s.icon}</span>
      <span style="flex:1;color:var(--text)">${s.text}</span>
      <button class="settings-save-btn" style="padding:3px 10px;font-size:11px;flex-shrink:0"
              onclick="applySuggestion('${host}', ${i}, this)">
        ${s.label}
      </button>
    </div>`).join('');

  // Stash patches in a module-level map keyed by host so applySuggestion can read them
  _suggestionPatches[host] = suggestions.map(s => s.patch);

  return `<div class="settings-edit-card" style="border-left:3px solid var(--yellow)">
    ${hwTitle('Hardware Profile &amp; Suggestions')}
    ${hwSummary}
    ${sugRows}
  </div>`;
}

const _suggestionPatches = {};

async function applySuggestion(host, idx, btn) {
  const patch = (_suggestionPatches[host] || [])[idx];
  if (!patch) return;
  await saveSettings(host, patch, btn);
  // Re-render to update suggestion list after applying
  setTimeout(() => {
    if (window.settingsData[host]) renderSysAdmin(host);
  }, 2200);
}

async function runProbe(host, btn) {
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Analyzing…';
  try {
    const r = await fetch(`/api/probe/${host}`, { method: 'POST' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    if (data.__error) throw new Error(data.__error);
    window.hwData[host] = data;
    btn.textContent = '✓ Done';
    setTimeout(() => {
      btn.disabled = false;
      renderSysAdmin(host);
    }, 800);
  } catch (e) {
    btn.textContent = '✗ ' + e.message;
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 4000);
  }
}

function _mkDataVisCard(host) {
  const meta = state.STATIONS_META[host] || {};
  const pub  = new Set(meta.public_tabs || []);
  const row  = (tab, label, desc) => `
    <div class="svc-row">
      <div class="svc-text">
        <div class="svc-name">${label}</div>
        <div class="svc-desc">${desc}</div>
      </div>
      <label class="svc-toggle">
        <input type="checkbox" ${pub.has(tab) ? 'checked' : ''}
               onchange="saveDataVisibility('${host}','${tab}',this.checked)">
        <span class="svc-toggle-track"></span>
      </label>
    </div>`;
  return `<div class="card">
    <div class="card-title">Data Visibility</div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:12px">
      Control which content guests and non-owning hosts can access for this station.
    </div>
    <div class="svc-list">
      ${row('timelapse', 'Timelapses',   'Allow public access to nightly timelapse videos')}
      ${row('videodb',   'Video Database','Allow public access to color clip browser')}
      ${row('archive',   'Archive',       'Allow public access to the cloud archive')}
    </div>
  </div>`;
}

async function saveDataVisibility(host, tab, enabled) {
  const meta = state.STATIONS_META[host] || {};
  const current = new Set(meta.public_tabs || []);
  if (enabled) current.add(tab); else current.delete(tab);
  const r = await fetch(`/api/admin/network-config/station/${host}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({public_tabs: [...current]}),
  });
  if (r.ok) {
    if (!state.STATIONS_META[host]) state.STATIONS_META[host] = {};
    state.STATIONS_META[host].public_tabs = [...current];
  }
}

// Tracks which (host, cfg-fingerprint) pair currently owns the #pane-settings
// DOM so renderSysAdmin can skip the ~200-element rebuild on every tab switch
// (T2). Cleared by refreshSysAdmin, fetchSettings, and any save handler that
// rewrites cfg — those still need a full rebuild because the static HTML
// reflects cfg values via spinners/inputs.
const _settingsRenderState = {};

// Fingerprint the data baked into the static HTML by renderSysAdmin. Cheap
// regions (vitals, services list, crons, storage rows, dawn progress, RMS
// status, updater status, logs) are NOT included — they refresh via their
// own updater functions and don't trigger a full rebuild. status.services
// 'color-capture' IS included because the rrovimen-disabled banner and the
// color-capture svc-block border depend on it.
function _sysAdminCfgSig(host) {
  const cfg = window.settingsData[host];
  if (!cfg) return null;
  const status = state.statusData[host] || {};
  return JSON.stringify({
    cfg,                       // every form value rides on this
    hw:  window.hwData[host] || null, // capabilities (vaapi/hwencode)
    enc: window.encStatsData[host]?.overall || null,  // header stats box
    cc:  (status.services || {})['color-capture'] || null, // affects banner
    sl:  !!state.statusData[host],   // statusLoaded gate
  });
}

// Refresh only the data-driven sub-regions of #pane-settings without
// rebuilding the form structure. Called on tab re-entry when the fingerprint
// matches and the pane is already wired up.
function _refreshSettingsRegions(host) {
  const status = state.statusData[host] || {};
  // Services list
  const sec = document.getElementById(`sec-services-sysadmin-${host}`);
  if (sec) sec.innerHTML = mkServices(status.services || {});
  // Storage card
  const stEl = document.getElementById(`storage-data-${host}`);
  if (stEl) stEl.innerHTML = mkStorage(status.storage, status.disk, status.extra_disks);
  // Vitals bars (cheap in-place; no rebuild)
  updateVitalsDOM(host);
  // Async region refreshers — same calls the full render makes post-mount
  window.fetchCrons(host);
  window.fetchStoragewatchStatus(host);
  fetchUpdaterStatus(host);
  fetchDawnProgress(host);
}

function renderSysAdmin(host) {
  const el  = document.getElementById('pane-settings');

  // Host-role view: live cameras only. The full admin surface (encoding/retention
  // controls, restart buttons, log viewers, dawn pipeline) is admin-gated server
  // side too — this short-circuit avoids exposing buttons whose backend would
  // 403, and avoids tempting a host into changing config they shouldn't touch.
  // Marker check keeps active live-stream <img> elements alive across re-renders.
  if (!state.IS_ADMIN) {
    if (document.getElementById(`sysadmin-host-${host}`)) return;
    const cams = mkCameraWindows(host, null, { collapsible: false });
    // mkCameraWindows returns '' for two distinct reasons; disambiguate so a
    // host isn't told a populated station has "no cameras". The live feed is
    // gated to the station's own operator (or an admin) — a host viewing a
    // station they don't operate gets the grid hidden, which is intentional.
    let body;
    if (cams) {
      body = cams;
    } else if (!_ownsStation(host)) {
      body = '<div class="offline">Live feed is only available to this station’s operator.</div>';
    } else {
      body = '<div class="offline">No cameras configured for this station</div>';
    }
    el.innerHTML = `<div id="sysadmin-host-${host}">${body}</div>`;
    return;
  }

  const cfg = window.settingsData[host];
  if (!cfg) { el.innerHTML = '<div class="offline">Loading…</div>'; return; }
  if (cfg.__error) { el.innerHTML = `<div class="offline">Failed — ${escHtml(cfg.__error)}</div>`; return; }

  // Incremental render path: pane already owns this host's form, cfg
  // fingerprint hasn't changed. Skip the ~30 ms innerHTML rebuild and only
  // touch the data-driven sub-regions.
  const sig = _sysAdminCfgSig(host);
  const prev = _settingsRenderState[host];
  const marker = document.getElementById(`sysadmin-root-${host}`);
  if (prev && prev.sig === sig && marker) {
    _refreshSettingsRegions(host);
    switchSysAdminTab(host, activeSysAdminTab[host] || 'health');
    return;
  }

  const ov     = cfg.overlay    || {};
  const ret    = cfg.retention  || {};
  const svc    = cfg.services   || {};
  const arc    = cfg.archive    || {};
  const det    = cfg.detection  || {};
  const dsk    = cfg.disk       || {};
  const status = state.statusData[host] || {};
  const statusLoaded = !!state.statusData[host];
  // Only treat as disabled if status has loaded — avoids false banner on first render
  const rrovimenOn = !statusLoaded || status.online === false || (status.services || {})['color-capture'] === 'active';

  // Encoding stats info box (shared by reencoder panel)
  const encStatsBox = (() => {
    const st = window.encStatsData[host];
    if (!st || !st.overall || !st.overall.chunks) return '';
    const o = st.overall;
    const rawMb = st.raw_mb || 0;
    const camLines = Object.entries(st.cameras || {}).map(([cam, c]) => {
      const sizeStr = c.size_avg != null
        ? `${c.size_avg} MB avg (${c.size_min}–${c.size_max} MB)`
        : '';
      return `<div style="margin-top:3px"><span style="color:var(--muted)">${cam}</span>`
           + `  ${c.avg}% avg / ${c.median}% median`
           + (sizeStr ? `  <span style="color:var(--muted)">${sizeStr}</span>` : '')
           + `</div>`;
    }).join('');
    const sizeRow = o.size_avg != null
      ? `<span style="color:var(--muted);margin-left:10px">${o.size_avg} MB avg (${o.size_min}–${o.size_max} MB)</span>`
        + (rawMb ? `<span style="color:var(--muted);margin-left:6px">vs ${rawMb} MB raw</span>` : '')
      : '';
    return `<div style="background:rgba(88,166,255,.06);border:1px solid rgba(88,166,255,.18);
                         border-radius:6px;padding:8px 12px;margin-bottom:10px;font-size:11px">
      <span style="color:var(--blue);font-weight:600">Last night actual reduction</span>
      <span style="color:var(--muted);margin-left:6px">${o.chunks} chunks</span>
      <div style="margin-top:4px;color:var(--text)">
        <b>${o.avg}%</b> avg · <b>${o.median}%</b> median · <b>${o.min ?? '—'}%</b> min · <b>${o.max ?? '—'}%</b> max
        ${sizeRow}
      </div>
      ${camLines ? `<div style="margin-top:4px;border-top:1px solid var(--border);padding-top:4px">${camLines}</div>` : ''}
    </div>`;
  })();

  // Compression level radios
  const qualityRadios = `
    <div id="cfg-quality-list-${host}" style="display:flex;flex-direction:column;gap:6px;margin-bottom:10px">
      ${[
        [0, 'Raw copy', null,     'No recompression — original CBR stream preserved'],
        [1, 'QP 19',   'CRF 20', 'Visually lossless — smallest perceptible quality impact'],
        [2, 'QP 20',   'CRF 21', 'Minimal loss — good balance for most nights'],
        [3, 'QP 21',   'CRF 22', 'Mild loss — compression ratio depends heavily on sky conditions'],
        [4, 'QP 22',   'CRF 23', 'Moderate loss — effective only on clear, dark nights'],
      ].map(([lvl, qp, crf, note]) => {
        const useVaapi = cfg.capabilities?.vaapi ?? true;
        const primary   = lvl === 0 ? 'Raw copy' : (useVaapi ? qp : crf);
        const secondary = lvl === 0 ? '' : (useVaapi
          ? `<span class="enc-secondary">/ ${crf} CPU</span>`
          : `<span class="enc-secondary">/ ${qp} VAAPI</span>`);
        return `<label data-lvl="${lvl}" data-qp="${qp??''}" data-crf="${crf??''}"
                       style="display:flex;align-items:flex-start;gap:10px;padding:8px 10px;
                              border-radius:6px;border:1px solid var(--border);cursor:pointer;
                              background:${(cfg.compression_level??0)===lvl?'rgba(88,166,255,.07)':'transparent'}">
          <input type="radio" name="cfg-quality-${host}" value="${lvl}"
                 ${(cfg.compression_level??0)===lvl?'checked':''}
                 style="accent-color:var(--blue);margin-top:2px;cursor:pointer;flex-shrink:0">
          <div>
            <div class="enc-label" style="font-size:13px;font-weight:700;color:var(--text)">${primary} ${secondary}</div>
            <div style="font-size:11px;color:var(--muted);margin-top:1px">${note}</div>
          </div>
        </label>`;
      }).join('')}
    </div>`;

  // Helper: render an expandable service block
  const svcBlock = (id, icon, name, desc, enabled, settingsHtml, saveFn) => {
    const panelId = `svc-panel-${host}-${id}`;
    return `<div class="svc-block">
      <div class="svc-row">
        <span class="svc-icon">${icon}</span>
        <div class="svc-text">
          <div class="svc-name">${name}</div>
          <div class="svc-desc">${desc}</div>
        </div>
        ${settingsHtml ? `<button class="svc-expand-btn" onclick="toggleSvcPanel('${panelId}', this)"
                                  aria-expanded="false">Settings ▾</button>` : ''}
        <label class="svc-toggle" style="margin-left:${settingsHtml?'8px':'auto'}">
          <input type="checkbox" id="cfg-svc-${id}" ${enabled?'checked':''}
            ${saveFn ? `onchange="${saveFn}"` : ''}>
          <span class="svc-toggle-track"></span>
        </label>
      </div>
      ${settingsHtml ? `<div class="svc-panel" id="${panelId}">
        <div class="svc-panel-inner">
          ${settingsHtml}
          ${settingsHtml.includes('Save Overlay') ? '' : `<button class="settings-save-btn" style="margin-top:8px" onclick="${saveFn}">Save</button>`}
        </div>
      </div>` : ''}
    </div>`;
  };

  const spinner = (id, min, max, step, val) =>
    `<div class="num-spinner">
      <button type="button" class="spinner-btn" onclick="stepSpinner('${id}',${-step},${min},${max})">−</button>
      <input type="number" id="${id}" min="${min}" max="${max}" step="${step}" value="${val}">
      <button type="button" class="spinner-btn" onclick="stepSpinner('${id}',${step},${min},${max})">+</button>
    </div>`;

  const cfgSelect = (id, opts, sel) =>
    `<select id="${id}" class="cfg-select">${opts.map(([v,l])=>`<option value="${v}"${sel===v?' selected':''}>${l}</option>`).join('')}</select>`;

  // ── [1] System Graphs ────────────────────────────────────────────────────
  const graphsCard = `<div class="card">
    <div class="card-title">System Graphs</div>
    <div class="graph-grid">
      ${[['cpu','CPU Load %'],['temp','Temperature °C'],['ram','RAM %'],['disk','Disk Write MB/s']].map(([k,lbl]) => `
        <div class="graph-box">
          <div class="graph-label">${lbl}</div>
          <canvas id="graph-${k}-${host}" class="graph-canvas" height="80"></canvas>
        </div>`).join('')}
      <div class="graph-box" style="grid-column:1/-1">
        <div class="graph-label">Per-Core Load &amp; Frequency</div>
        <div id="graph-cores-${host}" class="cores-bars"><span style="color:var(--muted);font-size:11px">Waiting for data…</span></div>
        <div class="graph-label" style="margin-top:10px">Top CPU Users</div>
        <div id="graph-top-procs-${host}" class="cores-bars"></div>
      </div>
    </div>
  </div>`;

  // ── [2] Storage + Janitor ─────────────────────────────────────────────────
  const storageCard = `<div class="card">
    <div class="card-title">Storage</div>
    <div id="storage-data-${host}">${mkStorage(status.storage, status.disk, status.extra_disks)}</div>
    <div id="janitor-section-${host}">
      <div style="font-size:11px;color:var(--muted)">Janitor: loading…</div>
    </div>
  </div>`;

  // ── [3] Services + Cron Jobs ──────────────────────────────────────────────
  const servicesCard = `<div class="card">
    <div class="card-title">Services &amp; Cron Jobs</div>
    <div id="sec-services-sysadmin-${host}">${mkServices(status.services || {})}</div>
    <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">
      <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:8px">Cron Jobs</div>
      <div id="crons-table-${host}"><span style="font-size:12px;color:var(--muted)">Loading…</span></div>
    </div>
  </div>`;

  // ── [4] Processing Pipeline ───────────────────────────────────────────────
  const pipelineCard = `<div class="card">
    <div class="card-title" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      Processing Pipeline
      <span id="dawn-running-badge-${host}" class="dawn-running-badge" style="display:none">&#9679; Processing now&hellip;</span>
      <button class="svc-expand-btn" onclick="dawnNavDate('${host}',-1)">◀</button>
      <span id="dawn-date-lbl-${host}" style="font-size:12px;color:var(--muted);min-width:70px;text-align:center">—</span>
      <button class="svc-expand-btn" onclick="dawnNavDate('${host}',1)">▶</button>
    </div>
    <div id="dawn-progress-${host}" style="font-size:12px;color:var(--muted)">Loading…</div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">
      <span style="font-size:12px;color:var(--muted)">Force process night</span>
      <input type="date" id="cfg-dawn-date-${host}"
             style="font-size:12px;padding:4px 8px;background:var(--card);border:1px solid var(--border);border-radius:4px;color:var(--text)">
      <button class="settings-save-btn secondary" onclick="runDawn('${host}',this)">Run</button>
      <div id="dawn-run-result-${host}" style="font-size:11px"></div>
    </div>
  </div>`;

  // ── [5] RMS Process Status ────────────────────────────────────────────────
  const rmsCard = `<div class="card">
    <div class="card-title" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      RMS Process Status
      <button class="svc-expand-btn" onclick="rmsStatusNavDate('${host}',-1)">◀</button>
      <span id="rms-status-date-lbl-${host}" style="font-size:12px;color:var(--muted);min-width:70px;text-align:center">—</span>
      <button class="svc-expand-btn" onclick="rmsStatusNavDate('${host}',1)">▶</button>
    </div>
    <div id="rms-status-table-${host}"><span style="font-size:12px;color:var(--muted)">Loading…</span></div>
  </div>`;

  // ── [6] Configuration ─────────────────────────────────────────────────────
  const rotationBlock = (() => {
    const panelId = `svc-panel-${host}-rotation`;
    const rows = Object.entries(cfg.stations || {}).map(([code, sc]) => `
      <div style="display:flex;align-items:center;gap:10px;padding:6px 0">
        <span style="font-family:monospace;font-size:13px;min-width:80px">${code}</span>
        <label class="vdb-toggle-label" style="gap:6px">
          <input type="checkbox" id="cfg-rotate-${code}" ${sc.rotate ? 'checked' : ''}
                 style="accent-color:var(--blue);width:14px;height:14px;cursor:pointer;margin:0">
          <span>Rotated 180°</span>
        </label>
      </div>`).join('');
    return `<div class="svc-block">
      <div class="svc-row" style="cursor:pointer" onclick="toggleSvcPanel('${panelId}',this.querySelector('.svc-expand-btn'))">
        <span class="svc-icon">🔄</span>
        <div class="svc-text">
          <div class="svc-name">Camera Rotation</div>
          <div class="svc-desc">180° flip for upside-down camera installations</div>
        </div>
        <button class="svc-expand-btn" aria-expanded="false">Settings ▾</button>
      </div>
      <div class="svc-panel" id="${panelId}">
        <div class="svc-panel-inner">
          ${rows}
          <button class="settings-save-btn" style="margin-top:4px" onclick="saveRotation('${host}', this)">Save</button>
        </div>
      </div>
    </div>`;
  })();

  // Color-capture master toggle block (controls the systemd service, not just config)
  const colorCaptureBlock = (() => {
    const ccBorderStyle = rrovimenOn ? '' : 'border-color:rgba(210,153,34,.6)';
    return `<div class="svc-block" style="${ccBorderStyle}">
      <div class="svc-row">
        <span class="svc-icon">📹</span>
        <div class="svc-text">
          <div class="svc-name">Color Capture</div>
          <div class="svc-desc">Records color video from all cameras. Stacking, encoding, detection lock and archive upload all depend on this service.</div>
        </div>
        <label class="svc-toggle" style="margin-left:auto">
          <input type="checkbox" id="cfg-svc-color-capture-svc" ${rrovimenOn ? 'checked' : ''}
            onchange="toggleColorCapture('${host}', this)">
          <span class="svc-toggle-track"></span>
        </label>
      </div>
    </div>
    <div style="border-top:1px solid var(--border);margin:4px 0 8px;opacity:.4"></div>`;
  })();


  // ── Capture Settings block ─────────────────────────────────────────────────
  const captureBlock = svcBlock('capture', '📷', 'Capture Settings', 'Video capture parameters and disk safety limits',
    true,
    `<div class="settings-edit-grid">
      <label>Segment duration</label>
      ${spinner('cfg-segment-duration', 5, 120, 5, cfg.segment_duration ?? 20)}
      <span class="unit">seconds</span>
      <label>RTSP start delay</label>
      ${spinner('cfg-rtsp-delay', 0, 30, 1, cfg.rtsp_capture_delay_s ?? 6)}
      <span class="unit">seconds</span>
      <label>FFmpeg idle timeout</label>
      ${spinner('cfg-ff-timeout', 5, 120, 5, cfg.ff_idle_timeout_minutes ?? 30)}
      <span class="unit">minutes</span>
      <label>Min free disk</label>
      ${spinner('cfg-min-disk', 1, 100, 1, cfg.min_disk_gb_free ?? 10)}
      <span class="unit">GB</span>
    </div>
    <div class="settings-section">Disk Thresholds</div>
    <div class="settings-edit-grid">
      <label>Warn at</label>
      ${spinner('cfg-disk-warn', 50, 95, 1, dsk.warn_pct ?? 85)}
      <span class="unit">%</span>
      <label>Aggressive cleanup</label>
      ${spinner('cfg-disk-nuclear', 60, 98, 1, dsk.nuclear_pct ?? 90)}
      <span class="unit">%</span>
      <label>Emergency cleanup</label>
      ${spinner('cfg-disk-extreme', 70, 99, 1, dsk.extreme_pct ?? 95)}
      <span class="unit">%</span>
    </div>`,
    `saveSettings('${host}', {
      segment_duration:      numVal('cfg-segment-duration'),
      rtsp_capture_delay_s:  numVal('cfg-rtsp-delay'),
      ff_idle_timeout_minutes: numVal('cfg-ff-timeout'),
      min_disk_gb_free:      numVal('cfg-min-disk'),
      disk: {
        warn_pct:    numVal('cfg-disk-warn'),
        nuclear_pct: numVal('cfg-disk-nuclear'),
        extreme_pct: numVal('cfg-disk-extreme'),
      },
    }, this)`
  );

  const configCard = `<div class="card">
    <div class="card-title">Configuration</div>
    ${renderSuggestions(host, cfg)}
    <div class="svc-list">
      ${colorCaptureBlock}
      ${captureBlock}
      ${svcBlock('stacker', '⚡', 'Stacker', 'Build maxpixel stacks from color chunks',
        svc.stacker?.enabled ?? true,
        `<div class="svc-group" style="margin-bottom:10px">
          ${svcRow('stacker-realtime', '🕐', 'Real-time stacking',
            'Stack chunks as they arrive during the night. When disabled, stacking only runs during the morning sweep.',
            svc.stacker?.realtime ?? true)}
        </div>
        <div class="settings-edit-grid" style="margin-bottom:12px">
          <label>Nice level</label>
          ${spinner('cfg-stacker-nice', -20, 19, 1, svc.stacker?.nice ?? 10)}
          <span class="unit">−20 to 19</span>
          <label>CPU quota</label>
          ${spinner('cfg-stacker-cpu-quota', 5, 200, 5, svc.stacker?.cpu_quota ?? 50)}
          <span class="unit">%</span>
        </div>
        <div style="font-size:12px;color:var(--muted);line-height:1.6;border-top:1px solid var(--border);padding-top:10px">
          <p style="margin:0 0 8px">Real-time stacking runs as each 20-second chunk is recorded. Tries to keep pace with the live stream — if processing a chunk takes too long, it is skipped so the queue does not back up.</p>
          <p style="margin:0 0 8px">Yields to RMS detection: when RMS starts analysing frames, the stacker pauses to avoid competing for disk I/O and CPU.</p>
          <p style="margin:0">Chunks missed at night are caught by the <b style="color:var(--text)">morning sweep</b> (dawn processing), which runs a full pass after RMS completes — so every night's stacks are always complete by morning.</p>
        </div>`,
        `saveSettings('${host}', { services: {
          stacker: {
            enabled: document.getElementById('cfg-svc-stacker').checked,
            realtime: document.getElementById('cfg-svc-stacker-realtime').checked,
            nice: numVal('cfg-stacker-nice'),
            cpu_quota: numVal('cfg-stacker-cpu-quota'),
          }
        }}, this)`
      )}
      ${svcBlock('reencode', '🎬', 'Encoder', 'Encode raw MKVs with OSD annotation',
        svc.reencode?.enabled ?? true,
        `<div class="svc-group" style="margin-bottom:10px">
          ${svcRow('vaapi', '⚡', 'Hardware encoding',
            'GPU-accelerated H.264 (h264_vaapi); falls back to libx264 when disabled',
            cfg.capabilities?.hwencode ?? true,
            `updateEncoderLabels('${host}', this.checked)`)}
        </div>
        <div class="settings-edit-grid" style="margin-bottom:10px">
          <label>Workers</label>
          ${spinner('cfg-encode-workers', 1, 8, 1, cfg.encode_workers ?? 1)}
          <span class="unit">parallel</span>
        </div>
        <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;
                    letter-spacing:.8px;margin-bottom:6px">Compression</div>
        ${encStatsBox}
        ${qualityRadios}
        `,
        `saveSettings('${host}', {
          services: { reencode: { enabled: document.getElementById('cfg-svc-reencode').checked }},
          compression_level: parseInt(document.querySelector('input[name=\\'cfg-quality-${host}\\']:checked')?.value ?? '0'),
          encode_workers: numVal('cfg-encode-workers'),
          capabilities: { hwencode: document.getElementById('cfg-svc-vaapi').checked },
        }, this)`
      )}
      ${svcBlock('detection_lock', '🔒', 'Detection lock', 'Lock color chunks that contain RMS meteor detections',
        svc.detection_lock?.enabled ?? true,
        `<div class="settings-section" style="margin-top:4px">Detection window</div>
        <div class="settings-edit-grid">
          <label>Pre-event</label>
          ${spinner('cfg-detect-pre', 0, 30, 1, det.pre_seconds ?? 3)}
          <span class="unit">seconds</span>
          <label>Post-event</label>
          ${spinner('cfg-detect-post', 0, 60, 1, det.post_seconds ?? 12)}
          <span class="unit">seconds</span>
        </div>
        <div class="settings-section">Retention</div>
        <div class="settings-edit-grid">
          <label>Unlocked clips</label>
          ${spinner('cfg-color-days', 1, 30, 1, ret.color_days ?? 2)}
          <span class="unit">days</span>
          <label>Locked clips</label>
          ${spinner('cfg-locked-days', 1, 90, 1, ret.locked_days ?? 7)}
          <span class="unit">days</span>
          <label>Stacks</label>
          ${spinner('cfg-stacks-days', 1, 90, 1, ret.stacks_days ?? 7)}
          <span class="unit">days</span>
          <label>Timelapses</label>
          ${spinner('cfg-timelapse-days', 1, 90, 1, ret.timelapse_days ?? 7)}
          <span class="unit">days</span>
        </div>`,
        `saveSettings('${host}', {
          services: { detection_lock: { enabled: document.getElementById('cfg-svc-detection_lock').checked }},
          detection: { pre_seconds:  numVal('cfg-detect-pre'),
                       post_seconds: numVal('cfg-detect-post') },
          retention: { color_days:     numVal('cfg-color-days'),
                       locked_days:    numVal('cfg-locked-days'),
                       stacks_days:    numVal('cfg-stacks-days'),
                       timelapse_days: numVal('cfg-timelapse-days') },
        }, this)`
      )}
      ${svcBlock('archive_upload', '☁️', 'Archive upload', 'Upload meteors and timelapses to cloud storage box',
        svc.archive_upload?.enabled ?? true,
        `<div class="svc-group" style="margin-bottom:10px">
          ${[['arc-meteors','upload meteors',arc.upload_meteors??true],
             ['arc-timelapses','upload timelapses',arc.upload_timelapses??true],
             ['arc-stacks','upload stacks',arc.upload_stacks??false],
            ].map(([id,label,val]) => `<div class="svc-row" style="padding:8px 14px">
              <div class="svc-text"><div class="svc-name">${label}</div></div>
              <label class="svc-toggle">
                <input type="checkbox" id="cfg-${id}" ${val?'checked':''}>
                <span class="svc-toggle-track"></span>
              </label>
            </div>`).join('')}
        </div>
        <div class="settings-edit-grid">
          <label>Host</label>
          <input type="text" id="cfg-arc-host" value="${arc.host ?? ''}">
          <span></span>
          <label>Port</label>
          ${spinner('cfg-arc-port', 1, 65535, 1, arc.port ?? 22)}
          <span></span>
          <label>User</label>
          <input type="text" id="cfg-arc-user" value="${arc.user ?? 'root'}">
          <span></span>
          <label>Base path</label>
          <input type="text" id="cfg-arc-path" value="${arc.base_path ?? ''}">
          <span></span>
          <label>Upload interval</label>
          ${spinner('cfg-arc-interval', 5, 120, 5, arc.interval_minutes ?? 20)}
          <span class="unit">minutes</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:8px">
          <button class="settings-save-btn secondary" onclick="testArchive('${host}', this)">Test Connection</button>
          <span id="arc-test-${host}" style="font-size:11px"></span>
        </div>`,
        `saveSettings('${host}', {
          services: { archive_upload: { enabled: document.getElementById('cfg-svc-archive_upload').checked }},
          archive: { enabled:          document.getElementById('cfg-svc-archive_upload').checked,
                     host:             document.getElementById('cfg-arc-host').value,
                     port:             numVal('cfg-arc-port'),
                     user:             document.getElementById('cfg-arc-user').value,
                     base_path:        document.getElementById('cfg-arc-path').value,
                     upload_meteors:    document.getElementById('cfg-arc-meteors').checked,
                     upload_timelapses: document.getElementById('cfg-arc-timelapses').checked,
                     upload_stacks:     document.getElementById('cfg-arc-stacks').checked,
                     interval_minutes:  numVal('cfg-arc-interval') },
        }, this)`
      )}
      ${svcBlock('timelapse', '🎞', 'Timelapse build', 'Assemble nightly timelapse from stacked frames',
        svc.timelapse_build?.enabled ?? true, '', ''
      )}
      ${svcBlock('overlay-enabled', '🎨', 'Overlay', 'Burn OSD annotation into re-encoded video',
        ov.enabled !== false,
        `<div class="settings-edit-grid" style="margin-bottom:10px">
          <label>Style</label>
          ${cfgSelect('cfg-ov-style-' + host, [['standard','Standard'],['cinema','Cinema']], ov.style || 'standard')}
          <span></span>
          <label>Network name</label>
          <input type="text" id="cfg-ov-network" value="${escHtml(ov.network ?? 'ROVIMEN')}">
          <span></span>
          <label>Coordinates</label>
          <input type="text" id="cfg-ov-coords" value="${escHtml(ov.coords ?? '')}">
          <span style="font-size:10px;color:var(--muted)">e.g. 44 47N 026 41E</span>
          <label>Font size</label>
          ${spinner('cfg-ov-font-size', 8, 48, 1, ov.font_size ?? 19)}
          <span class="unit">px</span>
          <label>Text opacity</label>
          ${spinner('cfg-text-opacity', 0, 1, 0.05, ov.text_opacity ?? 0.8)}
          <span class="unit">0 – 1</span>
          <label>Logo opacity</label>
          ${spinner('cfg-logo-opacity', 0, 1, 0.05, ov.logo_opacity ?? 0.8)}
          <span class="unit">0 – 1</span>
          <label>Logo size</label>
          ${spinner('cfg-logo-size', 0, 200, 1, ov.logo_size ?? 0)}
          <span class="unit">px (0 = auto)</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px">
          ${[['show_logo','Logo'],['show_network','Network'],['show_station','Station'],
             ['show_coords','Coords'],['show_pointing','Pointing'],['show_timestamp','Timestamp']
            ].map(([k,label]) => `
            <label class="vdb-toggle-label" style="gap:6px">
              <input type="checkbox" id="cfg-ov-${k}" ${(ov[k]!==false)?'checked':''}
                     style="accent-color:var(--blue);width:14px;height:14px;cursor:pointer;margin:0">
              <span>${label}</span>
            </label>`).join('')}
        </div>
        <div style="margin-bottom:10px;padding:10px 12px;background:var(--bg2);border-radius:6px;border:1px solid var(--border)">
          <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;color:var(--muted);margin-bottom:8px">Logo file</div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
            <select id="cfg-logo-path-${host}" style="font-size:12px;padding:4px 8px;background:var(--card);border:1px solid var(--border);border-radius:4px;color:var(--text);min-width:180px"
                    onchange="saveOverlay('${host}')">
              <option value="">— none —</option>
            </select>
            <button class="settings-save-btn secondary" style="font-size:11px;padding:4px 10px"
                    onclick="logoRefreshList('${host}')">&#8635; Refresh</button>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input type="file" id="cfg-logo-file-${host}" accept="image/*"
                   style="font-size:12px;color:var(--muted);display:none"
                   onchange="logoFileChosen('${host}',this)">
            <button class="settings-save-btn secondary" style="font-size:11px;padding:4px 10px"
                    onclick="document.getElementById('cfg-logo-file-${host}').click()">&#128194; Browse files</button>
            <span id="cfg-logo-chosen-${host}" style="font-size:11px;color:var(--muted)"></span>
            <button id="cfg-logo-upload-btn-${host}" class="settings-save-btn" style="font-size:11px;padding:4px 10px;display:none"
                    onclick="logoUpload('${host}',this)">&#8593; Upload</button>
          </div>
        </div>
        <div style="display:flex;gap:8px;margin-bottom:8px">
          <button class="settings-save-btn" onclick="saveOverlay('${host}', this)">Save Overlay</button>
          <button class="settings-save-btn secondary"
                  onclick="previewOverlay('${host}', this)">Generate Preview</button>
        </div>
        <div id="overlay-preview-${host}" style="display:none;margin-top:8px">
          <img id="overlay-preview-img-${host}" decoding="async"
               style="width:100%;border-radius:6px;border:1px solid var(--border)">
        </div>`,
        `saveOverlay('${host}', this)`
      )}
      ${rotationBlock}
    </div>
    <label class="vdb-toggle-label" style="gap:6px;margin-top:12px;cursor:pointer">
      <input type="checkbox"
             onchange="this.closest('label').nextElementSibling.style.display=this.checked?'block':'none'"
             style="accent-color:var(--blue);width:14px;height:14px;cursor:pointer;margin:0">
      <span style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)">Show raw JSON</span>
    </label>
    <pre class="settings-raw" style="display:none">${escHtml(JSON.stringify(cfg, null, 2))}</pre>
  </div>`;

  // ── [7] Software Update ────────────────────────────────────────────────────
  const updateCard = `<div class="card">
    <div class="card-title">Software Update</div>
    <div class="settings-edit-grid" style="margin-bottom:10px">
      <label>Channel</label>
      <select id="cfg-channel-${host}" style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:12px">
        ${['main','dev'].map(c => `<option value="${c}" ${(cfg.update_channel||'main')===c?'selected':''}>${c}</option>`).join('')}
      </select>
      <span></span>
      <label>Local version</label>
      <span id="upd-local-${host}" style="font-family:monospace;font-size:12px">—</span>
      <span></span>
      <label>Remote version</label>
      <span id="upd-remote-${host}" style="font-family:monospace;font-size:12px">—</span>
      <span id="upd-status-badge-${host}" style="font-size:11px;color:var(--muted)"></span>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="settings-save-btn secondary"
              onclick="saveSettings('${host}',{update_channel:document.getElementById('cfg-channel-${host}').value},this)">Save channel</button>
      <button class="settings-save-btn secondary" onclick="checkUpdater('${host}',this)">Check for update</button>
      <button class="settings-save-btn" onclick="runUpdater('${host}',this)">Force update now</button>
    </div>
    <div id="upd-log-${host}" style="display:none;margin-top:10px;max-height:200px;overflow-y:auto;
         background:rgba(0,0,0,.3);border-radius:6px;padding:8px;font-size:11px;font-family:monospace;
         white-space:pre-wrap"></div>
  </div>`;

  const logsCard = `<div class="card">
    <div class="card-title">Log Viewer</div>
    <div id="logs-section-${host}"><span style="font-size:12px;color:var(--muted)">Loading…</span></div>
  </div>`;

  const rrovimenBanner = rrovimenOn ? '' :
    `<div class="rovimen-disabled-banner">&#9888; Color capture is disabled — stacking, encoding, detection lock and all dependent processes are not running.</div>`;
  const svcStatusCard = `<div class="card">
    <div class="card-title" style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
      <span>Services Check</span>
      <div style="display:flex;gap:6px;align-items:center">
        <button class="settings-save-btn" style="padding:4px 10px;font-size:11px"
                onclick="rovimenControl('${host}','on',this)">On</button>
        <button class="settings-save-btn secondary" style="padding:4px 10px;font-size:11px"
                onclick="rovimenControl('${host}','off',this)"
                title="Stops color capture and dependent services. rovimen-station-api stays running so the dashboard remains reachable.">Off</button>
        <button class="settings-save-btn secondary" style="padding:4px 10px;font-size:11px"
                onclick="rovimenControl('${host}','restart',this)">Restart</button>
      </div>
    </div>
    <div id="rovimen-status-out-${host}" style="margin-top:10px;
         background:rgba(0,0,0,.25);border-radius:6px;padding:10px 14px;
         font-size:12px;font-family:monospace;white-space:pre;line-height:1.7;
         overflow-x:auto"><span style="color:var(--muted)">Loading…</span></div>
  </div>`;

  const refreshBtn = `<div style="display:flex;justify-content:flex-end;margin-bottom:6px">
    <button class="svc-expand-btn" onclick="refreshSysAdmin('${host}', this)">↻ Refresh</button>
  </div>`;

  const innerTabsNav = `<div class="inner-tabs">
    <button id="sa-tab-health-${host}"     class="inner-tab" onclick="switchSysAdminTab('${host}','health')">System Health</button>
    <button id="sa-tab-processes-${host}"  class="inner-tab" onclick="switchSysAdminTab('${host}','processes')">ROVIMEN Configuration</button>
    <button id="sa-tab-updatelogs-${host}" class="inner-tab" onclick="switchSysAdminTab('${host}','updatelogs')">Logs</button>
    <button id="sa-tab-livestream-${host}" class="inner-tab" onclick="switchSysAdminTab('${host}','livestream')">Livestream</button>
  </div>`;

  const healthPane     = `<div id="sa-pane-health-${host}">${graphsCard}${storageCard}${servicesCard}</div>`;
  const dataVisCard = _mkDataVisCard(host);
  const processesPane  = `<div id="sa-pane-processes-${host}">${svcStatusCard}${pipelineCard}${rmsCard}${configCard}${dataVisCard}${updateCard}</div>`;
  const updatelogPane  = `<div id="sa-pane-updatelogs-${host}">${logsCard}</div>`;
  const livestreamPane = `<div id="sa-pane-livestream-${host}">${mkCameraWindows(host, null, { collapsible: false })}</div>`;

  // Wrap the rendered output in a marker div so subsequent renderSysAdmin
  // calls can detect "pane is wired for this host" and take the incremental
  // path (T2). Clearing window.settingsData[host] or calling refreshSysAdmin both
  // invalidate the cache via _settingsRenderState below.
  el.innerHTML = `<div id="sysadmin-root-${host}">`
    + [refreshBtn, rrovimenBanner, innerTabsNav, healthPane, processesPane, updatelogPane, livestreamPane].join('')
    + `</div>`;
  _settingsRenderState[host] = { sig };

  // Post-render: activate the remembered tab (or default to health)
  switchSysAdminTab(host, activeSysAdminTab[host] || 'health');

  const _dawnInput = document.getElementById(`cfg-dawn-date-${host}`);
  if (_dawnInput && !_dawnInput.value) {
    const _y = new Date(); _y.setUTCDate(_y.getUTCDate() - 1);
    _dawnInput.value = _y.toISOString().slice(0, 10);
  }
  window.fetchCrons(host);
  window.fetchStoragewatchStatus(host);
  fetchUpdaterStatus(host);
  logoRefreshList(host, (window.settingsData[host]?.overlay?.logo) || '');
}

function numVal(id) { const el = document.getElementById(id); return el ? parseFloat(el.value) : NaN; }

function stepSpinner(id, delta, min, max) {
  const el = document.getElementById(id);
  if (!el) return;
  const v = Math.round((parseFloat(el.value) + delta) * 10000) / 10000;
  el.value = Math.min(max, Math.max(min, v));
}

function switchSysAdminTab(host, tab) {
  activeSysAdminTab[host] = tab;
  ['health', 'processes', 'updatelogs', 'livestream'].forEach(p => {
    const pane = document.getElementById(`sa-pane-${p}-${host}`);
    const btn  = document.getElementById(`sa-tab-${p}-${host}`);
    if (pane) pane.style.display = (p === tab) ? '' : 'none';
    if (btn)  btn.classList.toggle('active', p === tab);
  });
  if (tab === 'health')     setTimeout(() => window.drawAllGraphs(host), 0);
  if (tab === 'processes')  {
    setTimeout(() => checkRovimenStatus(host), 0);
    // Populate the RMS Process Status table on every entry to the Processes
    // sub-tab. Without this the table was only refreshed when the date arrows
    // were clicked (rmsStatusNavDate), leaving it stuck on "Loading…".
    setTimeout(() => window.fetchRMSStatus(host), 0);
  }
  if (tab === 'updatelogs') { window.renderLogsShell(); window.fetchLogs(); }
}

/* ─────────────────────────────────────────
   Morning Pipeline progress
───────────────────────────────────────── */
function _lastNight() {
  const d = new Date();
  if (d.getUTCHours() < 12) d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10).replace(/-/g, '');
}

async function fetchDawnProgress(host) {
  if (!dawnProgressState[host]) dawnProgressState[host] = { date: window._lastNight() };
  const date = dawnProgressState[host].date;
  const lbl   = document.getElementById(`dawn-date-lbl-${host}`);
  const tbl   = document.getElementById(`dawn-progress-${host}`);
  const badge = document.getElementById(`dawn-running-badge-${host}`);
  if (!tbl) return;
  if (lbl) lbl.textContent = `${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6,8)}`;
  try {
    const r = await fetchOnce(`tab:dawn:${host}`, `/api/dawn/progress/${host}?date=${date}`);
    if (!r.ok) { tbl.innerHTML = `<span style="color:var(--muted)">Error ${escHtml(r.status)}</span>`; return; }
    const data = await r.json();
    if (badge) badge.style.display = data.running ? '' : 'none';
    const cams = Object.keys(data.cameras || {});
    if (!cams.length) { tbl.innerHTML = '<span style="color:var(--muted)">No cameras configured</span>'; return; }
    const ck  = v => v ? '<span style="color:var(--green,#3fb950)">✓</span>' : '<span style="color:var(--muted)">…</span>';
    const frac = (a, b, done) => {
      if (b === 0) return '<span style="color:var(--muted)">—</span>';
      return done
        ? `<span style="color:var(--green,#3fb950)">${a}/${b} ✓</span>`
        : `<span style="color:var(--yellow,#d29922)">${a}/${b} …</span>`;
    };
    const rows = cams.map(cam => {
      const c = data.cameras[cam];
      const camE = escHtml(cam);
      if (c.error) return `<tr><td style="font-family:monospace;padding:4px 8px 4px 0">${camE}</td><td colspan="6" style="color:var(--muted);padding:4px 6px">${escHtml(c.error)}</td></tr>`;
      return `<tr>
        <td style="font-family:monospace;padding:4px 8px 4px 0">${camE}</td>
        <td style="padding:4px 6px;text-align:center">${ck(c.rms_complete)}</td>
        <td style="padding:4px 6px">${frac(c.stacked, c.total, c.stacked === c.total && c.total > 0)}</td>
        <td style="padding:4px 6px;text-align:center">${ck(c.timelapse_done)}</td>
        <td style="padding:4px 6px">${frac(c.reencoded, c.total, c.reencoded === c.total && c.total > 0)}</td>
        <td style="padding:4px 6px">${frac(c.uploaded, c.locked, c.locked > 0 && c.uploaded === c.locked)}</td>
        <td style="padding:4px 6px;text-align:center">${ck(c.morning_done)}</td>
      </tr>`;
    }).join('');
    tbl.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead><tr style="color:var(--muted);text-align:left;border-bottom:1px solid var(--border)">
        <th style="padding:4px 8px 4px 0">Camera</th>
        <th style="padding:4px 6px;text-align:center">EON</th>
        <th style="padding:4px 6px">Stacking</th>
        <th style="padding:4px 6px;text-align:center">Timelapse</th>
        <th style="padding:4px 6px">Encoding</th>
        <th style="padding:4px 6px">Uploaded</th>
        <th style="padding:4px 6px;text-align:center">Done</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  } catch(e) {
    if (_isAbort(e)) return;
    if (tbl) tbl.innerHTML = `<span style="color:var(--muted)">Fetch failed: ${escHtml(e?.message || e)}</span>`;
  }
}

function dawnNavDate(host, delta) {
  if (!dawnProgressState[host]) dawnProgressState[host] = { date: window._lastNight() };
  const d = dawnProgressState[host].date;
  const dt = new Date(`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}T12:00:00Z`);
  dt.setUTCDate(dt.getUTCDate() + delta);
  dawnProgressState[host].date = dt.toISOString().slice(0, 10).replace(/-/g, '');
  fetchDawnProgress(host);
}

/* ─────────────────────────────────────────
   Archive test
───────────────────────────────────────── */
async function testArchive(host, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Testing…';
  const el = document.getElementById(`arc-test-${host}`);
  try {
    const d = await fetchJson(`/api/archive/test/${host}`, { method: 'POST' });
    if (d.ok) {
      if (el) el.innerHTML = '<span style="color:var(--green,#3fb950)">✓ Connected</span>';
    } else {
      if (el) el.innerHTML = `<span style="color:var(--red,#f85149)">✗ ${escHtml(d.error || 'Failed')}</span>`;
    }
  } catch(e) {
    if (el) el.innerHTML = `<span style="color:var(--red,#f85149)">✗ ${escHtml(e?.message || e)}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

/* ─────────────────────────────────────────
   Updater
───────────────────────────────────────── */
function _setUpdaterUI(host, d) {
  const localEl  = document.getElementById(`upd-local-${host}`);
  const remoteEl = document.getElementById(`upd-remote-${host}`);
  const badge    = document.getElementById(`upd-status-badge-${host}`);
  if (localEl)         localEl.textContent  = d.local  || '—';
  if (remoteEl)        remoteEl.textContent = d.remote || '—';
  if (badge && d.status) {
    badge.textContent = d.status;
    badge.style.color = d.up_to_date ? 'var(--green,#3fb950)' : 'var(--yellow,#d29922)';
  }
}

async function fetchUpdaterStatus(host) {
  let r;
  try {
    r = await fetchOnce(`tab:updater:${host}`, `/api/updater/status/${host}`);
  } catch(e) {
    if (_isAbort(e)) return;
    r = null;
  }
  if (!r || !r.ok) return;
  const d = await r.json().catch(() => null);
  if (!d) return;
  // Merge cached remote info from localStorage so remote version survives refresh
  const cached = JSON.parse(localStorage.getItem(`updater_check_${host}`) || 'null');
  if (cached && !d.remote) {
    d.remote     = cached.remote;
    d.status     = cached.status;
    d.up_to_date = cached.up_to_date;
  }
  _setUpdaterUI(host, d);
}

async function checkUpdater(host, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Checking…';
  try {
    const d = await fetchJson(`/api/updater/check/${host}`, { method: 'POST' });
    if (d.error) { alert('Check failed: ' + d.error); return; }
    // Persist remote version so it survives page refresh
    localStorage.setItem(`updater_check_${host}`, JSON.stringify({
      remote: d.remote, status: d.status, up_to_date: d.up_to_date,
    }));
    _setUpdaterUI(host, d);
  } catch(e) {
    alert('Check failed: ' + e);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

function runUpdater(host, btn) {
  askConfirm(
    'Run the auto-updater now? This will restart services on the station.',
    async () => {
      btn.disabled = true;
      btn.textContent = 'Starting…';
      const logDiv = document.getElementById(`upd-log-${host}`);
      if (logDiv) { logDiv.style.display = 'block'; logDiv.textContent = 'Starting updater…\n'; }
      try {
        const d = await fetchJson(`/api/updater/run/${host}`, { method: 'POST' });
        if (!d.ok) {
          if (logDiv) logDiv.textContent += `Error: ${d.error}\n`;
          btn.disabled = false; btn.textContent = 'Force update now';
          return;
        }
        if (updaterLogTimer) clearInterval(updaterLogTimer);
        updaterLogTimer = setInterval(async () => {
          const lr = await fetch(`/api/updater/log/${host}`).catch(() => null);
          if (!lr || !lr.ok) return;
          const ld = await lr.json().catch(() => null);
          if (!ld || !logDiv) return;
          logDiv.innerHTML = (ld.lines || []).map(l =>
            (l.toLowerCase().includes('error') || l.toLowerCase().includes('failed'))
              ? `<span style="color:var(--red,#f85149)">${escHtml(l)}</span>`
              : escHtml(l)
          ).join('\n');
          logDiv.scrollTop = logDiv.scrollHeight;
          if (!ld.running) {
            clearInterval(updaterLogTimer); updaterLogTimer = null;
            btn.disabled = false; btn.textContent = 'Force update now';
          }
        }, 2000);
      } catch(e) {
        if (logDiv) logDiv.textContent += `Error: ${e}\n`;
        btn.disabled = false; btn.textContent = 'Force update now';
      }
    },
    { confirmLabel: 'Run updater', title: 'Run auto-updater' }
  );
}

/* ─────────────────────────────────────────
   Actions
───────────────────────────────────────── */
function restartServices(host, btn) {
  askConfirm(
    'Restart capture services? This briefly interrupts recording (~5s).',
    async () => {
      btn.disabled = true;
      btn.textContent = 'Restarting…';
      try {
        const r = await fetch(`/api/services/restart/${host}`, { method: 'POST' });
        const d = await r.json();
        btn.textContent = d.ok ? 'Restarted ✓' : `Error: ${d.errors?.join(', ') || 'failed'}`;
      } catch(e) {
        btn.textContent = 'Failed';
      }
      setTimeout(() => { btn.textContent = 'Restart services'; btn.disabled = false; }, 3000);
    },
    { confirmLabel: 'Restart services', title: 'Restart capture services' }
  );
}

function runDawn(host, btn) {
  const dateInput = document.getElementById(`cfg-dawn-date-${host}`);
  const dateVal = dateInput?.value;
  if (!dateVal) { alert('Select a date first'); return; }
  const dateStr = dateVal.replace(/-/g, '');
  if (!/^\d{8}$/.test(dateStr)) { alert('Invalid date'); return; }
  askConfirm(
    `Run dawn process for ${dateVal}?`,
    async () => {
      btn.disabled = true;
      const resEl = document.getElementById(`dawn-run-result-${host}`);
      if (resEl) resEl.textContent = 'Starting…';
      try {
        const r = await fetch(`/api/dawn/run/${host}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ date: dateStr }),
        });
        const d = await r.json();
        if (resEl) resEl.innerHTML = d.ok
          ? `<span style="color:var(--green,#3fb950)">Started (PID ${escHtml(d.pid)})</span>`
          : `<span style="color:var(--red,#f85149)">Error: ${escHtml(d.error || 'failed')}</span>`;
      } catch(e) {
        if (resEl) resEl.innerHTML = `<span style="color:var(--red,#f85149)">${escHtml(e?.message || e)}</span>`;
      } finally {
        btn.disabled = false;
      }
    },
    { confirmLabel: 'Run dawn', destructive: false, title: 'Run dawn process' }
  );
}


function saveOverlay(host, btn) {
  const style = document.getElementById(`cfg-ov-style-${host}`)?.value || 'standard';
  const keys  = ['show_logo','show_network','show_station','show_coords','show_pointing','show_timestamp'];
  const patch  = {
    enabled: document.getElementById('cfg-svc-overlay-enabled').checked,
    style,
    text_opacity: numVal('cfg-text-opacity'),
    logo_opacity: numVal('cfg-logo-opacity'),
    logo_size:    numVal('cfg-logo-size'),
    font_size:    numVal('cfg-ov-font-size'),
    network:      document.getElementById('cfg-ov-network')?.value ?? '',
    coords:       document.getElementById('cfg-ov-coords')?.value ?? '',
  };
  for (const k of keys) {
    const el = document.getElementById('cfg-ov-' + k);
    if (el) patch[k] = el.checked;
  }
  const logoSel = document.getElementById(`cfg-logo-path-${host}`);
  if (logoSel && logoSel.value) patch.logo = logoSel.value;
  saveSettings(host, {overlay: patch}, btn);
}

async function logoRefreshList(host, selectValue) {
  const sel = document.getElementById(`cfg-logo-path-${host}`);
  if (!sel) return;
  try {
    const r = await fetch(`/api/logo/list/${host}`);
    const logos = r.ok ? await r.json() : [];
    const current = selectValue ?? sel.value ?? (window.settingsData[host]?.overlay?.logo || '');
    sel.innerHTML = '<option value="">— none —</option>' +
      logos.map(l => `<option value="${l.path}"${l.path === current ? ' selected' : ''}>${l.filename}</option>`).join('');
  } catch(e) {}
}

function logoFileChosen(host, input) {
  const label = document.getElementById(`cfg-logo-chosen-${host}`);
  const btn   = document.getElementById(`cfg-logo-upload-btn-${host}`);
  if (input.files[0]) {
    if (label) label.textContent = input.files[0].name;
    if (btn)   btn.style.display = '';
  } else {
    if (label) label.textContent = '';
    if (btn)   btn.style.display = 'none';
  }
}

async function logoUpload(host, btn) {
  const input = document.getElementById(`cfg-logo-file-${host}`);
  if (!input?.files[0]) return;
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = 'Uploading…';
  try {
    const fd = new FormData();
    fd.append('file', input.files[0]);
    const r = await fetch(`/api/logo/upload/${host}`, { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Upload failed');
    await logoRefreshList(host, data.path);
    btn.textContent = '✓ Uploaded';
    const label = document.getElementById(`cfg-logo-chosen-${host}`);
    if (label) label.textContent = '';
    input.value = '';
    setTimeout(() => { btn.textContent = orig; btn.style.display = 'none'; btn.disabled = false; }, 2000);
  } catch(e) {
    btn.textContent = 'Error';
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
  }
}

async function previewOverlay(host, btn) {
  const style = document.getElementById(`cfg-ov-style-${host}`)?.value || 'standard';
  const keys  = ['show_logo','show_network','show_station','show_coords','show_pointing','show_timestamp'];
  const payload = { style, text_opacity: numVal('cfg-text-opacity'), logo_opacity: numVal('cfg-logo-opacity'),
    logo_size: numVal('cfg-logo-size'), font_size: numVal('cfg-ov-font-size'),
    network: document.getElementById('cfg-ov-network')?.value ?? '',
    coords: document.getElementById('cfg-ov-coords')?.value ?? '' };
  const logoSel = document.getElementById(`cfg-logo-path-${host}`);
  if (logoSel && logoSel.value) payload.logo = logoSel.value;
  for (const k of keys) {
    const el = document.getElementById('cfg-ov-' + k);
    if (el) payload[k] = el.checked;
  }
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Generating…';
  try {
    const resp = await fetch(`/api/overlay_preview/${host}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const blob = await resp.blob();
    const imgEl = document.getElementById(`overlay-preview-img-${host}`);
    if (imgEl.src.startsWith('blob:')) URL.revokeObjectURL(imgEl.src);
    imgEl.src = URL.createObjectURL(blob);
    document.getElementById(`overlay-preview-${host}`).style.display = 'block';
    btn.textContent = prev;
  } catch(e) {
    btn.textContent = 'Error';
    setTimeout(() => { btn.textContent = prev; }, 2000);
  } finally {
    btn.disabled = false;
  }
}

function saveRotation(host, btn) {
  const cfg = window.settingsData[host] || {};
  const stations = {};
  for (const code of Object.keys(cfg.stations || {})) {
    const el = document.getElementById('cfg-rotate-' + code);
    if (el) stations[code] = {rotate: el.checked};
  }
  saveSettings(host, {stations}, btn);
}

async function toggleRovimen(host, checkbox) {
  const enabled = checkbox.checked;
  checkbox.disabled = true;
  const marker = document.createElement('span');
  marker.className = 'svc-saved-marker';
  marker.textContent = enabled ? 'Enabling…' : 'Disabling…';
  marker.style.color = 'var(--muted)';
  checkbox.closest('.card-title').appendChild(marker);
  try {
    const resp = await fetch(`/api/rovimen/${host}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || data.output || resp.statusText);
    marker.textContent = enabled ? 'Enabled ✓' : 'Disabled ✓';
    marker.style.color = enabled ? 'var(--green)' : 'var(--yellow)';
    // Refresh status and re-render to update banner + service dots
    await fetchStatus(host);
    setTimeout(() => { marker.remove(); renderSysAdmin(host); }, 1500);
  } catch(e) {
    marker.textContent = 'Error: ' + e.message;
    marker.style.color = '#f85149';
    checkbox.checked = !enabled; // revert
    setTimeout(() => { marker.remove(); checkbox.disabled = false; }, 4000);
  }
}

function _ansiToHtml(text) {
  const styles = {
    '0;32': 'color:var(--green)', '1;32': 'color:var(--green)',
    '1;33': 'color:var(--yellow)', '0;33': 'color:var(--yellow)',
    '0;31': 'color:#f85149',
    '0;34': 'color:var(--blue)',
    '1':    'font-weight:600',
    '2':    'color:var(--muted)',
  };
  let open = 0;
  return text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\x1b\[([0-9;]+)m/g, (_, code) => {
      if (code === '0') { const c = open; open = 0; return '</span>'.repeat(c); }
      const style = styles[code];
      if (!style) return '';
      open++;
      return `<span style="${style}">`;
    });
}

async function refreshSysAdmin(host, btn) {
  btn.disabled = true;
  btn.textContent = '↻ Refreshing…';
  // Force a full rebuild — the user clicked refresh to see fresh structure,
  // not just data updates.
  delete _settingsRenderState[host];
  await Promise.all([fetchStatus(host), fetchSettings(host)]);
  renderSysAdmin(host);
}

async function checkRovimenStatus(host, btn = null) {
  const out = document.getElementById(`rovimen-status-out-${host}`);
  if (!out) return;
  if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  out.innerHTML = '<span style="color:var(--muted)">Loading…</span>';
  try {
    const resp = await fetch(`/api/rovimen/status/${host}`);
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || resp.statusText);
    out.innerHTML = _ansiToHtml(data.output);
  } catch(e) {
    out.innerHTML = `<span style="color:#f85149">Error: ${escHtml(e?.message || e)}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Check'; }
  }
}

async function rovimenControl(host, action, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '…';
  try {
    const url = action === 'restart'
      ? `/api/rovimen/restart/${host}`
      : `/api/rovimen/${host}`;
    const body = action === 'restart' ? '{}' : JSON.stringify({enabled: action === 'on'});
    const data = await fetchJson(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body });
    if (!data.ok) throw new Error(data.error || data.output || 'Unknown error');
    await fetchStatus(host);
    renderSysAdmin(host);
  } catch(e) {
    btn.disabled = false;
    btn.textContent = orig;
    alert('Error: ' + e.message);
  }
}

async function toggleColorCapture(host, checkbox) {
  const enabled = checkbox.checked;
  checkbox.disabled = true;
  const marker = document.createElement('span');
  marker.className = 'svc-saved-marker';
  marker.textContent = enabled ? 'Starting…' : 'Stopping…';
  marker.style.color = 'var(--muted)';
  checkbox.closest('.svc-row').appendChild(marker);
  try {
    const data = await fetchJson(`/api/color-capture/${host}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    if (!data.ok) throw new Error(data.error || data.output || 'Unknown error');
    marker.textContent = enabled ? 'Started ✓' : 'Stopped ✓';
    marker.style.color = enabled ? 'var(--green)' : 'var(--yellow)';
    // We know the new state — set it directly and re-render.
    // Don't call fetchStatus here: it may return stale cached data and
    // overwrite our known state before renderSysAdmin runs.
    if (state.statusData[host]?.services) {
      state.statusData[host].services['color-capture'] = enabled ? 'active' : 'inactive';
    }
    setTimeout(() => { marker.remove(); renderSysAdmin(host); }, 1500);
  } catch(e) {
    marker.textContent = 'Error: ' + e.message;
    marker.style.color = '#f85149';
    checkbox.checked = !enabled;
    setTimeout(() => { marker.remove(); checkbox.disabled = false; }, 4000);
  }
}

async function saveSettings(host, payload, btn) {
  const isToggle = btn && btn.type === 'checkbox';
  let marker = null;
  // Capture the button's original label so we can restore it after save/error.
  // The button may be a "Set to N" suggestion, "Apply", or "Save"; the old
  // code always rewrote it back to "Save", erasing its identity (P1-35).
  const origLabel = (!isToggle && btn) ? btn.textContent : null;
  if (isToggle) {
    const label = btn.closest('.svc-toggle');
    if (label) {
      marker = document.createElement('div');
      marker.className = 'toggle-saved-overlay';
      marker.textContent = '✓';
      label.appendChild(marker);
      setTimeout(() => marker.remove(), 2000);
    }
  } else if (btn) {
    btn.disabled = true;
    btn.textContent = 'Saving…';
  }
  try {
    const data = await fetchJson(`/api/settings/${host}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (data.error) throw new Error(data.error);
    if (!isToggle && btn) {
      btn.textContent = 'Saved ✓';
      btn.style.background = 'var(--green)';
    }
    // Refresh cached settings
    window.settingsData[host] = await fetchJson(`/api/settings/${host}`);
    if (!isToggle && btn) {
      setTimeout(() => {
        btn.textContent = origLabel != null ? origLabel : 'Save';
        btn.style.background = '';
        btn.disabled = false;
      }, 2000);
    }
  } catch(e) {
    if (isToggle) {
      if (marker) marker.remove();
      const label = btn.closest('.svc-toggle');
      if (label) {
        const err = document.createElement('div');
        err.className = 'toggle-saved-overlay';
        err.textContent = '✕';
        err.style.color = '#f85149';
        label.appendChild(err);
        setTimeout(() => err.remove(), 3000);
      }
    } else if (btn) {
      btn.textContent = 'Error';
      btn.style.background = '#f85149';
      setTimeout(() => {
        btn.textContent = origLabel != null ? origLabel : 'Save';
        btn.style.background = '';
        btn.disabled = false;
      }, 3000);
    }
  }
}

/* ─────────────────────────────────────────
   Data fetching
───────────────────────────────────────── */
async function fetchStatus(host) {
  try {
    const resp = await fetch(`/api/status/${host}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data) return;
    state.statusData[host] = data;
    if (window.updateStationDots) updateStationDots();
    if (state.activeStation === host && state.activeTab === 'settings') {
      const secSA = document.getElementById(`sec-services-sysadmin-${host}`);
      if (secSA) secSA.innerHTML = mkServices(data.services || {});
      // Keep color-capture checkbox in sync without a full re-render
      const ccActive = data.online === false || (data.services || {})['color-capture'] === 'active';
      const ccCheckbox = document.getElementById('cfg-svc-color-capture-svc');
      if (ccCheckbox && ccCheckbox.checked !== ccActive) {
        ccCheckbox.checked = ccActive;
        const ccBlock = ccCheckbox.closest('.svc-block');
        if (ccBlock) ccBlock.style.borderColor = ccActive ? '' : 'rgba(210,153,34,.6)';
        const banner = document.querySelector(`#pane-settings .rovimen-disabled-banner`);
        if (!ccActive && !banner) {
          const msg = document.createElement('div');
          msg.className = 'rovimen-disabled-banner';
          msg.innerHTML = '&#9888; Color capture is disabled — stacking, encoding, detection lock and all dependent processes are not running.';
          document.getElementById('pane-settings').prepend(msg);
        } else if (ccActive && banner) {
          banner.remove();
        }
      }
    }
    // Fetch timelapses lazily
    if (!state.tlData[host]) fetchTimelapses(host);
  } catch(e) { console.warn('fetchStatus failed', e); }
}

async function fetchVitals(host) {
  try {
    const resp = await fetch(`/api/vitals/${host}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data) return;
    state.vitalsData[host] = data;
    _pushVitalsHistory(host, data);
  } catch(e) { console.warn('fetchVitals failed', e); }
}

// One round-trip for all stations' status. Cuts N × RTT (where N = number of
// stations) plus N TCP/TLS connection setups on every poll cycle — the
// difference is most visible from high-latency clients (Romania → Falkenstein
// is ~70 ms, so 8 stations = 560 ms shaved per refresh).
async function fetchAllStatus() {
  try {
    const resp = await fetch('/api/status/all');
    if (!resp.ok) return;
    const all = await resp.json();
    for (const [host, data] of Object.entries(all || {})) {
      if (!data || Object.keys(data).length === 0) continue;
      state.statusData[host] = data;
    }
    if (window.updateStationDots) updateStationDots();
  } catch(e) { console.warn('fetchAllStatus failed', e); }
}

async function fetchAllVitals() {
  try {
    const resp = await fetch('/api/vitals/all');
    if (!resp.ok) return;
    const all = await resp.json();
    for (const [host, data] of Object.entries(all || {})) {
      if (!data || Object.keys(data).length === 0) continue;
      state.vitalsData[host] = data;
      _pushVitalsHistory(host, data);
    }
  } catch(e) { console.warn('fetchAllVitals failed', e); }
}

async function fetchStorageForSysAdmin(host) {
  try {
    const resp = await fetch(`/api/status/${host}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data) return;
    state.statusData[host] = data;
    const el = document.getElementById(`storage-data-${host}`);
    if (el) el.innerHTML = mkStorage(data.storage, data.disk, data.extra_disks);
    window.fetchStoragewatchStatus(host);
  } catch(e) { /* silent */ }
}

async function fetchVitalsFast(host) {
  try {
    const resp = await fetch(`/api/vitals/${host}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data) return;
    state.vitalsData[host] = data;
    _pushVitalsHistory(host, data);
    if (state.activeStation === host && state.activeTab === 'settings') {
      window.drawAllGraphs(host);
    }
  } catch(e) { /* silent */ }
}

function _pushVitalsHistory(host, data) {
  if (!window.vitalsHistory[host]) window.vitalsHistory[host] = [];
  window.vitalsHistory[host].push({
    cpu_pct:         data.cpu_pct         ?? null,
    temp_c:          data.temp_c          ?? null,
    ram_pct:         data.ram_pct         ?? null,
    disk_write_mbps: data.disk_write_mbps ?? null,
  });
  if (window.vitalsHistory[host].length > 150) window.vitalsHistory[host].shift();
  if (data.cores?.length) window.coreData[host] = {
    cores:       data.cores,
    top_procs:   data.top_procs   ?? null,
    total_cores: data.total_cores ?? null,
  };
}

async function fetchTimelapses(host) {
  try {
    const resp = await fetchOnce(`tab:timelapses:${host}`, `/api/timelapses/${host}`);
    if (!resp.ok) return;
    state.tlData[host] = await resp.json();
    if (state.activeStation === host && state.activeTab === 'fdp') {
      renderTimelapses(host);
    }
  } catch(e) {
    if (_isAbort(e)) return;
    console.warn('fetchTimelapses failed', e);
  }
}

async function fetchRMSRefresh(host) {
  // On manual refresh: re-fetch the "All nights" view per camera. The legacy
  // fetchRMSNights / rmsNightSelected pair (single-night drill-down) was removed
  // when the tab moved to window.fetchRMSAllNights; calling them threw ReferenceError
  // and aborted the rest of refreshCurrent().
  const cameras = state.VDB_CAMERAS[host] || [];
  for (const cam of cameras) {
    await window.fetchRMSAllNights(host, cam);
  }
}

async function fetchSettings(host) {
  document.getElementById('pane-settings').innerHTML =
    '<div class="offline">Loading config.json…</div>';
  const [settingsResp, statsResp, hwResp] = await Promise.allSettled([
    fetchOnce(`tab:settings:${host}`,    `/api/settings/${host}`),
    fetchOnce(`tab:encstats:${host}`,    `/api/encoding/stats/${host}`),
    fetchOnce(`tab:hardware:${host}`,    `/api/hardware/${host}`),
  ]);
  // An abort on any of the three is a tab-switch — bail without mutating state.
  if ([settingsResp, statsResp, hwResp].some(
        s => s.status === 'rejected' && _isAbort(s.reason))) {
    return;
  }
  try {
    const r = settingsResp.status === 'fulfilled' ? settingsResp.value : null;
    if (!r || !r.ok) {
      window.settingsData[host] = {__error: r ? 'HTTP '+r.status+(r.status===404?' (mount missing?)':'') : 'fetch failed'};
    } else {
      window.settingsData[host] = await r.json();
    }
  } catch(e) { window.settingsData[host] = {__error: String(e)}; }
  try {
    const r = statsResp.status === 'fulfilled' ? statsResp.value : null;
    if (r && r.ok) window.encStatsData[host] = await r.json();
  } catch(e) {}
  try {
    const r = hwResp.status === 'fulfilled' ? hwResp.value : null;
    if (r && r.ok) window.hwData[host] = await r.json();
  } catch(e) {}
  if (state.activeStation === host && state.activeTab === 'settings') {
    renderSysAdmin(host);
    fetchDawnProgress(host);
  }
}

async function refreshCurrent() {
  const btn  = document.getElementById('refresh-btn');
  const host = state.activeStation;
  btn.textContent = 'Refreshing…';
  btn.disabled    = true;
  try {
    await fetchStatus(host);
    await fetchVitals(host);
    await fetchTimelapses(host);
    if (state.activeTab === 'rms')      await fetchRMSRefresh(host);
    if (state.activeTab === 'settings') await fetchSettings(host);
  } finally {
    btn.textContent = 'Refresh';
    btn.disabled    = false;
  }
}

/* ─────────────────────────────────────────
   Auto-refresh timers
───────────────────────────────────────── */
// Skip polls when the tab isn't visible — saves bandwidth + battery and
// shrinks the noise on the access log. The next visibilitychange event
// triggers a single catch-up refresh in the IIFE bootstrap.
function _docVisible() { return !(document.hidden); }

/* ─────────────────────────────────────────
   Server-Sent Events: status stream
───────────────────────────────────────── */
// Single shared connection — closing+reopening on visibility/reconnect keeps
// this strictly one-per-tab. Tracked at module scope so subscribe/unsubscribe
// helpers can find each other without globals on window.
let _statusES         = null;   // active EventSource, or null
let _statusESLastMsg  = 0;      // performance.now() of last received event
let _statusESDownSince = 0;     // ms timestamp the connection first went bad

// Open one SSE connection. Idempotent: closes any prior one before opening
// a new, so callers (reconnect handler, visibility-resume, bootstrap) don't
// have to bookkeep.
function subscribeToStatusStream() {
  if (!('EventSource' in window)) return false;  // caller will fall back to polling
  closeStatusStream();
  let es;
  try {
    es = new EventSource('/api/events/status');
  } catch (e) {
    console.warn('EventSource open failed', e);
    return false;
  }
  _statusES = es;
  _statusESLastMsg = performance.now();
  _statusESDownSince = 0;

  // Snapshot event: full state.statusData replacement on (re)connect. Mirrors what
  // fetchAllStatus() did at the top of every poll cycle — same downstream
  // call into updateStationDots() so the rest of the UI is none the wiser.
  es.addEventListener('snapshot', (ev) => {
    _statusESLastMsg = performance.now();
    try {
      const all = JSON.parse(ev.data);
      for (const [host, data] of Object.entries(all || {})) {
        if (!data || Object.keys(data).length === 0) continue;
        state.statusData[host] = data;
      }
      if (window.updateStationDots) updateStationDots();
    } catch (e) { console.warn('SSE snapshot parse failed', e); }
  });

  // Incremental status event: one host changed.
  es.addEventListener('status', (ev) => {
    _statusESLastMsg = performance.now();
    try {
      const msg = JSON.parse(ev.data);
      if (msg && msg.host && msg.status) {
        state.statusData[msg.host] = msg.status;
        if (window.updateStationDots) updateStationDots();
      }
    } catch (e) { console.warn('SSE status parse failed', e); }
  });

  // EventSource auto-reconnects on transport errors with its own backoff
  // (3 s default in browsers), so we don't manually re-open here. We DO
  // track when it went down so the watchdog below can trigger a one-shot
  // fetchAllStatus() catch-up if the outage stretches past 2 minutes.
  es.addEventListener('error', () => {
    if (!_statusESDownSince) _statusESDownSince = Date.now();
    // ReadyState 2 (CLOSED) is terminal — browser gave up. Anything else is
    // a transient blip that EventSource is already retrying.
    if (es.readyState === 2) {
      console.warn('SSE connection closed by browser; will be reopened on visibility');
      _statusES = null;
    }
  });
  return true;
}

function closeStatusStream() {
  if (_statusES) {
    try { _statusES.close(); } catch (e) {}
    _statusES = null;
  }
}

// Watchdog: if we haven't heard anything for >2 minutes (heartbeats arrive
// every ~25 s, real events more often), the stream is probably dead behind
// a stale proxy/NAT entry even though EventSource still thinks it's open.
// Force a fetchAllStatus() to catch up and reopen the stream.
function _statusStreamWatchdog() {
  if (!_docVisible() || !_statusES) return;
  const since = performance.now() - _statusESLastMsg;
  if (since > 120000) {
    console.warn('SSE silent for', Math.round(since/1000), 's — forcing reconnect');
    fetchAllStatus();
    subscribeToStatusStream();
  }
}

function startTimers() {
  // Status: prefer SSE push. Polling on /api/status/all is replaced by a
  // server stream; we keep fetchAllStatus() callable for refresh-button +
  // visibility-resume catch-up + watchdog fallback.
  const sseOk = subscribeToStatusStream();
  if (!sseOk) {
    // Browsers without EventSource (very rare — IE/old Edge only) fall back
    // to the previous 60 s aggregated poll. Same code path as before.
    statusTimer = setInterval(() => {
      if (_docVisible()) fetchAllStatus();
    }, 60000);
  } else {
    // Cheap (per-30s) watchdog tick to catch zombie connections.
    statusTimer = setInterval(_statusStreamWatchdog, 30000);
  }
  // Vitals: aggregated, every 60s (was 30s — CPU/temp don't change that fast,
  // and the saved RTT per cycle matters more than a 30 s data freshness gap).
  // SSE intentionally NOT used here — vitals change every second, the delta
  // savings wouldn't beat polling and would add a second long-lived thread on
  // the server with little benefit.
  vitalsTimer = setInterval(() => {
    if (_docVisible()) fetchAllVitals();
  }, 60000);
}

// Catch up immediately when the tab regains focus instead of waiting for the
// next interval tick. Triggers every paused background poller so the user
// doesn't stare at up-to-two-minutes-stale data right after switching back.
document.addEventListener('visibilitychange', () => {
  if (typeof state.STATIONS_META === 'undefined' || !state.STATIONS_META) return;
  if (!_docVisible()) {
    // Close the SSE proactively — browsers don't always tear down EventSource
    // on tab-hide, and we don't want a long-lived idle TCP connection per
    // tab held just to be courteous when the user comes back.
    closeStatusStream();
    return;
  }
  // Resume: kick the catch-up fetch + reopen the stream.
  fetchAllStatus();
  fetchAllVitals();
  if ('EventSource' in window) subscribeToStatusStream();
  if (typeof window.vdbPoll === 'function') {
    try { window.vdbPoll(); } catch (e) { /* not on VideoDB tab — fine */ }
  }
});





/* ─────────────────────────────────────────
   Access control helpers
───────────────────────────────────────── */
function _ownsStation(host) {
  return state.USER_ROLE === 'admin' || (state.USER_ROLE === 'host' && state.USER_STATIONS.includes(host));
}

function _canUseLiveFeed(host) {
  return _ownsStation(host);
}

function canAccessTab(tab, host) {
  if (state.USER_ROLE === 'admin') return true;
  if (tab === 'settings') return _ownsStation(host);
  if (tab === 'rms') return true;
  if (tab === 'fdp') {
    if (state.USER_ROLE === 'host' || state.USER_ROLE === 'visitor') return true;
    const meta = state.STATIONS_META[host];
    return meta && Array.isArray(meta.public_tabs)
      && (meta.public_tabs.includes('timelapse') || meta.public_tabs.includes('fdp'));
  }
  if (state.USER_ROLE === 'host' || state.USER_ROLE === 'visitor') return true;
  // guest
  const meta = state.STATIONS_META[host];
  return meta && Array.isArray(meta.public_tabs) && meta.public_tabs.includes(tab);
}

// Returns a "not public" banner card for content the current user can't access.
function _accessBanner(title) {
  const msg = state.USER_ROLE === 'guest'
    ? 'The station host has not made this content public.'
    : 'This content is not shared with other hosts.';
  return `<div class="card"><div class="card-title">${title}</div>
    <div style="padding:24px 16px;color:var(--muted);font-size:12px;text-align:center;font-style:italic">${msg}</div>
  </div>`;
}

function updateTabVisibility(host) {
  // videodb tab is always visible — content inside is gated per-card via window._accessBanner
  const tabs = ['rms', 'archive', 'settings', 'fdp'];
  for (const tab of tabs) {
    const btn = document.getElementById('tab-btn-' + tab);
    if (!btn) continue;
    const visible = canAccessTab(tab, host);
    btn.style.display = visible ? '' : 'none';
  }
  // If active tab is now hidden, switch to rms
  const activeBtn = document.querySelector('.sub-tab.active');
  if (activeBtn && activeBtn.style.display === 'none') {
    const rmsBtn = document.getElementById('tab-btn-rms');
    if (rmsBtn) switchTab('rms', rmsBtn);
  }
}

/* ─────────────────────────────────────────
   Auth UI
───────────────────────────────────────── */
function renderAuthArea() {
  const el = document.getElementById('auth-area');
  if (!el) return;
  if (state.AUTH_USER) {
    const color = state.IS_ADMIN ? 'var(--green)' : 'var(--blue)';
    const span = document.createElement('span');
    span.style.cssText = `color:${color};font-size:11px`;
    span.textContent = state.AUTH_USER;
    const logoutLink = document.createElement('a');
    logoutLink.href = '/logout';
    logoutLink.className = 'hdr-btn';
    logoutLink.style.cssText = 'font-size:11px;text-decoration:none;padding:3px 10px';
    logoutLink.textContent = 'Logout';
    el.textContent = '';
    el.appendChild(span);
    el.appendChild(document.createTextNode(' '));
    el.appendChild(logoutLink);
  } else {
    el.innerHTML = `<a href="/login" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Login</a>`;
  }
  // Admin Tools dropdown: only for admin
  const adminMenuItem = document.getElementById('logo-dd-admin');
  if (adminMenuItem) adminMenuItem.style.display = state.IS_ADMIN ? '' : 'none';
  const socialMenuItem = document.getElementById('logo-dd-social');
  if (socialMenuItem) socialMenuItem.style.display = state.IS_ADMIN ? '' : 'none';
  // Station switcher: hidden for guests (visibility:hidden preserves layout)
  const stationWrapper = document.getElementById('station-dropdown-wrapper');
  if (stationWrapper) stationWrapper.style.visibility = (state.USER_ROLE === 'guest') ? 'hidden' : '';
  // Tab visibility for current station
  if (state.activeStation) updateTabVisibility(state.activeStation);
}

/* ─────────────────────────────────────────
   Keyboard shortcuts
───────────────────────────────────────── */
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeCamModal(null);
    vdbCloseModal(null);
    stackCloseModal(null);
    appPanelClose(null);
    arcModalClose(null);
    if (typeof window.vdbCloseSyncedModal === 'function') window.vdbCloseSyncedModal();
  }
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    const delta = e.key === 'ArrowLeft' ? -1 : 1;
    if (window.vdbModalIsOpen?.()) {
      e.preventDefault();
      vdbNavModal(delta);
    } else if (document.getElementById('stack-modal').classList.contains('open')) {
      e.preventDefault();
      stackNavModal(delta);
    }
  }
  // [ / ] jump to previous / next station — only when no modal is open
  // and the focus isn't inside an input/textarea/select.
  if (e.key === '[' || e.key === ']') {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    const anyModalOpen = document.querySelector('.cam-modal.open, .app-panel.open, .vm-backdrop.open, .mdm-backdrop.open');
    if (anyModalOpen) return;
    e.preventDefault();
    if (window.selectStationByOffset) window.selectStationByOffset(e.key === '[' ? -1 : 1);
  }
});


// Format a UTC hour-of-day (may be < 0 or > 24) as "HH:MM", wrapping to a day.
export function fmtCaptureUTC(utcHours) {
  const totalMin = Math.round(((utcHours % 24) + 24) % 24 * 60);
  return `${String(Math.floor(totalMin / 60)).padStart(2,'0')}:${String(totalMin % 60).padStart(2,'0')}`;
}

// Pure solar geometry for the RMS capture window. Returns null when the sun
// never crosses the threshold (polar day/night), otherwise the sunset/sunrise
// UTC hours, whether `now` is inside the window, and the formatted label.
//
// Threshold: civil twilight (sun at -6° elevation). RMS does not gate capture
// on nautical twilight — the earlier -12° value here came from misreading
// `cf3` in the station config (a sky-conditions flag, not a start elevation).
// Civil twilight matches observed camera start times, which fire 25-35 min
// earlier than the -12° model predicted at Romanian latitudes (#495).
export function computeCaptureWindow(lat, lon, now = new Date()) {
  if (lat == null || lon == null) return null;
  const dayOfYear = Math.floor((now - new Date(Date.UTC(now.getUTCFullYear(), 0, 1))) / 86400000) + 1;
  const declRad = 23.45 * Math.PI / 180 * Math.sin((360 / 365 * (dayOfYear - 81)) * Math.PI / 180);
  const latRad = lat * Math.PI / 180;
  const elevRad = -6 * Math.PI / 180; // civil twilight (approximation of RMS start)
  const cosH = (Math.sin(elevRad) - Math.sin(latRad) * Math.sin(declRad)) / (Math.cos(latRad) * Math.cos(declRad));
  if (Math.abs(cosH) > 1) return null; // sun never reaches the threshold today
  const H = Math.acos(cosH) * 180 / Math.PI;
  const solarNoonUTC = 12 - lon / 15;
  const sunsetUTC  = solarNoonUTC + H / 15;
  const sunriseUTC = solarNoonUTC - H / 15;

  // Capture runs from evening sunset to next-morning sunrise, wrapping
  // midnight. Wrap all values into [0,24) and test membership accordingly.
  const wrap = h => ((h % 24) + 24) % 24;
  const nowH = now.getUTCHours() + now.getUTCMinutes() / 60 + now.getUTCSeconds() / 3600;
  const setW = wrap(sunsetUTC);
  const riseW = wrap(sunriseUTC);
  const active = setW > riseW
    ? (nowH >= setW || nowH < riseW)   // window wraps midnight (normal night)
    : (nowH >= setW && nowH < riseW);  // window within a single UTC day

  const label = active
    ? `<span class="hdr-clock-label">Capture window active &middot; ends</span> <span class="hdr-clock">${fmtCaptureUTC(sunriseUTC)} UTC</span>`
    : `<span class="hdr-clock-label">Next RMS capture window:</span> <span class="hdr-clock">${fmtCaptureUTC(sunsetUTC)} &rarr; ${fmtCaptureUTC(sunriseUTC)} UTC</span>`;

  return { sunsetUTC, sunriseUTC, active, label };
}

function updateNextCapture(lat, lon) {
  const el = document.getElementById('next-capture');
  if (!el) return;
  const win = computeCaptureWindow(lat, lon);
  if (!win) { el.style.display = 'none'; return; }
  el.innerHTML = win.label;
  el.style.display = '';
}

/* ─────────────────────────────────────────
   Init
───────────────────────────────────────── */
(async function init() {
  // Clamp any date pickers to today (UTC) — captured nights can't be in the future.
  clampDateInputsToToday();
  // Fetch auth status and station config in parallel
  try {
    const [stationsResp, authResp] = await Promise.all([
      fetch('/api/stations'),
      fetch('/api/auth/status'),
    ]);
    state.STATIONS_META = await stationsResp.json();
    const authData = await authResp.json();
    state.IS_ADMIN      = authData.admin || false;
    state.AUTH_USER     = authData.user || null;
    state.USER_ROLE     = authData.role || 'guest';
    state.USER_STATIONS = authData.stations || [];
  } catch (e) {
    document.getElementById('pane-rms').innerHTML =
      '<div class="offline">Failed to load station config</div>';
    return;
  }

  renderAuthArea();

  // Derive state.VDB_CAMERAS from config
  for (const [host, meta] of Object.entries(state.STATIONS_META)) {
    state.VDB_CAMERAS[host] = meta.cameras.map(c => c.code);
  }

  // Build station dropdown dynamically
  const wrapper = document.getElementById('station-dropdown-wrapper');
  const hosts = Object.keys(state.STATIONS_META);

  // Resolve active station from URL path (/station/<host>/<tab>) or DEFAULT_STATION
  const _pathParts = window.location.pathname.split('/').filter(Boolean);
  const _urlStation = _pathParts[0] === 'station' ? (_pathParts[1] || '') : '';
  const _urlTab     = _pathParts[0] === 'station' ? (_pathParts[2] || '') : '';
  const _urlCam     = new URLSearchParams(window.location.search).get('cam') || '';
  const _urlDate    = new URLSearchParams(window.location.search).get('date') || '';

  // If the URL points to a station the user can't access, show a clear message
  if (_urlStation && !hosts.includes(_urlStation)) {
    const _safeStation = document.createElement('span');
    _safeStation.textContent = _urlStation;
    const _escapedStation = _safeStation.innerHTML;
    const msg = state.USER_ROLE === 'host'
      ? `Your account does not have access to station <b>${_escapedStation}</b>. Contact an admin to request access.`
      : `Station <b>${_escapedStation}</b> was not found.`;
    wrapper.textContent = _urlStation;
    document.querySelectorAll('.tab-pane').forEach(p => {
      p.classList.add('active');
      p.innerHTML = `<div class="offline" style="margin-top:2em">${msg}</div>`;
    });
    document.querySelectorAll('.sub-tab-bar').forEach(b => b.style.display = 'none');
    return;
  }
  if (!hosts.length) {
    wrapper.innerHTML = '';
    document.querySelectorAll('.tab-pane').forEach(p => {
      p.classList.add('active');
      p.innerHTML = '<div class="offline" style="margin-top:2em">No stations available for your account.</div>';
    });
    document.querySelectorAll('.sub-tab-bar').forEach(b => b.style.display = 'none');
    return;
  }

  state.activeStation = (hosts.includes(_urlStation) ? _urlStation : null)
    || (window.DEFAULT_STATION && hosts.includes(window.DEFAULT_STATION) ? window.DEFAULT_STATION : null)
    || hosts[0];

  function _stationDot(host) {
    const d = state.statusData[host];
    const cls = !d ? '' : (d.online ? 'dot-up' : 'dot-down');
    const lbl = !d ? 'status unknown' : (d.online ? 'station online' : 'station offline');
    return `<span class="pill-dot ${cls}" id="dot-${host}" role="img" aria-label="${host} ${lbl}"></span>`;
  }

  window.renderStationDropdown = function renderStationDropdown() {
    const meta = state.STATIONS_META[state.activeStation];
    const idx = hosts.indexOf(state.activeStation);
    const prevDisabled = idx <= 0 ? 'disabled' : '';
    const nextDisabled = idx >= hosts.length - 1 ? 'disabled' : '';
    wrapper.innerHTML = `
      <button class="station-nav-btn" id="station-prev-btn" ${prevDisabled}
              title="Previous station ([)"
              onclick="selectStationByOffset(-1)">&#8592;</button>
      <div class="station-dropdown" id="station-dropdown">
        <div class="station-dropdown-selected" onclick="toggleStationDropdown(event)">
          ${_stationDot(state.activeStation)}
          <span id="station-dd-current">${escHtml(state.activeStation)}</span>
          <span class="station-dd-label" id="station-dd-current-label">${meta ? escHtml(meta.label) : ''}</span>
          <span class="station-dropdown-arrow">&#9660;</span>
        </div>
        <div class="station-dropdown-menu" id="station-dropdown-menu" onclick="event.stopPropagation()">
          ${hosts.map(h => {
            const m = state.STATIONS_META[h];
            return `<div class="station-option${h === state.activeStation ? ' active' : ''}" onclick="selectStationFromDropdown('${escHtml(h)}')">
              ${_stationDot(h)}
              ${escHtml(h)} <span class="station-dd-label">${m ? escHtml(m.label) : ''}</span>
            </div>`;
          }).join('')}
        </div>
      </div>
      <button class="station-nav-btn" id="station-next-btn" ${nextDisabled}
              title="Next station (])"
              onclick="selectStationByOffset(1)">&#8594;</button>`;
  }

  window.selectStationByOffset = function(delta) {
    const idx = hosts.indexOf(state.activeStation);
    const target = hosts[idx + delta];
    if (target) selectStation(target);
  };

  renderStationDropdown();
  const _initMeta = state.STATIONS_META[state.activeStation];
  if (_initMeta) updateNextCapture(_initMeta.lat, _initMeta.lon);
  updateTabVisibility(state.activeStation);

  window.toggleStationDropdown = function(e) {
    e.stopPropagation();
    document.getElementById('station-dropdown').classList.toggle('open');
  };
  document.addEventListener('click', () => {
    const dd = document.getElementById('station-dropdown');
    if (dd) dd.classList.remove('open');
  });

  window.selectStationFromDropdown = function(host) {
    document.getElementById('station-dropdown').classList.remove('open');
    selectStation(host);
  };

  window.updateStationDots = function() {
    for (const host of hosts) {
      const dot = document.getElementById('dot-' + host);
      if (!dot) continue;
      const d = state.statusData[host];
      dot.className = 'pill-dot' + (!d ? '' : (d.online ? ' dot-up' : ' dot-down'));
      const lbl = !d ? 'status unknown' : (d.online ? 'station online' : 'station offline');
      dot.setAttribute('aria-label', host + ' ' + lbl);
    }
  };

  // Restore tab from URL path, or default to rms
  const _startTab = (_urlTab && document.getElementById('tab-btn-' + _urlTab)) ? _urlTab : 'rms';
  const _startBtn = document.getElementById('tab-btn-' + _startTab);
  // Pre-select camera from ?cam= query param, and arm the one-shot scroll
  // target if ?date= is also present so the user lands on the right night
  // when arriving from a deep link.
  if (_urlCam && _startTab === 'rms') {
    const cams = state.VDB_CAMERAS[state.activeStation] || [];
    if (cams.includes(_urlCam)) {
      window.rmsState = { station: state.activeStation, selectedCamera: _urlCam };
      if (/^\d{8}$/.test(_urlDate)) window._rmsPendingScrollDate = _urlDate;
    }
  }
  // Replace the current history entry with a clean state (no extra push)
  history.replaceState({ station: state.activeStation, tab: _startTab }, '', `/station/${state.activeStation}/${_startTab}`);
  window.vdbLoadAzimuths?.(state.activeStation);
  switchTab(_startTab, _startBtn, { push: false });

  // Back/forward navigation
  window.addEventListener('popstate', e => {
    const s = e.state;
    if (!s) return;
    if (s.station && hosts.includes(s.station) && s.station !== state.activeStation) {
      state.activeStation = s.station;
      renderStationDropdown();
      window._vdbAzimuths = {};
      window.vdbLoadAzimuths?.(s.station);
      if (dawnProgressTimer)   { clearInterval(dawnProgressTimer);   dawnProgressTimer   = null; }
      if (sysadminVitalsTimer) { clearInterval(sysadminVitalsTimer); sysadminVitalsTimer = null; }
      if (sysadminStorageTimer){ clearInterval(sysadminStorageTimer);sysadminStorageTimer= null; }
    }
    if (s.tab && s.tab !== state.activeTab) {
      const tabBtn = document.getElementById('tab-btn-' + s.tab);
      if (tabBtn) switchTab(s.tab, tabBtn, { push: false });
    }
  });

  // Initial fetch for all stations (one round-trip each instead of N).
  fetchAllStatus();
  fetchAllVitals();
  startTimers();

  // Retry until we have data for the active station
  (function poll() {
    if (!state.statusData[state.activeStation]) {
      setTimeout(() => { fetchStatus(state.activeStation); poll(); }, 4000);
    }
  })();
})();

// Expose to global scope for inline onclick handlers
window.switchTab = switchTab;
window.refreshCurrent = refreshCurrent;
window.closeCamModal = closeCamModal;
window.stackCloseModal = stackCloseModal;
window.stackNavModal = stackNavModal;
window.toggleCamPanel = toggleCamPanel;
window.toggleCamLive = toggleCamLive;
window.expandCam = expandCam;
window.toggleSvcPanel = toggleSvcPanel;
window.saveSettings = saveSettings;
window.saveRotation = saveRotation;
window.saveOverlay = saveOverlay;
window.previewOverlay = previewOverlay;
window.logoUpload = logoUpload;
window.logoRefreshList = logoRefreshList;
window.rebootStation = rebootStation;
window.rovimenControl = rovimenControl;
window.checkUpdater = checkUpdater;
window.runUpdater = runUpdater;
window.runDawn = runDawn;
window.dawnNavDate = dawnNavDate;
// rmsStatusNavDate is in dashboard-sysadmin.js
window.testArchive = testArchive;
window.refreshSysAdmin = refreshSysAdmin;
window.switchSysAdminTab = switchSysAdminTab;
window.stepSpinner = stepSpinner;
window.fdpOpenRmsPlot = fdpOpenRmsPlot;
window.applySuggestion = applySuggestion;
window.vdbSkyDomeOpenModal = vdbSkyDomeOpenModal;
window.stackOpenModal = stackOpenModal;
window.canAccessTab = canAccessTab;
window.fetchOnce = fetchOnce;
window.renderTimelapses = renderTimelapses;
window.askConfirm = askConfirm;
window.fetchStatus = fetchStatus;
window._accessBanner = _accessBanner;
window._lastNight = _lastNight;
window._isAbort = _isAbort;
window.fdpRenderDomeTimelapse = fdpRenderDomeTimelapse;
window.liveStreamError = liveStreamError;
window.liveStreamLoaded = liveStreamLoaded;
window.logoFileChosen = logoFileChosen;
window.onTlNightChange = onTlNightChange;
window.saveDataVisibility = saveDataVisibility;
window.toggleColorCapture = toggleColorCapture;
window.updateEncoderLabels = updateEncoderLabels;
window.vdbSkyDomeOnError = vdbSkyDomeOnError;
