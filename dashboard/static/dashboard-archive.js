import { fmtBytes, fmtDate, parseFn, _modalOpen, _modalClose, _closeAllOpenModals, escHtml, state } from './dashboard-common.js';
import { VideoModal } from './dashboard-video-modal.js';
/*
 * dashboard-archive.js
 * ────────────────────
 * Archive tab — permanent storage box browsing (recordings + nightly timelapse), archive video modal. Loaded by dashboard.html before dashboard-station.js.
 *
 * Depends on dashboard-common.js (escHtml, fmtBytes, fmtDate, parseFn, state.STATIONS_META,
 * state.VDB_CAMERAS, state.statusData, state.vitalsData, state.tlData, state.activeStation, state.activeTab, state.IS_ADMIN,
 * state.AUTH_USER, state.USER_ROLE, state.USER_STATIONS, _modalOpen, _modalClose) and lexical bindings
 * declared at the top level of dashboard-station.js (classic non-module scripts share
 * the global lexical environment, so top-level `let`/`const` are visible across bundles).
 */

/* ─────────────────────────────────────────
   Archive tab
───────────────────────────────────────── */
let archiveState = { station: null, selectedCamera: null, data: {} }; // data: {camera: [{date, meteors, timelapse}]}
const _arcCardData = new Map(); // keyed by filename → { stackUrl, cam, meteor_time }

const NIGHTS_PER_PAGE = 7;

let _archiveImgObserver = null;
let _archiveSentinelObserver = null;
let _archiveRenderedCount = 0;

function _archiveCamerasFor(host) {
  // Prefer the live station settings when available (lets newly-added RMS
  // cams appear without a dashboard-config edit), but fall back to the
  // dashboard's state.STATIONS_META — which is the source of truth for which
  // cameras the dashboard knows about, and is always loaded. SSHFS archive
  // data is keyed only by camera code, so the station being offline must
  // not blank out the Archive tab.
  const fromSettings = Object.keys((window.settingsData[host] || {}).stations || {});
  if (fromSettings.length) return fromSettings;
  const meta = (typeof state.STATIONS_META !== 'undefined' && state.STATIONS_META[host]) || null;
  return meta && Array.isArray(meta.cameras) ? meta.cameras.map(c => c.code) : [];
}

function _prefetchOtherCameras(cameras, currentCam) {
  for (const c of cameras) {
    if (c === currentCam || archiveState.data[c]) continue;
    fetch(`/api/archive/nights_full/${c}`)
      .then(r => r.ok ? r.json() : [])
      .then(d => { archiveState.data[c] = d; })
      .catch(() => {});
  }
}

async function archiveInit() {
  const host = state.activeStation;
  const el = document.getElementById('pane-archive');

  // Kick off a settings fetch in the background — if it arrives we'll
  // pick up any RMS-side cameras the dashboard config doesn't know about
  // on the next archiveInit pass — but don't gate render on it.
  if (!window.settingsData[host]) {
    fetch(`/api/settings/${host}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) window.settingsData[host] = d; })
      .catch(() => {});
  }

  const cameras = _archiveCamerasFor(host);
  if (!cameras.length) {
    el.innerHTML = '<div class="card"><div class="card-title">Archive</div><div class="no-data">No cameras configured for this station.</div></div>';
    return;
  }

  // Reset if station changed
  if (archiveState.station !== host) {
    archiveState = { station: host, selectedCamera: cameras[0], data: {} };
  }
  if (!archiveState.selectedCamera) archiveState.selectedCamera = cameras[0];

  const cam = archiveState.selectedCamera;

  // Already loaded for this camera
  if (archiveState.data[cam]) { archiveRender(cameras); _prefetchOtherCameras(cameras, cam); return; }

  el.innerHTML = '<div class="offline">Loading archive…</div>';
  try {
    const r = await fetch(`/api/archive/nights_full/${cam}`);
    archiveState.data[cam] = r.ok ? await r.json() : [];
    archiveRender(cameras);
    _prefetchOtherCameras(cameras, cam);
  } catch(e) {
    el.innerHTML = `<div class="offline">Archive unavailable — ${escHtml(e?.message || e)}</div>`;
  }
}

async function archiveJumpToDate(cam, iso) {
  if (!iso) return;
  const date = iso.replace(/-/g, '');  // YYYY-MM-DD → YYYYMMDD
  // Ensure the night is rendered before scrolling
  const nights = (archiveState.data[cam] || []).filter(n => n.meteors.length || n.timelapse);
  const idx = nights.findIndex(n => n.date === date);
  if (idx >= 0 && idx >= _archiveRenderedCount) {
    _archiveAppendNights(cam, nights, idx + 1 - _archiveRenderedCount);
  }
  requestAnimationFrame(() => {
    const sec = document.getElementById(`archive-night-sec-${cam}-${date}`);
    if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

async function archiveSelectCamera(cam) {
  // Close any clip/plot modal left open from the previous camera —
  // tapping a different camera in the archive list shouldn't leave
  // a stale playback on top.
  _closeAllOpenModals();
  archiveState.selectedCamera = cam;
  const el = document.getElementById('pane-archive');
  const cameras = _archiveCamerasFor(state.activeStation);
  if (!archiveState.data[cam]) {
    el.innerHTML = '<div class="offline">Loading…</div>';
    try {
      const r = await fetch(`/api/archive/nights_full/${cam}`);
      archiveState.data[cam] = r.ok ? await r.json() : [];
    } catch(e) {
      archiveState.data[cam] = [];
    }
  }
  archiveRender(cameras);
  _prefetchOtherCameras(cameras, cam);
}

function _buildNightSection(cam, night, idx) {
  const sep = idx > 0 ? '<hr style="border:none;border-top:1px solid var(--border);margin:14px 0">' : '';
  const dateLabel = `<div style="font-size:13px;font-weight:600;color:var(--text);margin-bottom:8px">${fmtDate(night.date)}</div>`;

  const allCards = [];
  const isFirstPage = idx < NIGHTS_PER_PAGE;

  if (night.timelapse) {
    const tlUrl = `/api/archive/file/${cam}/${night.date}/timelapse/${encodeURIComponent(night.timelapse)}`;
    const tlStackUrl = night.timelapse_stack
      ? `/api/archive/file/${cam}/${night.date}/timelapse/${encodeURIComponent(night.timelapse_stack)}`
      : null;
    const tlImgHtml = tlStackUrl
      ? `<img data-src="${tlStackUrl}" decoding="async" alt="timelapse"${isFirstPage ? ' fetchpriority="high"' : ''} onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
      : '';
    allCards.push(`<div class="vdb-chunk-card" style="border:2px solid #7c3aed;position:relative"
        onclick="archivePlayVideo('${tlUrl}','${night.timelapse}',0)">
      <span style="position:absolute;top:4px;left:4px;background:#7c3aed;color:#fff;font-size:9px;font-weight:700;padding:2px 5px;border-radius:3px;z-index:1;letter-spacing:0.5px">TIMELAPSE</span>
      ${tlImgHtml}
      <div class="vdb-chunk-nostack" style="${tlStackUrl?'display:none':''}">&#9654;</div>
      <div class="vdb-chunk-meta">
        <div class="vdb-chunk-time">${fmtDate(night.date)}</div>
        ${night.timelapse_size_mb != null ? `<div class="vdb-chunk-size">${night.timelapse_size_mb} MB</div>` : ''}
        <a class="vdb-dl-btn" href="${tlUrl}" download onclick="event.stopPropagation()">&#8595; Download</a>
      </div>
    </div>`);
  }

  night.meteors.forEach(m => {
    const stackUrl = m.stack ? `/api/archive/file/${cam}/${night.date}/${m.stack_subdir || 'meteors'}/${encodeURIComponent(m.stack)}` : null;
    const videoUrl = `/api/archive/file/${cam}/${night.date}/meteors/${encodeURIComponent(m.filename)}`;
    _arcCardData.set(m.filename, { stackUrl, cam, date: night.date, meteor_time: m.meteor_time || null });
    const imgHtml = stackUrl
      ? `<img data-src="${stackUrl}" decoding="async" alt="${escHtml(m.filename)}"${isFirstPage ? ' fetchpriority="high"' : ''} onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
      : '';
    const ts = m.filename.match(/_(\d{6})_/)?.[1] || '';
    const timeStr = ts ? `${ts.slice(0,2)}:${ts.slice(2,4)}:${ts.slice(4,6)}` : m.filename;
    allCards.push(`<div class="vdb-chunk-card vdb-chunk-locked-detection"
        onclick="archivePlayVideo('${videoUrl}','${m.filename}',${m.size_mb ?? 0},${m.detection_offset_s ?? 'null'})">
      <button class="vdb-lock-btn vdb-lock-btn--detection" title="Archived" disabled>&#128451;</button>
      ${imgHtml}
      <div class="vdb-chunk-nostack" style="${stackUrl?'display:none':''}">&#9654;</div>
      <div class="vdb-chunk-meta">
        <div class="vdb-chunk-time">${timeStr}</div>
        ${m.size_mb != null ? `<div class="vdb-chunk-size">${m.size_mb} MB</div>` : ''}
        <a class="vdb-dl-btn" href="${videoUrl}" download onclick="event.stopPropagation()">&#8595; Download</a>
      </div>
    </div>`);
  });

  const countLabel = [
    night.meteors.length ? `${night.meteors.length} meteor clip${night.meteors.length!==1?'s':''}` : '',
    night.timelapse ? '1 timelapse' : '',
  ].filter(Boolean).join(' · ');

  const grid = `<div style="font-size:11px;color:var(--muted);margin-bottom:6px">${countLabel}</div>
    <div class="vdb-chunk-grid" style="grid-template-columns:repeat(auto-fill,minmax(180px,1fr))">${allCards.join('')}</div>`;

  return `<div id="archive-night-sec-${cam}-${night.date}" style="scroll-margin-top:80px">${sep}${dateLabel}${grid}</div>`;
}

function _archiveObserveImages(container) {
  if (!_archiveImgObserver) {
    _archiveImgObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          const img = entry.target;
          if (img.dataset.src) {
            img.src = img.dataset.src;
            delete img.dataset.src;
          }
          _archiveImgObserver.unobserve(img);
        }
      }
    }, { rootMargin: '400px 0px' });
  }
  const imgs = container.querySelectorAll('img[data-src]');
  for (const img of imgs) _archiveImgObserver.observe(img);
}

function _archiveAppendNights(cam, visibleNights, count) {
  const container = document.getElementById('archive-nights-container');
  if (!container) return;
  const end = Math.min(_archiveRenderedCount + count, visibleNights.length);
  for (let i = _archiveRenderedCount; i < end; i++) {
    const div = document.createElement('div');
    div.innerHTML = _buildNightSection(cam, visibleNights[i], i);
    container.appendChild(div.firstElementChild);
  }
  _archiveRenderedCount = end;
  _archiveObserveImages(container);

  const sentinel = document.getElementById('archive-load-sentinel');
  if (sentinel) {
    if (_archiveRenderedCount >= visibleNights.length) {
      sentinel.remove();
      if (_archiveSentinelObserver) { _archiveSentinelObserver.disconnect(); _archiveSentinelObserver = null; }
    }
  }
}

function archiveRender(cameras) {
  const el = document.getElementById('pane-archive');
  if (!cameras) cameras = Object.keys(archiveState.data);
  const cam = archiveState.selectedCamera || cameras[0];

  // Tear down previous observers
  if (_archiveImgObserver) { _archiveImgObserver.disconnect(); _archiveImgObserver = null; }
  if (_archiveSentinelObserver) { _archiveSentinelObserver.disconnect(); _archiveSentinelObserver = null; }
  _archiveRenderedCount = 0;

  const camBtns = cameras.map(c =>
    `<button class="cam-btn${c === cam ? ' active' : ''}" onclick="archiveSelectCamera('${c}')">${c}</button>`
  ).join('');
  const selector = `<div class="card" style="padding:10px 14px">
    <div class="cam-btns">${camBtns}</div>
  </div>`;

  const nights = archiveState.data[cam] || [];
  const totalMeteors = nights.reduce((s, n) => s + n.meteors.length, 0);
  const totalNights = nights.filter(n => n.meteors.length || n.timelapse).length;
  const visibleNightsList = nights.filter(n => n.meteors.length || n.timelapse).map(n => n.date);
  const minDate = visibleNightsList.length ? visibleNightsList[visibleNightsList.length-1] : '';
  const maxDate = visibleNightsList.length ? visibleNightsList[0] : '';
  const toIso = d => d ? `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}` : '';
  const camE = escHtml(cam);
  const header = `<div class="card-title" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <span>${camE}
      <span style="font-size:11px;color:var(--muted);font-weight:400;margin-left:8px">${totalNights} night${totalNights!==1?'s':''} · ${totalMeteors} clip${totalMeteors!==1?'s':''}</span>
    </span>
    ${visibleNightsList.length ? `
      <label style="font-size:11px;color:var(--muted);font-weight:400;display:flex;align-items:center;gap:6px;margin-left:auto">
        Jump to date
        <input type="date" id="archive-date-picker-${camE}"
               min="${escHtml(toIso(minDate))}" max="${escHtml(toIso(maxDate))}"
               onchange="archiveJumpToDate('${camE}', this.value)"
               style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:3px 6px;font-size:12px;font-family:inherit">
      </label>` : ''}
  </div>`;

  const visibleNights = nights.filter(n => n.meteors.length || n.timelapse);
  if (!visibleNights.length) {
    el.innerHTML = selector + `<div class="card">${header}<div class="no-data">No archived data for this camera.</div></div>`;
    return;
  }

  const initialCount = Math.min(NIGHTS_PER_PAGE, visibleNights.length);
  const initialSections = [];
  for (let i = 0; i < initialCount; i++) {
    initialSections.push(_buildNightSection(cam, visibleNights[i], i));
  }
  _archiveRenderedCount = initialCount;

  const sentinelHtml = visibleNights.length > initialCount
    ? '<div id="archive-load-sentinel" style="height:1px"></div>'
    : '';

  el.innerHTML = selector + `<div class="card">${header}<div id="archive-nights-container">${initialSections.join('')}</div>${sentinelHtml}</div>`;

  _archiveObserveImages(el);

  const sentinel = document.getElementById('archive-load-sentinel');
  if (sentinel) {
    _archiveSentinelObserver = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) {
        _archiveAppendNights(cam, visibleNights, NIGHTS_PER_PAGE);
      }
    }, { rootMargin: '600px 0px' });
    _archiveSentinelObserver.observe(sentinel);
  }
}

/* ── Archive modal ─────────────────────────────────────────────────────── */
const _arcModal = new VideoModal(
  document.getElementById('arc-modal-container'),
  { trim: true, nav: false }
);
// Register global close-all handler (set once; may already be set by overview.js)
if (!window._VideoModalCloseAll) window._VideoModalCloseAll = () => VideoModal.closeAll();

async function archivePlayVideo(url, filename, sizeMb, detOffset) {
  const extra = _arcCardData.get(filename) || {};
  const ts = filename.match(/_(\d{6})_/)?.[1] || '';
  const timeStr = ts ? `${ts.slice(0,2)}:${ts.slice(2,4)}:${ts.slice(4,6)} UTC` : filename;
  const title = [extra.cam, timeStr].filter(Boolean).join(' · ');

  // Fetch RMS detections for this night and match by meteor_time (truncated to second)
  let detection = extra.meteor_time ? { time_utc: extra.meteor_time, camera: extra.cam } : null;
  if (extra.cam && extra.date && extra.meteor_time) {
    const meteortSec = extra.meteor_time.slice(0, 19); // "2026-06-09T19:53:14"
    try {
      const r = await fetch(`/api/archive/rms-detections/${extra.cam}/${extra.date}`);
      if (r.ok) {
        const data = await r.json();
        const match = data.detections?.find(d => d.time_utc === meteortSec);
        if (match) detection = { ...match, camera: extra.cam };
      }
    } catch (_) {}
  }

  _arcModal.open({
    src:       url,
    title:     title || filename,
    camera:    extra.cam   || '',
    date:      extra.date  || '',
    filename,
    loopDlPath: extra.cam && extra.date
      ? `/loop_clip_archive/${extra.cam}/${extra.date}/${encodeURIComponent(filename)}`
      : null,
    detOffset: detOffset ?? null,
    download:  {
      onClick: async () => {
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
      },
    },
    stack:     extra.stackUrl ? { url: extra.stackUrl } : null,
    detection,
  });
}

function arcModalClose(e) {
  // Legacy inline onclick handler — keep for any remaining callers
  if (e && e.target && !e.target.closest('.vm-backdrop')) return;
  _arcModal.close();
}

// Expose to global scope for callers in dashboard-station.js
window.arcModalClose    = arcModalClose;
window.archivePlayVideo = archivePlayVideo;
window.archiveSelectCamera = archiveSelectCamera;
window.archiveInit = archiveInit;
window.archiveJumpToDate = archiveJumpToDate;
